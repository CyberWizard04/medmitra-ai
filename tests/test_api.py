from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from medmitra import api, voice
from medmitra.limits import Budget
from medmitra.reminder import Action
from medmitra.script import (
    FIXED_LINES, SPEECH, reminder_intro_text, reminder_lines,
)

client = TestClient(api.app)
MED_NAME = "Vitamin C"
MED_LOOK = "small round orange tablet"


def blocked(*args, **kwargs):
    raise AssertionError("a test tried to use the network")


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    api.WORKSPACES.clear()
    api.AUDIO_CACHE.clear()
    monkeypatch.setattr(api, "TURN_BUDGET", Budget(1000))
    monkeypatch.setattr(api, "TTS_BUDGET", Budget(1000))
    monkeypatch.setattr(voice, "tts", lambda text: b"RIFF-fake-wav")
    monkeypatch.setattr(voice, "_post", blocked)
    monkeypatch.setattr("medmitra.brain._post_groq", blocked)


def fake_label(label, source="groq"):
    return lambda text, question=None: (label, source)


def new_workspace():
    data = client.post("/api/w").json()
    return data["id"], data["state"]


def add_medicine(wid, name=MED_NAME, look=MED_LOOK):
    response = client.post(f"/api/w/{wid}/medicines", json={"name": name, "appearance": look})
    assert response.status_code == 200, response.text
    return response.json()["medicine"]["id"]


def schedule(wid, medicine_id, minutes=0):
    response = client.post(f"/api/w/{wid}/doses",
                           json={"medicine_id": medicine_id, "in_minutes": minutes})
    assert response.status_code == 200, response.text
    return response.json()["dose_id"]


def ringing_dose():
    wid, _ = new_workspace()
    medicine_id = add_medicine(wid)
    dose_id = schedule(wid, medicine_id, 0)
    state = client.get(f"/api/w/{wid}/state").json()
    assert state["ringing"]["dose_id"] == dose_id
    return wid, medicine_id, dose_id


def in_call():
    wid, medicine_id, dose_id = ringing_dose()
    response = client.post(f"/api/w/{wid}/doses/{dose_id}/answer")
    assert response.status_code == 200, response.text
    return wid, medicine_id, dose_id, response.json()


def send_audio(wid, dose_id, data=b"fake-webm-bytes"):
    return client.post(f"/api/w/{wid}/doses/{dose_id}/audio",
                       files={"file": ("reply.webm", data, "audio/webm")})


def spoken(wid, dose_id, data=b"fake-webm-bytes"):
    response = send_audio(wid, dose_id, data)
    assert response.status_code == 200, response.text
    return response.json()


# ---------- workspaces ----------

def test_health():
    assert client.get("/health").json() == {"status": "ok"}


def test_new_workspace_starts_with_labelled_sample_history():
    wid, state = new_workspace()
    assert wid
    assert state["has_sample_data"] is True
    assert state["summary"]["totals"]["total"] == 14
    assert state["ringing"] is None
    assert len(state["medicines"]) == 2


def test_unknown_workspace_is_404():
    assert client.get("/api/w/nope/state").status_code == 404
    assert client.post("/api/w/nope/medicines", json={"name": "A"}).status_code == 404


def test_workspaces_are_private_to_each_visitor():
    first, _ = new_workspace()
    second, _ = new_workspace()
    add_medicine(first)
    assert len(client.get(f"/api/w/{first}/state").json()["medicines"]) == 3
    assert len(client.get(f"/api/w/{second}/state").json()["medicines"]) == 2


def test_least_recently_used_workspace_is_forgotten(monkeypatch):
    monkeypatch.setattr(api, "MAX_WORKSPACES", 2)
    a, _ = new_workspace()
    b, _ = new_workspace()
    client.get(f"/api/w/{a}/state")
    c, _ = new_workspace()
    assert client.get(f"/api/w/{a}/state").status_code == 200
    assert client.get(f"/api/w/{c}/state").status_code == 200
    assert client.get(f"/api/w/{b}/state").status_code == 404


def test_reset_restores_the_sample_data_and_keeps_the_link():
    wid, _ = new_workspace()
    add_medicine(wid)
    state = client.post(f"/api/w/{wid}/reset").json()
    assert len(state["medicines"]) == 2
    assert state["summary"]["totals"]["total"] == 14
    assert client.get(f"/api/w/{wid}/state").status_code == 200


# ---------- caretaker ----------

def test_add_medicine_checks_input():
    wid, _ = new_workspace()
    good = client.post(f"/api/w/{wid}/medicines",
                       json={"name": " Vitamin  C ", "appearance": "round"})
    assert good.status_code == 200
    assert good.json()["medicine"]["name"] == "Vitamin C"
    assert client.post(f"/api/w/{wid}/medicines", json={"name": "   "}).status_code == 400
    assert client.post(f"/api/w/{wid}/medicines", json={"name": "x" * 61}).status_code == 400


def test_scheduling_needs_a_time_and_a_real_medicine():
    wid, _ = new_workspace()
    medicine_id = add_medicine(wid)
    assert client.post(f"/api/w/{wid}/doses",
                       json={"medicine_id": medicine_id}).status_code == 400
    assert client.post(f"/api/w/{wid}/doses",
                       json={"medicine_id": "nope", "in_minutes": 5}).status_code == 404
    naive = client.post(f"/api/w/{wid}/doses",
                        json={"medicine_id": medicine_id, "due_at": "2026-11-07T10:00:00"})
    assert naive.status_code == 400
    aware = client.post(f"/api/w/{wid}/doses",
                        json={"medicine_id": medicine_id, "due_at": "2026-11-07T10:00:00+05:30"})
    assert aware.status_code == 200


def test_dose_for_later_does_not_ring():
    wid, _ = new_workspace()
    schedule(wid, add_medicine(wid), 30)
    state = client.get(f"/api/w/{wid}/state").json()
    assert state["ringing"] is None
    assert state["doses"][0]["status"] == "scheduled"


def test_due_dose_rings_when_the_page_polls():
    ringing_dose()


def test_unanswered_ring_counts_as_no_answer(monkeypatch):
    wid, _, dose_id = ringing_dose()
    real_now = api._now
    monkeypatch.setattr(api, "_now", lambda: real_now() + timedelta(seconds=31))
    state = client.get(f"/api/w/{wid}/state").json()
    assert state["ringing"] is None
    assert state["doses"][0]["status"] == "retry_wait"


def test_cannot_ring_a_dose_twice():
    wid, _, dose_id = ringing_dose()
    assert client.post(f"/api/w/{wid}/doses/{dose_id}/call").status_code == 409


# ---------- the call: typed replies ----------

def test_full_call_with_typed_reply(monkeypatch):
    wid, medicine_id, dose_id, started = in_call()
    assert [line["text"] for line in started["say"]] == reminder_lines("", MED_NAME, MED_LOOK)
    assert started["say"][-1]["audio"] == "/api/audio/line/question"
    monkeypatch.setattr(api, "classify", fake_label("taken"))
    done = client.post(f"/api/w/{wid}/doses/{dose_id}/reply", json={"text": "haan, le li"}).json()
    assert done["status"] == "confirmed"
    assert done["finished"] is True
    assert done["say"][0]["audio"] == "/api/audio/line/speak_thanks"
    totals = done["state"]["summary"]["totals"]
    assert (totals["taken"], totals["total"], totals["adherence_percent"]) == (13, 15, 87)


def test_history_keeps_the_conversation(monkeypatch):
    wid, _, dose_id, _ = in_call()
    monkeypatch.setattr(api, "classify", fake_label("taken"))
    client.post(f"/api/w/{wid}/doses/{dose_id}/reply", json={"text": "haan, le li"})
    calls = client.get(f"/api/w/{wid}/history").json()["calls"]
    assert len(calls) == 15
    transcript = calls[0]["transcript"]
    assert [line["speaker"] for line in transcript] == ["agent"] * 3 + ["parent", "agent"]
    assert transcript[3]["text"] == "haan, le li"
    assert transcript[3]["label"] == "taken"


def test_typed_reply_needs_a_call_in_progress(monkeypatch):
    wid, _, dose_id = ringing_dose()
    monkeypatch.setattr(api, "classify", fake_label("taken"))
    response = client.post(f"/api/w/{wid}/doses/{dose_id}/reply", json={"text": "haan"})
    assert response.status_code == 409


def test_blank_typed_reply_is_rejected():
    wid, _, dose_id, _ = in_call()
    response = client.post(f"/api/w/{wid}/doses/{dose_id}/reply", json={"text": "   "})
    assert response.status_code == 400


def test_call_again_after_not_taken(monkeypatch):
    wid, medicine_id, dose_id, _ = in_call()
    monkeypatch.setattr(api, "classify", fake_label("not_taken"))
    waiting = client.post(f"/api/w/{wid}/doses/{dose_id}/reply", json={"text": "abhi nahi"}).json()
    assert waiting["status"] == "retry_wait"
    assert waiting["retry_available"] is True
    ringing = client.post(f"/api/w/{wid}/doses/{dose_id}/call").json()
    assert ringing["ringing"]["dose_id"] == dose_id
    again = client.post(f"/api/w/{wid}/doses/{dose_id}/answer").json()
    assert again["status"] == "in_call"
    assert len(again["say"]) == 3


def test_hanging_up_or_silence_counts_as_no_answer():
    wid, _, dose_id, _ = in_call()
    result = client.post(f"/api/w/{wid}/doses/{dose_id}/silence").json()
    assert result["status"] == "retry_wait"
    assert result["say"] == []


# ---------- the call: spoken replies ----------

def test_spoken_reply_is_transcribed_understood_and_saved(monkeypatch):
    wid, _, dose_id, _ = in_call()
    monkeypatch.setattr(voice, "transcribe", lambda data, ctype: ("हाँ, ले ली", "gnani"))
    monkeypatch.setattr(api, "classify", fake_label("taken"))
    result = spoken(wid, dose_id)
    assert (result["heard"], result["label"], result["stt"]) == ("हाँ, ले ली", "taken", "gnani")
    assert result["status"] == "confirmed"


def test_spoken_help_request_is_urgent_without_the_ai(monkeypatch):
    wid, _, dose_id, _ = in_call()
    monkeypatch.setattr(voice, "transcribe", lambda data, ctype: ("मुझे सीने में दर्द है", "gnani"))
    result = spoken(wid, dose_id)
    assert result["label"] == "needs_help"
    assert result["status"] == "escalated"
    assert result["state"]["alerts"][0]["level"] == "urgent"


def test_silent_recording_counts_as_no_answer(monkeypatch):
    wid, _, dose_id, _ = in_call()
    monkeypatch.setattr(voice, "transcribe", lambda data, ctype: ("  ", "gnani"))
    result = spoken(wid, dose_id)
    assert result["label"] == "no_response"
    assert result["status"] == "retry_wait"
    assert result["say"] == []


def test_failed_transcription_is_unclear_never_taken(monkeypatch):
    wid, _, dose_id, _ = in_call()

    def broken(data, ctype):
        raise voice.TranscriptionFailed("both services down")

    monkeypatch.setattr(voice, "transcribe", broken)
    result = spoken(wid, dose_id)
    assert result["label"] == "unclear"
    assert result["status"] == "in_call"
    assert result["say"][0]["audio"] == "/api/audio/line/speak_clarify"


def test_off_topic_speech_gets_the_fixed_redirect(monkeypatch):
    wid, _, dose_id, _ = in_call()
    monkeypatch.setattr(voice, "transcribe", lambda data, ctype: ("आज मौसम कैसा है?", "gnani"))
    monkeypatch.setattr(api, "classify", fake_label("off_topic"))
    result = spoken(wid, dose_id)
    assert result["say"][0]["text"] == SPEECH[Action.SPEAK_OFF_TOPIC]
    assert result["say"][0]["audio"] == "/api/audio/line/speak_off_topic"
    assert result["status"] == "in_call"


def test_oversized_recording_is_refused(monkeypatch):
    wid, _, dose_id, _ = in_call()
    monkeypatch.setattr(api, "MAX_AUDIO_BYTES", 10)
    monkeypatch.setattr(voice, "transcribe", blocked)
    assert send_audio(wid, dose_id, b"x" * 11).status_code == 413


def test_empty_recording_is_refused():
    wid, _, dose_id, _ = in_call()
    assert send_audio(wid, dose_id, b"").status_code in (400, 422)


def test_recording_for_a_dose_not_in_a_call_costs_nothing(monkeypatch):
    wid, _, dose_id = ringing_dose()
    monkeypatch.setattr(voice, "transcribe", blocked)
    assert send_audio(wid, dose_id).status_code == 409


def test_voice_cap_stops_recordings_but_not_typed_emergencies(monkeypatch):
    wid, _, dose_id, _ = in_call()
    monkeypatch.setattr(api, "TURN_BUDGET", Budget(0))
    monkeypatch.setattr(voice, "transcribe", blocked)
    assert send_audio(wid, dose_id).status_code == 503
    state = client.get(f"/api/w/{wid}/state").json()
    assert state["doses"][0]["status"] == "in_call"
    urgent = client.post(f"/api/w/{wid}/doses/{dose_id}/reply",
                         json={"text": "mujhe chakkar aa raha hai"})
    assert urgent.status_code == 200
    assert urgent.json()["status"] == "escalated"


# ---------- audio ----------

def test_fixed_line_audio_is_served_and_cached(monkeypatch):
    calls = []
    monkeypatch.setattr(voice, "tts", lambda text: calls.append(text) or b"RIFF-fake-wav")
    for _ in range(2):
        response = client.get("/api/audio/line/speak_thanks")
        assert response.status_code == 200
        assert response.headers["content-type"] == "audio/wav"
        assert response.content == b"RIFF-fake-wav"
    assert calls == [SPEECH[Action.SPEAK_THANKS]]


def test_only_script_lines_can_be_synthesised(monkeypatch):
    monkeypatch.setattr(voice, "tts", blocked)
    for name in ("alert_urgent", "speak_reminder", "nope", "SPEAK_THANKS"):
        assert client.get(f"/api/audio/line/{name}").status_code == 404


def test_every_script_line_has_a_working_audio_route():
    assert set(api._FIXED_REFS) == set(FIXED_LINES.values())
    for url in api._FIXED_REFS.values():
        assert client.get(url).status_code == 200


def test_reminder_audio_speaks_the_saved_medicine_and_nothing_else(monkeypatch):
    spoken_text = []
    monkeypatch.setattr(voice, "tts", lambda text: spoken_text.append(text) or b"RIFF-fake-wav")
    wid, _ = new_workspace()
    medicine_id = add_medicine(wid)
    assert client.get(f"/api/w/{wid}/audio/reminder/{medicine_id}").status_code == 200
    assert spoken_text == [reminder_intro_text(MED_NAME, MED_LOOK)]
    assert client.get(f"/api/w/{wid}/audio/reminder/nope").status_code == 404
    other, _ = new_workspace()
    assert client.get(f"/api/w/{other}/audio/reminder/{medicine_id}").status_code == 404


def test_voice_failure_becomes_503_so_the_page_can_fall_back(monkeypatch):
    def broken(text):
        raise voice.TtsFailed("down")

    monkeypatch.setattr(voice, "tts", broken)
    assert client.get("/api/audio/line/speak_thanks").status_code == 503


def test_voice_cap_still_serves_cached_lines(monkeypatch):
    assert client.get("/api/audio/line/speak_thanks").status_code == 200
    monkeypatch.setattr(api, "TTS_BUDGET", Budget(0))
    assert client.get("/api/audio/line/speak_thanks").status_code == 200
    assert client.get("/api/audio/line/speak_clarify").status_code == 503


def test_scheduling_prepares_the_reminder_audio(monkeypatch):
    spoken_text = []
    monkeypatch.setattr(voice, "tts", lambda text: spoken_text.append(text) or b"RIFF-fake-wav")
    wid, _ = new_workspace()
    schedule(wid, add_medicine(wid), 30)
    assert reminder_intro_text(MED_NAME, MED_LOOK) in spoken_text


def test_warmup_prepares_every_fixed_line_once(monkeypatch):
    spoken_text = []
    monkeypatch.setattr(voice, "tts", lambda text: spoken_text.append(text) or b"RIFF-fake-wav")
    assert client.post("/api/warmup").status_code == 200
    assert sorted(spoken_text) == sorted(FIXED_LINES.values())
    client.post("/api/warmup")
    assert len(spoken_text) == len(FIXED_LINES)


# ---------- browser access and caps ----------

def test_allowed_origin_gets_cors_headers():
    origin = api.ALLOWED_ORIGINS[0]
    response = client.get("/health", headers={"Origin": origin})
    assert response.headers["access-control-allow-origin"] == origin


def test_other_origins_get_no_cors_headers():
    response = client.get("/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in response.headers


def test_budget_allows_then_blocks_then_recovers():
    now = [0.0]
    budget = Budget(max_calls=2, window_seconds=60, clock=lambda: now[0])
    assert budget.allow() is True
    assert budget.allow() is True
    assert budget.allow() is False
    now[0] = 61
    assert budget.allow() is True

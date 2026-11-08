from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from medmitra import api, voice
from medmitra.brain import LABELS, SYSTEM_PROMPT, classify, parse_label
from medmitra.limits import Budget
from medmitra.reminder import Action, DoseReminder, Event, State
from medmitra.script import (
    FIXED_LINES, QUESTION, SPEECH, greeting_text, medicine_info_text,
    reminder_lines, spoken_lines,
)
from medmitra.store import BadRequest, Workspace

T0 = datetime(2026, 11, 7, 4, 30, tzinfo=timezone.utc)
NAME, LOOK = "Vitamin C", "small round orange tablet"
client = TestClient(api.app)


def later(seconds):
    return T0 + timedelta(seconds=seconds)


def is_hindi(text):
    return any("\u0900" <= ch <= "\u097f" for ch in text)


def blocked(*args, **kwargs):
    raise AssertionError("a test tried to use the network")


def must_not_run(payload):
    raise AssertionError("the AI should not run for safety terms")


def fake_model(reply_text):
    def transport(payload):
        return {"choices": [{"message": {"content": reply_text}}]}
    return transport


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    api.WORKSPACES.clear()
    api.AUDIO_CACHE.clear()
    monkeypatch.setattr(api, "TURN_BUDGET", Budget(1000))
    monkeypatch.setattr(api, "TTS_BUDGET", Budget(1000))
    monkeypatch.setattr(voice, "tts", lambda text: b"RIFF-fake-wav")
    monkeypatch.setattr(voice, "_post", blocked)
    monkeypatch.setattr("medmitra.brain._post_groq", blocked)


# ---------- the script ----------

def test_greeting_uses_the_parent_name():
    assert greeting_text("Atharv") == "हैलो Atharv जी।"
    assert greeting_text("  Atharv   Kumar ") == "हैलो Atharv Kumar जी।"
    assert greeting_text("") == "हैलो।"
    assert greeting_text(None) == "हैलो।"


def test_reminder_is_three_messages_ending_with_the_question():
    lines = reminder_lines("Atharv", NAME, LOOK)
    assert len(lines) == 3
    assert lines[0] == greeting_text("Atharv")
    assert NAME in lines[1] and LOOK in lines[1]
    assert lines[2] == QUESTION
    assert not any("।।" in line for line in lines)


def test_medicine_info_without_a_description():
    assert NAME in medicine_info_text(NAME)
    assert "पहचान" not in medicine_info_text(NAME)
    assert "पहचान" in medicine_info_text(NAME, "round।")


def test_asking_about_the_medicine_repeats_the_info_then_the_question():
    lines = spoken_lines([Action.SPEAK_MEDICINE_INFO], "Atharv", NAME, LOOK)
    assert lines == [medicine_info_text(NAME, LOOK), QUESTION]


def test_every_spoken_action_gives_hindi_lines():
    for action in Action:
        if action.value.startswith("speak_"):
            lines = spoken_lines([action], "Atharv", NAME, LOOK)
            assert lines, action
            assert all(is_hindi(line) for line in lines), action


def test_fixed_lines_include_the_question_and_are_hindi():
    assert FIXED_LINES["question"] == QUESTION
    assert all(is_hindi(text) for text in FIXED_LINES.values())
    assert set(SPEECH.values()) <= set(FIXED_LINES.values())


# ---------- the call logic ----------

def started():
    dose = DoseReminder("Atharv", NAME)
    dose.handle(Event.START)
    return dose


def test_asking_which_medicine_gets_the_info_and_keeps_the_call_open():
    dose = started()
    assert dose.handle(Event.ASKS_MEDICINE) == [Action.SPEAK_MEDICINE_INFO]
    assert dose.state == State.REMINDED


def test_info_requests_are_limited_then_the_attempt_ends():
    dose = started()
    dose.handle(Event.ASKS_MEDICINE)
    dose.handle(Event.ASKS_MEDICINE)
    assert dose.handle(Event.ASKS_MEDICINE) == [Action.SPEAK_RETRY_NOTICE, Action.SCHEDULE_RETRY]
    assert dose.state == State.RETRY_WAIT


def test_info_requests_do_not_use_up_the_clarifications():
    dose = started()
    dose.handle(Event.ASKS_MEDICINE)
    dose.handle(Event.ASKS_MEDICINE)
    assert dose.handle(Event.UNCLEAR) == [Action.SPEAK_CLARIFY]
    assert dose.handle(Event.OFF_TOPIC) == [Action.SPEAK_OFF_TOPIC]


def test_info_counter_resets_on_a_new_attempt():
    dose = started()
    for _ in range(3):
        dose.handle(Event.ASKS_MEDICINE)
    dose.handle(Event.RETRY_DUE)
    assert dose.info_requests == 0
    assert dose.handle(Event.ASKS_MEDICINE) == [Action.SPEAK_MEDICINE_INFO]


def test_the_machine_can_still_finish_after_questions():
    dose = started()
    dose.handle(Event.ASKS_MEDICINE)
    assert dose.handle(Event.TAKEN) == [Action.SPEAK_THANKS]
    assert dose.state == State.CONFIRMED


# ---------- the brain ----------

def test_new_label_is_known_to_the_brain():
    assert "asks_medicine" in LABELS
    assert "asks_medicine" in SYSTEM_PROMPT
    assert parse_label("asks-medicine") == "asks_medicine"
    assert parse_label(" Asks medicine. ") == "asks_medicine"


def test_the_agents_question_is_shown_to_the_model_before_the_reply():
    seen = {}

    def capture(payload):
        seen.update(payload)
        return {"choices": [{"message": {"content": "taken"}}]}

    classify("haan theek hai", transport=capture, question=QUESTION)
    assert [m["role"] for m in seen["messages"]] == ["system", "assistant", "user"]
    assert seen["messages"][1]["content"] == QUESTION
    assert seen["messages"][2]["content"] == "haan theek hai"


def test_without_a_question_only_the_reply_is_sent():
    seen = {}

    def capture(payload):
        seen.update(payload)
        return {"choices": [{"message": {"content": "taken"}}]}

    classify("haan", transport=capture)
    assert [m["role"] for m in seen["messages"]] == ["system", "user"]


def test_question_about_the_medicine_is_labelled_by_the_model():
    result = classify("kaun si dawai", transport=fake_model("asks_medicine"), question=QUESTION)
    assert result == ("asks_medicine", "groq")


def test_safety_words_still_win_when_a_question_is_known():
    result = classify("haan theek hai, lekin chakkar aa raha hai",
                      transport=must_not_run, question=QUESTION)
    assert result == ("needs_help", "safety_check")


def test_prompt_teaches_the_new_rules():
    for phrase in ("already", "theek hai", "kaun si dawai"):
        assert phrase in SYSTEM_PROMPT


# ---------- the data core ----------

def make():
    ws = Workspace()
    ws.set_parent_name("Atharv")
    medicine = ws.add_medicine(NAME, LOOK)
    dose = ws.schedule_dose(medicine.id, T0)
    return ws, medicine, dose


def test_parent_name_is_cleaned_and_limited():
    ws = Workspace()
    assert ws.set_parent_name("  Atharv \n") == "Atharv"
    assert ws.set_parent_name("Ath\x00arv") == "Atharv"
    assert ws.set_parent_name("") == ""
    with pytest.raises(BadRequest):
        ws.set_parent_name("x" * 41)
    assert ws.parent_name == ""


def test_a_call_opens_with_the_greeting_by_name():
    ws, _, dose = make()
    ws.call_now(dose.id, T0)
    result = ws.answer(dose.id, later(1))
    assert result["say"] == reminder_lines("Atharv", NAME, LOOK)
    agent_lines = [e["text"] for e in dose.transcript if e["speaker"] == "agent"]
    assert agent_lines == reminder_lines("Atharv", NAME, LOOK)
    assert agent_lines[0] == "हैलो Atharv जी।"


def test_the_last_agent_line_is_what_the_parent_is_answering():
    ws, _, dose = make()
    assert ws.last_agent_line(dose.id) is None
    ws.call_now(dose.id, T0)
    ws.answer(dose.id, later(1))
    assert ws.last_agent_line(dose.id) == QUESTION
    ws.parent_reply(dose.id, "weather", "off_topic", "groq", later(2))
    assert ws.last_agent_line(dose.id) == SPEECH[Action.SPEAK_OFF_TOPIC]


def test_asking_which_medicine_repeats_it_and_the_call_goes_on():
    ws, _, dose = make()
    ws.call_now(dose.id, T0)
    ws.answer(dose.id, later(1))
    asked = ws.parent_reply(dose.id, "कौन सी दवाई?", "asks_medicine", "groq", later(5))
    assert asked["say"] == [medicine_info_text(NAME, LOOK), QUESTION]
    assert asked["status"] == "in_call"
    assert ws.last_agent_line(dose.id) == QUESTION
    done = ws.parent_reply(dose.id, "haan, le raha hu", "taken", "groq", later(9))
    assert done["status"] == "confirmed"
    assert ws.alerts == []


def test_sample_history_still_builds_with_the_longer_reminder():
    ws = Workspace()
    ws.load_sample_history(T0)
    assert ws.summary()["totals"]["total"] == 14
    oldest = ws.call_history()[-1]["transcript"]
    assert oldest[0]["text"] == greeting_text("")


# ---------- the API ----------

def new_workspace():
    return client.post("/api/w").json()["id"]


def add_medicine(wid):
    response = client.post(f"/api/w/{wid}/medicines", json={"name": NAME, "appearance": LOOK})
    return response.json()["medicine"]["id"]


def named_call(name="Atharv"):
    wid = new_workspace()
    assert client.post(f"/api/w/{wid}/parent", json={"name": name}).status_code == 200
    medicine_id = add_medicine(wid)
    dose_id = client.post(f"/api/w/{wid}/doses",
                          json={"medicine_id": medicine_id, "in_minutes": 0}).json()["dose_id"]
    client.get(f"/api/w/{wid}/state")
    started_call = client.post(f"/api/w/{wid}/doses/{dose_id}/answer").json()
    return wid, medicine_id, dose_id, started_call


def send_audio(wid, dose_id):
    return client.post(f"/api/w/{wid}/doses/{dose_id}/audio",
                       files={"file": ("reply.webm", b"fake-webm", "audio/webm")})


def test_parent_name_route_saves_and_validates():
    wid = new_workspace()
    saved = client.post(f"/api/w/{wid}/parent", json={"name": " Atharv "})
    assert saved.status_code == 200
    assert saved.json()["parent_name"] == "Atharv"
    assert client.get(f"/api/w/{wid}/state").json()["parent_name"] == "Atharv"
    assert client.post(f"/api/w/{wid}/parent", json={"name": "x" * 41}).status_code == 400
    assert client.post("/api/w/nope/parent", json={"name": "A"}).status_code == 404


def test_saving_the_name_prepares_the_greeting_audio(monkeypatch):
    spoken = []
    monkeypatch.setattr(voice, "tts", lambda text: spoken.append(text) or b"RIFF-fake-wav")
    wid = new_workspace()
    client.post(f"/api/w/{wid}/parent", json={"name": "Atharv"})
    assert greeting_text("Atharv") in spoken


def test_the_call_starts_with_three_messages_each_with_audio():
    wid, medicine_id, _, started_call = named_call()
    assert [line["text"] for line in started_call["say"]] == reminder_lines("Atharv", NAME, LOOK)
    assert [line["audio"] for line in started_call["say"]] == [
        f"/api/w/{wid}/audio/greeting",
        f"/api/w/{wid}/audio/reminder/{medicine_id}",
        "/api/audio/line/question",
    ]


def test_preview_shows_what_the_parent_will_hear():
    wid = new_workspace()
    client.post(f"/api/w/{wid}/parent", json={"name": "Atharv"})
    medicine_id = add_medicine(wid)
    response = client.get(f"/api/w/{wid}/preview/{medicine_id}")
    assert response.status_code == 200
    say = response.json()["say"]
    assert [line["text"] for line in say] == reminder_lines("Atharv", NAME, LOOK)
    assert say[2]["audio"] == "/api/audio/line/question"
    assert client.get(f"/api/w/{wid}/preview/nope").status_code == 404


def test_greeting_and_info_audio_say_only_server_built_text(monkeypatch):
    spoken = []
    monkeypatch.setattr(voice, "tts", lambda text: spoken.append(text) or b"RIFF-fake-wav")
    wid, medicine_id, _, _ = named_call()
    spoken.clear()
    api.AUDIO_CACHE.clear()
    assert client.get(f"/api/w/{wid}/audio/greeting").status_code == 200
    assert client.get(f"/api/w/{wid}/audio/info/{medicine_id}").status_code == 200
    assert spoken == [greeting_text("Atharv"), medicine_info_text(NAME, LOOK)]
    assert client.get(f"/api/w/{wid}/audio/info/nope").status_code == 404
    other = new_workspace()
    assert client.get(f"/api/w/{other}/audio/info/{medicine_id}").status_code == 404


def test_asking_which_medicine_by_voice_repeats_it_and_keeps_the_call_open(monkeypatch):
    wid, medicine_id, dose_id, _ = named_call()
    monkeypatch.setattr(voice, "transcribe", lambda data, ctype: ("कौन सी दवाई लेनी है?", "gnani"))
    monkeypatch.setattr(api, "classify", lambda text, question=None: ("asks_medicine", "groq"))
    result = send_audio(wid, dose_id).json()
    assert result["label"] == "asks_medicine"
    assert result["status"] == "in_call"
    assert [line["text"] for line in result["say"]] == [medicine_info_text(NAME, LOOK), QUESTION]
    assert result["say"][0]["audio"] == f"/api/w/{wid}/audio/info/{medicine_id}"
    assert result["say"][1]["audio"] == "/api/audio/line/question"


def test_the_brain_is_told_what_the_agent_just_asked(monkeypatch):
    seen = []

    def spy(text, question=None):
        seen.append(question)
        return "taken", "groq"

    monkeypatch.setattr(api, "classify", spy)
    monkeypatch.setattr(voice, "transcribe", lambda data, ctype: ("haan theek hai", "gnani"))
    wid, _, dose_id, _ = named_call()
    assert send_audio(wid, dose_id).status_code == 200
    wid2, _, dose_id2, _ = named_call()
    typed = client.post(f"/api/w/{wid2}/doses/{dose_id2}/reply", json={"text": "haan"})
    assert typed.status_code == 200
    assert seen == [QUESTION, QUESTION]


def test_a_spoken_emergency_still_wins_over_the_question(monkeypatch):
    wid, _, dose_id, _ = named_call()
    monkeypatch.setattr(voice, "transcribe",
                        lambda data, ctype: ("haan theek hai, par chakkar aa raha hai", "gnani"))
    result = send_audio(wid, dose_id).json()
    assert result["label"] == "needs_help"
    assert result["status"] == "escalated"

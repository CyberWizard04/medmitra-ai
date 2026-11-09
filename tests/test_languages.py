import io
import re
import shutil
import wave
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from medmitra import api, voice
from medmitra.languages import (
    CURRENT, DEFAULT_LANGUAGE, LANGUAGES, get_language, language_list,
)
from medmitra.limits import Budget
from medmitra.reminder import Action
from medmitra.script import (
    PACKS, fixed_lines, greeting_text, medicine_info_text, reminder_lines, spoken_lines,
)
from medmitra.store import BadRequest, Conflict, MAX_MEDICINES, Workspace

HTML = (Path(__file__).resolve().parent.parent / "frontend" / "index.html").read_text(encoding="utf-8")
ORIGINAL_POST = voice._post
ORIGINAL_TTS = voice.tts
client = TestClient(api.app)
T0 = datetime(2026, 11, 7, 4, 30, tzinfo=timezone.utc)
NAME, LOOK = "Vitamin C", "small round orange tablet"


def later(seconds):
    return T0 + timedelta(seconds=seconds)


def has_devanagari(text):
    return any("\u0900" <= ch <= "\u097f" for ch in text)


def blocked(*args, **kwargs):
    raise AssertionError("a test tried to use the network")


@contextmanager
def speaking(code):
    token = CURRENT.set(code)
    try:
        yield
    finally:
        CURRENT.reset(token)


class FakeResponse:
    def __init__(self, status_code=200, content=b"", data=None):
        self.status_code = status_code
        self.content = content
        self._data = data

    def json(self):
        if self._data is None:
            raise ValueError("not json")
        return self._data


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    api.WORKSPACES.clear()
    api.AUDIO_CACHE.clear()
    monkeypatch.setattr(api, "TURN_BUDGET", Budget(1000))
    monkeypatch.setattr(api, "TTS_BUDGET", Budget(1000))
    monkeypatch.setattr(voice, "tts", lambda text: b"RIFF-fake-wav")
    monkeypatch.setattr(voice, "_post", blocked)
    monkeypatch.setattr("medmitra.brain._post_groq", blocked)


def record_speech(monkeypatch):
    """Records (language, text) for every text-to-speech request."""
    spoken = []
    monkeypatch.setattr(
        voice, "tts",
        lambda text: spoken.append((get_language().code, text)) or b"RIFF-fake-wav")
    return spoken


def new_workspace():
    return client.post("/api/w").json()["id"]


def ringing_call(wid, name="Atharv"):
    client.post(f"/api/w/{wid}/parent", json={"name": name})
    medicine_id = client.post(f"/api/w/{wid}/medicines",
                              json={"name": NAME, "appearance": LOOK}).json()["medicine"]["id"]
    dose_id = client.post(f"/api/w/{wid}/doses",
                          json={"medicine_id": medicine_id, "in_minutes": 0}).json()["dose_id"]
    client.get(f"/api/w/{wid}/state")
    started = client.post(f"/api/w/{wid}/doses/{dose_id}/answer").json()
    return medicine_id, dose_id, started


# ---------- the scripts ----------

def test_every_language_has_a_complete_script():
    assert set(PACKS) == set(LANGUAGES)
    reference = PACKS[DEFAULT_LANGUAGE]
    for code, pack in PACKS.items():
        assert set(pack) == set(reference), code
        assert set(pack["lines"]) == set(reference["lines"]), code
        assert "{name}" in pack["hello_name"] and "{name}" in pack["medicine_is"], code
        assert "{look}" in pack["looks_like"], code
        assert all(text.strip() for text in pack["lines"].values()), code


def test_language_settings_are_valid():
    for code, lang in LANGUAGES.items():
        assert lang.code == code
        assert re.fullmatch(r"[a-z]{2}-IN", lang.tts_code), code
        assert re.fullmatch(r"[a-z]{2}-IN", lang.stt_code), code
        assert re.fullmatch(r"[a-z]{2}", lang.whisper), code
        assert lang.voice and lang.name and lang.native
    assert DEFAULT_LANGUAGE in LANGUAGES
    assert [item["code"] for item in language_list()] == list(LANGUAGES)


def test_english_script_reads_naturally():
    assert reminder_lines("Atharv", NAME, LOOK, "en") == [
        "Hello Atharv ji.",
        "It is time for your medicine. The medicine is Vitamin C. "
        "It looks like this: small round orange tablet.",
        "Are you taking your medicine now?",
    ]
    assert greeting_text("", "en") == "Hello."
    assert greeting_text("  Atharv  Kumar ", "en") == "Hello Atharv Kumar ji."
    assert medicine_info_text(NAME, "", "en") == "The medicine is Vitamin C."
    assert not any(has_devanagari(text) for text in fixed_lines("en").values())


def test_hindi_is_still_the_default():
    assert reminder_lines("Atharv", NAME, LOOK) == reminder_lines("Atharv", NAME, LOOK, "hi")
    assert greeting_text("Atharv") == "हैलो Atharv जी।"
    assert all(has_devanagari(text) for text in fixed_lines().values())


def test_unknown_language_uses_the_default_script():
    assert greeting_text("A", "xx") == greeting_text("A", "hi")


def test_asking_which_medicine_in_english():
    lines = spoken_lines([Action.SPEAK_MEDICINE_INFO], "A", NAME, LOOK, "en")
    assert lines == [medicine_info_text(NAME, LOOK, "en"), "Are you taking your medicine now?"]


def test_every_spoken_action_has_a_line_in_every_language():
    for code in LANGUAGES:
        for action in Action:
            if action.value.startswith("speak_"):
                assert spoken_lines([action], "A", NAME, LOOK, code), (code, action)


# ---------- speech follows the language ----------

def test_voice_requests_follow_the_current_language(monkeypatch):
    seen = []

    def fake_post(url, **kwargs):
        seen.append((url, kwargs))
        if url == voice.GNANI_TTS_URL:
            return FakeResponse(200, content=b"RIFF")
        if url == voice.GNANI_STT_URL:
            return FakeResponse(200, data={"success": True, "transcript": "yes"})
        return FakeResponse(200, data={"text": "yes"})

    monkeypatch.setattr(voice, "get_secret", lambda name: "key")
    monkeypatch.setattr(voice, "_post", fake_post)
    monkeypatch.setattr(voice, "tts", ORIGINAL_TTS)  # the shared setup replaces it with a fake
    english = LANGUAGES["en"]
    with speaking("en"):
        voice.tts("Hello")
        voice._gnani_stt(b"wav")
        voice._groq_stt(b"audio", "reply.webm", "audio/webm")
    tts_call, gnani_call, groq_call = seen
    assert tts_call[1]["json"]["language"] == english.tts_code
    assert tts_call[1]["json"]["voice"] == english.voice
    assert gnani_call[1]["data"]["language_code"] == english.stt_code
    assert groq_call[1]["data"]["language"] == english.whisper
    seen.clear()
    voice.tts("नमस्ते")  # outside the block: Hindi again
    assert seen[0][1]["json"]["language"] == "hi-IN"


def test_rate_limited_requests_are_retried(monkeypatch):
    replies = [FakeResponse(429), FakeResponse(429), FakeResponse(200, content=b"ok")]
    calls, pauses = [], []
    monkeypatch.setattr(voice, "_post", ORIGINAL_POST)
    monkeypatch.setattr(voice.httpx, "post",
                        lambda url, **kwargs: calls.append(url) or replies[len(calls) - 1])
    monkeypatch.setattr(voice.time, "sleep", pauses.append)
    response = voice._post("https://example.test", json={})
    assert response.status_code == 200
    assert len(calls) == 3 and len(pauses) == 2


def test_a_persistent_rate_limit_is_returned_after_three_tries(monkeypatch):
    calls = []
    monkeypatch.setattr(voice, "_post", ORIGINAL_POST)
    monkeypatch.setattr(voice.httpx, "post",
                        lambda url, **kwargs: calls.append(url) or FakeResponse(429))
    monkeypatch.setattr(voice.time, "sleep", lambda seconds: None)
    assert voice._post("https://example.test", json={}).status_code == 429
    assert len(calls) == 3


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
def test_clips_are_capped_below_gnanis_limit(tmp_path):
    source = tmp_path / "long.wav"
    with wave.open(str(source), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(16000)
        f.writeframes(b"\x00\x00" * 16000 * 35)
    converted = voice.convert_to_wav(source.read_bytes())
    with wave.open(io.BytesIO(converted), "rb") as f:
        seconds = f.getnframes() / f.getframerate()
    assert voice.MAX_CLIP_SECONDS < 30
    assert 28 < seconds < 30


# ---------- the data core ----------

def test_language_defaults_to_hindi_and_is_validated():
    ws = Workspace()
    assert ws.language == DEFAULT_LANGUAGE == "hi"
    assert ws.set_language(" EN ") == "en"
    for bad in ("xx", "", None, "hindi"):
        with pytest.raises(BadRequest):
            ws.set_language(bad)
    assert ws.language == "en"


def test_a_call_speaks_the_selected_language():
    ws = Workspace()
    ws.set_language("en")
    ws.set_parent_name("Atharv")
    medicine = ws.add_medicine(NAME, LOOK)
    dose = ws.schedule_dose(medicine.id, T0)
    ws.call_now(dose.id, T0)
    started = ws.answer(dose.id, later(1))
    assert started["say"] == reminder_lines("Atharv", NAME, LOOK, "en")
    done = ws.parent_reply(dose.id, "yes I took it", "taken", "groq", later(5))
    assert done["say"] == [fixed_lines("en")["speak_thanks"]]
    agent = [entry["text"] for entry in dose.transcript if entry["speaker"] == "agent"]
    assert agent[:3] == reminder_lines("Atharv", NAME, LOOK, "en")


def test_snapshot_lists_the_languages():
    ws = Workspace()
    snap = ws.snapshot()
    assert snap["language"] == "hi" and snap["language_tag"] == "hi-IN"
    assert snap["languages"] == language_list()
    assert {item["code"] for item in snap["languages"]} == set(LANGUAGES)
    ws.set_language("en")
    assert ws.snapshot()["language_tag"] == "en-IN"


def test_clear_sample_data_removes_only_the_fake_history():
    ws = Workspace()
    ws.load_sample_history(T0)
    real = ws.add_medicine("Real medicine", "white tablet")
    dose = ws.schedule_dose(real.id, T0)
    ws.call_now(dose.id, T0)
    ws.answer(dose.id, later(1))
    ws.parent_reply(dose.id, "haan", "taken", "groq", later(5))
    ws.clear_sample_data()
    snap = ws.snapshot()
    assert snap["has_sample_data"] is False
    assert snap["summary"]["totals"]["total"] == 1
    assert snap["summary"]["totals"]["taken"] == 1
    assert [m["name"] for m in snap["medicines"]] == ["Real medicine"]
    assert len(ws.call_history()) == 1


def test_clearing_keeps_a_sample_medicine_a_real_dose_uses():
    ws = Workspace()
    ws.load_sample_history(T0)
    used = next(iter(ws.medicines.values()))
    ws.schedule_dose(used.id, T0 + timedelta(days=1))
    ws.clear_sample_data()
    assert list(ws.medicines) == [used.id]
    assert ws.medicines[used.id].sample is False
    snap = ws.snapshot()
    assert snap["doses"][0]["medicine"] == used.name
    assert snap["has_sample_data"] is False


def test_restore_sample_data_brings_it_back():
    ws = Workspace()
    ws.load_sample_history(T0)
    ws.clear_sample_data()
    assert ws.snapshot()["summary"]["totals"]["total"] == 0
    ws.set_language("en")
    ws.restore_sample_data(T0)
    snap = ws.snapshot()
    assert snap["has_sample_data"] is True
    assert snap["summary"]["totals"]["total"] == Workspace.SAMPLE_DOSES == 14
    assert len(snap["medicines"]) == Workspace.SAMPLE_MEDICINES == 2
    assert ws.language == "en"
    assert has_devanagari(ws.call_history()[0]["transcript"][0]["text"])


def test_restore_refuses_when_already_loaded():
    ws = Workspace()
    ws.load_sample_history(T0)
    with pytest.raises(Conflict):
        ws.restore_sample_data(T0)
    assert ws.snapshot()["summary"]["totals"]["total"] == 14


def test_restore_refuses_when_there_is_no_room():
    ws = Workspace()
    for number in range(MAX_MEDICINES - 1):
        ws.add_medicine(f"Medicine {number}")
    with pytest.raises(BadRequest):
        ws.restore_sample_data(T0)
    assert len(ws.medicines) == MAX_MEDICINES - 1
    assert ws.doses == {}


def test_clearing_an_empty_workspace_is_harmless():
    ws = Workspace()
    ws.clear_sample_data()
    assert ws.snapshot()["has_sample_data"] is False


# ---------- the API ----------

def test_language_route_validates_and_saves():
    wid = new_workspace()
    saved = client.post(f"/api/w/{wid}/language", json={"language": "en"})
    assert saved.status_code == 200 and saved.json()["language"] == "en"
    assert client.get(f"/api/w/{wid}/state").json()["language"] == "en"
    assert client.post(f"/api/w/{wid}/language", json={"language": "xx"}).status_code == 400
    assert client.get(f"/api/w/{wid}/state").json()["language"] == "en"
    assert client.post("/api/w/nope/language", json={"language": "en"}).status_code == 404


def test_changing_language_prepares_the_new_voice(monkeypatch):
    spoken = record_speech(monkeypatch)
    wid = new_workspace()
    client.post(f"/api/w/{wid}/parent", json={"name": "Atharv"})
    spoken.clear()
    client.post(f"/api/w/{wid}/language", json={"language": "en"})
    texts = {text for code, text in spoken if code == "en"}
    assert set(fixed_lines("en").values()) <= texts
    assert greeting_text("Atharv", "en") in texts
    assert all(code == "en" for code, _ in spoken)


def test_a_call_in_english_uses_english_lines_and_audio_urls(monkeypatch):
    spoken = record_speech(monkeypatch)
    wid = new_workspace()
    client.post(f"/api/w/{wid}/language", json={"language": "en"})
    medicine_id, _, started = ringing_call(wid)
    assert [line["text"] for line in started["say"]] == reminder_lines("Atharv", NAME, LOOK, "en")
    assert [line["audio"] for line in started["say"]] == [
        f"/api/w/{wid}/audio/greeting",
        f"/api/w/{wid}/audio/reminder/{medicine_id}",
        "/api/audio/line/question?lang=en",
    ]
    spoken.clear()
    api.AUDIO_CACHE.clear()
    for line in started["say"]:
        assert client.get(line["audio"]).status_code == 200
    assert spoken == [("en", line["text"]) for line in started["say"]]


def test_speech_in_follows_the_selected_language(monkeypatch):
    heard_in = []
    monkeypatch.setattr(
        voice, "transcribe",
        lambda data, ctype: heard_in.append(get_language().code) or ("yes I took it", "gnani"))
    monkeypatch.setattr(api, "classify", lambda text, question=None: ("taken", "groq"))
    wid = new_workspace()
    client.post(f"/api/w/{wid}/language", json={"language": "en"})
    _, dose_id, _ = ringing_call(wid)
    result = client.post(f"/api/w/{wid}/doses/{dose_id}/audio",
                         files={"file": ("reply.webm", b"fake", "audio/webm")}).json()
    assert heard_in == ["en"]
    assert result["status"] == "confirmed"
    assert result["say"][0] == {"text": fixed_lines("en")["speak_thanks"],
                                "audio": "/api/audio/line/speak_thanks?lang=en"}


def test_unknown_language_line_is_404(monkeypatch):
    monkeypatch.setattr(voice, "tts", blocked)
    assert client.get("/api/audio/line/question?lang=xx").status_code == 404
    assert client.get("/api/audio/line/nope?lang=en").status_code == 404


def test_personalised_audio_is_never_cached_by_the_browser():
    wid = new_workspace()
    assert client.get(f"/api/w/{wid}/audio/greeting").headers["cache-control"] == "no-store"
    assert "max-age" in client.get("/api/audio/line/question").headers["cache-control"]


def test_warmup_can_prepare_another_language(monkeypatch):
    spoken = record_speech(monkeypatch)
    client.post("/api/warmup?lang=en")
    assert sorted(text for _, text in spoken) == sorted(fixed_lines("en").values())
    assert all(code == "en" for code, _ in spoken)
    spoken.clear()
    client.post("/api/warmup?lang=xx")  # unknown: the default language
    assert sorted(text for _, text in spoken) == sorted(fixed_lines("hi").values())


def test_the_language_can_change_between_calls(monkeypatch):
    monkeypatch.setattr(api, "classify", lambda text, question=None: ("taken", "groq"))
    wid = new_workspace()
    client.post(f"/api/w/{wid}/language", json={"language": "en"})
    medicine_id, first, started = ringing_call(wid, name="")
    assert started["say"][0]["text"] == "Hello."
    done = client.post(f"/api/w/{wid}/doses/{first}/reply", json={"text": "yes"}).json()
    assert done["finished"] is True
    client.post(f"/api/w/{wid}/language", json={"language": "hi"})
    second = client.post(f"/api/w/{wid}/doses",
                         json={"medicine_id": medicine_id, "in_minutes": 0}).json()["dose_id"]
    client.get(f"/api/w/{wid}/state")
    again = client.post(f"/api/w/{wid}/doses/{second}/answer").json()
    assert again["say"][0]["text"] == "हैलो।"


def test_sample_data_routes():
    wid = new_workspace()
    cleared = client.post(f"/api/w/{wid}/sample/clear")
    assert cleared.status_code == 200
    assert cleared.json()["has_sample_data"] is False
    assert cleared.json()["summary"]["totals"]["total"] == 0
    restored = client.post(f"/api/w/{wid}/sample/restore").json()
    assert restored["has_sample_data"] is True
    assert restored["summary"]["totals"]["total"] == 14
    assert client.post(f"/api/w/{wid}/sample/restore").status_code == 409
    assert client.post("/api/w/nope/sample/clear").status_code == 404


def test_page_has_language_and_sample_controls():
    for piece in ('id="lang"', 'id="sbtn"', "toggleSample", "setLang",
                  "/api/warmup?lang=", "u.lang=(S&&S.language_tag)"):
        assert piece in HTML, piece

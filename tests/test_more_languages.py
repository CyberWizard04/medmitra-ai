from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from medmitra import api, voice
from medmitra.brain import SYSTEM_PROMPT, safety_check
from medmitra.languages import LANGUAGES, get_language, language_list
from medmitra.limits import Budget
from medmitra.script import PACKS, fixed_lines, greeting_text, reminder_lines

HTML = (Path(__file__).resolve().parent.parent / "frontend" / "index.html").read_text(encoding="utf-8")
client = TestClient(api.app)
NAME, LOOK = "Vitamin C", "small round orange tablet"
NEW = ["mr", "gu", "pa", "bn", "ta", "te", "kn", "ml"]

# From Gnani's documentation: the language codes (text-to-speech and speech-to-text).
EXPECTED_CODES = {
    "hi": "hi-IN", "en": "en-IN", "ta": "ta-IN", "te": "te-IN", "kn": "kn-IN",
    "ml": "ml-IN", "mr": "mr-IN", "pa": "pa-IN", "bn": "bn-IN", "gu": "gu-IN",
}

# From Gnani's Timbre v2.5 voice list: the voices made for each language.
CATALOG = {
    "en": {"Kaveri", "Trupti", "Devika", "Pranav", "Shlok", "Girish"},
    "hi": {"Nalini", "Bhavna", "Yashvi", "Urmila", "Jwala", "Chitra", "Ambuja", "Deepak",
           "Roopesh", "Vikrant", "Hemraj", "Jalaj", "Omkar"},
    "ta": {"Asmita", "Trisha", "Brinda", "Vedika", "Noopur"},
    "te": {"Suhana", "Lehara", "Lavanya", "Yukti", "Varuni"},
    "kn": {"Saanvi", "Kavin"},
    "ml": {"Reshma", "Riyaan"},
    "mr": {"Zahira", "Ishaan"},
    "bn": {"Kirra", "Dhruva"},
    "gu": {"Falak", "Veera"},
    "pa": {"Mehuli", "Zayan"},
}

SCRIPT_RANGES = {
    "mr": (0x0900, 0x097F), "gu": (0x0A80, 0x0AFF), "pa": (0x0A00, 0x0A7F),
    "bn": (0x0980, 0x09FF), "ta": (0x0B80, 0x0BFF), "te": (0x0C00, 0x0C7F),
    "kn": (0x0C80, 0x0CFF), "ml": (0x0D00, 0x0D7F),
}

HELP = {
    "mr": "मला छातीत दुखत आहे",
    "gu": "મને છાતીમાં દુખાવો થાય છે",
    "pa": "ਮੇਰੀ ਛਾਤੀ ਵਿੱਚ ਦਰਦ ਹੈ",
    "bn": "আমার বুকে ব্যথা করছে",
    "ta": "எனக்கு நெஞ்சு வலிக்கிறது",
    "te": "నాకు ఛాతీ నొప్పిగా ఉంది",
    "kn": "ನನಗೆ ಎದೆ ನೋವು ಇದೆ",
    "ml": "എനിക്ക് നെഞ്ചുവേദന ഉണ്ട്",
}

ORDINARY = {
    "mr": ["हो, मी औषध घेतले आहे", "नाही, अजून घेतले नाही", "कोणते औषध घ्यायचे?"],
    "gu": ["હા, મેં દવા લઈ લીધી છે", "ના, હજી નથી લીધી", "કઈ દવા લેવાની છે?"],
    "pa": ["ਹਾਂ, ਮੈਂ ਦਵਾਈ ਲੈ ਲਈ ਹੈ", "ਨਹੀਂ, ਹਾਲੇ ਨਹੀਂ ਲਈ", "ਕਿਹੜੀ ਦਵਾਈ ਲੈਣੀ ਹੈ?"],
    "bn": ["হ্যাঁ, আমি ওষুধ খেয়েছি", "না, এখনও খাইনি", "কোন ওষুধ খেতে হবে?"],
    "ta": ["ஆம், நான் மருந்து சாப்பிட்டேன்", "இல்லை, இன்னும் சாப்பிடவில்லை", "எந்த மருந்து எடுக்க வேண்டும்?"],
    "te": ["అవును, నేను మందు వేసుకున్నాను", "లేదు, ఇంకా వేసుకోలేదు", "ఏ మందు వేసుకోవాలి?"],
    "kn": ["ಹೌದು, ನಾನು ಔಷಧಿ ತೆಗೆದುಕೊಂಡೆ", "ಇಲ್ಲ, ಇನ್ನೂ ತೆಗೆದುಕೊಂಡಿಲ್ಲ", "ಯಾವ ಔಷಧಿ ತೆಗೆದುಕೊಳ್ಳಬೇಕು?"],
    "ml": ["അതെ, ഞാൻ മരുന്ന് കഴിച്ചു", "ഇല്ല, ഇതുവരെ കഴിച്ചിട്ടില്ല", "ഏത് മരുന്നാണ് കഴിക്കേണ്ടത്?"],
}


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    api.WORKSPACES.clear()
    api.AUDIO_CACHE.clear()
    monkeypatch.setattr(api, "TURN_BUDGET", Budget(1000))
    monkeypatch.setattr(api, "TTS_BUDGET", Budget(1000))
    monkeypatch.setattr(voice, "tts", lambda text: b"RIFF-fake-wav")
    monkeypatch.setattr(voice, "_post", lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("a test tried to use the network")))
    monkeypatch.setattr("medmitra.brain._post_groq", lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("a test tried to use the network")))


def test_all_of_gnanis_languages_are_available():
    assert set(LANGUAGES) == set(EXPECTED_CODES)
    for code, lang in LANGUAGES.items():
        assert lang.tts_code == EXPECTED_CODES[code] == lang.stt_code
        assert lang.whisper == code


def test_voices_come_from_gnanis_catalog():
    for code, lang in LANGUAGES.items():
        assert lang.voice in CATALOG[code], (code, lang.voice)


@pytest.mark.parametrize("code", NEW)
def test_scripts_use_the_right_writing_system(code):
    low, high = SCRIPT_RANGES[code]
    pack = PACKS[code]
    texts = list(fixed_lines(code).values()) + [
        pack["time_for_medicine"], pack["hello"], pack["hello_name"].format(name=""),
        pack["medicine_is"].format(name=""), pack["looks_like"].format(look=""),
    ]
    for text in texts:
        letters = [ch for ch in text if ch.isalpha()]
        assert letters, text
        assert all(low <= ord(ch) <= high for ch in letters), text


@pytest.mark.parametrize("code", list(LANGUAGES))
def test_reminder_reads_correctly(code):
    lines = reminder_lines("Atharv", NAME, LOOK, code)
    assert len(lines) == 3
    assert "Atharv" in lines[0]
    assert NAME in lines[1] and LOOK in lines[1]
    assert all("{" not in line and "}" not in line for line in lines)
    assert greeting_text("", code) != greeting_text("Atharv", code)


@pytest.mark.parametrize("code", list(LANGUAGES))
def test_help_line_gives_the_emergency_numbers(code):
    line = fixed_lines(code)["speak_help_ack"]
    assert "108" in line and "112" in line


@pytest.mark.parametrize("code", NEW)
def test_emergency_phrases_are_caught(code):
    assert safety_check(HELP[code]) is True


@pytest.mark.parametrize("code", NEW)
def test_ordinary_answers_are_not_flagged(code):
    for phrase in ORDINARY[code]:
        assert safety_check(phrase) is False, phrase


def test_only_checked_languages_are_marked_reviewed():
    assert LANGUAGES["hi"].reviewed and LANGUAGES["en"].reviewed
    for code in NEW:
        assert LANGUAGES[code].reviewed is False, code
    assert all(isinstance(item["reviewed"], bool) for item in language_list())


def test_english_voice_is_devika():
    assert LANGUAGES["en"].voice == "Devika"


@pytest.mark.parametrize("code", list(LANGUAGES))
def test_a_full_call_in_every_language(monkeypatch, code):
    spoken = []
    monkeypatch.setattr(
        voice, "tts",
        lambda text: spoken.append((get_language().code, text)) or b"RIFF-fake-wav")
    monkeypatch.setattr(api, "classify", lambda text, question=None: ("taken", "groq"))
    wid = client.post("/api/w").json()["id"]
    assert client.post(f"/api/w/{wid}/language", json={"language": code}).status_code == 200
    client.post(f"/api/w/{wid}/parent", json={"name": "Atharv"})
    medicine_id = client.post(f"/api/w/{wid}/medicines",
                              json={"name": NAME, "appearance": LOOK}).json()["medicine"]["id"]
    dose_id = client.post(f"/api/w/{wid}/doses",
                          json={"medicine_id": medicine_id, "in_minutes": 0}).json()["dose_id"]
    client.get(f"/api/w/{wid}/state")
    started = client.post(f"/api/w/{wid}/doses/{dose_id}/answer").json()
    assert [line["text"] for line in started["say"]] == reminder_lines("Atharv", NAME, LOOK, code)
    spoken.clear()
    api.AUDIO_CACHE.clear()
    for line in started["say"]:
        assert client.get(line["audio"]).status_code == 200
    assert spoken == [(code, line["text"]) for line in started["say"]]
    done = client.post(f"/api/w/{wid}/doses/{dose_id}/reply", json={"text": "ok"}).json()
    assert done["status"] == "confirmed"
    assert done["say"][0]["text"] == fixed_lines(code)["speak_thanks"]


def test_page_labels_draft_languages_and_explains_the_description():
    assert '(draft)' in HTML
    assert "Write the medicine description in the language you want it to say" in HTML
    assert "Write the medicine description in " in HTML


def test_brain_knows_the_other_languages():
    for name in ("Marathi", "Gujarati", "Punjabi", "Bengali", "Tamil", "Telugu", "Kannada", "Malayalam"):
        assert name in SYSTEM_PROMPT, name

"""Languages the agent can speak.

Adding a language takes three things:
1. an entry in LANGUAGES below (Gnani language codes and a voice),
2. a script pack (packs_extra.py or script.py): every line the agent says,
   checked by a fluent speaker,
3. emergency words for that language in brain.py (SAFETY_TERMS).

The language of the current request travels in CURRENT, so the speech functions
keep their simple signatures. The API sets it around each text-to-speech and
speech-to-text call.
"""
import os
from contextvars import ContextVar
from dataclasses import dataclass

# Languages whose agent lines a fluent speaker has checked. The rest show as drafts.
# Set REVIEWED_LANGUAGES on the server (for example "hi,en,mr") once a language is checked.
REVIEWED = {code.strip()
            for code in os.environ.get("REVIEWED_LANGUAGES", "hi,en").split(",")
            if code.strip()}


@dataclass(frozen=True)
class Language:
    code: str      # our short code (also the Groq Whisper language)
    name: str      # English name
    native: str    # name in its own script
    tts_code: str  # Gnani text-to-speech language value
    stt_code: str  # Gnani speech-to-text language_code
    whisper: str   # Groq Whisper (backup speech-to-text) language
    voice: str     # Gnani Timbre v2.5 voice made for this language
    reviewed: bool = False


DEFAULT_LANGUAGE = "hi"


def _language(code, name, native, tag, voice):
    return Language(code, name, native, tag, tag, code, voice, code in REVIEWED)


LANGUAGES = {item.code: item for item in [
    _language("hi", "Hindi", "हिन्दी", "hi-IN", os.environ.get("GNANI_VOICE", "Nalini")),
    _language("en", "English", "English", "en-IN", os.environ.get("GNANI_VOICE_EN", "Devika")),
    _language("mr", "Marathi", "मराठी", "mr-IN", "Ishaan"),
    _language("gu", "Gujarati", "ગુજરાતી", "gu-IN", "Veera"),
    _language("pa", "Punjabi", "ਪੰਜਾਬੀ", "pa-IN", "Zayan"),
    _language("bn", "Bengali", "বাংলা", "bn-IN", "Kirra"),
    _language("ta", "Tamil", "தமிழ்", "ta-IN", "Trisha"),
    _language("te", "Telugu", "తెలుగు", "te-IN", "Lavanya"),
    _language("kn", "Kannada", "ಕನ್ನಡ", "kn-IN", "Kavin"),
    _language("ml", "Malayalam", "മലയാളം", "ml-IN", "Reshma"),
]}

CURRENT = ContextVar("medmitra_language", default=DEFAULT_LANGUAGE)


def get_language(code=None):
    """The language for code, or for the current request. Unknown codes give Hindi."""
    return LANGUAGES.get(code or CURRENT.get(), LANGUAGES[DEFAULT_LANGUAGE])


def language_list():
    return [{"code": item.code, "name": item.name, "native": item.native,
             "reviewed": item.reviewed}
            for item in LANGUAGES.values()]

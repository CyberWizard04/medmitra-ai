"""Languages the agent can speak.

Adding a language takes three things:
1. an entry in LANGUAGES below (Gnani language codes and a voice),
2. a script pack in script.py (every line the agent says), checked by a fluent speaker,
3. emergency words for that language in brain.py (SAFETY_TERMS).

The language of the current request travels in CURRENT, so the speech functions
keep their simple signatures. The API sets it around each text-to-speech and
speech-to-text call.
"""
import os
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class Language:
    code: str      # our short code
    name: str      # English name
    native: str    # name in its own script
    tts_code: str  # Gnani text-to-speech language value
    stt_code: str  # Gnani speech-to-text language_code
    whisper: str   # Groq Whisper (backup speech-to-text) language
    voice: str     # Gnani Timbre v2.5 voice made for this language


DEFAULT_LANGUAGE = "hi"

LANGUAGES = {
    "hi": Language("hi", "Hindi", "हिन्दी", "hi-IN", "hi-IN", "hi",
                   os.environ.get("GNANI_VOICE", "Nalini")),
    "en": Language("en", "English", "English", "en-IN", "en-IN", "en",
                   os.environ.get("GNANI_VOICE_EN", "Kaveri")),
}

CURRENT = ContextVar("medmitra_language", default=DEFAULT_LANGUAGE)


def get_language(code=None):
    """The language for code, or for the current request. Unknown codes give Hindi."""
    return LANGUAGES.get(code or CURRENT.get(), LANGUAGES[DEFAULT_LANGUAGE])


def language_list():
    return [{"code": item.code, "name": item.name, "native": item.native}
            for item in LANGUAGES.values()]

"""Speech in and speech out.

- tts(): Gnani Timbre turns a Hindi sentence into WAV audio (verified live).
- transcribe(): Gnani Prisma turns a recording into text (verified live with WAV).
  Browsers record WebM, which Gnani rejects, so recordings are converted to WAV with
  ffmpeg first. If that or Gnani fails, Groq Whisper is the backup. It accepts WebM
  directly (NOT yet verified live).
Keys come from the environment or Colab Secrets and are never printed or put in errors.
"""
import os
import subprocess
import tempfile

import httpx

from medmitra.config import get_secret

GNANI_TTS_URL = "https://api.vachana.ai/api/v1/tts/inference"
GNANI_STT_URL = "https://api.vachana.ai/stt/v3"
GROQ_STT_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GNANI_TTS_MODEL = os.environ.get("GNANI_TTS_MODEL", "timbre-v2.5")
GNANI_VOICE = os.environ.get("GNANI_VOICE", "Nalini")
GROQ_STT_MODEL = "whisper-large-v3"
TIMEOUT = 40


class TtsFailed(Exception):
    """Text-to-speech did not produce audio."""


class TranscriptionFailed(Exception):
    """Speech-to-text did not produce a result."""


def _post(url, **kwargs):
    kwargs.setdefault("timeout", TIMEOUT)
    return httpx.post(url, **kwargs)


def tts(text):
    """Hindi text in, WAV bytes out."""
    key = get_secret("GNANI_API_KEY")
    if not key:
        raise TtsFailed("no Gnani key")
    body = {
        "text": text,
        "model": GNANI_TTS_MODEL,
        "voice": GNANI_VOICE,
        "language": "hi-IN",
        "audio_config": {"container": "wav", "encoding": "linear_pcm", "sample_rate": 44100},
    }
    try:
        response = _post(GNANI_TTS_URL, json=body,
                         headers={"X-API-Key-ID": key, "User-Agent": "medmitra-dev"})
    except httpx.HTTPError as error:
        raise TtsFailed(type(error).__name__) from None
    if response.status_code != 200 or not response.content:
        raise TtsFailed(f"Gnani HTTP {response.status_code}")
    return response.content


def convert_to_wav(audio_bytes):
    """Any browser recording in, 16 kHz mono WAV out (needs ffmpeg)."""
    with tempfile.TemporaryDirectory() as folder:
        source = os.path.join(folder, "input")
        target = os.path.join(folder, "output.wav")
        with open(source, "wb") as f:
            f.write(audio_bytes)
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-i", source,
                 "-ac", "1", "-ar", "16000", "-t", "60", target],
                check=True, capture_output=True, timeout=30)
        except (subprocess.SubprocessError, OSError) as error:
            raise TranscriptionFailed(f"conversion failed: {type(error).__name__}") from None
        with open(target, "rb") as f:
            return f.read()


def _gnani_stt(wav_bytes):
    key = get_secret("GNANI_API_KEY")
    if not key:
        raise TranscriptionFailed("no Gnani key")
    try:
        response = _post(
            GNANI_STT_URL,
            headers={"X-API-Key-ID": key, "User-Agent": "medmitra-dev"},
            files={"audio_file": ("reply.wav", wav_bytes, "audio/wav")},
            data={"language_code": "hi-IN", "format": "transcribe"})
        data = response.json()
    except (httpx.HTTPError, ValueError) as error:
        raise TranscriptionFailed(type(error).__name__) from None
    if response.status_code != 200 or not isinstance(data, dict) or not data.get("success"):
        raise TranscriptionFailed(f"Gnani HTTP {response.status_code}")
    return str(data.get("transcript") or "").strip()


def _groq_stt(audio_bytes, filename, content_type):
    key = get_secret("GROQ_API_KEY")
    if not key:
        raise TranscriptionFailed("no Groq key")
    try:
        response = _post(
            GROQ_STT_URL,
            headers={"Authorization": f"Bearer {key}", "User-Agent": "medmitra-dev"},
            files={"file": (filename, audio_bytes, content_type)},
            data={"model": GROQ_STT_MODEL, "language": "hi", "temperature": "0",
                  "response_format": "json"})
        data = response.json()
    except (httpx.HTTPError, ValueError) as error:
        raise TranscriptionFailed(type(error).__name__) from None
    if response.status_code != 200 or not isinstance(data, dict):
        raise TranscriptionFailed(f"Groq HTTP {response.status_code}")
    return str(data.get("text") or "").strip()


def transcribe(audio_bytes, content_type="audio/webm"):
    """Returns (text, source). Text is empty when nothing was said.

    Raises TranscriptionFailed only if both services fail."""
    try:
        return _gnani_stt(convert_to_wav(audio_bytes)), "gnani"
    except TranscriptionFailed:
        pass
    base = (content_type or "audio/webm").split(";")[0].strip().lower() or "audio/webm"
    extension = "mp4" if "mp4" in base else "ogg" if "ogg" in base else "webm"
    return _groq_stt(audio_bytes, f"reply.{extension}", base), "groq"

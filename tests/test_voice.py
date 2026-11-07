import shutil
import subprocess
import wave

import httpx
import pytest

from medmitra import voice


class FakeResponse:
    def __init__(self, status_code=200, content=b"", data=None):
        self.status_code = status_code
        self.content = content
        self._data = data

    def json(self):
        if self._data is None:
            raise ValueError("not json")
        return self._data


def use_key(monkeypatch, value="secret-key"):
    monkeypatch.setattr(voice, "get_secret", lambda name: value)


def blocked(*args, **kwargs):
    raise AssertionError("a test tried to use the network")


def test_tts_sends_the_right_request_and_returns_audio(monkeypatch):
    seen = {}

    def fake_post(url, **kwargs):
        seen["url"] = url
        seen.update(kwargs)
        return FakeResponse(200, content=b"RIFFaudio")

    use_key(monkeypatch)
    monkeypatch.setattr(voice, "_post", fake_post)
    assert voice.tts("नमस्ते") == b"RIFFaudio"
    assert seen["url"] == voice.GNANI_TTS_URL
    assert seen["json"]["text"] == "नमस्ते"
    assert seen["json"]["language"] == "hi-IN"
    assert seen["headers"]["X-API-Key-ID"] == "secret-key"


def test_tts_failures_raise_without_leaking_the_key(monkeypatch):
    use_key(monkeypatch)
    for response in (FakeResponse(401), FakeResponse(200, content=b""),
                     FakeResponse(500, content=b"oops")):
        monkeypatch.setattr(voice, "_post", lambda url, response=response, **kwargs: response)
        with pytest.raises(voice.TtsFailed) as error:
            voice.tts("नमस्ते")
        assert "secret-key" not in str(error.value)


def test_tts_network_error_is_wrapped(monkeypatch):
    use_key(monkeypatch)

    def down(url, **kwargs):
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(voice, "_post", down)
    with pytest.raises(voice.TtsFailed):
        voice.tts("नमस्ते")


def test_missing_keys_fail_cleanly(monkeypatch):
    monkeypatch.setattr(voice, "get_secret", lambda name: None)
    monkeypatch.setattr(voice, "_post", blocked)
    with pytest.raises(voice.TtsFailed):
        voice.tts("x")
    with pytest.raises(voice.TranscriptionFailed):
        voice._gnani_stt(b"wav")
    with pytest.raises(voice.TranscriptionFailed):
        voice._groq_stt(b"audio", "reply.webm", "audio/webm")


def test_gnani_stt_returns_the_transcript(monkeypatch):
    seen = {}

    def fake_post(url, **kwargs):
        seen["url"] = url
        seen.update(kwargs)
        return FakeResponse(200, data={"success": True, "transcript": " हाँ, ले ली "})

    use_key(monkeypatch)
    monkeypatch.setattr(voice, "_post", fake_post)
    assert voice._gnani_stt(b"wav") == "हाँ, ले ली"
    assert seen["url"] == voice.GNANI_STT_URL
    assert seen["data"]["language_code"] == "hi-IN"
    assert seen["headers"]["X-API-Key-ID"] == "secret-key"
    assert "audio_file" in seen["files"]


def test_gnani_stt_rejects_errors_and_garbage(monkeypatch):
    use_key(monkeypatch)
    bad_responses = [
        FakeResponse(400, data={"success": False, "error": {"type": "UNSUPPORTED_AUDIO_FORMAT"}}),
        FakeResponse(200, data=None),
        FakeResponse(200, data=["not", "a", "dict"]),
        FakeResponse(200, data={"success": False}),
    ]
    for response in bad_responses:
        monkeypatch.setattr(voice, "_post", lambda url, response=response, **kwargs: response)
        with pytest.raises(voice.TranscriptionFailed):
            voice._gnani_stt(b"wav")


def test_groq_stt_returns_the_text(monkeypatch):
    seen = {}

    def fake_post(url, **kwargs):
        seen["url"] = url
        seen.update(kwargs)
        return FakeResponse(200, data={"text": " हाँ "})

    use_key(monkeypatch)
    monkeypatch.setattr(voice, "_post", fake_post)
    assert voice._groq_stt(b"webm", "reply.webm", "audio/webm") == "हाँ"
    assert seen["url"] == voice.GROQ_STT_URL
    assert seen["headers"]["Authorization"] == "Bearer secret-key"
    assert seen["data"]["model"] == voice.GROQ_STT_MODEL


def test_transcribe_prefers_gnani_after_converting(monkeypatch):
    def must_not_run(*args):
        raise AssertionError("the backup should not run")

    monkeypatch.setattr(voice, "convert_to_wav", lambda audio: b"WAV")
    monkeypatch.setattr(voice, "_gnani_stt", lambda wav: "हाँ" if wav == b"WAV" else "wrong")
    monkeypatch.setattr(voice, "_groq_stt", must_not_run)
    assert voice.transcribe(b"webm-bytes", "audio/webm") == ("हाँ", "gnani")


def test_transcribe_falls_back_when_conversion_fails(monkeypatch):
    seen = {}

    def broken(audio):
        raise voice.TranscriptionFailed("no ffmpeg")

    def groq(audio, filename, content_type):
        seen.update(audio=audio, filename=filename, content_type=content_type)
        return "haan"

    monkeypatch.setattr(voice, "convert_to_wav", broken)
    monkeypatch.setattr(voice, "_groq_stt", groq)
    assert voice.transcribe(b"webm-bytes", "audio/webm;codecs=opus") == ("haan", "groq")
    assert seen == {"audio": b"webm-bytes", "filename": "reply.webm",
                    "content_type": "audio/webm"}
    voice.transcribe(b"mp4-bytes", "audio/mp4")
    assert seen["filename"] == "reply.mp4"


def test_transcribe_falls_back_when_gnani_fails(monkeypatch):
    def gnani_down(wav):
        raise voice.TranscriptionFailed("Gnani HTTP 503")

    monkeypatch.setattr(voice, "convert_to_wav", lambda audio: b"WAV")
    monkeypatch.setattr(voice, "_gnani_stt", gnani_down)
    monkeypatch.setattr(voice, "_groq_stt", lambda audio, name, ctype: "haan")
    assert voice.transcribe(b"x", "audio/webm") == ("haan", "groq")


def test_silence_does_not_trigger_the_fallback(monkeypatch):
    def must_not_run(*args):
        raise AssertionError("silence should not call the backup")

    monkeypatch.setattr(voice, "convert_to_wav", lambda audio: b"WAV")
    monkeypatch.setattr(voice, "_gnani_stt", lambda wav: "")
    monkeypatch.setattr(voice, "_groq_stt", must_not_run)
    assert voice.transcribe(b"x", "audio/webm") == ("", "gnani")


def test_both_services_failing_raises(monkeypatch):
    def down(*args):
        raise voice.TranscriptionFailed("down")

    monkeypatch.setattr(voice, "convert_to_wav", lambda audio: b"WAV")
    monkeypatch.setattr(voice, "_gnani_stt", down)
    monkeypatch.setattr(voice, "_groq_stt", down)
    with pytest.raises(voice.TranscriptionFailed):
        voice.transcribe(b"x", "audio/webm")


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
def test_real_ffmpeg_converts_webm_to_wav(tmp_path):
    source = tmp_path / "tone.wav"
    with wave.open(str(source), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(16000)
        f.writeframes(b"\x00\x00" * 8000)
    webm = tmp_path / "tone.webm"
    try:
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(source),
                        "-c:a", "libopus", str(webm)], check=True, capture_output=True)
    except subprocess.CalledProcessError:
        pytest.skip("this ffmpeg cannot make WebM")
    converted = voice.convert_to_wav(webm.read_bytes())
    assert converted[:4] == b"RIFF"
    assert converted[8:12] == b"WAVE"


def test_unreadable_audio_raises():
    with pytest.raises(voice.TranscriptionFailed):
        voice.convert_to_wav(b"this is not audio")

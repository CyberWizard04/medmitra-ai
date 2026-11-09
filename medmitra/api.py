"""HTTP API for the demo (FastAPI). Runs on Render.

Each visitor gets a private workspace held in memory: a parent, medicines, doses,
call transcripts, alerts and a language. A server restart clears everything, so every
workspace starts with labelled sample history.

There is no background worker. The screens poll /state every couple of seconds and
each poll runs the "is a dose due?" check. The scheduling logic is switched on only
while a page is open. Real scheduling and phone calls stay off.

The browser never sends text to be spoken. The audio routes build the text on the
server from the fixed script and the caretaker's saved medicine, in the workspace's
language, so the page cannot use our Gnani credits to say arbitrary things.
"""
import os
import secrets
import threading
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from medmitra import voice
from medmitra.brain import classify, safety_check
from medmitra.languages import CURRENT, DEFAULT_LANGUAGE, LANGUAGES
from medmitra.limits import Budget
from medmitra.reminder import State
from medmitra.script import (
    fixed_lines, greeting_text, medicine_info_text, reminder_intro_text, reminder_lines,
)
from medmitra.store import BadRequest, Conflict, NotFound, Workspace

ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("ALLOWED_ORIGINS", "http://localhost:3000").split(",")
    if origin.strip()
]
MAX_WORKSPACES = 50
MAX_AUDIO_BYTES = 3_000_000
CACHE_LIMIT = 80

# Silent caps. They protect the Groq and Gnani accounts.
TURN_BUDGET = Budget(int(os.environ.get("MAX_VOICE_TURNS_PER_HOUR", "300")))
TTS_BUDGET = Budget(int(os.environ.get("MAX_TTS_PER_HOUR", "200")))

LOCK = threading.RLock()
WORKSPACES = OrderedDict()
CACHE_LOCK = threading.Lock()
AUDIO_CACHE = OrderedDict()  # (language, text) -> WAV bytes


def _line_url(key, lang):
    base = f"/api/audio/line/{key}"
    return base if lang == DEFAULT_LANGUAGE else f"{base}?lang={lang}"


def _fixed_refs(lang):
    """Spoken line -> the URL that serves its audio."""
    return {text: _line_url(key, lang) for key, text in fixed_lines(lang).items()}


_FIXED_REFS = _fixed_refs(DEFAULT_LANGUAGE)

app = FastAPI(title="MedMitra AI demo")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


@app.exception_handler(BadRequest)
async def _bad_request(request, error):
    return JSONResponse({"detail": str(error)}, status_code=400)


@app.exception_handler(NotFound)
async def _not_found(request, error):
    return JSONResponse({"detail": str(error)}, status_code=404)


@app.exception_handler(Conflict)
async def _conflict(request, error):
    return JSONResponse({"detail": str(error)}, status_code=409)


class MedicineIn(BaseModel):
    name: str = Field(max_length=200)
    appearance: str = Field(default="", max_length=500)


class DoseIn(BaseModel):
    medicine_id: str
    in_minutes: Optional[int] = Field(default=None, ge=0, le=10080)
    due_at: Optional[datetime] = None


class ReplyIn(BaseModel):
    text: str = Field(max_length=500)


class ParentIn(BaseModel):
    name: str = Field(default="", max_length=200)


class LanguageIn(BaseModel):
    language: str = Field(max_length=20)


# ---------- helpers ----------

def _now():
    return datetime.now(timezone.utc)


def _create_workspace():
    workspace = Workspace()
    workspace.load_sample_history(_now())
    wid = secrets.token_urlsafe(9)
    with LOCK:
        WORKSPACES[wid] = workspace
        while len(WORKSPACES) > MAX_WORKSPACES:
            WORKSPACES.popitem(last=False)
    return wid, workspace


def _get(wid):
    with LOCK:
        workspace = WORKSPACES.get(wid)
        if workspace is None:
            raise HTTPException(404, "Unknown workspace. Please start a new demo.")
        WORKSPACES.move_to_end(wid)
        return workspace


def _check_in_call(workspace, dose_id):
    """Raises unless the dose is in a call. Returns the agent's last line (the question)."""
    dose = workspace.doses.get(dose_id)
    if dose is None:
        raise NotFound("No such dose.")
    if dose.reminder.state != State.REMINDED:
        raise Conflict("No call is in progress for this dose.")
    return workspace.last_agent_line(dose_id)


def _items_for(wid, workspace, medicine, lines):
    """Each spoken line, with the URL where its audio can be fetched."""
    lang = workspace.language
    refs = {
        greeting_text(workspace.parent_name, lang): f"/api/w/{wid}/audio/greeting",
        reminder_intro_text(medicine.name, medicine.appearance, lang):
            f"/api/w/{wid}/audio/reminder/{medicine.id}",
        medicine_info_text(medicine.name, medicine.appearance, lang):
            f"/api/w/{wid}/audio/info/{medicine.id}",
    }
    refs.update(_fixed_refs(lang))
    return [{"text": line, "audio": refs.get(line)} for line in lines]


def _say_items(wid, workspace, dose_id, lines):
    dose = workspace.doses[dose_id]
    return _items_for(wid, workspace, workspace.medicines[dose.medicine_id], lines)


def _turn_response(wid, workspace, dose_id, result, extra=None):
    response = {
        **result,
        "say": _say_items(wid, workspace, dose_id, result["say"]),
        "state": workspace.snapshot(),
    }
    if extra:
        response.update(extra)
    return response


def _classify_text(text, question=None):
    """Returns (label, source). Safety words never need the AI or the cap."""
    if safety_check(text):
        return classify(text, question=question)
    if not TURN_BUDGET.allow():
        raise HTTPException(503, "The demo is busy right now. Please try again in a few minutes.")
    return classify(text, question=question)


@contextmanager
def _speaking(lang):
    """Makes the speech functions use this language for the code inside the block."""
    token = CURRENT.set(lang)
    try:
        yield
    finally:
        CURRENT.reset(token)


def _synthesize(text, lang=DEFAULT_LANGUAGE):
    """Audio for a server-built sentence, cached so repeats are instant and free."""
    key = (lang, text)
    with CACHE_LOCK:
        if key in AUDIO_CACHE:
            AUDIO_CACHE.move_to_end(key)
            return AUDIO_CACHE[key]
    if not TTS_BUDGET.allow():
        raise HTTPException(503, "Voice is busy right now.")
    try:
        with _speaking(lang):
            audio = voice.tts(text)
    except voice.TtsFailed:
        raise HTTPException(503, "Voice is unavailable right now.") from None
    with CACHE_LOCK:
        AUDIO_CACHE[key] = audio
        while len(AUDIO_CACHE) > CACHE_LIMIT:
            AUDIO_CACHE.popitem(last=False)
    return audio


def _prewarm(text, lang=DEFAULT_LANGUAGE):
    try:
        _synthesize(text, lang)
    except Exception:
        pass


def _prewarm_fixed_lines(lang=DEFAULT_LANGUAGE):
    for text in fixed_lines(lang).values():
        _prewarm(text, lang)


def _wav(audio, cache=False):
    """cache=True only for fixed lines. Personalised audio (greeting, medicine) must never
    be cached by the browser: its address stays the same when the name or language changes."""
    header = "private, max-age=3600" if cache else "no-store"
    return Response(content=audio, media_type="audio/wav", headers={"Cache-Control": header})


# ---------- general ----------

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/warmup")
def warmup(background: BackgroundTasks, lang: str = DEFAULT_LANGUAGE):
    """Prepares the fixed lines so the first call has no delay. Repeats cost nothing."""
    background.add_task(_prewarm_fixed_lines, lang if lang in LANGUAGES else DEFAULT_LANGUAGE)
    return {"status": "warming up"}


@app.post("/api/w")
def new_workspace():
    wid, workspace = _create_workspace()
    with LOCK:
        return {"id": wid, "state": workspace.snapshot()}


@app.get("/api/w/{wid}/state")
def get_state(wid: str):
    """Both screens poll this. Each poll also runs the 'is a dose due?' check."""
    with LOCK:
        workspace = _get(wid)
        workspace.tick(_now())
        return workspace.snapshot()


@app.post("/api/w/{wid}/reset")
def reset(wid: str):
    with LOCK:
        _get(wid)
        workspace = Workspace()
        workspace.load_sample_history(_now())
        WORKSPACES[wid] = workspace
        return workspace.snapshot()


@app.get("/api/w/{wid}/history")
def history(wid: str):
    with LOCK:
        return {"calls": _get(wid).call_history()}


# ---------- caretaker ----------

@app.post("/api/w/{wid}/parent")
def set_parent(wid: str, body: ParentIn, background: BackgroundTasks):
    """Saves the parent's name. The agent greets them by it."""
    with LOCK:
        workspace = _get(wid)
        workspace.set_parent_name(body.name)
        lang = workspace.language
        greeting = greeting_text(workspace.parent_name, lang)
        state = workspace.snapshot()
    background.add_task(_prewarm, greeting, lang)
    return state


@app.post("/api/w/{wid}/language")
def set_language(wid: str, body: LanguageIn, background: BackgroundTasks):
    """Chooses the language the agent speaks and listens in."""
    with LOCK:
        workspace = _get(wid)
        lang = workspace.set_language(body.language)
        texts = [greeting_text(workspace.parent_name, lang)]
        waiting = {d.medicine_id for d in workspace.doses.values() if not d.reminder.is_finished}
        for medicine_id in waiting:
            medicine = workspace.medicines[medicine_id]
            texts.append(reminder_intro_text(medicine.name, medicine.appearance, lang))
        state = workspace.snapshot()
    background.add_task(_prewarm_fixed_lines, lang)
    for text in texts:
        background.add_task(_prewarm, text, lang)
    return state


@app.post("/api/w/{wid}/sample/clear")
def clear_sample(wid: str):
    with LOCK:
        workspace = _get(wid)
        workspace.clear_sample_data()
        return workspace.snapshot()


@app.post("/api/w/{wid}/sample/restore")
def restore_sample(wid: str):
    with LOCK:
        workspace = _get(wid)
        workspace.restore_sample_data(_now())
        return workspace.snapshot()


@app.post("/api/w/{wid}/medicines")
def add_medicine(wid: str, body: MedicineIn):
    with LOCK:
        workspace = _get(wid)
        medicine = workspace.add_medicine(body.name, body.appearance)
        return {"medicine": asdict(medicine), "state": workspace.snapshot()}


@app.post("/api/w/{wid}/doses")
def schedule_dose(wid: str, body: DoseIn, background: BackgroundTasks):
    if body.due_at is None and body.in_minutes is None:
        raise BadRequest("Choose a time: in_minutes or due_at.")
    with LOCK:
        workspace = _get(wid)
        due = body.due_at if body.due_at is not None else _now() + timedelta(minutes=body.in_minutes)
        dose = workspace.schedule_dose(body.medicine_id, due)
        medicine = workspace.medicines[dose.medicine_id]
        lang = workspace.language
        texts = [greeting_text(workspace.parent_name, lang),
                 reminder_intro_text(medicine.name, medicine.appearance, lang)]
        result = {"dose_id": dose.id, "state": workspace.snapshot()}
    for text in texts:
        background.add_task(_prewarm, text, lang)  # so the call starts without a wait
    return result


@app.post("/api/w/{wid}/doses/{dose_id}/call")
def call_now(wid: str, dose_id: str):
    """Rings the parent's phone now. Also used for 'Call again'."""
    with LOCK:
        workspace = _get(wid)
        workspace.call_now(dose_id, _now())
        return workspace.snapshot()


# ---------- parent's phone ----------

@app.post("/api/w/{wid}/doses/{dose_id}/answer")
def answer(wid: str, dose_id: str):
    with LOCK:
        workspace = _get(wid)
        result = workspace.answer(dose_id, _now())
        return _turn_response(wid, workspace, dose_id, result)


@app.post("/api/w/{wid}/doses/{dose_id}/audio")
def parent_audio(wid: str, dose_id: str, file: UploadFile = File(...)):
    """The parent's spoken reply: transcribe, understand, update the dose."""
    data = file.file.read(MAX_AUDIO_BYTES + 1)
    if len(data) > MAX_AUDIO_BYTES:
        raise HTTPException(413, "The recording is too large.")
    if not data:
        raise BadRequest("The recording was empty.")
    with LOCK:
        workspace = _get(wid)
        question = _check_in_call(workspace, dose_id)  # spend nothing on a dose that is not in a call
        lang = workspace.language
    if not TURN_BUDGET.allow():
        raise HTTPException(503, "The demo is busy right now. Please try again in a few minutes.")

    try:
        with _speaking(lang):
            text, stt = voice.transcribe(data, file.content_type or "audio/webm")
    except voice.TranscriptionFailed:
        text, stt = None, "failed"

    if text is None:
        label, source, spoken = "unclear", "stt_error", "(could not transcribe)"
    elif not text.strip():
        label, source, spoken = "no_response", "silence", ""
    else:
        label, source = classify(text, question=question)
        spoken = text

    with LOCK:
        workspace = _get(wid)
        if label == "no_response":
            result = workspace.no_response(dose_id, _now(), note="No speech heard.")
        else:
            result = workspace.parent_reply(dose_id, spoken, label, source, _now())
        return _turn_response(wid, workspace, dose_id, result,
                              {"heard": text or "", "label": label, "stt": stt})


@app.post("/api/w/{wid}/doses/{dose_id}/reply")
def parent_text(wid: str, dose_id: str, body: ReplyIn):
    """Typed reply: a fallback when the microphone does not work."""
    if not body.text.strip():
        raise BadRequest("The reply was empty.")
    with LOCK:
        question = _check_in_call(_get(wid), dose_id)
    label, source = _classify_text(body.text, question)
    with LOCK:
        workspace = _get(wid)
        result = workspace.parent_reply(dose_id, body.text, label, source, _now())
        return _turn_response(wid, workspace, dose_id, result,
                              {"heard": body.text, "label": label})


@app.post("/api/w/{wid}/doses/{dose_id}/silence")
def parent_silence(wid: str, dose_id: str):
    """No speech heard, or the parent hung up."""
    with LOCK:
        workspace = _get(wid)
        result = workspace.no_response(dose_id, _now())
        return _turn_response(wid, workspace, dose_id, result,
                              {"heard": "", "label": "no_response"})


# ---------- audio ----------

@app.get("/api/audio/line/{action}")
def fixed_line_audio(action: str, lang: str = DEFAULT_LANGUAGE):
    """Audio for one fixed script line. Only lines from the script can be requested."""
    text = fixed_lines(lang).get(action) if lang in LANGUAGES else None
    if text is None:
        raise HTTPException(404, "No such line.")
    return _wav(_synthesize(text, lang), cache=True)


@app.get("/api/w/{wid}/audio/greeting")
def greeting_audio(wid: str):
    with LOCK:
        workspace = _get(wid)
        lang = workspace.language
        text = greeting_text(workspace.parent_name, lang)
    return _wav(_synthesize(text, lang))


@app.get("/api/w/{wid}/audio/reminder/{medicine_id}")
def reminder_audio(wid: str, medicine_id: str):
    """The reminder for one saved medicine, built on the server."""
    with LOCK:
        workspace = _get(wid)
        medicine = workspace.medicines.get(medicine_id)
        if medicine is None:
            raise HTTPException(404, "No such medicine.")
        lang = workspace.language
        text = reminder_intro_text(medicine.name, medicine.appearance, lang)
    return _wav(_synthesize(text, lang))


@app.get("/api/w/{wid}/audio/info/{medicine_id}")
def info_audio(wid: str, medicine_id: str):
    """The medicine's name and look, built on the server from what the caretaker saved."""
    with LOCK:
        workspace = _get(wid)
        medicine = workspace.medicines.get(medicine_id)
        if medicine is None:
            raise HTTPException(404, "No such medicine.")
        lang = workspace.language
        text = medicine_info_text(medicine.name, medicine.appearance, lang)
    return _wav(_synthesize(text, lang))


@app.get("/api/w/{wid}/preview/{medicine_id}")
def preview(wid: str, medicine_id: str):
    """What the parent will hear for this medicine, so the caretaker can check it."""
    with LOCK:
        workspace = _get(wid)
        medicine = workspace.medicines.get(medicine_id)
        if medicine is None:
            raise HTTPException(404, "No such medicine.")
        lines = reminder_lines(workspace.parent_name, medicine.name, medicine.appearance,
                               workspace.language)
        return {"say": _items_for(wid, workspace, medicine, lines)}

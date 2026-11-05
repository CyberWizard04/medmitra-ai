"""HTTP API for the demo.
Runs on Render in production. The Groq key lives only in Render's settings,
never in the frontend or in this repository.
"""
import os
import time
from collections import deque

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from medmitra.brain import classify, safety_check
from medmitra.reminder import Action, DoseReminder, Event, State

DEMO_PARENT = "Demo Parent"
DEMO_MEDICINE = "Demo Medicine"

# What the agent says, in Hindi. Only spoken actions appear here.
SPEECH = {
    Action.SPEAK_REMINDER: "नमस्ते। दवाई लेने का समय हो गया है। क्या आपने दवाई ले ली है?",
    Action.SPEAK_CLARIFY: "माफ़ कीजिए, मुझे समझ नहीं आया। कृपया बताइए, क्या आपने दवाई ले ली है?",
    Action.SPEAK_THANKS: "धन्यवाद। आपकी दवाई दर्ज कर ली गई है। अपना ख़याल रखिए।",
    Action.SPEAK_RETRY_NOTICE: "ठीक है। थोड़ी देर बाद फिर याद दिलाएँगे।",
    Action.SPEAK_FAMILY_NOTICE: "आपकी दवाई अभी दर्ज नहीं हुई है। हम आपके परिवार को सूचित कर रहे हैं।",
    Action.SPEAK_HELP_ACK: (
        "आपने मदद माँगी है। कृपया तुरंत पास में किसी व्यक्ति को बुलाइए। "
        "सीने में दर्द या सांस की तकलीफ़ हो, तो 108 या 112 पर तुरंत कॉल कीजिए।"
    ),
}

# What the caregiver sees. English, because the caregiver reads it.
ALERTS = {
    Action.ALERT_WARNING: "Dose missed after all attempts. Please check on the parent.",
    Action.ALERT_URGENT: "URGENT: parent asked for help or reported pain or dizziness. Contact them now.",
}


class Budget:
    """Caps AI calls per rolling window. Silent: judges never see it.
    It protects the Groq account if the link is passed around."""

    def __init__(self, max_calls, window_seconds=3600, clock=time.time):
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        self.clock = clock
        self.calls = deque()

    def allow(self):
        now = self.clock()
        while self.calls and now - self.calls[0] > self.window_seconds:
            self.calls.popleft()
        if len(self.calls) >= self.max_calls:
            return False
        self.calls.append(now)
        return True


MAX_AI_CALLS_PER_HOUR = int(os.environ.get("MAX_AI_CALLS_PER_HOUR", "200"))
BUDGET = Budget(MAX_AI_CALLS_PER_HOUR)
_state = {"dose": None}  # one demo call at a time, held in memory

app = FastAPI(title="MedMitra AI demo")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in
                   os.environ.get("ALLOWED_ORIGINS", "http://localhost:3000").split(",")
                   if o.strip()],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


class ReplyIn(BaseModel):
    text: str


def _respond(dose, actions):
    return {
        "say": [SPEECH[a] for a in actions if a in SPEECH],
        "caregiver_alerts": [ALERTS[a] for a in actions if a in ALERTS],
        "retry_scheduled": Action.SCHEDULE_RETRY in actions,
        "state": dose.state.value,
        "attempt": dose.attempts,
        "finished": dose.is_finished,
    }


def _classify_reply(text):
    # Safety words are handled inside classify() without AI, so they always
    # work, even when the budget is used up.
    if safety_check(text):
        return classify(text)
    if not BUDGET.allow():
        raise HTTPException(503, "The demo is busy right now. Please try again in a few minutes.")
    return classify(text)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/call/start")
def start_call():
    dose = DoseReminder(DEMO_PARENT, DEMO_MEDICINE)
    _state["dose"] = dose
    return _respond(dose, dose.handle(Event.START))


@app.post("/api/call/reply")
def reply(body: ReplyIn):
    dose = _state["dose"]
    if dose is None:
        raise HTTPException(404, "No call in progress. Start a call first.")
    if dose.is_finished:
        raise HTTPException(409, "This dose is already finished.")
    if dose.state == State.RETRY_WAIT:
        raise HTTPException(409, "Waiting for the next reminder.")
    label, source = _classify_reply(body.text)
    actions = dose.handle(Event(label))
    return {**_respond(dose, actions), "heard": label, "source": source}


@app.post("/api/call/retry")
def retry_call():
    dose = _state["dose"]
    if dose is None or dose.state != State.RETRY_WAIT:
        raise HTTPException(409, "No retry is waiting.")
    return _respond(dose, dose.handle(Event.RETRY_DUE))


@app.get("/api/caregiver")
def caregiver_status():
    dose = _state["dose"]
    if dose is None:
        return {"status": "no call yet"}
    return dose.to_dict()

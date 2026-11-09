"""In-memory data for one demo workspace (one visitor).

Holds the parent, medicines, scheduled doses, call transcripts, alerts and the
numbers the caretaker screen shows. Plain Python: no internet, no AI, no
database. A server restart clears it, so the demo starts from sample data.
"""
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from medmitra.config import APP_TIMEZONE
from medmitra.reminder import Action, DoseReminder, Event, InvalidTransition, State
from medmitra.languages import DEFAULT_LANGUAGE, LANGUAGES, get_language, language_list
from medmitra.script import ALERT_TEXT, spoken_lines

MAX_NAME = 60
MAX_PARENT_NAME = 40
MAX_APPEARANCE = 160
MAX_REPLY_TEXT = 500
MAX_MEDICINES = 10
MAX_DOSES = 60
RING_SECONDS = 30
REPLY_LABELS = ("taken", "not_taken", "unclear", "needs_help", "off_topic", "asks_medicine")

_STATUS = {
    State.PENDING: "scheduled",
    State.REMINDED: "in_call",
    State.RETRY_WAIT: "retry_wait",
    State.CONFIRMED: "confirmed",
    State.MISSED: "missed",
    State.ESCALATED: "escalated",
}
_BUCKET = {"confirmed": "taken", "missed": "missed", "escalated": "needs_help"}


class BadRequest(ValueError):
    """The input is not acceptable (HTTP 400)."""


class NotFound(LookupError):
    """No such medicine or dose (HTTP 404)."""


class Conflict(Exception):
    """The action does not fit the dose's current state (HTTP 409)."""


def _new_id():
    return uuid.uuid4().hex[:8]


def _clean(text, limit, what):
    kept = "".join(ch for ch in str(text or "")
                   if ch.isprintable() or ch in " \t\n\u200c\u200d")
    cleaned = " ".join(kept.split())
    if len(cleaned) > limit:
        raise BadRequest(f"{what} is too long (maximum {limit} characters).")
    return cleaned


def _blank_counts():
    return {"taken": 0, "missed": 0, "needs_help": 0, "waiting": 0, "total": 0}


@dataclass
class Medicine:
    id: str
    name: str
    appearance: str = ""
    sample: bool = False


@dataclass
class Dose:
    id: str
    medicine_id: str
    due_at: datetime
    reminder: DoseReminder
    ringing: bool = False
    ring_started_at: datetime = None
    sample: bool = False
    transcript: list = field(default_factory=list)

    def status(self):
        if self.ringing:
            return "ringing"
        return _STATUS[self.reminder.state]


class Workspace:
    def __init__(self, parent_name=""):
        self.parent_name = parent_name
        self.medicines = {}
        self.doses = {}
        self.alerts = []
        self.language = DEFAULT_LANGUAGE

    # ---------- small helpers ----------
    @staticmethod
    def _now(now):
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            raise BadRequest("Time must include a timezone.")
        return now.astimezone(timezone.utc)

    def _medicine(self, medicine_id):
        try:
            return self.medicines[medicine_id]
        except KeyError:
            raise NotFound("No such medicine.") from None

    def _dose(self, dose_id):
        try:
            return self.doses[dose_id]
        except KeyError:
            raise NotFound("No such dose.") from None

    def _handle(self, dose, event, now):
        try:
            return dose.reminder.handle(event, now=now)
        except InvalidTransition as error:
            raise Conflict(str(error)) from None

    def _note(self, dose, speaker, text, now, label=None, source=None):
        dose.transcript.append({
            "at": now.isoformat(), "speaker": speaker, "text": text,
            "label": label, "source": source,
        })

    def _record_actions(self, dose, actions, now):
        """Write what the agent said and raise alerts. Returns the spoken lines."""
        medicine = self.medicines[dose.medicine_id]
        lines = spoken_lines(actions, self.parent_name, medicine.name, medicine.appearance,
                             self.language)
        for line in lines:
            self._note(dose, "agent", line, now)
        if not dose.sample:
            for action in actions:
                template = ALERT_TEXT.get(action)
                if template:
                    self.alerts.append({
                        "at": now.isoformat(),
                        "dose_id": dose.id,
                        "medicine": medicine.name,
                        "level": "urgent" if action == Action.ALERT_URGENT else "warning",
                        "message": template.format(medicine=medicine.name),
                    })
        return lines

    def _turn_result(self, dose, actions, now):
        lines = self._record_actions(dose, actions, now)
        return {
            "say": lines,
            "retry_available": Action.SCHEDULE_RETRY in actions,
            "status": dose.status(),
            "finished": dose.reminder.is_finished,
        }

    def _dose_view(self, dose):
        medicine = self.medicines[dose.medicine_id]
        return {
            "id": dose.id,
            "medicine_id": medicine.id,
            "medicine": medicine.name,
            "due_at": dose.due_at.isoformat(),
            "status": dose.status(),
            "attempts": dose.reminder.attempts,
            "sample": dose.sample,
        }

    def set_parent_name(self, name):
        """Who the agent greets. Blank means no name is used."""
        self.parent_name = _clean(name, MAX_PARENT_NAME, "Parent name")
        return self.parent_name

    def last_agent_line(self, dose_id):
        """The last thing the agent said on this dose: what the parent is answering."""
        for line in reversed(self._dose(dose_id).transcript):
            if line["speaker"] == "agent":
                return line["text"]
        return None

    # ---------- caretaker actions ----------
    def add_medicine(self, name, appearance=""):
        name = _clean(name, MAX_NAME, "Medicine name")
        appearance = _clean(appearance, MAX_APPEARANCE, "Description").rstrip("।.!? ")
        if not name:
            raise BadRequest("Medicine name is required.")
        if len(self.medicines) >= MAX_MEDICINES:
            raise BadRequest(f"Demo limit reached: {MAX_MEDICINES} medicines.")
        medicine = Medicine(id=_new_id(), name=name, appearance=appearance)
        self.medicines[medicine.id] = medicine
        return medicine

    def schedule_dose(self, medicine_id, due_at):
        medicine = self._medicine(medicine_id)
        due_at = self._now(due_at)
        if len(self.doses) >= MAX_DOSES:
            raise BadRequest(f"Demo limit reached: {MAX_DOSES} doses.")
        dose = Dose(id=_new_id(), medicine_id=medicine.id, due_at=due_at,
                    reminder=DoseReminder(self.parent_name, medicine.name))
        self.doses[dose.id] = dose
        return dose

    def call_now(self, dose_id, now=None):
        """Ring the parent's phone now (also used for 'Call again')."""
        dose = self._dose(dose_id)
        now = self._now(now)
        if dose.reminder.is_finished:
            raise Conflict("This dose is already finished.")
        if dose.status() in ("ringing", "in_call"):
            raise Conflict("A call is already in progress for this dose.")
        dose.ringing = True
        dose.ring_started_at = now
        return dose

    # ---------- clock ----------
    def tick(self, now=None):
        """Ring doses that are due. Unanswered rings count as no answer."""
        now = self._now(now)
        changed = []
        for dose in self.doses.values():
            if dose.ringing:
                if (now - dose.ring_started_at).total_seconds() >= RING_SECONDS:
                    self._ring_expired(dose, now)
                    changed.append(dose.id)
            elif dose.reminder.state == State.PENDING and dose.due_at <= now:
                dose.ringing = True
                dose.ring_started_at = now
                changed.append(dose.id)
        return changed

    def _ring_expired(self, dose, now):
        dose.ringing = False
        reminder = dose.reminder
        if reminder.state == State.PENDING:
            reminder.handle(Event.START, now=now)
        elif reminder.state == State.RETRY_WAIT:
            reminder.handle(Event.RETRY_DUE, now=now)
        self._note(dose, "system", "Call not answered.", now)
        self._record_actions(dose, self._handle(dose, Event.NO_RESPONSE, now), now)

    # ---------- parent's phone ----------
    def answer(self, dose_id, now=None):
        dose = self._dose(dose_id)
        now = self._now(now)
        if not dose.ringing:
            raise Conflict("This dose is not ringing.")
        event = Event.START if dose.reminder.state == State.PENDING else Event.RETRY_DUE
        actions = self._handle(dose, event, now)
        dose.ringing = False
        return self._turn_result(dose, actions, now)

    def parent_reply(self, dose_id, text, label, source, now=None):
        dose = self._dose(dose_id)
        now = self._now(now)
        if label not in REPLY_LABELS:
            raise BadRequest(f"Unknown reply label: {label}")
        if dose.reminder.state != State.REMINDED:
            raise Conflict("No call is in progress for this dose.")
        spoken = " ".join(str(text or "").split())[:MAX_REPLY_TEXT]
        self._note(dose, "parent", spoken, now, label=label, source=source)
        actions = self._handle(dose, Event(label), now)
        return self._turn_result(dose, actions, now)

    def no_response(self, dose_id, now=None, note="No reply heard."):
        """Silence, or the parent hung up mid-call."""
        dose = self._dose(dose_id)
        now = self._now(now)
        if dose.reminder.state != State.REMINDED:
            raise Conflict("No call is in progress for this dose.")
        self._note(dose, "system", note, now)
        actions = self._handle(dose, Event.NO_RESPONSE, now)
        return self._turn_result(dose, actions, now)

    # ---------- what the caretaker screen shows ----------
    def summary(self):
        tz = ZoneInfo(APP_TIMEZONE)
        totals = _blank_counts()
        per_medicine = {m.id: {"medicine": m.name, **_blank_counts()}
                        for m in self.medicines.values()}
        by_day = {}
        for dose in self.doses.values():
            bucket = _BUCKET.get(dose.status(), "waiting")
            day = dose.due_at.astimezone(tz).date().isoformat()
            day_row = by_day.setdefault(day, {"date": day, **_blank_counts()})
            for row in (totals, per_medicine[dose.medicine_id], day_row):
                row[bucket] += 1
                row["total"] += 1
        replies = {label: 0 for label in REPLY_LABELS}
        replies["no_answer"] = 0
        for dose in self.doses.values():
            for entry in dose.transcript:
                if entry["speaker"] == "parent" and entry["label"] in replies:
                    replies[entry["label"]] += 1
                elif entry["speaker"] == "system":
                    replies["no_answer"] += 1
        confirmed = [d.reminder.attempts for d in self.doses.values()
                     if d.reminder.state == State.CONFIRMED]
        avg_attempts = round(sum(confirmed) / len(confirmed), 1) if confirmed else None
        finished = totals["taken"] + totals["missed"] + totals["needs_help"]
        totals["adherence_percent"] = (
            round(100 * totals["taken"] / finished) if finished else None)
        return {
            "totals": totals,
            "per_medicine": list(per_medicine.values()),
            "by_day": [by_day[day] for day in sorted(by_day)],
            "replies": replies,
            "avg_attempts_to_confirm": avg_attempts,
        }

    def snapshot(self):
        """Everything the screens poll for, in one small piece of data."""
        ringing = [d for d in self.doses.values() if d.ringing]
        first = min(ringing, key=lambda d: d.ring_started_at) if ringing else None
        ordered = sorted(self.doses.values(), key=lambda d: d.due_at, reverse=True)
        return {
            "parent_name": self.parent_name,
            "language": self.language,
            "language_tag": get_language(self.language).tts_code,
            "languages": language_list(),
            "medicines": [asdict(m) for m in self.medicines.values()],
            "doses": [self._dose_view(d) for d in ordered],
            "alerts": list(reversed(self.alerts[-20:])),
            "summary": self.summary(),
            "ringing": ({"dose_id": first.id,
                         "medicine": self.medicines[first.medicine_id].name}
                        if first else None),
            "has_sample_data": any(d.sample for d in self.doses.values()),
        }

    def call_history(self):
        rows = []
        ordered = sorted(self.doses.values(), key=lambda d: d.due_at, reverse=True)
        for dose in ordered:
            if dose.transcript:
                row = self._dose_view(dose)
                row["transcript"] = list(dose.transcript)
                rows.append(row)
        return rows

    # ---------- language and sample data ----------
    SAMPLE_MEDICINES = 2   # what load_sample_history adds
    SAMPLE_DOSES = 14

    def set_language(self, code):
        """The language the agent speaks and listens in."""
        code = str(code or "").strip().lower()
        if code not in LANGUAGES:
            raise BadRequest("That language is not supported.")
        self.language = code
        return code

    def clear_sample_data(self):
        """Removes the fake history. A sample medicine a real dose still uses is kept."""
        for dose_id in [d.id for d in self.doses.values() if d.sample]:
            del self.doses[dose_id]
        in_use = {d.medicine_id for d in self.doses.values()}
        for medicine in list(self.medicines.values()):
            if medicine.sample:
                if medicine.id in in_use:
                    medicine.sample = False
                else:
                    del self.medicines[medicine.id]

    def restore_sample_data(self, now=None):
        """Brings the fake history back. The sample calls are always in Hindi."""
        if any(d.sample for d in self.doses.values()):
            raise Conflict("Sample data is already loaded.")
        if (len(self.medicines) + self.SAMPLE_MEDICINES > MAX_MEDICINES
                or len(self.doses) + self.SAMPLE_DOSES > MAX_DOSES):
            raise BadRequest("There is not enough room for the sample data. Use Reset demo instead.")
        keep = self.language
        self.language = DEFAULT_LANGUAGE
        try:
            self.load_sample_history(now)
        finally:
            self.language = keep

    # ---------- sample data ----------
    def load_sample_history(self, now=None):
        """Seven days of fake history so the analytics are never empty."""
        now = self._now(now)
        tz = ZoneInfo(APP_TIMEZONE)
        today = now.astimezone(tz).date()
        morning = self.add_medicine("विटामिन सी", "छोटी गोल नारंगी गोली")
        evening = self.add_medicine("कैल्शियम", "बड़ी सफ़ेद गोली")
        morning.sample = evening.sample = True
        outcomes = ["taken", "ask_taken", "taken", "retry_taken", "unclear_taken", "missed", "taken",
                    "chat_taken", "taken", "taken", "needs_help", "taken", "ask_taken", "retry_taken"]
        for i, outcome in enumerate(outcomes):
            day = today - timedelta(days=7 - i // 2)
            medicine, hour = (morning, 8) if i % 2 == 0 else (evening, 20)
            due = datetime(day.year, day.month, day.day, hour, 0,
                           tzinfo=tz).astimezone(timezone.utc)
            dose = self.schedule_dose(medicine.id, due)
            dose.sample = True
            self._seed_outcome(dose, outcome, due)

    def _seed_outcome(self, dose, outcome, due):
        reminder = dose.reminder
        clock = [due]

        def turn(event, parent_text=None, label=None):
            clock[0] += timedelta(minutes=2)
            if parent_text:
                self._note(dose, "parent", parent_text, clock[0],
                           label=label, source="sample")
            actions = reminder.handle(event, now=clock[0])
            self._record_actions(dose, actions, clock[0])

        turn(Event.START)
        if outcome == "taken":
            turn(Event.TAKEN, "हाँ, मैंने दवाई ले ली है।", "taken")
        elif outcome == "ask_taken":
            turn(Event.ASKS_MEDICINE, "कौन सी दवाई है?", "asks_medicine")
            turn(Event.TAKEN, "हाँ, ले ली।", "taken")
        elif outcome == "unclear_taken":
            turn(Event.UNCLEAR, "हम्म, ठीक है।", "unclear")
            turn(Event.TAKEN, "हाँ, ले ली।", "taken")
        elif outcome == "chat_taken":
            turn(Event.OFF_TOPIC, "आज मौसम कैसा है?", "off_topic")
            turn(Event.TAKEN, "हाँ, ले ली।", "taken")
        elif outcome == "retry_taken":
            turn(Event.NOT_TAKEN, "अभी नहीं, थोड़ी देर बाद।", "not_taken")
            turn(Event.RETRY_DUE)
            turn(Event.TAKEN, "हाँ, अब ले ली।", "taken")
        elif outcome == "missed":
            for attempt in range(3):
                self._note(dose, "system", "Call not answered.", clock[0])
                turn(Event.NO_RESPONSE)
                if attempt < 2:
                    turn(Event.RETRY_DUE)
        elif outcome == "needs_help":
            turn(Event.NEEDS_HELP, "मुझे चक्कर आ रहे हैं।", "needs_help")

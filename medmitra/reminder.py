"""Dose reminder state machine.

Pure logic: no internet, no database, no AI.
It tracks what happened to ONE dose and says what should happen next.
It never gives medical advice.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class State(str, Enum):
    PENDING = "pending"        # dose created, nothing said yet
    REMINDED = "reminded"      # reminder spoken, waiting for the parent's answer
    RETRY_WAIT = "retry_wait"  # not confirmed yet, waiting to try again
    CONFIRMED = "confirmed"    # parent said they took it (finished)
    MISSED = "missed"          # all attempts used, no confirmation (finished)
    ESCALATED = "escalated"    # parent asked for help (finished, urgent)


class Event(str, Enum):
    START = "start"              # "Call me" pressed (or a scheduler, later)
    TAKEN = "taken"              # parent said they took the medicine
    NOT_TAKEN = "not_taken"      # parent said not yet / later
    UNCLEAR = "unclear"          # answer could not be understood
    OFF_TOPIC = "off_topic"      # understood, but not about the medicine
    ASKS_MEDICINE = "asks_medicine"  # asked which medicine it is or what it looks like
    NEEDS_HELP = "needs_help"    # parent said they need help or feel unwell
    NO_RESPONSE = "no_response"  # silence, or the call ended
    RETRY_DUE = "retry_due"      # time to try again


class Action(str, Enum):
    """What the outside world should do. The state machine never does these itself."""
    SPEAK_REMINDER = "speak_reminder"
    SPEAK_CLARIFY = "speak_clarify"
    SPEAK_OFF_TOPIC = "speak_off_topic"
    SPEAK_MEDICINE_INFO = "speak_medicine_info"
    SPEAK_THANKS = "speak_thanks"
    SPEAK_RETRY_NOTICE = "speak_retry_notice"
    SPEAK_FAMILY_NOTICE = "speak_family_notice"
    SPEAK_HELP_ACK = "speak_help_ack"
    SCHEDULE_RETRY = "schedule_retry"
    ALERT_WARNING = "alert_warning"
    ALERT_URGENT = "alert_urgent"


class InvalidTransition(Exception):
    """Raised when an event makes no sense in the current state."""


FINISHED_STATES = {State.CONFIRMED, State.MISSED, State.ESCALATED}


@dataclass
class DoseReminder:
    parent_name: str
    medicine: str
    max_attempts: int = 3
    max_clarifications: int = 2
    max_info_requests: int = 2
    state: State = State.PENDING
    attempts: int = 0
    clarifications: int = 0
    info_requests: int = 0
    history: list = field(default_factory=list)

    @property
    def is_finished(self):
        return self.state in FINISHED_STATES

    def handle(self, event, now=None):
        """Apply one event. Returns the list of Actions to carry out."""
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            raise ValueError("now must include a timezone (use UTC)")
        before = self.state
        actions = self._decide(event)
        self.history.append({
            "at": now.astimezone(timezone.utc).isoformat(),
            "event": event.value,
            "from": before.value,
            "to": self.state.value,
            "attempt": self.attempts,
        })
        return actions

    def to_dict(self):
        """Plain data, ready to save in a database or send to the browser."""
        return {
            "parent_name": self.parent_name,
            "medicine": self.medicine,
            "state": self.state.value,
            "attempts": self.attempts,
            "clarifications": self.clarifications,
            "is_finished": self.is_finished,
            "history": list(self.history),
        }

    def _decide(self, event):
        if self.is_finished:
            raise InvalidTransition(
                f"dose is already {self.state.value}; cannot handle {event.value}")

        if self.state == State.PENDING and event == Event.START:
            self.state = State.REMINDED
            self.attempts = 1
            self.clarifications = 0
            self.info_requests = 0
            return [Action.SPEAK_REMINDER]

        if self.state == State.RETRY_WAIT and event == Event.RETRY_DUE:
            self.state = State.REMINDED
            self.attempts += 1
            self.clarifications = 0
            self.info_requests = 0
            return [Action.SPEAK_REMINDER]

        if self.state == State.REMINDED:
            if event == Event.TAKEN:
                self.state = State.CONFIRMED
                return [Action.SPEAK_THANKS]
            if event == Event.NEEDS_HELP:
                self.state = State.ESCALATED
                return [Action.SPEAK_HELP_ACK, Action.ALERT_URGENT]
            if event == Event.ASKS_MEDICINE:
                if self.info_requests < self.max_info_requests:
                    self.info_requests += 1
                    return [Action.SPEAK_MEDICINE_INFO]
                return self._no_confirmation(parent_can_hear=True)
            if event in (Event.UNCLEAR, Event.OFF_TOPIC):
                if self.clarifications < self.max_clarifications:
                    self.clarifications += 1
                    if event == Event.OFF_TOPIC:
                        return [Action.SPEAK_OFF_TOPIC]
                    return [Action.SPEAK_CLARIFY]
                return self._no_confirmation(parent_can_hear=True)
            if event == Event.NOT_TAKEN:
                return self._no_confirmation(parent_can_hear=True)
            if event == Event.NO_RESPONSE:
                return self._no_confirmation(parent_can_hear=False)

        raise InvalidTransition(
            f"event {event.value} is not allowed in state {self.state.value}")

    def _no_confirmation(self, parent_can_hear):
        if self.attempts >= self.max_attempts:
            self.state = State.MISSED
            actions = [Action.ALERT_WARNING]
            if parent_can_hear:
                actions.insert(0, Action.SPEAK_FAMILY_NOTICE)
            return actions
        self.state = State.RETRY_WAIT
        actions = [Action.SCHEDULE_RETRY]
        if parent_can_hear:
            actions.insert(0, Action.SPEAK_RETRY_NOTICE)
        return actions

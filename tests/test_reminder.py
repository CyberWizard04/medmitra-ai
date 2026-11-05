import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from medmitra.reminder import (
    Action, DoseReminder, Event, FINISHED_STATES, InvalidTransition, State,
)


def new_dose(**kwargs):
    return DoseReminder("Demo Parent", "Demo Medicine", **kwargs)


def test_happy_path_taken():
    dose = new_dose()
    assert dose.handle(Event.START) == [Action.SPEAK_REMINDER]
    assert dose.state == State.REMINDED
    assert dose.handle(Event.TAKEN) == [Action.SPEAK_THANKS]
    assert dose.state == State.CONFIRMED
    assert dose.is_finished
    assert dose.attempts == 1


def test_not_taken_then_taken_on_retry():
    dose = new_dose()
    dose.handle(Event.START)
    assert dose.handle(Event.NOT_TAKEN) == [Action.SPEAK_RETRY_NOTICE, Action.SCHEDULE_RETRY]
    assert dose.state == State.RETRY_WAIT
    assert dose.handle(Event.RETRY_DUE) == [Action.SPEAK_REMINDER]
    assert dose.attempts == 2
    dose.handle(Event.TAKEN)
    assert dose.state == State.CONFIRMED


def test_silence_until_missed():
    dose = new_dose()
    dose.handle(Event.START)
    for _ in range(2):
        assert dose.handle(Event.NO_RESPONSE) == [Action.SCHEDULE_RETRY]
        dose.handle(Event.RETRY_DUE)
    assert dose.handle(Event.NO_RESPONSE) == [Action.ALERT_WARNING]
    assert dose.state == State.MISSED
    assert dose.attempts == 3


def test_needs_help_escalates_immediately():
    dose = new_dose()
    dose.handle(Event.START)
    actions = dose.handle(Event.NEEDS_HELP)
    assert dose.state == State.ESCALATED
    assert Action.ALERT_URGENT in actions
    assert Action.SPEAK_HELP_ACK in actions
    assert dose.is_finished


def test_unclear_asks_again_then_gives_up():
    dose = new_dose()
    dose.handle(Event.START)
    assert dose.handle(Event.UNCLEAR) == [Action.SPEAK_CLARIFY]
    assert dose.handle(Event.UNCLEAR) == [Action.SPEAK_CLARIFY]
    assert dose.state == State.REMINDED
    assert dose.handle(Event.UNCLEAR) == [Action.SPEAK_RETRY_NOTICE, Action.SCHEDULE_RETRY]
    assert dose.state == State.RETRY_WAIT


def test_unclear_counter_resets_on_new_attempt():
    dose = new_dose()
    dose.handle(Event.START)
    dose.handle(Event.UNCLEAR)
    dose.handle(Event.UNCLEAR)
    dose.handle(Event.NO_RESPONSE)
    dose.handle(Event.RETRY_DUE)
    assert dose.clarifications == 0
    assert dose.handle(Event.UNCLEAR) == [Action.SPEAK_CLARIFY]


def test_last_attempt_not_taken_becomes_missed():
    dose = new_dose(max_attempts=1)
    dose.handle(Event.START)
    assert dose.handle(Event.NOT_TAKEN) == [Action.SPEAK_FAMILY_NOTICE, Action.ALERT_WARNING]
    assert dose.state == State.MISSED


def test_finished_dose_cannot_change():
    dose = new_dose()
    dose.handle(Event.START)
    dose.handle(Event.TAKEN)
    with pytest.raises(InvalidTransition):
        dose.handle(Event.NOT_TAKEN)
    assert dose.state == State.CONFIRMED


def test_wrong_event_for_state_is_rejected_and_changes_nothing():
    dose = new_dose()
    with pytest.raises(InvalidTransition):
        dose.handle(Event.TAKEN)
    assert dose.state == State.PENDING
    assert dose.history == []


def test_history_records_every_step_in_utc():
    dose = new_dose()
    ist_8am = datetime(2026, 11, 5, 8, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    dose.handle(Event.START, now=ist_8am)
    dose.handle(Event.TAKEN, now=datetime(2026, 11, 5, 2, 31, tzinfo=timezone.utc))
    assert len(dose.history) == 2
    assert dose.history[0]["at"] == "2026-11-05T02:30:00+00:00"
    assert dose.history[0]["from"] == "pending"
    assert dose.history[1]["to"] == "confirmed"


def test_naive_time_is_rejected():
    dose = new_dose()
    with pytest.raises(ValueError):
        dose.handle(Event.START, now=datetime(2026, 11, 5, 8, 0))
    assert dose.state == State.PENDING


def test_every_state_and_event_combination_is_handled():
    for state in State:
        for event in Event:
            dose = new_dose()
            dose.state = state
            if state in FINISHED_STATES:
                with pytest.raises(InvalidTransition):
                    dose.handle(event)
            else:
                try:
                    dose.handle(event)
                except InvalidTransition:
                    pass


def test_to_dict_can_be_saved_as_json():
    dose = new_dose()
    dose.handle(Event.START)
    dose.handle(Event.TAKEN)
    saved = json.loads(json.dumps(dose.to_dict()))
    assert saved["state"] == "confirmed"
    assert saved["is_finished"] is True
    assert len(saved["history"]) == 2

from datetime import datetime, timedelta, timezone

import pytest

from medmitra.brain import LABELS, SYSTEM_PROMPT, classify, parse_label
from medmitra.reminder import Action, DoseReminder, Event, State
from medmitra.script import SPEECH, reminder_text, spoken_lines
from medmitra.store import (
    MAX_DOSES, RING_SECONDS, BadRequest, Conflict, NotFound, Workspace,
)

T0 = datetime(2026, 11, 7, 4, 30, tzinfo=timezone.utc)  # 10:00 in India


def later(seconds):
    return T0 + timedelta(seconds=seconds)


def make():
    ws = Workspace()
    medicine = ws.add_medicine("Vitamin C", "small round orange tablet")
    dose = ws.schedule_dose(medicine.id, T0)
    return ws, medicine, dose


def start_call(ws, dose):
    ws.call_now(dose.id, T0)
    return ws.answer(dose.id, later(1))


def fake_model(reply):
    def transport(payload):
        return {"choices": [{"message": {"content": reply}}]}
    return transport


# ---------- off-topic handling in the reminder logic and the brain ----------

def test_off_topic_is_a_label_and_parses():
    assert "off_topic" in LABELS
    assert parse_label("off-topic") == "off_topic"
    assert parse_label("Off topic.") == "off_topic"


def test_off_topic_redirects_and_shares_the_reask_limit():
    dose = DoseReminder("Demo Parent", "Demo Medicine")
    dose.handle(Event.START)
    assert dose.handle(Event.OFF_TOPIC) == [Action.SPEAK_OFF_TOPIC]
    assert dose.handle(Event.UNCLEAR) == [Action.SPEAK_CLARIFY]
    assert dose.state == State.REMINDED
    assert dose.handle(Event.OFF_TOPIC) == [Action.SPEAK_RETRY_NOTICE, Action.SCHEDULE_RETRY]
    assert dose.state == State.RETRY_WAIT


def test_endless_chatter_cannot_hold_the_call_open():
    dose = DoseReminder("Demo Parent", "Demo Medicine")
    dose.handle(Event.START)
    for _ in range(40):
        if dose.is_finished:
            break
        if dose.state == State.RETRY_WAIT:
            dose.handle(Event.RETRY_DUE)
        else:
            dose.handle(Event.OFF_TOPIC)
    assert dose.state == State.MISSED


def test_off_topic_model_reply_is_used():
    result = classify("what is the weather today", transport=fake_model("off_topic"))
    assert result == ("off_topic", "groq")


def test_pain_plus_chat_is_still_help_without_ai():
    def must_not_run(payload):
        raise AssertionError("the AI should not run for safety terms")
    result = classify("what is for dinner? my chest hurts", transport=must_not_run)
    assert result == ("needs_help", "safety_check")


def test_prompt_lists_every_label():
    for label in LABELS:
        assert label in SYSTEM_PROMPT


# ---------- medicines, scheduling and the call flow ----------

def test_medicine_input_is_checked_and_cleaned():
    ws = Workspace()
    with pytest.raises(BadRequest):
        ws.add_medicine("   ")
    with pytest.raises(BadRequest):
        ws.add_medicine("x" * 61)
    with pytest.raises(BadRequest):
        ws.add_medicine("Vit C", "y" * 161)
    medicine = ws.add_medicine("  Vitamin   C \n", "  round। ")
    assert medicine.name == "Vitamin C"
    assert medicine.appearance == "round"


def test_scheduling_checks_inputs():
    ws, medicine, _ = make()
    with pytest.raises(NotFound):
        ws.schedule_dose("nope", T0)
    with pytest.raises(BadRequest):
        ws.schedule_dose(medicine.id, datetime(2026, 11, 7, 10, 0))


def test_dose_rings_only_when_due():
    ws, _, dose = make()
    assert ws.tick(T0 - timedelta(seconds=1)) == []
    assert dose.status() == "scheduled"
    assert ws.tick(T0) == [dose.id]
    assert dose.status() == "ringing"
    assert ws.tick(later(5)) == []


def test_full_call_taken():
    ws, _, dose = make()
    started = start_call(ws, dose)
    assert started["say"] == [reminder_text("Vitamin C", "small round orange tablet")]
    assert dose.status() == "in_call"
    done = ws.parent_reply(dose.id, "haan, le li", "taken", "groq", later(10))
    assert done["finished"] is True
    assert done["status"] == "confirmed"
    assert [e["speaker"] for e in dose.transcript] == ["agent", "parent", "agent"]
    assert dose.transcript[1]["text"] == "haan, le li"
    assert dose.transcript[1]["label"] == "taken"


def test_unanswered_rings_end_as_missed_with_a_warning():
    ws, _, dose = make()
    now = T0
    ws.tick(now)
    for attempt in range(3):
        now += timedelta(seconds=RING_SECONDS)
        assert ws.tick(now) == [dose.id]
        if attempt < 2:
            assert dose.status() == "retry_wait"
            ws.call_now(dose.id, now)
    assert dose.status() == "missed"
    assert dose.reminder.attempts == 3
    assert ws.alerts[-1]["level"] == "warning"
    assert "Vitamin C" in ws.alerts[-1]["message"]


def test_help_request_is_urgent_and_final():
    ws, _, dose = make()
    start_call(ws, dose)
    result = ws.parent_reply(dose.id, "chakkar aa raha hai", "needs_help",
                             "safety_check", later(5))
    assert result["status"] == "escalated"
    assert ws.alerts[-1]["level"] == "urgent"
    assert ws.alerts[-1]["message"].startswith("URGENT")
    with pytest.raises(Conflict):
        ws.parent_reply(dose.id, "haan", "taken", "groq", later(9))


def test_not_taken_waits_for_call_again():
    ws, _, dose = make()
    start_call(ws, dose)
    result = ws.parent_reply(dose.id, "abhi nahi", "not_taken", "groq", later(5))
    assert result["retry_available"] is True
    assert result["status"] == "retry_wait"
    with pytest.raises(Conflict):
        ws.answer(dose.id, later(6))
    ws.call_now(dose.id, later(10))
    again = ws.answer(dose.id, later(12))
    assert dose.reminder.attempts == 2
    assert again["say"]
    ws.parent_reply(dose.id, "haan li", "taken", "groq", later(20))
    assert dose.status() == "confirmed"


def test_off_topic_reply_gets_the_fixed_redirect_line():
    ws, _, dose = make()
    start_call(ws, dose)
    result = ws.parent_reply(dose.id, "what is the weather", "off_topic", "groq", later(5))
    assert result["say"] == [SPEECH[Action.SPEAK_OFF_TOPIC]]
    assert dose.status() == "in_call"


def test_silence_counts_as_no_answer():
    ws, _, dose = make()
    start_call(ws, dose)
    result = ws.no_response(dose.id, later(15))
    assert result["status"] == "retry_wait"
    assert result["say"] == []


def test_unknown_reply_label_is_rejected():
    ws, _, dose = make()
    start_call(ws, dose)
    with pytest.raises(BadRequest):
        ws.parent_reply(dose.id, "x", "start", "groq", later(2))
    assert dose.status() == "in_call"


def test_cannot_double_ring_or_ring_a_finished_dose():
    ws, _, dose = make()
    ws.call_now(dose.id, T0)
    with pytest.raises(Conflict):
        ws.call_now(dose.id, later(1))
    ws.answer(dose.id, later(2))
    with pytest.raises(Conflict):
        ws.call_now(dose.id, later(3))
    ws.parent_reply(dose.id, "haan", "taken", "groq", later(4))
    with pytest.raises(Conflict):
        ws.call_now(dose.id, later(5))


# ---------- what the caretaker screen shows ----------

def test_summary_counts_and_adherence():
    ws = Workspace()
    a = ws.add_medicine("A")
    b = ws.add_medicine("B")

    def run(medicine, label):
        dose = ws.schedule_dose(medicine.id, T0)
        ws.call_now(dose.id, T0)
        ws.answer(dose.id, later(1))
        ws.parent_reply(dose.id, "x", label, "groq", later(2))

    run(a, "taken")
    run(a, "taken")
    run(a, "needs_help")
    run(b, "taken")
    ws.schedule_dose(b.id, T0 + timedelta(days=1))
    summary = ws.summary()
    assert summary["totals"] == {"taken": 3, "missed": 0, "needs_help": 1,
                                 "waiting": 1, "total": 5, "adherence_percent": 75}
    per = {row["medicine"]: row for row in summary["per_medicine"]}
    assert per["A"]["taken"] == 2 and per["A"]["needs_help"] == 1
    assert per["B"]["taken"] == 1 and per["B"]["waiting"] == 1
    assert sum(day["total"] for day in summary["by_day"]) == 5


def test_sample_history_is_labelled_and_complete():
    ws = Workspace()
    ws.load_sample_history(T0)
    snap = ws.snapshot()
    totals = snap["summary"]["totals"]
    assert snap["has_sample_data"] is True
    assert totals["total"] == 14
    assert totals["missed"] == 1
    assert totals["needs_help"] == 1
    assert len(snap["summary"]["by_day"]) == 7
    assert ws.alerts == []
    assert snap["ringing"] is None
    assert ws.tick(T0) == []
    assert len(ws.call_history()) == 14


def test_demo_limits_protect_memory():
    ws = Workspace()
    medicine = ws.add_medicine("A")
    for _ in range(MAX_DOSES):
        ws.schedule_dose(medicine.id, T0)
    with pytest.raises(BadRequest):
        ws.schedule_dose(medicine.id, T0)


def test_parent_text_is_capped():
    ws, _, dose = make()
    start_call(ws, dose)
    ws.parent_reply(dose.id, "x" * 900, "unclear", "groq", later(5))
    assert len(dose.transcript[1]["text"]) == 500


def test_snapshot_shows_the_ringing_call():
    ws, _, dose = make()
    assert ws.snapshot()["ringing"] is None
    ws.call_now(dose.id, T0)
    assert ws.snapshot()["ringing"] == {"dose_id": dose.id, "medicine": "Vitamin C"}


# ---------- the Hindi script ----------

def test_reminder_text_includes_name_and_look():
    text = reminder_text("विटामिन सी", "छोटी गोल नारंगी गोली।")
    assert "विटामिन सी" in text
    assert "छोटी गोल नारंगी गोली" in text
    assert text.endswith("क्या आपने दवाई ले ली है?")
    assert "।।" not in text


def test_reminder_text_without_description():
    text = reminder_text("Vitamin C")
    assert "Vitamin C" in text
    assert "पहचान" not in text


def test_every_spoken_action_has_a_hindi_line():
    def is_devanagari(ch):
        return "\u0900" <= ch <= "\u097f"
    for action in Action:
        if action.value.startswith("speak_"):
            lines = spoken_lines([action], "Vitamin C")
            assert len(lines) == 1, action
            assert any(is_devanagari(ch) for ch in lines[0]), action

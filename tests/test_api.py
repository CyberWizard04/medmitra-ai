import pytest
from fastapi.testclient import TestClient

from medmitra import api
from medmitra.api import SPEECH, Budget
from medmitra.brain import safety_check

client = TestClient(api.app)


@pytest.fixture(autouse=True)
def fresh_demo(monkeypatch):
    api._state["dose"] = None
    monkeypatch.setattr(api, "BUDGET", Budget(max_calls=100))


def fake_label(label):
    def classify_stub(text):
        return label, "groq"
    return classify_stub


def model_must_not_run(*args, **kwargs):
    raise AssertionError("the AI ran for a safety phrase")


def test_health_ok():
    assert client.get("/health").json() == {"status": "ok"}


def test_reply_before_start_is_404():
    assert client.post("/api/call/reply", json={"text": "haan"}).status_code == 404


def test_happy_path_taken(monkeypatch):
    monkeypatch.setattr(api, "classify", fake_label("taken"))
    start = client.post("/api/call/start").json()
    assert start["say"] == [SPEECH[api.Action.SPEAK_REMINDER]]
    result = client.post("/api/call/reply", json={"text": "haan, le li"}).json()
    assert result["heard"] == "taken"
    assert result["state"] == "confirmed"
    assert result["finished"] is True


def test_not_taken_schedules_retry_then_retry_works(monkeypatch):
    monkeypatch.setattr(api, "classify", fake_label("not_taken"))
    client.post("/api/call/start")
    result = client.post("/api/call/reply", json={"text": "abhi nahi"}).json()
    assert result["retry_scheduled"] is True
    assert result["state"] == "retry_wait"
    again = client.post("/api/call/retry").json()
    assert again["state"] == "reminded"
    assert again["attempt"] == 2


def test_retry_without_waiting_is_rejected():
    client.post("/api/call/start")
    assert client.post("/api/call/retry").status_code == 409


def test_needs_help_is_urgent_and_ends_the_call(monkeypatch):
    monkeypatch.setattr(api, "classify", fake_label("needs_help"))
    client.post("/api/call/start")
    result = client.post("/api/call/reply", json={"text": "mujhe chakkar aa raha hai"}).json()
    assert result["state"] == "escalated"
    assert any(text.startswith("URGENT") for text in result["caregiver_alerts"])
    assert client.post("/api/call/reply", json={"text": "haan"}).status_code == 409


def test_safety_words_never_reach_the_model(monkeypatch):
    monkeypatch.setattr("medmitra.brain._post_groq", model_must_not_run)
    monkeypatch.setattr("medmitra.brain.get_secret", lambda name: "not-a-real-key")
    client.post("/api/call/start")
    result = client.post("/api/call/reply",
                         json={"text": "haan li, lekin seene mein dard hai"}).json()
    assert result["heard"] == "needs_help"
    assert result["source"] == "safety_check"


def test_budget_full_blocks_ai_but_not_safety(monkeypatch):
    monkeypatch.setattr(api, "BUDGET", Budget(max_calls=0))
    monkeypatch.setattr("medmitra.brain._post_groq", model_must_not_run)
    monkeypatch.setattr("medmitra.brain.get_secret", lambda name: "not-a-real-key")
    client.post("/api/call/start")
    blocked = client.post("/api/call/reply", json={"text": "haan le li"})
    assert blocked.status_code == 503
    assert api._state["dose"].state.value == "reminded"
    urgent = client.post("/api/call/reply", json={"text": "chakkar aa raha hai"})
    assert urgent.status_code == 200
    assert urgent.json()["state"] == "escalated"


def test_budget_allows_then_blocks_then_recovers():
    now = [0.0]
    budget = Budget(max_calls=2, window_seconds=60, clock=lambda: now[0])
    assert budget.allow() is True
    assert budget.allow() is True
    assert budget.allow() is False
    now[0] = 61
    assert budget.allow() is True


def test_every_spoken_line_is_hindi():
    def is_devanagari(ch):
        return "\u0900" <= ch <= "\u097f"
    for action, text in SPEECH.items():
        assert any(is_devanagari(ch) for ch in text), action


def test_hindi_safety_words_are_detected():
    assert safety_check("मुझे चक्कर आ रहे हैं") is True
    assert safety_check("सीने में दर्द है") is True
    assert safety_check("हाँ, ले ली") is False


def test_caregiver_status_shows_history(monkeypatch):
    assert client.get("/api/caregiver").json() == {"status": "no call yet"}
    monkeypatch.setattr(api, "classify", fake_label("taken"))
    client.post("/api/call/start")
    client.post("/api/call/reply", json={"text": "haan"})
    status = client.get("/api/caregiver").json()
    assert status["state"] == "confirmed"
    assert len(status["history"]) == 2

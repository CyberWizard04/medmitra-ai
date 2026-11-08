import json
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from medmitra import api
from medmitra.store import RING_SECONDS, Workspace

T0 = datetime(2026, 11, 7, 4, 30, tzinfo=timezone.utc)
PAGE = Path(__file__).resolve().parent.parent / "frontend" / "index.html"
HTML = PAGE.read_text(encoding="utf-8")
SCRIPT = re.search(r"<script>(.*?)</script>", HTML, re.S).group(1)
NODE = shutil.which("node")
client = TestClient(api.app)


def later(seconds):
    return T0 + timedelta(seconds=seconds)


def one_dose(ws, medicine=None):
    medicine = medicine or ws.add_medicine("Vitamin C", "round tablet")
    return ws.schedule_dose(medicine.id, T0)


def start(ws, dose, at=0):
    ws.call_now(dose.id, later(at))
    ws.answer(dose.id, later(at + 1))


# ---------- the numbers behind the charts ----------

def test_replies_count_what_parents_said():
    ws = Workspace()
    dose = one_dose(ws)
    start(ws, dose)
    ws.parent_reply(dose.id, "weather?", "off_topic", "groq", later(5))
    ws.parent_reply(dose.id, "which one?", "asks_medicine", "groq", later(6))
    ws.parent_reply(dose.id, "okay", "unclear", "groq", later(7))
    ws.parent_reply(dose.id, "haan", "taken", "groq", later(8))
    assert ws.summary()["replies"] == {
        "taken": 1, "not_taken": 0, "unclear": 1, "needs_help": 0,
        "off_topic": 1, "asks_medicine": 1, "no_answer": 0,
    }


def test_unanswered_rings_count_as_no_answer():
    ws = Workspace()
    one_dose(ws)
    ws.tick(T0)
    ws.tick(T0 + timedelta(seconds=RING_SECONDS))
    assert ws.summary()["replies"]["no_answer"] == 1


def test_average_reminders_needed_to_confirm():
    ws = Workspace()
    medicine = ws.add_medicine("Vitamin C")
    first = one_dose(ws, medicine)
    start(ws, first)
    ws.parent_reply(first.id, "haan", "taken", "groq", later(5))
    second = one_dose(ws, medicine)
    start(ws, second, at=100)
    ws.parent_reply(second.id, "abhi nahi", "not_taken", "groq", later(105))
    ws.call_now(second.id, later(110))
    ws.answer(second.id, later(111))
    ws.parent_reply(second.id, "haan", "taken", "groq", later(115))
    assert ws.summary()["avg_attempts_to_confirm"] == 1.5


def test_average_is_none_before_any_dose_is_confirmed():
    ws = Workspace()
    one_dose(ws)
    assert ws.summary()["avg_attempts_to_confirm"] is None


def test_empty_workspace_has_zero_replies():
    replies = Workspace().summary()["replies"]
    assert set(replies) == {"taken", "not_taken", "unclear", "needs_help",
                            "off_topic", "asks_medicine", "no_answer"}
    assert all(value == 0 for value in replies.values())


def test_sample_history_has_a_realistic_mix():
    ws = Workspace()
    ws.load_sample_history(T0)
    summary = ws.summary()
    assert summary["replies"] == {
        "taken": 12, "not_taken": 2, "asks_medicine": 2, "unclear": 1,
        "off_topic": 1, "needs_help": 1, "no_answer": 3,
    }
    assert summary["avg_attempts_to_confirm"] == 1.2
    assert summary["totals"]["adherence_percent"] == 86
    assert ws.alerts == []


def test_state_route_carries_the_analytics():
    summary = client.post("/api/w").json()["state"]["summary"]
    assert summary["replies"]["taken"] == 12
    assert summary["avg_attempts_to_confirm"] == 1.2
    assert len(summary["by_day"]) == 7


# ---------- the page ----------

def test_every_element_the_script_looks_up_exists():
    used = set(re.findall(r'\$\("([A-Za-z0-9_-]+)"\)', HTML))
    defined = set(re.findall(r'\sid="([A-Za-z0-9_-]+)"', HTML))
    assert not used - defined, f"the script looks for elements that are missing: {used - defined}"


def test_the_page_has_a_real_api_address():
    assert "__API__" not in HTML
    assert 'const API="https://' in HTML


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
def test_page_script_has_no_syntax_errors(tmp_path):
    path = tmp_path / "page.js"
    path.write_text(SCRIPT, encoding="utf-8")
    result = subprocess.run([NODE, "--check", str(path)], capture_output=True,
                            text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr


STUBS = """
const els = {};
const el = id => els[id] || (els[id] = {innerHTML: "", textContent: "", value: "", hidden: false,
  style: {}, classList: {toggle() {}}, append() {}, scrollTop: 0});
global.document = {getElementById: el, activeElement: null, createElement: () => ({})};
global.localStorage = {getItem: () => null, setItem() {}, removeItem() {}};
global.fetch = async () => ({ok: true, status: 200, json: async () => ({calls: []})});
"""


def run_page(snapshot, tmp_path):
    """Runs the page's real script against a server snapshot and returns what it drew."""
    script = SCRIPT.replace("\nconnect();\n", "\n")
    assert script != SCRIPT
    harness = (
        STUBS + script
        + "\nS = " + json.dumps(snapshot) + ';\nwid = "w1";\nrender();\n'
        + 'console.log(JSON.stringify({kpis: els.kpis.innerHTML, donut: els["c-donut"].innerHTML, '
          'days: els["c-days"].innerHTML, trend: els["c-trend"].innerHTML, '
          'meds: els["c-meds"].innerHTML, said: els["c-said"].innerHTML}));\n'
    )
    path = tmp_path / "harness.js"
    path.write_text(harness, encoding="utf-8")
    result = subprocess.run([NODE, str(path)], capture_output=True, text=True,
                            encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
def test_dashboard_draws_every_chart_from_real_data(tmp_path):
    ws = Workspace()
    ws.load_sample_history(T0)
    out = run_page(ws.snapshot(), tmp_path)
    assert "Adherence" in out["kpis"] and "86%" in out["kpis"]
    assert "<svg" in out["donut"] and "stroke-dasharray" in out["donut"]
    assert out["days"].count("<rect") >= 7
    assert "<polyline" in out["trend"]
    assert out["meds"].count('class="hb"') == 2
    assert "Said taken" in out["said"] and ">12<" in out["said"]


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
def test_dashboard_copes_with_no_data(tmp_path):
    out = run_page(Workspace().snapshot(), tmp_path)
    assert "–" in out["kpis"]
    for key in ("donut", "days", "trend", "meds", "said"):
        assert "empty" in out[key], key

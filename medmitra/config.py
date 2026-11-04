"""Central configuration. Secrets come from environment variables first,
then Colab Secrets. Values are never printed."""
import os

# Colab runs in UTC. Store times in UTC, schedule in the parent's local zone.
APP_TIMEZONE = os.environ.get("APP_TIMEZONE", "Asia/Kolkata")

# Feature flags. The logic exists and is tested, but these stay OFF on purpose
# for the hackathon demo (no automatic scheduling, no real phone calls).
SCHEDULER_ENABLED = os.environ.get("SCHEDULER_ENABLED", "false").lower() == "true"
PHONE_CALLS_ENABLED = os.environ.get("PHONE_CALLS_ENABLED", "false").lower() == "true"

# Names only. Values live in Colab Secrets, never in code.
EXPECTED_SECRETS = {
    "GROQ_API_KEY": "brain (primary)",
    "OPENROUTER_API_KEY": "brain (fallback)",
    "GNANI_API_KEY": "optional: Indian-language speech, after docs are verified",
    "DATABASE_URL": "Milestone 5 (database)",
}


def get_secret(name, default=None):
    value = os.environ.get(name)
    if value:
        return value
    try:
        from google.colab import userdata
        return userdata.get(name)
    except Exception:
        return default


def check_config():
    """Print which secrets are set or missing. Never prints values."""
    for name, needed_for in EXPECTED_SECRETS.items():
        status = "set" if get_secret(name) else "missing"
        print(f"{name:20} {status:8} {needed_for}")
    print(f"SCHEDULER_ENABLED={SCHEDULER_ENABLED}  PHONE_CALLS_ENABLED={PHONE_CALLS_ENABLED}")

from medmitra.brain import GROQ_MODEL, LABELS, classify, parse_label, safety_check


def fake_model(reply_text):
    """Pretends to be Groq and always replies with reply_text."""
    def transport(payload):
        return {"choices": [{"message": {"content": reply_text}}]}
    return transport


def model_must_not_run(payload):
    raise AssertionError("the AI should not run for safety terms")


def broken_model(payload):
    raise OSError("network down")


def test_safety_terms_win_over_a_yes():
    label, source = classify("haan li, lekin seene mein dard hai",
                             transport=model_must_not_run)
    assert (label, source) == ("needs_help", "safety_check")


def test_dizziness_hindi_escalates_without_ai():
    label, _ = classify("mujhe chakkar aa rahe hain", transport=model_must_not_run)
    assert label == "needs_help"


def test_breathing_english_escalates_without_ai():
    label, _ = classify("I can't breathe properly", transport=model_must_not_run)
    assert label == "needs_help"


def test_plain_yes_goes_to_model():
    assert classify("haan, le li", transport=fake_model("taken")) == ("taken", "groq")


def test_not_taken_hinglish_goes_to_model():
    assert classify("abhi nahi, baad mein",
                    transport=fake_model("not_taken")) == ("not_taken", "groq")


def test_model_unclear_stays_unclear():
    assert classify("samajh nahi aaya", transport=fake_model("unclear"))[0] == "unclear"


def test_sentence_from_model_is_not_taken():
    label, _ = classify("ok", transport=fake_model("The parent said they took it."))
    assert label == "unclear"


def test_model_error_is_unclear_never_taken():
    label, source = classify("haan", transport=broken_model)
    assert label == "unclear"
    assert source == "error:OSError"


def test_missing_key_is_unclear(monkeypatch):
    monkeypatch.setattr("medmitra.brain.get_secret", lambda name: None)
    assert classify("haan li") == ("unclear", "no_key")


def test_normal_speech_is_not_flagged():
    assert safety_check("haan, le li") is False
    assert safety_check("samajh nahi aaya") is False
    assert safety_check("abhi nahi, baad mein") is False


def test_parse_label_handles_formatting():
    assert parse_label('  "Taken." ') == "taken"
    assert parse_label("needs help") == "needs_help"
    assert parse_label("Not_Taken") == "not_taken"
    assert parse_label("") == "unclear"
    assert parse_label(None) == "unclear"
    assert parse_label("maybe taken") == "unclear"
    assert all(label in LABELS for label in LABELS)


def test_request_uses_safe_settings():
    seen = {}

    def capture(payload):
        seen.update(payload)
        return {"choices": [{"message": {"content": "taken"}}]}

    classify("haan li", transport=capture)
    assert seen["model"] == GROQ_MODEL
    assert seen["temperature"] == 0
    assert seen["messages"][0]["role"] == "system"
    assert seen["messages"][-1] == {"role": "user", "content": "haan li"}

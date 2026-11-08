"""Everything the agent says (Hindi) and the alerts the caretaker reads (English).

All lines are fixed. The AI never writes what the agent says, so it cannot drift
into medical advice. The parent's name, the medicine name and its description come
from the caretaker and are only repeated, never interpreted.
"""
from medmitra.reminder import Action

# Every reminder ends with this question.
QUESTION = "क्या आप अभी दवाई ले रहे हैं?"

SPEECH = {
    Action.SPEAK_CLARIFY: "जी, कृपया एक बार और बताइए। क्या आपने दवाई ले ली है?",
    Action.SPEAK_OFF_TOPIC: (
        "मैं यहाँ सिर्फ़ आपको दवाई याद दिलाने के लिए हूँ। "
        "बाकी बातों के लिए कृपया अपने देखभाल करने वाले से संपर्क करें। "
        "क्या आपने दवाई ले ली है?"
    ),
    Action.SPEAK_THANKS: "धन्यवाद। आपकी दवाई दर्ज कर ली गई है। अपना ख़याल रखिए।",
    Action.SPEAK_RETRY_NOTICE: "ठीक है। थोड़ी देर बाद फिर याद दिलाएँगे।",
    Action.SPEAK_FAMILY_NOTICE: "आपकी दवाई अभी दर्ज नहीं हुई है। हम आपके परिवार को सूचित कर रहे हैं।",
    Action.SPEAK_HELP_ACK: (
        "आपने मदद माँगी है। कृपया तुरंत पास में किसी व्यक्ति को बुलाइए। "
        "सीने में दर्द या सांस की तकलीफ़ हो, तो 108 या 112 पर तुरंत कॉल कीजिए।"
    ),
}

# Every fixed line, by the key its audio route uses.
FIXED_LINES = {action.value: text for action, text in SPEECH.items()}
FIXED_LINES["question"] = QUESTION

ALERT_TEXT = {
    Action.ALERT_WARNING: (
        "Missed dose: {medicine} was not confirmed after all attempts. "
        "Please check on the parent."
    ),
    Action.ALERT_URGENT: (
        "URGENT: the parent asked for help or mentioned pain, dizziness or "
        "breathing trouble ({medicine}). Contact them now."
    ),
}


def greeting_text(parent_name=""):
    name = " ".join(str(parent_name or "").split())
    return f"हैलो {name} जी।" if name else "हैलो।"


def medicine_info_text(name, appearance=""):
    appearance = (appearance or "").strip().rstrip("।.!? ")
    text = f"दवाई का नाम है {name}।"
    if appearance:
        text += f" इसकी पहचान यह है: {appearance}।"
    return text


def reminder_intro_text(name, appearance=""):
    return "आपकी दवाई लेने का समय हो गया है। " + medicine_info_text(name, appearance)


def reminder_lines(parent_name, name, appearance=""):
    """The reminder as three messages: greeting, which medicine, the question."""
    return [greeting_text(parent_name), reminder_intro_text(name, appearance), QUESTION]


def spoken_lines(actions, parent_name, medicine_name, appearance=""):
    """The Hindi lines to speak for a list of actions."""
    lines = []
    for action in actions:
        if action == Action.SPEAK_REMINDER:
            lines.extend(reminder_lines(parent_name, medicine_name, appearance))
        elif action == Action.SPEAK_MEDICINE_INFO:
            lines.extend([medicine_info_text(medicine_name, appearance), QUESTION])
        elif action in SPEECH:
            lines.append(SPEECH[action])
    return lines

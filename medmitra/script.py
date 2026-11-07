"""Everything the agent says (Hindi) and the alerts the caretaker reads (English).

All lines are fixed. The AI never writes what the agent says, so it cannot drift
into medical advice. A medicine name and description come from the caretaker and
are only repeated, never interpreted.
"""
from medmitra.reminder import Action

SPEECH = {
    Action.SPEAK_CLARIFY: "माफ़ कीजिए, मुझे समझ नहीं आया। कृपया बताइए, क्या आपने दवाई ले ली है?",
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


def reminder_text(name, appearance=""):
    """The reminder for one medicine, with how it looks if the caretaker said."""
    appearance = (appearance or "").strip().rstrip("।.!? ")
    text = f"नमस्ते। अब आपकी दवाई का समय हो गया है। दवाई का नाम है {name}।"
    if appearance:
        text += f" इसकी पहचान यह है: {appearance}।"
    return text + " क्या आपने दवाई ले ली है?"


def spoken_lines(actions, name, appearance=""):
    """The Hindi lines to speak for a list of actions."""
    lines = []
    for action in actions:
        if action == Action.SPEAK_REMINDER:
            lines.append(reminder_text(name, appearance))
        elif action in SPEECH:
            lines.append(SPEECH[action])
    return lines

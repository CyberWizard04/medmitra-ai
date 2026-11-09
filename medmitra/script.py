"""Everything the agent says and the alerts the caretaker reads.

All spoken lines are fixed, one pack per language (see languages.py). The AI never
writes what the agent says, so it cannot drift into medical advice. The parent's
name, the medicine name and its description come from the caretaker and are only
repeated, never interpreted. Alerts for the caretaker are always English.
"""
from medmitra.languages import DEFAULT_LANGUAGE
from medmitra.reminder import Action
from medmitra.packs_extra import EXTRA_PACKS

PACKS = {
    "hi": {
        "hello_name": "हैलो {name} जी।",
        "hello": "हैलो।",
        "time_for_medicine": "आपकी दवाई लेने का समय हो गया है। ",
        "medicine_is": "दवाई का नाम है {name}।",
        "looks_like": " इसकी पहचान यह है: {look}।",
        "question": "क्या आप अभी दवाई ले रहे हैं?",
        "lines": {
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
        },
    },
    "en": {
        "hello_name": "Hello {name} ji.",
        "hello": "Hello.",
        "time_for_medicine": "It is time for your medicine. ",
        "medicine_is": "The medicine is {name}.",
        "looks_like": " It looks like this: {look}.",
        "question": "Are you taking your medicine now?",
        "lines": {
            Action.SPEAK_CLARIFY: "Please tell me once more. Have you taken your medicine?",
            Action.SPEAK_OFF_TOPIC: (
                "I am only here to remind you about your medicine. "
                "For anything else, please contact your caretaker. "
                "Have you taken your medicine?"
            ),
            Action.SPEAK_THANKS: "Thank you. Your medicine has been recorded. Please take care.",
            Action.SPEAK_RETRY_NOTICE: "Alright. We will remind you again in a little while.",
            Action.SPEAK_FAMILY_NOTICE: "Your medicine has not been recorded yet. We are letting your family know.",
            Action.SPEAK_HELP_ACK: (
                "You have asked for help. Please call someone nearby right away. "
                "If you have chest pain or trouble breathing, call 108 or 112 immediately."
            ),
        },
    },
}


PACKS.update(EXTRA_PACKS)  # the other Gnani languages: drafts until a fluent speaker checks them


def _pack(lang=DEFAULT_LANGUAGE):
    return PACKS.get(lang) or PACKS[DEFAULT_LANGUAGE]


def fixed_lines(lang=DEFAULT_LANGUAGE):
    """Every line that does not depend on the caretaker's input, by the key its audio route uses."""
    pack = _pack(lang)
    lines = {action.value: text for action, text in pack["lines"].items()}
    lines["question"] = pack["question"]
    return lines


# The default language, under the names the rest of the code already uses.
SPEECH = PACKS[DEFAULT_LANGUAGE]["lines"]
QUESTION = PACKS[DEFAULT_LANGUAGE]["question"]
FIXED_LINES = fixed_lines(DEFAULT_LANGUAGE)

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


def greeting_text(parent_name="", lang=DEFAULT_LANGUAGE):
    pack = _pack(lang)
    name = " ".join(str(parent_name or "").split())
    return pack["hello_name"].format(name=name) if name else pack["hello"]


def medicine_info_text(name, appearance="", lang=DEFAULT_LANGUAGE):
    pack = _pack(lang)
    appearance = (appearance or "").strip().rstrip("।.!? ")
    text = pack["medicine_is"].format(name=name)
    if appearance:
        text += pack["looks_like"].format(look=appearance)
    return text


def reminder_intro_text(name, appearance="", lang=DEFAULT_LANGUAGE):
    return _pack(lang)["time_for_medicine"] + medicine_info_text(name, appearance, lang)


def reminder_lines(parent_name, name, appearance="", lang=DEFAULT_LANGUAGE):
    """The reminder as three messages: greeting, which medicine, the question."""
    return [greeting_text(parent_name, lang),
            reminder_intro_text(name, appearance, lang),
            _pack(lang)["question"]]


def spoken_lines(actions, parent_name, medicine_name, appearance="", lang=DEFAULT_LANGUAGE):
    """The lines to speak for a list of actions."""
    pack = _pack(lang)
    lines = []
    for action in actions:
        if action == Action.SPEAK_REMINDER:
            lines.extend(reminder_lines(parent_name, medicine_name, appearance, lang))
        elif action == Action.SPEAK_MEDICINE_INFO:
            lines.extend([medicine_info_text(medicine_name, appearance, lang), pack["question"]])
        elif action in pack["lines"]:
            lines.append(pack["lines"][action])
    return lines

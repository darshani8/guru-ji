"""Fixed replies in Indian English, Hindi (Devanagari and Hinglish) and Kannada.

These answer greetings instantly and keep the assistant friendly when no model
is configured or the model is slow. Hindi phrases use feminine first person to
match the Kajal voice.
"""

from __future__ import annotations

import random

from .language import DetectedLanguage

_PHRASES: dict[str, dict[str, tuple[str, ...]]] = {
    "greeting": {
        "en-IN": ("Namaste! I'm Guru Ji. What can I do for you today?", "Hello! Guru Ji here. How can I help you?", "Namaste! Tell me, how can I help?"),
        "hi-IN": ("नमस्ते! मैं गुरु जी हूँ। बताइए, मैं आपकी क्या मदद कर सकती हूँ?", "नमस्ते! कहिए, आज मैं आपके लिए क्या करूँ?"),
        "hi-Latn": ("Namaste! Main Guru Ji hoon. Bataiye, main aapki kya madad kar sakti hoon?", "Namaste ji! Kahiye, aaj main aapke liye kya karoon?"),
        "kn-IN": ("ನಮಸ್ಕಾರ! ನಾನು ಗುರು ಜಿ. ನಾನು ನಿಮಗೆ ಹೇಗೆ ಸಹಾಯ ಮಾಡಲಿ?", "ನಮಸ್ಕಾರ! ಹೇಳಿ, ಇವತ್ತು ನಿಮಗೆ ಏನು ಬೇಕು?"),
    },
    "how_are_you": {
        "en-IN": ("I'm doing very well, thank you for asking! How are you? What can I help you with?", "All good here, thank you! How about you? Tell me what you need."),
        "hi-IN": ("मैं बिल्कुल ठीक हूँ, पूछने के लिए धन्यवाद! आप कैसे हैं? बताइए, क्या मदद करूँ?",),
        "hi-Latn": ("Main bilkul theek hoon, poochne ke liye shukriya! Aap kaise hain? Bataiye, kya madad karoon?",),
        "kn-IN": ("ನಾನು ಚೆನ್ನಾಗಿದ್ದೇನೆ, ಕೇಳಿದ್ದಕ್ಕೆ ಧನ್ಯವಾದಗಳು! ನೀವು ಹೇಗಿದ್ದೀರಿ? ಏನು ಸಹಾಯ ಬೇಕು?",),
    },
    "thanks": {
        "en-IN": ("You're most welcome! Anything else I can help with?", "My pleasure! Just ask if you need anything else."),
        "hi-IN": ("आपका स्वागत है! और कुछ मदद चाहिए तो बताइए।",),
        "hi-Latn": ("Aapka swagat hai! Aur kuch chahiye toh bataiye.",),
        "kn-IN": ("ಪರವಾಗಿಲ್ಲ! ಇನ್ನೇನಾದರೂ ಬೇಕಿದ್ದರೆ ಹೇಳಿ.",),
    },
    "identity": {
        "en-IN": ("I'm Guru Ji, your institution's assistant. I can answer from the college's records, search the internet, and just chat. Ask me anything!",),
        "hi-IN": ("मैं गुरु जी हूँ, आपके संस्थान की सहायक। मैं कॉलेज के रिकॉर्ड से जवाब दे सकती हूँ, इंटरनेट पर खोज सकती हूँ, और आपसे बात भी कर सकती हूँ।",),
        "hi-Latn": ("Main Guru Ji hoon, aapke institution ki assistant. Main college ke records se jawab de sakti hoon, internet par search kar sakti hoon, aur aapse baat bhi kar sakti hoon.",),
        "kn-IN": ("ನಾನು ಗುರು ಜಿ, ನಿಮ್ಮ ಸಂಸ್ಥೆಯ ಸಹಾಯಕಿ. ಕಾಲೇಜಿನ ದಾಖಲೆಗಳಿಂದ ಉತ್ತರ ಕೊಡಬಲ್ಲೆ, ಇಂಟರ್ನೆಟ್‌ನಲ್ಲಿ ಹುಡುಕಬಲ್ಲೆ, ಮತ್ತು ನಿಮ್ಮ ಜೊತೆ ಮಾತನಾಡಬಲ್ಲೆ.",),
    },
    "help": {
        "en-IN": (
            "I can check attendance, fees, results and students from the college records, find policies in documents, search the internet for news "
            "or facts, and simply chat. For example, say 'How many MBA students are below 75% attendance?' or 'Search the internet for ISRO's latest launch'.",
        ),
        "hi-IN": ("मैं कॉलेज के रिकॉर्ड से हाज़िरी, फ़ीस और रिज़ल्ट देख सकती हूँ, दस्तावेज़ों में नियम ढूँढ सकती हूँ, और इंटरनेट पर खबरें खोज सकती हूँ। जैसे पूछिए: 'इंटरनेट पर इसरो के नए लॉन्च के बारे में खोजो'।",),
        "hi-Latn": ("Main college ke records se attendance, fees aur results dekh sakti hoon, documents mein rules dhoondh sakti hoon, aur internet par news search kar sakti hoon. Jaise poochiye: 'internet par ISRO ke latest launch ke baare mein search karo'.",),
        "kn-IN": ("ನಾನು ಕಾಲೇಜಿನ ದಾಖಲೆಗಳಿಂದ ಹಾಜರಾತಿ, ಶುಲ್ಕ, ಫಲಿತಾಂಶ ನೋಡಬಲ್ಲೆ, ದಾಖಲೆಗಳಲ್ಲಿ ನಿಯಮ ಹುಡುಕಬಲ್ಲೆ, ಮತ್ತು ಇಂಟರ್ನೆಟ್‌ನಲ್ಲಿ ಸುದ್ದಿ ಹುಡುಕಬಲ್ಲೆ. ಉದಾಹರಣೆಗೆ: 'ಇಂಟರ್ನೆಟ್‌ನಲ್ಲಿ ಇಸ್ರೋ ಬಗ್ಗೆ ಹುಡುಕಿ'.",),
    },
    "goodbye": {
        "en-IN": ("Goodbye! Have a wonderful day.", "Bye for now! Take care."),
        "hi-IN": ("अलविदा! आपका दिन शुभ हो।",),
        "hi-Latn": ("Alvida! Aapka din shubh ho.",),
        "kn-IN": ("ಹೋಗಿ ಬನ್ನಿ! ನಿಮ್ಮ ದಿನ ಶುಭವಾಗಲಿ.",),
    },
    "ack": {
        "en-IN": ("Sure. What would you like to do next?", "Alright! Tell me whenever you're ready."),
        "hi-IN": ("ठीक है। आगे क्या करना है, बताइए।",),
        "hi-Latn": ("Theek hai. Aage kya karna hai, bataiye.",),
        "kn-IN": ("ಸರಿ. ಮುಂದೆ ಏನು ಮಾಡೋಣ, ಹೇಳಿ.",),
    },
    "not_understood": {
        "en-IN": ("Sorry, I didn't quite get that. You can ask about attendance, fees, results or documents, or say 'search the internet for' something.",),
        "hi-IN": ("माफ़ कीजिए, मैं समझ नहीं पाई। आप हाज़िरी, फ़ीस, रिज़ल्ट या दस्तावेज़ों के बारे में पूछ सकते हैं, या कहिए 'इंटरनेट पर खोजो'।",),
        "hi-Latn": ("Maaf kijiye, main samajh nahi paayi. Aap attendance, fees, results ya documents ke baare mein pooch sakte hain, ya kahiye 'internet par search karo'.",),
        "kn-IN": ("ಕ್ಷಮಿಸಿ, ನನಗೆ ಅರ್ಥವಾಗಲಿಲ್ಲ. ಹಾಜರಾತಿ, ಶುಲ್ಕ, ಫಲಿತಾಂಶ ಅಥವಾ ದಾಖಲೆಗಳ ಬಗ್ಗೆ ಕೇಳಬಹುದು, ಅಥವಾ 'ಇಂಟರ್ನೆಟ್‌ನಲ್ಲಿ ಹುಡುಕಿ' ಎಂದು ಹೇಳಿ.",),
    },
    "web_filler": {
        "en-IN": ("One moment, let me check the internet.", "Give me a second, I'm searching the web."),
        "hi-IN": ("एक सेकंड, मैं इंटरनेट पर देखती हूँ।",),
        "hi-Latn": ("Ek second, main internet par dekhti hoon.",),
        "kn-IN": ("ಒಂದು ನಿಮಿಷ, ಇಂಟರ್ನೆಟ್‌ನಲ್ಲಿ ನೋಡುತ್ತೇನೆ.",),
    },
    "web_nothing": {
        "en-IN": ("I couldn't find anything reliable on the internet for that. Could you say it another way?",),
        "hi-IN": ("मुझे इंटरनेट पर इसके बारे में भरोसेमंद जानकारी नहीं मिली। क्या आप दूसरे शब्दों में पूछेंगे?",),
        "hi-Latn": ("Mujhe internet par iske baare mein bharosemand jaankari nahi mili. Kya aap doosre shabdon mein poochenge?",),
        "kn-IN": ("ಇದರ ಬಗ್ಗೆ ಇಂಟರ್ನೆಟ್‌ನಲ್ಲಿ ನಂಬಲರ್ಹ ಮಾಹಿತಿ ಸಿಗಲಿಲ್ಲ. ಬೇರೆ ರೀತಿಯಲ್ಲಿ ಕೇಳುತ್ತೀರಾ?",),
    },
    "web_off": {
        "en-IN": ("Internet search isn't switched on for this assistant yet, so I can't look that up right now.",),
        "hi-IN": ("इस सहायक के लिए इंटरनेट खोज अभी चालू नहीं है, इसलिए मैं अभी यह नहीं देख सकती।",),
        "hi-Latn": ("Is assistant ke liye internet search abhi chalu nahi hai, isliye main abhi yeh nahi dekh sakti.",),
        "kn-IN": ("ಈ ಸಹಾಯಕಕ್ಕೆ ಇಂಟರ್ನೆಟ್ ಹುಡುಕಾಟ ಇನ್ನೂ ಆನ್ ಆಗಿಲ್ಲ, ಹಾಗಾಗಿ ಈಗ ಅದನ್ನು ನೋಡಲಾಗುವುದಿಲ್ಲ.",),
    },
    "web_unavailable": {
        "en-IN": ("The internet search service isn't responding right now. Please try again in a minute.",),
        "hi-IN": ("इंटरनेट खोज सेवा अभी जवाब नहीं दे रही है। कृपया एक मिनट बाद फिर कोशिश करें।",),
        "hi-Latn": ("Internet search service abhi jawab nahi de rahi. Please ek minute baad phir try kariye.",),
        "kn-IN": ("ಇಂಟರ್ನೆಟ್ ಹುಡುಕಾಟ ಸೇವೆ ಈಗ ಸ್ಪಂದಿಸುತ್ತಿಲ್ಲ. ಒಂದು ನಿಮಿಷದ ನಂತರ ಮತ್ತೆ ಪ್ರಯತ್ನಿಸಿ.",),
    },
    "web_quota": {
        "en-IN": ("You've used today's internet searches. The limit resets at midnight.",),
        "hi-IN": ("आज की इंटरनेट खोज की सीमा पूरी हो गई है। यह आधी रात को फिर शुरू होगी।",),
        "hi-Latn": ("Aaj ki internet search ki limit poori ho gayi hai. Yeh aadhi raat ko reset hogi.",),
        "kn-IN": ("ಇಂದಿನ ಇಂಟರ್ನೆಟ್ ಹುಡುಕಾಟದ ಮಿತಿ ಮುಗಿದಿದೆ. ಮಧ್ಯರಾತ್ರಿ ಮತ್ತೆ ಆರಂಭವಾಗುತ್ತದೆ.",),
    },
    "web_personal": {
        "en-IN": ("I won't send personal details like phone numbers, emails or student numbers to an internet search. Please ask without them.",),
        "hi-IN": ("मैं फ़ोन नंबर, ईमेल या छात्र संख्या जैसी निजी जानकारी इंटरनेट खोज में नहीं भेजती। कृपया इनके बिना पूछिए।",),
        "hi-Latn": ("Main phone number, email ya student number jaisi personal details internet search mein nahi bhejti. Please inke bina poochiye.",),
        "kn-IN": ("ಫೋನ್ ನಂಬರ್, ಇಮೇಲ್ ಅಥವಾ ವಿದ್ಯಾರ್ಥಿ ಸಂಖ್ಯೆಯಂತಹ ವೈಯಕ್ತಿಕ ಮಾಹಿತಿಯನ್ನು ಇಂಟರ್ನೆಟ್ ಹುಡುಕಾಟಕ್ಕೆ ಕಳುಹಿಸುವುದಿಲ್ಲ.",),
    },
    "web_forbidden": {
        "en-IN": ("Your account isn't allowed to search the internet from the assistant.",),
        "hi-IN": ("आपके खाते को सहायक से इंटरनेट खोज की अनुमति नहीं है।",),
        "hi-Latn": ("Aapke account ko assistant se internet search ki permission nahi hai.",),
        "kn-IN": ("ನಿಮ್ಮ ಖಾತೆಗೆ ಸಹಾಯಕದಿಂದ ಇಂಟರ್ನೆಟ್ ಹುಡುಕಲು ಅನುಮತಿ ಇಲ್ಲ.",),
    },
    "confirm_on_screen": {
        "en-IN": ("This needs your confirmation. Please tap Confirm on the screen.",),
        "hi-IN": ("इसके लिए आपकी पुष्टि चाहिए। कृपया स्क्रीन पर Confirm दबाइए।",),
        "hi-Latn": ("Iske liye aapka confirmation chahiye. Please screen par Confirm dabaiye.",),
        "kn-IN": ("ಇದಕ್ಕೆ ನಿಮ್ಮ ದೃಢೀಕರಣ ಬೇಕು. ದಯವಿಟ್ಟು ಪರದೆಯ ಮೇಲೆ Confirm ಒತ್ತಿ.",),
    },
    "here_is_what_i_found": {
        "en-IN": ("Here's what I found on the internet.",),
        "hi-IN": ("इंटरनेट पर मुझे यह मिला।",),
        "hi-Latn": ("Internet par mujhe yeh mila.",),
        "kn-IN": ("ಇಂಟರ್ನೆಟ್‌ನಲ್ಲಿ ನನಗೆ ಸಿಕ್ಕಿದ್ದು ಇದು.",),
    },
}


def variant(language: DetectedLanguage) -> str:
    if language.hinglish:
        return "hi-Latn"
    return language.code


def phrase(key: str, language: DetectedLanguage, *, pick: random.Random | None = None) -> str:
    options = _PHRASES[key].get(variant(language)) or _PHRASES[key]["en-IN"]
    return (pick or random).choice(options) if len(options) > 1 else options[0]


def keys() -> tuple[str, ...]:
    return tuple(_PHRASES)


def all_variants() -> tuple[str, ...]:
    return ("en-IN", "hi-IN", "hi-Latn", "kn-IN")


def has(key: str, variant_name: str) -> bool:
    return bool(_PHRASES.get(key, {}).get(variant_name))


__all__ = ["all_variants", "has", "keys", "phrase", "variant"]

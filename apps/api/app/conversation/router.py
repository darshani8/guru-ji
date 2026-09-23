"""Decide what a turn is: small talk, an open-web search, or an institutional task.

Small talk is only a turn that is nothing but a greeting, thanks, a goodbye or
a question about the assistant itself. A web turn needs an explicit web phrase
("search the internet for", "latest news", "इंटरनेट पर", "ಇಂಟರ್ನೆಟ್‌ನಲ್ಲಿ");
if it is about the institution itself ("our college online") it stays an
institutional task, which goes to the internet-intelligence tools. Everything
else is a task for the master agent, whose "could not map" becomes free
conversation one level up.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

Intent = Literal["small_talk", "web", "task"]
SmallTalkKind = Literal["greeting", "how_are_you", "thanks", "identity", "help", "goodbye", "ack"]

_PUNCTUATION = re.compile(r"[^\w\sऀ-ॿಀ-೿']+")

_SMALL_TALK: tuple[tuple[SmallTalkKind, re.Pattern[str]], ...] = (
    ("how_are_you", re.compile(
        r"\b(how are you|how r u|how are things|how do you do|kaise ho|kaisi ho|kaise hain|kaisi hain|aap kaise|kya haal|hegiddira|hegidiya|hegiddiya)\b"
        r"|कैसे हो|कैसी हो|कैसे हैं|कैसी हैं|क्या हाल|ಹೇಗಿದ್ದೀರಾ|ಹೇಗಿದ್ದೀಯ|ಹೇಗಿದ್ದೀರಿ"
    )),
    ("thanks", re.compile(r"\b(thank you|thanks|thankyou|thx|dhanyavaad|dhanyavad|dhanyawad|shukriya|thanks a lot)\b|धन्यवाद|शुक्रिया|ಧನ್ಯವಾದ")),
    ("identity", re.compile(
        r"\b(who are you|what is your name|what's your name|whats your name|your name|introduce yourself|are you a robot|are you human"
        r"|tum kaun ho|aap kaun ho|aap kaun hain|tumhara naam|aapka naam|nimma hesaru|ninna hesaru|neevu yaaru)\b"
        r"|आप कौन|तुम कौन|आपका नाम|तुम्हारा नाम|ನೀವು ಯಾರು|ನಿಮ್ಮ ಹೆಸರು|ನಿನ್ನ ಹೆಸರು"
    )),
    ("help", re.compile(
        r"\b(what can you do|how can you help|what do you do|help me|can you help|what are you able to do|kya kar sakti ho|kya kar sakte ho|kya kar sakti hain)\b"
        r"|क्या कर सकती|क्या कर सकते|मदद कर|ನೀವು ಏನು ಮಾಡಬಹುದು|ಸಹಾಯ ಮಾಡ"
    )),
    ("goodbye", re.compile(r"\b(bye|goodbye|good bye|see you|see ya|good night|alvida|phir milenge|tata|hogi baruttini)\b|अलविदा|फिर मिलेंगे|ಬಾಯ್|ಹೋಗಿ ಬರುತ್ತೇನೆ|ಶುಭ ರಾತ್ರಿ")),
    ("greeting", re.compile(
        r"^(hi+|hello+|hey+|hai|helo|namaste|namaskar|namaskara|namaskaram|vanakkam|good (morning|afternoon|evening)|guru ?ji|ok guru ?ji|hello guru ?ji|hi guru ?ji)\b"
        r"|^(नमस्ते|नमस्कार|हेलो|हाय|प्रणाम|ನಮಸ್ಕಾರ|ಹಲೋ|ಹಾಯ್)"
    )),
    ("ack", re.compile(r"^(ok|okay|fine|great|nice|cool|good|super|perfect|alright|all right|got it|theek hai|thik hai|accha|acha|achha|haan|sari|ಸರಿ|ठीक है|अच्छा|हाँ)$")),
)
_SMALL_TALK_FILLERS = frozenset({
    "guru", "ji", "gurují", "please", "so", "much", "very", "a", "lot", "again", "there", "dear", "sir", "madam", "mam", "maam", "bhai", "and", "you", "u",
    "जी", "गुरु", "गुरुजी", "ಗುರು", "ಗುರೂಜಿ", "ಜೀ", "बहुत", "ತುಂಬಾ", "friend", "buddy", "today", "aaj",
})

_WEB_ANCHOR = re.compile(
    r"\b(internet|the web|web search|on the web|from the web|online|google|googling|net pe|net par|browse|wikipedia|youtube)\b"
    r"|इंटरनेट|इन्टरनेट|गूगल|ऑनलाइन|ऑन लाइन|वेब|ಇಂಟರ್ನೆಟ್|ಗೂಗಲ್|ಆನ್‌ಲೈನ್|ಆನ್ ಲೈನ್|ವೆಬ್"
)
_WEB_VERB = re.compile(
    r"\b(search|google|look up|lookup|find|check|browse|dhoondo|dhundo|khojo|dekho|search karo|batao|huduku|hudukku)\b"
    r"|खोज|ढूंढ|ढूँढ|सर्च|देखो|बताओ|ಹುಡುಕ|ನೋಡಿ|ಹೇಳಿ|ಸರ್ಚ್"
)
_WEB_DIRECT = re.compile(
    r"^(google|search for|search the web|search online|search internet|look up online)\b"
    r"|\b(latest|today'?s|current|live|breaking|recent)\s+(news|headlines|score|scores|weather|price|prices|rate|rates|updates?|results?)\b"
    r"|\b(news|headlines)\s+(about|on|of|for|today)\b|\bwhat'?s in the news\b|\bweather (in|at|today|tomorrow|for)\b"
    r"|\b(stock price|share price|gold rate|petrol price|diesel price|exchange rate|who won|match score)\b"
    r"|ताज़ा खबर|ताजा खबर|आज की खबर|समाचार|मौसम|ಇಂದಿನ ಸುದ್ದಿ|ಸುದ್ದಿ|ಹವಾಮಾನ"
)
# About us: the institution's own presence online is an institutional task.
_SELF_REFERENCE = re.compile(
    r"\b(our|my|this) (college|institution|institute|university|campus|school|department|dept|principal|hod|students?|faculty|placements?|results?|exams?|admissions?)\b"
    r"|\babout us\b|\bnamma (college|kaleju)\b|\bhamar[ae] (college|sanstha)\b"
    r"|हमारे कॉलेज|हमारा कॉलेज|हमारी संस्था|मेरे कॉलेज|ನಮ್ಮ ಕಾಲೇಜ|ನಮ್ಮ ಸಂಸ್ಥೆ"
)
# Longer phrases first: an alternation takes the first branch that matches.
_QUERY_NOISE = re.compile(
    r"\b(internet|net|google) (pe|par|mein)\b|\b(search|google) (karo|kariye|kijiye|karke batao)\b|\bke baa?re (mein|me)\b"
    r"|\b(dhoondo|dhundo|khojo|batao|bataiye|dekho)\b"
    r"|^(please|can you|could you|would you|kindly|hey|ok|okay|guru ?ji)\s+"
    r"|\b(please|kindly)\b"
    r"|\b(search|google|look up|lookup|find|check|browse)( (for|about|on))?\b"
    r"|\b(on|from|in|using|via) (the )?(internet|web|google|net)\b|\b(the )?(internet|web) (for|about)\b|\bonline\b|\binternet\b"
    r"|\b(and )?(tell|let) me( know)?\b"
    r"|इंटरनेट पर|इन्टरनेट पर|गूगल पर|ऑनलाइन|खोजो|खोजिए|ढूंढो|सर्च करो|बताओ|बताइए|के बारे में"
    r"|ಇಂಟರ್ನೆಟ್‌ನಲ್ಲಿ|ಇಂಟರ್ನೆಟ್ ನಲ್ಲಿ|ಗೂಗಲ್‌ನಲ್ಲಿ|ಹುಡುಕಿ|ಹುಡುಕು|ಹೇಳಿ|ಬಗ್ಗೆ",
    re.IGNORECASE,
)


# Words that make a turn a question for the institution's records. The
# read-only assistant (used when the data platform is off) answers every
# prompt from its overview tool, so anything without them is conversation.
_INSTITUTIONAL = re.compile(
    r"\b(attendance|attendence|present|absent|students?|faculty|staff|teachers?|hod|principal|fees?|dues|results?|marks|exams?|grades?|"
    r"semester|sem|department|dept|programs?|courses?|admissions?|placements?|college|institution|institute|campus|overview|summary|"
    r"source|health|availability|report|policy|policies|circular|notice|syllabus|timetable|events?|at risk)\b"
    r"|हाज़िरी|हाजिरी|उपस्थिति|छात्र|फ़ीस|फीस|परिणाम|रिज़ल्ट|परीक्षा|कॉलेज|विभाग|ಹಾಜರಾತಿ|ವಿದ್ಯಾರ್ಥಿ|ಶುಲ್ಕ|ಫಲಿತಾಂಶ|ಪರೀಕ್ಷೆ|ಕಾಲೇಜು|ವಿಭಾಗ"
)


def looks_institutional(text: str) -> bool:
    return bool(_INSTITUTIONAL.search(_normalise(text)))


@dataclass(frozen=True, slots=True)
class Classification:
    intent: Intent
    small_talk: SmallTalkKind | None = None
    query: str | None = None
    news: bool = False


def _normalise(text: str) -> str:
    return " ".join(_PUNCTUATION.sub(" ", text.lower()).split())


def _small_talk(normalised: str) -> SmallTalkKind | None:
    words = normalised.split()
    if not words or len(words) > 9:
        return None
    for kind, pattern in _SMALL_TALK:
        match = pattern.search(normalised)
        if match is None:
            continue
        # Nothing but small talk: what remains after the phrase is filler.
        rest = (normalised[: match.start()] + " " + normalised[match.end():]).split()
        leftover = [word for word in rest if word not in _SMALL_TALK_FILLERS and not any(p.search(word) for _, p in _SMALL_TALK)]
        if len(leftover) <= (1 if kind in {"greeting", "thanks"} else 2):
            return kind
    return None


def extract_query(text: str) -> str:
    """The search terms without the request around them ("search the internet for X" -> "X")."""

    query = _QUERY_NOISE.sub(" ", text)
    query = " ".join(query.replace("?", " ").split()).strip(" ,.;:-!?")
    return query if len(query) >= 2 else " ".join(text.split())


def classify(text: str, *, institution_names: tuple[str, ...] = ()) -> Classification:
    normalised = _normalise(text)
    kind = _small_talk(normalised)
    if kind is not None:
        return Classification("small_talk", small_talk=kind)
    about_us = bool(_SELF_REFERENCE.search(normalised)) or any(
        name and len(name) >= 4 and name.lower() in normalised for name in institution_names
    )
    direct = _WEB_DIRECT.search(normalised)
    anchored = _WEB_ANCHOR.search(normalised) and _WEB_VERB.search(normalised)
    if (direct or anchored) and not about_us:
        news = bool(re.search(r"news|headline|latest|today|breaking|खबर|समाचार|ಸುದ್ದಿ", normalised))
        return Classification("web", query=extract_query(text)[:300], news=news)
    return Classification("task")


__all__ = ["Classification", "classify", "extract_query", "looks_institutional"]

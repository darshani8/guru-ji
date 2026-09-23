"""Which language a person spoke, so the reply comes back in the same one.

Speech recognition returns Hindi in Devanagari and Kannada in Kannada script,
so the script decides. Hinglish (Hindi typed or spoken in Latin letters,
usually mixed with English) is recognised by Hindi function words; Kajal reads
it as Hindi, which Polly supports in romanised form.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .contracts import LANGUAGES

_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
_KANNADA = re.compile(r"[ಀ-೿]")
_LATIN_WORD = re.compile(r"[a-z']+")
_HINDI_WORDS = frozenset({
    "hai", "hain", "hoon", "hu", "tha", "thi", "kya", "kyaa", "kaise", "kaisa", "kaisi", "nahi", "nahin", "mujhe", "mera", "meri", "mere",
    "aap", "aapka", "aapki", "tum", "tumhara", "karo", "kariye", "karna", "karke", "batao", "bataiye", "bataye", "kitne", "kitna", "kitni",
    "kaun", "kab", "kahan", "kyun", "kyon", "accha", "acha", "achha", "theek", "thik", "haan", "bhai", "yaar", "ke", "ki", "ka", "mein",
    "par", "pe", "se", "ko", "aur", "bhi", "abhi", "kal", "aaj", "chahiye", "dijiye", "samjhao", "dekho", "dhoondo", "khojo", "wala",
    "wali", "hamara", "hamare", "hamari", "apna", "apni", "sab", "kuch", "koi", "yeh", "ye", "woh", "wo", "jaldi", "shukriya", "namaste",
})
_ENGLISH_ANCHORS = frozenset({"the", "is", "are", "what", "how", "please", "can", "you", "of", "and", "to", "for", "my", "me", "in", "on"})


@dataclass(frozen=True, slots=True)
class DetectedLanguage:
    code: str  # en-IN | hi-IN | kn-IN
    hinglish: bool = False

    @property
    def name(self) -> str:
        if self.hinglish:
            return "Hinglish (Hindi written in Latin letters, mixed with English words as the person did)"
        return {"en-IN": "Indian English", "hi-IN": "Hindi in Devanagari script", "kn-IN": "Kannada in Kannada script"}[self.code]


def detect_language(text: str, hint: str | None = None) -> DetectedLanguage:
    kannada = len(_KANNADA.findall(text))
    devanagari = len(_DEVANAGARI.findall(text))
    if kannada and kannada >= devanagari:
        return DetectedLanguage("kn-IN")
    if devanagari:
        return DetectedLanguage("hi-IN")
    words = _LATIN_WORD.findall(text.lower())
    if words:
        hindi = sum(1 for word in words if word in _HINDI_WORDS)
        english = sum(1 for word in words if word in _ENGLISH_ANCHORS)
        if hindi >= 2 and hindi > english or (hindi >= 1 and len(words) <= 3 and hint == "hi-IN"):
            return DetectedLanguage("hi-IN", hinglish=True)
    # Latin text from a Kannada or Hindi recogniser is usually an English
    # phrase (a name, a number); the person's chosen language still wins.
    if hint in LANGUAGES and hint != "en-IN" and not words:
        return DetectedLanguage(hint)
    return DetectedLanguage("en-IN")


__all__ = ["DetectedLanguage", "detect_language"]

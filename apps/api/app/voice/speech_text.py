"""Turn a written answer into text that sounds right when spoken.

Answers carry things a listener should not hear read out: links, download
paths, citation markers, markdown and long lists of names. Speech gets a
cleaned copy; the screen keeps the full answer. Sentences are split so the
first one can be spoken while the rest are still being synthesised.
"""

from __future__ import annotations

import re

# A link or path ends before trailing punctuation, so the sentence keeps its full stop.
_URL = re.compile(r"\b(?:https?://|www\.)\S+?(?=[.,;:!?)\]]*(?:\s|$))", re.IGNORECASE)
_API_PATH = re.compile(r"(?<![\w/])/v1/\S+?(?=[.,;:!?)\]]*(?:\s|$))")
_DOWNLOAD = re.compile(r"\bDownload:\s*\S*\s*", re.IGNORECASE)
_CITATION = re.compile(r"\[(?:\d+(?:\s*[,-]\s*\d+)*|doc:[^\]]{0,80}|source:[^\]]{0,80})\]", re.IGNORECASE)
_MARKDOWN = re.compile(r"(\*\*|__|`+|^#{1,6}\s*|^\s*[-*•]\s+)", re.MULTILINE)
_EMPTY_BRACKETS = re.compile(r"\(\s*[,;]?\s*\)")
_SPACES = re.compile(r"[ \t\r\f\v]+")
_NAMES = re.compile(r"Names:\s*(?P<body>[^.]*?)(?P<more>\s+and\s+\d+\s+more)?\.", re.IGNORECASE)
# The token before a full stop that does not end a sentence.
_ABBREVIATIONS = frozenset({
    "dr", "mr", "mrs", "ms", "prof", "st", "no", "vs", "sr", "jr", "smt", "shri", "sri", "dept", "govt", "approx",
    "e.g", "i.e", "etc", "b.tech", "m.tech", "b.e", "m.e", "b.sc", "m.sc", "b.com", "m.com", "ph.d", "u.s",
})
_SENTENCE_END = re.compile(r"([.!?।॥]+)(\s+|$)")

ON_SCREEN = {
    "en-IN": "The rest is on your screen.",
    "hi-IN": "बाकी जानकारी आपकी स्क्रीन पर है।",
    "kn-IN": "ಉಳಿದ ಮಾಹಿತಿ ನಿಮ್ಮ ಪರದೆಯ ಮೇಲಿದೆ.",
}
MORE_ON_SCREEN = {
    "en-IN": "and {count} more on your screen",
    "hi-IN": "और {count} आपकी स्क्रीन पर",
    "kn-IN": "ಮತ್ತು ಇನ್ನೂ {count} ನಿಮ್ಮ ಪರದೆಯ ಮೇಲೆ",
}


def _shorten_names(text: str, language: str, keep: int = 5) -> str:
    def replace(match: re.Match[str]) -> str:
        names = [item.strip() for item in match.group("body").split(";") if item.strip()]
        extra = 0
        if match.group("more"):
            extra = int(re.sub(r"\D", "", match.group("more")) or 0)
        if len(names) <= keep and not extra:
            return "Names: " + ", ".join(re.sub(r"\s*\([^)]*\)", "", name) for name in names) + "."
        spoken = [re.sub(r"\s*\([^)]*\)", "", name) for name in names[:keep]]
        remaining = len(names) - len(spoken) + extra
        return "Names: " + ", ".join(spoken) + ", " + MORE_ON_SCREEN.get(language, MORE_ON_SCREEN["en-IN"]).format(count=remaining) + "."

    return _NAMES.sub(replace, text)


def to_speech(text: str, language: str = "en-IN") -> str:
    """A copy of ``text`` fit to be read aloud; empty when nothing speakable remains."""

    if not text:
        return ""
    spoken = _shorten_names(text, language)
    spoken = _DOWNLOAD.sub("", spoken)
    spoken = _URL.sub("", spoken)
    spoken = _API_PATH.sub("", spoken)
    spoken = _CITATION.sub("", spoken)
    spoken = _MARKDOWN.sub("", spoken)
    spoken = spoken.replace("&", " and ").replace(" | ", ", ")
    spoken = _EMPTY_BRACKETS.sub("", spoken)
    spoken = "\n".join(_SPACES.sub(" ", line).strip() for line in spoken.splitlines())
    spoken = re.sub(r"\s*\n\s*", ". ", spoken.strip())
    spoken = re.sub(r"\s+([,.;:!?।])", r"\1", spoken)
    spoken = re.sub(r"([.!?।])\.+", r"\1", spoken)
    return spoken.strip(" ,;")


def split_sentences(text: str) -> list[str]:
    """Sentences in order; decimals (75.5), degrees (B.Tech) and titles (Dr.) stay whole."""

    sentences: list[str] = []
    start = 0
    for match in _SENTENCE_END.finditer(text):
        end = match.end(1)
        before = text[start:match.start(1)].rsplit(None, 1)
        last_word = before[-1].lower().rstrip(".") if before else ""
        if match.group(1) == "." and (last_word in _ABBREVIATIONS or (len(last_word) == 1 and last_word.isalpha())):
            continue
        sentence = text[start:end].strip()
        if sentence:
            sentences.append(sentence)
        start = match.end()
    tail = text[start:].strip()
    if tail:
        sentences.append(tail)
    return sentences


def speech_chunks(text: str, *, language: str = "en-IN", max_chars: int = 1500, first_chars: int = 180, chunk_chars: int = 450) -> list[str]:
    """Group sentences into synthesis requests: a short first one so speech starts quickly.

    Speech stops at ``max_chars`` on a sentence boundary and tells the listener
    the rest is on screen, which bounds both listening time and TTS cost.
    """

    sentences = split_sentences(to_speech(text, language))
    chunks: list[str] = []
    total = 0
    truncated = False
    for sentence in sentences:
        if len(sentence) > chunk_chars:
            sentence = sentence[:chunk_chars].rsplit(" ", 1)[0] + "…"
        if total + len(sentence) > max_chars and chunks:
            truncated = True
            break
        total += len(sentence) + 1
        limit = first_chars if len(chunks) == 1 else chunk_chars
        if chunks and len(chunks[-1]) + len(sentence) + 1 <= limit:
            chunks[-1] = f"{chunks[-1]} {sentence}"
        else:
            chunks.append(sentence)
    if truncated:
        chunks.append(ON_SCREEN.get(language, ON_SCREEN["en-IN"]))
    return chunks


__all__ = ["ON_SCREEN", "speech_chunks", "split_sentences", "to_speech"]

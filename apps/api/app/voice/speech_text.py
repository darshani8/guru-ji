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


_CLAUSE_BREAK = re.compile(r"[,;:—–]\s+|\s+")


class SentenceStreamer:
    """Turns a reply arriving in pieces into speakable sentences as soon as each one ends.

    A sentence counts as finished only when its full stop is followed by
    whitespace, so "75." waiting for its "5" is never cut. A long opening clause
    is spoken at a comma once it passes ``first_chars``, so speech starts early;
    very short sentences ("Sure.") wait to join the next one. Speech stops at
    ``max_chars`` with a pointer to the screen, like ``speech_chunks``.
    """

    def __init__(self, language: str = "en-IN", *, first_chars: int = 120, chunk_chars: int = 300, min_chars: int = 25, max_chars: int = 1500) -> None:
        self.language = language
        self.first_chars = first_chars
        self.chunk_chars = chunk_chars
        self.min_chars = min_chars
        self.max_chars = max_chars
        self._buffer = ""
        self._pending = ""
        self._spoken_chars = 0
        self._emitted = 0
        self._capped = False

    @property
    def emitted(self) -> int:
        return self._emitted

    def _complete(self) -> list[str]:
        sentences: list[str] = []
        start = 0
        for match in _SENTENCE_END.finditer(self._buffer):
            if not match.group(2):
                break  # the end of what has arrived: the next piece may continue it
            before = self._buffer[start:match.start(1)].rsplit(None, 1)
            last_word = before[-1].lower().rstrip(".") if before else ""
            if match.group(1) == "." and (last_word in _ABBREVIATIONS or (len(last_word) == 1 and last_word.isalpha())):
                continue
            sentence = self._buffer[start:match.end(1)].strip()
            if sentence:
                sentences.append(sentence)
            start = match.end()
        self._buffer = self._buffer[start:]
        limit = self.first_chars if self._emitted == 0 and not self._pending else self.chunk_chars
        if not sentences and len(self._buffer) > limit:
            # No sentence end yet in a long stretch: speak up to the last clause break.
            cut = None
            for match in _CLAUSE_BREAK.finditer(self._buffer[: limit + 40]):
                if match.start() >= limit // 2:
                    cut = match
            if cut is not None:
                sentences.append(self._buffer[: cut.start()].strip())
                self._buffer = self._buffer[cut.end():]
        return sentences

    def _emit(self, pieces: list[str], *, final: bool) -> list[str]:
        out: list[str] = []
        for piece in pieces:
            self._pending = f"{self._pending} {piece}".strip()
            if len(self._pending) < self.min_chars and not final:
                continue
            out.extend(self._speak(self._pending))
            self._pending = ""
        if final and self._pending:
            out.extend(self._speak(self._pending))
            self._pending = ""
        return out

    def _speak(self, text: str) -> list[str]:
        if self._capped:
            return []
        spoken = to_speech(text, self.language)
        if not spoken:
            return []
        if self._spoken_chars and self._spoken_chars + len(spoken) > self.max_chars:
            self._capped = True
            self._emitted += 1
            return [ON_SCREEN.get(self.language, ON_SCREEN["en-IN"])]
        self._spoken_chars += len(spoken)
        self._emitted += 1
        return [spoken]

    def feed(self, delta: str) -> list[str]:
        if self._capped or not delta:
            return []
        self._buffer += delta
        return self._emit(self._complete(), final=False)

    def flush(self) -> list[str]:
        if self._capped:
            return []
        rest = self._complete()
        tail = self._buffer.strip()
        self._buffer = ""
        return self._emit(rest + ([tail] if tail else []), final=True)


__all__ = ["ON_SCREEN", "SentenceStreamer", "speech_chunks", "split_sentences", "to_speech"]

"""Generate targeted search queries from the profile and the user's question."""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime, timezone

from .profile import InstitutionProfile

DEFAULT_TOPICS: tuple[str, ...] = ("news", "admission", "placement", "event", "ranking", "accreditation", "results", "faculty", "campus")
_TOPIC_TERMS = {
    "news": "news", "placement": "placement", "event": "event", "ranking": "ranking", "accreditation": "NAAC accreditation",
    "results": "results", "faculty": "faculty", "campus": "campus", "fees": "fees", "sports": "sports", "research": "research", "convocation": "convocation",
    "scholarship": "scholarship", "controversy": "complaint", "recruitment": "recruitment",
}
_STOP = frozenset({"what", "happened", "about", "our", "the", "on", "internet", "this", "week", "month", "today", "college", "institution", "online", "news", "is", "are", "there", "any", "of", "for", "in", "and", "tell", "me", "find", "search", "please", "latest", "recent", "update", "updates", "give", "a", "an", "to", "with", "did", "do", "has", "have", "been", "was", "were", "we", "us", "it", "that", "which", "how"})
# Latin words plus Kannada script (U+0C80-U+0CFF), so a question asked in
# Kannada still yields search terms instead of falling back to generic topics.
_TERM = re.compile(r"[a-zA-Zಀ-೿][a-zA-Z0-9ಀ-೿-]{2,}")


def question_terms(question: str | None) -> list[str]:
    if not question:
        return []
    words = [word for word in _TERM.findall(question.lower()) if word not in _STOP]
    return list(dict.fromkeys(words))[:6]


def topic_term(topic: str, *, now: datetime | None = None) -> str:
    """The search words for a topic; admissions name the current year, never a hard-coded one."""

    if topic == "admission":
        return f"admission {(now or datetime.now(timezone.utc)).year}"
    return _TOPIC_TERMS.get(topic, topic)


def _rotated(items: list[str], rotation: int, step: int) -> list[str]:
    """Shift ``items`` by ``step`` places per rotation, so each run starts where the last stopped."""

    if not items or not rotation:
        return items
    shift = (rotation * max(1, step)) % len(items)
    return items[shift:] + items[:shift]


def generate_queries(
    profile: InstitutionProfile, *, question: str | None = None, topics: Sequence[str] | None = None, max_queries: int = 8, rotation: int = 0, now: datetime | None = None,
) -> list[str]:
    """Queries for one run, at most ``max_queries``.

    Identity queries (name with location, aliases, keywords) find mentions no
    topic word would, so a third of the slots is reserved for them and a long
    topic list can never crowd them out. ``rotation`` (the monitor passes its
    run count) shifts both lists so that, over successive runs, every topic
    and every alias gets its turn even when one run cannot fit them all.
    """

    limit = max(1, max_queries)
    base_names = list(profile.all_names())[:3]
    primary = base_names[0]
    with_location = f"{primary} {profile.location}".strip() if profile.location else primary
    terms = question_terms(question)
    focused: list[str] = []
    if terms:
        focused.append(f'"{primary}" {" ".join(terms[:4])}')
        if profile.location:
            focused.append(f'"{primary}" {profile.location} {" ".join(terms[:3])}')
    chosen = [topic.lower() for topic in (topics or ()) if topic] or ([] if terms else list(DEFAULT_TOPICS[:5]))
    identity = [f'"{with_location}"']
    identity.extend(f'"{alias}"' + (f" {profile.location}" if profile.location else "") for alias in base_names[1:])
    identity.extend(f'"{primary}" {keyword}' for keyword in profile.keywords[:2])
    reserved = min(len(identity), max(1, limit // 3))
    topic_slots = max(1, limit - reserved - len(focused))
    focused.extend(f'"{primary}" {topic_term(topic, now=now)}' for topic in _rotated(chosen, rotation, topic_slots))
    for program in profile.programs[:2]:
        if terms and any(program.lower() in term for term in terms):
            focused.append(f'"{primary}" {program}')
    identity = _rotated(identity, rotation, reserved)
    ordered = focused[: limit - reserved] + identity[:reserved] + focused[limit - reserved :] + identity[reserved:]
    queries: list[str] = []
    for query in ordered:
        cleaned = " ".join(query.split())
        if cleaned and cleaned.lower() not in {item.lower() for item in queries}:
            queries.append(cleaned)
    return queries[:limit]


__all__ = ["DEFAULT_TOPICS", "generate_queries", "question_terms", "topic_term"]

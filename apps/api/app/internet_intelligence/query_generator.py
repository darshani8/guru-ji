"""Generate targeted search queries from the profile and the user's question."""

from __future__ import annotations

import re
from collections.abc import Sequence

from .profile import InstitutionProfile

DEFAULT_TOPICS: tuple[str, ...] = ("news", "admission", "placement", "event", "ranking", "accreditation", "results", "faculty", "campus")
_TOPIC_TERMS = {
    "news": "news", "admission": "admission 2026", "placement": "placement", "event": "event", "ranking": "ranking", "accreditation": "NAAC accreditation",
    "results": "results", "faculty": "faculty", "campus": "campus", "fees": "fees", "sports": "sports", "research": "research", "convocation": "convocation",
    "scholarship": "scholarship", "controversy": "complaint", "recruitment": "recruitment",
}
_STOP = frozenset({"what", "happened", "about", "our", "the", "on", "internet", "this", "week", "month", "today", "college", "institution", "online", "news", "is", "are", "there", "any", "of", "for", "in", "and", "tell", "me", "find", "search", "please", "latest", "recent", "update", "updates", "give", "a", "an", "to", "with", "did", "do", "has", "have", "been", "was", "were", "we", "us", "it", "that", "which", "how"})


def question_terms(question: str | None) -> list[str]:
    if not question:
        return []
    words = [word for word in re.findall(r"[a-zA-Z][a-zA-Z0-9-]{2,}", question.lower()) if word not in _STOP]
    return list(dict.fromkeys(words))[:6]


def generate_queries(profile: InstitutionProfile, *, question: str | None = None, topics: Sequence[str] | None = None, max_queries: int = 8) -> list[str]:
    base_names = list(profile.all_names())[:3]
    primary = base_names[0]
    with_location = f"{primary} {profile.location}".strip() if profile.location else primary
    queries: list[str] = []

    def add(query: str) -> None:
        cleaned = " ".join(query.split())
        if cleaned and cleaned.lower() not in {item.lower() for item in queries}:
            queries.append(cleaned)

    terms = question_terms(question)
    if terms:
        add(f'"{primary}" {" ".join(terms[:4])}')
        if profile.location:
            add(f'"{primary}" {profile.location} {" ".join(terms[:3])}')
    chosen = [topic.lower() for topic in (topics or ()) if topic] or ([] if terms else list(DEFAULT_TOPICS[:5]))
    for topic in chosen:
        term = _TOPIC_TERMS.get(topic, topic)
        add(f'"{primary}" {term}')
    add(f'"{with_location}"')
    for alias in base_names[1:]:
        add(f'"{alias}"' + (f" {profile.location}" if profile.location else ""))
    for keyword in profile.keywords[:2]:
        add(f'"{primary}" {keyword}')
    for program in profile.programs[:2]:
        if terms and any(program.lower() in term for term in terms):
            add(f'"{primary}" {program}')
    return queries[: max(1, max_queries)]


__all__ = ["DEFAULT_TOPICS", "generate_queries", "question_terms"]

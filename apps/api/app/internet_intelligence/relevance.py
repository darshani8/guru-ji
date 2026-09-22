"""Relevance, topic tagging, and time-window filtering."""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone

from .query_generator import question_terms

TOPIC_KEYWORDS: dict[str, tuple[str, ...]] = {
    "admission": ("admission", "admissions", "apply", "application", "intake", "seats", "counselling", "cutoff", "eligibility"),
    "placement": ("placement", "placements", "recruit", "recruitment", "package", "hired", "campus drive", "internship", "offer letters"),
    "event": ("event", "fest", "seminar", "workshop", "conference", "celebrat", "inaugurat", "webinar", "hackathon", "cultural", "sports meet", "symposium"),
    "ranking": ("rank", "ranking", "nirf", "rated", "top college"),
    "accreditation": ("naac", "nba", "accredit", "autonomous", "affiliat", "approved by", "ugc", "aicte"),
    "results": ("result", "results", "topper", "pass percentage", "exam", "examination", "merit"),
    "faculty": ("faculty", "professor", "lecturer", "staff", "appointed", "principal", "vice chancellor", "hod"),
    "campus": ("campus", "hostel", "infrastructure", "library", "lab", "building", "facility"),
    "fees": ("fee", "fees", "scholarship", "tuition", "refund"),
    "controversy": ("protest", "complaint", "allegation", "fir", "police", "strike", "suspend", "ragging", "harass", "fraud", "court", "notice"),
    "research": ("research", "patent", "publication", "journal", "grant", "innovation"),
    "announcement": ("announce", "announced", "launch", "new program", "new course", "starts", "introduces"),
}
_IMPORTANT_TOPICS = frozenset({"accreditation", "ranking", "controversy", "announcement", "admission", "placement"})


def topic_tags(text: str, title: str = "") -> list[str]:
    haystack = f"{title}\n{text}".lower()
    tags = [topic for topic, keywords in TOPIC_KEYWORDS.items() if any(keyword in haystack for keyword in keywords)]
    return tags


def relevance_score(*, title: str, text: str, question: str | None, topics: Sequence[str] | None, matched_entity: bool) -> float:
    if not matched_entity:
        return 0.0
    score = 0.4
    haystack = f"{title}\n{text}".lower()
    terms = question_terms(question)
    if terms:
        overlap = sum(1 for term in terms if term in haystack) / len(terms)
        score += 0.4 * overlap
    else:
        score += 0.2
    tags = topic_tags(text, title)
    if topics:
        wanted = {topic.lower() for topic in topics}
        if wanted & set(tags):
            score += 0.2
    elif tags:
        score += 0.1
    return round(min(1.0, score), 3)


def importance(tags: Sequence[str], source_type: str) -> str:
    if source_type in {"official_website", "government", "news"} and set(tags) & _IMPORTANT_TOPICS:
        return "high"
    if set(tags) & _IMPORTANT_TOPICS or source_type in {"official_website", "government"}:
        return "medium"
    return "low"


def parse_published(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    candidates = [text, text.replace("Z", "+00:00")]
    for candidate in candidates:
        try:
            parsed = datetime.fromisoformat(candidate)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    for fmt in ("%Y-%m-%d", "%d %B %Y", "%d %b %Y", "%B %d, %Y", "%b %d, %Y", "%d/%m/%Y", "%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S %z"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    match = re.search(r"(20\d{2})-(\d{2})-(\d{2})", text)
    if match:
        try:
            return datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)), tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def within_window(published_at: datetime | None, *, window_days: int, now: datetime | None = None) -> tuple[bool, str]:
    """Return (keep, date_status) where date_status is 'in_window', 'out_of_window', or 'unknown'."""

    if published_at is None:
        return True, "unknown"
    current = now or datetime.now(timezone.utc)
    if published_at < current - timedelta(days=window_days):
        return False, "out_of_window"
    if published_at > current + timedelta(days=1):
        return True, "unknown"
    return True, "in_window"


__all__ = ["TOPIC_KEYWORDS", "importance", "parse_published", "relevance_score", "topic_tags", "within_window"]

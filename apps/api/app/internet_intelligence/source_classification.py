"""Classify where a result came from so its weight and wording can differ."""

from __future__ import annotations

from .profile import InstitutionProfile
from .urls import domain_of

SOCIAL_DOMAINS = ("facebook.com", "instagram.com", "twitter.com", "x.com", "linkedin.com", "youtube.com", "threads.net", "t.me")
FORUM_DOMAINS = ("reddit.com", "quora.com", "shiksha.com/forum", "collegedunia.com/reviews", "pagalguy.com")
EDUCATION_PORTALS = ("shiksha.com", "collegedunia.com", "careers360.com", "collegedekho.com", "getmyuni.com", "collegesearch.in", "collegebatch.com", "edufever.com", "universitykart.com", "aglasem.com", "sarvgyan.com", "targetstudy.com", "campusoption.com")
GOVERNMENT_DOMAINS = ("gov.in", "nic.in", "ugc.gov.in", "aicte-india.org", "naac.gov.in", "nirfindia.org", "education.gov.in", "nba.co.in", "ncte.gov.in", "kea.kar.nic.in", "ac.in")
NEWS_MARKERS = ("news", "times", "express", "hindu", "ndtv", "deccan", "herald", "tribune", "mirror", "chronicle", "today", "post", "gazette", "journal", "prabha", "vijaya", "udayavani", "tv9", "abplive", "zee", "indiatoday", "livemint", "firstpost", "thequint", "scroll.in", "theprint", "outlook", "week", "edexlive", "newsminute", "bhaskar", "jagran", "patrika", "lokmat", "samachar")


def classify_source(url: str, profile: InstitutionProfile | None = None) -> str:
    if profile is not None and profile.is_official_url(url):
        return "official_website"
    domain = domain_of(url)
    path = url.lower()
    for item in FORUM_DOMAINS:
        if item in f"{domain}{path}":
            return "public_forum"
    if any(domain == item or domain.endswith("." + item) for item in SOCIAL_DOMAINS):
        return "public_social"
    if any(domain == item or domain.endswith("." + item) for item in EDUCATION_PORTALS):
        return "education_portal"
    if any(domain == item or domain.endswith("." + item) for item in GOVERNMENT_DOMAINS):
        if domain.endswith(".ac.in") or domain.endswith(".edu") or domain.endswith(".edu.in"):
            return "academic_website"
        return "government"
    if domain.endswith((".edu", ".edu.in", ".ac.in")):
        return "academic_website"
    if any(marker in domain for marker in NEWS_MARKERS):
        return "news"
    return "other_web"


SOURCE_LABELS = {
    "official_website": "Official institution website", "government": "Government/public website", "news": "News website", "education_portal": "Education portal",
    "public_social": "Public social-media page", "public_forum": "Public forum/discussion", "academic_website": "Academic website", "other_web": "Other web source",
}


__all__ = ["SOURCE_LABELS", "classify_source"]

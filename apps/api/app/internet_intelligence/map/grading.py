"""The grading rule: evidence in, grade and reasons out.

Grades are never typed in or taken from an agent's opinion; they are
recomputed from the evidence log by this one versioned rule, so the same
evidence always gives the same grade and any grade can be explained. The rule:

* D  - refuted by a reviewer, marked a look-alike or impersonation, on a
       parked or hijacked domain, or dead on two checks at least a day apart.
* O  - the owner confirmed it (domain-verified accounts file, meta tag, DNS).
* A  - a live identity link (header, navigation, footer, sameAs, rel=me) on a
       healthy official page, or a configured official domain that is live
       and healthy. Only in an archived copy: A-arch ("was official then").
* B  - one step from an O/A anchor (a hub or account page it links from), an
       official directory record, a reviewer's confirmation, or at least two
       independent channels (vendors) agreeing. A link passes on at most one
       grade less than its source, so nothing becomes A second-hand.
* C  - a single channel: one search snippet, one community record
       (Wikidata, a user-made directory profile), one directory mention, or
       an imported claim nobody has re-verified.

A live, healthy subdomain of an official domain is one step below its
parent (only the domain's owner controls its DNS).

"Blocked" (a login wall or 403) is not "dead": it changes the status, never
the grade. A snippet never refreshes "last verified".
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .store import GRADE_RANK

SCORER_VERSION = "grader-1"
DEAD_CONFIRMATION = timedelta(hours=24)
# Kinds whose channel counts toward the two-independent-channels rule.
_CORROBORATING = frozenset({"search_snippet", "directory_record", "community_record", "hub_link", "reviewer_confirm", "backlink", "api_identity"})
_ONE_STEP_DOWN = {"O": "A", "A": "B", "A-arch": "C", "B": "C", "C": "C"}


@dataclass(slots=True)
class GradeResult:
    grade: str
    reasons: list[str] = field(default_factory=list)
    status: str | None = None  # a status change the evidence implies (live, blocked, dead, parked, hijacked, compromised)
    verified_at: str | None = None
    verified_via: str | None = None


def _when(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _detail(item: Mapping[str, Any]) -> str:
    return str(item.get("detail") or "")


def grade(asset: Mapping[str, Any], evidence: Sequence[Mapping[str, Any]], *, now: datetime | None = None) -> GradeResult:
    """Grade one asset from its evidence (oldest first)."""

    current = now or datetime.now(timezone.utc)
    ordered = sorted(evidence, key=lambda item: (_when(item.get("observed_at")), str(item.get("evidence_id", ""))))
    supports = [item for item in ordered if item.get("polarity") == "supports"]
    refutes = [item for item in ordered if item.get("polarity") == "refutes"]
    status = _status(ordered, current)
    live = [item for item in supports if item.get("observed_via") in {"live", "owner", "reviewer"}]
    verified = live[-1] if live else None
    verified_at = str(verified["observed_at"]) if verified else None
    verified_via = str(verified["observed_via"]) if verified else None

    # -- D: refutations win over everything that came before them.
    for item in reversed(refutes):
        kind = item.get("kind")
        if kind in {"reviewer_reject", "lookalike", "impersonation"}:
            later = [s for s in supports if s.get("kind") in {"owner_claim", "reviewer_confirm"} and _when(s.get("observed_at")) > _when(item.get("observed_at"))]
            if not later:
                return GradeResult("D", [f"{kind}: {_detail(item)[:120]}"], status, verified_at, verified_via)
        if kind == "integrity" and _detail(item).split(":", 1)[0] in {"parked", "hijacked"}:
            return GradeResult("D", [f"domain {_detail(item).split(':', 1)[0]}"], status, verified_at, verified_via)
    if status == "dead":
        return GradeResult("D", ["dead on two checks at least a day apart"], status, verified_at, verified_via)

    # -- O / A / A-arch / B / C from supporting evidence. An official link that
    # a later clean fetch of the same page no longer found stops counting.
    withdrawn = {str(item.get("source_url")): _when(item.get("observed_at")) for item in refutes if item.get("kind") == "official_link"}
    candidates: list[tuple[str, str]] = []
    channels: dict[str, str] = {}
    for item in supports:
        if item.get("kind") == "official_link" and str(item.get("source_url")) in withdrawn and withdrawn[str(item.get("source_url"))] > _when(item.get("observed_at")):
            continue
        kind, via, detail = item.get("kind"), item.get("observed_via"), _detail(item)
        if kind == "owner_claim" and via == "owner":
            candidates.append(("O", f"owner confirmed ({detail[:80]})"))
        elif kind == "official_link":
            # detail is "<anchor grade>:<position>": a footer link on an A/O
            # domain is A; on a B domain it can only be B.
            anchor, _, position = detail.partition(":")
            label = f"official link ({position or detail}) on {str(item.get('source_url', ''))[:120]}"
            if via == "live":
                candidates.append(("A" if anchor in {"O", "A"} else "B" if anchor == "B" else "C", label))
            elif via == "archive":
                candidates.append(("A-arch" if anchor in {"O", "A", "A-arch"} else "C", label))
            else:
                candidates.append(("C", label))
        elif kind == "configured_domain":
            candidates.append(("A", "official domain configured by the institution"))
        elif kind == "hub_link":
            source_grade = detail.split(":", 1)[0] if ":" in detail else "C"
            candidates.append((_ONE_STEP_DOWN.get(source_grade, "C"), f"linked from a {source_grade}-graded page ({detail[:80]})"))
        elif kind == "subdomain" and via == "live":
            # Only a domain's owner can point its subdomains somewhere, so a
            # live, healthy subdomain is one step from its parent's grade.
            parent = detail.split(":", 1)[0]
            candidates.append((_ONE_STEP_DOWN.get(parent, "C"), f"live subdomain of a {parent}-graded domain ({detail[:80]})"))
        elif kind == "directory_record":
            # A regulator's listing of an institution's website (AICTE, UGC,
            # NIRF, NAAC, NMC, VTU) anchors a domain; other directories corroborate.
            authoritative = detail.startswith("authority:") and asset.get("kind") == "domain"
            candidates.append(("A" if authoritative else "B", f"{'authoritative ' if authoritative else ''}directory record ({detail[:80]})"))
        elif kind == "reviewer_confirm":
            candidates.append(("B", f"confirmed by a reviewer ({detail[:80]})"))
        elif kind == "api_identity":
            candidates.append(("C", f"platform API identity ({detail[:80]})"))
        elif kind in {"search_snippet", "backlink", "community_record"}:
            candidates.append(("C", f"{kind.replace('_', ' ')} ({detail[:80]})"))
        elif kind == "imported_claim":
            candidates.append(("C", "unverified imported claim"))
        if kind in _CORROBORATING and item.get("channel"):
            channels.setdefault(str(item["channel"]), str(kind))
    if len(channels) >= 2:
        candidates.append(("B", f"{len(channels)} independent channels agree ({', '.join(sorted(channels))[:120]})"))
    if not candidates:
        return GradeResult("unrated", ["no supporting evidence yet"], status, verified_at, verified_via)
    best_rank = max(GRADE_RANK[value] for value, _ in candidates)
    best = next(value for value, _ in candidates if GRADE_RANK[value] == best_rank)
    reasons = list(dict.fromkeys(reason for value, reason in candidates if value == best))
    compromised = any(item.get("kind") == "integrity" and _detail(item).startswith("compromised") for item in _latest_integrity(ordered))
    if compromised and best in {"A", "O"}:
        reasons.append("the official site is compromised; it cannot vouch for new accounts until cleaned")
    return GradeResult(best, reasons[:6], status, verified_at, verified_via)


def _latest_integrity(ordered: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    latest = [item for item in ordered if item.get("kind") == "integrity"]
    return latest[-1:] if latest else []


def _status(ordered: Sequence[Mapping[str, Any]], now: datetime) -> str | None:
    """The status the latest observations imply; None leaves the stored status alone."""

    integrity = _latest_integrity(ordered)
    if integrity:
        verdict = _detail(integrity[0]).split(":", 1)[0]
        if integrity[0].get("polarity") == "refutes" and verdict in {"parked", "hijacked", "compromised"}:
            return verdict
    liveness = [item for item in ordered if item.get("kind") == "liveness"]
    if not liveness:
        return "live" if integrity else None
    last = liveness[-1]
    if last.get("polarity") == "supports":
        return "live"
    reason = _detail(last).split(":", 1)[0]
    if reason in {"blocked", "login_wall", "robots", "snippet_only"}:
        return "blocked"
    if reason in {"not_found", "gone"}:
        # Dead only when a second failure came at least a day after the first
        # in the same unbroken run of failures.
        failures: list[Mapping[str, Any]] = []
        for item in reversed(liveness):
            if item.get("polarity") == "supports":
                break
            if _detail(item).split(":", 1)[0] in {"not_found", "gone"}:
                failures.append(item)
        if len(failures) >= 2 and _when(failures[0].get("observed_at")) - _when(failures[-1].get("observed_at")) >= DEAD_CONFIRMATION:
            return "dead"
        return None
    return None


def apply_disputes(assets: Iterable[Mapping[str, Any]], grades: Mapping[str, str]) -> dict[str, str]:
    """Two or more accounts claiming to be one entity's official channel on one platform, none
    anchored to an official page or owner: all of them are disputed and held at B at most."""

    groups: dict[tuple[str, str], list[str]] = {}
    for asset in assets:
        if asset.get("relation") == "official" and asset.get("entity_id") and asset.get("kind") == "account":
            groups.setdefault((str(asset["entity_id"]), str(asset["platform"])), []).append(str(asset["asset_id"]))
    capped: dict[str, str] = {}
    for members in groups.values():
        if len(members) < 2:
            continue
        ranks = [GRADE_RANK.get(grades.get(member, "unrated"), 1) for member in members]
        if max(ranks) >= GRADE_RANK["A-arch"]:
            continue  # at least one is anchored; the others are simply more accounts
        for member in members:
            if GRADE_RANK.get(grades.get(member, "unrated"), 1) >= GRADE_RANK["B"]:
                capped[member] = "disputed"
    return capped


__all__ = ["SCORER_VERSION", "GradeResult", "apply_disputes", "grade"]

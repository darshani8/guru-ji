"""The grading rule: evidence in, grade and reasons out.

Grades are never typed in or taken from an agent's opinion; they are
recomputed from the evidence log by this one versioned rule, so the same
evidence always gives the same grade and any grade can be explained. The rule:

* D  - refuted by a reviewer, marked a look-alike or impersonation, on a
       parked or hijacked domain, or dead on two checks at least a day apart.
       Only a reviewer's later confirmation lifts a refutation or a takeover:
       an owner's proof is published on the site itself, so whoever holds a
       compromised or re-registered domain could publish it.
* O  - the owner confirmed it (domain-verified accounts file, meta tag, DNS).
* A  - a live identity link (header, navigation, footer, sameAs, rel=me) on a
       healthy official page, or a configured official domain that is live
       and healthy (one that now redirects to another host is B, status
       "redirected"). Only in an archived copy: A-arch ("was official then").
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

Retention leans on this rule: past the retention period it blanks only the
free text the rule never parses (FREE_TEXT_KINDS) and deletes only the
observations it no longer reads (superseded_observations), so no grade moves.
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
# Kinds whose detail is free text (a page or channel title, a reviewer's note,
# an imported claim, why something is a look-alike). The rule reads only their
# kind, polarity, channel, time and how they were observed, and quotes the
# detail at the end of a reason, so retention (MapStore.prune_retention) can
# replace the text with EXPIRED_DETAIL without moving a grade. A kind whose
# detail the rule parses (official_link, hub_link, subdomain, directory_record,
# integrity, liveness, owner_claim) must never be listed here.
FREE_TEXT_KINDS = frozenset({"search_snippet", "imported_claim", "reviewer_confirm", "reviewer_reject", "impersonation", "lookalike", "spam_indexed", "backlink", "api_identity", "community_record"})
EXPIRED_DETAIL = "[expired]"
_OBSERVATIONS = frozenset({"liveness", "integrity"})
_NOT_FOUND = frozenset({"not_found", "gone"})


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
    # A source a reviewer rejected (a look-alike site, an impostor's hub) lends nothing, before or since: not even A-arch.
    refuted_sources = {str(item["source_asset_id"]) for item in refutes if item.get("kind") == "source_refuted" and item.get("source_asset_id")}
    supports = [item for item in supports if not (item.get("kind") in {"official_link", "hub_link", "subdomain", "backlink"} and str(item.get("source_asset_id")) in refuted_sources)]
    status = _status(ordered, current)
    takeover = _takeover(ordered)
    live = [item for item in supports if item.get("observed_via") in {"live", "owner", "reviewer"}]
    verified = live[-1] if live else None
    verified_at = str(verified["observed_at"]) if verified else None
    verified_via = str(verified["observed_via"]) if verified else None

    # -- D: refutations win over everything that came before them.
    for item in reversed(refutes):
        kind = item.get("kind")
        if kind in {"reviewer_reject", "lookalike", "impersonation"}:
            if not _confirmed_after(ordered, _when(item.get("observed_at"))):
                return GradeResult("D", [f"{kind}: {_detail(item)[:120]}"], status, verified_at, verified_via)
    if takeover:
        return GradeResult("D", [f"domain {takeover}"], status, verified_at, verified_via)
    if status == "dead":
        return GradeResult("D", ["dead on two checks at least a day apart"], status, verified_at, verified_via)

    # -- O / A / A-arch / B / C from supporting evidence. An official link that
    # a later clean fetch of the same page no longer found stops counting.
    withdrawn = {str(item.get("source_url")): _when(item.get("observed_at")) for item in refutes if item.get("kind") == "official_link"}
    # A domain or hub that died, lapsed or was taken over no longer vouches:
    # what it linked before then is history ("was official then").
    anchor_lost = {str(item.get("source_asset_id")): _when(item.get("observed_at")) for item in refutes if item.get("kind") == "anchor_lost" and item.get("source_asset_id")}
    # The owner can take a confirmation back (token removed, account delisted).
    owner_withdrawn = {str(item.get("channel")): _when(item.get("observed_at")) for item in refutes if item.get("kind") == "owner_claim"}
    latest_integrity = _latest_integrity(ordered)
    redirected_to = (
        _detail(latest_integrity[0]).split(":", 1)[1] or "another host"
        if latest_integrity and latest_integrity[0].get("polarity") == "refutes" and _detail(latest_integrity[0]).startswith("redirects_offsite:") else ""
    )
    candidates: list[tuple[str, str]] = []
    channels: dict[str, str] = {}
    for item in supports:
        if item.get("kind") == "official_link" and str(item.get("source_url")) in withdrawn and withdrawn[str(item.get("source_url"))] > _when(item.get("observed_at")):
            continue
        kind, via, detail = item.get("kind"), item.get("observed_via"), _detail(item)
        stale = str(item.get("source_asset_id")) in anchor_lost and anchor_lost[str(item.get("source_asset_id"))] > _when(item.get("observed_at"))
        if stale and kind == "official_link" and via == "live":
            via = "archive"
        elif stale and kind in {"hub_link", "subdomain"}:
            candidates.append(("C", f"{kind.replace('_', ' ')} from a source that no longer vouches ({detail[:80]})"))
            continue
        if kind == "owner_claim" and via == "owner":
            if owner_withdrawn.get(str(item.get("channel")), datetime.min.replace(tzinfo=timezone.utc)) < _when(item.get("observed_at")):
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
            if redirected_to:
                # The institution configured it, but it now sends visitors to
                # another host (a move, a lapse, a takeover): it vouches for
                # nothing until a clean fetch of its own pages says otherwise.
                candidates.append(("B", f"official domain configured by the institution, now redirecting to {redirected_to[:80]}"))
            else:
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


def _latest_integrity_by_channel(ordered: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    """The newest integrity observation from each channel (the site's own pages, DNS, ...)."""

    latest: dict[str, Mapping[str, Any]] = {}
    for item in ordered:
        if item.get("kind") == "integrity":
            latest[str(item.get("channel") or "")] = item
    return latest


def _confirmed_after(ordered: Sequence[Mapping[str, Any]], moment: datetime) -> bool:
    """A reviewer confirmed the asset after ``moment``.

    An owner's token never counts here: it is public and lives on the site,
    so a hijacker or a re-registrant could republish it, and a reviewer's
    rejection must not be undone by the very account it rejected.
    """

    return any(
        item.get("polarity") == "supports" and item.get("kind") == "reviewer_confirm" and item.get("observed_via") == "reviewer" and _when(item.get("observed_at")) > moment
        for item in ordered
    )


def _takeover(ordered: Sequence[Mapping[str, Any]]) -> str | None:
    """'hijacked' or 'parked' while the domain is out of the institution's hands, else None.

    A hijacker can serve a page that looks clean (and republish the old
    ownership token), so a hijack stands until a reviewer confirms the
    domain again. A parked lander is cleared as soon as each channel that
    saw it sees a healthy page (the registration was renewed), or by the
    same confirmation.
    """

    for item in ordered:
        if item.get("kind") == "integrity" and item.get("polarity") == "refutes" and _detail(item).startswith("hijacked") and not _confirmed_after(ordered, _when(item.get("observed_at"))):
            return "hijacked"
    for item in _latest_integrity_by_channel(ordered).values():
        if item.get("polarity") == "refutes" and _detail(item).startswith("parked") and not _confirmed_after(ordered, _when(item.get("observed_at"))):
            return "parked"
    return None


def _status(ordered: Sequence[Mapping[str, Any]], now: datetime) -> str | None:
    """The status the latest observations imply; None leaves the stored status alone."""

    integrity = _latest_integrity(ordered)
    takeover = _takeover(ordered)
    if takeover:
        return takeover
    verdicts = {_detail(item).split(":", 1)[0] for item in _latest_integrity_by_channel(ordered).values() if item.get("polarity") == "refutes"}
    if "compromised" in verdicts:
        return "compromised"
    if "redirects_offsite" in verdicts:
        return "redirected"
    liveness = [item for item in ordered if item.get("kind") == "liveness"]
    if not liveness:
        return "live" if integrity else None
    last = liveness[-1]
    if last.get("polarity") == "supports":
        return "live"
    reason = _detail(last).split(":", 1)[0]
    if reason in {"blocked", "login_wall", "robots", "snippet_only"}:
        return "blocked"
    if reason in _NOT_FOUND:
        # Dead only when a second failure came at least a day after the first
        # in the same unbroken run of failures (superseded_observations keeps
        # what this reads).
        failures: list[Mapping[str, Any]] = []
        for item in reversed(liveness):
            if item.get("polarity") == "supports":
                break
            if _detail(item).split(":", 1)[0] in _NOT_FOUND:
                failures.append(item)
        if len(failures) >= 2 and _when(failures[0].get("observed_at")) - _when(failures[-1].get("observed_at")) >= DEAD_CONFIRMATION:
            return "dead"
        return None
    return None


def expire_quotes(reasons: Sequence[str], details: Iterable[str]) -> list[str]:
    """Grade reasons with their quotes of these details replaced by EXPIRED_DETAIL, worded as grade() now words them.

    grade() quotes a detail only at the end of a reason, as "(<first 80
    characters>)" or ": <first 120 characters>", so only such an ending is
    replaced; the same words elsewhere in a reason stay.
    """

    texts = [text for text in dict.fromkeys(details) if text]

    def expired(reason: str) -> str:
        for text in texts:
            for quote, blank in ((f"({text[:80]})", f"({EXPIRED_DETAIL})"), (f": {text[:120]}", f": {EXPIRED_DETAIL}")):
                if reason.endswith(quote):
                    return reason[: -len(quote)] + blank
        return reason

    return list(dict.fromkeys(expired(str(reason)) for reason in reasons))


def superseded_observations(rows: Iterable[Mapping[str, Any]], *, before: datetime) -> list[str]:
    """The liveness and integrity rows observed before ``before`` that grade() no longer reads (evidence ids).

    Per asset, in grade()'s own order, every row the rule can still read is
    kept whatever its age: the newest of each kind on each channel (the last
    check, each channel's latest verdict); the newest success of each kind by
    how it was observed (when the asset was last verified); every hijacked
    verdict (a hijack stands until the owner or a reviewer confirms the domain
    again); and of liveness, the last success and the oldest and newest
    not-found failures after it ("dead on two checks at least a day apart"
    spans the whole unbroken run of failures, across channels). Deleting the
    rest leaves every grade, status and verification time as it was.
    """

    by_asset: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        if row.get("kind") in _OBSERVATIONS:
            by_asset.setdefault(str(row.get("asset_id")), []).append(row)
    doomed: list[str] = []
    for items in by_asset.values():
        items.sort(key=lambda item: (_when(item.get("observed_at")), str(item.get("evidence_id", ""))))
        keep = {str(item["evidence_id"]) for item in {(item["kind"], str(item.get("channel") or "")): item for item in items}.values()}
        keep |= {str(item["evidence_id"]) for item in {(item["kind"], item.get("observed_via")): item for item in items if item.get("polarity") == "supports"}.values()}
        keep |= {str(item["evidence_id"]) for item in items if item["kind"] == "integrity" and _detail(item).startswith("hijacked")}
        liveness = [item for item in items if item["kind"] == "liveness"]
        # The last success (kept above as the newest liveness success) ends the run the "dead" test walks.
        start = max((index for index, item in enumerate(liveness) if item.get("polarity") == "supports"), default=-1)
        failures = [item for item in liveness[start + 1 :] if _detail(item).split(":", 1)[0] in _NOT_FOUND]
        keep |= {str(item["evidence_id"]) for item in failures[:1] + failures[-1:]}
        doomed.extend(str(item["evidence_id"]) for item in items if str(item["evidence_id"]) not in keep and _when(item.get("observed_at")) < before)
    return doomed


def apply_disputes(assets: Iterable[Mapping[str, Any]], grades: Mapping[str, str]) -> dict[str, str]:
    """Two or more accounts claiming to be one entity's official channel on one platform, none
    anchored to an official page or owner: all of them are disputed and held at B at most."""

    groups: dict[tuple[str, str], list[str]] = {}
    for asset in assets:
        # A refuted account (an impostor a reviewer rejected) disputes nothing.
        if grades.get(str(asset.get("asset_id")), asset.get("grade")) == "D":
            continue
        # An institution may publish several apps, so app listings never dispute each other.
        if asset.get("relation") == "official" and asset.get("entity_id") and asset.get("kind") == "account" and asset.get("platform") != "google_play":
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


__all__ = ["EXPIRED_DETAIL", "FREE_TEXT_KINDS", "SCORER_VERSION", "GradeResult", "apply_disputes", "expire_quotes", "grade", "superseded_observations"]

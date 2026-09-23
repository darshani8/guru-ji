"""Recompute grades from evidence, and bring the institution profile into the map."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from typing import Any

from ..profile import InstitutionProfile
from .assets import asset_ref
from .connectors.common import _words
from .grading import SCORER_VERSION, apply_disputes, grade
from .authority import authorities
from .store import GRADE_RANK, MapStore


# Statuses that say a site is out of the institution's hands: never held back from readers.
SECURITY_STATUSES = frozenset({"compromised", "hijacked", "parked", "redirected", "dead"})


def regrade(
    store: MapStore, institution_id: str, asset_ids: Iterable[str] | None = None, *, now: datetime | None = None, proposed: bool = False, run_id: str | None = None, publish: bool = False,
) -> list[dict[str, Any]]:
    """Recompute grades (all assets, or the given ones); returns the changes.

    ``proposed`` parks the new grades in ``proposed_grade`` (tagged with
    ``run_id``) instead of publishing them, for a run whose results a gate
    must approve first. A grade last computed by an older rule is always
    parked: a rule change reaches readers only through the gate (``rescore``),
    and so is a change to a grade already waiting for a manager. Two things
    are never parked: a fall to D or to a site out of the institution's
    hands (a hijack must not hide behind a pending proposal), and anything
    with ``publish`` (a reviewer's verdict takes effect at once).
    """

    current = now or datetime.now(timezone.utc)
    if asset_ids is None:
        assets = {asset["asset_id"]: asset for asset in store.iter_assets(institution_id)}
        wanted = list(assets)
    else:
        wanted = list(dict.fromkeys(asset_ids))
        assets = store.get_assets(institution_id, wanted)
    # Every row of every graded asset: an ascending cap would drop the newest.
    evidence_by_asset = store.evidence_for(institution_id, [asset_id for asset_id in wanted if asset_id in assets])
    # What each hub or site that passed evidence on was itself found through (see grade's found_by).
    sources = {str(row["source_asset_id"]) for rows in evidence_by_asset.values() for row in rows if row["kind"] in {"hub_link", "backlink"} and row.get("source_asset_id")}
    found_by = {
        source: frozenset(str(row["channel"]) for row in rows if row["polarity"] == "supports" and row.get("channel"))
        for source, rows in (store.evidence_for(institution_id, sorted(sources)).items() if sources else ())
    }
    results = {asset_id: grade(assets[asset_id], evidence_by_asset.get(asset_id, []), now=current, found_by=found_by) for asset_id in wanted if asset_id in assets}
    # Disputes compare an entity's official accounts on one platform, so the
    # rest of each affected group is loaded too.
    peers = dict(assets)
    for entity_id, platform in {(asset["entity_id"], asset["platform"]) for asset_id, asset in assets.items() if asset_id in results and asset["relation"] == "official" and asset["kind"] == "account" and asset["entity_id"]}:
        if asset_ids is not None:
            peers.update({peer["asset_id"]: peer for peer in store.iter_assets(institution_id, entity_id=entity_id, platform=platform, kind="account", relation="official")})
    grades = {asset_id: asset["grade"] for asset_id, asset in peers.items()}
    grades.update({asset_id: result.grade for asset_id, result in results.items()})
    disputed = apply_disputes(peers.values(), grades)
    changes: list[dict[str, Any]] = []
    # A dispute ends for the whole group once one account is anchored or the
    # others are refuted: members graded now or not, none stays 'disputed'.
    for peer_id, peer in peers.items():
        if peer_id in results:
            continue
        if peer["status"] == "disputed" and peer_id not in disputed:
            store.set_status(institution_id, peer_id, status="unknown")
        elif peer_id in disputed and peer["status"] != "disputed":
            store.set_status(institution_id, peer_id, status="disputed")
    for asset_id, result in results.items():
        asset = assets[asset_id]
        status = "disputed" if asset_id in disputed else (result.status or ("unknown" if asset["status"] == "disputed" else None))
        if status and status != asset["status"]:
            store.set_status(institution_id, asset_id, status=status, verified_via=result.verified_via, verified_at=result.verified_at)
        elif result.verified_at and result.verified_at != asset.get("last_verified_at"):
            store.set_status(institution_id, asset_id, status=asset["status"], verified_via=result.verified_via, verified_at=result.verified_at)
        stale = bool(asset.get("scorer_version")) and asset.get("scorer_version") != SCORER_VERSION
        if result.grade == asset["grade"]:
            # Same grade: the reasons and the rule are brought up to date, and a
            # proposal waiting for a manager is left exactly as it is.
            if stale or result.reasons != asset.get("grade_reasons"):
                store.restamp_grade(institution_id, asset_id, reasons=result.reasons, scorer_version=SCORER_VERSION)
        else:
            # A grade already waiting for a manager (a held pass or re-scoring) stays waiting: a later pass never publishes around the gate.
            pending = asset.get("proposed_grade") is not None
            lowered = GRADE_RANK.get(result.grade, 1) < GRADE_RANK.get(asset["grade"], 1)
            urgent = publish or (lowered and (result.grade == "D" or (result.status or asset["status"]) in SECURITY_STATUSES))
            park = proposed or ((stale or pending) and not urgent)
            # Never re-tag another run's proposal: publishing or discarding a run acts on its own proposals alone.
            parked_run = asset.get("proposed_run_id") or run_id
            store.set_grade(institution_id, asset_id, grade=result.grade, reasons=result.reasons, scorer_version=SCORER_VERSION, proposed=park, run_id=parked_run if park else None)
            if park:
                continue
        if result.grade != asset["grade"]:
            changes.append({"asset_id": asset_id, "asset_key": asset["asset_key"], "from": asset["grade"], "to": result.grade, "reasons": result.reasons})
    return changes


def entity_key(name: str) -> str:
    """A name reduced to its words, without "and": "BGS College of Engineering & Technology" and "... and Technology" are one key."""

    return "".join(word for word in _words(name.replace("&", " and ")) if word != "and")


def find_entity(store: MapStore, institution_id: str, names: Sequence[str]) -> dict[str, Any] | None:
    """The mapped entity (never a look-alike) known by any of ``names``, trying them in order."""

    entities = [entity for entity in store.list_entities(institution_id, limit=5000) if entity["kind"] != "lookalike"]
    known = [(entity, {entity_key(str(name)) for name in [entity["name"], *entity["names"]]}) for entity in entities]
    for key in (entity_key(name) for name in names):
        found = next((entity for entity, keys in known if key and key in keys), None)
        if found is not None:
            return found
    return None


def sync_profile(store: MapStore, profile: InstitutionProfile, *, run_id: str | None = None) -> dict[str, Any]:
    """Make the profile's institution an entity and its configured domains graded assets.

    A domain the institution's own managers configured is operator input:
    it anchors the map (grade A while live and healthy). The evidence is
    added once per domain, not on every sync. An entity already mapped under
    any of the profile's names (seeded as "... Engineering & Technology" or
    "BGSCET" for a profile named "... Engineering and Technology") is the
    same institution: it gains the profile's names instead of a twin.
    """

    names = [profile.name, *profile.aliases]
    existing = find_entity(store, profile.institution_id, names)
    name, kind = (str(existing["name"]), str(existing["kind"])) if existing else (profile.name, "institution")
    entity_id = store.upsert_entity(profile.institution_id, name=name, kind=kind, names=[item for item in names if item != name], locations=[profile.location] if profile.location else [])
    created: list[str] = []
    configured: set[str] = set()
    refused: list[str] = []
    for domain in profile.official_domains:
        ref = asset_ref(f"https://{domain}/")
        known = store.find_asset(profile.institution_id, ref.key)
        if known is not None and authorities(lambda entity_id: store.get_entity(profile.institution_id, entity_id), known.get("entity_id")):
            # Another group's domain (the Math's): the profile cannot make it the
            # institution's own; only that group's approvers may settle it.
            refused.append(domain)
            continue
        asset_id, is_new = store.upsert_asset(profile.institution_id, ref, entity_id=entity_id, relation="official", note="configured in the intelligence profile")
        configured.add(asset_id)
        if _configured_now(store, profile.institution_id, asset_id) is not True:
            store.add_evidence(profile.institution_id, asset_id=asset_id, kind="configured_domain", detail=f"profile of {profile.name}", channel="profile", observed_via="reviewer", run_id=run_id)
        if is_new:
            created.append(asset_id)
    # A domain taken out of the profile is no longer the institution's say-so:
    # it stops anchoring, being watched and vouching until named again.
    removed = [
        domain["asset_id"] for domain in store.iter_assets(profile.institution_id, kind="domain")
        if domain["asset_id"] not in configured and _configured_now(store, profile.institution_id, domain["asset_id"]) is True
    ]
    for asset_id in removed:
        store.add_evidence(profile.institution_id, asset_id=asset_id, kind="configured_domain", polarity="refutes", detail=f"removed from the profile of {profile.name}", channel="profile", observed_via="reviewer", run_id=run_id)
    if removed:
        regrade(store, profile.institution_id, removed)
    # Profile social handles are bare ("@bgscet") and cannot be placed on a
    # platform without guessing; they stay in the profile for entity resolution.
    return {"entity_id": entity_id, "domains_added": created, "domains_removed": removed, "domains_refused": refused}


def _configured_now(store: MapStore, institution_id: str, asset_id: str) -> bool | None:
    """True while the profile names the domain, False once it was taken out, None if it never did."""

    rows = [row for row in store.evidence_for(institution_id, [asset_id])[asset_id] if row["kind"] == "configured_domain"]
    if not rows:
        return None
    return max(rows, key=lambda row: (str(row["observed_at"]), str(row.get("evidence_id", ""))))["polarity"] == "supports"


# Evidence that the institution itself (or a person acting for it, or a
# regulator) says a domain is its own. Only such a domain vouches for accounts.
_NOMINATING = frozenset({"configured_domain", "owner_claim", "reviewer_confirm"})
# A reviewer's rejection; it stands until the owner or a reviewer confirms the asset again.
REVIEWER_REFUTATIONS = frozenset({"reviewer_reject", "lookalike", "impersonation"})
# Evidence one asset gives another by linking to it.
LINK_KINDS = ("official_link", "hub_link", "subdomain", "backlink")


def _last(rows: Iterable[dict[str, Any]], kinds: Iterable[str], polarity: str) -> str:
    wanted = set(kinds)
    return max((str(row["observed_at"]) for row in rows if row["polarity"] == polarity and row["kind"] in wanted), default="")


def nominated(store: MapStore, institution_id: str, asset_id: str) -> bool:
    """Whether the institution, a reviewer or a regulator named this domain as the institution's.

    A later reviewer rejection takes the nomination back until someone names
    it again: an outdated regulator listing of a look-alike must neither
    keep it vouching nor keep its host's leads alive.
    """

    rows = store.evidence_for(institution_id, [asset_id])[asset_id]
    named = max((str(row["observed_at"]) for row in rows if row["polarity"] == "supports" and (row["kind"] in _NOMINATING or (row["kind"] == "directory_record" and str(row["detail"]).startswith("authority:")))), default="")
    # Taken out of the profile (and not named since by the owner, a reviewer or a regulator).
    unnamed = _last(rows, {"configured_domain"}, "refutes")
    return bool(named) and named > _last(rows, REVIEWER_REFUTATIONS, "refutes") and named > unnamed


def reviewer_refuted(store: MapStore, institution_id: str, asset_id: str) -> bool:
    """Whether a reviewer's rejection of the asset stands (no owner or reviewer confirmed it since)."""

    rows = store.evidence_for(institution_id, [asset_id])[asset_id]
    refuted = _last(rows, REVIEWER_REFUTATIONS, "refutes")
    return bool(refuted) and refuted >= _last(rows, {"owner_claim", "reviewer_confirm"}, "supports")


def refute_source(store: MapStore, institution_id: str, source_asset_id: str, *, reason: str, channel: str = "reviewer", run_id: str | None = None) -> list[str]:
    """A reviewer rejected a site or hub: what it linked keeps nothing it lent.

    Each asset it linked gets one ``source_refuted`` row naming it, and the
    grader drops every link from that source, earlier or later, with no
    "was official then" fallback (``lose_anchor``'s A-arch): a look-alike's
    footer never was official. Returns the assets affected, already regraded.
    """

    linked = {row["asset_id"] for kind in LINK_KINDS for row in store.links_from(institution_id, source_asset_id, kind=kind) if row["polarity"] == "supports"}
    linked -= {row["asset_id"] for row in store.links_from(institution_id, source_asset_id, kind="source_refuted")} | {source_asset_id}
    for asset_id in sorted(linked):
        store.add_evidence(institution_id, asset_id=asset_id, kind="source_refuted", polarity="refutes", detail=reason[:120], source_asset_id=source_asset_id, channel=channel, observed_via="reviewer", run_id=run_id)
    if linked:
        regrade(store, institution_id, sorted(linked), publish=True)  # a reviewer's verdict takes effect at once
    return sorted(linked)


def forget(store: MapStore, institution_id: str, asset_id: str, *, keep_review_id: str | None = None) -> bool:
    """Take a person's asset out of the map with every trace (``MapStore.forget_asset``), then regrade what it had vouched for."""

    with store.batch(institution_id):
        cited = store.cited_by(institution_id, asset_id)
        if not store.forget_asset(institution_id, asset_id, keep_review_id=keep_review_id):
            return False
        regrade(store, institution_id, cited, publish=True)
    return True


def lose_anchor(store: MapStore, institution_id: str, source_asset_id: str, *, reason: str, run_id: str | None = None) -> list[str]:
    """Record that a domain or hub no longer vouches for what it linked (it died, lapsed or was taken over).

    Each account it vouched for gets one ``anchor_lost`` row (repeated
    failures add nothing more); the grader then treats the old live links as
    history ("was official then", A-arch) until a healthy fetch links them again.
    The owner's confirmation of every account the domain's well-known file
    listed is withdrawn too: whoever holds the domain now is not the owner.
    Returns the accounts affected, already regraded.
    """

    linked: dict[str, str] = {}  # asset -> latest time the source vouched for it
    lost_at: dict[str, str] = {}  # asset -> latest anchor_lost from this source
    for kind in ("official_link", "hub_link", "subdomain"):
        for row in store.links_from(institution_id, source_asset_id, kind=kind):
            if row["polarity"] == "supports":
                linked[row["asset_id"]] = max(linked.get(row["asset_id"], ""), str(row["observed_at"]))
    for row in store.links_from(institution_id, source_asset_id, kind="anchor_lost"):
        lost_at[row["asset_id"]] = max(lost_at.get(row["asset_id"], ""), str(row["observed_at"]))
    lost: list[str] = []
    for asset_id, vouched in linked.items():
        if lost_at.get(asset_id, "") >= vouched:
            continue  # already recorded for this loss
        store.add_evidence(institution_id, asset_id=asset_id, kind="anchor_lost", polarity="refutes", detail=reason[:80], source_asset_id=source_asset_id, channel="anchor", observed_via="live", run_id=run_id)
        lost.append(asset_id)
    lost.extend(asset_id for asset_id in withdraw_owner_listing(store, institution_id, source_asset_id, detail=f"the listing domain no longer vouches ({reason})", run_id=run_id) if asset_id not in lost)
    if lost:
        regrade(store, institution_id, lost)
    return lost


def withdraw_owner_listing(
    store: MapStore, institution_id: str, domain_asset_id: str, *, detail: str, keep: Iterable[str] = (), observed_via: str = "live", source_url: str = "", run_id: str | None = None,
) -> list[str]:
    """Withdraw the owner's confirmation of each account a domain's well-known file listed (all but ``keep``).

    A listed account is O only while its domain still speaks for the owner:
    the file gone or its token broken, the domain's own proof withdrawn, or
    the domain lost (dead, parked, hijacked, redirected) each end it. Only
    accounts whose latest word on a channel is "supports" get a row, so a
    repeated loss adds nothing. Returns the accounts withdrawn (not regraded).
    """

    kept = set(keep)
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for row in store.links_from(institution_id, domain_asset_id, kind="owner_claim"):
        latest[(row["asset_id"], row["channel"])] = row
    withdrawn: list[str] = []
    for (asset_id, channel), row in latest.items():
        if row["polarity"] == "supports" and asset_id not in kept:
            store.add_evidence(institution_id, asset_id=asset_id, kind="owner_claim", polarity="refutes", detail=detail[:200], source_url=source_url or str(row["source_url"] or ""), source_asset_id=domain_asset_id, channel=channel, observed_via=observed_via, run_id=run_id)
            withdrawn.append(asset_id)
    return unique(withdrawn)


def anchor_grade(store: MapStore, institution_id: str, asset_id: str | None) -> str:
    if not asset_id:
        return "C"
    asset = store.get_asset(institution_id, asset_id)
    if asset is None:
        return "C"
    if asset["status"] in {"parked", "hijacked", "compromised", "dead", "redirected"}:
        return "D"
    return str(asset["grade"])


def unique(items: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(items))


__all__ = ["LINK_KINDS", "REVIEWER_REFUTATIONS", "anchor_grade", "entity_key", "find_entity", "forget", "lose_anchor", "nominated", "refute_source", "regrade", "reviewer_refuted", "sync_profile", "unique", "withdraw_owner_listing"]

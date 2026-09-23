"""Recompute grades from evidence, and bring the institution profile into the map."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from typing import Any

from ..profile import InstitutionProfile
from .assets import asset_ref
from .grading import SCORER_VERSION, apply_disputes, grade
from .store import MapStore


def regrade(store: MapStore, institution_id: str, asset_ids: Iterable[str] | None = None, *, now: datetime | None = None, proposed: bool = False) -> list[dict[str, Any]]:
    """Recompute grades (all assets, or the given ones); returns the changes.

    ``proposed`` parks the new grades in ``proposed_grade`` instead of
    publishing them, for a run whose results a gate must approve first.
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
    results = {asset_id: grade(assets[asset_id], evidence_by_asset.get(asset_id, []), now=current) for asset_id in wanted if asset_id in assets}
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
        if result.grade != asset["grade"] or result.reasons != asset.get("grade_reasons"):
            store.set_grade(institution_id, asset_id, grade=result.grade, reasons=result.reasons, scorer_version=SCORER_VERSION, proposed=proposed and result.grade != asset["grade"])
        if result.grade != asset["grade"]:
            changes.append({"asset_id": asset_id, "asset_key": asset["asset_key"], "from": asset["grade"], "to": result.grade, "reasons": result.reasons})
    return changes


def sync_profile(store: MapStore, profile: InstitutionProfile, *, run_id: str | None = None) -> dict[str, Any]:
    """Make the profile's institution an entity and its configured domains graded assets.

    A domain the institution's own managers configured is operator input:
    it anchors the map (grade A while live and healthy). The evidence is
    added once per domain, not on every sync.
    """

    entity_id = store.upsert_entity(profile.institution_id, name=profile.name, kind="institution", names=list(profile.aliases), locations=[profile.location] if profile.location else [])
    created: list[str] = []
    for domain in profile.official_domains:
        ref = asset_ref(f"https://{domain}/")
        asset_id, is_new = store.upsert_asset(profile.institution_id, ref, entity_id=entity_id, relation="official", note="configured in the intelligence profile")
        if not store.has_evidence(profile.institution_id, asset_id, "configured_domain"):
            store.add_evidence(profile.institution_id, asset_id=asset_id, kind="configured_domain", detail=f"profile of {profile.name}", channel="profile", observed_via="reviewer", run_id=run_id)
        if is_new:
            created.append(asset_id)
    # Profile social handles are bare ("@bgscet") and cannot be placed on a
    # platform without guessing; they stay in the profile for entity resolution.
    return {"entity_id": entity_id, "domains_added": created}


# Evidence that the institution itself (or a person acting for it, or a
# regulator) says a domain is its own. Only such a domain vouches for accounts.
_NOMINATING = frozenset({"configured_domain", "owner_claim", "reviewer_confirm"})


def nominated(store: MapStore, institution_id: str, asset_id: str) -> bool:
    """Whether the institution, a reviewer or a regulator named this domain as the institution's."""

    for row in store.evidence_for(institution_id, [asset_id])[asset_id]:
        if row["polarity"] != "supports":
            continue
        if row["kind"] in _NOMINATING or (row["kind"] == "directory_record" and str(row["detail"]).startswith("authority:")):
            return True
    return False


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


__all__ = ["anchor_grade", "lose_anchor", "nominated", "regrade", "sync_profile", "unique", "withdraw_owner_listing"]

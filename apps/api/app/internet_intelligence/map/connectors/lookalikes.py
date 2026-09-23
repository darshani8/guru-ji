"""Look-alike domain registrations: names one slip away from an institution's own.

A typo-squatted or homoglyph domain (bgsct.ac.in, bgscet.com, or bgsсet.ac.in
with a Cyrillic "с") is how fake admission portals and phishing pages reach
students and parents. For each domain the institution, a reviewer or a
regulator named, this connector:

* asks the certificate transparency logs (SSLMate's Cert Spotter) whether
  certificates were issued for the brand token under the other common
  suffixes (bgscet.com, bgscet.org ...), when the token has five or more
  characters (a shorter one names too many unrelated sites). Cert Spotter
  has no substring search, and crt.sh, which has, disallows crawling in its
  robots.txt, so the token is looked up under each suffix instead;
* resolves generated variants of the name over DNS-over-HTTPS: another
  suffix (.in, .co.in, .org, .com, .net, .edu.in, .ac.in) and, for names of
  four characters or more, a character left out, two swapped, one doubled,
  0/o, 1/l, rn/m, a hyphen inserted, and Cyrillic homoglyphs of a few Latin
  letters (as IDN). At most ``max_variants`` names a run; the next run
  carries on where this one stopped.

A name that resolves or has a certificate goes to a person as a
"lookalike_domain" review item. Nothing becomes an asset or a lead:
registering a similar name is not wrongdoing, and only a person can tell a
namesake from an impostor.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ..pipeline import nominated
from .base import ConnectorContext, ConnectorResult, Lead
from .common import ApiClient
from .infrastructure import certificate_names, registrable, resolve

SUFFIXES: tuple[str, ...] = ("in", "co.in", "org", "com", "net", "edu.in", "ac.in")
# Cyrillic letters drawn like Latin ones; a label using one is registered as punycode (xn--).
HOMOGLYPHS: dict[str, str] = {"a": "а", "c": "с", "e": "е", "i": "і", "o": "о", "p": "р", "x": "х", "y": "у"}
MIN_TOKEN = 5  # the shortest brand token looked up in the certificate logs
MIN_TYPO_LABEL = 4  # shorter names have no typo variants that are not someone else's name
_LABEL = re.compile(r"^(?:xn--)?[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def _swap(label: str, old: str, new: str) -> list[str]:
    return [label[:index] + new + label[index + len(old):] for index in range(len(label)) if label.startswith(old, index)]


def variants(domain: str) -> list[str]:
    """Registrable names one slip away from ``domain``'s, the likeliest first (the name itself excluded)."""

    own = registrable(domain)
    label, _, suffix = own.partition(".")
    labels: list[str] = []
    if len(label) >= MIN_TYPO_LABEL:
        labels += [label[:index] + label[index + 1:] for index in range(len(label))]
        labels += [label[:index] + label[index + 1] + label[index] + label[index + 2:] for index in range(len(label) - 1) if label[index] != label[index + 1]]
        labels += [label[:index] + label[index] + label[index:] for index in range(len(label))]
        for old, new in (("o", "0"), ("0", "o"), ("l", "1"), ("1", "l"), ("rn", "m"), ("m", "rn")):
            labels += _swap(label, old, new)
        labels += [label[:index] + "-" + label[index:] for index in range(1, len(label))]
        for letter, glyph in HOMOGLYPHS.items():
            for spoof in _swap(label, letter, glyph):
                try:
                    labels.append(spoof.encode("idna").decode("ascii"))
                except UnicodeError:
                    continue
    names = [f"{label}.{other}" for other in SUFFIXES if other != suffix]
    names += [f"{candidate}.{suffix}" for candidate in labels]
    return [name for name in dict.fromkeys(names) if name != own and _LABEL.match(name.split(".", 1)[0]) and "--" not in name.split(".", 1)[0].removeprefix("xn--")]


def readable(name: str) -> str:
    """A punycode name as people see it (xn--bgset-… as bgsсet…)."""

    try:
        return name.encode("ascii").decode("idna") if "xn--" in name else name
    except UnicodeError:
        return name


@dataclass(slots=True)
class LookalikeDomainConnector:
    active: bool = False
    client: ApiClient = field(default_factory=ApiClient)
    token: str = field(default="", repr=False)  # an optional Cert Spotter API key (Bearer)
    name: str = "lookalike_domains"
    access_mode: str = "registry"
    budget_key: str = "lookalikes"
    max_grade: str = "C"
    default_interval: int = 7 * 86400
    max_variants: int = 60

    def enabled(self) -> bool:
        return self.active

    def cost(self, source: Mapping[str, Any]) -> float:
        return float(self.max_variants + len(SUFFIXES) - 1)

    def plan(self, context: ConnectorContext) -> list[Lead]:
        existing = context.store.source_targets(context.institution_id, self.name)
        leads: dict[str, Lead] = {}
        for domain in context.store.iter_assets(context.institution_id, kind="domain", relation="official"):
            own = registrable(domain["asset_key"].removeprefix("web:"))
            if own not in leads and domain["asset_id"] not in existing and nominated(context.store, context.institution_id, domain["asset_id"]):
                leads[own] = Lead(self.name, domain["asset_id"], entity_id=domain["entity_id"], asset_id=domain["asset_id"], hops=0, work_class="rotation", origin="recurring", interval_seconds=self.default_interval)
        return list(leads.values())

    async def run(self, source: Mapping[str, Any], context: ConnectorContext) -> ConnectorResult:
        domain = context.store.get_asset(context.institution_id, str(source["target"]))
        if domain is None or domain["kind"] != "domain":
            return ConnectorResult(outcome="asset_missing", prune=True)
        own = registrable(domain["asset_key"].removeprefix("web:"))
        label, _, suffix = own.partition(".")
        # Names already in the map (our own, or known namesakes) are not news.
        known = {asset["asset_key"].removeprefix("web:") for asset in context.store.iter_assets(context.institution_id, kind="domain")}
        names = [name for name in variants(own) if name not in known]
        start = (int(source.get("runs") or 0) * self.max_variants) % len(names) if names else 0
        batch = (names[start:] + names[:start])[: self.max_variants]
        found: dict[str, list[str]] = {}
        calls = failures = 0
        for name in batch:
            outcome, answers = await resolve(self.client, name, "A")
            calls += 1
            if outcome == "ok":
                found.setdefault(name, []).append(f"resolves to {', '.join(answers[:3])}" if answers else "registered (no address)")
            elif outcome != "nxdomain":
                failures += 1
        if len(label) >= MIN_TOKEN:
            for other in SUFFIXES:
                name = f"{label}.{other}"
                if other == suffix or name in known:
                    continue
                response = await certificate_names(self.client, name, token=self.token)
                calls += 1
                rows = response.json() if response.ok else None
                if isinstance(rows, list) and rows:
                    listed = sorted({str(item).lower() for row in rows if isinstance(row, dict) and isinstance(row.get("dns_names"), list) for item in row["dns_names"]})
                    found.setdefault(name, []).append(f"{len(rows)} certificate(s) issued for {', '.join(listed[:3]) or name}")
                elif not response.ok:
                    failures += 1
        result = ConnectorResult(outcome="ok" if failures < calls or not calls else "unavailable", failed=bool(calls) and failures == calls, cost=float(calls))
        for name, signs in found.items():
            result.review.append({
                "kind": "lookalike_domain", "entity_id": domain["entity_id"], "url": f"https://{name}/", "title": f"{readable(name)} looks like {own} and is registered",
                "detail": f"{'; '.join(signs)[:400]}. Check who registered it and what it serves before concluding anything: a similar name can be a namesake.",
            })
        result.notes.append(f"{len(batch)} of {len(names)} variants checked, {len(found)} registered")
        return result


__all__ = ["HOMOGLYPHS", "LookalikeDomainConnector", "MIN_TOKEN", "SUFFIXES", "readable", "variants"]

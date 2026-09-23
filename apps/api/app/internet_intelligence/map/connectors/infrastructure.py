"""Public registries: certificate transparency, RDAP and DNS over HTTPS.

The manual sweep's worst findings were infrastructure, not content: a
lapsed domain now parked, one hijacked by a gambling site, subdomains
nobody remembered. These connectors watch for that directly:

* certificates: subdomains of an official domain seen in certificate
  transparency logs become leads (a live, healthy one is one step from its
  parent's grade, because only the domain's owner controls its DNS). The
  logs are read through SSLMate's Cert Spotter API (unauthenticated at a
  low rate, or with GURU_INTELLIGENCE_CERTSPOTTER_TOKEN); crt.sh is not
  used because its robots.txt disallows every path;
* rdap: registration expiry and hold / redemption status, raised as an
  incident well before a domain lapses (urgent within two weeks);
* dns: a name that no longer resolves, or whose name servers are a parking
  service, is recorded as such.

RDAP and DNS watch only domains the institution, a reviewer or a regulator
named, or that the map grades B or better: a domain only a community edit
(Wikidata, OpenStreetMap) calls official must not raise urgent alerts.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from ..pipeline import nominated
from .base import ConnectorContext, ConnectorResult, Lead
from .common import ApiClient

CERTSPOTTER = "https://api.certspotter.com/v1/issuances"
RDAP = "https://rdap.org/domain/"
DOH = "https://dns.google/resolve"
_SECOND_LEVEL = ("ac.in", "edu.in", "org.in", "co.in", "gov.in", "net.in", "res.in", "nic.in", "ernet.in", "co.uk", "ac.uk", "org.uk", "com.au", "edu.au")
_PARKING_NS = re.compile(r"sedoparking|parkingcrew|bodis|above\.com|dan\.com|afternic|parklogic|namebrightdns|domaincontrol-parking|parked|uniregistrymarket|hugedomains", re.IGNORECASE)
_HOST = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$")
EXPIRY_WARNING = timedelta(days=45)
EXPIRY_URGENT = timedelta(days=14)
_LAPSING = ("redemption period", "pending delete", "client hold", "server hold", "inactive")


def registrable(host: str) -> str:
    """The registrable domain of a host (bgscet.ac.in for www.bgscet.ac.in); a small suffix table, not the full PSL."""

    labels = host.lower().strip(".").split(".")
    keep = 3 if ".".join(labels[-2:]) in _SECOND_LEVEL else 2
    return ".".join(labels[-keep:])


def _official_domains(context: ConnectorContext, *, grades: frozenset[str] | None = None) -> list[dict[str, Any]]:
    return [
        domain for domain in context.store.iter_assets(context.institution_id, kind="domain", relation="official")
        if grades is None or domain["grade"] in grades
    ]


def _watched_domains(context: ConnectorContext) -> list[dict[str, Any]]:
    """Official domains the institution, a reviewer or a regulator named, or graded B or better (the official_site rule)."""

    return [domain for domain in _official_domains(context) if domain["grade"] in {"O", "A", "A-arch", "B"} or nominated(context.store, context.institution_id, domain["asset_id"])]


def certificate_names(client: ApiClient, domain: str, *, token: str = "", include_subdomains: bool = True) -> Any:
    """Ask Cert Spotter for the unexpired certificates issued for ``domain`` (and its subdomains)."""

    headers = {"Authorization": f"Bearer {token}"} if token else None
    return client.request(CERTSPOTTER, params={"domain": domain, "include_subdomains": "true" if include_subdomains else "false", "expand": "dns_names"}, headers=headers)


def _plan(connector: Any, context: ConnectorContext, domains: list[dict[str, Any]], work_class: str = "recheck") -> list[Lead]:
    existing = context.store.source_targets(context.institution_id, connector.name)
    return [
        Lead(connector.name, domain["asset_id"], entity_id=domain["entity_id"], asset_id=domain["asset_id"], hops=0, work_class=work_class, origin="recurring", interval_seconds=connector.default_interval)
        for domain in domains if domain["asset_id"] not in existing
    ]


def _domain(context: ConnectorContext, source: Mapping[str, Any]) -> dict[str, Any] | None:
    asset = context.store.get_asset(context.institution_id, str(source["target"]))
    return asset if asset is not None and asset["kind"] == "domain" else None


@dataclass(slots=True)
class CertificateConnector:
    active: bool = False
    client: ApiClient = field(default_factory=ApiClient)
    token: str = field(default="", repr=False)  # an optional Cert Spotter API key (Bearer)
    name: str = "certificates"
    access_mode: str = "registry"
    budget_key: str = "certificates"
    max_grade: str = "B"
    default_interval: int = 30 * 86400
    max_subdomains: int = 40

    def enabled(self) -> bool:
        return self.active

    def cost(self, source: Mapping[str, Any]) -> float:
        return 1.0

    def plan(self, context: ConnectorContext) -> list[Lead]:
        return _plan(self, context, _official_domains(context, grades=frozenset({"O", "A", "B"})), work_class="explore")

    async def run(self, source: Mapping[str, Any], context: ConnectorContext) -> ConnectorResult:
        domain = _domain(context, source)
        if domain is None:
            return ConnectorResult(outcome="asset_missing", prune=True)
        host = domain["asset_key"].removeprefix("web:")
        response = await certificate_names(self.client, host, token=self.token)
        if not response.ok:
            return ConnectorResult(outcome=response.outcome, failed=True)
        rows = response.json()
        names: list[str] = []
        for row in rows if isinstance(rows, list) else []:
            listed = row.get("dns_names") if isinstance(row, dict) and str(row.get("not_after") or "9999")[:19] >= context.now.isoformat()[:19] else None
            for name in listed if isinstance(listed, list) else []:
                name = str(name).lower().strip().removeprefix("*.").removeprefix("www.")
                if name != host and name.endswith("." + host) and _HOST.match(name) and name not in names:
                    names.append(name)
        result = ConnectorResult(outcome="ok")
        hops = int(source.get("hops") or 0) + 1
        for name in names[: self.max_subdomains]:
            result.leads.append(Lead("lead_page", f"https://{name}/", entity_id=domain["entity_id"], hops=hops))
        result.notes.append(f"{len(names)} subdomains in certificate logs")
        return result


@dataclass(slots=True)
class RdapConnector:
    active: bool = False
    client: ApiClient = field(default_factory=ApiClient)
    name: str = "rdap"
    access_mode: str = "registry"
    budget_key: str = "rdap"
    max_grade: str = "C"
    default_interval: int = 7 * 86400

    def enabled(self) -> bool:
        return self.active

    def cost(self, source: Mapping[str, Any]) -> float:
        return 1.0

    def plan(self, context: ConnectorContext) -> list[Lead]:
        # Every watched domain, whatever its grade: a lapsing domain matters
        # most when it is already failing (a nominated one stays watched).
        return _plan(self, context, _watched_domains(context))

    async def run(self, source: Mapping[str, Any], context: ConnectorContext) -> ConnectorResult:
        domain = _domain(context, source)
        if domain is None:
            return ConnectorResult(outcome="asset_missing", prune=True)
        host = domain["asset_key"].removeprefix("web:")
        name = registrable(host)
        response = await self.client.request(RDAP + name, headers={"Accept": "application/rdap+json, application/json"})
        result = ConnectorResult(outcome=response.outcome, touched={domain["asset_id"]})
        if response.outcome == "not_found":
            result.incidents.append({"kind": "domain_unregistered", "target": name, "signals": ["RDAP has no registration: the name may be free for anyone to register"], "severity": "high"})
            return result
        if not response.ok:
            result.failed = True
            return result
        payload = response.json() or {}
        expires = None
        for event in payload.get("events") or []:
            if isinstance(event, dict) and event.get("eventAction") == "expiration":
                try:
                    expires = datetime.fromisoformat(str(event.get("eventDate", "")).replace("Z", "+00:00"))
                except ValueError:
                    expires = None
        statuses = [str(item).lower() for item in payload.get("status") or []]
        if expires is not None:
            expires = expires if expires.tzinfo else expires.replace(tzinfo=timezone.utc)
            context.store.set_observation(context.institution_id, domain["asset_id"], registration_expires_at=expires.isoformat())
            if expires - context.now <= EXPIRY_WARNING:
                # Urgent within two weeks: the store escalates the incident the digest already named.
                result.incidents.append({"kind": "domain_expiring", "target": name, "signals": [f"registration expires {expires.date().isoformat()}"], "severity": "high" if expires - context.now <= EXPIRY_URGENT else "medium"})
        lapsing = [status for status in statuses if any(flag in status for flag in _LAPSING)]
        if lapsing:
            result.incidents.append({"kind": "domain_lapsing", "target": name, "signals": lapsing[:5], "severity": "high"})
        context.store.add_evidence(context.institution_id, asset_id=domain["asset_id"], kind="registry_record", detail=f"rdap:{name}:expires {expires.date().isoformat() if expires else 'unknown'}:{','.join(statuses)[:120]}", source_url=RDAP + name, channel="rdap", observed_via="live", run_id=context.run_id)
        return result


async def resolve(client: ApiClient, name: str, record: str) -> tuple[str, list[str]]:
    """("ok" | "nxdomain" | a failure, answers) for one DNS question over HTTPS."""

    response = await client.request(DOH, params={"name": name, "type": record}, headers={"Accept": "application/dns-json"})
    if not response.ok:
        return response.outcome, []
    payload = response.json() or {}
    if payload.get("Status") == 3:
        return "nxdomain", []
    if payload.get("Status") not in (0, None):
        return "error", []
    answers = [str(item.get("data", "")).strip().strip('"') for item in payload.get("Answer") or [] if isinstance(item, dict)]
    return "ok", [answer for answer in answers if answer]


@dataclass(slots=True)
class DnsConnector:
    active: bool = False
    client: ApiClient = field(default_factory=ApiClient)
    name: str = "dns"
    access_mode: str = "registry"
    budget_key: str = "dns"
    max_grade: str = "C"
    default_interval: int = 7 * 86400

    def enabled(self) -> bool:
        return self.active

    def cost(self, source: Mapping[str, Any]) -> float:
        return 2.0

    def plan(self, context: ConnectorContext) -> list[Lead]:
        return _plan(self, context, _watched_domains(context))

    async def run(self, source: Mapping[str, Any], context: ConnectorContext) -> ConnectorResult:
        domain = _domain(context, source)
        if domain is None:
            return ConnectorResult(outcome="asset_missing", prune=True)
        host = domain["asset_key"].removeprefix("web:")
        outcome, addresses = await resolve(self.client, host, "A")
        result = ConnectorResult(outcome=outcome, touched={domain["asset_id"]})
        if outcome == "nxdomain":
            context.store.add_evidence(context.institution_id, asset_id=domain["asset_id"], kind="liveness", polarity="refutes", detail="not_found:nxdomain", source_url=f"dns:{host}", channel="dns", observed_via="live", run_id=context.run_id)
            result.incidents.append({"kind": "domain_not_resolving", "target": host, "signals": ["NXDOMAIN"], "severity": "medium"})
            return result
        if outcome != "ok":
            result.failed = True
            return result
        ns_outcome, servers = await resolve(self.client, registrable(host), "NS")
        parking = [server for server in servers if _PARKING_NS.search(server)]
        if ns_outcome == "ok" and parking:
            context.store.add_evidence(context.institution_id, asset_id=domain["asset_id"], kind="integrity", polarity="refutes", detail=f"parked:name servers {','.join(parking)[:120]}", source_url=f"dns:{host}", channel="dns", observed_via="live", run_id=context.run_id)
            result.incidents.append({"kind": "site_parked", "target": host, "signals": [f"parking name servers: {', '.join(parking)[:120]}"], "severity": "high"})
        result.notes.append(f"{len(addresses)} addresses; name servers {', '.join(servers)[:120]}")
        return result


__all__ = ["CERTSPOTTER", "CertificateConnector", "DnsConnector", "RdapConnector", "certificate_names", "registrable", "resolve"]

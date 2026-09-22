"""What every connector declares and returns.

A connector says how it reaches the web (``access_mode``), which budget it
spends from, what one run costs, and the highest grade its evidence can ever
support. The registry refuses access modes the platform does not use at all:
nothing logs in to a platform, borrows a session, or evades a block.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from ...fetch import PublicPageFetcher
from ...profile import InstitutionProfile
from ..store import MapStore

# official_api: a documented API used within its terms (YouTube Data API, Wikidata).
# public_feed: RSS/Atom/JSON feeds published for machines.
# public_page: robots-aware fetches of public pages.
# search_index: a licensed search provider's index (snippets only).
# archive: public web archives (Wayback CDX / snapshots).
# registry: public registries (RDAP, certificate transparency).
# owner: something the institution itself publishes or grants.
ACCESS_MODES = frozenset({"official_api", "public_feed", "public_page", "search_index", "archive", "registry", "owner"})
# Modes that are never allowed, whatever a future connector claims.
PROHIBITED_MODES = frozenset({"logged_in_session", "credential", "scraper_behind_login", "proxy_rotation", "captcha_solving", "ua_spoofing"})


@dataclass(frozen=True, slots=True)
class Lead:
    connector: str
    target: str
    entity_id: str | None = None
    asset_id: str | None = None
    hops: int = 1
    work_class: str = "explore"
    topic: str = ""
    origin: str = "lead"
    interval_seconds: int = 7 * 86400


@dataclass(slots=True)
class ConnectorResult:
    outcome: str = "ok"
    failed: bool = False
    yield_count: int = 0  # new assets found or grades raised by this run
    touched: set[str] = field(default_factory=set)
    new_assets: list[str] = field(default_factory=list)
    leads: list[Lead] = field(default_factory=list)
    incidents: list[dict[str, Any]] = field(default_factory=list)
    # Items a person has to look at before anything is concluded (a possible
    # impersonator, a court record); they never change a grade on their own.
    review: list[dict[str, Any]] = field(default_factory=list)
    etag: str | None = None
    last_modified: str | None = None
    cost: float | None = None  # actual units, when different from the estimate
    prune: bool = False  # the source proved irrelevant; stop scheduling it
    notes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ConnectorContext:
    store: MapStore
    institution_id: str
    run_id: str
    now: datetime
    fetcher: PublicPageFetcher | None = None
    search: Any | None = None
    profile: InstitutionProfile | None = None
    max_hops: int = 2
    extras: Mapping[str, Any] = field(default_factory=dict)

    def entities(self) -> list[dict[str, Any]]:
        return self.store.list_entities(self.institution_id)


class Connector(Protocol):
    name: str
    access_mode: str
    budget_key: str
    max_grade: str
    default_interval: int

    def enabled(self) -> bool: ...

    def cost(self, source: Mapping[str, Any]) -> float: ...

    async def run(self, source: Mapping[str, Any], context: ConnectorContext) -> ConnectorResult: ...


class ConnectorRegistry:
    def __init__(self, connectors: Iterable[Connector] = ()) -> None:
        self._connectors: dict[str, Connector] = {}
        for connector in connectors:
            self.register(connector)

    def register(self, connector: Connector) -> None:
        if connector.access_mode in PROHIBITED_MODES or connector.access_mode not in ACCESS_MODES:
            raise ValueError(f"connector {connector.name!r} uses a disallowed access mode {connector.access_mode!r}")
        self._connectors[connector.name] = connector

    def get(self, name: str) -> Connector | None:
        connector = self._connectors.get(name)
        return connector if connector is not None and connector.enabled() else None

    def names(self, *, enabled_only: bool = True) -> list[str]:
        return sorted(name for name, connector in self._connectors.items() if not enabled_only or connector.enabled())

    def describe(self) -> list[dict[str, Any]]:
        return [
            {"name": name, "access_mode": connector.access_mode, "budget_key": connector.budget_key, "max_grade": connector.max_grade, "enabled": connector.enabled(), "interval_seconds": connector.default_interval}
            for name, connector in sorted(self._connectors.items())
        ]


ConnectorFactory = Callable[[], Connector]

__all__ = ["ACCESS_MODES", "PROHIBITED_MODES", "Connector", "ConnectorContext", "ConnectorRegistry", "ConnectorResult", "Lead"]

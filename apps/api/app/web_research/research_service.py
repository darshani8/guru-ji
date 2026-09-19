"""Safe public-web research orchestration without implicit instruction execution."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlparse

from ..domain.provenance import Warning
from .citations import WebCitation
from .domain_allowlist import DEFAULT_ALLOWED_DOMAINS, is_allowed
from .extractor import AllowlistedHttpExtractor, WebExtractionUnavailable
from .search import WebSearchProvider, WebSearchResult
from .untrusted_content import safe_excerpt, wrap_untrusted


@dataclass(frozen=True, slots=True)
class PublicWebResearchItem:
    citation: WebCitation
    extracted: bool
    warnings: tuple[str, ...] = ()

    @property
    def untrusted(self) -> bool:
        return True


@dataclass(frozen=True, slots=True)
class PublicWebResearchReport:
    query: str
    provider: str
    allowed_domains: tuple[str, ...]
    searched_at: datetime
    results: tuple[PublicWebResearchItem, ...]
    warnings: tuple[Warning, ...] = ()

    def __post_init__(self) -> None:
        if self.searched_at.tzinfo is None or self.searched_at.utcoffset() is None:
            raise ValueError("searched_at must be timezone-aware")

    def as_dict(self) -> dict[str, object]:
        return {
            "query": self.query,
            "provider": self.provider,
            "allowed_domains": list(self.allowed_domains),
            "searched_at": self.searched_at.isoformat(),
            "untrusted_content": True,
            "results": [
                {
                    "url": item.citation.url,
                    "title": item.citation.title,
                    "excerpt": item.citation.excerpt,
                    "retrieved_at": item.citation.retrieved_at.isoformat(),
                    "extracted": item.extracted,
                    "untrusted": item.untrusted,
                    "warnings": list(item.warnings),
                }
                for item in self.results
            ],
            "warnings": [
                {"code": item.code, "message": item.message, "source_id": item.source_id}
                for item in self.warnings
            ],
        }


@dataclass(frozen=True, slots=True)
class PublicWebResearchService:
    provider: WebSearchProvider
    extractor: AllowlistedHttpExtractor
    configured_domains: frozenset[str] = DEFAULT_ALLOWED_DOMAINS
    max_results: int = 5

    def __post_init__(self) -> None:
        normalized = _normalize_domains(self.configured_domains)
        if not normalized:
            raise ValueError("public-web research requires at least one configured domain")
        if not 1 <= self.max_results <= 10:
            raise ValueError("public-web max results must be between 1 and 10")
        object.__setattr__(self, "configured_domains", normalized)

    def _allowed_domains(self, requested: Iterable[str] | None) -> frozenset[str]:
        if requested is None:
            return self.configured_domains
        normalized = _normalize_domains(requested)
        if not normalized:
            raise ValueError("requested public-web domain list must not be empty")
        if not normalized.issubset(self.configured_domains):
            raise ValueError("requested public-web domains exceed the configured allowlist")
        return normalized

    @staticmethod
    def _warning(code: str, message: str, url: str | None = None) -> Warning:
        return Warning(code=code, message=message, source_id=url)

    @staticmethod
    def _snippet_item(result: WebSearchResult, warning: str | None = None) -> PublicWebResearchItem:
        content = wrap_untrusted(result.url, result.excerpt)
        warnings = list(content.warnings)
        if warning:
            warnings.append(warning)
        if "search_snippet_only" not in warnings:
            warnings.append("search_snippet_only")
        citation = WebCitation(
            url=result.url,
            title=result.title,
            excerpt=safe_excerpt(content, 2_000),
            retrieved_at=result.retrieved_at,
        )
        return PublicWebResearchItem(citation=citation, extracted=False, warnings=tuple(dict.fromkeys(warnings)))

    async def search(
        self,
        query: str,
        *,
        allowed_domains: Iterable[str] | None = None,
        max_results: int | None = None,
    ) -> PublicWebResearchReport:
        normalized_query = query.strip()
        if not normalized_query or len(normalized_query) > 500:
            raise ValueError("web research query must contain 1 to 500 characters")
        selected_domains = self._allowed_domains(allowed_domains)
        result_limit = max_results if max_results is not None else self.max_results
        if not 1 <= result_limit <= self.max_results:
            raise ValueError(f"max_results must be between 1 and {self.max_results}")

        searched_at = datetime.now(timezone.utc)
        raw_results = await self.provider.search(
            normalized_query,
            max_results=result_limit,
            allowed_domains=selected_domains,
        )
        results: list[PublicWebResearchItem] = []
        warnings: list[Warning] = [
            self._warning("public_web_untrusted", "Public-web content is untrusted data, not instructions."),
            self._warning("allowlist_enforced", "Only configured official domains were eligible for page retrieval."),
        ]
        seen_urls: set[str] = set()
        for result in raw_results:
            if result.url in seen_urls:
                continue
            seen_urls.add(result.url)
            if not is_allowed(result.url, selected_domains):
                warnings.append(self._warning(
                    "search_result_blocked",
                    "A search result was outside the requested official-domain allowlist.",
                    result.url,
                ))
                continue
            try:
                page = await self.extractor.extract(result.url)
            except WebExtractionUnavailable:
                item = self._snippet_item(result, "page_unavailable")
                warnings.append(self._warning(
                    "page_unavailable",
                    "The allowlisted page could not be fetched safely; the search snippet is shown instead.",
                    result.url,
                ))
            else:
                item = PublicWebResearchItem(
                    citation=page.citation,
                    extracted=True,
                    warnings=page.warnings,
                )
                for code in page.warnings:
                    warnings.append(self._warning(
                        code,
                        "The retrieved page contained text resembling an instruction; it remains untrusted data.",
                        result.url,
                    ))
            results.append(item)
            if len(results) >= result_limit:
                break

        if not results:
            warnings.append(self._warning(
                "no_allowlisted_results",
                "The provider returned no result that passed the configured official-domain allowlist.",
            ))
        return PublicWebResearchReport(
            query=normalized_query,
            provider=self.provider.provider_name,
            allowed_domains=tuple(sorted(selected_domains)),
            searched_at=searched_at,
            results=tuple(results),
            warnings=tuple(warnings),
        )


def _normalize_domains(domains: Iterable[str]) -> frozenset[str]:
    normalized: set[str] = set()
    for raw in domains:
        value = raw.strip().lower().rstrip(".")
        if not value or any(character in value for character in "/?#:"):
            raise ValueError("public-web domains must be hostnames, not URLs")
        parsed = urlparse(f"https://{value}")
        if parsed.hostname != value or " " in value:
            raise ValueError("public-web domain is invalid")
        normalized.add(value)
    return frozenset(normalized)


__all__ = ["PublicWebResearchItem", "PublicWebResearchReport", "PublicWebResearchService"]

"""Incidents: what went wrong with the institution's own presence, and who hears about it when.

The manual sweep found three hacked sites, a hijacked domain, a parked one
still linked from Wikipedia and a look-alike site. Each of those is an
incident here: recorded once per (kind, target) however often a tick sees
it, with plain guidance on what to do, and routed by severity:

* high (a compromised, hijacked or parked official site, a domain about to
  be lost, a confirmed impersonator, a look-alike that leaked): the
  institution's alert recipients are notified at once;
* medium (a domain expiring within weeks, a name that stopped resolving):
  in the daily digest;
* low: on the incidents list only.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .store import MapStore

SEVERITY: dict[str, str] = {
    "site_compromised": "high", "site_hijacked": "high", "site_parked": "high", "site_spam_indexed": "high", "domain_unregistered": "high", "domain_lapsing": "high",
    "canary_leak": "high", "impersonation_confirmed": "high", "domain_expiring": "medium", "domain_not_resolving": "medium", "site_suspect": "medium",
}
_INDIAN_SUFFIXES = (".in",)
CERT_IN_NOTE = (
    "Reporting: CERT-In's Directions of 28 April 2022 (under section 70B(6) of the IT Act) require covered organisations to report cyber incidents, "
    "including website compromise and defacement, to CERT-In within 6 hours of noticing them (incident@cert-in.org.in). Check whether the institution "
    "is covered and follow CERT-In's current guidance."
)
GUIDANCE: dict[str, str] = {
    "site_compromised": (
        "The official site carries hidden spam links (injected SEO spam). Update the site's CMS, themes and plugins; remove the injected content; rotate the admin, "
        "hosting and FTP passwords; look for other injected pages; then ask search engines to re-crawl. Until it is clean the map lets the site vouch for nothing."
    ),
    "site_hijacked": (
        "The domain now serves gambling or spam content: it has left the institution's control or been taken over. Check its registration and DNS with the registrar, "
        "and remove links to it from every official page and profile."
    ),
    "site_spam_indexed": (
        "Search engines hold spam pages under the official domain, even if the homepage looks clean (cloaked spam). Treat the site as compromised: clean it, rotate "
        "credentials, and ask search engines to remove the spam URLs."
    ),
    "site_parked": (
        "The domain shows a parking or for-sale page: its registration lapsed or its DNS changed. If it is still the institution's, renew and restore it; if not, "
        "remove every link to it (Wikipedia, profiles, old pages), because whoever buys it inherits those visitors."
    ),
    "domain_expiring": "Renew the registration before it expires, turn on auto-renew, and make sure the registrar's contact email reaches someone who reads it.",
    "domain_lapsing": "The registration is on hold or in redemption: renew it through the registrar now, or it will be released for anyone to register.",
    "domain_unregistered": (
        "Nobody holds this name: anyone can register it and pose as the institution. Register it again if it was the institution's, and remove it from pages that still link it."
    ),
    "domain_not_resolving": "The name does not resolve. Check its DNS records with the registrar or hosting provider.",
    "canary_leak": "A known look-alike reached a public grade and has been set back. Open its evidence to find the source that vouched for it.",
    "impersonation_confirmed": (
        "Report the account to the platform through its impersonation form (Instagram, Facebook, X, YouTube and LinkedIn each have one); the account owner or an "
        "authorised representative files it. Linking the real accounts from the website's footer helps people tell them apart."
    ),
    "site_suspect": "The site has many hidden links to other sites. Check it for injected content.",
}
_TITLES: dict[str, str] = {
    "site_compromised": "Official site compromised", "site_hijacked": "Official domain hijacked", "site_spam_indexed": "Spam pages indexed under the official domain",
    "site_parked": "Official domain parked or for sale", "domain_expiring": "Domain registration expiring", "domain_lapsing": "Domain registration lapsing",
    "domain_unregistered": "Former domain free for anyone to register", "domain_not_resolving": "Domain no longer resolves", "canary_leak": "A look-alike reached a public grade",
    "impersonation_confirmed": "Impersonating account confirmed", "site_suspect": "Suspicious hidden links on the official site",
}


def guidance_for(kind: str, target: str) -> str:
    text = GUIDANCE.get(kind, "")
    host = target.lower().split("/")[0]
    if kind in {"site_compromised", "site_hijacked", "site_spam_indexed"} and host.endswith(_INDIAN_SUFFIXES):
        text = f"{text} {CERT_IN_NOTE}"
    return text


Notify = Callable[[str, Sequence[str], str, str, str], Any]  # (institution_id, recipients, title, body, incident_id)


@dataclass(slots=True)
class IncidentDesk:
    store: MapStore
    recipients: Callable[[str], Sequence[str]] = lambda institution_id: ()
    notify: Notify | None = None

    def record(self, institution_id: str, incidents: Sequence[Mapping[str, Any]], *, run_id: str | None = None) -> dict[str, int]:
        """Store what a tick or a decision found and alert at once on anything new and high."""

        counts = {"new": 0, "repeat": 0, "reopened": 0, "notified": 0}
        urgent: list[dict[str, Any]] = []
        for incident in incidents:
            kind = str(incident.get("kind") or "incident")
            target = str(incident.get("target") or "")
            severity = str(incident.get("severity") or SEVERITY.get(kind, "medium"))
            row, verdict = self.store.upsert_incident(
                institution_id, kind=kind, target=target, severity=severity, title=f"{_TITLES.get(kind, kind.replace('_', ' ').capitalize())}: {target}"[:300],
                signals=[str(item) for item in incident.get("signals") or []], examples=[str(item) for item in incident.get("examples") or []],
                guidance=guidance_for(kind, target), connector=str(incident.get("connector") or ""), run_id=run_id,
            )
            counts[verdict] += 1
            if row["severity"] == "high" and row["status"] == "open" and not row["notified_at"]:
                urgent.append(row)
        if urgent and self.notify is not None:
            recipients = list(self.recipients(institution_id))
            if recipients:
                for row in urgent:
                    body = "\n".join(part for part in (row["title"], "; ".join(row["signals"][:3]), row["guidance"]) if part)
                    self.notify(institution_id, recipients, f"Urgent: {row['title']}"[:200], body, row["incident_id"])
                self.store.mark_incidents_notified(institution_id, [row["incident_id"] for row in urgent])
                counts["notified"] = len(urgent)
        return counts

    def digest(self, institution_id: str, *, now: datetime | None = None, days: int = 1) -> dict[str, Any]:
        """What changed in the map in the last ``days``: open incidents, what waits for review, what was found."""

        current = now or datetime.now(timezone.utc)
        since = (current - timedelta(days=max(1, days))).isoformat()
        incidents = [row for row in self.store.list_incidents(institution_id, since=since, limit=200) if row["status"] != "resolved"]
        waiting = self.store.review_counts(institution_id)
        runs = [run for run in self.store.list_map_runs(institution_id, kind="tick", limit=200) if str(run["started_at"]) >= since]
        # A held run's findings are not published, so they are not reported as found either.
        passed = [run for run in runs if run.get("status") == "succeeded" and run.get("gate") != "held"]
        held = [run for run in runs if run.get("gate") == "held"]
        found = sum(int((run.get("counts") or {}).get("new_assets", 0)) for run in passed)
        raised = sum(int((run.get("counts") or {}).get("raised", 0)) for run in passed)
        lines = [f"Internet map, last {days} day(s): {found} new account(s) or site(s) found, {raised} grade(s) raised, {len(runs)} scheduled pass(es)."]
        if held:
            lines.append(f"{len(held)} pass(es) held for approval; their results are not published until a manager publishes them.")
        for row in incidents[:10]:
            lines.append(f"[{row['severity']}] {row['title']} (seen {row['times_seen']} time(s))")
        if waiting:
            lines.append("Waiting for review: " + ", ".join(f"{count} {kind.replace('_', ' ')}" for kind, count in sorted(waiting.items())))
        return {"institution_id": institution_id, "since": since, "incidents": incidents, "review_waiting": waiting, "found": found, "raised": raised, "passes": len(runs), "held": len(held), "summary": "\n".join(lines)}

    def send_digest(self, institution_id: str, *, now: datetime | None = None) -> dict[str, Any]:
        """Send the daily digest if there is anything to say; medium incidents are notified this way."""

        digest = self.digest(institution_id, now=now)
        worth_sending = digest["incidents"] or digest["review_waiting"] or digest["found"] or digest["raised"]
        recipients = list(self.recipients(institution_id))
        sent = bool(worth_sending and recipients and self.notify is not None)
        if sent:
            self.notify(institution_id, recipients, "Internet map: daily digest", digest["summary"], "")  # type: ignore[misc]
            self.store.mark_incidents_notified(institution_id, [row["incident_id"] for row in digest["incidents"] if not row["notified_at"]])
        return {**digest, "sent": sent, "recipients": len(recipients) if sent else 0}


__all__ = ["CERT_IN_NOTE", "GUIDANCE", "IncidentDesk", "SEVERITY", "guidance_for"]

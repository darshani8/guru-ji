"""Incidents: what went wrong with the institution's own presence, and who hears about it when.

The manual sweep found three hacked sites, a hijacked domain, a parked one
still linked from Wikipedia and a look-alike site. Each of those is an
incident here: recorded once per (kind, target) however often a tick sees
it, with plain guidance on what to do, and routed by severity:

* high (a compromised, hijacked or parked official site, a domain about to
  be lost, a confirmed impersonator, a look-alike that leaked): the site
  owner's security contacts are emailed and the alert recipients notified
  in-app at once; an incident on another authority's site or account (the
  Math's) goes to that authority's IT office instead;
* medium (a domain expiring within weeks, a name that stopped resolving):
  in the daily digest;
* low: on the incidents list only.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from functools import partial
from typing import Any

from .authority import authorities, members
from .store import MapStore

logger = logging.getLogger(__name__)

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


# (institution_id, recipients, title, body, incident_id) -> what it created; a notifier that returns nothing is taken at its word
Notify = Callable[[str, Sequence[str], str, str, str], Any]
# (institution_id, addresses, subject, body) -> a delivery record whose "status" says whether it went (EmailService.system_send)
Email = Callable[[str, Sequence[str], str, str], Any]
_DELIVERED = frozenset({"sent", "queued"})
# The digest goes out when the last one is this old; the scheduled pass runs hourly.
DIGEST_EVERY = timedelta(hours=23)
DIGEST_LISTED = 10


@dataclass(slots=True)
class IncidentDesk:
    store: MapStore
    recipients: Callable[[str], Sequence[str]] = lambda institution_id: ()
    notify: Notify | None = None
    # The site owner's security contacts (email addresses, from the profile) and how to email them.
    contacts: Callable[[str], Sequence[str]] = lambda institution_id: ()
    email: Email | None = None
    # sweep group -> its IT office's addresses (SAFFRON_INTELLIGENCE_AUTHORITY_CONTACTS)
    authority_contacts: Mapping[str, Sequence[str]] = field(default_factory=dict)

    def record(self, institution_id: str, incidents: Sequence[Mapping[str, Any]], *, run_id: str | None = None) -> dict[str, int]:
        """Store what a tick or a decision found and alert at once on anything new and high.

        Each incident is alerted once however many sources reported it in this
        pass (RDAP on a domain and its subdomain, DNS and the harvest on one
        parked host), and marked notified as soon as its own alert went
        through, so a failure on one never re-sends another. One with nowhere
        to go (no security contact, no alert recipient) is counted as
        ``unrouted`` and stays un-notified: the digest keeps showing it, and
        the next sighting alerts it once somebody is configured.
        """

        counts = {"new": 0, "repeat": 0, "reopened": 0, "escalated": 0, "notified": 0, "unrouted": 0, "failed": 0}
        urgent: dict[str, dict[str, Any]] = {}
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
                urgent[row["incident_id"]] = row
        for incident_id, row in urgent.items():
            outcome = self._alert(institution_id, row)
            counts[outcome] += 1
            if outcome == "notified":
                self.store.mark_incidents_notified(institution_id, [incident_id])
        return counts

    def authority(self, institution_id: str, target: str) -> list[str]:
        """The authorities other than the institution over the site or account an incident is about, nearest first."""

        key = target.strip() if ":" in target else f"web:{target.strip().lower()}"
        asset = self.store.find_asset(institution_id, key)
        if asset is None and ":" not in target:
            # A registry reports a mapped subdomain's registrable name (adichunchanagiri.org for acm.adichunchanagiri.org).
            asset = next((domain for domain in self.store.iter_assets(institution_id, kind="domain") if domain["asset_key"].endswith("." + key.removeprefix("web:"))), None)
        return authorities(partial(self.store.get_entity, institution_id), asset["entity_id"]) if asset and asset.get("entity_id") else []

    def _alert(self, institution_id: str, row: Mapping[str, Any]) -> str:
        """Deliver one urgent incident at once: 'notified', 'unrouted' (nowhere to send it) or 'failed'.

        The institution's own sites and accounts go to its security contacts
        by email (with the guidance and the CERT-In note) and to its alert
        recipients in-app. Another authority's (the Math's domain, the
        Swamiji's account) go to that authority's IT office when one is
        configured, and otherwise only to the alert recipients, marked for that
        office: never to the institution's own site owner, who cannot fix the
        Math's site.
        """

        chain = self.authority(institution_id, str(row["target"]))
        title = f"Urgent: {row['title']}"
        body = "\n".join(part for part in (row["title"], "; ".join(row["signals"][:3]), row["guidance"]) if part)
        letter = body if CERT_IN_NOTE in body else f"{body}\n\n{CERT_IN_NOTE}"
        managers = list(self.recipients(institution_id))
        attempted = delivered = False
        if chain:
            office = next((found for found in (members(self.authority_contacts, authority) for authority in chain) if found), ())
            if office and self.email is not None:
                attempted, delivered = True, self._email(institution_id, office, title, letter)
            if not delivered and managers and self.notify is not None:
                attempted, delivered = True, self._notify(institution_id, managers, f"Urgent, for the {chain[0]} IT office: {row['title']}", body, str(row["incident_id"]))
        else:
            contacts = list(self.contacts(institution_id))
            if contacts and self.email is not None:
                attempted, delivered = True, self._email(institution_id, contacts, title, letter)
            if managers and self.notify is not None:
                attempted, delivered = True, self._notify(institution_id, managers, title, body, str(row["incident_id"])) or delivered
        return "notified" if delivered else ("failed" if attempted else "unrouted")

    def _email(self, institution_id: str, addresses: Sequence[str], subject: str, body: str) -> bool:
        try:
            result = self.email(institution_id, list(addresses), subject[:200], body)  # type: ignore[misc]
        except Exception:  # noqa: BLE001 - one failed delivery must not stop the others; the incident stays un-notified
            logger.warning("security alert email failed for %s", institution_id, exc_info=True)
            return False
        return result.get("status") in _DELIVERED if isinstance(result, Mapping) else bool(result)

    def _notify(self, institution_id: str, recipients: Sequence[str], title: str, body: str, incident_id: str) -> bool:
        try:
            result = self.notify(institution_id, list(recipients), title[:200], body, incident_id)  # type: ignore[misc]
        except Exception:  # noqa: BLE001 - as above
            logger.warning("security alert notification failed for %s", institution_id, exc_info=True)
            return False
        return result is None or bool(result)

    def last_digest(self, institution_id: str) -> str | None:
        """When the last digest that went out (or had nothing to say) started; the next one covers everything since."""

        runs = self.store.list_map_runs(institution_id, kind="digest", status="succeeded", limit=1)
        return str(runs[0]["started_at"]) if runs else None

    def digest(self, institution_id: str, *, now: datetime | None = None, days: int = 1, since: str | None = None) -> dict[str, Any]:
        """What changed since the last digest went out: open incidents, what waits for review, what was found.

        The window starts where the last successful digest started (``days``
        back when none has), so a digest that failed or was never sent is
        covered by the next one rather than lost. An open incident nobody has
        been told about stays in every digest until one reaches someone.
        """

        current = now or datetime.now(timezone.utc)
        since = since or self.last_digest(institution_id) or (current - timedelta(days=max(1, days))).isoformat()
        incidents = [row for row in self.store.list_incidents(institution_id, limit=500) if row["status"] != "resolved" and (str(row["last_seen_at"]) >= since or not row["notified_at"])]
        waiting = self.store.review_counts(institution_id)
        runs = self.store.list_map_runs(institution_id, kind="tick", since=since, limit=10_000)
        # A held run's findings are not published, so they are not reported as found either.
        passed = [run for run in runs if run.get("status") == "succeeded" and run.get("gate") != "held"]
        held = [run for run in runs if run.get("gate") == "held"]
        found = sum(int((run.get("counts") or {}).get("new_assets", 0)) for run in passed)
        raised = sum(int((run.get("counts") or {}).get("raised", 0)) for run in passed)
        lines = [f"Internet map since {since[:16].replace('T', ' ')} UTC: {found} new account(s) or site(s) found, {raised} grade(s) raised, {len(runs)} scheduled pass(es)."]
        if held:
            lines.append(f"{len(held)} pass(es) held for approval; their results are not published until a manager publishes them.")
        for row in incidents[:DIGEST_LISTED]:
            lines.append(f"[{row['severity']}] {row['title']} (seen {row['times_seen']} time(s))")
        if len(incidents) > DIGEST_LISTED:
            lines.append(f"... and {len(incidents) - DIGEST_LISTED} more open incident(s) on the incidents list.")
        if waiting:
            lines.append("Waiting for review: " + ", ".join(f"{count} {kind.replace('_', ' ')}" for kind, count in sorted(waiting.items())))
        # News items that name an institution and raise no court or controversy words are only counted here.
        mentions = sum(int((run.get("counts") or {}).get("mentions", 0)) for run in runs)
        if mentions:
            lines.append(f"News: {mentions} item(s) mentioning a mapped institution.")
        return {"institution_id": institution_id, "since": since, "incidents": incidents, "review_waiting": waiting, "found": found, "raised": raised, "passes": len(runs), "held": len(held), "mentions": mentions, "summary": "\n".join(lines)}

    def send_digest(self, institution_id: str, *, now: datetime | None = None, only_if_due: bool = False) -> dict[str, Any]:
        """Send the digest if there is anything to say; medium incidents are notified this way.

        Each digest that went out, or found nothing to say, is recorded as a
        ``digest`` run and the next one starts from it. ``only_if_due`` (the
        scheduled pass, run hourly) sends only when the last one is 23 hours
        old or more, so a restart never sends twice and a failure is retried
        within the hour. Only the incidents the message names are marked
        notified; the rest wait for the next digest.
        """

        current = now or datetime.now(timezone.utc)
        last = self.last_digest(institution_id)
        if only_if_due and last and current - datetime.fromisoformat(last) < DIGEST_EVERY:
            return {"institution_id": institution_id, "since": last, "incidents": [], "sent": False, "due": False, "recipients": 0}
        run_id = self.store.start_map_run(institution_id, kind="digest")
        try:
            digest = self.digest(institution_id, now=current, since=last)
            worth_sending = digest["incidents"] or digest["review_waiting"] or digest["found"] or digest["raised"]
            recipients = list(self.recipients(institution_id))
            sent = False
            if worth_sending and recipients and self.notify is not None:
                result = self.notify(institution_id, recipients, "Internet map: daily digest", digest["summary"], "")
                sent = result is None or bool(result)
            if sent:
                self.store.mark_incidents_notified(institution_id, [row["incident_id"] for row in digest["incidents"][:DIGEST_LISTED] if not row["notified_at"]])
            # A window that reached nobody stays open, so the next digest reports it.
            self.store.finish_map_run(
                institution_id, run_id, status="succeeded" if sent or not worth_sending else "undelivered", counts={"sent": sent, "incidents": len(digest["incidents"]), "found": digest["found"]},
            )
        except Exception as exc:
            self.store.finish_map_run(institution_id, run_id, status="failed", error=str(exc)[:300])
            raise
        return {**digest, "sent": sent, "recipients": len(recipients) if sent else 0}


__all__ = ["CERT_IN_NOTE", "DIGEST_EVERY", "DIGEST_LISTED", "GUIDANCE", "IncidentDesk", "SEVERITY", "guidance_for"]

import asyncio
import importlib.util
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from app.actions.email import EmailService, OutboxEmailSender
from app.api.platform_runtime import build_platform
from app.config.settings import AppSettings
from app.domain.principals import PrincipalType
from app.institution_data.store import InstitutionDataStore
from app.internet_intelligence.map.assets import asset_ref
from app.internet_intelligence.map.connectors.base import ConnectorRegistry
from app.internet_intelligence.map.connectors.web import OfficialSiteConnector
from app.internet_intelligence.map.engine import MapEngine
from app.internet_intelligence.map.incidents import CERT_IN_NOTE, DIGEST_LISTED, IncidentDesk, guidance_for
from app.internet_intelligence.map.pipeline import sync_profile
from app.internet_intelligence.map.service import MapService
from app.internet_intelligence.map.store import MapStore
from app.internet_intelligence.profile import InstitutionProfile
from app.observability.tracing import TraceRecorder
from app.persistence.database import InMemoryControlStore
from app.policy.pdp import LocalPolicyDecisionPoint
from app.storage.object_store import InMemoryObjectStore
from app.workers.handlers import register_handlers
from platform_fixtures import principal
from test_intelligence_map_engine import Site

FASTAPI_AVAILABLE = importlib.util.find_spec("fastapi") is not None
if FASTAPI_AVAILABLE:
    from fastapi.testclient import TestClient

    from app.main import app

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
HACKED = """<html><head><title>BGS English School</title></head><body><p>Welcome to BGS English School, Agalagurki.</p>
<div style="position:absolute; left:-9454px; width:1px; height:1px; overflow:hidden;"><a href="https://www.replicaswiss.me/">replique montre</a>
<a href="https://www.rolex-replica.me/">orologi rolex falsi</a></div></body></html>"""


class Recorder:
    def __init__(self):
        self.sent = []

    def __call__(self, institution_id, recipients, title, body, incident_id):
        self.sent.append({"institution": institution_id, "recipients": list(recipients), "title": title, "body": body, "incident": incident_id})


class DeskTests(unittest.TestCase):
    def setUp(self):
        self.store = MapStore(":memory:", suppression_key=b"test-key")
        self.sent = Recorder()
        self.desk = IncidentDesk(self.store, recipients=lambda institution_id: ("principal-1",), notify=self.sent)

    def test_high_incidents_alert_once_and_come_back_when_they_recur(self):
        first = self.desk.record("bgses", [{"kind": "site_compromised", "target": "bgses.org", "signals": ["hidden_spam_links:2"], "examples": ["https://www.replicaswiss.me/"]}])
        self.assertEqual((first["new"], first["notified"]), (1, 1))
        self.assertTrue(self.sent.sent[0]["title"].startswith("Urgent: Official site compromised: bgses.org"))
        again = self.desk.record("bgses", [{"kind": "site_compromised", "target": "BGSES.org", "signals": ["hidden_spam_links:3"]}])
        self.assertEqual((again["repeat"], again["notified"]), (1, 0), "seen again: counted, not re-sent")
        [incident] = self.store.list_incidents("bgses")
        self.assertEqual((incident["times_seen"], incident["signals"]), (2, ["hidden_spam_links:3"]))
        self.store.set_incident_status("bgses", incident["incident_id"], status="resolved", by="principal-1", note="cleaned and passwords rotated")
        back = self.desk.record("bgses", [{"kind": "site_compromised", "target": "bgses.org"}])
        self.assertEqual((back["reopened"], back["notified"]), (1, 1), "a resolved incident that recurs is reopened and alerted")
        self.assertEqual(len(self.sent.sent), 2)

    def test_medium_incidents_wait_for_the_digest(self):
        soon = self.desk.record("bgscet", [{"kind": "domain_expiring", "target": "bgscet.ac.in", "signals": ["registration expires 2026-10-10"]}])
        self.assertEqual((soon["new"], soon["notified"]), (1, 0))
        self.store.add_review_item("bgscet", kind="court_record", title="State v. BGSCET", url="https://indiankanoon.org/doc/1/")
        digest = self.desk.send_digest("bgscet")
        self.assertTrue(digest["sent"])
        self.assertIn("[medium] Domain registration expiring: bgscet.ac.in", self.sent.sent[-1]["body"])
        self.assertIn("Waiting for review: 1 court record", self.sent.sent[-1]["body"])
        self.assertIsNotNone(self.store.list_incidents("bgscet")[0]["notified_at"])
        quiet = IncidentDesk(MapStore(":memory:", suppression_key=b"k"), recipients=lambda institution_id: ("p",), notify=self.sent).send_digest("empty")
        self.assertFalse(quiet["sent"], "nothing to say, nothing sent")

    def test_guidance_and_the_cert_in_note(self):
        self.assertIn(CERT_IN_NOTE, guidance_for("site_compromised", "bgses.org.in"))
        self.assertIn(CERT_IN_NOTE, guidance_for("site_hijacked", "sacinstitutions.in"))
        self.assertNotIn("CERT-In", guidance_for("site_compromised", "bgses.org"), "the note is for Indian domains")
        self.assertNotIn("CERT-In", guidance_for("domain_expiring", "bgscet.ac.in"))
        self.assertIn("renew", guidance_for("domain_lapsing", "x.in").lower())
        with self.assertRaises(ValueError):
            self.store.upsert_incident("x", kind="k", target="t", severity="apocalyptic", title="t")


class EngineIncidentTests(unittest.TestCase):
    def test_a_hacked_site_found_by_a_tick_alerts_the_recipients(self):
        store = MapStore(":memory:", suppression_key=b"test-key")
        profile = InstitutionProfile("bgses", "BGS English School", "Agalagurki", official_domains=["bgses.in"])
        sent = Recorder()
        desk = IncidentDesk(store, recipients=lambda institution_id: ("principal-1",), notify=sent)
        engine = MapEngine(store, ConnectorRegistry([OfficialSiteConnector()]), fetcher=Site({"https://bgses.in/": (200, HACKED)}).fetcher(), profile_loader=lambda institution_id: profile, clock=lambda: NOW, incidents=desk)
        result = asyncio.run(engine.tick("bgses"))
        self.assertEqual(result["counts"]["incidents_new"], 1)
        [incident] = store.list_incidents("bgses")
        self.assertEqual((incident["kind"], incident["severity"], incident["connector"]), ("site_compromised", "high", "official_site"))
        self.assertIn(CERT_IN_NOTE, incident["guidance"])
        self.assertEqual(sent.sent[0]["incident"], incident["incident_id"])

    def test_the_digest_job(self):
        store = MapStore(":memory:", suppression_key=b"test-key")
        sent = Recorder()
        desk = IncidentDesk(store, recipients=lambda institution_id: ("p",), notify=sent)
        desk.record("bgscet", [{"kind": "domain_not_resolving", "target": "old.bgscet.ac.in"}])

        class Queue:
            handlers = {}

            def register(self, name, handler):
                self.handlers[name] = handler

        queue = Queue()
        register_handlers(queue, map_desk=desk)
        self.assertEqual(asyncio.run(queue.handlers["intelligence.map_digest"]({"institution_id": "bgscet"}))["sent"], True)


class ServiceIncidentTests(unittest.TestCase):
    def test_managers_handle_incidents_and_impersonators_become_incidents(self):
        store = MapStore(":memory:", suppression_key=b"test-key")
        sent = Recorder()
        service = MapService(store, desk=IncidentDesk(store, recipients=lambda institution_id: ("p",), notify=sent))
        manager = principal(PrincipalType.PRINCIPAL, college_id="bgscet")
        reader = principal(PrincipalType.FACULTY, college_id="bgscet")
        entity = sync_profile(store, InstitutionProfile("bgscet", "BGS College of Engineering and Technology", "Bengaluru"))["entity_id"]
        fake, _ = store.upsert_asset("bgscet", asset_ref("https://www.instagram.com/bgscet.official/"), entity_id=entity)
        review_id, _ = store.add_review_item("bgscet", kind="impersonation_candidate", title="calls itself official", asset_id=fake)
        service.decide(manager, "bgscet", review_id, decision="impersonation", note="copies our logo")
        [incident] = service.incidents(manager, "bgscet")
        self.assertEqual((incident["kind"], incident["target"]), ("impersonation_confirmed", "instagram:bgscet.official"))
        self.assertEqual(len(sent.sent), 1)
        with self.assertRaises(PermissionError):
            service.incidents(reader, "bgscet")
        acknowledged = service.set_incident(manager, "bgscet", incident["incident_id"], status="acknowledged", note="reported to Instagram")
        self.assertEqual((acknowledged["status"], acknowledged["acknowledged_by"]), ("acknowledged", manager.principal_id))
        service.set_incident(manager, "bgscet", incident["incident_id"], status="resolved")
        with self.assertRaisesRegex(ValueError, "already resolved"):
            service.set_incident(manager, "bgscet", incident["incident_id"], status="acknowledged")
        with self.assertRaises(KeyError):
            service.set_incident(manager, "bgscet", "iinc-missing", status="resolved")
        preview = service.digest(manager, "bgscet")
        self.assertFalse(preview["sent"])


@unittest.skipUnless(FASTAPI_AVAILABLE, "FastAPI is not installed")
class IncidentRouteTests(unittest.TestCase):
    def test_incident_routes(self):
        client = TestClient(app)
        headers = {"Authorization": "Bearer dev-token", "X-Demo-Principal": "inc-principal", "X-Demo-Role": "principal", "X-Demo-College": "inc_college"}
        faculty = {**headers, "X-Demo-Principal": "inc-faculty", "X-Demo-Role": "faculty"}
        platform = app.state.runtime.platform
        store = MapStore(backend=platform.store.backend, suppression_key=b"test-key")
        row, _ = store.upsert_incident("inc_college", kind="site_parked", target="old-college.example", severity="high", title="Official domain parked or for sale: old-college.example")
        with mock.patch.object(platform, "intelligence_map", MapService(store, desk=IncidentDesk(store))):
            self.assertEqual(client.get("/v1/intelligence/map/incidents", headers=faculty).status_code, 403)
            listed = client.get("/v1/intelligence/map/incidents", headers=headers)
            self.assertEqual([item["incident_id"] for item in listed.json()["incidents"]], [row["incident_id"]])
            self.assertEqual(client.post(f"/v1/intelligence/map/incidents/{row['incident_id']}/acknowledge", headers=headers, json={"note": "renewing"}).status_code, 200)
            self.assertEqual(client.post(f"/v1/intelligence/map/incidents/{row['incident_id']}/delete", headers=headers, json={}).status_code, 422)
            self.assertEqual(client.post("/v1/intelligence/map/incidents/iinc-nope/resolve", headers=headers, json={}).status_code, 404)
            self.assertIn("Official domain parked", client.get("/v1/intelligence/map/digest", headers=headers).json()["summary"])
            self.assertEqual(client.post("/v1/intelligence/map/digest", headers=headers, json={}).status_code, 200)
            events = {event.event_type for event in app.state.runtime.store.recent_audit(limit=50)}
            self.assertTrue({"intelligence.map.incident_acknowledge", "intelligence.map.digest_sent"} <= events)

    def test_the_profile_keeps_security_contacts(self):
        client = TestClient(app)
        headers = {"Authorization": "Bearer dev-token", "X-Demo-Principal": "sec-principal", "X-Demo-Role": "principal", "X-Demo-College": "sec_college"}
        body = {"name": "Security College", "official_domains": ["sec.edu.in"], "security_contacts": ["Webmaster@Sec.edu.in"]}
        saved = client.put("/v1/intelligence/profile", headers=headers, json=body)
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(client.get("/v1/intelligence/profile", headers=headers).json()["profile"]["security_contacts"], ["webmaster@sec.edu.in"])
        self.assertEqual(client.put("/v1/intelligence/profile", headers=headers, json={**body, "security_contacts": ["the webmaster"]}).status_code, 422)


MUTT = "Mutt & Swamiji"


class SecurityContactTests(unittest.TestCase):
    """Security problems go to the site owner at once, by email, and another authority's go to that authority."""

    def setUp(self):
        self.store = MapStore(":memory:", suppression_key=b"test-key")
        self.data = InstitutionDataStore(":memory:")
        self.data.upsert_institution("bgscet", "BGS College of Engineering and Technology", settings={"email_domains": ["bgscet.ac.in", "adichunchanagiri.org"]})
        self.sender = OutboxEmailSender()
        self.email = EmailService(self.data, InMemoryObjectStore(), self.sender)
        self.sent = Recorder()

    def desk(self, **overrides):
        options = {"recipients": lambda institution_id: ("principal-1",), "notify": self.sent, "contacts": lambda institution_id: ("webmaster@bgscet.ac.in",), "email": self.email.system_send}
        return IncidentDesk(self.store, **{**options, **overrides})

    def test_a_compromised_site_is_emailed_to_the_site_owner_at_once(self):
        counts = self.desk(recipients=lambda institution_id: (), notify=None).record("bgscet", [{"kind": "site_compromised", "target": "bgscet.ac.in", "signals": ["hidden_spam_links:2"]}])
        self.assertEqual((counts["notified"], counts["unrouted"]), (1, 0))
        [message] = self.sender.sent
        self.assertEqual(message.to, ("webmaster@bgscet.ac.in",))
        self.assertTrue(message.subject.startswith("Urgent: Official site compromised: bgscet.ac.in"))
        self.assertIn("rotate the admin", message.body, "with the guidance")
        self.assertEqual(message.body.count(CERT_IN_NOTE), 1)
        self.assertIsNotNone(self.store.list_incidents("bgscet")[0]["notified_at"])
        self.assertEqual(self.data.list_emails("bgscet")[0]["created_by"], "system", "recorded in the email outbox")
        self.desk().record("bgscet", [{"kind": "impersonation_confirmed", "target": "instagram:bgscet.official", "severity": "high"}])
        self.assertIn(CERT_IN_NOTE, self.sender.sent[-1].body)
        self.assertEqual(len(self.sent.sent), 1, "and the alert recipients are told in-app")

    def test_an_address_outside_the_allowed_domains_is_never_mailed(self):
        counts = self.desk(recipients=lambda institution_id: (), contacts=lambda institution_id: ("someone@gmail.com",)).record("bgscet", [{"kind": "site_parked", "target": "bgscet.ac.in"}])
        self.assertEqual((counts["notified"], counts["failed"]), (0, 1))
        self.assertEqual(self.sender.sent, [])
        self.assertIsNone(self.store.list_incidents("bgscet")[0]["notified_at"], "nothing was delivered, so it is not marked notified")

    def test_with_nobody_to_tell_the_incident_is_unrouted_and_waits(self):
        counts = IncidentDesk(self.store).record("bgscet", [{"kind": "site_hijacked", "target": "bgscet.ac.in"}])
        self.assertEqual((counts["notified"], counts["unrouted"]), (0, 1))
        [incident] = self.store.list_incidents("bgscet")
        self.assertIsNone(incident["notified_at"])
        later = IncidentDesk(self.store).digest("bgscet", since="2999-01-01T00:00:00+00:00")
        self.assertEqual([row["incident_id"] for row in later["incidents"]], [incident["incident_id"]], "an incident nobody was told about stays in every digest")
        again = self.desk().record("bgscet", [{"kind": "site_hijacked", "target": "bgscet.ac.in"}])
        self.assertEqual((again["repeat"], again["notified"]), (1, 1), "once someone is configured, the next sighting alerts it")

    def test_another_authoritys_incidents_go_to_its_it_office(self):
        math = self.store.upsert_entity("bgscet", name="Sri Adichunchanagiri Mahasamsthana Math", kind="organisation", authority=MUTT)
        self.store.upsert_asset("bgscet", asset_ref("https://adichunchanagiri.org/"), entity_id=math, relation="official")
        sjbit = self.store.upsert_entity("bgscet", name="SJB Institute of Technology", authority="SJBIT")
        self.store.upsert_asset("bgscet", asset_ref("https://exam.sjbit.edu.in/"), entity_id=sjbit, relation="official")
        desk = self.desk(authority_contacts={MUTT: ["it@adichunchanagiri.org"]})
        self.assertEqual(desk.record("bgscet", [{"kind": "site_hijacked", "target": "adichunchanagiri.org"}])["notified"], 1)
        self.assertEqual([message.to for message in self.sender.sent], [("it@adichunchanagiri.org",)], "the Math's IT office, not BGSCET's webmaster")
        self.assertEqual(self.sent.sent, [])
        # No office configured (and a registry reporting a mapped subdomain's registrable name):
        # only the alert recipients, marked for that group's IT office.
        self.assertEqual(desk.record("bgscet", [{"kind": "domain_lapsing", "target": "sjbit.edu.in", "severity": "high"}])["notified"], 1)
        self.assertEqual(len(self.sender.sent), 1, "BGSCET's site owner is not mailed about SJBIT's registration")
        self.assertTrue(self.sent.sent[0]["title"].startswith("Urgent, for the SJBIT IT office: Domain registration lapsing: sjbit.edu.in"))

    def test_the_runtime_emails_the_profiles_security_contacts(self):
        settings = AppSettings(control_database_url=":memory:", object_store_backend="memory", intelligence_map_enabled=True, intelligence_entity_approvers='{"Mutt & Swamiji": ["math-it-office"]}')
        runtime = build_platform(settings, control_store=InMemoryControlStore(), pdp=LocalPolicyDecisionPoint(), tracer=TraceRecorder(), model=None, objects=InMemoryObjectStore())
        try:
            runtime.store.upsert_institution("rt_college", "RT College", settings={"email_domains": ["rt.edu.in"]})
            runtime.intelligence_store.save_profile(InstitutionProfile("rt_college", "RT College", official_domains=["rt.edu.in"], alert_recipients=["principal-1"], security_contacts=["Webmaster@RT.edu.in"]), updated_by="test")
            self.assertEqual(runtime.intelligence_map.desk.record("rt_college", [{"kind": "site_compromised", "target": "rt.edu.in"}])["notified"], 1)
            [email] = runtime.store.list_emails("rt_college")
            self.assertEqual((email["recipients"], email["created_by"]), (["webmaster@rt.edu.in"], "system"))
            self.assertEqual(len(runtime.store.list_notifications("rt_college", "principal-1")), 1)
            self.assertEqual(runtime.intelligence_map.approvers, {MUTT: ("math-it-office",)})
        finally:
            runtime.close()


class DeliveredOnceTests(unittest.TestCase):
    def setUp(self):
        self.store = MapStore(":memory:", suppression_key=b"test-key")

    def test_one_alert_per_incident_however_many_sources_report_it(self):
        sent = Recorder()
        counts = IncidentDesk(self.store, recipients=lambda institution_id: ("p",), notify=sent).record("bgscet", [
            {"kind": "domain_lapsing", "target": "bgscet.ac.in", "severity": "high"}, {"kind": "domain_lapsing", "target": "bgscet.ac.in", "severity": "high"},
            {"kind": "site_parked", "target": "old.bgscet.ac.in", "connector": "dns"}, {"kind": "site_parked", "target": "old.bgscet.ac.in", "connector": "official_site"},
        ])
        self.assertEqual((counts["new"], counts["repeat"], counts["notified"]), (2, 2, 2))
        self.assertEqual(len(sent.sent), 2)

    def test_each_incident_is_marked_as_soon_as_its_own_alert_goes_out(self):
        delivered = []

        def notify(institution_id, recipients, title, body, incident_id):
            if "parked" in title:
                raise RuntimeError("outbox insert failed")
            delivered.append(incident_id)

        desk = IncidentDesk(self.store, recipients=lambda institution_id: ("p",), notify=notify)
        found = [{"kind": "domain_lapsing", "target": "bgscet.ac.in"}, {"kind": "site_parked", "target": "old.bgscet.ac.in"}]
        with self.assertLogs("app.internet_intelligence.map.incidents", level="WARNING"):
            counts = desk.record("bgscet", found)
        self.assertEqual((counts["notified"], counts["failed"]), (1, 1))
        notified = {row["kind"]: row["notified_at"] for row in self.store.list_incidents("bgscet")}
        self.assertIsNotNone(notified["domain_lapsing"])
        self.assertIsNone(notified["site_parked"])
        with self.assertLogs("app.internet_intelligence.map.incidents", level="WARNING"):
            desk.record("bgscet", found)
        self.assertEqual(len(delivered), 1, "the one delivered is not sent again")


class DigestMemoryTests(unittest.TestCase):
    def setUp(self):
        self.store = MapStore(":memory:", suppression_key=b"test-key")
        self.bodies = []
        self.desk = IncidentDesk(self.store, recipients=lambda institution_id: ("p",), notify=lambda institution_id, recipients, title, body, incident_id: self.bodies.append(body))

    def notified(self):
        return sum(1 for row in self.store.list_incidents("bgscet") if row["notified_at"])

    def test_the_digest_names_ten_says_how_many_more_and_marks_only_those(self):
        self.desk.record("bgscet", [{"kind": "domain_not_resolving", "target": f"h{n}.bgscet.ac.in"} for n in range(12)])
        self.assertTrue(self.desk.send_digest("bgscet")["sent"])
        self.assertIn("and 2 more open incident(s)", self.bodies[0])
        self.assertEqual(self.notified(), DIGEST_LISTED)
        self.desk.send_digest("bgscet")
        self.assertEqual(self.notified(), 12, "the two left over are in the next digest")

    def test_a_scheduled_digest_goes_out_once_a_day_and_starts_where_the_last_one_did(self):
        self.desk.record("bgscet", [{"kind": "domain_expiring", "target": "bgscet.ac.in"}])
        self.assertTrue(self.desk.send_digest("bgscet", only_if_due=True)["sent"])
        [run] = self.store.list_map_runs("bgscet", kind="digest")
        self.assertEqual(run["status"], "succeeded")
        again = self.desk.send_digest("bgscet", only_if_due=True)
        self.assertEqual((again["sent"], again["due"]), (False, False), "a restart does not send it twice")
        later = self.desk.send_digest("bgscet", now=datetime.now(timezone.utc) + timedelta(hours=23), only_if_due=True)
        self.assertEqual(later["since"], run["started_at"])
        self.assertEqual(len(self.store.list_map_runs("bgscet", kind="digest")), 2)

    def test_a_failed_or_undelivered_digest_leaves_its_window_open(self):
        self.desk.record("bgscet", [{"kind": "domain_expiring", "target": "bgscet.ac.in"}])

        def broken(*args):
            raise RuntimeError("outbox insert failed")

        failing = IncidentDesk(self.store, recipients=lambda institution_id: ("p",), notify=broken)
        with self.assertRaises(RuntimeError):
            failing.send_digest("bgscet", only_if_due=True)
        self.assertEqual(self.store.list_map_runs("bgscet", kind="digest")[0]["status"], "failed")
        nobody = IncidentDesk(self.store).send_digest("bgscet", only_if_due=True)
        self.assertFalse(nobody["sent"])
        self.assertEqual(self.store.list_map_runs("bgscet", kind="digest")[0]["status"], "undelivered")
        self.assertIsNone(self.desk.last_digest("bgscet"))
        self.assertTrue(self.desk.send_digest("bgscet", only_if_due=True)["sent"], "due again at once")
        self.assertIn("Domain registration expiring", self.bodies[0])

    def test_every_pass_in_the_window_counts_however_many(self):
        for _ in range(600):
            run_id = self.store.start_map_run("bgscet", kind="tick")
            self.store.finish_map_run("bgscet", run_id, status="succeeded", counts={"new_assets": 1, "raised": 0})
        held = self.store.start_map_run("bgscet", kind="tick")
        self.store.finish_map_run("bgscet", held, status="succeeded", gate="held", counts={"new_assets": 5})
        digest = self.desk.digest("bgscet")
        self.assertEqual((digest["found"], digest["passes"], digest["held"]), (600, 601, 1))
        self.assertEqual(self.store.list_map_runs("bgscet", kind="tick", since="2999-01-01T00:00:00+00:00"), [])

    def test_one_institutions_failure_does_not_hold_back_the_others(self):
        spec = importlib.util.spec_from_file_location("run_monitor", Path(__file__).resolve().parents[1] / "scripts" / "run_monitor.py")
        run_monitor = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(run_monitor)
        sent = []

        def notify(institution_id, recipients, title, body, incident_id):
            if institution_id == "a":
                raise RuntimeError("outbox insert failed")
            sent.append(institution_id)

        desk = IncidentDesk(self.store, recipients=lambda institution_id: ("p",), notify=notify)
        for institution in ("a", "b", "c"):
            desk.record(institution, [{"kind": "domain_not_resolving", "target": f"{institution}.example"}])
        platform = types.SimpleNamespace(intelligence_map=types.SimpleNamespace(engine=object(), desk=desk), intelligence_store=types.SimpleNamespace(monitored_institutions=lambda: ["a", "b", "c"]))
        runtime = types.SimpleNamespace(platform=platform, settings=types.SimpleNamespace(intelligence_retention_days=365))
        results = asyncio.run(run_monitor._run(runtime, None, digest=True))
        self.assertEqual(sent, ["b", "c"])
        self.assertEqual(results[0], {"institution_id": "a", "error": "outbox insert failed"})
        self.assertFalse(asyncio.run(run_monitor._run(runtime, None, digest=True))[1]["sent"], "the hourly pass does not send b's twice")


if __name__ == "__main__":
    unittest.main()

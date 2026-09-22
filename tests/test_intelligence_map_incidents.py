import asyncio
import importlib.util
import unittest
from datetime import datetime, timezone
from unittest import mock

from app.domain.principals import PrincipalType
from app.internet_intelligence.map.assets import asset_ref
from app.internet_intelligence.map.connectors.base import ConnectorRegistry
from app.internet_intelligence.map.connectors.web import OfficialSiteConnector
from app.internet_intelligence.map.engine import MapEngine
from app.internet_intelligence.map.incidents import CERT_IN_NOTE, IncidentDesk, guidance_for
from app.internet_intelligence.map.pipeline import sync_profile
from app.internet_intelligence.map.service import MapService
from app.internet_intelligence.map.store import MapStore
from app.internet_intelligence.profile import InstitutionProfile
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


if __name__ == "__main__":
    unittest.main()

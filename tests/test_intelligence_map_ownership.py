import asyncio
import importlib.util
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from app.domain.principals import PrincipalType
from app.internet_intelligence.map.connectors.base import ConnectorContext
from app.internet_intelligence.map.grading import grade
from app.internet_intelligence.map.ownership import META_NAME, WELL_KNOWN_PATH, OwnerClaimsConnector, instructions, verification_token
from app.internet_intelligence.map.pipeline import regrade, sync_profile
from app.internet_intelligence.map.service import MapService
from app.internet_intelligence.map.store import MapStore
from app.internet_intelligence.profile import InstitutionProfile
from platform_fixtures import principal
from test_intelligence_map_connectors import api
from test_intelligence_map_engine import Site

FASTAPI_AVAILABLE = importlib.util.find_spec("fastapi") is not None
if FASTAPI_AVAILABLE:
    from fastapi.testclient import TestClient

    from app.main import app

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
KEY = b"test-key"
PROFILE = InstitutionProfile("bgscet", "BGS College of Engineering and Technology", "Bengaluru", official_domains=["bgscet.ac.in"])
TOKEN = verification_token(KEY, "bgscet", "bgscet.ac.in")


def home(token=None):
    meta = f'<meta name="{META_NAME}" content="{token}">' if token else ""
    return f"<html><head><title>BGSCET</title>{meta}</head><body><p>BGS College of Engineering and Technology</p></body></html>"


def well_known(token, accounts):
    return (200, json.dumps({"verification": token, "accounts": accounts}), "application/json")


class TokenTests(unittest.TestCase):
    def test_tokens_are_keyed_per_institution_and_domain(self):
        self.assertEqual(TOKEN, verification_token(KEY, "bgscet", "www.BGSCET.ac.in"))
        self.assertNotEqual(TOKEN, verification_token(KEY, "sjbit", "bgscet.ac.in"), "another tenant cannot reuse it")
        self.assertNotEqual(TOKEN, verification_token(KEY, "bgscet", "old.bgscet.ac.in"), "nor can another host")
        self.assertNotEqual(TOKEN, verification_token(b"other-key", "bgscet", "bgscet.ac.in"))
        [guide] = instructions(KEY, "bgscet", ["bgscet.ac.in"])["domains"]
        self.assertEqual(guide["token"], TOKEN)
        self.assertEqual([item["method"] for item in guide["methods"]], ["meta_tag", "dns_txt", "well_known_file"])
        self.assertIn("https://bgscet.ac.in/.well-known/guruji.json", guide["methods"][2]["how"])

    def test_a_withdrawn_owner_claim_stops_counting(self):
        def ev(polarity, at, channel="owner-file:bgscet.ac.in"):
            return {"kind": "owner_claim", "polarity": polarity, "observed_via": "owner", "channel": channel, "observed_at": at.isoformat(), "detail": "", "source_url": ""}

        account = {"kind": "account"}
        self.assertEqual(grade(account, [ev("supports", NOW)]).grade, "O")
        self.assertEqual(grade(account, [ev("supports", NOW), ev("refutes", NOW + timedelta(days=7))]).grade, "unrated")
        self.assertEqual(grade(account, [ev("supports", NOW), ev("refutes", NOW + timedelta(days=7)), ev("supports", NOW + timedelta(days=14))]).grade, "O", "listed again")
        self.assertEqual(grade(account, [ev("supports", NOW, "owner:bgscet.ac.in"), ev("refutes", NOW + timedelta(days=7))]).grade, "O", "a withdrawal on one channel leaves the others")


class OwnerConnectorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = MapStore(":memory:", suppression_key=KEY)
        self.domain_id = sync_profile(self.store, PROFILE)["domains_added"][0]
        regrade(self.store, "bgscet", [self.domain_id])

    def context(self, pages):
        return ConnectorContext(self.store, "bgscet", "run-1", NOW, fetcher=Site(pages).fetcher())

    async def test_the_meta_tag_proves_the_domain(self):
        connector = OwnerClaimsConnector(key=KEY)
        self.assertEqual([lead.target for lead in connector.plan(self.context({}))], [self.domain_id])
        result = await connector.run({"target": self.domain_id}, self.context({"https://bgscet.ac.in/": (200, home(TOKEN))}))
        self.assertEqual(result.outcome, "verified")
        self.assertEqual(self.store.get_asset("bgscet", self.domain_id)["grade"], "O")
        wrong = await connector.run({"target": self.domain_id}, self.context({"https://bgscet.ac.in/": (200, home(verification_token(KEY, "sjbit", "bgscet.ac.in")))}))
        self.assertEqual(wrong.outcome, "no_proof", "another institution's token proves nothing")
        self.assertEqual(self.store.get_asset("bgscet", self.domain_id)["grade"], "A", "the token is gone: back to what the rest of the evidence says")

    async def test_the_well_known_file_names_accounts_and_delisting_withdraws_them(self):
        connector = OwnerClaimsConnector(key=KEY)
        self.store.suppress("bgscet", "instagram:principal.personal", reason="a person")
        listed = ["https://www.instagram.com/bgscet_engg_coll/", "https://www.youtube.com/@bgscet", "https://www.instagram.com/principal.personal/", "not a url", "https://bgscet.ac.in/about"]
        pages = {"https://bgscet.ac.in/": (200, home()), f"https://bgscet.ac.in{WELL_KNOWN_PATH}": well_known(TOKEN, listed)}
        result = await connector.run({"target": self.domain_id}, self.context(pages))
        self.assertEqual(result.outcome, "verified")
        self.assertEqual(sorted(result.new_assets), ["instagram:bgscet_engg_coll", "youtube:@bgscet"])
        self.assertIsNone(self.store.find_asset("bgscet", "instagram:principal.personal"), "suppressed even when the owner lists it")
        for key in ("instagram:bgscet_engg_coll", "youtube:@bgscet", "web:bgscet.ac.in"):
            self.assertEqual(self.store.find_asset("bgscet", key)["grade"], "O", key)
        pages[f"https://bgscet.ac.in{WELL_KNOWN_PATH}"] = well_known(TOKEN, listed[:1])
        await connector.run({"target": self.domain_id}, self.context(pages))
        self.assertEqual(self.store.find_asset("bgscet", "youtube:@bgscet")["grade"], "unrated", "taken off the list: withdrawn")
        self.assertEqual(self.store.find_asset("bgscet", "instagram:bgscet_engg_coll")["grade"], "O")
        pages[f"https://bgscet.ac.in{WELL_KNOWN_PATH}"] = well_known("gj-forged", listed)
        forged = await connector.run({"target": self.domain_id}, self.context(pages))
        self.assertEqual(forged.outcome, "no_proof", "a file without the institution's token is ignored")

    async def test_a_domain_official_only_by_inference_is_never_checked(self):
        from app.internet_intelligence.map.assets import asset_ref

        # A subdomain the map inferred (say a dangling one someone took over) copies the site's token and lists its own account.
        sub_id, _ = self.store.upsert_asset("bgscet", asset_ref("https://old.bgscet.ac.in/"), entity_id=None, relation="official")
        self.store.add_evidence("bgscet", asset_id=sub_id, kind="subdomain", detail="A:bgscet.ac.in", source_asset_id=self.domain_id, channel="dns", observed_via="live")
        regrade(self.store, "bgscet", [sub_id])
        connector = OwnerClaimsConnector(key=KEY)
        self.assertEqual([lead.target for lead in connector.plan(self.context({}))], [self.domain_id], "only the domain the institution configured")
        pages = {"https://old.bgscet.ac.in/": (200, home(TOKEN)), f"https://old.bgscet.ac.in{WELL_KNOWN_PATH}": well_known(TOKEN, ["https://www.instagram.com/fake_college/"])}
        result = await connector.run({"target": sub_id}, self.context(pages))
        self.assertEqual(result.outcome, "not_nominated")
        self.assertIsNone(self.store.find_asset("bgscet", "instagram:fake_college"))
        self.assertNotEqual(self.store.get_asset("bgscet", sub_id)["grade"], "O")

    async def test_dns_txt_is_checked_only_when_dns_is_allowed(self):
        client = api({("dns.google", "/resolve"): (200, {"Status": 0, "Answer": [{"data": f'"{META_NAME}={TOKEN}"'}, {"data": '"v=spf1 -all"'}]})})
        result = await OwnerClaimsConnector(key=KEY, dns=client).run({"target": self.domain_id}, self.context({"https://bgscet.ac.in/": (200, home())}))
        self.assertEqual(result.outcome, "verified")
        self.assertIn("dns_txt", self.store.list_evidence("bgscet", asset_id=self.domain_id)[-1]["detail"])
        without = await OwnerClaimsConnector(key=KEY).run({"target": self.domain_id}, self.context({"https://bgscet.ac.in/": (200, home())}))
        self.assertEqual(without.outcome, "no_proof")


class OwnershipServiceTests(unittest.TestCase):
    def test_managers_get_the_token_and_verify(self):
        store = MapStore(":memory:", suppression_key=KEY)
        sync_profile(store, PROFILE)
        regrade(store, "bgscet")
        pages = {"https://bgscet.ac.in/": (200, home(TOKEN))}
        service = MapService(store, fetcher=Site(pages).fetcher())
        manager = principal(PrincipalType.PRINCIPAL, college_id="bgscet")
        with self.assertRaises(PermissionError):
            service.ownership(principal(PrincipalType.FACULTY, college_id="bgscet"), "bgscet")
        self.assertEqual([(item["domain"], item["token"]) for item in service.ownership(manager, "bgscet")["domains"]], [("bgscet.ac.in", TOKEN)])
        verified = asyncio.run(service.verify_ownership(manager, "bgscet"))
        self.assertEqual(verified["results"][0]["outcome"], "verified")
        self.assertEqual([item["asset_key"] for item in verified["confirmed"]], ["web:bgscet.ac.in"])
        with self.assertRaises(ValueError):
            asyncio.run(MapService(store).verify_ownership(manager, "bgscet"))


class SuppressionTests(unittest.TestCase):
    def test_a_suppressed_account_is_removed_and_stays_out(self):
        from app.internet_intelligence.map.assets import asset_ref

        store = MapStore(":memory:", suppression_key=KEY)
        service = MapService(store)
        manager = principal(PrincipalType.PRINCIPAL, college_id="bgscet")
        asset_id, _ = store.upsert_asset("bgscet", asset_ref("https://www.instagram.com/a.student/"), entity_id=None)
        store.add_evidence("bgscet", asset_id=asset_id, kind="search_snippet", channel="t", observed_via="index")
        result = service.suppress(manager, "bgscet", identifier="https://instagram.com/A.Student?igsh=x", reason="asked to be left out")
        self.assertEqual(result, {"suppressed": True, "removed": True})
        self.assertIsNone(store.get_asset("bgscet", asset_id))
        self.assertTrue(store.is_suppressed("bgscet", "instagram:a.student"))
        self.assertEqual(service.suppress(manager, "bgscet", identifier="someone@example.com", reason="opt-out")["removed"], False)
        with self.assertRaises(PermissionError):
            service.suppress(principal(PrincipalType.FACULTY, college_id="bgscet"), "bgscet", identifier="x", reason="y")


@unittest.skipUnless(FASTAPI_AVAILABLE, "FastAPI is not installed")
class OwnershipRouteTests(unittest.TestCase):
    def test_routes(self):
        client = TestClient(app)
        headers = {"Authorization": "Bearer dev-token", "X-Demo-Principal": "own-principal", "X-Demo-Role": "principal", "X-Demo-College": "own_college"}
        platform = app.state.runtime.platform
        store = MapStore(backend=platform.store.backend, suppression_key=KEY)
        with mock.patch.object(platform, "intelligence_map", MapService(store)):
            shown = client.get("/v1/intelligence/map/ownership", headers=headers)
            self.assertEqual(shown.status_code, 200)
            self.assertEqual(shown.json()["domains"], [], "no configured domain yet, so nothing to confirm")
            self.assertEqual(client.get("/v1/intelligence/map/ownership", headers={**headers, "X-Demo-Role": "faculty"}).status_code, 403)
            self.assertEqual(client.post("/v1/intelligence/map/ownership/verify", headers=headers, json={}).status_code, 422, "no fetcher configured")
            suppressed = client.post("/v1/intelligence/map/suppress", headers=headers, json={"identifier": "https://www.instagram.com/some.person/", "reason": "a person's account"})
            self.assertEqual(suppressed.status_code, 200)
            audit = [event for event in app.state.runtime.store.recent_audit(limit=50) if event.event_type == "intelligence.map.suppress"]
            self.assertTrue(audit)
            self.assertNotIn("some.person", str(audit[0].decision_metadata), "the audit trail does not undo the suppression")


if __name__ == "__main__":
    unittest.main()

import asyncio
import importlib.util
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import httpx

from app.domain.principals import PrincipalType
from app.internet_intelligence.fetch import PublicPageFetcher
from app.internet_intelligence.map.assets import asset_ref
from app.internet_intelligence.map.connectors.base import ConnectorContext
from app.internet_intelligence.map.grading import grade
from app.internet_intelligence.map.metrics import map_metrics
from app.internet_intelligence.map.ownership import META_NAME, WELL_KNOWN_PATH, OwnerClaimsConnector, instructions, verification_token
from app.internet_intelligence.map.pipeline import lose_anchor, regrade, sync_profile
from app.internet_intelligence.map.service import MAX_HARVEST_DOMAINS, MapService
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


HOME_URL = "https://bgscet.ac.in/"
FILE_URL = f"https://bgscet.ac.in{WELL_KNOWN_PATH}"
INSTAGRAM = "https://www.instagram.com/bgscet_engg_coll/"
# Hidden, off-screen links to a replica shop: the SEO spam a compromised CMS carries.
SPAM = '<div style="position:absolute;left:-9999px"><a href="https://www.replicaswiss.me/">replica rolex</a> <a href="https://casino-gacor.example/">slot gacor</a></div>'


def web(routes=None, *, status=None, robots=None, timeout=False):
    """A fake web: ``routes[url] = (status, body, headers)``, else every page answers ``status``; robots.txt may refuse, or every page time out."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=robots, request=request) if robots else httpx.Response(404, request=request)
        if timeout:
            raise httpx.ReadTimeout("no answer", request=request)
        answer, body, headers = (routes or {}).get(f"https://{request.headers.get('host', request.url.host)}{request.url.path}", (status or 404, "", {}))
        return httpx.Response(answer, headers={"content-type": "text/html", **headers}, text=body, request=request)

    return PublicPageFetcher(transport=httpx.MockTransport(handler), resolver=lambda host: ("93.184.216.34",))


def ev(kind, at, *, polarity="supports", via="owner", channel="owner:bgscet.ac.in"):
    return {"kind": kind, "polarity": polarity, "observed_via": via, "channel": channel, "observed_at": at.isoformat(), "detail": "", "source_url": ""}


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

    def test_only_a_reviewer_lifts_a_refutation(self):
        account = {"kind": "account"}
        for kind in ("reviewer_reject", "lookalike", "impersonation"):
            refuted = [ev("owner_claim", NOW), ev(kind, NOW + timedelta(days=1), polarity="refutes", via="reviewer", channel="reviewer:r")]
            later_owner = ev("owner_claim", NOW + timedelta(days=8), channel="owner-file:bgscet.ac.in")
            self.assertEqual(grade(account, [*refuted, later_owner]).grade, "D", f"{kind}: the owner's next check does not undo it")
            confirmed = ev("reviewer_confirm", NOW + timedelta(days=9), via="reviewer", channel="reviewer:r")
            self.assertEqual(grade(account, [*refuted, later_owner, confirmed]).grade, "O", f"{kind}: a reviewer's confirmation does")


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

    async def test_the_dns_txt_record_proves_the_domain(self):
        client = api({("dns.google", "/resolve"): (200, {"Status": 0, "Answer": [{"data": f'"{META_NAME}={TOKEN}"'}, {"data": '"v=spf1 -all"'}]})})
        result = await OwnerClaimsConnector(key=KEY, dns=client).run({"target": self.domain_id}, self.context({"https://bgscet.ac.in/": (200, home())}))
        self.assertEqual(result.outcome, "verified")
        self.assertIn("dns_txt", self.store.list_evidence("bgscet", asset_id=self.domain_id)[-1]["detail"])
        without = await OwnerClaimsConnector(key=KEY).run({"target": self.domain_id}, self.context({"https://bgscet.ac.in/": (200, home())}))
        self.assertEqual(without.outcome, "no_proof", "a connector without a DNS client checks only the page and the file")


class OwnerBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = MapStore(":memory:", suppression_key=KEY)
        self.domain_id = sync_profile(self.store, PROFILE)["domains_added"][0]
        regrade(self.store, "bgscet", [self.domain_id])
        self.connector = OwnerClaimsConnector(key=KEY)

    async def check(self, pages=None, *, fetcher=None, connector=None):
        return await (connector or self.connector).run({"target": self.domain_id}, ConnectorContext(self.store, "bgscet", "run-1", NOW, fetcher=fetcher or Site(pages or {}).fetcher()))

    def grade_of(self, key):
        asset = self.store.find_asset("bgscet", key)
        return asset["grade"] if asset else None

    def domain(self):
        return self.store.get_asset("bgscet", self.domain_id)

    def evidence_count(self):
        return len(self.store.list_evidence("bgscet", limit=20000))

    def service_tokens(self):
        """The token for each domain as the console shows it to the institution's managers."""

        shown = MapService(self.store).ownership(principal(PrincipalType.PRINCIPAL, college_id="bgscet"), "bgscet")
        return {item["domain"]: item["token"] for item in shown["domains"]}


class OwnerTrustTests(OwnerBase):
    """P7-1: the token is public, so only a healthy site may use it, and only a reviewer undoes a reviewer or a takeover."""

    async def test_a_compromised_site_confirms_nothing(self):
        self.store.add_evidence("bgscet", asset_id=self.domain_id, kind="integrity", polarity="refutes", detail="compromised:hidden_spam_links:2", source_url=HOME_URL, channel="site:bgscet.ac.in", observed_via="live")
        regrade(self.store, "bgscet", [self.domain_id])
        self.assertEqual(self.domain()["status"], "compromised")
        before = self.evidence_count()
        result = await self.check({HOME_URL: (200, home(TOKEN)), FILE_URL: well_known(TOKEN, ["https://www.instagram.com/attacker_fake/"])})
        self.assertEqual(result.outcome, "unhealthy")
        self.assertEqual(self.evidence_count(), before, "no evidence either way")
        self.assertIsNone(self.store.find_asset("bgscet", "instagram:attacker_fake"))
        self.assertNotEqual(self.domain()["grade"], "O")

    async def test_spam_on_the_homepage_stops_the_check(self):
        page = home(TOKEN).replace("</body>", f"{SPAM}</body>")
        before = self.evidence_count()
        result = await self.check({HOME_URL: (200, page), FILE_URL: well_known(TOKEN, ["https://www.instagram.com/attacker_fake/"])})
        self.assertEqual(result.outcome, "unhealthy", "the page has not been reported yet, but it is not clean")
        self.assertEqual(self.evidence_count(), before)
        self.assertIsNone(self.store.find_asset("bgscet", "instagram:attacker_fake"))
        self.assertEqual(self.domain()["grade"], "A")

    async def test_a_hijacked_domain_replaying_the_old_token_stays_D(self):
        await self.check({HOME_URL: (200, home(TOKEN)), FILE_URL: well_known(TOKEN, [INSTAGRAM])})
        self.assertEqual(self.grade_of("instagram:bgscet_engg_coll"), "O")
        # The domain lapses and a re-registrant serves a gambling page: the harvester records it and drops the anchor.
        self.store.add_evidence("bgscet", asset_id=self.domain_id, kind="integrity", polarity="refutes", detail="hijacked:gambling_terms:slot,togel", source_url=HOME_URL, channel="site:bgscet.ac.in", observed_via="live")
        lose_anchor(self.store, "bgscet", self.domain_id, reason="hijacked")
        regrade(self.store, "bgscet", [self.domain_id])
        self.assertEqual((self.domain()["grade"], self.domain()["status"]), ("D", "hijacked"))
        self.assertNotEqual(self.grade_of("instagram:bgscet_engg_coll"), "O", "a lost domain no longer vouches for what its file listed")
        # The new holder republishes the old (public, archived) token and lists its own account.
        replay = await self.check({HOME_URL: (200, home(TOKEN)), FILE_URL: well_known(TOKEN, [INSTAGRAM, "https://www.instagram.com/squatter_acct/"])})
        self.assertEqual(replay.outcome, "unhealthy")
        self.assertEqual((self.domain()["grade"], self.domain()["status"]), ("D", "hijacked"))
        self.assertNotEqual(self.grade_of("instagram:squatter_acct"), "O")
        self.assertNotEqual(self.grade_of("instagram:bgscet_engg_coll"), "O")
        # Even an owner proof that got through would not end the hijack; a reviewer does.
        self.store.add_evidence("bgscet", asset_id=self.domain_id, kind="owner_claim", detail="domain verified by meta_tag", channel="owner:bgscet.ac.in", observed_via="owner")
        regrade(self.store, "bgscet", [self.domain_id])
        self.assertEqual(self.domain()["grade"], "D")
        self.store.add_evidence("bgscet", asset_id=self.domain_id, kind="reviewer_confirm", detail="recovered from the registrar", channel="reviewer:r", observed_via="reviewer")
        regrade(self.store, "bgscet", [self.domain_id])
        self.assertEqual((self.domain()["grade"], self.domain()["status"]), ("O", "live"))
        # The token an archive kept was voided when the domain was lost; the owner publishes the new one the console shows.
        stale = await self.check({HOME_URL: (200, home(TOKEN)), FILE_URL: well_known(TOKEN, [INSTAGRAM])})
        self.assertEqual(stale.outcome, "no_proof", "the old token proves nothing once the domain was lost")
        fresh = verification_token(KEY, "bgscet", "bgscet.ac.in", 0, 1)
        self.assertEqual(self.service_tokens(), {"bgscet.ac.in": fresh})
        recovered = await self.check({HOME_URL: (200, home(fresh)), FILE_URL: well_known(fresh, [INSTAGRAM])})
        self.assertEqual(recovered.outcome, "verified")
        self.assertEqual(self.grade_of("instagram:bgscet_engg_coll"), "O", "back in the owner's hands, the file counts again")

    async def test_a_reviewer_rejection_survives_later_owner_checks(self):
        pages = {HOME_URL: (200, home()), FILE_URL: well_known(TOKEN, ["https://www.instagram.com/attacker_fake/"])}
        await self.check(pages)
        asset_id = self.store.find_asset("bgscet", "instagram:attacker_fake")["asset_id"]
        self.store.add_evidence("bgscet", asset_id=asset_id, kind="reviewer_reject", polarity="refutes", detail="not ours", channel="reviewer:r", observed_via="reviewer")
        regrade(self.store, "bgscet", [asset_id])
        self.assertEqual(self.grade_of("instagram:attacker_fake"), "D")
        self.assertEqual((await self.check(pages)).outcome, "verified")
        self.assertEqual(self.grade_of("instagram:attacker_fake"), "D", "the weekly check does not undo a reviewer")

    async def test_rotating_the_tokens_retires_the_old_ones(self):
        self.assertEqual((await self.check({HOME_URL: (200, home(TOKEN))})).outcome, "verified")
        self.assertEqual(self.store.rotate_owner_token("bgscet", by="manager"), 1)
        fresh = verification_token(KEY, "bgscet", "bgscet.ac.in", 1)
        self.assertNotEqual(fresh, TOKEN)
        self.assertEqual(verification_token(KEY, "bgscet", "bgscet.ac.in", 0), TOKEN, "epoch 0 keeps the tokens already published")
        stale = await self.check({HOME_URL: (200, home(TOKEN)), FILE_URL: well_known(TOKEN, [INSTAGRAM])})
        self.assertEqual(stale.outcome, "no_proof")
        self.assertEqual(self.domain()["grade"], "A", "the old token proves nothing any more")
        self.assertIsNone(self.store.find_asset("bgscet", "instagram:bgscet_engg_coll"))
        self.assertEqual((await self.check({HOME_URL: (200, home(fresh))})).outcome, "verified")
        self.assertEqual(self.domain()["grade"], "O")
        self.assertEqual(self.store.rotate_owner_token("bgscet", by="manager"), 2)
        self.assertEqual(self.store.owner_token_epoch("sjbit"), 0, "each institution has its own epoch")

    async def test_a_file_or_tag_served_from_another_host_proves_nothing(self):
        moved = web({
            HOME_URL: (301, "", {"location": "https://elsewhere.example/"}), "https://elsewhere.example/": (200, home(TOKEN), {}),
            FILE_URL: (301, "", {"location": "https://elsewhere.example/guruji.json"}),
            "https://elsewhere.example/guruji.json": (200, json.dumps({"verification": TOKEN, "accounts": ["https://www.instagram.com/fake_bgscet/"]}), {"content-type": "application/json"}),
        })
        self.assertEqual((await self.check(fetcher=moved)).outcome, "no_proof")
        self.assertIsNone(self.store.find_asset("bgscet", "instagram:fake_bgscet"))
        self.assertNotEqual(self.domain()["grade"], "O")
        www = web({HOME_URL: (301, "", {"location": "https://www.bgscet.ac.in/"}), "https://www.bgscet.ac.in/": (200, home(TOKEN), {})})
        self.assertEqual((await self.check(fetcher=www)).outcome, "verified", "www. is the same host")

    async def test_the_file_speaks_only_for_its_own_institution(self):
        entity_id = self.domain()["entity_id"]
        placed = {
            "https://x.com/swamiji_one": self.store.upsert_entity("bgscet", name="Sri Sri Nirmalanandanatha Swamiji", kind="role"),
            "https://www.instagram.com/bgs_uk/": self.store.upsert_entity("bgscet", name="British Geological Survey", kind="lookalike", authority="external"),
            "https://www.instagram.com/another_college/": self.store.upsert_entity("bgscet", name="Another College of Engineering", kind="institution"),
            "https://www.instagram.com/bgscet_placements/": self.store.upsert_entity("bgscet", name="BGSCET Placement Cell", kind="unit", parent_id=entity_id),
        }
        for url, owner in placed.items():
            self.store.upsert_asset("bgscet", asset_ref(url), entity_id=owner, relation="unknown")
        await self.check({HOME_URL: (200, home()), FILE_URL: well_known(TOKEN, [*placed, INSTAGRAM])})
        for key in ("x:swamiji_one", "instagram:bgs_uk", "instagram:another_college"):
            self.assertNotEqual(self.grade_of(key), "O", key)
            self.assertFalse([row for row in self.store.list_evidence("bgscet", asset_id=self.store.find_asset("bgscet", key)["asset_id"]) if row["kind"] == "owner_claim"], key)
        self.assertEqual(self.grade_of("instagram:bgscet_placements"), "O", "a unit under the institution is its own")
        self.assertEqual(self.grade_of("instagram:bgscet_engg_coll"), "O", "an account the map did not know yet")


class OwnerWithdrawalTests(OwnerBase):
    """P7-2: what the file listed stops being O when the file, its token or the domain goes."""

    async def verified(self):
        await self.check({HOME_URL: (200, home(TOKEN)), FILE_URL: well_known(TOKEN, [INSTAGRAM, "https://www.youtube.com/@bgscet"])})
        self.assertEqual([self.grade_of("instagram:bgscet_engg_coll"), self.grade_of("youtube:@bgscet")], ["O", "O"])

    async def test_deleting_the_file_withdraws_its_accounts(self):
        await self.verified()
        self.assertEqual((await self.check({HOME_URL: (200, home(TOKEN))})).outcome, "verified", "the meta tag still proves the domain")
        self.assertEqual([self.grade_of("instagram:bgscet_engg_coll"), self.grade_of("youtube:@bgscet")], ["unrated", "unrated"])
        self.assertEqual(self.domain()["grade"], "O")

    async def test_breaking_the_file_token_withdraws_its_accounts(self):
        await self.verified()
        await self.check({HOME_URL: (200, home(TOKEN)), FILE_URL: well_known("gj-typo", [INSTAGRAM, "https://www.youtube.com/@bgscet"])})
        self.assertEqual([self.grade_of("instagram:bgscet_engg_coll"), self.grade_of("youtube:@bgscet")], ["unrated", "unrated"])

    async def test_withdrawing_the_domain_proof_withdraws_its_accounts(self):
        await self.verified()
        self.assertEqual((await self.check({HOME_URL: (200, home())})).outcome, "no_proof")
        self.assertEqual(self.domain()["grade"], "A")
        self.assertEqual([self.grade_of("instagram:bgscet_engg_coll"), self.grade_of("youtube:@bgscet")], ["unrated", "unrated"])

    async def test_losing_the_domain_withdraws_its_accounts(self):
        await self.verified()
        withdrawn = lose_anchor(self.store, "bgscet", self.domain_id, reason="dead")
        self.assertEqual(len(withdrawn), 3, "both listed accounts, and the domain's own proof")
        self.assertNotEqual(self.domain()["grade"], "O", "whoever holds the name now is not the owner")
        self.assertEqual([self.grade_of("instagram:bgscet_engg_coll"), self.grade_of("youtube:@bgscet")], ["unrated", "unrated"])
        before = self.evidence_count()
        self.assertEqual(lose_anchor(self.store, "bgscet", self.domain_id, reason="dead"), [], "a repeated loss adds nothing")
        self.assertEqual(self.evidence_count(), before)
        fresh = verification_token(KEY, "bgscet", "bgscet.ac.in", 0, 1)
        await self.check({HOME_URL: (200, home(fresh)), FILE_URL: well_known(fresh, [INSTAGRAM])})
        self.assertEqual(self.grade_of("instagram:bgscet_engg_coll"), "O", "the domain is back, proven with its new token, and lists it again")


class OwnerOutageTests(OwnerBase):
    """P7-3: an outage, a block or robots.txt withdraws nothing; only plain answers do."""

    async def test_a_failed_check_writes_nothing(self):
        await self.check({HOME_URL: (200, home(TOKEN)), FILE_URL: well_known(TOKEN, [INSTAGRAM])})
        before = self.evidence_count()
        for name, fetcher, outcome in (
            ("503", web(status=503), "server_error"), ("403", web(status=403), "blocked"),
            ("robots", web(robots="User-agent: AgenticSaffron-InstitutionIntelligence/1.0\nDisallow: /\n"), "robots"), ("timeout", web(timeout=True), "timeout"),
        ):
            result = await self.check(fetcher=fetcher)
            self.assertTrue(result.failed, name)
            self.assertEqual(result.outcome, outcome, name)
            self.assertEqual(self.evidence_count(), before, name)
            self.assertEqual((self.domain()["grade"], self.grade_of("instagram:bgscet_engg_coll")), ("O", "O"), name)

    async def test_only_plain_answers_from_every_method_withdraw(self):
        await self.check({HOME_URL: (200, home(TOKEN)), FILE_URL: well_known(TOKEN, [INSTAGRAM])})
        partial = await self.check({HOME_URL: (200, home()), FILE_URL: (503, "")})
        self.assertEqual((partial.outcome, partial.failed), ("inconclusive", False))
        self.assertEqual((self.domain()["grade"], self.grade_of("instagram:bgscet_engg_coll")), ("O", "O"), "the file could not be read: nothing is withdrawn")
        gone = await self.check({HOME_URL: (503, "")})
        self.assertEqual((gone.outcome, gone.failed), ("inconclusive", False))
        self.assertEqual((self.domain()["grade"], self.grade_of("instagram:bgscet_engg_coll")), ("O", "unrated"), "the file is plainly gone; the homepage said nothing")
        dns_down = OwnerClaimsConnector(key=KEY, dns=api({("dns.google", "/resolve"): (500, {})}))
        self.assertEqual((await self.check({HOME_URL: (200, home())}, connector=dns_down)).outcome, "inconclusive", "DNS gave no answer")
        self.assertEqual(self.domain()["grade"], "O")
        dns_plain = OwnerClaimsConnector(key=KEY, dns=api({("dns.google", "/resolve"): (200, {"Status": 0, "Answer": [{"data": '"v=spf1 -all"'}]})}))
        self.assertEqual((await self.check({HOME_URL: (200, home())}, connector=dns_plain)).outcome, "no_proof")
        self.assertEqual(self.domain()["grade"], "A")

    async def test_the_weekly_check_sends_validators(self):
        site = Site({HOME_URL: (200, home(TOKEN)), FILE_URL: well_known(TOKEN, [INSTAGRAM])}, etags=True)
        self.assertEqual((await self.check(fetcher=site.fetcher())).outcome, "verified")
        self.assertEqual([tag for _, tag in site.seen], [None, None])
        repeat = await self.check(fetcher=site.fetcher())
        self.assertEqual(repeat.outcome, "verified", "both answered 304: what they said last time stands")
        self.assertTrue(all(tag for _, tag in site.seen[2:]) and len(site.seen) == 4)
        self.assertEqual((self.domain()["grade"], self.grade_of("instagram:bgscet_engg_coll")), ("O", "O"))
        self.store.rotate_owner_token("bgscet", by="manager")
        rotated = await self.check(fetcher=site.fetcher())
        self.assertEqual([tag for _, tag in site.seen[4:]], [None, None], "a new token is read in full")
        self.assertEqual(rotated.outcome, "no_proof")
        self.assertEqual(self.grade_of("instagram:bgscet_engg_coll"), "unrated")


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

    def test_rotation_issues_new_tokens_for_managers_only(self):
        store = MapStore(":memory:", suppression_key=KEY)
        sync_profile(store, PROFILE)
        service = MapService(store)
        manager = principal(PrincipalType.PRINCIPAL, college_id="bgscet")
        with self.assertRaises(PermissionError):
            service.rotate_ownership(principal(PrincipalType.FACULTY, college_id="bgscet"), "bgscet")
        rotated = service.rotate_ownership(manager, "bgscet")
        self.assertEqual(rotated["epoch"], 1)
        self.assertEqual([item["token"] for item in rotated["domains"]], [verification_token(KEY, "bgscet", "bgscet.ac.in", 1)])
        self.assertEqual(service.ownership(manager, "bgscet")["domains"][0]["token"], rotated["domains"][0]["token"])
        store.suppress("bgscet", "instagram:a.person", reason="a person")
        service.rotate_ownership(manager, "bgscet")
        self.assertTrue(store.suppression_key == KEY and store.is_suppressed("bgscet", "instagram:a.person"), "rotation moves the epoch, never the suppression key")

    def test_checking_at_once_reports_the_domains_it_skipped(self):
        store = MapStore(":memory:", suppression_key=KEY)
        domains = [f"bgscet{index}.ac.in" for index in range(MAX_HARVEST_DOMAINS + 2)]
        sync_profile(store, InstitutionProfile("bgscet", "BGS College of Engineering and Technology", "Bengaluru", official_domains=domains))
        service = MapService(store, fetcher=Site({}).fetcher())
        manager = principal(PrincipalType.PRINCIPAL, college_id="bgscet")
        first = asyncio.run(service.verify_ownership(manager, "bgscet"))
        self.assertEqual((len(first["results"]), first["skipped"], len(first["skipped_asset_ids"])), (MAX_HARVEST_DOMAINS, 2, 2))
        self.assertEqual(store.list_map_runs("bgscet", kind="ownership")[0]["stop_reason"], "domain_limit")
        rest = asyncio.run(service.verify_ownership(manager, "bgscet", asset_ids=first["skipped_asset_ids"]))
        self.assertEqual(sorted(item["asset_id"] for item in rest["results"]), sorted(first["skipped_asset_ids"]))
        self.assertEqual(rest["skipped"], 0)


class SuppressionTests(unittest.TestCase):
    def test_suppressing_a_hidden_holdout_item_drops_its_plaintext(self):
        store = MapStore(":memory:", suppression_key=KEY)
        ref = asset_ref("https://www.instagram.com/some.person/")
        store.add_gold("bgscet", asset_key=ref.key, platform=ref.platform, split="holdout", entity_name="X", relation="official", expected_min_grade="B", source="sweep")
        result = MapService(store).suppress(principal(PrincipalType.PRINCIPAL, college_id="bgscet"), "bgscet", identifier="https://www.instagram.com/some.person/", reason="opt-out")
        self.assertEqual(result, {"suppressed": True, "removed": False}, "the reply does not reveal the hidden holdout")
        self.assertEqual(store.list_gold("bgscet"), [])
        self.assertEqual(map_metrics(store, "bgscet")["holdout_total"], 0)
        self.assertTrue(store.is_suppressed("bgscet", ref.key))

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
            self.assertEqual(client.post("/v1/intelligence/map/ownership/verify", headers=headers, json={"asset_ids": ["x"] * 11}).status_code, 422, "at most ten, as for harvest")
            self.assertEqual(client.post("/v1/intelligence/map/ownership/rotate", headers={**headers, "X-Demo-Role": "faculty"}, json={}).status_code, 403)
            before = store.owner_token_epoch("own_college")
            rotated = client.post("/v1/intelligence/map/ownership/rotate", headers=headers, json={})
            self.assertEqual((rotated.status_code, rotated.json()["epoch"]), (200, before + 1))
            audit = [event for event in app.state.runtime.store.recent_audit(limit=50) if event.event_type == "intelligence.map.ownership_rotate"]
            self.assertIn(("epoch", before + 1), audit[0].decision_metadata)
            suppressed = client.post("/v1/intelligence/map/suppress", headers=headers, json={"identifier": "https://www.instagram.com/some.person/", "reason": "a person's account"})
            self.assertEqual(suppressed.status_code, 200)
            audit = [event for event in app.state.runtime.store.recent_audit(limit=50) if event.event_type == "intelligence.map.suppress"]
            self.assertTrue(audit)
            self.assertNotIn("some.person", str(audit[0].decision_metadata), "the audit trail does not undo the suppression")


if __name__ == "__main__":
    unittest.main()

import unittest
from datetime import datetime, timedelta, timezone

import httpx

from app.domain.principals import PrincipalType
from app.internet_intelligence.fetch import PublicPageFetcher
from app.internet_intelligence.map.assets import asset_ref
from app.internet_intelligence.map.export import export_tsv
from app.internet_intelligence.map.grading import apply_disputes, grade
from app.internet_intelligence.map.harvest import OfficialSiteHarvester
from app.internet_intelligence.map.integrity import COMPROMISED, HIJACKED, PARKED, assess
from app.internet_intelligence.map.pipeline import regrade, sync_profile
from app.internet_intelligence.map.service import MapService
from app.internet_intelligence.map.store import MapStore
from app.internet_intelligence.map.structure import FOOTER, HEAD_LINK, HEADER, HIDDEN, NAV, parse_structure
from app.internet_intelligence.profile import InstitutionProfile
from platform_fixtures import principal

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
PUBLIC_IP = "93.184.216.34"

OFFICIAL_HOME = """<html><head><title>BGS College of Engineering and Technology</title>
<link rel="canonical" href="https://bgscet.ac.in/">
<link rel="alternate" type="application/rss+xml" href="/feed/">
<link rel="me" href="https://x.com/BGSCET_ENGG_COL">
<meta name="guruji-verification" content="token-123">
<script type="application/ld+json">{"@context":"https://schema.org","@type":"CollegeOrUniversity","name":"BGSCET","sameAs":["https://www.linkedin.com/company/bgs-college-of-engineering-technology"]}</script>
</head><body>
<header><a href="/">Home</a><nav><a href="/contact-us/">Contact</a> <a href="/about/">About</a></nav></header>
<main><p>BGS College of Engineering and Technology, Mahalakshmipuram, Bengaluru.</p><a href="https://www.instagram.com/some.student/">a student's post</a>
<a href="https://sjbit.edu.in/">Sister institution</a></main>
<div class="site-footer"><a href="https://www.instagram.com/bgscet_engg_coll/">Instagram</a> <a href="https://www.facebook.com/profile.php?id=100093242520553">Facebook</a>
<a href="https://www.youtube.com/channel/UClACJA8pf4QbkdH4__7MTGA">YouTube</a></div>
</body></html>"""
CONTACT_PAGE = """<html><head><title>Contact</title></head><body><main><h1>Contact us</h1>
<p>Follow the MBA school: <a href="https://www.instagram.com/bgscet_mba">@bgscet_mba</a></p></main></body></html>"""
HACKED_HOME = """<html><head><title>BGS English School</title></head><body><p>Welcome to BGS English School, Agalagurki.</p>
<footer><a href="https://www.instagram.com/bgsrscbpur">Instagram</a></footer>
<div style="position:absolute; left:-9454px; width:1px; height:1px; overflow:hidden;"><a href="https://www.replicaswiss.me/">replique montre</a>
<a href="https://www.rolex-replica.me/">orologi rolex falsi</a></div></body></html>"""
PARKED_PAGE = '<script>window.onload=function(){window.location.href="/lander"}</script>'
GAMBLING_PAGE = "<html><head><title>BELO4D : Website Tebak Angka &amp; Prediksi Slot Gacor</title></head><body>Situs togel dan slot gacor maxwin, bandar toto 4D terpercaya.</body></html>"


def _host(request: httpx.Request) -> str:
    return request.headers.get("host", request.url.host).split(":")[0]


def fetcher_for(pages: dict[str, tuple[int, str]], seen: list[str] | None = None) -> PublicPageFetcher:
    def handler(request: httpx.Request) -> httpx.Response:
        url = f"https://{_host(request)}{request.url.path}"
        if seen is not None:
            seen.append(url)
        if request.url.path == "/robots.txt":
            return httpx.Response(404, request=request)
        status, body = pages.get(url, (404, ""))
        return httpx.Response(status, headers={"content-type": "text/html; charset=utf-8"}, text=body, request=request)

    return PublicPageFetcher(transport=httpx.MockTransport(handler), resolver=lambda host: (PUBLIC_IP,))


class StructureTests(unittest.TestCase):
    def test_positions_declarations_and_hidden_links(self):
        page = parse_structure(OFFICIAL_HOME, "https://bgscet.ac.in/")
        positions = {link.href: link.position for link in page.links}
        self.assertEqual(positions["https://www.instagram.com/bgscet_engg_coll/"], FOOTER, "class=site-footer counts as the footer")
        self.assertEqual(positions["https://bgscet.ac.in/contact-us/"], NAV)
        self.assertEqual(positions["https://bgscet.ac.in/"], HEADER)
        self.assertEqual(positions["https://x.com/BGSCET_ENGG_COL"], HEAD_LINK)
        self.assertEqual(positions["https://www.instagram.com/some.student/"], "body")
        self.assertEqual(page.same_as, ["https://www.linkedin.com/company/bgs-college-of-engineering-technology"])
        self.assertEqual(page.canonical, "https://bgscet.ac.in/")
        self.assertEqual(page.feeds, ["https://bgscet.ac.in/feed/"])
        self.assertEqual(page.meta["guruji-verification"], "token-123")
        identity = {link.href for link in page.identity_links()}
        self.assertIn("https://x.com/BGSCET_ENGG_COL", identity)
        self.assertNotIn("https://www.instagram.com/some.student/", identity, "a body link is not an identity declaration")
        hacked = parse_structure(HACKED_HOME, "https://bgses.org/")
        self.assertEqual({link.href for link in hacked.hidden_links()}, {"https://www.replicaswiss.me/", "https://www.rolex-replica.me/"})
        for style in ("display:none", "visibility: hidden", "left:-9999px", "font-size:0", "text-indent:-5000px"):
            self.assertEqual(parse_structure(f'<div style="{style}"><a href="https://x.example/">x</a></div>', "https://a.example/").links[0].position, HIDDEN, style)
        self.assertEqual(parse_structure('<div hidden><a href="https://x.example/">x</a></div>', "https://a.example/").links[0].position, HIDDEN)
        self.assertEqual(parse_structure('<a class="sr-only" href="#main">Skip</a><div role="contentinfo"><a href="https://x.example/">x</a></div>', "https://a.example/").links[0].position, FOOTER)

    def test_broken_markup_still_parses(self):
        page = parse_structure("<html><footer><div><a href='https://www.instagram.com/x'>ig</footer><p>after", "https://a.example/")
        self.assertEqual(page.links[0].position, FOOTER)


class IntegrityTests(unittest.TestCase):
    def test_the_three_failures_from_the_sweep(self):
        hacked = assess(parse_structure(HACKED_HOME, "https://bgses.org/"), HACKED_HOME)
        self.assertEqual(hacked.status, COMPROMISED)
        self.assertEqual(len(hacked.spam_links), 2)
        self.assertEqual(assess(parse_structure(PARKED_PAGE, "https://acmbgs.org/"), PARKED_PAGE).status, PARKED)
        self.assertEqual(assess(parse_structure(GAMBLING_PAGE, "https://sacinstitutions.org/"), GAMBLING_PAGE).status, HIJACKED)
        clean = assess(parse_structure(OFFICIAL_HOME, "https://bgscet.ac.in/"), OFFICIAL_HOME)
        self.assertTrue(clean.can_vouch)

    def test_ordinary_words_do_not_trip_the_checks(self):
        html = "<html><head><title>Exam time slots</title></head><body><p>The lab slot for toto the robot club is at 4 pm.</p><div style='display:none'><a href='https://cdn.example/x.js'>x</a></div></body></html>"
        self.assertEqual(assess(parse_structure(html, "https://college.example/"), html).status, "clean")


class RetrieveTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_failure_has_a_name(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(200, text="User-agent: *\nDisallow: /private\n", request=request)
            path = request.url.path
            if path == "/gone":
                return httpx.Response(410, request=request)
            if path == "/blocked":
                return httpx.Response(999, request=request)
            if path == "/forbidden":
                return httpx.Response(403, request=request)
            if path == "/down":
                return httpx.Response(503, request=request)
            if path == "/login":
                return httpx.Response(302, headers={"location": "/accounts/login/?next=/x"}, request=request)
            if path == "/data.json":
                return httpx.Response(200, headers={"content-type": "application/json"}, text='{"ok": true}', request=request)
            if path == "/cached":
                if request.headers.get("if-none-match") == '"v1"':
                    return httpx.Response(304, headers={"etag": '"v1"'}, request=request)
                return httpx.Response(200, headers={"content-type": "text/html", "etag": '"v1"'}, text="<html>v1</html>", request=request)
            return httpx.Response(404, request=request)

        fetcher = PublicPageFetcher(transport=httpx.MockTransport(handler), resolver=lambda host: (PUBLIC_IP,))
        outcomes = {path: (await fetcher.retrieve(f"https://site.example{path}")).outcome for path in ("/gone", "/blocked", "/forbidden", "/down", "/login", "/missing", "/private/x")}
        self.assertEqual(outcomes, {"/gone": "gone", "/blocked": "blocked", "/forbidden": "blocked", "/down": "server_error", "/login": "login_wall", "/missing": "not_found", "/private/x": "robots"})
        self.assertEqual((await fetcher.retrieve("https://site.example/data.json")).outcome, "content_type", "html is the default")
        data = await fetcher.retrieve("https://site.example/data.json", accept=frozenset({"json"}))
        self.assertEqual((data.outcome, data.text), ("ok", '{"ok": true}'))
        first = await fetcher.retrieve("https://site.example/cached")
        self.assertEqual((first.outcome, first.etag), ("ok", '"v1"'))
        again = await fetcher.retrieve("https://site.example/cached", etag=first.etag)
        self.assertEqual(again.outcome, "not_modified", "the validator from the last fetch still holds")
        self.assertEqual((await fetcher.retrieve("https://www.instagram.com/bgscet_engg_coll/")).outcome, "snippet_only")
        self.assertEqual((await PublicPageFetcher(transport=httpx.MockTransport(handler), resolver=lambda host: ("10.0.0.1",)).retrieve("https://intranet.example/")).outcome, "not_public")


def ev(kind, polarity="supports", via="live", detail="", channel="", source_url="https://bgscet.ac.in/", at=NOW, source_asset_id=None):
    return {"evidence_id": f"e-{kind}-{at.isoformat()}-{channel}-{polarity}", "kind": kind, "polarity": polarity, "observed_via": via, "detail": detail, "channel": channel, "source_url": source_url, "observed_at": at.isoformat(), "source_asset_id": source_asset_id}


ACCOUNT = {"asset_id": "a1", "kind": "account", "platform": "instagram", "relation": "official", "entity_id": "e1"}
DOMAIN = {"asset_id": "d1", "kind": "domain", "platform": "website", "relation": "official", "entity_id": "e1"}


class GradingTests(unittest.TestCase):
    def g(self, evidence, asset=ACCOUNT):
        return grade(asset, evidence, now=NOW).grade

    def test_the_grading_rule(self):
        self.assertEqual(self.g([]), "unrated")
        self.assertEqual(self.g([ev("imported_claim", via="import", detail="claimed A: official")]), "C", "a claim is not a verification")
        self.assertEqual(self.g([ev("configured_domain", via="reviewer")], DOMAIN), "A")
        self.assertEqual(self.g([ev("official_link", detail="A:footer")]), "A")
        self.assertEqual(self.g([ev("official_link", detail="B:footer")]), "B", "a B domain can only vouch for B")
        self.assertEqual(self.g([ev("official_link", detail="C:footer")]), "C")
        self.assertEqual(self.g([ev("official_link", via="archive", detail="A:footer")]), "A-arch")
        self.assertEqual(self.g([ev("hub_link", detail="A:linktree")]), "B", "one step down from the source")
        self.assertEqual(self.g([ev("hub_link", detail="O:linktree")]), "A")
        self.assertEqual(self.g([ev("hub_link", detail="B:linktree")]), "C")
        self.assertEqual(self.g([ev("owner_claim", via="owner", detail="well-known file")]), "O")
        self.assertEqual(self.g([ev("directory_record", detail="authority:AICTE")], DOMAIN), "A", "a regulator's listing anchors a domain")
        self.assertEqual(self.g([ev("directory_record", detail="authority:AICTE")]), "B", "but only a domain")
        self.assertEqual(self.g([ev("reviewer_confirm", via="reviewer")]), "B")

    def test_two_independent_channels_but_not_one_channel_twice(self):
        self.assertEqual(self.g([ev("search_snippet", via="index", channel="tavily"), ev("search_snippet", via="index", channel="tavily", at=NOW + timedelta(hours=1))]), "C")
        self.assertEqual(self.g([ev("search_snippet", via="index", channel="tavily"), ev("directory_record", channel="wikidata")]), "B")

    def test_refutations(self):
        self.assertEqual(self.g([ev("official_link", detail="A:footer"), ev("reviewer_reject", polarity="refutes", via="reviewer", at=NOW + timedelta(hours=1))]), "D")
        self.assertEqual(self.g([ev("reviewer_reject", polarity="refutes", via="reviewer"), ev("reviewer_confirm", via="reviewer", at=NOW + timedelta(hours=1))]), "B", "a later confirmation overrides")
        self.assertEqual(self.g([ev("configured_domain", via="reviewer"), ev("integrity", polarity="refutes", detail="parked:parked_lander", at=NOW + timedelta(hours=1))], DOMAIN), "D")
        compromised = grade(DOMAIN, [ev("configured_domain", via="reviewer"), ev("integrity", polarity="refutes", detail="compromised:hidden_spam_links:2")], now=NOW)
        self.assertEqual((compromised.grade, compromised.status), ("A", "compromised"), "a hacked official site stays official but cannot vouch")

    def test_dead_needs_two_failures_a_day_apart_and_blocked_is_not_dead(self):
        once = grade(ACCOUNT, [ev("official_link", detail="A:footer"), ev("liveness", polarity="refutes", detail="not_found:404", at=NOW + timedelta(hours=1))], now=NOW)
        self.assertEqual((once.grade, once.status), ("A", None))
        twice_fast = [ev("official_link", detail="A:footer"), ev("liveness", polarity="refutes", detail="not_found:404", at=NOW + timedelta(hours=1)), ev("liveness", polarity="refutes", detail="not_found:404", at=NOW + timedelta(hours=3))]
        self.assertEqual(self.g(twice_fast), "A")
        twice = [ev("official_link", detail="A:footer"), ev("liveness", polarity="refutes", detail="not_found:404", at=NOW + timedelta(hours=1)), ev("liveness", polarity="refutes", detail="gone:410", at=NOW + timedelta(hours=30))]
        self.assertEqual(self.g(twice), "D")
        recovered = [*twice, ev("liveness", detail="ok:200", at=NOW + timedelta(hours=40))]
        self.assertEqual(self.g(recovered), "A", "a page that came back is not dead")
        blocked = grade(ACCOUNT, [ev("official_link", detail="A:footer"), ev("liveness", polarity="refutes", detail="login_wall:302", at=NOW + timedelta(days=90))], now=NOW)
        self.assertEqual((blocked.grade, blocked.status), ("A", "blocked"))

    def test_a_link_removed_from_the_page_stops_counting(self):
        removed = [ev("imported_claim", via="import"), ev("official_link", detail="A:footer"), ev("official_link", polarity="refutes", detail="removed:https://bgscet.ac.in/", at=NOW + timedelta(days=1))]
        self.assertEqual(self.g(removed), "C")
        restored = [*removed, ev("official_link", detail="A:footer", at=NOW + timedelta(days=2))]
        self.assertEqual(self.g(restored), "A")

    def test_competing_unanchored_official_accounts_are_disputed(self):
        assets = [{"asset_id": "x1", "relation": "official", "entity_id": "swamiji", "kind": "account", "platform": "x"}, {"asset_id": "x2", "relation": "official", "entity_id": "swamiji", "kind": "account", "platform": "x"}]
        self.assertEqual(apply_disputes(assets, {"x1": "B", "x2": "B"}), {"x1": "disputed", "x2": "disputed"})
        self.assertEqual(apply_disputes(assets, {"x1": "A", "x2": "B"}), {}, "an anchored account settles it")
        self.assertEqual(apply_disputes(assets[:1], {"x1": "B"}), {})


class HarvestTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = MapStore(":memory:", suppression_key=b"test-key")
        self.profile = InstitutionProfile("bgscet", "BGS College of Engineering and Technology", "Bengaluru", aliases=["BGSCET"], official_domains=["bgscet.ac.in"])

    async def test_an_official_site_vouches_for_its_accounts(self):
        seen: list[str] = []
        pages = {"https://bgscet.ac.in/": (200, OFFICIAL_HOME), "https://bgscet.ac.in/contact-us/": (200, CONTACT_PAGE), "https://bgscet.ac.in/about/": (404, "")}
        synced = sync_profile(self.store, self.profile)
        domain_id = synced["domains_added"][0]
        self.store.suppress("bgscet", "instagram:some.student", reason="personal account")
        result = await OfficialSiteHarvester(fetcher_for(pages, seen), self.store).harvest("bgscet", domain_id, run_id="r1")
        self.assertEqual((result.integrity, result.anchor), ("clean", "A"))
        grades = {asset["asset_key"]: asset["grade"] for asset in self.store.list_assets("bgscet")}
        for key in ("instagram:bgscet_engg_coll", "facebook:id:100093242520553", "youtube:channel:UClACJA8pf4QbkdH4__7MTGA", "x:bgscet_engg_col", "linkedin:company:bgs-college-of-engineering-technology", "instagram:bgscet_mba"):
            self.assertEqual(grades.get(key), "A", key)
        self.assertNotIn("instagram:some.student", grades, "a suppressed personal account never enters the map")
        self.assertEqual(result.feeds, ["https://bgscet.ac.in/feed/"])
        self.assertIn("https://sjbit.edu.in/", result.leads, "sister sites become leads, not official accounts")
        self.assertEqual(self.store.get_asset("bgscet", domain_id)["status"], "live")
        self.assertTrue(all("bgscet.ac.in" in url for url in seen), "only the official host is fetched")
        # The footer drops Facebook: the next clean harvest withdraws that link.
        pages["https://bgscet.ac.in/"] = (200, OFFICIAL_HOME.replace('<a href="https://www.facebook.com/profile.php?id=100093242520553">Facebook</a>', ""))
        await OfficialSiteHarvester(fetcher_for(pages), self.store).harvest("bgscet", domain_id, run_id="r2")
        facebook = self.store.find_asset("bgscet", "facebook:id:100093242520553")
        self.assertNotEqual(facebook["grade"], "A")
        self.assertEqual(self.store.find_asset("bgscet", "instagram:bgscet_engg_coll")["grade"], "A")

    async def test_a_hacked_site_vouches_for_nothing_and_is_reported(self):
        profile = InstitutionProfile("bgses", "BGS English School", "Chikkaballapur", official_domains=["bgses.org"])
        domain_id = sync_profile(self.store, profile)["domains_added"][0]
        result = await OfficialSiteHarvester(fetcher_for({"https://bgses.org/": (200, HACKED_HOME)}), self.store).harvest("bgses", domain_id)
        self.assertEqual(result.integrity, "compromised")
        self.assertEqual(result.incidents[0]["kind"], "site_compromised")
        self.assertEqual(result.accounts, [], "no account is vouched for by a compromised page")
        self.assertEqual(self.store.get_asset("bgses", domain_id)["status"], "compromised")

    async def test_parked_domains_and_unvouched_domains(self):
        parked = InstitutionProfile("acm", "Sri Adichunchanagiri Mahasamsthana Math", official_domains=["acmbgs.org"])
        domain_id = sync_profile(self.store, parked)["domains_added"][0]
        await OfficialSiteHarvester(fetcher_for({"https://acmbgs.org/": (200, PARKED_PAGE)}), self.store).harvest("acm", domain_id)
        self.assertEqual(self.store.get_asset("acm", domain_id)["grade"], "D")
        # A domain known only from an imported claim is C: it cannot vouch for anything yet.
        unvouched_id, _ = self.store.upsert_asset("bgscet", asset_ref("https://sjbit.edu.in/"), entity_id=None, relation="official")
        self.store.add_evidence("bgscet", asset_id=unvouched_id, kind="imported_claim", observed_via="import")
        result = await OfficialSiteHarvester(fetcher_for({"https://sjbit.edu.in/": (200, OFFICIAL_HOME)}), self.store).harvest("bgscet", unvouched_id)
        self.assertEqual((result.anchor, result.accounts), ("C", []))

    async def test_a_domain_that_now_redirects_elsewhere_cannot_vouch(self):
        profile = InstitutionProfile("aitckm", "AIT", official_domains=["aitckm.in"])
        domain_id = sync_profile(self.store, profile)["domains_added"][0]

        def handler(request):
            if request.url.path == "/robots.txt":
                return httpx.Response(404, request=request)
            if _host(request) == "aitckm.in":
                return httpx.Response(301, headers={"location": "https://parking.example/"}, request=request)
            return httpx.Response(200, headers={"content-type": "text/html"}, text=OFFICIAL_HOME, request=request)

        result = await OfficialSiteHarvester(PublicPageFetcher(transport=httpx.MockTransport(handler), resolver=lambda host: (PUBLIC_IP,)), self.store).harvest("aitckm", domain_id)
        self.assertEqual((result.integrity, result.accounts), ("redirects_offsite", []))


class ExportAndServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_export_and_the_guarded_service(self):
        store = MapStore(":memory:", suppression_key=b"test-key")
        pages = {"https://bgscet.ac.in/": (200, OFFICIAL_HOME), "https://bgscet.ac.in/contact-us/": (200, CONTACT_PAGE)}
        service = MapService(store, fetcher=fetcher_for(pages))
        manager = principal(PrincipalType.PRINCIPAL, college_id="bgscet")
        reader = principal(PrincipalType.FACULTY, college_id="bgscet")
        profile = InstitutionProfile("bgscet", "BGS College of Engineering and Technology", "Bengaluru", official_domains=["bgscet.ac.in"])
        with self.assertRaises(ValueError):
            service.sync_profile(manager, "bgscet", None)
        service.sync_profile(manager, "bgscet", profile)
        harvested = await service.harvest(manager, "bgscet")
        self.assertEqual(harvested["domains"], 1)
        self.assertGreaterEqual(harvested["accounts"], 5)
        with self.assertRaises(PermissionError):
            await service.harvest(reader, "bgscet")
        store.upsert_asset("bgscet", asset_ref("https://www.instagram.com/unverified_page"), entity_id=None)
        regraded = service.regrade(manager, "bgscet")
        self.assertIn("changes", regraded)
        manager_tsv = service.export(manager, "bgscet")
        reader_tsv = service.export(reader, "bgscet")
        self.assertTrue(manager_tsv.startswith("group\tentity\turl\tgrade\trelation\tstatus\tlast_verified\tnote\n"))
        self.assertIn("instagram.com/unverified_page", manager_tsv)
        self.assertNotIn("instagram.com/unverified_page", reader_tsv, "readers get only what the map stands behind")
        self.assertIn("instagram.com/bgscet_engg_coll", reader_tsv)
        self.assertIn("official link (footer)", export_tsv(store, "bgscet"))
        self.assertEqual(regrade(store, "bgscet"), [], "regrading unchanged evidence changes nothing")


if __name__ == "__main__":
    unittest.main()

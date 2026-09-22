"""Static checks on the browser assets that guard against regressions the CSP
and the sign-in gate would otherwise only reveal in a browser, plus the
separation between the client assistant and the developer console."""

import importlib
import importlib.util
import os
import pathlib
import re
import unittest
from unittest.mock import patch

WEB = pathlib.Path(__file__).resolve().parents[1] / "apps" / "web"
ASSISTANT = WEB / "assistant"
CONSOLE = WEB / "console"
SHARED = WEB / "shared"

FASTAPI_AVAILABLE = importlib.util.find_spec("fastapi") is not None


class WebAssetTests(unittest.TestCase):
    def test_pages_and_scripts_carry_no_inline_style_attributes(self):
        # The app's CSP has no style-src 'unsafe-inline', so inline styles are dropped.
        paths = sorted(list(WEB.rglob("*.html")) + list(WEB.rglob("*.js")))
        self.assertTrue(paths)
        for path in paths:
            self.assertNotRegex(path.read_text(encoding="utf-8"), r'\sstyle="', f"{path.name} carries an inline style attribute")

    def test_hidden_containers_are_not_displayed(self):
        self.assertRegex((SHARED / "styles.css").read_text(encoding="utf-8"), r"\.chat-history\[hidden\][^{]*\{[^}]*display:\s*none")
        self.assertRegex((CONSOLE / "console.css").read_text(encoding="utf-8"), r"\.platform-body\[hidden\][^{]*\{[^}]*display:\s*none")

    def test_auth_client_fails_closed_when_config_is_unavailable(self):
        auth = (SHARED / "auth.js").read_text(encoding="utf-8")
        self.assertIn("let config = { mode: 'unavailable' }", auth)
        self.assertNotRegex(auth, r"config\s*=\s*\{\s*mode:\s*'demo'")

    def test_report_downloads_go_through_the_authenticated_client(self):
        js = (CONSOLE / "console.js").read_text(encoding="utf-8")
        self.assertNotRegex(js, re.compile(r'href="\$\{escapeHtml\((report|item)\.download_path\)\}'))
        self.assertIn("downloadReport(", js)

    def test_voice_socket_follows_the_page_scheme_and_host(self):
        # Behind a TLS-terminating load balancer the server sees plain HTTP and
        # advertises ws://, which the browser refuses to open from an HTTPS page.
        js = (ASSISTANT / "app.js").read_text(encoding="utf-8")
        self.assertNotIn("new WebSocket(data.websocket_url)", js)
        self.assertIn("new WebSocket(voiceSocketUrl(data.websocket_url))", js)
        self.assertIn("window.location.protocol === 'https:' ? 'wss:' : 'ws:'", js)

    def test_repeated_voice_phrase_is_not_dropped_after_recognition_restarts(self):
        # Result indexes restart at 0 on every recognition run, so the
        # duplicate-result keys must not outlive the run that produced them.
        js = (ASSISTANT / "app.js").read_text(encoding="utf-8")
        self.assertRegex(js, r"recognition\.onstart = \(\) => \{[^}]*state\.finalResultKeys\.clear\(\);")


class AppSeparationTests(unittest.TestCase):
    """The assistant is for clients and the console is for developers: neither
    page leads to the other, and each loads only its own app code."""

    def test_assistant_does_not_lead_clients_to_the_console(self):
        for path in ASSISTANT.iterdir():
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("/console", text, path.name)
            self.assertNotIn("platform.html", text, path.name)
            self.assertNotRegex(text, r"(?i)platform console", path.name)

    def test_console_does_not_link_back_to_the_assistant(self):
        page = (CONSOLE / "index.html").read_text(encoding="utf-8")
        self.assertNotRegex(page, r'href="/"')

    def test_each_page_loads_only_shared_assets_and_its_own_app(self):
        assistant = (ASSISTANT / "index.html").read_text(encoding="utf-8")
        console = (CONSOLE / "index.html").read_text(encoding="utf-8")
        self.assertEqual(re.findall(r'<script src="([^"]+)"', assistant), ["/shared/auth.js", "/app.js"])
        self.assertEqual(re.findall(r'<script src="([^"]+)"', console), ["/shared/auth.js", "/console/console.js"])
        self.assertEqual(re.findall(r'<link rel="stylesheet" href="([^"]+)"', assistant), ["/shared/styles.css"])
        self.assertEqual(re.findall(r'<link rel="stylesheet" href="([^"]+)"', console), ["/shared/styles.css", "/console/console.css"])

    def test_each_app_returns_to_itself_after_sign_in(self):
        # The identity provider sends the browser back to redirect_uri; a console
        # sign-in must not land a developer on the client assistant.
        self.assertIn('data-auth-return-path="/console/"', (CONSOLE / "index.html").read_text(encoding="utf-8"))
        self.assertNotIn("data-auth-return-path", (ASSISTANT / "index.html").read_text(encoding="utf-8"))
        auth = (SHARED / "auth.js").read_text(encoding="utf-8")
        self.assertIn("dataset.authReturnPath || '/'", auth)
        self.assertNotIn("global.location.origin + '/';", auth)


def _client(**env: str):
    # The app reads settings and mounts the pages at import time, so each case
    # needs a fresh module.
    from fastapi.testclient import TestClient

    values = {"GURU_ENVIRONMENT": "development", "CONTROL_DATABASE_URL": ":memory:"}
    values.update(env)
    with patch.dict("os.environ", values, clear=False):
        if "GURU_WEB_CONSOLE_ENABLED" not in env:
            os.environ.pop("GURU_WEB_CONSOLE_ENABLED", None)
        import app.main

        module = importlib.reload(app.main)
        return TestClient(module.app)


@unittest.skipUnless(FASTAPI_AVAILABLE, "FastAPI dependencies are not installed")
class AppServingTests(unittest.TestCase):
    def test_assistant_is_served_at_the_root(self):
        client = _client()
        page = client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("<title>Guru Ji — Assistant</title>", page.text)
        for path in ("/app.js", "/shared/auth.js", "/shared/styles.css"):
            self.assertEqual(client.get(path).status_code, 200, path)

    def test_console_is_served_on_its_own_path(self):
        client = _client()
        page = client.get("/console/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("<title>Guru Ji — Platform console</title>", page.text)
        for path in ("/console/console.js", "/console/console.css"):
            self.assertEqual(client.get(path).status_code, 200, path)
        for path in ("/console", "/platform.html"):
            response = client.get(path, follow_redirects=False)
            self.assertEqual(response.status_code, 307, path)
            self.assertEqual(response.headers["location"], "/console/", path)
        # The console's script is not reachable through the assistant's mount.
        self.assertEqual(client.get("/console.js").status_code, 404)

    def test_disabled_console_is_not_served_but_the_assistant_is(self):
        client = _client(GURU_WEB_CONSOLE_ENABLED="false")
        for path in ("/console", "/console/", "/console/console.js", "/console/console.css", "/platform.html"):
            self.assertEqual(client.get(path, follow_redirects=False).status_code, 404, path)
        self.assertEqual(client.get("/").status_code, 200)
        self.assertEqual(client.get("/shared/auth.js").status_code, 200)


if __name__ == "__main__":
    unittest.main()

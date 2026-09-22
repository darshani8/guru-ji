"""Static checks on the browser assets that guard against regressions the CSP
and the sign-in gate would otherwise only reveal in a browser."""

import pathlib
import re
import unittest

WEB = pathlib.Path(__file__).resolve().parents[1] / "apps" / "web"


class WebAssetTests(unittest.TestCase):
    def test_pages_and_scripts_carry_no_inline_style_attributes(self):
        # The app's CSP has no style-src 'unsafe-inline', so inline styles are dropped.
        for path in sorted(list(WEB.glob("*.html")) + list(WEB.glob("*.js"))):
            self.assertNotRegex(path.read_text(encoding="utf-8"), r'\sstyle="', f"{path.name} carries an inline style attribute")

    def test_hidden_containers_are_not_displayed(self):
        css = (WEB / "styles.css").read_text(encoding="utf-8")
        self.assertRegex(css, r"\.platform-body\[hidden\][^{]*\{[^}]*display:\s*none")
        self.assertRegex(css, r"\.chat-history\[hidden\][^{]*\{[^}]*display:\s*none")

    def test_auth_client_fails_closed_when_config_is_unavailable(self):
        auth = (WEB / "auth.js").read_text(encoding="utf-8")
        self.assertIn("let config = { mode: 'unavailable' }", auth)
        self.assertNotRegex(auth, r"config\s*=\s*\{\s*mode:\s*'demo'")

    def test_report_downloads_go_through_the_authenticated_client(self):
        js = (WEB / "platform.js").read_text(encoding="utf-8")
        self.assertNotRegex(js, re.compile(r'href="\$\{escapeHtml\((report|item)\.download_path\)\}'))
        self.assertIn("downloadReport(", js)


if __name__ == "__main__":
    unittest.main()

import unittest
from unittest.mock import patch

OIDC_ENV = {
    "SAFFRON_OIDC_ISSUER_URL": "https://issuer.example.test/pool",
    "SAFFRON_OIDC_AUDIENCE": "browser-client-id",
    "SAFFRON_OIDC_JWKS_URL": "https://issuer.example.test/pool/.well-known/jwks.json",
    "CONTROL_DATABASE_URL": ":memory:",
}


def _client(environment: str, **extra: str):
    # The app reads settings at import time, so each case needs a fresh module.
    import importlib

    from fastapi.testclient import TestClient

    env = {"SAFFRON_ENVIRONMENT": environment, "CONTROL_DATABASE_URL": ":memory:"}
    env.update(extra)
    with patch.dict("os.environ", env, clear=False):
        import app.main

        module = importlib.reload(app.main)
        return TestClient(module.app)


class BrowserAuthConfigTests(unittest.TestCase):
    """The browser must be told the same scheme the server will actually accept."""

    def test_development_reports_demo_mode(self):
        response = _client("development").get("/v1/auth/config")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"mode": "demo"})

    def test_configured_oidc_publishes_only_public_values(self):
        response = _client("staging", **OIDC_ENV).get("/v1/auth/config")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["mode"], "oidc")
        self.assertEqual(body["issuer"], OIDC_ENV["SAFFRON_OIDC_ISSUER_URL"])
        self.assertEqual(body["client_id"], OIDC_ENV["SAFFRON_OIDC_AUDIENCE"])
        self.assertIn("openid", body["scopes"])
        # Nothing secret may reach the browser.
        self.assertNotIn("dev_bearer_token", body)
        for value in body.values():
            self.assertNotIn("dev-token", str(value))

    def test_mode_matches_what_the_server_accepts(self):
        # principal_from_headers only honours demo headers in these two
        # environments; the advertised mode has to agree with that.
        for environment in ("development", "test"):
            response = _client(environment).get("/v1/auth/config")
            self.assertEqual(response.json()["mode"], "demo", environment)


class ForgedDemoHeaderTests(unittest.TestCase):
    """Demo headers let the caller name its own role, so they must not be
    honoured once a real identity provider is configured."""

    def test_demo_headers_are_rejected_outside_development(self):
        client = _client("staging", **OIDC_ENV)
        response = client.post(
            "/v1/chat",
            json={"prompt": "hello", "institution_scope": {"college_id": "college_a"}},
            headers={
                "Authorization": "Bearer dev-token",
                "X-Demo-Principal": "attacker",
                "X-Demo-Role": "main_admin",
                "X-Demo-Capabilities": "ask:read_only",
            },
        )
        self.assertEqual(response.status_code, 401)

    def test_unauthenticated_request_is_rejected(self):
        client = _client("staging", **OIDC_ENV)
        response = client.post(
            "/v1/chat",
            json={"prompt": "hello", "institution_scope": {"college_id": "college_a"}},
        )
        self.assertEqual(response.status_code, 401)



class LoginContentSecurityPolicyTests(unittest.TestCase):
    """default-src alone blocks the browser from reaching the identity
    provider, which silently breaks the whole login."""

    def test_connect_src_allows_the_configured_provider_origins(self):
        client = _client("staging", SAFFRON_OIDC_BROWSER_ORIGINS="https://idp.example.test", **OIDC_ENV)
        policy = client.get("/v1/auth/config").headers["content-security-policy"]
        directives = {
            part.strip().split(" ")[0]: part.strip()
            for part in policy.split(";")
            if part.strip()
        }
        self.assertIn("connect-src", directives)
        connect_src = directives["connect-src"]
        self.assertIn("'self'", connect_src)
        # The token endpoint host, and the issuer host, which is derived.
        self.assertIn("https://idp.example.test", connect_src)
        self.assertIn("https://issuer.example.test", connect_src)
        # Everything else stays closed.
        self.assertIn("default-src 'self'", policy)
        self.assertNotIn("*", connect_src)

    def test_demo_mode_keeps_the_policy_closed(self):
        policy = _client("development").get("/v1/auth/config").headers["content-security-policy"]
        self.assertIn("connect-src 'self';", policy)

    def test_malformed_origins_cannot_widen_the_policy(self):
        client = _client(
            "staging",
            SAFFRON_OIDC_BROWSER_ORIGINS="not-a-url, javascript:alert(1), https://ok.example.test/with/path",
            **OIDC_ENV,
        )
        policy = client.get("/v1/auth/config").headers["content-security-policy"]
        self.assertNotIn("javascript:", policy)
        self.assertNotIn("not-a-url", policy)
        # A URL carrying a path is reduced to its origin.
        self.assertIn("https://ok.example.test", policy)
        self.assertNotIn("/with/path", policy)
if __name__ == "__main__":
    unittest.main()

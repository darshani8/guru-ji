import time
import unittest

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from app.auth.oidc import AuthenticationError, JwtVerifier
from app.auth.principal import principal_from_headers
from app.config.settings import AppSettings
from app.domain.principals import Capability, InstitutionScope, PrincipalType


class _SigningKey:
    def __init__(self, key):
        self.key = key


class _FakeJwksClient:
    def __init__(self, key):
        self.key = key

    def get_signing_key_from_jwt(self, token):
        return _SigningKey(self.key)


class OidcAuthenticationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.public_key = cls.private_key.public_key()
        cls.settings = AppSettings(
            environment="production",
            dev_bearer_token="not-used-by-production",
            allowed_origins=("https://guru.example.test",),
            control_database_url="postgresql://user:password@private.example/guru",
            oidc_issuer_url="https://issuer.example.test/",
            oidc_audience="guru-ji-api",
            oidc_jwks_url="https://issuer.example.test/.well-known/jwks.json",
            oidc_algorithms=("RS256",),
        )

    def token(self, **overrides):
        now = int(time.time())
        claims = {
            "sub": "faculty-42",
            "iss": self.settings.oidc_issuer_url,
            "aud": self.settings.oidc_audience,
            "iat": now,
            "exp": now + 300,
            "guru_role": "faculty",
            "guru_capabilities": ["ask:read_only", "source:view_metadata"],
            "guru_scopes": [{"college_id": "college_a", "department_id": "cse"}],
        }
        claims.update(overrides)
        return jwt.encode(claims, self.private_key, algorithm="RS256")

    def verifier(self):
        return JwtVerifier(self.settings, _FakeJwksClient(self.public_key))

    def test_valid_token_maps_only_verified_claims(self):
        principal = self.verifier().verify(self.token())
        self.assertEqual(principal.principal_id, "faculty-42")
        self.assertEqual(principal.principal_type, PrincipalType.FACULTY)
        self.assertTrue(principal.has_capability(Capability.ASK_READ_ONLY))
        self.assertTrue(principal.can_access(InstitutionScope("college_a", "cse", "batch-2026")))
        self.assertFalse(principal.has_capability(Capability.MANAGE_ACCESS))

    def test_wrong_audience_is_rejected(self):
        with self.assertRaises(AuthenticationError):
            self.verifier().verify(self.token(aud="another-service"))

    def test_expired_token_is_rejected(self):
        with self.assertRaises(AuthenticationError):
            self.verifier().verify(self.token(exp=int(time.time()) - 1))

    def test_production_headers_require_verified_bearer_token(self):
        verifier = self.verifier()
        unauthenticated = principal_from_headers({}, self.settings, verifier)
        self.assertFalse(unauthenticated.authenticated)
        principal = principal_from_headers({"authorization": f"Bearer {self.token()}"}, self.settings, verifier)
        self.assertTrue(principal.authenticated)
        self.assertEqual(principal.principal_id, "faculty-42")

    def test_development_demo_identity_remains_explicitly_scoped(self):
        settings = AppSettings(environment="development", dev_bearer_token="dev-token")
        principal = principal_from_headers(
            {
                "authorization": "Bearer dev-token",
                "x-demo-principal": "demo-faculty",
                "x-demo-role": "faculty",
                "x-demo-college": "college_a",
            },
            settings,
        )
        self.assertTrue(principal.authenticated)
        self.assertEqual(principal.principal_id, "demo-faculty")
        self.assertEqual(principal.principal_type, PrincipalType.FACULTY)


if __name__ == "__main__":
    unittest.main()

"""Tests for pure authorization decisions."""

import unittest

from app.domain.principals import Capability, InstitutionScope, Principal, PrincipalType
from app.policy.authorization import AuthorizationDecision, DenialReason, authorize


class AuthorizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scope = InstitutionScope("college-a", "department-cse")

    def make_principal(
        self,
        *,
        capabilities: frozenset[Capability] = frozenset({Capability.ASK_READ_ONLY}),
        authenticated: bool = True,
        principal_type: PrincipalType = PrincipalType.FACULTY,
    ) -> Principal:
        return Principal(
            principal_id="faculty-1",
            principal_type=principal_type,
            capabilities=capabilities,
            scopes=(self.scope,),
            authenticated=authenticated,
        )

    def test_authorize_allows_matching_capability_and_scope(self) -> None:
        decision = authorize(
            self.make_principal(),
            Capability.ASK_READ_ONLY,
            InstitutionScope("college-a", "department-cse", "batch-2026"),
        )

        self.assertTrue(decision.allowed)
        self.assertIsNone(decision.reason)

    def test_unauthenticated_principal_is_denied_first(self) -> None:
        decision = authorize(
            self.make_principal(authenticated=False),
            Capability.ASK_READ_ONLY,
            self.scope,
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, DenialReason.UNAUTHENTICATED)

    def test_missing_capability_is_denied(self) -> None:
        decision = authorize(
            self.make_principal(capabilities=frozenset()),
            Capability.ASK_READ_ONLY,
            self.scope,
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, DenialReason.MISSING_CAPABILITY)

    def test_out_of_scope_request_is_denied(self) -> None:
        decision = authorize(
            self.make_principal(),
            Capability.ASK_READ_ONLY,
            InstitutionScope("college-b"),
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, DenialReason.OUT_OF_SCOPE)

    def test_decision_invariants_reject_ambiguous_states(self) -> None:
        with self.assertRaisesRegex(ValueError, "allowed decisions"):
            AuthorizationDecision(True, DenialReason.OUT_OF_SCOPE)

        with self.assertRaisesRegex(ValueError, "denied decisions"):
            AuthorizationDecision(False)


if __name__ == "__main__":
    unittest.main()

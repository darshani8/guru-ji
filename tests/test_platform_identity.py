import unittest

from app.auth.principal import principal_from_headers
from app.auth.roles import ROLE_CAPABILITIES, capabilities_for_role, role_from_alias
from app.config.settings import AppSettings
from app.domain.principals import Capability, InstitutionScope, Principal, PrincipalType
from app.policy.pdp import LocalPolicyDecisionPoint


class PlatformIdentityTests(unittest.TestCase):
    def test_every_role_has_an_explicit_grant_and_students_stay_least_privileged(self):
        for role in PrincipalType:
            self.assertIn(role, ROLE_CAPABILITIES)
        student = capabilities_for_role(PrincipalType.STUDENT)
        self.assertNotIn(Capability.STUDENTS_READ, student)
        self.assertNotIn(Capability.RECORDS_WRITE, student)
        self.assertIn(Capability.AGENT_COMMAND, student)
        self.assertEqual(capabilities_for_role(PrincipalType.INSTITUTION_ADMIN), frozenset(Capability))

    def test_role_hierarchy_is_monotonic(self):
        self.assertTrue(capabilities_for_role(PrincipalType.FACULTY) < capabilities_for_role(PrincipalType.HOD))
        self.assertTrue(capabilities_for_role(PrincipalType.HOD) < capabilities_for_role(PrincipalType.PRINCIPAL))
        self.assertIn(Capability.RECORDS_WRITE, capabilities_for_role(PrincipalType.PRINCIPAL))
        self.assertNotIn(Capability.MANAGE_ACCESS, capabilities_for_role(PrincipalType.PRINCIPAL))

    def test_unknown_role_aliases_never_escalate(self):
        self.assertEqual(role_from_alias("superuser"), PrincipalType.STUDENT)
        self.assertEqual(role_from_alias("Head_Of_Department"), PrincipalType.HOD)
        self.assertEqual(role_from_alias(None), PrincipalType.STUDENT)

    def test_demo_headers_accept_platform_roles(self):
        settings = AppSettings(environment="development")
        principal = principal_from_headers({"authorization": "Bearer dev-token", "x-demo-principal": "p1", "x-demo-role": "hod", "x-demo-college": "college_a"}, settings)
        self.assertEqual(principal.principal_type, PrincipalType.HOD)
        self.assertIn(Capability.ACTIONS_EMAIL, principal.capabilities)
        self.assertFalse(principal.consent_verified)

    def test_local_pdp_accepts_platform_actions_but_not_arbitrary_ones(self):
        pdp = LocalPolicyDecisionPoint()
        principal = Principal("p1", PrincipalType.PRINCIPAL, capabilities_for_role(PrincipalType.PRINCIPAL), (InstitutionScope("college_a"),))
        for action in ("retrieve", "execute", "high_risk"):
            self.assertTrue(pdp.evaluate(principal=principal, required_capability=Capability.STUDENTS_READ, action=action, resource_type="platform_tool", resource_id="find_students", requested_scope=InstitutionScope("college_a")).allowed)
        self.assertFalse(pdp.evaluate(principal=principal, required_capability=Capability.STUDENTS_READ, action="delete", resource_type="platform_tool", resource_id="x", requested_scope=InstitutionScope("college_a")).allowed)
        self.assertFalse(pdp.evaluate(principal=principal, required_capability=Capability.STUDENTS_READ, action="retrieve", resource_type="platform_tool", resource_id="x", requested_scope=InstitutionScope("college_b")).allowed)


if __name__ == "__main__":
    unittest.main()

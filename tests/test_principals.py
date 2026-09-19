"""Tests for identity and institution scope primitives."""

import unittest

from app.domain.principals import (
    Capability,
    InstitutionScope,
    Principal,
    PrincipalType,
)


class InstitutionScopeTests(unittest.TestCase):
    def test_scope_coverage_is_hierarchical(self) -> None:
        college = InstitutionScope("college-a")
        department = InstitutionScope("college-a", "department-cse")
        batch = InstitutionScope("college-a", "department-cse", "batch-2026")

        self.assertTrue(college.covers(department))
        self.assertTrue(college.covers(batch))
        self.assertTrue(department.covers(batch))
        self.assertFalse(department.covers(college))
        self.assertFalse(college.covers(InstitutionScope("college-b")))

    def test_scope_rejects_blank_identifiers(self) -> None:
        with self.assertRaisesRegex(ValueError, "college_id"):
            InstitutionScope(" ")
        with self.assertRaisesRegex(ValueError, "department_id"):
            InstitutionScope("college-a", " ")


class PrincipalTests(unittest.TestCase):
    def test_capability_and_scope_are_explicit(self) -> None:
        scope = InstitutionScope("college-a", "department-cse")
        principal = Principal(
            principal_id="faculty-1",
            principal_type=PrincipalType.FACULTY,
            capabilities=frozenset({Capability.ASK_READ_ONLY}),
            scopes=(scope,),
        )

        self.assertTrue(principal.has_capability(Capability.ASK_READ_ONLY))
        self.assertFalse(principal.has_capability(Capability.MANAGE_ACCESS))
        self.assertTrue(principal.can_access(InstitutionScope("college-a", "department-cse", "batch-2026")))
        self.assertFalse(principal.can_access(InstitutionScope("college-b")))

    def test_anonymous_principal_cannot_carry_authority(self) -> None:
        anonymous = Principal(
            principal_id="anonymous",
            principal_type=PrincipalType.ANONYMOUS,
            authenticated=False,
        )
        self.assertFalse(anonymous.capabilities)
        self.assertFalse(anonymous.scopes)

        with self.assertRaisesRegex(ValueError, "anonymous"):
            Principal(
                principal_id="anonymous",
                principal_type=PrincipalType.ANONYMOUS,
            )

        with self.assertRaisesRegex(ValueError, "anonymous"):
            Principal(
                principal_id="anonymous",
                principal_type=PrincipalType.ANONYMOUS,
                capabilities=frozenset({Capability.ASK_READ_ONLY}),
                authenticated=False,
            )

    def test_principal_id_must_not_be_blank(self) -> None:
        with self.assertRaisesRegex(ValueError, "principal_id"):
            Principal(" ", PrincipalType.FACULTY)


if __name__ == "__main__":
    unittest.main()

"""Tests for plan-only read-only orchestration."""

import unittest

from app.domain.principals import Capability, InstitutionScope, Principal, PrincipalType
from app.domain.requests import ChatRequest
from app.orchestration.execution_core import (
    ExecutionPlan,
    PlanStep,
    PlanStepKind,
    build_read_only_plan,
)


class ExecutionCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scope = InstitutionScope("college-a", "department-cse")
        self.request = ChatRequest(
            request_id="request-1",
            principal_id="faculty-1",
            prompt="Explain the attendance policy.",
            institution_scope=self.scope,
            source_ids=("policy-1", "policy-2"),
        )
        self.principal = Principal(
            principal_id="faculty-1",
            principal_type=PrincipalType.FACULTY,
            capabilities=frozenset({Capability.ASK_READ_ONLY}),
            scopes=(self.scope,),
        )

    def test_authorized_plan_has_bounded_semantic_steps(self) -> None:
        plan = build_read_only_plan(self.request, self.principal)

        self.assertTrue(plan.authorized)
        self.assertIsNone(plan.denial_reason)
        self.assertEqual(
            tuple(step.kind for step in plan.steps),
            (
                PlanStepKind.CLASSIFY_REQUEST,
                PlanStepKind.QUERY_APPROVED_SOURCES,
                PlanStepKind.SYNTHESIZE_CITED_ANSWER,
            ),
        )
        self.assertEqual(plan.steps[1].source_ids, ("policy-1", "policy-2"))

    def test_denied_plan_contains_no_steps(self) -> None:
        principal = Principal(
            principal_id="faculty-1",
            principal_type=PrincipalType.FACULTY,
            scopes=(self.scope,),
        )

        plan = build_read_only_plan(self.request, principal)

        self.assertFalse(plan.authorized)
        self.assertEqual(plan.denial_reason, "missing_capability")
        self.assertEqual(plan.steps, ())

    def test_plan_invariants_bound_future_execution(self) -> None:
        steps = tuple(
            PlanStep(str(index), PlanStepKind.CLASSIFY_REQUEST, "bounded step")
            for index in range(6)
        )
        with self.assertRaisesRegex(ValueError, "more than five"):
            ExecutionPlan("request-1", True, steps)

        with self.assertRaisesRegex(ValueError, "at least one step"):
            ExecutionPlan("request-1", True)

        with self.assertRaisesRegex(ValueError, "denied plans cannot"):
            ExecutionPlan("request-1", False, (steps[0],), denial_reason="denied")

    def test_plan_step_rejects_blank_metadata(self) -> None:
        with self.assertRaisesRegex(ValueError, "step_id"):
            PlanStep(" ", PlanStepKind.CLASSIFY_REQUEST, "description")

        with self.assertRaisesRegex(ValueError, "description"):
            PlanStep("step-1", PlanStepKind.CLASSIFY_REQUEST, " ")

        with self.assertRaisesRegex(ValueError, "source_ids"):
            PlanStep("step-1", PlanStepKind.QUERY_APPROVED_SOURCES, "description", (" ",))


if __name__ == "__main__":
    unittest.main()

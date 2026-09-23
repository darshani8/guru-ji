import unittest

from app.agents.bindings import BindingError, resolve_reference
from app.agents.contracts import AgentCommand, AgentPlan, PlanStep
from app.agents.planner import DeterministicPlanner, ModelPlanner, Vocabulary, extract_entities
from app.agents.specialists import STEP_FAILURE_MESSAGE, build_specialists
from app.domain.audit import AuditOutcome
from app.domain.principals import InstitutionScope, PrincipalType
from platform_fixtures import PlatformFixture, principal


class _Model:
    provider_id = "fake-model"
    model_id = "fake"

    def __init__(self, reply):
        self.reply = reply

    async def complete(self, prompt, *, max_tokens=800):
        return self.reply(prompt) if callable(self.reply) else self.reply


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.fx = PlatformFixture()
        self.tools = self.fx.registry.for_principal(principal(PrincipalType.PRINCIPAL))
        self.vocab = Vocabulary(("MBA", "BCA"), ("Computer Science",))
        self.planner = DeterministicPlanner()

    def test_entity_extraction(self):
        entities = extract_entities("List MBA students in 3rd sem below 65% attendance and email the excel to the HOD", self.vocab)
        self.assertEqual((entities.program, entities.semester, entities.threshold), ("MBA", 3, 65.0))
        self.assertTrue(entities.wants_report and entities.wants_email and entities.wants_list)
        self.assertEqual(entities.recipient_role, "hod")
        self.assertEqual(entities.report_format, "xlsx")
        self.assertEqual(extract_entities("What happened online this month?", self.vocab).window_days, 30)
        self.assertEqual(extract_entities("Update the phone number of student MBA001 to 9999988888", self.vocab).field_change, ("phone", "9999988888"))

    def test_plans_for_representative_commands(self):
        cases = {
            "How many MBA students have attendance below 75%?": ["find_low_attendance"],
            "Find all MBA students below 75% attendance and send the report to the HOD": ["find_low_attendance", "generate_report", "find_hod", "send_email"],
            "Create a pdf report of students with pending fees and notify me": ["get_pending_fees", "generate_report", "create_notification"],
            "Who is the HOD of Computer Science department?": ["find_hod"],
            "What is the leave policy?": ["search_documents"],
            "What happened about our college on the internet this week?": [],
            "Give me an overview of our institution": ["get_institution_summary"],
            "how many students": ["count_students"],
            "Update the email of student MBA002 to new@abc.edu.in": ["update_student_record"],
        }
        for text, expected in cases.items():
            plan = self.planner.plan(text, self.tools, self.vocab)
            self.assertEqual([step.tool for step in plan.steps], expected, text)
        emailed = self.planner.plan("Find all MBA students below 75% attendance and send the report to the HOD", self.tools, self.vocab)
        self.assertEqual(emailed.steps[1].bindings, {"rows": "$s1.data.students"})
        self.assertEqual(emailed.steps[3].bindings["recipients"], "$s3.data.hods[*].faculty_id")
        self.assertEqual(emailed.steps[3].depends_on, ("s2", "s3"))
        internet = self.planner.plan("What happened about our college on the internet this week?", self.tools, self.vocab)
        self.assertIsNotNone(internet.clarification, "no intelligence service registered -> clarification")

    def test_change_value_stops_at_conjunctions_and_clause_boundaries(self):
        cases = {
            "Change section of student MBA001 to B and notify me": ("section", "B"),
            "Set the status of student MBA001 to inactive then email the HOD": ("status", "inactive"),
            "Change the semester of student MBA001 to 4 and section to B": ("semester", "4"),
            "Update section of student MBA001 to B, then notify me": ("section", "B"),
            "Update the phone number of student MBA001 to 9999988888 and notify me": ("phone", "9999988888"),
            "Update the address of student MBA001 to Jayanagar Bengaluru": ("address", "Jayanagar Bengaluru"),
            "Update the address of student MBA001 to Jayanagar and notify me": ("address", "Jayanagar"),
            "Change the guardian phone of student MBA001 to 98765 43210": ("guardian_phone", "9876543210"),
            "Set status of student MBA001 to on hold": ("status", "on hold"),
            "Set status of student MBA001 to on hold and notify me": ("status", "on hold"),
        }
        for text, expected in cases.items():
            self.assertEqual(extract_entities(text, self.vocab).field_change, expected, text)
        plan = self.planner.plan("Change section of student MBA001 to B and notify me", self.tools, self.vocab)
        self.assertEqual(plan.steps[0].arguments, {"student_id": "MBA001", "changes": {"section": "B"}})

    def test_loose_student_id_needs_an_identifier_keyword(self):
        self.assertIsNone(extract_entities("Show results of students affected by COVID-19", self.vocab).student_id)
        self.assertIsNone(extract_entities("Show me results for sem-03 MBA students", self.vocab).student_id)
        self.assertIsNone(extract_entities("results for semester-3 MBA", self.vocab).student_id)
        self.assertIsNone(extract_entities("what is the pass percentage of sem 3 students", self.vocab).student_id)
        self.assertEqual(extract_entities("Show results of student MBA001", self.vocab).student_id, "MBA001")
        # A known program prefix followed by digits is an identifier on its own.
        self.assertEqual(extract_entities("Show results of MBA001", self.vocab).student_id, "MBA001")
        self.assertEqual(extract_entities("What are the marks of BCA0017?", self.vocab).student_id, "BCA0017")
        self.assertEqual(extract_entities("fees pending for MBA001", self.vocab).student_id, "MBA001")
        bare = self.planner.plan("Show results of MBA001", self.tools, self.vocab)
        self.assertEqual([(step.tool, step.arguments) for step in bare.steps], [("get_student_results", {"student_id": "MBA001"})])
        self.assertEqual(extract_entities("Get the results of student number MBA001", self.vocab).student_id, "MBA001")
        self.assertEqual(extract_entities("results of student sem-03 MBA001", self.vocab).student_id, "MBA001")
        self.assertEqual(extract_entities("Show marks of usn 1AB21CS001", self.vocab).student_id, "1AB21CS001")
        covid = self.planner.plan("Show results of students affected by COVID-19", self.tools, self.vocab)
        self.assertEqual([step.tool for step in covid.steps], ["get_exam_summary"])
        semester = self.planner.plan("Show me results for sem-03 MBA students", self.tools, self.vocab)
        self.assertEqual([(step.tool, step.arguments) for step in semester.steps], [("get_exam_summary", {"program": "MBA", "semester": 3})])
        one = self.planner.plan("Show results of student MBA001", self.tools, self.vocab)
        self.assertEqual([(step.tool, step.arguments) for step in one.steps], [("get_student_results", {"student_id": "MBA001"})])

    def test_clarifications_for_ambiguous_or_unauthorised_commands(self):
        self.assertIn("Who should receive", self.planner.plan("email the fee defaulters list", self.tools, self.vocab).clarification)
        self.assertIn("student ID", self.planner.plan("update the student record", self.tools, self.vocab).clarification)
        self.assertIn("could not map", self.planner.plan("do something", self.tools, self.vocab).clarification)
        student_tools = self.fx.registry.for_principal(principal(PrincipalType.STUDENT))
        self.assertIn("permission", self.planner.plan("how many students are there", student_tools, self.vocab).clarification)

    def test_model_planner_validates_and_falls_back(self):
        good = ModelPlanner(_Model('{"intent": "count", "steps": [{"step_id": "s1", "tool": "count_students", "arguments": {"program": "MBA"}}], "confidence": 0.9}'))
        plan = __import__("asyncio").run(good.plan("how many mba students", self.tools, self.vocab))
        self.assertEqual(plan.planner, "model")
        self.assertEqual(plan.steps[0].arguments, {"program": "MBA"})
        bad = ModelPlanner(_Model('{"steps": [{"step_id": "s1", "tool": "run_sql", "arguments": {"sql": "DROP TABLE"}}]}'))
        plan = __import__("asyncio").run(bad.plan("how many mba students", self.tools, self.vocab))
        self.assertEqual(plan.planner, "deterministic")
        self.assertEqual(plan.steps[0].tool, "count_students")
        broken = ModelPlanner(_Model("not json"))
        self.assertEqual(__import__("asyncio").run(broken.plan("how many mba students", self.tools, self.vocab)).planner, "deterministic")

    def test_model_planner_may_call_a_request_unmapped_but_never_overrides_a_match(self):
        import asyncio

        unknown = ModelPlanner(_Model('{"intent": "unknown", "steps": [], "clarification": null, "confidence": 0}'))
        chat = asyncio.run(unknown.plan("what is photosynthesis?", self.tools, self.vocab))
        self.assertEqual((chat.intent, chat.steps, chat.planner), ("unknown", [], "model"))
        self.assertIn("could not map", chat.clarification)
        matched = asyncio.run(unknown.plan("how many mba students", self.tools, self.vocab))
        self.assertEqual((matched.planner, matched.steps[0].tool), ("deterministic", "count_students"))

    def test_model_planner_falls_back_on_malformed_shapes(self):
        import asyncio

        def plan_for(raw):
            return asyncio.run(ModelPlanner(_Model(raw)).plan("how many mba students", self.tools, self.vocab))

        # Shapes that cannot be a plan: the deterministic plan stands and nothing raises.
        falls_back = [
            '{"steps": [{"step_id": "s1", "tool": ["count_students"]}]}',
            '{"steps": [{"step_id": "s1", "tool": {"name": "count_students"}}]}',
            '{"steps": [{"step_id": "s1", "tool": null}]}',
            '{"steps": "count_students"}',
            '{"steps": [null]}',
            '{"steps": [{"step_id": "s1", "tool": "count_students", "depends_on": [1, "s0"]}]}',
            '{"steps": [{"step_id": "s1", "tool": "count_students", "arguments": {"program": ["MBA"]}}]}',
        ]
        for raw in falls_back:
            plan = plan_for(raw)
            self.assertEqual(plan.planner, "deterministic", raw)
            self.assertEqual([step.tool for step in plan.steps], ["count_students"], raw)
        # Shapes with a valid tool but sloppy metadata are coerced to safe defaults.
        tolerated = [
            '{"steps": [{"step_id": "s1", "tool": "count_students", "depends_on": null}]}',
            '{"steps": [{"step_id": "s1", "tool": "count_students", "depends_on": "s0"}]}',
            '{"steps": [{"step_id": "s1", "tool": "count_students"}], "confidence": [1]}',
            '{"steps": [{"step_id": "s1", "tool": "count_students"}], "confidence": {"value": 1}}',
            '{"steps": [{"step_id": "s1", "tool": "count_students"}], "confidence": "NaN"}',
            '{"steps": [{"step_id": "s1", "tool": "count_students"}], "confidence": "very sure"}',
            '{"steps": [{"step_id": "s1", "tool": "count_students", "bindings": {"x": null}}]}',
            '{"steps": [{"step_id": null, "tool": "count_students", "purpose": ["x"]}], "intent": ["a"]}',
        ]
        for raw in tolerated:
            plan = plan_for(raw)
            self.assertEqual((plan.planner, plan.confidence), ("model", 0.7), raw)
            self.assertEqual([(step.tool, step.depends_on, step.bindings) for step in plan.steps], [("count_students", (), {})], raw)
        # A clarification with a non-numeric confidence is still a usable clarification.
        clarify = plan_for('{"clarification": "which program?", "confidence": "high"}')
        self.assertEqual((clarify.planner, clarify.clarification, clarify.confidence), ("model", "which program?", 0.5))
        clamped = plan_for('{"steps": [{"step_id": "s1", "tool": "count_students"}], "confidence": 7}')
        self.assertEqual((clamped.planner, clamped.confidence), ("model", 1.0))

    def test_bindings_and_plan_validation(self):
        results = {"s1": {"data": {"students": [{"student_id": "A", "name": "x"}, {"student_id": "B"}]}}}
        self.assertEqual(resolve_reference("$s1.data.students[*].student_id", results), ["A", "B"])
        self.assertEqual(resolve_reference("$s1.data.students[1].student_id", results), "B")
        with self.assertRaises(BindingError):
            resolve_reference("$s2.data", results)
        with self.assertRaises(ValueError):
            AgentPlan("x", [PlanStep("s1", "a"), PlanStep("s1", "b")])
        with self.assertRaises(ValueError):
            AgentPlan("x", [PlanStep("s1", "a", depends_on=("s9",))])


class MasterAgentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fx = PlatformFixture()
        self.hod = principal(PrincipalType.HOD)

    async def run_command(self, text, who=None, **kwargs):
        return await self.fx.agent.handle(AgentCommand("req-1", who or self.hod, InstitutionScope("college_a"), text, **kwargs))

    async def test_multi_step_command_performs_work_and_reports_sources(self):
        response = await self.run_command("Find all MBA students below 75% attendance and send the report to the HOD")
        self.assertEqual(response.status, "complete")
        self.assertEqual([step.tool for step in response.steps], ["find_low_attendance", "generate_report", "find_hod", "send_email"])
        self.assertIn("meena@abc.edu.in", response.answer)
        self.assertTrue(any(artifact["type"] == "report" for artifact in response.artifacts))
        self.assertTrue(response.sources)
        run = self.fx.store.list_agent_runs("college_a")[0]
        self.assertEqual(run["status"], "complete")
        self.assertEqual(run["tool_names"], ["find_low_attendance", "generate_report", "find_hod", "send_email"])
        self.assertEqual(self.fx.control.recent_audit(1)[0].event_type, "agent.command")

    async def test_names_are_listed_when_asked_and_counts_when_not(self):
        listed = await self.run_command("Give me the names of MBA students below 75% attendance")
        self.assertIn("Names: Student 1 (MBA001, 60.0%)", listed.answer)
        counted = await self.run_command("How many MBA students have attendance below 75%?")
        self.assertNotIn("Names:", counted.answer)
        self.assertIn("2 of 5", counted.answer)

    async def test_refusals_clarifications_and_permissions(self):
        student = principal(PrincipalType.STUDENT)
        response = await self.run_command("How many students are there?", who=student)
        self.assertEqual(response.status, "needs_input")
        anonymous = await self.run_command("hi", who=principal(PrincipalType.HOD, college_id="college_b"))
        self.assertEqual(anonymous.status, "refused")
        unclear = await self.run_command("please do the thing")
        self.assertEqual(unclear.status, "needs_input")
        self.assertIn("could not map", unclear.answer)

    async def test_high_risk_update_flows_through_approval(self):
        pri = principal(PrincipalType.PRINCIPAL)
        first = await self.run_command("Update the phone number of student MBA001 to 9999988888", who=pri)
        self.assertEqual(first.status, "approval_required")
        self.fx.gateway.decide_approval(pri, "college_a", first.approval["approval_id"], approve=True)
        second = await self.run_command("Update the phone number of student MBA001 to 9999988888", who=pri, approval_id=first.approval["approval_id"])
        self.assertEqual(second.status, "complete")
        self.assertEqual(self.fx.store.get_record("college_a", "student", "mba001")["phone"], "9999988888")

    async def test_background_execution_notifies_the_requester(self):
        response = await self.run_command("How many students are there?", run_in_background=True)
        self.assertEqual(response.status, "accepted")
        job = self.fx.jobs.status(response.job_id)
        self.assertEqual(job["status"], "succeeded")
        self.assertEqual(job["result"]["answer"], "8 student(s).")
        inbox = self.fx.notifications.inbox(self.hod, "college_a")
        self.assertEqual(inbox[0]["title"], "Command finished: complete")

    async def test_step_exception_becomes_failed_result_and_is_audited(self):
        class ExplodingGateway:
            async def invoke(self, tool_name, arguments, context):
                raise RuntimeError("postgresql://user:secret@db.internal:5432 connection refused")

        self.fx.agent.specialists = build_specialists(ExplodingGateway())
        response = await self.run_command("How many students are there?")
        self.assertEqual(response.status, "failed")
        self.assertEqual([(step.tool, step.status) for step in response.steps], [("count_students", "failed")])
        self.assertEqual(response.steps[0].denial_reason, STEP_FAILURE_MESSAGE)
        self.assertNotIn("secret", response.answer)
        self.assertNotIn("db.internal", response.answer)
        self.assertIn("count_students could not run", response.answer)
        event = self.fx.control.recent_audit(1)[0]
        self.assertEqual((event.event_type, event.request_id, event.outcome), ("agent.command", "req-1", AuditOutcome.FAILED))
        run = self.fx.store.list_agent_runs("college_a")[0]
        self.assertEqual((run["status"], run["tool_names"]), ("failed", ["count_students"]))
        self.assertNotIn("secret", str(run))

    async def test_step_exception_marks_dependants_and_keeps_partial_results(self):
        real = self.fx.gateway

        class FlakyGateway:
            async def invoke(self, tool_name, arguments, context):
                if tool_name == "generate_report":
                    raise OSError("object store unreachable")
                return await real.invoke(tool_name, arguments, context)

        self.fx.agent.specialists = build_specialists(FlakyGateway())
        response = await self.run_command("Find all MBA students below 75% attendance and send the report to the HOD")
        self.assertEqual(response.status, "partial")
        statuses = {step.tool: step.status for step in response.steps}
        self.assertEqual(statuses["find_low_attendance"], "success")
        self.assertEqual(statuses["generate_report"], "failed")
        self.assertNotIn("send_email", statuses, "a step whose dependency failed is skipped")
        self.assertEqual(self.fx.store.list_agent_runs("college_a")[0]["status"], "partial")

    async def test_model_wording_is_guarded(self):
        self.fx.agent.model = _Model("There are exactly ninety students.")
        response = await self.run_command("How many students are there?")
        self.assertEqual(response.generation_mode, "deterministic_fallback")
        self.assertIn("8 student(s)", response.answer)
        self.fx.agent.model = _Model(lambda prompt: "Right now the institution has 8 student(s) on record.")
        response = await self.run_command("How many students are there?")
        self.assertEqual(response.generation_mode, "fake-model")


if __name__ == "__main__":
    unittest.main()

"""The open-task agent: routing, the code sandbox, the Claude tool loop, delivery and limits."""

import asyncio
import json
import os
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.agents.contracts import AgentCommand, AgentPlan, PlanStep
from app.agents.planner import DeterministicPlanner, ModelPlanner, Vocabulary
from app.config.settings import AppSettings
from app.domain.errors import ErrorCode, GuruJiError, PublicError
from app.domain.principals import InstitutionScope, PrincipalType
from app.open_task.agent import RUN_PYTHON, SYSTEM_PROMPT, OpenTaskAgent, OpenTaskLimits
from app.open_task.routing import asks_for_work, requested_format_beyond_tools, route_to_open_task
from app.open_task.sandbox import Sandbox, SandboxConfig, SandboxUnavailable, available_libraries, namespaces_available
from app.providers.anthropic import OPEN_TASK_MODEL_ID, AnthropicProvider
from platform_fixtures import PlatformFixture, principal

SCOPE = InstitutionScope("college_a")


def _text(value):
    return SimpleNamespace(type="text", text=value)


def _tool(block_id, name, arguments):
    return SimpleNamespace(type="tool_use", id=block_id, name=name, input=arguments)


def _message(stop_reason, *blocks):
    return SimpleNamespace(stop_reason=stop_reason, content=list(blocks), usage=SimpleNamespace(input_tokens=100, output_tokens=40, cache_read_input_tokens=60, cache_creation_input_tokens=0))


def _last_results(messages):
    """The tool results the agent sent back in its latest user turn, decoded."""

    return [json.loads(block["content"]) for block in messages[-1]["content"] if block.get("type") == "tool_result"]


class ScriptedModel:
    """Stands in for AnthropicProvider.tool_turn: each step returns the next message."""

    model_id = OPEN_TASK_MODEL_ID

    def __init__(self, *steps):
        self.steps = list(steps)
        self.calls = []

    async def tool_turn(self, *, system, tools, messages, max_tokens):
        self.calls.append({"system": system, "tools": json.dumps(tools, sort_keys=True), "messages": list(messages), "max_tokens": max_tokens})
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step(messages) if callable(step) else step


def _agent(fx, model, **limits):
    return OpenTaskAgent(fx.gateway, fx.registry, fx.reports, fx.control, model, sandbox=Sandbox(SandboxConfig(mode="auto", run_timeout_seconds=30)), limits=OpenTaskLimits(**limits), background=False)


def _command(text, role=PrincipalType.PRINCIPAL, **options):
    return AgentCommand("req-open-1", principal(role), SCOPE, text, **options)


# A task that reads the attendance records the tool saved and writes a summary file.
REPORT_CODE = """
import csv, json
path = {path!r}
rows = list(csv.DictReader(open(path, encoding="utf-8")))
average = sum(float(row["attendance_percent"]) for row in rows) / len(rows)
with open("outputs/attendance_summary.md", "w", encoding="utf-8") as handle:
    handle.write("# MBA students below 75%\\n\\n")
    for row in rows:
        handle.write(f"- {{row['student_id']}}: {{row['attendance_percent']}}%\\n")
    handle.write(f"\\nAverage: {{average:.1f}}%\\n")
with open("outputs/notes.html", "w") as handle:
    handle.write("<script>alert(1)</script>")
print(len(rows), round(average, 1))
"""


def _attendance_task():
    def build(messages):
        files = _last_results(messages)[0]["files"]
        return _message("tool_use", _tool("tu_2", RUN_PYTHON, {"code": REPORT_CODE.format(path=files["csv"])}))

    return ScriptedModel(
        _message("tool_use", _text("Fetching attendance."), _tool("tu_1", "find_low_attendance", {"program": "MBA", "threshold": 75})),
        build,
        _message("end_turn", _text("I made a summary of the 2 MBA students below 75% attendance (average 65.0%).")),
    )


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.fx = PlatformFixture()
        self.tools = self.fx.registry.for_principal(principal(PrincipalType.PRINCIPAL))
        self.vocabulary = Vocabulary(("MBA", "BCA"), ())
        self.planner = DeterministicPlanner()

    def route(self, text):
        return route_to_open_task(text, self.planner.plan(text, self.tools, self.vocabulary))

    def test_formats_the_tools_cannot_make_go_to_the_open_task_agent_even_when_the_records_matched(self):
        self.assertEqual(self.route("Make a PPT on MBA students below 75% attendance"), "format")
        self.assertEqual(self.route("Create a presentation of our fee collection"), "format")
        self.assertEqual(self.route("Give me a bar chart of attendance by program"), "format")
        self.assertEqual(self.route("Export pending fees to a Word document"), "format")
        self.assertEqual(self.route("Make an excel with a separate sheet for each program and totals"), "format")

    def test_what_the_tools_already_do_stays_with_them(self):
        for text in (
            "How many MBA students have attendance below 75%?",
            "Find all MBA students below 75% attendance and send the report to the HOD",
            "Create a pdf report of students with pending fees",
            "Give me an excel of MBA students below 75% attendance",
            "Who is the HOD of MBA?",
        ):
            self.assertIsNone(self.route(text), text)

    def test_unmapped_work_goes_to_the_agent_and_unmapped_chat_does_not(self):
        self.assertEqual(self.route("Prepare a timetable for MBA semester 1"), "unmapped_work")
        self.assertEqual(self.route("Draft a circular about the sports day"), "unmapped_work")
        for text in ("hello", "Write a poem about the monsoon", "What is the capital of France", "thank you"):
            self.assertIsNone(self.route(text), text)

    def test_the_model_planner_can_hand_work_over_only_when_the_agent_is_on(self):
        class Model:
            provider_id, model_id = "fake", "fake"

            async def complete(self, prompt, *, max_tokens=800):
                return '{"intent": "open_task", "steps": [], "clarification": null, "confidence": 0.9}'

        text = "Compare this year's attendance with last year's by department"
        plan = asyncio.run(ModelPlanner(Model(), open_task=True).plan(text, self.tools, self.vocabulary))
        self.assertEqual(plan.intent, "open_task")
        self.assertEqual(route_to_open_task(text, plan), "planner")
        plan = asyncio.run(ModelPlanner(Model()).plan(text, self.tools, self.vocabulary))
        self.assertNotEqual(plan.intent, "open_task")

    def test_helpers(self):
        self.assertEqual(requested_format_beyond_tools("make slides"), "presentation")
        self.assertIsNone(requested_format_beyond_tools("excel of pending fees"))
        self.assertTrue(asks_for_work("banao ek timetable"))
        self.assertFalse(asks_for_work("timetable"))
        mapped = AgentPlan("attendance", [PlanStep("s1", "find_low_attendance")])
        self.assertIsNone(route_to_open_task("list students below 75%", mapped))


class SandboxTests(unittest.IsolatedAsyncioTestCase):
    MODES = ("guarded", "isolated") if namespaces_available() else ("guarded",)

    async def test_code_runs_in_its_workspace_and_cannot_reach_the_host(self):
        os.environ["GURU_TEST_SECRET"] = "should-not-leak"
        self.addCleanup(os.environ.pop, "GURU_TEST_SECRET", None)
        repository_file = str(Path(__file__).resolve())
        code = f"""
import os, socket, subprocess
checks = {{}}
def attempt(name, action):
    try:
        action()
        checks[name] = "allowed"
    except Exception as exc:
        checks[name] = type(exc).__name__
attempt("network", lambda: socket.create_connection(("1.1.1.1", 80), timeout=2))
attempt("process", lambda: subprocess.run(["true"]))
attempt("read_repo", lambda: open({repository_file!r}).read())
attempt("write_outside", lambda: open("/tmp/guru-sandbox-escape", "w").write("x"))
attempt("fork", lambda: os.fork())
checks["secret"] = os.environ.get("GURU_TEST_SECRET")
checks["home"] = os.environ.get("HOME") == os.getcwd()
open("outputs/result.csv", "w").write("a,b\\n1,2\\n")
open("outputs/page.html", "w").write("<p>no</p>")
print(__import__("json").dumps(checks))
"""
        for mode in self.MODES:
            workspace = Sandbox(SandboxConfig(mode=mode, run_timeout_seconds=30)).open()
            try:
                result = await workspace.run(code)
                self.assertEqual(result.exit_code, 0, result.stderr)
                checks = json.loads(result.stdout)
                for name in ("network", "process", "read_repo", "write_outside", "fork"):
                    self.assertNotEqual(checks[name], "allowed", f"{mode}: {name}")
                self.assertIsNone(checks["secret"])
                self.assertTrue(checks["home"])
                self.assertFalse(Path("/tmp/guru-sandbox-escape").exists())
                files, skipped = workspace.collect_outputs()
                self.assertEqual([item.name for item in files], ["result.csv"])
                self.assertTrue(any("page.html" in reason for reason in skipped))
                root = workspace.root
            finally:
                workspace.close()
            self.assertFalse(root.exists(), "the workspace holds records and must be deleted")

    async def test_runaway_code_is_stopped_and_output_is_capped(self):
        workspace = Sandbox(SandboxConfig(mode="guarded", run_timeout_seconds=30, max_output_chars=1000)).open()
        try:
            looping = await workspace.run("while True:\n    pass", timeout_seconds=2)
            self.assertTrue(looping.timed_out)
            self.assertFalse(looping.ok)
            chatty = await workspace.run("print('x' * 50000)")
            self.assertTrue(chatty.truncated)
            self.assertLessEqual(len(chatty.stdout), 1000)
            failing = await workspace.run("raise SystemExit(3)")
            self.assertEqual(failing.exit_code, 3)
        finally:
            workspace.close()

    def test_isolated_mode_fails_closed_without_namespaces(self):
        sandbox = Sandbox(SandboxConfig(mode="isolated", unshare_path="/nonexistent/unshare"))
        with self.assertRaises(SandboxUnavailable):
            sandbox.open()
        with self.assertRaises(ValueError):
            SandboxConfig(mode="none")


class OpenTaskAgentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fx = PlatformFixture()

    async def test_reads_records_runs_code_and_delivers_the_files_as_reports(self):
        model = _attendance_task()
        response = await _agent(self.fx, model).run(_command("Make a summary document of MBA students below 75% attendance"))
        self.assertEqual(response.status, "complete", response.answer)
        self.assertEqual(response.intent, "open_task")
        self.assertEqual([step.tool for step in response.steps], ["find_low_attendance", RUN_PYTHON])
        self.assertTrue(all(step.status == "success" for step in response.steps))
        self.assertEqual([artifact["file_name"] for artifact in response.artifacts], ["attendance_summary.md"])
        self.assertIn("/v1/reports/", response.answer)
        self.assertTrue(any(warning["code"] == "open_task_file_skipped" and "notes.html" in warning["message"] for warning in response.warnings))
        record, content = self.fx.reports.fetch(principal(PrincipalType.PRINCIPAL), "college_a", response.artifacts[0]["report_id"])
        self.assertEqual(record["tool_name"], "open_task")
        self.assertIn("MBA001: 60.0%", content.decode())
        self.assertIn("Average: 65.0%", content.decode())
        self.assertEqual(response.plan["usage"]["output_tokens"], 120)
        self.assertEqual(response.sources[0]["source_id"], "college_a:attendance")
        # The code saw the tool's result as files and printed what it computed.
        run_result = _last_results(model.calls[2]["messages"])[0]
        self.assertEqual(run_result["stdout"].split(), ["2", "65.0"])

    async def test_the_conversation_prefix_never_changes_between_turns(self):
        model = _attendance_task()
        await _agent(self.fx, model).run(_command("Make a summary document of MBA students below 75% attendance"))
        self.assertEqual(len(model.calls), 3)
        self.assertEqual({call["system"] for call in model.calls}, {SYSTEM_PROMPT})
        self.assertEqual(len({call["tools"] for call in model.calls}), 1)
        for earlier, later in zip(model.calls, model.calls[1:]):
            self.assertEqual(later["messages"][: len(earlier["messages"])], earlier["messages"])
        self.assertGreaterEqual(model.calls[0]["max_tokens"], 64_000)

    async def test_only_read_tools_the_person_holds_are_offered(self):
        model = ScriptedModel(_message("end_turn", _text("Nothing to do.")))
        await _agent(self.fx, model).run(_command("Make a chart of fees"))
        offered = {tool["name"] for tool in json.loads(model.calls[0]["tools"])}
        self.assertIn("find_low_attendance", offered)
        self.assertIn(RUN_PYTHON, offered)
        for tool in ("send_email", "generate_report", "create_notification", "update_student_record"):
            self.assertNotIn(tool, offered)
        staff = ScriptedModel(_message("end_turn", _text("Nothing to do.")))
        await _agent(self.fx, staff).run(_command("Make a chart", role=PrincipalType.STAFF))
        self.assertNotIn("find_low_attendance", {tool["name"] for tool in json.loads(staff.calls[0]["tools"])})

    async def test_a_tool_outside_its_list_is_refused_back_to_the_model(self):
        def check(messages):
            results = _last_results(messages)
            self.assertEqual(results[0]["status"], "unknown_tool")
            self.assertTrue(messages[-1]["content"][0]["is_error"])
            return _message("end_turn", _text("I could not send the email."))

        model = ScriptedModel(_message("tool_use", _tool("tu_1", "send_email", {"recipients": ["x@abc.edu.in"], "subject": "s", "body": "b"})), check)
        response = await _agent(self.fx, model).run(_command("Make a chart and email it"))
        self.assertEqual(response.steps[0].status, "unknown_tool")
        self.assertEqual(self.fx.email_sender.sent, [])

    async def test_people_without_reports_permission_are_refused(self):
        model = ScriptedModel()
        response = await _agent(self.fx, model).run(_command("Make a presentation", role=PrincipalType.STUDENT))
        self.assertEqual(response.status, "refused")
        self.assertEqual(model.calls, [])

    async def test_the_daily_limit_is_per_person(self):
        agent = _agent(self.fx, ScriptedModel(_message("end_turn", _text("Done.")), _message("end_turn", _text("Done."))), per_person_per_day=1)
        first = await agent.run(_command("Make a chart"))
        second = await agent.run(_command("Make another chart"))
        self.assertEqual(first.status, "complete")
        self.assertEqual(second.status, "refused")
        self.assertIn("today's 1 open tasks", second.answer)

    async def test_an_unavailable_model_or_a_refusal_creates_nothing(self):
        error = GuruJiError(PublicError(ErrorCode.SERVICE_UNAVAILABLE, "down", "provider"))
        failed = await _agent(self.fx, ScriptedModel(error)).run(_command("Make a chart"))
        self.assertEqual(failed.status, "failed")
        self.assertIn("Nothing was created", failed.answer)
        declined = await _agent(self.fx, ScriptedModel(_message("refusal"))).run(_command("Make a chart"))
        self.assertEqual(declined.status, "refused")
        self.assertEqual(self.fx.reports.list(principal(PrincipalType.PRINCIPAL), "college_a"), [])

    async def test_the_turn_limit_ends_the_task_and_keeps_what_was_made(self):
        code = "open('outputs/partial.csv', 'w').write('a\\n1\\n')"
        steps = [_message("tool_use", _tool(f"tu_{index}", RUN_PYTHON, {"code": code})) for index in range(3)]
        model = ScriptedModel(*steps)
        response = await _agent(self.fx, model, max_turns=3).run(_command("Make a chart"))
        self.assertEqual(response.status, "partial")
        self.assertEqual(len(model.calls), 3)
        self.assertEqual(response.artifacts[0]["file_name"], "partial.csv")
        budget = model.calls[2]["messages"][-1]["content"][-1]
        self.assertEqual(budget["type"], "text")
        self.assertIn("Finish now", budget["text"])

    @unittest.skipUnless("pptx" in available_libraries(), "python-pptx is not installed (the open-task extra)")
    async def test_builds_a_real_presentation(self):
        code = (
            "from pptx import Presentation\n"
            "deck = Presentation()\n"
            "slide = deck.slides.add_slide(deck.slide_layouts[1])\n"
            "slide.shapes.title.text = 'Attendance'\n"
            "slide.placeholders[1].text = '2 MBA students below 75%'\n"
            "deck.save('outputs/attendance.pptx')\n"
        )
        model = ScriptedModel(_message("tool_use", _tool("tu_1", RUN_PYTHON, {"code": code})), _message("end_turn", _text("Deck ready.")))
        response = await _agent(self.fx, model).run(_command("Make a PPT on attendance"))
        self.assertEqual(response.status, "complete", response.answer)
        self.assertEqual(response.artifacts[0]["format"], "pptx")
        self.assertTrue(response.artifacts[0]["content_type"].endswith("presentationml.presentation"))


class MasterHandoffTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fx = PlatformFixture()

    async def test_the_master_agent_hands_over_only_what_its_tools_cannot_do(self):
        model = _attendance_task()
        self.fx.agent.open_task = _agent(self.fx, model)
        handled = await self.fx.agent.handle(_command("Make a presentation of MBA students below 75% attendance"))
        self.assertEqual(handled.intent, "open_task")
        self.assertEqual(handled.status, "complete")
        self.assertEqual(len(model.calls), 3)
        runs = self.fx.store.list_agent_runs("college_a")
        self.assertEqual((runs[0]["status"], runs[0]["tool_names"]), ("complete", ["find_low_attendance", RUN_PYTHON]))
        direct = await self.fx.agent.handle(_command("How many MBA students have attendance below 75%?"))
        self.assertEqual(direct.intent, "low_attendance")
        self.assertEqual(len(model.calls), 3, "the planner's own tools answered; Claude was not called")

    async def test_without_the_agent_nothing_changes(self):
        response = await self.fx.agent.handle(_command("Make a presentation of MBA students below 75% attendance"))
        self.assertNotEqual(response.intent, "open_task")

    async def test_long_tasks_run_as_a_job_and_the_job_runs_them_once(self):
        queued = []

        class Queue:
            backend_name = "thread"

            def enqueue(self, institution_id, kind, payload):
                queued.append(payload)
                return "job-1"

        model = _attendance_task()
        agent = _agent(self.fx, model)
        agent.background = True
        self.fx.agent.open_task = agent
        self.fx.agent.background = Queue()
        accepted = await self.fx.agent.handle(_command("Make a presentation of MBA students below 75% attendance"))
        self.assertEqual((accepted.status, accepted.job_id), ("accepted", "job-1"))
        self.assertEqual(model.calls, [])
        self.assertEqual(len(queued), 1)
        worked = await self.fx.agent.handle(_command("Make a presentation of MBA students below 75% attendance", in_background=True))
        self.assertEqual(worked.status, "complete")
        self.assertEqual(len(queued), 1, "a command already running as a job is never queued again")

    async def test_a_student_asking_for_a_file_is_told_why_not(self):
        self.fx.agent.open_task = _agent(self.fx, ScriptedModel())
        response = await self.fx.agent.handle(_command("Make a presentation of my attendance", role=PrincipalType.STUDENT))
        self.assertEqual((response.intent, response.status), ("open_task", "refused"))
        self.assertIn("reports:generate", response.answer)


class ProviderToolTurnTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_turn_streams_one_turn_with_effort_and_caching(self):
        final = _message("end_turn", _text("done"))
        requests = []

        class Stream:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get_final_message(self):
                return final

        class Messages:
            def stream(self, **request):
                requests.append(request)
                return Stream()

        provider = AnthropicProvider(model_id=OPEN_TASK_MODEL_ID, effort="high", timeout_seconds=30, client=SimpleNamespace(beta=SimpleNamespace(messages=Messages())))
        tools = [{"name": "run_python", "description": "d", "input_schema": {"type": "object"}}]
        message = await provider.tool_turn(system="sys", tools=tools, messages=[{"role": "user", "content": "hi"}], max_tokens=64_000)
        self.assertIs(message, final)
        request = requests[0]
        self.assertEqual(request["model"], "claude-opus-5-5")
        self.assertEqual(request["max_tokens"], 64_000)
        self.assertEqual(request["output_config"], {"effort": "high"})
        self.assertEqual(request["cache_control"], {"type": "ephemeral"})
        self.assertEqual((request["system"], request["tools"]), ("sys", tools))
        # Opus 5.5 rejects disabled thinking and forced tool use; neither is ever sent.
        self.assertNotIn("thinking", request)
        self.assertNotIn("tool_choice", request)

    async def test_bedrock_gets_its_model_prefix(self):
        provider = AnthropicProvider(model_id=OPEN_TASK_MODEL_ID, platform="bedrock", aws_region="ap-south-1")
        self.assertEqual(provider.model_id, "anthropic.claude-opus-5-5")


class OpenTaskSettingsTests(unittest.TestCase):
    def _settings(self, **overrides):
        base = {"open_task_enabled": True, "model_provider": "anthropic", "platform_enabled": True}
        base.update(overrides)
        return AppSettings(**base)

    def test_defaults_are_off_and_point_at_opus_5_5(self):
        settings = AppSettings()
        self.assertFalse(settings.open_task_enabled)
        self.assertEqual(settings.open_task_model_id, "claude-opus-5-5")
        self.assertEqual(settings.open_task_sandbox, "isolated")
        self._settings()._validate_open_task()

    def test_invalid_configurations_are_rejected(self):
        for overrides in ({"model_provider": "deterministic"}, {"platform_enabled": False}, {"open_task_effort": "extreme"}, {"open_task_sandbox": "none"}, {"open_task_max_turns": 0}, {"open_task_memory_mb": 64}):
            with self.assertRaises(ValueError, msg=str(overrides)):
                self._settings(**overrides)._validate_open_task()

    def test_production_accepts_only_the_isolated_sandbox(self):
        settings = self._settings(environment="production", open_task_sandbox="guarded")
        with self.assertRaises(ValueError) as caught:
            settings.ensure_safe_for_production()
        self.assertTrue("GURU_OPEN_TASK_SANDBOX" in str(caught.exception) or "production" in str(caught.exception))

    def test_environment_variables_are_read(self):
        names = {"GURU_OPEN_TASK_ENABLED": "true", "GURU_OPEN_TASK_EFFORT": "xhigh", "GURU_OPEN_TASK_SANDBOX": "auto", "GURU_OPEN_TASK_PER_PERSON_PER_DAY": "3"}
        previous = {name: os.environ.get(name) for name in names}
        os.environ.update(names)
        try:
            settings = AppSettings.from_env()
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
        self.assertEqual((settings.open_task_enabled, settings.open_task_effort, settings.open_task_sandbox, settings.open_task_per_person_per_day), (True, "xhigh", "auto", 3))


if __name__ == "__main__":
    unittest.main()

"""Recorded checkpoint fixtures for the smoke exit contract."""
import tests

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from subprocess import CompletedProcess
import tempfile
import unittest

from foreman.adapters import Call, Outcome
from foreman.adapters.claude import ClaudeAdapter
from foreman.adapters.codex import CodexAdapter
from foreman.adapters.grok import GrokAdapter
from foreman.config import ModelSpec
from foreman.delivery import run_delivery
from foreman.dry_run import ScriptHandler, SCRIPTED_USAGE
from publication_fixture import TOTALS
from skill_fixture import skill_fixture
from foreman.outcomes import Halt
from foreman.readiness import MergeState
from foreman.smoke import SmokeVerdict, smoke_verdict
from foreman.stage_output import Attempt, AttemptUsage, PlanState
from foreman.state import ChecksState, DeliveryResult, RunState, ReviewState
from foreman.workspace import WorkState


def passing() -> DeliveryResult:
    return DeliveryResult(
        run=RunState(repo="bookout-labs/foreman-smoke", issue=1, release="c0ffee" + "0" * 34), plan=PlanState(),
        checks=ChecksState(verify="pass", check_exit_code=0, verified_head="abc"),
        path=["prepare", "implement", "check", "commit", "publish", "review", "merge"], stop="merged",
        work=WorkState(head_sha="abc", published_head="abc"),
        review=ReviewState(reviewed_head="abc", review_receipt={"id": 1, "state": "COMMENTED", "commit_id": "abc"}),
        merge=MergeState(status="merged", receipt={
            "repo": "bookout-labs/foreman-smoke", "pr": 2, "source_head": "abc",
            "base": "base", "base_ref": "main", "method": "SQUASH",
            "queue_policy": {"merge_method": "SQUASH"}, "identity": "bot",
            "status": "AUTO_MERGE_ENABLED",
        }),
        attempts=[Attempt(stage="implement", model="gpt-6.1-sol", commit="pin", methods=[],
                          outcome="ran", usage=AttemptUsage(input=1, output=1, cache_read=0,
                          cache_creation=0, cost_usd=None)).record()],
    )


class SmokeTest(unittest.TestCase):
    def assert_verdict(self, state, code, condition, attempt="1 implement gpt-6.1-sol"):
        self.assertEqual(smoke_verdict(state), SmokeVerdict(code, condition, attempt))

    def test_pass_and_purity(self):
        state = passing()
        before = deepcopy(state)
        self.assert_verdict(state, 0, "pass")
        self.assertEqual(state, before)

    def test_each_completion_predicate(self):
        fixtures = [
            ({"attempts": []}, "attempts.nonempty"),
            ({"stop": "answered"}, "stop=answered"),
            ({"stop": "children_filed"}, "stop=children_filed"),
            ({"stop": "needs_human"}, "stop=needs_human"),
            ({"path": ["review"]}, "terminal.merge"),
            ({"stop": "merge_pending"}, "stop=merge_pending"),
            ({"checks": ChecksState(verified_head="abc")}, "checks.verify"),
            ({"checks": ChecksState(verify="pass", check_exit_code=0)}, "verified_head"),
            ({"work": WorkState()}, "published_head"),
            ({"work": WorkState(head_sha="abc")}, "published_head"),
            ({"review": passing()["review"].model_copy(update={"reviewed_head": ""})}, "reviewed_head"),
            ({"review": passing()["review"].model_copy(update={"review_receipt": None})}, "review_receipt"),
            ({"merge": MergeState(status="armed")}, "merge.receipt"),
        ]
        for update, condition in fixtures:
            with self.subTest(condition=condition, update=update):
                state = passing()
                state.update(update)
                self.assert_verdict(state, 1, condition,
                                    "checkpoint" if "attempts" in update else "1 implement gpt-6.1-sol")
        state = passing()
        for status in ("not_armed", "armed"):
            state["merge"] = state["merge"].model_copy(update={"status": status})
            self.assert_verdict(state, 1, "merge.merged")

    def test_stale_heads(self):
        for field in ("published_head", "reviewed_head", "review_receipt", "merge.receipt", "merge.observed_head"):
            with self.subTest(field=field):
                state = passing()
                if field == "published_head":
                    state["work"] = WorkState(head_sha="old")
                elif field == "reviewed_head":
                    state["review"] = state["review"].model_copy(update={"reviewed_head": "old"})
                elif field == "review_receipt":
                    state["review"] = state["review"].model_copy(update={
                        "review_receipt": {**state["review"].review_receipt, "commit_id": "old"}})
                else:
                    merge = state["merge"]
                    if field == "merge.receipt":
                        state["merge"] = merge.model_copy(update={"receipt": {**merge.receipt, "source_head": "old"}})
                    else:
                        state["merge"] = merge.model_copy(update={"observed_head": "old"})
                self.assert_verdict(state, 1, field)

    def test_unavailable_then_ran(self):
        for outcome in ("quota", "cli_missing"):
            with self.subTest(outcome=outcome):
                state = passing()
                prior = {**state["attempts"][0], "outcome": outcome, "usage": None}
                state["attempts"].insert(0, prior)
                self.assert_verdict(state, 78, f"attempt.outcome={outcome}")
                state["merge"] = MergeState(status="armed")
                self.assert_verdict(state, 1, "merge.receipt", "2 implement gpt-6.1-sol")

    def test_unavailable_final_checkpoint(self):
        for halt in (Halt.env_unavailable, Halt.worker_unavailable):
            with self.subTest(halt=halt):
                state = DeliveryResult(run=RunState(), plan=PlanState(), checks=ChecksState(), review=ReviewState(),
                                      path=["check"], stop=halt, halt=halt)
                self.assert_verdict(state, 78, f"halt={halt}", "checkpoint")
                before = state["path"]
                state["path"] = []
                self.assert_verdict(state, 1, "terminal.merge", "checkpoint")
                state["path"] = before
                state["merge"] = MergeState(status="armed")
                self.assert_verdict(state, 1, "merge.armed_with_halt", "checkpoint")
                state["stop"] = "verified"
                self.assert_verdict(state, 1, "stop=verified", "checkpoint")

    def test_halts_are_not_blanket_unavailability(self):
        for halt in Halt:
            if halt in {Halt.env_unavailable, Halt.worker_unavailable}:
                continue
            with self.subTest(halt=halt):
                state = passing()
                state["halt"] = halt
                state["attempts"].insert(0, {**state["attempts"][0], "outcome": "quota"})
                self.assert_verdict(state, 1, f"halt={halt}", "2 implement gpt-6.1-sol")

    def test_failed_attempts_and_unknown_usage(self):
        for outcome in ("failed", "interrupted", "budget_exhausted"):
            state = passing()
            state["attempts"][0]["outcome"] = outcome
            self.assert_verdict(state, 1, f"attempt.outcome={outcome}")
        for usage in (None, {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0, "cost_usd": None}):
            state = passing()
            state["attempts"][0]["usage"] = usage
            self.assert_verdict(state, 1, "attempt.usage_unknown")

    def test_adapter_identity_is_not_the_requested_model_label(self):
        with tempfile.TemporaryDirectory() as directory:
            call = Call(prompt="", schema="{}", edit=False, temp_dir=Path(directory))
            for adapter, payload, code in (
                (ClaudeAdapter(ModelSpec.parse("claude-opus-5-5-high")),
                 '{"type":"result","subtype":"success","is_error":false,"structured_output":{},'
                 '"modelUsage":{"wrong-model":{"inputTokens":1,"outputTokens":1}}}', 1),
                (CodexAdapter(ModelSpec.parse("gpt-6.1-sol-high")),
                 '{"type":"item.completed","item":{"type":"error",'
                 '"message":"model rerouted: gpt-6.1-sol -> other"}}', 1),
                (GrokAdapter(ModelSpec.parse("grok-4.3-high")),
                 '{"stopReason":"end_turn","structuredOutput":{},'
                 '"modelUsage":{"ledger-alias":{"inputTokens":1,"outputTokens":1}}}', 0),
            ):
                outcome = adapter.read(CompletedProcess([], 0, payload, ""), call)
                state = passing()
                attempt = state["attempts"][0]
                attempt["model"] = adapter.model.name
                attempt["outcome"] = outcome.kind
                attempt["usage"] = (AttemptUsage(input=outcome.usage.input_tokens, output=outcome.usage.output_tokens,
                    cache_read=outcome.usage.cache_read_input_tokens or 0,
                    cache_creation=outcome.usage.cache_creation_input_tokens or 0,
                    cost_usd=outcome.usage.total_cost_usd).record() if outcome.usage else None)
                self.assert_verdict(state, code, "attempt.outcome=failed" if code else "pass",
                                    f"1 implement {adapter.model.name}")

    def test_receipt_contract_and_check_failure(self):
        for update, condition in (
            ({"receipt": {}}, "merge.receipt.invalid"),
            ({"receipt": {**passing()["merge"].receipt, "repo": "other/repo"}}, "merge.receipt.repo"),
            ({"receipt": {**passing()["merge"].receipt, "status": "UNMERGEABLE"}}, "merge.receipt.rejected"),
        ):
            state = passing()
            state["merge"] = state["merge"].model_copy(update=update)
            self.assert_verdict(state, 1, condition)
        state = passing()
        state["checks"] = ChecksState(verify="fail", check_exit_code=1, verified_head="abc")
        self.assert_verdict(state, 1, "checks.verify")
        state = passing()
        state["attempts"][0]["model"] = "claude-opus-5-5-high"
        self.assert_verdict(state, 1, "attempt.usage_unknown", "1 implement claude-opus-5-5-high")

    def test_unavailable_after_dispatch_preserves_contract_precedence(self):
        for halt in (Halt.env_unavailable, Halt.worker_unavailable):
            state = passing()
            state.update(stop=halt, halt=halt, merge=MergeState(), review=ReviewState())
            state["checks"] = ChecksState(check_exit_code=127 if halt == Halt.env_unavailable else None)
            self.assert_verdict(state, 78, f"halt={halt}")
            state["attempts"][0]["usage"] = None
            self.assert_verdict(state, 1, "attempt.usage_unknown")
        state = passing()
        state["attempts"].insert(0, {**state["attempts"][0], "outcome": "quota", "usage": None})
        state["attempts"][1]["usage"] = None
        self.assert_verdict(state, 1, "attempt.usage_unknown", "2 implement gpt-6.1-sol")

    def test_graph_produced_unavailability(self):
        for stage, kind in (("triage", "quota"), ("review", "quota"), ("review", "cli_missing")):
            handler = ScriptHandler()
            def dispatch(node, model, state, instruction):
                return Outcome(kind, usage=SCRIPTED_USAGE if kind == "quota" else None) if node == stage else handler(node, model, state, instruction)
            with self.subTest(stage=stage, kind=kind), skill_fixture() as (config, scratch):
                state = run_delivery(dispatch, repo="bookout-labs/foreman-smoke", request="Add a field",
                                     config=config, scratch=scratch)
                self.assertEqual((state.get("halt"), state["stop"]), ("worker_unavailable", "worker_unavailable"))
                self.assertNotIn("done", state["path"])
                first = next((i, a) for i, a in enumerate(state["attempts"], 1) if a["outcome"] == kind)
                i, a = first
                self.assert_verdict(state, 78, f"attempt.outcome={kind}", f"{i} {a['stage']} {a['model']}")

    def test_a_repaired_invalid_answer_passes_the_attempt_checks(self):
        # The invalid answer's attempt keeps outcome ran, so smoke judges the repaired run as it judges a clean one.
        def run(invalid):
            handler, calls = ScriptHandler(), []

            def dispatch(node, model, state, instruction):
                outcome = replace(handler(node, model, state, instruction), session_id="00000000-0000-4000-8000-000000000000", totals=TOTALS)
                calls.append(node)
                return replace(outcome, output={}) if node == "triage" and calls.count("triage") <= invalid else outcome
            with skill_fixture() as (config, scratch):
                return run_delivery(dispatch, repo="bookout-labs/foreman-smoke", request="Add a field", config=config, scratch=scratch)

        clean, repaired, stopped = run(0), run(1), run(2)
        self.assertEqual([a["outcome"] for a in repaired["attempts"][:2]], ["ran", "ran"])
        verdict = lambda state: (smoke_verdict(state).exit_code, smoke_verdict(state).condition)
        self.assertEqual(verdict(repaired), verdict(clean))
        self.assertEqual(verdict(stopped), (1, "halt=invalid_output"))

    def test_ran_identity_and_fallback(self):
        state = passing()
        state["attempts"][0]["model"] = None
        self.assert_verdict(state, 1, "attempt.model_missing", "1 implement no model")
        for prior, reason, code in ((False, "no_other_family_configured", 1),
                                    (False, "preceding_candidates_unavailable", 1),
                                    (True, "no_other_family_configured", 1),
                                    (True, "preceding_candidates_unavailable", 78)):
            state = passing()
            if prior:
                state["attempts"].insert(0, {**state["attempts"][0], "outcome": "quota"})
            state["attempts"][-1]["provenance"] = dict(reviewer="gpt-6.1-sol", contributors=["gpt-6.1-sol"],
                                                       same_family=True, reason=reason)
            self.assert_verdict(state, code, "attempt.outcome=quota" if code == 78 else "attempt.fallback",
                                "1 implement gpt-6.1-sol" if code == 78 or not prior else "2 implement gpt-6.1-sol")
        state["attempts"][-1]["provenance"]["same_family"] = False
        state["attempts"][-1]["provenance"]["reason"] = None
        state["attempts"].pop(0)
        self.assert_verdict(state, 0, "pass")

    def test_usage_fields_independently(self):
        for key in ("input", "output", "cache_read", "cache_creation"):
            for cost in (0.0, 0.25):
                state = passing()
                state["attempts"][0]["model"] = "claude-opus-5-5-high"
                state["attempts"][0]["usage"] = dict(input=0, output=0, cache_read=0, cache_creation=0, cost_usd=cost)
                state["attempts"][0]["usage"][key] = 1
                self.assert_verdict(state, 0, "pass", "1 implement claude-opus-5-5-high")

    def test_unavailable_precedence_and_round_two_heads(self):
        for halt in (None, "paused"):
            state = passing()
            state["attempts"].insert(0, {**state["attempts"][0], "outcome": "quota"})
            state["attempts"].insert(1, {**state["attempts"][0], "outcome": "cli_missing"})
            if halt:
                state.update(halt=Halt.worker_unavailable, stop=Halt.worker_unavailable, review=ReviewState(reviewed_head="old",
                             review_receipt={"id": 1, "state": "COMMENTED", "commit_id": "old"}))
                state["work"] = WorkState(head_sha="old")
                state["merge"] = state["merge"].model_copy(update={"status": "not_armed",
                    "receipt": {**state["merge"].receipt, "source_head": "old"}})
            self.assert_verdict(state, 78, "attempt.outcome=quota")
            for verify, exit_code in (("fail", 0), ("pass", 1), ("", 0), ("pass", None)):
                broken = deepcopy(state)
                broken["checks"] = ChecksState(verify=verify, check_exit_code=exit_code, verified_head="abc")
                expected = 1 if halt is None or verify == "fail" or exit_code == 1 else 78
                self.assert_verdict(broken, expected, "checks.verify" if expected == 1 else "attempt.outcome=quota",
                                    "3 implement gpt-6.1-sol" if expected == 1 else "1 implement gpt-6.1-sol")


SERVE = ("python3 -m http.server {port} --bind 127.0.0.1 > /dev/null 2>&1 & echo $! > server.pid; "
         "python3 -c \"import time, urllib.request\nfor _ in range(50):\n  try: urllib.request.urlopen('http://127.0.0.1:{port}/'); break\n"
         "  except OSError: time.sleep(0.1)\"")
CHECK = "python3 -c \"import urllib.request; assert b'items.txt' in urllib.request.urlopen('http://127.0.0.1:{port}/').read()\""


class LocalServerCheckTest(unittest.TestCase):
    """IM10: a check that needs a local server passes when the builder starts the server in the attempt container through
    Foreman's exec tool and runs the check there, each in its own tool call."""

    def drive(self, host, container: str, checkout: Path, port: int) -> list[dict[str, object]]:
        import io
        import json
        from unittest.mock import patch
        from foreman import exec_tool
        requests = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                    *({"jsonrpc": "2.0", "id": 3 + n, "method": "tools/call",
                       "params": {"name": "exec", "arguments": {"command": command.format(port=port)}}} for n, command in enumerate((SERVE, CHECK)))]
        out = io.StringIO()
        with patch("sys.stdin", io.StringIO("".join(json.dumps(request) + "\n" for request in requests))), patch("sys.stdout", out):
            exec_tool.main([container, str(checkout), "60"], host)
        return [json.loads(line) for line in out.getvalue().splitlines()]

    def assert_check_passed(self, answers: list[dict[str, object]]) -> None:
        self.assertEqual([answer["id"] for answer in answers], [1, 2, 3, 4])
        self.assertEqual([tool["name"] for tool in answers[1]["result"]["tools"]], ["exec"])
        started, checked = answers[2]["result"], answers[3]["result"]
        self.assertEqual((started["isError"], checked["isError"]), (False, False), checked)
        self.assertTrue(checked["content"][0]["text"].endswith("The command exited with 0."))

    def test_the_check_reaches_a_server_an_earlier_exec_call_started(self):
        from container_double import HostContainers
        from foreman.container import ContainerSpec
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory)
            (checkout / "items.txt").write_text("one\n")
            host = HostContainers()
            name = host.create(ContainerSpec("run", "image", checkout, ()), 60)
            try:
                self.assert_check_passed(self.drive(host, name, checkout, 18765))
            finally:
                if (checkout / "server.pid").exists():
                    host.run(name, "kill $(cat server.pid)", cwd=checkout, log=None, timeout=10)

    @unittest.skipUnless(__import__("os").environ.get("FOREMAN_DOCKER_TESTS"), "set FOREMAN_DOCKER_TESTS to a local image with python3")
    def test_in_the_real_container_with_loopback_only(self):
        import os
        import subprocess
        from foreman.container import DockerHost
        from foreman.contract import CONTRACT_PATH
        from foreman.workspace import LiveWorkspace
        # Colima shares the home directory, so the checkout sits in this one.
        root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix=".scratch.server-", dir=Path(__file__).resolve().parents[1]))).resolve()
        (root / CONTRACT_PATH).parent.mkdir()
        (root / CONTRACT_PATH).write_text(f"check: 'true'\nverify_skill: SKILL.md\nimage: {os.environ['FOREMAN_DOCKER_TESTS']}\n"
                                          "secrets: []\nnetwork: setup\nsetup: []\n")
        (root / "items.txt").write_text("one\n")
        for args in (("init", "-qb", "main"), ("add", "--all"), ("-c", "user.name=T", "-c", "user.email=t@example.test", "commit", "-qm", "Base")):
            subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
        workspace = LiveWorkspace(root, containers=DockerHost())
        self.addCleanup(workspace.remove_container)
        base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
        name, seconds = workspace.container_route(base)
        self.assertEqual(seconds, 1800)
        self.assert_check_passed(self.drive(DockerHost(), name, root, 8000))
        # No host login or credential is inside the container: the CLI stays on the host and reaches it only through exec.
        from foreman import exec_tool
        home = Path.home()
        probe = " && ".join([*(f"test ! -e {home / path}" for path in (".gitconfig", ".codex", ".claude", ".ssh", ".config/gh")),
                             "! env | grep -i -E 'token|secret|ssh_auth|^gh_|github|anthropic|openai|foreman_app'"])
        self.assertEqual(exec_tool.run(DockerHost(), name, root, 60, (), probe), ("\nThe command exited with 0.", False))

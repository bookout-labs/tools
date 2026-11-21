"""Recorded checkpoint fixtures for the smoke exit contract."""
import tests

from copy import deepcopy
from pathlib import Path
from subprocess import CompletedProcess
import tempfile
import unittest

from foreman.adapters import Call
from foreman.adapters.claude import ClaudeAdapter
from foreman.adapters.codex import CodexAdapter
from foreman.adapters.grok import GrokAdapter
from foreman.config import ModelSpec
from foreman.outcomes import Halt
from foreman.readiness import MergeState
from foreman.smoke import SmokeVerdict, smoke_verdict
from foreman.stage_output import Attempt, AttemptUsage, PlanState
from foreman.state import ChecksState, DeliveryResult, RunState
from foreman.workspace import WorkState


def passing() -> DeliveryResult:
    return DeliveryResult(
        run=RunState(repo="bookout-labs/foreman-smoke", issue=1), plan=PlanState(),
        checks=ChecksState(verify="pass", check_exit_code=0, verified_head="abc"),
        path=["implement", "publish", "verify", "review", "merge", "done"], stop="verified",
        work=WorkState(head_sha="abc"), reviewed_head="abc",
        review_receipt={"id": 1, "state": "COMMENTED", "commit_id": "abc"},
        merge=MergeState(status="armed", receipt={
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
            ({"stop": "sliced"}, "stop=sliced"),
            ({"stop": "needs_human"}, "stop=needs_human"),
            ({"path": ["merge"]}, "terminal.done"),
            ({"checks": ChecksState(verified_head="abc")}, "checks.verify"),
            ({"checks": ChecksState(verify="pass", check_exit_code=0)}, "verified_head"),
            ({"work": WorkState()}, "published_head"),
            ({"path": ["verify", "done"]}, "published_head"),
            ({"reviewed_head": ""}, "reviewed_head"),
            ({"review_receipt": None}, "review_receipt"),
            ({"merge": MergeState(status="armed")}, "merge.receipt"),
        ]
        for update, condition in fixtures:
            with self.subTest(condition=condition, update=update):
                state = passing()
                state.update(update)
                self.assert_verdict(state, 1, condition,
                                    "checkpoint" if "attempts" in update else "1 implement gpt-6.1-sol")
        state = passing()
        state["merge"] = state["merge"].model_copy(update={"status": "not_armed"})
        self.assert_verdict(state, 1, "merge.armed")

    def test_stale_heads(self):
        for field in ("published_head", "reviewed_head", "review_receipt.head", "merge.receipt.head", "merge.observed_head"):
            with self.subTest(field=field):
                state = passing()
                if field == "published_head":
                    state["work"] = WorkState(head_sha="old")
                elif field == "reviewed_head":
                    state["reviewed_head"] = "old"
                elif field == "review_receipt.head":
                    state["review_receipt"]["commit_id"] = "old"
                else:
                    merge = state["merge"]
                    if field == "merge.receipt.head":
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
        for halt in (Halt.cli_missing, Halt.env_unavailable, Halt.quota_exhausted):
            with self.subTest(halt=halt):
                state = DeliveryResult(run=RunState(), plan=PlanState(), checks=ChecksState(),
                                      path=["done"], stop=halt, halt=halt)
                self.assert_verdict(state, 78, f"halt={halt}", "checkpoint")
                state["merge"] = MergeState(status="armed")
                self.assert_verdict(state, 1, "merge.armed_with_halt", "checkpoint")
                state["stop"] = "verified"
                self.assert_verdict(state, 1, "stop=verified", "checkpoint")

    def test_halts_are_not_blanket_unavailability(self):
        for halt in Halt:
            if halt in {Halt.cli_missing, Halt.env_unavailable, Halt.quota_exhausted}:
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
                self.assertEqual(smoke_verdict(state).exit_code, code)
                if code == 1:
                    self.assertEqual(smoke_verdict(state).condition, "attempt.outcome=failed")

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
        for halt in (Halt.cli_missing, Halt.env_unavailable, Halt.quota_exhausted):
            state = passing()
            state.update(stop=halt, halt=halt, merge=MergeState(), reviewed_head="", review_receipt=None)
            state["checks"] = ChecksState(check_exit_code=127 if halt == Halt.env_unavailable else None)
            self.assert_verdict(state, 78, f"halt={halt}")
            state["attempts"][0]["usage"] = None
            self.assert_verdict(state, 1, "attempt.usage_unknown")
        state = passing()
        state["attempts"].insert(0, {**state["attempts"][0], "outcome": "quota", "usage": None})
        state["attempts"][1]["usage"] = None
        self.assert_verdict(state, 1, "attempt.usage_unknown", "2 implement gpt-6.1-sol")

import tests

import json
import unittest
from unittest.mock import Mock

from foreman.adapters import Outcome, Usage
from foreman.config import load_config
from foreman.github import PublishedPullRequest
from foreman.readiness import Escape, MergeState
from foreman.scorers import SCORERS, score_run
from foreman.state import ChecksState, PlanState, ReviewState, RunState
from foreman.status import status_of
from foreman.steps import StageRun, implement
from foreman.workspace import WorkState, WorktreeState

MERGE = "c" * 40
SLICE = {"title": "Add a field", "kind": "feature", "stateful": False, "non_goals": [], "removed_tests": [], "files": [], "screen": "",
         "properties": [], "criteria": [{"text": "The response includes the field.", "proof": {"kind": "drive", "not_drivable": None}}]}


def finished(**fields):
    """A run that ended merged after a Claude builder and a GPT reviewer did its work."""
    return {"run": RunState(repo="Example/Engine", issue=8, base_sha="b" * 40, request="Add a field"), "plan": PlanState(),
            "checks": ChecksState(verified_head="a" * 40), "review": ReviewState(), "path": ["merge"], "stop": "merged",
            "merge": MergeState(status="merged", merge_commit_sha=MERGE),
            "models_used": {"implement": "claude-opus-5-5-high", "review": "gpt-6.1-sol-high"}, **fields}


class Asked:
    """Each fresh session a scorer makes: its model, instruction and schema, answered from the script by model family."""

    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def __call__(self, model, instruction, schema):
        self.calls.append((model.name, instruction, json.loads(schema)))
        return self.answers[model.family](instruction)


def answer(label, cost=0.25):
    return lambda instruction: Outcome("ran", {"label": label, "reason": f"Judged {label}."},
                                       usage=Usage(input_tokens=10, output_tokens=5, total_cost_usd=cost))


class ScorerTest(unittest.TestCase):
    def test_SR1_each_scorer_scores_once_from_a_family_that_did_not_do_the_work_and_stores_label_reason_and_spend(self):
        asked = Asked({"gpt": lambda i: answer("compliant")(i) if "deliver" in i else answer("vague")(i),
                       "claude": lambda i: answer("actionable", 0.5)(i)})
        result = finished()
        scores = score_run(result, load_config(), asked)
        self.assertEqual([(score["scorer"], score["label"], score["model"], score["spend_usd"]) for score in scores],
                         [("task_compliance", "compliant", "gpt-6.1-sol-high", "0.25"),
                          ("review_quality", "actionable", "claude-opus-5-5-high", "0.5")])
        self.assertTrue(all(score["reason"] for score in scores))
        # Each scorer's instruction is its drawn question, and its schema allows only its labels.
        for (_, instruction, schema), scorer in zip(asked.calls, SCORERS.values()):
            self.assertIn(scorer.question, instruction)
            self.assertEqual(schema["properties"]["label"]["enum"], list(scorer.labels))
        # Scored once: the run is due nothing more, and the run's result is unchanged.
        self.assertEqual(score_run({**result, "scores": scores}, load_config(), asked), [])
        self.assertEqual((result["stop"], len(asked.calls)), ("merged", 2))
        # The run report lists each scorer's spend apart from the run's budget.
        text = status_of({**result, "scores": scores}, "merge", None).text
        self.assertIn("Score: task_compliance compliant by gpt-6.1-sol-high. Scorer spend: $0.25, outside the run's budget.", text)

    def test_SR2_a_later_escape_scores_the_review_again_once_for_each(self):
        asked = Asked({"gpt": answer("compliant"), "claude": answer("vague")})
        scores = score_run(finished(), load_config(), asked)
        escapes = [Escape(kind="hand_fix", key=key, evidence="Fixed by hand.") for key in ("e" * 40, "comment 3")]
        merge = MergeState(status="merged", merge_commit_sha=MERGE, escapes=escapes)
        again = score_run(finished(merge=merge, scores=scores), load_config(), asked)
        self.assertEqual([(score["scorer"], score["escapes"]) for score in again], [("review_quality", 1), ("review_quality", 2)])
        self.assertEqual(score_run(finished(merge=merge, scores=[*scores, *again]), load_config(), asked), [])

    def test_SR2_publish_records_a_persons_commit_on_the_advanced_pull_request_once(self):
        """A person's commit that advanced the run's PR is recorded as a hand fix when publish reads the PR, and the next
        attempt starts on that head."""
        head, moved, branch = "a" * 40, "d" * 40, "8-add-a-field"
        pull = PublishedPullRequest(state="OPEN", number=12, headRefOid=moved, headRefName=branch, baseRefName="main", baseRefOid="b" * 40,
                                    autoMergeRequest=None, body="Closes #8")
        forge = Mock(**{"pull_request_for.return_value": pull, "foreign_commits.return_value": [(moved, "octocat")]})
        workspace = Mock(**{"inspect.return_value": WorktreeState(head, True, branch), "remote_head.return_value": moved,
                            "contains_commit.return_value": False})
        run = StageRun(workspace=workspace, forge=forge, check_log=None, max_rounds=3, dispatch=Mock(), apply=Mock())
        published = {**finished(), "plan": PlanState(slice=SLICE),
                     "work": WorkState(branch=branch, base="main", attempt_head="b" * 40, head_sha=head)}
        hand_fix = Escape(kind="hand_fix", key=moved, evidence=f"Commit {moved} by octocat on the pull request's branch.")
        for recorded in ([], [hand_fix]):
            with self.subTest(already_recorded=bool(recorded)):
                update = implement.publish({**published, "merge": MergeState(escapes=recorded)}, run)
                self.assertEqual((update["work"].repair_head, update["merge"].escapes), (moved, [hand_fix]))
        forge.foreign_commits.assert_called_with("Example/Engine", head, moved)

    def test_SR3_with_no_available_model_the_score_is_unknown(self):
        asked = Asked({family: lambda i: Outcome("quota", detail="out of quota") for family in ("gpt", "claude")})
        result = finished()
        scores = score_run(result, load_config(), asked)
        self.assertEqual([(score["label"], score["model"]) for score in scores], [(None, ""), (None, "")])
        self.assertEqual(result["stop"], "merged")
        self.assertIn("Score: review_quality unknown.", status_of({**result, "scores": scores}, "merge", None).text)

    def test_a_scorer_with_no_work_to_judge_does_not_score(self):
        self.assertEqual(score_run(finished(models_used={}), load_config(), Asked({})), [])

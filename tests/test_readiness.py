import tests

import json
import unittest
from typing import get_args
from unittest.mock import Mock, patch

from foreman.readiness import Observation, Outcome, PullSnapshot
from foreman.github import CLOSE_MARKER, ForgeError, IssueComment, LiveForge, PublishedPullRequest, PullRequest
from foreman.state import ChecksState, PlanState, ReviewState, RunState, WorkState
from foreman.steps import CANCELLATION, TOO_LATE, StageRun, merge
from test_github import PR, HEAD, BASE, BRANCH, REPO

MERGE = "c" * 40
READINESS = dict(number=12, state="open", draft=False, head=dict(sha=HEAD, ref=BRANCH),
                 base=dict(sha=BASE, ref="main"), mergeable=True, merge_commit_sha=MERGE, mergeable_state="behind")
RULES = [dict(type="required_status_checks", parameters=dict(required_status_checks=[dict(context="test", integration_id=15368)]))]

QUEUE_RULE = dict(type="merge_queue", parameters=dict(merge_method="SQUASH"))
RULES.append(QUEUE_RULE)
REVIEWED = {"run": RunState(repo=REPO, issue=8), "checks": ChecksState(verified_head=HEAD), "plan": PlanState(), "path": [],
            "work": WorkState(branch=BRANCH, base="main"), "dispatch_plan": {"target": {"number": 12}},
            "review": ReviewState(review_findings=[{"reviewed_head": HEAD, "base": BASE, "findings": [], "provenance": None}],
                                  review_receipt={"id": 1, "state": "COMMENTED", "commit_id": HEAD})}


def published(state):
    return PublishedPullRequest(state=state, number=12, headRefOid=HEAD, headRefName=BRANCH, baseRefName="main", baseRefOid=BASE,
                                autoMergeRequest=None, body="Closes #8")


def stage_run(forge):
    return StageRun(workspace=None, forge=forge, check_log=None, max_rounds=3, human_login="fixture-owner", dispatch=None, apply=None)


class ReadinessTest(unittest.TestCase):
    def test_every_outcome_has_one_typed_arm_route(self):
        """ME1: stale or failing evidence never arms, and each GitHub outcome is a drawn arm route, a wait or a named halt."""
        expected = {"ready": "armed", "pending": "wait", "red": "repair", "conflict": "repair", "queue_rejected": "repair",
                    "policy": "readiness_unavailable", "head_changed": "head_unverified", "closed": "closed_or_merged",
                    "retargeted": "merge_not_armed"}
        self.assertEqual(set(expected), set(get_args(Outcome)))
        state = REVIEWED
        for outcome, route in expected.items():
            with self.subTest(outcome=outcome):
                observed = Observation(repo=REPO, pr=PullSnapshot.model_validate({**READINESS, "merged": False}), checks=[],
                                       outcome=outcome, recovery="Synthetic.", queue_policy={"merge_method": "SQUASH"})
                forge = Mock(**{"variables.return_value": {}, "readiness.return_value": observed, "arm_auto_merge.return_value": None,
                                "issue_comment_records.return_value": [], "pull_request_for.return_value": published("OPEN")})
                run = stage_run(forge)
                with patch("foreman.steps.merge.interrupt", side_effect=LookupError("wait")):
                    try:
                        update = merge.arm(state, run)
                    except LookupError:
                        update = {"stop": "wait"}
                self.assertEqual(update.get("halt") or update["stop"], route)
                if route != "armed":
                    forge.arm_auto_merge.assert_not_called()

    def test_a_stop_cannot_undo_a_merge(self):
        """ME9: arm reads the PR before the stop. A merged PR records the stop as too late and the run ends merged. An open,
        queued PR leaves the queue, closes with the cancellation and ends cancelled, unless GitHub merged it before the close."""
        stop = IssueComment.model_validate({"user": {"login": "fixture-owner"}, "body": "foreman stop\nNot needed."})
        merged = Observation(repo=REPO, pr=PullSnapshot.model_validate({**READINESS, "state": "closed", "merged": True}), checks=[],
                             outcome="ready", recovery="Merged.", queue_policy={"merge_method": "SQUASH"})
        for name, reads, result in (("merged before the stop", ["MERGED"], "merged"), ("queued", ["OPEN", "CLOSED"], "cancelled"),
                                    ("merged before the close", ["OPEN", "MERGED"], "merged")):
            with self.subTest(name):
                forge = Mock(**{"issue_comment_records.return_value": [stop], "readiness.return_value": merged, "blocking.return_value": [],
                                "pull_request_for.side_effect": [published(state) for state in reads]})
                with patch("foreman.steps.merge.interrupt", side_effect=AssertionError("A stop never waits.")):
                    ended = merge.compile_stage(stage_run(forge)).invoke(REVIEWED)
                self.assertEqual((ended["stop"], ended.get("halt")), (result, None))
                self.assertEqual(ended.get("halt_detail"), TOO_LATE if result == "merged" else None)
                self.assertEqual(forge.close_pull_request.call_args_list,
                                 [] if reads == ["MERGED"] else [((REPO, 12, CANCELLATION),)])
                forge.arm_auto_merge.assert_not_called()

    def test_the_close_leaves_the_queue_comments_and_keeps_the_branch(self):
        """The close takes a queued PR out of the merge queue, posts the cancellation once and closes the PR, leaving its branch."""
        for queued in (True, False):
            with self.subTest(queued=queued):
                commands = []
                def reply(repo, command, **kwargs):
                    commands.append(command)
                    if command[2] == "graphql" and "dequeuePullRequest" not in command[4]:
                        return json.dumps({"id": "PR_12", "mergeQueueEntry": {"id": "queue-12", "state": "QUEUED"} if queued else None})
                    return json.dumps([[]] if command[2].endswith("comments?per_page=100") else {"state": "closed"})
                with patch.object(LiveForge, "_run_command", side_effect=reply):
                    LiveForge().close_pull_request(REPO, 12, CANCELLATION)
                dequeue = [["gh", "api", "graphql", "-f", "query=mutation($id:ID!) { dequeuePullRequest(input:{id:$id}) { clientMutationId } }",
                            "-f", "id=PR_12"]] if queued else []
                self.assertEqual(commands[1:], [*dequeue,
                    ["gh", "api", f"repos/{REPO}/issues/12/comments?per_page=100", "--paginate", "--slurp"],
                    ["gh", "api", f"repos/{REPO}/issues/12/comments", "--method", "POST", "-f", f"body={CANCELLATION}\n\n{CLOSE_MARKER}"],
                    ["gh", "api", f"repos/{REPO}/pulls/12", "--method", "PATCH", "-f", "state=closed"]])
                self.assertFalse([command for command in commands if "DELETE" in command or "refs/heads" in " ".join(command)])

    def observe(self, *, pr=None, runs=None, rules=None, statuses=None, parents=None, after=None, legacy=None, tip=BASE):
        pr = pr or READINESS
        def reply(repo, command, **kwargs):
            self.assertEqual(repo, REPO)
            url = command[2]
            value = (rules if rules is not None else RULES) if "/rules/" in url else (
                [dict(check_runs=runs or [])] if "/check-runs" in url else (
                [statuses or []] if "/statuses" in url else (
                dict(parents=[dict(sha=s) for s in (parents or [HEAD, BASE])]) if "/commits/" in url else pr)))
            if "/pulls/" in url and after and reply.reads:
                value = after
            if "/pulls/" in url:
                reply.reads += 1
            if "/branches/" in url and "/rules/" not in url:
                value = dict(commit=dict(sha=tip), protection=dict(required_status_checks=legacy or dict(checks=[], contexts=[])))
            return json.dumps(value)
        reply.reads = 0
        with patch.object(LiveForge, "default_branch", return_value="main"), patch.object(LiveForge, "_view_pr", return_value=PullRequest.model_validate(PR)), patch.object(LiveForge, "_run_command", side_effect=reply):
            return LiveForge().readiness(REPO, 8, BRANCH, HEAD)

    def run_check(self, **changes):
        return dict(id=10, name="test", head_sha=HEAD, app=dict(id=15368), check_suite=dict(id=30), status="completed", conclusion="success", **changes)

    def test_source_and_synthetic_merge_success(self):
        for sha in [HEAD, MERGE]:
            run = self.run_check()
            run["head_sha"] = sha
            observed = self.observe(runs=[run])
            self.assertEqual(observed.outcome, "ready")
            self.assertEqual(observed.checks[0].source_commit, sha)
            self.assertEqual(observed.checks[0].tested_commit, sha if sha == MERGE else None)
            self.assertEqual(observed.checks[0].suite, 30)
            self.assertEqual(observed.checks[0].run, 10)

    def test_missing_wrong_app_wrong_head_and_unrelated_merge(self):
        for changes in [None, dict(app=dict(id=99)), dict(head_sha="d" * 40)]:
            run = self.run_check()
            if changes:
                run.update(changes)
            self.assertEqual(self.observe(runs=[run] if changes else []).outcome, "pending")
        run = self.run_check()
        run["head_sha"] = MERGE
        self.assertEqual(self.observe(runs=[run], parents=[HEAD, "d" * 40]).outcome, "pending")

    def test_pending_red_skipped_and_latest_rerun(self):
        for status, conclusion, expected in [("queued", None, "pending"), ("in_progress", None, "pending"),
                ("completed", "failure", "red"), ("completed", "skipped", "red"), ("completed", "neutral", "red")]:
            run = self.run_check()
            run.update(status=status, conclusion=conclusion)
            self.assertEqual(self.observe(runs=[run]).outcome, expected)
        rerun = self.run_check()
        rerun.update(id=11, status="queued", conclusion=None)
        self.assertEqual(self.observe(runs=[self.run_check(), rerun]).outcome, "pending")

    def test_conflict_unknown_policy_and_race(self):
        self.assertEqual(self.observe(pr={**READINESS, "mergeable": False}).outcome, "conflict")
        self.assertEqual(self.observe(pr={**READINESS, "mergeable": None}).outcome, "pending")
        self.assertEqual(self.observe(rules=[]).outcome, "policy")
        self.assertEqual(self.observe(runs=[self.run_check()], after={**READINESS, "draft": True}).outcome, "pending")
        with patch.object(LiveForge, "_view_pr", side_effect=ForgeError("HTTP 503")):
            with self.assertRaises(ForgeError):
                LiveForge().readiness(REPO, 8, BRANCH, HEAD)

    def test_commit_status_requires_explicit_unbound_app_policy(self):
        status = dict(id=20, context="test", state="success")
        self.assertEqual(self.observe(statuses=[status]).outcome, "pending")
        rules = [QUEUE_RULE, dict(type="required_status_checks", parameters=dict(required_status_checks=[dict(context="test")]))]
        self.assertEqual(self.observe(statuses=[status], rules=rules).outcome, "ready")

    def test_pr_state_changes_are_not_transport_errors(self):
        for changes, outcome in [(dict(head=dict(sha="d"*40, ref=BRANCH)), "head_changed"),
                                 (dict(state="closed"), "closed"), (dict(base=dict(sha=BASE, ref="other")), "retargeted")]:
            self.assertEqual(self.observe(pr={**READINESS, **changes}).outcome, outcome)

    def test_moved_base_validates_current_synthetic_merge(self):
        run = {**self.run_check(), "head_sha": MERGE}
        self.assertEqual(self.observe(runs=[run], parents=[HEAD, "d"*40], tip="d"*40).outcome, "ready")

    def test_legacy_context_without_app_identity_fails_closed(self):
        self.assertEqual(self.observe(legacy=dict(checks=[], contexts=["test"])).outcome, "policy")

    def test_ambiguous_app_identity_fails_closed(self):
        rules = [QUEUE_RULE, dict(type="required_status_checks", parameters=dict(required_status_checks=[dict(context="test")]))]
        self.assertEqual(self.observe(runs=[self.run_check(), {**self.run_check(), "app": dict(id=99)}], rules=rules).outcome, "policy")

    def test_superseded_status_success_cannot_hide_failure(self):
        rules = [QUEUE_RULE, dict(type="required_status_checks", parameters=dict(required_status_checks=[dict(context="test")]))]
        self.assertEqual(self.observe(statuses=[dict(id=20, context="test", state="success"),
                                               dict(id=21, context="test", state="failure")], rules=rules).outcome, "red")

    def test_queue_policy_is_observed_once_or_fails_closed(self):
        self.assertEqual(self.observe(runs=[self.run_check()]).queue_policy.merge_method, "SQUASH")
        for rules in [RULES[:1], RULES + [QUEUE_RULE]]:
            self.assertEqual(self.observe(rules=rules).outcome, "policy")

    def test_merged_head_can_reconcile_without_premerge_checks(self):
        self.assertEqual(self.observe(pr={**READINESS, "state": "closed", "merged": True, "mergeable": False}).outcome, "ready")

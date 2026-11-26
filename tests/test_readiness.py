import tests

from contextlib import nullcontext
import json
import unittest
from typing import get_args
from unittest.mock import Mock, patch

from langgraph.checkpoint.sqlite import SqliteSaver

from foreman.readiness import Observation, Outcome, PullSnapshot
from foreman.github import CLOSE_MARKER, CommandFailed, ForgeError, IssueComment, LiveForge, MergeNotArmed, PublishedPullRequest, PullRequest
from foreman.state import ChecksState, PlanState, ReviewState, RunState, StartSettings, WorkState, checkpoint_serializer
from foreman.status import STATUS_MARKER, shows
from foreman.steps import CANCELLATION, TOO_LATE, StageRun, merge
from test_github import PR, HEAD, BASE, BRANCH, REPO
import test_runner
from test_runner import WakeTest

MERGE = "c" * 40
READINESS = dict(number=12, state="open", draft=False, head=dict(sha=HEAD, ref=BRANCH),
                 base=dict(sha=BASE, ref="main"), mergeable=True, merge_commit_sha=MERGE, mergeable_state="behind")
RULES = [dict(type="required_status_checks", parameters=dict(required_status_checks=[dict(context="test", integration_id=15368)]))]

QUEUE_RULE = dict(type="merge_queue", parameters=dict(merge_method="SQUASH"))
RULES.append(QUEUE_RULE)
REVIEWED = {"run": RunState(repo=REPO, issue=8), "start": StartSettings(human_login="fixture-owner", max_tokens=100, max_budget_usd="3"), "checks": ChecksState(verified_head=HEAD), "plan": PlanState(), "path": [],
            "work": WorkState(branch=BRANCH, base="main"), "dispatch_plan": {"target": {"number": 12}},
            "review": ReviewState(review_findings=[{"reviewed_head": HEAD, "base": BASE, "findings": [], "provenance": None}],
                                  review_receipt={"id": 1, "state": "COMMENTED", "commit_id": HEAD})}


def published(state):
    return PublishedPullRequest(state=state, number=12, headRefOid=HEAD, headRefName=BRANCH, baseRefName="main", baseRefOid=BASE,
                                autoMergeRequest=None, body="Closes #8")


class Clock:
    """GitHub's minute, passed by each sleep between the merge step's reads."""

    def __init__(self):
        self.now, self.sleeps = 0.0, []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def check_suite(repo="Example/Engine"):
    return {"action": "completed", "check_suite": {"head_sha": "cd" * 20, "pull_requests": []}, "repository": {"full_name": repo}}


def stage_run(forge):
    return StageRun(workspace=None, forge=forge, check_log=None, max_rounds=3, dispatch=None, apply=None)


class ReadinessTest(unittest.TestCase):
    def test_every_outcome_has_one_typed_arm_route(self):
        """ME1: stale or failing evidence never arms, and each GitHub outcome is a drawn arm route, a wait or a named halt."""
        expected = {"ready": "armed", "pending": "wait", "uncomputed": "readiness_unavailable", "red": "repair", "conflict": "repair", "queue_rejected": "repair",
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
                with patch("foreman.steps.merge.interrupt", side_effect=LookupError("wait")), patch("foreman.steps.merge.time", Clock()):
                    try:
                        update = merge.arm(state, run)
                    except LookupError:
                        update = {"stop": "wait"}
                self.assertEqual(update.get("halt") or update["stop"], route)
                if route != "armed":
                    forge.arm_auto_merge.assert_not_called()

    def test_mergeability_github_has_not_computed_is_read_again_inside_the_step(self):
        """ME12: GitHub first reports mergeability as not yet computed, then that the PR cannot be merged: the step reads again
        without pausing and routes repair with evidence that names a conflict with the base. Not computed for more than a
        minute stops readiness_unavailable. A required check that stays pending pauses arm at once."""
        def observed(outcome):
            return Observation(repo=REPO, pr=PullSnapshot.model_validate({**READINESS, "mergeable": None if outcome == "uncomputed" else False}),
                               checks=[], outcome=outcome, recovery="Synthetic.", queue_policy={"merge_method": "SQUASH"})

        def arm(*outcomes):
            forge = Mock(**{"variables.return_value": {}, "readiness.side_effect": [observed(outcome) for outcome in outcomes],
                            "issue_comment_records.return_value": [], "pull_request_for.return_value": published("OPEN")})
            clock = Clock()
            with patch("foreman.steps.merge.interrupt", side_effect=LookupError("paused")), patch("foreman.steps.merge.time", clock):
                try:
                    update = merge.arm(REVIEWED, stage_run(forge))
                except LookupError:
                    update = {"stop": "paused"}
            forge.arm_auto_merge.assert_not_called()
            return update, forge.readiness.call_count, clock
        repaired, reads, clock = arm("uncomputed", "conflict")
        self.assertEqual((repaired["stop"], reads, clock.sleeps), ("repair", 2, [merge.REREAD_SECONDS]))
        self.assertIsNotNone(repaired["work"].base_merge)
        self.assertIn(": conflict;", repaired["checks"].failure_output)
        rereads = merge.MERGEABILITY_SECONDS // merge.REREAD_SECONDS
        halted, reads, clock = arm(*["uncomputed"] * (rereads + 1))
        self.assertEqual((halted["stop"], halted["halt"], reads, clock.now), ("readiness_unavailable", "readiness_unavailable", rereads + 1,
                                                                              merge.MERGEABILITY_SECONDS))
        self.assertIn(": uncomputed;", halted["halt_detail"])
        paused, reads, clock = arm("pending")
        self.assertEqual((paused["stop"], reads, clock.sleeps), ("paused", 1, []))

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
                                "ready_labeled_at.return_value": None, "pull_request_for.side_effect": [published(state) for state in reads]})
                with patch("foreman.steps.merge.interrupt", side_effect=AssertionError("A stop never waits.")):
                    ended = merge.compile_stage(stage_run(forge)).invoke(REVIEWED)
                self.assertEqual((ended["stop"], ended.get("halt"), ended["path"]), (result, None, ["merge"]))
                self.assertEqual(ended.get("halt_detail"), TOO_LATE if result == "merged" else None)
                self.assertEqual(forge.close_pull_request.call_args_list,
                                 [] if reads == ["MERGED"] else [((REPO, 12, CANCELLATION),)])
                forge.arm_auto_merge.assert_not_called()

    def test_a_cancel_answer_after_github_merged_is_recorded_on_the_merge(self):
        """ME9: a cancel answer at the head_unverified question cannot undo a merge. Merged first, or merged before the close
        landed, the run goes on merged and records the cancellation as too late."""
        merged = Observation(repo=REPO, pr=PullSnapshot.model_validate({**READINESS, "state": "closed", "merged": True}), checks=[],
                             outcome="ready", recovery="Merged.", queue_policy={"merge_method": "SQUASH"})
        cancelled = {**REVIEWED, "plan": PlanState(human_qa=[{"stage": "merge", "question": "Moved.", "answer": "cancel"}])}
        for reads in (["MERGED"], ["OPEN", "MERGED"]):
            with self.subTest(reads=reads):
                forge = Mock(**{"readiness.return_value": merged, "pull_request_for.side_effect": [published(state) for state in reads]})
                ended = merge.observe(cancelled, stage_run(forge))
                self.assertEqual((ended["stop"], ended.get("halt"), ended["halt_detail"]), ("merged", None, TOO_LATE))
                self.assertEqual(forge.close_pull_request.call_count, len(reads) - 1)

    def test_github_reports_the_latest_ready_event_and_each_comment_creation(self):
        """The stop read compares a comment's creation with the issue's latest Ready label event, both as GitHub reports them."""
        events = [[{"event": "labeled", "created_at": "2026-11-20T10:00:00Z", "label": {"name": "Ready", "color": "0e8a16"}},
                   {"event": "unlabeled", "created_at": "2026-11-21T08:00:00Z", "label": {"name": "Ready", "color": "0e8a16"}},
                   {"event": "labeled", "created_at": "2026-11-21T09:00:00Z", "label": {"name": "Ready", "color": "0e8a16"}}],
                  [{"event": "labeled", "created_at": "2026-11-22T09:00:00Z", "label": {"name": "Doing", "color": "1d76db"}},
                   {"event": "closed", "created_at": "2026-11-23T09:00:00Z", "label": None}]]
        for pages, latest in ((events, "2026-11-21T09:00:00+00:00"), ([[]], None)):
            with self.subTest(latest=latest), patch.object(LiveForge, "_run_command", return_value=json.dumps(pages)) as command:
                found = LiveForge().ready_labeled_at(REPO, 8)
                self.assertEqual(found.isoformat() if found else None, latest)
                self.assertEqual(command.call_args.args[1], ["gh", "api", f"repos/{REPO}/issues/8/events?per_page=100", "--paginate", "--slurp"])
        comment = {"id": 3, "body": "foreman stop", "user": {"login": "fixture-owner"}, "created_at": "2026-11-21T09:00:01Z",
                   "updated_at": "2026-11-24T09:00:00Z"}
        with patch.object(LiveForge, "_run_command", return_value=json.dumps([[comment]])):
            [record] = LiveForge().issue_comment_records(REPO, 8)
        self.assertEqual(record.created_at and record.created_at.isoformat(), "2026-11-21T09:00:01+00:00")

    def test_the_close_leaves_the_queue_comments_and_keeps_the_branch(self):
        """The close takes a queued PR out of the merge queue, posts the cancellation once and closes the PR, leaving its branch."""
        for queued in (True, False):
            with self.subTest(queued=queued):
                commands = []
                def reply(repo, command, **kwargs):
                    commands.append(command)
                    if command[2] == "graphql" and "dequeuePullRequest" not in command[4]:
                        return json.dumps({"id": "PR_12", "state": "OPEN",
                                           "mergeQueueEntry": {"id": "queue-12", "state": "QUEUED"} if queued else None})
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

    def test_the_close_leaves_a_pull_request_that_github_merged_first(self):
        """A PR that merged or closed before the close, or merged while it left the queue, gets no comment and no close (DQ1).
        A dequeue that fails on a PR still open fails the close."""
        for name, reads, dequeue_fails, raises in (("merged first", ["MERGED"], False, False), ("closed first", ["CLOSED"], False, False),
                                                   ("merged before the dequeue", ["OPEN", "MERGED"], True, False),
                                                   ("queue unreachable", ["OPEN", "OPEN"], True, True)):
            with self.subTest(name):
                commands, states = [], iter(reads)
                def reply(repo, command, **kwargs):
                    commands.append(command)
                    if "dequeuePullRequest" in command[4]:
                        if dequeue_fails:
                            raise CommandFailed("gh: Pull request is not in the merge queue.", status=1)
                        return "{}"
                    return json.dumps({"id": "PR_12", "state": next(states), "mergeQueueEntry": {"id": "queue-12", "state": "QUEUED"}})
                with patch.object(LiveForge, "_run_command", side_effect=reply):
                    if raises:
                        with self.assertRaises(CommandFailed):
                            LiveForge().close_pull_request(REPO, 12, CANCELLATION)
                    else:
                        LiveForge().close_pull_request(REPO, 12, CANCELLATION)
                self.assertEqual(next(states, None), None)
                self.assertFalse([command for command in commands if "comments" in command[2] or "PATCH" in command])

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
        self.assertEqual(self.observe(pr={**READINESS, "mergeable": None}).outcome, "uncomputed")
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


class MergeWaitTest(WakeTest):
    """ME12 on the runner: mergeability not computed for more than a minute halts, and Ready added again reads the PR again. A
    required check that stays pending pauses arm without halting, and its check_suite delivery wakes the run, which arms."""

    def test_a_pending_check_waits_for_its_delivery_and_uncomputed_mergeability_halts(self):
        replies = [{"triage": "supplied_slice"}, {"implementation": "built"}, test_runner.APPROVE]
        observe, outcome = self.github.readiness, ["uncomputed"]
        read = lambda *args: observe(*args).model_copy(update={"outcome": outcome[0]})
        self.enterContext(patch.object(self.github, "readiness", side_effect=read))
        with patch("foreman.steps.merge.time", Clock()):
            halted = self.walk(replies)
        self.assertEqual((halted["stop"], self.github.merge_calls), ("readiness_unavailable", []))
        outcome[0] = "pending"
        self.github.ready_again(8)
        clock = Clock()
        with patch("foreman.steps.merge.time", clock):
            waiting = self.walk([])
        self.assertEqual((waiting["stop"], waiting.get("halt"), clock.sleeps, self.github.merge_calls), ("merge_pending", None, [], []))
        self.assertEqual(self.deliver("check_suite", check_suite()), "wake Example/Engine#8")
        outcome[0] = "ready"
        code, _, armed = self.wake()
        self.assertEqual((code, armed["merge"].status, len(self.github.merge_calls)), (0, "merged", 1))


class MergeStopWakeTest(WakeTest):
    """ME13: a stop that wakes a run halted in merge or paused at merge's gate reads the PR first, as arm does. A stop after
    the merge is too late and changes nothing else. production_unconfirmed is not built yet (item 20), so its run is left out."""

    def merge_run(self, readiness=None, arm=None):
        """Walk a reviewed run into merge, with GitHub's readiness and arm answers replaced where given."""
        replies = [{"triage": "supplied_slice"}, {"implementation": "built"}, test_runner.APPROVE]
        with (patch.object(self.github, "readiness", side_effect=readiness) if readiness else nullcontext(),
              patch.object(self.github, "arm_auto_merge", side_effect=arm) if arm else nullcontext()):
            return self.walk(replies)

    def pr(self, state):
        opened = self.github.opened[test_runner.BRANCH]
        self.github.opened[test_runner.BRANCH] = opened.model_copy(update={"state": state})

    def moved(self, merged=False):
        """GitHub's readiness with the PR at a head that does not descend from the checked head, or merged at the reviewed head."""
        observe, foreign = self.github.readiness, "c" * 40
        self.github.foreign_heads.add(foreign)

        def read(*args):
            reading = observe(*args)
            if merged:
                return reading.model_copy(update={"pr": reading.pr.model_copy(update={"state": "closed", "merged": True})})
            head = reading.pr.head.model_copy(update={"sha": foreign})
            return reading.model_copy(update={"outcome": "head_changed", "pr": reading.pr.model_copy(update={"head": head})})
        return read

    def status_detail(self, stage=""):
        """The halt detail saved on the run, or on the stage whose namespace starts with stage."""
        with SqliteSaver.from_conn_string(str(self.checkpoint)) as saver:
            saver.serde = checkpoint_serializer()
            saved = next(saved for saved in saver.list({"configurable": {"thread_id": "example/engine#8"}})
                         if saved.config["configurable"]["checkpoint_ns"].startswith(stage))
            return saved.checkpoint["channel_values"].get("halt_detail", "")

    def stop(self):
        self.github.ruling("foreman stop")
        self.assertEqual(self.deliver("issue_comment", test_runner.comment("foreman stop")), "wake Example/Engine#8")
        return self.wake()[2]

    def test_a_stop_after_the_merge_is_too_late(self):
        """A run halted merged_unreviewed, and one paused at the head_unverified gate whose PR has since merged, read the PR,
        record the stop as too late and stay as they were. Each status comment says the stop came after the merge. A second
        delivery of the stop records and says it no more than once."""
        unreviewed = self.github.readiness("Example/Engine", 8, test_runner.BRANCH, HEAD)
        unreviewed.pr.state, unreviewed.pr.merged, unreviewed.pr.head.sha = "closed", True, "c" * 40
        readings = iter([self.github.readiness("Example/Engine", 8, test_runner.BRANCH, HEAD)])
        self.assertEqual(self.merge_run(lambda *args: next(readings, unreviewed))["stop"], "merged_unreviewed")
        self.pr("MERGED")
        self.stop()
        ended = self.stop()
        self.assertEqual((ended["stop"], ended["halt"], self.github.pr_closes), ("merged_unreviewed", "merged_unreviewed", []))
        self.assertEqual((self.status_detail().count(TOO_LATE), self.github.status["Example/Engine", 8].count(TOO_LATE)), (1, 1))
        self.assertTrue(self.status_detail().endswith(TOO_LATE))
        self.assertTrue(shows(self.github, "Example/Engine", 8, "Stopped"))

        self.checkpoint, self.github = self.cwd.parent / "gate.sqlite3", test_runner.FakeGitHub()
        self.assertEqual(self.merge_run(self.moved())["stop"], "needs_human")
        self.pr("MERGED")
        self.stop()
        paused = self.stop()
        self.assertEqual((paused["stop"], self.github.pr_closes), ("needs_human", []))
        self.assertEqual((self.status_detail("merge"), self.github.status["Example/Engine", 8].count(TOO_LATE)), (TOO_LATE, 1))
        self.assertTrue(shows(self.github, "Example/Engine", 8, "Needs answer"))

    def test_a_stop_before_the_merge_ends_the_run_cancelled(self):
        """A run halted merge_held whose PR is open leaves the queue, closes and ends cancelled. One halted merge_not_armed
        whose PR a person closed without merging ends cancelled and closes nothing."""
        self.github.hold = "Holding merges."
        self.assertEqual(self.merge_run()["stop"], "merge_held")
        self.assertEqual((self.stop()["stop"], self.github.pr_closes), ("cancelled", [("Example/Engine", 12, CANCELLATION)]))

        self.checkpoint, self.github = self.cwd.parent / "closed.sqlite3", test_runner.FakeGitHub()
        self.assertEqual(self.merge_run(arm=MergeNotArmed("Auto-merge is off."))["stop"], "merge_not_armed")
        self.pr("CLOSED")
        self.assertEqual((self.stop()["stop"], self.github.pr_closes), ("cancelled", []))

    def test_a_cancel_answered_after_the_merge_lets_observe_go_on(self):
        """A cancel answered at the head_unverified gate after its PR merged closes the question. Observe resumes, reads the
        PR and the run ends merged, not cancelled."""
        self.assertEqual(self.merge_run(self.moved())["stop"], "needs_human")
        self.pr("MERGED")
        self.github.answer(self.question, "cancel")
        self.assertEqual(self.deliver("issue_comment", test_runner.comment(self.github.rulings[-1].body)), "wake Example/Engine#8")
        with patch.object(self.github, "readiness", side_effect=self.moved(merged=True)):
            ended = self.wake()[2]
        self.assertEqual((ended["stop"], ended.get("halt_detail"), self.github.pr_closes),
                         ("merged", TOO_LATE, []))

    def test_a_refused_pull_request_read_changes_nothing(self):
        """GitHub answers a plain 403 to the runner's read of the PR on a run halted merge_held, and on one paused at the
        head_unverified gate. Nothing changes, and each status comment keeps the run's state under a git_failed report that
        says to fix the App's access and what to post again, worded by the run's saved state."""
        self.github.hold = "Holding merges."
        held = self.merge_run()
        self.checkpoint, gate = self.cwd.parent / "gate.sqlite3", self.github
        self.github = test_runner.FakeGitHub()
        paused = self.merge_run(self.moved())
        for checkpoint, github, run, again in ((self.cwd.parent / "runs.sqlite3", gate, held, "Post the stop again."),
                                               (self.checkpoint, self.github, paused, "Post the stop or the answer again.")):
            with self.subTest(run["stop"]):
                self.checkpoint, self.github = checkpoint, github
                shown = self.github.status["Example/Engine", 8]
                with patch.object(self.github, "pull_request_for", side_effect=ForgeError("HTTP 403: Resource not accessible by integration", 403)):
                    unchanged = self.stop()
                self.assertEqual((unchanged["stop"], self.github.pr_closes), (run["stop"], []))
                report = self.github.status["Example/Engine", 8]
                self.assertTrue(report.startswith("Foreman did not continue this run (git_failed).\n\nThe read of the pull request failed "
                                                  "because GitHub answered HTTP 403. Fix the Foreman GitHub App's access to this repository. "
                                                  f"{again}\n\n"), report)
                self.assertIn(shown.removesuffix(STATUS_MARKER).strip(), report)

import tests

import json
import unittest
from unittest.mock import patch

from foreman.github import ForgeError, LiveForge, PullRequest
from test_github import PR, HEAD, BASE, BRANCH, REPO

MERGE = 'c' * 40
READINESS = dict(number=12, state='open', draft=False, head=dict(sha=HEAD, ref=BRANCH),
                 base=dict(sha=BASE, ref='main'), mergeable=True, merge_commit_sha=MERGE, mergeable_state='behind')
RULES = [dict(type='required_status_checks', parameters=dict(required_status_checks=[dict(context='test', integration_id=15368)]))]


class ReadinessTest(unittest.TestCase):
    def observe(self, *, pr=None, runs=None, rules=None, statuses=None, parents=None, after=None):
        pr = pr or READINESS
        def reply(command, **kwargs):
            url = command[2]
            value = (rules if rules is not None else RULES) if '/rules/' in url else (
                [dict(check_runs=runs or [])] if '/check-runs' in url else (
                [statuses or []] if '/statuses' in url else (
                dict(parents=[dict(sha=s) for s in (parents or [HEAD, BASE])]) if '/commits/' in url else pr)))
            if '/pulls/' in url and after and reply.reads:
                value = after
            if '/pulls/' in url:
                reply.reads += 1
            if '/branches/' in url and '/rules/' not in url:
                value = dict(protection=dict(required_status_checks=dict(checks=[], contexts=[])))
            return json.dumps(value)
        reply.reads = 0
        with patch.object(LiveForge, 'review_target', return_value=PullRequest.model_validate(PR)), patch.object(LiveForge, '_run_command', side_effect=reply):
            return LiveForge().readiness(REPO, 8, BRANCH, HEAD)

    def run_check(self, **changes):
        return dict(id=10, name='test', head_sha=HEAD, app=dict(id=15368), status='completed', conclusion='success', **changes)

    def test_source_and_synthetic_merge_success(self):
        for sha in [HEAD, MERGE]:
            run = self.run_check()
            run['head_sha'] = sha
            observed = self.observe(runs=[run])
            self.assertEqual(observed.outcome, 'ready')
            self.assertEqual(observed.checks[0].tested_commit, sha)
            self.assertEqual(observed.checks[0].run, 10)

    def test_missing_wrong_app_wrong_head_and_unrelated_merge(self):
        for changes in [None, dict(app=dict(id=99)), dict(head_sha='d' * 40)]:
            run = self.run_check()
            if changes:
                run.update(changes)
            self.assertEqual(self.observe(runs=[run] if changes else []).outcome, 'pending')
        run = self.run_check()
        run['head_sha'] = MERGE
        self.assertEqual(self.observe(runs=[run], parents=[HEAD, 'd' * 40]).outcome, 'pending')

    def test_pending_red_skipped_and_latest_rerun(self):
        for status, conclusion, expected in [('queued', None, 'pending'), ('in_progress', None, 'pending'),
                ('completed', 'failure', 'red'), ('completed', 'skipped', 'red'), ('completed', 'neutral', 'red')]:
            run = self.run_check()
            run.update(status=status, conclusion=conclusion)
            self.assertEqual(self.observe(runs=[run]).outcome, expected)
        rerun = self.run_check()
        rerun.update(id=11, status='queued', conclusion=None)
        self.assertEqual(self.observe(runs=[self.run_check(), rerun]).outcome, 'pending')

    def test_conflict_unknown_policy_and_race(self):
        self.assertEqual(self.observe(pr={**READINESS, 'mergeable': False}).outcome, 'conflict')
        self.assertEqual(self.observe(pr={**READINESS, 'mergeable': None}).outcome, 'pending')
        self.assertEqual(self.observe(rules=[]).outcome, 'policy')
        self.assertEqual(self.observe(runs=[self.run_check()], after={**READINESS, 'draft': True}).outcome, 'pending')
        with patch.object(LiveForge, 'review_target', side_effect=ForgeError('HTTP 503')):
            with self.assertRaises(ForgeError):
                LiveForge().readiness(REPO, 8, BRANCH, HEAD)

    def test_commit_status_requires_explicit_unbound_app_policy(self):
        status = dict(id=20, context='test', state='success')
        self.assertEqual(self.observe(statuses=[status]).outcome, 'pending')
        rules = [dict(type='required_status_checks', parameters=dict(required_status_checks=[dict(context='test')]))]
        self.assertEqual(self.observe(statuses=[status], rules=rules).outcome, 'ready')

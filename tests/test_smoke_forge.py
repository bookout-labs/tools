"""Smoke transport operations without cleanup policy."""
import tests

import json
import subprocess
import unittest
from unittest.mock import patch

from foreman.credentials import CredentialFailure, COMMAND_TIMEOUT
from foreman.github import CommandFailed, CommandTimedOut, ForgeError, LiveForge

REPO = 'bookout-labs/foreman-smoke'
OUTER = '''query($owner:String!, $name:String!, $endCursor:String) {
 repository(owner:$owner, name:$name) {
  pullRequests(states:OPEN, first:100, after:$endCursor) {
   pageInfo { hasNextPage endCursor }
   nodes { number autoMergeRequest { enabledAt mergeMethod } mergeQueueEntry { id state }
    closingIssuesReferences(first:100) { nodes { number repository { name owner { login } } } pageInfo { hasNextPage endCursor } }
   }
  }
 }
}'''
INNER = '''query($owner:String!, $name:String!, $number:Int!, $endCursor:String) {
 repository(owner:$owner, name:$name) { pullRequest(number:$number) {
  closingIssuesReferences(first:100, after:$endCursor) {
   nodes { number repository { name owner { login } } } pageInfo { hasNextPage endCursor }
  }
 } }
}'''


def connection(nodes, cursor=None):
    return {'nodes': nodes, 'pageInfo': {'hasNextPage': cursor is not None, 'endCursor': cursor}}


def issue(number, **changes):
    return {'state': 'open', 'number': number, 'body': '<!-- smoke:run -->', 'repository_url': f'https://api.github.com/repos/{REPO}',
            'labels': [{'name': 'smoke'}], 'user': {'login': 'bot'}, **changes}


def pull(number, refs):
    return dict(number=number, closingIssuesReferences=refs, autoMergeRequest=None, mergeQueueEntry=None)


def linked(number):
    return {'number': number, 'repository': {'name': 'foreman-smoke', 'owner': {'login': 'bookout-labs'}}}


def graphql(query, inner=False, cursor=None):
    args = ['gh', 'api', 'graphql', '-f', 'owner=bookout-labs', '-f', 'name=foreman-smoke', '-f', f'query={query}']
    if inner:
        args += ['-F', 'number=2']
    args += ['--jq', '.data.repository.' + ('pullRequest.closingIssuesReferences' if inner else 'pullRequests')]
    if cursor is not None:
        args += ['-f', f'endCursor={cursor}']
    return args


def failed(status):
    return subprocess.CompletedProcess([], 1, '', f'gh: failed (HTTP {status})')


class SmokeForgeTest(unittest.TestCase):
    def setUp(self):
        self.environment = patch('foreman.github.credentials.environment', return_value=None)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def transport(self, outputs):
        return patch('foreman.github.subprocess.run', side_effect=[
            output if isinstance(output, (BaseException, subprocess.CompletedProcess)) else
            subprocess.CompletedProcess([], 0, json.dumps(output), '') for output in outputs])

    def assert_commands(self, run, expected):
        commands = [[(' '.join(arg.split()) if arg.startswith('query=') else arg) for arg in call.args[0]]
                    for call in run.call_args_list]
        self.assertEqual(commands, [[(' '.join(arg.split()) if arg.startswith('query=') else arg) for arg in args]
                                    for args in expected])

    def test_issue_pages_exclude_prs_other_repos_and_missing_label(self):
        data = [[issue(1), issue(2, pull_request={}), issue(3, labels=[])],
                [issue(4, repository_url='https://api.github.com/repos/other/repo'),
                 issue(5, body=None, state='closed', user=None, labels=[{'name': 'Smoke'}],
                       repository_url=f'https://api.github.com/repos/{REPO.upper()}'), issue(6, pull_request=None)]]
        with self.transport([data]) as run:
            items = LiveForge().smoke_issues(REPO.upper())
        self.assertEqual([item.number for item in items], [1, 5])
        self.assertEqual(items[-1].state, 'closed')
        self.assertIsNone(items[-1].body)
        self.assertIsNone(items[-1].user)
        self.assert_commands(run, [['gh', 'api', f'repos/{REPO.upper()}/issues?state=all&labels=smoke&per_page=100', '--paginate', '--slurp']])

    def test_empty_listings(self):
        for data in ([], [[]]):
            with self.transport([data]):
                self.assertEqual(LiveForge().smoke_issues(REPO), [])
        with self.transport([connection([])]) as run:
            self.assertEqual(LiveForge().smoke_pull_requests(REPO), [])
        self.assert_commands(run, [graphql(OUTER)])

    def test_pull_and_closing_link_pages_preserve_repo_and_armed_evidence(self):
        first = pull(2, connection([linked(1)], 'next'))
        first['autoMergeRequest'] = {'enabledAt': 'now', 'mergeMethod': 'SQUASH'}
        second = pull(4, connection([]))
        second['mergeQueueEntry'] = {'id': 'queue', 'state': 'QUEUED'}
        with self.transport([connection([first], 'outer'), connection([second]), connection([linked(3)])]) as run:
            items = LiveForge().smoke_pull_requests(REPO)
        self.assertEqual([item.number for item in items], [2, 4])
        self.assertEqual([item.number for item in items[0].closingIssuesReferences.nodes], [1, 3])
        self.assertEqual(items[0].closingIssuesReferences.nodes[0].repository.owner.login, 'bookout-labs')
        self.assertIsNotNone(items[0].autoMergeRequest)
        self.assertIsNotNone(items[1].mergeQueueEntry)
        self.assertFalse(items[0].closingIssuesReferences.pageInfo.hasNextPage)
        self.assert_commands(run, [graphql(OUTER), graphql(OUTER, cursor='outer'), graphql(INNER, True, 'next')])

    def test_label_and_close_repeat_with_exact_commands(self):
        with self.transport([{}, {'state': 'closed'}, {'state': 'closed'}] * 2) as run:
            forge = LiveForge()
            for _ in range(2):
                forge.ensure_smoke_label(REPO)
                forge.close_issue(REPO, 1)
                forge.close_pull_request(REPO, 2)
        self.assert_commands(run, [
            ['gh', 'label', 'create', 'smoke', '--repo', REPO, '--color', 'ededed', '--force'],
            ['gh', 'api', f'repos/{REPO}/issues/1', '--method', 'PATCH', '-f', 'state=closed'],
            ['gh', 'api', f'repos/{REPO}/pulls/2', '--method', 'PATCH', '-f', 'state=closed']] * 2)

    def test_close_reconciles_closed_merged_missing_and_timeout(self):
        for method, kind in (('close_issue', 'issues'), ('close_pull_request', 'pulls')):
            endpoint = f'repos/{REPO}/{kind}/1'
            for env in (None, {'GH_TOKEN': 'app-token'}):
                for error in (failed(404), failed(422), subprocess.TimeoutExpired('gh', 30)):
                    for missing in (False, True):
                        with self.subTest(method=method, env=env, error=error, missing=missing), \
                             patch('foreman.github.credentials.environment', return_value=env), \
                             self.transport([error, failed(404), {}] if missing else [error, {'state': 'closed', 'merged': True}]) as run:
                            self.assertIsNone(getattr(LiveForge(), method)(REPO, 1))
                        commands = [['gh', 'api', endpoint, '--method', 'PATCH', '-f', 'state=closed'], ['gh', 'api', endpoint]]
                        self.assert_commands(run, commands + ([['gh', 'api', f'repos/{REPO}']] if missing else []))

    def test_close_fails_closed_on_open_state_or_masked_repository(self):
        for method in ('close_issue', 'close_pull_request'):
            for env in (None, {'GH_TOKEN': 'app-token'}):
                for outputs in ([failed(422), {'state': 'open'}], [failed(404), failed(404), failed(403)],
                                [failed(404), failed(404), subprocess.TimeoutExpired('gh', 30)],
                                [failed(404), failed(403)], [failed(404), {'state': 'bad'}]):
                    with self.subTest(method=method, outputs=outputs, env=env), \
                         patch('foreman.github.credentials.environment', return_value=env), self.transport(outputs):
                        with self.assertRaises(CommandFailed) as caught:
                            getattr(LiveForge(), method)(REPO, 1)
                        self.assertEqual(caught.exception.status, 422 if outputs[0].stderr.endswith('422)') else 404)
            for output in ({'state': 'open'}, {}, []):
                with self.transport([output]), self.assertRaises(ForgeError):
                    getattr(LiveForge(), method)(REPO, 1)
            with self.transport([subprocess.TimeoutExpired('gh', 30), {'state': 'open'}]), self.assertRaises(CommandTimedOut):
                getattr(LiveForge(), method)(REPO, 1)

    def test_transport_failures_surface_for_every_operation(self):
        for method in ('ensure_smoke_label', 'close_issue', 'close_pull_request', 'smoke_issues', 'smoke_pull_requests'):
            args = [] if method.startswith(('ensure', 'smoke')) else [1]
            for env in (None, {'GH_TOKEN': 'app-token'}):
                for error, expected in ((OSError('offline'), CommandFailed),
                                        (subprocess.TimeoutExpired('gh', 30), CommandTimedOut),
                                        (failed(401), CommandFailed), (failed(403), CommandFailed),
                                        (failed(500), CommandFailed)):
                    outputs = [error]
                    if expected is CommandTimedOut and method.startswith('close'):
                        outputs.append({'state': 'open'})
                    with self.subTest(method=method, error=error, env=env), \
                         patch('foreman.github.credentials.environment', return_value=env), \
                         patch('foreman.github.credentials.invalidate') as invalidate, self.transport(outputs) as run:
                        with self.assertRaises(expected) as caught:
                            getattr(LiveForge(), method)(REPO, *args)
                        if isinstance(error, subprocess.CompletedProcess):
                            self.assertEqual(caught.exception.status, int(error.stderr[-4:-1]))
                            if env is not None:
                                self.assertIn('App credential', str(caught.exception))
                        self.assertEqual(invalidate.call_count, int(env is not None and isinstance(error, subprocess.CompletedProcess) and '401' in error.stderr))
                        self.assertEqual(run.call_args_list[0].kwargs['env'], env)
                        self.assertEqual(run.call_args_list[0].kwargs['timeout'], COMMAND_TIMEOUT)
            with patch('foreman.github.credentials.environment', side_effect=CredentialFailure('unavailable')), \
                 patch('foreman.github.subprocess.run') as run, self.assertRaisesRegex(CommandFailed, 'unavailable'):
                getattr(LiveForge(), method)(REPO, *args)
            run.assert_not_called()

    def test_invalid_listing_fails_closed(self):
        for method in ('smoke_issues', 'smoke_pull_requests'):
            for output in ([{'number': 'bad'}], {}, None, subprocess.CompletedProcess([], 0, "", "")):
                with self.subTest(method=method, output=output), self.transport([output]), self.assertRaises(ForgeError):
                    getattr(LiveForge(), method)(REPO)
        with self.transport([[]]), self.assertRaises(ForgeError):
            LiveForge().smoke_pull_requests(REPO)

    def test_pagination_rejects_missing_repeated_and_cyclic_cursors(self):
        for inner in (False, True):
            for cursors in ((None,), ('a', 'a'), ('a', 'b', 'a')):
                pages = [connection([], cursor) for cursor in cursors]
                for page in pages:
                    page['pageInfo']['hasNextPage'] = True
                outputs = [connection([pull(2, pages[0])]), *pages[1:]] if inner else pages
                with self.subTest(inner=inner, cursors=cursors), self.transport(outputs) as run:
                    with self.assertRaisesRegex(ForgeError, 'cursor'):
                        LiveForge().smoke_pull_requests(REPO)
                self.assertEqual(run.call_count, len(outputs))

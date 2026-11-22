"""Smoke transport operations without cleanup policy."""
import tests

import json
import subprocess
import unittest
from unittest.mock import patch

from foreman.github import ForgeError, LiveForge

REPO = 'bookout-labs/foreman-smoke'


def connection(nodes, cursor=None):
    return {'nodes': nodes, 'pageInfo': {'hasNextPage': cursor is not None, 'endCursor': cursor}}


def issue(number, **changes):
    return {'state': 'open', 'number': number, 'body': '<!-- smoke:run -->', 'repository_url': f'https://api.github.com/repos/{REPO}',
            'labels': [{'name': 'smoke'}], 'user': {'login': 'bot'}, **changes}


class SmokeForgeTest(unittest.TestCase):
    def transport(self, outputs):
        return patch('foreman.github.subprocess.run', side_effect=[
            subprocess.CompletedProcess([], 0, json.dumps(output), '') for output in outputs])

    def test_issue_pages_exclude_prs_other_repos_and_missing_label(self):
        data = [[issue(1), issue(2, pull_request={}), issue(3, labels=[])],
                [issue(4, repository_url='https://api.github.com/repos/other/repo'), issue(5, body=None)]]
        with self.transport([data]), patch('foreman.github.credentials.environment', return_value=None):
            items = LiveForge().smoke_issues(REPO)
        self.assertEqual([item.number for item in items], [1, 5])
        self.assertIsNone(items[-1].body)

    def test_pull_and_closing_link_pages_preserve_repo_and_armed_evidence(self):
        linked = lambda number: {'number': number, 'repository': {'name': 'foreman-smoke', 'owner': {'login': 'bookout-labs'}}}
        pull = lambda number, refs, **changes: dict(number=number, closingIssuesReferences=refs,
                                                   autoMergeRequest=None, mergeQueueEntry=None, **changes)
        pages = [connection([pull(2, connection([linked(1)], 'next'))], 'outer'),
                 connection([pull(4, connection([]))])]
        pages[0]['nodes'][0]['autoMergeRequest'] = {'enabledAt': 'now', 'mergeMethod': 'SQUASH'}
        pages[1]['nodes'][0]['mergeQueueEntry'] = {'id': 'queue', 'state': 'QUEUED'}
        with self.transport([pages, connection([linked(3)])]) as run, patch('foreman.github.credentials.environment', return_value=None):
            items = LiveForge().smoke_pull_requests(REPO)
        self.assertEqual([item.number for item in items], [2, 4])
        self.assertEqual([item.number for item in items[0].closingIssuesReferences.nodes], [1, 3])
        self.assertEqual(items[0].closingIssuesReferences.nodes[0].repository.owner.login, 'bookout-labs')
        self.assertIsNotNone(items[0].autoMergeRequest)
        self.assertIsNotNone(items[1].mergeQueueEntry)
        self.assertIn('endCursor=next', run.call_args.args[0])
        query = next(arg for arg in run.call_args_list[0].args[0] if arg.startswith('query='))
        self.assertLess(query.index('pageInfo'), query.index('closingIssuesReferences'))

    def test_label_and_close_repeat_and_propagate_transport_errors(self):
        with patch('foreman.github.credentials.environment', return_value=None), self.transport([{}] * 6) as run:
            forge = LiveForge()
            for _ in range(2):
                forge.ensure_smoke_label(REPO)
                forge.close_issue(REPO, 1)
                forge.close_pull_request(REPO, 2)
            commands = [call.args[0] for call in run.call_args_list]
            self.assertEqual(commands[:3], commands[3:])
            self.assertIn('--force', commands[0])
            self.assertIn(f'repos/{REPO}/issues/1', commands[1])
            self.assertIn(f'repos/{REPO}/pulls/2', commands[2])
            self.assertIn('state=closed', commands[2])
        for method in ('ensure_smoke_label', 'close_issue', 'close_pull_request', 'smoke_issues', 'smoke_pull_requests'):
            with self.subTest(method=method), patch.object(LiveForge, '_run_command', side_effect=ForgeError('denied')):
                with self.assertRaisesRegex(ForgeError, 'denied'):
                    getattr(LiveForge(), method)(REPO, *([] if method.startswith(('ensure', 'smoke')) else [1]))

    def test_invalid_listing_fails_closed(self):
        for method in ('smoke_issues', 'smoke_pull_requests'):
            with self.transport([[{'number': 'bad'}]]), patch('foreman.github.credentials.environment', return_value=None):
                with self.assertRaises(ForgeError):
                    getattr(LiveForge(), method)(REPO)

    def test_incomplete_pull_pagination_fails_closed(self):
        with self.transport([[connection([], 'more')]]), patch('foreman.github.credentials.environment', return_value=None):
            with self.assertRaisesRegex(ForgeError, 'Incomplete'):
                LiveForge().smoke_pull_requests(REPO)

    def test_closing_link_pagination_rejects_missing_or_stuck_cursor(self):
        for cursor in (None, 'stuck'):
            refs = connection([], cursor)
            refs['pageInfo']['hasNextPage'] = True
            pull = dict(number=2, closingIssuesReferences=refs, autoMergeRequest=None, mergeQueueEntry=None)
            with self.subTest(cursor=cursor), self.transport([[connection([pull])], refs]), \
                 patch('foreman.github.credentials.environment', return_value=None):
                with self.assertRaisesRegex(ForgeError, 'cursor|advance'):
                    LiveForge().smoke_pull_requests(REPO)

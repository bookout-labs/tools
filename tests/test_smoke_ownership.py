"""Ownership survives process death without relying on a checkpoint."""
import tests

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from foreman.github import BOT_LOGIN, ForgeError, SmokeIssue, SmokeOpenPull
from foreman.smoke_command import SMOKE_REPO as REPO
from foreman.smoke_ownership import cleanup_smoke, create_smoke_issue, smoke_lock
from tests.test_smoke_forge import connection, issue

MARKER = '<!-- foreman-smoke-run: ' + 'a' * 32 + ' -->'


def fixture(number=1, **changes):
    return SmokeIssue.model_validate(issue(number, body=MARKER, user={'login': BOT_LOGIN}, **changes))


def pull(number=2, linked_repo=REPO, **changes):
    owner, name = linked_repo.split('/')
    return SmokeOpenPull.model_validate(dict(number=number, autoMergeRequest=None, mergeQueueEntry=None,
        closingIssuesReferences=connection([{'number': 1, 'repository': {'owner': {'login': owner}, 'name': name}}]), **changes))


class SmokeOwnershipTest(unittest.TestCase):
    def test_exact_ownership_and_closing_links(self):
        for change in ({}, {'body': 'smoke'}, {'user': {'login': 'personal'}}, {'labels': []},
                       {'repository_url': 'https://api.github.com/repos/other/repo'}, {'pull_request': {}}):
            data = fixture().model_dump(exclude_unset=True)
            data.update(change)
            forge = Mock()
            forge.smoke_issues.return_value = [SmokeIssue.model_validate(data)]
            forge.smoke_pull_requests.return_value = [pull()]
            cleanup_smoke(forge, REPO)
            self.assertEqual(forge.close_issue.called, not change)
            self.assertEqual(forge.close_pull_request.called, not change)
        forge.smoke_issues.return_value = [fixture()]
        forge.smoke_pull_requests.return_value = [pull(linked_repo='other/repo')]
        forge.reset_mock()
        cleanup_smoke(forge, REPO)
        forge.close_pull_request.assert_not_called()
        forge.close_issue.assert_called_once_with(REPO, 1)

    def test_case_insensitive_label_and_missing_author(self):
        forge = Mock()
        forge.smoke_pull_requests.return_value = []
        forge.smoke_issues.return_value = [fixture(labels=[{'name': 'Smoke'}])]
        cleanup_smoke(forge, REPO)
        forge.close_issue.assert_called_once_with(REPO, 1)
        forge.reset_mock()
        forge.smoke_issues.return_value = [fixture().model_copy(update={'user': None})]
        cleanup_smoke(forge, REPO)
        forge.close_issue.assert_not_called()

    def test_armed_and_queued_prs_protect_the_issue(self):
        for field, value in (('autoMergeRequest', {'enabledAt': 'now', 'mergeMethod': 'SQUASH'}),
                             ('mergeQueueEntry', {'id': 'queue', 'state': 'QUEUED'})):
            data = pull().model_dump()
            data[field] = value
            forge = Mock()
            forge.smoke_issues.return_value = [fixture()]
            forge.smoke_pull_requests.return_value = [SmokeOpenPull.model_validate(data)]
            cleanup_smoke(forge, REPO)
            forge.close_issue.assert_not_called()
            forge.close_pull_request.assert_not_called()

    def test_partial_cleanup_retries_closed_issue_links_and_reports_failure(self):
        forge = Mock()
        forge.smoke_issues.return_value = [fixture()]
        forge.smoke_pull_requests.return_value = [pull()]
        forge.close_pull_request.side_effect = ForgeError('denied')
        with self.assertRaisesRegex(ForgeError, 'denied'):
            cleanup_smoke(forge, REPO)
        forge.close_issue.assert_not_called()
        forge.close_pull_request.side_effect = None
        forge.smoke_issues.return_value = [fixture(state='closed')]
        cleanup_smoke(forge, REPO)
        forge.close_issue.assert_not_called()
        forge.close_pull_request.assert_called_with(REPO, 2)

    def test_lost_create_response_reconciles_own_marker(self):
        forge = Mock()
        def lost(repo, title, body, labels):
            data = fixture().model_dump(exclude_unset=True)
            data['body'] = body
            forge.smoke_issues.return_value = [fixture(9), SmokeIssue.model_validate(data)]
            raise ForgeError('lost response')
        forge.create_issue.side_effect = lost
        self.assertEqual(create_smoke_issue(forge, REPO), 1)
        forge.create_issue.assert_called_once()
        forge.create_issue.side_effect = ForgeError('not created')
        forge.smoke_issues.return_value = [fixture(9)]
        with self.assertRaisesRegex(ForgeError, 'not created'):
            create_smoke_issue(forge, REPO)

    def test_process_crashes_release_lock_and_next_run_cleans_github(self):
        code = '''
import json, os, signal, sys, time
from pathlib import Path
from unittest.mock import patch
from foreman.github import SmokeIssue, SmokeOpenPull
from foreman.smoke_command import run_smoke
from tests.test_smoke import passing
from tests.test_smoke_ownership import fixture, pull
path, phase = Path(sys.argv[1]), sys.argv[2]
class Forge:
    def __init__(self): self.data = json.loads(path.read_text())
    def save(self): path.write_text(json.dumps(self.data))
    def default_branch(self, repo): return 'main'
    def ensure_smoke_label(self, repo): pass
    def smoke_issues(self, repo): return [SmokeIssue.model_validate(x) for x in self.data['issues']]
    def smoke_pull_requests(self, repo): return [SmokeOpenPull.model_validate(x) for x in self.data['pulls']]
    def close_issue(self, repo, number):
        next(x for x in self.data['issues'] if x['number'] == number)['state'] = 'closed'
        self.save()
    def close_pull_request(self, repo, number):
        self.data['pulls'] = [x for x in self.data['pulls'] if x['number'] != number]
        self.save()
    def create_issue(self, repo, title, body, labels):
        item = fixture(len(self.data['issues']) + 1).model_dump(exclude_unset=True)
        item['body'] = body
        self.data['issues'].append(item)
        self.save()
        if phase == 'issue': os.kill(os.getpid(), signal.SIGKILL)
        return item['number']
    def smoke_identity(self, *args): return True, True, True
forge = Forge()
def runner(**kwargs):
    if phase in ('pr', 'armed'):
        item = pull().model_dump()
        if phase == 'armed': item['mergeQueueEntry'] = {'id': 'queue', 'state': 'QUEUED'}
        forge.data['pulls'].append(item)
        forge.save()
        os.kill(os.getpid(), signal.SIGKILL)
    return passing()
with patch('foreman.smoke_command.LiveForge', return_value=forge), \\
     patch('foreman.smoke_command.credentials.environment', return_value={}), \\
     patch('foreman.smoke_command.subprocess.run'), \\
     patch('foreman.smoke_command.subprocess.check_output', return_value='base'), \\
     patch('foreman.smoke_command.run_live_delivery', side_effect=runner):
    sys.exit(run_smoke(path.parent, time.monotonic() + 30))
'''
        for phase in ('issue', 'pr', 'armed'):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                path = root / 'github.json'
                path.write_text(json.dumps({'issues': [], 'pulls': []}))
                env = {**os.environ, 'XDG_STATE_HOME': directory,
                       'PYTHONPATH': str(Path(__file__).resolve().parent) + os.pathsep + os.environ.get('PYTHONPATH', '')}
                def run(mode):
                    return subprocess.run([sys.executable, '-c', code, str(path), mode], env=env, capture_output=True, timeout=10)
                crashed = run(phase)
                self.assertEqual(crashed.returncode, -signal.SIGKILL, crashed.stderr)
                with patch.dict(os.environ, env), smoke_lock(REPO):
                    with smoke_lock('other/repo'):
                        pass
                    blocked = run('next')
                    self.assertEqual(blocked.returncode, 78, blocked.stderr)
                    self.assertEqual(len(json.loads(path.read_text())['issues']), 1)
                result = run('next')
                self.assertEqual(result.returncode, 0, result.stderr)
                data = json.loads(path.read_text())
                self.assertEqual([x['state'] for x in data['issues']],
                                 ['open' if phase == 'armed' else 'closed', 'open'])
                self.assertEqual(len(data['pulls']), int(phase == 'armed'))
                self.assertEqual(run('next').returncode, 0)
                self.assertEqual(json.loads(path.read_text())['issues'][1]['state'], 'closed')

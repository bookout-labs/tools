import tests

import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from foreman.github import CommandFailed, LiveForge
from foreman.workspace import LiveWorkspace, WorktreeInspectionFailed


class BotIdentityTest(unittest.TestCase):
    def test_foreman_and_adopted_tree_use_bot_identity(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            def git(*args):
                return subprocess.run(['git', *args], cwd=root, check=True, capture_output=True,
                                      text=True).stdout.strip()
            git('init', '-b', 'main')
            git('config', 'user.name', 'Personal')
            git('config', 'user.email', 'personal@example.com')
            git('config', 'commit.gpgsign', 'false')
            git('commit', '--allow-empty', '-m', 'Base')
            base = git('rev-parse', 'HEAD')
            (root / 'file').write_text('worker tree')
            git('add', '.')
            git('commit', '-m', 'Worker commit')
            tree = git('rev-parse', 'HEAD^{tree}')
            workspace = LiveWorkspace(root, 'org/repo')
            workspace.soft_reset(base)
            message = 'Adopt worker tree\n\nCo-Authored-By: GPT <noreply@openai.com>'
            with patch.dict(os.environ, FOREMAN_APP_ID='1', GIT_AUTHOR_NAME='Ambient',
                            GIT_COMMITTER_EMAIL='ambient@example.com'), patch.object(
                LiveForge, '_run_command', return_value='{"id":359513816}',
            ) as api:
                workspace.commit(message)
                self.assertTrue(workspace.committed(base, message))
                api.assert_called_once_with('org/repo', [
                    'gh', 'api', 'users/bookout-labs-foreman%5Bbot%5D',
                ], timeout=30, body=None)
            login = 'bookout-labs-foreman[bot]'
            email = f'359513816+{login}@users.noreply.github.com'
            self.assertEqual(git('show', '-s', '--format=%an%n%ae%n%cn%n%ce'),
                             '\n'.join([login, email, login, email]))
            self.assertEqual(git('rev-parse', 'HEAD^{tree}'), tree)
            self.assertEqual(git('show', '-s', '--format=%B'), message)
            self.assertEqual(git('config', 'user.name'), 'Personal')
            with patch.dict(os.environ, FOREMAN_APP_ID='1'):
                git('commit', '--amend', '--no-edit', '--reset-author')
                self.assertFalse(workspace.committed(base, message))

    def test_resolution_failure_stops_before_commit(self):
        for value in (0, -1, 'not an id'):
            with patch.object(LiveForge, '_run_command', return_value=json.dumps({'id': value})):
                with self.assertRaises(WorktreeInspectionFailed):
                    LiveWorkspace(Path.cwd(), 'org/repo')._bot_identity()
        with patch.object(LiveForge, '_run_command', side_effect=CommandFailed('permission denied')):
            with self.assertRaisesRegex(WorktreeInspectionFailed, 'permission denied'):
                LiveWorkspace(Path.cwd(), 'org/repo')._bot_identity()

"""The release an install reports: the commit its installer recorded, or a clean checkout's HEAD."""
import tests

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from foreman.release import release


def installed(origin: dict | None) -> Mock:
    """An installed distribution whose direct_url.json records origin, or an install that recorded none."""
    return Mock(read_text=Mock(return_value=None if origin is None else json.dumps(origin)))


class ReleaseTest(unittest.TestCase):
    def test_a_git_install_reports_the_commit_it_resolved_not_the_ref_it_was_given(self):
        origin = {"url": "https://github.com/bookout-labs/tools",
                  "vcs_info": {"vcs": "git", "commit_id": "a" * 40, "requested_revision": "delivery-22"}}
        with patch("foreman.release.distribution", return_value=installed(origin)) as distribution:
            self.assertEqual(release(), "a" * 40)
        distribution.assert_called_once_with("bookout-tools")

    def test_an_editable_checkout_reports_its_head_while_no_tracked_file_differs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def git(*args: str) -> str:
                return subprocess.run(["git", "-c", "user.name=test", "-c", "user.email=test@example.com", *args], cwd=root,
                                      capture_output=True, text=True, check=True).stdout.strip()

            git("init", "-q")
            (root / "code.py").write_text("one\n")
            git("add", ".")
            git("commit", "-qm", "base")
            origin = {"url": root.resolve().as_uri(), "dir_info": {"editable": True}}
            with patch("foreman.release.distribution", return_value=installed(origin)):
                self.assertEqual(release(), git("rev-parse", "HEAD"))
                # A file git does not track is no code the checkout runs unless a tracked file changes to import it.
                (root / "scratch.txt").write_text("notes\n")
                self.assertEqual(release(), git("rev-parse", "HEAD"))
                (root / "code.py").write_text("two\n")
                self.assertEqual(release(), "")
                git("add", ".")
                self.assertEqual(release(), "")

    def test_an_install_that_recorded_no_commit_reports_none(self):
        with tempfile.TemporaryDirectory() as directory:
            for origin in (None, {"url": Path(directory).as_uri(), "dir_info": {}},
                           {"url": Path(directory).as_uri(), "dir_info": {"editable": True}}):
                with self.subTest(origin=origin), patch("foreman.release.distribution", return_value=installed(origin)):
                    self.assertEqual(release(), "")


if __name__ == "__main__":
    unittest.main()

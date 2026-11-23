"""Managed checkouts: one shared clone per repository and one worktree per admitted issue, against real Git."""

import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from foreman.stage_output import spec_digest
from foreman.workspace import LiveWorkspace, lock_file

REPO = "Acme/Widgets"


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class ManagedCheckoutTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        home = Path(self.temp.name)
        origin, seed = home / "origin.git", home / "seed"
        git(home, "init", "--quiet", "--bare", str(origin))
        git(home, "init", "--quiet", "-b", "main", str(seed))
        (seed / "README.md").write_text("widgets\n")
        git(seed, "add", "README.md")
        git(seed, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "--quiet", "-m", "seed")
        git(seed, "push", "--quiet", str(origin), "main")
        self.base = git(seed, "rev-parse", "HEAD")
        # GitHub's URL resolves to the local origin, and the origin check is the credentials module's own concern.
        environment = {"XDG_RUNTIME_DIR": str(home / "run"), "GIT_CONFIG_COUNT": "1",
                       "GIT_CONFIG_KEY_0": f"url.{origin}.insteadOf", "GIT_CONFIG_VALUE_0": f"https://github.com/{REPO}"}
        for patcher in (patch.dict(os.environ, environment), patch("foreman.workspace.validate_remote", return_value=[])):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.workspace = LiveWorkspace.managed(home / "state", REPO, 7)

    def admit(self) -> str:
        self.workspace.validate_origin(REPO)
        return self.workspace.pin_base("main")

    def test_admission_allocates_the_issue_worktree_at_the_pinned_base(self):
        state = Path(self.temp.name, "state").resolve()
        self.assertEqual((self.workspace.root, self.workspace.clone),
                         (state / "foreman/runs/acme/widgets/7", state / "foreman/checkouts/acme/widgets"))
        self.assertFalse(self.workspace.root.exists())
        self.assertEqual(self.admit(), self.base)
        assert self.workspace.clone is not None
        self.assertEqual(git(self.workspace.clone, "config", "remote.origin.url"), f"https://github.com/{REPO}")
        observed = self.workspace.inspect()
        self.assertEqual((observed.head, observed.clean, observed.branch), (self.base, True, None))

    def test_a_crash_before_the_admission_checkpoint_reuses_the_worktree(self):
        self.admit()
        self.assertEqual(LiveWorkspace.managed(Path(self.temp.name, "state"), REPO, 7).pin_base("main"), self.base)
        self.assertEqual(self.workspace.inspect().head, self.base)

    def test_release_removes_only_a_clean_worktree_at_a_recorded_head(self):
        self.admit()
        (self.workspace.root / "scratch.txt").write_text("evidence\n")
        self.assertFalse(self.workspace.release({self.base}))
        (self.workspace.root / "scratch.txt").unlink()
        self.assertFalse(self.workspace.release({"0" * 40}))
        self.assertTrue(self.workspace.release({self.base}))
        self.assertFalse(self.workspace.root.exists())
        assert self.workspace.clone is not None
        self.assertEqual(git(self.workspace.clone, "worktree", "list", "--porcelain").count("worktree "), 1)
        # A finished run's readback still validates its origin, through the shared clone.
        self.workspace.validate_origin(REPO)

    def test_issue_lock_files_keep_their_names(self):
        self.assertEqual(lock_file("acme/widgets#7").name, spec_digest(["acme/widgets#7"]))


if __name__ == "__main__":
    unittest.main()

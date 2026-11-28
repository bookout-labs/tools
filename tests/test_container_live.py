"""The attempt container on the host's real docker: Git read-only, the network during setup only, the mount check and the
check's limit. The unit suite's double stands in everywhere else, so these run only where FOREMAN_DOCKER_TESTS names an image
the host has, such as python:3.12, which carries sh and git."""

import tests

import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

from foreman.container import ContainerUnavailable, DockerHost, TimedOut
from foreman.contract import CONTRACT_PATH
from foreman.workspace import LiveWorkspace

IMAGE = os.environ.get("FOREMAN_DOCKER_TESTS", "")
# Colima shares the home directory, so each checkout sits in this one.
ROOT = Path(__file__).resolve().parents[1]
GIT = "git -c safe.directory='*'"


@unittest.skipUnless(IMAGE, "set FOREMAN_DOCKER_TESTS to a local image with sh and git to run against docker")
class LiveContainerTest(unittest.TestCase):
    def checkout(self, contract: str, *, under: Path = ROOT) -> tuple[LiveWorkspace, str]:
        root = Path(self.enterContext(TemporaryDirectory(prefix=".scratch.container-", dir=under))).resolve()
        (root / CONTRACT_PATH).parent.mkdir()
        (root / CONTRACT_PATH).write_text(f"check: 'true'\nverify_skill: SKILL.md\ntest_report: junit.xml\nimage: {IMAGE}\nsecrets: []\nmutation: {{command: cat, sample_cap: 1}}\n{contract}")
        for args in (("init", "-qb", "main"), ("add", "--all"), ("-c", "user.name=T", "-c", "user.email=t@example.test", "commit", "-qm", "Base")):
            subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
        workspace = LiveWorkspace(root, containers=DockerHost())
        self.addCleanup(workspace.remove_container)
        return workspace, subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()

    def test_git_reads_work_in_the_container_and_its_writes_fail(self):
        workspace, base = self.checkout("setup: []\n")
        self.assertEqual(workspace.run(f"{GIT} rev-parse HEAD | grep -qx {base}", base=base, log=None), 0)
        output = []
        self.assertNotEqual(workspace.run(f"{GIT} switch -qc other", base=base, log=output.append), 0)
        self.assertIn("Read-only file system", "".join(output))
        self.assertEqual(subprocess.check_output(["git", "branch", "--show-current"], cwd=workspace.root, text=True).strip(), "main")

    def test_the_container_has_the_network_during_setup_only_unless_the_contract_keeps_it(self):
        interfaces = "ls /sys/class/net | sort | tr '\\n' ' '"
        workspace, base = self.checkout(f"setup: [\"{interfaces} > during-setup\"]\n")
        self.assertEqual(workspace.run(f'test "$({interfaces})" = "lo "', base=base, log=None), 0)
        self.assertNotEqual((workspace.root / "during-setup").read_text(), "lo ")
        kept, base = self.checkout("setup: []\nnetwork: run\n")
        self.assertNotEqual(kept.run(f'test "$({interfaces})" = "lo "', base=base, log=None), 0)

    def test_a_checkout_the_container_host_does_not_share_stops_with_its_path(self):
        """Colima shares only some host paths. Docker refuses a mount it cannot see, or mounts it empty, and either way the
        build stops with the checkout's path."""
        workspace, base = self.checkout("setup: []\n", under=Path("/tmp"))
        with self.assertRaisesRegex(ContainerUnavailable, workspace.root.name):
            workspace.prepare_container(base=base, log=None)

    def test_a_check_at_its_limit_is_ended_and_its_container_removed(self):
        workspace, base = self.checkout("setup: []\ncheck_timeout_seconds: 2\n")
        workspace.prepare_container(base=base, log=None)
        name = workspace.containers.leftovers(str(workspace.root))
        self.assertEqual(workspace.run("sleep 60", base=base, log=None), TimedOut(2))
        self.assertFalse(workspace.containers.running(name[0]))

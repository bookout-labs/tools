"""Doctor's contract requirements and admission reading the contract at the pinned base (target.yaml target_contract, doctor)."""
import tests

from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
import unittest.mock
from unittest.mock import MagicMock

from langgraph.checkpoint.sqlite import SqliteSaver

from foreman.contract import CONTRACT_PATH, ContractError, parse_contract
from foreman.delivery import LiveRun
from foreman.dry_run import ScriptHandler
from foreman.doctor import NotReady, quick
from foreman.outcomes import Halt
from foreman.runner import issue_thread, run_live_delivery, saved_workspace
from foreman.state import ChecksState, PlanState, ReviewState, RunState
from foreman.steps import triage
from foreman.workspace import LiveWorkspace
from skill_fixture import fixture_skills
from test_runner import FakeGitHub, FakeWorkspace

SKILL = ".claude/skills/verify-web/SKILL.md"
MINIMAL = f"check: npm run check\nverify_skill: {SKILL}\n"
FULL = f"""check: npm run check
verify_skill: {SKILL}
test_report: [reports/unit.xml, reports/e2e.xml]
image: node:22
setup: [npm ci]
network: run
start: {{command: npm start, health: curl -sf localhost:3000, timeout_seconds: 60}}
related_repositories:
  - {{repository: Example/Api, start: npm start}}
secrets: [DATABASE_URL, AUTH0_SECRET]
mutation: {{command: npx stryker run, sample_cap: 20}}
production_check: {{command: ./confirm.sh, not_deployed_exit: 3, timeout_seconds: 900}}
"""


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@example.test", *args], cwd=root,
                          capture_output=True, check=True, text=True).stdout.strip()


def commit(root: Path, files: dict[str, str]) -> str:
    for path, text in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)
    git(root, "add", "-A")
    git(root, "commit", "-qm", "c", "--allow-empty")
    return git(root, "rev-parse", "HEAD")


class Pinned(LiveWorkspace):
    """A real checkout whose base is a local commit, so admission needs no remote. Everything else is real Git."""

    def __init__(self, root: Path, base: str):
        super().__init__(root)
        self.base, self.calls = base, []

    def validate_origin(self, repo):
        pass

    def pin_base(self, base):
        self.calls.append("pin_base")
        return self.base

    def tracked_file(self, base, path):
        self.calls.append(("tracked_file", base, path))
        return super().tracked_file(base, path)


def admit(workspace):
    github = FakeGitHub(labels=["Ready"])
    run = MagicMock(forge=github, workspace=workspace, human_login="fixture-owner")
    state = {"run": RunState(repo="Example/Engine", issue=8), "plan": PlanState(), "checks": ChecksState(preflight="npm ci"),
             "review": ReviewState(), "path": []}
    return triage.admit(state, run)


class ContractTest(unittest.TestCase):
    def test_a_full_contract_parses_each_field_with_its_drawn_type(self):
        contract = parse_contract(FULL.encode())
        self.assertEqual((contract.check, contract.verify_skill, contract.network), ("npm run check", SKILL, "run"))
        self.assertEqual(contract.test_report, ["reports/unit.xml", "reports/e2e.xml"])
        self.assertEqual((contract.related_repositories[0].repository, contract.secrets), ("Example/Api", ["DATABASE_URL", "AUTH0_SECRET"]))
        self.assertEqual((contract.start and contract.start.timeout_seconds, contract.mutation and contract.mutation.sample_cap,
                          contract.production_check and contract.production_check.not_deployed_exit), (60, 20, 3))

    def test_only_check_and_verify_skill_are_required(self):
        contract = parse_contract(MINIMAL.encode())
        self.assertEqual((contract.image, contract.setup, contract.network, contract.start, contract.related_repositories,
                          contract.secrets, contract.mutation, contract.production_check, contract.test_report),
                         (None, [], "setup", None, [], [], None, None, None))
        self.assertEqual(parse_contract(MINIMAL.encode() + b"test_report: reports/unit.xml\n").test_report, "reports/unit.xml")

    def test_each_refinement_names_its_field(self):
        cases = {
            "check: ''\nverify_skill: a.md\n": "check",
            f"verify_skill: {SKILL}\n": "check",
            "check: x\n": "verify_skill",
            "check: 7\nverify_skill: a.md\n": "check",
            "check: x\nverify_skill: /abs/SKILL.md\n": "verify_skill",
            "check: x\nverify_skill: ../SKILL.md\n": "verify_skill",
            "check: x\nverify_skill: a//SKILL.md\n": "verify_skill",
            MINIMAL + "test_report: /tmp/report.xml\n": "test_report",
            MINIMAL + "test_report: [../report.xml]\n": "test_report",
            MINIMAL + "image: [node]\n": "image",
            MINIMAL + "setup: npm ci\n": "setup",
            MINIMAL + "network: always\n": "network",
            MINIMAL + "start: {command: a, health: b}\n": "start.timeout_seconds",
            MINIMAL + "start: {command: a, health: b, timeout_seconds: '60'}\n": "start.timeout_seconds",
            MINIMAL + "related_repositories: [{repository: noslash, start: a}]\n": "related_repositories.0.repository",
            MINIMAL + "related_repositories: [{repository: A/B, start: a, commit: abc}]\n": "related_repositories.0.commit",
            MINIMAL + "related_repositories: [{repository: A/B, start: a, sha: abc}]\n": "related_repositories.0.sha",
            MINIMAL + "secrets: [database_url]\n": "secrets.0",
            MINIMAL + "secrets: [1DB]\n": "secrets.0",
            MINIMAL + "mutation: {command: a, sample_cap: 0}\n": "mutation.sample_cap",
            MINIMAL + "production_check: {command: a, not_deployed_exit: 0, timeout_seconds: 9}\n": "production_check.not_deployed_exit",
            MINIMAL + "production_check: {command: a, not_deployed_exit: 256, timeout_seconds: 9}\n": "production_check.not_deployed_exit",
            MINIMAL + "deploy: now\n": "deploy",
            MINIMAL + "check: again\n": "duplicate key 'check'",
            "- check\n": "the file",
            "check: [unclosed\n": "YAML",
        }
        for text, named in cases.items():
            with self.subTest(text=text), self.assertRaises(ContractError) as raised:
                parse_contract(text.encode())
            self.assertIn(named, str(raised.exception))


class AdmissionTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory())).resolve()
        git(self.root, "init", "-q")

    def test_admission_reads_the_contract_at_the_pinned_base_whatever_the_branch_says(self):
        base = commit(self.root, {CONTRACT_PATH: f"check: check-a\nverify_skill: {SKILL}\n", SKILL: "# Verify\n"})
        git(self.root, "switch", "-qc", "8-items")
        commit(self.root, {CONTRACT_PATH: f"check: check-b\nverify_skill: {SKILL}\n"})
        workspace = Pinned(self.root, base)
        update = admit(workspace)
        self.assertNotIn("halt", update)
        self.assertEqual((update["checks"].check, update["checks"].verify_skill, update["checks"].preflight), ("check-a", SKILL, "npm ci"))
        self.assertEqual(update["run"].base_sha, base)
        # The contract is read after the base is pinned, at that base.
        self.assertEqual(workspace.calls[:2], ["pin_base", ("tracked_file", base, CONTRACT_PATH)])

    def test_each_missing_requirement_stops_not_ready_with_its_fix_and_keeps_ready(self):
        cases = {
            "missing contract": ({SKILL: "# Verify\n"}, f"the contract file {CONTRACT_PATH} is not a tracked file"),
            "malformed contract": ({CONTRACT_PATH: f"verify_skill: {SKILL}\n", SKILL: "# Verify\n"}, "check: Field required"),
            "no verify skill": ({CONTRACT_PATH: MINIMAL}, f"the verify skill {SKILL} that the contract names"),
        }
        for name, (files, requirement) in cases.items():
            with self.subTest(name), TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                git(root, "init", "-q")
                update = admit(Pinned(root, commit(root, files)))
                self.assertEqual((update["halt"], update["stop"], update["path"]), (Halt.not_ready, Halt.not_ready, ["admit"]))
                self.assertIn(requirement, update["halt_detail"])
                self.assertIn("Fix: ", update["halt_detail"])
                self.assertTrue(update["halt_detail"].endswith("Then remove and add the Ready label."))
                self.assertNotIn("checks", update)

    def test_an_untracked_or_symlinked_skill_is_not_ready(self):
        base = commit(self.root, {CONTRACT_PATH: MINIMAL, "real.md": "# Verify\n"})
        (self.root / SKILL).parent.mkdir(parents=True)
        (self.root / SKILL).symlink_to(self.root / "real.md")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", "link")
        for pinned in (base, git(self.root, "rev-parse", "HEAD")):
            with self.subTest(pinned=pinned), self.assertRaisesRegex(NotReady, "is not a tracked regular file"):
                quick(LiveWorkspace(self.root), pinned)

    def test_the_tracked_file_reader_returns_content_only_for_a_tracked_regular_file(self):
        base = commit(self.root, {"a.txt": "one\n", "dir/b.txt": "two\n"})
        (self.root / "untracked.txt").write_text("x")
        workspace = LiveWorkspace(self.root)
        self.assertEqual(workspace.tracked_file(base, "a.txt"), b"one\n")
        for path in ("dir", "untracked.txt", "../a.txt", "/a.txt", "missing.txt"):
            with self.subTest(path=path):
                self.assertIsNone(workspace.tracked_file(base, path))



class NotReadyRunTest(unittest.TestCase):
    def test_the_status_names_the_requirement_before_the_checkpoint_goes_and_a_new_ready_admits_again(self):
        root = Path(self.enterContext(TemporaryDirectory())).resolve()
        checkpoint, thread = root / "runs.sqlite3", issue_thread("Example/Engine", 8)
        github, workspace, events = FakeGitHub(), FakeWorkspace(root), []
        del workspace.base_files[CONTRACT_PATH]
        write, delete = github.upsert_comment, SqliteSaver.delete_thread

        def recorded_write(repo, number, text, facts, *, marker):
            events.append(("status", text))
            write(repo, number, text, facts, marker=marker)

        def recorded_delete(saver, thread_id):
            events.append(("delete", thread_id, saved_workspace(checkpoint, thread_id)))
            delete(saver, thread_id)

        github.upsert_comment = recorded_write
        dispatch = MagicMock(side_effect=AssertionError("not_ready makes no paid call"))
        with unittest.mock.patch.object(SqliteSaver, "delete_thread", recorded_delete):
            result = run_live_delivery(repo="Example/Engine", issue=8, checkpoint=checkpoint, dispatcher=dispatch,
                                       run=LiveRun(github, workspace, skills=fixture_skills, human_login="fixture-owner"))
        self.assertEqual((result["halt"], result["path"]), (Halt.not_ready, ["admit"]))
        (kind, text), deleted = events
        self.assertEqual(kind, "status")
        self.assertIn("Foreman halted with not_ready. The target repository is not ready for Foreman.", text)
        self.assertIn(f"the contract file {CONTRACT_PATH} is not a tracked file", text)
        self.assertIn("Then remove and add the Ready label.", text)
        # The checkpoint still existed when the status was written, and is gone after.
        self.assertEqual(deleted, ("delete", thread, str(root)))
        self.assertIsNone(saved_workspace(checkpoint, thread))
        # Before admission Foreman owns no label, so the issue keeps Ready.
        self.assertEqual(github.label_calls, [])
        dispatch.assert_not_called()
        # Once the requirement is supplied, the next Ready starts a fresh admission.
        workspace.base_files.update({CONTRACT_PATH: MINIMAL.encode(), SKILL: b"# Verify\n"})
        admitted = run_live_delivery(repo="Example/Engine", issue=8, checkpoint=checkpoint, dispatcher=ScriptHandler(triage="needs_human"),
                                     run=LiveRun(github, workspace, skills=fixture_skills, human_login="fixture-owner"))
        self.assertEqual((admitted["stop"], admitted["checks"].check), ("needs_human", "npm run check"))


if __name__ == "__main__":
    unittest.main()

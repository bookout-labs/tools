"""Doctor's quick requirements, its command, and admission reading the contract at the pinned base (target.yaml target_contract, doctor)."""
import tests

import contextlib
import io
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
import unittest.mock
from unittest.mock import MagicMock, patch

from langgraph.checkpoint.sqlite import SqliteSaver

from foreman.cli import main
from foreman.contract import CONTRACT_PATH, ContractError, parse_contract
from foreman.delivery import LiveRun
from foreman.dry_run import ScriptHandler
from foreman.credentials import CredentialFailure, NotInstalled, Unanswered
from foreman.doctor import QUICK, WRITE_ACCESS, NotReady, ReadFailed, contract, repository
from foreman.github import ForgeError, LiveForge
from foreman.outcomes import Halt
from foreman.runner import issue_thread, run_live_delivery, saved_workspace
from foreman.state import ChecksState, PlanState, ReviewState, RunState, StartSettings
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
    run = MagicMock(forge=github, workspace=workspace)
    state = {"run": RunState(repo="Example/Engine", issue=8), "start": StartSettings(human_login="fixture-owner", max_tokens=100, max_budget_usd="3"), "plan": PlanState(), "checks": ChecksState(preflight="npm ci"),
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

    def test_a_related_repository_is_owner_and_name_in_github_name_characters(self):
        for name in ("Example/Api", "bookout-labs/foreman-smoke.npm", "Owner_1/.github"):
            with self.subTest(name=name):
                contract = parse_contract((MINIMAL + f"related_repositories: [{{repository: {name}, start: a}}]\n").encode())
                self.assertEqual(contract.related_repositories[0].repository, name)

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
            MINIMAL + "test_report: []\n": "test_report",
            MINIMAL + "test_report: '   '\n": "test_report",
            "check: x\nverify_skill: ' '\n": "verify_skill",
            MINIMAL + "image: [node]\n": "image",
            MINIMAL + "setup: npm ci\n": "setup",
            MINIMAL + "network: always\n": "network",
            MINIMAL + "start: {command: a, health: b}\n": "start.timeout_seconds",
            MINIMAL + "start: {command: a, health: b, timeout_seconds: '60'}\n": "start.timeout_seconds",
            MINIMAL + "related_repositories: [{repository: noslash, start: a}]\n": "related_repositories.0.repository",
            MINIMAL + "related_repositories: [{repository: ../.., start: a}]\n": "related_repositories.0.repository",
            MINIMAL + "related_repositories: [{repository: ./., start: a}]\n": "related_repositories.0.repository",
            MINIMAL + "related_repositories: [{repository: Owner/.., start: a}]\n": "related_repositories.0.repository",
            MINIMAL + "related_repositories: [{repository: Ówner/name, start: a}]\n": "related_repositories.0.repository",
            MINIMAL + "related_repositories: [{repository: A/B, start: a, commit: abc}]\n": "related_repositories.0.commit",
            MINIMAL + "related_repositories: [{repository: A/B, start: a, sha: abc}]\n": "related_repositories.0.sha",
            MINIMAL + "secrets: [database_url]\n": "secrets.0",
            MINIMAL + "secrets: [1DB]\n": "secrets.0",
            MINIMAL + "mutation: {command: a, sample_cap: 0}\n": "mutation.sample_cap",
            MINIMAL + "production_check: {command: a, not_deployed_exit: 0, timeout_seconds: 9}\n": "production_check.not_deployed_exit",
            MINIMAL + "production_check: {command: a, not_deployed_exit: 256, timeout_seconds: 9}\n": "production_check.not_deployed_exit",
            MINIMAL + "deploy: now\n": "deploy",
            MINIMAL + "check: again\n": "duplicate key 'check'",
            # A merge key would fold keys in past the strict model and the duplicate check.
            MINIMAL + "<<: {check: c}\n": "merge key '<<'",
            MINIMAL + "start: {<<: {command: a}, health: b, timeout_seconds: 1}\n": "merge key '<<'",
            MINIMAL + "? [a]\n: b\n": "not a scalar",
            # A !!map tag on a sequence or a scalar is not a mapping.
            MINIMAL + "start: !!map [1, 2]\n": "is not a mapping",
            "!!map foo\n": "is not a mapping",
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
                contract(LiveWorkspace(self.root), pinned)

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
                                       run=LiveRun(github, workspace, skills=fixture_skills))
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
                                     run=LiveRun(github, workspace, skills=fixture_skills))
        self.assertEqual((admitted["stop"], admitted["checks"].check), ("needs_human", "npm run check"))


def remove(github: FakeGitHub, workspace: FakeWorkspace, requirement: str) -> None:
    """Take one quick requirement away from a ready repository."""
    match QUICK.index(requirement):
        case 0:
            github.installation = "not_installed"
        case 1:
            github.variables = lambda repo: {}
        case 2:
            github.permissions["fixture-owner"] = "read"
        case 3:
            github.repository_labels = set()
        case 4:
            github.queue_rule = False
        case 5:
            del workspace.base_files[CONTRACT_PATH]
        case 6:
            del workspace.base_files[SKILL]


class QuickCheckTest(unittest.TestCase):
    """TR4: a scratch repository with each quick requirement removed in turn."""

    MISSING = (
        ("the GitHub App is not installed on Example/Engine", "install the Foreman GitHub App on Example/Engine's account"),
        ("the FOREMAN_HUMAN_LOGIN repository variable is not set", "set FOREMAN_HUMAN_LOGIN to the GitHub login"),
        ("the FOREMAN_HUMAN_LOGIN account fixture-owner cannot write to Example/Engine", "give fixture-owner write access to Example/Engine"),
        ("Example/Engine has no Ready label", "create the Ready label in Example/Engine"),
        ("no single merge queue rule applies to Example/Engine's base branch main", "add a ruleset that targets main in Example/Engine"),
        (f"the contract file {CONTRACT_PATH} is not a tracked file", f"commit {CONTRACT_PATH} to the default branch"),
        (f"the verify skill {SKILL} that the contract names is not a tracked regular file", f"commit the repository's verify skill at {SKILL}"),
    )

    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory())).resolve()

    def test_each_missing_requirement_is_named_with_its_fix_and_no_run_starts(self):
        for requirement, (missing, fix) in zip(QUICK, self.MISSING, strict=True):
            with self.subTest(requirement):
                checkpoint, thread = self.root / f"{QUICK.index(requirement)}.sqlite3", issue_thread("Example/Engine", 8)
                github, workspace = FakeGitHub(), FakeWorkspace(self.root)
                remove(github, workspace, requirement)
                dispatch = MagicMock(side_effect=AssertionError("not_ready makes no paid call"))
                result = run_live_delivery(repo="Example/Engine", issue=8, checkpoint=checkpoint, dispatcher=dispatch,
                                           run=LiveRun(github, workspace, skills=fixture_skills))
                self.assertEqual((result["halt"], result["path"]), (Halt.not_ready, ["admit"]))
                self.assertIn(f"Not ready: {missing}", result["halt_detail"])
                self.assertIn(f"Fix: {fix}", result["halt_detail"])
                self.assertIn(f"Not ready: {missing}", github.status[("Example/Engine", 8)])
                dispatch.assert_not_called()
                self.assertIsNone(saved_workspace(checkpoint, thread))

    def test_github_answers_the_app_installation_and_a_failure_of_the_hosts_own_key_stays_a_credential_failure(self):
        answers = (({"return_value": None}, "unconfigured"), ({"side_effect": NotInstalled("not installed")}, "not_installed"),
                   ({"return_value": {"GH_TOKEN": "synthetic"}}, "installed"))
        for effect, installation in answers:
            with self.subTest(installation), patch("foreman.github.credentials.environment", **effect):
                self.assertEqual(LiveForge().app_installation("Example/Engine"), installation)
        # GitHub's answer to the token request keeps its status. No answer at all is a ForgeError without one.
        with patch("foreman.github.credentials.environment", side_effect=CredentialFailure("App credentials: rate limit.", 403, rate_limited=True)), \
                self.assertRaises(ForgeError) as answered:
            LiveForge().app_installation("Example/Engine")
        self.assertEqual((answered.exception.status, answered.exception.answer), (403, "answered HTTP 403 for its rate limit"))
        with patch("foreman.github.credentials.environment", side_effect=Unanswered("no answer")), self.assertRaises(ForgeError) as answered:
            LiveForge().app_installation("Example/Engine")
        self.assertEqual(answered.exception.answer, "gave no answer")
        with patch("foreman.github.credentials.environment", side_effect=CredentialFailure("App key must have owner-only permissions.")), \
                self.assertRaisesRegex(CredentialFailure, "owner-only"):
            LiveForge().app_installation("Example/Engine")
        github = FakeGitHub()
        github.installation = "unconfigured"
        with self.assertRaisesRegex(NotReady, "Fix: set FOREMAN_APP_ID on the Foreman host") as raised:
            repository(github, "Example/Engine")
        self.assertEqual(raised.exception.requirement, QUICK[0])

    def test_a_rate_limit_answer_without_a_status_is_reported_as_the_rate_limit(self):
        github = FakeGitHub()
        with patch("foreman.github.credentials.environment", return_value={"GH_TOKEN": "synthetic"}), \
                patch("foreman.github.subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "GraphQL: API rate limit exceeded for installation ID 1")):
            failed = ForgeError("unread")
            try:
                LiveForge().default_branch("Example/Engine")
            except ForgeError as exc:
                failed = exc
        with patch.object(github, "default_branch", side_effect=failed), self.assertRaises(ReadFailed) as raised:
            repository(github, "Example/Engine")
        self.assertEqual(str(raised.exception), "The read of Example/Engine's default branch, for the requirement \"a merge queue ruleset "
                                                "protects the base branch\", failed because GitHub answered with its rate limit.")

    def test_a_stop_waiting_in_a_repository_that_is_not_ready_gives_not_ready_and_a_new_ready_starts_the_run(self):
        checkpoint = self.root / "runs.sqlite3"
        github, workspace = FakeGitHub(), FakeWorkspace(self.root)
        github.queue_rule = False
        github.ruling("foreman stop")
        run = LiveRun(github, workspace, skills=fixture_skills)
        refused = run_live_delivery(repo="Example/Engine", issue=8, checkpoint=checkpoint, run=run,
                                    dispatcher=MagicMock(side_effect=AssertionError("not_ready makes no paid call")))
        # Doctor's GitHub requirements come before the stop read, so the repository's state is what the issue shows.
        self.assertEqual((refused["halt"], refused["stop"]), (Halt.not_ready, Halt.not_ready))
        self.assertIn("no single merge queue rule applies", github.status[("Example/Engine", 8)])
        # The fix, then Ready removed and added again: the older stop no longer applies and the run starts.
        github.queue_rule = True
        github.ready_events.append(github.now())
        started = run_live_delivery(repo="Example/Engine", issue=8, checkpoint=checkpoint, run=run, dispatcher=ScriptHandler(triage="needs_human"))
        self.assertEqual((started.get("halt"), started["stop"]), (None, "needs_human"))

    def test_the_command_prints_each_requirement_and_exits_non_zero_on_the_first_missing_one(self):
        git(self.root, "init", "-q")
        base = commit(self.root, {CONTRACT_PATH: MINIMAL, SKILL: "# Verify\n"})

        def doctor(github):
            with patch("foreman.cli.LiveWorkspace", return_value=Pinned(self.root, base)), \
                    contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
                code = main(["doctor", "--repo", "Example/Engine"], live_run=LiveRun(github, FakeWorkspace(self.root)))
            return code, out.getvalue()

        self.assertEqual(doctor(FakeGitHub()), (0, "".join(f"ok {requirement}\n" for requirement in QUICK) + "Example/Engine is ready\n"))
        github = FakeGitHub()
        github.queue_rule = False
        code, out = doctor(github)
        self.assertEqual(code, 1)
        self.assertEqual(out.splitlines(), [f"ok {requirement}" for requirement in QUICK[:4]] + [
            "Not ready: no single merge queue rule applies to Example/Engine's base branch main. "
            "Fix: add a ruleset that targets main in Example/Engine with one merge queue rule and a merge method."])
        # A read GitHub answers with a server error names the requirement and GitHub's answer, never a missing setting.
        github = FakeGitHub()
        with patch.object(github, "has_label", side_effect=ForgeError("gh api failed", 502)), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            code = main(["doctor", "--repo", "Example/Engine"], live_run=LiveRun(github, FakeWorkspace(self.root)))
        self.assertEqual((code, err.getvalue()), (1, 'cannot check Example/Engine: The read of Example/Engine\'s Ready label, for the '
                                                    'requirement "the Ready label exists", failed because GitHub answered HTTP 502.\n'))


PERMISSIONS = json.loads((Path(__file__).parent / "fixtures/github/collaborator-permission.json").read_text())


class LoginWriteAccessTest(unittest.TestCase):
    """TR12: FOREMAN_HUMAN_LOGIN set to a login GitHub does not know, and to an account with only read access, and a permission
    read that fails. GitHub answers each read as the recorded answers, through LiveForge with App credentials."""

    def start(self, answer: dict, login: str):
        github = FakeGitHub()
        github.settings["FOREMAN_HUMAN_LOGIN"] = login
        reads = []

        def permission(repo, read_login):
            reads.append(read_login)
            with patch("foreman.github.credentials.environment", return_value={"GH_TOKEN": "synthetic"}), \
                    patch("foreman.github.subprocess.run", return_value=subprocess.CompletedProcess(
                        [], answer["exit"], json.dumps(answer["stdout"]), answer["stderr"])):
                return LiveForge().permission(repo, read_login)
        github.permission = permission
        root = Path(self.enterContext(TemporaryDirectory())).resolve()
        dispatch = MagicMock(side_effect=AssertionError("a refused start makes no paid call"))
        result = run_live_delivery(repo="Example/Engine", issue=8, checkpoint=root / "runs.sqlite3", dispatcher=dispatch,
                                   run=LiveRun(github, FakeWorkspace(root), skills=fixture_skills))
        self.assertEqual((reads, github.label_calls), ([login], []))
        dispatch.assert_not_called()
        return result, github.status[("Example/Engine", 8)]

    def test_an_unknown_login_and_a_read_only_account_stop_not_ready_with_their_own_fix(self):
        cases = {
            "unknown_login": ("GitHub does not know the FOREMAN_HUMAN_LOGIN login foreman-no-such-login-7f3a9c",
                              "correct the spelling of FOREMAN_HUMAN_LOGIN in Example/Engine's Actions variables, or name another login"),
            "outside_account_public_repo": ("the FOREMAN_HUMAN_LOGIN account fixture-outsider cannot write to Example/Engine, where GitHub "
                                            "reports its permission as read", "give fixture-outsider write access to Example/Engine, or set "
                                            "FOREMAN_HUMAN_LOGIN in Example/Engine's Actions variables to another login that has it"),
            "outside_account_private_repo": ("the FOREMAN_HUMAN_LOGIN account fixture-outsider cannot write to Example/Engine, where GitHub "
                                             "reports its permission as none", "give fixture-outsider write access to Example/Engine, or set "
                                             "FOREMAN_HUMAN_LOGIN in Example/Engine's Actions variables to another login that has it"),
        }
        for name, (missing, fix) in cases.items():
            with self.subTest(name):
                result, status = self.start(PERMISSIONS[name], PERMISSIONS[name]["login"])
                self.assertEqual(result["halt"], Halt.not_ready)
                self.assertEqual(result["halt_detail"], f"Not ready: {missing}. Fix: {fix}. Then remove and add the Ready label.")
                self.assertIn("Foreman did not start this run (not_ready).", status)

    def test_an_account_that_can_write_passes(self):
        answer = PERMISSIONS["owner"]
        with patch("foreman.github.credentials.environment", return_value={"GH_TOKEN": "synthetic"}), \
                patch("foreman.github.subprocess.run", return_value=subprocess.CompletedProcess([], 0, json.dumps(answer["stdout"]), "")) as gh:
            self.assertEqual(LiveForge().permission("bookout-labs/foreman-smoke", "fixture-owner"), "admin")
        self.assertEqual(gh.call_args.args[0], ["gh", "api", "repos/bookout-labs/foreman-smoke/collaborators/fixture-owner/permission"])
        github = FakeGitHub()
        for permission in ("admin", "write"):
            github.permissions["fixture-owner"] = permission
            self.assertEqual(repository(github, "Example/Engine").start.human_login, "fixture-owner")

    def test_a_failed_permission_read_is_a_failed_read_that_does_not_say_to_change_the_login(self):
        answer = {"exit": 1, "stdout": "", "stderr": "gh: Server Error (HTTP 502)"}
        result, status = self.start(answer, "fixture-owner")
        self.assertEqual(result["halt"], Halt.git_failed)
        self.assertEqual(result["halt_detail"], f'The read of fixture-owner\'s permission on Example/Engine, for the requirement "{WRITE_ACCESS}", '
                                                "failed because GitHub answered HTTP 502. No setting needs to change. "
                                                "Remove and add the Ready label to try again.")
        self.assertIn("Foreman did not start this run (git_failed).", status)
        self.assertNotIn("FOREMAN_HUMAN_LOGIN in", status)


if __name__ == "__main__":
    unittest.main()

"""Doctor's quick requirements, its command, and admission reading the contract at the pinned base (target.yaml target_contract, doctor)."""
import tests

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
from tempfile import TemporaryDirectory
import unittest
import unittest.mock
from unittest.mock import MagicMock, patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from langgraph.checkpoint.sqlite import SqliteSaver

from foreman.cli import SETUP_PAGE, main, setup_page
from foreman.contract import CONTRACT_PATH, ContractError, parse_contract
from foreman.delivery import LiveRun
from foreman.dry_run import ScriptHandler
from foreman.credentials import CredentialFailure, NotInstalled, Subscription, Unanswered
from foreman.doctor import CHECK, EVENTS, PROBE, FULL as FULL_CHECK, QUICK, REQUIREMENTS, SETUP, START as APP_START, WRITE_ACCESS, GreenChecks, NotReady, ReadFailed, contract, repository
from foreman.github import BranchProbe, ForgeError, LiveForge
from foreman.outcomes import Halt
from foreman.runner import issue_thread, run_live_delivery, saved_workspace
from foreman.state import ChecksState, PlanState, ReviewState, RunState, StartSettings
from foreman.steps import triage
from foreman.workspace import LiveWorkspace
from skill_fixture import fixture_skills
from publication_fixture import SUBSCRIBED
from test_runner import FakeGitHub, FakeWorkspace

SKILL = ".claude/skills/verify-web/SKILL.md"
START = "start: {command: npm start, health: curl -sf localhost:3000, timeout_seconds: 60}\n"
MINIMAL = f"check: npm run check\nverify_skill: {SKILL}\nimage: node:22\nsetup: []\nsecrets: []\n{START}"
# The contract without its start, for a case that writes its own.
STARTLESS = MINIMAL.removesuffix(START)
# A contract whose full part runs on the host through the container double: setup writes a file with the test value, the
# check reads it, and the app's start writes the file its health command waits for.
HOST_CONTRACT = f"""check: test "$(cat built)" = "$TOKEN"
verify_skill: {SKILL}
image: node:22
setup: ['printf %s "$TOKEN" > built']
secrets: [TOKEN]
start: {{command: sleep 1 && touch started, health: test -f started, timeout_seconds: 30}}
"""
FULL = f"""check: npm run check
check_timeout_seconds: 3600
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
    state = {"run": RunState(repo="Example/Engine", issue=8), "start": StartSettings(human_login="fixture-owner", max_tokens=100, max_budget_usd="3"), "plan": PlanState(), "checks": ChecksState(),
             "review": ReviewState(), "path": []}
    return triage.admit(state, run)


class ContractTest(unittest.TestCase):
    def test_a_full_contract_parses_each_field_with_its_drawn_type(self):
        contract = parse_contract(FULL.encode())
        self.assertEqual((contract.check, contract.check_timeout_seconds, contract.verify_skill, contract.network),
                         ("npm run check", 3600, SKILL, "run"))
        self.assertEqual((contract.image, contract.setup), ("node:22", ["npm ci"]))
        self.assertEqual(contract.test_report, ["reports/unit.xml", "reports/e2e.xml"])
        self.assertEqual((contract.related_repositories[0].repository, contract.secrets), ("Example/Api", ["DATABASE_URL", "AUTH0_SECRET"]))
        self.assertEqual((contract.start and contract.start.timeout_seconds, contract.mutation and contract.mutation.sample_cap,
                          contract.production_check and contract.production_check.not_deployed_exit), (60, 20, 3))

    def test_a_related_repository_is_owner_and_name_in_github_name_characters(self):
        for name in ("Example/Api", "bookout-labs/foreman-smoke.npm", "Owner_1/.github"):
            with self.subTest(name=name):
                contract = parse_contract((MINIMAL + f"related_repositories: [{{repository: {name}, start: a}}]\n").encode())
                self.assertEqual(contract.related_repositories[0].repository, name)

    def test_check_verify_skill_image_setup_secrets_and_start_are_required(self):
        contract = parse_contract(MINIMAL.encode())
        # A field with a default stays optional: network and the check's time limit.
        self.assertEqual((contract.check_timeout_seconds, contract.network, contract.related_repositories,
                          contract.mutation, contract.production_check, contract.test_report),
                         (1800, "setup", [], None, None, None))
        self.assertEqual(parse_contract(MINIMAL.encode() + b"test_report: reports/unit.xml\n").test_report, "reports/unit.xml")
        for field in ("image", "setup", "secrets", "start"):
            with self.subTest(field=field), self.assertRaisesRegex(ContractError, f"^{field}: Field required$"):
                parse_contract("".join(line for line in MINIMAL.splitlines(keepends=True) if not line.startswith(field)).encode())

    def test_each_refinement_names_its_field(self):
        cases = {
            "check: ''\nverify_skill: a.md\n": "check",
            f"verify_skill: {SKILL}\n": "check",
            "check: x\n": "verify_skill",
            "check: 7\nverify_skill: a.md\n": "check",
            "check: x\nverify_skill: /abs/SKILL.md\n": "verify_skill",
            "check: x\nverify_skill: ../SKILL.md\n": "verify_skill",
            "check: x\nverify_skill: a//SKILL.md\n": "verify_skill",
            MINIMAL + "check_timeout_seconds: 0\n": "check_timeout_seconds",
            MINIMAL + "check_timeout_seconds: '600'\n": "check_timeout_seconds",
            MINIMAL + "test_report: /tmp/report.xml\n": "test_report",
            MINIMAL + "test_report: [../report.xml]\n": "test_report",
            MINIMAL + "test_report: []\n": "test_report",
            MINIMAL + "test_report: '   '\n": "test_report",
            "check: x\nverify_skill: ' '\n": "verify_skill",
            MINIMAL.replace("image: node:22", "image: [node]"): "image",
            MINIMAL.replace("setup: []", "setup: npm ci"): "setup",
            MINIMAL + "network: always\n": "network",
            STARTLESS + "start: {command: a, health: b}\n": "start.timeout_seconds",
            STARTLESS + "start: {command: a, health: b, timeout_seconds: '60'}\n": "start.timeout_seconds",
            MINIMAL + "related_repositories: [{repository: noslash, start: a}]\n": "related_repositories.0.repository",
            MINIMAL + "related_repositories: [{repository: ../.., start: a}]\n": "related_repositories.0.repository",
            MINIMAL + "related_repositories: [{repository: ./., start: a}]\n": "related_repositories.0.repository",
            MINIMAL + "related_repositories: [{repository: Owner/.., start: a}]\n": "related_repositories.0.repository",
            MINIMAL + "related_repositories: [{repository: Ówner/name, start: a}]\n": "related_repositories.0.repository",
            MINIMAL + "related_repositories: [{repository: A/B, start: a, commit: abc}]\n": "related_repositories.0.commit",
            MINIMAL + "related_repositories: [{repository: A/B, start: a, sha: abc}]\n": "related_repositories.0.sha",
            MINIMAL.replace("secrets: []", "secrets: [database_url]"): "secrets.0",
            MINIMAL.replace("secrets: []", "secrets: [1DB]"): "secrets.0",
            MINIMAL + "mutation: {command: a, sample_cap: 0}\n": "mutation.sample_cap",
            MINIMAL + "production_check: {command: a, not_deployed_exit: 0, timeout_seconds: 9}\n": "production_check.not_deployed_exit",
            MINIMAL + "production_check: {command: a, not_deployed_exit: 256, timeout_seconds: 9}\n": "production_check.not_deployed_exit",
            MINIMAL + "deploy: now\n": "deploy",
            MINIMAL + "check: again\n": "duplicate key 'check'",
            # A merge key would fold keys in past the strict model and the duplicate check.
            MINIMAL + "<<: {check: c}\n": "merge key '<<'",
            STARTLESS + "start: {<<: {command: a}, health: b, timeout_seconds: 1}\n": "merge key '<<'",
            MINIMAL + "? [a]\n: b\n": "not a scalar",
            # A !!map tag on a sequence or a scalar is not a mapping.
            STARTLESS + "start: !!map [1, 2]\n": "is not a mapping",
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
        base = commit(self.root, {CONTRACT_PATH: MINIMAL.replace("npm run check", "check-a"), SKILL: "# Verify\n"})
        git(self.root, "switch", "-qc", "8-items")
        commit(self.root, {CONTRACT_PATH: MINIMAL.replace("npm run check", "check-b")})
        workspace = Pinned(self.root, base)
        update = admit(workspace)
        self.assertNotIn("halt", update)
        self.assertEqual(update["checks"], ChecksState(check="check-a", verify_skill=SKILL))
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

    def test_each_event_subscription_the_app_lacks_is_named_with_the_permission_it_needs_and_no_run_starts(self):
        """TR4: the App's installation without each of its six event subscriptions in turn. Doctor names each missing event and
        the App permission GitHub delivers it under, and the start writes nothing but the status."""
        for event, permission in EVENTS.items():
            with self.subTest(event):
                github, workspace = FakeGitHub(), FakeWorkspace(self.root)
                github.subscription = SUBSCRIBED.model_copy(update={"events": frozenset(EVENTS) - {event}})
                checkpoint = self.root / f"{event}.sqlite3"
                dispatch = MagicMock(side_effect=AssertionError("not_ready makes no paid call"))
                result = run_live_delivery(repo="Example/Engine", issue=8, checkpoint=checkpoint, dispatcher=dispatch,
                                           run=LiveRun(github, workspace, skills=fixture_skills))
                self.assertEqual(result["halt"], Halt.not_ready)
                self.assertIn(f"Not ready: the GitHub App's installation on Example/Engine is not subscribed to {event}. "
                              f"Fix: in the Foreman GitHub App's settings, subscribe to {event} (permission {permission})", result["halt_detail"])
                self.assertEqual((github.variable_reads, github.label_calls), ([], []))
                dispatch.assert_not_called()
        github = FakeGitHub()
        github.subscription = SUBSCRIBED.model_copy(update={"events": frozenset({"issues"})})
        with self.assertRaises(NotReady) as raised:
            repository(github, "Example/Engine")
        self.assertIn("not subscribed to issue_comment, pull_request, pull_request_review, check_suite, status", str(raised.exception))

    def test_each_event_whose_permission_the_app_holds_below_read_is_named_with_that_permission_and_no_run_starts(self):
        """The App subscribed to every event, with the permission each is delivered under absent or none in turn. Doctor names
        the events that permission carries and the permission at read, and the start writes nothing but the status. Write
        passes, as Foreman's own writes need it."""
        for grant in {grant.key: grant for grant in EVENTS.values()}.values():
            events = [event for event, needed in EVENTS.items() if needed == grant]
            for level in (None, "none"):
                with self.subTest(grant.key, level=level):
                    github, workspace = FakeGitHub(), FakeWorkspace(self.root)
                    permissions = {key: value for key, value in SUBSCRIBED.permissions.items() if key != grant.key}
                    github.subscription = SUBSCRIBED.model_copy(update={"permissions": permissions | ({grant.key: level} if level else {})})
                    checkpoint = self.root / f"{grant.key}-{level}.sqlite3"
                    dispatch = MagicMock(side_effect=AssertionError("not_ready makes no paid call"))
                    result = run_live_delivery(repo="Example/Engine", issue=8, checkpoint=checkpoint, dispatcher=dispatch,
                                               run=LiveRun(github, workspace, skills=fixture_skills))
                    self.assertEqual(result["halt"], Halt.not_ready)
                    self.assertIn(f"Not ready: the GitHub App's installation on Example/Engine cannot read {', '.join(events)}. "
                                  "Fix: in the Foreman GitHub App's settings, grant "
                                  + ", ".join(f"{grant.name}: read for {event}" for event in events), result["halt_detail"])
                    self.assertEqual((github.variable_reads, github.label_calls), ([], []))
                    dispatch.assert_not_called()
        github = FakeGitHub()
        github.subscription = SUBSCRIBED.model_copy(update={"permissions": {key: "write" for key in SUBSCRIBED.permissions}})
        self.assertEqual(repository(github, "Example/Engine").human_login, "fixture-owner")

    def test_the_app_events_and_permissions_are_read_from_its_installation_on_the_repository(self):
        key = self.root / "app.pem"
        key.write_bytes(rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        key.chmod(0o600)
        urls = []

        def respond(request, timeout):
            urls.append(request.full_url)
            response = MagicMock()
            response.__enter__.return_value.read.return_value = json.dumps(
                {"id": 2, "events": ["issues", "status"], "permissions": {"issues": "write", "statuses": "read"}}).encode()
            return response
        with patch.dict(os.environ, {"FOREMAN_APP_ID": "1", "FOREMAN_APP_KEY_PATH": str(key)}), \
                patch("foreman.credentials.urlopen", side_effect=respond):
            self.assertEqual(LiveForge().app_subscription("Example/Engine"),
                             Subscription(events=frozenset({"issues", "status"}), permissions={"issues": "write", "statuses": "read"}))
        self.assertEqual(urls, ["https://api.github.com/repos/Example/Engine/installation"])
        with patch("foreman.github.credentials.subscription", side_effect=Unanswered("no answer")), self.assertRaises(ForgeError):
            LiveForge().app_subscription("Example/Engine")
        with patch("foreman.github.credentials.subscription", side_effect=CredentialFailure("App credentials: permission denied.", 403)), \
                self.assertRaises(ForgeError) as refused:
            LiveForge().app_subscription("Example/Engine")
        self.assertEqual(refused.exception.status, 403)

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
        base = commit(self.root, {CONTRACT_PATH: HOST_CONTRACT, SKILL: "# Verify\n"})
        store = self.root / "runs.sqlite3"
        self.enterContext(patch.dict(os.environ, {"FOREMAN_TEST_TOKEN": "fixture-token"}))

        def doctor(github):
            with patch("foreman.cli.LiveWorkspace", return_value=Pinned(self.root, base)), \
                    contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
                code = main(["doctor", "--repo", "Example/Engine"], checkpoint=store, live_run=LiveRun(github, FakeWorkspace(self.root)))
            return code, out.getvalue()

        self.assertEqual(doctor(FakeGitHub()), (0, "".join(f"ok {requirement}\n" for requirement in REQUIREMENTS) + "Example/Engine is ready\n"))
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
            code = main(["doctor", "--repo", "Example/Engine"], checkpoint=store, live_run=LiveRun(github, FakeWorkspace(self.root)))
        self.assertEqual((code, err.getvalue()), (1, 'cannot check Example/Engine: The read of Example/Engine\'s Ready label, for the '
                                                    'requirement "the Ready label exists", failed because GitHub answered HTTP 502.\n'))


class FullCheckTest(unittest.TestCase):
    """doctor.full: the command runs the quick part, then the full part at the default branch's head, and a pass records green."""

    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory())).resolve()
        self.store = self.root / "runs.sqlite3"
        self.enterContext(patch.dict(os.environ, {"FOREMAN_TEST_TOKEN": "fixture-token"}))
        git(self.root, "init", "-q")

    def doctor(self, text, github=None):
        base = commit(self.root, {CONTRACT_PATH: text, SKILL: "# Verify\n"})
        workspace, self.github = Pinned(self.root, base), github or FakeGitHub()
        with patch("foreman.cli.LiveWorkspace", return_value=workspace), \
                contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
            code = main(["doctor", "--repo", "Example/Engine"], checkpoint=self.store, live_run=LiveRun(self.github, FakeWorkspace(self.root)))
        with contextlib.closing(sqlite3.connect(self.store)) as connection:
            green = GreenChecks(connection).green("Example/Engine", hashlib.sha256(text.encode()).hexdigest())
        return code, out.getvalue().splitlines(), green, workspace.containers

    def test_a_pass_builds_the_image_runs_setup_with_the_test_values_the_check_and_the_app_and_records_green(self):
        code, lines, green, containers = self.doctor(HOST_CONTRACT)
        self.assertEqual((code, lines), (0, [f"ok {requirement}" for requirement in REQUIREMENTS] + ["Example/Engine is ready"]))
        self.assertTrue(green)
        [(name, _)] = containers.created
        self.assertEqual((containers.specs[name].image, containers.specs[name].secrets), ("node:22", {"TOKEN": "fixture-token"}))
        self.assertEqual([ran.command for ran in containers.setups(name)][:2], ['printf %s "$TOKEN" > built', 'test "$(cat built)" = "$TOKEN"'])
        # The health command was asked until the app's start wrote its file, and the container is gone.
        self.assertGreater([ran.command for ran in containers.ran].count("test -f started"), 1)
        self.assertEqual(containers.stopped, [name])

    def test_each_failed_full_requirement_is_named_with_its_fix_and_records_nothing(self):
        cases = {
            SETUP: (HOST_CONTRACT.replace("printf %s", "exit 4; printf %s"), "Not ready: Setup command 'exit 4; printf %s \"$TOKEN\" > built' exited with 4",
                    f"Fix: correct the setup commands in {CONTRACT_PATH}"),
            CHECK: (HOST_CONTRACT.replace('= "$TOKEN"', '= other'), "Not ready: the check 'test \"$(cat built)\" = other' exited with 1 on the default branch",
                    "Fix: make 'test \"$(cat built)\" = other' pass on the default branch."),
            APP_START: (HOST_CONTRACT.replace("timeout_seconds: 30", "timeout_seconds: 1").replace("touch started", "true"),
                        "Not ready: the health command 'test -f started' did not answer within 1 seconds of 'sleep 1 && true'",
                        "Fix: make 'sleep 1 && true' start the app so 'test -f started' exits 0 within start.timeout_seconds."),
        }
        for requirement, (text, missing, fix) in cases.items():
            with self.subTest(requirement):
                code, lines, green, containers = self.doctor(text)
                self.assertEqual((code, green), (1, False))
                passed = REQUIREMENTS.index(requirement)
                self.assertEqual(lines[:passed], [f"ok {passed}" for passed in REQUIREMENTS[:passed]])
                # A setup command's output follows its exit, so the report may run over several lines.
                self.assertTrue(lines[passed].startswith(missing), lines[passed])
                self.assertIn(fix, lines[-1])
                self.assertEqual(containers.stopped, [name for name, _ in containers.created])

    def test_green_is_recorded_only_when_the_probe_saw_both_pushes_refused(self):
        """S27: the probe arms a synthetic PR at the default branch's head and tries to push as the App and an outside writer."""
        self.assertEqual(self.doctor(HOST_CONTRACT)[:1], (0,))
        self.assertEqual(self.github.probes, [("Example/Engine", "main", git(self.root, "rev-parse", "HEAD"))])
        for app, outside, who in ((True, False, "the App"), (False, True, "an outside writer"), (True, True, "the App and an outside writer")):
            with self.subTest(who):
                github = FakeGitHub()
                github.probe = BranchProbe(app_moved=app, outside_moved=outside)
                code, lines, green, _ = self.doctor(HOST_CONTRACT + f"# {who}\n", github)
                self.assertEqual((code, green), (1, False))
                self.assertEqual(lines[:-1], [f"ok {passed}" for passed in REQUIREMENTS[:REQUIREMENTS.index(PROBE)]])
                self.assertEqual(lines[-1], f"Not ready: {who} moved the source branch of Example/Engine's armed synthetic pull "
                                            "request. Fix: protect pull request source branches so that GitHub refuses every push "
                                            "while a merge request is open.")

    def test_the_live_probe_arms_the_pull_request_tries_both_writers_then_closes_and_deletes_it(self):
        calls, human = [], []

        def app(repo, command, **kwargs):
            calls.append(command)
            match command:
                case [*_, "--jq", ".tree.sha"]:
                    return "tree\n"
                case [*_, "--jq", ".sha"]:
                    return "c" * 40 + "\n"
                case [*_, "--jq", ".object.sha"]:
                    return "c" * 40 + "\n"
                case [_, _, "--method", "PATCH", *_]:
                    raise ForgeError("gh api failed", 422)
            return ""

        def outside(args):
            human.append(args)
            refused = args[:3] == ["api", "--method", "PATCH"]
            return subprocess.CompletedProcess(args, 1 if refused else 0, "d" * 40 + "\n", "HTTP 403" if refused else "")

        forge = LiveForge()
        pull = MagicMock(number=41)
        with patch.object(forge, "_run_command", side_effect=app), patch("foreman.github.human_gh", side_effect=outside), \
                patch.object(forge, "open_pull_request") as opened, patch.object(forge, "pull_request_for", return_value=pull), \
                patch.object(forge, "close_pull_request") as closed:
            self.assertEqual(forge.branch_probe("Example/Engine", "main", "b" * 40), BranchProbe(app_moved=False, outside_moved=False))
        branch = opened.call_args.args[1]
        self.assertTrue(branch.startswith("foreman-probe-"))
        self.assertIn(["gh", "pr", "merge", "41", "--repo", "Example/Engine", "--auto", "--squash", "--match-head-commit", "c" * 40], calls)
        # The App's move comes after the arm, the outside writer's after it, and both target the branch.
        self.assertEqual([command[4] for command in calls if command[2:4] == ["--method", "PATCH"]], [f"repos/Example/Engine/git/refs/heads/{branch}"])
        self.assertEqual([args[3] for args in human if args[1:3] == ["--method", "PATCH"]], [f"repos/Example/Engine/git/refs/heads/{branch}"])
        closed.assert_called_once()
        self.assertEqual(calls[-1], ["gh", "api", "--method", "DELETE", f"repos/Example/Engine/git/refs/heads/{branch}"])

    def test_a_check_that_writes_no_test_report_fails_the_check_requirement(self):
        code, lines, green, _ = self.doctor(HOST_CONTRACT + "test_report: reports/unit.xml\n")
        self.assertEqual((code, green), (1, False))
        self.assertEqual(lines[-1], f"Not ready: the check 'test \"$(cat built)\" = \"$TOKEN\"' wrote no test report at reports/unit.xml. "
                                    f"Fix: make the check write its JUnit XML report at reports/unit.xml, or correct test_report in {CONTRACT_PATH}.")


class FullCheckWorkspace(FakeWorkspace):
    """A checkout whose base container answers each command 0, except those that fail, and keeps each build."""

    def __init__(self, cwd, fails=()):
        super().__init__(cwd, check="npm run check")
        self.fails, self.built = set(fails), []

    def container_at(self, commit, *, base, log):
        self.built.append(commit)
        return contextlib.nullcontext(lambda command, seconds: 3 if command in self.fails else 0)


class AdmissionFullCheckTest(unittest.TestCase):
    """TR7: a contract file change at the base, with a passing full check and with a failing check command."""

    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory())).resolve()
        self.checkpoint = self.root / "runs.sqlite3"

    def admit(self, workspace, issue, dispatcher):
        return run_live_delivery(repo="Example/Engine", issue=issue, checkpoint=self.checkpoint, dispatcher=dispatcher,
                                 run=LiveRun(FakeGitHub(), workspace, skills=fixture_skills))

    def green(self, workspace):
        with contextlib.closing(sqlite3.connect(self.checkpoint)) as connection:
            return GreenChecks(connection).green("example/engine", hashlib.sha256(workspace.base_files[CONTRACT_PATH]).hexdigest())

    def test_a_passing_full_check_runs_once_per_contract_digest_and_is_recorded_green_before_admitting(self):
        workspace = FullCheckWorkspace(self.root)
        admitted = self.admit(workspace, 8, ScriptHandler(triage="needs_human"))
        self.assertEqual((admitted.get("halt"), admitted["stop"], len(workspace.built)), (None, "needs_human", 1))
        self.assertTrue(self.green(workspace))
        # An admission whose digest already has a green record runs no container.
        self.assertEqual(self.admit(workspace, 9, ScriptHandler(triage="needs_human"))["stop"], "needs_human")
        self.assertEqual(len(workspace.built), 1)
        # A change to the contract file is checked once at the next admission.
        workspace.base_files[CONTRACT_PATH] += b"check_timeout_seconds: 600\n"
        self.assertFalse(self.green(workspace))
        self.assertEqual(self.admit(workspace, 10, ScriptHandler(triage="needs_human"))["stop"], "needs_human")
        self.assertEqual(len(workspace.built), 2)
        self.assertTrue(self.green(workspace))

    def test_a_failing_check_command_stops_not_ready_naming_the_check_and_no_run_or_paid_call_starts(self):
        workspace = FullCheckWorkspace(self.root, fails={"npm run check"})
        dispatch = MagicMock(side_effect=AssertionError("not_ready makes no paid call"))
        result = self.admit(workspace, 8, dispatch)
        self.assertEqual((result["halt"], result["path"]), (Halt.not_ready, ["admit"]))
        self.assertIn("Not ready: the check 'npm run check' exited with 3 on the default branch", result["halt_detail"])
        self.assertIn("Fix: make 'npm run check' pass on the default branch.", result["halt_detail"])
        dispatch.assert_not_called()
        self.assertIsNone(saved_workspace(self.checkpoint, issue_thread("Example/Engine", 8)))
        self.assertFalse(self.green(workspace))


class SetupPageTest(unittest.TestCase):
    """TR5: docs/target-repo-setup.md is generated from doctor's requirement list, and differs from it by no requirement."""

    PAGE = Path(__file__).parent.parent / SETUP_PAGE

    def test_the_page_lists_each_quick_and_full_requirement_with_why_and_how_as_generated(self):
        page = self.PAGE.read_text()
        self.assertEqual(page, setup_page(QUICK, FULL_CHECK), "run foreman setup-page --write")
        for requirement in REQUIREMENTS:
            with self.subTest(requirement):
                self.assertIn(f"{requirement.text[1:]}\n\n**Why:** {requirement.why}\n\n**How:** {requirement.how}\n", page)

    def test_a_list_that_differs_from_the_page_by_one_requirement_fails_the_page_test(self):
        page = self.PAGE.read_text()
        for index in range(len(REQUIREMENTS)):
            with self.subTest(REQUIREMENTS[index]):
                listed = REQUIREMENTS[:index] + REQUIREMENTS[index + 1:]
                self.assertNotEqual(page, setup_page(listed[:len(QUICK) - (index < len(QUICK))], listed[len(QUICK) - (index < len(QUICK)):]))

    def test_the_command_writes_the_page(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        (root / "docs").mkdir()
        with patch("foreman.cli.Path.cwd", return_value=root):
            self.assertEqual(main(["setup-page", "--write"]), 0)
        self.assertEqual((root / SETUP_PAGE).read_text(), setup_page(QUICK, FULL_CHECK))


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
            self.assertEqual(repository(github, "Example/Engine").human_login, "fixture-owner")

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

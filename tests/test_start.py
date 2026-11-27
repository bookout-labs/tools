import contextlib
from decimal import Decimal
import fcntl
import io
import json
import os
import subprocess
import threading
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from langgraph.checkpoint.sqlite import SqliteSaver

from foreman.cli import main
from foreman.contract import CONTRACT_PATH
from foreman.delivery import LiveRun
from foreman.doctor import DEFAULT_BUDGET_USD, DEFAULT_TOKEN_CAP, operator_variables, repository
from foreman.credentials import AppCredentials, credentials
from foreman.github import LiveForge
from foreman.report import run_report
from foreman.runner import IssueLocked, issue_lock, issue_thread, ready_resumes, released_issues, saved_run, saved_workspace
from foreman.stage_output import spec_digest
from foreman.state import ChecksState, StartSettings, checkpoint_serializer, checkpoint_store
from foreman.steps import CANCELLATION
from foreman.usd import BudgetUsd
from foreman.delivery import shape
from foreman.github import ForgeError
from foreman.skill_rules import SkillMissing
from foreman.workspace import WorkspaceError, WorktreeState, lock_file
from foreman.worker import AttemptJournal
from foreman.budget import claude_spend, work_tokens
from skill_fixture import fixture_skills
from publication_fixture import VERIFY_SKILL_PATH
from publication_fixture import BRANCH
from test_runner import APPROVE, FINDING, HEAD, FakeGitHub, FakeWorkspace, WakeTest, check_delivery, checker_reply, comment

THREAD = "example/engine#8"
SETTINGS = {"FOREMAN_HUMAN_LOGIN": "fixture-owner"}


class StartTest(unittest.TestCase):
    def setUp(self):
        # The checkpoint sits beside the checkout, as Foreman's state directory must.
        base = Path(self.enterContext(TemporaryDirectory()))
        self.root, self.store = base / "checkout", base / "runs.sqlite3"
        self.root.mkdir()
        self.enterContext(patch.dict("os.environ", {"XDG_RUNTIME_DIR": str(self.root / "runtime"), "FOREMAN_APP_ID": "1"}))
        self.app = self.enterContext(patch("foreman.cli.credentials.environment", return_value={"GH_TOKEN": "synthetic"}))
        self.forge = FakeGitHub()
        self.workspace = FakeWorkspace(self.root)
        self.worker_calls = 0

    def start(self, variables=SETTINGS, replies=()):
        """Start issue 8 with the repository's variables, or with the forge's own variables read when they are None."""
        outputs = iter(replies)

        def worker(args, cwd, env):
            self.worker_calls += 1
            return self.workspace.reply(args, checker_reply(args) or next(outputs))

        settings = contextlib.nullcontext() if variables is None else patch.object(
            self.forge, "variables", side_effect=lambda repo: dict(variables))
        with settings, contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            code = main(["start", "--repo", "Example/Engine", "--issue", "8"], checkpoint=self.store,
                        live_run=LiveRun(self.forge, self.workspace, skills=fixture_skills), worker_runner=worker)
        return code, out.getvalue(), err.getvalue()

    def github_answers(self, method, stderr):
        """The forge's read made as LiveForge makes it with App credentials, with gh exiting on GitHub's answer. It counts each read."""
        reads = []

        def read(*args, **kwargs):
            reads.append(args)
            with patch("foreman.github.subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", stderr)):
                return getattr(LiveForge(), method)(*args, **kwargs)
        return patch.object(self.forge, method, side_effect=read), reads

    def token_answers(self, status):
        """The App's installation read as LiveForge makes it, with GitHub answering the App's token request with this status."""
        key = self.root / "app.pem"
        if not key.exists():
            key.write_bytes(rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
            key.chmod(0o600)
        reads = []

        def respond(request, timeout):
            if not request.full_url.endswith("/installation"):
                raise HTTPError(request.full_url, status, "GitHub's answer", {}, None)
            response = MagicMock()
            response.__enter__.return_value.read.return_value = json.dumps({"id": 2}).encode()
            return response

        def installation(repo):
            reads.append(repo)
            with patch.dict(os.environ, {"FOREMAN_APP_KEY_PATH": str(key)}), patch("foreman.credentials.urlopen", side_effect=respond), \
                    patch.object(credentials, "environment", AppCredentials().environment):
                return LiveForge().app_installation(repo)
        return patch.object(self.forge, "app_installation", side_effect=installation), reads

    def stored(self):
        with SqliteSaver.from_conn_string(str(self.store)) as saver:
            saver.serde = checkpoint_serializer()
            values = saver.get_tuple({"configurable": {"thread_id": THREAD}}).checkpoint["channel_values"]
        return values["start"], values["checks"]

    def test_starts_once_with_the_repository_settings_pinned(self):
        # The check comes from the contract file at the pinned base. A leftover FOREMAN_CHECK or FOREMAN_PREFLIGHT is never read.
        variables = {**SETTINGS, "FOREMAN_CHECK": "exit 9", "FOREMAN_PREFLIGHT": "exit 9", "FOREMAN_MAX_TOKENS": "900000"}
        code, out, err = self.start(variables, [{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE])
        self.assertNotEqual(code, 1, out + err)
        start, checks = self.stored()
        self.assertEqual(checks, ChecksState(check="exit 0", verify_skill=VERIFY_SKILL_PATH, check_exit_code=0, verify="pass",
                                             checked_tree=checks.checked_tree, verified_head=checks.verified_head))
        self.assertEqual((start.max_tokens, start.max_budget_usd), (900000, "3"))
        calls = self.worker_calls
        # A repeated delivery for a finished run is a no-op.
        self.assertEqual(self.start(variables)[:2], (0, f"already started {THREAD}\n"))
        self.assertEqual(self.worker_calls, calls)

    def test_a_started_run_resumes_and_run_takes_no_check_option(self):
        self.start(SETTINGS, [{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE])
        self.assertEqual(saved_workspace(self.store, THREAD), str(self.root.resolve()))
        self.assertIsNone(saved_workspace(self.store, "example/engine#9"))
        live = LiveRun(self.forge, self.workspace, skills=fixture_skills)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertNotEqual(main(["run", "delivery", "--repo", "Example/Engine", "--issue", "8"], checkpoint=self.store, live_run=live), 2)
            with self.assertRaises(SystemExit) as refused:
                main(["run", "delivery", "--repo", "Example/Engine", "--issue", "9", "--check", "exit 0"], checkpoint=self.store, live_run=live)
        self.assertEqual(refused.exception.code, 2)

    def test_settings_default_and_parse(self):
        self.assertEqual(operator_variables("Example/Engine", {**SETTINGS, "FOREMAN_MAX_BUDGET_USD": "4.50"}),
                         StartSettings(human_login="fixture-owner", max_tokens=DEFAULT_TOKEN_CAP, max_budget_usd="4.5"))
        self.assertEqual(operator_variables("Example/Engine", SETTINGS),
                         StartSettings(human_login="fixture-owner", max_tokens=DEFAULT_TOKEN_CAP, max_budget_usd="3"))
        # The drawing's defaults for an unset cap: 3 dollars and 20,000,000 work tokens.
        self.assertEqual((DEFAULT_BUDGET_USD, DEFAULT_TOKEN_CAP), (BudgetUsd(Decimal("3")), 20_000_000))
        self.assertEqual(self.start(SETTINGS, [{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE])[0], 0)
        self.assertEqual((self.stored()[0].max_tokens, self.stored()[0].max_budget_usd), (DEFAULT_TOKEN_CAP, "3"))

    def test_run_settings_read_only_the_operator_variables(self):
        reads = []

        class Recorded(dict):
            def get(self, key, default=None):
                reads.append(key)
                return super().get(key, default)

        operator_variables("Example/Engine", Recorded({**SETTINGS, "FOREMAN_CHECK": "exit 0", "FOREMAN_MERGE_HOLD": "x"}))
        # The contract's setup commands replaced FOREMAN_PREFLIGHT. FOREMAN_MERGE_HOLD is read live at arm.
        self.assertEqual(set(reads), {"FOREMAN_HUMAN_LOGIN", "FOREMAN_MAX_TOKENS", "FOREMAN_MAX_BUDGET_USD"})

    def test_missing_or_malformed_settings_stop_admission_not_ready_before_any_paid_call(self):
        # Doctor's quick check names each setting at admission, in place of the start-time errors, and Ready stays.
        cases = [({}, "the FOREMAN_HUMAN_LOGIN repository variable is not set"),
                 ({"FOREMAN_CHECK": "exit 0"}, "the FOREMAN_HUMAN_LOGIN repository variable is not set"),
                 ({**SETTINGS, "FOREMAN_HUMAN_LOGIN": " "}, "the FOREMAN_HUMAN_LOGIN repository variable is not set"),
                 ({**SETTINGS, "FOREMAN_MAX_TOKENS": "many"}, "the FOREMAN_MAX_TOKENS repository variable 'many' is not a positive integer"),
                 ({**SETTINGS, "FOREMAN_MAX_TOKENS": "0"}, "the FOREMAN_MAX_TOKENS repository variable '0' is not a positive integer"),
                 ({**SETTINGS, "FOREMAN_MAX_BUDGET_USD": "-1"}, "the FOREMAN_MAX_BUDGET_USD repository variable '-1' is not a positive dollar amount"),
                 ({**SETTINGS, "FOREMAN_MAX_BUDGET_USD": ""}, "the FOREMAN_MAX_BUDGET_USD repository variable '' is not a positive dollar amount")]
        for variables, missing in cases:
            with self.subTest(variables=variables):
                code, out, err = self.start(variables)
                self.assertEqual(code, 1, out + err)
                self.assertIn("done not_ready", out)
                status = self.forge.status[("Example/Engine", 8)]
                self.assertIn(f"Foreman did not start this run (not_ready).\n\nNot ready: {missing}. Fix: ", status)
                self.assertIn("in Example/Engine's Actions variables. Then remove and add the Ready label.", status)
                self.assertEqual((self.worker_calls, self.forge.label_calls), (0, []))
                self.assertEqual(self.forge.load_issue("Example/Engine", 8)["labels"], ["slice", "Ready"])
                self.assertIsNone(saved_workspace(self.store, THREAD))

    def test_a_failed_github_read_reports_git_failed_and_a_refusal_of_the_app_not_ready(self):
        """TR13: at a new run's start and at a resume's start, in turn, GitHub answers 502 to the Ready label read, a rate-limit
        403 to the variables read, a plain 403 to the variables read, and 502 and 422 to the App's token request. Once more at a
        new run's start, the 502 comes with a status comment write that also fails. No start makes a paid call. The 502 and
        rate-limit starts report git_failed, the plain 403 and the token request's 422 not_ready. Each reads once, saves no
        checkpoint for the new run, keeps the resume's checkpoint and halt, and leaves the labels as they were."""
        label_502 = ("has_label", "gh: Server Error (HTTP 502)")
        variables_rate_limit = ("variables", "gh: API rate limit exceeded for installation ID 1. (HTTP 403)")
        variables_403 = ("variables", "gh: Resource not accessible by integration (HTTP 403)")
        token_502, token_422 = ("token", 502), ("token", 422)
        again = "No setting needs to change. Remove and add the Ready label to try again."
        reports = {
            label_502: ("git_failed", 75, 'The read of Example/Engine\'s Ready label, for the requirement "the Ready label exists", '
                                          f"failed because GitHub answered HTTP 502. {again}"),
            variables_rate_limit: ("git_failed", 75, "The read of Example/Engine's Actions variables, for the requirement "
                                                     '"FOREMAN_HUMAN_LOGIN is set and not blank, and each budget cap that is set parses. '
                                                     f'An unset cap uses its default", failed because GitHub answered HTTP 403 for its rate limit. {again}'),
            variables_403: ("not_ready", 1, "Not ready: GitHub answered HTTP 403 to the read of Example/Engine's Actions variables, "
                                            "refusing the App. Fix: give the Foreman GitHub App's installation access to Example/Engine "
                                            "and every repository permission the App requests. Then remove and add the Ready label."),
            token_502: ("git_failed", 75, "The read of the App's token for Example/Engine, for the requirement \"the GitHub App is installed "
                                          f"on the repository and GitHub lets it make doctor's reads\", failed because GitHub answered HTTP 502. {again}"),
            token_422: ("not_ready", 1, "Not ready: GitHub answered HTTP 422 to the read of the App's token for Example/Engine, refusing "
                                        "the App. Fix: give the Foreman GitHub App's installation access to Example/Engine and every "
                                        "repository permission the App requests. Then remove and add the Ready label."),
        }

        def refused(answer, verb):
            labels = self.forge.load_issue("Example/Engine", 8)["labels"]
            failing, reads = self.token_answers(answer[1]) if answer[0] == "token" else self.github_answers(*answer)
            with failing:
                code, out, err = self.start(None)
            code_name, exit_code, detail = reports[answer]
            self.assertEqual(code, exit_code, out + err)
            self.assertEqual(self.forge.status[("Example/Engine", 8)].split("\n\n<!--")[0],
                             f"Foreman did not {verb} this run ({code_name}).\n\n{detail}")
            self.assertEqual((len(reads), self.forge.load_issue("Example/Engine", 8)["labels"]), (1, labels))

        for answer in reports:
            with self.subTest(start="new", answer=answer):
                refused(answer, "start")
                self.assertEqual((self.worker_calls, self.forge.label_calls), (0, []))
                self.assertIsNone(saved_workspace(self.store, THREAD))
        failing, reads = self.github_answers(*label_502)
        unwritten, _ = self.github_answers("upsert_comment", "gh: Server Error (HTTP 502)")
        with failing, unwritten:
            code, out, err = self.start(None)
        self.assertEqual((code, len(reads), self.worker_calls), (75, 1, 0))
        self.assertIn("status not written: GitHub answered HTTP 502.", err)
        self.assertNotIn("configuration", err)
        self.assertIn(f"Foreman did not start this run (git_failed).\n\n{reports[label_502][2]}", err)

        self.assertEqual(self.start({**SETTINGS, "FOREMAN_MAX_TOKENS": "1"}, [{"triage": "supplied_slice"}])[0], 75)
        halted = self.stored()
        for answer in reports:
            with self.subTest(start="resume", answer=answer):
                self.forge.ready_again(8)
                refused(answer, "resume")
                self.assertEqual((self.worker_calls, self.stored()), (1, halted))
                with SqliteSaver.from_conn_string(str(self.store)) as saver:
                    saver.serde = checkpoint_serializer()
                    self.assertEqual(saver.get_tuple({"configurable": {"thread_id": THREAD}}).checkpoint["channel_values"]["halt"], "budget_exceeded")

    def test_a_github_requirement_stops_in_the_runner_and_a_contract_requirement_in_admit(self):
        """TR11: a new run's start in a repository missing a GitHub requirement, and one missing a contract requirement. The
        first stops in the runner before the graph and the second in triage.admit. Each reports not_ready on the issue,
        keeps Ready and starts no run or paid call."""
        del self.workspace.base_files[CONTRACT_PATH]
        self.forge.repository_labels = set()
        pinned = patch.object(self.workspace, "pin_base", wraps=self.workspace.pin_base)
        with pinned as pin:
            self.assertEqual(self.start()[0], 1)
        # The runner's GitHub check comes first, so the graph never pinned a base or read the contract.
        pin.assert_not_called()
        self.assertIn("Not ready: Example/Engine has no Ready label.", self.forge.status[("Example/Engine", 8)])
        self.forge.repository_labels = {"Ready"}
        with pinned as pin:
            self.assertEqual(self.start()[0], 1)
        pin.assert_called_once()
        self.assertIn(f"Not ready: the contract file {CONTRACT_PATH} is not a tracked file", self.forge.status[("Example/Engine", 8)])
        self.assertEqual((self.worker_calls, self.forge.label_calls), (0, []))
        self.assertEqual(self.forge.load_issue("Example/Engine", 8)["labels"], ["slice", "Ready"])
        self.assertIsNone(saved_workspace(self.store, THREAD))

    def test_a_missing_setting_is_commented_once_while_ready_waits(self):
        for _ in range(2):
            self.assertEqual(self.start({})[0], 1)
        self.assertEqual(len(self.forge.status_writes), 1)

    def test_a_stale_delivery_without_ready_is_not_commented(self):
        self.forge = FakeGitHub(["slice"])
        self.start({})
        self.assertEqual((self.forge.status, self.forge.label_calls, self.worker_calls), ({}, [], 0))

    def test_a_duplicate_ready_gives_one_checkpoint_and_the_issue_shows_doing(self):
        self.forge.queue_merges = False
        code, out, err = self.start(SETTINGS, [{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE])
        self.assertEqual(code, 75, out + err)
        writes = list(self.forge.status_writes)
        self.assertEqual(self.start()[:2], (0, f"already started {THREAD}\n"))
        self.assertEqual(self.forge.status_writes, writes)
        self.assertEqual(self.forge.load_issue("Example/Engine", 8)["labels"], ["slice", "Doing"])
        # One status comment on the issue; the pull request carries Verify's evidence comment.
        self.assertEqual(list(self.forge.status), [("Example/Engine", 8), ("Example/Engine", 12)])

    def test_ready_again_resumes_a_budget_halt_with_the_raised_repository_cap(self):
        replies = [{"triage": "supplied_slice"}, {"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE]
        code, out, err = self.start({**SETTINGS, "FOREMAN_MAX_TOKENS": "1"}, replies[:1])
        self.assertEqual(code, 75, out + err)
        self.assertEqual((self.stored()[0].max_tokens, self.worker_calls), (1, 1))
        self.forge.ready_again(8)
        # An unchanged cap stops again before any worker call. A lowered dollar cap becomes the run's cap.
        self.assertEqual(self.start({**SETTINGS, "FOREMAN_MAX_TOKENS": "1"})[0], 75)
        self.forge.ready_again(8)
        self.assertEqual(self.start({**SETTINGS, "FOREMAN_MAX_TOKENS": "1", "FOREMAN_MAX_BUDGET_USD": "2"})[0], 75)
        self.assertEqual((self.stored()[0].max_budget_usd, self.worker_calls), ("2", 1))
        self.forge.ready_again(8)
        # The halted triage answer was never applied, so the resumed route asks again, and the plan checker answers too.
        code, out, err = self.start({**SETTINGS, "FOREMAN_MAX_TOKENS": "900000"}, replies[1:])
        self.assertEqual(code, 0, out + err)
        self.assertIn("done merged", out)
        # Both triage calls, spec check, Implement, Verify and Review.
        self.assertEqual((self.stored()[0].max_tokens, self.worker_calls), (900000, 6))
        # A finished run is never started again.
        self.assertEqual(self.start({**SETTINGS, "FOREMAN_MAX_TOKENS": "900000"})[:2], (0, f"already started {THREAD}\n"))

    def test_a_resume_refused_by_doctor_keeps_its_halt_and_resumes_once_the_setting_is_fixed(self):
        """TR9: a run halted budget_exceeded, the operator sets FOREMAN_MAX_BUDGET_USD to 4.5o and adds Ready, then corrects
        it and removes and adds Ready. The first start reports not_ready naming the variable and its fix, makes no paid call and keeps
        the checkpoint and its budget_exceeded halt. The second resumes at the interrupted step within the corrected cap."""
        code, out, err = self.start({**SETTINGS, "FOREMAN_MAX_TOKENS": "1"}, [{"triage": "supplied_slice"}])
        self.assertEqual(code, 75, out + err)
        halted = self.stored()
        self.forge.ready_again(8)
        # A resume runs only doctor's GitHub requirements: the contract already passed at the pinned base.
        contract = self.workspace.base_files.pop(CONTRACT_PATH)
        # The operator raises the cap with a typo. Doctor's quick check refuses the resume before any paid call.
        code, out, err = self.start({**SETTINGS, "FOREMAN_MAX_TOKENS": "900000", "FOREMAN_MAX_BUDGET_USD": "4.5o"})
        self.assertEqual(code, 1, out + err)
        status = self.forge.status[("Example/Engine", 8)]
        self.assertIn("Not ready: the FOREMAN_MAX_BUDGET_USD repository variable '4.5o' is not a positive dollar amount. "
                      "Fix: set FOREMAN_MAX_BUDGET_USD to a positive amount", status)
        self.assertTrue(status.startswith("Foreman did not resume this run (not_ready).\n\nNot ready: "), status)
        self.assertIn("in Example/Engine's Actions variables. Then remove and add the Ready label.", status)
        self.assertNotIn(CONTRACT_PATH, status)
        # The refusal is no halt, so the labels stay as the start found them and Ready waits for the fix.
        self.assertEqual(self.forge.load_issue("Example/Engine", 8)["labels"], ["slice", "Ready"])
        self.assertEqual(self.worker_calls, 1)
        # The checkpoint and its budget halt are untouched, so the next Ready resumes the halted step.
        self.assertEqual(self.stored(), halted)
        with SqliteSaver.from_conn_string(str(self.store)) as saver:
            saver.serde = checkpoint_serializer()
            self.assertEqual(saver.get_tuple({"configurable": {"thread_id": THREAD}}).checkpoint["channel_values"]["halt"], "budget_exceeded")
        # The resumed step builds the run's container from the contract at the pinned base.
        self.workspace.base_files[CONTRACT_PATH] = contract
        self.forge.ready_again(8)
        code, out, err = self.start({**SETTINGS, "FOREMAN_MAX_TOKENS": "900000", "FOREMAN_MAX_BUDGET_USD": "4.50"},
                                    [{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE])
        self.assertEqual(code, 0, out + err)
        self.assertIn("done merged", out)
        self.assertEqual(self.stored()[0].max_budget_usd, "4.5")

    def test_a_cap_below_what_the_run_spent_stops_at_the_first_reservation(self):
        code, out, err = self.start({**SETTINGS, "FOREMAN_MAX_TOKENS": "1"}, [{"triage": "supplied_slice"}])
        self.assertEqual(code, 75, out + err)
        with SqliteSaver.from_conn_string(str(self.store)) as saver:
            saver.serde = checkpoint_serializer()
            attempts = saver.get_tuple({"configurable": {"thread_id": THREAD}}).checkpoint["channel_values"]["attempts"]
        spent_tokens, spent_dollars = work_tokens(attempts), claude_spend(attempts)
        assert spent_tokens is not None and spent_dollars is not None
        self.assertGreater(spent_dollars, Decimal("0.001"))
        for variables in ({"FOREMAN_MAX_TOKENS": str(spent_tokens - 1)},
                          {"FOREMAN_MAX_TOKENS": "900000", "FOREMAN_MAX_BUDGET_USD": "0.001"}):
            with self.subTest(variables=variables):
                self.forge.ready_again(8)
                code, out, err = self.start({**SETTINGS, **variables})
                self.assertEqual(code, 75, out + err)
                self.assertIn("done budget_exceeded", out)
                self.assertEqual(self.worker_calls, 1)
                start = self.stored()[0]
                self.assertEqual((str(start.max_tokens), start.max_budget_usd),
                                 (variables["FOREMAN_MAX_TOKENS"], variables.get("FOREMAN_MAX_BUDGET_USD", "3")))

    def test_ready_again_resumes_in_the_saved_checkout_and_a_new_issue_in_its_managed_one(self):
        self.assertEqual(self.start({**SETTINGS, "FOREMAN_MAX_TOKENS": "1"}, [{"triage": "supplied_slice"}])[0], 75)
        # The state directory sits outside the saved checkout, as each worker call's scratch must.
        state_home, seen = Path(self.enterContext(TemporaryDirectory())) / "state", []
        with patch.dict("os.environ", {"XDG_STATE_HOME": str(state_home)}), patch("foreman.cli.LiveForge", return_value=self.forge), \
                patch.object(self.forge, "variables", return_value={**SETTINGS, "FOREMAN_MAX_TOKENS": "900000"}), \
                patch("foreman.cli.run_live_delivery", side_effect=lambda **kwargs: seen.append(kwargs) or {"stop": "merged"}), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            for issue in ("8", "9"):
                self.assertEqual(main(["start", "--repo", "Example/Engine", "--issue", issue], checkpoint=self.store), 0)
        self.assertEqual([(kwargs["issue"], kwargs["run"].workspace.root) for kwargs in seen], [
            (8, self.root.resolve()), (9, (state_home / "foreman").resolve() / "runs" / "example" / "engine" / "9")])

    def test_a_start_saves_in_the_store_the_smoke_watches(self):
        state_home, seen = self.root / "state", []
        with patch.dict("os.environ", {"XDG_STATE_HOME": str(state_home)}), patch("foreman.cli.LiveForge", return_value=self.forge), \
                patch.object(self.forge, "variables", return_value=SETTINGS), \
                patch("foreman.cli.run_live_delivery", side_effect=lambda **kwargs: seen.append(kwargs) or {"stop": "merged"}), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["start", "--repo", "Example/Engine", "--issue", "9"]), 0)
            watched = checkpoint_store()
        self.assertEqual([kwargs["checkpoint"] for kwargs in seen], [watched])
        self.assertEqual(watched, state_home / "foreman" / "checkpoints.sqlite3")

    def test_a_run_on_a_terminal_halt_is_never_started_again(self):
        code, out, err = self.start(SETTINGS, [{"triage": "unknown route"}, {"triage": "unknown route"}])
        self.assertEqual(code, 1, out + err)
        self.assertIn("invalid_output", out + err)
        calls = self.worker_calls
        self.forge.ready_again(8)
        self.assertEqual(self.start({**SETTINGS, "FOREMAN_MAX_TOKENS": "900000"})[:2], (0, f"already started {THREAD}\n"))
        self.assertEqual(self.worker_calls, calls)

    def test_without_app_credentials_nothing_is_read_or_run(self):
        with patch.dict("os.environ", {"FOREMAN_APP_ID": ""}), patch.object(self.forge, "variables") as variables, patch("foreman.cli.run_live_delivery") as run:
            code, _, err = self.start()
        self.assertEqual(code, 1)
        self.assertIn("FOREMAN_APP_ID", err)
        variables.assert_not_called()
        run.assert_not_called()

    def test_start_waits_for_a_held_lock_then_applies_the_checkpoint_rule(self):
        self.start(SETTINGS, [{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE])
        with (self.root / "runtime" / "foreman-locks" / spec_digest([THREAD])).open("a") as other:
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(IssueLocked), issue_lock(THREAD):
                pass
            # The holder may be a blocked admission about to delete its checkpoint, so start waits rather than skips.
            unlock = threading.Timer(0.3, lambda: fcntl.flock(other, fcntl.LOCK_UN))
            unlock.start()
            self.assertEqual(self.start()[:2], (0, f"already started {THREAD}\n"))
            unlock.join()

    def test_start_runs_once_a_blocked_admission_deletes_its_checkpoint_and_unlocks(self):
        with lock_file(THREAD).open("a") as other:
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # The holder leaves no checkpoint, as a blocked admission does, so the waiting start runs the issue.
            unlock = threading.Timer(0.3, lambda: fcntl.flock(other, fcntl.LOCK_UN))
            unlock.start()
            code, out, err = self.start(SETTINGS, [{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE])
            unlock.join()
        self.assertEqual(code, 0, out + err)
        self.assertIn("done merged", out)
        self.assertEqual(saved_workspace(self.store, THREAD), str(self.root.resolve()))

    def test_the_holder_reenters_its_own_lock(self):
        with issue_lock(THREAD), issue_lock(THREAD):
            with (self.root / "runtime" / "foreman-locks" / spec_digest([THREAD])).open("a") as other:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with issue_lock(THREAD):
            pass



READY = {"action": "labeled", "label": {"name": "Ready"}, "issue": {"number": 8}, "repository": {"full_name": "Example/Engine"}}
RAISED = {"FOREMAN_MAX_TOKENS": "990000000", "FOREMAN_MAX_BUDGET_USD": "77"}
# The checkout as the cut-off builder left it, on the owned branch at the attempt's parent, and moved to another commit.
EDITED, MOVED = WorktreeState(HEAD, False, BRANCH), WorktreeState("d" * 40, False, BRANCH)


def fresh(args):
    """Whether a worker call starts a new session rather than resuming one."""
    return not {"resume", "--resume"} & set(args)


class ReleasedIssuesTest(WakeTest):
    """S26: the run prints no start line for its released issues. The receiver starts them from the checkpoint."""

    def test_a_merged_run_prints_no_start_line_for_its_released_issues_including_on_replay(self):
        self.github.create_issue("Example/Engine", "Dependent", "Body", ["slice"], marker="")
        self.github.create_issue("Example/Engine", "Later", "Body", ["slice"], marker="")
        self.github.dependencies = {101: [8], 102: [8]}
        fake = FakeWorkspace(self.cwd)
        replies = iter([{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE])
        for _ in range(2):
            with contextlib.redirect_stdout(io.StringIO()) as stdout:
                code = main(["run", "delivery", "--repo", "Example/Engine", "--issue", "8"],
                            worker_runner=lambda args, cwd, env: fake.reply(args, checker_reply(args) or next(replies)),
                            checkpoint=self.checkpoint, live_run=LiveRun(self.github, fake))
            self.assertEqual(code, 0)
            self.assertIn("done merged\n", stdout.getvalue())
            self.assertFalse([line for line in stdout.getvalue().splitlines() if line.startswith("start ")])
        self.assertEqual(released_issues(self.checkpoint, issue_thread("Example/Engine", 8)), ["Example/Engine#101", "Example/Engine#102"])
        self.assertEqual(len(self.github.merge_calls), 1)


class InterruptedRunTest(WakeTest):
    """TR18: a run whose process died mid-step is interrupted. Ready added again resumes it at that step, a stop ends it
    cancelled, and no other wake resumes it. A run paused at a gate, waiting in merge or halted is not interrupted."""

    def killed_in_repair(self, edits=False):
        """Walk a run whose review finds a P1, so its PR is open, and kill its process during the builder's repair call, after
        the call was sent. With edits, the builder had changed the checkout when the process died."""
        reply, repair = FakeWorkspace.reply, {"implementation": "built"}

        def killed(workspace, args, output):
            if output is repair:
                self.cut = args
                if edits:
                    workspace.current = EDITED
                raise KeyboardInterrupt("Foreman's process was killed.")
            return reply(workspace, args, output)
        with patch.object(FakeWorkspace, "reply", killed), self.assertRaises(KeyboardInterrupt):
            self.walk([{"triage": "supplied_slice"}, {"implementation": "built"}, {**APPROVE, "findings": [FINDING]}, repair])
        self.assertEqual(self.github.opened[BRANCH].state, "OPEN")

    def start(self, replies=(), skills=None):
        return self.command(["start", "--repo", "Example/Engine", "--issue", "8"], replies, **({"skills": skills} if skills else {}))

    def checkout(self, state):
        """Each later command finds the checkout as the builder or a person left it."""
        init = FakeWorkspace.__init__

        def found(workspace, *args, **kwargs):
            init(workspace, *args, **kwargs)
            workspace.current = state
        return patch.object(FakeWorkspace, "__init__", found)

    def builds(self):
        """The argv of each builder call from now on, the checkout it was made on and the status comment shown during it."""
        calls, reply = [], FakeWorkspace.reply

        def recorded(workspace, args, output):
            if "implementation" in output:
                calls.append((args, workspace.current, self.github.status_writes[-1][2]))
            return reply(workspace, args, output)
        self.enterContext(patch.object(FakeWorkspace, "reply", recorded))
        return calls

    def saved(self):
        """The saved start, read as every reader outside the graph reads it, and the attempt journal's rows."""
        with SqliteSaver.from_conn_string(str(self.checkpoint)) as saver:
            saver.serde = checkpoint_serializer()
            start = shape(saver).get_state({"configurable": {"thread_id": THREAD}}).values["start"]
            return start, saver.conn.execute("SELECT id, evidence FROM worker_attempt ORDER BY id").fetchall()

    @contextlib.contextmanager
    def unread(self):
        """Fail any read of GitHub or call of doctor, for a delivery that must read nothing."""
        with contextlib.ExitStack() as reads:
            for name in ("load_issue", "issue_comment_records", "pull_request_for", "readiness", "variables"):
                reads.enter_context(patch.object(self.github, name, side_effect=AssertionError(f"{name} was read.")))
            reads.enter_context(patch("foreman.runner.repository", side_effect=AssertionError("doctor ran.")))
            yield

    def test_ready_resumes_an_interrupted_run_at_its_step_and_charges_the_call_once(self):
        """Run one: the start takes the lock, finds the run interrupted, runs doctor's GitHub requirements once, reads the login
        and caps again and resumes the repair's builder call. Dispatch reconciles the call before any repeat. It had been sent
        and left no result, so it is charged the allowance saved on its reservation, and with the caps unchanged the run stops
        budget_exceeded with no worker call. Its report shows the call as interrupted with that charge. After the operator
        raises the caps and adds Ready again, the builder call is made, in a fresh session."""
        self.killed_in_repair()
        self.assertFalse(fresh(self.cut))
        self.github.settings["FOREMAN_HUMAN_LOGIN"] = "someone-else"
        self.github.ready_again(8)
        self.assertEqual(self.deliver("issues", READY), "start Example/Engine#8")
        builds = self.builds()
        with patch("foreman.runner.repository", wraps=repository) as doctor:
            _, _, resumed = self.start()
        self.assertEqual((doctor.call_count, builds), (1, []))
        self.assertEqual(resumed["start"], StartSettings(human_login="someone-else", max_tokens=DEFAULT_TOKEN_CAP, max_budget_usd="3"))
        self.github.settings["FOREMAN_HUMAN_LOGIN"] = "fixture-owner"
        cut = resumed["attempts"][-1]
        repaired = [(attempt["stage"], attempt["outcome"]) for attempt in resumed["attempts"]][-2:]
        self.assertEqual(repaired, [("review", "ran"), ("implement", "interrupted")])
        self.assertEqual((work_tokens(resumed["attempts"]), "dollar_charge" in cut, resumed["stop"], resumed["path"].count("prepare")),
                         (DEFAULT_TOKEN_CAP, False, "budget_exceeded", 2))
        self.assertIn("Ready makes the call again once FOREMAN_MAX_TOKENS is raised.", resumed["halt_detail"])
        self.assertIn(f"| interrupted | {cut['work_charge']} | unknown |", run_report(resumed, resumed["start"]))
        self.github.settings.update(RAISED)
        self.github.ready_again(8)
        _, _, merged = self.start([{"implementation": "built"}, APPROVE])
        remade = [(attempt["stage"], attempt["outcome"]) for attempt in merged["attempts"]][len(resumed["attempts"]):][:1]
        self.assertEqual((merged["stop"], merged["start"].max_tokens, remade), ("merged", 990000000, [("implement", "ran")]))
        self.assertTrue(fresh(builds[0][0]))

    def test_one_ready_after_the_caps_were_raised_makes_the_cut_off_call_again_on_its_edits(self):
        """Run six: the builder had changed the checkout when its process died. The operator raises both caps first and the human
        adds Ready once. The start charges the cut-off call once, with the allowance saved on its reservation, and makes the
        builder call on the tree the call left, in a fresh session, within what the raised caps leave. During that call the
        status comment shows the raised caps."""
        self.killed_in_repair(edits=True)
        before = saved_run(self.checkpoint, THREAD).result["attempts"]
        self.github.settings.update(RAISED)
        self.github.ready_again(8)
        builds = self.builds()
        with self.checkout(EDITED):
            _, _, merged = self.start([{"implementation": "built"}, APPROVE])
        self.assertEqual(merged["stop"], "merged", merged.get("halt_detail"))
        made = [(attempt["stage"], attempt["outcome"]) for attempt in merged["attempts"][len(before):]]
        self.assertEqual(made[:2], [("implement", "interrupted"), ("implement", "ran")])
        self.assertEqual(merged["attempts"][len(before)]["work_charge"], DEFAULT_TOKEN_CAP - work_tokens(before))
        (args, tree, status), = builds
        self.assertEqual((fresh(args), tree), (True, EDITED))
        self.assertIn(f"Tokens: {DEFAULT_TOKEN_CAP} of 990000000", status)
        self.assertIn("of $77", status)

    def test_a_builder_cut_off_before_it_edited_is_made_again_in_a_fresh_session(self):
        """The cut-off call left the checkout as it was, so the builder's saved conversation still matches it. The call made
        again starts fresh all the same, because dispatch dropped that conversation when it charged the cut-off call."""
        self.killed_in_repair()
        self.github.settings.update(RAISED)
        self.github.ready_again(8)
        builds = self.builds()
        _, _, merged = self.start([{"implementation": "built"}, APPROVE])
        self.assertEqual(merged["stop"], "merged", merged.get("halt_detail"))
        self.assertEqual([fresh(args) for args, _, _ in builds], [True])

    def test_a_cut_off_call_whose_checkout_moved_stops_dirty_worktree_until_it_is_put_back(self):
        """Run six with the checkout moved to another commit: the start charges the cut-off call and stops dirty_worktree with
        no worker call. The status comment names the checkout and what differs and says to put it back, keeping its files.
        Ready after the checkout is put back makes the same test again, and the builder call is made on those files."""
        self.killed_in_repair(edits=True)
        self.github.settings.update(RAISED)
        self.github.ready_again(8)
        builds = self.builds()
        with self.checkout(MOVED):
            _, _, stopped = self.start()
        self.assertEqual((stopped["halt"], stopped["steps"][-1], builds), ("dirty_worktree", "implement.edit", []))
        status = self.github.status_writes[-1][2]
        for text in (f"The checkout is on {BRANCH} at {'d' * 40}, not on the owned branch {BRANCH} at the attempt's parent {HEAD}",
                     f"Put the checkout back on {BRANCH} at {HEAD}, keeping its files, then add Ready"):
            self.assertIn(text, status)
        self.assertIn(f"The checkout {self.cwd} is preserved.", stopped["halt_detail"])
        self.github.ready_again(8)
        with self.checkout(EDITED):
            _, _, merged = self.start([{"implementation": "built"}, APPROVE])
        self.assertEqual(merged["stop"], "merged", merged.get("halt_detail"))
        self.assertEqual([(fresh(args), tree) for args, tree, _ in builds], [(True, EDITED)])
        self.assertEqual([attempt["outcome"] for attempt in merged["attempts"]].count("interrupted"), 1)

    def test_a_halted_cut_off_call_whose_checkout_moved_stops_at_reserve(self):
        """Ready under unchanged caps charges the cut-off call and stops budget_exceeded. The checkout is then moved and the caps
        raised, so Ready enters implement at prepare again. Prepare passes the cut-off call's checkout on, and dispatch's reserve,
        which owns the test, stops dirty_worktree with no worker call."""
        self.killed_in_repair(edits=True)
        self.github.ready_again(8)
        with self.checkout(EDITED):
            _, _, halted = self.start()
        self.assertEqual(halted["stop"], "budget_exceeded")
        self.github.settings.update(RAISED)
        self.github.ready_again(8)
        builds = self.builds()
        with self.checkout(MOVED):
            _, _, stopped = self.start()
        self.assertEqual((stopped["halt"], stopped["steps"][-2:], builds),
                         ("dirty_worktree", ["implement.prepare", "implement.edit"], []))
        self.assertIn(f"not on the owned branch {BRANCH} at the attempt's parent {HEAD}. Put the checkout back on {BRANCH} at "
                      f"{HEAD}, keeping its files, then add Ready", stopped["halt_detail"])

    def test_a_cut_off_review_call_keeps_the_builder_session_for_the_repair(self):
        """The reviewer's call was cut off, not the builder's. Ready under unchanged caps charges it and stops budget_exceeded,
        which leaves the builder's conversation alone, since the checkout it saw is unchanged. Under raised caps the review is
        made again, and the repair's builder call resumes that conversation (D5)."""
        reply, review = FakeWorkspace.reply, {**APPROVE, "findings": [FINDING]}

        def killed(workspace, args, output):
            if output is review:
                raise KeyboardInterrupt("Foreman's process was killed.")
            return reply(workspace, args, output)
        with patch.object(FakeWorkspace, "reply", killed), self.assertRaises(KeyboardInterrupt):
            self.walk([{"triage": "supplied_slice"}, {"implementation": "built"}, review])
        self.github.ready_again(8)
        _, _, halted = self.start()
        self.assertEqual((halted["stop"], halted["attempts"][-1]["stage"], halted["attempts"][-1]["outcome"]),
                         ("budget_exceeded", "review", "interrupted"))
        self.assertIsNotNone(halted["builder_session"])
        self.github.settings.update(RAISED)
        self.github.ready_again(8)
        builds = self.builds()
        _, _, merged = self.start([{**APPROVE, "findings": [FINDING]}, {"implementation": "built"}, APPROVE])
        self.assertEqual(merged["stop"], "merged", merged.get("halt_detail"))
        self.assertEqual([fresh(args) for args, _, _ in builds], [False])

    def test_a_cut_off_repair_turn_is_made_again_as_the_original_call(self):
        """The builder's repair answer was invalid, so it got its repair turn, and the process died during that turn. Under raised
        caps, Ready makes the call again from its original instructions in a fresh session, and that call has its own repair
        turn."""
        reply, invalid, turn = FakeWorkspace.reply, {"implementation": 7}, {"implementation": "built"}

        def killed(workspace, args, output):
            if output is turn:
                self.cut = args
                raise KeyboardInterrupt("Foreman's process was killed.")
            return reply(workspace, args, output)
        with patch.object(FakeWorkspace, "reply", killed), self.assertRaises(KeyboardInterrupt):
            self.walk([{"triage": "supplied_slice"}, {"implementation": "built"}, {**APPROVE, "findings": [FINDING]}, invalid, turn])
        self.assertEqual((fresh(self.cut), "Delivery state:" in self.cut[-1]), (False, False))
        self.github.settings.update(RAISED)
        self.github.ready_again(8)
        builds = self.builds()
        _, _, merged = self.start([{"implementation": 7}, {"implementation": "built"}, APPROVE])
        self.assertEqual(merged["stop"], "merged", merged.get("halt_detail"))
        (original, _, _), (own_turn, _, _) = builds
        self.assertEqual((fresh(original), "Delivery state:" in original[-1]), (True, True))
        self.assertEqual((fresh(own_turn), "Delivery state:" in own_turn[-1]), (False, False))

    def test_a_second_crash_and_raise_resume_under_the_latest_caps_and_charge_each_cut_off_call_once(self):
        """The call made again is cut off too. The operator raises the caps again and adds Ready: the run resumes under the
        latest caps, and each cut-off call is charged once, with the allowance saved on its own reservation."""
        self.killed_in_repair()
        before = saved_run(self.checkpoint, THREAD).result["attempts"]
        self.github.settings.update({"FOREMAN_MAX_TOKENS": "500000000", "FOREMAN_MAX_BUDGET_USD": "50"})
        self.github.ready_again(8)
        reply, again = FakeWorkspace.reply, {"implementation": "built"}

        def killed(workspace, args, output):
            if output is again:
                raise KeyboardInterrupt("Foreman's process was killed.")
            return reply(workspace, args, output)
        with patch.object(FakeWorkspace, "reply", killed), self.assertRaises(KeyboardInterrupt):
            self.start([again])
        self.assertEqual(self.saved()[0].max_tokens, 500000000)
        self.github.settings.update(RAISED)
        self.github.ready_again(8)
        _, _, merged = self.start([{"implementation": "built"}, APPROVE])
        self.assertEqual((merged["stop"], merged["start"].max_tokens), ("merged", 990000000), merged.get("halt_detail"))
        first, second = [attempt for attempt in merged["attempts"] if attempt["outcome"] == "interrupted"]
        self.assertEqual((first["work_charge"], second["work_charge"]),
                         (DEFAULT_TOKEN_CAP - work_tokens(before), 500000000 - DEFAULT_TOKEN_CAP))
        self.assertEqual(len({attempt["attempt_id"] for attempt in merged["attempts"]}), len(merged["attempts"]))

    def test_a_refused_start_replaces_nothing_and_charges_nothing(self):
        """Each refusal the runner can give at a start, with the caps raised, leaves the saved start and the attempt journal as
        they were, and the run stays interrupted. The next start that passes resumes under the raised caps."""
        self.killed_in_repair()
        before = self.saved()
        self.github.settings.update(RAISED)
        refusals = {
            "not_ready": (patch.dict(self.github.settings, {"FOREMAN_HUMAN_LOGIN": ""}), None),
            "git_failed": (patch.object(self.github, "variables", side_effect=ForgeError("GitHub is down.", 502)), None),
            "origin": (patch.object(FakeWorkspace, "validate_origin", side_effect=WorkspaceError("origin moved")), None),
            "skill_missing": (contextlib.nullcontext(), MagicMock(side_effect=SkillMissing(self.pin))),
        }
        for name, (refusal, skills) in refusals.items():
            with self.subTest(refusal=name):
                self.github.ready_again(8)
                with refusal:
                    self.start(skills=skills)
                self.assertEqual(self.saved(), before)
                self.assertTrue(ready_resumes(self.checkpoint, THREAD))
        self.github.ready_again(8)
        _, _, merged = self.start([{"implementation": "built"}, APPROVE])
        self.assertEqual((merged["stop"], merged["start"].max_tokens), ("merged", 990000000), merged.get("halt_detail"))

    def test_after_an_interrupted_resume_under_raised_caps_a_gate_answer_is_admitted_under_them(self):
        """The call made again asks a question. The answer, from the login the start read, wakes the run, and its call is
        admitted under the raised caps, which the cut-off call's charge left no room in before. So the start replaced the saved
        start and not only this process's view of it."""
        self.killed_in_repair()
        self.github.settings.update({**RAISED, "FOREMAN_HUMAN_LOGIN": "someone-else"})
        self.github.ready_again(8)
        _, _, asked = self.start([{"implementation": "built", "human_question": "Keep it?"}])
        self.assertEqual(asked["stop"], "needs_human", asked.get("halt_detail"))
        # The answer counts from the login this start read, which accept reads from the saved start.
        self.github.answer(asked["halt_detail"], "Yes, keep it.", login="someone-else")
        delivery = comment(self.github.rulings[-1].body, login="someone-else")
        self.assertEqual(self.deliver("issue_comment", delivery), "wake Example/Engine#8")
        _, _, merged = self.command(["wake", "--repo", "Example/Engine", "--issue", "8"], [{"implementation": "built"}, APPROVE])
        self.assertEqual((merged["stop"], merged["start"].max_tokens), ("merged", 990000000), merged.get("halt_detail"))

    def test_a_reservation_never_sent_goes_out_with_its_saved_allowance_under_a_lowered_cap(self):
        """Known limit, by ruling: a call whose reservation was saved and never sent is made under that reservation even when the
        start read a lower cap. The lower cap stops the run after it."""
        send, killed = AttemptJournal.send, []

        def kill(journal, attempt_id):
            if len(self.github.reviews) == 1 and not killed:
                killed.append(attempt_id)
                raise KeyboardInterrupt("Foreman's process was killed.")
            return send(journal, attempt_id)
        with patch.object(AttemptJournal, "send", kill), self.assertRaises(KeyboardInterrupt):
            self.walk([{"triage": "supplied_slice"}, {"implementation": "built"}, {**APPROVE, "findings": [FINDING]}])
        self.github.settings["FOREMAN_MAX_TOKENS"] = "1"
        self.github.ready_again(8)
        builds = self.builds()
        _, _, stopped = self.start([{"implementation": "built"}])
        made = [attempt["outcome"] for attempt in stopped["attempts"] if attempt["attempt_id"] == killed[0]]
        self.assertEqual((len(builds), made, stopped["stop"], stopped["start"].max_tokens), (1, ["ran"], "budget_exceeded", 1))

    def test_ready_resumes_a_run_killed_before_its_reserved_call_was_sent_and_makes_the_call_once(self):
        """A run killed after dispatch saved the repair's reservation and before the call was sent spent nothing on it. Ready
        resumes it at that call, which is made and charged once, and the run merges."""
        send, killed = AttemptJournal.send, []

        def kill(journal, attempt_id):
            if len(self.github.reviews) == 1 and not killed:
                killed.append(attempt_id)
                raise KeyboardInterrupt("Foreman's process was killed.")
            return send(journal, attempt_id)
        with patch.object(AttemptJournal, "send", kill), self.assertRaises(KeyboardInterrupt):
            self.walk([{"triage": "supplied_slice"}, {"implementation": "built"}, {**APPROVE, "findings": [FINDING]}])
        self.assertTrue(ready_resumes(self.checkpoint, THREAD))
        self.github.ready_again(8)
        _, _, merged = self.start([{"implementation": "built"}, APPROVE])
        made = [(attempt["stage"], attempt["outcome"]) for attempt in merged["attempts"] if attempt["attempt_id"] == killed[0]]
        self.assertEqual((merged["stop"], made), ("merged", [("implement", "ran")]))

    def test_a_never_sent_call_whose_checkout_changed_stops_dirty_worktree_until_it_is_restored(self):
        """No worker of this run wrote after the reservation was saved, so edits found then are not the run's. Ready stops
        dirty_worktree with no worker call and says to restore the checkout to the tree saved on the reservation. Ready after
        it is restored makes the call."""
        send, killed = AttemptJournal.send, []

        def kill(journal, attempt_id):
            if len(self.github.reviews) == 1 and not killed:
                killed.append(attempt_id)
                raise KeyboardInterrupt("Foreman's process was killed.")
            return send(journal, attempt_id)
        with patch.object(AttemptJournal, "send", kill), self.assertRaises(KeyboardInterrupt):
            self.walk([{"triage": "supplied_slice"}, {"implementation": "built"}, {**APPROVE, "findings": [FINDING]}])
        self.github.ready_again(8)
        builds = self.builds()
        with self.checkout(EDITED), patch.object(FakeWorkspace, "fingerprint", return_value="edits made after the reservation"):
            _, _, stopped = self.start()
        self.assertEqual((stopped["halt"], builds), ("dirty_worktree", []))
        self.assertIn("The checkout differs from the tree saved on the reservation of a call never sent. Restore it to that tree, "
                      "then add Ready.", self.github.status_writes[-1][2])
        self.assertIn(f"The checkout {self.cwd} is preserved.", stopped["halt_detail"])
        self.github.ready_again(8)
        _, _, merged = self.start([{"implementation": "built"}, APPROVE])
        self.assertEqual((merged["stop"], len(builds)), ("merged", 1), merged.get("halt_detail"))

    def test_a_run_killed_after_its_gate_answer_resumes_on_ready_and_no_other_wake_takes_the_answer_again(self):
        """The run took the answer to its repair's gate and was killed in the builder call that followed, so the question is
        closed and the run is interrupted. The answer posted again wakes nothing: no worker is called and the run stays
        interrupted. Ready resumes it at its step with the answer it took."""
        built, after = {"implementation": "built"}, {"implementation": "built after the answer"}
        asked = self.walk([{"triage": "supplied_slice"}, built, {**APPROVE, "findings": [FINDING]}, {**built, "human_question": "Keep it?"}])
        self.assertEqual(asked["stop"], "needs_human")
        reply = FakeWorkspace.reply

        def killed(workspace, args, output):
            if output is after:
                raise KeyboardInterrupt("Foreman's process was killed.")
            return reply(workspace, args, output)
        with patch.object(FakeWorkspace, "reply", killed), self.assertRaises(KeyboardInterrupt):
            self.walk([after], answer="Yes, keep it.")
        self.assertTrue(ready_resumes(self.checkpoint, THREAD))
        writes = list(self.github.status_writes)
        self.github.answer(self.question, "Yes, keep it.")
        self.deliver("issue_comment", comment("Yes, keep it."))
        _, _, woken = self.wake()
        self.assertEqual((woken["stop"] != "needs_human", self.github.status_writes), (True, writes))
        self.assertTrue(ready_resumes(self.checkpoint, THREAD))
        self.github.ready_again(8)
        _, _, resumed = self.start()
        self.assertEqual((resumed["stop"], resumed["attempts"][-1]["stage"], resumed["attempts"][-1]["outcome"]),
                         ("budget_exceeded", "implement", "interrupted"))
        self.assertEqual([qa["answer"] for qa in resumed["plan"].human_qa], ["Yes, keep it."])

    def test_ready_resumes_a_run_interrupted_between_calls_and_its_later_calls_keep_their_methods(self):
        """A run killed while verify posts its evidence on the repair's PR resumes at that post with its skills: the review call
        that follows gets its methods, and the run merges."""
        post = self.github.upsert_comment

        def killed(repo, number, *args, **kwargs):
            if number == 12 and len(self.github.reviews) == 1:
                raise KeyboardInterrupt("Foreman's process was killed.")
            return post(repo, number, *args, **kwargs)
        with patch.object(self.github, "upsert_comment", killed), self.assertRaises(KeyboardInterrupt):
            self.walk([{"triage": "supplied_slice"}, {"implementation": "built"}, {**APPROVE, "findings": [FINDING]}, {"implementation": "built"}])
        self.assertTrue(ready_resumes(self.checkpoint, THREAD))
        self.github.ready_again(8)
        reply, prompts = FakeWorkspace.reply, []

        def heard(workspace, args, output):
            prompts.append(args[-1])
            return reply(workspace, args, output)
        with patch.object(FakeWorkspace, "reply", heard):
            _, _, merged = self.start([APPROVE])
        self.assertEqual((merged["stop"], [attempt["stage"] for attempt in merged["attempts"]][-2:]), ("merged", ["verify", "review"]))
        self.assertEqual(len(prompts), 1)
        self.assertIn("Apply the methods listed below.", prompts[0])

    def test_a_stop_ends_an_interrupted_run_cancelled_with_its_pr_closed(self):
        """Run two: the stop's wake ends the run cancelled, closes its PR and calls no worker. Its report shows the cut-off call
        as interrupted with unknown tokens and dollars, its spend lines count the charged attempts only, and the stop leaves the
        attempts unchanged."""
        self.killed_in_repair()
        before = saved_run(self.checkpoint, THREAD).result["attempts"]
        calls = len(self.calls)
        self.github.ruling("foreman stop")
        self.assertEqual(self.deliver("issue_comment", comment("foreman stop")), "wake Example/Engine#8")
        _, _, stopped = self.wake()
        self.assertEqual((stopped["stop"], len(self.calls), self.github.pr_closes),
                         ("cancelled", calls, [("Example/Engine", 12, CANCELLATION)]))
        self.assertFalse(ready_resumes(self.checkpoint, THREAD))
        self.assertEqual(stopped["attempts"], before)
        status = self.github.status_writes[-1][2]
        self.assertEqual(status.count("| interrupted | unknown | unknown |"), 1)
        self.assertIn(f"| implement | edit | {stopped['cut_off']['model']} |", status)
        self.assertIn(f"Tokens: {work_tokens(before)} of {DEFAULT_TOKEN_CAP}", status)
        self.assertIn(f"Dollars: ${claude_spend(before)} of $3", status)

    def test_a_stop_on_a_run_whose_reservation_was_never_sent_shows_no_cut_off_call(self):
        """No call was made, so the stopped run's report has no interrupted row."""
        send = AttemptJournal.send

        def kill(journal, attempt_id):
            if len(self.github.reviews) == 1:
                raise KeyboardInterrupt("Foreman's process was killed.")
            return send(journal, attempt_id)
        with patch.object(AttemptJournal, "send", kill), self.assertRaises(KeyboardInterrupt):
            self.walk([{"triage": "supplied_slice"}, {"implementation": "built"}, {**APPROVE, "findings": [FINDING]}])
        self.github.ruling("foreman stop")
        self.assertEqual(self.deliver("issue_comment", comment("foreman stop")), "wake Example/Engine#8")
        _, _, stopped = self.wake()
        self.assertEqual((stopped["stop"], "cut_off" in stopped), ("cancelled", False))
        self.assertNotIn("| interrupted |", self.github.status_writes[-1][2])

    def test_ready_on_a_gate_paused_run_starts_nothing_and_reads_nothing(self):
        """Run three: the run paused at a repair's gate, whose top-level stop is review's route, is not interrupted."""
        built = {"implementation": "built"}
        asked = self.walk([{"triage": "supplied_slice"}, built, {**APPROVE, "findings": [FINDING]}, {**built, "human_question": "Keep it?"}])
        self.assertEqual(asked["stop"], "needs_human")
        writes, calls = list(self.github.status_writes), len(self.calls)
        self.github.ready_again(8)
        with self.unread():
            self.assertEqual(self.start()[:2], (0, f"already started {THREAD}\n"))
        self.assertEqual((self.github.status_writes, len(self.calls)), (writes, calls))

    def test_ready_on_a_run_paused_at_its_second_question_in_one_step_starts_nothing(self):
        """The repair's builder asks again after the human's answer, so the step is paused at its gate on the new question."""
        built = {"implementation": "built"}
        self.walk([{"triage": "supplied_slice"}, built, {**APPROVE, "findings": [FINDING]}, {**built, "human_question": "Keep it?"}])
        self.assertIn("Again?", self.walk([{**built, "human_question": "Again?"}], answer="Yes, keep it.")["halt_detail"])
        self.assertFalse(ready_resumes(self.checkpoint, THREAD))
        self.github.ready_again(8)
        with self.unread():
            self.assertEqual(self.start()[:2], (0, f"already started {THREAD}\n"))

    def test_ready_on_a_run_waiting_in_arm_starts_nothing_and_its_check_still_arms_it(self):
        """Run four: Ready on the run waiting in arm for a required check starts nothing and reads nothing. The check's delivery
        then wakes it, and it arms."""
        observe = self.github.readiness
        with patch.object(self.github, "readiness", side_effect=lambda *args: observe(*args).model_copy(update={"outcome": "pending"})):
            self.assertEqual(self.walk([{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE])["stop"], "merge_pending")
        self.assertEqual(self.github.merge_calls, [])
        writes = list(self.github.status_writes)
        self.github.ready_again(8)
        with self.unread():
            self.assertEqual(self.start()[:2], (0, f"already started {THREAD}\n"))
        self.assertEqual(self.github.status_writes, writes)
        self.assertEqual(self.deliver("check_suite", check_delivery()), "wake Example/Engine#8")
        _, _, armed = self.wake()
        self.assertEqual((armed["stop"], len(self.github.merge_calls)), ("merged", 1))

    def test_a_check_delivery_resumes_nothing_on_a_run_interrupted_in_arm(self):
        """Run five: the process is killed while arm reads GitHub. A finished check wakes the run, which is in merge: the wake
        reads the pull request and then the issue, finds no stop, and resumes and writes nothing. The run stays interrupted, and
        Ready then resumes arm, which arms the PR."""
        with patch.object(self.github, "readiness", side_effect=KeyboardInterrupt("Foreman's process was killed.")), \
                self.assertRaises(KeyboardInterrupt):
            self.walk([{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE])
        writes, calls = list(self.github.status_writes), len(self.calls)
        self.assertEqual(self.deliver("check_suite", check_delivery()), "wake Example/Engine#8")
        log = self.github_log()
        self.wake()
        self.assertEqual(log, ["read PR", "read issue"])
        self.assertEqual((self.github.status_writes, len(self.calls), self.github.merge_calls), (writes, calls, []))
        self.assertTrue(ready_resumes(self.checkpoint, THREAD))
        self.github.ready_again(8)
        _, _, merged = self.start()
        self.assertEqual((merged["stop"], len(self.github.merge_calls), len(self.calls)), ("merged", 1, calls))

    def test_ready_on_a_run_on_a_terminal_halt_starts_nothing_and_reads_nothing(self):
        """A halt outside the resumable halts is not an interrupted run, though the run has not ended."""
        self.assertEqual(self.capped()["stop"], "verify_cap")
        writes, calls = list(self.github.status_writes), len(self.calls)
        self.github.ready_again(8)
        with self.unread():
            self.assertEqual(self.start()[:2], (0, f"already started {THREAD}\n"))
        self.assertEqual((self.github.status_writes, len(self.calls)), (writes, calls))

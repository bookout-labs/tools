import contextlib
from decimal import Decimal
import fcntl
import io
import threading
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from langgraph.checkpoint.sqlite import SqliteSaver

from foreman.cli import main
from foreman.contract import CONTRACT_PATH
from foreman.delivery import LiveRun
from foreman.doctor import DEFAULT_BUDGET_USD, DEFAULT_TOKEN_CAP, OperatorVariables, operator_variables
from foreman.github import ForgeError, VariablesUnreadable
from foreman.runner import IssueLocked, issue_lock, saved_workspace
from foreman.stage_output import spec_digest
from foreman.state import checkpoint_serializer
from foreman.usd import BudgetUsd
from foreman.workspace import lock_file
from foreman.budget import claude_spend, work_tokens
from skill_fixture import fixture_skills
from test_delivery import VERIFY_SKILL_PATH
from test_runner import APPROVE, FakeGitHub, FakeWorkspace, checker_reply, confine_host

THREAD = "example/engine#8"
SETTINGS = {"FOREMAN_HUMAN_LOGIN": "fixture-owner"}


class StartTest(unittest.TestCase):
    def setUp(self):
        confine_host(self)
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(patch.dict("os.environ", {"XDG_RUNTIME_DIR": str(self.root / "runtime")}))
        self.app = self.enterContext(patch("foreman.cli.credentials.environment", return_value={"GH_TOKEN": "synthetic"}))
        self.forge = FakeGitHub()
        self.workspace = FakeWorkspace(self.root)
        self.worker_calls = 0

    def start(self, variables=SETTINGS, replies=()):
        outputs = iter(replies)

        def worker(args, cwd, env):
            self.worker_calls += 1
            return self.workspace.reply(args, checker_reply(args) or next(outputs))

        with patch.object(self.forge, "variables", side_effect=lambda repo: dict(variables)), \
                contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            code = main(["start", "--repo", "Example/Engine", "--issue", "8"], checkpoint=self.root / "runs.sqlite3",
                        live_run=LiveRun(self.forge, self.workspace, skills=fixture_skills, human_login="fixture-owner"), worker_runner=worker)
        return code, out.getvalue(), err.getvalue()

    def stored(self):
        with SqliteSaver.from_conn_string(str(self.root / "runs.sqlite3")) as saver:
            saver.serde = checkpoint_serializer()
            values = saver.get_tuple({"configurable": {"thread_id": THREAD}}).checkpoint["channel_values"]
        return values["run"], values["checks"]

    def test_starts_once_with_the_repository_settings_pinned(self):
        # The check comes from the contract file at the pinned base. A leftover FOREMAN_CHECK variable is never read.
        variables = {**SETTINGS, "FOREMAN_CHECK": "exit 9", "FOREMAN_PREFLIGHT": "exit 0", "FOREMAN_MAX_TOKENS": "900000"}
        code, out, err = self.start(variables, [{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE])
        self.assertNotEqual(code, 1, out + err)
        run, checks = self.stored()
        self.assertEqual((checks.check, checks.verify_skill, checks.preflight), ("exit 0", VERIFY_SKILL_PATH, "exit 0"))
        self.assertEqual((run.token_cap, run.max_budget_usd), (900000, "3"))
        calls = self.worker_calls
        # A repeated delivery for a finished run is a no-op.
        self.assertEqual(self.start(variables)[:2], (0, f"already started {THREAD}\n"))
        self.assertEqual(self.worker_calls, calls)

    def test_a_started_run_resumes_and_run_takes_no_check_option(self):
        self.start(SETTINGS, [{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE])
        store = self.root / "runs.sqlite3"
        self.assertEqual(saved_workspace(store, THREAD), str(self.root.resolve()))
        self.assertIsNone(saved_workspace(store, "example/engine#9"))
        live = LiveRun(self.forge, self.workspace, skills=fixture_skills, human_login="fixture-owner")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertNotEqual(main(["run", "delivery", "--repo", "Example/Engine", "--issue", "8"], checkpoint=store, live_run=live), 2)
            with self.assertRaises(SystemExit) as refused:
                main(["run", "delivery", "--repo", "Example/Engine", "--issue", "9", "--check", "exit 0"], checkpoint=store, live_run=live)
        self.assertEqual(refused.exception.code, 2)

    def test_settings_default_and_parse(self):
        self.assertEqual(operator_variables("Example/Engine", {**SETTINGS, "FOREMAN_MAX_BUDGET_USD": "4.50"}),
                         OperatorVariables("fixture-owner", DEFAULT_TOKEN_CAP, BudgetUsd(Decimal("4.50")), ""))
        self.assertEqual(operator_variables("Example/Engine", SETTINGS), OperatorVariables("fixture-owner", DEFAULT_TOKEN_CAP, DEFAULT_BUDGET_USD, ""))
        # The drawing's defaults for an unset cap: 3 dollars and 20,000,000 work tokens.
        self.assertEqual((DEFAULT_BUDGET_USD, DEFAULT_TOKEN_CAP), (BudgetUsd(Decimal("3")), 20_000_000))
        self.assertEqual(self.start(SETTINGS, [{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE])[0], 0)
        self.assertEqual((self.stored()[0].token_cap, self.stored()[0].max_budget_usd), (DEFAULT_TOKEN_CAP, "3"))

    def test_run_settings_read_only_the_operator_variables(self):
        reads = []

        class Recorded(dict):
            def get(self, key, default=None):
                reads.append(key)
                return super().get(key, default)

        operator_variables("Example/Engine", Recorded({**SETTINGS, "FOREMAN_CHECK": "exit 0", "FOREMAN_MERGE_HOLD": "x"}))
        # FOREMAN_PREFLIGHT stays until the contract's setup commands replace it. FOREMAN_MERGE_HOLD is read live at arm.
        self.assertEqual(set(reads), {"FOREMAN_HUMAN_LOGIN", "FOREMAN_MAX_TOKENS", "FOREMAN_MAX_BUDGET_USD", "FOREMAN_PREFLIGHT"})

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
                self.assertIn(f"Foreman halted with not_ready. The target repository is not ready for Foreman.\n\nNot ready: {missing}. Fix: ", status)
                self.assertIn("in Example/Engine's Actions variables. Then remove and add the Ready label.", status)
                self.assertEqual((self.worker_calls, self.forge.label_calls), (0, []))
                self.assertEqual(self.forge.load_issue("Example/Engine", 8)["labels"], ["slice", "Ready"])
                self.assertIsNone(saved_workspace(self.root / "runs.sqlite3", THREAD))
        with patch.object(self.forge, "variables", side_effect=VariablesUnreadable("HTTP 403")), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            code = main(["start", "--repo", "Example/Engine", "--issue", "8"], checkpoint=self.root / "runs.sqlite3",
                        live_run=LiveRun(self.forge, self.workspace, skills=fixture_skills))
        self.assertEqual((code, "HTTP 403" in err.getvalue()), (75, True))
        self.assertIn("Foreman halted with git_failed.", self.forge.status[("Example/Engine", 8)])
        self.assertIn("HTTP 403. No setting needs to change.", self.forge.status[("Example/Engine", 8)])
        self.assertEqual(self.worker_calls, 0)
        self.assertIsNone(saved_workspace(self.root / "runs.sqlite3", THREAD))

    def test_a_failed_github_read_at_a_start_reports_git_failed_and_saves_nothing(self):
        """TR13: GitHub fails a read of doctor's GitHub requirements at a new run's start and at a resume's start, and once
        fails the status comment write as well. Each start makes no paid call and reports git_failed in the issue's status
        comment, naming the failed read and saying that no setting needs to change. The new run saves no checkpoint and keeps
        Ready. The resume keeps its checkpoint and its earlier halt. When the comment write fails too, the command exits
        non-zero with the error on its error output."""
        unreadable = patch.object(self.forge, "has_label", side_effect=ForgeError("HTTP 502 reading the Ready label"))
        with unreadable:
            code, out, err = self.start()
        self.assertEqual(code, 75, out + err)
        status = self.forge.status[("Example/Engine", 8)]
        self.assertIn("Foreman halted with git_failed.", status)
        self.assertIn("HTTP 502 reading the Ready label. No setting needs to change. Remove and add the Ready label to try again.", status)
        self.assertNotIn("not_ready", status)
        self.assertEqual((self.worker_calls, self.forge.load_issue("Example/Engine", 8)["labels"]), (0, ["slice", "Ready"]))
        self.assertIsNone(saved_workspace(self.root / "runs.sqlite3", THREAD))

        self.assertEqual(self.start({**SETTINGS, "FOREMAN_MAX_TOKENS": "1"}, [{"triage": "supplied_slice"}])[0], 75)
        halted = self.stored()
        self.forge.ready_again(8)
        with unreadable:
            code, out, err = self.start({**SETTINGS, "FOREMAN_MAX_TOKENS": "900000"})
        self.assertEqual(code, 75, out + err)
        status = self.forge.status[("Example/Engine", 8)]
        self.assertIn("No setting needs to change. The run stays halted at its step. Add the Ready label again to try again.", status)
        self.assertEqual((self.worker_calls, self.stored()), (1, halted))
        self.assertEqual(self.forge.load_issue("Example/Engine", 8)["labels"], ["slice", "Stopped"])
        with SqliteSaver.from_conn_string(str(self.root / "runs.sqlite3")) as saver:
            saver.serde = checkpoint_serializer()
            self.assertEqual(saver.get_tuple({"configurable": {"thread_id": THREAD}}).checkpoint["channel_values"]["halt"], "budget_exceeded")

        self.forge.ready_again(8)
        with unreadable, patch.object(self.forge, "upsert_comment", side_effect=ForgeError("HTTP 502 writing the comment")):
            code, out, err = self.start({**SETTINGS, "FOREMAN_MAX_TOKENS": "900000"})
        self.assertNotEqual(code, 0)
        self.assertIn("HTTP 502 reading the Ready label", err)
        self.assertIn("status not written: HTTP 502 writing the comment", err)
        self.assertEqual((self.worker_calls, self.stored()), (1, halted))

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
        self.assertIsNone(saved_workspace(self.root / "runs.sqlite3", THREAD))

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
        self.assertEqual((self.stored()[0].token_cap, self.worker_calls), (1, 1))
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
        self.assertEqual((self.stored()[0].token_cap, self.worker_calls), (900000, 6))
        # A finished run is never started again.
        self.assertEqual(self.start({**SETTINGS, "FOREMAN_MAX_TOKENS": "900000"})[:2], (0, f"already started {THREAD}\n"))

    def test_a_resume_refused_by_doctor_keeps_its_halt_and_resumes_once_the_setting_is_fixed(self):
        """TR9: a run halted budget_exceeded, the operator sets FOREMAN_MAX_BUDGET_USD to 4.5o and adds Ready, then corrects
        it and adds Ready again. The first start reports not_ready naming the variable and its fix, makes no paid call and keeps
        the checkpoint and its budget_exceeded halt. The second resumes at the interrupted step within the corrected cap."""
        code, out, err = self.start({**SETTINGS, "FOREMAN_MAX_TOKENS": "1"}, [{"triage": "supplied_slice"}])
        self.assertEqual(code, 75, out + err)
        halted = self.stored()
        # A resume runs only doctor's GitHub requirements: the contract already passed at the pinned base.
        del self.workspace.base_files[CONTRACT_PATH]
        self.forge.ready_again(8)
        # The operator raises the cap with a typo. Doctor's quick check refuses the resume before any paid call.
        code, out, err = self.start({**SETTINGS, "FOREMAN_MAX_TOKENS": "900000", "FOREMAN_MAX_BUDGET_USD": "4.5o"})
        self.assertEqual(code, 1, out + err)
        status = self.forge.status[("Example/Engine", 8)]
        self.assertIn("Not ready: the FOREMAN_MAX_BUDGET_USD repository variable '4.5o' is not a positive dollar amount. "
                      "Fix: set FOREMAN_MAX_BUDGET_USD to a positive amount", status)
        self.assertIn("The run stays halted at its step. Then add the Ready label again to resume it.", status)
        self.assertEqual(self.forge.load_issue("Example/Engine", 8)["labels"], ["slice", "Stopped"])
        self.assertEqual(self.worker_calls, 1)
        # The checkpoint and its budget halt are untouched, so the next Ready resumes the halted step.
        self.assertEqual(self.stored(), halted)
        with SqliteSaver.from_conn_string(str(self.root / "runs.sqlite3")) as saver:
            saver.serde = checkpoint_serializer()
            self.assertEqual(saver.get_tuple({"configurable": {"thread_id": THREAD}}).checkpoint["channel_values"]["halt"], "budget_exceeded")
        self.forge.ready_again(8)
        code, out, err = self.start({**SETTINGS, "FOREMAN_MAX_TOKENS": "900000", "FOREMAN_MAX_BUDGET_USD": "4.50"},
                                    [{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE])
        self.assertEqual(code, 0, out + err)
        self.assertIn("done merged", out)
        self.assertEqual(self.stored()[0].max_budget_usd, "4.5")

    def test_a_cap_below_what_the_run_spent_stops_at_the_first_reservation(self):
        code, out, err = self.start({**SETTINGS, "FOREMAN_MAX_TOKENS": "1"}, [{"triage": "supplied_slice"}])
        self.assertEqual(code, 75, out + err)
        with SqliteSaver.from_conn_string(str(self.root / "runs.sqlite3")) as saver:
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
                run = self.stored()[0]
                self.assertEqual((str(run.token_cap), run.max_budget_usd),
                                 (variables["FOREMAN_MAX_TOKENS"], variables.get("FOREMAN_MAX_BUDGET_USD", "3")))

    def test_ready_again_resumes_in_the_saved_checkout_and_a_new_issue_in_its_managed_one(self):
        self.assertEqual(self.start({**SETTINGS, "FOREMAN_MAX_TOKENS": "1"}, [{"triage": "supplied_slice"}])[0], 75)
        state_home, seen = self.root / "state", []
        with patch.dict("os.environ", {"XDG_STATE_HOME": str(state_home)}), patch("foreman.cli.LiveForge", return_value=self.forge), \
                patch.object(self.forge, "variables", return_value={**SETTINGS, "FOREMAN_MAX_TOKENS": "900000"}), \
                patch("foreman.cli.run_live_delivery", side_effect=lambda **kwargs: seen.append(kwargs) or {"stop": "merged"}), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            for issue in ("8", "9"):
                self.assertEqual(main(["start", "--repo", "Example/Engine", "--issue", issue], checkpoint=self.root / "runs.sqlite3"), 0)
        self.assertEqual([(kwargs["issue"], kwargs["run"].workspace.root) for kwargs in seen], [
            (8, self.root.resolve()), (9, (state_home / "foreman").resolve() / "runs" / "example" / "engine" / "9")])

    def test_a_run_on_a_terminal_halt_is_never_started_again(self):
        code, out, err = self.start(SETTINGS, [{"triage": "unknown route"}, {"triage": "unknown route"}])
        self.assertEqual(code, 1, out + err)
        self.assertIn("invalid_output", out + err)
        calls = self.worker_calls
        self.forge.ready_again(8)
        self.assertEqual(self.start({**SETTINGS, "FOREMAN_MAX_TOKENS": "900000"})[:2], (0, f"already started {THREAD}\n"))
        self.assertEqual(self.worker_calls, calls)

    def test_without_app_credentials_nothing_is_read_or_run(self):
        self.app.return_value = None
        with patch.object(self.forge, "variables") as variables, patch("foreman.cli.run_live_delivery") as run:
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
        self.assertEqual(saved_workspace(self.root / "runs.sqlite3", THREAD), str(self.root.resolve()))

    def test_the_holder_reenters_its_own_lock(self):
        with issue_lock(THREAD), issue_lock(THREAD):
            with (self.root / "runtime" / "foreman-locks" / spec_digest([THREAD])).open("a") as other:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with issue_lock(THREAD):
            pass

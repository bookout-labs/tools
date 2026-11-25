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
from foreman.delivery import DEFAULT_TOKEN_CAP, LiveRun
from foreman.github import VariablesUnreadable
from foreman.runner import IssueLocked, issue_lock, saved_workspace
from foreman.stage_output import spec_digest
from foreman.state import checkpoint_serializer
from foreman.usd import BudgetUsd
from foreman.workspace import lock_file
from skill_fixture import checker_reply, fixture_skills
from test_runner import APPROVE, FakeGitHub, FakeWorkspace

THREAD = "example/engine#8"
SETTINGS = {"FOREMAN_CHECK": "exit 0", "FOREMAN_HUMAN_LOGIN": "fixture-owner"}


class StartTest(unittest.TestCase):
    def setUp(self):
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
        variables = {**SETTINGS, "FOREMAN_PREFLIGHT": "exit 0", "FOREMAN_MAX_TOKENS": "900000"}
        code, out, err = self.start(variables, [{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE])
        self.assertNotEqual(code, 1, out + err)
        run, checks = self.stored()
        self.assertEqual((checks.check, checks.preflight), ("exit 0", "exit 0"))
        self.assertEqual((run.token_cap, run.max_budget_usd), (900000, "3"))
        calls = self.worker_calls
        # A repeated delivery for a finished run is a no-op.
        self.assertEqual(self.start(variables)[:2], (0, f"already started {THREAD}\n"))
        self.assertEqual(self.worker_calls, calls)

    def test_a_started_run_resumes_without_a_check_and_a_new_run_needs_one(self):
        self.start(SETTINGS, [{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE])
        store = self.root / "runs.sqlite3"
        self.assertEqual(saved_workspace(store, THREAD), str(self.root.resolve()))
        self.assertIsNone(saved_workspace(store, "example/engine#9"))
        live = LiveRun(self.forge, self.workspace, skills=fixture_skills, human_login="fixture-owner")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertNotEqual(main(["run", "delivery", "--repo", "Example/Engine", "--issue", "8"], checkpoint=store, live_run=live), 2)
            with self.assertRaises(SystemExit) as refused:
                main(["run", "delivery", "--repo", "Example/Engine", "--issue", "9"], checkpoint=store, live_run=live)
        self.assertEqual(refused.exception.code, 2)

    def test_settings_default_and_parse(self):
        seen = []
        with patch("foreman.cli.run_live_delivery", side_effect=lambda **kwargs: seen.append(kwargs) or {"stop": "merged"}):
            self.assertEqual(self.start({**SETTINGS, "FOREMAN_MAX_BUDGET_USD": "4.50"})[0], 0)
            self.assertEqual(self.start()[0], 0)
        self.assertEqual([(s["check"], s["preflight"], s["max_tokens"], s["max_budget_usd"]) for s in seen], [
            ("exit 0", "", DEFAULT_TOKEN_CAP, BudgetUsd(Decimal("4.50"))),
            ("exit 0", "", DEFAULT_TOKEN_CAP, BudgetUsd(Decimal("3"))),
        ])

    def test_missing_or_malformed_settings_fail_closed_before_any_run(self):
        for variables in ({}, {"FOREMAN_CHECK": " "}, {"FOREMAN_CHECK": "exit 0"}, {**SETTINGS, "FOREMAN_HUMAN_LOGIN": " "},
                          {**SETTINGS, "FOREMAN_MAX_TOKENS": "many"},
                          {**SETTINGS, "FOREMAN_MAX_TOKENS": "0"}, {**SETTINGS, "FOREMAN_MAX_BUDGET_USD": "-1"},
                          {**SETTINGS, "FOREMAN_MAX_BUDGET_USD": ""}):
            with self.subTest(variables=variables), patch("foreman.cli.run_live_delivery") as run:
                code, _, err = self.start(variables)
                self.assertEqual(code, 1)
                self.assertRegex(err, "FOREMAN_CHECK|FOREMAN_HUMAN_LOGIN|FOREMAN_MAX_TOKENS")
                run.assert_not_called()
        with patch.object(self.forge, "variables", side_effect=VariablesUnreadable("HTTP 403")), \
                patch("foreman.cli.run_live_delivery") as run, contextlib.redirect_stderr(io.StringIO()) as err:
            code = main(["start", "--repo", "Example/Engine", "--issue", "8"], checkpoint=self.root / "runs.sqlite3",
                        live_run=LiveRun(self.forge, self.workspace, skills=fixture_skills, human_login="fixture-owner"))
        self.assertEqual((code, "HTTP 403" in err.getvalue()), (1, True))
        run.assert_not_called()

    def test_a_missing_setting_is_commented_once_while_ready_waits(self):
        for _ in range(2):
            self.assertEqual(self.start({})[0], 1)
        self.assertEqual(len(self.forge.status_writes), 1)
        self.assertIn("Set the FOREMAN_CHECK repository variable", self.forge.status[("Example/Engine", 8)])
        self.assertEqual(self.start({**SETTINGS, "FOREMAN_MAX_BUDGET_USD": "-1"})[0], 1)
        self.assertIn("FOREMAN_MAX_BUDGET_USD a positive amount", self.forge.status[("Example/Engine", 8)])
        self.assertEqual(self.forge.label_calls, [])
        self.assertEqual(self.forge.load_issue("Example/Engine", 8)["labels"], ["slice", "Ready"])

    def test_a_stale_delivery_without_ready_is_not_commented(self):
        self.forge = FakeGitHub(["slice"])
        self.assertEqual(self.start({})[0], 1)
        self.assertEqual((self.forge.status, self.forge.label_calls), ({}, []))

    def test_a_duplicate_ready_gives_one_checkpoint_and_the_issue_shows_doing(self):
        self.forge.queue_merges = False
        code, out, err = self.start(SETTINGS, [{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE])
        self.assertEqual(code, 75, out + err)
        writes = list(self.forge.status_writes)
        self.assertEqual(self.start()[:2], (0, f"already started {THREAD}\n"))
        self.assertEqual(self.forge.status_writes, writes)
        self.assertEqual(self.forge.load_issue("Example/Engine", 8)["labels"], ["slice", "Doing"])
        self.assertEqual(list(self.forge.status), [("Example/Engine", 8)])

    def test_ready_again_resumes_a_budget_halt_with_the_raised_repository_cap(self):
        replies = [{"triage": "supplied_slice"}, {"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE]
        code, out, err = self.start({**SETTINGS, "FOREMAN_MAX_TOKENS": "1"}, replies[:1])
        self.assertEqual(code, 75, out + err)
        self.assertEqual((self.stored()[0].token_cap, self.worker_calls), (1, 1))
        self.forge.ready_again(8)
        # An unchanged cap stops again before any worker call, and a lowered one is refused.
        self.assertEqual(self.start({**SETTINGS, "FOREMAN_MAX_TOKENS": "1"})[0], 75)
        self.forge.ready_again(8)
        self.assertEqual(self.start({**SETTINGS, "FOREMAN_MAX_TOKENS": "1", "FOREMAN_MAX_BUDGET_USD": "2"})[0], 1)
        self.assertEqual(self.worker_calls, 1)
        self.forge.ready_again(8)
        # The halted triage answer was never applied, so the resumed route asks again, and the plan checker answers too.
        code, out, err = self.start({**SETTINGS, "FOREMAN_MAX_TOKENS": "900000"}, replies[1:])
        self.assertEqual(code, 0, out + err)
        self.assertIn("done merged", out)
        self.assertEqual((self.stored()[0].token_cap, self.worker_calls), (900000, 5))
        # A finished run is never started again.
        self.assertEqual(self.start({**SETTINGS, "FOREMAN_MAX_TOKENS": "900000"})[:2], (0, f"already started {THREAD}\n"))

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

import contextlib
import io
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

from langgraph.checkpoint.sqlite import SqliteSaver

from foreman.cli import main
from foreman.delivery import LiveRun
from foreman.stage_output import PULL_REQUEST_LINES
from foreman.workspace import LiveWorkspace
from test_runner import APPROVE, BRANCH, EMPTY_SLICE, FakeGitHub, worker_reply


class PullRequestSizeTest(unittest.TestCase):
    def setUp(self):
        directory = Path(self.enterContext(TemporaryDirectory()))
        self.root = directory / "checkout"
        self.root.mkdir()
        self.checkpoint = directory / "run.sqlite3"
        self.forge = FakeGitHub()
        self.stages = []
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Synthetic Test")
        self.git("config", "user.email", "synthetic@example.invalid")
        self.write(".gitattributes", (Path(__file__).resolve().parents[1] / ".gitattributes").read_text())
        self.write("src/items.py", "old item\n")
        self.base = self.commit()
        self.write("src/main-only.py", "main\n" * (PULL_REQUEST_LINES + 1))
        self.git("update-ref", "refs/remotes/origin/main", self.commit())
        self.git("switch", "-c", BRANCH, self.base)

    def git(self, *args):
        return subprocess.run(
            ["git", *args], cwd=self.root, capture_output=True, text=True, check=True,
        ).stdout.strip()

    def write(self, path, text):
        file = self.root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text)

    def commit(self):
        self.git("add", ".")
        self.git("-c", "commit.gpgsign=false", "commit", "-m", "Synthetic change")
        return self.git("rev-parse", "HEAD")

    def worker(self, args, cwd):
        self.assertEqual(cwd, self.root)
        prompt, state_json = args[-1].rsplit("\n\nDelivery state:\n", 1)
        stage = json.loads(prompt.rsplit("\n\nOutput schema:\n", 1)[1])["title"]
        self.stages.append(stage)
        replies = {
            "TriageOutput": {"triage": "implement", "slice": EMPTY_SLICE},
            "ImplementOutput": {"implementation": "Built."},
            "SpecOutput": {"spec": "Ship the list first, then add sorting.", "spec_gate": "ready"},
            "ReviewOutput": APPROVE,
        }
        if stage == "SpecOutput":
            self.spec_state = json.loads(state_json)
            if "slice" not in self.forge.labels:
                replies[stage]["slices"] = [EMPTY_SLICE, {**EMPTY_SLICE, "title": "Sort items"}]
        return worker_reply(args, replies[stage])

    def invoke(self, *flags, expected_code=0):
        with contextlib.redirect_stdout(io.StringIO()) as stdout, contextlib.redirect_stderr(io.StringIO()) as stderr:
            code = main(
                ["run", "delivery", "--repo", "Example/Engine", "--issue", "8", "--check", "exit 0", *flags],
                worker_runner=self.worker, checkpoint=self.checkpoint, live_run=LiveRun(self.forge, LiveWorkspace(self.root)),
            )
        self.assertEqual(code, expected_code, stderr.getvalue())
        return stdout.getvalue(), stderr.getvalue()

    def test_under_limit_diff_reaches_review(self):
        self.write("src/items.py", "changed\n" * (PULL_REQUEST_LINES - 2))
        head = self.commit()
        stdout, _ = self.invoke()
        self.assertIn(f"pull request lines {PULL_REQUEST_LINES - 1}\n", stdout)
        self.assertIn("done verified", stdout)
        self.assertEqual(self.forge.merge_calls, [("Example/Engine", 8, BRANCH, head)])

    def test_limit_is_inclusive_and_a_pure_rename_adds_no_lines(self):
        self.git("mv", "src/items.py", "src/renamed\titems.py")
        self.write("tests/test_items.py", "handwritten\n" * PULL_REQUEST_LINES)
        self.commit()
        stdout, _ = self.invoke()
        self.assertIn(f"pull request lines {PULL_REQUEST_LINES}\n", stdout)
        self.assertIn("done verified", stdout)

    def test_oversized_diff_goes_to_spec_with_the_measured_count(self):
        self.write("tests/test_items.py", "handwritten\n" * (PULL_REQUEST_LINES + 1))
        head = self.commit()
        stdout, _ = self.invoke()
        self.assertEqual(self.stages, ["TriageOutput", "ImplementOutput", "SpecOutput"])
        self.assertEqual(self.spec_state["pr_size"], {
            "head": head, "merge_base": self.base, "lines": PULL_REQUEST_LINES + 1,
        })
        self.assertIn("paused needs_human", stdout)
        self.assertIn(f"{PULL_REQUEST_LINES + 1} changed lines", stdout)
        self.assertIn("done pr_split_required", self.invoke("--answer", "split")[0])
        self.assertEqual(self.stages, ["TriageOutput", "ImplementOutput", "SpecOutput"])
        self.assertEqual(self.forge.merge_calls, [])

    def test_generated_only_diff_reaches_review(self):
        for path in ("tests/fixtures/nested/example\tline\n.json", "tests/stage-schemas.json", "src/example.lock"):
            self.write(path, "generated\n" * (PULL_REQUEST_LINES + 1))
        head = self.commit()
        stdout, _ = self.invoke()
        self.assertIn("pull request lines 0\n", stdout)
        self.assertIn("done verified", stdout)
        self.assertEqual(self.forge.merge_calls, [("Example/Engine", 8, BRANCH, head)])

    def test_only_a_typed_operator_choice_records_an_override(self):
        self.forge = FakeGitHub(["factory"])
        self.write("src/items.py", "changed\n" * PULL_REQUEST_LINES)
        head = self.commit()
        stdout, _ = self.invoke()
        self.assertIn("paused needs_human", stdout)
        self.assertIn("split or override", self.invoke("--answer", "yes", expected_code=1)[1])
        self.assertEqual(self.invoke()[0], stdout)
        stdout, _ = self.invoke("--answer", "override")
        self.assertIn("done verified", stdout)
        self.assertEqual(self.forge.merge_calls, [("Example/Engine", 8, BRANCH, head)])
        self.assertEqual(self.forge.created, [])
        self.assertEqual(self.invoke()[0], stdout)
        with SqliteSaver.from_conn_string(str(self.checkpoint)) as saver:
            saved = saver.get({"configurable": {"thread_id": "example/engine#8"}})
        assert saved is not None
        self.assertEqual(saved["channel_values"]["pr_size_override"], {
            "head": head, "merge_base": self.base, "lines": PULL_REQUEST_LINES + 1,
        })

    def test_accepting_the_parent_split_files_child_issues_without_review(self):
        self.forge = FakeGitHub(["factory"])
        self.write("tests/test_items.py", "handwritten\n" * (PULL_REQUEST_LINES + 1))
        self.commit()
        self.assertIn("paused needs_human", self.invoke()[0])
        self.assertEqual(self.forge.created, [])
        self.assertIn("done sliced", self.invoke("--answer", "split")[0])
        self.assertEqual([issue[0] for issue in self.forge.created], [EMPTY_SLICE["title"], "Sort items"])
        self.assertNotIn("ReviewOutput", self.stages)

    def test_override_cannot_approve_a_different_head_with_the_same_count(self):
        self.write("src/items.py", "changed\n" * PULL_REQUEST_LINES)
        self.commit()
        self.invoke()
        self.write("src/items.py", "new change\n" * PULL_REQUEST_LINES)
        new_head = self.commit()
        stdout, _ = self.invoke("--answer", "override")
        self.assertIn("paused needs_human", stdout)
        self.assertEqual(self.spec_state["pr_size"]["head"], new_head)
        self.assertEqual(self.spec_state["pr_size_override"]["lines"], self.spec_state["pr_size"]["lines"])
        self.assertNotIn("ReviewOutput", self.stages)
        self.assertEqual(self.forge.merge_calls, [])

    def test_branch_attributes_cannot_exempt_its_own_diff(self):
        self.write(".gitattributes", "src/** linguist-generated -diff\n")
        self.write("src/items.py", "changed\n" * PULL_REQUEST_LINES)
        self.commit()
        stdout, _ = self.invoke()
        self.assertIn("paused needs_human", stdout)
        self.assertEqual(self.spec_state["pr_size"]["lines"], PULL_REQUEST_LINES + 1)

    def test_missing_base_stops_before_review(self):
        self.git("update-ref", "-d", "refs/remotes/origin/main")
        stdout, _ = self.invoke()
        self.assertIn("done pr_size_unavailable", stdout)
        self.assertNotIn("ReviewOutput", self.stages)
        self.assertEqual(self.forge.merge_calls, [])

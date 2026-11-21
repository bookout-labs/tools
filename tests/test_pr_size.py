import tests

from dataclasses import replace
from skill_fixture import fixture_pin, fixture_skills
import contextlib
import io
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

from langgraph.checkpoint.sqlite import SqliteSaver

from foreman.cli import main
from foreman.delivery import DEFAULT_TOKEN_CAP, LiveRun, compile_delivery
from foreman.config import load_config
from foreman.stage_output import PULL_REQUEST_LINES
from foreman.workspace import LiveWorkspace
from test_runner import APPROVE, BRANCH, EMPTY_SLICE, FakeGitHub, worker_reply


class PullRequestSizeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(cls.enterClassContext(TemporaryDirectory()))
        cls.config = replace(load_config(), pstack=fixture_pin(root / "fixture"))

    def setUp(self):
        directory = Path(self.enterContext(TemporaryDirectory()))
        self.root = directory / "checkout"
        self.root.mkdir()
        self.checkpoint = directory / "run.sqlite3"
        self.forge = FakeGitHub()
        self.stages = []
        self.original_spec = None
        remote = directory / "origin.git"
        self.git("init", "--bare", str(remote))
        self.git("init", "-b", "main")
        self.git("remote", "add", "origin", str(remote))
        self.git("config", "user.name", "Synthetic Test")
        self.git("config", "user.email", "synthetic@example.invalid")
        self.write(".gitattributes", (Path(__file__).resolve().parents[1] / ".gitattributes").read_text())
        self.write("src/items.py", "old item\n")
        self.base = self.commit()
        self.forge.base = self.base
        self.write("src/main-only.py", "main\n" * (PULL_REQUEST_LINES + 1))
        self.commit()
        self.git("push", "origin", "main")
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

    def worker(self, args, cwd, env):
        self.assertEqual(cwd, self.root)
        _, state_json = args[-1].rsplit("\n\nDelivery state:\n", 1)
        schema = Path(args[args.index("--output-schema") + 1]).read_text() if args[0] == "codex" else args[args.index("--json-schema") + 1]
        stage = json.loads(schema)["title"]
        self.stages.append(stage)
        state = json.loads(state_json)
        replies = {
            "TriageOutput": {"triage": "implement", "slice": EMPTY_SLICE},
            "ImplementOutput": {"implementation": "Built."},
            "SpecOutput": {"spec": "Ship the list first, then add sorting.", "spec_gate": "ready"},
            "ReviewOutput": APPROVE,
        }
        if stage == "TriageOutput" and self.original_spec:
            return worker_reply(args, {"triage": "spec"})
        if stage == "SpecOutput":
            if self.original_spec and "pr_size" not in state:
                return worker_reply(args, {"spec": self.original_spec, "spec_gate": "ready"})
            self.spec_state = state
            if "slice" not in self.forge.labels:
                replies[stage]["slices"] = [EMPTY_SLICE, {**EMPTY_SLICE, "title": "Sort items"}]
        return worker_reply(args, replies[stage])

    def invoke(self, *flags, expected_code=0):
        if not self.checkpoint.exists():
            with SqliteSaver.from_conn_string(str(self.checkpoint)) as saver:
                graph = compile_delivery(None, self.config, run=LiveRun(self.forge, LiveWorkspace(self.root), skills=fixture_skills), checkpointer=saver)
                graph.update_state({"configurable": {"thread_id": "example/engine#8"}}, {
                    "pstack_pin": self.config.pstack.model_dump(), "token_cap": DEFAULT_TOKEN_CAP,
                    "repo": "Example/Engine", "issue": 8, "issue_kind": "slice" if "slice" in self.forge.labels else "parent", "slice": EMPTY_SLICE,
                    **({"spec": self.original_spec} if self.original_spec else {}),
                    "workspace": str(self.root.resolve()), "check": "exit 0", "head_sha": self.git("rev-parse", "HEAD"),
                    "models_used": {"implement": "gpt-6.1-sol-high"}, "path": ["triage", "implement", "publish"],
                }, as_node="publish")
        with contextlib.redirect_stdout(io.StringIO()) as stdout, contextlib.redirect_stderr(io.StringIO()) as stderr:
            code = main(
                ["run", "delivery", "--repo", "Example/Engine", "--issue", "8", "--check", "exit 0", *flags],
                worker_runner=self.worker, checkpoint=self.checkpoint, live_run=LiveRun(self.forge, LiveWorkspace(self.root), skills=fixture_skills),
            )
        self.assertEqual(code, expected_code, stderr.getvalue())
        return stdout.getvalue(), stderr.getvalue()

    def checkpoint_values(self):
        with SqliteSaver.from_conn_string(str(self.checkpoint)) as saver:
            saved = saver.get({"configurable": {"thread_id": "example/engine#8"}})
        assert saved is not None
        return saved["channel_values"]

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
        stdout, _ = self.invoke(expected_code=3)
        self.assertEqual(self.stages, ["SpecOutput"])
        self.assertEqual(self.spec_state["pr_size"], {
            "head": head, "merge_base": self.base, "lines": PULL_REQUEST_LINES + 1,
        })
        self.assertIn("paused needs_human", stdout)
        self.assertIn(f"{PULL_REQUEST_LINES + 1} changed lines", stdout)
        self.assertIn("done pr_split_required", self.invoke("--answer", "split", expected_code=1)[0])
        self.assertEqual(self.stages, ["SpecOutput"])
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
        self.forge.base = self.base
        self.write("src/items.py", "changed\n" * PULL_REQUEST_LINES)
        head = self.commit()
        stdout, _ = self.invoke(expected_code=3)
        self.assertIn("paused needs_human", stdout)
        self.assertIn("split or override", self.invoke("--answer", "yes", expected_code=1)[1])
        self.assertEqual(self.invoke(expected_code=3)[0], stdout)
        stdout, _ = self.invoke("--answer", "override")
        self.assertIn("done verified", stdout)
        self.assertEqual(self.forge.merge_calls, [("Example/Engine", 8, BRANCH, head)])
        self.assertEqual(self.forge.created, [])
        self.assertEqual(self.invoke()[0], stdout)
        self.assertEqual(self.checkpoint_values()["pr_size_override"], {
            "head": head, "merge_base": self.base, "lines": PULL_REQUEST_LINES + 1,
        })

    def test_accepting_the_parent_split_files_child_issues_without_review(self):
        self.forge = FakeGitHub(["factory"])
        self.forge.base = self.base
        self.write("tests/test_items.py", "handwritten\n" * (PULL_REQUEST_LINES + 1))
        self.commit()
        self.assertIn("paused needs_human", self.invoke(expected_code=3)[0])
        self.assertEqual(self.forge.created, [])
        self.assertIn("done sliced", self.invoke("--answer", "split")[0])
        self.assertEqual([issue[0] for issue in self.forge.created], [EMPTY_SLICE["title"], "Sort items"])
        self.assertNotIn("ReviewOutput", self.stages)

    def test_override_cannot_approve_a_different_head_with_the_same_count(self):
        self.write("src/items.py", "changed\n" * PULL_REQUEST_LINES)
        self.commit()
        self.invoke(expected_code=3)
        self.write("src/items.py", "new change\n" * PULL_REQUEST_LINES)
        new_head = self.commit()
        stdout, _ = self.invoke("--answer", "override", expected_code=3)
        self.assertIn("paused needs_human", stdout)
        values = self.checkpoint_values()
        self.assertEqual(values["pr_size"]["head"], new_head)
        self.assertEqual(values["pr_size_override"]["lines"], values["pr_size"]["lines"])
        self.assertNotIn("ReviewOutput", self.stages)
        self.assertEqual(self.forge.merge_calls, [])

    def test_split_preserves_spec_and_new_push_prompts_with_new_count(self):
        self.original_spec = "List open items without changing their order."
        self.write("tests/test_items.py", "handwritten\n" * 500)
        old_head = self.commit()
        self.git("push", "origin", BRANCH)
        self.assertIn("500 changed lines", self.invoke(expected_code=3)[0])
        values = self.checkpoint_values()
        self.assertEqual(values["spec"], self.original_spec)
        self.assertEqual(values["pr_split"]["plan"], "Ship the list first, then add sorting.")

        self.write("tests/test_items.py", "handwritten\n" * 501)
        new_head = self.commit()
        self.git("push", "origin", BRANCH)
        stdout, _ = self.invoke("--answer", "override", expected_code=3)
        self.assertIn("501 changed lines", stdout)
        self.assertIn("paused needs_human", stdout)
        values = self.checkpoint_values()
        self.assertEqual(values["spec"], self.original_spec)
        self.assertEqual(values["pr_size_override"]["head"], old_head)
        self.assertEqual(values["pr_size"]["head"], new_head)
        self.assertEqual(self.stages, ["SpecOutput"])
        self.assertEqual(self.forge.merge_calls, [])
        self.assertIn("done verified", self.invoke("--answer", "override")[0])

    def test_branch_attributes_cannot_exempt_its_own_diff(self):
        self.write(".gitattributes", "src/** linguist-generated -diff\n")
        self.write("src/items.py", "changed\n" * PULL_REQUEST_LINES)
        self.commit()
        stdout, _ = self.invoke(expected_code=3)
        self.assertIn("paused needs_human", stdout)
        self.assertEqual(self.spec_state["pr_size"]["lines"], PULL_REQUEST_LINES + 1)

    def test_missing_base_stops_before_review(self):
        self.forge.base_branch = "missing"
        self.git("update-ref", "refs/remotes/origin/missing", self.base)
        stdout, _ = self.invoke(expected_code=1)
        self.assertIn("done pr_size_unavailable", stdout)
        self.assertNotIn("ReviewOutput", self.stages)
        self.assertEqual(self.forge.merge_calls, [])

    def test_repository_info_attributes_stop_the_run_and_name_the_file(self):
        self.write(".git/info/attributes", "src/** linguist-generated\n")
        self.write("src/items-extra.py", "handwritten\n" * 500)
        self.commit()
        stdout, _ = self.invoke(expected_code=1)
        self.assertIn("done pr_size_unavailable", stdout)
        self.assertIn(".git/info/attributes must be empty", stdout)
        self.assertNotIn("ReviewOutput", self.stages)

    def test_core_attributes_file_cannot_exempt_handwritten_lines(self):
        self.write("../local-attributes", "src/** linguist-generated\n")
        self.git("config", "core.attributesFile", str(self.root.parent / "local-attributes"))
        self.write("src/items-extra.py", "handwritten\n" * 500)
        self.commit()
        self.assertIn("500 changed lines", self.invoke(expected_code=3)[0])
        self.assertNotIn("ReviewOutput", self.stages)

    def test_nul_bearing_file_counts_all_500_lines(self):
        self.write("src/items.dat", "\0item\n" * 500)
        self.commit()
        self.assertIn("500 changed lines", self.invoke(expected_code=3)[0])
        self.assertNotIn("ReviewOutput", self.stages)

    def test_literal_true_is_generated_and_false_is_counted(self):
        self.git("switch", "main")
        self.write(".gitattributes", "src/generated.py linguist-generated=true\nsrc/handwritten.py linguist-generated=false\n")
        base = self.commit()
        self.git("push", "origin", "main")
        self.git("switch", "-C", BRANCH, base)
        self.write("src/generated.py", "generated\n" * 500)
        self.write("src/handwritten.py", "handwritten\n" * 12)
        self.commit()
        stdout, _ = self.invoke()
        self.assertIn("pull request lines 12\n", stdout)
        self.assertIn("done verified", stdout)

    def test_fetch_refreshes_the_actual_pr_base_before_measurement(self):
        self.forge.base_branch = "release/current"
        self.git("switch", "-c", self.forge.base_branch, self.base)
        self.git("push", "origin", self.forge.base_branch)
        self.write("src/release.py", "existing on release\n" * 500)
        release_tip = self.commit()
        self.git("push", "origin", self.forge.base_branch)
        tracking_ref = f"refs/remotes/origin/{self.forge.base_branch}"
        self.git("update-ref", tracking_ref, self.base)
        self.git("switch", "-C", BRANCH, release_tip)
        self.write("src/items.py", "new item\n")
        self.commit()
        stdout, _ = self.invoke()
        self.assertIn("pull request lines 2\n", stdout)
        self.assertIn("done verified", stdout)
        self.assertEqual(self.git("rev-parse", tracking_ref), release_tip)

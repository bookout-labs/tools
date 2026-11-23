import tests

from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from foreman.workspace import LiveWorkspace, WorkspaceError
from test_runner import BRANCH


class PullRequestSizeTest(unittest.TestCase):
    """The changed-line count that publication reports, measured from the PR's merge base."""

    def setUp(self):
        directory = Path(self.enterContext(TemporaryDirectory()))
        self.root = directory / "checkout"
        self.root.mkdir()
        remote = directory / "origin.git"
        self.git("init", "--bare", str(remote))
        self.git("init", "-b", "main")
        self.git("remote", "add", "origin", str(remote))
        self.git("config", "user.name", "Synthetic Test")
        self.git("config", "user.email", "synthetic@example.invalid")
        self.write(".gitattributes", (Path(__file__).resolve().parents[1] / ".gitattributes").read_text())
        self.write("src/items.py", "old item\n")
        self.base = self.commit()
        # Main moves past the branch point, so only the merge base can give the PR's count.
        self.write("src/main-only.py", "main\n" * 401)
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

    def lines(self, base_branch="main"):
        return LiveWorkspace(self.root).pull_request_size(self.git("rev-parse", "HEAD"), base_branch)["lines"]

    def branch_from_main(self):
        base = self.commit()
        self.git("push", "origin", "main")
        self.git("switch", "-C", BRANCH, base)
        return base

    def test_candidate_edits_and_adopted_commits_share_the_size_policy(self):
        self.git("switch", "main")
        self.write("deleted.md", "gone\n" * 7)
        self.write("original.dat", "\0item\n" * 500)
        self.write("original.md", "same\n" * 20)
        self.write("tests/fixtures/extract.json", "fixture\n" * 13)
        base = self.branch_from_main()
        self.git("rm", "deleted.md")
        self.git("mv", "original.dat", "renamed.dat")
        self.write("renamed.dat", "\0item\n" * 500 + "added\n")
        self.git("mv", "original.md", "renamed.md")
        self.git("mv", "tests/fixtures/extract.json", "extracted.md")
        self.write("new\tfile\n.md", "deliverable\n" * 401)
        self.write("tests/fixtures/generated.json", "excluded\n" * 500)
        attributes = (self.root / ".gitattributes").read_text()
        self.write(".gitattributes", attributes + "** linguist-generated -diff\n")
        workspace = LiveWorkspace(self.root)
        with patch.object(workspace, "pull_request_size", wraps=workspace.pull_request_size) as measure:
            edits = workspace.reconcile(base, BRANCH, "main")
        self.assertIsNotNone(edits)
        self.assertEqual(edits["size"]["lines"], 423)
        self.assertEqual(edits["size"]["merge_base"], base)
        self.assertEqual(measure.call_count, 1)
        index = Path(self.git("rev-parse", "--path-format=absolute", "--git-path", "index"))
        index_bytes = index.read_bytes()
        status = self.git("status", "--porcelain=v1", "-z")
        before = {path.relative_to(self.root): path.read_bytes() for path in self.root.rglob("*")
                  if path.is_file() and ".git" not in path.relative_to(self.root).parts}
        with patch.object(workspace, "_base_merge_base", side_effect=AssertionError("no fetch")):
            measured = workspace.pull_request_size(None, "unused", merge_base=base)
        self.assertEqual(measured, edits["size"])
        self.assertEqual(index.read_bytes(), index_bytes)
        self.assertEqual(self.git("status", "--porcelain=v1", "-z"), status)
        self.assertEqual({path: (self.root / path).read_bytes() for path in before}, before)
        head = self.commit()
        adopted = workspace.reconcile(base, BRANCH, "main")
        self.assertEqual(adopted["size"], measured)
        self.assertEqual(workspace.pull_request_size(head, "main")["lines"], measured["lines"])
        self.assertEqual(workspace.tree(head), measured["head"])

    def test_later_round_sizes_edits_and_adopted_commits_against_merge_base(self):
        self.write("round-one.md", "first round\n" * 11)
        parent = self.commit()
        self.assertNotEqual(parent, self.base)
        self.write("round-two.md", "second round\n" * 7)
        workspace = LiveWorkspace(self.root)
        edits = workspace.reconcile(parent, BRANCH, "main")
        self.commit()
        adopted = workspace.reconcile(parent, BRANCH, "main")
        for candidate in (edits, adopted):
            with self.subTest(disposition=candidate["disposition"]):
                self.assertEqual(candidate["size"]["merge_base"], self.base)
                self.assertEqual(candidate["size"]["lines"], 18)
        self.assertEqual(adopted["size"], edits["size"])

    def test_changed_lines_count_both_sides(self):
        self.write("src/items.py", "changed\n" * 398)
        self.commit()
        self.assertEqual(self.lines(), 399)

    def test_a_pure_rename_adds_no_lines(self):
        self.git("mv", "src/items.py", "src/renamed\titems.py")
        self.write("tests/test_items.py", "handwritten\n" * 400)
        self.commit()
        self.assertEqual(self.lines(), 400)

    def test_deletions_and_edited_renames_outside_source_count(self):
        self.git("switch", "main")
        self.write("docs/deleted.md", "deleted\n" * 7)
        self.write("original.txt", "unchanged\n" * 100)
        self.branch_from_main()
        self.git("rm", "docs/deleted.md")
        renamed = "renamed \tline\n.txt"
        self.git("mv", "original.txt", renamed)
        self.commit()
        self.assertEqual(self.lines(), 7)
        self.write(renamed, "unchanged\n" * 100 + "added\n" * 2)
        self.commit()
        self.assertEqual(self.lines(), 9)

    def test_binary_rename_with_edit_counts_only_changed_lines(self):
        self.git("switch", "main")
        self.write("original.dat", "\0item\n" * 500)
        self.branch_from_main()
        self.git("mv", "original.dat", "renamed.dat")
        self.write("renamed.dat", "\0item\n" * 500 + "added\n")
        self.commit()
        self.assertEqual(self.lines(), 1)

    def test_nul_bearing_file_counts_every_line(self):
        self.write(":(glob)* weird\tline\n.dat", "\0item\n" * 500)
        self.commit()
        self.assertEqual(self.lines(), 500)

    def test_rename_across_generated_boundary_counts_handwritten_side(self):
        self.git("switch", "main")
        self.write("docs/original.md", "content\n" * 11)
        self.write("tests/fixtures/original.json", "fixture\n" * 13)
        self.branch_from_main()
        self.git("mv", "docs/original.md", "tests/fixtures/moved.json")
        self.git("mv", "tests/fixtures/original.json", "extracted.md")
        self.commit()
        self.assertEqual(self.lines(), 24)

    def test_generated_paths_count_nothing(self):
        for path in (
            "tests/fixtures/nested/example\tline\n.json", "tests/stage-schemas.json", "src/example.lock",
            "uv.lock", "package-lock.json", "pnpm-lock.yaml",
        ):
            self.write(path, "generated\n" * 401)
        self.commit()
        self.assertEqual(self.lines(), 0)

    def test_literal_true_is_generated_and_false_is_counted(self):
        self.git("switch", "main")
        self.write(".gitattributes", "src/generated.py linguist-generated=true\nsrc/handwritten.py linguist-generated=false\n")
        self.branch_from_main()
        self.write("src/generated.py", "generated\n" * 500)
        self.write("src/handwritten.py", "handwritten\n" * 12)
        self.commit()
        self.assertEqual(self.lines(), 12)

    def test_branch_attributes_cannot_exempt_its_own_diff(self):
        self.write(".gitattributes", (self.root / ".gitattributes").read_text() + "** linguist-generated -diff\n")
        self.write("docs/items.md", "changed\n" * 400)
        self.commit()
        self.assertEqual(self.lines(), 401)

    def test_core_attributes_file_cannot_exempt_handwritten_lines(self):
        self.write("../local-attributes", "src/** linguist-generated\n")
        self.git("config", "core.attributesFile", str(self.root.parent / "local-attributes"))
        self.write("src/items-extra.py", "handwritten\n" * 500)
        self.commit()
        self.assertEqual(self.lines(), 500)

    def test_repository_info_attributes_stop_measurement_and_name_the_file(self):
        self.write(".git/info/attributes", "src/** linguist-generated\n")
        self.write("src/items-extra.py", "handwritten\n" * 500)
        self.commit()
        with self.assertRaisesRegex(WorkspaceError, r"\.git/info/attributes must be empty"):
            self.lines()

    def test_hardened_grep_uses_base_attributes_and_basic_patterns(self):
        self.write("src/items.py", "first\nlast")
        self.write(".gitattributes", "src/items.py -diff\n")
        head = self.commit()
        self.git("config", "grep.patternType", "fixed")
        self.git("config", "grep.extendedRegexp", "true")
        self.git("config", "color.grep", "always")
        self.write("../local-attributes", "src/items.py -diff\n")
        self.git("config", "core.attributesFile", str(self.root.parent / "local-attributes"))
        raw = LiveWorkspace(self.root)._inspection_git(
            self.base, "grep", "-c", "-I", "-z", "--no-textconv", "-e", "^", head,
            "--", "src/items.py", allowed_returncodes=(0, 1),
        )
        self.assertEqual(raw, head.encode() + b":src/items.py\x002\n")

    def test_measurement_refreshes_the_actual_pr_base(self):
        release = "release/current"
        self.git("switch", "-c", release, self.base)
        self.write("src/release.py", "existing on release\n" * 500)
        release_tip = self.commit()
        self.git("push", "origin", release)
        tracking_ref = f"refs/remotes/origin/{release}"
        self.git("update-ref", tracking_ref, self.base)
        self.git("switch", "-C", BRANCH, release_tip)
        self.write("src/items.py", "new item\n")
        self.commit()
        self.assertEqual(self.lines(release), 2)
        self.assertEqual(self.git("rev-parse", tracking_ref), release_tip)

    def test_missing_base_stops_measurement(self):
        self.git("update-ref", "refs/remotes/origin/missing", self.base)
        self.write("src/items.py", "new item\n")
        self.commit()
        with self.assertRaises(WorkspaceError):
            self.lines("missing")


if __name__ == "__main__":
    unittest.main()

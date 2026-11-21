import tests

from dataclasses import FrozenInstanceError, replace
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

from foreman.delivery import DryRun
from foreman.review_context import ReviewContext, ReviewSide, SCRIPTED_REVIEW_CONTEXT
from foreman.workspace import LiveWorkspace, WorktreeInspectionFailed, changed_paths


class ChangedPathsTest(unittest.TestCase):
    def test_statuses_assign_paths_to_the_side_where_they_exist(self):
        raw = (b"A\0added\0D\0deleted\0M\0modified\0T\0type\0"
               b"R100\0old name\0new : name\0C075\0source\0copy\0")
        self.assertEqual(changed_paths(raw), (
            frozenset({"deleted", "modified", "type", "old name"}),
            frozenset({"added", "modified", "type", "new : name", "copy"}),
        ))
        self.assertEqual(changed_paths(b""), (frozenset(), frozenset()))

    def test_nul_delimiters_preserve_path_characters(self):
        self.assertEqual(changed_paths(b"M\0dir/tab\tline\n : file\0"), (
            frozenset({"dir/tab\tline\n : file"}), frozenset({"dir/tab\tline\n : file"}),
        ))

    def test_unknown_or_incomplete_records_fail_closed(self):
        for raw in (b"U\0unmerged\0", b"X\0unknown\0", b"M\0path", b"M\0",
                    b"R100\0old\0", b"C100\0source\0", b"A\0\0", b"\0", b"Rbad\0old\0new\0"):
            with self.subTest(raw=raw), self.assertRaises(WorktreeInspectionFailed):
                changed_paths(raw)


class ReviewDataTest(unittest.TestCase):
    def test_context_and_side_cannot_be_changed_after_construction(self):
        lines = {"items.txt": 3}
        side = ReviewSide("a" * 40, frozenset(lines), lines, frozenset())
        context = ReviewContext(side, side)
        lines["items.txt"] = 99
        self.assertEqual(context.head.lines, {"items.txt": 3})
        with self.assertRaises(FrozenInstanceError):
            context.head = side
        with self.assertRaises(FrozenInstanceError):
            side.sha = "b" * 40
        with self.assertRaises(TypeError):
            side.lines["items.txt"] = 99


class WorkspaceReviewContextTest(unittest.TestCase):
    def setUp(self):
        directory = Path(self.enterContext(TemporaryDirectory()))
        self.root = directory / "checkout"
        self.root.mkdir()
        self.git("init", "--bare", str(directory / "origin.git"))
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Synthetic Test")
        self.git("config", "user.email", "synthetic@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "core.filemode", "true")
        self.git("remote", "add", "origin", str(directory / "origin.git"))
        self.workspace = LiveWorkspace(self.root)

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, capture_output=True, check=True).stdout

    def write(self, path, content):
        file = self.root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(content)

    def commit(self):
        self.git("add", "--all")
        self.git("commit", "--allow-empty", "-m", "Synthetic change")
        return self.git("rev-parse", "HEAD").decode().strip()

    def test_merge_base_trees_line_counts_and_changes(self):
        original = {
            "README.md": b"unchanged\n", "modified.txt": b"first\nold\nlast\n",
            "deleted.txt": b"obsolete\n", "old name.txt": b"rename me\n",
            "binary.bin": b"\0old\n", "mode.sh": b"run\n",
            "space : name.txt": b"old\n", "existing-empty": b"",
            "tab\tline\n : name.txt": b"first\nlast",
        }
        for path, content in original.items():
            self.write(path, content)
        merge_base = self.commit()
        self.write("base-only.txt", b"base branch moved\n")
        base_tip = self.commit()
        self.git("push", "origin", "main")
        self.git("switch", "-c", "pr", merge_base)
        self.write("modified.txt", b"first\nnew\nlast\nextra")
        (self.root / "deleted.txt").unlink()
        self.git("mv", "old name.txt", "new : name.txt")
        self.write("binary.bin", b"\0new\nmore\n")
        (self.root / "mode.sh").chmod(0o755)
        self.write("empty.txt", b"")
        self.write("space : name.txt", b"new\n\nlast")
        head = self.commit()
        self.git("push", "origin", "pr")
        # A dirty checkout and local Git presentation settings must not affect committed trees.
        self.write("modified.txt", b"local edit\n")
        self.write("untracked.txt", b"local\n")
        self.git("config", "diff.renames", "false")
        self.git("config", "grep.lineNumber", "true")
        context = self.workspace.review_context(base_tip, head)
        self.assertEqual((context.base.sha, context.head.sha), (merge_base, head))
        self.assertEqual(context.base.files, frozenset(original))
        self.assertEqual(context.head.files, frozenset({
            "README.md", "modified.txt", "new : name.txt", "binary.bin", "mode.sh",
            "empty.txt", "space : name.txt", "existing-empty",
            "tab\tline\n : name.txt",
        }))
        self.assertEqual(context.base.lines, {
            "README.md": 1, "modified.txt": 3, "deleted.txt": 1,
            "old name.txt": 1, "mode.sh": 1, "space : name.txt": 1,
            "tab\tline\n : name.txt": 2,
        })
        self.assertEqual(context.head.lines, {
            "README.md": 1, "modified.txt": 4, "new : name.txt": 1,
            "mode.sh": 1, "space : name.txt": 3,
            "tab\tline\n : name.txt": 2,
        })
        self.assertEqual(context.base.changed, frozenset({
            "modified.txt", "deleted.txt", "old name.txt", "binary.bin", "mode.sh", "space : name.txt",
        }))
        self.assertEqual(context.head.changed, frozenset({
            "modified.txt", "new : name.txt", "binary.bin", "mode.sh", "space : name.txt", "empty.txt",
        }))

    def test_binary_empty_and_empty_trees_have_no_text_lines(self):
        base = self.commit()
        self.write("binary.bin", b"\0binary\n")
        self.write("empty.txt", b"")
        head = self.commit()
        self.git("push", "origin", "main")
        context = self.workspace.review_context(base, head)
        self.assertEqual(context.base.files, frozenset())
        self.assertEqual(context.head.files, frozenset({"binary.bin", "empty.txt"}))
        self.assertEqual(context.base.lines, {})
        self.assertEqual(context.head.lines, {})
        self.assertEqual(context.base.changed, frozenset())
        self.assertEqual(context.head.changed, frozenset({"binary.bin", "empty.txt"}))

    def test_non_full_shas_and_git_failures_raise_inspection_error(self):
        head = self.commit()
        self.git("push", "origin", "main")
        for base, target in (("main", head), (head, "HEAD"), (head[:12], head),
                             (head, "f" * 40), ("-" * 40, head)):
            with self.subTest(base=base, head=target), self.assertRaises(WorktreeInspectionFailed):
                self.workspace.review_context(base, target)
        # Use a separate directory outside the checkout so Git cannot discover its parent.
        nonrepo = self.root.parent / "not-a-repository"
        nonrepo.mkdir()
        with self.assertRaises(WorktreeInspectionFailed):
            LiveWorkspace(nonrepo).review_context(head, head)

    def test_git_grep_failure_is_not_treated_as_an_empty_lines_map(self):
        self.write("text.txt", b"text\n")
        head = self.commit()
        self.git("push", "origin", "main")
        self.git("config", "grep.patternType", "invalid")
        with self.assertRaises(WorktreeInspectionFailed):
            self.workspace.review_context(head, head)

    def test_line_counts_ignore_local_grep_pattern_settings(self):
        self.write("text.txt", b"first\nlast")
        head = self.commit()
        self.git("push", "origin", "main")
        self.git("config", "grep.patternType", "fixed")
        context = self.workspace.review_context(head, head)
        self.assertEqual(context.base.lines, {"text.txt": 2})
        self.assertEqual(context.head.lines, {"text.txt": 2})

    def test_line_counts_use_each_committed_trees_attributes(self):
        self.write("text.txt", b"first\nlast")
        base = self.commit()
        self.write(".gitattributes", b"text.txt -diff\n")
        head = self.commit()
        self.git("push", "origin", "main")
        attributes = self.root.parent / "local-attributes"
        attributes.write_text("text.txt -diff\n")
        for source in ("worktree", "attributesFile"):
            with self.subTest(source=source):
                if source == "worktree":
                    self.write(".gitattributes", b"# local override\ntext.txt -diff\n")
                else:
                    (self.root / ".gitattributes").unlink()
                    self.git("config", "core.attributesFile", str(attributes))
                context = self.workspace.review_context(base, head)
                self.assertEqual(context.base.lines, {"text.txt": 2})
                self.assertEqual(context.head.lines, {".gitattributes": 1})

    def test_dry_run_default_matches_the_review_diff_and_can_be_overridden(self):
        for path, content in {"README.md": b"unchanged\n", "items.txt": b"first\nold\nlast\n",
                              "deleted.txt": b"obsolete\n"}.items():
            self.write(path, content)
        base = self.commit()
        fixture = Path(__file__).with_name("fixtures") / "review.diff"
        self.git("apply", str(fixture))
        head = self.commit()
        self.git("push", "origin", "main")
        actual = self.workspace.review_context(base, head)
        scripted = DryRun().review_context
        self.assertIs(scripted, SCRIPTED_REVIEW_CONTEXT)
        self.assertEqual(replace(scripted.base, sha=base), actual.base)
        self.assertEqual(replace(scripted.head, sha=head), actual.head)
        self.assertEqual(DryRun(review_context=actual).review_context, actual)

from dataclasses import replace
from pathlib import Path
import unittest

from foreman.cli import save_config_for_run
from foreman.pointer import PointerError, load_pointer
from foreman.resolve import Cmd
from foreman.save import (
    UPDATE,
    SaveError,
    SaveRequest,
    added,
    config_from_pointer,
    leaks,
    missing,
    publish_reviewed,
    rounds_since_regroup,
    run_save,
)

ROOT = Path(__file__).parents[1]
SECRET = "zz-secret-marker"


def pointer(name):
    return config_from_pointer(load_pointer(ROOT / "examples" / name))


class ProofParityTest(unittest.TestCase):
    def test_allowlist_proof_and_leak_count_never_echoes_the_input(self):
        config = pointer("life-ai-engine.yaml")
        self.assertEqual(missing(["apps/a.ts"], "Board: #96", config)[0][:4], "Add ")
        self.assertEqual(missing(["docs/a.md"], "Board: #96", config), [])
        self.assertIn("Not engine", missing(["wiki/a.md"], "Board: #96", config)[0])
        self.assertEqual(missing(["package.json"], "Board: #96\nProof: synthetic proof", config), [])
        self.assertEqual(added("--- a\n+++ b\n-private-marker\n+safe"), "safe")
        self.assertEqual(added("--- a/x\n+++ b/x\n@@ -0,0 +1 @@\n+++ secret-marker"), "++ secret-marker")
        self.assertEqual(leaks(["SAFE\nzz-secret-marker"], SECRET), 1)
        for terms in ("", "# comment", "["):
            with self.subTest(terms=terms):
                with self.assertRaises(SaveError) as caught:
                    leaks(["the scanned text"], terms)
                self.assertNotIn("the scanned text", str(caught.exception))

    def test_rounds_since_regroup(self):
        blocked = "Review blocked (round 1/3, codex) at abc"
        self.assertEqual(rounds_since_regroup([]), 0)
        self.assertEqual(rounds_since_regroup([blocked, "chatter", blocked, blocked]), 3)
        self.assertEqual(rounds_since_regroup([blocked, blocked, blocked, "Regroup: structured output", blocked]), 1)
        self.assertEqual(rounds_since_regroup(["quoting Review blocked (round 1/3)", " Regroup: no"]), 0)


class SaveCeremonyTest(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(__import__("tempfile").TemporaryDirectory())
        self.root = Path(self.directory)
        self.calls = []

    def write(self, rel, text="hello\n"):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return rel

    def run(self, branch="feature", diff="", merge_base=0, merge=0, issue_state="OPEN"):
        def respond(args, env):
            self.calls.append((list(args), env))
            if args[:3] == ["gh", "token", "generate"]:
                return Cmd(0, "app-token\n")
            if args[:2] == ["git", "branch"]:
                return Cmd(0, branch + "\n")
            if args[:2] == ["git", "fetch"]:
                return Cmd(0, "")
            if args[:2] == ["git", "show-ref"]:
                return Cmd(1, "")
            if args[:4] == ["git", "rev-parse", "-q", "--verify"]:
                return Cmd(1, "")
            if args[:3] == ["git", "merge-base"]:
                return Cmd(merge_base, "")
            if args[:3] == ["git", "merge", "--abort"]:
                return Cmd(0, "")
            if args[:2] == ["git", "merge"]:
                return Cmd(merge, "")
            if args[:2] == ["git", "rev-parse"]:
                return Cmd(0, str(self.root) + "\n")
            if args[:2] == ["git", "diff"] and args[2] == "--cached" and "--name-only" in args:
                name = "docs/note.md" if (self.root / "docs/note.md").exists() else "notes.md"
                return Cmd(0, name + "\0")
            if args[:2] == ["git", "diff"]:
                return Cmd(0, diff)
            if args[:2] == ["git", "log"]:
                return Cmd(0, "")
            if args[0] == "git" and "-C" in args and "add" in args:
                return Cmd(0, "")
            if args[:3] == ["gh", "issue", "view"]:
                return Cmd(0, '{"state":"%s"}' % issue_state)
            if args[:3] == ["gh", "issue", "comment"]:
                return Cmd(0, "")
            if args[0] == "npm":
                return Cmd(0, "")
            if args[:2] == ["git", "commit"]:
                return Cmd(0, "")
            if args[:2] == ["git", "push"]:
                return Cmd(0, "")
            if args[:3] == ["gh", "pr", "list"]:
                return Cmd(0, "[]")
            if args[:3] == ["gh", "pr", "create"]:
                return Cmd(0, "https://example.test/pull/4\n")
            if args[:3] == ["gh", "pr", "merge"]:
                return Cmd(0, "")
            return Cmd(1, "", "unexpected %s" % args)

        return respond

    def test_never_main_and_autostash_guidance(self):
        config = pointer("life-ai-engine.yaml")
        request = SaveRequest(work="LAB.1.3", message="Note", issue="2", paths=("docs/note.md",))
        with self.assertRaises(SaveError) as caught:
            run_save(config, request, self.run(branch="main"), cwd=self.root, environ={})
        self.assertIn("never from main", str(caught.exception))
        self.assertFalse(any(args[:2] == ["git", "commit"] for args, _env in self.calls))

        self.calls.clear()
        self.write("docs/note.md")
        with self.assertRaises(SaveError) as caught:
            run_save(config, request, self.run(merge_base=1, merge=1), cwd=self.root, environ={})
        self.assertIn(UPDATE.format(ref="origin/main"), str(caught.exception))
        self.assertTrue(any(args[:3] == ["git", "merge", "--abort"] for args, _env in self.calls))

    def test_app_token_authors_the_pull_request_and_a_leak_blocks_without_echoing(self):
        config = pointer("life-ai-engine.yaml")
        self.write("docs/note.md")
        terms = self.root / "terms"
        terms.write_text(SECRET + "\n")
        config = replace(config, privacy=str(terms))
        request = SaveRequest(work="LAB.1.3", message="Note", issue="2", paths=("docs/note.md",))
        text = run_save(config, request, self.run(), cwd=self.root, environ={})
        self.assertIn("https://example.test/pull/4", text)
        token = self.calls[0]
        self.assertEqual(token[0][:3], ["gh", "token", "generate"])
        self.assertIn("5673796", token[0])
        created = next(args for args, _env in self.calls if args[:3] == ["gh", "pr", "create"])
        overlay = next(env for args, env in self.calls if args[:3] == ["gh", "pr", "create"])
        self.assertEqual(overlay, {"GH_TOKEN": "app-token"})
        self.assertIn("bookout-labs/life-ai-engine", created)
        self.assertTrue(any(args[:3] == ["gh", "pr", "merge"] and args[-1] == "--auto" for args, _env in self.calls))
        self.assertFalse(any(args[:5] == ["gh", "pr", "merge", "--auto", "--match-head-commit"] for args, _env in self.calls))

        self.calls.clear()
        diff = "--- a\n+++ b\n+" + SECRET + "\n"
        with self.assertRaises(SaveError) as caught:
            run_save(config, request, self.run(diff=diff), cwd=self.root, environ={})
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertIn("personal-term list", str(caught.exception))
        self.assertFalse(any(args[:2] == ["git", "push"] for args, _env in self.calls))

    def test_closed_issue_is_refused(self):
        config = pointer("life-ai-engine.yaml")
        self.write("docs/note.md")
        terms = self.root / "terms"
        terms.write_text("nothing-personal\n")
        config = replace(config, privacy=str(terms))
        request = SaveRequest(work="LAB.1.3", message="Note", issue="2", paths=("docs/note.md",))
        with self.assertRaises(SaveError) as closed:
            run_save(config, request, self.run(issue_state="CLOSED"), cwd=self.root, environ={})
        self.assertIn("CLOSED", str(closed.exception))

    def test_regroup_waits_for_three_blocked_rounds(self):
        terms = self.root / "terms"
        terms.write_text("nothing-personal\n")
        config = replace(pointer("life-ai-engine.yaml"), privacy=str(terms))
        blocked = "Review blocked (round 1/3)"
        early = SaveRequest(regroup="narrow the slice", issue="2")
        with self.assertRaises(SaveError):
            run_save(config, early, self.run(), cwd=self.root, comments=[blocked, blocked], environ={})
        self.assertFalse(any(args[:3] == ["gh", "issue", "comment"] for args, _env in self.calls))
        text = run_save(
            config, early, self.run(), cwd=self.root, comments=[blocked, blocked, blocked], environ={},
        )
        self.assertIn("Regrouped #2", text)
        self.calls.clear()
        text = run_save(config, early, self.run(), cwd=self.root, comments=None, environ={})
        self.assertIn("Regrouped #2", text)

    def test_doctorcre_saves_with_config_only(self):
        config = pointer("doctorcre-engine.yaml")
        self.assertIsNone(config.app)
        self.assertIsNone(config.privacy)
        self.assertIsNone(config.paths)
        self.assertEqual(config.repo, "DoctorCRE/doctorcre-engine")
        self.write("notes.md")
        text = run_save(
            config, SaveRequest(message="Note", paths=("notes.md",)), self.run(), cwd=self.root, environ={},
        )
        self.assertIn("https://example.test/pull/4", text)
        flat = " ".join(" ".join(args) for args, _env in self.calls)
        self.assertNotIn("token", flat)
        self.assertNotIn("5673796", flat)
        self.assertNotIn("life-ai", flat)
        self.assertIn("DoctorCRE/doctorcre-engine", flat)


class PublishTest(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(__import__("tempfile").TemporaryDirectory())
        self.root = Path(self.directory)
        self.calls = []

    def respond(
        self,
        branch="feature",
        porcelain="",
        names="apps/a.ts\0",
        sha="b" * 40,
        ancestor=0,
        diff="",
        issue_state="OPEN",
        push=0,
    ):
        def run(args, env):
            self.calls.append((list(args), env))
            if args[:3] == ["gh", "token", "generate"]:
                return Cmd(0, "app-token\n")
            if args[:2] == ["git", "branch"]:
                return Cmd(0, branch + "\n")
            if args[:2] == ["git", "fetch"]:
                return Cmd(0, "")
            if args[:2] == ["git", "show-ref"]:
                return Cmd(1, "")
            if args[:4] == ["git", "rev-parse", "-q", "--verify"]:
                return Cmd(1, "")
            if args[:2] == ["git", "merge-base"]:
                return Cmd(ancestor, "")
            if args[:2] == ["git", "status"]:
                return Cmd(0, porcelain)
            if args[:2] == ["git", "rev-parse"]:
                return Cmd(0, sha + "\n")
            if args[:2] == ["git", "diff"] and "--name-only" in args:
                return Cmd(0, names)
            if args[:2] == ["git", "diff"]:
                return Cmd(0, diff)
            if args[:2] == ["git", "log"]:
                return Cmd(0, "")
            if args[:3] == ["gh", "issue", "view"]:
                return Cmd(0, '{"state":"%s"}' % issue_state)
            if args[:2] == ["git", "push"]:
                return Cmd(push, "")
            if args[:3] == ["gh", "pr", "list"]:
                return Cmd(0, "[]")
            if args[:3] == ["gh", "pr", "create"]:
                return Cmd(0, "https://example.test/pull/9\n")
            return Cmd(1, "", "unexpected %s" % args)

        return run

    def life(self):
        terms = self.root / "terms"
        terms.write_text("nothing-personal\n")
        return replace(pointer("life-ai-engine.yaml"), privacy=str(terms))

    def test_doctorcre_publishes_without_committing_or_queueing(self):
        config = pointer("doctorcre-engine.yaml")
        self.assertTrue(config.queue)
        result = publish_reviewed(
            config,
            SaveRequest(message="Name the release"),
            self.respond(names="src/field.ts\0"),
            environ={},
            expected_sha="b" * 40,
        )
        self.assertEqual(result.sha, "b" * 40)
        self.assertIn("https://example.test/pull/9", result.report)
        self.assertIn("is open", result.report)
        flat = [args for args, _env in self.calls]
        self.assertTrue(any(args[:2] == ["git", "push"] for args in flat))
        self.assertFalse(any(args[:2] == ["git", "commit"] for args in flat))
        self.assertFalse(any(args[:2] == ["git", "merge"] for args in flat))
        self.assertFalse(any(args[:3] == ["gh", "pr", "merge"] for args in flat))
        self.assertFalse(any(args[0] == "npm" for args in flat))
        self.assertFalse(any("token" in arg for args in flat for arg in args))
        self.assertTrue(config.queue)

    def test_stale_dirty_empty_and_leak_stop_before_push(self):
        config = self.life()
        request = SaveRequest(work="LAB.1.3", message="Note", issue="2", proof="Shown on the board.")
        with self.assertRaises(SaveError) as stale:
            publish_reviewed(config, request, self.respond(ancestor=1), environ={})
        self.assertIn(UPDATE.format(ref="origin/main"), str(stale.exception))
        self.assertFalse(any(args[:2] == ["git", "merge"] for args, _env in self.calls))
        self.assertFalse(any(args[:2] == ["git", "push"] for args, _env in self.calls))

        self.calls.clear()
        with self.assertRaises(SaveError) as dirty:
            publish_reviewed(
                config, request, self.respond(porcelain=" M " + SECRET + "\n"), environ={},
            )
        self.assertNotIn(SECRET, str(dirty.exception))
        self.assertIn("uncommitted", str(dirty.exception))
        self.assertFalse(any(args[:2] == ["git", "push"] for args, _env in self.calls))

        self.calls.clear()
        with self.assertRaises(SaveError) as empty:
            publish_reviewed(config, request, self.respond(names=""), environ={})
        self.assertIn("Nothing to publish", str(empty.exception))
        self.assertFalse(any(args[:2] == ["git", "push"] for args, _env in self.calls))

        self.calls.clear()
        terms = self.root / "terms"
        terms.write_text(SECRET + "\n")
        leaked = replace(config, privacy=str(terms))
        diff = "--- a\n+++ b\n+" + SECRET + "\n"
        with self.assertRaises(SaveError) as caught:
            publish_reviewed(leaked, request, self.respond(diff=diff), environ={})
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertIn("personal-term list", str(caught.exception))
        self.assertFalse(any(args[:2] == ["git", "push"] for args, _env in self.calls))

    def test_life_ai_proof_and_work_id_fail_before_a_token_or_push(self):
        config = self.life()
        with self.assertRaises(SaveError) as missing_work:
            publish_reviewed(config, SaveRequest(message="Note"), self.respond(), environ={})
        self.assertIn("work ID", str(missing_work.exception))
        self.assertEqual(self.calls, [])

        self.calls.clear()
        with self.assertRaises(SaveError) as missing_proof:
            publish_reviewed(
                config,
                SaveRequest(work="LAB.1.3", message="Note", issue="2"),
                self.respond(),
                environ={},
            )
        self.assertIn("Proof:", str(missing_proof.exception))
        self.assertFalse(any(args[:2] == ["git", "push"] for args, _env in self.calls))
        self.assertFalse(any(args[:3] == ["gh", "token", "generate"] for args, _env in self.calls))

    def test_life_ai_publishes_with_the_app_and_leaves_the_queue_alone(self):
        config = self.life()
        result = publish_reviewed(
            config,
            SaveRequest(work="LAB.1.3", message="Note", issue="2", proof="Shown on the board."),
            self.respond(),
            environ={},
        )
        self.assertEqual(result.sha, "b" * 40)
        self.assertIn("is open", result.report)
        created = next(args for args, _env in self.calls if args[:3] == ["gh", "pr", "create"])
        overlay = next(env for args, env in self.calls if args[:3] == ["gh", "pr", "create"])
        self.assertEqual(overlay, {"GH_TOKEN": "app-token"})
        self.assertIn("Work: LAB.1.3", created[created.index("--body") + 1])
        self.assertIn("Proof: Shown on the board.", created[created.index("--body") + 1])
        self.assertIn("Board: #2", created[created.index("--body") + 1])
        self.assertFalse(any(args[:3] == ["gh", "pr", "merge"] for args, _env in self.calls))
        self.assertTrue(config.queue)

    def test_a_closed_board_issue_and_the_main_branch_are_refused(self):
        config = self.life()
        request = SaveRequest(work="LAB.1.3", message="Note", issue="2", proof="Shown on the board.")
        with self.assertRaises(SaveError) as closed:
            publish_reviewed(config, request, self.respond(issue_state="CLOSED"), environ={})
        self.assertIn("CLOSED", str(closed.exception))
        self.assertFalse(any(args[:2] == ["git", "push"] for args, _env in self.calls))

        self.calls.clear()
        with self.assertRaises(SaveError) as main:
            publish_reviewed(config, request, self.respond(branch="main"), environ={})
        self.assertIn("never from main", str(main.exception))

    def test_live_run_keeps_queue_for_the_merge_node(self):
        self.assertIsNone(save_config_for_run(self.root))
        (self.root / "tools.yaml").write_text((ROOT / "examples" / "doctorcre-app.yaml").read_text())
        self.assertIsNone(save_config_for_run(self.root))
        (self.root / "tools.yaml").write_text((ROOT / "examples" / "doctorcre-engine.yaml").read_text())
        loaded = save_config_for_run(self.root)
        self.assertFalse(loaded.queue)
        self.assertEqual(loaded.repo, "DoctorCRE/doctorcre-engine")
        self.assertIsNone(loaded.app)
        (self.root / "tools.yaml").write_text("source: no\n")
        with self.assertRaises(PointerError):
            save_config_for_run(self.root)


if __name__ == "__main__":
    unittest.main()

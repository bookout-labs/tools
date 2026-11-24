"""#236 recovery: a crash around each research and child-filing write replays without a paid call."""

import tests

import unittest

from foreman.stage_output import spec_digest
import test_runner
from test_runner import BUILDER, CONSUMER, FakeGitHub

REPO = "Example/Engine"
PLAN = [{"triage": "plan"}, {"spec": "Plan", "spec_gate": "ready", "slices": [CONSUMER, BUILDER]}]
QUESTION = [{"triage": "question"}, {"research": "Only open items."}]


class Crash(Exception):
    """A process death between two GitHub calls. It is not a ForgeError, so no step absorbs it."""


class StageRecoveryTest(unittest.TestCase):
    # The live runner's harness, imported by module so discovery doesn't collect its tests here too.
    setUpClass = classmethod(test_runner.LiveRunnerTest.setUpClass.__func__)
    setUp = test_runner.LiveRunnerTest.setUp
    walk = test_runner.LiveRunnerTest.walk

    def crash(self, replies, operation, *, after_write):
        self.checkpoint = self.cwd / f"{operation}-{after_write}.sqlite3"
        self.github.fail_next(operation, after_write=after_write, error=Crash(operation))
        with self.assertRaises(Crash):
            self.walk(replies)
        return len(self.calls)

    def assert_children_filed(self):
        self.assertEqual([title for title, _, _ in self.github.created], [BUILDER["title"], CONSUMER["title"]])
        self.assertEqual(self.github.parents, {(REPO, 101): 8, (REPO, 102): 8})
        self.assertEqual(self.github.dependencies, {102: [101]})
        # Every child carries Ready, and admission holds the blocked one until its prerequisite merges.
        self.assertEqual(self.github.release_labels(), [(REPO, 101, ["Ready"], []), (REPO, 102, ["Ready"], [])])

    def test_crash_around_each_child_write_replays_twice_without_a_paid_call(self):
        for operation in ("create_issue", "add_sub_issue", "add_blocker"):
            for after_write in (False, True):
                with self.subTest(operation=operation, after_write=after_write):
                    self.github = FakeGitHub(["Ready"])
                    paid = self.crash(PLAN, operation, after_write=after_write)
                    for _ in range(2):
                        self.assertEqual(self.walk([])["stop"], "children_filed")
                    self.assertEqual(len(self.calls), paid)
                    self.assert_children_filed()

    def test_a_child_hidden_from_the_lagging_issue_list_is_adopted_once(self):
        # Found by engine #1048: GitHub listed no issue for the marker right after a successful create.
        github = self.github = FakeGitHub(["Ready"])
        create, listed = github.create_issue, github.marked_issues
        fresh: set[int] = set()

        def create_issue(*args, **kwargs):
            number = create(*args, **kwargs)
            fresh.add(number)
            return number

        def marked_issues(repo, marker):
            seen = [number for number in listed(repo, marker) if number not in fresh]
            fresh.clear()  # The list catches up by the next read.
            return seen

        github.create_issue, github.marked_issues = create_issue, marked_issues
        self.assertEqual(self.walk(PLAN)["stop"], "children_filed")
        self.assert_children_filed()
        self.assertEqual(self.walk([])["stop"], "children_filed")
        self.assertEqual(len(github.created), 2)

    def test_crash_around_the_answer_comment_replays_twice_with_one_comment(self):
        for after_write in (False, True):
            with self.subTest(after_write=after_write):
                self.github = FakeGitHub(["Ready"])
                paid = self.crash(QUESTION, "comment_issue", after_write=after_write)
                for _ in range(2):
                    self.assertEqual(self.walk([])["stop"], "answered")
                self.assertEqual(len(self.calls), paid)
                comments = self.github.issue_comments(REPO, 8)
                self.assertEqual(len(comments), 1)
                self.assertTrue(comments[0].startswith("Only open items.\n\n<!-- foreman answer "))

    def test_unconfirmed_child_evidence_halts_without_release(self):
        marker = f"<!-- foreman slice {spec_digest([REPO.lower(), 8, BUILDER['title']])} -->"
        cases = {
            "duplicate": (lambda: self.github.create_issue(REPO, BUILDER["title"], "", ["slice"], marker=marker),
                          f"2 issues carry the marker for {BUILDER['title']}."),
            "body": (lambda: self.github.bodies.update({(REPO, 101): f"{marker}\nEdited by hand."}),
                     f"Issue #101 carries the marker for {BUILDER['title']} with a different repository or body."),
            "parent": (lambda: self.github.parents.update({(REPO, 101): 9}), "GitHub does not show #101 under parent #8."),
        }
        for name, (tamper, detail) in cases.items():
            with self.subTest(name):
                self.github = FakeGitHub(["Ready"])
                paid = self.crash(PLAN, "add_sub_issue", after_write=name == "parent")
                tamper()
                created = len(self.github.created)
                for _ in range(2):
                    result = self.walk([])
                    self.assertEqual((result["stop"], result["halt_detail"]), ("publication_unreconciled", detail))
                self.assertEqual(len(self.calls), paid)
                self.assertEqual(len(self.github.created), created)
                self.assertEqual(self.github.dependencies, {})
                self.assertEqual(self.github.release_labels(), [])


if __name__ == "__main__":
    unittest.main()

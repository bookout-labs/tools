"""implement.mutate: plain code flips a capped sample of the changed lines, and a mutation no test notices goes back to edit (IM6, IM12)."""

import tests  # noqa: F401

from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

from foreman.adapters import Outcome
from foreman.check_output import CheckOutput
from foreman.config import load_config
from foreman.container import ContainerUnavailable
from foreman.contract import CONTRACT_PATH
from foreman.delivery import LiveRun
from foreman.dry_run import SCRIPTED_USAGE, ScriptHandler
from foreman.github import LiveForge
from foreman.proof import standing, survived
from foreman.runner import run_live_delivery
from foreman.state import ChecksState, RunState
from foreman.stage_output import ReviewState
from foreman.workspace import LiveWorkspace, WorkState
from container_double import HostContainers
from publication_fixture import PublicationFixture, contract_text

# The target's pinned mutation tool: one mutation per sampled line, with an id kept across runs. Each survives until the
# repository has kills.txt, the test that notices them.
MUTATE = """while IFS= read -r line; do
  id="flip-$(printf %s "$line" | tr -c 'A-Za-z0-9' '-')"
  if test -f kills.txt; then result=killed; else result=survived; fi
  echo "mutating $line"
  printf '{"line": "%s", "id": "%s", "result": "%s"}\\n' "$line" "$id" "$result"
done
"""
FLIP = "flip-items-txt-1"
FINDING = {"id": "equivalent", "kind": "correctness", "severity": "P1", "ruling_id": None,
           "location": {"side": "head", "path": "items.txt", "line": 1},
           "summary": f"The equivalent claim for {FLIP} is wrong.",
           "impact": "The view can list an item twice.", "evidence": "The flipped line changes which items are listed."}


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@example.test", *args], cwd=root, check=True,
                          capture_output=True, text=True).stdout.strip()


class SampleTest(unittest.TestCase):
    """The sample is up to sample_cap changed lines, fed to the command on standard input, and its JSON lines are read back."""

    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory())).resolve()
        (self.root / CONTRACT_PATH).parent.mkdir()
        git(self.root, "init", "-qb", "main")
        (self.root / "a.txt").write_text("1\n2\n3\n4\n")
        git(self.root, "add", "--all")
        git(self.root, "commit", "-qm", "Base")
        self.base = git(self.root, "rev-parse", "HEAD")
        self.containers = HostContainers()

    def run_sample(self, command: str, cap: int, claims: dict[str, str] | None = None, findings=()):
        (self.root / CONTRACT_PATH).write_bytes(contract_text(mutation=command, sample_cap=cap))
        git(self.root, "add", "--all")
        git(self.root, "commit", "-qm", "Contract")
        base = git(self.root, "rev-parse", "HEAD")
        (self.root / "a.txt").write_text("one\ntwo\nthree\nfour\n")
        workspace = LiveWorkspace(self.root, containers=self.containers)
        self.addCleanup(workspace.remove_container)
        checks = ChecksState(mutation=command, sample_cap=cap, checked_tree=workspace.tree(None), equivalent_mutations=claims or {})
        state = {"work": WorkState(), "run": RunState(base_sha=base), "checks": checks,
                 "review": ReviewState(findings=list(findings))}
        return survived(workspace, state, output=CheckOutput())  # type: ignore[arg-type]

    def test_the_command_reads_up_to_sample_cap_changed_lines_spread_over_them(self):
        (self.root / "seen").mkdir()
        checks = self.run_sample("cat > seen/sample", 2)
        self.assertEqual((self.root / "seen/sample").read_text(), "a.txt:1\na.txt:3\n")
        self.assertEqual(checks.verify, "")

    def test_each_survived_line_is_evidence_and_a_standing_equivalent_claim_is_left_out(self):
        checks = self.run_sample(MUTATE, 20, claims={"flip-a-txt-2": "Same output."})
        self.assertEqual((checks.verify, checks.verify_failures), ("survived", 1))
        self.assertIn("- a.txt:1 (id flip-a-txt-1)", checks.failure_output)
        self.assertNotIn("flip-a-txt-2", checks.failure_output)
        self.assertIn("equivalent_mutations", checks.failure_output)

    def test_a_command_that_does_not_start_is_env_unavailable(self):
        with self.assertRaisesRegex(ContainerUnavailable, "exited with 127"):
            self.run_sample("no-such-mutation-tool", 20)

    def test_a_blocking_finding_that_names_the_id_rejects_the_claim(self):
        claims = {FLIP: "Same output.", "flip-items-txt-10": "Same output."}
        self.assertEqual(standing(claims, [FINDING]), {"flip-items-txt-10": "Same output."})  # type: ignore[list-item]
        self.assertEqual(standing(claims, []), claims)


class MutationTest(PublicationFixture):
    def setUp(self):
        super().setUp()
        (self.checkout / "mutate.sh").write_text(MUTATE)
        (self.checkout / CONTRACT_PATH).write_bytes(contract_text(self.check, mutation="sh mutate.sh"))
        self.git("add", "--", "mutate.sh", CONTRACT_PATH)
        self.git("commit", "-qm", "Mutation tool")
        self.git("push", "-q", "origin", self.base)
        self.calls: list[tuple[str, ChecksState]] = []

    def deliver(self, builds, reviews=None):
        """Run the issue. Each builder call takes the next (files it writes, equivalent claims) from builds."""
        script, workspace, answers = ScriptHandler(reviews=reviews), LiveWorkspace(self.checkout, containers=self.containers), iter(builds)

        def handler(node, model, state, instruction):
            self.calls.append((node, ChecksState.model_validate(state["checks"])))
            if node == "implement":
                files, claims = next(answers)
                for path in files:
                    (self.checkout / path).write_text("one\n")
                return Outcome("ran", {"implementation": "Built.", "no_change": None, "human_question": None,
                                       "equivalent_mutations": [{"id": key, "reason": "Same output."} for key in claims]},
                               usage=SCRIPTED_USAGE)
            if node == "review" and reviews and reviews[0] == "reject":
                reviews.pop(0)
                return Outcome("ran", {"findings": [FINDING], "human_question": None}, usage=SCRIPTED_USAGE)
            return script(node, model, state, instruction)

        return run_live_delivery(repo="Example/Engine", issue=8, checkpoint=self.checkpoint,
                                 run=LiveRun(LiveForge(), workspace), dispatcher=handler)

    def builders(self):
        return [checks for node, checks in self.calls if node == "implement"]

    def test_IM6_a_surviving_mutation_returns_to_edit_and_commit_waits_until_every_sampled_mutation_fails_a_test(self):
        result = self.deliver([(["items.txt"], []), (["kills.txt"], [])])
        self.assertEqual(result["stop"], "merged", result.get("halt_detail"))
        first, second = self.builders()
        self.assertEqual(first.failure_output, "")
        self.assertEqual(second.verify, "survived")
        self.assertIn(f"- items.txt:1 (id {FLIP})", second.failure_output)
        # The surviving round counted toward the failed-check cap, and only the tree whose mutations all fail a test published.
        self.assertEqual(result["checks"].verify_failures, 1)
        self.assertEqual(len(self.created), 1)
        self.assertEqual(self.git("--git-dir", str(self.remote), "show", f"{result['work'].head_sha}:kills.txt"), "one")

    def test_surviving_rounds_stop_at_the_failed_check_cap_and_nothing_publishes(self):
        cap = load_config().max_review_rounds
        result = self.deliver([(["items.txt"], [])] * cap)
        self.assertEqual(result["stop"], "failed_check_cap", result.get("halt_detail"))
        self.assertIn(FLIP, result["halt_detail"])
        self.assertEqual(len(self.builders()), cap)
        self.assertEqual(self.created, [])

    def test_IM12_an_equivalent_claim_leaves_the_sample_goes_to_review_and_a_rejected_one_samples_again(self):
        # The repair addresses the finding with an edit, whose own line is sampled too.
        result = self.deliver([(["items.txt"], []), ([], [FLIP]), (["notes.txt"], []), (["kills.txt"], [])], reviews=["reject", "approve"])
        self.assertEqual(result["stop"], "merged", result.get("halt_detail"))
        reviews = [checks for node, checks in self.calls if node == "review"]
        # The claim left the next sample, so the claimed tree published, and review saw the claim with its reason.
        self.assertEqual(reviews[0].equivalent_mutations, {FLIP: "Same output."})
        # The review finding rejected the claim, so the repair's sample held the mutation again and it went back to the builder.
        third, fourth = self.builders()[2:]
        self.assertEqual(third.verify, "")
        self.assertEqual(fourth.verify, "survived")
        self.assertIn(FLIP, fourth.failure_output)
        self.assertEqual(reviews[1].equivalent_mutations, {})


if __name__ == "__main__":
    unittest.main()

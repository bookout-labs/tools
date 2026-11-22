"""Test review rounds."""
import tests
from foreman.state import ReviewState, PlanState, ChecksState, RunState, checkpoint_serializer, SCHEMA_VERSION
import json
import subprocess
from contextlib import contextmanager
from dataclasses import replace
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from foreman.worker import CliWorker, run_worker_command
from unittest.mock import patch
from foreman.workspace import LiveWorkspace
from foreman.review_round import GraphCheck, ReviewHistory, accept
from foreman.delivery import LiveRun, compile_delivery, read_delivery_state
from foreman.config import ConfigError, ModelSpec, load_config
from foreman.adapters import Outcome
from foreman.dry_run import SCRIPTED_USAGE
from foreman.stage_output import ReviewOutput, parse_output
from foreman.review_context import ReviewContext, SCRIPTED_REVIEW_CONTEXT
from skill_fixture import skill_fixture
from test_adapters import fixture_process
from test_runner import APPROVE, FINDING, BASE, HEAD, EMPTY_SLICE, FakeGitHub, FakeWorkspace


@contextmanager
def review_trees():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        def git(*args):
            return subprocess.check_output(
                ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", *args],
                cwd=root, text=True, stderr=subprocess.PIPE).strip()
        git("init", "-b", "main")
        (root / "items.txt").write_text("one\ntwo\n")
        (root / "shared.txt").write_text("base only line\n")
        (root / "deleted.txt").write_text("deleted from base\n")
        (root / "old.txt").write_text("rename source\n")
        git("add", ".")
        git("commit", "-m", "Base")
        base = git("rev-parse", "HEAD")
        git("switch", "-c", "review")
        (root / "docs").mkdir()
        for name in ("issue-36-verification.md", "plus-sign-validation.txt"):
            (root / "docs" / name).write_text("historical evidence\n" * 14)
        (root / "items.txt").write_text("one\nchanged\n")
        (root / "shared.txt").write_text("previous\nextra line\n")
        (root / "deleted.txt").unlink()
        git("mv", "old.txt", "new.txt")
        git("add", ".")
        git("commit", "-m", "First reviewed head")
        previous = git("rev-parse", "HEAD")
        for path in (root / "docs").iterdir():
            path.unlink()
        (root / "shared.txt").write_text("head line\n")
        git("add", ".")
        git("commit", "-m", "Remove historical evidence")
        head = git("rev-parse", "HEAD")
        git("switch", "main")
        (root / "main.txt").write_text("base tip advance\n")
        git("add", ".")
        git("commit", "-m", "Advance base tip")
        tip = git("rev-parse", "HEAD")
        git("switch", "review")
        git("remote", "add", "origin", str(root))
        yield LiveWorkspace(root), tip, base, previous, head


class ReviewRoundsTest(unittest.TestCase):
    def run_rounds(self, reviews, *, initial=None, crash=False, answer=False, bad_diff=False, divergent=False, gate_heads=None, advance_head=False, post_crash=False, review_crash=False, review_quota=False, bad_interdiff=False, rerun=False, trees=None, quota_after=0, review_crash_after=0):
        self.prompts = []
        self.diffs = []
        self.gate_reads = []
        self.forge = FakeGitHub()
        original_post = self.forge.post_review
        receipts = {}
        def post(*args, marker):
            if marker in receipts:
                return receipts[marker]
            receipts[marker] = original_post(*args, marker=marker)
            if post_crash:
                raise RuntimeError("process lost")
            return receipts[marker]
        self.forge.post_review = post
        with TemporaryDirectory() as directory, skill_fixture() as (settings, scratch):
            if review_quota:
                builder = settings.nodes["implement"]
                settings.nodes["implement"] = replace(builder, models=tuple(m for m in builder.models if m.family == "grok"))
            outer = self

            class Workspace(FakeWorkspace):
                def review_scope(self, base, head, previous):
                    return trees[0].review_scope(base, head, previous) if trees else super().review_scope(base, head, previous)

                def commit(self, message):
                    super().commit(message)
                    if trees:
                        self.current = type(self.current)(trees[3] if implementations == 1 else trees[4], True, self.current.branch)
                    elif answer or advance_head:
                        self.current = type(self.current)(str(implementations) * 40, True, self.current.branch)

                def review_context(self, base, head):
                    if trees:
                        return trees[0].review_context(base, head)
                    context = super().review_context(base, head)
                    return ReviewContext(context.base, replace(context.head, files=context.head.files | {"main.txt"}, lines={**context.head.lines, "main.txt": 1})) if divergent else context

                def files_at(self, head, prefix):
                    outer.gate_reads.append(head)
                    return {"tests/fixtures/manifest.toml", "tests/fixtures/result.txt"} if head in (gate_heads or ()) else set()

                def read_file_at(self, head, path):
                    return '[fixtures."result.txt"]\nkind = "captured_run"\ntool = "example"\nversion = "1"\nargv = ["example"]'

                def review_diff(self, base, head, *, interdiff=False):
                    outer.diffs.append((base, head, interdiff))
                    diff = (trees[0] if trees else super()).review_diff(base, head, interdiff=interdiff)
                    if divergent and interdiff:
                        return type(diff)(diff.command, "diff --git a/main.txt b/main.txt\n--- a/main.txt\n+++ b/main.txt\n@@ -0,0 +1 @@\n+mainline\n")
                    if bad_diff or (bad_interdiff and interdiff):
                        return type(diff)(diff.command, "--- a/x\n+++ b/x\n@@ -1 +1 @@\ninvalid\n")
                    return diff

            if trees:
                target = self.forge.review_target
                self.forge.review_target = lambda repo, issue, branch, head: target(repo, issue, branch, head).model_copy(update={"baseRefOid": trees[1]})
            replies = iter(reviews)
            crashed = False
            quota_returned = False
            workspace = Workspace(Path(directory))
            implementations = 0

            def dispatch(node, model, state, instruction):
                nonlocal crashed, implementations, quota_returned
                self.prompts.append((node, state, instruction))
                if node == "review":
                    if review_crash and not crashed and len([p for p in self.prompts if p[0] == "review"]) > review_crash_after:
                        crashed = True
                        raise RuntimeError("process lost")
                    if review_quota and not quota_returned and len([p for p in self.prompts if p[0] == "review"]) == quota_after + 1:
                        quota_returned = True
                        return Outcome("quota")
                    raw = next(replies)
                    return raw if isinstance(raw, Outcome) else Outcome("ran", raw, usage=SCRIPTED_USAGE)
                if crash and state["review"].review_findings and not crashed:
                    crashed = True
                    raise RuntimeError("process lost")
                if node == "triage":
                    return Outcome("ran", {"triage": "implement", "work_kind": "feature", "slice": None, "human_question": None}, usage=SCRIPTED_USAGE)
                implementations += 1
                workspace.current = type(workspace.current)(workspace.current.head, False, workspace.current.branch)
                return Outcome("ran", {"implementation": "Fixed.",
                                       "browser_proof": None, "formal_proof": None}, usage=SCRIPTED_USAGE)

            with SqliteSaver.from_conn_string(str(Path(directory) / "checkpoint")) as saver:
                saver.serde = checkpoint_serializer()
                graph = compile_delivery(dispatch, settings, scratch=scratch,
                                         run=LiveRun(self.forge, workspace), checkpointer=saver)
                config = {"configurable": {"thread_id": "example/repo#8"}}
                state = {"review": ReviewState(), "schema_version": SCHEMA_VERSION, "run": RunState(repo="Example/Repo", issue=8, token_cap=20_000_000, max_budget_usd=None), "plan": PlanState(issue_kind="slice", slice=EMPTY_SLICE),
                         "checks": ChecksState(check="exit 0"), "pstack_pin": settings.pstack.model_dump(), **(initial or {})}
                if crash or post_crash or review_crash:
                    with self.assertRaisesRegex(RuntimeError, "process lost"):
                        graph.invoke(state, config)
                    self.interrupted = read_delivery_state({"stop": "interrupted", **graph.get_state(config).values})
                    if review_crash:
                        self.assertEqual(self.forge.reviews, [])
                    (scratch / "resume").mkdir()
                    graph = compile_delivery(dispatch, settings, scratch=scratch / "resume",
                                             run=LiveRun(self.forge, workspace), checkpointer=saver)
                    result = graph.invoke(None, config)
                else:
                    result = graph.invoke(state, config)
                if rerun:
                    from foreman.runner import run_live_delivery
                    from unittest.mock import patch
                    with patch('foreman.runner.load_config', return_value=settings):
                        result = run_live_delivery(repo='Example/Repo', issue=8, check='exit 0',
                            checkpoint=Path(directory) / 'checkpoint', run=LiveRun(self.forge, workspace), dispatcher=dispatch)
                if answer:
                    self.assertEqual(result["halt"], "design_rethink")
                    self.assertEqual(len(read_delivery_state({"stop": "needs_human", **result})["review"].review_findings), 2)
                    result = graph.invoke(Command(resume="Redesign the ownership."), config)
                return read_delivery_state({"stop": "needs_human", **result})

    def test_three_pinned_trees_across_rounds_and_unavailable_dispatch(self):
        with review_trees() as trees:
            workspace, tip, base, previous, head = trees
            result = self.run_rounds([
                {**APPROVE, "findings": [FINDING]},
                {**APPROVE, "resolutions": [{"id": "f1", "status": "resolved"}],
                 "findings": [{**FINDING, "id": "f2", "severity": "nit", "summary": "Adjust wording.", "fix": "Adjust wording."}]},
                {**APPROVE, "resolutions": [{"id": "f2", "status": "resolved"}]},
            ], trees=trees, review_quota=True, quota_after=1)
            self.assertEqual(result["merge"].status, "armed")
            self.assertEqual(len(result["review"].review_findings), 3)
            prompts = [p[2] for p in self.prompts if p[0] == "review"]
            self.assertEqual(len(prompts), 4)
            self.assertNotEqual(tip, base)
            for index, (prompt, current) in enumerate(zip(prompts, (previous, head, head, head))):
                self.assertIn(f"# base = PR merge-base {base}", prompt)
                self.assertIn(f"# head = current verified head {current}", prompt)
                self.assertIn(f"{tip}...{current}", prompt)
                self.assertIn("Full PR diff: old = merge-base (base); new = head", prompt)
                for guidance in (
                    "Use base for deletions or rename sources in the full PR diff from the merge-base",
                    "never for deletions only in the interdiff from previous",
                    "A previous-only path is never risk evidence, even with line=null",
                    "Resolve its earlier finding by ID and ground dimensions in the remaining net PR changes",
                    "Previous is for navigation only and is not a citation side",
                ):
                    with self.subTest(round=index, guidance=guidance):
                        self.assertIn(guidance, prompt)
                self.assertNotIn("HEAD~1", prompt)
                self.assertNotIn("origin/main", prompt)
                if index:
                    prior = head if index == 3 else previous
                    self.assertIn(f"# previous = last accepted reviewed head {prior} (history only)", prompt)
                    self.assertIn("Interdiff: old = previous; new = head", prompt)
                    self.assertIn(f"{prior}..{current}", prompt)
                else:
                    self.assertNotIn("# previous =", prompt)
            self.assertEqual(prompts[1], prompts[2])
            self.assertEqual(workspace.review_diff(head, head, interdiff=True).patch, "")
            subprocess.run(["sh", "-c", workspace.review_scope(tip, head, previous).command],
                           cwd=workspace.root, check=True, stdout=subprocess.PIPE)

    def test_claude_grants_each_rendered_review_command(self):
        with review_trees() as trees:
            workspace, tip, _, previous, head = trees
            result = self.run_rounds([
                {**APPROVE, "findings": [FINDING]},
                {**APPROVE, "resolutions": [{"id": "f1", "status": "resolved"}]},
            ], trees=trees)
            self.assertEqual(result["merge"].status, "armed")
            reviews = [p for p in self.prompts if p[0] == "review"]
            self.assertEqual(len(reviews), 2)
            for index, (node, state, instruction) in enumerate(reviews):
                scope = state["dispatch_plan"]["scope"]["command"]
                self.assertIn(scope, instruction)
                commands = [line for line in scope.splitlines() if line.startswith("git ")]
                diffs = [c for c in commands if " diff " in c]
                self.assertEqual(len(diffs), index + 1)
                current = head if index else previous
                self.assertIn(f"{tip}...{current} --", diffs[0])
                if index:
                    self.assertIn(f"{previous}..{head} --", diffs[1])
                outside = ("git status", "git diff --output=escape", "exit 0", "head", "history", "base")
                denied_commands = ()

                for command in commands:
                    subprocess.run(["sh", "-c", command], cwd=workspace.root, check=True, stdout=subprocess.PIPE)

                def runner(args, cwd, env):
                    permissions = json.loads(Path(args[args.index("--settings") + 1]).read_text())["permissions"]
                    self.assertEqual(permissions["allow"], [f"Bash({c})" for c in commands])
                    for command in (*outside, commands[0] + " --stat"):
                        self.assertNotIn(f"Bash({command})", permissions["allow"])
                    self.assertEqual(args[args.index("--permission-mode") + 1], "dontAsk")
                    process = fixture_process(args, "success", 0)
                    envelope = json.loads(process.stdout)
                    envelope["permission_denials"] = [
                        {"tool_name": "Bash", "tool_input": {"command": c}} for c in denied_commands
                    ]
                    return subprocess.CompletedProcess(args, 0, json.dumps(envelope), "")

                cases = [((), "ran"), (outside, "review_denied")]
                for command in commands:
                    cases.extend(((denied,), "review_denied") for denied in
                                 (command, f"bash -lc '{command}'", f"cd . && {command}", command + " --stat"))
                for denied_commands, expected in cases:
                    with self.subTest(round=index + 1, denials=denied_commands):
                        outcome = CliWorker(workspace.root, runner)(
                            node, ModelSpec.parse("claude-opus-5-5-high"), state, instruction)
                        self.assertEqual(outcome.kind, expected, outcome.detail)

    def test_worker_refuses_scratch_root_inside_checkout(self):
        with TemporaryDirectory() as directory:
            checkout = Path(directory) / "checkout"
            checkout.mkdir()
            nested = checkout / "scratch"
            nested.mkdir()
            for scratch_root in (checkout, nested):
                with self.subTest(scratch_root=scratch_root):
                    worker = CliWorker(checkout, lambda *_: self.fail("CLI must not run"),
                                       scratch_root=scratch_root)
                    with self.assertRaisesRegex(ConfigError, "scratch root must be outside the checkout"):
                        worker("review", ModelSpec.parse("claude-opus-5-5-high"), {}, "Review.")

    def test_review_payload_stays_off_exec_argv(self):
        with review_trees() as (workspace, tip, _, previous, head), TemporaryDirectory() as scratch:
            tools = LiveWorkspace(Path.cwd())
            with patch.object(tools, "_fetch_review"):
                scopes = [tools.review_scope(
                    subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
                    subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(), None)]
            for index in range(2000):
                (workspace.root / f"blob-{index}.txt").write_text(str(index))
            subprocess.run(["git", "add", "."], cwd=workspace.root, check=True)
            subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                            "commit", "-qm", "2000 blobs"], cwd=workspace.root, check=True)
            head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=workspace.root, text=True).strip()
            scopes.append(workspace.review_scope(tip, head, previous))
            for scope in scopes:
                instruction = "Review with standalone commands.\n" + scope.command
                plan = {"models": [], "instruction": instruction, "methods": [], "read_roots": [],
                        "target": None, "scope": {"command": scope.command, "inline": []}}
                def runner(args, cwd, env):
                    settings = Path(args[args.index("--settings") + 1])
                    self.assertTrue(settings.resolve().is_relative_to(Path(scratch).resolve()))
                    self.assertFalse(Path(env["TMPDIR"]).resolve().is_relative_to(workspace.root.resolve()))
                    with patch("foreman.worker.subprocess.run", return_value=fixture_process(args, "success", 0)) as execute:
                        result = run_worker_command(args, cwd, env)
                    prompt = execute.call_args.kwargs["input"]
                    self.assertEqual(prompt.count(instruction), 1)
                    projected_plan = json.loads(prompt.split("Delivery state:\n")[1])["dispatch_plan"]
                    self.assertFalse({"instruction", "scope"} & projected_plan.keys())
                    self.assertLess(sum(len(arg.encode()) + 1 for arg in execute.call_args.args[0]), 32768)
                    return result
                CliWorker(workspace.root, runner, scratch_root=Path(scratch))(
                    "review", ModelSpec.parse("claude-opus-5-5-high"), {"dispatch_plan": plan}, instruction)

    def test_denied_review_retry_resume_quota_and_budget(self):
        denial = Outcome("review_denied", APPROVE, usage=SCRIPTED_USAGE.model_copy(update={"total_cost_usd": 0.1}),
                         detail="Bash denied: git commit -m x")
        for mode in ("recover", "repeat", "resume", "quota", "quota-repeat", "budget", "failed"):
            with self.subTest(mode=mode):
                repeated = mode in ("repeat", "quota-repeat", "resume")
                replies = [denial, denial if repeated else APPROVE]
                if mode == "failed":
                    replies = [Outcome("failed", detail="bad output", usage=SCRIPTED_USAGE)]
                initial = {"review": ReviewState(settled_rulings=[{"id": "scope", "text": "Pinned."}])}
                if mode == "budget":
                    initial["run"] = RunState(repo="Example/Repo", issue=8, token_cap=20_000_000, max_budget_usd="0.10")
                result = self.run_rounds(replies, initial=initial,
                    review_crash=mode == "resume", review_crash_after=1,
                    review_quota=mode.startswith("quota"), quota_after=1)
                attempts = [a for a in result["attempts"] if a["stage"] == "review"]
                self.assertEqual(len(self.forge.reviews), int(mode in ("recover", "quota")))
                if mode == "budget":
                    self.assertFalse(self.forge.merge_calls)
                    self.assertFalse(result["review"].review_findings)
                    self.assertEqual([a["outcome"] for a in attempts], ["review_denied"])
                    self.assertEqual(result["halt"], "budget_exceeded")
                    continue
                success = mode in ("recover", "quota")
                self.assertEqual(len(result["review"].review_findings), int(success))
                self.assertEqual(bool(self.forge.merge_calls), success)
                self.assertEqual(result.get("halt"), None if success else "invalid_output")
                self.assertEqual([a["outcome"] for a in attempts],
                    ["failed"] if mode == "failed" else
                    ["review_denied", *(["quota"] if mode.startswith("quota") else []),
                     "ran" if success else "review_denied"])
                for attempt in attempts:
                    if attempt["outcome"] != "quota":
                        self.assertEqual(attempt["usage"]["cost_usd"],
                                         0.1 if attempt["outcome"] == "review_denied" else SCRIPTED_USAGE.total_cost_usd)
                    if attempt["outcome"] == "review_denied":
                        self.assertIn("git commit", attempt["detail"])
                prompts = [p for p in self.prompts if p[0] == "review"]
                self.assertTrue(all(p[2] == prompts[0][2] for p in prompts))
                if not mode.startswith("quota"):
                    self.assertTrue(all(p[1]["dispatch_plan"]["models"] == prompts[0][1]["dispatch_plan"]["models"] for p in prompts))
                if mode == "resume":
                    self.assertEqual(self.interrupted["attempts"][-1]["outcome"], "review_denied")
                    self.assertFalse(self.interrupted["review"].review_findings)

    def test_retry_preserves_unresolved_findings_and_settled_rulings(self):
        denial = Outcome("review_denied", usage=SCRIPTED_USAGE, detail="denied chain")
        resolved = {**APPROVE, "resolutions": [{"id": "f1", "status": "resolved"}]}
        result = self.run_rounds([{**APPROVE, "findings": [FINDING]}, denial, resolved],
            initial={"review": ReviewState(settled_rulings=[{"id": "scope", "text": "Pinned."}])})
        prompts = [p for p in self.prompts if p[0] == "review"]
        self.assertEqual(prompts[1][2], prompts[2][2])
        self.assertIn("f1", prompts[2][2])
        self.assertIn("Pinned.", prompts[2][2])
        self.assertEqual(len(result["review"].review_findings), 2)
        self.assertEqual(len(self.forge.reviews), 2)
        self.assertTrue(self.forge.merge_calls)

    def test_committed_blob_grants_handle_hostile_paths(self):
        with review_trees() as (workspace, tip, base, previous, head):
            path = "space ' quote * ? [x] (:) $(touch ESCAPE).txt"
            (workspace.root / path).write_text("dependency bytes\n")
            subprocess.run(["git", "add", "--", path], cwd=workspace.root, check=True)
            subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=x@example.invalid",
                            "commit", "-m", "Dependency"], cwd=workspace.root, check=True, stdout=subprocess.PIPE)
            head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=workspace.root, text=True).strip()
            scope = workspace.review_scope(tip, head, previous)
            reads = []
            for command in scope.commands:
                output = subprocess.check_output(["sh", "-c", command], cwd=workspace.root)
                if "cat-file blob" in command:
                    reads.append(output)
                    self.assertRegex(command, r"^git --no-replace-objects cat-file blob [0-9a-f]{40}$")
            for content in (b"deleted from base\n", b"rename source\n", b"head line\n",
                            b"historical evidence\n" * 14, b"dependency bytes\n"):
                self.assertIn(content, reads)
            self.assertIn(ascii(path), scope.command)
            self.assertFalse((workspace.root / "ESCAPE").exists())

    def test_missing_duplicate_unknown_resolutions_and_reused_ids_fail_closed(self):
        resolved = {"id": "f1", "status": "resolved"}
        for resolutions, findings in (([], []), ([resolved, resolved], []),
                                       ([{"id": "missing", "status": "resolved"}], []), ([resolved], [FINDING])):
            with self.subTest(resolutions=resolutions, findings=findings):
                result = self.run_rounds([{**APPROVE, "findings": [FINDING]},
                                         {**APPROVE, "resolutions": resolutions, "findings": findings}])
                self.assertEqual(result["halt"], "invalid_output")
                self.assertFalse(self.forge.merge_calls)

    def test_settled_ruling_disagreement_is_a_note(self):
        result = self.run_rounds([{**APPROVE, "findings": [{**FINDING, "ruling_id": "size"}]}],
                                 initial={"review": ReviewState(settled_rulings=[{"id": "size", "text": "Size rule lives in the contract."}])})
        self.assertEqual(result["merge"].status, "armed")
        self.assertIn("Coordinator note f1", result["review"].detail)
        self.assertIn("Size rule lives in the contract.", self.prompts[-1][2])

    def test_resolved_findings_travel_as_settled_rulings(self):
        nit = {**FINDING, "severity": "nit"}
        result = self.run_rounds([
            {**APPROVE, "findings": [nit]},
            {**APPROVE, "resolutions": [{"id": "f1", "status": "resolved"}], "findings": [{**nit, "id": "f2"}]},
            {**APPROVE, "resolutions": [{"id": "f2", "status": "resolved"}],
             "findings": [{**nit, "id": "f3", "ruling_id": "finding:f1"}]},
        ])
        self.assertEqual(result["merge"].status, "armed")
        self.assertEqual(json.loads(self.prompts[-1][2].split("Settled, don't reopen:\n")[1])[0]["id"], "finding:f1")
        self.assertEqual(result["review"].review_findings[-1]["coordinator_notes"][0]["id"], "f3")

    def test_full_diff_rounds_resume_and_keep_worker_output(self):
        result = self.run_rounds([{**APPROVE, "findings": [FINDING]},
                                 {**APPROVE, "resolutions": [{"id": "f1", "status": "resolved"}]}], crash=True)
        self.assertEqual(result["merge"].status, "armed")
        self.assertEqual(self.diffs, [(BASE, HEAD, False), (BASE, HEAD, False), (HEAD, HEAD, True)])
        raw = parse_output(ReviewOutput, {**APPROVE, "findings": [FINDING]}, context=SCRIPTED_REVIEW_CONTEXT)
        before = raw.model_dump()
        past = ReviewHistory()
        accepted = accept(past, raw, HEAD, None)
        self.assertEqual(raw.model_dump(), before)
        self.assertEqual(past.rounds, [])
        self.assertEqual(accepted.change_kind, "blocking")

    def test_gate_rejects_every_previously_failing_head(self):
        past = ReviewHistory()
        output = parse_output(ReviewOutput, APPROVE, context=SCRIPTED_REVIEW_CONTEXT)
        for head, fails, expected in [("a", True, "changes"), ("b", True, "changes"),
                                      ("b", False, "changes"), ("c", False, "approve")]:
            accepted = accept(past, output, head, GraphCheck(failing_heads=[head], detail="Missing fixture") if fails else None)
            self.assertEqual(accepted.review, expected)
            past = accepted.history
        self.assertEqual(past.rulings(), [])

    def test_malformed_history_halts_only_in_review(self):
        result = self.run_rounds([APPROVE], initial={"review": ReviewState(review_findings=[{"broken": True}])})
        self.assertEqual(result["halt"], "invalid_output")
        self.assertIn("implement", result["path"])
    def test_recurrence_pauses_and_counts_accepted_round(self):
        result = self.run_rounds([{**APPROVE, "findings": [FINDING]},
                                 {**APPROVE, "resolutions": [{"id": "f1", "status": "resolved"}],
                                  "findings": [{**FINDING, "id": "again"}]}])
        self.assertEqual(result["halt"], "design_rethink")
        self.assertEqual(len(result["review"].review_findings), 2)
        self.assertEqual(len(self.forge.reviews), 2)
        self.assertEqual(result["review"].review_receipt["id"], 2)
        self.assertEqual(result["path"].count("implement"), 2)
        self.assertEqual([json.loads(line)["id"] for line in result["halt_detail"].splitlines()[1:]], ["f1", "again"])
        self.assertFalse(self.forge.merge_calls)

    def test_design_answer_resumes_implementation(self):
        result = self.run_rounds([{**APPROVE, "findings": [FINDING]},
                                 {**APPROVE, "resolutions": [{"id": "f1", "status": "resolved"}],
                                  "findings": [{**FINDING, "id": "again"}]},
                                 {**APPROVE, "resolutions": [{"id": "again", "status": "resolved"}]}], answer=True)
        self.assertEqual(result["merge"].status, "armed")
        self.assertEqual(result["review"].settled_rulings[0]["text"], "Redesign the ownership.")

        instruction = [p[2] for p in self.prompts if p[0] == "review"][2]
        self.assertEqual(json.loads(instruction.split("Settled, don't reopen:\n")[1])[0]["id"], "design:2")

    def test_outside_interdiff_nit_needs_explicit_resolution(self):
        nit = {**FINDING, "severity": "nit"}
        for status in ("resolved", "unresolved"):
            result = self.run_rounds([{**APPROVE, "findings": [nit]},
                {**APPROVE, "resolutions": [{"id": "f1", "status": "resolved"}],
                 "findings": [{**nit, "id": "main", "file": "main.txt", "line": 1}, {**nit, "id": "old"}]},
                {**APPROVE, "resolutions": [{"id": "main", "status": status}, {"id": "old", "status": status}]}], divergent=True)
            self.assertEqual(len(result["review"].review_findings), 3)
            self.assertEqual(result["stop"], "verified" if status == "resolved" else "review_cap")
            self.assertIn("main.txt:1 [nit]", self.forge.reviews[1][3])
            self.assertNotIn("Coordinator note main", self.forge.reviews[1][3])
            self.assertEqual(len(self.forge.reviews[1][4]), 1)
            self.assertEqual(result["path"].count("implement"), 3)

    def test_unknown_settled_ruling_rejected(self):
        result = self.run_rounds([{**APPROVE, "findings": [{**FINDING, "ruling_id": "unknown"}]}])
        self.assertEqual(result["halt"], "invalid_output")

    def test_graph_then_nit_then_approval_counts_at_cap(self):
        fact = {"claim": "CLI behavior", "fixture": "result.txt"}
        result = self.run_rounds([{**APPROVE, "tool_facts": [fact]},
            {**APPROVE, "findings": [{**FINDING, "severity": "nit"}]},
            {**APPROVE, "resolutions": [{"id": "f1", "status": "resolved"}]}],
            advance_head=True, gate_heads=["2" * 40, "3" * 40], initial={"review": ReviewState(fact_record="tests/fixtures/manifest.toml")})
        self.assertEqual(len(result["review"].review_findings), 3)
        self.assertEqual(result["merge"].status, "armed")
        self.assertIn("Graph verdict: changes", self.forge.reviews[0][3])

    def test_review_dispatch_crash_resumes_with_one_accepted_round(self):
        result = self.run_rounds([APPROVE], review_crash=True)
        self.assertEqual(self.interrupted["pending_dispatch"]["stage"], "review")
        self.assertEqual(len(self.interrupted["review"].review_findings), 0)
        self.assertEqual(result["merge"].status, "armed")
        self.assertEqual(len(result["review"].review_findings), 1)
        self.assertEqual(len(self.forge.reviews), 1)
        self.assertEqual([p[0] for p in self.prompts].count("review"), 2)

    def test_review_quota_failover_accepts_one_round(self):
        result = self.run_rounds([APPROVE], review_quota=True)
        self.assertEqual(result["merge"].status, "armed")
        self.assertEqual(len(result["review"].review_findings), 1)
        attempts = [a for a in result["attempts"] if a["stage"] == "review"]
        self.assertEqual([a["outcome"] for a in attempts], ["quota", "ran"])
        self.assertNotEqual(attempts[0]["model"], attempts[1]["model"])
        self.assertEqual(len(self.forge.reviews), 1)

    def test_post_crash_reuses_accepted_round(self):
        result = self.run_rounds([APPROVE], post_crash=True)
        self.assertEqual(len(result["review"].review_findings), 1)
        self.assertEqual(len(self.forge.reviews), 1)
        self.assertEqual(result["review"].review_receipt["id"], 1)
        self.assertEqual([p[0] for p in self.prompts].count("review"), 1)

    def test_interdiff_need_not_parse_for_focused_instruction(self):
        result = self.run_rounds([{**APPROVE, "findings": [FINDING]},
            {**APPROVE, "resolutions": [{"id": "f1", "status": "resolved"}]}], bad_interdiff=True)
        self.assertEqual(result["merge"].status, "armed")
        self.assertIn((HEAD, HEAD, True), self.diffs)

    def test_diff_parse_failure_is_tolerated_first_and_unavailable_later(self):
        result = self.run_rounds([APPROVE], bad_diff=True)
        self.assertEqual(result["merge"].status, "armed")
        result = self.run_rounds([{**APPROVE, "findings": [FINDING]}, APPROVE], bad_diff=True)
        self.assertEqual(result["halt"], "review_unavailable")

    def test_gate_never_counts_as_reviewer_recurrence(self):
        fact = {"claim": "CLI behavior", "fixture": None}
        result = self.run_rounds([{**APPROVE, "tool_facts": [fact]},
                                 {**APPROVE, "findings": [FINDING]},
                                 {**APPROVE, "resolutions": [{"id": "f1", "status": "resolved"}]}])
        self.assertEqual(result["stop"], "review_cap")

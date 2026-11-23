"""Test review rounds."""
import tests
from foreman.state import ReviewState, PlanState, ChecksState, RunState, checkpoint_serializer, SCHEMA_VERSION
import json
import os
import shlex
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
from foreman.delivery import LiveRun, compile_delivery, read_delivery_state
from foreman.runner import snapshot_result
from foreman.review_round import history
from foreman.smoke import smoke_verdict
from foreman.github import LiveForge
from foreman.config import ModelSpec
from foreman.adapters import Outcome
from foreman.dry_run import CLEAR, SCRIPTED_USAGE
from skill_fixture import skill_fixture
from test_adapters import codex_events, codex_process, fixture_process, worker_process
from test_runner import APPROVE, FINDING, BASE, HEAD, EMPTY_SLICE, FakeGitHub, FakeWorkspace


@contextmanager
def review_trees(*, fix_items=False):
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
        (root / "items.txt").write_text("one\none\n")
        (root / "shared.txt").write_text("previous\nextra line\n")
        (root / "deleted.txt").unlink()
        git("mv", "old.txt", "new.txt")
        git("add", ".")
        git("commit", "-m", "First reviewed head")
        previous = git("rev-parse", "HEAD")
        for path in (root / "docs").iterdir():
            path.unlink()
        if fix_items:
            (root / "items.txt").write_text("one\nthree\n")
        (root / "shared.txt").write_text("head line\n")
        git("add", ".")
        git("commit", "-m", "Repair reviewed changes")
        head = git("rev-parse", "HEAD")
        git("switch", "main")
        (root / "main.txt").write_text("base tip advance\n")
        git("add", ".")
        git("commit", "-m", "Advance base tip")
        tip = git("rev-parse", "HEAD")
        git("switch", "review")
        git("remote", "add", "origin", str(root))
        yield LiveWorkspace(root), tip, base, previous, head


class MergeReadbackForge(FakeGitHub, LiveForge):
    arm_auto_merge = LiveForge.arm_auto_merge

    def _run_command(self, repo, command, **kwargs):
        if command[1:3] == ["pr", "merge"]:
            number = int(command[3])
            head = command[command.index("--match-head-commit") + 1]
            if (number, head) != (self.target.number, self.target.headRefOid):
                raise AssertionError("Merge command did not bind the published PR head")
            self.merge_calls.append((repo, int(self.target.headRefName.split("-")[0]), self.target.headRefName, head))
            return ""
        if command[1:3] == ["api", "graphql"] and f"number={self.target.number}" in command:
            return json.dumps({
                "number": self.target.number, "headRefOid": self.target.headRefOid,
                "baseRefOid": self.base, "baseRefName": self.target.baseRefName, "isDraft": False,
                "state": "MERGED" if self.merge_calls else "OPEN",
                "autoMergeRequest": None, "mergeQueueEntry": None,
            })
        raise AssertionError(f"Unexpected GitHub command: {command}")

    def review_target(self, *args):
        self.target = super().review_target(*args)
        return self.target


def rendered_patch(workspace, state):
    scope = state["dispatch_plan"]["scope"]["command"]
    diff, = (line for line in scope.splitlines() if line.startswith("git ") and " diff " in line)
    return subprocess.check_output(["sh", "-c", diff], cwd=workspace.root, text=True)


class ReviewRoundsTest(unittest.TestCase):
    def run_rounds(self, reviews, *, initial=None, crash=False, post_crash=False, review_crash=False, review_quota=False,
                   rerun=False, answer=None, trees=None, repo="Example/Repo", mutate=None):
        self.prompts = []
        self.forge = MergeReadbackForge() if repo.startswith("bookout-labs/foreman-smoke") else FakeGitHub()
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

            class Workspace(FakeWorkspace):
                def review_scope(self, base, head):
                    return trees[0].review_scope(base, head) if trees else super().review_scope(base, head)

                def commit(self, message):
                    super().commit(message)
                    if trees:
                        self.current = type(self.current)(trees[3] if implementations == 1 else trees[4], True, self.current.branch)

                def review_context(self, base, head):
                    if trees:
                        return trees[0].review_context(base, head)
                    return super().review_context(base, head)

            if trees:
                self.forge.base = trees[1]
            replies = iter(reviews)
            crashed = False
            quota_returned = False
            workspace = Workspace(Path(directory))
            workspace.remote = self.forge.branches
            implementations = 0

            def dispatch(node, model, state, instruction):
                nonlocal crashed, implementations, quota_returned
                self.prompts.append((node, state, instruction))
                if node == "review":
                    if review_crash and not crashed:
                        crashed = True
                        raise RuntimeError("process lost")
                    if review_quota and not quota_returned:
                        quota_returned = True
                        return Outcome("quota", usage=SCRIPTED_USAGE)
                    raw = next(replies)
                    if callable(raw):
                        raw = raw(state, instruction)
                    if mutate is not None:
                        mutate(self.forge)
                    return raw if isinstance(raw, Outcome) else Outcome("ran", raw, usage=SCRIPTED_USAGE)
                if crash and state["review"].review_findings and not crashed:
                    crashed = True
                    raise RuntimeError("process lost")
                if node == "triage":
                    return Outcome("ran", {"triage": "supplied_slice", "work_kind": "feature", "slice": None, "human_question": None}, usage=SCRIPTED_USAGE)
                if node == "spec_check":
                    return Outcome("ran", CLEAR.model_dump(), usage=SCRIPTED_USAGE)
                implementations += 1
                workspace.current = type(workspace.current)(workspace.current.head, False, workspace.current.branch)
                return Outcome("ran", {"implementation": "Fixed.", "human_question": None}, usage=SCRIPTED_USAGE)

            with SqliteSaver.from_conn_string(str(Path(directory) / "checkpoint")) as saver:
                saver.serde = checkpoint_serializer()
                graph = compile_delivery(dispatch, settings, scratch=scratch,
                                         run=LiveRun(self.forge, workspace), checkpointer=saver)
                config = {"configurable": {"thread_id": "example/repo#8"}}
                state = {"review": ReviewState(), "schema_version": SCHEMA_VERSION, "run": RunState(repo=repo, issue=8, base_branch="main", base_sha=workspace.current.head, token_cap=20_000_000, max_budget_usd=None), "plan": PlanState(issue_kind="slice", slice=EMPTY_SLICE),
                         "checks": ChecksState(check="exit 0"), "pstack_pin": settings.pstack.model_dump(), "matt_skills_pin": settings.matt_skills.model_dump(), **(initial or {})}
                if crash or post_crash or review_crash:
                    with self.assertRaisesRegex(RuntimeError, "process lost"):
                        graph.invoke(state, config)
                    self.interrupted = read_delivery_state({"stop": "interrupted", **graph.get_state(config).values})
                    self.interrupted_next = graph.get_state(config).next
                    if review_crash:
                        self.assertEqual(self.forge.reviews, [])
                    (scratch / "resume").mkdir()
                    graph = compile_delivery(dispatch, settings, scratch=scratch / "resume",
                                             run=LiveRun(self.forge, workspace), checkpointer=saver)
                    result = graph.invoke(None, config)
                else:
                    result = graph.invoke(state, config)
                if answer is not None:
                    result = graph.invoke(Command(resume=answer), config)
                if rerun:
                    from foreman.runner import run_live_delivery
                    from unittest.mock import patch
                    with patch('foreman.runner.load_config', return_value=settings):
                        result = run_live_delivery(repo=repo, issue=8, check='exit 0',
                            checkpoint=Path(directory) / 'checkpoint', run=LiveRun(self.forge, workspace), dispatcher=dispatch)
                if (paused := graph.get_state(config, subgraphs=True)).interrupts:
                    return snapshot_result(paused)
                return read_delivery_state({"stop": "needs_human", **result})

    def test_claude_grants_each_rendered_review_command(self):
        with review_trees() as trees:
            workspace, tip, _, previous, head = trees
            result = self.run_rounds([
                {**APPROVE, "findings": [FINDING]},
                APPROVE,
            ], trees=trees)
            self.assertEqual(result["merge"].status, "merged")
            reviews = [p for p in self.prompts if p[0] == "review"]
            self.assertEqual(len(reviews), 2)
            for index, (node, state, instruction) in enumerate(reviews):
                scope = state["dispatch_plan"]["scope"]["command"]
                self.assertIn(scope, instruction)
                commands = [line for line in scope.splitlines() if line.startswith("git ")]
                diffs = [c for c in commands if " diff " in c]
                self.assertEqual(len(diffs), 1)
                current = head if index else previous
                self.assertIn(f"{tip}...{current} --", diffs[0])
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

                cases = [((), "ran"), (outside, "failed")]
                for command in commands:
                    cases.extend(((denied,), "failed") for denied in
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
                    def runner(args, cwd, env):
                        self.assertFalse(Path(env["TMPDIR"]).resolve().is_relative_to(checkout.resolve()))
                        settings = Path(args[args.index("--settings") + 1])
                        self.assertFalse(settings.resolve().is_relative_to(checkout.resolve()))
                        return fixture_process(args, "success", 0)
                    outcome = CliWorker(checkout, runner, scratch_root=scratch_root)(
                        "review", ModelSpec.parse("claude-opus-5-5-high"), {}, "Review.")
                    self.assertEqual(outcome.kind, "ran")
            self.assertEqual(list(checkout.iterdir()), [nested])
            self.assertEqual(list(nested.iterdir()), [])

    def test_review_payload_stays_off_exec_argv(self):
        with review_trees() as (workspace, tip, _, previous, head), TemporaryDirectory() as scratch:
            tools = LiveWorkspace(Path.cwd())
            with patch.object(tools, "_fetch_review"):
                scopes = [tools.review_scope(
                    subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
                    subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip())]
            for index in range(2000):
                (workspace.root / f"blob-{index}.txt").write_text(str(index))
            subprocess.run(["git", "add", "."], cwd=workspace.root, check=True)
            subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                            "commit", "-qm", "2000 blobs"], cwd=workspace.root, check=True)
            head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=workspace.root, text=True).strip()
            scopes.append(workspace.review_scope(tip, head))
            for scope in scopes:
                instruction = "Review with standalone commands.\n" + scope.command
                plan = {"models": [], "instruction": instruction, "methods": [], "read_roots": [],
                        "target": None, "scope": {"command": scope.command, "inline": []}}
                def runner(args, cwd, env):
                    settings = Path(args[args.index("--settings") + 1])
                    self.assertTrue(settings.resolve().is_relative_to(Path(scratch).resolve()))
                    self.assertFalse(Path(env["TMPDIR"]).resolve().is_relative_to(workspace.root.resolve()))
                    with worker_process(fixture_process(args, "success", 0)) as execute:
                        result = run_worker_command(args, cwd, env)
                    prompt = execute.prompt
                    self.assertEqual(prompt.count(instruction), 1)
                    projected_plan = json.loads(prompt.split("Delivery state:\n")[1])["dispatch_plan"]
                    self.assertFalse({"instruction", "scope"} & projected_plan.keys())
                    self.assertLess(sum(len(arg.encode()) + 1 for arg in execute.call_args.args[0]), 32768)
                    return result
                CliWorker(workspace.root, runner, scratch_root=Path(scratch))(
                    "review", ModelSpec.parse("claude-opus-5-5-high"), {"dispatch_plan": plan}, instruction)

    def test_committed_blob_grants_handle_hostile_paths(self):
        with review_trees() as (workspace, tip, base, previous, head):
            path = "space ' quote * ? [x] (:) $(touch ESCAPE).txt"
            (workspace.root / path).write_text("dependency bytes\n")
            subprocess.run(["git", "add", "--", path], cwd=workspace.root, check=True)
            subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=x@example.invalid",
                            "commit", "-m", "Dependency"], cwd=workspace.root, check=True, stdout=subprocess.PIPE)
            head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=workspace.root, text=True).strip()
            scope = workspace.review_scope(tip, head)
            reads = []
            for command in scope.commands:
                output = subprocess.check_output(["sh", "-c", command], cwd=workspace.root)
                if "cat-file blob" in command:
                    reads.append(output)
                    self.assertRegex(command, r"^git --no-replace-objects cat-file blob [0-9a-f]{40}$")
            for content in (b"deleted from base\n", b"rename source\n", b"head line\n",
                            b"dependency bytes\n"):
                self.assertIn(content, reads)
            self.assertIn(ascii(path), scope.command)
            self.assertFalse((workspace.root / "ESCAPE").exists())

    def test_review_dispatch_crash_resumes_with_one_accepted_round(self):
        result = self.run_rounds([APPROVE], review_crash=True)
        self.assertEqual(self.interrupted_next, ("review",))
        self.assertEqual(len(self.interrupted["review"].review_findings), 0)
        self.assertEqual(result["merge"].status, "merged")
        self.assertEqual(len(result["review"].review_findings), 1)
        self.assertEqual(len(self.forge.reviews), 1)
        self.assertEqual([p[0] for p in self.prompts].count("review"), 2)

    def test_review_quota_failover_accepts_one_round(self):
        result = self.run_rounds([APPROVE], review_quota=True)
        self.assertEqual(result["merge"].status, "merged")
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

    def test_foreman_smoke_optional_record_needs_no_repair(self):
        note = {**FINDING, "kind": "other", "severity": "P2",
                "summary": "An optional diagnostics record would help future readers."}
        result = self.run_rounds([{**APPROVE, "findings": [note]}], repo="bookout-labs/foreman-smoke")
        head = result["checks"].verified_head
        self.assertEqual(result["path"].count("implement"), 1)
        self.assertEqual(result["review"].review_receipt["commit_id"], head)
        self.assertEqual(self.forge.merge_calls, [("bookout-labs/foreman-smoke", 8, "8-list-items-once", head)])
        self.assertIn(note["summary"], self.forge.reviews[0][4][0]["body"])
        self.assertEqual(result["merge"].receipt["status"], "MERGED")
        self.assertEqual(result["merge"].receipt["source_head"], head)
        self.assertEqual(smoke_verdict(result).exit_code, 0)

    def test_foreman_smoke_npm_repairs_p1_once_then_reposts_advice(self):
        note = {**FINDING, "id": "optional-record", "kind": "other", "severity": "P2"}
        with review_trees(fix_items=True) as trees:
            workspace, tip, base, previous, head = trees

            def review(state, instruction):
                findings = [note]
                if "-two\n+one\n" in rendered_patch(workspace, state):
                    findings.append(FINDING)
                return {**APPROVE, "findings": findings}

            result = self.run_rounds([review, review], trees=trees, repo="bookout-labs/foreman-smoke-npm")
            self.assertEqual(result["path"].count("implement"), 2)
            self.assertEqual([post[1:3] for post in self.forge.reviews], [(12, previous), (12, head)])
            self.assertEqual(self.forge.merge_calls[-1][-1], head)
            self.assertEqual(result["checks"].verified_head, head)
            self.assertEqual(result["review"].review_receipt["commit_id"], head)
            self.assertEqual(result["merge"].receipt["status"], "MERGED")
            self.assertEqual(result["merge"].receipt["source_head"], head)
            self.assertEqual(smoke_verdict(result).exit_code, 0)
            self.assertEqual([[finding["id"] for finding in report["findings"]]
                              for report in result["review"].review_findings], [["optional-record", "f1"], ["optional-record"]])
            reviews = (p for p in self.prompts if p[0] == "review")
            for (_, state, _), current, line in zip(reviews, (previous, head), ("one", "three"), strict=True):
                commands = state["dispatch_plan"]["scope"]["command"].splitlines()
                diff, = (command for command in commands if command.startswith("git ") and " diff " in command)
                self.assertIn(f"{tip}...{current}", diff)
                self.assertIn(f"# base = PR merge-base {base}", commands)
                self.assertIn(f"-two\n+{line}\n", rendered_patch(workspace, state))

    def test_unchanged_defect_and_priority_count_once_per_round_at_cap(self):
        critical = {**FINDING, "id": "critical", "severity": "P0",
                    "location": {"side": "head", "path": "docs/issue-36-verification.md", "line": 1},
                    "summary": "Remove the committed incident data.",
                    "evidence": "The new verification record contains historical evidence.",
                    "impact": "The evidence becomes public if this branch merges."}
        with review_trees() as trees:
            workspace = trees[0]

            def review(state, instruction):
                patch = rendered_patch(workspace, state)
                findings = [FINDING] if "-two\n+one\n" in patch else []
                if "historical evidence" in patch:
                    findings.append(critical)
                return {**APPROVE, "findings": findings}

            result = self.run_rounds([review] * 3, trees=trees)
        self.assertEqual(result["halt"], "review_cap")
        self.assertEqual(result["path"].count("implement"), 3)
        self.assertEqual([body.split(".")[0] for _, _, _, body, _ in self.forge.reviews],
                         ["Review route: P0", "Review route: P1", "Review route: P1"])
        self.assertFalse(self.forge.merge_calls)

    def test_settled_ruling_is_advisory_and_unknown_ruling_rejects(self):
        output = {**APPROVE, "findings": [{**FINDING, "ruling_id": "size"}]}
        result = self.run_rounds([output], initial={"review": ReviewState(
            settled_rulings=[{"id": "size", "text": "Size rule lives in the contract."}])})
        self.assertEqual(result["merge"].status, "merged")
        self.assertIn("Advisory, settled by size", self.forge.reviews[0][4][0]["body"])
        result = self.run_rounds([output])
        self.assertEqual(result["halt"], "invalid_output")
        self.assertFalse(self.forge.reviews)

    def test_posted_comment_cannot_clear_a_moved_base(self):
        moved = "c" * 40

        def advance(forge):
            # The base advances after the first review, so the next review reads the new base.
            if not forge.reviews:
                forge.base = moved
        result = self.run_rounds([APPROVE, APPROVE], mutate=advance)
        self.assertEqual(result["stop"], "merged")
        self.assertEqual([review[2] for review in self.forge.reviews], [HEAD, HEAD])
        self.assertEqual(history({"rounds": result["review"].review_findings}).rounds[-1].base, moved)
        self.assertEqual(len(self.forge.merge_calls), 1)

    def test_posted_comment_cannot_clear_an_unverified_head(self):
        def move(forge):
            readiness = forge.readiness
            def moved(*args):
                observation = readiness(*args)
                return observation.model_copy(update={"outcome": "head_changed", "pr": observation.pr.model_copy(
                    update={"head": observation.pr.head.model_copy(update={"sha": "c" * 40})})})
            forge.readiness = moved
            forge.foreign_heads.add("c" * 40)
        result = self.run_rounds([APPROVE], mutate=move)
        # Without ancestry from the checked head the run asks and neither adopts nor repairs the revision.
        self.assertEqual(result["stop"], "needs_human")
        self.assertIn("does not descend from the checked head", result["halt_detail"])
        self.assertFalse(self.forge.merge_calls)

    def test_failed_review_stops_without_comment_or_retry(self):
        result = self.run_rounds([Outcome("failed", detail="Read denied.", usage=SCRIPTED_USAGE)])
        self.assertEqual(result["halt"], "invalid_output")
        self.assertEqual([stage for stage, _, _ in self.prompts].count("review"), 1)
        self.assertFalse(self.forge.reviews)
        self.assertFalse(self.forge.merge_calls)

    def test_codex_review_accepts_no_match_after_required_reads(self):
        for search in (
            "git --no-replace-objects grep -n FOREMAN_REMOVED_PATH {head} -- items.txt",
            "git grep -F '|' {head} -- items.txt",
            'git grep -F "&&" {head} -- items.txt',
            "git grep -F ';' {head} -- items.txt",
            "git grep -F '>' {head} -- items.txt",
            r"git grep -F \| {head} -- items.txt",
        ):
            with self.subTest(search=search):
                self.check_codex_review_reads(search)

    def test_codex_review_rejects_compound_searches_with_exit_one(self):
        for search in (
            "git grep -F one {head} -- items.txt | grep -F FOREMAN_REMOVED_PATH",
            "git grep -F one {head} -- items.txt|grep -F FOREMAN_REMOVED_PATH",
            "git grep -F FOREMAN_REMOVED_PATH {head} -- items.txt && true",
            "git grep -F FOREMAN_REMOVED_PATH {head} -- items.txt; false",
            "git grep -F FOREMAN_REMOVED_PATH {head} -- items.txt > search-output.txt",
        ):
            with self.subTest(search=search):
                self.check_codex_review_reads(search, halt_detail="exit 1")

    def test_codex_review_rejects_git_grep_error(self):
        self.check_codex_review_reads(
            "git grep -G '[' {head} -- items.txt", search_exit=128, halt_detail="exit 128",
        )

    def test_codex_review_rejects_failed_denied_or_missing_reads(self):
        for status, halt_detail in (
            ("failed", "exit 128"),
            ("declined", "command failed"),
            ("missing", "required read"),
        ):
            with self.subTest(status=status):
                self.check_codex_review_reads(
                    "git --no-replace-objects grep -n FOREMAN_REMOVED_PATH {head} -- items.txt",
                    read_status=status, halt_detail=halt_detail,
                )

    def check_codex_review_reads(self, search_template, *, search_exit=1, read_status=None, halt_detail=None):
        with review_trees() as trees:
            workspace = trees[0]

            def review(state, instruction):
                required = [line for line in instruction.splitlines() if line.startswith("git ")]
                blob = next(command for command in required if "cat-file blob" in command)
                head = state["dispatch_plan"]["target"]["headRefOid"]
                search = search_template.format(head=head)
                commands = []
                for command in [*required, search]:
                    status = read_status if command == blob else None
                    if status == "missing":
                        continue
                    code, output = None, ""
                    if status != "declined":
                        env = {**os.environ, "GIT_DIR": str(workspace.root / "missing.git")} if status == "failed" else None
                        argv = ["/bin/sh", "-c", command] if command == search else shlex.split(command)
                        completed = subprocess.run(argv, cwd=workspace.root, env=env,
                                                   text=True, capture_output=True)
                        code, output = completed.returncode, completed.stdout + completed.stderr
                        status = "failed" if code else "completed"
                    if command == search:
                        self.assertEqual(code, search_exit)
                        if search_exit == 1:
                            self.assertEqual(output, "")
                    commands.append({"type": "item.completed", "item": {
                        "id": f"read_{len(commands)}", "type": "command_execution",
                        "command": f"/bin/zsh -lc {shlex.quote(command)}",
                        "exit_code": code, "status": status, "aggregated_output": output,
                    }})
                events = codex_events()
                events[-2]["item"]["text"] = json.dumps(APPROVE)
                events[-2:-2] = commands
                worker = CliWorker(workspace.root, lambda *_: codex_process(events))
                return worker("review", ModelSpec.parse("gpt-6.1-sol-high"), state, instruction)

            result = self.run_rounds([review], trees=trees)
        if halt_detail is None:
            self.assertEqual(result["stop"], "merged")
            self.assertEqual(len(self.forge.merge_calls), 1)
        else:
            self.assertEqual(result.get("halt"), "invalid_output")
            self.assertIn(halt_detail, result["halt_detail"])
            self.assertFalse(self.forge.reviews)
            self.assertFalse(self.forge.merge_calls)

    def test_resuming_after_base_move_reviews_same_checked_head_again(self):
        new_base = "d" * 40

        def first_review(state, instruction):
            self.forge.base = new_base
            return APPROVE

        result = self.run_rounds([first_review, APPROVE], rerun=True)
        self.assertEqual(result["stop"], "merged")
        self.assertEqual(result["path"].count("implement"), 1)
        self.assertEqual([(r["reviewed_head"], r["base"]) for r in result["review"].review_findings],
                         [(HEAD, BASE), (HEAD, new_base)])
        self.assertEqual(len(self.forge.reviews), 2)
        self.assertEqual(len(self.forge.merge_calls), 1)
        self.assertEqual(result["merge"].receipt["base"], new_base)

    def test_malformed_history_halts_only_in_review(self):
        result = self.run_rounds([APPROVE], initial={"review": ReviewState(review_findings=[{"broken": True}])})
        self.assertEqual(result["halt"], "invalid_output")
        self.assertIn("implement", result["path"])
        self.assertFalse(self.forge.reviews)
        self.assertFalse(self.forge.merge_calls)

    def test_interrupted_repair_keeps_the_posted_round(self):
        result = self.run_rounds([{**APPROVE, "findings": [FINDING]}, APPROVE], crash=True)
        self.assertEqual(len(self.interrupted["review"].review_findings), 1)
        self.assertEqual(self.interrupted["review"].review_receipt["id"], 1)
        self.assertEqual(result["path"].count("implement"), 2)
        self.assertEqual(len(self.forge.reviews), 2)
        self.assertEqual(result["stop"], "merged")

    def test_answer_reopens_review_before_repair(self):
        question = "Should the list include archived items?"
        answer = "Active items only."

        def after_answer(state, instruction):
            self.assertIn(answer, instruction)
            return APPROVE

        result = self.run_rounds([{**APPROVE, "human_question": question, "findings": [FINDING]}, after_answer],
                                 answer=answer)
        self.assertEqual(result["stop"], "merged")
        self.assertEqual(result["path"].count("implement"), 1)
        # The question pauses inspect, so only the answered review is posted.
        self.assertEqual(len(self.forge.reviews), 1)

    def test_report_with_unresolved_intent_holds_for_an_answer(self):
        question = "Should the list include archived items?"
        result = self.run_rounds([{**APPROVE, "human_question": question}])
        self.assertEqual(result["stop"], "needs_human")
        self.assertEqual(result["review"].review, "")
        self.assertTrue(result["halt_detail"].startswith(question))
        self.assertEqual(self.forge.reviews, [])
        self.assertFalse(self.forge.merge_calls)

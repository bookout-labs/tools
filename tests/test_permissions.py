import tests
from foreman.state import PlanState, ChecksState, RunState

from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import unittest
import re
import tomllib
from tempfile import TemporaryDirectory
import subprocess
from subprocess import CompletedProcess
from unittest.mock import patch

from foreman.adapters import Call, CapabilityUnavailable, Outcome
from foreman.adapters.claude import CAPTURED_VERSION, SYSTEM_READS, ClaudeAdapter
from foreman.adapters.codex import CodexAdapter
from foreman.adapters.grok import GrokAdapter
from foreman.config import ModelSpec, NodeConfig, load_config
from foreman.delivery import run_delivery
from foreman.dry_run import ScriptHandler, SCRIPTED_USAGE
from foreman.skill_rules import PACKAGE_SKILLS, export_skills, git, stage_methods
from foreman.stages import STAGES
from foreman.credentials import command_environment
from foreman.worker import CliWorker, git_dirs
from foreman.workspace import LiveWorkspace, ReviewScope, added_lines
from skill_fixture import skill_fixture, verify_checkout
from test_adapters import codex_events, codex_process, fixture_process
from publication_fixture import contract_text
from test_runner import EMPTY_SLICE


DIFF = "git --no-replace-objects diff --no-color a...b --"
CLAUDE = Path(__file__).parent / "fixtures/claude/permissions"


@contextmanager
def claude_cli(version: str = "2.1.332"):
    """A `claude` on PATH that reports this version, as the host check reads it."""
    with TemporaryDirectory() as directory:
        fake = Path(directory) / "claude"
        fake.write_text(f"#!/bin/sh\necho '{version} (Claude Code)'\n")
        fake.chmod(0o755)
        with patch.dict(os.environ, PATH=f"{directory}{os.pathsep}{os.environ['PATH']}"):
            yield


def sandbox(call: Call, *, auto: bool) -> dict[str, object]:
    """The sandbox every Claude shell gets: reads denied everywhere, then the system, git's prefix and the call's own roots."""
    forms = lambda paths: list(dict.fromkeys(str(form) for path in paths for form in (path.absolute(), path.resolve())))
    git = shutil.which("git")
    roots = (*((call.git_dirs[0].parent,) if call.git_dirs else ()), *call.read_roots, *call.git_dirs, call.scratch,
             *((call.shell_temp,) if call.shell_temp else ()))
    return {"enabled": True, "autoAllowBashIfSandboxed": auto, "failIfUnavailable": True, "allowUnsandboxedCommands": False,
            "network": {"allowedDomains": [], "strictAllowlist": True},
            "filesystem": {"denyRead": ["/"], "allowRead": [*SYSTEM_READS["Darwin"], *forms((Path(git).parent.parent,) if git else ()),
                                                            *forms(roots)],
                           "denyWrite": forms((*call.read_roots, *call.git_dirs))}}


class PermissionsTest(unittest.TestCase):
    def test_method_paths_are_covered_by_precise_roots(self):
        with skill_fixture() as (config, scratch), TemporaryDirectory() as directory:
            root = export_skills(config.pstack, config.matt_skills, scratch)
            # The worker runs in the target checkout, so its own verify skill needs no extra read root.
            checkout = Path(directory)
            # A repository opts in to each target skill by tracking it: verify-<app> and formal-<area>.
            git(checkout, "init", "--quiet")
            for skill in ("verify-board", "formal-recovery"):
                target_skill = checkout / f".claude/skills/{skill}/SKILL.md"
                target_skill.parent.mkdir(parents=True)
                target_skill.write_text("Run the board checks.\n")
            git(checkout, "add", "--", ".claude")
            git(checkout, "-c", "user.name=Foreman", "-c", "user.email=foreman@example.invalid", "commit", "--quiet", "-m", "Target skills")
            for stage, row in STAGES.items():
                if not row.skills:
                    continue
                for kind in ("feature", "bug_fix", "perf_issue"):
                    state = {"run": RunState(workspace=str(checkout), base_sha=git(checkout, "rev-parse", "HEAD")),
                             "checks": ChecksState(verify_skill=".claude/skills/verify-board/SKILL.md"), "plan": PlanState(work_kind=kind, issue_kind="slice", slice={**EMPTY_SLICE, "stateful": True, "screen": "docs/screens/ui.md", "properties": [{"name": "rule", "constraint": "unique"}]})}
                    text, methods, roots = stage_methods(stage, state, root, LiveWorkspace(checkout))
                    self.assertEqual(len(roots), len(set(roots)))
                    self.assertTrue(set(roots) <= {str(root.resolve()), str(PACKAGE_SKILLS.resolve())})
                    instructions = text.split('\n\n') if text else []
                    self.assertEqual(len(instructions), len(methods))
                    for instruction in instructions:
                        if instruction.startswith("Follow the repository's verify skill"):
                            # The verify skill is carried as the base's text, so it needs no read root.
                            self.assertIn("Run the board checks.", instruction)
                            continue
                        path = instruction.split(' of ')[-1].split(' as the method.')[0] if ' of ' in instruction else instruction.removeprefix('Read ').split(' in full')[0]
                        self.assertTrue(Path(path).is_file())
                        self.assertTrue(any(Path(path).is_relative_to(Path(r)) for r in (*roots, checkout)))
                    for skill, owner in (("verify-board", "verify"), ("formal-recovery", "implement")):
                        self.assertEqual(f".claude/skills/{skill}/SKILL.md" in methods, stage == owner)

    @patch("foreman.adapters.claude.sandboxed")
    def test_every_worker_reads_the_related_checkouts_and_none_writes_them(self, _confinable):
        """Each related checkout the contract names is a read root of all seven stages, which the adapters grant read-only."""
        contract = contract_text("sh check.sh") + b"related_repositories: [{repository: Example/Api, start: 'true'}]\n"
        roots: dict[str, list[str]] = {}
        for script, item in ((ScriptHandler(triage="question"), None), (ScriptHandler(triage="plan", slices=[EMPTY_SLICE]), None),
                             (ScriptHandler(), EMPTY_SLICE)):
            def handler(node, model, state, instruction):
                roots[node] = state["dispatch_plan"]["read_roots"]
                return script(node, model, state, instruction)
            with verify_checkout() as target:
                run_delivery(handler, repo="Example/Engine", request="Change", contract=contract, slice=item, checkout=target)
        checkout = "<checkout of Example/Api>"
        self.assertEqual(roots, dict.fromkeys(("triage", "research", "spec", "spec_check", "implement", "verify", "review"), [checkout]))
        with TemporaryDirectory() as directory:
            related, attempt = Path(directory) / "api", Path(directory) / "attempt"
            attempt.mkdir()
            claude = ClaudeAdapter(ModelSpec.parse("claude-opus-5-5-high"))
            for edit, drive in ((False, False), (True, False), (False, True)):
                with self.subTest(edit=edit, drive=drive), patch("foreman.adapters.claude.platform.system", return_value="Darwin"):
                    args = claude.command(Call("prompt", "{}", edit, attempt, read_roots=(related,), drive=drive))
                    settings = json.loads(Path(args[args.index("--settings") + 1]).read_text())
                    self.assertIn(f"Edit(/{related}/**)", settings["permissions"]["deny"])
                    if edit or drive:
                        self.assertIn(str(related), settings["sandbox"]["filesystem"]["denyWrite"])
            codex = CodexAdapter(ModelSpec.parse("gpt-6.1-sol-high")).command(Call("prompt", "{}", True, attempt, read_roots=(related,)))
            self.assertIn(f'"{related.resolve()}"="read"', " ".join(codex))

    @patch("foreman.adapters.claude.sandboxed")
    def test_first_fix_and_quota_fallback_receive_the_bound_grant(self, _confinable):
        for fallback in (False, True):
            config = load_config()
            names = (["gpt-6.1-sol-high", "gpt-6.1-terra-high"] if fallback else []) + ["claude-opus-5-5-high"]
            config.nodes["implement"] = NodeConfig("implementer", tuple(ModelSpec.parse(n) for n in names))
            with skill_fixture(config) as (config, scratch):
                calls = []
                script = ScriptHandler(reviews=["changes", "approve"])

                def runner(args, cwd, env):
                    calls.append((args, json.loads(Path(args[args.index("--settings") + 1]).read_text())))
                    return fixture_process(args, "success", 0)

                worker = CliWorker(scratch, runner, scratch_root=Path(self.enterContext(TemporaryDirectory())))

                def handler(node, model, state, instruction):
                    if node != "implement":
                        return script(node, model, state, instruction)
                    if model.family == "gpt":
                        return Outcome("quota", usage=SCRIPTED_USAGE)
                    worker(node, model, state, instruction)
                    roots = state["dispatch_plan"]["read_roots"]
                    argv, settings = calls[-1]
                    self.assertEqual([argv[i + 1] for i, a in enumerate(argv) if a == "--add-dir"][:-1], roots)
                    permissions = settings["permissions"]
                    self.assertEqual(permissions["allow"], ["Bash(sh check.sh)", "Edit", "Write"])
                    return script(node, model, state, instruction)

                result = run_delivery(handler, repo="Example/Engine", request="Change", contract=contract_text("sh check.sh"),
                                      config=config, slice=EMPTY_SLICE, scratch=scratch)
                self.assertEqual(result["stop"], "merge_pending")
                self.assertEqual(len(calls), 2)

    def test_editing_bash_runs_in_a_fail_closed_sandbox_that_writes_only_the_checkout_and_scratch(self):
        adapter = ClaudeAdapter(ModelSpec.parse("claude-opus-5-5-high"))
        with TemporaryDirectory() as directory:
            root, attempt = Path(directory), Path(directory) / "attempt"
            attempt.mkdir()
            gits = (root / "repo/.git", root / "clone/.git/worktrees/repo", root / "clone/.git")
            call = Call("prompt", "{}", True, attempt, checks=("sh check.sh",), read_roots=(root / "methods",), git_dirs=gits)
            with patch("foreman.adapters.claude.platform.system", return_value="Darwin"), claude_cli():
                args = adapter.command(call)
            settings = json.loads(Path(args[args.index("--settings") + 1]).read_text())
            # The settings file stays outside every directory a worker writes.
            self.assertEqual([args[i + 1] for i, a in enumerate(args) if a == "--add-dir"], [str(root / "methods"), str(attempt / "scratch")])
            # Denials name each path as given and as resolved (macOS /var is a link to /private/var).
            forms = lambda paths: list(dict.fromkeys(str(form) for path in paths for form in (path, path.resolve())))
            paths = forms((root / "methods", *gits))
            self.assertEqual(settings["sandbox"], sandbox(call, auto=True))
            self.assertEqual(settings["sandbox"]["filesystem"]["denyWrite"], paths)
            self.assertTrue(settings["permissions"]["blockWritesOutsideWorkingDirectories"])
            self.assertTrue(settings["permissions"]["blockReadsOutsideWorkingDirectories"])
            self.assertEqual(settings["permissions"]["deny"], [*(f"Edit(/{path}/**)" for path in forms((root / "methods",))),
                                                               *(rule for path in forms(gits) for rule in (f"Edit(/{path})", f"Edit(/{path}/**)"))])
            # A sandbox that cannot start ends the call before model work, so the dispatcher tries the next model.
            refused = CompletedProcess(args, 1, "", "Error: sandbox required but unavailable: bwrap: setting up uid map: Permission denied\n"
                                                    "  Refusing to start without a working sandbox (see sandbox.failIfUnavailable).\n")
            self.assertEqual(adapter.read(refused, call).kind, "cli_missing")
            self.assertEqual(adapter.read(refused, Call("prompt", "{}", False, attempt)).kind, "failed")
            readonly = adapter.command(Call("prompt", "{}", False, attempt, read_roots=(root / "methods",)))
            self.assertNotIn("sandbox", json.loads(Path(readonly[readonly.index("--settings") + 1]).read_text()))
            # A readonly call that has Bash runs it in the same sandbox, and only its granted commands run.
            review = Call("prompt", "{}", False, attempt, checks=(DIFF,), read_roots=(root / "methods",), git_dirs=gits)
            with patch("foreman.adapters.claude.platform.system", return_value="Darwin"), claude_cli():
                args = adapter.command(review)
            settings = json.loads(Path(args[args.index("--settings") + 1]).read_text())
            self.assertEqual(settings["sandbox"], sandbox(review, auto=False))
            self.assertEqual(settings["permissions"]["allow"], [f"Bash({DIFF})"])
            self.assertEqual(adapter.read(refused, review).kind, "cli_missing")

    def test_the_verifier_drives_the_app_from_a_networkless_sandbox_without_edit_tools(self):
        adapter = ClaudeAdapter(ModelSpec.parse("claude-opus-5-5-high"))
        with TemporaryDirectory() as directory:
            root, attempt = Path(directory), Path(directory) / "attempt"
            attempt.mkdir()
            gits = (root / "repo/.git", root / "clone/.git")
            call = Call("prompt", "{}", False, attempt, read_roots=(root / "methods",), drive=True, git_dirs=gits)
            with patch("foreman.adapters.claude.platform.system", return_value="Darwin"), claude_cli():
                args = adapter.command(call)
            settings = json.loads(Path(args[args.index("--settings") + 1]).read_text())
            self.assertEqual(args[args.index("--tools") + 1], "Read,Glob,Grep,Bash")
            self.assertEqual([args[i + 1] for i, a in enumerate(args) if a == "--add-dir"], [str(root / "methods"), str(attempt / "scratch")])
            forms = lambda paths: list(dict.fromkeys(str(form) for path in paths for form in (path, path.resolve())))
            self.assertEqual(settings["sandbox"], sandbox(call, auto=True))
            # No Edit or Write is granted, so the write block has nothing to govern.
            self.assertEqual(settings["permissions"], {"allow": [], "deny": [f"Edit(/{path}/**)" for path in forms((root / "methods",))],
                                                       "blockReadsOutsideWorkingDirectories": True})
            refused = CompletedProcess(args, 1, "", "Error: sandbox required but unavailable: sandbox-exec missing\n")
            self.assertEqual(adapter.read(refused, call).kind, "cli_missing")
            with patch("foreman.adapters.claude.platform.system", return_value="Windows"), self.assertRaises(CapabilityUnavailable):
                adapter.command(call)
            # No capture shows Codex confining a verifier yet, and Grok confines nothing.
            for other in (CodexAdapter(ModelSpec.parse("gpt-6.1-sol-high")), GrokAdapter(ModelSpec.parse("grok-4.7-high"))):
                with self.subTest(other=type(other).__name__), self.assertRaises(CapabilityUnavailable):
                    other.command(call)

    @patch("foreman.adapters.claude.sandboxed")
    def test_the_verifier_keeps_its_attempt_scratch_and_names_it_for_captures(self, _confinable):
        with TemporaryDirectory() as directory, patch.dict("foreman.worker.STAGES", {"verify": STAGES["review"]}):
            root = Path(directory).resolve()
            checkout, state_dir = root / "checkout", root / "state"
            state_dir.mkdir()
            subprocess.run(["git", "init", "-q", str(checkout)], check=True)
            seen = []
            def runner(args, cwd, env):
                seen.append((args, json.loads(Path(args[args.index("--settings") + 1]).read_text()), env))
                return fixture_process(args, "success", 0)
            state = {"run": RunState(repo="Org/App", issue=7)}
            outcome = CliWorker(checkout, runner, scratch_root=state_dir)("verify", ModelSpec.parse("claude-opus-5-5-high"), state, "verify")
            self.assertEqual(outcome.kind, "ran")
            args, settings, env = seen[0]
            # The scratch Bash writes outlives the call under its run's own directory; Claude's short temp directory does not.
            run_dir = state_dir / "foreman-verify" / "Org-App-7"
            [kept] = run_dir.iterdir()
            scratch = kept / "scratch"
            self.assertTrue(scratch.is_dir())
            self.assertEqual(env["TMPDIR"], str(scratch))
            self.assertFalse(Path(env["CLAUDE_CODE_TMPDIR"]).exists())
            self.assertIn(f"Write scratch and captures under {scratch} and name each capture's path as its evidence.", args[-1])
            self.assertIn("Bash runs sandboxed with no network.", args[-1])
            self.assertNotIn("Do not return the scratch path as evidence.", args[-1])
            self.assertEqual(settings["sandbox"]["filesystem"]["denyWrite"][0], str(checkout / ".git"))
            # Every exit before launch leaves no attempt directory behind: a model that cannot drive the app,
            # a missing executable, and an error before the call.
            def missing(args, cwd, env):
                raise FileNotFoundError(args[0])
            for model, run in (("gpt-6.1-sol-high", runner), ("claude-opus-5-5-high", missing)):
                with self.subTest(model=model):
                    refused = CliWorker(checkout, run, scratch_root=state_dir)("verify", ModelSpec.parse(model), state, "verify")
                    self.assertEqual((refused.kind, list(run_dir.iterdir()), len(seen)), ("cli_missing", [kept], 1))
            # No short temp directory for Claude, after the attempt directory exists.
            with patch("foreman.worker.TemporaryDirectory", side_effect=OSError("no /tmp")):
                outcome = CliWorker(checkout, runner, scratch_root=state_dir)("verify", ModelSpec.parse("claude-opus-5-5-high"), state, "verify")
            self.assertEqual((outcome.kind, list(run_dir.iterdir()), len(seen)), ("cli_missing", [kept], 1))
            # Errors before and after the attempt directory exists.
            with self.assertRaises(ValueError):
                CliWorker(checkout, runner, scratch_root=state_dir)("verify", ModelSpec.parse("claude-opus-5-5-high"),
                                                                    {**state, "dispatch_plan": {"bad": 1}}, "verify")
            with patch("foreman.worker.ClaudeAdapter.command", side_effect=RuntimeError("adapter broke")), self.assertRaises(RuntimeError):
                CliWorker(checkout, runner, scratch_root=state_dir)("verify", ModelSpec.parse("claude-opus-5-5-high"), state, "verify")
            self.assertEqual(list(run_dir.iterdir()), [kept])

    def test_captured_verifier_reaches_no_network_and_writes_only_the_checkout_and_scratch(self):
        capture = json.loads((Path(__file__).parent / "fixtures/claude/permissions/verify.json").read_text())
        self.assertEqual((capture["returncode"], capture["commits"]), (0, 1))
        self.assertEqual(capture["argv"][capture["argv"].index("--tools") + 1], "Read,Glob,Grep,Bash")
        self.assertEqual(capture["stdout"]["permission_denials"], [])
        content = [c for row in capture["transcript"] for c in row["message"]["content"] if isinstance(c, dict)]
        text = lambda result: result["content"] if isinstance(result["content"], str) else " ".join(p.get("text", "") for p in result["content"])
        bash = {use["input"]["command"]: (result.get("is_error", False), text(result)) for use in content
                if use.get("type") == "tool_use" and use.get("name") == "Bash" for result in content if result.get("tool_use_id") == use["id"]}
        # The sandbox refuses a loopback listener, every host, and every write outside the checkout and scratch.
        self.assertIn("Operation not permitted", bash["sh serve.sh"][1])
        self.assertIn("curl=7", bash["sh serve.sh"][1])
        self.assertEqual({command for command, (failed, _) in bash.items() if not failed},
                         {"sh serve.sh", "touch <probe>/attempt/scratch/capture.txt", "touch build-output"})
        for command in ("rm <probe>/methods/method.md", "rm <probe>/outside.txt", "touch <probe>/clone/.git/probe", "git commit -qm x"):
            self.assertIn("Operation not permitted", bash[command][1])
        self.assertIn("not on the allow list", bash["curl -sS --max-time 10 https://example.com"][1])
        files = capture["files"]
        self.assertEqual((files["repo/build-output"], files["attempt/scratch/capture.txt"]), ("", ""))
        self.assertEqual((files["methods/method.md"], files["outside.txt"]), ("method original\n", "outside\n"))
        self.assertNotIn("clone/.git/probe", files)

    def test_an_unconfinable_host_has_no_claude_shell_before_launch(self):
        adapter = ClaudeAdapter(ModelSpec.parse("claude-opus-5-5-high"))
        for call in (Call("prompt", "{}", True, Path("/nonexistent")), Call("prompt", "{}", False, Path("/nonexistent"), checks=(DIFF,))):
            for system, tools, reason in (("Windows", {}, "the Bash sandbox is unsupported"), ("Linux", {"socat": "/usr/bin/socat"}, "bwrap missing")):
                with self.subTest(system=system, edit=call.edit), patch("foreman.adapters.claude.platform.system", return_value=system), \
                        patch("foreman.adapters.claude.shutil.which", side_effect=tools.get), self.assertRaisesRegex(CapabilityUnavailable, reason):
                    adapter.command(call)
            # A CLI older than every read-reach capture, or none at all, cannot be shown to confine the shell.
            for version, reason in (("2.1.331", "Claude Code 2.1.331 is older than 2.1.332"), ("garbled", "'garbled \\(Claude Code\\)' is older")):
                with self.subTest(version=version), patch("foreman.adapters.claude.platform.system", return_value="Darwin"), \
                        claude_cli(version), self.assertRaisesRegex(CapabilityUnavailable, reason):
                    adapter.command(call)
            with patch("foreman.adapters.claude.platform.system", return_value="Darwin"), patch.dict(os.environ, PATH=""), \
                    self.assertRaisesRegex(CapabilityUnavailable, "claude executable is unavailable"):
                adapter.command(call)
        with patch("foreman.adapters.claude.platform.system", return_value="Windows"), TemporaryDirectory() as directory:
            self.assertIn("--tools", adapter.command(Call("prompt", "{}", False, Path(directory))))

    @patch("foreman.adapters.claude.sandboxed")
    def test_editing_claude_gets_its_own_short_temp_directory_and_the_linked_worktree_git_dirs(self, _confinable):
        with TemporaryDirectory() as directory:
            clone, checkout = Path(directory).resolve() / "clone", Path(directory).resolve() / "checkout"
            clone.mkdir()
            for args in (["init", "-q"], ["-c", "user.name=T", "-c", "user.email=t@example.test", "commit", "-q", "--allow-empty", "-m", "base"],
                         ["worktree", "add", "-q", str(checkout)]):
                subprocess.run(["git", *args], cwd=clone, check=True)
            # A worker started through a link denies the linked path and its target.
            (Path(directory) / "link").symlink_to(checkout)
            linked = []
            def linked_runner(args, cwd, env):
                linked.append(json.loads(Path(args[args.index("--settings") + 1]).read_text())["sandbox"]["filesystem"]["denyWrite"])
                return fixture_process(args, "success", 0)
            CliWorker(Path(directory) / "link", linked_runner, scratch_root=Path(directory))("implement", ModelSpec.parse("claude-opus-5-5-high"),
                                                              {"checks": ChecksState(check="sh check.sh")}, "implement")
            self.assertEqual(linked[0][:2], [str(Path(directory).absolute() / "link/.git"), str(checkout / ".git")])
            seen = []
            def runner(args, cwd, env):
                seen.append((json.loads(Path(args[args.index("--settings") + 1]).read_text()), env))
                return fixture_process(args, "success", 0)
            CliWorker(checkout, runner, scratch_root=Path(directory))("implement", ModelSpec.parse("claude-opus-5-5-high"), {"checks": ChecksState(check="sh check.sh")}, "implement")
            settings, env = seen[0]
            self.assertEqual(settings["sandbox"]["filesystem"]["denyWrite"],
                             [str(checkout / ".git"), str(clone / ".git/worktrees/checkout"), str(clone / ".git")])
            # Claude Code keeps a per-user temp directory under CLAUDE_CODE_TMPDIR only while the path fits its AF_UNIX sockets.
            # Resolved, so the sandbox opens it without the /tmp link; the shell alone reads and writes it.
            self.assertTrue(env["CLAUDE_CODE_TMPDIR"].startswith(f"{Path('/tmp').resolve()}/fw"))
            self.assertLessEqual(len(env["CLAUDE_CODE_TMPDIR"]), 24)
            self.assertIn(env["CLAUDE_CODE_TMPDIR"], settings["sandbox"]["filesystem"]["allowRead"])
            self.assertFalse(Path(env["CLAUDE_CODE_TMPDIR"]).exists())
            # A review granted its diff has Bash too, so it gets one; a call with no Bash gets none.
            plan = {"models": [], "instruction": "review", "methods": [], "read_roots": [], "target": None,
                    "scope": {"command": DIFF, "inline": []}}
            CliWorker(checkout, runner, scratch_root=Path(directory))("review", ModelSpec.parse("claude-opus-5-5-high"),
                                                                      {"dispatch_plan": plan}, "review")
            self.assertTrue(seen[-1][1]["CLAUDE_CODE_TMPDIR"].startswith(f"{Path('/tmp').resolve()}/fw"))
            self.assertIn(str(checkout / ".git"), seen[-1][0]["sandbox"]["filesystem"]["allowRead"])
            CliWorker(checkout, runner, scratch_root=Path(directory))("triage", ModelSpec.parse("claude-opus-5-5-high"), {"plan": PlanState()}, "triage")
            self.assertNotIn("CLAUDE_CODE_TMPDIR", seen[-1][1])
            with patch("foreman.worker.TemporaryDirectory", side_effect=[TemporaryDirectory(), OSError("no /tmp")]):
                outcome = CliWorker(checkout, runner, scratch_root=Path(directory))("implement", ModelSpec.parse("claude-opus-5-5-high"), {"checks": ChecksState(check="sh check.sh")}, "implement")
            self.assertEqual((outcome.kind, len(seen)), ("cli_missing", 3))
            # The checkout's own .git decides, never an inherited GIT_DIR, and an unreadable one fails closed.
            with patch.dict(os.environ, {"GIT_DIR": str(clone / ".git")}):
                self.assertEqual(git_dirs(checkout), (checkout / ".git", clone / ".git/worktrees/checkout", clone / ".git"))
            (checkout / ".git").write_text("gitdir: /nonexistent\n")
            with self.assertRaises(CapabilityUnavailable):
                git_dirs(checkout)
            self.assertEqual(git_dirs(clone.parent), ())

    def test_readonly_has_no_edit_tools_or_allow_rules(self):
        adapter = ClaudeAdapter(ModelSpec.parse("claude-opus-5-5-high"))
        with TemporaryDirectory() as directory:
            args = adapter.command(Call("prompt", "{}", False, Path(directory)))
            permissions = json.loads(Path(args[args.index("--settings") + 1]).read_text())
        self.assertEqual(args[args.index("--tools") + 1], "Read,Glob,Grep")
        self.assertEqual(permissions["permissions"]["allow"], [])
        self.assertIn("--restricted", args)
        self.assertIn("--strict-mcp-config", args)
        self.assertEqual(args[args.index("--permission-mode") + 1], "dontAsk")

    def test_spec_read_first_rule_reaches_worker(self):
        instructions = []
        script = ScriptHandler(triage="plan", spec_gate="needs_human")
        def handler(node, model, state, instruction):
            if node == "spec":
                instructions.append(instruction)
            return script(node, model, state, instruction)
        with skill_fixture() as (config, scratch):
            run_delivery(handler, repo="org/repo", request="Change", config=config, scratch=scratch)
        for rule in ("Read the target checkout at the named base", "pinned base:", "grounded questions", "target_repository: org/repo"):
            self.assertIn(rule, instructions[0])

    @patch("foreman.adapters.claude.sandboxed")
    def test_review_grants_only_the_bound_scope_command(self, _confinable):
        args_seen = []
        def runner(args, cwd, env):
            self.assertEqual(args[args.index("--tools") + 1], "Read,Glob,Grep,Bash")
            args_seen.append(json.loads(Path(args[args.index("--settings") + 1]).read_text()))
            return fixture_process(args, "success", 0)
        plan = {"models": [], "instruction": "review", "methods": [], "read_roots": [],
                "target": None, "scope": {"command": "  # head history base\n  git diff --no-color abc...def --  \n\n", "inline": []}}
        CliWorker(Path.cwd(), runner, scratch_root=Path(self.enterContext(TemporaryDirectory())))("review", ModelSpec.parse("claude-opus-5-5-high"),
                                     {"checks": ChecksState(check="sh check.sh"), "dispatch_plan": plan}, "review")
        args = args_seen[0]
        self.assertEqual(args["permissions"]["allow"],
                         ["Bash(git diff --no-color abc...def --)"])

    @patch("foreman.adapters.claude.sandboxed")
    def test_multiline_implementation_check_stays_one_grant(self, _confinable):
        check = "# implementation check\npython check.py\npython other.py"
        def runner(args, cwd, env):
            self.assertEqual(args[args.index("--tools") + 1], "Read,Glob,Grep,Bash,Edit,Write")
            permissions = json.loads(Path(args[args.index("--settings") + 1]).read_text())["permissions"]
            self.assertEqual(permissions["allow"], [f"Bash({check})", "Edit", "Write"])
            return fixture_process(args, "success", 0)
        result = CliWorker(Path.cwd(), runner, scratch_root=Path(self.enterContext(TemporaryDirectory())))("implement", ModelSpec.parse("claude-opus-5-5-high"),
                                              {"checks": ChecksState(check=check)}, "implement")
        self.assertEqual(result.kind, "ran")

    def test_captured_large_review_diff_reaches_claude_whole(self):
        capture = json.loads((Path(__file__).parent / "fixtures/claude/permissions/large-diff.json").read_text())
        self.assertEqual(capture["returncode"], 0)
        self.assertEqual(capture["stdout"]["permission_denials"], [])
        self.assertGreater(capture["review"]["patch_bytes"], 400_000)
        self.assertEqual(capture["review"]["last_line"], "+SENTINEL-END-OF-DIFF")
        content = [c for row in capture["transcript"] for c in row["message"]["content"]]
        results = {use["name"]: next(r for r in content if r.get("tool_use_id") == use["id"])
                   for use in content if use.get("type") == "tool_use"}
        bash = results["Bash"]["content"]
        # Bash hands over a preview and the saved full output; restricted Read and Grep open that file.
        saved = re.search(r"Full output saved to: (\S+\.txt)", bash)
        assert saved is not None
        self.assertNotIn("SENTINEL-END-OF-DIFF", bash)
        for tool in ("Grep", "Read"):
            self.assertFalse(results[tool].get("is_error"))
        self.assertIn("+SENTINEL-END-OF-DIFF", results["Grep"]["content"])
        self.assertIn("+SENTINEL-END-OF-DIFF", results["Read"]["content"])
        self.assertEqual({use["input"].get("file_path") or use["input"].get("path") for use in content
                          if use.get("type") == "tool_use" and use["name"] in ("Grep", "Read")}, {saved.group(1)})
        self.assertTrue(capture["stdout"]["structured_output"]["reached_rest"])

    def test_captured_permissions_and_effects(self):
        folder = Path(__file__).parent / "fixtures/claude/permissions"
        for mode in ("edit", "inherited", "compound", "readonly"):
            capture = json.loads((folder / f"{mode}.json").read_text())
            self.assertEqual(capture["returncode"], 0)
            files = capture["files"]
            self.assertEqual(files["methods/method.md"], "method original\n")
            self.assertNotIn("repo/f", files)
            denied = capture["stdout"]["permission_denials"]
            content = [c for row in capture["transcript"] for c in row["message"]["content"] if isinstance(c, dict)]
            if mode == "readonly":
                commands = {d["tool_input"].get("command") for d in denied if d["tool_name"] == "Bash"}
                self.assertNotIn("repo/x", files)
                self.assertIn("touch x", commands)
                self.assertEqual(files["repo/source.py"], "value = 1\n")
                self.assertNotIn("scratch/scratch.txt", files)
                self.assertIn("git diff --output=f", commands)
                # The exact command review_diff emits, granted by one exact Bash rule, returns the whole two-commit patch.
                review = capture["review"]
                granted = ReviewScope(review["command"], None).commands
                self.assertEqual(review["settings"]["permissions"]["allow"], [f"Bash({granted[0]})"])
                self.assertEqual(len(added_lines(review["patch"]) or ()), 3)
                self.assertNotIn(granted[0], commands)
                output = [result["content"] for use in content if use.get("type") == "tool_use" and use["input"].get("command") == granted[0]
                          for result in content if result.get("tool_use_id") == use["id"] and not result.get("is_error")]
                self.assertEqual(output, [review["patch"].rstrip("\n")])
            else:
                # Sandboxed Bash runs without a prompt; the OS boundary refuses a write or connection as a failed tool result.
                # The inherited probe's project settings try to lift the sandbox; restricted mode ignores them.
                def text(result):
                    body = result.get("content")
                    return body if isinstance(body, str) else " ".join(part.get("text", "") for part in body or [])
                bash = {use["input"]["command"]: (result.get("is_error", False), text(result)) for use in content
                        if use.get("type") == "tool_use" and use.get("name") == "Bash"
                        for result in content if result.get("tool_use_id") == use["id"]}
                denied_write, denied_host = "Operation not permitted", "not on the allow list"
                self.assertEqual({command: evidence for command, (failed, output) in bash.items() if failed
                                  for evidence in (denied_write, denied_host, "Could not resolve host") if evidence in output}, {
                    "rm <probe>/methods/method.md": denied_write, "rm <probe>/outside.txt": denied_write,
                    "touch <probe>/clone/.git/probe": denied_write, "touch /tmp/claude-<uid>/foreman-probe": denied_write,
                    "git commit -qm x": denied_write, "gh pr list -R octocat/Hello-World": denied_host,
                    "curl -sS --max-time 10 https://example.com": denied_host,
                    "curl -sS --noproxy '*' --max-time 10 https://example.com": "Could not resolve host"})
                check = "touch first && touch second" if mode == "compound" else "sh check.sh"
                self.assertEqual({command for command, (failed, _) in bash.items() if not failed}, {
                    check, "touch x", "rm doomed.py", "rm <probe>/attempt/scratch/scratch-doomed.txt",
                    'touch "$TMPDIR/temp-probe" && printenv TMPDIR', "python -c 1"})
                self.assertEqual(bash['touch "$TMPDIR/temp-probe" && printenv TMPDIR'][1].strip(), "<temp>/claude-<uid>")
                self.assertEqual(files["repo/source.py"], "value = 2\n")
                self.assertEqual(files["attempt/scratch/scratch.txt"], "scratch")
                self.assertIn('name = "after"', files["repo/pyproject.toml"])
                self.assertEqual(files["repo/.github/workflows/ci.yml"], "name: after\n")
                self.assertEqual(files["outside.txt"], "outside\n")
                self.assertIn("<temp>/claude-<uid>/temp-probe", files)
                for gone in ("repo/doomed.py", "attempt/scratch/scratch-doomed.txt", "clone/.git/probe", "/tmp/claude-<uid>/foreman-probe"):
                    self.assertNotIn(gone, files)
                self.assertEqual(capture["commits"], 1)
                self.assertTrue(any(d["tool_name"] == "Edit" and "methods/" in d["tool_input"]["file_path"] for d in denied))
                for marker in (("first", "second") if mode == "compound" else ("checked",)):
                    self.assertIn(f"repo/{marker}", files)
            reads = [c for c in content if c.get("type") == "tool_use" and c.get("name") == "Read" and c["input"]["file_path"].endswith("methods/method.md")]
            self.assertTrue(reads)
            self.assertTrue(any(c.get("type") == "tool_result" and c.get("tool_use_id") == reads[0]["id"] and not c.get("is_error") for c in content))

    def test_read_reach_captures_are_the_adapters_own_settings(self):
        """Each profile's capture recorded the settings ClaudeAdapter.command builds for the probe's layout today."""
        base = Path("/reach-base")
        runs, clone = base / "state/foreman/runs/probe/repo", base / "state/foreman/checkouts/probe/repo"
        attempt = base / "state/foreman/foreman-worker-probe"
        gits = (runs / "7/.git", clone / ".git/worktrees/7", clone / ".git")
        for mode in ("readonly", "edit", "verify"):
            capture = json.loads((CLAUDE / f"read-reach-{mode}.json").read_text())
            self.assertGreaterEqual(tuple(map(int, capture["version"].split()[0].split("."))), CAPTURED_VERSION)
            for variant, taken in capture["variants"].items():
                checks = tuple(rule.removeprefix("Bash(").removesuffix(")") for rule in taken["settings"]["permissions"]["allow"]
                               if rule.startswith("Bash("))
                call = Call("", "{}", mode == "edit", attempt, checks=checks, drive=mode == "verify",
                            read_roots=(base / "methods", runs / "7.related/probe/lib"), git_dirs=gits,
                            shell_temp=Path("/private/tmp/fw<call>"))
                # The capture host found git under /opt/homebrew/bin and ran macOS.
                with self.subTest(mode=mode, variant=variant), patch("foreman.adapters.claude.platform.system", return_value="Darwin"), \
                        patch("foreman.adapters.claude.shutil.which", return_value="/opt/homebrew/bin/git"), claude_cli(), \
                        patch.object(Path, "write_text") as written:
                    ClaudeAdapter(ModelSpec.parse("claude-haiku-5-5")).command(call)
                    built = written.call_args.args[0].replace("/private/tmp/fw<call>", "/tmp/fw<call>").replace(str(base), "<probe>")
                    self.assertEqual(json.loads(built), taken["settings"])

    def test_read_reach_captures_deny_every_outside_read_and_pass_the_work(self):
        # What Claude Code and its shell set themselves: the session, its sandbox proxy and the shell's own.
        claude_own = {
                      "AI_AGENT", "ALL_PROXY", "CLAUDECODE", "CLAUDE_CODE_CHILD_NOTES_DIR", "CLAUDE_CODE_CHILD_SESSION",
                      "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_EXECPATH", "CLAUDE_CODE_SESSION_ATTENDED",
                      "CLAUDE_CODE_SESSION_ID", "CLAUDE_EFFORT", "CLAUDE_PID", "CLAUDE_TMPDIR", "CLOUDSDK_PROXY_ADDRESS",
                      "CLOUDSDK_PROXY_PASSWORD", "CLOUDSDK_PROXY_PORT", "CLOUDSDK_PROXY_TYPE", "CLOUDSDK_PROXY_USERNAME",
                      "COREPACK_ENABLE_AUTO_PIN", "DOCKER_HTTPS_PROXY", "DOCKER_HTTP_PROXY", "FTP_PROXY",
                      "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_KEY_1", "GIT_CONFIG_KEY_2", "GIT_CONFIG_KEY_3",
                      "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_VALUE_0", "GIT_CONFIG_VALUE_1", "GIT_CONFIG_VALUE_2",
                      "GIT_CONFIG_VALUE_3", "GIT_EDITOR", "GIT_SSH_COMMAND", "GRPC_PROXY", "HTTPS_PROXY", "HTTP_PROXY",
                      "JAVA_TOOL_OPTIONS", "LOGNAME", "NO_PROXY", "NoDefaultCurrentDirectoryInExePath", "OLDPWD", "PWD",
                      "RSYNC_PROXY", "SANDBOX_RUNTIME", "SHELL", "SHLVL", "SRT_SSH_PROXY_COMMAND", "TMPPREFIX", "_",
                      "__CF_USER_TEXT_ENCODING", "all_proxy", "ftp_proxy", "grpc_proxy", "http_proxy", "https_proxy",
                      "no_proxy"}
        with patch.dict(os.environ, {"PATH": "/bin", "HOME": "/home"}, clear=True):
            built = {*command_environment(), *ClaudeAdapter.env, "TMPDIR", "CLAUDE_CODE_TMPDIR"}
        work = {"readonly": {"git diff", "wc -c source.py", "git status --porcelain", "git log -1 --format=%H", "cat <probe>/methods/method.md",
                             "cat <probe>/state/foreman/runs/probe/repo/7.related/probe/lib/lib.py"}}
        for mode in ("readonly", "edit", "verify"):
            capture = json.loads((CLAUDE / f"read-reach-{mode}.json").read_text())
            for variant, taken in capture["variants"].items():
                with self.subTest(mode=mode, variant=variant):
                    self.assertEqual((taken["returncode"], taken["envelope"]["structured_output"]), (0, {"done": True}))
                    probes = taken["probes"]
                    for probe in probes:
                        target, kind = str(probe["target"]), probe["kind"]
                        if kind == "work":
                            gated = mode == "readonly" and probe["tool"] == "Bash"
                            if not gated or any(target.startswith(command) for command in work["readonly"]):
                                self.assertFalse(probe["is_error"], target)
                        elif kind == "system":
                            self.assertEqual(probe["bytes"], 213 if not (mode == "readonly" and variant != "os-only") else None)
                        elif kind == "environment":
                            if "names" in probe:
                                # Foreman's built names and Claude Code's own; nothing else from the operator's environment.
                                names = set(probe["names"])
                                self.assertEqual({name for name in names - claude_own if not name.startswith("LC_")}, built)
                        elif probe["tool"] != "Bash":
                            self.assertTrue(probe["is_error"], target)
                        else:
                            # A byte count of a real or planted file never comes back; the OS or the permission check refuses it.
                            self.assertIn(probe["bytes"], (None, 0), target)
                            # Git names the file it could not open, without the sandbox's own words.
                            refusal = "could not be found in the keychain" if kind == "keychain" else \
                                "denied" if mode == "readonly" and variant != "os-only" else \
                                "Could not access" if target.startswith("git diff --no-index") else "Operation not permitted"
                            self.assertIn(refusal, probe["result"], target)
                    if variant == "os-only":
                        self.assertTrue(all("via" in p for p in probes if p["tool"] == "Bash" and p["kind"] not in ("work", "environment")))

    def test_grok_fails_before_launch_for_editing_and_readonly(self):
        from unittest.mock import Mock, patch
        for system in ('Darwin', 'Linux', 'Windows'):
            for stage in ('implement', 'review'):
                runner = Mock()
                with patch('foreman.adapters.grok.platform.system', return_value=system):
                    with self.assertRaises(CapabilityUnavailable):
                        GrokAdapter(ModelSpec.parse('grok-4.7-high')).command(Call('probe', '{}', stage == 'implement', Path.cwd()))
                    outcome = CliWorker(Path.cwd(), runner, scratch_root=Path(self.enterContext(TemporaryDirectory())))(stage, ModelSpec.parse('grok-4.7-high'), {'checks': ChecksState()}, 'probe')
                self.assertEqual(outcome.kind, 'cli_missing')
                self.assertIn(system, outcome.detail)
                self.assertIn('editing requires an isolated worker launcher' if stage == 'implement' else 'temp writes', outcome.detail)
                self.assertEqual('child network isolation is unavailable' in outcome.detail, system == 'Darwin' and stage == 'review')
                runner.assert_not_called()

    def test_codex_current_commands_enforce_the_policy(self):
        adapter = CodexAdapter(ModelSpec.parse('gpt-6.1-sol-high'))
        with TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            for stage in ('implement', 'review', 'triage'):
                for roots in ((), (root / 'methods-🦊',)):
                    def run(args, cwd, env):
                        for flag in ('--ignore-user-config', '--ignore-rules'):
                            self.assertIn(flag, args)
                        overrides = [args[i + 1] for i, arg in enumerate(args) if arg == '-c']
                        self.assertIn('approval_policy=never', overrides)
                        self.assertIn('default_permissions=foreman', overrides)
                        policy = tomllib.loads(next(v for v in overrides if v.startswith('permissions.foreman=')))['permissions']['foreman']
                        self.assertEqual(policy['extends'], ':read-only')
                        self.assertIs(policy['network']['enabled'], False)
                        filesystem = {str(r): 'read' for r in roots}
                        if stage == 'implement':
                            filesystem.update({':workspace_roots': {'.': 'write', '.git': 'read', '.codex': 'read'}, env['TMPDIR']: 'write'})
                        self.assertEqual(policy['filesystem'], filesystem)
                        events = codex_events()
                        events[-2:-2] = [{'type': 'item.completed', 'item': {'type': 'command_execution', 'command': DIFF,
                                                                             'exit_code': 0, 'status': 'completed'}}]
                        return codex_process(events)
                    scope = {'command': DIFF, 'inline': []} if stage == 'review' else None
                    plan = {'models': [], 'instruction': '', 'methods': [], 'read_roots': [str(r) for r in roots], 'target': None, 'scope': scope}
                    self.assertEqual(CliWorker(root, run, scratch_root=Path(self.enterContext(TemporaryDirectory())))(stage, adapter.model, {'checks': ChecksState(), 'dispatch_plan': plan}, 'probe').kind, 'ran')
                    args = adapter.command(Call('probe', '{}', stage == 'implement', root, read_roots=roots))
                    run(args, root, {'TMPDIR': str(root / 'scratch')})

    def test_codex_kernel_captures_enforce_writes_and_network(self):
        for mode in ('edit', 'readonly'):
            capture = json.loads((Path(__file__).parent / f'fixtures/codex/permissions/{mode}.json').read_text())
            self.assertEqual(capture['returncode'], 0)
            adapter = CodexAdapter(ModelSpec.parse('gpt-6.1-sol-high'))
            with TemporaryDirectory() as folder:
                root = Path(folder).resolve()
                roots = tuple(root / name for name in ('methods', 'scratch/methods', 'repo/methods'))
                call = Call(capture['argv'][-1].replace('<probe>', str(root)), '{}', mode == 'edit', root, read_roots=roots)
                # Every Codex call keeps its rollout so a quota stop is charged what it used (#242). Session
                # persistence is not a sandbox control; re-record both captures when they are refreshed.
                captured = [arg for arg in capture['argv'] if arg != '--ephemeral']
                self.assertEqual([arg.replace(str(root), '<probe>') for arg in adapter.command(call)], captured)
            outcome = adapter.read(CompletedProcess(capture['argv'], capture['returncode'], capture['stdout'], capture['stderr']), call)
            self.assertEqual((outcome.kind, outcome.output), ('ran', {'done': True}))
            roots = ('methods', 'scratch/methods', 'repo/methods')
            expected = {'sh check.sh', 'echo outside > <probe>/outside/escape', 'echo scratch > <probe>/scratch/shell.txt',
                        'git -c user.name=Probe -c user.email=probe@example.test commit --allow-empty -m probe',
                        'curl --noproxy "*" --connect-timeout 3 -k -I https://1.1.1.1', 'gh pr list'}
            expected.update(f'{prefix} <probe>/{r}/method.md' for r in roots for prefix in ('cat', 'echo changed >'))
            self.assertEqual({row['command'] for row in capture['kernel']}, expected)
            self.assertEqual(set(capture['isolation_controls']), {'--ignore-user-config', '--ignore-rules'})
            for flag, control in capture['isolation_controls'].items():
                self.assertNotIn(flag, control['argv'])
                self.assertNotEqual(control['returncode'], 0)
                self.assertIn('invalid-inherited-provider' if flag == '--ignore-user-config' else 'rule', control['stderr'])
            self.assertFalse(capture['commit_succeeded'])
            for row in capture['kernel']:
                command = row['command']
                allowed = command.startswith('cat ') or (mode == 'edit' and (
                    command == 'sh check.sh' or command.startswith('echo scratch ')))
                self.assertEqual(row['returncode'] == 0, allowed, row)
            for path in ('methods/method.md', 'scratch/methods/method.md', 'repo/methods/method.md'):
                self.assertEqual(capture['files'][path], 'original\n')
            self.assertNotIn('outside/escape', capture['files'])
            if mode == 'readonly':
                self.assertEqual(capture['read_outside'], {'command': 'cat <probe>/outside/read-probe', 'returncode': 0,
                                                           'stdout': 'synthetic outside file\n', 'stderr': ''})
            self.assertEqual('repo/checked' in capture['files'], mode == 'edit')
            self.assertEqual(capture['unavailable_policy']['argv'][:2], ['codex', 'exec'])
            self.assertNotEqual(capture['unavailable_policy']['returncode'], 0)
            self.assertEqual(capture['unavailable_policy']['stdout'], '')
            self.assertNotEqual(capture['sandbox_unavailable']['returncode'], 0)
            self.assertEqual(capture['sandbox_unavailable']['stdout'], '')

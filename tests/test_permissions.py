import tests
from foreman.state import PlanState, ChecksState, RunState

import json
import os
from pathlib import Path
import unittest
import tomllib
from tempfile import TemporaryDirectory
import subprocess
from subprocess import CompletedProcess
from unittest.mock import patch

from foreman.adapters import Call, CapabilityUnavailable, Outcome
from foreman.adapters.claude import ClaudeAdapter
from foreman.adapters.codex import CodexAdapter
from foreman.adapters.grok import GrokAdapter
from foreman.config import ModelSpec, NodeConfig, load_config
from foreman.delivery import run_delivery
from foreman.dry_run import ScriptHandler, SCRIPTED_USAGE
from foreman.skill_rules import PACKAGE_SKILLS, export_skills, stage_methods
from foreman.stages import STAGES
from foreman.worker import CliWorker, git_dirs
from skill_fixture import skill_fixture
from test_adapters import fixture_process
from test_runner import EMPTY_SLICE


class PermissionsTest(unittest.TestCase):
    def test_method_paths_are_covered_by_precise_roots(self):
        with skill_fixture() as (config, scratch), TemporaryDirectory() as directory:
            root = export_skills(config.pstack, config.matt_skills, scratch)
            # The worker runs in the target checkout, so its own verify skill needs no extra read root.
            checkout = Path(directory)
            # A repository opts in to each target skill by having it: verify-<app> and formal-<area>.
            for skill in ("verify-board", "formal-recovery"):
                target_skill = checkout / f".claude/skills/{skill}/SKILL.md"
                target_skill.parent.mkdir(parents=True)
                target_skill.write_text("Run the board checks.\n")
            for stage, row in STAGES.items():
                if not row.skills:
                    continue
                for kind in ("feature", "bug_fix", "perf_issue"):
                    state = {"run": RunState(workspace=str(checkout)), "plan": PlanState(work_kind=kind, issue_kind="slice", slice={**EMPTY_SLICE, "stateful": True, "screen": "docs/screens/ui.md", "properties": [{"name": "rule", "constraint": "unique"}]})}
                    text, methods, roots = stage_methods(stage, state, root)
                    self.assertEqual(len(roots), len(set(roots)))
                    self.assertTrue(set(roots) <= {str(root.resolve()), str(PACKAGE_SKILLS.resolve())})
                    instructions = text.split('\n\n') if text else []
                    self.assertEqual(len(instructions), len(methods))
                    for instruction in instructions:
                        path = instruction.split(' of ')[-1].split(' as the method.')[0] if ' of ' in instruction else instruction.removeprefix('Read ').split(' in full')[0]
                        self.assertTrue(Path(path).is_file())
                        self.assertTrue(any(Path(path).is_relative_to(Path(r)) for r in (*roots, checkout)))
                    for skill in ("verify-board", "formal-recovery"):
                        self.assertEqual(f".claude/skills/{skill}/SKILL.md" in methods, stage == "implement")

    def test_first_fix_and_quota_fallback_receive_the_bound_grant(self):
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

                worker = CliWorker(scratch, runner)

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

                result = run_delivery(handler, repo="Example/Engine", request="Change", check="sh check.sh",
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
            with patch("foreman.adapters.claude.platform.system", return_value="Darwin"):
                args = adapter.command(call)
            settings = json.loads(Path(args[args.index("--settings") + 1]).read_text())
            # The settings file stays outside every directory a worker writes.
            self.assertEqual([args[i + 1] for i, a in enumerate(args) if a == "--add-dir"], [str(root / "methods"), str(attempt / "scratch")])
            # Denials name each path as given and as resolved (macOS /var is a link to /private/var).
            forms = lambda paths: list(dict.fromkeys(str(form) for path in paths for form in (path, path.resolve())))
            paths = forms((root / "methods", *gits))
            self.assertEqual(settings["sandbox"], {
                "enabled": True, "autoAllowBashIfSandboxed": True, "failIfUnavailable": True, "allowUnsandboxedCommands": False,
                "network": {"allowedDomains": [], "strictAllowlist": True}, "filesystem": {"denyWrite": paths}})
            self.assertTrue(settings["permissions"]["blockWritesOutsideWorkingDirectories"])
            self.assertEqual(settings["permissions"]["deny"], [*(f"Edit(/{path}/**)" for path in forms((root / "methods",))),
                                                               *(rule for path in forms(gits) for rule in (f"Edit(/{path})", f"Edit(/{path}/**)"))])
            # A sandbox that cannot start ends the call before model work, so the dispatcher tries the next model.
            refused = CompletedProcess(args, 1, "", "Error: sandbox required but unavailable: bwrap: setting up uid map: Permission denied\n"
                                                    "  Refusing to start without a working sandbox (see sandbox.failIfUnavailable).\n")
            self.assertEqual(adapter.read(refused, call).kind, "cli_missing")
            self.assertEqual(adapter.read(refused, Call("prompt", "{}", False, attempt)).kind, "failed")
            readonly = adapter.command(Call("prompt", "{}", False, attempt, read_roots=(root / "methods",)))
            self.assertNotIn("sandbox", json.loads(Path(readonly[readonly.index("--settings") + 1]).read_text()))

    def test_an_unconfinable_host_has_no_editing_claude_before_launch(self):
        adapter = ClaudeAdapter(ModelSpec.parse("claude-opus-5-5-high"))
        call = Call("prompt", "{}", True, Path("/nonexistent"))
        for system, tools, reason in (("Windows", {}, "the Bash sandbox is unsupported"), ("Linux", {"socat": "/usr/bin/socat"}, "bwrap missing")):
            with self.subTest(system=system), patch("foreman.adapters.claude.platform.system", return_value=system), \
                    patch("foreman.adapters.claude.shutil.which", side_effect=tools.get), self.assertRaisesRegex(CapabilityUnavailable, reason):
                adapter.command(call)
        with patch("foreman.adapters.claude.platform.system", return_value="Windows"), TemporaryDirectory() as directory:
            self.assertIn("--tools", adapter.command(Call("prompt", "{}", False, Path(directory))))

    def test_editing_claude_gets_its_own_short_temp_directory_and_the_linked_worktree_git_dirs(self):
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
            CliWorker(Path(directory) / "link", linked_runner)("implement", ModelSpec.parse("claude-opus-5-5-high"),
                                                              {"checks": ChecksState(check="sh check.sh")}, "implement")
            self.assertEqual(linked[0][:2], [str(Path(directory).absolute() / "link/.git"), str(checkout / ".git")])
            seen = []
            def runner(args, cwd, env):
                seen.append((json.loads(Path(args[args.index("--settings") + 1]).read_text()), env))
                return fixture_process(args, "success", 0)
            CliWorker(checkout, runner)("implement", ModelSpec.parse("claude-opus-5-5-high"), {"checks": ChecksState(check="sh check.sh")}, "implement")
            settings, env = seen[0]
            self.assertEqual(settings["sandbox"]["filesystem"]["denyWrite"],
                             [str(checkout / ".git"), str(clone / ".git/worktrees/checkout"), str(clone / ".git")])
            # Claude Code keeps a per-user temp directory under CLAUDE_CODE_TMPDIR only while the path fits its AF_UNIX sockets.
            self.assertTrue(env["CLAUDE_CODE_TMPDIR"].startswith("/tmp/fw"))
            self.assertLessEqual(len(env["CLAUDE_CODE_TMPDIR"]), 24)
            self.assertFalse(Path(env["CLAUDE_CODE_TMPDIR"]).exists())
            # Only an editing Claude call gets one, and a host without /tmp has no confinable Claude.
            CliWorker(checkout, runner)("triage", ModelSpec.parse("claude-opus-5-5-high"), {"plan": PlanState()}, "triage")
            self.assertNotIn("CLAUDE_CODE_TMPDIR", seen[-1][1])
            with patch("foreman.worker.TemporaryDirectory", side_effect=[TemporaryDirectory(), OSError("no /tmp")]):
                outcome = CliWorker(checkout, runner)("implement", ModelSpec.parse("claude-opus-5-5-high"), {"checks": ChecksState(check="sh check.sh")}, "implement")
            self.assertEqual((outcome.kind, len(seen)), ("cli_missing", 2))
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

    def test_review_grants_only_the_bound_scope_command(self):
        args_seen = []
        def runner(args, cwd, env):
            self.assertEqual(args[args.index("--tools") + 1], "Read,Glob,Grep,Bash")
            args_seen.append(json.loads(Path(args[args.index("--settings") + 1]).read_text()))
            return fixture_process(args, "success", 0)
        plan = {"models": [], "instruction": "review", "methods": [], "read_roots": [],
                "target": None, "scope": {"command": "  # head history base\n  git diff --no-color abc...def --  \n\n", "inline": []}}
        CliWorker(Path.cwd(), runner)("review", ModelSpec.parse("claude-opus-5-5-high"),
                                     {"checks": ChecksState(check="sh check.sh"), "dispatch_plan": plan}, "review")
        args = args_seen[0]
        self.assertEqual(args["permissions"]["allow"],
                         ["Bash(git diff --no-color abc...def --)"])

    def test_multiline_implementation_check_stays_one_grant(self):
        check = "# implementation check\npython check.py\npython other.py"
        def runner(args, cwd, env):
            self.assertEqual(args[args.index("--tools") + 1], "Read,Glob,Grep,Bash,Edit,Write")
            permissions = json.loads(Path(args[args.index("--settings") + 1]).read_text())["permissions"]
            self.assertEqual(permissions["allow"], [f"Bash({check})", "Edit", "Write"])
            return fixture_process(args, "success", 0)
        result = CliWorker(Path.cwd(), runner)("implement", ModelSpec.parse("claude-opus-5-5-high"),
                                              {"checks": ChecksState(check=check)}, "implement")
        self.assertEqual(result.kind, "ran")

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
                self.assertFalse(any(c.startswith("git diff --no-color") for c in commands))
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

    def test_grok_fails_before_launch_for_editing_and_readonly(self):
        from unittest.mock import Mock, patch
        for system in ('Darwin', 'Linux', 'Windows'):
            for stage in ('implement', 'review'):
                runner = Mock()
                with patch('foreman.adapters.grok.platform.system', return_value=system):
                    with self.assertRaises(CapabilityUnavailable):
                        GrokAdapter(ModelSpec.parse('grok-4.7-high')).command(Call('probe', '{}', stage == 'implement', Path.cwd()))
                    outcome = CliWorker(Path.cwd(), runner)(stage, ModelSpec.parse('grok-4.7-high'), {'checks': ChecksState()}, 'probe')
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
                        return fixture_process(args, 'success', 0)
                    plan = {'models': [], 'instruction': '', 'methods': [], 'read_roots': [str(r) for r in roots], 'target': None, 'scope': None}
                    self.assertEqual(CliWorker(root, run)(stage, adapter.model, {'checks': ChecksState(), 'dispatch_plan': plan}, 'probe').kind, 'ran')
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
            self.assertEqual('repo/checked' in capture['files'], mode == 'edit')
            self.assertEqual(capture['unavailable_policy']['argv'][:2], ['codex', 'exec'])
            self.assertNotEqual(capture['unavailable_policy']['returncode'], 0)
            self.assertEqual(capture['unavailable_policy']['stdout'], '')
            self.assertNotEqual(capture['sandbox_unavailable']['returncode'], 0)
            self.assertEqual(capture['sandbox_unavailable']['stdout'], '')

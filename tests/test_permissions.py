import tests
from foreman.state import PlanState, ChecksState

import json
from pathlib import Path
import unittest
import tomllib
from tempfile import TemporaryDirectory
from subprocess import CompletedProcess

from foreman.adapters import Call, CapabilityUnavailable, Outcome
from foreman.adapters.claude import ClaudeAdapter
from foreman.adapters.codex import CodexAdapter
from foreman.adapters.grok import GrokAdapter
from foreman.config import ModelSpec, NodeConfig, load_config
from foreman.delivery import run_delivery
from foreman.dry_run import ScriptHandler, SCRIPTED_USAGE
from foreman.skill_rules import PACKAGE_SKILLS, SKILL_RULES, export_skills, stage_methods
from foreman.worker import CliWorker
from skill_fixture import skill_fixture
from test_adapters import fixture_process
from test_runner import EMPTY_SLICE


class PermissionsTest(unittest.TestCase):
    def test_method_paths_are_covered_by_precise_roots(self):
        with skill_fixture() as (config, scratch):
            root = export_skills(config.pstack, scratch)
            for stage in {rule.stage for rule in SKILL_RULES}:
                for kind in ("feature", "bug_fix", "perf_issue"):
                    state = {"plan": PlanState(work_kind=kind, issue_kind="slice", slice={**EMPTY_SLICE, "stateful": True, "screen": "docs/screens/ui.md", "properties": [{"name": "rule", "constraint": "unique"}]})}
                    text, methods, roots = stage_methods(stage, state, root)
                    self.assertEqual(len(roots), len(set(roots)))
                    self.assertTrue(set(roots) <= {str(root.resolve()), str(PACKAGE_SKILLS.resolve())})
                    self.assertEqual(len(text.split('\n\n')), len(methods))
                    for instruction in text.split('\n\n'):
                        path = instruction.split(' of ')[-1].split(' as the method.')[0] if ' of ' in instruction else instruction.removeprefix('Read ').split(' in full')[0]
                        self.assertTrue(Path(path).is_file())
                        self.assertTrue(any(Path(path).is_relative_to(Path(r)) for r in roots))

    def test_first_fix_and_quota_fallback_receive_the_bound_grant(self):
        for fallback in (False, True):
            config = load_config()
            names = (["gpt-6.1-sol-high", "gpt-6.1-terra-high"] if fallback else []) + ["claude-opus-5-5-high"]
            config.nodes["implement"] = NodeConfig("implementer", tuple(ModelSpec.parse(n) for n in names))
            with skill_fixture(config) as (config, scratch):
                calls = []
                script = ScriptHandler(reviews=["changes", "approve"])

                def runner(args, cwd, env):
                    calls.append(args)
                    return fixture_process(args, "success", 0)

                worker = CliWorker(scratch, runner)

                def handler(node, model, state, instruction):
                    if node != "implement":
                        return script(node, model, state, instruction)
                    if model.family == "gpt":
                        return Outcome("quota", usage=SCRIPTED_USAGE)
                    worker(node, model, state, instruction)
                    roots = state["dispatch_plan"]["read_roots"]
                    argv = calls[-1]
                    self.assertEqual([argv[i + 1] for i, a in enumerate(argv) if a == "--add-dir"][:-1], roots)
                    permissions = json.loads(argv[argv.index("--settings") + 1])["permissions"]
                    self.assertEqual(permissions["allow"], ["Bash(sh check.sh)", "Edit", "Write"])
                    return script(node, model, state, instruction)

                result = run_delivery(handler, repo="Example/Engine", request="Change", check="sh check.sh",
                                      config=config, slice=EMPTY_SLICE, scratch=scratch)
                self.assertEqual(result["stop"], "verified")
                self.assertEqual(len(calls), 2)

    def test_readonly_has_no_edit_tools_or_allow_rules(self):
        adapter = ClaudeAdapter(ModelSpec.parse("claude-opus-5-5-high"))
        args = adapter.command(Call("prompt", "{}", False, Path("scratch")))
        self.assertEqual(args[args.index("--tools") + 1], "Read,Glob,Grep,Bash")
        self.assertEqual(json.loads(args[args.index("--settings") + 1])["permissions"]["allow"], [])
        self.assertIn("--restricted", args)
        self.assertIn("--strict-mcp-config", args)
        self.assertEqual(args[args.index("--permission-mode") + 1], "dontAsk")

    def test_review_grants_only_the_bound_scope_command(self):
        args_seen = []
        def runner(args, cwd, env):
            args_seen.append(args)
            return fixture_process(args, "success", 0)
        plan = {"models": [], "instruction": "review", "methods": [], "read_roots": [],
                "target": None, "scope": {"command": "  # head history base\n  git diff --no-color abc...def --  \n\n", "inline": []}}
        CliWorker(Path.cwd(), runner)("review", ModelSpec.parse("claude-opus-5-5-high"),
                                     {"checks": ChecksState(check="sh check.sh"), "dispatch_plan": plan}, "review")
        args = args_seen[0]
        self.assertEqual(json.loads(args[args.index("--settings") + 1])["permissions"]["allow"],
                         ["Bash(git diff --no-color abc...def --)"])

    def test_multiline_implementation_check_stays_one_grant(self):
        check = "# implementation check\npython check.py\npython other.py"
        def runner(args, cwd, env):
            permissions = json.loads(args[args.index("--settings") + 1])["permissions"]
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
            self.assertNotIn("repo/x", files)
            self.assertNotIn("repo/f", files)
            denied = capture["stdout"]["permission_denials"]
            commands = {d["tool_input"].get("command") for d in denied if d["tool_name"] == "Bash"}
            self.assertIn("touch x", commands)
            if mode == "readonly":
                self.assertEqual(files["repo/source.py"], "value = 1\n")
                self.assertNotIn("scratch/scratch.txt", files)
                self.assertIn("git diff --output=f", commands)
                self.assertFalse(any(c.startswith("git diff --no-color") for c in commands))
            else:
                self.assertEqual(files["repo/source.py"], "value = 2\n")
                self.assertEqual(files["scratch/scratch.txt"], "scratch")
                self.assertIn('name = "after"', files["repo/pyproject.toml"])
                self.assertEqual(files["repo/.github/workflows/ci.yml"], "name: after\n")
                self.assertTrue({"git commit -m x", "gh pr list", "python -c 1"} <= commands)
                self.assertTrue(any(d["tool_name"] == "Edit" and "methods/" in d["tool_input"]["file_path"] for d in denied))
                for marker in (("first", "second") if mode == "compound" else ("checked",)):
                    self.assertIn(f"repo/{marker}", files)
            content = [c for row in capture["transcript"] for c in row["message"]["content"] if isinstance(c, dict)]
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
                self.assertEqual([arg.replace(str(root), '<probe>') for arg in adapter.command(call)], capture['argv'])
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

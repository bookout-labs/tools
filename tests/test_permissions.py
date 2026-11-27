import tests
from foreman.state import PlanState, ChecksState, RunState

from functools import partial
import json
import os
from pathlib import Path
import unittest
import re
import sys
import tomllib
from tempfile import TemporaryDirectory
import subprocess
from subprocess import CompletedProcess
from unittest.mock import patch

from foreman.adapters import Call, CapabilityUnavailable, ExecRoute, Outcome
from foreman.adapters.claude import EXEC_TOOL, ClaudeAdapter
from foreman.adapters.codex import CodexAdapter
from foreman.adapters.grok import GrokAdapter
from foreman.config import ModelSpec, NodeConfig, load_config
from foreman.container import ContainerSpec, Mount, create_arguments
from foreman.delivery import run_delivery
from foreman.dry_run import ScriptHandler, SCRIPTED_USAGE
from foreman import exec_tool
from foreman.skill_rules import PACKAGE_SKILLS, export_skills, git, stage_methods
from foreman.stages import STAGES
from foreman.worker import CliWorker, git_dirs
from foreman.workspace import LiveWorkspace
from container_double import HostContainers
from skill_fixture import skill_fixture, verify_checkout
from test_adapters import codex_events, codex_process, fixture_process
from publication_fixture import contract_text
from test_runner import EMPTY_SLICE


DIFF = "git --no-replace-objects diff --no-color a...b --"
ROUTE = ExecRoute("container-1", Path("/checkout"), 1800)


def containers(base: str) -> tuple[str, float]:
    """The run's container, as Workspace.container_route gives it."""
    return "container-1", 1800


def exec_server(args: list[str]) -> list[str]:
    """The exec tool's argv from a Claude call's MCP config: the container, the checkout, the seconds and the granted commands."""
    config = json.loads(Path(args[args.index("--mcp-config") + 1]).read_text())
    server = config["mcpServers"]["foreman"]
    assert server["type"] == "stdio" and server["args"][:2] == ["-m", "foreman.exec_tool"]
    return server["args"][2:]


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

    def test_every_worker_reads_the_related_checkouts_and_none_writes_them(self):
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
                with self.subTest(edit=edit, drive=drive):
                    args = claude.command(Call("prompt", "{}", edit, attempt, read_roots=(related,), drive=drive, route=ROUTE))
                    settings = json.loads(Path(args[args.index("--settings") + 1]).read_text())
                    self.assertIn(f"Edit(/{related}/**)", settings["permissions"]["deny"])
            codex = CodexAdapter(ModelSpec.parse("gpt-6.1-sol-high")).command(Call("prompt", "{}", True, attempt, read_roots=(related,),
                                                                                    route=ROUTE))
            self.assertIn(f'"{related.resolve()}"="read"', " ".join(codex))

    def test_first_fix_and_quota_fallback_receive_the_bound_grant(self):
        for fallback in (False, True):
            config = load_config()
            names = (["gpt-6.1-sol-high", "gpt-6.1-terra-high"] if fallback else []) + ["claude-opus-5-5-high"]
            config.nodes["implement"] = NodeConfig("implementer", tuple(ModelSpec.parse(n) for n in names))
            with skill_fixture(config) as (config, scratch):
                calls = []
                script = ScriptHandler(reviews=["changes", "approve"])

                def runner(args, cwd, env):
                    calls.append(([args[i + 1] for i, a in enumerate(args) if a == "--add-dir"], exec_server(args),
                                  json.loads(Path(args[args.index("--settings") + 1]).read_text())))
                    return fixture_process(args, "success", 0)

                worker = CliWorker(scratch, runner, scratch_root=Path(self.enterContext(TemporaryDirectory())), containers=containers)

                def handler(node, model, state, instruction):
                    if node != "implement":
                        return script(node, model, state, instruction)
                    if model.family == "gpt":
                        return Outcome("quota", usage=SCRIPTED_USAGE)
                    worker(node, model, state, instruction)
                    roots = state["dispatch_plan"]["read_roots"]
                    directories, server, settings = calls[-1]
                    self.assertEqual(directories[:-1], roots)
                    permissions = settings["permissions"]
                    self.assertEqual(permissions["allow"], ["Edit", "Write", EXEC_TOOL])
                    self.assertEqual(server, ["container-1", str(scratch.absolute()), "1800"])
                    return script(node, model, state, instruction)

                result = run_delivery(handler, repo="Example/Engine", request="Change", contract=contract_text("sh check.sh"),
                                      config=config, slice=EMPTY_SLICE, scratch=scratch)
                self.assertEqual(result["stop"], "merge_pending")
                self.assertEqual(len(calls), 2)

    def test_the_verifier_keeps_its_attempt_scratch_and_names_it_for_captures(self):
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
            worker = partial(CliWorker, checkout, scratch_root=state_dir, containers=containers)
            outcome = worker(runner)("verify", ModelSpec.parse("claude-opus-5-5-high"), state, "verify")
            self.assertEqual(outcome.kind, "ran")
            args, settings, env = seen[0]
            # The scratch the verifier writes in the container outlives the call under its run's own directory.
            run_dir = state_dir / "foreman-verify" / "Org-App-7"
            [kept] = run_dir.iterdir()
            scratch = kept / "scratch"
            self.assertTrue(scratch.is_dir())
            self.assertEqual(env["TMPDIR"], str(scratch))
            self.assertIn(f"Write scratch and captures under {scratch} and name each capture's path as its evidence.", args[-1])
            self.assertIn("Run each command with Foreman's exec tool, which runs it in the run's attempt container.", args[-1])
            self.assertNotIn("Do not return the scratch path as evidence.", args[-1])
            # The verifier has no Edit or Write and no host shell: its commands go to the run's container, unrestricted.
            self.assertEqual((args[args.index("--tools") + 1], settings["permissions"]["allow"]), ("Read,Glob,Grep", [EXEC_TOOL]))
            self.assertEqual(exec_server(args), ["container-1", str(checkout), "1800"])
            # Every exit before launch leaves no attempt directory behind: a model that cannot drive the app,
            # a missing executable, and an error before the call.
            def missing(args, cwd, env):
                raise FileNotFoundError(args[0])
            for model, run in (("grok-4.7-high", runner), ("claude-opus-5-5-high", missing)):
                with self.subTest(model=model):
                    refused = worker(run)("verify", ModelSpec.parse(model), state, "verify")
                    self.assertEqual((refused.kind, list(run_dir.iterdir()), len(seen)), ("cli_missing", [kept], 1))
            # A worker with no attempt container runs no command, so it never launches.
            outcome = CliWorker(checkout, runner, scratch_root=state_dir)("verify", ModelSpec.parse("claude-opus-5-5-high"), state, "verify")
            self.assertEqual((outcome.kind, list(run_dir.iterdir()), len(seen)), ("cli_missing", [kept], 1))
            # Errors before and after the attempt directory exists.
            with self.assertRaises(ValueError):
                worker(runner)("verify", ModelSpec.parse("claude-opus-5-5-high"), {**state, "dispatch_plan": {"bad": 1}}, "verify")
            with patch("foreman.worker.ClaudeAdapter.command", side_effect=RuntimeError("adapter broke")), self.assertRaises(RuntimeError):
                worker(runner)("verify", ModelSpec.parse("claude-opus-5-5-high"), state, "verify")
            self.assertEqual(list(run_dir.iterdir()), [kept])

    def test_editing_claude_writes_no_git_directory_of_its_linked_worktree(self):
        with TemporaryDirectory() as directory:
            clone, checkout = Path(directory).resolve() / "clone", Path(directory).resolve() / "checkout"
            clone.mkdir()
            for args in (["init", "-q"], ["-c", "user.name=T", "-c", "user.email=t@example.test", "commit", "-q", "--allow-empty", "-m", "base"],
                         ["worktree", "add", "-q", str(checkout)]):
                subprocess.run(["git", *args], cwd=clone, check=True)
            # A worker started through a link denies the linked path and its target.
            (Path(directory) / "link").symlink_to(checkout)
            seen = []
            def runner(args, cwd, env):
                seen.append(json.loads(Path(args[args.index("--settings") + 1]).read_text())["permissions"]["deny"])
                return fixture_process(args, "success", 0)
            for root in (Path(directory) / "link", checkout):
                CliWorker(root, runner, scratch_root=Path(directory), containers=containers)(
                    "implement", ModelSpec.parse("claude-opus-5-5-high"), {"checks": ChecksState(check="sh check.sh")}, "implement")
            self.assertEqual(seen[0][:2], [f"Edit(/{Path(directory).absolute() / 'link/.git'})", f"Edit(/{Path(directory).absolute() / 'link/.git'}/**)"])
            self.assertEqual(seen[1], [rule for path in (checkout / ".git", clone / ".git/worktrees/checkout", clone / ".git")
                                       for rule in (f"Edit(/{path})", f"Edit(/{path}/**)")])
            # The checkout's own .git decides, never an inherited GIT_DIR, and an unreadable one fails closed.
            with patch.dict(os.environ, {"GIT_DIR": str(clone / ".git")}):
                self.assertEqual(git_dirs(checkout), (checkout / ".git", clone / ".git/worktrees/checkout", clone / ".git"))
            (checkout / ".git").write_text("gitdir: /nonexistent\n")
            with self.assertRaises(CapabilityUnavailable):
                git_dirs(checkout)
            self.assertEqual(git_dirs(clone.parent), ())

    def test_a_shared_clone_s_config_holds_no_credential(self):
        """Every worker reads the shared clone's config, so the clone Foreman makes keeps credentials out of it."""
        with TemporaryDirectory() as directory, patch.dict(os.environ, {"GH_TOKEN": "ghs_secret-token"}), \
                patch("foreman.workspace.LiveWorkspace._validate_remote", return_value=[]):
            workspace = LiveWorkspace.managed(Path(directory), "Example/Engine", 7)
            workspace.validate_origin("Example/Engine")
            assert workspace.clone is not None
            config = (workspace.clone / ".git/config").read_text() if (workspace.clone / ".git").is_dir() else (workspace.clone / "config").read_text()
        self.assertIn("url = https://github.com/Example/Engine\n", config)
        # Fetches authenticate with -c flags on the command line (GIT_AUTH), never with a URL token or a stored header.
        for credential in ("ghs_secret-token", "extraheader", "x-access-token", "@github.com", "authorization", "credential"):
            self.assertNotIn(credential, config.lower())

    def test_readonly_has_no_edit_tools_or_allow_rules(self):
        adapter = ClaudeAdapter(ModelSpec.parse("claude-opus-5-5-high"))
        with TemporaryDirectory() as directory:
            args = adapter.command(Call("prompt", "{}", False, Path(directory)))
            permissions = json.loads(Path(args[args.index("--settings") + 1]).read_text())
        self.assertEqual(args[args.index("--tools") + 1], "Read,Glob,Grep")
        self.assertEqual(permissions["permissions"]["allow"], [])
        # Triage, plan and spec have no Bash, and their file tools still refuse every read outside the working directories.
        self.assertTrue(permissions["permissions"]["blockReadsOutsideWorkingDirectories"])
        self.assertIn("--restricted", args)
        self.assertIn("--strict-mcp-config", args)
        self.assertNotIn("--mcp-config", args)
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

    def test_the_reviewer_runs_only_its_diff_command_and_the_contract_s_check_in_the_container(self):
        args_seen = []
        def runner(args, cwd, env):
            args_seen.append((exec_server(args), json.loads(Path(args[args.index("--settings") + 1]).read_text())))
            return fixture_process(args, "success", 0)
        plan = {"models": [], "instruction": "review", "methods": [], "read_roots": [],
                "target": None, "scope": {"command": "  # head history base\n  git diff --no-color abc...def --  \n\n", "inline": []}}
        CliWorker(Path.cwd(), runner, scratch_root=Path(self.enterContext(TemporaryDirectory())), containers=containers)(
            "review", ModelSpec.parse("claude-opus-5-5-high"), {"checks": ChecksState(check="sh check.sh"), "dispatch_plan": plan}, "review")
        (server, settings), = args_seen
        self.assertEqual(settings["permissions"]["allow"], [EXEC_TOOL])
        self.assertEqual(server, ["container-1", str(Path.cwd().absolute()), "1800", "git diff --no-color abc...def --", "sh check.sh"])
        # The tool runs each granted command in the container and refuses every other.
        host = HostContainers()
        with TemporaryDirectory() as directory:
            name = host.create(ContainerSpec("run", "image", Path(directory), ()), 60)
            only = ("git diff --no-color abc...def --", "exit 3")
            self.assertEqual(exec_tool.run(host, name, Path(directory), 60, only, "  exit 3  "), ("\nThe command exited with 3.", True))
            self.assertEqual(host.ran[-1].command, "  exit 3  ")
            text, error = exec_tool.run(host, name, Path(directory), 60, only, "cat ~/.gitconfig")
            self.assertTrue(error)
            self.assertIn("Foreman runs only these commands here", text)
            self.assertEqual(len(host.ran), 1)

    def test_every_claude_profile_offers_only_its_role_s_tools_and_only_foreman_s_exec_server(self):
        adapter = ClaudeAdapter(ModelSpec.parse("claude-opus-5-5-high"))
        profiles = {"readonly": ({}, "Read,Glob,Grep"), "review": ({"checks": (DIFF,)}, "Read,Glob,Grep"),
                    "verify": ({"drive": True}, "Read,Glob,Grep"), "implement": ({"edit": True, "checks": ("sh check.sh",)},
                                                                               "Read,Glob,Grep,Edit,Write")}
        with TemporaryDirectory() as directory:
            for role, (fields, tools) in profiles.items():
                with self.subTest(role=role):
                    call = Call("prompt", "{}", **{"edit": False, **fields}, temp_dir=Path(directory), route=ROUTE)
                    args = adapter.command(call)
                    self.assertEqual([args[i + 1] for i, a in enumerate(args) if a == "--tools"], [tools])
                    self.assertIn("--strict-mcp-config", args)
                    if call.shell:
                        self.assertEqual(list(json.loads(Path(args[args.index("--mcp-config") + 1]).read_text())["mcpServers"]), ["foreman"])
                        # A call that runs commands and has no container never launches.
                        with self.assertRaises(CapabilityUnavailable):
                            adapter.command(Call("prompt", "{}", **{"edit": False, **fields}, temp_dir=Path(directory)))
                    else:
                        self.assertNotIn("--mcp-config", args)

    def test_the_container_holds_no_credential_and_the_cli_reaches_it_only_through_the_exec_tool(self):
        """docker run gets the checkout, Git's directory read-only and the run's scratch, and the names of the contract's test
        values only. The worker's CLI and its login stay on the host and start the exec tool, which runs docker exec."""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            spec = ContainerSpec("run", "node:22", root / "checkout", (Mount(root / "checkout"), Mount(root / "clone/.git", read_only=True),
                                                                         Mount(root / "scratch")), {"API_KEY": "test-value"})
            args = create_arguments("foreman-1", spec)
            mounts = [args[i + 1] for i, a in enumerate(args) if a == "--mount"]
            self.assertEqual(mounts, [f"type=bind,source={root}/checkout,target={root}/checkout",
                                      f"type=bind,source={root}/clone/.git,target={root}/clone/.git,readonly",
                                      f"type=bind,source={root}/scratch,target={root}/scratch"])
            self.assertEqual([args[i + 1] for i, a in enumerate(args) if a == "--env"], ["API_KEY"])
            self.assertNotIn("test-value", args)
            for credential in (str(Path.home()), ".codex", ".claude", ".gitconfig", "GH_TOKEN", "FOREMAN_APP", "SSH_AUTH_SOCK"):
                self.assertFalse(any(credential in arg for arg in args), credential)
        claude = ClaudeAdapter(ModelSpec.parse("claude-opus-5-5-high"))
        with TemporaryDirectory() as directory:
            args = claude.command(Call("prompt", "{}", True, Path(directory), checks=("sh check.sh",), route=ROUTE))
            server = json.loads(Path(args[args.index("--mcp-config") + 1]).read_text())["mcpServers"]["foreman"]
        self.assertEqual(server, {"type": "stdio", "command": sys.executable,
                                  "args": ["-m", "foreman.exec_tool", "container-1", "/checkout", "1800"]})
        self.assertNotIn("Bash", args[args.index("--tools") + 1])
        # The tool's one route to the container is docker exec in the checkout.
        with patch("foreman.container.stream", return_value=0) as streamed:
            exec_tool.run(exec_tool.DockerHost(), "container-1", Path("/checkout"), 1800, (), "npm test")
        self.assertEqual(streamed.call_args.args[0], ["docker", "exec", "--workdir", "/checkout", "container-1", "sh", "-c", "npm test"])

    def test_grok_fails_before_launch_for_editing_and_readonly(self):
        from unittest.mock import Mock, patch
        for system in ('Darwin', 'Linux', 'Windows'):
            for stage in ('implement', 'review'):
                runner = Mock()
                with patch('foreman.adapters.grok.platform.system', return_value=system):
                    with self.assertRaises(CapabilityUnavailable):
                        GrokAdapter(ModelSpec.parse('grok-4.7-high')).command(Call('probe', '{}', stage == 'implement', Path.cwd()))
                    outcome = CliWorker(Path.cwd(), runner, scratch_root=Path(self.enterContext(TemporaryDirectory())), containers=containers)(stage, ModelSpec.parse('grok-4.7-high'), {'checks': ChecksState()}, 'probe')
                self.assertEqual(outcome.kind, 'cli_missing')
                self.assertIn(system, outcome.detail)
                self.assertIn('editing requires an isolated worker launcher' if stage == 'implement' else 'temp writes', outcome.detail)
                self.assertEqual('child network isolation is unavailable' in outcome.detail, system == 'Darwin' and stage == 'review')
                runner.assert_not_called()

    def test_codex_current_commands_enforce_the_policy(self):
        adapter = CodexAdapter(ModelSpec.parse('gpt-6.1-sol-high'))
        with TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            for stage in ('implement', 'verify', 'review', 'triage'):
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
                        # A call that runs commands has Codex's own shell off and Foreman's exec tool on, approved.
                        disabled = [args[i + 1] for i, arg in enumerate(args) if arg == '--disable']
                        servers = tomllib.loads("\n".join(v for v in overrides if v.startswith('mcp_servers.')))
                        if stage == 'triage':
                            self.assertEqual((disabled, servers), ([], {}))
                        else:
                            self.assertEqual(disabled, ['shell_tool', 'unified_exec'])
                            foreman = servers['mcp_servers']['foreman']
                            self.assertEqual((foreman['command'], foreman['args'][:3]), (sys.executable, ['-m', 'foreman.exec_tool', 'container-1']))
                            self.assertEqual(foreman['tools'], {'exec': {'approval_mode': 'approve'}})
                        events = codex_events()
                        events[-2:-2] = [{'type': 'item.completed', 'item': {'type': 'command_execution', 'command': DIFF,
                                                                             'exit_code': 0, 'status': 'completed'}}]
                        return codex_process(events)
                    scope = {'command': DIFF, 'inline': []} if stage == 'review' else None
                    plan = {'models': [], 'instruction': '', 'methods': [], 'read_roots': [str(r) for r in roots], 'target': None, 'scope': scope}
                    self.assertEqual(CliWorker(root, run, scratch_root=Path(self.enterContext(TemporaryDirectory())), containers=containers)(stage, adapter.model, {'checks': ChecksState(), 'dispatch_plan': plan}, 'probe').kind, 'ran')
                    args = adapter.command(Call('probe', '{}', stage == 'implement', root, read_roots=roots, drive=stage == 'verify',
                                                checks=(DIFF,) if stage == 'review' else (), route=ROUTE))
                    run(args, root, {'TMPDIR': str(root / 'scratch')})

    def test_codex_kernel_capture_enforces_reads_writes_and_network(self):
        for mode in ('readonly',):
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

from collections.abc import Mapping
from math import isclose
from pathlib import Path
import json
import os
import platform
import pwd
import re
import shutil
import subprocess
from subprocess import CompletedProcess
from typing import ClassVar, Literal

from pydantic import BaseModel, Field, TypeAdapter, ValidationError
from typing_extensions import override

from foreman.adapters import Call, CapabilityUnavailable, NativeAdapter, NativeEnvelope, Outcome, OutcomeKind, Usage, build_outcome
from foreman.usd import CostUsd, usd_text


QUOTA_ERRORS = {"usage_limit_reached", "model_requires_usage_credits"}
# Claude Code 2.1.329 prints this and exits at startup when failIfUnavailable meets a sandbox that cannot start.
SANDBOX_UNAVAILABLE = "sandbox required but unavailable"
# The operating system's own directories a tool reads to start, as each platform lays them out. Only macOS is captured.
SYSTEM_READS = {
    "Darwin": ("/System", "/Library", "/Applications", "/usr", "/bin", "/sbin", "/dev", "/etc", "/private/etc",
               "/var/select", "/private/var/select"),
    "Linux": ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc", "/dev", "/proc", "/sys"),
}
# The oldest Claude Code the read-reach captures (tests/fixtures/claude/permissions/read-reach-*.json) were taken on.
CAPTURED_VERSION = (2, 1, 332)


class PermissionDenial(BaseModel):
    tool_name: str
    tool_input: dict[str, object] = Field(default_factory=dict)


class ClaudeEnvelope(NativeEnvelope):
    type: Literal["result"]
    subtype: str
    is_error: bool
    api_error_status: int | None = None
    api_error: str | None = None
    result: str = ""
    errors: list[str] = Field(default_factory=list)
    structured_output: dict[str, object] | None = None
    session_id: str | None = None
    total_cost_usd: object = None
    permission_denials: object = Field(default_factory=list)

    @override
    def read_usage(self) -> Usage | None:
        usage = super().read_usage()
        if usage is None or usage.total_cost_usd is None:
            return usage
        try:
            total = TypeAdapter(CostUsd).validate_python(self.total_cost_usd, strict=True)
        except ValidationError:
            total = None
        # The envelope total corroborates the ledger; it is not an extra charge.
        if total is None or not isclose(usage.total_cost_usd, total, rel_tol=1e-6, abs_tol=1e-6):
            return usage.model_copy(update={"total_cost_usd": None})
        return usage

    @override
    def outcome(self, process: CompletedProcess[str], model_id: str, call: Call) -> Outcome:
        kind: OutcomeKind = "ran"
        detail = "\n".join(self.errors) or self.result or process.stderr.strip()
        if self.subtype == "error_max_budget_usd":
            kind = "budget_exhausted"
            detail = detail or "Claude exhausted its dollar budget"
        elif self.is_error and (self.api_error_status == 429 or self.api_error in QUOTA_ERRORS):
            kind = "quota"
            detail = detail or self.api_error or "Claude API status 429"
        elif process.returncode or self.is_error or self.api_error or self.subtype != "success":
            kind = "failed"
            detail = detail or self.api_error or f"Claude {self.subtype} (exit {process.returncode})"
        elif not self.model_usage:
            kind = "failed"
            detail = f"modelUsage missing; cannot confirm {model_id}"
        elif model_id not in self.model_usage:
            kind = "failed"
            detail = f"requested {model_id}, modelUsage reported {', '.join(self.model_usage)}"
        if kind == "ran" and call.checks:
            granted = "check" if call.edit else "diff command"
            try:
                denials = TypeAdapter(list[PermissionDenial]).validate_python(self.permission_denials, strict=True)
            except ValidationError:
                kind, detail = "failed", f"Claude denial evidence malformed: {self.permission_denials!r}; {call.checks}"
            else:
                for denial in denials:
                    command = denial.tool_input.get("command")
                    if denial.tool_name == "Bash" and denies_grant(call, command):
                        kind = "failed"
                        detail = f"Claude denied the granted {granted}: {call.checks}; denied command: {command!r}"
                        break
        if kind == "ran" and self.structured_output is None:
            kind = "failed"
            detail = detail or "missing structured output"
        return build_outcome(
            kind, output=self.structured_output, usage=self.read_usage(),
            session_id=self.session_id, detail=detail,
        )


def denies_grant(call: Call, command: object) -> bool:
    if not call.edit:
        # Review's grant is one exact command; a denied variant is the boundary working.
        return isinstance(command, str) and command.strip() in call.checks
    return (not isinstance(command, str) or not command.strip()
            or any(check in command or command in check for check in call.checks))


def both(paths: tuple[Path, ...]) -> list[str]:
    """Each path as given and as resolved, so a denial holds whether a tool names the link or its target."""
    return list(dict.fromkeys(str(form) for path in paths for form in (path.absolute(), path.resolve())))


def sandboxed(system: str) -> None:
    """Claude Code sandboxes Bash with Seatbelt on macOS, and with bubblewrap and socat on Linux, from the oldest version captured."""
    missing = [tool for tool in ("bwrap", "socat") if shutil.which(tool) is None] if system == "Linux" else []
    if system not in ("Darwin", "Linux") or missing:
        reason = f"{', '.join(missing)} missing" if missing else "the Bash sandbox is unsupported"
        raise CapabilityUnavailable(f"Claude on {system}: {reason}, so a worker's Bash cannot be confined")
    needed = ".".join(map(str, CAPTURED_VERSION))
    try:
        printed = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=30, check=True).stdout
    except FileNotFoundError:
        raise CapabilityUnavailable("claude executable is unavailable") from None
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
        raise CapabilityUnavailable(f"claude --version gave no version ({exc}), so Claude Code {needed} or newer is unproven") from None
    found = re.match(r"\s*(\d+)\.(\d+)\.(\d+)", printed)
    if found is None:
        raise CapabilityUnavailable(f"no version could be read from claude --version output {printed.strip()!r}; "
                                    f"Claude Code {needed} or newer is needed")
    if tuple(map(int, found.groups())) < CAPTURED_VERSION:
        raise CapabilityUnavailable(f"Claude Code {'.'.join(found.groups())} is older than {needed}, "
                                    "the oldest version whose read-reach capture shows a worker's Bash confined")


class ClaudeAdapter(NativeAdapter):
    envelope = ClaudeEnvelope
    # Claude Code finds its login in the keychain under the user's account name.
    env: ClassVar[Mapping[str, str]] = {"CLAUDE_CODE_NO_MODEL_FALLBACK": "1", "USER": pwd.getpwuid(os.getuid()).pw_name}

    @override
    def read(self, process: CompletedProcess[str], call: Call) -> Outcome:
        # A sandbox that cannot start on this host ends the call before any model work, so the next model runs.
        if call.shell and process.returncode and SANDBOX_UNAVAILABLE in process.stderr.lower():
            return Outcome("cli_missing", detail=process.stderr.strip())
        return super().read(process, call)

    def command(self, call: Call) -> list[str]:
        effort = ["--effort", self.model.effort] if self.model.effort else []
        budget = ["--max-budget-usd", usd_text(call.budget_usd)] if call.budget_usd is not None else []
        allow = [f"Bash({command})" for command in call.checks]
        if call.edit:
            allow += ["Edit", "Write"]
        deny = [f"Edit(/{root}/**)" for root in both(call.read_roots)]
        # Read, Glob and Grep refuse every path outside the working directories, symlinks included, in every call.
        permissions: dict[str, object] = {"allow": allow, "deny": deny, "blockReadsOutsideWorkingDirectories": True}
        settings: dict[str, object] = {"permissions": permissions}
        directories = [str(root) for root in call.read_roots]
        if call.edit:
            # Edit and Write stay inside the working directories and off the git directories.
            permissions["blockWritesOutsideWorkingDirectories"] = True
            deny += [rule for path in both(call.git_dirs) for rule in (f"Edit(/{path})", f"Edit(/{path}/**)")]
        if call.shell:
            system = platform.system()
            sandboxed(system)
            system_reads = SYSTEM_READS.get(system, ())
            # What a tool needs to start: the operating system's own directories and the program directories of the prefix
            # the host's git is installed in, unless the operating system's already hold them.
            tools = [*system_reads, *both(tuple(path for path in call.tool_reads
                                                  if not any(path.is_relative_to(read) for read in system_reads)))]
            # The checkout is the parent of its own .git entry, the first of the git directories.
            checkout = (call.git_dirs[0].parent,) if call.git_dirs else ()
            roots = (*checkout, *call.read_roots, *call.git_reads, call.scratch, *((call.shell_temp,) if call.shell_temp else ()))
            # Every shell runs in Claude Code's OS sandbox, which fails closed. It reads nothing but the call's own roots and
            # the tools' directories, writes the checkout and the attempt scratch, never the method roots --add-dir exposes
            # or the git directories, and reaches no network. Only an editing or driving call runs Bash without a grant.
            settings["sandbox"] = {
                "enabled": True, "autoAllowBashIfSandboxed": call.edit or call.drive, "failIfUnavailable": True,
                "allowUnsandboxedCommands": False, "network": {"allowedDomains": [], "strictAllowlist": True},
                "filesystem": {"denyRead": ["/"], "allowRead": [*tools, *both(roots)],
                               "denyWrite": both((*call.read_roots, *call.git_dirs))},
            }
            directories.append(str(call.scratch))
        settings_path = call.temp_dir / "claude-settings.json"
        settings_path.write_text(json.dumps(settings))
        scope = [arg for directory in directories for arg in ("--add-dir", directory)]
        resume = ["--resume", call.resume_session, "--fork-session"] if call.resume_session else []
        return ["claude", "--model", self.model.model_id, *effort, *resume, "--restricted",
                "--strict-mcp-config", "--permission-mode", "dontAsk",
                "--tools", "Read,Glob,Grep,Bash,Edit,Write" if call.edit else
                "Read,Glob,Grep,Bash" if call.shell else "Read,Glob,Grep",
                *scope, "--settings", str(settings_path),
                *budget, "--output-format", "json",
                "--json-schema", call.schema, "-p", call.prompt]

"""#420 step 1: what each CLI's own session record holds after a cut-off call. Measurement only.

    uv run python tests/probe-cli-session-records.py claude   # four live claude-haiku-5-5 calls
    uv run python tests/probe-cli-session-records.py claude-adapter   # five calls through ClaudeAdapter.command
    uv run python tests/probe-cli-session-records.py codex    # on-disk rollouts, field names only
    uv run python tests/probe-cli-session-records.py grok     # on-disk sessions, field names only

Captures go to tests/fixtures/<cli>/session-record/. A real session's record yields only file names and field names.
A probe's own session, made from a synthetic prompt, also yields byte counts and token counts. No text is recorded.
"""
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from decimal import Decimal
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

from foreman._raw import is_dict, is_list
from foreman.adapters import Call
from foreman.adapters.claude import ClaudeAdapter
from foreman.config import ModelSpec
from foreman.usd import BudgetUsd
from foreman.worker import git_dirs

ROOT = Path(__file__).resolve().parents[1]
MODEL = "claude-haiku-5-5"
# Discriminators are schema labels, so their values are kept when they look like one.
LABELS = ("type", "subtype", "payload.type", "message.role", "message.stop_reason", "stopReason", "params.update.sessionUpdate")
LABEL = re.compile(r"[A-Za-z_.-]{1,40}")
# A key that isn't a plain identifier is data, such as a file path or a model name, so it becomes <key>.
KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
ID = re.compile(r"_[A-Za-z0-9]{16,}$")
SCHEMA = json.dumps({"type": "object", "properties": {"answer": {"type": "string", "enum": ["finished"]}},
                     "required": ["answer"], "additionalProperties": False})
TOKENS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def kind(value: object) -> str:
    names: dict[type, str] = {dict: "object", list: "array", str: "string", bool: "bool", int: "int", float: "float"}
    return names.get(type(value), "null")


def paths(value: object, prefix: str = "") -> Iterator[str]:
    if is_dict(value):
        for key, item in value.items():
            name = key if isinstance(key, str) and KEY.fullmatch(key) and not ID.search(key) else "<key>"
            yield from paths(item, f"{prefix}.{name}" if prefix else name)
    elif is_list(value):
        for item in value:
            yield from paths(item, f"{prefix}[]")
        if not value:
            yield f"{prefix}[]: empty"
    else:
        yield f"{prefix}: {kind(value)}"


def dig(record: object, dotted: str) -> object:
    for part in dotted.split("."):
        record = record.get(part) if is_dict(record) else None
    return record


def label(record: object) -> str:
    parts: list[str] = []
    for key in LABELS:
        value = dig(record, key)
        if value is not None:
            parts.append(f"{key}={value if isinstance(value, str) and LABEL.fullmatch(value) else kind(value)}")
    return " ".join(parts) or "(unlabelled)"


def shape(path: Path, *, sizes: bool) -> dict[str, object]:
    """A JSON or JSONL file's field paths and value types, grouped by line label. Never a value."""
    found: dict[str, set[str]] = {}
    lines = 0
    text = path.read_text(encoding="utf-8", errors="replace")
    records: list[object] = []
    if path.suffix == ".jsonl":
        for line in text.splitlines():
            if line.strip():
                lines += 1
                records.append(json.loads(line))
    elif path.suffix == ".json":
        records.append(json.loads(text))
    for record in records:
        found.setdefault(label(record), set()).update(paths(record))
    result: dict[str, object] = {"labels": {name: sorted(keys) for name, keys in sorted(found.items())}}
    if path.suffix == ".jsonl":
        result["lines"] = lines
    if sizes:
        result["bytes"] = path.stat().st_size
    return result


def scrub(name: str) -> str:
    name = re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "<uuid>", name)
    return re.sub(r"\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}", "<timestamp>", name)


def save(cli: str, name: str, data: object) -> None:
    out = ROOT / "tests" / "fixtures" / cli / "session-record" / name
    out.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2, sort_keys=True) + "\n"
    for private in (str(Path.home()), os.environ.get("USER", "") or "\0", str(ROOT)):
        assert private not in text, f"{name} would record a private path"
    out.write_text(text, encoding="utf-8")
    print(f"wrote {out.relative_to(ROOT)}")


# Claude ------------------------------------------------------------------------------------------------------------

STAND_IN = """
import fcntl, json, os, sys
from pathlib import Path
from foreman.process_group import signals_end_foreman
from foreman.worker import run_worker_command, worker_lock
lock = open(sys.argv[1], "a")
fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
worker_lock.set(lock.fileno())
os.environ["PROBE_LOCK_FD"] = str(lock.fileno())
print(lock.fileno(), flush=True)
with signals_end_foreman():
    done = run_worker_command(json.loads(sys.argv[2]), Path(sys.argv[3]), {"CLAUDE_CODE_NO_MODEL_FALLBACK": "1"})
print(json.dumps({"returncode": done.returncode, "stdout": done.stdout}), flush=True)
"""


def claude_args(session: str, prompt: str, *, bash: bool = False, resume: str | None = None) -> list[str]:
    # The cut-off calls carry an output schema, as every Foreman call does.
    tools = ["--tools", "Bash", "--allowedTools", "Bash", "--json-schema", SCHEMA] if bash else ["--tools", ""]
    fork = ["--resume", resume, "--fork-session"] if resume else []
    return ["claude", "--model", MODEL, *fork, "--session-id", session, "--strict-mcp-config", "--setting-sources", "",
            "--permission-mode", "dontAsk", *tools, "--output-format", "json", "-p", prompt]


def transcript(session: str) -> Path | None:
    found = list((Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects").glob(f"*/{session}.jsonl"))
    return found[0] if len(found) == 1 else None


def transcript_usage(path: Path) -> dict[str, object]:
    """Usage summed once per API message id, and the evidence the transcript gives of how the turn ended."""
    messages: dict[str, dict[str, int]] = {}
    lines = 0
    stops: list[str] = []
    structured: list[str] = []
    labels: list[str] = []
    requests = 0
    cost: object = None
    tools: dict[str, str] = {}
    results: list[tuple[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        record: object = json.loads(line)
        lines += 1
        labels.append(label(record))
        requests += dig(record, "type") == "api-request"
        if dig(record, "type") == "cost-state":
            cost = dig(record, "totalCostUSD")
        content = dig(record, "message.content")
        for item in content if is_list(content) else ():
            if dig(item, "type") == "tool_use" and isinstance(ident := dig(item, "id"), str):
                tools[ident] = str(dig(item, "name"))
            elif dig(item, "type") == "tool_result" and isinstance(ident := dig(item, "tool_use_id"), str):
                results.append((ident, dig(item, "is_error")))
        message = dig(record, "message")
        if not is_dict(message) or dig(record, "type") != "assistant":
            continue
        usage, ident = message.get("usage"), message.get("id")
        if is_dict(usage) and isinstance(ident, str):
            messages[ident] = {field: count if isinstance(count := usage.get(field), int) else 0 for field in TOKENS}
        if isinstance(stop := message.get("stop_reason"), str):
            stops.append(stop)
        for item in content if is_list(content) else ():
            if dig(item, "type") == "tool_use" and dig(item, "name") == "StructuredOutput" and is_dict(given := dig(item, "input")):
                structured.append(",".join(sorted(str(key) for key in given)))
    total = {field: sum(m[field] for m in messages.values()) for field in TOKENS}
    return {"api_requests": requests, "api_messages": len(messages), "cost_state_total_usd": cost, "lines": lines, "summed_usage": total, "stop_reasons": stops,
            "structured_output_keys": structured, "last_lines": labels[-3:],
            "tool_results": [f"{tools.get(ident, '?')} is_error={error}" for ident, error in results]}


def envelope_usage(stdout: str) -> dict[str, object]:
    envelope: object = json.loads(stdout)
    models = dig(envelope, "modelUsage")
    return {"subtype": dig(envelope, "subtype"), "is_error": dig(envelope, "is_error"),
            "usage": {field: dig(envelope, f"usage.{field}") for field in TOKENS},
            "total_cost_usd": dig(envelope, "total_cost_usd"),
            "model_usage_models": sorted(str(name) for name in models) if is_dict(models) else []}


def alive_group(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False  # macOS answers EPERM while only zombies remain.
    return True


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def state(pid: int) -> str:
    """running, zombie (exited, not yet reaped) or gone."""
    found = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return "gone" if not found else "zombie" if found.startswith("Z") else "running"


def lock_held(lock_path: Path) -> bool:
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(lock, fcntl.LOCK_UN)
        return False


@contextmanager
def deadline(seconds: int, reason: str) -> Generator[None]:
    """Raise TimeoutError in the block after seconds, and restore the previous SIGALRM handler however it ends."""
    def expire(signum: int, frame: object) -> None:
        raise TimeoutError(reason)

    previous = signal.signal(signal.SIGALRM, expire)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def wait_for_lock(lock_path: Path, limit: int) -> float | None:
    """Seconds until every holder of the issue lock is gone, as Foreman's next start would see it; None past the limit."""
    started = time.monotonic()
    try:
        with deadline(limit, "the lock stayed held"), lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            fcntl.flock(lock, fcntl.LOCK_UN)
    except TimeoutError:
        return None
    return round(time.monotonic() - started, 1)


def cut_off(scratch: Path, how: str) -> dict[str, object]:
    """Start a Bash-using call through Foreman's run_worker_command under a held issue lock, then end the stand-in
    once the call is mid-turn. SIGKILL is a crash Foreman cannot handle; SIGTERM is Foreman's own stop."""
    session = str(uuid4())
    fifo, lock_path = scratch / f"{how}.fifo", scratch / f"{how}.lock"
    os.mkfifo(fifo)
    command = (f"sh -c 'echo \"$$ $(ps -o pgid= -p $$) $([ -e /dev/fd/$PROBE_LOCK_FD ] && echo fd_open || echo fd_closed)\""
               f" > {fifo.name}; sleep 20; echo done'")
    prompt = f"Run exactly this one Bash command, then return the answer finished: {command}"
    args = claude_args(session, prompt, bash=True)
    stand_in = subprocess.Popen([sys.executable, "-c", STAND_IN, str(lock_path), json.dumps(args), str(scratch)],
                                stdout=subprocess.PIPE, text=True, cwd=scratch)
    assert stand_in.stdout is not None
    stand_in.stdout.readline()

    try:
        with deadline(180, "the call never ran its Bash command"), fifo.open(encoding="utf-8") as pipe:
            bash_pid, bash_pgid, fd_state = pipe.readline().split()
    except TimeoutError:
        # The stand-in's TERM handling ends the CLI's group before it exits, so nothing is left running.
        stand_in.terminate()
        stand_in.wait()
        raise
    found = subprocess.run(["pgrep", "-f", f"^claude .*{session}"], capture_output=True, text=True).stdout.split()
    cli_pgid = os.getpgid(int(found[0]))
    record = transcript(session)
    lines_at_signal = len(record.read_text(encoding="utf-8").splitlines()) if record else 0
    signalled = time.monotonic()
    stand_in.send_signal(signal.SIGKILL if how == "crash" else signal.SIGTERM)
    stand_in.wait()
    stand_in_exit = round(time.monotonic() - signalled, 1)
    after = {"cli_group_alive": alive_group(cli_pgid), "bash_child_alive": alive(int(bash_pid)), "issue_lock_held": lock_held(lock_path)}
    released = wait_for_lock(lock_path, 180)
    record = transcript(session)
    if record:
        save("claude", f"cut-off-{how}-shape.json", shape(record, sizes=True))
    return {"signal": "SIGKILL" if how == "crash" else "SIGTERM", "cli_processes_matching_session": len(found),
            "bash_child_in_cli_group": int(bash_pgid) == cli_pgid, "bash_child_leads_own_group": bash_pid == bash_pgid, "bash_child_is_not_cli": int(bash_pid) != cli_pgid,
            "lock_fd_in_bash_child": fd_state, "stand_in_exit_seconds": stand_in_exit, "right_after_signal": after,
            "lock_released_after_seconds": released, "cli_group_alive_after_release": alive_group(cli_pgid),
            "bash_child_alive_after_release": alive(int(bash_pid)),
            "record_lines_at_signal": lines_at_signal, "record": transcript_usage(record) if record else None}


def claude() -> None:
    scratch = Path(tempfile.mkdtemp(prefix=".scratch.probe-420.", dir=ROOT)).resolve()
    report: dict[str, object] = {"cli": subprocess.run(["claude", "--version"], capture_output=True, text=True).stdout.strip(),
                                 "model": MODEL}
    first = str(uuid4())
    done = subprocess.run(claude_args(first, "Reply with the single word ok."), cwd=scratch, capture_output=True, text=True,
                          env={**os.environ, "CLAUDE_CODE_NO_MODEL_FALLBACK": "1"})
    record = transcript(first)
    report["named_call"] = {"exit": done.returncode, "envelope": envelope_usage(done.stdout),
                            "session_id_matches": dig(json.loads(done.stdout), "session_id") == first,
                            "record_found_by_name": record is not None,
                            "record": transcript_usage(record) if record else None}
    if record:
        save("claude", "named-call-shape.json", shape(record, sizes=True))
    forked = str(uuid4())
    done = subprocess.run(claude_args(forked, "Reply with the single word again.", resume=first), cwd=scratch, capture_output=True,
                          text=True, env={**os.environ, "CLAUDE_CODE_NO_MODEL_FALLBACK": "1"})
    record = transcript(forked)
    report["named_fork"] = {"exit": done.returncode, "envelope": envelope_usage(done.stdout),
                            "session_id_matches": dig(json.loads(done.stdout), "session_id") == forked,
                            "record_found_by_name": record is not None, "record": transcript_usage(record) if record else None}
    for how in ("crash", "stop"):
        report[f"cut_off_{how}"] = cut_off(scratch, how)
    save("claude", "probe-report.json", report)


# Claude through the adapter's own command: the answer file #420's ruling names -------------------------------------

ADAPTER_STAND_IN = """
import fcntl, json, os, sys
from foreman.process_group import owned_group
lock_path, args, cwd, answer, prompt, env = sys.argv[1], json.loads(sys.argv[2]), *sys.argv[3:6], json.loads(sys.argv[6])
lock = open(lock_path, "a")
fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
os.environ["PROBE_LOCK_FD"] = str(lock.fileno())
with open(prompt) as stdin, open(answer, "w") as stdout, open(answer + ".stderr", "w") as stderr:
    with owned_group(args, cwd=cwd, env={**os.environ, **env}, stdin=stdin, stdout=stdout, stderr=stderr,
                     pass_fds=(lock.fileno(),)) as process:
        print(process.pid, flush=True)
        process.wait()
"""


def adapter_layout(base: Path, edit: bool) -> tuple[ClaudeAdapter, Path, Path, Path]:
    """A committed checkout, an attempt directory with its scratch, and an answers directory outside both."""
    checkout, attempt, answers = base / "checkout", base / "attempt", base / "answers"
    for folder in (checkout, attempt, answers):
        folder.mkdir()
    (checkout / "source.txt").write_text("original\n")
    for git_args in (["init", "-q"], ["add", "."], ["-c", "user.name=Probe", "-c", "user.email=probe@example.test", "commit", "-qm", "initial"]):
        subprocess.run(["git", *git_args], cwd=checkout, check=True)
    if edit:
        (attempt / "scratch").mkdir()
    return ClaudeAdapter(ModelSpec.parse(MODEL)), checkout, attempt, answers


def adapter_call(prompt: str, checkout: Path, attempt: Path, *, edit: bool, resume: str | None = None) -> Call:
    return Call(prompt, SCHEMA, edit=edit, temp_dir=attempt, budget_usd=BudgetUsd(Decimal("0.50")), resume_session=resume,
                git_dirs=git_dirs(checkout) if edit else ())


def launch(base: Path, args: list[str], checkout: Path, answer: Path, lock_path: Path, call: Call) -> tuple[subprocess.Popen[str], int]:
    """The stand-in for Foreman: holds the issue lock, hands it to the CLI and sends the CLI's answer to a named file."""
    prompt = base / "prompt.txt"
    prompt.write_text(args[-1])
    env = {**ClaudeAdapter.env, "TMPDIR": str(call.scratch if call.edit else call.temp_dir)}
    stand_in = subprocess.Popen([sys.executable, "-c", ADAPTER_STAND_IN, str(lock_path), json.dumps(args[:-1]), str(checkout),
                                 str(answer), str(prompt), json.dumps(env)], stdout=subprocess.PIPE, text=True)
    assert stand_in.stdout is not None
    return stand_in, int(stand_in.stdout.readline())


def holders(lock_path: Path) -> set[int]:
    found = subprocess.run(["lsof", "-t", str(lock_path)], capture_output=True, text=True).stdout.split()
    return {int(pid) for pid in found}


def recovered(answer: Path, call: Call) -> dict[str, object]:
    """The answer file read with the adapter's own read. The exit status died with the stand-in, so it reads as 0."""
    text = answer.read_text(encoding="utf-8")
    try:
        whole = is_dict(json.loads(text))
    except json.JSONDecodeError:
        whole = False
    outcome = ClaudeAdapter(ModelSpec.parse(MODEL)).read(subprocess.CompletedProcess([], 0, text, ""), call)
    session = dig(json.loads(text), "session_id") if whole else None
    record = transcript(session) if isinstance(session, str) else None
    return {"answer_bytes": len(text.encode()), "answer_is_one_json_object": whole,
            "envelope": envelope_usage(text) if whole else None,
            "adapter_outcome": outcome.kind, "adapter_output_keys": sorted(outcome.output),
            "adapter_usage": outcome.usage.model_dump() if outcome.usage else None,
            "record": transcript_usage(record) if record else None}


def adapter_cut_off(scratch: Path, how: str) -> dict[str, object]:
    """(a) and (d): SIGKILL or SIGTERM the stand-in while the call's sandboxed Bash command runs. The stand-in installs
    no TERM handler, so neither signal ends the CLI's group."""
    base = scratch / f"adapter-{how}"
    base.mkdir()
    adapter, checkout, attempt, answers = adapter_layout(base, edit=True)
    fifo, lock_path, answer = attempt / "scratch" / "started.fifo", base / "issue.lock", answers / "answer.json"
    os.mkfifo(fifo)
    command = f"sh -c 'echo \"$$\" > {fifo}; sleep 20; echo done'"
    call = adapter_call(f"Run exactly this one Bash command, then return the answer finished: {command}", checkout, attempt, edit=True)
    stand_in, cli_pid = launch(base, adapter.command(call), checkout, answer, lock_path, call)
    try:
        with deadline(180, "the call never ran its Bash command"), fifo.open(encoding="utf-8") as pipe:
            bash_pid = int(pipe.readline())
    except TimeoutError:
        os.killpg(cli_pid, signal.SIGKILL)
        stand_in.kill()
        stand_in.wait()
        raise
    held = holders(lock_path)
    cli_pgid, bash_pgid = os.getpgid(cli_pid), os.getpgid(bash_pid)
    signalled = time.monotonic()
    stand_in.send_signal(signal.SIGKILL if how == "crash" else signal.SIGTERM)
    stand_in.wait()
    stand_in_exit = round(time.monotonic() - signalled, 1)
    after = {"cli": state(cli_pid), "bash": state(bash_pid), "issue_lock_held": lock_held(lock_path),
             "answer_bytes": answer.stat().st_size}
    released = wait_for_lock(lock_path, 300)
    # Read at once: the answer must already be whole when the lock frees, since reconcile reads it then.
    at_release = {"cli": state(cli_pid), "bash": state(bash_pid), "answer_bytes": answer.stat().st_size}
    return {"signal": "SIGKILL" if how == "crash" else "SIGTERM", "stand_in_exit_seconds": stand_in_exit,
            "bash_in_cli_group": bash_pgid == cli_pgid, "lock_holders_while_bash_ran": {
                "cli": cli_pid in held, "bash": bash_pid in held, "others": len(held - {cli_pid, bash_pid, stand_in.pid})},
            "right_after_signal": after, "lock_released_after_seconds": released, "at_release": at_release,
            **recovered(answer, call)}


def adapter_write_attempt(scratch: Path) -> dict[str, object]:
    """(b): the call's Bash and its Write and Edit tools each try to write the answer file and a file beside it."""
    base = scratch / "adapter-write"
    base.mkdir()
    adapter, checkout, attempt, answers = adapter_layout(base, edit=True)
    lock_path, answer, beside = base / "issue.lock", answers / "answer.json", answers / "beside.txt"
    beside.write_text("original\n")
    steps = (f"1. Bash: echo probe >> {answer}\n2. Bash: echo probe > {beside}\n3. Write tool: write the text probe to {answer}\n"
             f"4. Edit tool: in {beside} replace original with probe")
    call = adapter_call(f"Try each of these four steps once, even if one fails, then return the answer finished.\n{steps}",
                        checkout, attempt, edit=True)
    stand_in, _ = launch(base, adapter.command(call), checkout, answer, lock_path, call)
    stand_in.wait()
    text = answer.read_text(encoding="utf-8")
    denials = dig(json.loads(text), "permission_denials") if text.lstrip().startswith("{") else None
    return {"beside_unchanged": beside.read_text() == "original\n", "answers_dir_files": len(list(answers.iterdir())),
            "permission_denials_by_tool": sorted(str(dig(item, "tool_name")) for item in denials) if is_list(denials) else None,
            **recovered(answer, call)}


def fork_cost(scratch: Path) -> dict[str, object]:
    """Item 2: a read-only parent call, then the fork a repair turn makes, both through the adapter's command."""
    base = scratch / "adapter-fork"
    base.mkdir()
    adapter, checkout, attempt, answers = adapter_layout(base, edit=False)
    report: dict[str, object] = {}
    session: str | None = None
    for name in ("parent", "fork"):
        call = adapter_call("Return the answer finished.", checkout, attempt, edit=False, resume=session)
        answer = answers / f"{name}.json"
        stand_in, _ = launch(base, adapter.command(call), checkout, answer, base / "issue.lock", call)
        stand_in.wait()
        envelope: object = json.loads(answer.read_text(encoding="utf-8"))
        models = dig(envelope, "modelUsage")
        report[name] = {**recovered(answer, call), "model_usage_cost_usd": sorted(
            cost for usage in (models.values() if is_dict(models) else ()) if isinstance(cost := dig(usage, "costUSD"), float))}
        found = dig(envelope, "session_id")
        session = found if isinstance(found, str) else None
    return report


def claude_adapter() -> None:
    scratch = Path(tempfile.mkdtemp(prefix=".scratch.probe-420.", dir=ROOT)).resolve()
    report: dict[str, object] = {"cli": subprocess.run(["claude", "--version"], capture_output=True, text=True).stdout.strip(),
                                 "model": MODEL, "command": "ClaudeAdapter.command, edit call (sandbox, --settings, --restricted)"}
    report["a_crash"] = adapter_cut_off(scratch, "crash")
    report["b_write_attempt"] = adapter_write_attempt(scratch)
    report["d_term"] = adapter_cut_off(scratch, "term")
    report["fork_cost"] = fork_cost(scratch)
    save("claude", "adapter-report.json", report)


# Codex and Grok: records already on disk ------------------------------------------------------------------------------

def merge(shapes: list[dict[str, object]], focus: tuple[str, ...]) -> dict[str, object]:
    """Field paths for the labels the probe asks about. Other labels are named only, because a real record's other
    fields include maps keyed by its own data, such as configured server names."""
    merged: dict[str, set[str]] = {}
    for one in shapes:
        labels = one["labels"]
        assert is_dict(labels)
        for name, keys in labels.items():
            assert is_list(keys)
            merged.setdefault(str(name), set()).update(str(key) for key in keys)
    return {name: sorted(keys) if any(part in name for part in focus) else "fields not recorded"
            for name, keys in sorted(merged.items())}


def spread(items: list[Path], count: int) -> list[Path]:
    """Records spread evenly over time, so one period's failures don't stand for every record."""
    ordered = sorted(items, key=lambda path: path.stat().st_mtime)
    return ordered if len(ordered) <= count else [ordered[round(i * (len(ordered) - 1) / (count - 1))] for i in range(count)]


CODEX_FOCUS = ("session_meta", "task_started", "task_complete", "turn_aborted", "token_count", "token_usage_record", "payload.type=error")
GROK_FOCUS = {"usage.json": ("",), "summary.json": ("",), "events.jsonl": ("turn_",),
              "updates.jsonl": ("turn_completed",)}


def codex(count: int = 40) -> None:
    sessions = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "sessions"
    rollouts = spread(list(sessions.glob("*/*/*/rollout-*.jsonl")), count)
    version = subprocess.run(["codex", "--version"], capture_output=True, text=True).stdout.strip()
    save("codex", "rollout-shape.json", {
        "cli": version, "layout": "$CODEX_HOME/sessions/YYYY/MM/DD/" + scrub(rollouts[-1].name) if rollouts else None,
        "rollouts_read": len(rollouts), "labels": merge([shape(path, sizes=False) for path in rollouts], CODEX_FOCUS)})


def grok(count: int = 40) -> None:
    home = Path(os.environ.get("GROK_HOME") or Path.home() / ".grok") / "sessions"
    directories = spread([path for path in home.glob("*/*") if path.is_dir()], count)
    files: dict[str, list[dict[str, object]]] = {}
    for directory in directories:
        for path in directory.iterdir():
            if path.is_file() and path.suffix in (".json", ".jsonl"):
                files.setdefault(scrub(path.name), []).append(shape(path, sizes=False))
            elif path.is_file() or path.is_dir():
                files.setdefault(scrub(path.name) + ("/" if path.is_dir() else ""), [])
    version = subprocess.run(["grok", "--version"], capture_output=True, text=True).stdout.strip()
    save("grok", "session-shape.json", {
        "cli": version, "layout": "$GROK_HOME/sessions/<encoded-cwd>/<session-id>/", "sessions_read": len(directories),
        "files": {name: merge(shapes, GROK_FOCUS.get(name, ())) if shapes else "not JSON" for name, shapes in sorted(files.items())}})


if __name__ == "__main__":
    {"claude": claude, "claude-adapter": claude_adapter, "codex": codex, "grok": grok}[sys.argv[1]]()

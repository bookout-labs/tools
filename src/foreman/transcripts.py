"""Transcript parsers and redacted evidence condensers across Claude, Codex, and Grok JSONL logs."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
from typing import Any, Mapping

from foreman.redaction import SecretRedactor, redact_text

SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,}$")
PROVIDER_ROOT = {"gpt": "codex", "codex": "codex", "claude": "claude", "grok": "grok"}


def _transcript_provider(model: object) -> str | None:
    return PROVIDER_ROOT.get(model.split("-", 1)[0]) if isinstance(model, str) else None


def default_transcript_roots() -> dict[str, Path]:
    home = Path.home()
    return {
        "claude": Path(os.environ.get("CLAUDE_CONFIG_DIR") or home / ".claude") / "projects",
        "codex": Path(os.environ.get("CODEX_HOME") or home / ".codex") / "sessions",
        "grok": Path(os.environ.get("GROK_HOME") or home / ".grok") / "sessions",
    }


def locate_session_transcript(
    session_id: str,
    *,
    provider: str | None = None,
    search_roots: Mapping[str, Path] | None = None,
) -> Path | None:
    """Safely resolve a session transcript file under provider roots without traversal."""
    if not isinstance(session_id, str) or not SAFE_SESSION_ID.fullmatch(session_id):
        return None
    roots = search_roots if search_roots is not None else default_transcript_roots()
    provider = "codex" if provider == "gpt" else provider
    patterns = {
        "claude": (f"{session_id}.jsonl",),
        "codex": (f"{session_id}.jsonl", f"*-{session_id}.jsonl"),
        "grok": (f"{session_id}/chat_history.jsonl",),
    }
    found: Path | None = None
    for name, root in roots.items():
        if (provider is not None and name != provider) or root.is_symlink() or not root.is_dir():
            continue
        resolved_root = root.resolve()
        for pattern in patterns.get(name, ()):
            for candidate in root.rglob(pattern):
                try:
                    if any(path.is_symlink() for path in (candidate, *candidate.parents) if path.is_relative_to(root)):
                        continue
                    if not candidate.is_file() or not candidate.resolve().is_relative_to(resolved_root):
                        continue
                except (OSError, RuntimeError):
                    continue
                if found is not None and candidate != found:
                    return None
                found = candidate
    return found


def _clip_line(text: object, limit: int = 240) -> str:
    return " ".join(str(text or "").split())[:limit]


def _truncate_lines(lines: list[str], max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    out: list[str] = []
    used = 0
    for line in lines:
        needed = len(line) + (1 if out else 0)
        if used + needed > max_chars:
            remaining = max_chars - used - (1 if out else 0)
            if remaining > 24:
                out.append(line[:remaining - 3] + "...")
            break
        out.append(line)
        used += needed
    return "\n".join(out)


def _load_jsonl_text(source: Path | str) -> str:
    if isinstance(source, Path):
        return source.read_text(encoding="utf-8", errors="replace")
    return str(source or "")


def _is_harness_boilerplate(text: str) -> bool:
    stripped = text.lstrip()
    return stripped.startswith((
        "<system-reminder>",
        "<user_info>",
        "<rules>",
        "<environment_context>",
        "<skills_instructions>",
        "<multi_agent_role>",
        "<multi_agent_mode>",
    ))


def condense_claude_jsonl(source: Path | str, *, max_chars: int = 8000) -> str:
    """Condense a Claude Code session JSONL transcript into compact redacted action lines."""
    redactor = SecretRedactor.from_environ()
    raw = _load_jsonl_text(source)
    lines_out: list[str] = []
    for raw_line in raw.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            ev = json.loads(stripped)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        ev_type = str(ev.get("type") or "")
        if ev_type == "system":
            continue
        message = ev.get("message") if isinstance(ev.get("message"), dict) else {}
        role = str(message.get("role") or ev_type or "event")
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text" and isinstance(block.get("text"), str):
                    if _is_harness_boilerplate(block["text"]):
                        continue
                    snippet = _clip_line(block["text"])
                    if snippet:
                        lines_out.append(f"[{role}] {snippet}")
                elif btype == "tool_use":
                    name = block.get("name") or "tool"
                    inp = block.get("input") if isinstance(block.get("input"), dict) else {}
                    summary_arg = inp.get("file_path") or inp.get("path") or inp.get("command") or ""
                    lines_out.append(f"[tool:{name}] {_clip_line(summary_arg, 160)}")
                elif btype == "tool_result":
                    if block.get("is_error"):
                        lines_out.append(f"[tool_error] {_clip_line(block.get('content'), 200)}")
        elif isinstance(content, str) and content.strip():
            if _is_harness_boilerplate(content):
                continue
            lines_out.append(f"[{role}] {_clip_line(content)}")
    scrubbed = redactor.redact(_truncate_lines(lines_out, max_chars), redact_pii=True)
    return scrubbed


def condense_codex_jsonl(source: Path | str, *, max_chars: int = 8000) -> str:
    """Condense a Codex CLI JSONL session rollout or exec event stream into redacted lines."""
    redactor = SecretRedactor.from_environ()
    raw = _load_jsonl_text(source)
    lines_out: list[str] = []
    for raw_line in raw.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            ev = json.loads(stripped)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        ev_type = str(ev.get("type") or "")
        payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
        item = ev.get("item") if isinstance(ev.get("item"), dict) else {}
        if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
            lines_out.append(f"[assistant] {_clip_line(item['text'])}")
        elif item.get("type") == "command_execution":
            cmd = _clip_line(item.get("command"), 140)
            exit_code = item.get("exit_code")
            lines_out.append(f"[cmd exit={exit_code}] {cmd}")
        elif item.get("type") == "file_change":
            lines_out.append(f"[file] {_clip_line(item.get('changes') or item.get('path'), 160)}")
        elif ev_type == "response_item":
            ptype = payload.get("type")
            if ptype == "message" and payload.get("role") in {"assistant", "user"}:
                role = str(payload["role"])
                for block in payload.get("content") or []:
                    if not isinstance(block, dict):
                        continue
                    text = block.get("text")
                    if isinstance(text, str) and text.strip() and not _is_harness_boilerplate(text):
                        lines_out.append(f"[{role}] {_clip_line(text)}")
            elif ptype in {"function_call", "custom_tool_call"}:
                name = payload.get("name") or "tool"
                args = payload.get("arguments") or payload.get("input") or ""
                lines_out.append(f"[tool:{name}] {_clip_line(args, 160)}")
            elif ptype in {"function_call_output", "custom_tool_call_output"}:
                out_text = str(payload.get("output") or "")
                if any(tok in out_text.lower() for tok in ("error", "fail", "exception", "traceback")):
                    lines_out.append(f"[tool_error] {_clip_line(out_text, 200)}")
        elif ev_type == "event_msg" and payload.get("type") == "agent_message":
            lines_out.append(f"[assistant] {_clip_line(payload.get('message'))}")
        elif ev_type == "event_msg" and payload.get("type") in {"exec_command_end", "exec_command"}:
            lines_out.append(f"[cmd] {_clip_line(payload.get('command') or payload.get('aggregated_output'), 160)}")
        elif ev_type == "event_msg" and payload.get("type") == "token_count":
            info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
            if isinstance(info.get("total_token_usage"), dict):
                lines_out.append(f"[usage] {json.dumps(info['total_token_usage'], sort_keys=True)}")
        elif ev_type == "turn.completed" and isinstance(ev.get("usage"), dict):
            lines_out.append(f"[usage] {json.dumps(ev['usage'], sort_keys=True)}")
    return redactor.redact(_truncate_lines(lines_out, max_chars), redact_pii=True)


def condense_grok_jsonl(source: Path | str, *, max_chars: int = 8000) -> str:
    """Condense a Grok chat_history.jsonl transcript into compact redacted action lines."""
    redactor = SecretRedactor.from_environ()
    raw = _load_jsonl_text(source)
    lines_out: list[str] = []
    for raw_line in raw.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            ev = json.loads(stripped)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        role = str(ev.get("role") or ev.get("type") or "event")
        if role == "system":
            continue
        content = ev.get("content")
        if isinstance(content, str) and content.strip():
            if not _is_harness_boilerplate(content):
                lines_out.append(f"[{role}] {_clip_line(content)}")
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    txt = block["text"]
                    if not _is_harness_boilerplate(txt):
                        lines_out.append(f"[{role}] {_clip_line(txt)}")
        for tc in ev.get("tool_calls") or []:
            if isinstance(tc, dict):
                fn = tc.get("function") if isinstance(tc.get("function"), dict) else tc
                lines_out.append(f"[tool:{fn.get('name', 'tool')}] {_clip_line(fn.get('arguments'), 140)}")
    return redactor.redact(_truncate_lines(lines_out, max_chars), redact_pii=True)


def condense_transcript(path: Path, *, max_chars: int = 8000) -> str:
    """Detect transcript dialect (Claude, Codex, or Grok) and condense safely."""
    raw = _load_jsonl_text(path)
    if path.name == "chat_history.jsonl":
        return condense_grok_jsonl(raw, max_chars=max_chars)
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            first = json.loads(stripped)
        except ValueError:
            continue
        if not isinstance(first, dict):
            continue
        if first.get("type") in {"session_meta", "thread.started", "event_msg", "item.completed"}:
            return condense_codex_jsonl(raw, max_chars=max_chars)
        if "message" in first or first.get("type") in {"assistant", "user", "system"}:
            return condense_claude_jsonl(raw, max_chars=max_chars)
        if "role" in first:
            return condense_grok_jsonl(raw, max_chars=max_chars)
    return redact_text(raw[:max_chars], redact_pii=True)


def build_run_evidence(
    run: Mapping[str, Any],
    *,
    stage_calls: list[dict[str, Any]] | None = None,
    search_roots: Mapping[str, Path] | None = None,
    diff_text: str = "",
    max_chars: int = 12000,
) -> str:
    """Assemble redacted run summary, stage trace, transcripts, and diff for judge scorers."""
    state = run.get("state") if isinstance(run.get("state"), dict) else {}
    parts: list[str] = [
        f"Run: {run.get('run_id', '')}",
        f"Repo: {run.get('repo', '')}  Issue: {run.get('issue', '')}  Host: {run.get('host', '')}",
        f"Stop: {run.get('stop', '')}  Verify rounds: {run.get('verify_rounds', 0)}  Review rounds: {run.get('review_rounds', 0)}",
        f"Tokens: {run.get('total_tokens', 0)}  Ref cost USD: {run.get('cost_usd', 0.0)}  Duration ms: {run.get('duration_ms', 0)}",
    ]
    if title := state.get("request_title"):
        parts.append(f"Request title: {title}")
    if halt_detail := state.get("halt_detail"):
        parts.append(f"Halt detail: {halt_detail}")
    if check_out := state.get("check_output"):
        parts.append(f"Check output:\n{str(check_out)[-1500:]}")
    if models := run.get("models_used"):
        parts.append(f"Models used: {json.dumps(models, sort_keys=True)}")
    if risk := state.get("risk"):
        parts.append(f"Merge risk: {json.dumps(risk, sort_keys=True)}")
    if note := state.get("review_note"):
        parts.append(f"Review note: {note}")

    if stage_calls:
        call_lines = [
            f"  #{c.get('seq')} {c.get('node')} model={c.get('model')} status={c.get('status')} "
            f"tokens={c.get('total_tokens')} ms={c.get('duration_ms')}"
            for c in stage_calls
        ]
        parts.append("Stage calls:\n" + "\n".join(call_lines))

    models = run.get("models_used") or state.get("models_used") or {}
    if not isinstance(models, dict):
        models = {}
    sessions = state.get("sessions") if isinstance(state.get("sessions"), dict) else {}
    session_sources: dict[str, set[str]] = {}

    def add_session(sid: object, model: object) -> None:
        if not isinstance(sid, str) or not sid.strip():
            return
        providers = session_sources.setdefault(sid.strip(), set())
        if provider := _transcript_provider(model):
            providers.add(provider)

    for call in stage_calls or []:
        add_session(call.get("session_id"), call.get("provider") or call.get("model") or models.get(call.get("node")))
    for node, sid in sessions.items():
        if isinstance(sid, str) and not session_sources.get(sid.strip()):
            add_session(sid, models.get(node))
    top_sid = run.get("session_id")
    if isinstance(top_sid, str) and not session_sources.get(top_sid.strip()):
        providers = {_transcript_provider(model) for model in models.values()}
        add_session(top_sid, providers.pop() if len(providers) == 1 else None)
    targets = [
        (sid, provider)
        for sid, providers in session_sources.items()
        for provider in sorted(providers) or [None]
    ]

    for sid, provider in targets[:4]:
        path = locate_session_transcript(sid, provider=provider, search_roots=search_roots)
        if path is not None:
            condensed = condense_transcript(path, max_chars=3000)
            if condensed:
                parts.append(f"Transcript ({sid}):\n{condensed}")

    if diff_text.strip():
        parts.append(f"Diff excerpt:\n{diff_text.strip()[:3000]}")

    combined = redact_text("\n\n".join(parts), redact_pii=True)
    return combined[:max_chars]

"""Transcript parsers and redacted evidence condensers across Claude, Codex, and Grok JSONL logs."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
from typing import Any, Mapping

from foreman.redaction import SecretRedactor, redact_text

SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9._-]+$")


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
    search_roots: Mapping[str, Path] | None = None,
) -> Path | None:
    """Safely resolve a session transcript file under provider roots without traversal."""
    if not isinstance(session_id, str) or not session_id or not SAFE_SESSION_ID.fullmatch(session_id):
        return None
    roots = search_roots if search_roots is not None else default_transcript_roots()
    for provider, root in roots.items():
        if not root.exists():
            continue
        if provider == "grok":
            candidates = [
                root / session_id / "chat_history.jsonl",
                *sorted(root.glob(f"*/{session_id}/chat_history.jsonl")),
            ]
            for cand in candidates:
                if cand.is_file():
                    return cand
        for pattern in (f"*{session_id}*.jsonl", f"**/*{session_id}*.jsonl"):
            matches = sorted(root.glob(pattern))
            for cand in matches:
                if cand.is_file():
                    return cand
    return None


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
        message = ev.get("message") if isinstance(ev.get("message"), dict) else {}
        role = str(message.get("role") or ev_type or "event")
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text" and isinstance(block.get("text"), str):
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
            lines_out.append(f"[{role}] {_clip_line(content)}")
    scrubbed = redactor.redact(_truncate_lines(lines_out, max_chars), redact_pii=True)
    return scrubbed


def condense_codex_jsonl(source: Path | str, *, max_chars: int = 8000) -> str:
    """Condense a Codex CLI JSONL session or exec event stream into compact redacted lines."""
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
        elif ev_type == "event_msg" and payload.get("type") == "agent_message":
            lines_out.append(f"[assistant] {_clip_line(payload.get('message'))}")
        elif ev_type == "event_msg" and payload.get("type") in {"exec_command_end", "exec_command"}:
            lines_out.append(f"[cmd] {_clip_line(payload.get('command') or payload.get('aggregated_output'), 160)}")
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
        content = ev.get("content")
        if isinstance(content, str) and content.strip():
            lines_out.append(f"[{role}] {_clip_line(content)}")
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

    seen_sessions: set[str] = set()
    session_ids: list[str] = []
    for c in stage_calls or []:
        sid = str(c.get("session_id") or "").strip()
        if sid and sid not in seen_sessions:
            seen_sessions.add(sid)
            session_ids.append(sid)
    for sid in ((state.get("sessions") or {}).values() if isinstance(state.get("sessions"), dict) else []):
        if isinstance(sid, str) and sid.strip() and sid.strip() not in seen_sessions:
            seen_sessions.add(sid.strip())
            session_ids.append(sid.strip())
    if top_sid := str(run.get("session_id") or "").strip():
        if top_sid not in seen_sessions:
            session_ids.append(top_sid)

    for sid in session_ids[:4]:
        path = locate_session_transcript(sid, search_roots=search_roots)
        if path is not None:
            condensed = condense_transcript(path, max_chars=3000)
            if condensed:
                parts.append(f"Transcript ({sid}):\n{condensed}")

    if diff_text.strip():
        parts.append(f"Diff excerpt:\n{diff_text.strip()[:3000]}")

    combined = redact_text("\n\n".join(parts), redact_pii=True)
    return combined[:max_chars]

"""Annotate unified diffs and validate inline PR review comments.

Adapted from Warp's review_validation / validate_review_json patterns so
inline comments target real diff lines and suggestion blocks never duplicate
adjacent context lines.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, TypedDict


class ReviewComment(TypedDict, total=False):
    """Normalized review comment accepted by GitHub's create-review API."""

    path: str
    line: int
    side: str
    body: str
    start_line: int
    start_side: str


@dataclass(frozen=True)
class ReviewValidationResult:
    """Validated review fields plus any comment-location errors."""

    body: str
    comments: list[ReviewComment]
    errors: list[str]

    @property
    def is_valid(self) -> bool:
        return not self.errors


HUNK_HEADER_PATTERN = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? \+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@"
)
SUGGESTION_BLOCK_PATTERN = re.compile(
    r"```suggestion[^\n]*\r?\n(?P<content>.*?)\r?\n```",
    re.DOTALL,
)
ANNOTATED_OLD_PATTERN = re.compile(r"^\[OLD:(?P<old>\d+)\] ?(?P<text>.*)$")
ANNOTATED_NEW_PATTERN = re.compile(r"^\[NEW:(?P<new>\d+)\] ?(?P<text>.*)$")
ANNOTATED_CONTEXT_PATTERN = re.compile(
    r"^\[OLD:(?P<old>\d+),NEW:(?P<new>\d+)\] ?(?P<text>.*)$"
)
DIFF_PREFIX = re.compile(r"^[ab]/")
DiffHunkMap = dict[str, dict[str, dict[int, int]]]


def normalize_review_path(value: Any) -> str:
    return str(value or "").strip().removeprefix("./")


def annotate_unified_diff(diff_text: str) -> str:
    """Prefix unified diff hunk lines with explicit OLD/NEW line anchors."""
    if not diff_text:
        return ""
    out: list[str] = []
    old_line = new_line = old_rem = new_rem = 0

    for raw_line in diff_text.splitlines():
        hunk = HUNK_HEADER_PATTERN.match(raw_line)
        if hunk:
            old_line = int(hunk.group("old_start"))
            new_line = int(hunk.group("new_start"))
            old_rem = int(hunk.group("old_count") or 1)
            new_rem = int(hunk.group("new_count") or 1)
            out.append(raw_line)
            continue
        if old_rem <= 0 and new_rem <= 0:
            out.append(raw_line)
            continue
        marker = raw_line[:1]
        text = raw_line[1:]
        if marker == "-":
            out.append(f"[OLD:{old_line}] {text}")
            old_line += 1
            old_rem -= 1
        elif marker == "+":
            out.append(f"[NEW:{new_line}] {text}")
            new_line += 1
            new_rem -= 1
        elif marker == " ":
            out.append(f"[OLD:{old_line},NEW:{new_line}] {text}")
            old_line += 1
            new_line += 1
            old_rem -= 1
            new_rem -= 1
        else:
            out.append(raw_line)
            if raw_line.startswith("diff --git "):
                old_rem = new_rem = 0
    return "\n".join(out)


def build_diff_maps_from_annotated_diff(
    diff_text: str,
) -> tuple[dict[str, dict[str, set[int]]], dict[str, dict[str, dict[int, str]]]]:
    """Build valid commentable-line and line-content maps from an annotated diff."""
    line_map, content_map, _ = _build_diff_maps(diff_text)
    return line_map, content_map


def build_diff_hunk_maps(diff_text: str) -> DiffHunkMap:
    """Map each commentable line to its hunk within the file."""
    _, _, hunk_map = _build_diff_maps(annotate_unified_diff(diff_text))
    return hunk_map


def _build_diff_maps(diff_text: str) -> tuple[
    dict[str, dict[str, set[int]]], dict[str, dict[str, dict[int, str]]], DiffHunkMap,
]:
    diff_line_map: dict[str, dict[str, set[int]]] = {}
    diff_content_map: dict[str, dict[str, dict[int, str]]] = {}
    diff_hunk_map: DiffHunkMap = {}
    current_path = ""
    old_path = ""
    hunk_index = 0

    def put(side: str, line: int, text: str) -> None:
        diff_line_map[current_path][side].add(line)
        diff_content_map[current_path][side][line] = text
        diff_hunk_map[current_path][side][line] = hunk_index

    for raw_line in diff_text.splitlines():
        if raw_line.startswith("diff --git "):
            current_path = ""
            old_path = ""
            continue
        if raw_line.startswith("--- "):
            candidate = raw_line[4:].strip()
            old_path = "" if candidate == "/dev/null" else DIFF_PREFIX.sub("", candidate)
            continue
        if raw_line.startswith("+++ "):
            candidate = raw_line[4:].strip()
            current_path = old_path if candidate == "/dev/null" else DIFF_PREFIX.sub("", candidate)
            if current_path:
                diff_line_map.setdefault(current_path, {"LEFT": set(), "RIGHT": set()})
                diff_content_map.setdefault(current_path, {"LEFT": {}, "RIGHT": {}})
                diff_hunk_map.setdefault(current_path, {"LEFT": {}, "RIGHT": {}})
            continue
        if not current_path:
            continue
        if HUNK_HEADER_PATTERN.match(raw_line):
            hunk_index += 1
        elif match := ANNOTATED_OLD_PATTERN.match(raw_line):
            put("LEFT", int(match.group("old")), match.group("text"))
        elif match := ANNOTATED_NEW_PATTERN.match(raw_line):
            put("RIGHT", int(match.group("new")), match.group("text"))
        elif match := ANNOTATED_CONTEXT_PATTERN.match(raw_line):
            put("LEFT", int(match.group("old")), match.group("text"))
            put("RIGHT", int(match.group("new")), match.group("text"))

    return diff_line_map, diff_content_map, diff_hunk_map


def _validate_suggestion_blocks(
    comment: ReviewComment,
    diff_content_map: dict[str, dict[str, dict[int, str]]],
) -> list[str]:
    errors: list[str] = []
    body = comment.get("body") or ""
    blocks = [
        [ln.rstrip("\r") for ln in m.group("content").split("\n")]
        for m in SUGGESTION_BLOCK_PATTERN.finditer(body)
    ]
    if not blocks:
        return errors

    path = comment.get("path") or ""
    side = comment.get("side") or "RIGHT"
    start_side = comment.get("start_side") or side
    line_no = comment.get("line")
    if not isinstance(line_no, int):
        return errors
    start_line = comment.get("start_line") or line_no
    content_for_start = diff_content_map.get(path, {}).get(start_side, {})
    content_for_end = diff_content_map.get(path, {}).get(side, {})

    for idx, block_lines in enumerate(blocks):
        if not block_lines or block_lines == [""]:
            continue
        prev_context = content_for_start.get(start_line - 1)
        next_context = content_for_end.get(line_no + 1)
        if prev_context is not None and block_lines[0] == prev_context:
            errors.append(
                f"suggestion block {idx} duplicates context line {start_line - 1} above start_line"
            )
        if next_context is not None and block_lines[-1] == next_context:
            errors.append(
                f"suggestion block {idx} duplicates context line {line_no + 1} below line"
            )
    return errors


def validate_review_comments(
    raw_comments: Any,
    diff_line_map: dict[str, dict[str, set[int]]],
    diff_content_map: dict[str, dict[str, dict[int, str]]] | None = None,
    *,
    body: str = "",
    diff_hunk_map: DiffHunkMap | None = None,
) -> ReviewValidationResult:
    """Validate inline review comments against diff_line_map and diff_content_map."""
    if not isinstance(raw_comments, list):
        raise ValueError("Review comments must be a list.")

    normalized: list[ReviewComment] = []
    errors: list[str] = []

    for index, raw_comment in enumerate(raw_comments):
        if not isinstance(raw_comment, dict):
            errors.append(f"comments[{index}] must be an object.")
            continue

        path = normalize_review_path(raw_comment.get("path"))
        if path not in diff_line_map and (stripped := DIFF_PREFIX.sub("", path)) in diff_line_map:
            path = stripped
        line = raw_comment.get("line")
        raw_body = raw_comment.get("body")
        comment_body = raw_body.strip() if isinstance(raw_body, str) else ""
        side = raw_comment.get("side")

        if not path:
            errors.append(f"comments[{index}] is missing path.")
            continue
        if path not in diff_line_map:
            errors.append(f"comments[{index}] references {path}, which is not in the diff.")
            continue
        if not isinstance(line, int) or isinstance(line, bool) or line <= 0:
            errors.append(f"comments[{index}] for {path} must have a positive integer line.")
            continue
        if side not in {"LEFT", "RIGHT"}:
            errors.append(f"comments[{index}] for {path}:{line} must set side to LEFT or RIGHT.")
            continue
        if not comment_body:
            errors.append(f"comments[{index}] for {path}:{line} is missing body.")
            continue
        if line not in diff_line_map[path][side]:
            errors.append(f"comments[{index}] references {path}:{line} on {side}, which is not commentable.")
            continue

        item: ReviewComment = {"path": path, "line": line, "side": side, "body": comment_body}

        start_line = raw_comment.get("start_line")
        if start_line is not None:
            if not isinstance(start_line, int) or isinstance(start_line, bool) or start_line <= 0:
                errors.append(f"comments[{index}] for {path} has invalid start_line.")
                continue
            start_side = raw_comment.get("start_side")
            if start_side not in {"LEFT", "RIGHT"}:
                errors.append(f"comments[{index}] for {path} has start_line without valid start_side.")
                continue
            if start_side == side and start_line >= line:
                errors.append(f"comments[{index}] for {path} must have start_line < line.")
                continue
            if start_line not in diff_line_map[path][start_side]:
                errors.append(f"comments[{index}] start_line {path}:{start_line} on {start_side} is not commentable.")
                continue
            if diff_hunk_map is not None:
                hunks = diff_hunk_map.get(path, {})
                start_hunk = hunks.get(start_side, {}).get(start_line)
                end_hunk = hunks.get(side, {}).get(line)
                if start_hunk is None or start_hunk != end_hunk:
                    errors.append(f"comments[{index}] for {path} must start and end in the same hunk.")
                    continue
            item["start_line"] = start_line
            item["start_side"] = start_side
        elif raw_comment.get("start_side") is not None:
            errors.append(f"comments[{index}] for {path}:{line} has start_side without start_line.")
            continue

        if diff_content_map is not None:
            suggestion_errors = _validate_suggestion_blocks(item, diff_content_map)
            if suggestion_errors:
                errors.extend(f"comments[{index}] ({path}:{line}): {err}" for err in suggestion_errors)
                continue

        normalized.append(item)

    return ReviewValidationResult(body=body.strip(), comments=normalized, errors=errors)


def validate_review_payload(
    review: Any,
    diff_line_map: dict[str, dict[str, set[int]]],
    diff_content_map: dict[str, dict[str, dict[int, str]]] | None = None,
) -> ReviewValidationResult:
    """Validate a full review.json payload against an annotated PR diff."""
    if not isinstance(review, dict):
        raise ValueError("Review payload must be a JSON object.")
    raw_body = review.get("body")
    if raw_body is None:
        raw_body = review.get("summary") or ""
    if not isinstance(raw_body, str):
        raise ValueError("Review payload body must be a string.")
    raw_comments = review.get("comments") or []
    return validate_review_comments(raw_comments, diff_line_map, diff_content_map, body=raw_body)

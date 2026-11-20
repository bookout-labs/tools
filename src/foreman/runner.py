"""Run one delivery issue against its SQLite checkpoint."""

import json
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Callable
from uuid import NAMESPACE_URL, uuid4, uuid5

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from foreman.delivery import FULL_SHA, DeliveryState, Slice, clean_slices, compile_delivery, render_pr_body, review_comment
from foreman.dispatch import FailoverDispatcher, InvalidOutput
from foreman.github import GhClient, GitHub
from foreman.redaction import SecretRedactor, redact_text
from foreman.telemetry import TelemetryStore, utcnow_iso

WorktreeInspector = Callable[[Path], tuple[str, bool]]
SLICE_FENCE = re.compile(r"^```json[ \t]*\r?\n((?:(?!^```)[^\n]*\n)*)^```[ \t]*\r?$", re.MULTILINE)


def _load_slice(body: str) -> Slice:
    blocks = SLICE_FENCE.findall(body)
    if not blocks:
        raise InvalidOutput("slice issue needs a fenced JSON object")
    return clean_slices([json.loads(blocks[-1])], allow_external_dependencies=True)[0]


def _inspect_worktree(cwd: Path) -> tuple[str, bool]:
    head = subprocess.run(["git", "rev-parse", "HEAD"], shell=False, cwd=cwd, capture_output=True, text=True)
    status = subprocess.run(["git", "status", "--porcelain"], shell=False, cwd=cwd, capture_output=True, text=True)
    return head.stdout.strip(), head.returncode == status.returncode == 0 and not status.stdout


def _run(cwd: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    return subprocess.run(
        args, cwd=cwd, shell=False, capture_output=True, text=True,
        stdin=subprocess.DEVNULL, env=env,
    )


def _execute(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return _run(cwd, args)


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return _run(cwd, ["git", *args])


def _branch_slug(title: str) -> str:
    words = re.findall(r"[a-z0-9]+", title.lower())
    slug = "-".join(words)
    if len(slug) > 40:
        slug = slug[:40].rsplit("-", 1)[0]
    return slug or "change"


def commit_implementation(cwd: Path, issue: int, title: str) -> str:
    """Record the implementation on its slice branch.

    A linked worktree keeps its git directory outside the worker sandbox, so
    the worker can edit files and still be unable to write the index. This
    process can. A clean worktree already has the worker's commit.
    """
    branch = _git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    if branch.returncode != 0:
        raise InvalidOutput("implement worktree is not a git checkout")
    name = branch.stdout.strip()
    if name == "HEAD" or not name.startswith(f"{issue}-"):
        name = f"{issue}-{_branch_slug(title)}"
        created = _git(cwd, "checkout", "-b", name)
        if created.returncode != 0:
            detail = " ".join((created.stderr or created.stdout).split())
            raise InvalidOutput(f"implement could not create branch {name}: {detail}")
    status = _git(cwd, "status", "--porcelain")
    head = _git(cwd, "rev-parse", "HEAD")
    if status.returncode != 0 or head.returncode != 0:
        raise InvalidOutput("implement could not read the worktree")
    current = head.stdout.strip()
    if not re.fullmatch(r"[0-9a-fA-F]{40}", current):
        raise InvalidOutput("implement HEAD is not a commit")
    if not status.stdout:
        return current
    subject = title.strip() or f"Implement #{issue}"
    if f"#{issue}" not in subject:
        subject = f"{subject} (#{issue})"
    staged = _git(cwd, "add", "-A")
    if staged.returncode != 0:
        detail = " ".join((staged.stderr or staged.stdout).split())
        raise InvalidOutput(f"implement could not stage the change: {detail}")
    committed = _git(cwd, "commit", "-m", subject)
    if committed.returncode != 0:
        detail = " ".join((committed.stderr or committed.stdout).split())
        raise InvalidOutput(f"implement could not commit: {detail}")
    status = _git(cwd, "status", "--porcelain")
    head = _git(cwd, "rev-parse", "HEAD")
    if status.returncode != 0 or status.stdout or head.returncode != 0:
        raise InvalidOutput("implement commit left the worktree dirty")
    current = head.stdout.strip()
    if not re.fullmatch(r"[0-9a-fA-F]{40}", current):
        raise InvalidOutput("implement HEAD is not a commit")
    return current


def _command_detail(result: subprocess.CompletedProcess[str]) -> str:
    return " ".join((result.stderr or result.stdout).split())


def _pull_request_closes(body: object, issue: int) -> bool:
    return isinstance(body, str) and re.search(rf"(?i)\bCloses #{issue}\b", body) is not None


def _ensure_closes(execute, cwd: Path, repo: str, issue: int, url: str, existing_body: object, desired_body: str) -> None:
    if _pull_request_closes(existing_body, issue):
        return
    edited = execute(
        ["gh", "pr", "edit", url, "--repo", repo, "--body", desired_body],
        cwd,
    )
    if edited.returncode != 0:
        raise InvalidOutput(f"could not set the pull request body: {_command_detail(edited)}")


def comment_on_pull_request(
    cwd: Path,
    repo: str,
    url: str,
    body: str,
    run: Callable[[list[str], Path], subprocess.CompletedProcess[str]] | None = None,
) -> None:
    """Comment the review on the pull request. The body is one argument, never a shell."""
    execute = run or _execute
    cleaned = redact_text(body, redact_pii=True).strip() if isinstance(body, str) else ""
    if not isinstance(url, str) or not url.startswith("https://") or not cleaned:
        raise InvalidOutput("could not record the review")
    commented = execute(
        ["gh", "pr", "comment", url, "--repo", repo, "--body", cleaned],
        cwd,
    )
    if commented.returncode != 0:
        raise InvalidOutput(f"could not record the review: {_command_detail(commented)}")


def _read_branch_diff(cwd: Path, head_sha: str | None = None) -> str:
    head = head_sha if head_sha and FULL_SHA.fullmatch(head_sha) else "HEAD"
    for rev_range in (f"origin/main...{head}", f"main...{head}", f"{head}~1..{head}"):
        diff = _git(cwd, "diff", "--no-color", "--unified=3", rev_range, "--")
        if diff.returncode == 0:
            return diff.stdout
    return ""


def post_inline_review_comments(
    cwd: Path,
    repo: str,
    url: str,
    comments: list[dict],
    *,
    commit_id: str | None = None,
    run: Callable[[list[str], Path, str], subprocess.CompletedProcess[str]] | None = None,
) -> None:
    """Post validated inline PR review comments via GitHub API without a shell."""
    if not comments or not isinstance(url, str):
        return
    match = re.search(r"/pull/(\d+)(?:[/?#].*)?$", url.strip())
    if not match:
        return
    pr_number = match.group(1)
    cleaned_comments: list[dict] = []
    for item in comments:
        if not isinstance(item, dict):
            continue
        body = redact_text(item.get("body") or "", redact_pii=True).strip()
        path = str(item.get("path") or "").strip()
        line = item.get("line")
        side = item.get("side")
        if not path or not body or not isinstance(line, int) or line <= 0 or side not in {"LEFT", "RIGHT"}:
            continue
        entry: dict = {"path": path, "line": line, "side": side, "body": body}
        if isinstance(item.get("start_line"), int) and item.get("start_side") in {"LEFT", "RIGHT"}:
            entry["start_line"] = item["start_line"]
            entry["start_side"] = item["start_side"]
        cleaned_comments.append(entry)
    if not cleaned_comments:
        return
    if not isinstance(commit_id, str) or not FULL_SHA.fullmatch(commit_id):
        raise InvalidOutput("inline review comments require the full reviewed commit SHA")
    payload = json.dumps({"event": "COMMENT", "body": "", "commit_id": commit_id, "comments": cleaned_comments})
    args = ["gh", "api", f"repos/{repo}/pulls/{pr_number}/reviews", "--method", "POST", "--input", "-"]
    if run is not None:
        proc = run(args, cwd, payload)
    else:
        proc = subprocess.run(
            args,
            cwd=cwd,
            shell=False,
            input=payload,
            capture_output=True,
            text=True,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    if proc.returncode != 0:
        raise InvalidOutput(f"could not post inline review comments: {_command_detail(proc)}")


def publish_reviewed_branch(
    cwd: Path,
    repo: str,
    issue: int,
    sha: str,
    run: Callable[[list[str], Path], subprocess.CompletedProcess[str]] | None = None,
    *,
    body: str | None = None,
) -> str:
    """Push the reviewed slice branch and open its pull request.

    The merge arm runs only after this returns. A missing pull request is not
    an armed merge.
    """
    execute = run or _execute
    desired_body = redact_text(body, redact_pii=True).strip() if isinstance(body, str) and body.strip() else f"Closes #{issue}"
    if not _pull_request_closes(desired_body, issue):
        desired_body = f"Closes #{issue}\n\n{desired_body}"
    if not re.fullmatch(r"[0-9a-fA-F]{40}", sha):
        raise InvalidOutput("the reviewed commit is not a full SHA")
    branch = execute(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd)
    name = branch.stdout.strip()
    if branch.returncode != 0 or name in {"", "HEAD", "main", "master"} or not name.startswith(f"{issue}-"):
        raise InvalidOutput(f"could not publish from {name or 'this checkout'}")
    status = execute(["git", "status", "--porcelain"], cwd)
    head = execute(["git", "rev-parse", "HEAD"], cwd)
    if status.returncode != 0 or head.returncode != 0 or status.stdout or head.stdout.strip().lower() != sha.lower():
        raise InvalidOutput("the reviewed commit is not the clean HEAD")
    pushed = execute(["git", "push", "--set-upstream", "origin", f"HEAD:refs/heads/{name}"], cwd)
    if pushed.returncode != 0:
        raise InvalidOutput(f"could not push {name}: {_command_detail(pushed)}")
    view_args = [
        "gh", "pr", "view", "--repo", repo, "--json",
        "url,state,body,headRefOid,headRefName,baseRefName", "",
    ]
    viewed = execute(view_args, cwd)
    if viewed.returncode == 0:
        try:
            payload = json.loads(viewed.stdout)
        except json.JSONDecodeError as exc:
            raise InvalidOutput("could not read the pull request") from exc
        if not isinstance(payload, dict):
            raise InvalidOutput("could not read the pull request")
        if payload.get("state") == "OPEN" and payload.get("headRefName") == name and payload.get("baseRefName") == "main":
            current = payload.get("headRefOid")
            if not isinstance(current, str) or current.lower() != sha.lower():
                raise InvalidOutput("the open pull request is not the reviewed commit")
            url = payload.get("url")
            if not isinstance(url, str) or not url.strip():
                raise InvalidOutput("could not read the pull request")
            url = url.strip()
            _ensure_closes(execute, cwd, repo, issue, url, payload.get("body"), desired_body)
            return url
    subject = execute(["git", "log", "-1", "--format=%s"], cwd)
    title = subject.stdout.strip().splitlines()[0] if subject.returncode == 0 and subject.stdout.strip() else f"Implement #{issue}"
    created = execute([
        "gh", "pr", "create", "--repo", repo, "--base", "main", "--head", name,
        "--title", title, "--body", desired_body,
    ], cwd)
    if created.returncode != 0:
        raise InvalidOutput(f"could not open the pull request: {_command_detail(created)}")
    url = created.stdout.strip().splitlines()[-1] if created.stdout.strip() else ""
    if not url.startswith("https://"):
        raise InvalidOutput("could not open the pull request")
    return url


def run_live_delivery(
    *,
    repo: str,
    issue: int,
    check: str,
    cwd: Path,
    checkpoint: Path,
    dispatcher: FailoverDispatcher | None = None,
    request: str | None = None,
    github: GitHub | None = None,
    answer: str | None = None,
    run_gh: Callable[[list[str]], int] | None = None,
    inspect_worktree: WorktreeInspector | None = None,
    commit_worktree: Callable[[str], str] | None = None,
    publish_worktree: Callable[[str], str] | None = None,
    record_review: Callable[[DeliveryState, str], None] | None = None,
    read_merge_hold: Callable[[str], str | None] | None = None,
    preflight: Callable[[], tuple[bool, str]] | None = None,
    telemetry_path: Path | None = None,
    budget: dict | None = None,
) -> dict:
    started_at = utcnow_iso()
    started_clock = time.monotonic()
    graph_ran = False

    def run_check(command: str) -> tuple[int, str]:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        redacted = SecretRedactor.from_environ().redact(proc.stdout or "")
        return proc.returncode, redacted[-4000:]

    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    config = {"configurable": {"thread_id": f"{repo.lower()}#{issue}"}}
    workspace = str(cwd.resolve())
    inspect = inspect_worktree or _inspect_worktree
    finish_commit = commit_worktree
    if finish_commit is None and inspect_worktree is None:
        def finish_commit(title: str) -> str:
            return commit_implementation(cwd, issue, title)
    finish_publish = publish_worktree
    finish_record = record_review
    if finish_record is None and inspect_worktree is None:
        def finish_record(state: DeliveryState, url: str) -> None:
            comment_on_pull_request(cwd, repo, url, review_comment(state))
            if comments := state.get("review_comments"):
                post_inline_review_comments(cwd, repo, url, comments, commit_id=state.get("head_sha"))
    with SqliteSaver.from_conn_string(str(checkpoint)) as saver:
        graph_ref: list = []
        if finish_publish is None and inspect_worktree is None:
            def finish_publish(sha: str) -> str:
                current = dict(graph_ref[0].get_state(config).values or {}) if graph_ref else {}
                return publish_reviewed_branch(cwd, repo, issue, sha, body=render_pr_body(current, issue))

        def read_review_diff() -> str:
            head = graph_ref[0].get_state(config).values.get("head_sha")
            return _read_branch_diff(cwd, head)

        graph = compile_delivery(
            dispatcher, checkpointer=saver, run_check=run_check, run_gh=run_gh,
            inspect_worktree=lambda: inspect(cwd), commit_worktree=finish_commit,
            publish_worktree=finish_publish, record_review=finish_record,
            read_merge_hold=read_merge_hold, preflight=preflight, budget=budget,
            read_diff=read_review_diff if inspect_worktree is None else None,
        )
        graph_ref.append(graph)
        saved = graph.get_state(config)
        saved_workspace = saved.values.get("workspace")
        if saved_workspace and saved_workspace != workspace:
            raise ValueError(f"Run this issue from {saved_workspace}.")
        if answer is not None:
            if not saved.interrupts:
                raise ValueError("This issue has no pending question.")
            if not answer.strip():
                raise ValueError("The answer must contain text.")
            graph.invoke(Command(resume=answer), config, durability="sync")
            graph_ran = True
        elif not saved.values:
            loaded = (github or GhClient()).load_issue(repo, issue)
            text = request if request is not None else loaded["title"] + ("\n\n" + loaded["body"] if loaded["body"] else "")
            initial: DeliveryState = {
                "run_id": uuid4().hex,
                "started_at": started_at,
                "repo": repo,
                "issue": issue,
                "workspace": workspace,
                "request": text,
                "check": check,
                "issue_kind": "slice" if "slice" in loaded["labels"] else "parent",
                "attempts": [],
            }
            if initial["issue_kind"] == "slice":
                try:
                    initial["slice"] = _load_slice(loaded["body"])
                except (InvalidOutput, json.JSONDecodeError) as exc:
                    initial.update(halt="missing_slice", halt_detail=str(exc))
            graph.invoke(initial, config, durability="sync")
            graph_ran = True
        elif saved.next and not saved.interrupts:
            graph.invoke(None, config, durability="sync")
            graph_ran = True

        saved = graph.get_state(config)
        if saved.values.get("stop") == "sliced":
            client = github or GhClient()
            numbers = list(saved.values.get("child_issue_numbers", []))
            for item in saved.values["slices"][len(numbers):]:
                dependencies = "\n".join(f"- {title}" for title in item["depends_on"]) or "None"
                constraints = "\n".join(f"- {prop['constraint']}" for prop in item["properties"]) or "None"
                body = (f"Acceptance:\n{item['acceptance']}\n\nDependencies:\n{dependencies}"
                        f"\n\nScreen:\n{item['screen'] or 'None'}\n\nConstraints:\n{constraints}"
                        f"\n\n```json\n{json.dumps(item, indent=2)}\n```")
                labels = ["slice"] + ([] if item["depends_on"] else ["factory"])
                numbers.append(client.create_issue(repo, item["title"], body, labels))
                graph.update_state(config, {"child_issue_numbers": numbers})
            siblings = {item["title"]: number for item, number in zip(saved.values["slices"], numbers)}
            for item, number in zip(saved.values["slices"], numbers):
                deps = item["depends_on"]
                if (deps and all(title in siblings and client.is_merged(repo, siblings[title]) for title in deps)
                        and "factory" not in client.load_issue(repo, number)["labels"]):
                    client.add_label(repo, number, "factory")
            saved = graph.get_state(config)
        if saved.interrupts:
            question = saved.interrupts[0].value["question"]
            return {**saved.values, "stop": "needs_human", "halt_detail": question}
        final_state = dict(saved.values)
        if telemetry_path is not None:
            if not final_state.get("run_id"):
                # Checkpoints written before run identity was saved still need a stable telemetry key.
                final_state["run_id"] = uuid5(NAMESPACE_URL, f"{checkpoint.resolve()}:{repo.lower()}#{issue}").hex
                final_state["started_at"] = saved.created_at or started_at
            store = TelemetryStore(telemetry_path)
            if graph_ran or store.get_run(final_state["run_id"]) is None:
                elapsed_ms = int((time.monotonic() - started_clock) * 1000)
                store.record_run(
                    final_state,
                    run_id=final_state.get("run_id"),
                    started_at=final_state.get("started_at"),
                    finished_at=utcnow_iso(),
                    duration_ms=elapsed_ms if elapsed_ms > 0 else None,
                )
        return final_state

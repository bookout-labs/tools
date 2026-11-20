"""Save and publish finished work from tools.yaml.

`foreman save` commits named files, pushes, and opens a pull request.
`publish_reviewed` pushes the commit review already accepted and opens the pull request.
The delivery merge node arms `gh pr merge --auto --match-head-commit`.
"""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shlex

from foreman.resolve import Cmd

ISSUE = re.compile(r"^[1-9]\d*$")
BOARD = re.compile(r"^Board: #([1-9]\d*)\r?$", re.MULTILINE)
AGENT = re.compile(r"^Agent: (\S+)\r?$", re.MULTILINE)
BLOCKED = re.compile(r"^Review blocked \(round ")
REGROUP = re.compile(r"^Regroup:")
MAX_ROUNDS = 3
UPDATE = (
    "Run git merge --autostash {ref}. If the merge stops on conflicts, resolve them and "
    "run git merge --continue. If only the autostash replay conflicts, resolve those files; "
    "your changes stay in git stash list until you drop them."
)
NOT_ENGINE = (
    "Not engine paths: {count} file(s). Records and prose go to Neon (pages.ts), files to R2 "
    "(files.ts); only code and system files live in git."
)


class SaveError(ValueError):
    pass


@dataclass(frozen=True)
class SaveConfig:
    app: dict[str, str] | None
    privacy: str | None
    work_id_pattern: str | None
    board_repo: str | None
    repo: str
    proof: str
    queue: bool
    hooks: dict[str, str]
    paths: dict[str, tuple[str, ...]] | None
    check: str


def config_from_pointer(pointer: dict) -> SaveConfig:
    raw = pointer.get("save")
    if not isinstance(raw, dict):
        raise SaveError("This pointer has no save config.")
    paths = raw.get("paths")
    repo = raw.get("repo") or raw.get("board_repo")
    if not isinstance(repo, str) or not repo:
        raise SaveError("save needs repo or board_repo")
    return SaveConfig(
        app=raw.get("app") if isinstance(raw.get("app"), dict) else None,
        privacy=raw.get("privacy"),
        work_id_pattern=raw.get("work_id_pattern"),
        board_repo=raw.get("board_repo"),
        repo=repo,
        proof=raw.get("proof") or "optional",
        queue=bool(raw.get("queue")),
        hooks=dict(raw.get("hooks") or {}),
        paths=None if paths is None else {
            "code": tuple(paths.get("code") or ()),
            "system": tuple(paths.get("system") or ()),
        },
        check=pointer.get("check") or "npm run check",
    )


@dataclass(frozen=True)
class SaveRequest:
    work: str = ""
    message: str = ""
    agent: str | None = None
    proof: str = ""
    issue: str | None = None
    regroup: str | None = None
    paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class PublishResult:
    sha: str
    report: str


_SHA = re.compile(r"[0-9a-fA-F]{40}")


def kind_of(path: str, rules: dict[str, tuple[str, ...]] | None) -> str | None:
    if rules is None:
        return "any"
    for kind, prefixes in rules.items():
        for prefix in prefixes:
            if prefix.endswith("/"):
                if path.startswith(prefix):
                    return kind
            elif path == prefix:
                return kind
    return None


def board_number(body: str) -> str | None:
    match = BOARD.search(body)
    return match.group(1) if match else None


def agent_of(body: str) -> str:
    match = AGENT.search(body)
    return match.group(1) if match else "unknown"


def needs_proof(files: list[str], config: SaveConfig) -> bool:
    if config.proof == "optional":
        return False
    if config.paths is None:
        return True
    return any(kind_of(path, config.paths) == "code" for path in files)


def missing(files: list[str], body: str, config: SaveConfig) -> list[str]:
    errors: list[str] = []
    if config.paths is not None:
        foreign = [path for path in files if kind_of(path, config.paths) is None]
        if foreign:
            errors.append(NOT_ENGINE.format(count=len(foreign)))
    if config.board_repo and not board_number(body):
        errors.append('Add "Board: #<n>" naming an open Builds issue: foreman save … --issue <n>.')
    proved = any(line.startswith("Proof: ") and len(line[7:].strip()) >= 10 for line in body.splitlines())
    if needs_proof(files, config) and not proved:
        errors.append(
            'Add "Proof: <10+ chars>" saying how it will be shown working in production: … --proof "<text>".'
        )
    return errors


def added(diff: str) -> str:
    lines = diff.split("\n")
    kept = []
    for index, line in enumerate(lines):
        header = line.startswith("+++ ") and index > 0 and lines[index - 1].startswith("--- ")
        if line.startswith("+") and not header:
            kept.append(line[1:])
    return "\n".join(kept)


def leaks(texts: list[str], term_list: str) -> int:
    terms = [line.strip() for line in term_list.split("\n") if line.strip() and not line.strip().startswith("#")]
    if not terms:
        raise SaveError("Personal-term list is empty.")
    try:
        pattern = re.compile("|".join(f"(?:{term})" for term in terms), re.IGNORECASE)
    except re.error as exc:
        raise SaveError("Personal-term list has an invalid pattern.") from exc
    return sum(1 for text in texts for line in text.split("\n") if pattern.search(line))


def rounds_since_regroup(comments: list[str]) -> int:
    rounds = 0
    for body in comments:
        if REGROUP.match(body):
            rounds = 0
        elif BLOCKED.match(body):
            rounds += 1
    return rounds


def term_list(config: SaveConfig, environ: dict[str, str]) -> str | None:
    if config.privacy is None:
        return None
    if "PERSONAL_TERMS" in environ:
        return environ["PERSONAL_TERMS"]
    path = Path(config.privacy).expanduser()
    try:
        return path.read_text()
    except OSError as exc:
        raise SaveError(
            "Personal-term list is missing. Configure PERSONAL_TERMS or the local personal-terms file."
        ) from exc


def _within(base: str, path: str) -> str | None:
    rel = os.path.relpath(path, base)
    if rel == ".." or rel.startswith(".." + os.sep):
        return None
    return "" if rel == "." else rel


def _canonical(path: Path) -> Path:
    try:
        return Path(os.path.realpath(path, strict=True))
    except FileNotFoundError:
        try:
            path.lstat()
        except FileNotFoundError:
            return _canonical(path.parent) / path.name
        raise


def _stage_paths(root: Path, positionals: tuple[str, ...], cwd: Path) -> list[str]:
    staged: list[str] = []
    for raw in positionals:
        absolute = Path(raw).expanduser()
        if not absolute.is_absolute():
            absolute = cwd / absolute
        try:
            location = _canonical(absolute.parent) / absolute.name
            named = _within(str(root), str(location))
        except OSError as exc:
            raise SaveError(f"Cannot resolve save path: {exc}") from exc
        if named is None:
            raise SaveError(f"Path resolves outside the repository: {raw}")
        chosen = [named or "."]
        try:
            link = location.is_symlink() and location.exists()
        except OSError:
            link = False
        if link:
            target = Path(os.path.realpath(location))
            inside = _within(str(root), str(target))
            if inside is not None and _within(str(target), str(location)) is None:
                chosen.append(inside or ".")
        staged.extend(chosen)
    return staged


def _agent(request: SaveRequest, environ: dict[str, str]) -> str:
    if request.agent:
        return request.agent
    if environ.get("CLAUDECODE"):
        return "claude-code"
    if environ.get("CODEX_THREAD_ID"):
        return "codex"
    return "user"


def _session(environ: dict[str, str]) -> str:
    return environ.get("CLAUDE_CODE_SESSION_ID") or environ.get("CODEX_THREAD_ID") or "terminal"


def _tail(text: str) -> str:
    return "\n".join(text.split("\n")[-8:])


def run_save(
    config: SaveConfig,
    request: SaveRequest,
    run,
    *,
    cwd: Path | None = None,
    comments: list[str] | None = None,
    environ: dict[str, str] | None = None,
) -> str:
    workdir = cwd or Path.cwd()
    env_map = os.environ if environ is None else environ
    lines: list[str] = []
    token_env: dict[str, str] | None = None

    def spawn(args: list[str], overlay: dict[str, str] | None = None) -> Cmd:
        return run(args, overlay)

    def git(*args: str, check: bool = True) -> Cmd:
        result = spawn(["git", *args])
        if check and result.code != 0:
            raise SaveError(f"git {' '.join(args)} failed")
        return result

    def gh(*args: str) -> str:
        result = spawn(["gh", *args], token_env)
        if result.code != 0:
            raise SaveError(f"gh {' '.join(args[:3])} failed")
        return result.stdout.strip()

    if config.app:
        minted = spawn([
            "gh", "token", "generate", "--app-id", config.app["id"],
            "--installation-id", config.app["installation"], "--key", config.app["key"], "--token-only",
        ])
        if minted.code != 0 or not minted.stdout.strip():
            raise SaveError("Could not mint a GitHub App token.")
        token_env = {"GH_TOKEN": minted.stdout.strip()}

    if request.regroup:
        if not request.issue or not ISSUE.fullmatch(request.issue):
            raise SaveError("--regroup needs --issue <n>")
        if not config.board_repo:
            raise SaveError("--regroup needs board_repo")
        if comments is not None and rounds_since_regroup(comments) < MAX_ROUNDS:
            raise SaveError("Regroup waits until 3 blocked rounds since the last regroup.")
        text = f"Regroup: {request.regroup}"
        terms = term_list(config, env_map)
        if terms is not None and leaks([text], terms):
            raise SaveError("The regroup text matches the personal-term list.")
        gh("issue", "comment", request.issue, "-R", config.board_repo, "--body", text)
        lines.append(f"Regrouped #{request.issue}")
        if not request.paths:
            return "\n".join(lines)

    if config.work_id_pattern and not re.fullmatch(config.work_id_pattern, request.work):
        raise SaveError("Give a work ID, like --work LAB.1.3")
    if not request.message:
        raise SaveError('Give a message, like -m "what changed"')
    if not request.paths:
        raise SaveError("Name the files to save. Nothing is saved by default.")
    if request.issue and not ISSUE.fullmatch(request.issue):
        raise SaveError("--issue is a Builds board issue number, like --issue 2")
    if re.search(r"[\r\n]", request.proof):
        raise SaveError("--proof must be one line")

    branch = git("branch", "--show-current").stdout.strip()
    if not branch or branch == "main":
        raise SaveError("Save from a worktree, never from main.")
    if spawn(["git", "fetch", "--prune", "origin", "+refs/heads/*:refs/remotes/origin/*"]).code != 0:
        raise SaveError("git fetch failed. Check origin access, then save again.")
    remote = f"origin/{branch}"
    exists = spawn(["git", "show-ref", "--verify", "--quiet", f"refs/remotes/{remote}"])
    refs = ["origin/main"]
    if exists.code == 0:
        refs.append(remote)
    elif exists.code != 1:
        raise SaveError("Cannot establish remote branch status.")
    if spawn(["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"]).code == 0:
        raise SaveError("A merge is in progress. Finish it with git merge --continue, then save again.")
    for ref in refs:
        status = spawn(["git", "merge-base", "--is-ancestor", ref, "HEAD"]).code
        if status not in (0, 1):
            raise SaveError("git merge-base failed. Cannot establish branch freshness.")
        if status == 1:
            merged = spawn(["git", "merge", "--no-edit", ref])
            if merged.code != 0:
                spawn(["git", "merge", "--abort"])
                raise SaveError(f"Branch is stale and {ref} does not merge cleanly. {UPDATE.format(ref=ref)}")
            lines.append(f"Merged {ref}")

    root = Path(git("rev-parse", "--show-toplevel").stdout.strip())
    paths = _stage_paths(root, request.paths, workdir)

    def present(rel: str) -> bool:
        try:
            (root / rel).lstat()
            return True
        except FileNotFoundError:
            listed = git("-C", str(root), "ls-files", "--", rel).stdout.strip()
            return listed != ""

    for rel in paths:
        if present(rel):
            continue
        cached = git("-C", str(root), "diff", "--cached", "--name-only", "--", rel).stdout.strip()
        if cached == "":
            raise SaveError(f"Nothing to save at {rel}.")
    addable = [rel for rel in paths if present(rel)]
    if addable:
        git("-C", str(root), "add", "-A", "--", *addable)
    files = [name for name in git("diff", "--cached", "--name-only", "--no-renames", "-z").stdout.split("\0") if name]
    if not files:
        lines.append("Nothing to save in those files.")
        return "\n".join(lines)

    agent = _agent(request, env_map)
    session = _session(env_map)
    if config.work_id_pattern:
        trailers = f"Work: {request.work}\nAgent: {agent}\nSession: {session}"
    else:
        trailers = f"Agent: {agent}\nSession: {session}"
    parts = [f"Proof: {request.proof}" if request.proof else "", f"Board: #{request.issue}" if request.issue else "", trailers]
    body = "\n".join(part for part in parts if part)
    errors = missing(files, body, config)
    if errors:
        raise SaveError("\n".join(errors))
    if request.issue and config.board_repo:
        viewed = spawn(["gh", "issue", "view", request.issue, "-R", config.board_repo, "--json", "state"], token_env)
        if viewed.code != 0:
            raise SaveError(
                f"Cannot read Builds issue #{request.issue}. It must exist and be OPEN; check the number and GitHub access."
            )
        try:
            state = json.loads(viewed.stdout).get("state")
        except json.JSONDecodeError as exc:
            raise SaveError(
                f"Cannot read Builds issue #{request.issue}. It must exist and be OPEN; check the number and GitHub access."
            ) from exc
        if state != "OPEN":
            raise SaveError(f"Builds issue #{request.issue} is {state}. Choose an OPEN issue (… --issue <n>).")
    terms = term_list(config, env_map)
    if terms is not None:
        try:
            hits = leaks([
                "\n".join(files),
                added(git("diff", "--text", "-U0", "origin/main...HEAD").stdout),
                added(git("diff", "--cached", "--text", "-U0").stdout),
                request.message,
                body,
                git("log", "--format=%B", "origin/main..HEAD").stdout,
            ], terms)
        except SaveError:
            raise
        if hits:
            raise SaveError(
                f"{hits} line(s) match the personal-term list. Personal data lives in Neon and R2, never in git."
            )
    if config.hooks.get("pre_save"):
        hook = spawn(shlex.split(config.hooks["pre_save"]))
        if hook.code != 0:
            raise SaveError("pre_save hook failed")
    check = spawn(shlex.split(config.check))
    if check.code != 0:
        raise SaveError(f"{config.check} failed. Fix it, then save again.\n{_tail(check.stdout)}\n{_tail(check.stderr)}")
    git("commit", "-m", f"{request.message}\n\n{trailers}")
    pushed = spawn(["git", "push", "-u", "origin", branch])
    if pushed.code != 0:
        raise SaveError(f"Push failed. Fetch origin, then {UPDATE.format(ref=remote)} Then save again.")
    lines.append(f"1/2 pushed {branch}")
    listed = spawn(["gh", "pr", "list", "-R", config.repo, "--head", branch, "--state", "open", "--json", "url"], token_env)
    if listed.code != 0:
        raise SaveError("gh pr list failed")
    try:
        found = json.loads(listed.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise SaveError("gh pr list failed") from exc
    if found:
        pr = found[0]["url"]
        edited = spawn(["gh", "pr", "edit", pr, "--title", request.message, "--body", body], token_env)
        if edited.code != 0:
            raise SaveError("gh pr edit failed")
    else:
        created = spawn([
            "gh", "pr", "create", "-R", config.repo, "--base", "main", "--head", branch,
            "--title", request.message, "--body", body,
        ], token_env)
        if created.code != 0 or not created.stdout.strip():
            raise SaveError("gh pr create failed")
        pr = created.stdout.strip()
    if config.queue:
        queued = spawn(["gh", "pr", "merge", pr, "--auto"], token_env)
        if queued.code != 0:
            raise SaveError(f"{pr} is pushed but not queued. Save again to queue it.")
        lines.append(f"2/2 {pr} is queued and merges when check, gate and secrets pass.")
        lines.append("Link and watch it; merged is not done until the repository's ship check passes.")
    else:
        lines.append(f"2/2 {pr} is open.")
    if config.hooks.get("post_save"):
        hook = spawn(shlex.split(config.hooks["post_save"]))
        if hook.code != 0:
            raise SaveError("post_save hook failed")
    return "\n".join(lines)


def publish_reviewed(
    config: SaveConfig,
    request: SaveRequest,
    run,
    *,
    environ: dict[str, str] | None = None,
    expected_sha: str | None = None,
) -> PublishResult:
    """Push the reviewed HEAD and open or update its pull request.

    A stale branch stops with autostash guidance. Merging it would move HEAD
    off the commit verify recorded.
    """
    env_map = os.environ if environ is None else environ
    if config.work_id_pattern and not re.fullmatch(config.work_id_pattern, request.work):
        raise SaveError("Give a work ID, like --work LAB.1.3")
    if not request.message:
        raise SaveError('Give a message, like -m "what changed"')
    if re.search(r"[\r\n]", request.proof):
        raise SaveError("--proof must be one line")
    if request.issue and not ISSUE.fullmatch(request.issue):
        raise SaveError("--issue is a Builds board issue number, like --issue 2")

    token_env: dict[str, str] | None = None

    def spawn(args: list[str], overlay: dict[str, str] | None = None) -> Cmd:
        return run(args, overlay)

    def git(*args: str, check: bool = True) -> Cmd:
        result = spawn(["git", *args])
        if check and result.code != 0:
            raise SaveError(f"git {' '.join(args)} failed")
        return result

    branch = git("branch", "--show-current").stdout.strip()
    if not branch or branch == "main":
        raise SaveError("Save from a worktree, never from main.")
    if spawn(["git", "fetch", "--prune", "origin", "+refs/heads/*:refs/remotes/origin/*"]).code != 0:
        raise SaveError("git fetch failed. Check origin access, then save again.")
    remote = f"origin/{branch}"
    exists = spawn(["git", "show-ref", "--verify", "--quiet", f"refs/remotes/{remote}"])
    refs = ["origin/main"]
    if exists.code == 0:
        refs.append(remote)
    elif exists.code != 1:
        raise SaveError("Cannot establish remote branch status.")
    if spawn(["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"]).code == 0:
        raise SaveError("A merge is in progress. Finish it with git merge --continue, then save again.")
    for ref in refs:
        status = spawn(["git", "merge-base", "--is-ancestor", ref, "HEAD"]).code
        if status not in (0, 1):
            raise SaveError("git merge-base failed. Cannot establish branch freshness.")
        if status == 1:
            raise SaveError(f"Branch is stale against {ref}. {UPDATE.format(ref=ref)}")

    status = spawn(["git", "status", "--porcelain"])
    if status.code != 0:
        raise SaveError("git status --porcelain failed")
    if status.stdout.strip():
        raise SaveError("The checkout has uncommitted changes, so the reviewed commit stays unpublished.")
    sha = git("rev-parse", "HEAD").stdout.strip()
    if not _SHA.fullmatch(sha):
        raise SaveError("git rev-parse HEAD did not return a full SHA.")
    if expected_sha and _SHA.fullmatch(expected_sha) and sha.lower() != expected_sha.lower():
        raise SaveError("HEAD is not the reviewed commit.")
    names = git("diff", "--name-only", "--no-renames", "-z", "origin/main...HEAD")
    files = [name for name in names.stdout.split("\0") if name]
    if not files:
        raise SaveError("Nothing to publish.")

    agent = _agent(request, env_map)
    session = _session(env_map)
    if config.work_id_pattern:
        trailers = f"Work: {request.work}\nAgent: {agent}\nSession: {session}"
    else:
        trailers = f"Agent: {agent}\nSession: {session}"
    parts = [
        f"Proof: {request.proof}" if request.proof else "",
        f"Board: #{request.issue}" if request.issue else "",
        trailers,
    ]
    body = "\n".join(part for part in parts if part)
    errors = missing(files, body, config)
    if errors:
        raise SaveError("\n".join(errors))
    terms = term_list(config, env_map)
    if terms is not None:
        hits = leaks([
            "\n".join(files),
            added(git("diff", "--text", "-U0", "origin/main...HEAD").stdout),
            request.message,
            body,
            git("log", "--format=%B", "origin/main..HEAD").stdout,
        ], terms)
        if hits:
            raise SaveError(
                f"{hits} line(s) match the personal-term list. Personal data lives in Neon and R2, never in git."
            )
    if config.app:
        minted = spawn([
            "gh", "token", "generate", "--app-id", config.app["id"],
            "--installation-id", config.app["installation"], "--key", config.app["key"], "--token-only",
        ])
        if minted.code != 0 or not minted.stdout.strip():
            raise SaveError("Could not mint a GitHub App token.")
        token_env = {"GH_TOKEN": minted.stdout.strip()}
    if request.issue and config.board_repo:
        viewed = spawn(["gh", "issue", "view", request.issue, "-R", config.board_repo, "--json", "state"], token_env)
        if viewed.code != 0:
            raise SaveError(
                f"Cannot read Builds issue #{request.issue}. It must exist and be OPEN; check the number and GitHub access."
            )
        try:
            state = json.loads(viewed.stdout).get("state")
        except json.JSONDecodeError as exc:
            raise SaveError(
                f"Cannot read Builds issue #{request.issue}. It must exist and be OPEN; check the number and GitHub access."
            ) from exc
        if state != "OPEN":
            raise SaveError(f"Builds issue #{request.issue} is {state}. Choose an OPEN issue (… --issue <n>).")
    pushed = spawn(["git", "push", "-u", "origin", branch])
    if pushed.code != 0:
        raise SaveError(f"Push failed. Fetch origin, then {UPDATE.format(ref=remote)} Then save again.")
    listed = spawn(["gh", "pr", "list", "-R", config.repo, "--head", branch, "--state", "open", "--json", "url"], token_env)
    if listed.code != 0:
        raise SaveError("gh pr list failed")
    try:
        found = json.loads(listed.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise SaveError("gh pr list failed") from exc
    if found:
        pr = found[0]["url"]
        edited = spawn(["gh", "pr", "edit", pr, "--title", request.message, "--body", body], token_env)
        if edited.code != 0:
            raise SaveError("gh pr edit failed")
    else:
        created = spawn([
            "gh", "pr", "create", "-R", config.repo, "--base", "main", "--head", branch,
            "--title", request.message, "--body", body,
        ], token_env)
        if created.code != 0 or not created.stdout.strip():
            raise SaveError("gh pr create failed")
        pr = created.stdout.strip()
    return PublishResult(sha=sha, report=f"1/2 pushed {branch}\n2/2 {pr} is open.")

"""The delivery graph. Edges live here. Models live in models.toml."""

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from typing import Annotated, Literal, TypedDict
import operator

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from unidiff.errors import UnidiffParseError
from unidiff.patch import PatchSet

from foreman.adapters import Outcome, OutcomeKind
from foreman.config import Config, ConfigError, load_config, order_for
from foreman.dispatch import FailoverDispatcher, InvalidOutput, QuotaExhausted
from foreman.github import (
    MERGE_HOLD_VARIABLE, Forge, ForgeError, MergeHoldUnreadable, MergeNotArmed, PullRequest, ReviewComment, merge_command,
)
from foreman.proof import browser_proof_label, browser_required, formal_proof_label, formal_required
from foreman.skill_rules import SkillFailureReason, SkillMissing, export_skills, stage_methods
from foreman.redaction import redact
from foreman.stage_output import (
    Contract, HumanQuestionOutput, PULL_REQUEST_LINES, ReviewOutput, STAGE_OUTPUTS, Slice, SpecOutput, StoredSlice, TriageOutput, VerifyOutput, WorkKind,
    parse_output,
)
from foreman.workspace import CheckLog, PullRequestSize, ReviewDiff, Workspace, WorktreeInspectionFailed, commit_sha, head_mismatch_detail

GRAPH_NAME = "delivery"
PACKAGE = Path(__file__).parent
REQUIRED_NODES = ("triage", "research", "spec", "implement", "verify", "review")
STAGE_ORDER = REQUIRED_NODES + ("merge",)
MergeHalt = Literal["merge_held", "merge_not_armed"]


@dataclass(frozen=True)
class LiveRun:
    forge: Forge
    workspace: Workspace
    check_log: CheckLog | None = None


@dataclass(frozen=True)
class DryRun:
    def record_merge(self, repo: str, head: str | None) -> list[str]:
        revision = (commit_sha(head) or redact(head)) if head else "<sha>"
        return merge_command(redact(repo), "<number>", revision)


class SkillAttempt(TypedDict):
    stage: str
    model: str | None
    commit: str
    methods: list[str]
    outcome: OutcomeKind


class PullRequestSizeDecision(StrEnum):
    SPLIT = "split"
    OVERRIDE = "override"


class PullRequestSplit(TypedDict):
    plan: str
    gate: Literal["ready", "needs_human"]
    question: str | None
    slices: list[dict[str, object]]


class DeliveryState(TypedDict, total=False):
    request: str
    repo: str
    issue: int
    workspace: str
    check: str
    check_exit_code: int
    triage: str
    spec: str
    spec_gate: str
    slices: list[dict[str, object]]
    child_issue_numbers: list[int]
    slice: dict[str, object]
    issue_kind: str
    work_kind: WorkKind
    pstack_pin: dict[str, str]
    skill_reason: SkillFailureReason
    skill_attempts: Annotated[list[SkillAttempt], operator.add]
    research: str
    implementation: str
    head_sha: str | None
    verified_head: str
    merge_observed_head: str
    merge: Literal["armed", "skipped", "not_armed"]
    merge_command: list[str]
    browser_proof: dict[str, object] | None
    formal_proof: dict[str, object] | None
    proof_labels: dict[str, str]
    human_question: str
    human_answers: list[str]
    pr_size: PullRequestSize
    pr_size_override: PullRequestSize
    pr_split: PullRequestSplit
    review: str
    findings: list[dict[str, object]]
    detail: str
    review_change_kind: Literal["blocking", "nits"] | None
    risk: dict[str, str]
    verify: str
    review_rounds: int
    models_used: dict[str, str]
    implement_models: list[str]
    halt: str
    halt_detail: str
    stop: str
    path: Annotated[list[str], operator.add]


def stage_text(name: str) -> str:
    return (PACKAGE / "stages" / f"{name}.md").read_text().strip()


def clean_slices(
    raw: list[Slice] | None,
    field: Literal["slice", "slices"],
    *,
    allow_external_dependencies: bool = False,
) -> list[dict[str, object]]:
    if not raw:
        raise InvalidOutput(f"{field}: ready parent spec needs at least one slice")
    paths = [f"{field}.{index}" if field == "slices" else field for index in range(len(raw))]
    graph: dict[str, list[str]] = {}
    for path, item in zip(paths, raw):
        if item.title in graph:
            raise InvalidOutput(f"{path}.title: must be unique")
        graph[item.title] = item.depends_on
    for path, item in zip(paths, raw):
        for index, dep in enumerate(item.depends_on):
            if dep not in graph and not allow_external_dependencies:
                raise InvalidOutput(f"{path}.depends_on.{index}: unknown dependency {dep}")
    try:
        TopologicalSorter(graph).prepare()
    except CycleError as exc:
        dep, title = exc.args[1][:2]
        path = next(path for path, item in zip(paths, raw) if item.title == title)
        raise InvalidOutput(f"{path}.depends_on.{graph[title].index(dep)}: dependency cycle") from exc
    return [item.model_dump() for item in raw]


def load_slice(raw: object) -> DeliveryState:
    stored = parse_output(StoredSlice, raw)
    state: DeliveryState = {"slice": clean_slices([stored], "slice", allow_external_dependencies=True)[0]}
    if stored.work_kind is not None:
        state["work_kind"] = stored.work_kind
    return state


def stage_update(name: str, output: Contract, state: DeliveryState, model: str | None = None) -> dict[str, object]:
    update: dict[str, object] = output.model_dump(exclude={"slice", "slices"})
    if isinstance(output, HumanQuestionOutput):
        update["human_question"] = output.human_question or ""
    if isinstance(output, TriageOutput) and state.get("issue_kind") == "slice" and "work_kind" in state:
        update["work_kind"] = state["work_kind"]
    if isinstance(output, SpecOutput):
        slices = (
            clean_slices(output.slices, "slices")
            if state.get("issue_kind") == "parent" and output.spec_gate == "ready" else []
        )
        if oversized_pr(state):
            split: PullRequestSplit = {
                "plan": output.spec, "gate": output.spec_gate, "question": output.human_question, "slices": slices,
            }
            update = {"pr_split": split}
        elif slices:
            update["slices"] = slices
    update["path"] = [name]
    if isinstance(output, TriageOutput) and state.get("issue_kind") == "parent" and output.triage == "implement":
        if output.slice is None:
            raise InvalidOutput("slice: required when a parent triages as implement")
        update["slice"] = clean_slices([output.slice], "slice")[0]
    if isinstance(output, ReviewOutput):
        update["review_change_kind"] = None
        if output.review == "changes":
            only_nits = all(finding.severity == "nit" for finding in output.findings)
            update["review_change_kind"] = "nits" if only_nits else "blocking"
    if update.get("review") == "changes" or update.get("verify") == "fail":
        update["review_rounds"] = state.get("review_rounds", 0) + 1
    if model:
        used = state.get("models_used", {})
        update["models_used"] = {**used, name: model}
        if name == "implement":
            latest = used.get("implement")
            history = state.get("implement_models", [latest] if latest else [])
            update["implement_models"] = [*history, model]
    return update


def review_submission(output: ReviewOutput, diff: ReviewDiff) -> tuple[str, list[ReviewComment]]:
    try:
        patch = PatchSet(diff.patch)
    except UnidiffParseError:
        patch = PatchSet("")
    changed = {
        (file.path, line.target_line_no)
        for file in patch if not file.is_removed_file
        for hunk in file for line in hunk if line.is_added
    }
    body = [output.detail]
    comments: list[ReviewComment] = []
    for finding in output.findings:
        message = f"[{finding.severity}] {finding.summary}"
        if (finding.file, finding.line) in changed:
            comments.append({"path": finding.file, "line": finding.line, "side": "RIGHT", "body": message})
        else:
            body.append(f"{finding.file}:{finding.line} {message}")
    return "\n\n".join(body), comments


def stop_reason(state: DeliveryState) -> str:
    if halt := state.get("halt"):
        return halt
    if oversized_pr(state):
        split = state.get("pr_split")
        gate = split["gate"] if split else state.get("spec_gate")
        if gate == "ready":
            return "sliced" if state.get("issue_kind") == "parent" else "pr_split_required"
        if gate == "needs_human":
            return "needs_human"
    if state.get("triage") == "needs_human" or state.get("spec_gate") == "needs_human":
        return "needs_human"
    if state.get("spec_gate") == "ready" and state.get("path", [])[-1:] == ["spec"]:
        if state.get("issue_kind") == "parent":
            return "sliced"
    if state.get("triage") == "question":
        return "answered"
    if state.get("verify") == "fail":
        return "verify_cap"
    if state.get("review") == "changes":
        return "review_cap"
    if state.get("verify") == "pass":
        return "verified"
    return "stopped"


def oversized_pr(state: DeliveryState) -> PullRequestSize | None:
    size = state.get("pr_size")
    if size is not None and size["lines"] > PULL_REQUEST_LINES and size != state.get("pr_size_override"):
        return size
    return None


def compile_delivery(
    dispatcher: FailoverDispatcher | None,
    config: Config,
    *,
    run: LiveRun | DryRun,
    scratch: Path | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
):
    missing = [name for name in REQUIRED_NODES if name not in config.nodes]
    if missing:
        raise ValueError(f"delivery config is missing {', '.join(missing)}")
    methods_root = export_skills(config.pstack, scratch) if scratch is not None else None
    prompts = {name: stage_text(name) for name in REQUIRED_NODES}

    def bind(name: str):
        def node(state: DeliveryState) -> dict[str, object]:
            if dispatcher is None:
                return {"path": [name], "halt": "no_dispatcher"}
            try:
                models = order_for(name, config, state.get("models_used") or {}, state.get("implement_models"))
            except ConfigError as exc:
                return {"path": [name], "halt": "no_reviewer", "halt_detail": str(exc)}
            instruction = prompts[name]
            if isinstance(run, LiveRun) and methods_root is None:
                raise RuntimeError("Delivery execution requires a compiled skill export.")
            methods_text, methods = stage_methods(name, state, methods_root) if methods_root is not None else ("", [])
            if methods_text:
                instruction = f"{instruction}\n\nApply the methods listed below. Do not start subagents.\n\n{methods_text}"
            if name == "verify":
                command = state.get("check") or "(none)"
                instruction = f"{instruction}\n\nCommand: {command}"
            if name == "spec" and (size := oversized_pr(state)):
                instruction += (
                    f"\n\nThe pull request has {size['lines']} changed lines. The limit is {PULL_REQUEST_LINES}."
                    " Split the work into independently shippable pull requests."
                    " For a slice issue, describe the split in spec."
                )

            review_context: tuple[Forge, PullRequest, ReviewDiff] | None = None
            repo = state.get("repo", "")
            if name == "review" and isinstance(run, LiveRun):
                head, issue = state.get("verified_head"), state.get("issue")
                try:
                    worktree = run.workspace.inspect()
                    if not (repo and issue is not None and head and worktree.clean and worktree.branch and worktree.head == head):
                        raise WorktreeInspectionFailed("Review requires a clean branch at the verified HEAD.")
                    target = run.forge.review_target(repo, issue, worktree.branch, head)
                    diff = run.workspace.review_diff(target.baseRefOid, head)
                except (ForgeError, WorktreeInspectionFailed) as exc:
                    return {"path": [name], "halt": "review_unavailable", "halt_detail": str(exc)}
                review_context = (run.forge, target, diff)
                instruction = f"{instruction}\n\nRead the reviewed diff with this command in the checkout:\n{diff.command}"
            attempts: list[SkillAttempt] = []

            def record_attempt(model: str | None, kind: OutcomeKind) -> None:
                attempts.append({
                    "stage": name, "model": model, "commit": config.pstack.commit, "methods": methods, "outcome": kind,
                })

            outcome, model = dispatcher.run(
                name, models, state, instruction, on_attempt=record_attempt if methods_root and name != "verify" else None,
            )
            stopped: dict[str, object] = {"path": [name], "skill_attempts": attempts}
            if outcome.kind == "failed":
                return {**stopped, "halt": "invalid_output", "halt_detail": f"{model}: {outcome.detail}"}
            if outcome.kind == "cli_missing":
                return {**stopped, "halt": "cli_missing", "halt_detail": outcome.detail}
            if outcome.kind == "quota":
                if name != "review" and isinstance(run, DryRun):
                    raise QuotaExhausted(model)
                halt = "no_reviewer" if isinstance(run, DryRun) else "quota_exhausted"
                return {**stopped, "halt": halt, "halt_detail": f"{name} quota exhausted: {model}"}
            try:
                output = parse_output(STAGE_OUTPUTS[name], outcome.output)
                update = stage_update(name, output, state, model)
                update["skill_attempts"] = attempts
            except InvalidOutput as exc:
                return {**stopped, "halt": "invalid_output", "halt_detail": str(exc)}

            if review_context is not None and isinstance(output, ReviewOutput):
                forge, target, diff = review_context
                body, comments = review_submission(output, diff)
                try:
                    forge.post_review(repo, target, body, comments)
                except ForgeError as exc:
                    return {**update, "halt": "review_not_posted", "halt_detail": str(exc)}
            return update

        return node

    scripted_verify = bind("verify")

    def verify(state: DeliveryState) -> dict[str, object]:
        match run:
            case DryRun():
                return scripted_verify(state)
            case LiveRun(workspace=workspace, check_log=check_log):
                try:
                    worktree = workspace.inspect()
                except WorktreeInspectionFailed as exc:
                    return {"path": ["verify"], "halt": "dirty_worktree", "halt_detail": str(exc)}
                if not worktree.clean:
                    return {"path": ["verify"], "halt": "dirty_worktree"}
                exit_code = workspace.run(state["check"], log=check_log)
                verdict = "pass" if exit_code == 0 else "fail"
                checked = {"verified_head": worktree.head} if exit_code == 0 else {}
                return {
                    **stage_update("verify", VerifyOutput(verify=verdict), state),
                    "check_exit_code": exit_code,
                    "proof_labels": {},
                    **checked,
                }

    def prove(state: DeliveryState) -> DeliveryState:
        slice = state["slice"]
        labels = {
            "browser": browser_proof_label(slice, state.get("browser_proof")),
            "formal": formal_proof_label(slice, state.get("formal_proof")),
        }
        update: DeliveryState = {"path": ["proof"], "proof_labels": {key: label for key, label in labels.items() if label}}
        if "insufficient_evidence" in labels.values():
            update["halt"] = "insufficient_evidence"
        elif "block" in labels.values():
            update["review_rounds"] = state.get("review_rounds", 0) + 1
            if update["review_rounds"] >= config.max_review_rounds:
                update["halt"] = "review_cap"
        return update

    def needs_human(state: DeliveryState) -> DeliveryState:
        stage = state["path"][-1]
        split = state.get("pr_split") if stage == "spec" and oversized_pr(state) else None
        question = (split["question"] if split else state.get("human_question")) or "What should Foreman know to continue?"
        answer = interrupt({"stage": stage, "question": question})
        return {"human_answers": [*state.get("human_answers", []), answer]}

    def quota_wait(state: DeliveryState) -> DeliveryState:
        interrupt({"reason": "quota_exhausted", "detail": state.get("halt_detail")})
        return {"halt": "", "halt_detail": ""}

    def decide_pr_size(state: DeliveryState) -> DeliveryState:
        size = oversized_pr(state)
        assert size is not None
        split = state.get("pr_split")
        plan = split["plan"] if split else state.get("spec", "")
        answer = PullRequestSizeDecision(interrupt({
            "stage": "pr_size",
            "question": (
                f"The pull request has {size['lines']} changed lines. The limit is {PULL_REQUEST_LINES}.\n"
                f"{plan}\n"
                "Use --answer split to accept the split or --answer override to review this diff."
            ),
        }))
        if answer == PullRequestSizeDecision.OVERRIDE:
            return {"pr_size_override": size}
        return {}

    dispatch_review = bind("review")

    def moved_head_detail(actual: str, verified: str) -> str:
        return (
            f"HEAD is {actual}, verification checked {verified}. "
            "Reset the PR branch to the verified commit.\n"
            f"  git reset --keep {verified}\n"
            "Then rerun the same command. New commits need a new run."
        )

    def moved_before_review(actual: str, verified: str) -> dict[str, object]:
        return {
            "merge": "not_armed", "halt": "merge_not_armed", "path": ["review"],
            "halt_detail": moved_head_detail(actual, verified),
        }

    def review(state: DeliveryState) -> dict[str, object]:
        if isinstance(run, DryRun):
            return dispatch_review(state)
        try:
            head = state.get("verified_head")
            if head is None:
                raise WorktreeInspectionFailed("The run did not record a verified HEAD.")
            worktree = run.workspace.inspect()
            if worktree.head != head:
                return moved_before_review(worktree.head, head)
            if worktree.branch is None:
                raise WorktreeInspectionFailed("HEAD is detached. Pull request sizing requires a checked-out branch.")
            repo = state.get("repo")
            assert repo is not None
            base = run.forge.pull_request_base(repo, worktree.branch)
            size = run.workspace.pull_request_size(head, base)
            checked = run.workspace.inspect()
            if checked.head != head:
                return moved_before_review(checked.head, head)
            if not checked.clean or checked.branch != worktree.branch:
                raise WorktreeInspectionFailed("The worktree changed after verification.")
        except (ForgeError, WorktreeInspectionFailed) as exc:
            return {"halt": "pr_size_unavailable", "halt_detail": str(exc)}
        measured: DeliveryState = {**state, "pr_size": size}
        if oversized_pr(measured):
            return {"pr_size": size}
        return {**dispatch_review(measured), "pr_size": size}

    def after_triage(state: DeliveryState) -> str:
        if state.get("halt"):
            return "done"
        decision = state.get("triage")
        if decision == "needs_human":
            return "needs_human" if checkpointer is not None else "done"
        if decision == "question":
            return "spec" if state.get("issue_kind") == "parent" else "research"
        return decision or "done"

    def after_spec(state: DeliveryState) -> str:
        if state.get("halt"):
            return "done"
        if oversized_pr(state):
            if checkpointer is None:
                return "done"
            split = state.get("pr_split")
            if split and split["gate"] == "needs_human":
                return "needs_human"
            return "decide_pr_size"
        if state.get("spec_gate") == "needs_human" and checkpointer is not None:
            return "needs_human"
        if state.get("issue_kind") == "parent":
            return "done"
        return "implement" if state.get("spec_gate") == "ready" else "done"

    def after_review(state: DeliveryState) -> str:
        if state.get("halt"):
            return "done"
        if oversized_pr(state):
            if state.get("pr_size_override"):
                return "decide_pr_size" if checkpointer is not None else "done"
            return "spec"
        if state.get("review") == "approve":
            return "merge"
        if state.get("review_rounds", 0) < config.max_review_rounds:
            return "implement"
        return "done"

    def after_verify(state: DeliveryState) -> str:
        if state.get("halt"):
            return "done"
        if state.get("verify") == "pass":
            slice = state["slice"]
            if browser_required(slice) or formal_required(slice):
                return "proof"
            return "review"
        if state.get("review_rounds", 0) < config.max_review_rounds:
            return "implement"
        return "done"

    def after_proof(state: DeliveryState) -> str:
        if state.get("halt"):
            return "done"
        return "implement" if state.get("proof_labels") else "review"

    def after_implement(state: DeliveryState) -> str:
        return "done" if state.get("halt") else "verify"

    def arm_merge(state: DeliveryState) -> DeliveryState:
        repo = state["repo"]
        head = state.get("verified_head")

        def halted(halt: MergeHalt, detail: str) -> DeliveryState:
            return {
                "merge": "not_armed", "halt": halt, "halt_detail": detail, "path": ["merge"],
                "merge_command": merge_command(repo, "<number>", head or "<sha>"),
            }

        match run:
            case DryRun():
                return {
                    "merge": "skipped", "path": ["merge"],
                    "merge_command": run.record_merge(repo, state.get("head_sha")),
                }
            case LiveRun(forge=forge, workspace=workspace):
                if head is None:
                    return halted("merge_not_armed", "The run did not record a verified HEAD.")
                try:
                    worktree = workspace.inspect()
                except WorktreeInspectionFailed as exc:
                    return halted("merge_not_armed", str(exc))
                if not worktree.clean:
                    return halted("merge_not_armed", "The worktree has changes or Git could not inspect it.")
                if worktree.head != head:
                    return {
                        **halted("merge_not_armed", head_mismatch_detail(worktree.head, head)),
                        "merge_observed_head": worktree.head,
                    }
                if worktree.branch is None:
                    return halted("merge_not_armed", "HEAD is detached. Merge requires a checked-out branch.")
                try:
                    hold = forge.merge_hold(repo)
                except MergeHoldUnreadable as exc:
                    return halted("merge_held", f"could not read {MERGE_HOLD_VARIABLE}: {exc}")
                if hold:
                    return halted("merge_held", hold)
                try:
                    forge.arm_auto_merge(repo, state["issue"], worktree.branch, head)
                except MergeNotArmed as exc:
                    return halted("merge_not_armed", str(exc))
                return {"merge": "armed", "path": ["merge"]}

    def finish(state: DeliveryState) -> DeliveryState:
        stop = stop_reason(state)
        update: DeliveryState = {"stop": stop, "path": ["done"]}
        split = state.get("pr_split")
        if stop == "sliced" and split:
            update["slices"] = split["slices"]
        if stop == "pr_split_required":
            update["halt_detail"] = split["plan"] if split else state.get("spec", "")
        return update

    builder = StateGraph(DeliveryState)

    def add_agent_edges(stage: str, route: Callable[[DeliveryState], str], targets: dict[str, str]) -> None:
        def next_stage(state: DeliveryState) -> str:
            return "quota_wait" if state.get("halt") == "quota_exhausted" else route(state)

        builder.add_conditional_edges(stage, next_stage, {**targets, "quota_wait": "quota_wait"})


    graph_nodes = {"verify": verify, "review": review}
    for name in REQUIRED_NODES:
        builder.add_node(name, graph_nodes[name] if name in graph_nodes else bind(name))
    builder.add_node("proof", prove)
    builder.add_node("needs_human", needs_human)
    builder.add_node("quota_wait", quota_wait)

    builder.add_node("decide_pr_size", decide_pr_size)
    builder.add_node("merge", arm_merge)
    builder.add_node("done", finish)
    builder.add_conditional_edges(
        START, lambda state: "done" if state.get("halt") else "triage", {"done": "done", "triage": "triage"},
    )
    add_agent_edges(
        "triage",
        after_triage,
        {"implement": "implement", "spec": "spec", "research": "research",
         "needs_human": "needs_human", "done": "done"},
    )
    add_agent_edges(
        "spec", after_spec,
        {"implement": "implement", "needs_human": "needs_human", "decide_pr_size": "decide_pr_size", "done": "done"},
    )
    builder.add_conditional_edges(
        "decide_pr_size", lambda state: "done" if oversized_pr(state) else "verify", {"done": "done", "verify": "verify"},
    )
    builder.add_conditional_edges(
        "needs_human", lambda state: state["path"][-1], {"triage": "triage", "spec": "spec"},
    )
    builder.add_conditional_edges(
        "quota_wait", lambda state: state["path"][-1], {name: name for name in REQUIRED_NODES if name != "verify"},
    )
    add_agent_edges("research", lambda _: "done", {"done": "done"})
    add_agent_edges("implement", after_implement, {"verify": "verify", "done": "done"})
    add_agent_edges(
        "review",
        after_review,
        {"merge": "merge", "implement": "implement", "spec": "spec", "decide_pr_size": "decide_pr_size", "done": "done"},
    )
    builder.add_conditional_edges(
        "verify",
        after_verify,
        {"proof": "proof", "review": "review", "implement": "implement", "done": "done"},
    )
    builder.add_conditional_edges(
        "proof", after_proof, {"review": "review", "implement": "implement", "done": "done"},
    )
    builder.add_edge("merge", "done")
    builder.add_edge("done", END)
    return builder.compile(checkpointer=checkpointer)


def run_delivery(
    dispatcher: FailoverDispatcher,
    *,
    repo: str,
    request: str,
    check: str = "",
    config: Config | None = None,
    slice: dict[str, object] | None = None,
    scratch: Path | None = None,
) -> DeliveryState:
    state: DeliveryState = {
        "repo": repo,
        "request": request,
        "check": check,
        "issue_kind": "parent" if slice is None else "slice",
        "review_rounds": 0,
        "models_used": {},
        "path": [],
    }
    if slice is not None:
        state.update(load_slice(slice))
    config = config if config is not None else load_config()
    if scratch is not None:
        state["pstack_pin"] = config.pstack.model_dump()
    try:
        graph = compile_delivery(dispatcher, config, run=DryRun(), scratch=scratch)
    except SkillMissing as exc:
        return {**state, "path": ["done"], "halt": exc.halt, "stop": exc.halt,
                "halt_detail": str(exc), "skill_reason": exc.reason}
    return graph.invoke(state)


def diagram(dispatcher: FailoverDispatcher | None = None, config: Config | None = None) -> str:
    if dispatcher is None:
        dispatcher = FailoverDispatcher(
            lambda node, model, state, instruction: Outcome(
                "ran", {"triage": "implement", "slice": None, "human_question": None},
            ),
        )
    graph = compile_delivery(dispatcher, config if config is not None else load_config(), run=DryRun())
    return graph.get_graph().draw_mermaid()

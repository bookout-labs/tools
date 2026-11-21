"""The delivery graph. Edges live here. Models live in models.toml."""

from dataclasses import dataclass
from enum import StrEnum
from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from typing import Annotated, Literal, TypedDict, cast, get_args
import operator

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from unidiff.errors import UnidiffParseError
from unidiff.patch import PatchSet

from foreman.adapters import Outcome
from foreman.config import Config, ConfigError, load_config, order_for
from foreman.dispatch import FailoverDispatcher, InvalidOutput, QuotaExhausted
from foreman.github import (
    MERGE_HOLD_VARIABLE, Forge, ForgeError, MergeHoldUnreadable, MergeNotArmed, PullRequest, ReviewComment, merge_command,
)
from foreman.proof import browser_proof_label, browser_required, formal_proof_label, formal_required
from foreman.redaction import redact
from foreman.stage_output import (
    Contract, HumanQuestionOutput, PULL_REQUEST_LINES, ReviewOutput, STAGE_OUTPUTS, Slice, SpecOutput, StoredSlice, TriageOutput, VerifyOutput, parse_output,
)
from foreman.workspace import CheckLog, PullRequestSize, ReviewDiff, Workspace, WorktreeInspectionFailed, commit_sha, head_mismatch_detail

GRAPH_NAME = "delivery"
PACKAGE = Path(__file__).parent
REQUIRED_NODES = ("triage", "research", "spec", "implement", "verify", "review")
STAGE_ORDER = REQUIRED_NODES + ("merge",)
SKILLS_FOR = {
    "review": ("review-quality", "merge-risk"),
}
MergeHalt = Literal["merge_held", "merge_not_armed"]
HaltReason = Literal[
    MergeHalt, "no_dispatcher", "no_reviewer", "invalid_output", "cli_missing",
    "dirty_worktree", "insufficient_evidence", "review_cap", "verify_cap",
    "missing_slice", "review_unavailable", "review_not_posted", "spec_cap",
]
RESUMABLE_HALTS: frozenset[MergeHalt] = frozenset(get_args(MergeHalt))


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


SpecRejectionKind = Literal["duplicate_title", "duplicate_builder", "dangling_seam", "dependency_cycle", "existing_seam_builder"]


class SpecRejection(TypedDict):
    kind: SpecRejectionKind
    detail: str


class InvalidSlicePlan(InvalidOutput):
    def __init__(self, kind: SpecRejectionKind, detail: str):
        super().__init__(detail)
        self.reason: SpecRejection = {"kind": kind, "detail": detail}


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
    spec_rejection: SpecRejection | None
    slices: list[dict[str, object]]
    child_issue_numbers: list[int]
    slice: dict[str, object]
    issue_kind: str
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
    halt: HaltReason
    halt_detail: str
    stop: str
    path: Annotated[list[str], operator.add]


def halted(reason: HaltReason, detail: str, path: str | None) -> DeliveryState:
    if not detail.strip():
        raise ValueError(f"{reason} requires a nonempty halt detail")
    return {"halt": reason, "halt_detail": detail, "path": [path] if path else []}


def stage_text(name: str) -> str:
    path = PACKAGE / "stages" / f"{name}.md"
    text = path.read_text().strip()
    for skill in SKILLS_FOR.get(name, ()):
        skill_text = (PACKAGE / "skills" / f"{skill}.md").read_text().strip()
        text = f"{text}\n\n{skill_text}"
    return text


def clean_slices(
    raw: list[Slice] | None,
    workspace: Workspace | None,
) -> list[dict[str, object]]:
    if not raw:
        raise InvalidOutput("slices: ready parent spec needs at least one slice")
    if len(raw) == 1:
        return [{**raw[0].model_dump(), "depends_on": []}]
    slices = {item.title: item for item in raw}
    if len(slices) != len(raw):
        raise InvalidSlicePlan("duplicate_title", "Slice titles must be unique.")
    builders: dict[str, str] = {}
    for item in raw:
        for seam in item.builds:
            if seam.name in builders and builders[seam.name] != item.title:
                raise InvalidSlicePlan("duplicate_builder", f"{seam.name}: built by {builders[seam.name]} and {item.title}.")
            builders[seam.name] = item.title
    graph: dict[str, dict[str, None]] = {}
    for item in raw:
        dependencies = graph[item.title] = {}
        for seam in item.uses:
            if seam.path is not None and (workspace is None or not workspace.has_file(seam.path)):
                raise InvalidSlicePlan("dangling_seam", f"{item.title} uses {seam.name}, but {seam.path} is not a workspace file.")
            if builder := builders.get(seam.name):
                if seam.exists:
                    raise InvalidSlicePlan("existing_seam_builder", f"{seam.name}: marked existing but built by {builder}.")
                if builder != item.title:
                    dependencies[builder] = None
            elif seam.path is None:
                raise InvalidSlicePlan("dangling_seam", f"{item.title} uses {seam.name}, which has no builder or existing file.")
    try:
        ordered = list(TopologicalSorter(graph).static_order())
    except CycleError as exc:
        raise InvalidSlicePlan("dependency_cycle", "Dependency cycle: " + " -> ".join(exc.args[1])) from exc
    return [{**slices[title].model_dump(), "depends_on": list(graph[title])} for title in ordered]


def load_slice(raw: object) -> dict[str, object]:
    return parse_output(StoredSlice, raw).model_dump(exclude_unset=True)


def stage_update(
    name: str, output: Contract, state: DeliveryState, model: str | None = None, *, workspace: Workspace | None = None,
) -> DeliveryState:
    update = cast(DeliveryState, output.model_dump(exclude={"slice", "slices"}))
    if isinstance(output, HumanQuestionOutput):
        update["human_question"] = output.human_question or ""
    if isinstance(output, SpecOutput):
        update["spec_rejection"] = None
        slices = (
            clean_slices(output.slices, workspace)
            if state.get("issue_kind") == "parent" and output.spec_gate == "ready" else []
        )
        if oversized_pr(state):
            split: PullRequestSplit = {
                "plan": output.spec, "gate": output.spec_gate, "question": output.human_question, "slices": slices,
            }
            update = {"pr_split": split, "spec_rejection": None}
        elif state.get("issue_kind") == "parent" and output.spec_gate == "ready":
            update["slices"] = slices
    update["path"] = [name]
    if isinstance(output, TriageOutput) and state.get("issue_kind") == "parent" and output.triage == "implement":
        if output.slice is None:
            raise InvalidOutput("slice: required when a parent triages as implement")
        update["slice"] = output.slice.model_dump()
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
    checkpointer: BaseCheckpointSaver | None = None,
):
    missing = [name for name in REQUIRED_NODES if name not in config.nodes]
    if missing:
        raise ValueError(f"delivery config is missing {', '.join(missing)}")
    prompts = {name: stage_text(name) for name in REQUIRED_NODES}
    plan_workspace = run.workspace if isinstance(run, LiveRun) else None

    def bind(name: str):
        def node(state: DeliveryState) -> DeliveryState:
            if dispatcher is None:
                return halted("no_dispatcher", "No dispatcher is configured.", name)
            try:
                models = order_for(name, config, state.get("models_used") or {}, state.get("implement_models"))
            except ConfigError as exc:
                return halted("no_reviewer", str(exc), name)
            instruction = prompts[name]
            if name == "implement":
                slice = state["slice"]
                for required, skill in ((browser_required, "browser-proof"), (formal_required, "formal-proof")):
                    if required(slice):
                        proof = (PACKAGE / "skills" / f"{skill}.md").read_text().strip()
                        instruction = f"{instruction}\n\n{proof}"
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
                    return halted("review_unavailable", str(exc), name)
                review_context = (run.forge, target, diff)
                instruction = f"{instruction}\n\nRead the reviewed diff with this command in the checkout:\n{diff.command}"
            outcome, model = dispatcher.run(name, models, state, instruction)
            if outcome.kind == "failed":
                return halted("invalid_output", f"{model}: {outcome.detail}", name)
            if outcome.kind == "cli_missing":
                return halted("cli_missing", outcome.detail, name)
            if outcome.kind == "quota":
                if name != "review":
                    raise QuotaExhausted(model)
                return halted("no_reviewer", f"review quota exhausted: {model}", name)
            try:
                output = parse_output(STAGE_OUTPUTS[name], outcome.output)
                update = stage_update(name, output, state, model, workspace=plan_workspace)
            except InvalidSlicePlan as exc:
                rounds = state.get("review_rounds", 0) + 1
                update: DeliveryState = {"path": [name], "spec_rejection": exc.reason, "review_rounds": rounds}
                if rounds >= config.max_review_rounds:
                    update.update(halted("spec_cap", str(exc), name))
                return update
            except InvalidOutput as exc:
                return halted("invalid_output", str(exc), name)

            if review_context is not None and isinstance(output, ReviewOutput):
                forge, target, diff = review_context
                body, comments = review_submission(output, diff)
                try:
                    forge.post_review(repo, target, body, comments)
                except ForgeError as exc:
                    return {**update, **halted("review_not_posted", str(exc), name)}
            return update

        return node

    scripted_verify = bind("verify")

    def verify(state: DeliveryState) -> DeliveryState:
        match run:
            case DryRun():
                return scripted_verify(state)
            case LiveRun(workspace=workspace, check_log=check_log):
                try:
                    worktree = workspace.inspect()
                except WorktreeInspectionFailed as exc:
                    return halted("dirty_worktree", str(exc), "verify")
                if not worktree.clean:
                    return halted("dirty_worktree", "The worktree has changes.", "verify")
                exit_code = workspace.run(state["check"], log=check_log)
                verdict = "pass" if exit_code == 0 else "fail"
                checked: DeliveryState = {"verified_head": worktree.head} if exit_code == 0 else {}
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
            update.update(halted("insufficient_evidence", "Required proof is missing.", "proof"))
        elif "block" in labels.values():
            update["review_rounds"] = state.get("review_rounds", 0) + 1
            if update["review_rounds"] >= config.max_review_rounds:
                detail = f"Proof failed after {update['review_rounds']} rounds: {update['proof_labels']}."
                update.update(halted("review_cap", detail, "proof"))
        return update

    def needs_human(state: DeliveryState) -> DeliveryState:
        stage = state["path"][-1]
        split = state.get("pr_split") if stage == "spec" and oversized_pr(state) else None
        question = (split["question"] if split else state.get("human_question")) or "What should Foreman know to continue?"
        answer = interrupt({"stage": stage, "question": question})
        return {"human_answers": [*state.get("human_answers", []), answer]}

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
        if state.get("spec_rejection"):
            return "spec"
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

        def merge_halted(halt: MergeHalt, detail: str) -> DeliveryState:
            return {
                **halted(halt, detail, "merge"), "merge": "not_armed",
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
                    return merge_halted("merge_not_armed", "The run did not record a verified HEAD.")
                try:
                    worktree = workspace.inspect()
                except WorktreeInspectionFailed as exc:
                    return merge_halted("merge_not_armed", str(exc))
                if not worktree.clean:
                    return merge_halted("merge_not_armed", "The worktree has changes or Git could not inspect it.")
                if worktree.head != head:
                    return {
                        **merge_halted("merge_not_armed", head_mismatch_detail(worktree.head, head)),
                        "merge_observed_head": worktree.head,
                    }
                if worktree.branch is None:
                    return merge_halted("merge_not_armed", "HEAD is detached. Merge requires a checked-out branch.")
                try:
                    hold = forge.merge_hold(repo)
                except MergeHoldUnreadable as exc:
                    return merge_halted("merge_held", f"could not read {MERGE_HOLD_VARIABLE}: {exc}")
                if hold:
                    return merge_halted("merge_held", f"{MERGE_HOLD_VARIABLE} is set" + (f": {hold}" if hold.strip() else "."))
                try:
                    forge.arm_auto_merge(repo, state["issue"], worktree.branch, head)
                except MergeNotArmed as exc:
                    return merge_halted("merge_not_armed", str(exc))
                return {"merge": "armed", "path": ["merge"]}

    def finish(state: DeliveryState) -> DeliveryState:
        reason = stop_reason(state)
        if not state.get("halt"):
            rounds = state.get("review_rounds", 0)
            if reason == "verify_cap":
                detail = f"Verification failed after {rounds} rounds. Check {state.get('check') or '(none)'} failed."
                return {**halted("verify_cap", detail, "done"), "stop": reason}
            if reason == "review_cap":
                detail = f"Review requested changes after {rounds} rounds. Last review: {state.get('detail', '')}"
                return {**halted("review_cap", detail, "done"), "stop": reason}
            if reason == "needs_human":
                return {"stop": reason, "path": ["done"], "halt_detail":
                        state.get("human_question") or "What should Foreman know to continue?"}
        update: DeliveryState = {"stop": reason, "path": ["done"]}
        split = state.get("pr_split")
        if reason == "sliced" and split:
            update["slices"] = split["slices"]
        if reason == "pr_split_required":
            update["halt_detail"] = split["plan"] if split else state.get("spec", "")
        return update

    builder = StateGraph(DeliveryState)
    graph_nodes = {"verify": verify, "review": review}
    for name in REQUIRED_NODES:
        builder.add_node(name, graph_nodes[name] if name in graph_nodes else bind(name))
    builder.add_node("proof", prove)
    builder.add_node("needs_human", needs_human)
    builder.add_node("decide_pr_size", decide_pr_size)
    builder.add_node("merge", arm_merge)
    builder.add_node("done", finish)
    builder.add_conditional_edges(
        START, lambda state: "done" if state.get("halt") else "triage", {"done": "done", "triage": "triage"},
    )
    builder.add_conditional_edges(
        "triage",
        after_triage,
        {"implement": "implement", "spec": "spec", "research": "research",
         "needs_human": "needs_human", "done": "done"},
    )
    builder.add_conditional_edges(
        "spec", after_spec,
        {"spec": "spec", "implement": "implement", "needs_human": "needs_human", "decide_pr_size": "decide_pr_size", "done": "done"},
    )
    builder.add_conditional_edges(
        "decide_pr_size", lambda state: "done" if oversized_pr(state) else "verify", {"done": "done", "verify": "verify"},
    )
    builder.add_conditional_edges(
        "needs_human", lambda state: state["path"][-1], {"triage": "triage", "spec": "spec"},
    )
    builder.add_edge("research", "done")
    builder.add_conditional_edges("implement", after_implement, {"verify": "verify", "done": "done"})
    builder.add_conditional_edges(
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
        state["slice"] = load_slice(slice)
    graph = compile_delivery(dispatcher, config if config is not None else load_config(), run=DryRun())
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

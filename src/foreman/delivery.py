"""The delivery graph. Edges live here. Models live in models.toml."""

from dataclasses import dataclass
from enum import StrEnum
from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from typing import Annotated, Literal, TypedDict
import operator

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from foreman.adapters import Outcome
from foreman.config import Config, ConfigError, load_config, order_for
from foreman.dispatch import FailoverDispatcher, InvalidOutput, QuotaExhausted
from foreman.github import Forge, ForgeError, MergeNotArmed, merge_command
from foreman.proof import browser_proof_label, browser_required, formal_proof_label, formal_required
from foreman.stage_output import (
    Contract, HumanQuestionOutput, PULL_REQUEST_LINES, STAGE_OUTPUTS, Slice, SpecOutput, TriageOutput, VerifyOutput,
    parse_output,
)
from foreman.workspace import PullRequestSize, Workspace, WorktreeInspectionFailed

GRAPH_NAME = "delivery"
PACKAGE = Path(__file__).parent
REQUIRED_NODES = ("triage", "research", "spec", "implement", "verify", "review")
STAGE_ORDER = REQUIRED_NODES + ("merge",)
SKILLS_FOR = {
    "review": ("review-quality", "merge-risk"),
}
@dataclass(frozen=True)
class LiveRun:
    forge: Forge
    workspace: Workspace


@dataclass(frozen=True)
class DryRun:
    pass


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
    research: str
    implementation: str
    head_sha: str | None
    verified_head: str
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
    review_note: str | None
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
    path = PACKAGE / "stages" / f"{name}.md"
    text = path.read_text().strip()
    for skill in SKILLS_FOR.get(name, ()):
        skill_text = (PACKAGE / "skills" / f"{skill}.md").read_text().strip()
        text = f"{text}\n\n{skill_text}"
    return text


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


def load_slice(raw: object) -> dict[str, object]:
    return clean_slices([parse_output(Slice, raw)], "slice", allow_external_dependencies=True)[0]


def stage_update(name: str, output: Contract, state: DeliveryState, model: str | None = None) -> dict[str, object]:
    update: dict[str, object] = output.model_dump(exclude={"slice", "slices"})
    if isinstance(output, HumanQuestionOutput):
        update["human_question"] = output.human_question or ""
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

    def bind(name: str):
        def node(state: DeliveryState) -> dict[str, object]:
            if dispatcher is None:
                return {"path": [name], "halt": "no_dispatcher"}
            try:
                models = order_for(name, config, state.get("models_used") or {}, state.get("implement_models"))
            except ConfigError as exc:
                return {"path": [name], "halt": "no_reviewer", "halt_detail": str(exc)}
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
            outcome, model = dispatcher.run(name, models, state, instruction)
            if outcome.kind == "failed":
                return {"path": [name], "halt": "invalid_output", "halt_detail": f"{model}: {outcome.detail}"}
            if outcome.kind == "cli_missing":
                return {"path": [name], "halt": "cli_missing", "halt_detail": outcome.detail}
            if outcome.kind == "quota":
                if name != "review":
                    raise QuotaExhausted(model)
                return {"path": [name], "halt": "no_reviewer", "halt_detail": f"review quota exhausted: {model}"}
            try:
                return stage_update(name, parse_output(STAGE_OUTPUTS[name], outcome.output), state, model)
            except InvalidOutput as exc:
                return {"path": [name], "halt": "invalid_output", "halt_detail": str(exc)}

        return node

    scripted_verify = bind("verify")

    def verify(state: DeliveryState) -> dict[str, object]:
        match run:
            case DryRun():
                return scripted_verify(state)
            case LiveRun(workspace=workspace):
                try:
                    worktree = workspace.inspect()
                except WorktreeInspectionFailed as exc:
                    return {"path": ["verify"], "halt": "dirty_worktree", "halt_detail": str(exc)}
                if not worktree.clean:
                    return {"path": ["verify"], "halt": "dirty_worktree"}
                exit_code = workspace.run(state["check"])
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

    def merge_not_armed(detail: str) -> DeliveryState:
        return {"merge": "not_armed", "halt": "merge_not_armed", "halt_detail": detail, "path": ["merge"]}

    def moved_head(actual: str, verified: str) -> DeliveryState:
        return merge_not_armed(
            f"HEAD is {actual}, verification checked {verified}. This run is finished and the merge was not armed.",
        )

    def review(state: DeliveryState) -> dict[str, object]:
        if isinstance(run, DryRun):
            return dispatch_review(state)
        try:
            head = state.get("verified_head")
            if head is None:
                raise WorktreeInspectionFailed("The run did not record a verified HEAD.")
            worktree = run.workspace.inspect()
            if worktree.head != head:
                return dict(moved_head(worktree.head, head))
            if worktree.branch is None:
                raise WorktreeInspectionFailed("HEAD is detached. Pull request sizing requires a checked-out branch.")
            repo = state.get("repo")
            assert repo is not None
            base = run.forge.pull_request_base(repo, worktree.branch)
            size = run.workspace.pull_request_size(head, base)
            checked = run.workspace.inspect()
            if checked.head != head:
                return dict(moved_head(checked.head, head))
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
        match run:
            case DryRun():
                return {
                    "merge": "skipped", "path": ["merge"],
                    "merge_command": merge_command(state["repo"], "<number>", state.get("head_sha") or "<sha>"),
                }
            case LiveRun(forge=forge, workspace=workspace):
                head = state.get("verified_head")
                if head is None:
                    return merge_not_armed("The run did not record a verified HEAD.")
                try:
                    worktree = workspace.inspect()
                except WorktreeInspectionFailed as exc:
                    return merge_not_armed(str(exc))
                if not worktree.clean:
                    return merge_not_armed("The worktree has changes or Git could not inspect it.")
                if worktree.head != head:
                    return moved_head(worktree.head, head)
                if worktree.branch is None:
                    return merge_not_armed("HEAD is detached. Merge requires a checked-out branch.")
                try:
                    forge.arm_auto_merge(state["repo"], state["issue"], worktree.branch, head)
                except MergeNotArmed as exc:
                    return merge_not_armed(str(exc))
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
        {"implement": "implement", "needs_human": "needs_human", "decide_pr_size": "decide_pr_size", "done": "done"},
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

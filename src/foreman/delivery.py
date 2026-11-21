"""The delivery graph. Edges live here. Models live in models.toml."""

from dataclasses import dataclass
from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from typing import Annotated, Literal, TypedDict
import operator
import re

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from foreman.adapters import Outcome
from foreman.config import Config, ConfigError, load_config, order_for
from foreman.dispatch import FailoverDispatcher, InvalidOutput, QuotaExhausted
from foreman.github import (
    MERGE_BASE, MERGE_HOLD_VARIABLE, Forge, ForgeError, MergeHoldUnreadable, MergeNotArmed, merge_command,
    pull_request_command, repository_url,
)
from foreman.proof import browser_proof_label, browser_required, formal_proof_label, formal_required
from foreman.stage_output import (
    Contract, HumanQuestionOutput, STAGE_OUTPUTS, Slice, SpecOutput, TriageOutput, VerifyOutput, parse_output,
)
from foreman.workspace import (
    Workspace, WorkspaceError, WorktreeInspectionFailed, branch_command, commit_commands, push_command,
)

GRAPH_NAME = "delivery"
PACKAGE = Path(__file__).parent
REQUIRED_NODES = ("triage", "research", "spec", "implement", "verify", "review")
STAGE_ORDER = REQUIRED_NODES + ("merge",)
SKILLS_FOR = {
    "review": ("review-quality", "merge-risk"),
}
MergeHalt = Literal["merge_held", "merge_not_armed"]
PUBLISH_FAILED = "publish_failed"


@dataclass(frozen=True)
class LiveRun:
    forge: Forge
    workspace: Workspace


@dataclass(frozen=True)
class DryRun:
    pass


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
    delivery_branch: str
    head_sha: str
    commands: Annotated[list[list[str]], operator.add]
    verified_head: str
    merge: Literal["armed", "skipped", "not_armed"]
    merge_command: list[str]
    browser_proof: dict[str, object] | None
    formal_proof: dict[str, object] | None
    proof_labels: dict[str, str]
    human_question: str
    human_answers: list[str]
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
    update["path"] = [name]
    if isinstance(output, SpecOutput) and state.get("issue_kind") == "parent" and output.spec_gate == "ready":
        update["slices"] = clean_slices(output.slices, "slices")
    if isinstance(output, HumanQuestionOutput):
        update["human_question"] = output.human_question or ""
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
    if state.get("triage") == "needs_human" or state.get("spec_gate") == "needs_human":
        return "needs_human"
    if state.get("issue_kind") == "parent" and state.get("spec_gate") == "ready":
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

    scripted_implement = bind("implement")
    scripted_verify = bind("verify")

    def implement(state: DeliveryState) -> dict[str, object]:
        if dispatcher is None:
            return scripted_implement(state)
        assert "slice" in state
        title = str(state["slice"]["title"])
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60].rstrip("-") or "change"
        branch = state.get("delivery_branch") or f"{state.get('issue', '<issue>')}-{slug}"
        message = f"{state.get('issue', '<issue>')}: {' '.join(title.split())}"
        if isinstance(run, DryRun):
            update = scripted_implement(state)
            if not update.get("halt"):
                commands = [] if "delivery_branch" in state else [branch_command(branch, MERGE_BASE)]
                update.update(delivery_branch=branch, commands=[*commands, *commit_commands(message)])
            return update

        workspace = run.workspace
        try:
            before = workspace.inspect()
            if not before.clean:
                return {"path": ["implement"], "halt": "dirty_worktree"}
            if "delivery_branch" in state and (before.branch != branch or before.head != state.get("head_sha")):
                raise WorkspaceError("The checkout moved since the previous implementation.")
            if before.branch != branch:
                workspace.create_branch(branch, MERGE_BASE)
                before = workspace.inspect()
                if not before.clean or before.branch != branch:
                    raise WorkspaceError("Git did not leave a clean checkout on the delivery branch.")
            update = scripted_implement(state)
            after = workspace.inspect()
            if after.head != before.head or after.branch != branch:
                return {"path": ["implement"], "halt": "failed", "head_sha": after.head,
                        "halt_detail": "The worker changed HEAD or switched branches."}
            if update.get("halt"):
                return update
            if after.clean:
                raise WorkspaceError("no changes")
            workspace.commit(message)
            committed = workspace.inspect()
            if not committed.clean or committed.branch != branch or committed.head == before.head:
                raise WorkspaceError("Git did not leave a clean new commit on the delivery branch.")
            return {**update, "delivery_branch": branch, "head_sha": committed.head}
        except WorkspaceError as exc:
            return {"path": ["implement"], "halt": "failed", "halt_detail": str(exc)}

    def publish(state: DeliveryState) -> DeliveryState:
        assert "repo" in state and "delivery_branch" in state and "slice" in state
        repo = state["repo"]
        branch = state["delivery_branch"]
        title = str(state["slice"]["title"])
        body = f"Closes #{state.get('issue', '<issue>')}"
        if isinstance(run, DryRun):
            return {"path": ["publish"], "commands": [
                push_command(repository_url(repo), branch),
                pull_request_command(repo, branch, MERGE_BASE, title, body),
            ]}
        try:
            worktree = run.workspace.inspect()
            if not worktree.clean or worktree.branch != branch or worktree.head != state.get("head_sha"):
                raise WorkspaceError("Restore the clean delivery branch at its implementation commit before publishing.")
            run.workspace.push(branch)
            run.forge.open_pull_request(repo, branch, MERGE_BASE, title, body)
        except (WorkspaceError, ForgeError) as exc:
            return {"path": ["publish"], "halt": PUBLISH_FAILED, "halt_detail": str(exc)}
        return {"path": ["publish"]}

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
                if "delivery_branch" in state and (
                    worktree.branch != state["delivery_branch"] or worktree.head != state.get("head_sha")
                ):
                    return {"path": ["verify"], "halt": "dirty_worktree",
                            "halt_detail": "The checkout moved since publication."}
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
        question = state.get("human_question") or "What should Foreman know to continue?"
        answer = interrupt({"stage": stage, "question": question})
        return {"human_answers": [*state.get("human_answers", []), answer]}

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
        if state.get("spec_gate") == "needs_human" and checkpointer is not None:
            return "needs_human"
        if state.get("issue_kind") == "parent":
            return "done"
        return "implement" if state.get("spec_gate") == "ready" else "done"

    def after_review(state: DeliveryState) -> str:
        if state.get("halt"):
            return "done"
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
                    "merge_command": merge_command(repo, "<number>", "<sha>"),
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
                    return halted(
                        "merge_not_armed",
                        f"HEAD is {worktree.head}, verification checked {head}. "
                        "Reset the PR branch to the verified commit.\n"
                        f"  git reset --keep {head}\n"
                        "Then rerun the same command. New commits need a new run."
                    )
                if worktree.branch is None:
                    return halted("merge_not_armed", "HEAD is detached. Merge requires a checked-out branch.")
                if worktree.branch != state.get("delivery_branch", worktree.branch):
                    return halted("merge_not_armed", "Switch back to the delivery branch before arming merge.")
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
        return {"stop": stop_reason(state), "path": ["done"]}

    builder = StateGraph(DeliveryState)
    for name in REQUIRED_NODES:
        builder.add_node(name, {"implement": implement, "verify": verify}.get(name) or bind(name))
    builder.add_node("publish", publish)
    builder.add_node("proof", prove)
    builder.add_node("needs_human", needs_human)
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
        "spec", after_spec, {"implement": "implement", "needs_human": "needs_human", "done": "done"},
    )
    builder.add_conditional_edges(
        "needs_human", lambda state: state["path"][-1], {"triage": "triage", "spec": "spec"},
    )
    builder.add_edge("research", "done")
    builder.add_conditional_edges(
        "implement", lambda state: "done" if state.get("halt") else "publish", {"publish": "publish", "done": "done"},
    )
    builder.add_conditional_edges(
        "publish", lambda state: "done" if state.get("halt") else "verify", {"verify": "verify", "done": "done"},
    )
    builder.add_conditional_edges(
        "review",
        after_review,
        {"merge": "merge", "implement": "implement", "done": "done"},
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
    issue: int | None = None,
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
    if issue is not None:
        state["issue"] = issue
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

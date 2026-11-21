"""The delivery graph. Edges live here. Models live in models.toml."""

from dataclasses import dataclass
from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from typing import Annotated, Literal, TypedDict
import operator
import json

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from unidiff.errors import UnidiffParseError

from foreman.adapters import Outcome
from foreman.config import Config, ConfigError, load_config, order_for
from foreman.dispatch import FailoverDispatcher, InvalidOutput, QuotaExhausted
from foreman.github import (
    MERGE_HOLD_VARIABLE, Forge, ForgeError, MergeHoldUnreadable, MergeNotArmed, PullRequest, ReviewComment, merge_command,
)
from foreman.proof import browser_proof_label, browser_required, formal_proof_label, formal_required
from foreman.stage_output import (
    Contract, HumanQuestionOutput, ReviewOutput, STAGE_OUTPUTS, Slice, SpecOutput, TriageOutput, VerifyOutput, parse_output,
)
from foreman.review_round import changed_lines, history
from foreman.workspace import ReviewDiff, Workspace, WorktreeInspectionFailed

GRAPH_NAME = "delivery"
PACKAGE = Path(__file__).parent
REQUIRED_NODES = ("triage", "research", "spec", "implement", "verify", "review")
STAGE_ORDER = REQUIRED_NODES + ("merge",)
SKILLS_FOR = {
    "review": ("review-quality", "merge-risk"),
}
MergeHalt = Literal["merge_held", "merge_not_armed"]


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
    head_sha: str | None
    verified_head: str
    merge: Literal["armed", "skipped", "not_armed"]
    merge_command: list[str]
    browser_proof: dict[str, object] | None
    formal_proof: dict[str, object] | None
    proof_labels: dict[str, str]
    human_question: str
    human_answers: list[str]
    review: str
    review_findings: list[dict[str, object]]
    reviewed_head: str
    settled_rulings: list[dict[str, object]]
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
    if isinstance(output, ReviewOutput):
        update["review"] = output.review
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
        changed = changed_lines(diff.patch)
    except UnidiffParseError:
        changed = set()
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
            past = history({"rounds": state.get("review_findings", []), "settled": state.get("settled_rulings", [])})
            review_context: tuple[Forge, PullRequest, ReviewDiff] | None = None
            repo = state.get("repo", "")
            if name == "review" and isinstance(run, LiveRun):
                head, issue = state.get("verified_head"), state.get("issue")
                try:
                    worktree = run.workspace.inspect()
                    if not (repo and issue is not None and head and worktree.clean and worktree.branch and worktree.head == head):
                        raise WorktreeInspectionFailed("Review requires a clean branch at the verified HEAD.")
                    target = run.forge.review_target(repo, issue, worktree.branch, head)
                    if past.rounds:
                        diff = run.workspace.review_diff(past.rounds[-1].reviewed_head, head, interdiff=True)
                    else:
                        diff = run.workspace.review_diff(target.baseRefOid, head)
                except (ForgeError, WorktreeInspectionFailed) as exc:
                    return {"path": [name], "halt": "review_unavailable", "halt_detail": str(exc)}
                review_context = (run.forge, target, diff)
                instruction = f"{instruction}\n\nRead the reviewed diff with this command in the checkout:\n{diff.command}"
            worker_state: DeliveryState = state.copy()
            if name == "review":
                scope = "Full PR review." if not past.rounds else "Check only unresolved findings and the interdiff."
                instruction += "\n\n" + scope
                instruction += "\nUnresolved findings:\n" + json.dumps([f.model_dump() for f in past.unresolved()])
                instruction += "\nSettled, don't reopen:\n" + json.dumps([r.model_dump() for r in past.rulings()])
                for key in ("review_findings", "settled_rulings", "findings", "detail", "implementation"):
                    worker_state.pop(key, None)
            outcome, model = dispatcher.run(name, models, worker_state, instruction)
            if outcome.kind == "failed":
                return {"path": [name], "halt": "invalid_output", "halt_detail": f"{model}: {outcome.detail}"}
            if outcome.kind == "cli_missing":
                return {"path": [name], "halt": "cli_missing", "halt_detail": outcome.detail}
            if outcome.kind == "quota":
                if name != "review":
                    raise QuotaExhausted(model)
                return {"path": [name], "halt": "no_reviewer", "halt_detail": f"review quota exhausted: {model}"}
            try:
                output = parse_output(STAGE_OUTPUTS[name], outcome.output)
                rethink = ""
                unresolved = []
                if isinstance(output, ReviewOutput):
                    patch = review_context[2].patch if review_context else None
                    unresolved, rethink = past.accept(output, state.get("verified_head") or state.get("head_sha") or "", patch)
                update = stage_update(name, output, state, model)
                if isinstance(output, ReviewOutput):
                    update.update(review_findings=[r.model_dump() for r in past.rounds],
                                  reviewed_head=past.rounds[-1].reviewed_head,
                                  findings=[f.model_dump() for f in unresolved])
                    update["review_change_kind"] = ("nits" if all(f.severity == "nit" for f in unresolved) else "blocking") if unresolved else None
                    if rethink:
                        update.update(halt="design_rethink", halt_detail=rethink, human_question=rethink,
                                      review_rounds=state.get("review_rounds", 0))
            except InvalidOutput as exc:
                return {"path": [name], "halt": "invalid_output", "halt_detail": str(exc)}

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
        question = state.get("human_question") or "What should Foreman know to continue?"
        answer = interrupt({"stage": stage, "question": question})
        update: DeliveryState = {"human_answers": [*state.get("human_answers", []), answer]}
        if stage == "review":
            past = history({"rounds": state.get("review_findings", []), "settled": state.get("settled_rulings", [])})
            update.update(halt="", halt_detail="", settled_rulings=[r.model_dump() for r in past.settled] +
                          [{"id": f"design:{len(past.rounds)}", "text": answer}])
        return update

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
        if state.get("halt") == "design_rethink" and checkpointer is not None:
            return "needs_human"
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
                    "merge_command": merge_command(repo, "<number>", state.get("head_sha") or "<sha>"),
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
        builder.add_node(name, verify if name == "verify" else bind(name))
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
        "needs_human", lambda state: "implement" if state["path"][-1] == "review" else state["path"][-1],
        {"triage": "triage", "spec": "spec", "implement": "implement"},
    )
    builder.add_edge("research", "done")
    builder.add_conditional_edges("implement", after_implement, {"verify": "verify", "done": "done"})
    builder.add_conditional_edges(
        "review",
        after_review,
        {"merge": "merge", "implement": "implement", "needs_human": "needs_human", "done": "done"},
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

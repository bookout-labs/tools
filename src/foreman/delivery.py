"""The delivery graph. Edges live here. Models live in models.toml."""

from uuid import uuid4
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol
import os

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from pydantic import TypeAdapter

from foreman.config import Config, ModelSpec, load_config
from foreman.dry_run import DryRun, ScriptHandler
from foreman.dispatch import DISPATCH, DispatchResult, DispatchRun, Handler, InvalidOutput
from foreman.github import (
    MERGE_HOLD_VARIABLE, Forge, ForgeError, MergeHoldUnreadable, MergeNotArmed, HandoffChanged, QueueRejected, PullRequest, ReviewComment, merge_command,
)
from foreman.skill_rules import SkillMissing, SkillSource, export_skills, stage_methods
from foreman.stage_output import (
    PlanState, GateStage, DispatchPlan, DispatchPlanRecord, AttemptRecord, StageOutput, StoredSlice, parse_output, spec_digest,
)
from foreman.stages import STAGES, Acceptance, InvalidSlicePlan, reject_candidate
from foreman.steps import StageRun, implement, research, spec, triage
from foreman.state import ReviewState, ChecksState, RunState, SCHEMA_VERSION, checkpoint_serializer, DeliveryState, DeliveryUpdate, DeliveryResult
from foreman.readiness import MergeReceipt, MergeState
from foreman.outcomes import Halt, READINESS_HALTS, halted
from foreman.review_round import ReviewRound, history
from foreman.usd import BudgetUsd, usd_text
from foreman.worker import AttemptJournal
from foreman.workspace import WorkspaceError, CheckLog, ReviewScope, Workspace, WorktreeInspectionFailed, head_mismatch_detail

GRAPH_NAME = "delivery"
DEFAULT_TOKEN_CAP = 20_000_000
PACKAGE = Path(__file__).parent
MODEL_STAGES = tuple(name for name, stage in STAGES.items() if stage.kind == "model" and name != "spec_check")


@dataclass(frozen=True)
class LiveRun:
    forge: Forge
    workspace: Workspace
    check_log: CheckLog | None = None
    skills: SkillSource = export_skills
    human_login: str = field(default_factory=lambda: os.environ.get("FOREMAN_HUMAN_LOGIN", ""))
    journal: AttemptJournal | None = None


def run_tokens(attempts: list[AttemptRecord]) -> int:
    return sum(usage["input"] + usage["output"] + (usage["cache_read"] or 0) + (usage["cache_creation"] or 0)
               for attempt in attempts if (usage := attempt["usage"]) is not None)


_DELIVERY_STATE = TypeAdapter(DeliveryResult)


def review_count(state: DeliveryState | DeliveryResult) -> int:
    return sum(round.severity is not None for round in history({"rounds": state["review"].review_findings}).rounds)


def read_delivery_state(raw: object) -> DeliveryResult:
    return _DELIVERY_STATE.validate_python(raw, strict=True)


def stage_text(name: str) -> str:
    prompt = STAGES[name].prompt
    assert prompt is not None
    return (PACKAGE / "stages" / prompt).read_text().strip()


def load_slice(raw: object, plan: PlanState) -> DeliveryUpdate:
    stored = parse_output(StoredSlice, raw)
    return {"plan": plan.model_copy(update={"slice": stored.record(), "work_kind": stored.work_kind})}


def stage_update(
    name: str, output: StageOutput, state: DeliveryState, model: str | None = None, *, workspace: Workspace | None = None, accepted_model: ModelSpec | None = None, config: Config | None = None, attempt: AttemptRecord | None = None,
) -> DeliveryUpdate:
    update = STAGES[name].accept(output, Acceptance(state, accepted_model or model, workspace, config, attempt))
    update["path"] = [name]
    if model:
        used = state.get("models_used", {})
        update["models_used"] = {**used, name: model}
    return update


def review_submission(output: ReviewRound, scope: ReviewScope) -> tuple[str, list[ReviewComment]]:
    body = [f"Review route: {output.severity or 'none'}. Head: {output.reviewed_head}. Base ref: {output.base}."]
    provenance = output.provenance
    if provenance is not None:
        reason = provenance.reason.replace("_", " ") if provenance.reason else "not needed"
        body.append(f"Reviewer {provenance.reviewer}; implemented by {', '.join(provenance.contributors)}; same-family fallback: {reason}.")
    comments: list[ReviewComment] = []
    for finding in sorted(output.findings, key=lambda item: item.severity):
        location = finding.location
        path, line = location["path"], location["line"]
        label = f"Advisory, settled by {finding.ruling_id}" if finding.ruling_id else finding.severity
        message = f"[{label}] {finding.summary}\n\nImpact: {finding.impact}\n\nEvidence: {finding.evidence}"
        if location["side"] == "head" and line is not None and (path, line) in scope.inline:
            comments.append({"path": path, "line": line, "side": "RIGHT", "body": message})
        else:
            citation = f"{path}:{line}" if line is not None else path
            body.append(f"{location['side']} {citation} {message}")
    return "\n\n".join(body), comments


def stop_reason(state: DeliveryState) -> Halt | Literal["verified", "stopped", "cancelled"]:
    review_state = state["review"]
    checks = state["checks"]
    if halt := state.get("halt"):
        return halt
    if state.get("stop") == "cancelled":
        return "cancelled"
    if review_state.review == "changes":
        return Halt.review_cap
    if checks.verify == "pass":
        return "verified"
    return "stopped"


class DeliveryNode(Protocol):
    def __call__(self, state: DeliveryState, runtime: Runtime[LiveRun]) -> DeliveryUpdate: ...


def compile_delivery(
    dispatcher: Handler,
    config: Config,
    *,
    run: LiveRun,
    scratch: Path | None = None,
    checkpointer: BaseCheckpointSaver[str] | None = None,
) -> CompiledStateGraph[DeliveryState, LiveRun, DeliveryState, DeliveryResult]:
    missing = [name for name in MODEL_STAGES if name not in config.nodes]
    if missing:
        raise ValueError(f"delivery config is missing {', '.join(missing)}")
    methods_root = run.skills(config.pstack, config.matt_skills, scratch) if scratch is not None else None
    prompts = {name: stage_text(name) for name in (*MODEL_STAGES, "spec_check")}

    def with_run_id(state: DeliveryState) -> DeliveryState:
        review_state = state["review"]
        return {**state, "review": review_state.model_copy(update={"review_run_id": review_state.review_run_id or uuid4().hex})}

    def dispatch_run(name: GateStage, state: DeliveryState, owned: str = "") -> DispatchRun:
        def prepare_plan() -> DispatchPlanRecord:
            models = STAGES[name].models(name, state, config)
            suffix, target, scope = STAGES[name].prepare(state, run.workspace, run.forge)
            methods_text, methods, read_roots = stage_methods(name, state, methods_root) if methods_root is not None else ("", [], [])
            instruction = prompts[name]
            if methods_text:
                instruction += f"\n\nApply the methods listed below. Do not start subagents.\n\n{methods_text}"
            return DispatchPlan(models=[model.name for model in models], instruction=instruction + suffix,
                                methods=methods, read_roots=read_roots, target=target,
                                scope=scope.record() if scope else None).record()

        return DispatchRun(dispatcher, run.workspace, run.journal, config.pstack.commit, name == "implement", prepare_plan, owned)

    def apply(name: GateStage, state: DeliveryState, result: DispatchResult) -> DeliveryUpdate:
        # The nested graph owns the paid attempts. The stage applies a result once.
        attempts = result.get("attempts", [])
        stopped: DeliveryUpdate = {"path": [name], "attempts": attempts}
        if "halt" in result:
            return {**stopped, **halted(result["halt"], result.get("detail", ""), name)}
        assert "output" in result and "plan" in result
        plan = result["plan"]
        model = attempts[-1]["model"]
        state = {**state, "dispatch_plan": plan}
        try:
            contract = STAGES[name].output
            assert contract is not None
            _, citation_context = STAGES[name].project(state, run.workspace)
            output = parse_output(contract, result["output"], context=citation_context)
            update = stage_update(name, output, state, model, workspace=run.workspace,
                                  accepted_model=ModelSpec.parse(model) if model else None, config=config, attempt=attempts[-1])
        except InvalidSlicePlan as exc:
            revision = spec_digest([state["run"].target(), result["output"]])
            return {**stopped, **reject_candidate(state["plan"], revision, str(exc), config.max_review_rounds, exc.reason)}
        except InvalidOutput as exc:
            return {**stopped, **halted(Halt.invalid_output, f"{name} output from {model}: {exc}", name)}
        except WorkspaceError as exc:
            return {**stopped, **halted(Halt.git_failed, str(exc), name)}
        return {"review": state["review"], **update, "dispatch_plan": plan,
                "attempts": [*attempts[:-1], *update.get("attempts", attempts[-1:])]}

    stage = StageRun(
        workspace=run.workspace, forge=run.forge, check_log=run.check_log, max_rounds=config.max_review_rounds,
        human_login=run.human_login, dispatch=dispatch_run, apply=lambda name, state, result: apply(name, with_run_id(state), result))

    def post_review(state: DeliveryState) -> DeliveryUpdate:
        review_state = state["review"]
        assert "dispatch_plan" in state and review_state.review_run_id
        plan = state["dispatch_plan"]
        assert plan["target"] is not None and plan["scope"] is not None
        past = history({"rounds": review_state.review_findings, "settled": review_state.settled_rulings})
        report = past.rounds[-1]
        target = PullRequest.model_validate(plan["target"])
        if report.reviewed_head != state["checks"].verified_head or (
            report.reviewed_head, report.base
        ) != (target.headRefOid, target.baseRefOid):
            return halted(Halt.review_not_posted, "Review does not match the checked PR head and base.", "review_post")
        body, comments = review_submission(report, TypeAdapter(ReviewScope).validate_python(plan["scope"]))
        marker = f"{review_state.review_run_id}:{len(past.rounds)}:{report.base}:{report.reviewed_head}"
        try:
            receipt = run.forge.post_review(state["run"].repo, target, body, comments, marker=marker)
        except ForgeError as exc:
            return halted(Halt.review_not_posted, str(exc), "review_post")
        if receipt["state"] != "COMMENTED" or receipt["commit_id"] != report.reviewed_head:
            return halted(Halt.review_not_posted, "The review receipt does not match the submitted COMMENT.", "review_post")
        return {"review": review_state.model_copy(update={"review_receipt": receipt})}

    def review(state: DeliveryState) -> DeliveryUpdate:
        try:
            head = state["checks"].verified_head
            if not head:
                raise WorktreeInspectionFailed("The run did not record a verified HEAD.")
            worktree = run.workspace.inspect()
            if worktree.head != head:
                return {**halted(Halt.merge_not_armed, head_mismatch_detail(worktree.head, head), "review"),
                        "merge": MergeState(status="not_armed")}
            if not worktree.clean or worktree.branch is None:
                raise WorktreeInspectionFailed("The worktree changed after verification.")
        except WorktreeInspectionFailed as exc:
            return halted(Halt.git_failed, str(exc), "review")

        return inspect(with_run_id(state))

    def inspect(state: DeliveryState) -> DeliveryUpdate:
        # #237 makes this call the review stage's own inspect step.
        def settle(current: DeliveryState, answer: str) -> DeliveryUpdate:
            # The human's ruling on a disputed scope or rule settles it for the fresh review of this head.
            review_state = current["review"]
            past = history({"rounds": review_state.review_findings, "settled": review_state.settled_rulings})
            rulings = [ruling.model_dump() for ruling in past.settled] + [{"id": f"design:{len(past.rounds)}", "text": answer}]
            return {"review": review_state.model_copy(update={"settled_rulings": rulings})}

        update, _ = stage.consult(DISPATCH, "review", state, None, answered=settle)
        return update

    def outcome(state: DeliveryState, success: str) -> str:
        """The typed result a stage carries to the top router."""
        if state.get("halt") or state.get("stop") == "blocked":
            return "halted"
        return "cancelled" if state.get("stop") == "cancelled" else success

    def after_review(state: DeliveryState) -> str:
        review_state = state["review"]
        if state.get("halt"):
            return "done"
        if review_state.review == "changes" and review_count(state) >= config.max_review_rounds or state.get("stop") == "cancelled":
            return "done"
        return "implement" if review_state.review == "changes" else "merge"

    def arm_merge(state: DeliveryState) -> DeliveryUpdate:
        review_state = state["review"]
        run_state = state["run"]
        repo = run_state.repo
        head = state["checks"].verified_head
        merge = MergeState(
            status="not_armed", receipt=state.get("merge", MergeState()).receipt,
            command=merge_command(repo, "<number>", head or "<sha>"),
        )

        def merge_halted(halt: Halt, detail: str) -> DeliveryUpdate:
            return {
                **halted(halt, detail, "merge"),
                "merge": merge,
            }

        forge, workspace = run.forge, run.workspace
        if not head:
            return merge_halted(Halt.merge_not_armed, "The run did not record a verified HEAD.")
        try:
            worktree = workspace.inspect()
        except WorktreeInspectionFailed as exc:
            return merge_halted(Halt.merge_not_armed, str(exc))
        if not worktree.clean:
            return merge_halted(Halt.merge_not_armed, "The worktree has changes or Git could not inspect it.")
        if worktree.head != head:
            merge = merge.model_copy(update={"observed_head": worktree.head})
            return merge_halted(Halt.merge_not_armed, head_mismatch_detail(worktree.head, head))
        if worktree.branch is None:
            return merge_halted(Halt.merge_not_armed, "HEAD is detached. Merge requires a checked-out branch.")
        try:
            hold = forge.merge_hold(repo)
        except MergeHoldUnreadable as exc:
            return merge_halted(Halt.merge_held, f"could not read {MERGE_HOLD_VARIABLE}: {exc}")
        if hold:
            return merge_halted(Halt.merge_held, f"{MERGE_HOLD_VARIABLE} is set" + (f": {hold}" if hold.strip() else "."))
        issue = run_state.issue
        if issue is None:
            return merge_halted(Halt.merge_not_armed, "Live merge requires an issue number.")
        receipt = review_state.review_receipt
        rounds = history({"rounds": review_state.review_findings}).rounds
        report = rounds[-1] if rounds else None
        plan = state.get("dispatch_plan")
        target = plan["target"] if plan else None
        if (report is None or target is None or report.severity or state.get("human_question")
                or report.reviewed_head != head or target["headRefOid"] != head or report.base != target["baseRefOid"]
                or not receipt or receipt["state"] != "COMMENTED" or receipt["commit_id"] != head):
            return merge_halted(Halt.merge_not_armed, "Renew review and its posted receipt at the verified head.")
        if previous := merge.receipt:
            saved = MergeReceipt.model_validate(previous)
            if saved.source_head == head:
                if (saved.repo, saved.pr, saved.base, saved.base_ref) != (repo, target["number"], report.base, target["baseRefName"]):
                    merge = merge.model_copy(update={"receipt": None})
                    return merge_halted(Halt.merge_not_armed, "The saved handoff does not match the reviewed pull request and base.")
                try:
                    confirmed_receipt = forge.reconcile_merge(saved)
                except HandoffChanged as exc:
                    merge = merge.model_copy(update={"receipt": None})
                    return merge_halted(Halt.merge_not_armed, str(exc))
                except QueueRejected as exc:
                    return merge_halted(READINESS_HALTS[exc.outcome], str(exc))
                except MergeNotArmed as exc:
                    return merge_halted(Halt.merge_not_armed, str(exc))
                return {"merge": merge.model_copy(update={"status": "armed", "receipt": confirmed_receipt.model_dump(mode="json")}), "path": ["merge"]}
        try:
            observation = forge.readiness(repo, issue, worktree.branch, head)
        except ForgeError as exc:
            return merge_halted(Halt.readiness_unavailable, f"Platform/policy observation unavailable: {exc}")
        if observation.outcome != "ready":
            merge = merge.model_copy(update={"observation": observation.model_dump(mode="json")})
            return merge_halted(READINESS_HALTS[observation.outcome], observation.detail())
        if (observation.pr.number, observation.pr.head.sha, observation.pr.base.sha, observation.pr.base.ref) != (
            target["number"], head, report.base, target["baseRefName"],
        ):
            return merge_halted(Halt.merge_not_armed, "The pull request head, base or identity changed after review.")
        try:
            receipt = forge.arm_auto_merge(repo, issue, worktree.branch, head, observation)
            if receipt is None:
                return {"merge": merge.model_copy(update={"status": "skipped", "command": merge_command(repo, "<number>", head)}), "path": ["merge"]}
            merge_receipt = receipt.model_dump(mode="json")
        except QueueRejected as exc:
            return merge_halted(READINESS_HALTS[exc.outcome], str(exc))
        except MergeNotArmed as exc:
            return merge_halted(Halt.merge_not_armed, str(exc))
        if merge_receipt["status"] == "MERGED":
            return {"merge": merge.model_copy(update={"status": "armed", "receipt": merge_receipt}), "path": ["merge"]}
        try:
            confirmed = forge.readiness(repo, issue, worktree.branch, head)
        except ForgeError as exc:
            return {**merge_halted(Halt.readiness_unavailable, str(exc)), "merge": merge.model_copy(update={"status": "armed", "receipt": merge_receipt})}
        if (confirmed.pr.number, confirmed.pr.head.sha, confirmed.pr.base.sha, confirmed.pr.base.ref) != (
            observation.pr.number, observation.pr.head.sha, observation.pr.base.sha, observation.pr.base.ref,
        ):
            merge = merge.model_copy(update={"observation": confirmed.model_dump(mode="json"), "receipt": None})
            return merge_halted(Halt.merge_not_armed, "The pull request head, base or identity changed after arming.")
        update: DeliveryUpdate = {"merge": merge.model_copy(update={
            "status": "armed", "receipt": merge_receipt, "observation": confirmed.model_dump(mode="json"),
        }), "path": ["merge"]}
        if confirmed.outcome != "ready":
            update.update(halted(READINESS_HALTS[confirmed.outcome], confirmed.detail(), "merge"))
        elif confirmed.pr.state != observation.pr.state:
            update.update(halted(Halt.merge_not_armed, confirmed.detail(), "merge"))
        return update

    def finish(state: DeliveryState) -> DeliveryUpdate:
        reason = stop_reason(state)
        if not state.get("halt"):
            if reason == Halt.review_cap:
                detail = f"Review requested changes after {review_count(state)} rounds. Last review: {state['review'].detail}"
                return {**halted(Halt.review_cap, detail, "done"), "stop": str(reason)}
        return {"stop": str(reason), "path": ["done"]}

    def after_implement(state: DeliveryState) -> str:
        if state.get("halt"):
            return "halted"
        stop = state.get("stop")
        return stop if stop in {"cancelled", "no_change"} else "published"

    builder = StateGraph[DeliveryState, LiveRun, DeliveryState, DeliveryResult](
        DeliveryState, input_schema=DeliveryState, output_schema=DeliveryResult, context_schema=LiveRun,
    )

    def after_halt(target: str) -> Callable[[DeliveryState], str]:
        def route(state: DeliveryState) -> str:
            return "done" if state.get("halt") or state.get("stop") == "cancelled" else target
        return route

    builder.add_node("triage", triage.compile_stage(stage))
    builder.add_node("research", research.compile_stage(stage))
    builder.add_node("spec", spec.compile_stage(stage))
    builder.add_node("implement", implement.compile_stage(stage))
    builder.add_node("review", review)
    builder.add_node("review_post", post_review)
    builder.add_node("merge", arm_merge)
    builder.add_node("done", finish)
    builder.add_edge(START, "triage")
    builder.add_conditional_edges("triage", lambda state: outcome(state, state["plan"].triage),
        {"question": "research", "plan": "spec", "supplied_slice": "spec", "cancelled": END, "halted": END})
    builder.add_conditional_edges("research", lambda state: outcome(state, "answered"),
        {"answered": END, "cancelled": END, "halted": END})
    builder.add_conditional_edges("spec", lambda state: outcome(state, state.get("stop") or "validated_single"),
        {"validated_single": "implement", "children_filed": END, "cancelled": END, "halted": END})
    builder.add_conditional_edges("implement", after_implement,
        {"published": "review", "no_change": END, "cancelled": END, "halted": END})
    builder.add_conditional_edges(
        "review",
        after_halt("review_post"),
        {"review_post": "review_post", "merge": "merge", "implement": "implement", "done": "done"},
    )
    builder.add_conditional_edges("review_post", after_review,
        {"merge": "merge", "implement": "implement", "done": "done"})
    builder.add_edge("merge", "done")
    builder.add_edge("done", END)
    # Each round visits at most all stages, each with one setup and every configured candidate,
    # plus the implement steps, prompts, merge and done.
    steps = sum(1 + max(1, len(config.nodes[name].models)) for name in (*MODEL_STAGES, "spec")) + 12
    return builder.compile(checkpointer=checkpointer).with_config({"recursion_limit": steps * (config.max_review_rounds + 1)})


def run_delivery(
    dispatcher: Handler,
    *,
    repo: str,
    request: str,
    check: str = "",
    config: Config | None = None,
    slice: object = None,
    scratch: Path | None = None,
    max_tokens: int = DEFAULT_TOKEN_CAP,
    max_budget_usd: BudgetUsd | None = None,
    skills: SkillSource = export_skills,
    commands: list[list[str]] | None = None,
) -> DeliveryResult:
    dry = DryRun(commands=commands if commands is not None else [])
    state: DeliveryState = {
        "schema_version": SCHEMA_VERSION,
        "run": RunState(issue=dry.issue_placeholder, repo=repo, request=request, token_cap=max_tokens,
                        base_branch=dry.default_branch(repo), base_sha=dry.pin_base(dry.default_branch(repo)),
                        max_budget_usd=usd_text(max_budget_usd) if max_budget_usd is not None else None),
        "checks": ChecksState(check=check),
        "plan": PlanState(issue_kind="parent" if slice is None else "slice"),
        "models_used": {},
        "path": [],
        "review": ReviewState(),
    }
    if slice is not None:
        state.update(load_slice(slice, state["plan"]))
    config = config if config is not None else load_config()
    if scratch is not None:
        state["pstack_pin"] = config.pstack.model_dump()
        state["matt_skills_pin"] = config.matt_skills.model_dump()
    try:
        saver = InMemorySaver(serde=checkpoint_serializer())
        graph = compile_delivery(dispatcher, config, run=LiveRun(forge=dry, workspace=dry, skills=skills), scratch=scratch, checkpointer=saver)
    except SkillMissing as exc:
        return read_delivery_state({**state, **halted(Halt.skill_missing, str(exc), "done"), "stop": exc.halt,
                                    "skill_reason": exc.reason})
    from foreman.runner import snapshot_result

    thread_id = uuid4().hex
    graph.invoke(state, {"configurable": {"thread_id": thread_id}}, version="v2", durability="sync")
    return snapshot_result(graph.get_state({"configurable": {"thread_id": thread_id}}, subgraphs=True))


def diagram() -> str:
    dry = DryRun()
    graph = compile_delivery(ScriptHandler(), load_config(), run=LiveRun(forge=dry, workspace=dry))
    return graph.get_graph().draw_mermaid()

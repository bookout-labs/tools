"""The delivery graph. Edges live here. Models live in models.toml."""

from uuid import uuid4
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from pydantic import TypeAdapter

from foreman.config import Config, ModelSpec, load_config
from foreman.dry_run import DryRun, ScriptHandler
from foreman.dispatch import DispatchResult, DispatchRun, Handler, InvalidOutput
from foreman.github import Forge
from foreman.skill_rules import SkillMissing, SkillSource, export_skills, stage_methods
from foreman.stage_output import (
    PlanState, DispatchStage, DispatchPlan, DispatchPlanRecord, AttemptRecord, StageOutput, StoredSlice, parse_output, spec_digest,
)
from foreman.stages import STAGES, Acceptance, InvalidSlicePlan, reject_candidate
from foreman.steps import StageRun, implement, merge, research, review, spec, triage, verify
from foreman.state import ReviewState, ChecksState, RunState, SCHEMA_VERSION, checkpoint_serializer, DeliveryState, DeliveryUpdate, DeliveryResult
from foreman.outcomes import Halt, halted
from foreman.usd import BudgetUsd, usd_text
from foreman.worker import AttemptJournal
from foreman.workspace import WorkspaceError, CheckLog, Workspace

GRAPH_NAME = "delivery"
DEFAULT_TOKEN_CAP = 20_000_000
PACKAGE = Path(__file__).parent
MODEL_STAGES = tuple(name for name, stage in STAGES.items() if stage.kind == "model" and name != "spec_check")


# GitHub's reserved login for deleted accounts never posts, so a dry run or smoke fixture reads no human comments.
FIXTURE_HUMAN = "ghost"


@dataclass(frozen=True)
class LiveRun:
    forge: Forge
    workspace: Workspace
    check_log: CheckLog | None = None
    skills: SkillSource = export_skills
    human_login: str = ""
    journal: AttemptJournal | None = None


def run_tokens(attempts: list[AttemptRecord]) -> int:
    return sum(usage["input"] + usage["output"] + (usage["cache_read"] or 0) + (usage["cache_creation"] or 0)
               for attempt in attempts if (usage := attempt["usage"]) is not None)


_DELIVERY_STATE = TypeAdapter(DeliveryResult)


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

    def dispatch_run(name: DispatchStage, state: DeliveryState, owned: str = "") -> DispatchRun:
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

    def apply(name: DispatchStage, state: DeliveryState, result: DispatchResult) -> DeliveryUpdate:
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

    def outcome(state: DeliveryState, success: str) -> str:
        """The typed result a stage carries to the top router."""
        if state.get("halt") or state.get("stop") == "blocked":
            return "halted"
        return "cancelled" if state.get("stop") == "cancelled" else success

    def after_implement(state: DeliveryState) -> str:
        if state.get("halt"):
            return "halted"
        stop = state.get("stop")
        return stop if stop in {"cancelled", "no_change"} else "published"

    builder = StateGraph[DeliveryState, LiveRun, DeliveryState, DeliveryResult](
        DeliveryState, input_schema=DeliveryState, output_schema=DeliveryResult, context_schema=LiveRun,
    )

    builder.add_node("triage", triage.compile_stage(stage))
    builder.add_node("research", research.compile_stage(stage))
    builder.add_node("spec", spec.compile_stage(stage))
    builder.add_node("implement", implement.compile_stage(stage))
    builder.add_node("verify", verify.compile_stage(stage))
    builder.add_node("review", review.compile_stage(stage))
    builder.add_node("merge", merge.compile_stage(stage))
    builder.add_edge(START, "triage")
    builder.add_conditional_edges("triage", lambda state: outcome(state, state["plan"].triage),
        {"question": "research", "plan": "spec", "supplied_slice": "spec", "cancelled": END, "halted": END})
    builder.add_conditional_edges("research", lambda state: outcome(state, "answered"),
        {"answered": END, "cancelled": END, "halted": END})
    builder.add_conditional_edges("spec", lambda state: outcome(state, state.get("stop") or "validated_single"),
        {"validated_single": "implement", "children_filed": END, "cancelled": END, "halted": END})
    builder.add_conditional_edges("implement", after_implement,
        {"published": "verify", "no_change": END, "cancelled": END, "halted": END})
    builder.add_conditional_edges("verify", lambda state: outcome(state, state.get("stop", "")),
        {"posted": "review", "no_skill": "review", "head_moved": "implement", "halted": END})
    builder.add_conditional_edges("review", lambda state: outcome(state, state.get("stop", "")),
        {"P0": "implement", "P1": "implement", "head_moved": "implement", "none": "merge", "cancelled": END, "halted": END})
    builder.add_conditional_edges("merge", lambda state: outcome(state, state.get("stop", "")),
        {"merged": END, "closed": END, "reviewable_head": "review", "repair": "implement", "cancelled": END, "halted": END})
    # Each round visits at most all stages, each with one setup and every configured candidate,
    # plus the implement steps, prompts and merge.
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
    checkout: Path | None = None,
) -> DeliveryResult:
    """Walk the graph offline. `checkout` is a target checkout whose tracked skills the stages read; without one there are none."""
    dry = DryRun(commands=commands if commands is not None else [])
    state: DeliveryState = {
        "schema_version": SCHEMA_VERSION,
        "run": RunState(issue=dry.issue_placeholder, repo=repo, request=request, token_cap=max_tokens, workspace=str(checkout or ""),
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
        graph = compile_delivery(dispatcher, config, run=LiveRun(forge=dry, workspace=dry, skills=skills, human_login=FIXTURE_HUMAN),
                                 scratch=scratch, checkpointer=saver)
    except SkillMissing as exc:
        return read_delivery_state({**state, **halted(Halt.skill_missing, str(exc), "admit"), "stop": exc.halt,
                                    "skill_reason": exc.reason})
    from foreman.runner import snapshot_result

    thread_id = uuid4().hex
    graph.invoke(state, {"configurable": {"thread_id": thread_id}}, version="v2", durability="sync")
    return snapshot_result(graph.get_state({"configurable": {"thread_id": thread_id}}, subgraphs=True))


def diagram() -> str:
    dry = DryRun()
    graph = compile_delivery(ScriptHandler(), load_config(), run=LiveRun(forge=dry, workspace=dry))
    return graph.get_graph().draw_mermaid()

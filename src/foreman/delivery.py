"""The delivery graph. Edges live here. Models live in models.toml."""

from uuid import uuid4
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from pydantic import TypeAdapter

from foreman.config import Config, ModelSpec, load_config
from foreman.contract import parse_contract
from foreman.dry_run import DRY_CONTRACT, DryRun, ScriptHandler
from foreman.dispatch import DispatchResult, DispatchRun, Handler
from foreman.report import Rewrite, no_rewrite
from foreman.doctor import DEFAULT_BUDGET_USD, DEFAULT_TOKEN_CAP, GreenChecks
from foreman.github import Forge
from foreman.skill_rules import SkillMissing, SkillSource, export_skills, stage_methods
from foreman.stage_output import (
    PlanState, DispatchStage, DispatchPlan, DispatchPlanRecord, AttemptRecord, StageOutput, StoredSlice, parse_output, spec_digest,
)
from foreman.stages import STAGES, Acceptance, InvalidSlicePlan, reject_candidate
from foreman.steps import Reverts, StageRun, implement, merge, research, review, spec, triage, verify, no_reverts
from foreman.state import (BuilderSession, ReviewState, ChecksState, RunState, StartSettings, SCHEMA_VERSION, checkpoint_serializer,
                           DeliveryGraphState, DeliveryState, DeliveryUpdate, DeliveryResult)
from foreman.outcomes import Halt, halted
from foreman.usd import BudgetUsd, usd_text
from foreman.worker import AttemptJournal
from foreman.workspace import WorkspaceError, CheckLog, Workspace

GRAPH_NAME = "delivery"
PACKAGE = Path(__file__).parent
# The checker and the acceptance test writer take their models from the spec and verify policies.
MODEL_STAGES = tuple(name for name, stage in STAGES.items() if stage.kind == "model" and name not in ("spec_check", "implement_tests"))


# GitHub's reserved login for deleted accounts never posts, so a dry run or smoke fixture reads no human comments.
FIXTURE_HUMAN = "ghost"


@dataclass(frozen=True)
class LiveRun:
    forge: Forge
    workspace: Workspace
    check_log: CheckLog | None = None
    skills: SkillSource = export_skills
    journal: AttemptJournal | None = None
    rewrite: Rewrite = no_rewrite
    # The login and caps every reader inside the graph uses. The runner fills them at every invoke from the start it read, or
    # from the saved start on a wake or an answer. A graph that is only read needs none.
    start: StartSettings | None = None
    # Each green full check doctor recorded. The runner gives the host's run store, and a run outside it keeps them in memory.
    greens: GreenChecks = field(default_factory=GreenChecks.in_memory)
    # The reverts that the host's saved runs recorded, which admission reads. A run outside the run store sees none.
    reverts: Reverts = no_reverts


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


def load_slice(raw: object, plan: PlanState) -> PlanState:
    stored = parse_output(StoredSlice, raw)
    return plan.model_copy(update={"slice": stored.record(), "work_kind": stored.work_kind})


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
) -> CompiledStateGraph[DeliveryState, LiveRun, DeliveryGraphState, DeliveryResult]:
    missing = [name for name in MODEL_STAGES if name not in config.nodes]
    if missing:
        raise ValueError(f"delivery config is missing {', '.join(missing)}")
    methods_root = run.skills(config.pstack, config.matt_skills, scratch) if scratch is not None else None
    prompts = {name: stage_text(name) for name in (*MODEL_STAGES, "spec_check", "implement_tests")}

    def with_run_id(state: DeliveryState) -> DeliveryState:
        review_state = state["review"]
        return {**state, "review": review_state.model_copy(update={"review_run_id": review_state.review_run_id or uuid4().hex})}

    def dispatch_run(name: DispatchStage, state: DeliveryState, owned: str = "") -> DispatchRun:
        def prepare_plan() -> DispatchPlanRecord:
            models = STAGES[name].models(name, state, config)
            suffix, target, scope = STAGES[name].prepare(state, run.workspace, run.forge)
            methods_text, methods, read_roots = stage_methods(name, state, methods_root, run.workspace) if methods_root is not None else ("", [], [])
            # Every worker reads the related checkouts and none writes them.
            read_roots = [*read_roots, *(related.checkout for related in state.get("related", []))]
            instruction = prompts[name]
            if methods_text:
                instruction += f"\n\nApply the methods listed below. Do not start subagents.\n\n{methods_text}"
            return DispatchPlan(models=[model.name for model in models], instruction=instruction + suffix,
                                methods=methods, read_roots=read_roots, target=target,
                                scope=scope.record() if scope else None).record()

        def validate(plan: DispatchPlanRecord, model: str, output: dict[str, object]) -> None:
            try:
                answer(name, {**state, "dispatch_plan": plan}, model, output)
            except InvalidSlicePlan:
                pass  # A rejected candidate is a plan revision that spec counts, not an invalid answer to repair.

        assert run.start is not None
        return DispatchRun(dispatcher, run.workspace, run.journal, config.pstack.commit, name == "implement", prepare_plan, validate,
                           run.start, owned)

    def answer(name: DispatchStage, state: DeliveryState, model: str | None, output: dict[str, object],
               attempt: AttemptRecord | None = None) -> DeliveryUpdate:
        """The stage's update for an answer, after its schema and the stage's own checks of it."""
        contract = STAGES[name].output
        assert contract is not None
        _, citation_context = STAGES[name].project(state, run.workspace)
        parsed = parse_output(contract, output, context=citation_context)
        return stage_update(name, parsed, state, model, workspace=run.workspace,
                            accepted_model=ModelSpec.parse(model) if model else None, config=config, attempt=attempt)

    def apply(name: DispatchStage, state: DeliveryState, result: DispatchResult) -> DeliveryUpdate:
        # The nested graph owns the paid attempts and has already checked the answer. The stage applies a result once.
        attempts = result.get("attempts", [])
        stopped: DeliveryUpdate = {"path": [name], "attempts": attempts}
        if "halt" in result:
            update: DeliveryUpdate = {**stopped, **halted(result["halt"], result.get("detail", ""), name)}
            if held := next(iter(result.get("sessions", {}).items()), None):
                # The builder keeps a conversation its quota-stopped calls left on this tree, for the call Ready makes (D5).
                update["builder_session"] = BuilderSession(model=held[0], session=held[1], fingerprint=result.get("fingerprint", ""))
            elif "session" in result:
                # A charged cut-off builder call drops the builder's conversation, so the call Ready makes starts fresh.
                update["builder_session"] = None
            return update
        assert "output" in result and "plan" in result
        plan = result["plan"]
        model = attempts[-1]["model"]
        state = {**state, "dispatch_plan": plan}
        try:
            update = answer(name, state, model, result["output"], attempts[-1])
        except InvalidSlicePlan as exc:
            revision = spec_digest([state["run"].target(), result["output"]])
            return {**stopped, **reject_candidate(state["plan"], revision, str(exc), config.max_review_rounds, exc.reason)}
        except WorkspaceError as exc:
            return {**stopped, **halted(Halt.git_failed, str(exc), name)}
        if "session" in result:
            # The builder keeps the conversation its latest accepted answer returned, bound to that checkout (D5).
            session = result["session"]
            update["builder_session"] = session and BuilderSession(model=model or "", session=session, fingerprint=result.get("fingerprint", ""))
        return {"review": state["review"], **update, "dispatch_plan": plan,
                "attempts": [*attempts[:-1], *update.get("attempts", attempts[-1:])]}

    stage = StageRun(
        workspace=run.workspace, forge=run.forge, check_log=run.check_log, max_rounds=config.max_review_rounds,
        dispatch=dispatch_run, apply=lambda name, state, result: apply(name, with_run_id(state), result), rewrite=run.rewrite,
        start=run.start, greens=run.greens, reverts=run.reverts)

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

    # The stages hold the state without the start, which only the delivery graph's own channel holds.
    builder = StateGraph[DeliveryState, LiveRun, DeliveryGraphState, DeliveryResult](
        DeliveryGraphState, input_schema=DeliveryGraphState, output_schema=DeliveryResult, context_schema=LiveRun,
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
    builder.add_conditional_edges("spec", lambda state: outcome(
        state, state.get("stop") or ("validated_batch" if state["plan"].batch_issues else "validated_single")),
        {"validated_single": "implement", "validated_batch": "implement", "children_filed": END, "cancelled": END, "halted": END})
    builder.add_conditional_edges("implement", after_implement,
        {"published": "verify", "no_change": END, "cancelled": END, "halted": END})
    builder.add_conditional_edges("verify", lambda state: outcome(state, state.get("stop", "")),
        {"posted": "review", "failed": "implement", "head_moved": "implement", "cancelled": END, "halted": END})
    builder.add_conditional_edges("review", lambda state: outcome(state, state.get("stop", "")),
        {"P0": "implement", "P1": "implement", "head_moved": "implement", "none": "merge", "cancelled": END, "halted": END})
    builder.add_conditional_edges("merge", lambda state: outcome(state, state.get("stop", "")),
        {"merged": END, "reverted": END, "closed": END, "reviewable_head": "review", "repair": "implement", "cancelled": END, "halted": END})
    # Each round visits at most all stages, each with one setup and every configured candidate, the checker and the
    # acceptance test writer by their spec and verify policies, plus the implement steps, prompts and merge.
    steps = sum(1 + max(1, len(config.nodes[name].models)) for name in (*MODEL_STAGES, "spec", "verify")) + 12
    return builder.compile(checkpointer=checkpointer).with_config({"recursion_limit": steps * (config.max_review_rounds + 1)})


def run_delivery(
    dispatcher: Handler,
    *,
    repo: str,
    request: str,
    contract: bytes = DRY_CONTRACT,
    config: Config | None = None,
    slice: object = None,
    scratch: Path | None = None,
    max_tokens: int = DEFAULT_TOKEN_CAP,
    max_budget_usd: BudgetUsd = DEFAULT_BUDGET_USD,
    skills: SkillSource = export_skills,
    commands: list[list[str]] | None = None,
    checkout: Path | None = None,
) -> DeliveryResult:
    """Walk the graph offline with a contract fixture. `checkout` is a target checkout whose tracked skills the stages read; without one there are none."""
    dry = DryRun(commands=commands if commands is not None else [], checkout=checkout)
    terms = parse_contract(contract)
    state: DeliveryGraphState = {
        "schema_version": SCHEMA_VERSION,
        # A live run's start saves the login and caps it read, so the dry run carries them too.
        "start": StartSettings(human_login=FIXTURE_HUMAN, max_tokens=max_tokens, max_budget_usd=usd_text(max_budget_usd)),
        "run": RunState(issue=dry.issue_placeholder, repo=repo, request=request, workspace=str(checkout or ""),
                        base_branch=dry.default_branch(repo), base_sha=dry.pin_base(dry.default_branch(repo))),
        "checks": ChecksState(check=terms.check, verify_skill=terms.verify_skill, test_report=terms.reports,
                              mutation=terms.mutation.command, sample_cap=terms.mutation.sample_cap),
        "related": [dry.check_out_related(item.repository, dry.default_branch(item.repository)) for item in terms.related_repositories],
        "plan": PlanState(issue_kind="parent" if slice is None else "slice"),
        "models_used": {},
        "path": [],
        "review": ReviewState(),
    }
    if slice is not None:
        state["plan"] = load_slice(slice, state["plan"])
    config = config if config is not None else load_config()
    if scratch is not None:
        state["pstack_pin"] = config.pstack.model_dump()
        state["matt_skills_pin"] = config.matt_skills.model_dump()
    try:
        saver = InMemorySaver(serde=checkpoint_serializer())
        dispatcher = dry.builder(dispatcher)
        graph = compile_delivery(dispatcher, config, run=LiveRun(forge=dry, workspace=dry, skills=skills, start=state["start"]),
                                 scratch=scratch, checkpointer=saver)
    except SkillMissing as exc:
        return read_delivery_state({**state, **halted(Halt.skill_missing, str(exc), "admit"), "stop": exc.halt,
                                    "skill_reason": exc.reason})
    from foreman.runner import snapshot_result

    thread_id = uuid4().hex
    graph.invoke(state, {"configurable": {"thread_id": thread_id}}, version="v2", durability="sync")
    return snapshot_result(graph.get_state({"configurable": {"thread_id": thread_id}}, subgraphs=True))


def shape(checkpointer: BaseCheckpointSaver[str] | None = None) -> CompiledStateGraph[DeliveryState, LiveRun, DeliveryGraphState, DeliveryResult]:
    """The delivery graph with no forge or worker behind it, to draw it or read the stage of a saved run. It calls nothing."""
    dry = DryRun()
    return compile_delivery(ScriptHandler(), load_config(), run=LiveRun(forge=dry, workspace=dry), checkpointer=checkpointer)


def diagram() -> str:
    return shape().get_graph().draw_mermaid()

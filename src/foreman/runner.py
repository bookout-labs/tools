"""Run one delivery issue against its SQLite checkpoint."""

from collections.abc import Callable, Generator
from contextlib import closing, contextmanager
from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path
import fcntl
from typing import Any
import sqlite3
from tempfile import TemporaryDirectory

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, StateSnapshot
from pydantic import TypeAdapter

from foreman._raw import is_dict
from foreman.config import SkillPin, load_config
from foreman.outcomes import HALTS, Halt, RESUMABLE_HALTS
from foreman.delivery import DEFAULT_TOKEN_CAP, LiveRun, compile_delivery, halted, read_delivery_state
from foreman.readiness import MergeState, MergeWait
from foreman.state import ReviewState, PlanState, ChecksState, RunState, SCHEMA_VERSION, DeliveryResult, DeliveryState, DeliveryUpdate, checkpoint_serializer, require_schema_version
from foreman.dispatch import Handler, QuotaPause
from foreman.skill_rules import SkillMissing
from foreman.github import ForgeError, human_comments
from foreman.watch import run_key, watch
from foreman.stage_output import HumanPrompt, SpecCandidate, spec_digest, parse_output
from foreman.steps import implement
from foreman.usd import BudgetUsd, usd_text
from foreman.workspace import WorkState, WorkspaceError
from foreman.worker import AttemptJournal, worker_lock

_CHECKPOINT_STATE = TypeAdapter(DeliveryState)
_DELIVERY_UPDATE = TypeAdapter(DeliveryUpdate)
# A paused run waits for worker capacity or for GitHub. A later run resumes the same step, or restarts a waiting merge stage.
PAUSES = frozenset({"quota_exhausted", "merge_pending"})
STAGE_PROGRESS = ("research", "implementation", "models_used", "implement_models", "human_answers", "dispatch_plan", "pr_size")
# Each step a stage records in the path, so a halt inside a stage resumes that stage.
STEP_STAGES = {"admit": "triage", "triage": "triage", "research": "research", "post": "research",
               "spec": "spec", "spec_check": "spec", "file_children": "spec", "review": "review", "merge": "merge",
               **dict.fromkeys(implement.STEPS, "implement")}


@dataclass
class RunPresentation:
    """Invocation-local console data, kept outside graph state and return values."""

    restored: DeliveryResult | None = None
    updates: list[DeliveryUpdate] = field(default_factory=list[DeliveryUpdate])
    commands: list[list[str]] = field(default_factory=list[list[str]])

    def restore(self, snapshot: StateSnapshot) -> None:
        self.restored = snapshot_result(snapshot)


def paused_values(snapshot: StateSnapshot) -> dict[str, Any]:
    """The run's values, including what a stage paused at its gate has reached."""
    values: dict[str, Any] = snapshot.values
    for task in snapshot.tasks:
        if task.interrupts and isinstance(nested := task.state, StateSnapshot):
            values = {**values, **nested.values, "path": [*values["path"], *nested.values.get("path", [])]}
    return values


def snapshot_result(snapshot: StateSnapshot) -> DeliveryResult:
    values = paused_values(snapshot)
    if snapshot.interrupts:
        pause: object = snapshot.interrupts[0].value
        if is_dict(pause) and pause.get("reason") == "quota_exhausted":
            quota = parse_output(QuotaPause, pause)
            return read_delivery_state({**values, "stop": quota.reason, "halt_detail": quota.detail,
                                        "path": [*values["path"], quota.stage],
                                        "attempts": quota.attempts})
        if is_dict(pause) and pause.get("reason") == "merge_pending":
            pending = MergeWait.model_validate(pause)
            return read_delivery_state({**values, "stop": pending.reason, "halt_detail": pending.detail,
                                        "path": [*values["path"], "merge"]})
        prompt = parse_output(HumanPrompt, pause)
        question = prompt.question
        if prompt.revision:
            question += f"\n\nReply on the issue under the configured GitHub identity. Start with {prompt.answer_marker} and put the answer on the next line."
        return read_delivery_state({**values, "stop": "needs_human", "halt_detail": question,
                                    "models_used": {**values.get("models_used", {}), **prompt.models_used},
                                    "path": [*values["path"], *prompt.path],
                                    "attempts": [*values.get("attempts", []), *prompt.attempts]})
    return read_delivery_state({**values, "stop": values.get("stop", "")})


def checkpoint_state(values: dict[str, object]) -> DeliveryState | None:
    if not values:
        return None
    require_schema_version(values)
    state = _CHECKPOINT_STATE.validate_python(values, strict=True)
    plan = state["plan"]
    if plan.spec_candidate is not None:
        candidate = SpecCandidate.from_record(plan.spec_candidate)
        if state["run"].base_sha and candidate.target != state["run"].target():
            raise ValueError("The saved candidate targets a different repository, issue or base.")
        expected = candidate.update(plan, single=plan.slice is not None)
        for key in ("slice", "slices", "spec", "work_kind"):
            if getattr(plan, key) != getattr(expected, key):
                raise ValueError(f"{key} differs from candidate")
    return state


class AnswerError(RuntimeError):
    """An answer cannot be applied to this checkpoint's question."""


def _checkpoint_before_halt(
    graph: CompiledStateGraph[DeliveryState, LiveRun, DeliveryState, DeliveryResult],
    config: RunnableConfig,
    path: list[str],
    node: str | None = None,
) -> StateSnapshot:
    if not path:
        raise ValueError("Cannot resume a run without a halted node.")
    node = node or path[-1]
    before = next((snapshot for snapshot in graph.get_state_history(config) if snapshot.next == (node,)), None)
    if before is None:
        raise ValueError(f"Cannot resume a run without a checkpoint before {node}.")
    return before


def require_same_budget(stored: str | None, max_budget_usd: BudgetUsd | None) -> None:
    if max_budget_usd is not None and (stored is None or Decimal(stored) != max_budget_usd.amount):
        raise ValueError("--max-budget-usd differs from the stored dollar allowance")


def require_run_limits(run_state: RunState, checkpoint: Path, max_budget_usd: BudgetUsd | None) -> None:
    if run_state.token_cap is None:
        raise ValueError(f"Cannot resume {checkpoint} with unknown usage history. Start a new run without --answer and with XDG_STATE_HOME set to a new directory.")
    require_same_budget(run_state.max_budget_usd, max_budget_usd)


@contextmanager
def issue_lock(checkpoint: Path, thread: str) -> Generator[None]:
    """Hold one writer per checkpoint and issue. Worker subprocesses inherit the lock."""
    locks = checkpoint.parent / "locks"
    locks.mkdir(parents=True, exist_ok=True)
    with (locks / spec_digest([str(checkpoint.resolve()), thread])).open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError(f"Foreman is already running {thread}.") from exc
        token = worker_lock.set(lock.fileno())
        try:
            yield
        finally:
            worker_lock.reset(token)


def run_live_delivery(
    *,
    repo: str,
    issue: int,
    check: str,
    checkpoint: Path,
    run: LiveRun,
    preflight: str = "",
    dispatcher: Handler,
    request: str | None = None,
    answer: str | None = None,
    max_tokens: int | None = None,
    max_budget_usd: BudgetUsd | None = None,
    presentation: RunPresentation | None = None,
    watch_repo: str = "bookout-labs/tools",
    report: Callable[[DeliveryResult], None] = lambda result: None,
    log: Callable[[str], None] = lambda message: None,
) -> DeliveryResult:
    if presentation is not None:
        presentation.restored = None
        presentation.updates.clear()
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    thread = f"{repo.lower()}#{issue}"
    config: RunnableConfig = {"configurable": {"thread_id": thread}}
    workspace = str(run.workspace.root.resolve())
    client = run.forge
    with (issue_lock(checkpoint, thread), SqliteSaver.from_conn_string(str(checkpoint)) as saver,
          TemporaryDirectory(prefix="foreman-skills-") as scratch,
          closing(sqlite3.connect(checkpoint, check_same_thread=False)) as connection):
        saver.serde = checkpoint_serializer()
        prior = saver.get(config)
        channels = prior["channel_values"] if prior else {}
        if prior is not None:
            require_schema_version(channels, checkpoint, thread)
        run = replace(run, journal=AttemptJournal(connection))
        delivery_config = load_config()
        run_state = RunState()
        if prior is not None:
            run_state = RunState.model_validate(channels["run"])
            if run_state.token_cap is not None and max_tokens is not None and run_state.token_cap != max_tokens:
                raise ValueError("--max-tokens differs from the stored token cap")
            if not channels.get("stop") or channels.get("halt") in RESUMABLE_HALTS:
                require_run_limits(run_state, checkpoint, max_budget_usd)
            else:
                require_same_budget(run_state.max_budget_usd, max_budget_usd)
        saved_pin, saved_matt = channels.get("pstack_pin"), channels.get("matt_skills_pin")
        if prior and not channels.get("stop") and (saved_pin is None or saved_matt is None):
            raise ValueError("Cannot resume a delivery checkpoint without its skill pins.")
        # A resumed run keeps the skills it started with.
        pin = SkillPin.model_validate(saved_pin) if saved_pin is not None else delivery_config.pstack
        matt = SkillPin.model_validate(saved_matt) if saved_matt is not None else delivery_config.matt_skills
        pinned = replace(delivery_config, pstack=pin, matt_skills=matt)
        early_result: DeliveryResult | None = None
        try:
            graph = compile_delivery(
                dispatcher, pinned, run=run, checkpointer=saver,
                scratch=None if channels.get("stop") and channels.get("halt") not in RESUMABLE_HALTS else Path(scratch),
            )
        except SkillMissing as exc:
            graph = compile_delivery(dispatcher, pinned, run=run, checkpointer=saver)
            skill_halt: DeliveryUpdate = {"schema_version": SCHEMA_VERSION, **halted(Halt.skill_missing, str(exc), "admit"), "stop": exc.halt,
                                          "skill_reason": exc.reason, "pstack_pin": pin.model_dump(), "matt_skills_pin": matt.model_dump(),
                                          "run": run_state if prior else RunState(issue=issue, workspace=workspace, repo=repo,
                                              max_budget_usd=usd_text(max_budget_usd) if max_budget_usd is not None else None,
                                              token_cap=max_tokens if max_tokens is not None else DEFAULT_TOKEN_CAP),
                                          "plan": PlanState.model_validate(channels["plan"]) if prior else PlanState(),
                                          "checks": ChecksState.model_validate(channels["checks"]) if prior else ChecksState(check=check, preflight=preflight),
                                          "review": ReviewState.model_validate(channels["review"]) if prior else ReviewState()}
            if prior and (not channels.get("stop") or channels.get("halt") in RESUMABLE_HALTS):
                early_result = read_delivery_state(skill_halt)
            else:
                graph.update_state(config, skill_halt, as_node="triage")
        def invoke(graph_input: DeliveryState | Command[str] | None, invocation_config: RunnableConfig = config) -> None:
            for part in graph.stream(
                graph_input, invocation_config, context=run,
                durability="sync", stream_mode="updates", version="v2",
            ):
                if presentation is not None:
                    for node, raw in part["data"].items():
                        if not node.startswith("__") and raw is not None:
                            presentation.updates.append(_DELIVERY_UPDATE.validate_python(raw, strict=True))

        saved = graph.get_state(config, subgraphs=True)
        if saved.values:
            require_run_limits(RunState.model_validate(saved.values.get("run", RunState())), checkpoint, max_budget_usd)
        saved_values = checkpoint_state(saved.values)
        saved_review = saved_values["review"] if saved_values is not None else None
        if presentation is not None and saved.values:
            presentation.restore(saved)
        saved_workspace = saved_values["run"].workspace if saved_values else None
        if saved_workspace and saved_workspace != workspace:
            raise ValueError(f"Run this issue from {saved_workspace}.")
        if early_result is None and saved_values is not None and saved_values["run"].base_sha:
            # A resumed run keeps the identity triage admitted, in a checkout of the same repository.
            saved_run = saved_values["run"]
            loaded = client.load_issue(repo, issue)
            if (loaded["repo"].lower(), loaded["number"]) != (repo.lower(), issue) or (saved_run.repo.lower(), saved_run.issue) != (repo.lower(), issue):
                raise ValueError("The issue does not match the saved repository and number.")
            try:
                run.workspace.validate_origin(saved_run.repo)
            except WorkspaceError as exc:
                early_result = read_delivery_state({**saved_values, **halted(Halt.git_failed, str(exc), "admit"),
                                                    "stop": Halt.git_failed})
        pause: object = saved.interrupts[0].value if saved.interrupts else None
        waiting = is_dict(pause) and pause.get("reason") in PAUSES
        merge_wait = is_dict(pause) and pause.get("reason") == "merge_pending"
        if saved.interrupts and not waiting and saved_values is not None and early_result is None:
            prompt = parse_output(HumanPrompt, saved.interrupts[0].value)
            if prompt.revision is not None:
                prefix = prompt.answer_marker + "\n"
                paused = checkpoint_state(paused_values(saved))
                assert paused is not None and paused["run"].issue is not None
                used = {qa["answer"] for qa in paused["plan"].human_qa if qa.get("revision") == prompt.revision}
                replies = [ruling.removeprefix(prefix).strip() for ruling in human_comments(
                    client, run.human_login, paused["run"].repo, paused["run"].issue) if ruling.startswith(prefix)]
                replies = [reply for reply in replies if reply and reply not in used]
                if answer is not None and answer not in replies:
                    raise AnswerError("This question needs a matching answer posted under the configured GitHub identity.")
                answer = answer or next((reply for reply in reversed(replies) if reply), None)
        if answer is not None and early_result is None:
            if waiting:
                raise AnswerError("This issue has no pending question.")
            if not answer.strip():
                raise AnswerError("The answer must contain text.")
            if not saved.interrupts:
                answers = saved_values.get("human_answers", []) if saved_values else []
                if not answers:
                    raise AnswerError("This issue has no pending question.")
                if answer != answers[-1]:
                    raise AnswerError("This question was already answered differently. Resume with the recorded answer.")
        if early_result is not None:
            pass
        elif answer is not None and saved.interrupts and saved_values is not None:
            invoke(Command(resume=answer))
        elif saved_values is None:
            # Triage admits the issue: its identity, slice, prerequisites and pinned base.
            invoke({
                "schema_version": SCHEMA_VERSION,
                "run": RunState(repo=repo, issue=issue, workspace=workspace, request=request or "",
                                token_cap=max_tokens if max_tokens is not None else DEFAULT_TOKEN_CAP,
                                max_budget_usd=usd_text(max_budget_usd) if max_budget_usd is not None else None),
                "checks": ChecksState(check=check, preflight=preflight),
                "pstack_pin": pin.model_dump(),
                "matt_skills_pin": matt.model_dump(),
                "path": [],
                "plan": PlanState(),
                "review": ReviewState(),
            })
        elif waiting and not merge_wait:
            invoke(Command(resume=True))
        elif merge_wait or saved_values.get("halt") in RESUMABLE_HALTS:
            # A merge wait restarts its stage with the progress it reached, so each GitHub event reads GitHub once.
            if merge_wait:
                saved_values = checkpoint_state(paused_values(saved))
                assert saved_values is not None
            stage = "merge" if merge_wait else STEP_STAGES.get(next(iter(saved_values["path"][-1:]), ""))
            inside = stage is not None
            before = _checkpoint_before_halt(graph, config, saved_values["path"], stage)
            require_schema_version(before.values, checkpoint, thread)
            require_run_limits(RunState.model_validate(before.values.get("run", RunState())), checkpoint, max_budget_usd)
            restored = checkpoint_state(before.values)
            assert restored is not None
            previous_attempts = restored.get("attempts", [])
            attempts = saved_values.get("attempts", [])
            if attempts[:len(previous_attempts)] != previous_attempts:
                raise ValueError("Cannot resume a run with non-prefix attempt history.")
            if presentation is not None:
                presentation.restore(before)
            replay = before.config
            receipt = saved_values.get("merge", MergeState()).receipt
            progress: DeliveryUpdate = {}
            prior = saved_values.get("work", WorkState())
            if stage == "implement" and prior.head_sha and prior.published_head == prior.head_sha:
                # A halt before the next attempt started discards the published attempt.
                work, checks = WorkState(branch=prior.branch, base=prior.base), saved_values["checks"].for_attempt()
            elif inside:
                # The stage restarts with the progress it reached, so no paid call or remote write repeats.
                work, checks = saved_values.get("work", WorkState()), saved_values["checks"]
                progress = _DELIVERY_UPDATE.validate_python(
                    {key: value for key, value in saved_values.items() if key in STAGE_PROGRESS}, strict=True)
            else:
                work, checks = restored.get("work", WorkState()).model_copy(update={"repair_head": None}), restored["checks"]
            assert saved_review is not None
            retained: DeliveryUpdate = {**progress, "plan": (saved_values if inside else restored)["plan"].model_copy(update={
                                            "rejected_revisions": saved_values["plan"].rejected_revisions,
                                        }),
                                        "work": work, "merge": MergeState(receipt=receipt), "checks": checks, "review": saved_review}
            if halted_attempts := attempts[len(previous_attempts):]:
                retained["attempts"] = halted_attempts
            invoke(None, graph.update_state(replay, retained))
        elif saved.next and not saved.interrupts:
            invoke(None)

        saved = graph.get_state(config, subgraphs=True)
        result = early_result if early_result is not None else snapshot_result(saved)
        if result.get("stop") == "blocked":
            # Open prerequisites leave the issue unadmitted, so a later Ready recheck starts it fresh.
            saver.delete_thread(thread)
        report(result)
        if (halt := result.get("halt")) is not None:
            if HALTS[halt].watch is None:
                log(f"watch skipped {halt}")
            elif result.get("watch") is None and early_result is None:
                step = (saved.metadata or {}).get("step")
                assert step is not None
                key = run_key(repo, issue, step)
                try:
                    record = watch(result, client, watch_repo, key)
                except ForgeError as exc:
                    log(f"watch failed: {exc}")
                else:
                    if record is not None:
                        graph.update_state(config, {"watch": record}, as_node="triage")
                        result["watch"] = record
                        log(f"watch {record['action']} #{record['issue']}")
        return result

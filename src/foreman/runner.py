"""Run one delivery issue against its SQLite checkpoint."""

from collections.abc import Callable, Generator
from contextlib import closing, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from pathlib import Path
import fcntl
import shutil
from typing import Any, NamedTuple
import sqlite3
from tempfile import TemporaryDirectory

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import CheckpointTuple
from langgraph.checkpoint.serde.types import INTERRUPT
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, StateSnapshot
from pydantic import TypeAdapter

from foreman._raw import is_dict
from foreman.accept import OpenRun
from foreman.config import SkillPin, load_config
from foreman.outcomes import HALTS, PAUSES, RELEASED_STOPS, Halt, RESUMABLE_HALTS
from foreman.delivery import LiveRun, compile_delivery, halted, read_delivery_state
from foreman.readiness import MergeState, MergeWait
from foreman.state import ReviewState, PlanState, ChecksState, RunState, StartSettings, SCHEMA_VERSION, DeliveryResult, DeliveryState, DeliveryUpdate, checkpoint_serializer, require_schema_version
from foreman.dispatch import Handler, QuotaPause
from foreman.doctor import REFUSALS, NotReady, OperatorVariables, ReadFailed, repository
from foreman.skill_rules import SkillMissing
from foreman.github import Forge, ForgeError, human_comment_records, human_comments
from foreman.watch import run_key, watch
from foreman.stage_output import HumanAnswer, HumanPrompt, SpecCandidate, parse_output
from foreman.status import Status, not_continued, refused, running, status_of, write_status
from foreman.steps import TOO_LATE, close, implement, run_pull_request, stop_counts
from foreman.workspace import WorkState, WorkspaceError, lock_file
from foreman.worker import AttemptJournal, verify_attempts, worker_lock

_CHECKPOINT_STATE = TypeAdapter(DeliveryState)
_DELIVERY_UPDATE = TypeAdapter(DeliveryUpdate)
STAGE_PROGRESS = ("research", "implementation", "models_used", "implement_models", "human_answers", "dispatch_plan", "pr_size", "verify")
# Each step a stage records in the path, so a halt inside a stage resumes that stage.
STEP_STAGES = {"admit": "triage", "triage": "triage", "research": "research", "post": "research",
               "spec": "spec", "spec_check": "spec", "file_children": "spec", "verify": "verify", "review": "review", "merge": "merge",
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
            question += f"\n\n{prompt.reply}"
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


class IssueLocked(ValueError):
    """Another Foreman process holds this issue's lock."""


_held_locks: ContextVar[frozenset[str]] = ContextVar("held_issue_locks", default=frozenset())


def issue_thread(repo: str, issue: int) -> str:
    return f"{repo.lower()}#{issue}"


@contextmanager
def issue_lock(thread: str, *, wait: bool = False) -> Generator[None]:
    """Hold one writer per repository and issue for this user, whichever checkpoint it uses. The worker inherits the lock.

    A holder that enters again keeps its lock, so a start can check and run under one hold.
    """
    if thread in _held_locks.get():
        yield
        return
    with lock_file(thread).open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise IssueLocked(f"Foreman is already running {thread}.") from exc
        token, held = worker_lock.set(lock.fileno()), _held_locks.set(_held_locks.get() | {thread})
        try:
            yield
        finally:
            _held_locks.reset(held)
            worker_lock.reset(token)


def _saved(checkpoint: Path, thread: str) -> CheckpointTuple | None:
    if not checkpoint.exists():
        return None
    with SqliteSaver.from_conn_string(str(checkpoint)) as saver:
        saver.serde = checkpoint_serializer()
        return saver.get_tuple({"configurable": {"thread_id": thread}})


def _saved_values(checkpoint: Path, thread: str) -> dict[str, Any] | None:
    saved = _saved(checkpoint, thread)
    return saved.checkpoint["channel_values"] if saved is not None else None


def open_run(checkpoint: Path, thread: str) -> OpenRun | None:
    """The issue's run unless it has none or it ended with a result other than a halt. Only such a run takes a wake.

    A stage's pause is saved as the run's pending interrupt, so this reads it without compiling the graph."""
    saved = _saved(checkpoint, thread)
    if saved is None:
        return None
    values = saved.checkpoint["channel_values"]
    require_schema_version(values, checkpoint, thread)
    pending = [value for _, channel, value in (saved.pending_writes or []) if channel == INTERRUPT]
    if not pending and values.get("stop") in RELEASED_STOPS:
        return None
    pause: object = pending[0][0].value if pending else None
    start: StartSettings = values["start"]
    return OpenRun(start.human_login, None if pause is None or is_dict(pause) and pause.get("reason") in PAUSES else parse_output(HumanPrompt, pause))


def ended_cancelled(checkpoint: Path, thread: str) -> bool:
    """Whether the issue's saved run ended cancelled, the one ended run that Ready added again replaces (intake.policy)."""
    return (_saved_values(checkpoint, thread) or {}).get("stop") == "cancelled"


def forget_run(checkpoint: Path, thread: str) -> None:
    """Delete the issue's saved run, so that Ready added after it ended cancelled admits a new run."""
    with SqliteSaver.from_conn_string(str(checkpoint)) as saver:
        saver.delete_thread(thread)


def saved_workspace(checkpoint: Path, thread: str) -> str | None:
    """The checkout of the issue's saved run, or None when it has none. Blocked admission deletes its checkpoint, so a later Ready starts fresh."""
    if (values := _saved_values(checkpoint, thread)) is None:
        return None
    run = values.get("run")
    return RunState.model_validate(run).workspace if run is not None else ""


def halted_resumably(checkpoint: Path, thread: str) -> bool:
    """Whether the issue's saved run stopped on a resumable halt, which a Ready label added again resumes (intake)."""
    values = _saved_values(checkpoint, thread) or {}
    return bool(values.get("stop")) and values.get("halt") in RESUMABLE_HALTS


def _shown(result: DeliveryResult, saved: StateSnapshot, watch_repo: str) -> Status | None:
    """The status the issue shows for the run's result, with the question it is paused on."""
    pending = saved.interrupts[0].value if saved.interrupts else None
    prompt = parse_output(HumanPrompt, pending) if result["stop"] == "needs_human" and pending is not None else None
    last = prompt.stage if prompt is not None else STEP_STAGES.get(next(iter(result["path"][-1:]), ""))
    return status_of(result, last, prompt, watch_repo)


class Wake(NamedTuple):
    """What a wake found: the run's unchanged result and the status to write when nothing resumes, whether a stop ended the
    run cancelled, and the human's comments its stop read fetched, which also hold a gate's answer."""

    result: DeliveryResult | None
    status: Status | None = None
    stopped: bool = False
    comments: list[str] | None = None


def _wake(graph: CompiledStateGraph[DeliveryState, LiveRun, DeliveryState, DeliveryResult], config: RunnableConfig,
          saved: StateSnapshot, client: Forge, repo: str, issue: int, watch_repo: str) -> Wake:
    """Handle a wake on the issue's saved run before anything resumes (rules.gates.continuation, merge's invariants)."""
    current = snapshot_result(saved)
    if current["stop"] in RELEASED_STOPS:
        # A wake that finds the run ended with a result other than a halt reads nothing and writes nothing.
        return Wake(current)
    pause: object = saved.interrupts[0].value if saved.interrupts else None
    if is_dict(pause) and pause.get("reason") == "merge_pending":
        return Wake(None)  # The wait resumes merge, whose arm reads the PR and then the stop.
    # A run with no pause and no halt is one whose process died mid-step. Like a halt, only a stop wakes it.
    held = "waiting" if is_dict(pause) and pause.get("reason") in PAUSES else "gate" if saved.interrupts else "halted"
    values = checkpoint_state(paused_values(saved))
    assert values is not None
    step = next(iter(current["path"][-1:]), "admit")
    stage = STEP_STAGES[step]
    merging, failed = stage == "merge", "The read of the pull request"
    try:
        # In merge the PR is read before the stop, as arm reads it.
        pr = run_pull_request(values, client) if merging else None
        failed = "The read of the issue"
        records = human_comment_records(client, values["start"].human_login, repo, issue)
        comments = [(comment.body or "").strip() for comment in records]
        if not stop_counts(client, records, repo, issue):
            # A waiting or gate-paused run resumes, and the gate reads its answer from these comments.
            return Wake(current if held == "halted" else None, comments=comments)
        if not merging:
            failed = "The read of the pull request"
            pr = run_pull_request(values, client)
        failed = "The close of the pull request"
        settled = close(values, client, step, pr, merging=merging)
    except ForgeError as exc:
        # Nothing resumes and the run stays as it was. The report goes above its state until the next rewrite.
        shown = _shown(current, saved, watch_repo) or running(stage)
        return Wake(current, not_continued(failed, exc.answer, exc.status in REFUSALS and not exc.rate_limited, held, shown))
    if settled.get("stop"):
        graph.update_state(config, {"stop": "cancelled", "halt": None, "halt_detail": "", "path": [step]}, as_node=stage)
        return Wake(None, stopped=True)
    # The PR merged first, so the stop is too late: it is recorded on the run, once, and the run stays as it was.
    detail = values.get("halt_detail", "")
    if held == "halted":
        recorded = detail if detail.endswith(TOO_LATE) else f"{detail}\n\n{TOO_LATE}"
        if recorded != detail:
            graph.update_state(config, {"halt_detail": recorded}, as_node=stage)
        current = read_delivery_state({**current, "halt_detail": recorded})
        return Wake(current, _shown(current, saved, watch_repo))
    if detail != TOO_LATE:
        # Merge's own state records it, as merge does, so the question stays open.
        paused = next(task.state for task in saved.tasks if task.interrupts and isinstance(task.state, StateSnapshot))
        graph.update_state(paused.config, {"halt_detail": TOO_LATE})
    shown = _shown(current, saved, watch_repo)
    return Wake(current, shown and Status(shown.label, f"{shown.text}\n\n{TOO_LATE}", shown.facts))


def run_live_delivery(
    *,
    repo: str,
    issue: int,
    checkpoint: Path,
    run: LiveRun,
    dispatcher: Handler,
    request: str | None = None,
    answer: str | None = None,
    presentation: RunPresentation | None = None,
    watch_repo: str = "bookout-labs/tools",
    wake: bool = False,
    report: Callable[[DeliveryResult], None] = lambda result: None,
    log: Callable[[str], None] = lambda message: None,
) -> DeliveryResult:
    if presentation is not None:
        presentation.restored = None
        presentation.updates.clear()
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    thread = issue_thread(repo, issue)
    config: RunnableConfig = {"configurable": {"thread_id": thread}}
    workspace = str(run.workspace.root.resolve())
    client = run.forge
    with (issue_lock(thread), SqliteSaver.from_conn_string(str(checkpoint)) as saver,
          TemporaryDirectory(prefix="foreman-skills-") as scratch,
          closing(sqlite3.connect(checkpoint, check_same_thread=False)) as connection):
        saver.serde = checkpoint_serializer()
        prior = saver.get(config)
        channels = prior["channel_values"] if prior else {}
        if prior is not None:
            require_schema_version(channels, checkpoint, thread)
        run_state = RunState.model_validate(channels["run"]) if prior is not None else RunState(
            repo=repo, issue=issue, workspace=workspace, request=request or "")
        # A start is a new run or the resume of a resumable halt. It reads the repository's settings once, before the graph.
        # A wake is never a start.
        starting = not wake and (prior is None or (bool(channels.get("stop")) and channels.get("halt") in RESUMABLE_HALTS))
        settings: OperatorVariables | None = None
        refusal: NotReady | ReadFailed | None = None
        # Every later wake and answer uses the login and caps of the run's last start unchanged.
        start: StartSettings | None = channels.get("start")
        if starting:
            try:
                settings = repository(client, repo)
            except (NotReady, ReadFailed) as exc:
                refusal = exc
            else:
                start = settings.start
        run = replace(run, journal=AttemptJournal(connection))
        delivery_config = load_config()
        saved_pin, saved_matt = channels.get("pstack_pin"), channels.get("matt_skills_pin")
        if prior and not channels.get("stop") and (saved_pin is None or saved_matt is None):
            raise ValueError("Cannot resume a delivery checkpoint without its skill pins.")
        # A resumed run keeps the skills it started with.
        pin = SkillPin.model_validate(saved_pin) if saved_pin is not None else delivery_config.pstack
        matt = SkillPin.model_validate(saved_matt) if saved_matt is not None else delivery_config.matt_skills
        pinned = replace(delivery_config, pstack=pin, matt_skills=matt)
        early_result: DeliveryResult | None = None
        # The status an early result writes: the runner's refusal of a start, or what a wake's stop read found.
        early_status: Status | None = None
        try:
            graph = compile_delivery(
                dispatcher, pinned, run=run, checkpointer=saver,
                scratch=None if refusal is not None or channels.get("stop") and (wake or channels.get("halt") not in RESUMABLE_HALTS)
                else Path(scratch),
            )
        except SkillMissing as exc:
            # Skills export only for a start the runner accepted or a saved run, so the run has its start.
            assert start is not None
            graph = compile_delivery(dispatcher, pinned, run=run, checkpointer=saver)
            skill_halt: DeliveryUpdate = {"schema_version": SCHEMA_VERSION, **halted(Halt.skill_missing, str(exc), "admit"), "stop": exc.halt,
                                          "skill_reason": exc.reason, "pstack_pin": pin.model_dump(), "matt_skills_pin": matt.model_dump(),
                                          "run": run_state, "start": start,
                                          "plan": PlanState.model_validate(channels["plan"]) if prior else PlanState(),
                                          "checks": ChecksState.model_validate(channels["checks"]) if prior else ChecksState(
                                              preflight=settings.preflight if settings else ""),
                                          "review": ReviewState.model_validate(channels["review"]) if prior else ReviewState()}
            if prior and (not channels.get("stop") or channels.get("halt") in RESUMABLE_HALTS):
                early_result = read_delivery_state(skill_halt)
            else:
                graph.update_state(config, skill_halt, as_node="triage")
        def mirror(status: Status) -> None:
            """The issue shows the checkpoint's state. A GitHub failure here never changes the run."""
            try:
                write_status(client, repo, issue, status)
            except ForgeError as exc:
                log(f"status not written: {exc}")

        mirrored: list[str | None] = [None]

        def invoke(graph_input: DeliveryState | Command[str] | None, invocation_config: RunnableConfig = config) -> None:
            for part in graph.stream(
                graph_input, invocation_config, context=run,
                durability="sync", stream_mode=["updates", "tasks"], subgraphs=True, version="v2",
            ):
                namespace, data = part["ns"], part["data"]
                if part["type"] == "tasks":
                    if "input" not in data:
                        continue
                    # A task's start carries its input. The first task of each stage on an admitted run shows Doing,
                    # including the admitted triage task that routes, whose base only its subgraph holds.
                    stage = namespace[0].split(":")[0] if namespace else data["name"]
                    task_input: object = data["input"]
                    target = task_input.get("run") if is_dict(task_input) else None
                    if isinstance(target, RunState) and target.base_sha and stage != mirrored[0]:
                        mirrored[0] = stage
                        mirror(running(stage))
                elif presentation is not None and not namespace:
                    for node, raw in data.items():
                        if not node.startswith("__") and raw is not None:
                            presentation.updates.append(_DELIVERY_UPDATE.validate_python(raw, strict=True))

        saved = graph.get_state(config, subgraphs=True)
        saved_values = checkpoint_state(saved.values)
        saved_review = saved_values["review"] if saved_values is not None else None
        if presentation is not None and saved.values:
            presentation.restore(saved)
        saved_workspace = saved_values["run"].workspace if saved_values else None
        if saved_workspace and saved_workspace != workspace:
            raise ValueError(f"Run this issue from {saved_workspace}.")
        if refusal is not None:
            # The runner's not_ready or git_failed is not a saved halt. A new run keeps nothing. A resumed run keeps its checkpoint and halt.
            kept: dict[str, object] = dict(saved_values) if saved_values is not None else {
                "schema_version": SCHEMA_VERSION, "run": run_state, "checks": ChecksState(), "plan": PlanState(),
                "review": ReviewState(), "path": []}
            code, detail = Halt.not_ready if isinstance(refusal, NotReady) else Halt.git_failed, refusal.halt_detail()
            early_result = read_delivery_state({**kept, **halted(code, detail, "admit"), "stop": code})
            early_status = refused(code, detail, resumed=saved_values is not None)
        pause: object = saved.interrupts[0].value if saved.interrupts else None
        waiting = is_dict(pause) and pause.get("reason") in PAUSES
        merge_wait = is_dict(pause) and pause.get("reason") == "merge_pending"
        woken = _wake(graph, config, saved, client, repo, issue, watch_repo) if wake and saved_values is not None and early_result is None else Wake(None)
        if woken.result is not None:
            early_result, early_status = woken.result, woken.status
        # A wake whose stop ended the run resumes nothing, as an early result does, and its cancelled run releases its checkout.
        resumes = early_result is None and not woken.stopped
        finished = bool(saved_values and (saved_values.get("stop") in RELEASED_STOPS or (
            saved_values.get("halt") is not None and saved_values.get("halt") not in RESUMABLE_HALTS)))
        if resumes and saved_values is not None and saved_values["run"].base_sha and not finished:
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
        if waiting:
            # A wake that resumes the paused stage keeps its waiting status, so the comment does not flap.
            mirrored[0] = STEP_STAGES.get(snapshot_result(saved)["path"][-1])
        prompt: HumanPrompt | None = None
        if saved.interrupts and not waiting and saved_values is not None and resumes:
            prompt = parse_output(HumanPrompt, saved.interrupts[0].value)
            if prompt.revision is not None:
                paused = checkpoint_state(paused_values(saved))
                assert paused is not None and paused["run"].issue is not None
                used = {qa["answer"] for qa in paused["plan"].human_qa if qa.get("revision") == prompt.revision}
                read = woken.comments if woken.comments is not None else human_comments(client, paused["start"].human_login, paused["run"].repo, paused["run"].issue)
                replies = [reply for comment in read if (reply := prompt.answer_in(comment)) and reply not in used]
                if answer is not None and answer not in replies:
                    raise AnswerError("This question needs a matching answer posted under the configured GitHub identity.")
                answer = answer or next((reply for reply in reversed(replies) if reply), None)
        if answer is not None and resumes:
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
        # A merge gate's answer is saved before the stage reads GitHub again, so it never depends on a replay.
        merge_gate = answer is not None and prompt is not None and prompt.stage == "merge"
        if not resumes:
            pass
        elif answer is not None and saved.interrupts and saved_values is not None and not merge_gate:
            invoke(Command(resume=answer))
        elif saved_values is None:
            # Triage admits the issue: its identity, slice, prerequisites and pinned base.
            assert settings is not None and start is not None
            invoke({
                "schema_version": SCHEMA_VERSION,
                "run": run_state,
                "start": start,
                "checks": ChecksState(preflight=settings.preflight),
                "pstack_pin": pin.model_dump(),
                "matt_skills_pin": matt.model_dump(),
                "path": [],
                "plan": PlanState(),
                "review": ReviewState(),
            })
        elif waiting and not merge_wait:
            invoke(Command(resume=True))
        elif merge_wait or merge_gate or saved_values.get("halt") in RESUMABLE_HALTS:
            # A paused merge stage restarts with the progress it reached, so each GitHub event reads GitHub once.
            if paused_merge := merge_wait or merge_gate:
                saved_values = checkpoint_state(paused_values(saved))
                assert saved_values is not None
            stage = "merge" if paused_merge else STEP_STAGES.get(next(iter(saved_values["path"][-1:]), ""))
            inside = stage is not None
            before = _checkpoint_before_halt(graph, config, saved_values["path"], stage)
            require_schema_version(before.values, checkpoint, thread)
            restored = checkpoint_state(before.values)
            assert restored is not None
            previous_attempts = restored.get("attempts", [])
            attempts = saved_values.get("attempts", [])
            if attempts[:len(previous_attempts)] != previous_attempts:
                raise ValueError("Cannot resume a run with non-prefix attempt history.")
            if presentation is not None:
                presentation.restore(before)
            replay = before.config
            saved_merge = saved_values.get("merge", MergeState())
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
            plan = (saved_values if inside else restored)["plan"].model_copy(update={"rejected_revisions": saved_values["plan"].rejected_revisions})
            if merge_gate:
                assert prompt is not None and answer is not None and prompt.revision is not None
                plan = plan.model_copy(update={"human_qa": [*plan.human_qa, HumanAnswer(
                    stage="merge", question=prompt.question, answer=answer, revision=prompt.revision)]})
            # A paused merge keeps its confirmed arm, so the restart reconciles it at observe rather than at arm.
            merge = MergeState(receipt=saved_merge.receipt, status=saved_merge.status if paused_merge else None)
            retained: DeliveryUpdate = {**progress, "plan": plan, "work": work, "merge": merge, "checks": checks, "review": saved_review}
            if starting:
                # The rewound step keeps the run's admitted identity, with the login and caps this start read.
                assert start is not None
                retained["run"], retained["start"] = run_state, start
            if "builder_session" in saved_values:
                # The builder keeps its conversation for the PR; its fingerprint decides whether the next call resumes it (D5).
                retained["builder_session"] = saved_values["builder_session"]
            if halted_attempts := attempts[len(previous_attempts):]:
                retained["attempts"] = halted_attempts
            invoke(None, graph.update_state(replay, retained))
        elif saved.next and not saved.interrupts:
            invoke(None)

        saved = graph.get_state(config, subgraphs=True)
        result = early_result if early_result is not None else snapshot_result(saved)
        if result.get("stop") in RELEASED_STOPS and early_result is None:
            # A finished run's clean checkout is removed. A halted run keeps its checkout as evidence.
            heads = {head for head in (result["run"].base_sha, result.get("work", WorkState()).published_head) if head}
            try:
                if run.workspace.release(heads):
                    log(f"released {run.workspace.root}")
            except WorkspaceError as exc:
                log(f"checkout kept: {exc}")
            # The CLI worker keeps its scratch beside the checkpoint, so the run removes its verifier captures with its checkout.
            attempts = verify_attempts(checkpoint.parent, result["run"].repo, result["run"].issue)
            try:
                if attempts.exists():
                    shutil.rmtree(attempts)
                    log(f"released {attempts}")
            except OSError as exc:
                log(f"verify captures kept: {exc}")
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
        if early_status is not None:
            try:
                write_status(client, repo, issue, early_status)
            except ForgeError as exc:
                # The command's error output and its exit code are then the only report.
                log(f"status not written: GitHub {exc.answer}.\n{early_status.text}")
        elif early_result is None:
            # An early result was never saved, so the issue keeps the status its checkpoint last showed.
            if (status := _shown(result, saved, watch_repo)) is not None:
                mirror(status)
        if early_result is None and result.get("stop") in ("blocked", Halt.not_ready):
            # Open prerequisites or a missing requirement leave the issue unadmitted, so a later Ready starts it fresh.
            # The status comment already names a missing requirement and its fix.
            saver.delete_thread(thread)
        return result

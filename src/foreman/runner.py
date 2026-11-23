"""Run one delivery issue against its SQLite checkpoint."""

from collections.abc import Callable, Generator
from contextlib import closing, contextmanager
from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path
import fcntl
import sqlite3
from tempfile import TemporaryDirectory

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, StateSnapshot
from pydantic import TypeAdapter

from foreman._raw import is_dict
from foreman.config import PstackPin, load_config
from foreman.outcomes import HALTS, Halt, RESUMABLE_HALTS
from foreman.delivery import DEFAULT_TOKEN_CAP, LiveRun, compile_delivery, halted, read_delivery_state, human_comments, spec_effect, spec_target
from foreman.readiness import MergeState
from foreman.state import ReviewState, PlanState, ChecksState, RunState, SCHEMA_VERSION, DeliveryResult, DeliveryState, DeliveryUpdate, checkpoint_serializer, require_schema_version
from foreman.dispatch import Handler, InvalidOutput, QuotaPause
from foreman.skill_rules import SkillMissing
from foreman.github import Forge, ForgeError
from foreman.watch import run_key, watch
from foreman.stage_output import HumanPrompt, SpecCandidate, spec_digest, StoredSlice, parse_output, load_stored_slice as _load_slice
from foreman.steps import implement
from foreman.usd import BudgetUsd, usd_text
from foreman.workspace import WorkState, WorkspaceError
from foreman.worker import AttemptJournal, worker_lock

_CHECKPOINT_STATE = TypeAdapter(DeliveryState)
_DELIVERY_UPDATE = TypeAdapter(DeliveryUpdate)
IMPLEMENT_PROGRESS = ("implementation", "models_used", "implement_models", "human_answers", "dispatch_plan", "pr_size")


@dataclass
class RunPresentation:
    """Invocation-local console data, kept outside graph state and return values."""

    restored: DeliveryResult | None = None
    updates: list[DeliveryUpdate] = field(default_factory=list[DeliveryUpdate])
    commands: list[list[str]] = field(default_factory=list[list[str]])

    def restore(self, snapshot: StateSnapshot) -> None:
        self.restored = snapshot_result(snapshot)


def snapshot_result(snapshot: StateSnapshot) -> DeliveryResult:
    values = snapshot.values
    for task in snapshot.tasks:
        if task.name == "implement" and task.interrupts and isinstance(nested := task.state, StateSnapshot):
            # A pause inside implement shows the attempt the stage has reached.
            values = {**values, **nested.values, "path": [*values["path"], *nested.values.get("path", [])]}
    if snapshot.interrupts:
        pause: object = snapshot.interrupts[0].value
        if is_dict(pause) and pause.get("reason") == "quota_exhausted":
            quota = parse_output(QuotaPause, pause)
            return read_delivery_state({**values, "stop": quota.reason, "halt_detail": quota.detail,
                                        "path": [*values["path"], quota.stage],
                                        "attempts": [*values.get("attempts", []), *quota.attempts]})
        prompt = parse_output(HumanPrompt, pause)
        question = prompt.question
        if prompt.revision:
            question += f"\n\nReply on the issue under the configured GitHub identity. Start with {prompt.answer_marker} and put the answer on the next line."
        return read_delivery_state({**values, "stop": "needs_human", "halt_detail": question})
    return read_delivery_state({**values, "stop": values.get("stop", "")})


def checkpoint_state(values: dict[str, object]) -> DeliveryState | None:
    if not values:
        return None
    require_schema_version(values)
    state = _CHECKPOINT_STATE.validate_python(values, strict=True)
    plan = state["plan"]
    if plan.spec_candidate is not None:
        candidate = SpecCandidate.from_record(plan.spec_candidate)
        if state["run"].base_sha and candidate.target != spec_target(state["run"]):
            raise ValueError("The saved candidate targets a different repository, issue or base.")
        expected = candidate.update(plan, single=plan.slice is not None)
        for key in ("slice", "slices", "spec", "work_kind"):
            if getattr(plan, key) != getattr(expected, key):
                raise ValueError(f"{key} differs from candidate")
    return state


class AnswerError(RuntimeError):
    """An answer cannot be applied to this checkpoint's question."""


def _blocker_detail(forge: Forge, repo: str, issue: int, item: StoredSlice | None) -> str:
    blockers = forge.blockers(repo, issue)
    if open_urls := [blocker.html_url for blocker in blockers if blocker.state == "open"]:
        return "Open blockers: " + ", ".join(open_urls)
    if item is None:
        return ""
    local = {blocker.number: blocker for blocker in blockers if blocker.in_repo(repo)}
    planned = item.blocker_numbers
    if len(planned) != len(item.depends_on) or not set(planned) <= local.keys():
        return "Planned blocker links are missing."
    if unmerged := [number for number in planned if not forge.is_merged(repo, number)]:
        return "Blockers have no merged pull request: " + ", ".join(f"#{number}" for number in unmerged)
    return ""


def _checkpoint_before_halt(
    graph: CompiledStateGraph[DeliveryState, LiveRun, DeliveryState, DeliveryResult],
    config: RunnableConfig,
    path: list[str],
    node: str | None = None,
) -> StateSnapshot:
    if path and path[-1] == "done":
        path = path[:-1]
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
        saved_pin = channels.get("pstack_pin")
        if prior and not channels.get("stop") and saved_pin is None:
            raise ValueError("Cannot resume a delivery checkpoint without a pstack pin.")
        pin = PstackPin.model_validate(saved_pin) if saved_pin is not None else delivery_config.pstack
        early_result: DeliveryResult | None = None
        try:
            graph = compile_delivery(
                dispatcher, replace(delivery_config, pstack=pin), run=run, checkpointer=saver,
                scratch=None if channels.get("stop") and channels.get("halt") not in RESUMABLE_HALTS else Path(scratch),
            )
        except SkillMissing as exc:
            graph = compile_delivery(dispatcher, replace(delivery_config, pstack=pin), run=run, checkpointer=saver)
            skill_halt: DeliveryUpdate = {"schema_version": SCHEMA_VERSION, **halted(Halt.skill_missing, str(exc), "done"), "stop": exc.halt,
                                          "skill_reason": exc.reason, "pstack_pin": pin.model_dump(),
                                          "run": run_state if prior else RunState(issue=issue, workspace=workspace, repo=repo,
                                              max_budget_usd=usd_text(max_budget_usd) if max_budget_usd is not None else None,
                                              token_cap=max_tokens if max_tokens is not None else DEFAULT_TOKEN_CAP),
                                          "plan": PlanState.model_validate(channels["plan"]) if prior else PlanState(),
                                          "checks": ChecksState.model_validate(channels["checks"]) if prior else ChecksState(check=check, preflight=preflight),
                                          "review": ReviewState.model_validate(channels["review"]) if prior else ReviewState()}
            if prior and (not channels.get("stop") or channels.get("halt") in RESUMABLE_HALTS):
                early_result = read_delivery_state(skill_halt)
            else:
                graph.update_state(config, skill_halt, as_node="done")
        def invoke(graph_input: DeliveryState | Command[str] | None, invocation_config: RunnableConfig = config) -> None:
            for part in graph.stream(
                graph_input, invocation_config, context=run,
                durability="sync", stream_mode="updates", version="v2",
            ):
                if presentation is not None:
                    for node, raw in part["data"].items():
                        if not node.startswith("__") and raw is not None:
                            presentation.updates.append(_DELIVERY_UPDATE.validate_python(raw, strict=True))

        saved = graph.get_state(config)
        if saved.values:
            require_run_limits(RunState.model_validate(saved.values.get("run", RunState())), checkpoint, max_budget_usd)
        saved_values = checkpoint_state(saved.values)
        saved_review = saved_values["review"] if saved_values is not None else None
        if presentation is not None and saved.values:
            presentation.restore(saved)
        saved_workspace = saved_values["run"].workspace if saved_values else None
        if saved_workspace and saved_workspace != workspace:
            raise ValueError(f"Run this issue from {saved_workspace}.")
        loaded = client.load_issue(repo, issue) if early_result is None and (saved_values is None or saved_values["run"].base_sha) else None
        identity_ok = loaded is not None and loaded["repo"].lower() == repo.lower() and loaded["number"] == issue
        if early_result is None and saved_values is not None and saved_values["run"].base_sha:
            saved_run = saved_values["run"]
            if not identity_ok or (saved_run.repo.lower(), saved_run.issue) != (repo.lower(), issue):
                raise ValueError("The issue does not match the saved repository and number.")
            try:
                run.workspace.validate_origin(saved_run.repo)
                if detail := _blocker_detail(client, saved_run.repo, issue, None):
                    early_result = read_delivery_state({**saved_values, "stop": "blocked", "halt_detail": detail})
            except (ForgeError, WorkspaceError) as exc:
                early_result = read_delivery_state({**saved_values, **halted(Halt.git_failed, str(exc), "triage"),
                                                    "stop": Halt.git_failed})
        pause: object = saved.interrupts[0].value if saved.interrupts else None
        waiting_for_quota = is_dict(pause) and pause.get("reason") == "quota_exhausted"
        if saved.interrupts and not waiting_for_quota and saved_values is not None and early_result is None:
            prompt = parse_output(HumanPrompt, saved.interrupts[0].value)
            if prompt.revision is not None:
                prefix = prompt.answer_marker + "\n"
                used = {qa["answer"] for qa in saved_values["plan"].human_qa if qa.get("revision") == prompt.revision}
                replies = [ruling.removeprefix(prefix).strip() for ruling in human_comments(run, saved_values["run"])
                           if ruling.startswith(prefix)]
                replies = [reply for reply in replies if reply and reply not in used]
                if answer is not None and answer not in replies:
                    raise AnswerError("This question needs a matching answer posted under the configured GitHub identity.")
                answer = answer or next((reply for reply in reversed(replies) if reply), None)
        if answer is not None and early_result is None:
            if waiting_for_quota:
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
            assert loaded is not None
            text = loaded["title"] + ("\n\n" + loaded["body"] if loaded["body"] else "")
            if request is not None:
                text = f"{text}\n\nOperator context:\n{request}"
            initial: DeliveryState = {
                "schema_version": SCHEMA_VERSION,
                "run": RunState(repo=loaded["repo"], issue=issue, workspace=workspace, request=text,
                                token_cap=max_tokens if max_tokens is not None else DEFAULT_TOKEN_CAP,
                                max_budget_usd=usd_text(max_budget_usd) if max_budget_usd is not None else None),
                "checks": ChecksState(check=check, preflight=preflight),
                "pstack_pin": pin.model_dump(),
                "path": [],
                "plan": PlanState(issue_kind="slice" if "slice" in loaded["labels"] else "parent"),
                "review": ReviewState(),
            }
            item = None
            if not identity_ok:
                initial.update(halted(Halt.invalid_output, "foreign_repository: issue identity differs from the requested assignment.", "triage"))
            elif initial["plan"].issue_kind == "slice":
                try:
                    item = _load_slice(loaded["body"])
                except InvalidOutput as exc:
                    initial.update(halted(Halt.missing_slice, str(exc), None))
                else:
                    initial["plan"] = initial["plan"].model_copy(update={"slice": item.record(), "work_kind": item.work_kind})
            if not initial.get("halt"):
                try:
                    run.workspace.validate_origin(loaded["repo"])
                    if detail := _blocker_detail(client, repo, issue, item):
                        initial.update(path=["done"], stop="blocked", halt_detail=detail)
                    else:
                        base = client.default_branch(repo)
                        initial["run"] = initial["run"].model_copy(update={"base_branch": base, "base_sha": run.workspace.pin_base(base)})
                except (ForgeError, WorkspaceError) as exc:
                    initial.update(halted(Halt.git_failed, str(exc), None))
            if initial.get("stop") == "blocked" or initial.get("halt") == Halt.git_failed:
                if halt := initial.get("halt"):
                    initial["stop"] = halt
                early_result = read_delivery_state(initial)
            else:
                invoke(initial)
        elif waiting_for_quota:
            invoke(Command(resume=True))
        elif saved_values.get("halt") in RESUMABLE_HALTS:
            repaired = False
            renew_review = False
            repair_head: str | None = None
            if HALTS[Halt(saved_values.get("halt"))].recovery == "recheck_merge" and not saved_values.get("merge", MergeState()).receipt and (head := saved_values["checks"].verified_head) and (branch := saved_values.get("work", WorkState()).branch):
                worktree = run.workspace.inspect()
                try:
                    observed = client.readiness(repo, issue, branch, head)
                except ForgeError:
                    observed = None
                if observed is not None and observed.outcome == "head_changed":
                    repair_head = observed.pr.head.sha
                    repaired = True
                elif observed is None or observed.outcome not in {"closed", "retargeted"}:
                    repaired = worktree.head != head
                    plan = saved_values.get("dispatch_plan")
                    target = plan["target"] if plan else None
                    if observed is not None and target is not None:
                        pr = observed.pr
                        renew_review = (pr.number, pr.head.sha, pr.base.ref) == (
                            target["number"], head, target["baseRefName"],
                        ) and pr.base.sha != target["baseRefOid"]
            inside = not repaired and saved_values["path"][-1:] in [[step] for step in implement.STEPS]
            before = _checkpoint_before_halt(
                graph, config, saved_values["path"], "implement" if repaired or inside else "review" if renew_review else None,
            )
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
            receipt = None if repaired else saved_values.get("merge", MergeState()).receipt
            progress: DeliveryUpdate = {}
            prior = saved_values.get("work", WorkState())
            if repaired or inside and prior.head_sha and prior.published_head == prior.head_sha:
                # A repair, or a halt before the next attempt started, discards the published attempt.
                work, checks = WorkState(branch=prior.branch, base=prior.base, repair_head=repair_head), saved_values["checks"].for_attempt()
            elif inside:
                # Implement restarts at prepare with the attempt it reached, so no paid call or remote write repeats.
                work, checks = saved_values.get("work", WorkState()), saved_values["checks"]
                progress = _DELIVERY_UPDATE.validate_python(
                    {key: value for key, value in saved_values.items() if key in IMPLEMENT_PROGRESS}, strict=True)
            else:
                work, checks = restored.get("work", WorkState()).model_copy(update={"repair_head": None}), restored["checks"]
            assert saved_review is not None
            retained: DeliveryUpdate = {**progress, "plan": (saved_values if inside else restored)["plan"].model_copy(update={
                                            "rejected_revisions": saved_values["plan"].rejected_revisions,
                                        }),
                                        "work": work, "merge": MergeState(receipt=receipt), "checks": checks, "review": (
                ReviewState(review_findings=saved_review.review_findings, settled_rulings=saved_review.settled_rulings,
                            review_run_id=saved_review.review_run_id)
                if repaired else saved_review)}
            if halted_attempts := attempts[len(previous_attempts):]:
                retained["attempts"] = halted_attempts
            if repaired or renew_review:
                invoke(Command(update=retained), replay)
            else:
                invoke(None, graph.update_state(replay, retained))
        elif saved.next and not saved.interrupts:
            invoke(None)

        saved = graph.get_state(config, subgraphs=True)
        result = early_result if early_result is not None else snapshot_result(saved)
        if result.get("stop") == "sliced":
            saved_values = checkpoint_state(saved.values)
            assert saved_values is not None
            clearance = spec_effect(saved_values, run)
            if clearance.get("halt") or clearance.get("stop"):
                graph.update_state(config, {**clearance, "stop": clearance.get("stop") or Halt.invalid_output}, as_node="done")
                result = snapshot_result(graph.get_state(config))
                report(result)
                return result
            if clearance.get("plan") != saved_values["plan"]:
                graph.update_state(config, clearance, as_node="done")
                result = snapshot_result(graph.get_state(config))
        if result.get("stop") == "sliced":
            saved_values = checkpoint_state(graph.get_state(config).values)
            assert saved_values is not None
            owner = saved_values["plan"]
            if not owner.slices:
                raise ValueError("Sliced checkpoint has no slice plan.")
            numbers = list(owner.child_issue_numbers)
            work_kind = owner.work_kind
            plan = [parse_output(StoredSlice, {**raw, "work_kind": work_kind}) for raw in owner.slices]
            siblings = {item.title: number for item, number in zip(plan, numbers)}
            for item in plan[len(numbers):]:
                child = item.model_copy(update={"blocker_numbers": [siblings[title] for title in item.depends_on]})
                labels = ["slice"] + ([] if item.depends_on else ["factory"])
                number = client.create_issue(repo, item.title, "", labels, child=child)
                numbers.append(number)
                siblings[item.title] = number
                graph.update_state(config, {"plan": owner.model_copy(update={"child_issue_numbers": numbers})})
            for item, number in zip(plan, numbers):
                if not item.depends_on:
                    continue
                blockers = client.blockers(repo, number)
                linked = {blocker.number for blocker in blockers if blocker.in_repo(repo)}
                planned = [siblings[title] for title in item.depends_on]
                for blocker in planned:
                    if blocker not in linked:
                        client.add_blocker(repo, number, blocker)
                if (all(blocker.state == "closed" for blocker in blockers)
                        and all(client.is_merged(repo, blocker) for blocker in planned)
                        and "factory" not in client.load_issue(repo, number)["labels"]):
                    client.add_label(repo, number, "factory")
            saved = graph.get_state(config)
            result = snapshot_result(saved)
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
                        graph.update_state(config, {"watch": record}, as_node="done")
                        result["watch"] = record
                        log(f"watch {record['action']} #{record['issue']}")
        return result

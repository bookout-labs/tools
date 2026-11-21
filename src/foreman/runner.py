"""Run one delivery issue against its SQLite checkpoint."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from decimal import Decimal
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, StateSnapshot
from pydantic import TypeAdapter

from foreman.config import PstackPin, load_config
from foreman.outcomes import HALTS, Halt, RESUMABLE_HALTS
from foreman.delivery import DEFAULT_TOKEN_CAP, HumanPrompt, QuotaPause, EnvironmentProbe, LiveRun, PullRequestSizeDecision, compile_delivery, halted, unknown_usage, read_delivery_state
from foreman.readiness import MergeState
from foreman.state import SCHEMA_VERSION, DeliveryResult, DeliveryState, DeliveryUpdate, checkpoint_serializer, require_schema_version
from foreman.dispatch import Handler, InvalidOutput
from foreman.skill_rules import SkillMissing
from foreman.fixture_manifest import MANIFEST_PATH, fact_record_path
from foreman.github import Forge, ForgeError
from foreman.watch import run_key, watch
from foreman.stage_output import Attempt, SpecCandidate, StoredSlice, parse_output
from foreman.usd import BudgetUsd, usd_text
from foreman.workspace import WorkState

_CHECKPOINT_STATE = TypeAdapter(DeliveryState)
_DELIVERY_UPDATE = TypeAdapter(DeliveryUpdate)


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
    if snapshot.interrupts:
        if values.get("halt") == Halt.quota_exhausted:
            parse_output(QuotaPause, snapshot.interrupts[0].value)
            return read_delivery_state({**values, "stop": Halt.quota_exhausted.value})
        question = parse_output(HumanPrompt, snapshot.interrupts[0].value).question
        return read_delivery_state({**values, "stop": "needs_human", "halt_detail": question})
    return read_delivery_state({**values, "stop": values.get("stop", "")})


def checkpoint_state(values: dict[str, object]) -> DeliveryState | None:
    if not values:
        return None
    require_schema_version(values)
    normalized = dict(values)
    if normalized.get("slice") is not None:
        normalized["slice"] = StoredSlice.model_validate(normalized["slice"]).record()
    state = _CHECKPOINT_STATE.validate_python(normalized, strict=True)
    if "spec_candidate" in state:
        candidate = SpecCandidate.from_record(state["spec_candidate"])
        expected = candidate.update(single="slice" in state)
        for key in ("slice", "slices", "spec", "work_kind"):
            if key in state and state.get(key) != expected.get(key):
                raise ValueError(f"{key} differs from candidate")
    return state


SLICE_FENCE = re.compile(r"^```json[ \t]*\r?\n((?:(?!^```)[^\n]*\n)*)^```[ \t]*\r?$", re.MULTILINE)


class AnswerError(RuntimeError):
    """An answer cannot be applied to this checkpoint's question."""


def _load_slice(body: str) -> StoredSlice:
    blocks = SLICE_FENCE.findall(body)
    if not blocks:
        raise InvalidOutput("slice issue needs a fenced JSON object")
    return parse_output(StoredSlice, json.loads(blocks[-1]))


def _blocker_detail(forge: Forge, repo: str, issue: int, item: StoredSlice) -> str:
    blockers = forge.blockers(repo, issue)
    if open_urls := [blocker.html_url for blocker in blockers if blocker.state == "open"]:
        return "Open blockers: " + ", ".join(open_urls)
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


def require_token_cap(values: Mapping[str, object], checkpoint: Path) -> None:
    if "token_cap" not in values:
        raise ValueError(f"Cannot resume {checkpoint} with unknown usage history. Start a new run with a new checkpoint path.")


def require_same_budget(stored: object, max_budget_usd: BudgetUsd | None) -> None:
    if max_budget_usd is not None and (stored is None or Decimal(str(stored)) != max_budget_usd.amount):
        raise ValueError("--max-budget-usd differs from the stored dollar allowance")


def require_run_limits(values: Mapping[str, object], checkpoint: Path, max_budget_usd: BudgetUsd | None) -> None:
    require_token_cap(values, checkpoint)
    if "max_budget_usd" not in values:
        raise ValueError(f"Cannot resume {checkpoint} with unknown usage history for --max-budget-usd. Start a new run with a new checkpoint path.")
    require_same_budget(values["max_budget_usd"], max_budget_usd)


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
    fact_record: str | None = None,
    presentation: RunPresentation | None = None,
    watch_repo: str = "bookout-labs/tools",
    report: Callable[[DeliveryResult], None] = lambda result: None,
    log: Callable[[str], None] = lambda message: None,
) -> DeliveryResult:
    if presentation is not None:
        presentation.restored = None
        presentation.updates.clear()
    if fact_record is not None:
        fact_record = fact_record_path(fact_record)
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    config: RunnableConfig = {"configurable": {"thread_id": f"{repo.lower()}#{issue}"}}
    workspace = str(run.workspace.root.resolve())
    client = run.forge
    with SqliteSaver.from_conn_string(str(checkpoint)) as saver, TemporaryDirectory(prefix="foreman-skills-") as scratch:
        saver.serde = checkpoint_serializer()
        prior = saver.get(config)
        channels = prior["channel_values"] if prior else {}
        if prior is not None:
            require_schema_version(channels, checkpoint)
        delivery_config = load_config()
        if prior and "token_cap" in channels and max_tokens is not None and channels["token_cap"] != max_tokens:
            raise ValueError("--max-tokens differs from the stored token cap")
        if prior and "max_budget_usd" in channels:
            require_same_budget(channels["max_budget_usd"], max_budget_usd)
        if prior and (not channels.get("stop") or channels.get("halt") in RESUMABLE_HALTS):
            require_run_limits(channels, checkpoint, max_budget_usd)
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
                                          "skill_reason": exc.reason, "pstack_pin": pin.model_dump(), "pending_dispatch": None,
                                          "fact_record": fact_record or channels.get("fact_record", MANIFEST_PATH),
                                          "issue": issue, "workspace": workspace, "repo": repo, "check": check,
                                          "max_budget_usd": channels.get("max_budget_usd") if prior else (usd_text(max_budget_usd) if max_budget_usd is not None else None),
                                          "token_cap": channels.get("token_cap", max_tokens if max_tokens is not None else DEFAULT_TOKEN_CAP)}
            if prior and (not channels.get("stop") or channels.get("halt") in RESUMABLE_HALTS):
                early_result = read_delivery_state(skill_halt)
            else:
                graph.update_state(config, skill_halt, as_node="done")
        def invoke(
            graph_input: DeliveryState | Command[str] | None,
            state: DeliveryState,
            invocation_config: RunnableConfig = config,
        ) -> None:
            environment = EnvironmentProbe(run.workspace, state.get("preflight", ""), run.check_log)
            for part in graph.stream(
                graph_input, invocation_config, context=replace(run, environment=environment),
                durability="sync", stream_mode="updates", version="v2",
            ):
                if presentation is not None:
                    for node, raw in part["data"].items():
                        if not node.startswith("__") and raw is not None:
                            presentation.updates.append(_DELIVERY_UPDATE.validate_python(raw, strict=True))

        saved = graph.get_state(config)
        if saved.values:
            require_run_limits(saved.values, checkpoint, max_budget_usd)
        saved_values = checkpoint_state(saved.values)
        if presentation is not None and saved.values:
            presentation.restore(saved)
        if saved_values is not None and fact_record is not None and fact_record != saved_values.get("fact_record", MANIFEST_PATH):
            raise ValueError("--fact-record differs from the checkpoint.")
        saved_workspace = saved_values.get("workspace") if saved_values else None
        if saved_workspace and saved_workspace != workspace:
            raise ValueError(f"Run this issue from {saved_workspace}.")
        waiting_for_quota = bool(saved.interrupts) and saved_values is not None and saved_values.get("halt") == Halt.quota_exhausted
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
        elif saved_values is not None and (pending := saved_values.get("pending_dispatch")):
            assert "dispatch_plan" in saved_values
            stage = pending["stage"]
            models = saved_values["dispatch_plan"]["models"]
            model = models[pending["index"]] if models else None
            graph.update_state(config, {**halted(Halt.usage_unknown, unknown_usage(stage, model), stage),
                                       "attempts": [Attempt(stage=stage, model=model, commit=pin.commit,
                                                            methods=saved_values["dispatch_plan"]["methods"], outcome="interrupted", usage=None).record()],
                                       "pending_dispatch": None}, as_node=stage + "_call")
            invoke(None, saved_values)
        elif answer is not None and saved.interrupts and saved_values is not None:
            if parse_output(HumanPrompt, saved.interrupts[0].value).stage == "pr_size":
                try:
                    answer = PullRequestSizeDecision(answer).value
                except ValueError as exc:
                    raise AnswerError("Answer split or override at the pull request size prompt.") from exc
            invoke(Command(resume=answer), saved_values)
        elif saved_values is None:
            loaded = client.load_issue(repo, issue)
            text = request if request is not None else loaded["title"] + ("\n\n" + loaded["body"] if loaded["body"] else "")
            initial: DeliveryState = {
                "schema_version": SCHEMA_VERSION,
                "fact_record": fact_record or MANIFEST_PATH,
                "repo": repo,
                "token_cap": max_tokens if max_tokens is not None else DEFAULT_TOKEN_CAP,
                "max_budget_usd": usd_text(max_budget_usd) if max_budget_usd is not None else None,
                "issue": issue,
                "workspace": workspace,
                "request": text,
                "check": check,
                "pstack_pin": pin.model_dump(),
                "path": [],
                "preflight": preflight,
                "issue_kind": "slice" if "slice" in loaded["labels"] else "parent",
                "review_rounds": 0,
            }
            if initial["issue_kind"] == "slice":
                try:
                    item = _load_slice(loaded["body"])
                except (InvalidOutput, json.JSONDecodeError) as exc:
                    initial.update(halted(Halt.missing_slice, str(exc), None))
                else:
                    if detail := _blocker_detail(client, repo, issue, item):
                        initial.update(path=["done"], stop="blocked", halt_detail=detail)
                    initial["slice"] = item.record()
                    if item.work_kind is not None:
                        initial["work_kind"] = item.work_kind
            if initial.get("stop") == "blocked":
                early_result = read_delivery_state(initial)
            else:
                invoke(initial, initial)
        elif waiting_for_quota:
            invoke(Command(resume=True), saved_values)
        elif saved_values.get("halt") in RESUMABLE_HALTS:
            repaired = False
            repair_head: str | None = None
            if HALTS[Halt(saved_values.get("halt"))].recovery == "recheck_merge" and not saved_values.get("merge", MergeState()).receipt and (head := saved_values.get("verified_head")) and (branch := saved_values.get("work", WorkState()).branch):
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
            before = _checkpoint_before_halt(graph, config, saved_values["path"], "prepare" if repaired else None)
            require_schema_version(before.values, checkpoint)
            require_run_limits(before.values, checkpoint, max_budget_usd)
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
            prior_work = restored.get("work", WorkState())
            reset_work = repaired or before.next == ("prepare",)
            work = (WorkState(branch=prior_work.branch, base=prior_work.base, repair_head=repair_head)
                    if reset_work else prior_work.model_copy(update={"repair_head": None}))
            if not reset_work and (partial := saved_values.get("work", WorkState()).partial_edits):
                work = work.model_copy(update={"partial_edits": partial})
            retained: DeliveryUpdate = {"work": work, "merge": MergeState(receipt=receipt)}
            if halted_attempts := attempts[len(previous_attempts):]:
                retained["attempts"] = halted_attempts
            if retained:
                replay = graph.update_state(replay, retained)
                restored = checkpoint_state(graph.get_state(replay).values)
                assert restored is not None
            invoke(None, restored, replay)
        elif saved.next and not saved.interrupts:
            invoke(None, saved_values)

        saved = graph.get_state(config)
        result = early_result if early_result is not None else snapshot_result(saved)
        if result.get("stop") == "sliced":
            saved_values = checkpoint_state(saved.values)
            assert saved_values is not None
            if "slices" not in saved_values:
                raise ValueError("Sliced checkpoint has no slice plan.")
            numbers = list(saved_values.get("child_issue_numbers", []))
            work_kind = saved_values.get("work_kind")
            plan = [parse_output(StoredSlice, {**raw, "work_kind": work_kind}) for raw in saved_values["slices"]]
            siblings = {item.title: number for item, number in zip(plan, numbers)}
            for item in plan[len(numbers):]:
                child = item.model_copy(update={"blocker_numbers": [siblings[title] for title in item.depends_on]})
                labels = ["slice"] + ([] if item.depends_on else ["factory"])
                number = client.create_issue(repo, item.title, "", labels, child=child)
                numbers.append(number)
                siblings[item.title] = number
                graph.update_state(config, {"child_issue_numbers": numbers})
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

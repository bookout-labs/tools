"""The delivery graph. Edges live here. Models live in models.toml."""

from dataclasses import replace
from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from typing import Annotated, Callable, TypedDict
import operator
import re

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from foreman.config import Config, ConfigError, family, load_config, order_for
from foreman.dispatch import Attempt, CliMissing, FailoverDispatcher, InvalidOutput, QuotaExhausted
from foreman.proof import browser_proof_label, formal_proof_label
from foreman.router import choose_order
from foreman.save import SaveConfig, SaveError, SaveRequest, publish_reviewed
from foreman.watcher import Proposal, watch_run

GRAPH_NAME = "delivery"
PACKAGE = Path(__file__).parent
REQUIRED_NODES = ("triage", "research", "spec", "implement", "verify", "review")
STAGE_ORDER = REQUIRED_NODES + ("merge",)
SKILLS_FOR = {
    "review": ("review-quality", "merge-risk"),
}
TRIAGE_OUTCOMES = {"implement", "spec", "question", "needs_human"}
SPEC_GATES = {"ready", "needs_human"}
REVIEW_OUTCOMES = {"approve", "changes"}
VERIFY_OUTCOMES = {"pass", "fail"}
RISK_FIELDS = ("alignment", "impact", "verification", "rollback", "error_detection")
RISK_LEVELS = {"low", "medium", "high"}
TEXT_FIELDS = {"implement": "implementation", "research": "research"}
FULL_SHA = re.compile(r"[0-9a-fA-F]{40}")


class SliceProperty(TypedDict):
    name: str
    constraint: str


class Slice(TypedDict):
    title: str
    acceptance: str
    depends_on: list[str]
    screen: str
    properties: list[SliceProperty]


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
    slices: list[Slice]
    child_issue_numbers: list[int]
    slice: Slice
    issue_kind: str
    research: str
    implementation: str
    head_sha: str | None
    head_checked: bool
    merge_armed: bool
    merge_args: list[str]
    browser_proof: dict | None
    formal_proof: dict | None
    proof_labels: dict[str, str]
    human_question: str
    human_answers: list[str]
    review: str
    risk: dict[str, str]
    verify: str
    review_rounds: int
    models_used: dict[str, str]
    implement_models: list[str]
    model_choices: Annotated[list[dict], operator.add]
    attempts: Annotated[list[Attempt], operator.add]
    proposal: Proposal | None
    labels: list[str]
    files: list[str]
    halt: str
    halt_detail: str
    save_report: str
    stop: str
    path: Annotated[list[str], operator.add]


def stage_text(name: str) -> str:
    path = PACKAGE / "stages" / f"{name}.md"
    text = path.read_text().strip()
    for skill in SKILLS_FOR.get(name, ()):
        skill_text = (PACKAGE / "skills" / f"{skill}.md").read_text().strip()
        text = f"{text}\n\n{skill_text}"
    return text


def required_text(value: object, detail: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidOutput(detail)
    return value


def stage_document(value: object, name: str) -> str:
    """The graph stores one document for a text stage.

    A string is that document. An object of strings is the same document split
    into sections. Lists, nested values, and blank sections are not a document.
    """
    detail = f"{name} did not return text"
    if isinstance(value, str):
        return value
    if not isinstance(value, dict) or not value:
        raise InvalidOutput(detail)
    parts: list[str] = []
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str) or not item.strip():
            raise InvalidOutput(detail)
        parts.append(f"{key}\n{item.strip()}")
    return "\n\n".join(parts)


def choice(value: object, options: set[str], field: str) -> str:
    if not isinstance(value, str) or value not in options:
        raise InvalidOutput(f"{field} must be one of {', '.join(sorted(options))}")
    return value


def clean_risk(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise InvalidOutput("review did not report merge risk")
    risk = {
        field: required_text(raw.get(field), f"review risk is missing {field}").strip()
        for field in RISK_FIELDS
    }
    risk["level"] = choice(raw.get("level"), RISK_LEVELS, "review risk level")
    return risk


def clean_slices(raw: object, *, allow_external_dependencies: bool = False) -> list[Slice]:
    if not isinstance(raw, list) or not raw:
        raise InvalidOutput("spec did not return slices")
    cleaned: list[Slice] = []
    for item in raw:
        if not isinstance(item, dict) or item.keys() != Slice.__required_keys__:
            raise InvalidOutput("slice needs exactly title, acceptance, depends_on, screen, and properties")
        title = required_text(item["title"], "slice needs a title").strip()
        acceptance = required_text(item["acceptance"], "slice needs acceptance").strip()
        depends = item["depends_on"]
        if not isinstance(depends, list):
            raise InvalidOutput("slice dependencies must be a list of titles")
        screen = item["screen"]
        if not isinstance(screen, str) or (
            screen and (".." in screen or not re.fullmatch(r"docs/screens/[^/\\\x00-\x1f]+\.md", screen))
        ):
            raise InvalidOutput("slice screen must be empty or a docs/screens/<file>.md path without '..'")
        properties = item["properties"]
        if not isinstance(properties, list):
            raise InvalidOutput("slice properties must be a list")
        cleaned_properties: list[SliceProperty] = []
        for prop in properties:
            if not isinstance(prop, dict) or prop.keys() != SliceProperty.__required_keys__:
                raise InvalidOutput("slice property needs exactly name and constraint")
            cleaned_properties.append({
                "name": required_text(prop["name"], "slice property needs a name").strip(),
                "constraint": required_text(prop["constraint"], "slice property needs a constraint").strip(),
            })
        cleaned.append({
            "title": title,
            "acceptance": acceptance,
            "depends_on": [required_text(dep, "slice dependency needs a title").strip() for dep in depends],
            "screen": screen,
            "properties": cleaned_properties,
        })
    graph = {item["title"]: item["depends_on"] for item in cleaned}
    if len(graph) != len(cleaned):
        raise InvalidOutput("slice titles must be unique")
    for deps in graph.values():
        for dep in deps:
            if dep not in graph and not allow_external_dependencies:
                raise InvalidOutput(f"unknown dependency {dep}")
    try:
        TopologicalSorter(graph).prepare()
    except CycleError as exc:
        raise InvalidOutput("slice dependencies cycle") from exc
    return cleaned


def read_output(name: str, raw: dict, state: DeliveryState) -> dict:
    update: dict = {"path": [name]}
    if name == "triage":
        update["triage"] = choice(raw.get("triage"), TRIAGE_OUTCOMES, "triage")
    elif name == "spec":
        gate = choice(raw.get("spec_gate"), SPEC_GATES, "spec gate")
        update["spec"] = required_text(stage_document(raw.get("spec"), "spec"), "spec did not return text")
        update["spec_gate"] = gate
        if state.get("issue_kind") == "parent" and gate == "ready":
            update["slices"] = clean_slices(raw.get("slices"))
    elif name in TEXT_FIELDS:
        key = TEXT_FIELDS[name]
        update[key] = required_text(stage_document(raw.get(key), name), f"{name} did not return text")
        if name == "implement":
            head = raw.get("head_sha")
            update["head_sha"] = (head.strip() or None) if isinstance(head, str) else None
            for field in ("browser_proof", "formal_proof"):
                artifact = raw.get(field)
                update[field] = artifact if isinstance(artifact, dict) else None
    elif name == "verify":
        verdict = choice(raw.get("verify"), VERIFY_OUTCOMES, "verify")
        update["verify"] = verdict
        if verdict == "fail":
            update["review_rounds"] = state.get("review_rounds", 0) + 1
    elif name == "review":
        verdict = choice(raw.get("review"), REVIEW_OUTCOMES, "review")
        update["review"] = verdict
        update["risk"] = clean_risk(raw.get("risk"))
        if verdict == "changes":
            update["review_rounds"] = state.get("review_rounds", 0) + 1
    if name in {"triage", "spec"}:
        question = raw.get("human_question")
        update["human_question"] = question if isinstance(question, str) else ""
        ready = update.get("triage") == "implement" or update.get("spec_gate") == "ready"
        if ready and state.get("issue_kind") == "slice" and "slice" not in state and "slice" in raw:
            update["slice"] = clean_slices([raw["slice"]])[0]
    model = raw.get("model")
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


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def _matching_work_id(config: SaveConfig, text: str) -> str:
    if not config.work_id_pattern:
        return ""
    try:
        pattern = re.compile(config.work_id_pattern)
    except re.error as exc:
        raise SaveError("save work_id_pattern is not a valid regex.") from exc
    for token in text.split():
        if pattern.fullmatch(token):
            return token
    return ""


def _publish_request(state: DeliveryState, config: SaveConfig) -> SaveRequest:
    raw_slice = state.get("slice")
    title = ""
    acceptance = ""
    if isinstance(raw_slice, dict):
        if isinstance(raw_slice.get("title"), str):
            title = raw_slice["title"].strip()
        if isinstance(raw_slice.get("acceptance"), str):
            acceptance = raw_slice["acceptance"]
    message = title or _first_line(state.get("request") or "")
    proof = " ".join(acceptance.split()) if config.proof == "required" else ""
    blob = " ".join(part for part in (state.get("request") or "", title, acceptance) if part)
    number = state.get("issue")
    issue = str(number) if isinstance(number, int) and number > 0 else None
    return SaveRequest(work=_matching_work_id(config, blob), message=message, proof=proof, issue=issue)


def compile_delivery(
    dispatcher: FailoverDispatcher | None,
    config: Config | None = None,
    *,
    checkpointer: BaseCheckpointSaver | None = None,
    run_check: Callable[[str], int] | None = None,
    run_gh: Callable[[list[str]], int] | None = None,
    inspect_worktree: Callable[[], tuple[str, bool]] | None = None,
    resolve: Callable[[str, str], str] | None = None,
    save_config: SaveConfig | None = None,
    save_run: Callable | None = None,
    commit_worktree: Callable[[str], str] | None = None,
    read_merge_hold: Callable[[str], str | None] | None = None,
):
    config = config or load_config()
    missing = [name for name in REQUIRED_NODES if name not in config.nodes]
    if missing:
        raise ValueError(f"delivery config is missing {', '.join(missing)}")
    prompts = {name: stage_text(name) for name in REQUIRED_NODES}
    identity = lambda route, _effort: route

    def bind(name: str):
        def node(state: DeliveryState) -> dict:
            if dispatcher is None:
                return {"path": [name], "halt": "no_dispatcher"}
            try:
                models = order_for(name, config, state.get("models_used") or {}, state.get("implement_models"))
            except ConfigError as exc:
                return {"path": [name], "halt": "no_reviewer", "halt_detail": str(exc)}
            ordered, reason, inputs = choose_order(name, models, state)
            recorded: list[Attempt] = []
            resolved_pairs: list[tuple[str, str]] = []
            resolver = resolve or identity
            for route in ordered:
                try:
                    slug = resolver(route, config.effort)
                except ConfigError as exc:
                    msg = str(exc)
                    if "CLI is not installed" in msg:
                        recorded.append({"node": name, "model": route, "status": "cli_missing", "detail": msg})
                        continue
                    recorded.append({"node": name, "model": route, "status": "stopped", "detail": msg})
                    halt = "no_reviewer" if name == "review" else "invalid_output"
                    return {"path": [name], "attempts": recorded, "halt": halt, "halt_detail": msg}
                resolved_pairs.append((route, slug))
            if ordered and not resolved_pairs:
                detail = recorded[-1]["detail"] if recorded else "CLI is not installed"
                if name == "review":
                    return {
                        "path": [name],
                        "attempts": recorded,
                        "halt": "no_reviewer",
                        "halt_detail": f"review CLI missing: {detail}",
                    }
                return {"path": [name], "attempts": recorded, "halt": "cli_missing", "halt_detail": detail}
            resolved = [slug for _, slug in resolved_pairs]
            instruction = prompts[name]
            if name == "implement" and (slice := state.get("slice")):
                for field, skill in (("screen", "browser-proof"), ("properties", "formal-proof")):
                    if slice[field]:
                        proof = (PACKAGE / "skills" / f"{skill}.md").read_text().strip()
                        instruction = f"{instruction}\n\n{proof}"
            if name == "verify":
                command = state.get("check") or "(none)"
                instruction = f"{instruction}\n\nCommand: {command}"
            call_state = dict(state)
            call_state["effort"] = config.effort
            raw = None
            had_cli_missing = any(a["status"] == "cli_missing" for a in recorded)
            try:
                raw = dispatcher.run(name, resolved, call_state, instruction, attempts=recorded)
                update = read_output(name, raw, state)
                update["attempts"] = recorded
                if name == "implement" and commit_worktree is not None:
                    request = state.get("request")
                    title = request.strip().splitlines()[0] if isinstance(request, str) and request.strip() else ""
                    try:
                        update["head_sha"] = commit_worktree(title)
                        update["head_checked"] = False
                    except InvalidOutput as exc:
                        if recorded and recorded[-1]["status"] == "ran":
                            recorded[-1] = {
                                "node": name,
                                "model": recorded[-1]["model"],
                                "status": "stopped",
                                "detail": str(exc),
                            }
                        halted = {
                            "path": [name], "attempts": recorded,
                            "halt": "invalid_output", "halt_detail": str(exc),
                        }
                        model = raw.get("model") if isinstance(raw, dict) else None
                        if isinstance(model, str) and model:
                            halted["models_used"] = {**(state.get("models_used") or {}), name: model}
                        return halted
                elif name == "implement" and inspect_worktree is not None:
                    update.update(head_sha=None, head_checked=False)
                used = raw.get("model")
                if used:
                    route = resolved_pairs[0][0]
                    for route_name, slug in resolved_pairs:
                        if slug == used:
                            route = route_name
                            break
                    if resolved and used != resolved[0]:
                        non_ran = [a["status"] for a in recorded if a["status"] != "ran"]
                        choice_reason = (
                            "cli_missing fallback"
                            if non_ran and all(status == "cli_missing" for status in non_ran)
                            else "quota fallback"
                        )
                    elif had_cli_missing:
                        choice_reason = "cli_missing fallback"
                    else:
                        choice_reason = reason
                    record = {
                        "node": name,
                        "inputs": inputs,
                        "route": route,
                        "family": family(used),
                        "model": used,
                        "reason": choice_reason,
                        "outcome": "ran",
                    }
                    usage = raw.get("usage")
                    if isinstance(usage, dict):
                        record["tokens"] = usage
                    update["model_choices"] = [record]
                return update
            except InvalidOutput as exc:
                if recorded and recorded[-1]["status"] == "ran":
                    recorded[-1] = {
                        "node": name,
                        "model": recorded[-1]["model"],
                        "status": "stopped",
                        "detail": str(exc),
                    }
                halted = {"path": [name], "attempts": recorded, "halt": "invalid_output", "halt_detail": str(exc)}
                model = raw.get("model") if isinstance(raw, dict) else (recorded[-1]["model"] if recorded else None)
                if isinstance(model, str) and model:
                    halted["models_used"] = {**(state.get("models_used") or {}), name: model}
                return halted
            except QuotaExhausted as exc:
                if name == "review":
                    return {
                        "path": [name],
                        "attempts": recorded,
                        "halt": "no_reviewer",
                        "halt_detail": f"review quota exhausted: {exc}",
                    }
                return {
                    "path": [name],
                    "attempts": recorded,
                    "halt": "quota_exhausted",
                    "halt_detail": f"{name} quota exhausted: {exc}",
                }
            except CliMissing as exc:
                if name == "review":
                    return {
                        "path": [name],
                        "attempts": recorded,
                        "halt": "no_reviewer",
                        "halt_detail": f"review CLI missing: {exc}",
                    }
                return {
                    "path": [name],
                    "attempts": recorded,
                    "halt": "cli_missing",
                    "halt_detail": str(exc),
                }

        return node

    def verify(state: DeliveryState) -> dict:
        assert run_check is not None
        checked = {}
        if inspect_worktree is not None:
            head, clean = inspect_worktree()
            if not clean or not FULL_SHA.fullmatch(head):
                return {"path": ["verify"], "halt": "dirty_worktree"}
            checked = {"head_sha": head, "head_checked": True}
        exit_code = run_check(state["check"])
        verdict = "pass" if exit_code == 0 else "fail"
        return {
            **read_output("verify", {"verify": verdict}, state),
            "check_exit_code": exit_code,
            "proof_labels": {},
            **(checked if exit_code == 0 else {}),
        }

    def prove(state: DeliveryState) -> dict:
        slice = state["slice"]
        labels = {
            "browser": browser_proof_label(slice, state.get("browser_proof")),
            "formal": formal_proof_label(slice, state.get("formal_proof")),
        }
        update = {"path": ["proof"], "proof_labels": {key: label for key, label in labels.items() if label}}
        if "insufficient_evidence" in labels.values():
            update["halt"] = "insufficient_evidence"
        elif "block" in labels.values():
            update["review_rounds"] = state.get("review_rounds", 0) + 1
            if update["review_rounds"] >= config.max_review_rounds:
                update["halt"] = "review_cap"
        return update

    def needs_human(state: DeliveryState) -> dict:
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
        if decision == "implement":
            return "implement"
        if state.get("issue_kind") == "parent":
            return "spec"
        if decision == "question":
            return "research"
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
            return "save" if save_config is not None else "merge"
        if state.get("review_rounds", 0) < config.max_review_rounds:
            return "implement"
        return "done"

    def after_save(state: DeliveryState) -> str:
        return "done" if state.get("halt") else "merge"

    def after_verify(state: DeliveryState) -> str:
        if state.get("halt"):
            return "done"
        if state.get("verify") == "pass":
            slice = state.get("slice")
            if slice and (slice["screen"] or slice["properties"]):
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

    def arm_merge(state: DeliveryState) -> dict:
        head = state.get("head_sha")
        args = ["gh", "pr", "merge", "--auto", "--match-head-commit", head] if head and FULL_SHA.fullmatch(head) else None
        if read_merge_hold is not None:
            hold = read_merge_hold(state["repo"])
            if isinstance(hold, str) and hold.strip():
                halted: dict = {
                    "merge_armed": False,
                    "halt": "merge_held",
                    "halt_detail": hold.strip(),
                    "path": ["merge"],
                }
                if args is not None:
                    halted["merge_args"] = args
                return halted
        if args is None:
            return {"merge_armed": False, "path": ["merge"]}
        if inspect_worktree is not None:
            current, clean = inspect_worktree()
            if not state.get("head_checked") or not clean or current != head:
                return {"merge_armed": False, "merge_args": args, "path": ["merge"]}
        armed = run_gh(args) == 0 if run_gh is not None else run_check is None
        return {"merge_armed": armed, "merge_args": args, "path": ["merge"]}

    def save_node(state: DeliveryState) -> dict:
        if save_config is None or save_run is None:
            return {"path": ["save"], "halt": "save_failed", "halt_detail": "This run has no save config."}
        try:
            request = _publish_request(state, save_config)
            # The merge node arms match-head-commit. This step only publishes.
            published = publish_reviewed(
                replace(save_config, queue=False),
                request,
                save_run,
                expected_sha=state.get("head_sha"),
            )
        except SaveError as exc:
            return {"path": ["save"], "halt": "save_failed", "halt_detail": str(exc)}
        return {
            "path": ["save"],
            "head_sha": published.sha,
            "head_checked": True,
            "save_report": published.report,
        }

    def finish(state: DeliveryState) -> dict:
        stop = stop_reason(state)
        return {"stop": stop, "proposal": watch_run(state, stop), "path": ["done"]}

    builder = StateGraph(DeliveryState)
    for name in REQUIRED_NODES:
        builder.add_node(name, verify if name == "verify" and run_check is not None else bind(name))
    builder.add_node("proof", prove)
    builder.add_node("needs_human", needs_human)
    builder.add_node("save", save_node)
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
    builder.add_conditional_edges("implement", after_implement, {"verify": "verify", "done": "done"})
    builder.add_conditional_edges(
        "review",
        after_review,
        {"save": "save", "merge": "merge", "implement": "implement", "done": "done"},
    )
    builder.add_conditional_edges("save", after_save, {"merge": "merge", "done": "done"})
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
    issue_kind: str = "slice",
    labels: list[str] | None = None,
    files: list[str] | None = None,
    resolve: Callable[[str, str], str] | None = None,
    save_config: SaveConfig | None = None,
    save_run: Callable | None = None,
    read_merge_hold: Callable[[str], str | None] | None = None,
) -> dict:
    if issue_kind not in {"parent", "slice"}:
        raise ValueError(f"unknown issue kind {issue_kind}")
    graph = compile_delivery(
        dispatcher, config, resolve=resolve, save_config=save_config, save_run=save_run,
        read_merge_hold=read_merge_hold,
    )
    initial: dict = {
        "repo": repo,
        "request": request,
        "check": check,
        "issue_kind": issue_kind,
        "review_rounds": 0,
        "models_used": {},
        "model_choices": [],
        "attempts": [],
        "path": [],
    }
    if labels is not None:
        initial["labels"] = list(labels)
    if files is not None:
        initial["files"] = list(files)
    return graph.invoke(initial)


def diagram(dispatcher: FailoverDispatcher | None = None, config: Config | None = None) -> str:
    if dispatcher is None:
        dispatcher = FailoverDispatcher(lambda *args: {"triage": "implement"})
    graph = compile_delivery(dispatcher, config)
    return graph.get_graph().draw_mermaid()

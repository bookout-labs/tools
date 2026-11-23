"""Spec: clear one repository-bound candidate with an independent check, then build it or file its children."""

from collections.abc import Callable
from typing import TypeVar

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from foreman.config import family
from foreman.contracts import InvalidOutput
from foreman.dispatch import DISPATCH
from foreman.github import READY_LABEL, ForgeError, human_comments, render_slice_body
from foreman.outcomes import Halt, stopped
from foreman.spec_check import SpecHistory
from foreman.stage_output import (
    CandidateSlice, InvalidSlicePlan, SpecAuthor, SpecCandidate, StoredSlice, WorkKind, load_stored_slice, parse_output, spec_digest,
)
from foreman.stages import clean_slices, reject_candidate
from foreman.state import DeliveryState, DeliveryUpdate
from foreman.steps import StageInput, StageRun
from foreman.workspace import WorkspaceError

HALTS = [Halt.invalid_output, Halt.spec_cap, Halt.git_failed, Halt.publication_unreconciled]
Observed = TypeVar("Observed")


def draft(state: DeliveryState, run: StageRun) -> DeliveryUpdate:
    plan = state["plan"]
    if plan.spec_candidate is not None and plan.spec_candidate["revision"] not in plan.rejected_revisions:
        # A resumed stage keeps the candidate it already drafted.
        return {}
    target = state["run"].target()
    update, _ = run.consult(DISPATCH, "spec", state, lambda plan: [target, plan.spec, plan.work_kind, plan.slice, plan.slices])
    return update


def validate(state: DeliveryState, run: StageRun) -> DeliveryUpdate:
    plan = state["plan"]
    if plan.spec_candidate is not None or plan.spec_rejection:
        # Only a new draft replaces the validated candidate or repairs a rejected one.
        return {}
    target = state["run"].target()
    repo, base = target["repo"], target["base"]
    supplied = plan.slice is not None and not plan.spec_gate
    pending = [plan.slice] if supplied and plan.slice is not None else plan.slices
    if any((scope := item.get("repository")) and scope.lower() != repo for item in pending):
        return stopped(Halt.invalid_output, "foreign_repository: slice scope differs from the admitted repository.", "spec")

    def prerequisite_seams(item: StoredSlice) -> set[str]:
        """The seams that a filed slice's merged blockers built in the pinned tree."""
        seams: set[str] = set()
        if len(item.depends_on) != len(item.blocker_numbers):
            raise InvalidSlicePlan("dangling_seam", "The filed slice does not match its planned blockers.")
        for title, number in zip(item.depends_on, item.blocker_numbers):
            if not run.forge.is_merged(repo, number, contains=lambda commit: run.workspace.contains_commit(base, commit)):
                raise InvalidSlicePlan("dangling_seam", f"Blocker #{number} has no merge in the pinned tree.")
            issue = run.forge.load_issue(repo, number)
            if issue["repo"].lower() != repo or issue["number"] != number or "slice" not in issue["labels"]:
                raise InvalidSlicePlan("dangling_seam", f"Blocker #{number} is not a slice in the target repository.")
            blocker = load_stored_slice(issue["body"])
            if blocker.title != title or (blocker.repository and blocker.repository.lower() != repo):
                raise InvalidSlicePlan("dangling_seam", f"Blocker #{number} does not match the planned slice {title}.")
            seams.update(seam.name for seam in blocker.builds)
        return seams

    try:
        items = [parse_output(StoredSlice, item) for item in pending]
        filed = supplied and plan.issue_kind == "slice"
        seams = prerequisite_seams(items[0]) if filed else set[str]()
        # A filed slice is checked against the pinned tree too, but keeps its stored record.
        cleaned = clean_slices(items, run.workspace, base, prerequisite_seams=seams)
        slices = pending if filed else cleaned
    except InvalidSlicePlan as exc:
        return reject_candidate(plan, spec_digest([target, plan.spec, plan.work_kind, pending]), str(exc), run.max_rounds, exc.reason)
    except InvalidOutput as exc:
        return stopped(Halt.invalid_output, str(exc), "spec")
    except (ForgeError, WorkspaceError) as exc:
        return stopped(Halt.git_failed, str(exc), "spec")
    stage = "triage" if supplied else "spec"
    model = state.get("models_used", {}).get(stage)
    provenance = pending[0].get("provenance") if supplied else None
    author = SpecAuthor.model_validate(provenance["author"]) if provenance else SpecAuthor(
        stage=stage, model=model or "github", family=family(model) if model else "github")
    kind: WorkKind = plan.work_kind or "feature"
    candidate = SpecCandidate(slices=[CandidateSlice.model_validate({k: v for k, v in item.items() if k != "provenance"}) for item in slices],
                              spec=plan.spec, work_kind=kind, author=author, target=target)
    if candidate.revision in plan.rejected_revisions:
        return reject_candidate(plan, candidate.revision, "The checker already rejected this scope.", run.max_rounds)
    single = plan.slice is not None and len(slices) == 1
    planning = candidate.update(plan if single else plan.model_copy(update={"slice": None}), single=single)
    return {"plan": planning.model_copy(update={"spec_gate": "ready"})}


def check(state: DeliveryState, run: StageRun) -> DeliveryUpdate:
    plan = state["plan"]
    update: DeliveryUpdate = {}
    if not SpecHistory.cleared(plan):
        update, _ = run.consult(DISPATCH, "spec_check", state, lambda plan: [(plan.spec_candidate or {}).get("revision")])
        plan = update.get("plan", plan)
        if update.get("halt") or update.get("stop") or not SpecHistory.cleared(plan):
            return update
    target = state["run"]
    assert plan.spec_candidate is not None and target.issue is not None
    if plan.spec_candidate.get("target") != target.target():
        return {**update, **stopped(Halt.invalid_output, "The checker verdict does not clear the current repository-bound plan.", "spec")}
    revision = plan.spec_candidate["revision"]
    try:
        # The human's recorded stop prevents any build or filing. Their approval is optional and recorded.
        for ruling in human_comments(run.forge, run.human_login, target.repo, target.issue):
            if ruling == f"foreman stop {revision}":
                return {**update, "stop": "cancelled"}
            if ruling in {f"foreman approve {revision}", f"we're good {revision}"}:
                plan = plan.model_copy(update={"human_approval": revision})
    except ForgeError as exc:
        return {**update, **stopped(Halt.git_failed, str(exc), "spec")}
    return {**update, "plan": plan}


def file_children(state: DeliveryState, run: StageRun) -> DeliveryUpdate:
    plan, target = state["plan"], state["run"]
    repo, parent, forge = target.repo, target.issue, run.forge
    assert parent is not None and plan.spec_candidate is not None
    revision = plan.spec_candidate["revision"]
    numbers: dict[str, int] = {}
    children: list[tuple[int, StoredSlice]] = []

    def unreconciled(detail: str) -> DeliveryUpdate:
        return stopped(Halt.publication_unreconciled, detail, "file_children")

    def ensure(observe: Callable[[], Observed], write: Callable[[], object]) -> Observed:
        """Write only when GitHub does not show the result. A failed write may have landed, so GitHub decides."""
        if seen := observe():
            return seen
        try:
            write()
        except ForgeError:
            pass
        return observe()

    try:
        # The candidate lists slices in checked dependency order, so every blocker is filed before its dependents.
        for raw in plan.slices:
            item = parse_output(StoredSlice, {**raw, "work_kind": plan.work_kind})
            child = item.model_copy(update={"blocker_numbers": [numbers[title] for title in item.depends_on]})
            marker = f"<!-- foreman slice {spec_digest([repo.lower(), parent, child.title])} -->"
            if not (found := forge.marked_issues(repo, marker)):
                # GitHub's issue list can lag a fresh write, so a created child is confirmed by its own number below.
                try:
                    found = [forge.create_issue(repo, child.title, "", ["slice"], marker=marker, child=child)]
                except ForgeError:  # A failed write may have landed, so the marker decides.
                    found = forge.marked_issues(repo, marker)
            if len(found) != 1:
                return unreconciled(f"{len(found)} issues carry the marker for {child.title}." if found
                                    else f"GitHub does not show the child issue for {child.title} yet.")
            number = found[0]
            issue = forge.load_issue(repo, number)
            if issue["repo"].lower() != repo.lower() or issue["body"] != render_slice_body(marker, child):
                return unreconciled(f"Issue #{number} carries the marker for {child.title} with a different repository or body.")
            link = ensure(lambda: forge.parent_issue(repo, number), lambda: forge.add_sub_issue(repo, parent, number))
            if link is None or link.number != parent or not link.in_repo(repo):
                return unreconciled(f"GitHub does not show #{number} under parent #{parent}.")
            for blocker in child.blocker_numbers:
                if not ensure(lambda: blocker in {link.number for link in forge.blockers(repo, number) if link.in_repo(repo)},
                              lambda: forge.add_blocker(repo, number, blocker)):
                    return unreconciled(f"GitHub does not show #{blocker} blocking #{number}.")
            numbers[child.title] = number
            children.append((number, child))
        # Runs start only after every child and link is confirmed.
        for number, child in children:
            if all(forge.is_merged(repo, blocker) for blocker in child.blocker_numbers) and not ensure(
                    lambda: READY_LABEL in forge.load_issue(repo, number)["labels"],
                    lambda: forge.add_label(repo, number, READY_LABEL)):
                return unreconciled(f"GitHub does not show #{number} released.")
    except ForgeError as exc:
        return unreconciled(f"GitHub could not confirm the children of #{parent} for {revision} ({exc}).")
    return {"stop": "children_filed"}


def compile_stage(run: StageRun) -> CompiledStateGraph[DeliveryState, None, StageInput, DeliveryState]:
    def ended(state: DeliveryState) -> str | None:
        if halt := state.get("halt"):
            return "exhausted" if halt == Halt.spec_cap else "halted"
        return "cancelled" if state.get("stop") == "cancelled" else None

    def after_validate(state: DeliveryState) -> str:
        if state.get("halt"):
            return "foreign_repository" if state.get("halt_detail", "").startswith("foreign_repository") else "exhausted"
        return "valid" if state["plan"].spec_candidate is not None else "repair"

    def after_check(state: DeliveryState) -> str:
        if stop := ended(state):
            return stop
        plan = state["plan"]
        if not SpecHistory.cleared(plan):
            return "changes"
        return "validated_single" if plan.slice is not None else "validated_parent"

    declarations = {"halts": [halt.value for halt in HALTS], "retries": {"plan_revisions": "src/foreman/models.toml#max_review_rounds"}}
    return (StateGraph[DeliveryState, None, StageInput, DeliveryState](DeliveryState, input_schema=StageInput, output_schema=DeliveryState)
            .add_node("draft", run.bind(draft))
            .add_node("validate", run.bind(validate))
            .add_node("check", run.bind(check))
            .add_node("file_children", run.bind(file_children))
            .add_conditional_edges(START, lambda state: state["plan"].triage, {"supplied_slice": "validate", "plan": "draft"})
            .add_conditional_edges("draft", lambda state: "halted" if state.get("halt") else ended(state) or "ready",
                                   {"ready": "validate", "cancelled": END, "halted": END})
            .add_conditional_edges("validate", after_validate,
                                   {"foreign_repository": END, "exhausted": END, "repair": "draft", "valid": "check"})
            .add_conditional_edges("check", after_check, {"exhausted": END, "changes": "draft", "cancelled": END,
                                                          "validated_single": END, "validated_parent": "file_children", "halted": END})
            .add_conditional_edges("file_children", lambda state: "halted" if state.get("halt") else "children_filed",
                                   {"children_filed": END, "halted": END})
            .compile().with_config({"metadata": {"contract_id": "spec", "stage_declarations": declarations}}))

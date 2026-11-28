"""Canonical worker contracts, methods and callbacks. Graph successors stay in delivery."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from graphlib import CycleError, TopologicalSorter
import json
from typing import ClassVar, Literal, TYPE_CHECKING, cast

from foreman.contract import CONTRACT_PATH, parse_contract
from foreman.config import Config, ModelSpec, order_for, contributors_for, family
from foreman.skill_rules import SkillMethod, SkillRule
from foreman.outcomes import Halt, stopped
from foreman.spec_check import SpecHistory, accept as accept_checked
from foreman.stage_output import (
    Assumption, StageOutput, TriageOutput, ResearchOutput, SpecOutput, SpecCheckOutput, SpecCandidate, SpecCitation, SpecRejection, PlanState,
    AcceptanceTestsOutput, ImplementOutput, ReviewOutput, UpdateContext, StoredSlice, SliceRecord, WorkKind,
    InvalidSlicePlan as InvalidSlicePlan, AttemptRecord, ReviewSelection, VerifyOutput, VerifyState,
)

from foreman.contracts import InvalidOutput
from foreman.review_round import QUESTIONS, history, accept
from foreman.workspace import RelatedCheckout, Workspace, ReviewScope, WorktreeInspectionFailed, related_path
from foreman.github import Forge, PullRequest
from foreman.review_context import ReviewContext

if TYPE_CHECKING:
    from foreman.state import DeliveryState, DeliveryUpdate


@dataclass(frozen=True)
class Acceptance:
    state: "DeliveryState"
    model: ModelSpec | str | None = None
    workspace: Workspace | None = None
    config: Config | None = None
    attempt: AttemptRecord | None = None


def check_files(raw: Sequence[StoredSlice], workspace: Workspace, base: str) -> None:
    """A listed path names a tracked regular file at the pinned base, or is absent there when the slice adds it (rules.paths)."""
    for item in raw:
        for path in item.files:
            kind = workspace.tracked_path(base, path)
            if kind is None:
                raise InvalidSlicePlan("file_path", f"{item.title} lists {path}, which is not a relative path without '.' or '..'.")
            if kind not in ("file", "absent"):
                raise InvalidSlicePlan("file_path", f"{item.title} lists {path}, which is a {kind} at {base}, not a regular file.")


def check_proofs(raw: Sequence[StoredSlice], work_kind: WorkKind | None) -> None:
    """A criterion the base already meets, as every criterion of a refactor or perf slice does, is proven by drive."""
    if work_kind not in ("refactoring", "perf_issue"):
        return
    for item in raw:
        for number, criterion in enumerate(item.criteria, 1):
            if criterion.proof.kind != "drive":
                raise InvalidSlicePlan("criterion_proof", f"{item.title} proves criterion {number} by {criterion.proof.kind}, but "
                                       f"the base already meets a {work_kind} slice's criteria, so prove it by drive.")


def check_assumptions(items: Sequence[StoredSlice], assumptions: Sequence[Assumption], related: Sequence[RelatedCheckout]) -> None:
    """Each assumption cites a tracked regular file in a related checkout at its pin (rules.paths), and a contract criterion
    names a related repository and the assumption it proves."""
    pinned = {checkout.repository.lower(): checkout for checkout in related}
    for assumption in assumptions:
        if (citation := assumption.citation) is None:
            raise InvalidSlicePlan("assumption_citation", f"The assumption '{assumption.fact}' cites no file. Cite a tracked file "
                                   "in a related repository checkout, or ask a grounded question instead.")
        if (checkout := pinned.get(citation.repository.lower())) is None:
            raise InvalidSlicePlan("assumption_citation", f"The assumption '{assumption.fact}' cites {citation.repository}, which "
                                   "is not a related repository pinned for this run.")
        if (kind := related_path(checkout, citation.path)) != "file":
            shown = ("not a relative path without '.' or '..'" if kind is None
                     else f"{'absent' if kind == 'absent' else f'a {kind}'} at {checkout.commit}")
            raise InvalidSlicePlan("assumption_citation", f"The assumption '{assumption.fact}' cites {citation.repository} "
                                   f"{citation.path}, which is {shown}, not a tracked regular file.")
    facts = {assumption.fact for assumption in assumptions}
    for item in items:
        for number, criterion in enumerate(item.criteria, 1):
            proof = criterion.proof
            if proof.kind == "contract" and (proof.repository.lower() not in pinned or proof.assumption not in facts):
                raise InvalidSlicePlan("criterion_proof", f"{item.title} proves criterion {number} by contract, so it names a "
                                       "related repository pinned for this run and the plan's assumption it proves.")


def clean_slices(raw: Sequence[StoredSlice] | None, workspace: Workspace, base: str) -> list[SliceRecord]:
    """A candidate's slices in dependency order, each depending only on the titles of its siblings."""
    if not raw:
        raise InvalidOutput("slices: ready parent spec needs at least one slice")
    check_files(raw, workspace, base)
    slices = {item.title: item for item in raw}
    if len(slices) != len(raw):
        raise InvalidSlicePlan("duplicate_title", "Slice titles must be unique.")
    for item in raw:
        if unknown := [title for title in item.depends_on if title == item.title or title not in slices]:
            raise InvalidSlicePlan("unknown_dependency", f"{item.title} depends on {', '.join(unknown)}, which is not another slice of this plan.")
    try:
        ordered = list(TopologicalSorter({item.title: item.depends_on for item in raw}).static_order())
    except CycleError as exc:
        raise InvalidSlicePlan("dependency_cycle", "Dependency cycle: " + " -> ".join(exc.args[1])) from exc
    return [slices[title].record() for title in ordered]


def no_preparation(state: "DeliveryState", workspace: Workspace, forge: Forge) -> tuple[str, PullRequest | None, ReviewScope | None]:
    return "", None, None


def related_checkouts(state: "DeliveryState") -> str:
    """The drawn context's related repository checkouts, for the procedures that name them."""
    related = state.get("related", [])
    if not related:
        return ""
    return ("\n\nRelated repository checkouts, read-only at the commits pinned at admission. Read their files in place:"
            + "".join(f"\n{item.repository} at {item.commit}: {item.checkout}" for item in related))


def pinned_instructions(state: "DeliveryState", workspace: Workspace, forge: Forge) -> tuple[str, PullRequest | None, ReviewScope | None]:
    run = state["run"]
    pinned = (f"\n\ntarget_repository: {run.repo}\ntarget_checkout: {workspace.root.resolve()}\npinned base: {run.base_sha}"
              f"\nRead the committed tree with git ls-tree {run.base_sha} and git show {run.base_sha}:<path>.")
    return pinned + related_checkouts(state), None, None


def accept_output(output: StageOutput, context: Acceptance) -> "DeliveryUpdate":
    state, model = context.state, context.model
    return {**output.update(UpdateContext(state["plan"], state["review"], [], model if isinstance(model, ModelSpec) else None))}


def accept_spec(output: StageOutput, context: Acceptance) -> "DeliveryUpdate":
    state, model, workspace = context.state, context.model, context.workspace
    assert isinstance(output, SpecOutput)
    if workspace is not None:
        for question in output.questions:
            for path in question.checked_paths:
                if workspace.tracked_path(state["run"].base_sha, path) != "file":
                    raise InvalidOutput(f"checked_paths: {path} is not a tracked regular file at the pinned base")
    plan = state["plan"]
    slices = [item.record() for item in output.slices or []]
    if output.spec_gate == "ready" and not slices and plan.slice is not None:
        slices = [plan.slice]
    if output.spec_gate == "ready" and not slices:
        raise InvalidOutput("slices: ready spec needs at least one slice")
    return {**output.update(UpdateContext(plan, state["review"], slices, model if isinstance(model, ModelSpec) else None))}


def reject_candidate(plan: PlanState, revision: str, detail: str, max_rounds: int,
                     reason: SpecRejection | None = None) -> "DeliveryUpdate":
    """Count each rejected candidate once. The final allowed rejection or a repeated one stops planning."""
    repeated = revision in plan.rejected_revisions
    rejected = plan.rejected_revisions if repeated else [*plan.rejected_revisions, revision]
    planning = plan.model_copy(update={"rejected_revisions": rejected, "spec_rejection": reason})
    update: "DeliveryUpdate" = {"plan": planning, "human_question": ""}
    if planning.spec_failures >= max_rounds:
        update.update(stopped(Halt.spec_cap, detail, "spec"))
    elif repeated:
        update.update(stopped(Halt.invalid_output, f"Revision repeats a rejected plan. {detail}", "spec"))
    return update


def accept_spec_check(output: StageOutput, context: Acceptance) -> "DeliveryUpdate":
    assert isinstance(output, SpecCheckOutput) and context.config is not None
    if output.human_question:
        return {"human_question": output.human_question}
    plan = context.state["plan"]
    assert plan.spec_candidate is not None
    candidate = SpecCandidate.from_record(plan.spec_candidate)
    run, workspace = context.state["run"], context.workspace
    related = {checkout.repository.lower(): checkout for checkout in context.state.get("related", [])}

    def cited(citation: SpecCitation) -> bool:
        if isinstance(citation, str):
            return citation in run.request[:context.state.get("issue_text_length", 0)]
        repository, path = citation["repository"].lower(), citation["path"]
        if repository == run.repo.lower():
            return workspace is not None and workspace.tracked_path(run.base_sha, path) == "file"
        return repository in related and related_path(related[repository], path) == "file"

    checked = accept_checked(SpecHistory.model_validate(plan.spec_history), output, candidate, cited=cited)
    planning = plan.model_copy(update={"spec_history": {"rounds": checked.history.rounds, "settled": checked.history.settled}})
    if checked.approval:
        return {"plan": planning, "human_question": ""}
    detail = "\n".join(finding.summary for finding in checked.findings)
    return reject_candidate(planning, candidate.revision, detail, context.config.max_review_rounds)


def accept_implement(output: StageOutput, context: Acceptance) -> "DeliveryUpdate":
    """Edit's own check of the builder's answer (item 12). Only a first attempt with no edits may report no_change.

    A clean tree is the attempt's branch at its parent with nothing uncommitted. An answer this check rejects is invalid, so
    dispatch gives it the one repair turn (item 1)."""
    state, model, workspace = context.state, context.model, context.workspace
    assert isinstance(output, ImplementOutput) and workspace is not None and "work" in state
    # A mutation the builder names equivalent stays out of the next sample, and review rules on the claim (item 15).
    update: DeliveryUpdate = {"implementation": output.implementation, "human_question": output.human_question or ""}
    if output.equivalent_mutations:
        claims = {**state["checks"].equivalent_mutations, **{claim.id: claim.reason for claim in output.equivalent_mutations}}
        update["checks"] = state["checks"].model_copy(update={"equivalent_mutations": claims})
    work, tree = state["work"], workspace.inspect()
    clean = tree.clean and (tree.branch, tree.head) == (work.branch, work.attempt_head)
    first = not state["review"].findings and not state["checks"].verify_failures and work.attempt_head == state["run"].base_sha
    if output.no_change and not clean:
        raise InvalidOutput("no_change: the tree has edits, so leave no_change null.")
    if output.no_change and not first:
        raise InvalidOutput("no_change: a repair cannot report no_change. Address the evidence with edits.")
    if clean and not output.no_change and not output.human_question:
        raise InvalidOutput("no_change: the tree has no edits. Edit within the acceptance, or on a first attempt whose "
                            "acceptance already holds on the pinned base, set no_change to the checks you ran and what they showed.")
    if output.no_change:
        update.update(stop="no_change", halt_detail=output.no_change)
    if model:
        latest = state.get("models_used", {}).get("implement")
        history = state.get("implement_models", [latest] if latest else [])
        update["implement_models"] = [*history, model.name if isinstance(model, ModelSpec) else model]
    return update


def accept_tests(output: StageOutput, context: Acceptance) -> "DeliveryUpdate":
    """The acceptance test writer names the files it wrote in the checkout. Foreman proves them at implement.tests (IM5)."""
    state, workspace = context.state, context.workspace
    assert isinstance(output, AcceptanceTestsOutput) and workspace is not None
    paths = sorted({test.path for test in output.tests})
    for path in paths:
        written = workspace.root / path
        if path.startswith("/") or ".." in path.split("/") or written.is_symlink() or not written.is_file():
            raise InvalidOutput(f"tests: {path} is not a file you wrote in the checkout. Name each test and fixture file by its "
                                "path relative to the repository root.")
    return {"checks": state["checks"].model_copy(update={"acceptance_tests": paths})}


def accept_verify(output: StageOutput, context: Acceptance) -> "DeliveryUpdate":
    assert isinstance(output, VerifyOutput)
    assert "work" in context.state
    head = context.state["work"].published_head
    if not head or output.tested_head != head:
        raise InvalidOutput(f"tested_head {output.tested_head} is not the published head {head}.")
    return {"verify": VerifyState(outcome="driven", tested_head=head, criteria=[item.record() for item in output.criteria])}


def accept_review(output: StageOutput, context: Acceptance) -> "DeliveryUpdate":
    state, model, config = context.state, context.model, context.config
    assert isinstance(output, ReviewOutput) and "dispatch_plan" in state
    target = state["dispatch_plan"]["target"]
    assert target is not None
    review_state = state["review"]
    past = history({"rounds": review_state.review_findings, "settled": review_state.settled_rulings})
    head, base = target["headRefOid"], target["baseRefOid"]
    review_pass = past.next_pass(head, base)
    accepted, replaces = accept(past, output, head, base)
    provenance: ReviewSelection | None = None
    if isinstance(model, ModelSpec) and config is not None:
        contributors, families = contributors_for("review", config, state.get("models_used", {}), state.get("implement_models"))
        same_family = model.family in families
        other_configured = any(ModelSpec.parse(candidate).family not in families for candidate in state["dispatch_plan"]["models"])
        provenance = ReviewSelection(
            reviewer=model.name, contributors=contributors, same_family=same_family,
            reason=("preceding_candidates_unavailable" if other_configured else "no_other_family_configured") if same_family else None,
        )
        accepted.provenance[review_pass] = provenance
    blockers = accepted.blockers
    update: DeliveryUpdate = {
        "review": review_state.model_copy(update={
            "review": "changes" if accepted.severity else "" if output.human_question else "approve",
            "detail": "\n".join(f"{finding.severity} {finding.summary}" for finding in blockers),
            # Each pass extends its round, so the three passes post together as one report.
            "review_findings": [*review_state.review_findings[:-1 if replaces else None], accepted.model_dump(mode="json")],
            "reviewed_head": accepted.reviewed_head,
            "findings": [finding.record() for finding in blockers],
            "review_receipt": None,
        }),
    }
    if output.human_question or "human_question" in state:
        update["human_question"] = output.human_question or ""
    if "halt_detail" in state:
        update["halt_detail"] = ""
    if context.attempt is not None and provenance is not None:
        update["attempts"] = [{**context.attempt, "provenance": provenance.record()}]
    return update


def implement_preparation(state: "DeliveryState", workspace: Workspace, forge: Forge) -> tuple[str, PullRequest | None, ReviewScope | None]:
    """Render the drawn context's exact repository check, granted as written so a wrapped or split run is denied, and the related checkouts implement's scope names."""
    check = state["checks"].check
    granted = (f"\n\nRepository check, granted exactly as written. Run it unchanged, with no wrapper, prefix, suffix or chained command, "
               f"and never run one segment of it alone:\n{check}") if check else ""
    return granted + related_checkouts(state), None, None


def verify_preparation(state: "DeliveryState", workspace: Workspace, forge: Forge) -> tuple[str, PullRequest | None, ReviewScope | None]:
    """The drawn context's related services: each related checkout at its pin and the command the pinned contract starts it with."""
    related = state.get("related", [])
    text = workspace.tracked_file(state["run"].base_sha, CONTRACT_PATH) if related else None
    starts = {item.repository.lower(): item.start for item in parse_contract(text).related_repositories} if text else {}
    services = "".join(f"\n{item.repository} at {item.commit}: {item.checkout}"
                       + (f", started with: {start}" if (start := starts.get(item.repository.lower())) else "") for item in related)
    return ("\n\nRelated services, each started from its checkout at the commit pinned at admission:" + services if related else ""), None, None


def review_preparation(state: "DeliveryState", workspace: Workspace, forge: Forge) -> tuple[str, PullRequest | None, ReviewScope | None]:
    review_state, checks = state["review"], state["checks"]
    run_state = state["run"]
    repo, head, issue = run_state.repo, checks.verified_head, run_state.issue
    worktree = workspace.inspect()
    if not (repo and issue is not None and head and worktree.clean and worktree.branch and worktree.head == head):
        raise WorktreeInspectionFailed("Review requires a clean branch at the verified HEAD.")
    target = forge.review_target(repo, issue, worktree.branch, head)
    diff = workspace.review_scope(target.baseRefOid, head)
    review_pass = history({"rounds": review_state.review_findings}).next_pass(target.headRefOid, target.baseRefOid)
    instruction = f"\n\n{QUESTIONS[review_pass]}"
    if review_pass == "a" and (parent := forge.parent_issue(repo, issue)) is not None and parent.in_repo(repo):
        # Foreman wrote a child issue's text from the plan, so pass (a) also reads the parent issue's text (review's invariants).
        asked = forge.load_issue(repo, parent.number)
        instruction += f"\n\nParent issue #{parent.number} text:\n{asked['title']}\n\n{asked['body']}"
    instruction += f"\n\nRead the full PR diff with this one command in the checkout:\n{diff.command}"
    if workspace.demonstrations is not None:
        instruction += f"\n\nForeman writes a demonstration's test file at its path under {workspace.demonstrations}."
    instruction += "\n\nSettled rulings:\n" + json.dumps(review_state.settled_rulings) + related_checkouts(state)
    return instruction, target, diff


def review_projection(state: "DeliveryState", workspace: Workspace) -> tuple["DeliveryState", ReviewContext | None]:
    assert "dispatch_plan" in state
    review_state, plan = state["review"], state["dispatch_plan"]
    assert plan["target"] is not None
    target = plan["target"]
    worker_state = state.copy()
    if history({"rounds": review_state.review_findings}).next_pass(target["headRefOid"], target["baseRefOid"]) == "a":
        # Pass (a) reads the issue text the run carries, not the plan's account of itself. Only the worker's prompt reads this state.
        worker_state = cast("DeliveryState", {key: value for key, value in worker_state.items() if key != "plan"})
    worker_state["review"] = review_state.model_copy(update={
        "review_findings": [], "settled_rulings": [], "findings": [], "detail": "",
        "review": "", "reviewed_head": "", "review_receipt": None,
    })
    context = workspace.review_context(target["baseRefOid"], target["headRefOid"])
    if not (context.base.changed or context.head.changed):
        raise WorktreeInspectionFailed("Review requires changed paths.")
    return worker_state, context


@dataclass(frozen=True)
class Stage:
    step: ClassVar[str]  # The drawn step whose worker call this contract is.
    prompt: str | None
    output: type[StageOutput] | None
    skills: tuple[SkillRule, ...] = ()
    kind: Literal["model", "dormant"] = "model"
    input_fields: tuple[str, ...] = ()
    prepare: Callable[["DeliveryState", Workspace, Forge], tuple[str, PullRequest | None, ReviewScope | None]] = no_preparation
    accept: Callable[[StageOutput, Acceptance], "DeliveryUpdate"] = accept_output
    project: Callable[["DeliveryState", Workspace], tuple["DeliveryState", ReviewContext | None]] = lambda state, workspace: (state.copy(), None)

    models: Callable[[str, "DeliveryState", Config], list[ModelSpec]] = lambda name, state, config: order_for(
        name, config, state.get("models_used") or {}, state.get("implement_models"))


class TriageStage(Stage):
    step = "triage.route"


class ResearchStage(Stage):
    step = "research.answer"


class SpecStage(Stage):
    step = "spec.draft"


class SpecCheckStage(Stage):
    model_selector = "spec"
    model_dependency = "spec"
    worker = "checker"
    step = "spec.check"


class ImplementStage(Stage):
    step = "implement.edit"


class ImplementTestsStage(Stage):
    model_selector = "verify"
    model_dependency = None
    worker = "verifier"
    step = "implement.tests"


class VerifyStage(Stage):
    step = "verify.drive"


class ReviewStage(Stage):
    step = "review.inspect"


def spec_check_models(name: str, state: "DeliveryState", config: Config) -> list[ModelSpec]:
    models = order_for("spec", config, state.get("models_used", {}))
    candidate = state.get("plan")
    record = candidate.spec_candidate if candidate else None
    latest = state.get("models_used", {}).get("spec")
    author = record["author"]["family"] if record else family(latest) if latest else None
    return sorted(models, key=lambda model: model.family == author)


def spec_check_projection(state: "DeliveryState", workspace: Workspace) -> tuple["DeliveryState", ReviewContext | None]:
    plan = state["plan"]
    assert plan.spec_candidate is not None
    # The checker reads the human's answers for the issue. Only its own question is bound to the candidate it was asked about.
    revision = plan.spec_candidate["revision"]
    answers = [answer for answer in plan.human_qa if answer.get("candidate", revision) == revision]
    settled = SpecHistory.model_validate(plan.spec_history).settled
    return {**state, "plan": plan.model_copy(update={"human_qa": answers, "spec_history": {"settled": settled}}),
            "human_answers": [answer["answer"] for answer in answers]}, None


# The target repository's own skill, written by pstack create-verification-skill.
VERIFY_SKILL = SkillMethod(path="verify-<app>/SKILL.md", kind="target_skill")
DIAGNOSING_BUGS = SkillMethod(path="engineering/diagnosing-bugs/SKILL.md", kind="skill", repository="matt_skills")
STAGES = {
    "triage": TriageStage("triage.md", TriageOutput, prepare=pinned_instructions, input_fields=("run", "plan", "human_answers")),
    "research": ResearchStage("research.md", ResearchOutput, (
        SkillRule(skill=SkillMethod(path="how/SKILL.md", kind="leaf_prompt")),
        SkillRule(skill=SkillMethod(path="why/SKILL.md", kind="skill"), when="bug_fix"),
        SkillRule(skill=DIAGNOSING_BUGS, when="bug_fix"),
        SkillRule(skill=DIAGNOSING_BUGS, when="perf_issue"),
    ), prepare=pinned_instructions, input_fields=("run", "plan", "human_answers")),
    "spec": SpecStage("spec.md", SpecOutput, (
        SkillRule(skill=SkillMethod(path="architect/SKILL.md", kind="skill")),
        SkillRule(skill=SkillMethod(path="statechart/SKILL.md", kind="skill"), when="stateful"),
        SkillRule(skill=SkillMethod(path="engineering/to-tickets/SKILL.md", kind="skill",
                                    repository="matt_skills"), when="parent"),
    ), prepare=pinned_instructions, accept=accept_spec, input_fields=("run", "plan", "human_answers")),
    "spec_check": SpecCheckStage("spec_check.md", SpecCheckOutput, prepare=pinned_instructions, accept=accept_spec_check,
                                 project=spec_check_projection, input_fields=("run", "plan", "human_answers"), models=spec_check_models),
    "implement": ImplementStage("implement.md", ImplementOutput, (
        SkillRule(skill=SkillMethod(path="tdd/SKILL.md", kind="skill"), when="bug_fix"),
        SkillRule(skill=SkillMethod(path="benchmark-checklist/SKILL.md", kind="skill"), when="perf_issue"),
        SkillRule(skill=SkillMethod(path="formal-<area>/SKILL.md", kind="target_skill")),
    ), prepare=implement_preparation, accept=accept_implement, input_fields=("run", "plan", "work", "checks", "review", "verify", "human_answers")),
    "implement_tests": ImplementTestsStage("implement_tests.md", AcceptanceTestsOutput, prepare=pinned_instructions, accept=accept_tests,
                                           input_fields=("run", "plan", "work", "checks"),
                                           # It runs before any build, so no builder's family is there to avoid (workers.verifier).
                                           models=lambda name, state, config: list(config.nodes["verify"].models)),
    "verify": VerifyStage("verify.md", VerifyOutput, (SkillRule(skill=VERIFY_SKILL),), prepare=verify_preparation,
                          accept=accept_verify, input_fields=("run", "plan", "work", "checks")),
    "review": ReviewStage("review.md", ReviewOutput, (
        SkillRule(skill=SkillMethod(path="src/foreman/skills/review-quality.md", kind="repository_skill")),
        SkillRule(skill=SkillMethod(path="blast-radius/SKILL.md", kind="skill")),
    ), prepare=review_preparation, accept=accept_review, project=review_projection,
        input_fields=("run", "plan", "work", "checks", "verify", "review")),
}

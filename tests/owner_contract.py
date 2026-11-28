"""Owner-channel samples and the fields that survive a repair attempt."""

import tests

from dataclasses import dataclass
from pydantic import BaseModel

from foreman.delivery import MODEL_STAGES
from foreman.readiness import Escape, MergeState, ProductionCheckRun
from foreman.state import ChecksState, PlanState, RunState, ReviewState, StartSettings, VerifyState
from foreman.workspace import BaseMerge, WorkState
from foreman.dry_run import CLEAR
from foreman.spec_check import SpecHistory, accept
from test_spec_candidate import CandidateTests


@dataclass(frozen=True)
class OwnerCase:
    name: str
    value: BaseModel
    durable: frozenset[str]
    grouped_at: int
    legacy_keys: dict[str, str]


FINDING = {
    "id": "owner", "kind": "scope", "severity": "P1", "ruling_id": None,
    "location": {"side": "head", "path": "items.txt", "line": 1},
    "summary": "A fixture changed outside scope.",
    "impact": "The parser reads an unintended input.",
    "evidence": "The fixture's path is outside the acceptance.", "demonstration": None, "cites": None,
}


CANDIDATE = CandidateTests().candidate().model_copy(update={"target": {"repo": "a/b", "issue": 8, "base": "a" * 40}})
CHECKED = accept(SpecHistory(), CLEAR, CANDIDATE).history


OWNERS = (
    OwnerCase("merge", MergeState(status="not_armed", receipt={"status": "QUEUED"},
        observation={"outcome": "ready"}, merge_commit_sha="e" * 40, observed_head="c" * 40, command=["gh", "pr", "merge"],
        production_failure="The production check exited 1.", revert_issue=41, regression_issue=42,
        production_check=ProductionCheckRun(command="sh confirm.sh", exit_code=1, at="2026-11-27T00:00:00+00:00"),
        escapes=[Escape(kind="production_check", key="e" * 40, evidence="The production check exited 1.")]),
        frozenset({"receipt"}), 2, {"status": "merge", "receipt": "merge_receipt",
            "observation": "readiness_observation", "observed_head": "merge_observed_head", "command": "merge_command"}),
    OwnerCase("work", WorkState(branch="8-change", base="main", attempt_head="a" * 40, built=True, owned="tree",
        candidate={"tree": "c" * 40},
        head_sha="b" * 40, published_head="b" * 40, repair_head="d" * 40,
        base_merge=BaseMerge(head="e" * 40, unmerged=["items.txt"])), frozenset({"branch", "base"}), 3, {"branch": "delivery_branch", "base": "delivery_base",
            "candidate": "implementation_candidate"}),
    OwnerCase("run", RunState(repo="a/b", issue=8, request="Build it", workspace="/checkout",
        base_branch="main", base_sha="a" * 40, release="b" * 40,
        model_orders=dict.fromkeys(MODEL_STAGES, ("gpt-test", "claude-test"))), frozenset(RunState.model_fields), 4, {}),
    OwnerCase("start", StartSettings(human_login="fixture-owner", max_tokens=100, max_budget_usd="1.5"),
        frozenset(StartSettings.model_fields), 25, {}),
    OwnerCase("checks", ChecksState(verify_failures=2, check="true", verify_skill=".claude/skills/verify-web/SKILL.md",
        test_report=["reports/junit.xml"], base_tests={'["", [], "base", "kept"]': {"passed": 1, "failed": 0, "skipped": 0}}, acceptance_tests=["accept/once.t"],
        accepted_digest="d" * 64, mutation="sh mutate.sh", sample_cap=20, equivalent_mutations={"flip-1": "Same output."},
        mutations=[{"line": "items.py:3", "id": "flip-1", "result": "survived"}], check_exit_code=0, verify="pass", failure_output="stale failure", checked_tree="c" * 40,
        verified_head="b" * 40),
        frozenset({"check", "verify_skill", "test_report", "base_tests", "acceptance_tests", "accepted_digest", "verify_failures", "mutation",
                   "sample_cap", "equivalent_mutations"}), 5, {}),
    OwnerCase("plan", CANDIDATE.update(PlanState(
        rejected_revisions=["b" * 64, "c" * 64],
        spec_history={"rounds": CHECKED.rounds}, triage="supplied_slice", spec_gate="ready",
        issue_kind="parent", work_kind="feature", batch_issues=[101],
        assumptions=[{"fact": "The API lists items.", "citation": {"repository": "a/api", "path": "routes.py"}}],
        human_qa=[{"stage": "spec", "question": "Keep?", "answer": "Yes"}],
        spec_rejection={"kind": "unknown_dependency", "detail": "Missing"}), single=True),
        frozenset(PlanState.model_fields), 6, {}),
    OwnerCase("review", ReviewState(review="changes", review_findings=[{
        "reviewed_head": "b" * 40, "base": "a" * 40, "findings": [FINDING], "provenance": None}],
        reviewed_head="b" * 40, settled_rulings=[{"id": "design:1", "text": "Keep the list"}],
        findings=[FINDING], detail="Review complete",
        review_run_id="review-run", review_receipt={"id": 12, "commit_id": "b" * 40, "state": "COMMENTED"}),
        frozenset(ReviewState.model_fields), 8, {}),
    OwnerCase("verify", VerifyState(outcome="posted", tested_head="b" * 40, criteria=[{
        "criterion": "The page lists items.", "result": "failed", "observation": "The list was empty.",
        "evidence": "captures/items.png"}], stale=True, evidence_url="https://github.com/a/b/pull/7#issuecomment-91"), frozenset(VerifyState.model_fields), 21, {}),
)


COUNTERS = (("check", "checks", "verify_failures", "failed_check_cap"),
            ("spec", "plan", "spec_failures", "spec_cap"),
            ("review", "review", "review_findings", "review_cap"))

def assert_channels(test, values, expected, *, names=None):
    for owner in OWNERS:
        if names is not None and owner.name not in names:
            continue
        with test.subTest(owner=owner.name):
            test.assertIsInstance(values[owner.name], type(owner.value))
            test.assertEqual(values[owner.name], expected[owner.name])


def historical_channels(version):
    values = {"path": ["triage"], "attempts": []}
    if type(version) is not int or version < 9:
        values["review_rounds"] = 2
    for owner in OWNERS:
        if type(version) is int and version >= owner.grouped_at:
            values[owner.name] = owner.value
        else:
            values.update({owner.legacy_keys.get(field, field): value
                           for field, value in owner.value.model_dump(mode="json").items()})
    if type(version) is not int or version < 9:
        for stage, owner, field, _ in COUNTERS:
            if stage == "review":
                continue
            values[field] = 2
            if owner in values:
                values[owner] = values[owner].model_copy(update={field: 0})
    if type(version) is not int or version < 10:
        candidate = {"disposition": "edits", "fingerprint": "tree"}
        if "work" in values:
            work = values["work"].model_dump()
            work["candidate"] = {}
            values["work"] = WorkState.model_construct(**work)
        else:
            values["implementation_candidate"] = candidate
    if version is not None:
        values["schema_version"] = version
    return values

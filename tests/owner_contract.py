"""Owner-channel samples and the fields that survive a repair attempt."""

import tests

from dataclasses import dataclass
from pydantic import BaseModel

from foreman.dry_run import DEFAULT_RISK
from foreman.readiness import MergeState
from foreman.state import ChecksState, PlanState, RunState, ReviewState
from foreman.workspace import WorkState
from test_spec_candidate import CandidateTests, SLICE


@dataclass(frozen=True)
class OwnerCase:
    name: str
    value: BaseModel
    durable: frozenset[str]
    grouped_at: int
    legacy_keys: dict[str, str]
    clear_on_head_change: frozenset[str] = frozenset()
    repair_head_field: str | None = None


OWNERS = (
    OwnerCase("merge", MergeState(status="not_armed", receipt={"status": "QUEUED"},
        observation={"outcome": "ready"}, observed_head="c" * 40, command=["gh", "pr", "merge"]),
        frozenset({"receipt"}), 2, {"status": "merge", "receipt": "merge_receipt",
            "observation": "readiness_observation", "observed_head": "merge_observed_head", "command": "merge_command"},
        clear_on_head_change=frozenset({"receipt"})),
    OwnerCase("work", WorkState(branch="8-change", base="main", attempt_head="a" * 40,
        candidate={"disposition": "adopted", "head": "b" * 40, "tree": "c" * 40, "fingerprint": "tree"},
        partial_edits="partial", head_sha="b" * 40, repair_head="d" * 40), frozenset({"branch", "base"}), 3, {"branch": "delivery_branch", "base": "delivery_base",
            "candidate": "implementation_candidate"}, repair_head_field="repair_head"),
    OwnerCase("run", RunState(repo="a/b", issue=8, request="Build it", workspace="/checkout",
        token_cap=100, max_budget_usd="1.5"), frozenset(RunState.model_fields), 4, {}),
    OwnerCase("checks", ChecksState(verify_failures=2, proof_failures=1, check="true", preflight="env", check_exit_code=0, verify="pass",
        verified_head="b" * 40, browser_proof={"screen": "page", "trace": "trace", "passed": True},
        formal_proof={"ran": True, "typecheck": True, "constraints": [{"constraint": "unique", "rejected": True}]},
        proof_labels={"browser": "block"}), frozenset({"check", "preflight", "verify_failures", "proof_failures"}), 5, {}),
    OwnerCase("plan", CandidateTests().candidate().update(PlanState(spec_failures=2, triage="implement", spec_gate="ready",
        issue_kind="parent", work_kind="feature", child_issue_numbers=[9],
        spec_rejection={"kind": "dangling_seam", "detail": "Missing"},
        pr_split={"plan": "Split", "gate": "ready", "question": "Keep?", "slices": [SLICE]}), single=True),
        frozenset(PlanState.model_fields) - {"pr_split"}, 6, {}),
    OwnerCase("review", ReviewState(review="changes", review_findings=[{
        "round": 1, "reviewed_head": "b" * 40, "findings": [], "resolutions": {},
        "graph_check": None, "coordinator_notes": [], "provenance": None}],
        reviewed_head="b" * 40, settled_rulings=[{"id": "design:1", "text": "Keep the seam"}],
        fact_record="evidence/facts.toml", findings=[{"id": "nit", "kind": "untested-path", "fix": "Add coverage", "ruling_id": None, "file": "items.txt", "line": 1, "severity": "nit", "summary": "Cover the edge"}], detail="Review complete", review_change_kind="nits",
        risk=DEFAULT_RISK,
        review_run_id="review-run", review_receipt={"id": 12, "commit_id": "b" * 40, "state": "COMMENTED"}),
        frozenset({"review_findings", "settled_rulings", "fact_record", "review_run_id", "review_receipt",
                   "reviewed_head", "review", "risk", "findings", "detail", "review_change_kind"}), 7, {},
        clear_on_head_change=frozenset({"review", "reviewed_head", "review_receipt", "risk", "findings", "detail", "review_change_kind"})),

)


COUNTERS = (("verify", "checks", "verify_failures", "verify_cap"),
            ("proof", "checks", "proof_failures", "review_cap"),
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
    if type(version) is not int or version < 8:
        values["review_rounds"] = 2
    for owner in OWNERS:
        if type(version) is int and version >= owner.grouped_at:
            values[owner.name] = owner.value
        else:
            values.update({owner.legacy_keys.get(field, field): value
                           for field, value in owner.value.model_dump().items()})
    if type(version) is not int or version < 8:
        for stage, owner, field, _ in COUNTERS:
            if stage == "review":
                continue
            values[field] = 2
            if owner in values:
                values[owner] = values[owner].model_copy(update={field: 0})
    if version is not None:
        values["schema_version"] = version
    return values


def seed_repair_channels(values, *, head_changed=False):
    for owner in OWNERS:
        fields = (type(owner.value).model_fields.keys() - owner.durable)
        fields |= {field for _, name, field, _ in COUNTERS if name == owner.name and field != "review_findings"}
        if head_changed:
            fields |= owner.clear_on_head_change
        if owner.name == "review":
            fields |= {"settled_rulings", "fact_record"}
        values[owner.name] = values.get(owner.name, type(owner.value)()).model_copy(update=owner.value.model_dump(include=fields))


def assert_repaired(test, before, after, *, repair_head):
    for owner in OWNERS:
        durable = owner.durable - (owner.clear_on_head_change if repair_head is not None else frozenset())
        with test.subTest(owner=owner.name, durable=sorted(durable)):
            defaults = type(owner.value)()
            for field in type(owner.value).model_fields.keys() - durable:
                test.assertNotEqual(getattr(before[owner.name], field), getattr(defaults, field), field)
            required = durable if owner.name == "review" else {field for _, name, field, _ in COUNTERS if name == owner.name}
            for field in required:
                test.assertNotEqual(getattr(before[owner.name], field), getattr(defaults, field), field)
            fields = before[owner.name].model_dump(include=durable)
            if owner.repair_head_field is not None and repair_head is not None:
                fields[owner.repair_head_field] = repair_head
            test.assertEqual(after[owner.name], type(owner.value).model_validate(fields))

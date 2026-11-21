"""Owner-channel samples and the fields that survive a repair attempt."""

import tests

from dataclasses import dataclass
from pydantic import BaseModel

from foreman.readiness import MergeState
from foreman.state import ChecksState, PlanState, RunState
from foreman.workspace import WorkState
from test_spec_candidate import CandidateTests, SLICE


@dataclass(frozen=True)
class OwnerCase:
    name: str
    value: BaseModel
    durable: frozenset[str]


OWNERS = (
    OwnerCase("merge", MergeState(status="not_armed", receipt={"status": "QUEUED"},
        observation={"outcome": "ready"}, observed_head="c" * 40, command=["gh", "pr", "merge"]),
        frozenset({"receipt"})),
    OwnerCase("work", WorkState(branch="8-change", base="main", attempt_head="a" * 40,
        candidate={"disposition": "adopted", "head": "b" * 40, "tree": "c" * 40, "fingerprint": "tree"},
        partial_edits="partial", head_sha="b" * 40, repair_head="d" * 40), frozenset({"branch", "base"})),
    OwnerCase("run", RunState(repo="a/b", issue=8, request="Build it", workspace="/checkout",
        token_cap=100, max_budget_usd="1.5"), frozenset(RunState.model_fields)),
    OwnerCase("checks", ChecksState(check="true", preflight="env", check_exit_code=0, verify="pass",
        verified_head="b" * 40, browser_proof={"screen": "page", "trace": "trace", "passed": True},
        formal_proof={"ran": True, "typecheck": True, "constraints": [{"constraint": "unique", "rejected": True}]},
        proof_labels={"browser": "block"}), frozenset({"check", "preflight"})),
    OwnerCase("plan", CandidateTests().candidate().update(PlanState(triage="implement", spec_gate="ready",
        issue_kind="parent", work_kind="feature", child_issue_numbers=[9],
        spec_rejection={"kind": "dangling_seam", "detail": "Missing"},
        pr_split={"plan": "Split", "gate": "ready", "question": "Keep?", "slices": [SLICE]}), single=True),
        frozenset(PlanState.model_fields) - {"pr_split"}),
)


def assert_channels(test, values, expected):
    for owner in OWNERS:
        with test.subTest(owner=owner.name):
            test.assertIsInstance(values[owner.name], type(owner.value))
            test.assertEqual(values[owner.name], expected[owner.name])


def assert_repaired(test, before, after, *, repair_head):
    head_changed = repair_head is not None
    for owner in OWNERS:
        durable = owner.durable
        if owner.name == "merge" and head_changed:
            durable = frozenset()
        if owner.name == "plan" and not head_changed:
            durable = frozenset(type(owner.value).model_fields)
        if owner.name not in before:
            test.assertEqual(owner.name, "merge")
        fields = before[owner.name].model_dump(include=durable) if owner.name in before else {}
        if owner.name == "work" and head_changed:
            fields["repair_head"] = repair_head
        expected = type(owner.value).model_validate(fields)
        with test.subTest(owner=owner.name, durable=sorted(durable)):
            test.assertEqual(after[owner.name], expected)

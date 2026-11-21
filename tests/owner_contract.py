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
    OwnerCase("checks", ChecksState(check="true", preflight="env", check_exit_code=0, verify="pass",
        verified_head="b" * 40, browser_proof={"screen": "page", "trace": "trace", "passed": True},
        formal_proof={"ran": True, "typecheck": True, "constraints": [{"constraint": "unique", "rejected": True}]},
        proof_labels={"browser": "block"}), frozenset({"check", "preflight"}), 5, {}),
    OwnerCase("plan", CandidateTests().candidate().update(PlanState(triage="implement", spec_gate="ready",
        issue_kind="parent", work_kind="feature", child_issue_numbers=[9],
        spec_rejection={"kind": "dangling_seam", "detail": "Missing"},
        pr_split={"plan": "Split", "gate": "ready", "question": "Keep?", "slices": [SLICE]}), single=True),
        frozenset(PlanState.model_fields) - {"pr_split"}, 6, {}),
)


def assert_channels(test, values, expected, *, names=None):
    for owner in OWNERS:
        if names is not None and owner.name not in names:
            continue
        with test.subTest(owner=owner.name):
            test.assertIsInstance(values[owner.name], type(owner.value))
            test.assertEqual(values[owner.name], expected[owner.name])


def historical_channels(version):
    values = {"path": ["triage"], "attempts": [], "review_rounds": 2}
    for owner in OWNERS:
        if type(version) is int and version >= owner.grouped_at:
            values[owner.name] = owner.value
        else:
            values.update({owner.legacy_keys.get(field, field): value
                           for field, value in owner.value.model_dump().items()})
    if version is not None:
        values["schema_version"] = version
    return values


def assert_repaired(test, before, after, *, repair_head):
    for owner in OWNERS:
        durable = owner.durable - (owner.clear_on_head_change if repair_head is not None else frozenset())
        with test.subTest(owner=owner.name, durable=sorted(durable)):
            defaults = type(owner.value)()
            for field in type(owner.value).model_fields.keys() - durable:
                test.assertNotEqual(getattr(before[owner.name], field), getattr(defaults, field), field)
            fields = before[owner.name].model_dump(include=durable)
            if owner.repair_head_field is not None and repair_head is not None:
                fields[owner.repair_head_field] = repair_head
            test.assertEqual(after[owner.name], type(owner.value).model_validate(fields))

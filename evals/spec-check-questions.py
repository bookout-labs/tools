"""Paid checker pair. Run manually with FOREMAN_LIVE_SPEC_CHECK=1; never in CI."""
import json
import os
from pathlib import Path
import subprocess
from tempfile import mkdtemp

from foreman.config import ModelSpec
from foreman.delivery import stage_text
from foreman.stage_output import DispatchPlan, PlanState, SpecAuthor, SpecCandidate, SpecCheckOutput, parse_output
from foreman.state import ReviewState, RunState
from foreman.worker import CliWorker

if os.environ.get("FOREMAN_LIVE_SPEC_CHECK") != "1":
    raise SystemExit("Set FOREMAN_LIVE_SPEC_CHECK=1 for the paid checker pair ($1 cap per call).")
root = Path(__file__).resolve().parents[1]
scratch = Path(mkdtemp(prefix=".scratch.spec-check-", dir=root))
checkout = scratch / "target"
checkout.mkdir()
(checkout / "items.py").write_text('def visible(items):\n    return [item for item in items if item["open"]]\n')
for args in (("init", "-q"), ("add", "items.py"),
             ("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
              "commit", "-qm", "Existing open item filter")):
    subprocess.run(["git", *args], cwd=checkout, check=True)
sha = lambda cwd: subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=cwd, text=True).strip()
base = sha(checkout)
model = ModelSpec.parse("claude-opus-5-5-high")
report = {"target_commit": base, "graph_sha": sha(root),
          "cli": subprocess.check_output(["claude", "--version"], text=True).strip(),
          "model": model.name, "effort": model.effort, "cap_usd": 1, "attempts": []}
edit = {"title": "Visible items", "stateful": False, "files": ["items.py"], "screen": "", "properties": [], "non_goals": [], "removed_tests": [],
        "depends_on": [], "blocker_numbers": []}
cases = (
    # The issue contradicts itself, so no candidate can meet it and only the author can choose.
    ("visible(items) must keep returning only open items, and it must also return every closed item in the same list. "
     "Add no flag or parameter.", "Return open and closed items from visible(items).", "question"),
    # The issue is clear and the candidate misstates it, so a revised candidate fixes the gap.
    ("visible(items) must also return items whose status is \"pending\", keeping the open items.",
     "visible(items) returns only closed items.", "findings"),
)
for request, acceptance, expected in cases:
    criteria = [{"text": acceptance, "proof": {"kind": "test"}}]
    candidate = SpecCandidate.model_validate({"slices": [{**edit, "criteria": criteria}], "spec": request,
        "work_kind": "feature", "author": SpecAuthor(stage="spec", model="gpt-6.2-sol-high", family="gpt"),
        "target": {"repo": "fixture/items", "issue": 1, "base": base}})
    state = {"run": RunState(repo="fixture/items", issue=1, request=request, base_sha=base),
             "plan": PlanState(spec=request, spec_candidate=candidate.record()), "review": ReviewState(), "human_answers": [],
             "dispatch_plan": DispatchPlan(models=[model.name], instruction="", methods=[], read_roots=[],
                                           target=None, scope=None, budget_usd="1")}
    instruction = (f"{stage_text('spec_check')}\n\ntarget_repository: fixture/items\ntarget_checkout: {checkout}\npinned base: {base}"
                   f"\nRead the committed tree with git ls-tree {base} and git show {base}:<path>.")
    outcome = CliWorker(checkout, scratch_root=scratch)("spec_check", model, state, instruction)
    attempt = {"expected": expected, "kind": outcome.kind, "output": outcome.output,
               "prompt_bytes": outcome.prompt_bytes, "native_usage": outcome.native_usage,
               "usage": outcome.usage.model_dump() if outcome.usage else None, "detail": outcome.detail}
    report["attempts"].append(attempt)
    (scratch / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(attempt), flush=True)
    if outcome.kind != "ran" or outcome.usage is None or outcome.usage.total_cost_usd is None:
        raise SystemExit(f"Incomplete live evidence. See {scratch / 'report.json'}")
    output = parse_output(SpecCheckOutput, outcome.output)
    if expected == "question":
        assert output.human_question and not output.findings, output
    else:
        assert output.findings and not output.human_question, output
print(f"PASS {scratch / 'report.json'}")

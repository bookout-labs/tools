"""Paid acceptance pair. Run manually with FOREMAN_LIVE_SPEC=1; never in CI."""
import json
import os
from pathlib import Path
import subprocess
from tempfile import mkdtemp

from foreman.config import ModelSpec
from foreman.delivery import stage_text
from foreman.stage_output import DispatchPlan, PlanState, SpecOutput
from foreman.state import ReviewState
from foreman.stages import accept_spec
from foreman.worker import CliWorker
from foreman.workspace import LiveWorkspace

if os.environ.get("FOREMAN_LIVE_SPEC") != "1":
    raise SystemExit("Set FOREMAN_LIVE_SPEC=1 to authorize the paid pair (two $1 caps).")
root = Path(__file__).resolve().parents[1]
scratch = Path(mkdtemp(prefix=".scratch.spec-", dir=root))
checkout = scratch / "target"
checkout.mkdir()
(checkout / "items.py").write_text('def visible(items):\n    return [item for item in items if item["open"]]\n')
for args in (("init", "-q"), ("add", "items.py"),
             ("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
              "commit", "-qm", "Existing open item filter")):
    subprocess.run(["git", *args], cwd=checkout, check=True)
sha = lambda cwd: subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=cwd, text=True).strip()
model = ModelSpec.parse("claude-opus-5-5-high")
report = {"target_commit": sha(checkout), "graph_sha": sha(root),
          "cli": subprocess.check_output(["claude", "--version"], text=True).strip(),
          "model": model.name, "effort": model.effort, "cap_usd": 2, "attempts": []}
workspace = LiveWorkspace(checkout, "fixture/items")
for request, expected in (
    ("Document the current visible(items) behavior. Does it return all items or only open items? Preserve behavior.", "ready"),
    ("Change visible(items) for a new audience. The owner must decide whether closed items should become visible. No policy decision has been made; ask the owner before changing behavior.", "needs_human"),
):
    state = {"run": {"request": request}, "plan": PlanState(issue_kind="slice"), "review": ReviewState(),
             "dispatch_plan": DispatchPlan(models=[model.name], instruction="", methods=[], read_roots=[],
                                           target=None, scope=None, budget_usd="1")}
    outcome = CliWorker(checkout, scratch_root=scratch)("spec", model, state, stage_text("spec"))
    attempt = {"expected": expected, "kind": outcome.kind, "output": outcome.output,
               "prompt_bytes": outcome.prompt_bytes, "native_usage": outcome.native_usage,
               "usage": outcome.usage.model_dump() if outcome.usage else None, "detail": outcome.detail}
    report["attempts"].append(attempt)
    (scratch / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(attempt), flush=True)
    if outcome.kind != "ran" or outcome.usage is None or outcome.usage.total_cost_usd is None:
        raise SystemExit(f"Incomplete live evidence. See {scratch / 'report.json'}")
    output = SpecOutput.model_validate(outcome.output)
    accept_spec(output, state, model, workspace)
    assert output.spec_gate == expected
    if expected == "needs_human":
        assert any(q.checked_paths for q in output.questions)
print(f"PASS {scratch / 'report.json'}")

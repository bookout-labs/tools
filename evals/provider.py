"""Promptfoo provider. It walks the delivery graph without calling a model."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from foreman.accept import accept_event  # noqa: E402
from foreman.delivery import compile_delivery, run_delivery  # noqa: E402
from foreman.dispatch import FailoverDispatcher, QuotaExhausted  # noqa: E402
from foreman.dry_run import ScriptHandler  # noqa: E402
from foreman.improve import WriteSurfaceError, check_write_surface  # noqa: E402
from foreman.measure import grade, propose, selected  # noqa: E402
from foreman.scoring import load_scorers, score_run  # noqa: E402
from foreman.telemetry import TelemetryStore  # noqa: E402
from tempfile import TemporaryDirectory  # noqa: E402
from pathlib import Path as _Path  # noqa: E402


def _stop(handler, issue_kind="slice") -> str:
    result = run_delivery(
        FailoverDispatcher(handler),
        repo="DoctorCRE/doctorcre-engine",
        request="Add a field",
        check="npm run check",
        issue_kind=issue_kind,
    )
    return result["stop"]


def _with_quota_gap() -> str:
    handler = ScriptHandler()

    def exhaust_claude(node, model, state, instruction):
        if model == "claude-opus-5-5-high":
            raise QuotaExhausted(model)
        return handler(node, model, state, instruction)

    result = run_delivery(
        FailoverDispatcher(exhaust_claude),
        repo="DoctorCRE/doctorcre-engine",
        request="Add a field",
        check="npm run check",
    )
    return result["stop"]


def call_api(prompt, options, context):
    case = (context.get("vars") or {}).get("case") or prompt
    if case == "clear":
        output = _stop(ScriptHandler(triage="implement"))
    elif case == "spec-ready":
        output = _stop(ScriptHandler(triage="spec", spec_gate="ready"))
    elif case == "spec-gate":
        output = _stop(ScriptHandler(triage="spec", spec_gate="needs_human"))
    elif case == "parent":
        output = _stop(ScriptHandler(triage="implement"), issue_kind="parent")
    elif case == "parent-spec":
        output = _stop(
            ScriptHandler(
                triage="spec",
                spec_gate="ready",
                slices=[
                    {
                        "title": "Board lists the deals",
                        "acceptance": "The board shows the deals.",
                        "depends_on": [],
                        "screen": "docs/screens/deals.md",
                        "properties": [{"name": "Visibility", "constraint": "Every open deal appears once."}],
                    },
                    {
                        "title": "Board saves a move",
                        "acceptance": "A move is stored.",
                        "depends_on": ["Board lists the deals"],
                        "screen": "",
                        "properties": [],
                    },
                ],
            ),
            issue_kind="parent",
        )
    elif case == "question":
        output = _stop(ScriptHandler(triage="question"))
    elif case == "failover":
        output = _with_quota_gap()
    elif case == "score-approve":
        graded = grade("approve")
        output = "pass" if graded.passed and graded.score == 1.0 else "fail"
    elif case == "score-block":
        graded = grade("block")
        output = "fail" if not graded.passed and graded.score == 0.2 else "pass"
    elif case == "score-thin":
        graded = grade("insufficient_evidence")
        output = "insufficient_evidence" if not graded.passed and graded.score == 0.5 else "fail"
    elif case == "sample":
        output = "yes" if selected(0) and selected(10) and not selected(1) else "no"
    elif case == "proposal":
        output = propose("redundant-tests")
    elif case == "verify-env-halt":
        handler = ScriptHandler(triage="implement")
        res = compile_delivery(
            FailoverDispatcher(handler),
            run_check=lambda cmd: (1, "No PostgreSQL server answered on localhost:5432"),
        ).invoke({
            "repo": "DoctorCRE/doctorcre-engine",
            "issue": 1,
            "request": "Run check",
            "check": "npm run check",
            "issue_kind": "slice",
            "verify_rounds": 0,
            "review_rounds": 0,
            "models_used": {},
            "attempts": [],
            "path": [],
        })
        output = res["stop"]
    elif case == "budget-halt":
        handler = ScriptHandler(triage="implement")
        dispatcher = FailoverDispatcher(handler)
        orig_run = dispatcher.run

        def pricey(node, models, state, instruction, *, attempts=None, **kwargs):
            res = orig_run(node, models, state, instruction, attempts=attempts, **kwargs)
            dispatcher.last_meta = [{"node": node, "model": res["model"], "status": "ran", "total_cost_usd": 60.0}]
            return res

        dispatcher.run = pricey  # type: ignore[method-assign]
        res = run_delivery(
            dispatcher,
            repo="DoctorCRE/doctorcre-engine",
            request="Expensive task",
            budget={"soft_usd": 15.0, "hard_usd": 50.0, "hard_tokens": 0},
        )
        output = res["stop"]
    elif case == "scorer-insufficient":
        with TemporaryDirectory() as td:
            store = TelemetryStore(_Path(td) / "tel.sqlite3")
            rid = store.record_run({
                "repo": "Example/Engine", "issue": 1, "stop": "verified",
                "path": ["triage", "implement", "verify", "review", "done"],
            })
            spec = load_scorers()["task-compliance"]
            rows = score_run(
                store,
                rid,
                [spec],
                judge=lambda m, p, s: {"label": "insufficient_evidence", "reason": "missing logs"},
            )
            stats = store.compute_stats()["scorers"]["task-compliance"]
            output = (
                "insufficient_non_pass"
                if (not rows[0]["passed"] and stats["avg_score"] is None and stats["insufficient_evidence"] == 1)
                else "unexpected"
            )
    elif case == "improve-write-guard":
        try:
            check_write_surface(["src/foreman/models.toml"])
            output = "allowed"
        except WriteSurfaceError:
            output = "blocked"
    elif case == "alert-webhook-draft":
        decision = accept_event({
            "source": "alert_webhook",
            "severity": "error",
            "service": "api",
            "title": "500 on /deals for owner@example.invalid",
            "body": "token=ghp_0123456789abcdefghij0123456789",
        })
        output = (
            "draft_redacted"
            if (
                decision["start"]
                and decision["draft_only"]
                and "owner@example.invalid" not in decision["request"]
                and "ghp_0123456789abcdefghij0123456789" not in decision["request"]
            )
            else "leaked"
        )
    else:
        return {"error": f"unknown case {case}"}
    return {"output": output}

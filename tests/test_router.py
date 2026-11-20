import io
import contextlib
import unittest

from foreman.cli import main
from foreman.config import family
from foreman.delivery import run_delivery
from foreman.dispatch import FailoverDispatcher, QuotaExhausted
from foreman.dry_run import ScriptHandler
from foreman.router import choose_order


def walk(handler, **kwargs):
    return run_delivery(
        FailoverDispatcher(handler),
        repo="DoctorCRE/doctorcre-engine",
        request=kwargs.pop("request", "Add a field"),
        check="npm run check",
        **kwargs,
    )


class RouterTest(unittest.TestCase):
    def test_small_build_uses_terra_and_hard_build_uses_sol(self):
        small, small_reason, _ = choose_order("implement", ["sol", "terra", "opus"], {"request": "Add a field"})
        hard, hard_reason, _ = choose_order("implement", ["sol", "terra"], {"request": "x" * 401})
        empty, empty_reason, _ = choose_order("implement", ["sol", "terra"], {"request": "  "})
        files, file_reason, _ = choose_order(
            "implement", ["sol", "terra"], {"request": "Add a field", "files": ["a", "b", "c", "d"]},
        )
        risk, risk_reason, _ = choose_order(
            "implement", ["sol", "terra"], {"request": "Add a field", "labels": ["risk:high"]},
        )
        retry, retry_reason, _ = choose_order(
            "implement", ["sol", "terra"], {"request": "Add a field", "review_rounds": 1},
        )
        self.assertEqual((small[0], small_reason), ("terra", "small scoped build"))
        self.assertEqual((hard[0], hard_reason), ("sol", "large or ambiguous build"))
        self.assertEqual((empty[0], empty_reason), ("sol", "large or ambiguous build"))
        self.assertEqual((files[0], file_reason), ("sol", "large or ambiguous build"))
        self.assertEqual((risk[0], risk_reason), ("sol", "risk:high"))
        self.assertEqual((retry[0], retry_reason), ("sol", "retry after failure"))
        self.assertEqual(choose_order("triage", ["opus", "grok"], {"request": "Add a field"})[1], "configured order")

    def test_quota_fallback_keeps_a_different_family_for_review(self):
        handler = ScriptHandler()

        def exhaust_terra(node, model, state, instruction):
            if node == "implement" and model == "terra":
                raise QuotaExhausted(model)
            return handler(node, model, state, instruction)

        result = walk(exhaust_terra)
        choice = next(item for item in result["model_choices"] if item["node"] == "implement")
        self.assertEqual(choice["model"], "sol")
        self.assertEqual(choice["reason"], "quota fallback")
        self.assertEqual(result["models_used"]["review"], "opus")
        self.assertNotEqual(family("sol"), family("opus"))
        self.assertNotIn("terra", [call["model"] for call in handler.calls if call["node"] == "review"])

    def test_a_sol_build_is_not_reviewed_by_terra(self):
        handler = ScriptHandler()
        result = walk(handler, request="x" * 500)
        self.assertEqual(result["models_used"]["implement"], "sol")
        self.assertEqual(result["models_used"]["review"], "opus")
        self.assertEqual(family("sol"), family("terra"))
        self.assertNotEqual(family(result["models_used"]["implement"]), family(result["models_used"]["review"]))

    def test_dry_run_prints_the_model_and_reason(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = main([
                "run", "delivery", "--dry-run", "--repo", "Example/Engine",
                "--request", "Add a field", "--check", "npm run check",
            ])
        self.assertEqual(code, 0)
        text = stdout.getvalue()
        self.assertIn("implement terra small scoped build", text)
        self.assertIn("triage opus configured order", text)
        self.assertIn("review opus configured order", text)


if __name__ == "__main__":
    unittest.main()

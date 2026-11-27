"""#446: a resumed Claude call is charged the conversation's running total less the total its session saved."""

import tests

from decimal import Decimal
import json
from pathlib import Path
from subprocess import CompletedProcess
from tempfile import TemporaryDirectory
import unittest

from foreman.adapters import Call, NativeUsage, Outcome, Session
from foreman.adapters.claude import ClaudeAdapter
from foreman.adapters.codex import CodexAdapter
from foreman.budget import claude_spend
from foreman.config import ModelSpec
from foreman.dispatch import DispatchRun
from foreman.dry_run import ScriptHandler
from foreman.stage_output import DispatchPlan
from foreman.state import BuilderSession, StartSettings
from foreman.steps import follow
from foreman.worker import CliWorker
from tests.test_adapters import codex_events, codex_process
from test_delivery import walk

FIXTURES = Path(__file__).with_name("fixtures")
# Opus through ClaudeAdapter.command (tests/capture-claude-resume-totals.py): a parent, its fork, the fork's fork, and a
# fork of the parent its spending cap stopped. Each answer reports a Haiku side row and the Opus row.
CAPTURE = json.loads((FIXTURES / "claude" / "resume-totals.json").read_text())
OPUS = ClaudeAdapter(ModelSpec.parse(CAPTURE["model"]))
HAIKU = ClaudeAdapter(ModelSpec.parse("claude-haiku-5-5"))
SESSIONS = [f"0000000{n}-0000-4000-8000-000000000446" for n in range(4)]
ANSWER = {"answer": "finished"}


def envelope(report: dict[str, object], session: str, output: object = ANSWER) -> str:
    """A captured answer's numbers as the envelope Claude Code printed, with a fixed id and answer."""
    return json.dumps({"type": "result", "subtype": report["subtype"], "is_error": report["is_error"], "result": "",
                       "session_id": session, "structured_output": output, "total_cost_usd": report["total_cost_usd"],
                       "usage": report["usage"], "modelUsage": report["modelUsage"]})


def read(adapter: ClaudeAdapter, text: str, resume: Session | None = None) -> Outcome:
    with TemporaryDirectory() as folder:
        return adapter.read(CompletedProcess(["claude"], 0, text, ""), Call("", "{}", False, Path(folder), resume=resume))


def figures(outcome: Outcome) -> tuple[object, ...]:
    usage = outcome.usage
    assert usage is not None
    return (usage.input_tokens, usage.output_tokens, usage.cache_read_input_tokens, usage.cache_creation_input_tokens)


def spent(after: float, before: float) -> Decimal:
    """The exact difference of two reported dollar figures (T2-10), which the charge stores as its float."""
    return Decimal(repr(after)) - Decimal(repr(before))


def haiku_row(input: int, output: int, read: int, creation: int, cost: float) -> dict[str, object]:
    return {"claude-haiku-5-5": {"inputTokens": input, "outputTokens": output, "cacheReadInputTokens": read,
                                 "cacheCreationInputTokens": creation, "costUSD": cost}}


def haiku(input: int, output: int, read: int, creation: int, cost: float, *, total: float | None = None) -> dict[str, object]:
    return {"subtype": "success", "is_error": False, "total_cost_usd": cost if total is None else total,
            "usage": {}, "modelUsage": haiku_row(input, output, read, creation, cost)}


# PR #445's Haiku parent and fork, in the issue's order: cache creation / cache read / input / output.
PARENT = haiku(1174, 177, 5143, 1134, 0.00048413000000000004)
FORK = haiku(1176, 259, 11420, 1714, 0.0007041)


class ResumedChargeTests(unittest.TestCase):
    def test_a_fork_is_charged_its_own_part_of_the_capture(self):
        parent = read(HAIKU, envelope(PARENT, SESSIONS[0]))
        self.assertEqual(figures(parent), (1174, 177, 5143, 1134))
        assert parent.session is not None and parent.usage is not None
        self.assertEqual(parent.session, Session(id=SESSIONS[0], totals=parent.native_usage))
        fork = read(HAIKU, envelope(FORK, SESSIONS[1]), parent.session)
        assert fork.usage is not None and fork.usage.total_cost_usd is not None
        self.assertEqual(figures(fork), (2, 82, 6277, 580))
        self.assertEqual(fork.usage.total_cost_usd, float(spent(0.0007041, 0.00048413000000000004)))
        self.assertAlmostEqual(fork.usage.total_cost_usd, 0.00021997, places=12)
        # The attempt keeps the raw report, the running total, beside the charge (condition 4).
        assert fork.native_usage is not None
        self.assertEqual(fork.native_usage["entries"]["claude-haiku-5-5"]["cache_read_input_tokens"], 11420)

    def test_a_chain_of_three_charges_the_third_call_its_own_part_and_a_side_row_nothing_again(self):
        # K1 capture: the Haiku side row repeats unchanged and the Opus row is cumulative, so each call's part is its own.
        parent = read(OPUS, envelope(CAPTURE["parent"], SESSIONS[0]))
        fork = read(OPUS, envelope(CAPTURE["fork"], SESSIONS[1]), parent.session)
        third = read(OPUS, envelope(CAPTURE["fork_of_fork"], SESSIONS[2]), fork.session)
        for outcome, report in ((fork, CAPTURE["fork"]), (third, CAPTURE["fork_of_fork"])):
            usage = report["usage"]
            self.assertEqual(figures(outcome), (usage["input_tokens"], usage["output_tokens"], usage["cache_read_input_tokens"],
                                                usage["cache_creation_input_tokens"]))
        assert third.usage is not None and third.usage.total_cost_usd is not None
        self.assertEqual(figures(third), (2, 53, 6522, 445))
        self.assertEqual(third.usage.total_cost_usd, float(spent(CAPTURE["fork_of_fork"]["total_cost_usd"], CAPTURE["fork"]["total_cost_usd"])))

    def test_a_resumed_call_its_cap_stopped_is_charged_its_own_spend(self):
        # The stopped fork's envelope usage reads zeros; modelUsage less the saved totals is its spend.
        stopped = CAPTURE["cap_stopped_fork"]
        self.assertEqual(set(stopped["usage"].values()), {0})
        parent = read(OPUS, envelope(CAPTURE["parent"], SESSIONS[0]))
        outcome = read(OPUS, envelope(stopped, SESSIONS[3], None), parent.session)
        self.assertEqual(outcome.kind, "budget_exhausted")
        assert outcome.usage is not None and outcome.usage.total_cost_usd is not None
        self.assertEqual(figures(outcome), (2, 53, 6162, 361))
        self.assertEqual(outcome.usage.total_cost_usd, float(spent(stopped["total_cost_usd"], CAPTURE["parent"]["total_cost_usd"])))

    def test_a_fork_reporting_below_its_parent_or_without_a_saved_row_is_unknown(self):
        parent = read(HAIKU, envelope(PARENT, SESSIONS[0]))
        assert parent.session is not None
        lower = read(HAIKU, envelope(haiku(1176, 100, 11420, 1714, 0.0007041), SESSIONS[1]), parent.session)
        cheaper = read(HAIKU, envelope(haiku(1176, 259, 11420, 1714, 0.0004), SESSIONS[1]), parent.session)
        other = {**FORK, "modelUsage": {"claude-opus-5-5": FORK["modelUsage"]["claude-haiku-5-5"]}}
        missing = read(ClaudeAdapter(ModelSpec.parse("claude-opus-5-5-high")), envelope(other, SESSIONS[1]), parent.session)
        for name, outcome in (("negative tokens", lower), ("negative dollars", cheaper), ("saved row missing", missing)):
            with self.subTest(name):
                self.assertEqual(outcome.kind, "ran")
                self.assertIsNone(outcome.usage)
        run = DispatchRun(ScriptHandler(), None, None, "c" * 40, True, lambda: {}, lambda *_: None,  # type: ignore[arg-type]
                          StartSettings(human_login="ghost", max_tokens=1000, max_budget_usd="5"))
        state = {"stage": "implement", "plan": {"methods": []}}
        reservation = {"id": "r", "model": "claude-haiku-5-5", "work_allowance": 700, "dollar_allowance": "2.5", "fingerprint": ""}
        record = run.charge(state, reservation, ModelSpec.parse("claude-haiku-5-5"), lower)  # type: ignore[arg-type]
        self.assertEqual((record["work_charge"], record["dollar_charge"]), (700, "2.5"))
        self.assertTrue(record["detail"].startswith("usage_unknown:"))

    def test_a_row_the_session_never_reported_subtracts_nothing(self):
        parent = read(HAIKU, envelope(PARENT, SESSIONS[0]))
        side = {**FORK, "total_cost_usd": 0.0007041 + 0.0001,
                "modelUsage": {**FORK["modelUsage"], "claude-opus-5-5": haiku_row(10, 5, 0, 0, 0.0001)["claude-haiku-5-5"]}}
        outcome = read(HAIKU, envelope(side, SESSIONS[1]), parent.session)
        self.assertEqual(figures(outcome), (12, 87, 6277, 580))

    def test_a_zero_difference_is_a_known_zero(self):
        parent = read(HAIKU, envelope(PARENT, SESSIONS[0]))
        same = read(HAIKU, envelope(PARENT, SESSIONS[1]), parent.session)
        assert same.usage is not None
        self.assertEqual((*figures(same), same.usage.total_cost_usd), (0, 0, 0, 0, 0.0))

    def test_unknown_figures_are_unknown_alone(self):
        # T2-5: the parent's dollars did not corroborate, so its saved dollar total is unknown and so is the fork's charge.
        # The attempt still keeps the raw costUSD the CLI reported (T2-6).
        doubted = read(HAIKU, envelope(haiku(1174, 177, 5143, 1134, 0.00048413000000000004, total=0.9), SESSIONS[0]))
        assert doubted.native_usage is not None and doubted.session is not None and doubted.usage is not None
        self.assertEqual(doubted.native_usage["entries"]["claude-haiku-5-5"]["total_cost_usd"], 0.00048413000000000004)
        self.assertIsNone(doubted.session["totals"]["entries"]["claude-haiku-5-5"]["total_cost_usd"])
        fork = read(HAIKU, envelope(FORK, SESSIONS[1]), doubted.session)
        assert fork.usage is not None and fork.native_usage is not None
        self.assertEqual((*figures(fork), fork.usage.total_cost_usd), (2, 82, 6277, 580, None))
        self.assertEqual(fork.native_usage["entries"]["claude-haiku-5-5"]["total_cost_usd"], 0.0007041)
        # T2-3: a cache read the session's report lacked is unknown and charges nothing; the rest is charged as found.
        unread = {**PARENT, "modelUsage": {"claude-haiku-5-5": {
            key: value for key, value in PARENT["modelUsage"]["claude-haiku-5-5"].items() if key != "cacheReadInputTokens"}}}
        parent = read(HAIKU, envelope(unread, SESSIONS[0]))
        fork = read(HAIKU, envelope(FORK, SESSIONS[1]), parent.session)
        assert fork.usage is not None and fork.usage.total_cost_usd is not None
        self.assertEqual(figures(fork), (2, 82, None, 580))

    def test_a_fresh_session_after_a_resumed_one_subtracts_nothing(self):
        parent = read(HAIKU, envelope(PARENT, SESSIONS[0]))
        read(HAIKU, envelope(FORK, SESSIONS[1]), parent.session)
        fresh = read(HAIKU, envelope(FORK, SESSIONS[2]))
        self.assertEqual(figures(fresh), (1176, 259, 11420, 1714))

    def test_an_answer_without_totals_leaves_no_session(self):
        # K2: the pair has no optional totals, so an answer that reported none can't be resumed by any holder.
        outcome = read(HAIKU, json.dumps({**json.loads(envelope(PARENT, SESSIONS[0])), "modelUsage": None}))
        self.assertEqual((outcome.session_id, outcome.session), (SESSIONS[0], None))

    def test_a_resumed_codex_call_is_charged_as_before(self):
        # T2-11: only Claude subtracts.
        codex = CodexAdapter(ModelSpec.parse("gpt-6.1-sol-high"))
        totals = NativeUsage(family="gpt", aggregation="turn_total", entries={"turn": {
            "input_tokens": 10**6, "output_tokens": 10**6, "cache_read_input_tokens": 10**6, "cache_creation_input_tokens": 10**6,
            "total_cost_usd": None}})
        with TemporaryDirectory() as folder:
            fresh = codex.read(codex_process(codex_events()), Call("", "{}", False, Path(folder)))
            resumed = codex.read(codex_process(codex_events()), Call("", "{}", False, Path(folder), resume=Session(id=SESSIONS[0], totals=totals)))
        self.assertIsNotNone(fresh.usage)
        self.assertEqual(resumed.usage, fresh.usage)

    def test_the_plan_hands_the_worker_the_totals_so_a_recovered_answer_is_charged_as_the_live_one(self):
        # T2-8: the totals ride in the dispatch plan; reading the same answer under that plan gives the same charge.
        parent = read(HAIKU, envelope(PARENT, SESSIONS[0]))
        assert parent.session is not None
        answer = envelope(FORK, SESSIONS[1], {"answer": "finished"})
        plan = DispatchPlan(models=["claude-haiku-5-5"], instruction="", methods=[], target=None, scope=None,
                            resume=parent.session).record()
        argv: list[list[str]] = []
        with TemporaryDirectory() as scratch:
            live = CliWorker(Path.cwd(), lambda args, *_: argv.append(args) or CompletedProcess(args, 0, answer, ""),
                             scratch_root=Path(scratch))("triage", HAIKU.model, {"dispatch_plan": plan}, "")
        recovered = read(HAIKU, answer, DispatchPlan.model_validate(plan).resume)
        self.assertEqual(argv[0][argv[0].index("--resume") + 1], SESSIONS[0])
        self.assertEqual((live.usage, figures(live)), (recovered.usage, (2, 82, 6277, 580)))

    def test_a_moved_checkout_carries_the_saved_session_and_totals_unchanged(self):
        parent = read(HAIKU, envelope(PARENT, SESSIONS[0]))
        assert parent.session is not None
        saved = BuilderSession(model="claude-haiku-5-5", session=parent.session, fingerprint="before")
        moved = follow({"builder_session": saved}, "before", "after")  # type: ignore[arg-type]
        self.assertEqual(moved, {"builder_session": {**saved, "fingerprint": "after"}})

    def test_a_repair_turn_is_charged_its_own_spend(self):
        # An invalid Opus triage answer, then its repair turn as a fork: the run's spend is the two calls' own spends.
        script = ScriptHandler()

        def dispatcher(node, model, state, instruction):
            if node != "triage":
                return script(node, model, state, instruction)
            valid = script(node, model, state, instruction).output

            def run(args, cwd, env):
                fork = "--resume" in args
                text = envelope(CAPTURE["fork"], SESSIONS[1], valid) if fork else envelope(CAPTURE["parent"], SESSIONS[0], {})
                return CompletedProcess(args, 0, text, "")
            with TemporaryDirectory() as scratch:
                return CliWorker(Path.cwd(), run, scratch_root=Path(scratch))(node, model, state, instruction)

        result = walk(dispatcher)
        triage = [attempt for attempt in result["attempts"] if attempt["stage"] == "triage"]
        self.assertEqual([attempt["outcome"] for attempt in triage], ["ran", "ran"])
        first, repair = (attempt["usage"] for attempt in triage)
        assert first is not None and repair is not None
        self.assertEqual((repair["input"], repair["output"], repair["cache_read"], repair["cache_creation"]), (2, 53, 6085, 437))
        self.assertEqual((first["input"], first["output"], first["cache_read"], first["cache_creation"]), (1174, 69, 0, 6085))
        self.assertEqual(claude_spend(triage), Decimal(repr(first["cost_usd"])) + Decimal(repr(repair["cost_usd"])))
        self.assertAlmostEqual(float(claude_spend(triage) or 0), CAPTURE["fork"]["total_cost_usd"], places=12)
        self.assertTrue(all("work_charge" not in attempt and "dollar_charge" not in attempt for attempt in triage))


if __name__ == "__main__":
    unittest.main()

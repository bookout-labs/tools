"""Run a pinned worker and read its final stage result."""

from collections.abc import Mapping
from dataclasses import replace
from decimal import Decimal
import json
import os
from pathlib import Path
import subprocess
import tempfile
from tempfile import TemporaryDirectory
from typing import Callable

from pydantic import TypeAdapter
from pydantic_core import to_jsonable_python

from foreman.adapters import Call, CapabilityUnavailable, CliAdapter, Outcome
from foreman.adapters.claude import ClaudeAdapter
from foreman.adapters.codex import CodexAdapter
from foreman.adapters.grok import GrokAdapter
from foreman.config import ConfigError, ModelSpec
from foreman.state import ChecksState
from foreman.stage_output import DispatchPlan
from foreman.stages import STAGES
from foreman.usd import BudgetUsd
from foreman.workspace import ReviewScope

CommandRunner = Callable[[list[str], Path, Mapping[str, str]], subprocess.CompletedProcess[str]]

ADAPTERS: dict[str, Callable[[ModelSpec], CliAdapter]] = {
    "claude": ClaudeAdapter,
    "gpt": CodexAdapter,
    "grok": GrokAdapter,
}


def run_worker_command(args: list[str], cwd: Path, adapter_env: Mapping[str, str]) -> subprocess.CompletedProcess[str]:
    prompt = args[-1] if args[0] == "claude" and args[-2:-1] == ["-p"] else None
    return subprocess.run(
        args[:-1] if prompt is not None else args, cwd=cwd, env={**os.environ, **adapter_env},
        stdin=subprocess.DEVNULL if prompt is None else None, input=prompt, capture_output=True, text=True,
    )


class CliWorker:
    def __init__(self, cwd: Path, run: CommandRunner = run_worker_command, *, scratch_root: Path | None = None):
        self.cwd = cwd
        self.run = run
        self.scratch_root = scratch_root

    def __call__(self, node: str, model: ModelSpec | None, state: Mapping[str, object], instruction: str) -> Outcome:
        if model is None:
            raise ConfigError(f"{node} has no model configured")
        prompt = (
            f"{instruction}\n\nReturn exactly one JSON object that matches the output schema. "
            "Use null for inapplicable nullable fields. Do not arm a merge."
            " Write disposable scratch only under $TMPDIR, outside the checkout."
            " Do not return the scratch path as evidence."
        )
        contract = STAGES[node].output
        assert contract is not None
        schema = json.dumps(contract.model_json_schema())
        projected = to_jsonable_python(state)
        if plan_state := projected.get("dispatch_plan"):
            plan_state.pop("instruction", None)
            plan_state.pop("scope", None)
        for attempt in projected.get("attempts", []):
            attempt.pop("prompt_bytes", None)
            attempt.pop("native_usage", None)
        prompt += f"\n\nDelivery state:\n{json.dumps(projected)}"
        # Counts only the prompt, excluding the separate output schema,
        # CLI system prompts, tool definitions/results and hidden model work.
        prompt_bytes = len(prompt.encode("utf-8"))

        plan = state.get("dispatch_plan")
        plan = DispatchPlan.model_validate(plan) if plan else None
        budget = plan.budget_usd if plan else None
        edit = node == "implement"
        checks: tuple[str, ...] = ()
        if edit:
            check = ChecksState.model_validate(state["checks"]).check
            checks = (check,) if check else ()
        elif node == "review" and plan and plan.scope:
            checks = TypeAdapter(ReviewScope).validate_python(plan.scope).commands
        adapter = ADAPTERS[model.family](model)
        checkout = self.cwd.resolve()
        temp_root = (self.scratch_root or Path(tempfile.gettempdir())).resolve()
        if self.scratch_root is None and temp_root.is_relative_to(checkout):
            temp_root = checkout.parent
        with TemporaryDirectory(prefix="foreman-worker-", dir=temp_root) as directory:
            scratch = Path(directory) / "scratch"
            scratch.mkdir()
            call = Call(prompt, schema, edit=edit, checks=checks, review=node == "review",
                        read_roots=tuple(Path(root) for root in plan.read_roots) if plan else (),
                        temp_dir=Path(directory), budget_usd=BudgetUsd(Decimal(budget)) if budget else None)
            try:
                args = adapter.command(call)
            except CapabilityUnavailable as exc:
                return Outcome("cli_missing", detail=str(exc), prompt_bytes=prompt_bytes)
            try:
                process = self.run(args, self.cwd, {**adapter.env, "TMPDIR": str(scratch)})
            except FileNotFoundError:
                return Outcome("cli_missing", detail=f"{args[0]} executable is unavailable", prompt_bytes=prompt_bytes)
            return replace(adapter.read(process, call), prompt_bytes=prompt_bytes)

"""Try the configured models in order. Quota exhaustion is not a rule change."""

from typing import Callable, Protocol, TypedDict


class Attempt(TypedDict):
    node: str
    model: str | None
    status: str
    detail: str


class QuotaExhausted(Exception):
    def __init__(self, model: str | None, detail: str = ""):
        self.model = model
        self.detail = detail or (model or "no model")
        super().__init__(self.detail)


class CliMissing(Exception):
    def __init__(self, model: str | None, detail: str = ""):
        self.model = model
        self.detail = detail or (f"{model} CLI is not installed" if model else "CLI is not installed")
        super().__init__(self.detail)


class InvalidOutput(ValueError):
    pass


class BudgetExceeded(Exception):
    pass


class Handler(Protocol):
    def __call__(self, node: str, model: str | None, state: dict, instruction: str) -> dict:
        pass


class FailoverDispatcher:
    def __init__(self, handler: Handler):
        self.handler = handler
        self.last_meta: list[dict] = []

    def run(
        self,
        node: str,
        models: list[str],
        state: dict,
        instruction: str,
        *,
        attempts: list[Attempt] | None = None,
        budget_check: Callable[[list[dict]], str | None] | None = None,
    ) -> dict:
        self.last_meta = []
        candidates: list[str | None] = list(models) or [None]
        last_quota: QuotaExhausted | None = None
        last_missing: CliMissing | None = None
        for model in candidates:
            if hasattr(self.handler, "last_meta"):
                try:
                    self.handler.last_meta = None  # type: ignore[attr-defined]
                except AttributeError:
                    pass
            try:
                update = self.handler(node, model, state, instruction)
            except QuotaExhausted as exc:
                if attempts is not None:
                    attempts.append({"node": node, "model": model, "status": "quota", "detail": str(exc)})
                meta = getattr(self.handler, "last_meta", None)
                if isinstance(meta, dict):
                    self.last_meta.append({**meta, "node": node, "model": model, "status": "quota"})
                if budget_check is not None and (detail := budget_check(self.last_meta)):
                    raise BudgetExceeded(detail) from exc
                last_quota = exc
                continue
            except CliMissing as exc:
                if attempts is not None:
                    attempts.append({"node": node, "model": model, "status": "cli_missing", "detail": str(exc)})
                last_missing = exc
                continue
            except InvalidOutput as exc:
                if attempts is not None:
                    attempts.append({"node": node, "model": model, "status": "stopped", "detail": str(exc)})
                meta = getattr(self.handler, "last_meta", None)
                if isinstance(meta, dict):
                    self.last_meta.append({**meta, "node": node, "model": model, "status": "stopped"})
                if budget_check is not None and (detail := budget_check(self.last_meta)):
                    raise BudgetExceeded(detail) from exc
                raise
            meta = getattr(self.handler, "last_meta", None)
            if isinstance(meta, dict):
                self.last_meta.append({**meta, "node": node, "model": model, "status": "ran"})
            if budget_check is not None and (detail := budget_check(self.last_meta)):
                if attempts is not None:
                    attempts.append({"node": node, "model": model, "status": "ran", "detail": ""})
                raise BudgetExceeded(detail)
            if not isinstance(update, dict):
                detail = f"{node} returned {type(update).__name__}"
                if attempts is not None:
                    attempts.append({"node": node, "model": model, "status": "stopped", "detail": detail})
                raise InvalidOutput(detail)
            if attempts is not None:
                attempts.append({"node": node, "model": model, "status": "ran", "detail": ""})
            result = dict(update)
            result["model"] = model
            return result
        if last_quota is not None:
            raise last_quota
        if last_missing is not None:
            raise last_missing
        raise QuotaExhausted(None)

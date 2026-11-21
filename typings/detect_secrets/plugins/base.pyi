from collections.abc import Iterable
from re import Pattern

class BasePlugin:
    secret_type: str

class RegexBasedDetector(BasePlugin):
    @property
    def denylist(self) -> Iterable[Pattern[str]]: ...

"""The container host double: each container is a record, and its commands run on the host in the directory they name.

A real attempt container mounts the checkout at the same path, so a command sees the same files either way. The double keeps
what Workspace asked of it, so a test can read which containers were built, where setup ran and what was stopped.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
import os
from pathlib import Path
import subprocess

from foreman.container import CheckLog, ContainerSpec, TimedOut


@dataclass(frozen=True)
class Ran:
    container: str
    cwd: Path
    command: str
    timeout: float


@dataclass
class HostContainers:
    # Commands that never end, in the directories hang_in accepts: the double reports them ended at the limit Workspace gave.
    hangs: set[str] = field(default_factory=set[str])
    hang_in: Callable[[Path], bool] = lambda cwd: True
    specs: dict[str, ContainerSpec] = field(default_factory=dict[str, ContainerSpec])
    created: list[tuple[str, float]] = field(default_factory=list[tuple[str, float]])
    ran: list[Ran] = field(default_factory=list[Ran])
    live: set[str] = field(default_factory=set[str])
    isolated: set[str] = field(default_factory=set[str])
    stopped: list[str] = field(default_factory=list[str])

    def create(self, spec: ContainerSpec, timeout: float) -> str:
        name = f"container-{len(self.specs) + 1}"
        self.specs[name] = spec
        self.created.append((name, timeout))
        self.live.add(name)
        return name

    def run(self, name: str, command: str, *, cwd: Path, log: CheckLog | None, timeout: float) -> int | TimedOut:
        self.ran.append(Ran(name, cwd, command, timeout))
        if name not in self.live:
            return 1
        if command in self.hangs and self.hang_in(cwd):
            return TimedOut(timeout)
        env = {**os.environ, **self.specs[name].secrets}
        with subprocess.Popen(command, shell=True, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True, errors="replace") as process:
            assert process.stdout is not None
            for line in process.stdout:
                if log is not None:
                    log(line)
            return process.wait()

    def running(self, name: str) -> bool:
        return name in self.live

    def isolate(self, name: str) -> None:
        self.isolated.add(name)

    def leftovers(self, label: str) -> list[str]:
        return sorted(name for name in self.live if self.specs[name].label == label)

    def stop(self, names: Sequence[str]) -> None:
        for name in names:
            self.live.discard(name)
            self.stopped.append(name)

    def setups(self, name: str) -> list[Ran]:
        """The commands run in the container before its first check, past the mount check."""
        return [ran for ran in self.ran if ran.container == name][1:]


def container_route(base: str) -> tuple[str, float]:
    """The run's container for a worker double, as Workspace.container_route gives it."""
    return "container-1", 1800

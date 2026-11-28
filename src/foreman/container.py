"""The attempt container: the contract's image with its setup run, where the run's check runs (target.yaml item 3).

Workspace owns the run's container. This module is the container host it drives, the docker CLI on the Foreman host.
"""

from collections.abc import Callable, Mapping, Sequence
import csv
from dataclasses import dataclass, field
import io
import os
from pathlib import Path
import subprocess
import threading
from typing import Protocol
from uuid import uuid4

CheckLog = Callable[[str], None]
# The contract's setup field: a container has thirty minutes to be ready, from the start of the image pull to the end of the
# last setup command.
SETUP_SECONDS = 1800
LABEL = "foreman.checkout"
# The host's read of a container's state, a network change or a stop, each bounded so a stuck daemon ends in a named failure.
DOCKER_SECONDS = 60


class ContainerUnavailable(RuntimeError):
    """A container that did not build, a setup command that failed or did not end in time, or a container that is gone.
    The step stops with env_unavailable, naming the command."""


class SetupFailed(ContainerUnavailable):
    """A setup command that failed or did not end in time, or a test value the setup needs that the host does not set."""


@dataclass(frozen=True)
class TimedOut:
    """A command Foreman ended at its limit. It is never an exit code."""

    seconds: float


def ended(result: int | TimedOut) -> str:
    """How a command ended, for its evidence."""
    return f"did not end within {result.seconds:.0f} seconds" if isinstance(result, TimedOut) else f"exited with {result}"


@dataclass(frozen=True)
class Mount:
    path: Path
    read_only: bool = False


@dataclass(frozen=True)
class ContainerSpec:
    """One attempt container. Each mount is at the same path inside the container as on the host."""

    label: str
    image: str
    workdir: Path
    mounts: tuple[Mount, ...]
    # Each test value is set in the container under its name. The docker client reads it from its own environment, so no
    # value is ever an argument.
    secrets: Mapping[str, str] = field(default_factory=dict[str, str], repr=False)


class ContainerHost(Protocol):
    def create(self, spec: ContainerSpec, timeout: float) -> str: ...
    def run(self, name: str, command: str, *, cwd: Path, log: CheckLog | None, timeout: float) -> int | TimedOut: ...
    def running(self, name: str) -> bool: ...
    def isolate(self, name: str) -> None: ...
    def leftovers(self, label: str) -> list[str]: ...
    def stop(self, names: Sequence[str]) -> None: ...


def mount_argument(mount: Mount) -> str:
    """docker's --mount value, CSV-quoted so a comma or quote in the path stays part of it."""
    fields = ["type=bind", f"source={mount.path}", f"target={mount.path}", *(["readonly"] if mount.read_only else [])]
    line = io.StringIO()
    csv.writer(line, lineterminator="").writerow(fields)
    return line.getvalue()


def create_arguments(name: str, spec: ContainerSpec) -> list[str]:
    """The docker run arguments: the image, its own network, its mounts and the names of its test values, and nothing else."""
    return ["run", "--detach", "--rm", "--init", "--name", name, "--label", f"{LABEL}={spec.label}", "--network", name,
            "--workdir", str(spec.workdir), *(argument for mount in spec.mounts for argument in ("--mount", mount_argument(mount))),
            *(argument for secret in spec.secrets for argument in ("--env", secret)), spec.image, "tail", "-f", "/dev/null"]


def stream(args: Sequence[str], log: CheckLog | None, timeout: float) -> int | TimedOut:
    """Run a command, giving each output line to the log, and end it at the timeout. Ending it never ends what it started in
    a container, so the caller removes that container."""
    with subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                          errors="replace") as process:
        output = process.stdout
        assert output is not None

        def pump() -> None:
            for line in output:
                if log is not None:
                    log(line)
        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        try:
            code = process.wait(timeout)
        except BaseException as exc:
            process.kill()
            process.wait()
            reader.join()
            if isinstance(exc, subprocess.TimeoutExpired):
                return TimedOut(timeout)
            raise
        reader.join()
        return code


class DockerHost:
    """The docker CLI. Each container gets a network of its own with the same name, removed with it."""

    def _docker(self, *args: str, timeout: float = DOCKER_SECONDS, env: Mapping[str, str] | None = None,
                ok: Sequence[int] = (0,)) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(["docker", *args], stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace",
                                    timeout=timeout, env=None if env is None else {**os.environ, **env})
        except subprocess.TimeoutExpired:
            raise ContainerUnavailable(f"docker {args[0]} did not end within {timeout:.0f} seconds.") from None
        except OSError as exc:
            raise ContainerUnavailable(f"docker could not run ({exc}). Install docker, or start colima, on the Foreman host.") from None
        if result.returncode not in ok:
            raise ContainerUnavailable(f"docker {args[0]} exited with {result.returncode}: {result.stderr.strip()}")
        return result

    def create(self, spec: ContainerSpec, timeout: float) -> str:
        name = f"foreman-{uuid4().hex[:16]}"
        self._docker("network", "create", "--label", f"{LABEL}={spec.label}", name)
        try:
            # docker run pulls a missing image, so the pull counts toward the time a container has to be ready.
            self._docker(*create_arguments(name, spec), timeout=timeout, env=spec.secrets)
        except BaseException:
            self.stop([name])
            raise
        return name

    def run(self, name: str, command: str, *, cwd: Path, log: CheckLog | None, timeout: float) -> int | TimedOut:
        return stream(["docker", "exec", "--workdir", str(cwd), name, "sh", "-c", command], log, timeout)

    def running(self, name: str) -> bool:
        state = self._docker("container", "inspect", "--format", "{{.State.Running}}", name, ok=(0, 1))
        return state.returncode == 0 and state.stdout.strip() == "true"

    def isolate(self, name: str) -> None:
        """After setup the container keeps loopback only."""
        self._docker("network", "disconnect", name, name)

    def leftovers(self, label: str) -> list[str]:
        listed = self._docker("container", "ls", "--all", "--filter", f"label={LABEL}={label}", "--format", "{{.Names}}")
        return listed.stdout.split()

    def stop(self, names: Sequence[str]) -> None:
        """Stop each container, which --rm then removes, and remove its network. A container already gone is not an error."""
        if not names:
            return
        self._docker("stop", "--time", "5", *names, ok=(0, 1))
        self._docker("network", "rm", *names, ok=(0, 1))


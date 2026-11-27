"""The one tool a worker runs commands with. Each command runs in the run's attempt container, never on the Foreman host
(target.yaml item 3), so the worker's CLI stays on the host with its login and the container holds no credential.

A stdio MCP server the worker's CLI starts: `python -m foreman.exec_tool CONTAINER WORKDIR SECONDS [COMMAND ...]`. Named
commands are the only ones it runs, as for a reviewer granted its diff command and the contract's check.
"""

from collections.abc import Sequence
from pathlib import Path
import sys

from pydantic import BaseModel, TypeAdapter, ValidationError

from foreman.container import ContainerHost, ContainerUnavailable, DockerHost, ended

NAME = "exec"
TOOL = {
    "name": NAME,
    "description": "Run one shell command in the run's attempt container, in the checkout, and return its output and how it ended.",
    "inputSchema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"],
                    "additionalProperties": False},
}


def run(host: ContainerHost, container: str, workdir: Path, seconds: float, only: Sequence[str], command: str) -> tuple[str, bool]:
    """The tool's text and whether it is an error."""
    if only and command.strip() not in only:
        return f"Foreman runs only these commands here: {list(only)}.", True
    lines: list[str] = []
    try:
        result = host.run(container, command, cwd=workdir, log=lines.append, timeout=seconds)
    except ContainerUnavailable as exc:
        return str(exc), True
    return f"{''.join(lines)}\nThe command {ended(result)}.", result != 0


class Request(BaseModel):
    """One JSON-RPC message. A notification has no id and gets no answer."""

    id: int | str | None = None
    method: str
    params: dict[str, object] = {}


class Arguments(BaseModel):
    command: str


ANSWER = TypeAdapter(dict[str, object])


def answer(request: Request, host: ContainerHost, container: str, workdir: Path, seconds: float,
           only: Sequence[str]) -> dict[str, object] | None:
    """The JSON-RPC response to one request, or None for a notification."""
    if request.id is None:
        return None
    params = request.params
    if request.method == "initialize":
        result: dict[str, object] = {"protocolVersion": params.get("protocolVersion", "2025-06-18"),
                                     "capabilities": {"tools": {}}, "serverInfo": {"name": "foreman", "version": "1"}}
    elif request.method == "tools/list":
        result = {"tools": [TOOL]}
    elif request.method == "tools/call" and params.get("name") == NAME:
        try:
            command = Arguments.model_validate(params.get("arguments")).command
        except ValidationError:
            text, error = "The exec tool takes one string, command.", True
        else:
            text, error = run(host, container, workdir, seconds, only, command)
        result = {"content": [{"type": "text", "text": text}], "isError": error}
    elif request.method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": request.id, "error": {"code": -32601, "message": f"Unknown method {request.method!r}."}}
    return {"jsonrpc": "2.0", "id": request.id, "result": result}


def main(argv: Sequence[str] = sys.argv[1:], host: ContainerHost | None = None) -> None:
    container, workdir, seconds, *only = argv
    docker = host or DockerHost()
    for line in sys.stdin:
        if line.strip() and (response := answer(Request.model_validate_json(line), docker, container, Path(workdir), float(seconds), only)):
            sys.stdout.write(ANSWER.dump_json(response).decode() + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()

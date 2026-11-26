"""The receiver: plain code on the Foreman host that takes each delivery from the App's webhook, verifies its signature and
hands it unchanged to accept, then runs the one answer accept gives. It calls no model, and neither does accept."""

from collections.abc import Callable, Iterable, Iterator
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
from typing_extensions import override

from foreman.accept import AcceptError, Accepted, Paused, accept_event
from foreman.state import CheckpointVersionError

SECRET_PATH = "~/.config/bookout-foreman/webhook-secret"
RELEASED = re.compile(r"start ([\w.-]+/[\w.-]+)#([1-9][0-9]*)")

Command = Callable[[list[str]], Iterable[str]]


class Refused(Exception):
    def __init__(self, status: int, reason: str):
        super().__init__(reason)
        self.status = status


def webhook_secret() -> bytes:
    """The App's webhook secret from the owner-only file FOREMAN_WEBHOOK_SECRET_PATH names. Its text is never shown."""
    path = Path(os.environ.get("FOREMAN_WEBHOOK_SECRET_PATH", SECRET_PATH)).expanduser()
    if path.stat().st_mode & 0o077:
        raise ValueError(f"{path} must have owner-only permissions")
    if not (secret := path.read_bytes().rstrip(b"\r\n")):
        raise ValueError(f"{path} is empty")
    return secret


def receive(event: str | None, signature: str | None, body: bytes, secret: bytes,
            paused: Callable[[str, int], Paused | None]) -> Accepted | None:
    """Refuse a delivery whose X-Hub-Signature-256 is missing or does not sign its body, before accept reads any of it."""
    expected = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
    if signature is None or not hmac.compare_digest(expected.encode(), signature.encode()):
        raise Refused(401, "bad signature")
    if not event:
        raise Refused(400, "no X-GitHub-Event")
    try:
        payload = json.loads(body)
    except ValueError:
        raise Refused(400, "body is not JSON") from None
    try:
        return accept_event(event, payload, paused)
    except AcceptError as exc:
        raise Refused(400, str(exc)) from None


def run_answer(answer: Accepted, command: Command) -> None:
    """Run start or wake, then start each issue a finished run prints for admission, and each issue those print, in order."""
    pending = [(answer.action, answer.repo, answer.issue)]
    while pending:
        action, repo, issue = pending.pop(0)
        for line in command([action, "--repo", repo, "--issue", str(issue)]):
            if released := RELEASED.fullmatch(line.rstrip("\n")):
                pending.append(("start", released[1], int(released[2])))


def run_foreman(args: list[str]) -> Iterator[str]:
    """Run one foreman command in its own process, passing its output through line by line. A run sets signal handlers,
    which only a process's main thread can do, so it never runs in the receiver's thread."""
    with subprocess.Popen([sys.executable, "-m", "foreman", *args], stdout=subprocess.PIPE, text=True) as child:
        assert child.stdout is not None
        for line in child.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            yield line


def listener(address: tuple[str, int], secret: bytes, paused: Callable[[str, int], Paused | None], command: Command) -> HTTPServer:
    """Decide deliveries one at a time, in the order they arrive. Each answer runs in its own thread once GitHub has its reply,
    so a long run delays no later delivery. The issue's lock orders the runs of one issue."""

    class Delivery(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            delivery = self.headers.get("X-GitHub-Delivery", "-")
            length = self.headers.get("Content-Length", "")
            if not length.isdecimal():
                return self.reply(411, delivery, "refused no Content-Length")
            body = self.rfile.read(int(length))
            try:
                answer = receive(self.headers.get("X-GitHub-Event"), self.headers.get("X-Hub-Signature-256"), body, secret, paused)
            except Refused as exc:
                return self.reply(exc.status, delivery, f"refused {exc}")
            except CheckpointVersionError as exc:
                return self.reply(500, delivery, str(exc))
            if answer is None:
                return self.reply(202, delivery, "ignored")
            line = f"{answer.action} {answer.repo}#{answer.issue}"
            self.reply(202, delivery, line)
            threading.Thread(target=run_answer, args=(answer, command), name=f"foreman {line}").start()

        def reply(self, status: int, delivery: str, line: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(f"{line}\n".encode())
            print(f"{delivery} {line}", flush=True)

        @override
        def log_message(self, format: str, *args: object) -> None:
            pass

    return HTTPServer(address, Delivery)

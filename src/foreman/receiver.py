"""The receiver: plain code on the Foreman host that takes each delivery from the App's webhook, verifies its signature and
hands it unchanged to accept, then runs the one answer accept gives. It calls no model, and neither does accept."""

from collections.abc import Callable
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from typing_extensions import override

from foreman.accept import AcceptError, Accepted, SavedRuns, accept_event
from foreman.state import CheckpointVersionError

SECRET_PATH = "~/.config/bookout-foreman/webhook-secret"
# The host's listener, which Tailscale Funnel proxies the App's webhook to (#386). The smoke looks for it there.
PORT = 8787
# GitHub waits 10 seconds for a reply and caps a delivery's payload at 25 MB.
TIMEOUT = 10
MAX_BODY = 25 * 1024 * 1024

Command = Callable[[list[str]], int]
Released = Callable[[str, int], list[str]]


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


def receive(event: str | None, signature: str | None, body: bytes, secret: bytes, runs: SavedRuns) -> list[Accepted]:
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
        return accept_event(event, payload, runs)
    except AcceptError as exc:
        raise Refused(400, str(exc)) from None


def answered(answers: list[Accepted]) -> str:
    """The line that reports accept's answers: ignored, or each start or wake."""
    return ", ".join(f"{answer.action} {answer.repo}#{answer.issue}" for answer in answers) or "ignored"


def run_answer(answer: Accepted, command: Command, released: Released) -> None:
    """Run start or wake, then start each issue the run's checkpoint saves as released, as an accepted delivery starts.
    The run's output carries the target repository's check output, so it is never read."""
    if code := command([answer.action, "--repo", answer.repo, "--issue", str(answer.issue)]):
        print(f"{answer.action} {answer.repo}#{answer.issue} exited {code}", flush=True)
    for issue in released(answer.repo, answer.issue):
        repo, _, number = issue.rpartition("#")
        start(Accepted("start", repo, int(number)), command, released)


def start(answer: Accepted, command: Command, released: Released) -> None:
    threading.Thread(target=run_answer, args=(answer, command, released),
                     name=f"foreman {answer.action} {answer.repo}#{answer.issue}").start()


def run_foreman(args: list[str]) -> int:
    """Run one foreman command in its own process, which shares the receiver's output, and return its exit code. A run sets
    signal handlers, which only a process's main thread can do, so it never runs in the receiver's thread."""
    return subprocess.run([sys.executable, "-m", "foreman", *args], check=False).returncode


def listener(address: tuple[str, int], secret: bytes, runs: SavedRuns, command: Command, released: Released) -> ThreadingHTTPServer:
    """Read each delivery on its own connection, so a stalled one delays no other, and decide them one at a time. Each answer
    runs in its own thread once GitHub has its reply, so a long run delays no later delivery, and a wake for a running run
    waits for its lock there. The issue's lock serializes the runs of one issue."""
    deciding = threading.Lock()

    class Delivery(BaseHTTPRequestHandler):
        timeout = TIMEOUT

        def do_POST(self) -> None:
            delivery = self.headers.get("X-GitHub-Delivery", "-")
            length = self.headers.get("Content-Length", "")
            if not length.isdecimal():
                return self.reply(411, delivery, "refused no Content-Length")
            if int(length) > MAX_BODY:
                return self.reply(413, delivery, "refused body too large")
            try:
                body = self.rfile.read(int(length))
            except TimeoutError:
                return self.reply(408, delivery, "refused body not sent")
            try:
                with deciding:
                    answers = receive(self.headers.get("X-GitHub-Event"), self.headers.get("X-Hub-Signature-256"), body,
                                      secret, runs)
            except Refused as exc:
                return self.reply(exc.status, delivery, f"refused {exc}")
            except CheckpointVersionError as exc:
                return self.reply(409, delivery, str(exc))
            self.reply(202, delivery, answered(answers))
            for answer in answers:
                start(answer, command, released)

        def reply(self, status: int, delivery: str, line: str) -> None:
            print(f"{delivery} {line}", flush=True)
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(f"{line}\n".encode())

        @override
        def log_message(self, format: str, *args: object) -> None:
            pass

    return ThreadingHTTPServer(address, Delivery)

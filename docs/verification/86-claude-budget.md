# Claude dispatch budget verification (issue 86)

The new behavioral tests exercise two Claude candidates at each dispatching
stage, exact 0.2/0.1 remainders through a real CliWorker with a fake runner,
provider-specific argv, limit/overshoot stops, output withholding, unknown
costs, zero costs, missing CLIs, stop precedence, and mixed-provider spend.
Fresh-process tests cover interrupted dispatch and recomputation after quota
pauses. No live provider calls were made.

The execution sandbox prohibits loopback socket bind/connect operations.
Unmodified unit discovery stops while importing tests/__init__.py. To run
checks here, a temporary sitecustomize supplied only the closed loopback proxy
used by that import, then restored real sockets and retained the test audit
hook. No repository offline policy or socket test was changed.

```python
# The execution sandbox forbids binding the dead proxy in tests/__init__.py.
# Supply its loopback-only dead proxy at import, then restore real sockets.
import socket
class DeadProxy:
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def bind(self, address): pass
    def getsockname(self): return ('127.0.0.1', 9)
original = socket.socket
try:
    socket.socket = lambda *args, **kwargs: DeadProxy()
    import tests
finally:
    socket.socket = original
```

Commands used the bootstrap through
`PYTHONPATH=/tmp/foreman-86-test-bootstrap:$PWD:$PWD/src:$PWD/tests`.

## Focused behavior and recovery checks

```sh
python -m unittest -v test_dispatch_budget test_dollar_allowance test_dispatch_recovery test_budget test_token_cap test_usage test_watch
```

```text
----------------------------------------------------------------------
Ran 50 tests in 30.308s

OK
```

## Strict typing

```sh
pyright --pythonpath .venv/bin/python
```

```text
0 errors, 0 warnings, 0 informations
```

## Full unit discovery

```sh
python -m unittest discover -s tests -v
```

```text
----------------------------------------------------------------------
Ran 395 tests in 151.831s

FAILED (failures=1, errors=2, skipped=1)

ERROR: test_guard_rejects_external_python_sockets (test_offline.OfflineSuiteTest.test_guard_rejects_external_python_sockets)
ERROR: test_auth_failure_under_pty_returns_without_prompting (test_publication.PublicationTest.test_auth_failure_under_pty_returns_without_prompting)
FAIL: test_subprocess_transports_fail_fast_and_local_git_still_works (test_offline.OfflineSuiteTest.test_subprocess_transports_fail_fast_and_local_git_still_works) (command=['/Users/booko/.t3/worktrees/tools-graph/t3-bae56abe-8b806282a4358e6d/.scratch.TchJBl/.venv/bin/python', '-I', '-c', "import urllib.request; urllib.request.urlopen('https://example.invalid/repo')"])
```

These three failures concern the environment: the offline listener and HTTP
server cannot bind (PermissionError), and the isolated urllib probe gets
"Operation not permitted" instead of the expected "Connection refused".
All remaining tests passed except one pre-existing optional skip. The full
suite still needs a run where loopback sockets are permitted.

## Evals

`npm_config_offline=true sh evals/eval.sh` could not resolve npm metadata
(ENOTCACHED). Ran the same installed Promptfoo 0.125.1 package directly:

```sh
PROMPTFOO_PYTHON=$PWD/.venv/bin/python PROMPTFOO_DISABLE_TELEMETRY=1 PROMPTFOO_CONFIG_DIR=/tmp/foreman-86-promptfoo node /Users/booko/.npm/_npx/cd33a14de7de0b1b/node_modules/promptfoo/dist/src/entrypoint.js eval -c evals/promptfooconfig.yaml
```

```text
13 passed (100%)
0 failed (0%)
0 errors (0%)
```

`git diff --check` passed. `git diff --exit-code -- src/foreman/models.toml`
confirmed the model configuration was unchanged. No checkout commit or
publication was performed.

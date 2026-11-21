# Worker grant verification

The change adds immutable stage grants, checkpoint records, conservative check
parsing, and adapter permission mapping. No model configuration was changed.
No commits or publication were performed.

## Regression evidence

- `grant-red.txt`: before production edits, the unsafe live check reached graph
  compilation (`AssertionError: graph compiled`), the method result lacked
  `read_roots`, and the grant API did not exist.
- `quoted-operator-red.txt`: a quoted `&&` was incorrectly interpreted as a
  command separator. The final parser rejects that ambiguous input.
- `newline-red.txt`: whitespace-only newlines were incorrectly accepted during
  parser simplification. The final parser rejects them before empty-check handling.
- `grant-green.txt`: 44 grant, adapter, and stage record tests pass. Coverage
  includes all five worker stages, implementation review fixes and quota
  fallback, JSON round trips, legacy checkpoints, method paths under resolved
  read roots, and permission modes chosen from the supplied grant.
- `runner-checks.txt`: five existing runner tests pass after shell-program
  checks were moved into fixture scripts. Retry, environment probing, process
  resume, and exactly-once check effects retain their original assertions.
- `pyright.txt`: strict type checking passes with zero diagnostics.
- `diff-check.txt`: `git diff --check` output (empty on success).

## Execution environment

The sandbox rejects loopback socket binding. The standard unit command fails
while importing `tests/__init__.py`; see `direct-unit-tests.txt`.
`run-unit-tests.py` executes that same offline policy after replacing only its
closed proxy-port allocation with a fixed loopback discard port. The network
allowlist and audit hook remain active. `offline-bootstrap/sitecustomize.py`
applies this initialization to child Python processes as well. Production code
and the test policy file are not modified by the launcher.

Reproduce the broader suite under this environment with:

```sh
PYTHONPATH="$PWD/evidence/offline-bootstrap" .venv/bin/python \
  evidence/run-unit-tests.py discover -s tests
```

`unit-tests-final.txt` records 370 tests: 366 passed, one skipped, and three
environment-related failures (one failure and two errors). Two loopback-server tests
cannot bind in this sandbox, and a transport test expects "Connection refused"
where the sandbox reports "Operation not permitted". The optional real pinned
skill checkout test is skipped when `FOREMAN_PINNED_SKILL_REPO` is absent.

`sh evals/eval.sh` cannot download Promptfoo because npm DNS/network access is
unavailable (`evals.txt`); the package is also absent from the local npm cache
(`evals-offline.txt`). `run-eval-provider.py` runs every configured scenario
against the actual Python provider and checks its equals/contains assertions.
All 13 scenarios pass in `evals-provider.txt`. This exercises the provider,
not the unavailable Promptfoo process.

Other reproduction commands:

```sh
.venv/bin/python evidence/run-unit-tests.py \
  tests.test_grant tests.test_adapters tests.test_stage_records
.venv/bin/pyright --pythonpath .venv/bin/python
.venv/bin/python evidence/run-eval-provider.py
git diff --check
```

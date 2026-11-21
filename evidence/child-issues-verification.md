# Issue 103 verification

Changes are in the provided checkout. No repository commit, publishing, or merge
arming was performed. Model configuration was not edited.

- `python evidence/verify-child-issues.py test_github.py`: **40 passed**.
  Exercises two-page relationship and marker reads, distinct ids and numbers,
  exact no-parent handling, failed reads, literal redacted PATCH bodies, parent
  conflicts, child counts and each injected write failure before/after commit.
- `python evidence/verify-child-issues.py test_redaction.py`: **13 passed**.
- `python evidence/verify-child-issues.py test_fixture_manifest.py`: **17 passed**.
  Fixture inventory and manifest schema/provenance checks pass.
- `python -m pyright --pythonpath .venv/bin/python`: **0 errors, 0 warnings**.
  The interpreter argument is required here: bare pyright does not discover the
  checkout's installed dependencies in this environment.
- `git diff --check`: **passed**.

The shell sandbox forbids binding even loopback sockets. An unmodified
`python -m unittest discover -s tests` fails importing `tests/__init__.py`, which
selects an unused proxy port by binding a loopback socket. The evidence harness
mocks only bind/getsockname during that import, restores them before discovery,
and leaves the existing offline audit policy active. It does not modify any
test or application method. Focused pass claims above use this harness.

Full discovery with the same import workaround ran **374 tests**: 8 subtest
failures, 2 errors, 1 skipped. Every failing trace reports a sandbox socket
restriction: direct loopback server tests fail, subprocess imports hit the same
setup bind, and an offline transport test receives `Operation not permitted`
instead of its expected `Connection refused`. The full suite was run before
one final fake preset allocation correction; the Forge and manifest suites
were rerun after that correction. See `child-issues-unittest.log` and
`child-issues-direct-unittest.log`. A full normal-environment pass remains
unverified.

`sh evals/eval.sh` could not start Promptfoo because npm resolution failed with
`ENOTFOUND registry.npmjs.org`; see `child-issues-evals.log`.

The real no-parent HTTP error body was captured through the GitHub connector
from `GET /repos/octocat/Hello-World/issues/1347/parent`. The CLI network probe
was blocked, so the diagnostic uses the captured message/status and the
versioned gh formatter source. Other new API fixtures are explicitly marked
as published schema examples with synthetic pagination/errors, not live writes.
All five fixtures are registered in `tests/fixtures/manifest.toml`.

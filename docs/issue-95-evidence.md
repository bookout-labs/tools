# Issue 95 verification

- `python -m unittest discover -s tests && pyright && sh evals/eval.sh`: blocked at suite initialization by sandbox `PermissionError` on `tests/__init__.py`'s localhost socket bind. No repository workaround was added.
- `VIRTUAL_ENV="$PWD/.venv" PATH="$PWD/.venv/bin:$PATH" pyright`: passed, 0 errors and 0 warnings.
- Watch integration tests: 10 passed. They exercise SQLite reopen, crash deduplication, clearing an earlier watch, repeated Git halts, durable pre-graph skill failure, CLI targets, report ordering, error retries and exit codes, skipped/dry runs, and LiveForge redaction on both writes.
- To execute tests here, a temporary `sitecustomize.py` suppresses only the forbidden unused-proxy bind in `tests/__init__.py`; it preserves the suite's external-network audit. Repository test initialization is unchanged.
- The broader run excludes exactly three environment-dependent tests: `test_guard_rejects_external_python_sockets`, `test_subprocess_transports_fail_fast_and_local_git_still_works`, and `test_auth_failure_under_pty_returns_without_prompting`. The sandbox denies their socket operations (including localhost), producing `Operation not permitted` rather than the expected connection refusal.
- Broader result: 385 tests ran in 112.287s; OK (1 existing skip), with only the three sandbox-dependent tests above excluded. All 10 watch tests also passed separately on the final code.
- `sh evals/eval.sh`: npm registry DNS resolution is unavailable. Running the same script with a temporary `npx` wrapper selecting the locally cached **0.125.1** Promptfoo package and the proxy-bind harness passed all 13 cases (0 failures, 0 errors). Promptfoo's local output was redirected outside the checkout.
- `git diff --check`: passed. The patch has 348 changed lines, including the new tests and this evidence (limit: 400).
- No models configuration was changed. No commit, publication, or merge was performed.

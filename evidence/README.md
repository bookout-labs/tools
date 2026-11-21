# Review context verification

The implementation and tests are uncommitted. No publishing or merge action ran.

## Regression evidence

`review-context-before.txt` records the failing-before discovery: the requested
`foreman.review_context` module did not exist. `review-context-after.txt` records
all nine behavior tests passing after implementation. The integration tests use
real temporary Git repositories with bare origins; no workspace or Git responses
are mocked. They check merge-base selection after main advances, committed trees
despite dirty working files, all requested change kinds, exact text line counts,
unusual path characters, empty/binary-only trees, inspection errors, and the
scripted context against an applied `tests/fixtures/review.diff`.

The sandbox rejects the existing offline guard's loopback-port allocation in
`tests/__init__.py`. `run-tests.py` substitutes only that initial allocation,
retaining the audit guard and actual socket behavior in the tests. Run from the
checkout root:

```sh
python evidence/run-tests.py test_review_context.py
python evidence/run-tests.py test_fixture_manifest.py
pyright --pythonpath "$PWD/.venv/bin/python"
python evidence/capture-review-context.py
git diff --check
```

These checks passed: nine review-context tests, 17 fixture-manifest tests, strict
pyright (zero errors or warnings), the three Git captures, and whitespace checks.
The explicit pyright interpreter selects the installed checkout dependencies;
plain pyright does not discover them in this environment.

## Broader checks and environment limits

`unittest-standard.txt` records `python -m unittest discover -s tests` failing at
the offline guard's port allocation (`PermissionError: Operation not permitted`).

`unittest-sandbox.txt` records full discovery with the same initial-allocation
substitution as `run-tests.py` (verbosity 1): 372 tests ran, one skipped, eight
subtest failures and two errors. Every failure/error traces to sandbox socket
restrictions: four crash-recovery subtests and three runner subtests import the
offline guard in fresh Python processes; two tests require real loopback servers;
one transport check expects connection refusal but gets permission denied. No
production code or existing tests were changed to mask these restrictions.

`evals.txt` records `sh evals/eval.sh` failing before evaluation because npm could
not resolve `registry.npmjs.org` to download promptfoo (`ENOTFOUND`).

## Captured Git facts

`git-captures.txt` records Git 2.56.0 argv, exit statuses and exact stdout bytes.
`capture-review-context.py` reproduces the three raw `.z` fixtures recorded as
`captured_run` entries in `tests/fixtures/manifest.toml`: grep's
`revision:path NUL count LF` layout, empty stdout with exit 1 for no text lines,
and rename's `R100 NUL old-path NUL new-path NUL` layout. All captures use only
synthetic local data.

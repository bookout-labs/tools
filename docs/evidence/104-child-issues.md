# Issue 104 verification

Implemented child identity checkpoints, header migration, relationship reconciliation,
and readiness gating in the live runner. The existing Forge contract and recorded
GitHub evidence in `tests/fixtures/manifest.toml` were reused; no live issue writes
were made to verify this change.

## Results

- `pyright --pythonpath .venv/bin/python`: **0 errors, 0 warnings**.
- Focused runner behavior suite: **15 tests passed**. It exercises checkpoint
  durability before creation, JSON/provenance preservation, dependency order,
  readiness only after a relationship read, unchanged reruns, committed writes
  with lost responses, 403/422/pre-commit timeouts, unavailable confirmation,
  conflicting parents, capacity boundaries, legacy closed children and exact
  body preservation, and CLI failure details after a resumed spec.
- Supplementary unittest run: **401 tests, OK (skipped=1)**. Seven tests requiring
  sandbox-denied network operations were explicitly excluded (listed in the
  reproducible runner below). The one unittest skip is the suite's existing
  opt-in `FOREMAN_PINNED_SKILL_REPO` skip (`requires a local checkout with the
  pstack pin`).
- All **13 configured eval cases passed**, calling the real provider and applying
  every assertion from `evals/promptfooconfig.yaml` directly.
- `git diff --check`: passed.

The standard `python -m unittest discover -s tests` could not complete: this
sandbox denies even the initial loopback proxy allocation in `tests/__init__.py`
with `PermissionError: [Errno 1] Operation not permitted`. The supplementary
runs replace only that allocation during import, then restore real socket
methods and retain the suite's external-network audit policy. The excluded tests
were attempted in an earlier broader run and failed on denied sockets or child
process imports of that same proxy setup. This is not a full standard-suite pass.

`sh evals/eval.sh` could not launch Promptfoo because npm could not resolve
`registry.npmjs.org` (`ENOTFOUND`). Direct evaluation proves the configured
provider results, but does not claim that the Promptfoo launcher passed.

The broader run also exposed the missing watcher policy for the new halt. It was
fixed by classifying `child_filing_failed` as an operator-repair halt; the watcher
behavior checks pass in the supplementary run.

No browser screen or formal properties are required. No checkout commits, publishing,
model-list edits, merge arming, or manual repairs to live #84-#86/#66 were made.
The documentation assigns those legacy repairs and parent closure to the
coordinator.

## Reproduction

Use the checkout's Python environment. The following scripts are verification
harnesses only; they do not change repository test code or application behavior.
For example, save a block to `/tmp/verify.py` and run `PYTHONPATH=. python
/tmp/verify.py` from the checkout root. PyYAML's BaseLoader keeps YAML `yes` a
string, matching the eval configuration's string assertion.

### Supplementary unittest run

```python
import sys
import socket
from unittest.mock import patch
# Only replace the offline-policy proxy allocation denied by this sandbox.
# Tests run after restoring real sockets; external-network audit stays enabled.
with patch.object(socket.socket, 'bind'), patch.object(socket.socket, 'getsockname', return_value=('127.0.0.1', 9)):
    import tests
import unittest
sys.path.insert(0, 'tests')
suite = unittest.defaultTestLoader.discover('tests')

blocked = {
    'test_dispatch_recovery.DispatchRecoveryTests.test_fresh_process_crash_after_success_or_quota_never_replays',
    'test_dollar_allowance.DollarAllowanceTests.test_fresh_process_cli_resume_preserves_allowance_before_dispatch',
    'test_offline.OfflineSuiteTest.test_guard_rejects_external_python_sockets',
    'test_offline.OfflineSuiteTest.test_subprocess_transports_fail_fast_and_local_git_still_works',
    'test_publication.PublicationTest.test_auth_failure_under_pty_returns_without_prompting',
    'test_runner.LiveRunnerTest.test_fresh_process_retries_quota_and_an_older_merge_checkpoint',
    'test_runner.LiveRunnerTest.test_second_process_sees_the_saved_stop_and_answer_resumes_spec',
}
def flatten(items):
    for item in items:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item
all_tests = list(flatten(suite))
print('Sandbox-excluded tests (previous run failed on socket restrictions):', flush=True)
for item in all_tests:
    if item.id() in blocked:
        print(item.id(), flush=True)
result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(item for item in all_tests if item.id() not in blocked))
sys.exit(not result.wasSuccessful())
```

### Focused feature run

```python
import sys
import socket
from unittest.mock import patch
with patch.object(socket.socket, 'bind'), patch.object(socket.socket, 'getsockname', return_value=('127.0.0.1', 9)):
    import tests
import unittest
sys.path.insert(0, 'tests')
from test_runner import LiveRunnerTest
names = [
    'test_load_parent_and_file_slices_only_once',
    'test_child_plan_survives_interruption_and_recovers_a_closed_created_child',
    'test_committed_child_writes_with_lost_responses_are_recovered',
    'test_child_link_refusals_halt_on_stdout_and_retry_only_the_missing_link',
    'test_child_link_success_requires_a_confirming_read',
    'test_resumed_spec_prints_child_filing_failure_detail',
    'test_unavailable_confirmation_is_recovered_from_the_parent_list',
    'test_multiple_blockers_keep_the_spec_dependency_order',
    'test_a_child_with_another_parent_is_not_reparented',
    'test_child_capacity_is_checked_for_the_whole_plan_before_creating',
    'test_capacity_counts_existing_planned_children_once',
    'test_legacy_children_keep_their_original_bodies_when_linked',
    'test_link_failure_leaves_child_blocked_and_parent_retry_repairs_it',
    'test_existing_seam_resolves_in_workspace_and_skips_a_dependency',
    'test_self_built_seam_is_not_a_dependency',
]
result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(LiveRunnerTest(name) for name in names))
sys.exit(not result.wasSuccessful())
```

### Direct configured evals

```python
import socket
from unittest.mock import patch
with patch.object(socket.socket, 'bind'), patch.object(socket.socket, 'getsockname', return_value=('127.0.0.1', 9)):
    import tests
import yaml
from evals.provider import call_api
from pathlib import Path
cases = yaml.load(Path('evals/promptfooconfig.yaml').read_text(), Loader=yaml.BaseLoader)['tests']
for case in cases:
    result = call_api(case['vars']['case'], {}, {'vars': case['vars']})
    assert 'output' in result, result
    for check in case['assert']:
        if check['type'] == 'equals':
            assert result['output'] == check['value'], (case, result)
        elif check['type'] == 'contains':
            assert check['value'] in result['output'], (case, result)
        else:
            raise AssertionError(check)
    print('PASS', case['vars']['case'])
print(f'{len(cases)}/{len(cases)} configured eval cases passed (direct provider; Promptfoo launcher unavailable).')
```

```text
PASS clear
PASS spec-ready
PASS parent
PASS parent-spec
PASS spec-gate
PASS question
PASS failover
PASS score-approve
PASS score-block
PASS score-thin
PASS sample
PASS proposal
PASS review-converges
13/13 configured eval cases passed (direct provider; Promptfoo launcher unavailable).
```

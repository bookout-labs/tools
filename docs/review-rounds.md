# Converging review rounds

The delivery graph owns routing and synchronous checkpoints. `ReviewOutput` owns
worker output validation. `ReviewHistory` owns round IDs, resolution accounting,
settled findings and blocking-kind recurrence. `Workspace` reads both the initial
PR diff and subsequent interdiffs. Forge posts the validated review at its pinned
head. The failure cap remains the existing shared verify, proof and review cap.

## Spec check

- Extend S10's `Finding` with ID, kind, fix and a nullable ruling reference. Keep
  its bounded summary for inline reviews. Remove the worker's verdict field and
  derive the verdict from new findings and explicit unresolved statuses.
- Resolution entries reference stable finding IDs. Missing, duplicate, unknown
  resolutions and reused finding IDs fail closed. Unresolved findings keep their
  original locations and do not need to lie in the current interdiff.
- Semantic disagreement belongs to the reviewer. Its typed `ruling_id` identifies
  the ruling it contradicts. The graph validates that reference and converts the
  finding to a coordinator note. Resolved findings become rulings named
  `finding:<id>`. Design answers become typed rulings for later rounds.
- The initial review uses S10's three-dot PR diff. Later rounds use a two-commit
  diff. Interdiff exclusions become coordinator notes, even for blocking findings.
  Recurrence checks only accepted new blocking findings against all earlier
  accepted blocking findings, including resolved ones. An unresolved old finding
  does not count as a new recurrence.
- Every round stores its pinned head, accepted findings, resolution statuses and
  coordinator notes in one checkpoint update. A crash after that checkpoint
  resumes the pending implementation without losing the previous reviewed head.
  A crash before a review checkpoint can repeat the external review post, as in
  the existing S10 behavior. This change does not add Forge idempotency.
- `design_rethink` interrupts for a human answer without charging a failure round.
  The answer resumes implementation and becomes a settled ruling. Without a
  checkpointer the graph stops with that halt.

## Design choice

A flat list of mutable finding statuses would scatter round reconstruction across
prompt construction, recurrence checks and resume. Instead, round records retain
new findings and explicit resolutions. The unresolved set and settled entries
are derived from those records. This follows Model the Domain and leaves one
owner for the convergence rules. No second findings contract is introduced.

The throughput checkpoint was one contract/history unit followed by graph
integration, behavioral tests, then local and hosted verification. No independent
subagents were used because the worker brief forbids them.

## Statechart and transition checks

[The chart](review-rounds.mmd) was written before implementation. Tests derive
from these transitions. The statechart helper generated TLA+ files in scratch
space but could not execute TLC because `tla2tools.jar` is unavailable. This is
a tested statechart, not a claim of formal verification.

| Transition | Test |
| --- | --- |
| Start to reviewing | `test_clear_change_skips_spec` |
| Findings need fixes | `test_nits_converge_and_outside_interdiff_becomes_note` |
| Fixes ready for checking | `test_nits_converge_and_outside_interdiff_becomes_note` |
| Check passes to reviewing | `test_nits_converge_and_outside_interdiff_becomes_note` |
| Check fails below cap | `test_verify_cap_stops_without_review` |
| Check cap reached | `test_verify_cap_stops_without_review` |
| No unresolved findings to merge | `test_nits_converge_and_outside_interdiff_becomes_note` |
| Review cap reached | `test_unresolved_blocking_finding_stays_blocking_until_cap` |
| Blocking kind recurs to human | `test_recurrence_pauses_without_consuming_round_or_patching` |
| Design answer to fixing | `test_design_answer_resumes_implementation` |
| Invalid resolution or diff stops | `test_missing_duplicate_unknown_resolutions_and_reused_ids_fail_closed`, `test_malformed_diff_and_unknown_ruling_fail_closed` |
| Resume saved round | `test_resume_saved_round_preserves_findings_and_reviewed_head` |
| Merge armed to done | `test_nits_converge_and_outside_interdiff_becomes_note` |

The chart abstracts model failover, proof gates, Forge retries and git transport.
Existing delivery and runner tests exercise those paths. No new test requires
repository history beyond the checked-out commit.

## Validation

The original failure replay on merge base `ddd40cf` returned `review_cap` after
three reviews. With the same first nit and a later nit outside the interdiff, the
new graph returned `verified`, merge `armed`, after two reviews.

Run `uv run python -m unittest discover -s tests`, `sh evals/eval.sh`, and the
README dry run. The focused new tests enter the live graph through its worker
boundary and parse fixture responses through the production `ReviewOutput`.
The git test creates its own diverged commits and checks that the two-commit
interdiff differs from the three-dot PR diff and is empty for equal SHAs.

# Publication recovery

This statechart abstracts publication recovery. The [native delivery graph](graph/delivery.mmd) shows the graph topology.

The checkpoint owns the attempt's starting HEAD, branch, dispatch result, and candidate tree. Git owns commits and remote refs. Forge owns the open PR. Preparation precedes worker edits. Reconciliation captures the candidate tree, which must pass the repository check before Foreman commits it. An unexpected worker commit stops publication and stays on the branch. Commit recovery checks the expected parents, full message, and checked tree. Push compares the remote branch SHA. PR lookup matches the repository, head, and base.

New delivery branches start at the current origin base fetched during preparation. The fetch uses repository-scoped App credentials when configured and resolves the fetched ref once to a full SHA. Stale or missing local base branches have no effect. This policy also applies to tools itself and clean detached checkouts whose HEAD is newer or divergent. Detached commits and local main remain unchanged.

An existing delivery branch retains its inspected HEAD for correction and repair attempts, after any repair refresh. If preparation was interrupted after branch creation but before checkpointing, replay reuses that branch without fetching a new parent. When the checkout is not already attached to the delivery branch, that branch must equal the fetched origin base tip before switching to it. Otherwise preparation preserves the checkout and asks the operator to switch to the delivery branch to reuse it or delete it before rerunning. A checkpointed attempt retains its saved parent when origin advances.

The attempt parent and PR comparison base serve different purposes. Reconciliation fetches origin again and computes the merge base for the entire PR. A force rewind can yield a comparison base older than the attempt parent. Unrelated rewritten history or missing shallow ancestry halts with the checkout preserved and instructions to fetch the needed history. Fresh depth-one clones work without automatic deepening.

Smoke compares its fixture against the merge base with the origin ref fetched during preparation, excluding unrelated origin changes between clone and prepare.

```mermaid
stateDiagram-v2
    [*] --> Preparing
    Preparing --> Editing: clean checkout, create or reuse slice branch
    Preparing --> Paused: git failure
    Preparing --> Rejected: unowned initial edits
    Editing --> Reconciling: edits and dispatch result checkpointed
    Editing --> Paused: quota, save owned edits fingerprint
    Editing --> UsageUnknown: crash leaves pending dispatch
    UsageUnknown --> [*]: stop without redispatch
    Reconciling --> Checking: capture the candidate tree
    Reconciling --> Paused: unexpected commit or changed branch
    Reconciling --> Rejected: no changes
    Reconciling --> Paused: git failure
    Checking --> Editing: check fails with rounds remaining
    Checking --> Committing: check passes on unchanged tree
    Committing --> Publishing: Foreman commit succeeds or expected commit recovered
    Committing --> Paused: lock, timeout, or candidate mismatch
    Publishing --> Opening: remote already matches or push succeeds
    Publishing --> Paused: push fails or times out
    Opening --> Reviewing: matching open PR exists or create succeeds
    Opening --> Paused: create fails without matching PR
    Opening --> Paused: existing PR targets wrong base
    Paused --> Preparing: retry branch preparation
    Paused --> Editing: quota retry on unchanged owned partial edits
    Paused --> Reconciling: retry reconciliation with checkpointed worker output
    Paused --> Committing: retry using the checked tree
    Paused --> Publishing: retry push after commit
    Paused --> Opening: rerun sees pushed SHA
```

Only one writer owns a checkout. Fingerprints reject foreign edits after a quota stop and changed implementation edits after reconciliation. An interrupted dispatch with unknown usage stops as `usage_unknown` without repeating the worker. A quota stop can continue unchanged partial edits. Publication Git and gh calls have timeouts. Git disables credential prompts, and publication gh calls receive no stdin. Recoverable failures halt for explicit retry.

## Transition tests

All names below are methods in `tests/test_publication.py`.

| Transition or crash window | Test |
| --- | --- |
| New attempt to prepared branch | `test_graph_commits_and_pushes_before_opening_one_pull_request` |
| Prepared branch exists, retry preparation | `test_existing_branch_at_expected_base_is_reused` |
| Branch created before checkpoint, crash and retry | `test_crash_windows_resume_without_duplicate_completed_work` |
| Preparation lock, halt and retry | `test_index_lock_is_resumable` |
| Worker edits to checkpointed result | `test_graph_commits_and_pushes_before_opening_one_pull_request` |
| Crash during worker dispatch, stop with `usage_unknown` without redispatch | `test_crash_during_worker_stops_with_unknown_usage` |
| Quota halt, retry same partial edits | `test_quota_resume_guards_preserve_attempt_and_partial_tree` |
| Changed edits, wrong branch or HEAD, or a failing setup after quota halt, reject | `test_quota_resume_guards_preserve_attempt_and_partial_tree` |
| Unowned initial edits, reject | `test_dirty_initial_worktree_has_specific_detail` |
| Unexpected local commit, halt and preserve the branch | `test_worker_commits_are_preserved_without_publication` |
| Foreman's merge commit finishes before checkpoint, recover exact parents and tree | `test_IM4_a_merge_commit_that_landed_before_its_checkpoint_is_adopted_once` |
| Restore rejected worker history as edits, rerun to verified | `test_recovery_with_edits_uses_reconciled_fingerprint` |
| Reconcile git timeout or lock, halt and retry reconciliation | `test_reconcile_git_errors_remain_git_failed` |
| Extra commit after reconciliation, halt before commit | `test_reconciled_candidate_mutation_is_rejected_before_publication` |
| Worker shows that no change is justified, stop without pushing | `test_no_change_with_evidence_on_a_first_attempt_stops_without_pushing` |
| Worker answer that edit's check rejects (a clean tree without no_change evidence, or no_change with edits), one repair turn in its session, no gate | `test_an_invalid_no_change_answer_gets_one_repair_turn_and_no_gate` |
| Second invalid answer, including no_change on a repair, stop with invalid_output, nothing more pushed | `test_a_second_invalid_no_change_answer_stops_with_invalid_output` |
| Checkout curated back to the attempt parent after a rejection, halt, rerun asks the builder again | `test_a_checkout_curated_to_the_attempt_parent_asks_the_builder_again` |
| Repository check fails, repair in edit, publish the receipt's tree | `test_failed_repository_check_is_repaired_and_its_receipt_tree_is_published` |
| Check failures or a tree-changing check reach the cap, publish nothing | `test_check_cap_and_tree_changing_checks_never_publish` |
| Implement question pauses, posted answer resumes edit or cancel ends the run | `test_implement_question_waits_for_a_posted_answer_or_cancel` |
| Completed worker checkpoint, crash before commit, retry commit | `test_crash_windows_resume_without_duplicate_completed_work` |
| Commit lock, halt and retry commit | `test_index_lock_is_resumable` |
| Commit finishes before checkpoint, crash and recover | `test_crash_windows_resume_without_duplicate_completed_work` |
| Commit to pushed remote | `test_graph_commits_and_pushes_before_opening_one_pull_request` |
| Rejected push, halt and retry | `test_publication_failures` |
| Missing remote, halt | `test_publication_failures` |
| Wrong push URL, reject before network | `test_publication_failures` |
| Push finishes, crash and skip duplicate push | `test_crash_windows_resume_without_duplicate_completed_work` |
| Missing PR, verify and prove before create | `test_graph_commits_and_pushes_before_opening_one_pull_request` |
| PR create failure, halt and retry | `test_rerun_retries_publication_without_rerunning_the_worker` |
| PR creation finishes, crash and reuse PR | `test_crash_windows_resume_without_duplicate_completed_work` |
| Crash after commit, push or PR create, replay twice with matching, mismatched or unreadable observation | `test_publication_crashes_replay_twice_against_each_observation` |
| Existing PR on wrong base, halt | `test_wrong_base_open_pr_stops_without_creation` |
| Review changes, next attempt on same branch and PR | `test_second_attempt_adds_a_commit_to_the_same_pull_request` |
| Fetch, push, ls-remote and commit timeout, typed failure | `test_git_calls_disable_prompts_and_bound_network_and_commit` |
| Authentication fails under a pseudo-terminal, return | `test_auth_failure_under_pty_returns_without_prompting` |
| Repository default drives branch, PR and merge | `test_repository_default_branch` |

# Statuses for a two-commit comparison (Git 2.56.0)

[diff-format.adoc](https://github.com/git/git/blob/v2.56.0/Documentation/diff-format.adoc#L50-L64)
lists A/C/D/M/R/T/U/X and describes U as:

> file is unmerged (you must complete the merge before it can be committed)

[diff-options.adoc](https://github.com/git/git/blob/v2.56.0/Documentation/diff-options.adoc#L605-L606)
says:

> copied and renamed entries cannot appear if detection for those types is disabled.

[diff.c](https://github.com/git/git/blob/v2.56.0/diff.c#L6265-L6331)
assigns the status for each pair in `diff_resolve_rename_copy`. The fallback
unknown status is discarded by `check_pair_status`:

```c
case DIFF_STATUS_UNKNOWN:
    return 0;
```

The [name-status output loop](https://github.com/git/git/blob/v2.56.0/diff.c#L6772-L6785)
skips pairs rejected by that function. U requires an unmerged pair. The
[tree comparison](https://github.com/git/git/blob/v2.56.0/tree-diff.c#L151-L173)
creates pairs through change/add/remove callbacks from committed tree entries;
it has no unmerged index entries.

Inference for valid two-commit comparisons: emitted statuses are A/D/M/T/R/C,
with C requiring copy detection. U cannot arise from committed trees, and X
does not reach name-status output. This does not promise behavior for corrupt
objects or future Git versions. Foreman's parser still rejects unknown statuses.

# Child issues through Forge

## Reconcile pass

After the graph stops a parent as `sliced`, the runner persists `child_plan`
before the first create. Each ordered entry holds a stable marker and a child
number (initially absent). `child_issue_numbers` remains available to older
readers. The runner searches all issue states for the marker before creating an
uncheckpointed child, so a committed create with a lost response, including a
child since closed, is recovered rather than duplicated. Multiple matches
require manual repair.

Each new body starts with:

```text
Parent: https://github.com/<repo>/issues/<parent>
<!-- foreman-child:<repo>#<parent>:<ordinal> -->
```

The header has no closing keyword. Slice JSON, provenance, work classification,
and ordered blocked-by numbers follow it. For a legacy checkpoint containing
only `child_issue_numbers`, the pass adopts those numbers and prepends the
header, preserving every byte of the remaining body, including closed children.

Before creating anything, the runner lists the parent's sub-issues and refuses
a plan that would exceed 100 direct children. Existing planned relationships
count once. Listed relationships confirm ownership; otherwise the runner reads
the child's parent, refuses a different parent, adds a missing relationship, and
reads the parent again. A failed add may have committed, so the read is also
required after an error. It never blindly repeats an uncertain add.

Only after confirmation does the pass restore ordered blocked-by links and add
`factory` to independent children or children whose blockers are all closed and
whose planned blockers have merged pull requests. An unchanged rerun makes no
create, add-sub-issue, or parent-issue calls and leaves checkpoint state unchanged.
It still lists sub-issues, checks bodies, and refreshes dependency readiness.

## Refusals

All failures below return `stop: child_filing_failed`, exit 1, and print an
actionable `halt_detail` on stdout. Known child numbers remain checkpointed;
unconfirmed children receive no `factory` label, and the parent stays open.
Rerun the same command and checkpoint after repairing the cause. This retries
the reconcile pass without dispatching spec or creating checkpointed children.

| Refusal | Repair |
| --- | --- |
| 403 denial | Grant repository access and Issues: write permission, then rerun. |
| 422 validation refusal | Check hierarchy depth, ownership and capacity, repair the rejected relationship, then rerun. |
| Timeout or unconfirmed add | Read ownership before retrying. A confirmed committed link needs no second add; an absent link is added on rerun. |
| `parent_conflict` | Move the child from the reported different parent by hand; Foreman never replaces it. |
| `child_limit` | Reduce the plan or repair the hierarchy so the entire plan fits before any new create. |
| `child_identity_conflict` | Remove duplicate markers after identifying the intended child. |
| Body, dependency, create, or relationship-read failure | Resolve the reported GitHub error and rerun; markers recover uncertain creates. |

The runner never closes a sliced parent. The coordinator closes it by hand only
when every planned child is a confirmed sub-issue, every child is closed by a
pull request merged into the repository default branch, and parent acceptance
is satisfied. The coordinator must repair #84-#86 under #66 by hand as described
in [the coordinator role](../src/foreman/roles/coordinator.md).

## Forge contract

Forge uses the REST API through `gh api`:

| Method | Request | Result |
| --- | --- | --- |
| `sub_issues(repo, number)` | `GET /repos/{repo}/issues/{number}/sub_issues?per_page=100`, with `--paginate --slurp` | Flattened `LinkedIssue` records from every page |
| `parent_issue(repo, number)` | `GET /repos/{repo}/issues/{number}/parent` | A `LinkedIssue`, or `None` for the captured no-parent response |
| `add_sub_issue(repo, number, child)` | `GET /repos/{repo}/issues/{child}`, then `POST /repos/{repo}/issues/{number}/sub_issues` | Adds the child using `-F sub_issue_id=<id>` |
| `update_issue_body(repo, number, body)` | `PATCH /repos/{repo}/issues/{number}` | Replaces the body with redacted literal text using `-f body=...` |
| `marked_issues(repo, marker)` | `GET /repos/{repo}/issues?state=all&per_page=100`, with `--paginate --slurp` | Numbers whose bodies contain the exact marker, excluding PRs |

Endpoint references: [list sub-issues](https://docs.github.com/en/rest/issues/sub-issues#list-sub-issues), [get parent](https://docs.github.com/en/rest/issues/sub-issues#get-parent-issue), [get issue](https://docs.github.com/en/rest/issues/issues#get-an-issue), [add sub-issue](https://docs.github.com/en/rest/issues/sub-issues#add-sub-issue), [update issue](https://docs.github.com/en/rest/issues/issues#update-an-issue), [list repository issues](https://docs.github.com/en/rest/issues/issues#list-repository-issues), and [gh pagination and field types](https://cli.github.com/manual/gh_api).

The repository issue **number** selects the URL. The numeric global **id** from
the child's GET response supplies `sub_issue_id`; these values are distinct.
GitHub requires the child and parent repositories to have the same owner.
Forge's add method takes both numbers in one repository, satisfying that rule.
See [add sub-issue parameters](https://docs.github.com/en/rest/issues/sub-issues#parameters-for-add-sub-issue).

Fine-grained personal tokens and GitHub App user or installation tokens need
repository **Issues: read** for relationship reads and **Issues: write** for
adding relationships and updating bodies, with access to the target repository.
Public relationship reads can be unauthenticated. See [parent permissions](https://docs.github.com/en/rest/issues/sub-issues#fine-grained-access-tokens-for-get-parent-issue), [list permissions](https://docs.github.com/en/rest/issues/sub-issues#fine-grained-access-tokens-for-list-sub-issues), [add permissions](https://docs.github.com/en/rest/issues/sub-issues#fine-grained-access-tokens-for-add-sub-issue), and [update permissions](https://docs.github.com/en/rest/issues/issues#fine-grained-access-tokens-for-update-an-issue).

GitHub allows at most **100 direct children** per parent and **eight nested
levels**. See [adding sub-issues](https://docs.github.com/en/issues/tracking-your-work-with-issues/using-issues/adding-sub-issues).

An issue has one parent. Forge never sends `replace_parent`, the API option for
replacing that relationship. Repeat adds are not promised idempotent by the
[add endpoint contract](https://docs.github.com/en/rest/issues/sub-issues#add-sub-issue);
callers should read the relationship after an uncertain write before retrying.

Only `gh: No parent issue found (HTTP 404)` becomes `None`; generic 404s,
403s, 422s, malformed responses and transport errors remain `ForgeError`.
The actual no-parent payload is recorded in
[no-parent.json](../tests/fixtures/github/no-parent.json), with capture provenance
in [manifest.toml](../tests/fixtures/manifest.toml). The
[parent endpoint](https://docs.github.com/en/rest/issues/sub-issues#get-parent-issue)
documents 404 generally, so treating every 404 as absence would hide failures.
Other contract fixtures quote published schemas with explicitly synthetic pages;
no live issue mutation was used to obtain them.

`FakeGitHub` in `tests/test_runner.py` retains bodies and relationships, assigns
`id = number + 10000`, supports preset parents through `parents[(repo, child)]`
and child counts through `preset_sub_issues`, and rejects a second parent or a
101st child. `fail_next(operation, after_write=False, error=None)` injects a
one-shot error into `create_issue`, `add_sub_issue`, or `update_issue_body`.
`after_write=True` models a committed write followed by a lost response. The
default error is `ForgeError`; callers can supply any `BaseException` instance
to model interruption as well.

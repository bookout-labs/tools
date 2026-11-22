# CLI result fixtures

The recorded calls ran in separate empty temporary directories with a synthetic prompt. The native calls used JSON output, a JSON schema, and high effort. UUIDs, paths, and credential expiry are scrubbed. The Grok reasoning text is redacted. The Codex stderr fixture contains the relevant banner lines.

| Fixture | Source |
| --- | --- |
| Claude `success.json` | Claude Code 2.1.329, `--model claude-opus-5-5 --effort high`. A companion stream-JSON probe confirmed a WebFetch call to https://example.com/. Both runs exited 0 and reported `claude-haiku-5-5` and `claude-opus-5-5` in `modelUsage`. |
| Claude `is_error.json` | Claude Code 2.1.329 binary, `c6` result constructor with `subtype:"error_during_execution"` and the literal `turn_handoff: the turn was stopped before any of its calls ran`. |
| Claude `quota.json` | Claude Code 2.1.329 binary, result formatter with `is_error`, `api_error_status`, `api_error_type`, and the literal `You've hit your limit`. The binary also names `rate_limit_error`, `usage_limit_reached`, and `model_requires_usage_credits`. The synthetic `modelUsage` omits Opus to exercise quota precedence. |
| Claude `model_substitution_disabled.json` | Claude Code 2.1.329 binary, `apiError:pe?"model_substitution_disabled":"no_allowed_fallback"` and the literal `CLAUDE_CODE_NO_MODEL_FALLBACK forbids model substitution`. |
| Grok `success.json` | Grok Build 1.0.89 (28be5bb1bf65), `-m grok-4.7 --reasoning-effort high --no-subagents`. Exit 0. The CLI reported `grok-4.7-build` in `modelUsage`. |
| Grok `catalog_alias.json` | Grok Build 1.0.89 (28be5bb1bf65), the follow-up single-turn probe below. Exit 0, `stopReason: end_turn`, and sole `modelUsage` key `grok-4.7-build`. |
| Grok `is_error.json` | Grok Build 1.0.89, a recorded call with the invalid synthetic model ID `grok-foreman-fixture-invalid`. Exit 1. |
| Grok `quota.json` | Grok Build 1.0.89 binary, `RATE_LIMITED_USER_MESSAGE_OAUTH`. The API key and free-usage messages in the tests also match binary literals. The headless docs list exit 1 for errors. [Message source](https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-shell/src/sampling/error.rs#L17-L64) · [Headless emitter](https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/src/headless.rs#L351-L359) · [Exit codes](https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/docs/user-guide/14-headless-mode.md#exit-codes) |
| Native `missing_model_usage.json`, `wrong_model.json`, `zero_usage.json`, `negative_side.json`, `partial_side.json`, `fractional_side.json`, `zero_tokens_with_cost.json` and Grok `incomplete_usage.json` | Derived from each CLI's recorded success. Each changes the model or usage fields needed for the case. |
| Codex `success.json`, `success.stderr.txt`, derived `cached_success.jsonl` | Codex 0.171.0, `exec -m gpt-6.1-sol -c model_reasoning_effort=high -s read-only`. Exit 0. The banner reported the requested model and effort separately. |
| Codex `quota.stderr.txt` | Codex 0.171.0, three rendered messages with synthetic substitution values and no reset time. [Retry and quota formatters](https://github.com/openai/codex/blob/rust-v0.171.0/codex-rs/protocol/src/error.rs#L822-L977) |

## Probe arguments

The two shell variables hold the exact schema strings passed as single arguments.

```sh
triage_schema='{"type":"object","properties":{"triage":{"type":"string","enum":["needs_human"]},"slice":{"type":"null"},"human_question":{"type":"string"}},"required":["triage","slice","human_question"],"additionalProperties":false}'
answer_schema='{"type":"object","properties":{"answer":{"type":"string","enum":["ok"]}},"required":["answer"],"additionalProperties":false}'
```

Claude Code 2.1.329 `success.json` was recorded with the following argv. The companion probe kept the same tool permissions and schema, used `--output-format stream-json --verbose`, and confirmed WebFetch. The regular adapter uses `--permission-mode plan` for read-only work and supplies `CLAUDE_CODE_NO_MODEL_FALLBACK=1` in the subprocess environment.

```sh
claude --model claude-opus-5-5 --effort high --permission-mode dontAsk \
  --tools WebFetch --allowedTools WebFetch --strict-mcp-config --setting-sources '' \
  --no-session-persistence --output-format json --json-schema "$triage_schema" \
  -p 'Use WebFetch on https://example.com/ and ask it to extract the page heading. Then return the schema object with triage needs_human, slice null, and human_question set to "Which items?"'
```

These are the Grok Build 1.0.89 argv for `success.json`, `catalog_alias.json`, and `is_error.json`, in that order. They use the same flags and ordering as the read-only adapter.

```sh
grok -m grok-4.7 --reasoning-effort high --permission-mode plan --no-subagents \
  --output-format json --json-schema "$triage_schema" \
  -p 'Do not use any tools. Return triage needs_human, slice null, and human_question "Which items?" per the schema.'
grok -m grok-4.7 --reasoning-effort high --permission-mode plan --no-subagents \
  --output-format json --json-schema "$answer_schema" \
  -p 'Do not use tools or read files. Return the answer "ok" using the supplied schema.'
grok -m grok-foreman-fixture-invalid --reasoning-effort high --permission-mode plan --no-subagents \
  --output-format json --json-schema "$triage_schema" \
  -p 'Return triage needs_human, slice null and human_question "Which items?" per the schema.'
```

Codex 0.171.0 `success.json` and `success.stderr.txt` were recorded with the following argv. Codex receives its schema in the prompt until S3. The probe used extra flags to run outside a checkout with an ephemeral session.

```sh
codex exec -m gpt-6.1-sol -c model_reasoning_effort=high -c approval_policy=never \
  -c 'mcp_servers={}' --ephemeral --skip-git-repo-check --color never -s read-only \
  'Do not use tools or read files. Reply with just this JSON: {"triage":"needs_human","slice":null,"human_question":"Which items?"}'
```

With the probe schemas and a read-only call, these are the Claude and Codex adapter argv before the recording-only flags above. The command's final prompt argument is supplied by the worker.

```sh
claude --model claude-opus-5-5 --effort high --permission-mode plan \
  --output-format json --json-schema "$triage_schema" -p "$claude_prompt"
codex exec -m gpt-6.1-sol -c model_reasoning_effort=high --color never \
  -s read-only "$codex_prompt_with_schema"
```

In a temporary checkout, `/bin/sh --version` reported Bash 3.2.57(1) on macOS.
`/bin/sh -c './non-executable'` returned 126 for a script with mode 0644.
`/bin/sh -c './missing-check'` returned 127 for an absent file.
Both statuses follow POSIX.1-2017's
[Command Search and Execution, section 2.9.1.1](https://pubs.opengroup.org/onlinepubs/9699919799/utilities/V3_chap02.html#tag_18_09_01_01).

CI uses dash on Ubuntu 26.04. The runner image's
[published software list](https://github.com/actions/runner-images/blob/main/images/ubuntu/Ubuntu2604-Readme.md)
records dash `0.5.12-12ubuntu3` (image `20261116.202.1`). The CI
`Record check shell` step runs `readlink -f /bin/sh` and `dpkg-query -W dash`
to record the actual interpreter and package version on every run.
`test_check_that_cannot_start_stops_without_spending_a_round` executes the same
126 and 127 cases through `/bin/sh` on that runner. The POSIX section above
specifies 127 when command search fails and 126 when the found command cannot
execute.

## GitHub issue dependency contract

Foreman calls the GitHub REST API through `gh api`.

- `GET /repos/{owner}/{repo}/issues/{issue_number}/dependencies/blocked_by` returns issue objects. Foreman reads `number`, `title`, `repository_url`, `html_url`, and `state` from each page. [GitHub REST API 2026-03-10](https://docs.github.com/en/rest/issues/issue-dependencies?apiVersion=2026-03-10#list-dependencies-an-issue-is-blocked-by)
- `POST /repos/{owner}/{repo}/issues/{issue_number}/dependencies/blocked_by` takes the blocker's numeric `issue_id`. Foreman gets that ID from `GET /repos/{owner}/{repo}/issues/{issue_number}`. [Add a dependency](https://docs.github.com/en/rest/issues/issue-dependencies?apiVersion=2026-03-10#add-a-dependency-an-issue-is-blocked-by), [Get an issue](https://docs.github.com/en/rest/issues/issues?apiVersion=2026-03-10#get-an-issue)
- `gh api --paginate --slurp` returns an array of pages. `-F issue_id=<id>` sends the ID as a JSON integer. [GitHub CLI 2.102.0](https://github.com/cli/cli/blob/v2.102.0/pkg/cmd/api/api.go)

Read-only reachability check with `gh 2.102.0`:

```sh
gh api --method GET 'repos/bookout-labs/tools/issues/43/dependencies/blocked_by' --paginate --slurp
# [[]]
```

## Pull request size

Git 2.56.0 reported a binary numstat row for a synthetic 500-line file with one NUL byte per line. The corresponding text patch reported a 500-line hunk. The review gate reads the text hunk ranges when numstat supplies dashes.

```text
$ git show --format= --numstat --text
-	-	x
$ git show --format= --text --patch --unified=0 --inter-hunk-context=0 --no-color
...
@@ -0,0 +1,500 @@
```

[Git 2.43 fetch refspecs](https://git-scm.com/docs/git-fetch/2.43.0) document the exact base ref fetch. [Git 2.43 rev-parse](https://git-scm.com/docs/git-rev-parse/2.43.0) documents resolving the repository's attributes path with `--git-path`. [Git's diff format](https://git-scm.com/docs/diff-format/2.43.0) defines the hunk ranges.

## Redaction detector

Foreman pins detect-secrets v1.5.0 and loads its plugins using
[`default_settings()` and `get_plugins()`][settings]. It uses the full spans of
each [regex plugin's compiled `denylist`][base]. This runs locally without the
plugins' network verification. The dependency test checks the expected plugins
and pattern attributes against the pin.

A local v1.5.0 probe scanned the quoted synthetic leaves `"ghp_" + "z" * 36`
and `"glpat-" + "z" * 20` in surrounding prose. Both
[`scan_line()`][scan] and the [README's `SecretsCollection` API][readme]
returned only `ghp` and `glpat`, respectively. Substituting those returned
values left the entire synthetic suffix. The [GitHub][github] and
[GitLab][gitlab] patterns contain captures that the base class returns in
place of the full match.

The [entropy plugins][entropy] expose their compiled pattern and threshold
through the plugin instance. Foreman uses their unquoted mode and applies
their original thresholds. The [keyword plugin][keyword] has no `denylist`
attribute. In v1.5.0, `analyze_string()` defaults to
`QUOTES_REQUIRED_DENYLIST_REGEX_TO_GROUP`, a map from compiled patterns to
secret capture group numbers. Foreman uses that same upstream map and
`pattern.finditer(line)`, then `match.span(group_number)`, to locate every
assigned value without masking unrelated occurrences of the same text.
Foreman also honors the plugin's `keyword_exclude` guard. These attributes and
the default map are defined in the [pinned plugin source][keyword].

The [JWT plugin][jwt] reports the header and payload, and the
[private key plugin][key] reports armor headers. Regression tests record both
gaps. Foreman includes the signature or key block in the corresponding
replacement. Without a key footer, the replacement stops at the first blank
line.

[settings]: https://github.com/Yelp/detect-secrets/blob/v1.5.0/detect_secrets/settings.py
[base]: https://github.com/Yelp/detect-secrets/blob/v1.5.0/detect_secrets/plugins/base.py
[readme]: https://github.com/Yelp/detect-secrets/blob/v1.5.0/README.md#usage-in-other-python-scripts
[scan]: https://github.com/Yelp/detect-secrets/blob/v1.5.0/detect_secrets/core/scan.py#L109-L137
[github]: https://github.com/Yelp/detect-secrets/blob/v1.5.0/detect_secrets/plugins/github_token.py
[gitlab]: https://github.com/Yelp/detect-secrets/blob/v1.5.0/detect_secrets/plugins/gitlab_token.py
[entropy]: https://github.com/Yelp/detect-secrets/blob/v1.5.0/detect_secrets/plugins/high_entropy_strings.py
[keyword]: https://github.com/Yelp/detect-secrets/blob/v1.5.0/detect_secrets/plugins/keyword.py
[jwt]: https://github.com/Yelp/detect-secrets/blob/v1.5.0/detect_secrets/plugins/jwt.py
[key]: https://github.com/Yelp/detect-secrets/blob/v1.5.0/detect_secrets/plugins/private_key.py

## Review fixtures

`review.diff` was captured with Git 2.56.0 in a synthetic repository. The base
had a three-line `items.txt` and a one-line `deleted.txt`. The head replaced
the middle line of `items.txt` and removed `deleted.txt`.

```sh
git diff --no-color --no-ext-diff --no-textconv --no-relative --src-prefix=a/ --dst-prefix=b/ --find-renames HEAD^...HEAD --
```

The synthetic checkout test runs `git fetch --no-tags origin <base-oid> <head-oid>`
with Git 2.56.0 and checks that the fetched base commit becomes available.

LiveForge uses gh 2.102.0. The [`gh pr view` manual](https://cli.github.com/manual/gh_pr_view)
lists `baseRefOid` and `headRefOid` among its JSON fields. The
[`gh api` manual](https://cli.github.com/manual/gh_api) documents `--input -`
for sending a request body from stdin.

The [GitHub REST API 2022-11-28 review endpoint](https://docs.github.com/en/rest/pulls/reviews?apiVersion=2022-11-28#create-a-review-for-a-pull-request)
documents `POST /repos/{owner}/{repo}/pulls/{pull_number}/reviews`. Foreman
sends `commit_id`, `event: COMMENT`, `body`, and a `comments` array
with `path`, `line`, `side`, and `body` per inline comment. The
[`RIGHT` side identifies the new file](https://docs.github.com/en/rest/pulls/comments?apiVersion=2022-11-28#create-a-review-comment-for-a-pull-request).

## Review interdiff

Git 2.56.0 was exercised by `test_review_diff_fetches_an_unrelated_new_base_commit`.
The test creates all commits locally from working-tree files and runs
`git diff --no-color --no-ext-diff --no-textconv --no-relative --src-prefix=a/ --dst-prefix=b/ --find-renames <base>..<head> --`.
The two-commit diff includes a deletion from the diverged base that the
three-dot PR diff excludes. Equal SHAs return an empty patch. Both variants
fetch with the already recorded `git fetch --no-tags origin <base> <head>`.
Review-round test responses are contract-checked through `ReviewOutput` before
they enter the production parser. They cover explicit resolutions, scoped
findings, recurrence, settled notes and resume against a SQLite checkpoint.

## Closing pull request contract

Checked against GitHub GraphQL documentation on 2026-11-20 and GitHub CLI
v2.102.0 [query builder](https://github.com/cli/cli/blob/v2.102.0/api/query_builder.go#L78-L95).
The CLI requests closing references without a base-branch filter. The
[Issue field](https://docs.github.com/en/graphql/reference/issues#issue)
also admits manually linked PRs by default. A closed issue and a merged
referenced PR therefore do not establish that the work reached the default branch.
Foreman checks the closing repository's default branch, including for cross-repository references.

CLI v2.102.0 exposes `state` and `baseRefName` in `gh pr view --json`
and `defaultBranchRef { name }` in `gh repo view --json`.
[PR fields](https://github.com/cli/cli/blob/v2.102.0/api/query_builder.go#L353-L357),
[repository field](https://github.com/cli/cli/blob/v2.102.0/api/query_builder.go#L603-L604).
The tests feed those documented shapes through the production Pydantic parsers.
A merged PR targeting `release` with default branch `main` must return false;
a merged PR targeting default branch `trunk` must return true.


## Ordered check output

Python 3.11's [subprocess documentation](https://docs.python.org/3.11/library/subprocess.html#subprocess.STDOUT)
defines `stderr=STDOUT` as sending stderr to the same handle as stdout.
`Popen(stdout=PIPE, stderr=STDOUT, text=True, errors="replace")` gives Foreman
one text stream. Foreman iterates its lines and passes each to the CLI's
redacting writer before waiting for the exit code. The writer flushes every
line. An unterminated final line is emitted at EOF.

## Delivery publication

A Git 2.56.0 probe used a temporary checkout with a local `main` and a bare `origin`. These commands all exited 0.

| Command | Observed result |
| --- | --- |
| `git switch -c 55-probe refs/heads/main` | Switched to a new branch named `55-probe`. |
| `git rev-parse HEAD` | Printed a full 40-hex SHA before and after the commit. |
| `git branch --show-current` | Printed `55-probe`. |
| `git status --porcelain --untracked-files=all` | Printed `?? probe.txt` after creating the file. Printed nothing after committing it. |
| `git add --all` and `git commit -m '55: Probe'` | Staged the untracked file and created commit `cb9dc37`. |
| `git push --no-follow-tags origin refs/heads/55-probe:refs/heads/55-probe` | Created the remote branch. Repeating the command printed `Everything up-to-date`. |
| `git ls-remote origin refs/heads/55-probe` | Printed the same SHA as local HEAD, followed by a tab and `refs/heads/55-probe`. |

A push repository can be a remote name or URL. An explicit source and destination ref publish that branch. Git rejects non-fast-forward branch updates by default. `--no-follow-tags` overrides `push.followTags`. [Git 2.56.0 push documentation](https://github.com/git/git/blob/v2.56.0/Documentation/git-push.adoc)

Publication looks up the branch with `gh pr view BRANCH --repo OWNER/REPO --json number,state,headRefOid,headRefName,baseRefName,baseRefOid,isCrossRepository,autoMergeRequest`. Its captured response and command are recorded in [manifest.toml](manifest.toml).

GitHub CLI 2.102.0 documents that `gh pr create --repo OWNER/REPO --base main --head BRANCH --title TITLE --body BODY` selects the repo and both refs, supplies the title and body without prompting, and skips pushing or forking. Success prints the PR URL. Foreman leaves that output unparsed and uses `gh pr view` to resolve retries. The body uses the documented `Closes #N` syntax. [GitHub CLI 2.102.0 create command](https://github.com/cli/cli/blob/v2.102.0/pkg/cmd/pr/create/create.go)

## Publication recovery probes, 2026-11-20

Git 2.56.0 and gh 2.102.0 were used. Read-only capture commands and outputs:

```text
git remote get-url origin
https://github.com/bookout-labs/tools.git
gh repo view bookout-labs/tools --json defaultBranchRef
{"defaultBranchRef":{"name":"main"}}
gh pr view 60 --repo bookout-labs/tools --json number,state,headRefOid,headRefName,baseRefName,baseRefOid,isCrossRepository,autoMergeRequest
{"autoMergeRequest":null,"baseRefName":"main","baseRefOid":"84c08237e585eef84306d6d42ec1f2451b8a058e","headRefName":"55-graph-git-ops","headRefOid":"6e3de512844fff969c17976adc4f1df790ffff8b","isCrossRepository":false,"number":60,"state":"OPEN"}
```

Origin identity and credential-helper provenance live in [manifest.toml](manifest.toml), with the captured origin and quoted gh documentation. The graph uses the existing origin and does not mutate authentication configuration.

Git's `GIT_TERMINAL_PROMPT=0` prevents terminal authentication prompts. Fetch, push, ls-remote and commit are bounded to 30 seconds, including credential helpers, hooks and signing. Other git calls are bounded to 10 seconds. [Git 2.56.0 environment documentation](https://github.com/git/git/blob/v2.56.0/Documentation/git.adoc).

Recovery uses `git for-each-ref --format=%(refname) refs/heads/<branch>` to find an existing branch, `git merge-base --is-ancestor HEAD refs/heads/<branch>` to validate recovery from the current checkout, and `git show -s --format=%P%n%B HEAD` to compare the complete parent/message of an observed commit. [Git 2.56.0 for-each-ref](https://github.com/git/git/blob/v2.56.0/Documentation/git-for-each-ref.adoc), [merge-base](https://github.com/git/git/blob/v2.56.0/Documentation/git-merge-base.adoc), [pretty formats](https://github.com/git/git/blob/v2.56.0/Documentation/pretty-formats.adoc). The publication tests execute these production commands against local repositories. `git diff HEAD --binary --no-ext-diff --no-textconv` and `git ls-files --others --exclude-standard -z` fingerprint owned partial edits. [Git 2.56.0 diff](https://github.com/git/git/blob/v2.56.0/Documentation/git-diff.adoc), [ls-files](https://github.com/git/git/blob/v2.56.0/Documentation/git-ls-files.adoc).

The publication integration fixture substitutes only the recorded `remote get-url` identity and gh responses. Push and ls-remote still execute against its local bare origin. The auth repro uses a local HTTP 401 server under a pseudo-terminal; no GitHub graph write runs in verification.

`git remote get-url --push --all origin` also validates every configured push URL, so a separate push URL cannot redirect publication to another repository. Git 2.56.0 documents that `--push` queries push URLs and `--all` returns every URL. [Versioned remote documentation](https://github.com/git/git/blob/v2.56.0/Documentation/git-remote.adoc). The wrong push URL row in `test_publication_failures` covers a mismatched push URL.
### LangGraph typed invocation

LangGraph 1.2.24 exposes typed graph output through `invoke(..., version="v2").value`. The pinned package's [Pregel.invoke overloads](https://github.com/langchain-ai/langgraph/blob/1.2.24/libs/langgraph/langgraph/pregel/main.py) return `GraphOutput[OutputT]` for version v2 and an untyped dictionary for v1. `run_delivery` uses the graph's output schema through that API. Existing delivery tests and the README dry run exercise the real installed package.


Pydantic strict integer validation rejects fractional numbers and booleans. ModelUsage ignores extra ledger keys. `tests/test_usage.py` exercises ledger rejection without changing the envelope outcome.

### LangGraph dispatch durability

LangGraph 1.2.24 `durability="sync"` commits the setup node before entering the call node. `tests/test_dispatch_recovery.py` kills fresh processes after a successful call, an uncommitted quota, and a committed quota followed by fallback. Recovery uses `update_state(as_node="<stage>_call")` to select that node's conditional route. The pinned package's `get_state_history` returns newest checkpoints first; `_checkpoint_before_halt` takes the first matching entry. LangGraph defaults to 10007 recursion steps (overridable by LANGGRAPH_DEFAULT_RECURSION_LIMIT); delivery overrides this with the sum of stage setup and candidate counts plus 12 orchestration steps per review round.

### Git inspection

`git/grep-lines.z`, `git/grep-no-text.z`, `git/name-status-rename.z`,
`git/ls-tree.z`, `git/merge-base.txt`, `git/grep-fatal.stderr.txt`,
`git/fetch-by-sha.json`, `git/grep-basic-regexp.json`, and
`git/grep-attribute-source.json` record synthetic Git 2.56.0 runs. Recapture from the
checkout root with `python tests/capture-review-context.py`. The script uses
temporary repositories and a file-protocol origin, with fixed commit dates.
The `.z` files contain raw stdout, including NUL delimiters; the fatal fixture
contains raw stderr. The fetch transcript includes object-availability probes
before and after fetching two full SHAs into an empty repository. Exit statuses,
argv, and carried claim IDs are in `manifest.toml`. `git/name-status-types.md`
records versioned source evidence for the status set.
The grep transcripts compare configured pattern modes and local attributes
against the explicit basic-regexp and committed-tree attribute options.

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
| Native `missing_model_usage.json`, `wrong_model.json`, `zero_usage.json` and Grok `incomplete_usage.json` | Derived from each CLI's recorded success. Each changes the model or usage fields needed for the case. |
| Codex `success.json`, `success.stderr.txt` | Codex 0.171.0, `exec -m gpt-6.1-sol -c model_reasoning_effort=high -s read-only`. Exit 0. The banner reported the requested model and effort separately. |
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

# Skill export fixture

Git 2.56.0 ran the following commands against a temporary repository. The archive contained `example/SKILL.md` at its root, with no `pstack/skills/` prefix.

```sh
git -C "$source" init --quiet
git -C "$source" add -- pstack
git -C "$source" -c user.name=Foreman -c user.email=foreman@example.invalid -c commit.gpgSign=false commit --quiet -m 'Synthetic skill fixture'
pin=$(git -C "$source" rev-parse HEAD)
git -C "$fetch" init --quiet
git -C "$fetch" fetch --quiet --no-tags --depth=1 -- "$source" "$pin"
git -C "$fetch" archive --format=tar --output="$archive" FETCH_HEAD:pstack/skills
```

`git ls-remote https://github.com/cursor/plugins HEAD` returned
`d42aff08aa58965d2735c4ead61a9af82b89d0d0`. The same fetch and archive
commands ran successfully against that pin.

Unit tests and evals create synthetic Git repositories with `skill_fixture.py`.
Use a local checkout containing the pinned commit to inspect the real method
text without fetching from the network during tests.

```sh
FOREMAN_PINNED_SKILL_REPO=/path/to/cursor/plugins \
  python -m unittest discover -s tests -p test_skill_rules.py
```

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

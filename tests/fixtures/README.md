# CLI result fixtures

The recorded calls ran in separate empty temporary directories with a synthetic prompt. The native calls used JSON output, a JSON schema, and high effort. UUIDs, paths, and credential expiry are scrubbed. The Grok reasoning text is redacted. The Codex JSONL recordings retain only synthetic prompts and scrubbed thread IDs.

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
| Codex `success.jsonl` | `codex --version` reported `codex-cli 0.171.0`. The real triage schema was passed as a file. Exit 0. |
| Codex `invalid-model.jsonl` | Codex CLI 0.171.0. The same probe used `gpt-foreman-fixture-invalid`. Exit 1 with a structured unsupported-model failure. |
| Codex `quota.jsonl` | Codex CLI 0.171.0. A loopback Responses API stub returned HTTP 429 with `error.code` and `error.type` set to `insufficient_quota`. Exit 1. |

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

With the probe schema and a read-only call, this is the Claude adapter argv before the recording-only flags above. The worker supplies the final prompt argument.

```sh
claude --model claude-opus-5-5 --effort high --permission-mode plan \
  --output-format json --json-schema "$triage_schema" -p "$claude_prompt"
```

## Codex JSONL

Generate `triage.json` with `json.dumps(STAGE_OUTPUTS["triage"].model_json_schema())` from `foreman.stage_output`.
The success probe used this argv in a temporary directory with stdin closed.

```sh
codex exec -m gpt-6.1-sol -c model_reasoning_effort=high -c approval_policy=never \
  -c 'mcp_servers={}' --ephemeral --skip-git-repo-check --json --output-schema triage.json \
  --color never -s read-only \
  'Do not use tools or read files. Choose needs_human, with no slice, and ask "Which items?"'
```

For the invalid-model probe, replace the `-m` value with `gpt-foreman-fixture-invalid`. For the quota probe, use an empty `CODEX_HOME`, set `FOREMAN_PROBE_KEY` to a synthetic value, and replace `-c 'mcp_servers={}'` with the following arguments. Substitute the stub's port for `<port>`.

```sh
-c model_provider=synthetic \
-c 'model_providers.synthetic={name="Synthetic", env_key="FOREMAN_PROBE_KEY", base_url="http://127.0.0.1:<port>/v1", request_max_retries=0, wire_api="responses"}'
```

The adapter uses the following facts from the captures and Codex tag `rust-v0.171.0`.

- `--json` emits events to stdout. `--output-schema` reads the schema file, and `--ephemeral` disables session persistence. [Exec CLI arguments](https://github.com/openai/codex/blob/rust-v0.171.0/codex-rs/exec/src/cli.rs#L63-L93)
- `thread.started.thread_id` identifies the session. The last `item.completed.item` of type `agent_message` supplies `text`, a JSON string with a schema. [Event types](https://github.com/openai/codex/blob/rust-v0.171.0/codex-rs/exec/src/exec_events.rs#L8-L101) · [Agent messages](https://github.com/openai/codex/blob/rust-v0.171.0/codex-rs/exec/src/exec_events.rs#L104-L140) · [Final message selection](https://github.com/openai/codex/blob/rust-v0.171.0/codex-rs/exec/src/event_processor_with_jsonl_output.rs#L388-L403)
- `turn.completed.usage` reports `input_tokens`, `output_tokens`, `cached_input_tokens`, and `cache_write_input_tokens`. Codex emits a zero-filled report if it has no totals. [Usage payload](https://github.com/openai/codex/blob/rust-v0.171.0/codex-rs/exec/src/exec_events.rs#L49-L73) · [Usage defaults](https://github.com/openai/codex/blob/rust-v0.171.0/codex-rs/exec/src/event_processor_with_jsonl_output.rs#L119-L130)
- `turn.failed.error.message` gives the terminal error. `error.message` can precede a retry, so it supplies a diagnostic when a turn fails without a terminal message. Both event shapes omit the upstream error code. [JSONL conversion](https://github.com/openai/codex/blob/rust-v0.171.0/codex-rs/exec/src/event_processor_with_jsonl_output.rs#L450-L460) · [Terminal errors](https://github.com/openai/codex/blob/rust-v0.171.0/codex-rs/exec/src/event_processor_with_jsonl_output.rs#L528-L560) · [Retry and exit handling](https://github.com/openai/codex/blob/rust-v0.171.0/codex-rs/exec/src/lib.rs#L1472-L1491)
- Quota checks use the formatters for `RateLimitExceeded`, `QuotaExceeded`, retry exhaustion with HTTP 429, and the usage-limit, plan, and workspace cap variants of `UsageLimitReachedError`. [Rate limit message](https://github.com/openai/codex/blob/rust-v0.171.0/codex-rs/protocol/src/error.rs#L132-L136) · [Quota message](https://github.com/openai/codex/blob/rust-v0.171.0/codex-rs/protocol/src/error.rs#L219-L228) · [Retry and usage-limit messages](https://github.com/openai/codex/blob/rust-v0.171.0/codex-rs/protocol/src/error.rs#L823-L959)
- The success events carry no model ID, and the unknown-model probe failed with exit 1. Codex emits a `model rerouted: ` message as a completed `error` item when the server switches models. [Reroute notification](https://github.com/openai/codex/blob/rust-v0.171.0/codex-rs/exec/src/event_processor_with_jsonl_output.rs#L486-L511)

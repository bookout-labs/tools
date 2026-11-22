# Worker permissions

Claude calls use `--restricted`, `--strict-mcp-config`, `dontAsk`, an explicit tool list,
and one settings file in the run’s scratch area. Restricted mode ignores inherited settings. MCP servers cannot
extend the tool list. Read, Glob, Grep and Bash are available on every call. Implement
also approves Edit and Write within the checkout and scratch directory. Method roots
are added for reading, with absolute Edit denies covering both editing tools protecting those roots.

Implement grants one exact `Bash(<check>)` rule for the configured check string.
Foreman does not parse shell commands. The compound-check capture demonstrates the
native matcher accepting the configured compound command under one rule.
The native read-only matcher refuses the planned review diff. Review calls therefore
grant each standalone command from the bound `ReviewScope`, including pinned diffs,
tree listings and blob reads. The prompt and exact Bash rules share this command list.
Other read-only stages have no allow rules.
A command permission is not a sandbox. The check itself must be trusted.
Workers never commit, push or touch the Forge. Foreman owns those operations.

Codex uses a native permissions profile extending `:read-only`. Implement grants
writes to the checkout and its scratch directory. Method roots, `.git` and `.codex`
remain read-only. Temp directories receive no implicit write grant. Network access
is disabled. `--ignore-user-config` and `--ignore-rules` isolate inherited user
configuration and command approvals. `approval_policy=never` prevents escalation.
An invalid or unavailable native sandbox fails closed. Codex does not use a command
allowlist. The captures show `git commit` failing because `.git` is read-only.

Grok editing Calls require the separate isolated launcher slice.
Its read-only sandbox also permits temp writes, including method exports, on Linux
and macOS. macOS additionally lacks child network isolation. Until native controls
meet the boundary, every Grok Call returns `cli_missing` with its platform and
missing capability. The dispatcher tries the next model. No Grok process, prefix
grant, custom profile or new halt is introduced. `models.toml` remains Joe's decision.

Run `.venv/bin/python tests/capture-native-permissions.py codex edit` and `codex readonly`
to reproduce Codex captures. `grok readonly-gap` captures the native boundary gap
under inherited broad Bash and Edit grants. Probe credentials stay in ignored scratch.
The native kernel checks use the same Codex profile as the worker argv and record
command results independently of model prose.

Reproduce the Claude 2.1.329 captures with
`.venv/bin/python tests/capture-claude-permissions.py <mode>` for edit, inherited,
readonly and compound. Each capture contains the exact argv, result envelope and
filesystem observations. Transcript tool calls and results corroborate method reads
and denied operations. The inherited probe adds permissive project settings.
The clean edit capture proves source edits, scratch writes, and edits to pyproject.toml
and .github/workflows/ci.yml. Restricted mode can therefore remain enabled.

For the separate Grok editing slice, start with the lighter boundary. PR #144's
launcher filters command environments and brokers App credentials. It is not merged
at this slice's base and does not isolate a CLI from host credential files: it retains
HOME and intentionally supplies credentials to approved worker commands. Reuse its
environment filtering once available, but hide host gh auth, credential helpers,
App keys and SSH access from model-controlled processes too.
The workspace profile limits writes but permits child network and temporary writes.
Stable method exports outside the checkout, Grok home and temp paths, as planned
in #120 slice 13, would be read-only under workspace. They need no container just
for immutability. Exports inside a writable checkout still need a read-only boundary.
A hook can compare the whole command without shell parsing, but the official contract
fails open on crashes and timeouts. Local hook probes did not discover their guards,
so they establish no enforcement claim. Root-owned requirements can pin rules but
require operator installation; this slice installs nothing under /etc.
TanStack's Grok adapter requires an outer sandbox; Superserve isolates credentials
outside its microVM. The container capture proves filesystem mounts and network
isolation only. Recommend an outer launcher only for inaccessible host credentials and restricted
model-only egress that the lighter native boundary has not proven; read-only mounts
also protect exports when they must sit inside writable directories. A pinned Linux CLI/toolchain image and model-auth transport need their
own slice; `--network none` alone cannot run Grok's model API.

Source references are the [Grok sandbox guide](https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/docs/user-guide/18-sandbox.md),
[Grok hook contract](https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/docs/user-guide/10-hooks.md),
[TanStack adapter](https://github.com/TanStack/ai/blob/main/packages/ai-grok-build/src/adapters/text.ts),
and [Superserve credential isolation](https://www.superserve.ai/blog/introducing-secrets/).

On implement calls, Claude matches denied Bash text against `Call.checks` by literal containment in both directions.
This covers exact checks, wrappers, and compound segments without shell parsing.
Unrelated quoted commands and non-Bash denials stay `ran`. Missing, blank, or malformed evidence
fails closed on successful checked calls. On review calls, any permission denial
discards the output and requests one complete fresh review at the same scope. Malformed
denial evidence fails closed without retry. Existing failures, quota, budget, and usage stay intact.
Claude names commands, not allow rules. Obfuscated wrappers without literal check text cannot be
reliably attributed; `verify` remains authoritative. T1 joins tool IDs, successful results, and a marker.
Separate captures prove wrapper and compound-segment denials.

Failed verifier checks retain diagnostic context in `checks.failure_output`. Retries,
resume and provider failover receive it as untrusted previous-attempt data. Prepare
clears current verification and proof attestations. A pass clears the failure text.
Only the verifier check is captured, not preflight. The window keeps the first
2048 UTF-8 bytes / 32 lines and last 6144 bytes / 96 lines, with a final 8192-byte /
128-line cap including omission markers. Terminal escapes and newline variants are
normalized; empty output is labelled. This is context, not guaranteed assertion selection.

Before clipping or persistence, the full existing redactor detects provider tokens,
authorization credentials, private keys, keyword secrets and high-entropy values.
Multiline key bodies are suppressed through their footer or blank separator. Cached
Foreman App tokens and inherited GH_TOKEN/GITHUB_TOKEN values are also masked.
Arbitrary transformed secrets and unlabelled low-entropy values are not guaranteed.
No raw-output companion or environment dump is persisted. Codex still has no network
or database connections; Claude still receives only the exact check command grant.
The configured verifier remains authoritative when local reproduction is blocked.

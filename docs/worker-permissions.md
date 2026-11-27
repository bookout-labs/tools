# Worker permissions

Claude calls use `--restricted`, `--strict-mcp-config`, `dontAsk`, an explicit tool list,
and one settings file in the call's attempt directory. Restricted mode ignores inherited settings. Read, Glob and Grep
are available on every call. Implement also approves Edit and Write within the checkout and the attempt scratch.
Method roots are added for reading, with absolute Edit denies covering both editing tools protecting those roots.
Edit denies also cover the checkout's `.git` entry and the git and common directories it resolves to, each as given and
as resolved. The worker resolves them with `git rev-parse` from the checkout, ignoring any inherited `GIT_*` variable,
and a `.git` git cannot resolve fails the call closed. `permissions.blockReadsOutsideWorkingDirectories` holds Read,
Grep and Glob to the working directories, through symlinks too, in every call.

No worker has a shell on the Foreman host. The builder, the verifier and a reviewer run their commands with one tool,
Foreman's `exec` (target.yaml item 3). It is a stdio MCP server, `python -m foreman.exec_tool`, that the CLI starts
from the call's own MCP config, the only one `--strict-mcp-config` loads. Each command runs with `docker exec` in the
run's attempt container, in the checkout, allowed the contract's `check_timeout_seconds`. The tool answers with the
command's output and how it ended. Claude's tool list has no Bash, and its settings allow `mcp__foreman__exec`. A
Claude call whose denials name that tool fails, because it never got its grant. A call that runs commands while the
worker has no container returns `cli_missing` before launch.

The attempt container is the contract's image with its setup run (`foreman.container`). It mounts the checkout and
the run's scratch read-write and the clone's Git directory read-only, each at its host path, so a path in a prompt or
a capture means the same file on both sides. Its environment holds the contract's secrets by name, set to their test
values, and nothing else. No host home directory, CLI login, `gh` or Git credential, SSH agent or App key enters it.
The worker's CLI and its login stay on the host. After setup a contract with `network: setup` leaves the container
loopback only, so the builder can start the app or a server there and run the check against it, and the verifier can
drive the app the same way.

Each worker call makes its attempt directory in the run's scratch directory, which the container mounts. The
verifier's, `foreman-verify/<repo>-<issue>/<attempt>`, outlives the call so the captures it names stay readable as
evidence. Every exit before the CLI launches removes it. A run that finishes removes the scratch directory with its
checkout; a halted run keeps both as evidence. The verifier has no Edit or Write tool. A build may write ignored output
in the checkout; the verifier writes nothing there itself, and after the drive the Verify stage checks that tracked
and untracked non-ignored files are unchanged (#251). Codex and Grok calls for the verifier return `cli_missing`
before launch, because no capture yet shows either driving the app.

A reviewer's exec tool runs only the one full PR diff command from the bound `ReviewScope`, exactly as written, and
the contract's check (item 16). It refuses every other command. The prompt names the same diff command. Other
read-only stages have no exec tool.

Foreman keeps no per-file read list and parses no proof of reading. A review's answer counts from
its result envelope alone, and Claude review calls use `--output-format json` like every other call.
Workers never commit, push or touch the Forge. Foreman owns those operations.

A worker's CLI starts with the environment Foreman builds for every command it runs, not Foreman's own:
`command_environment()` with no token, then the adapter's variables, then `TMPDIR`. Claude's adapter adds `USER`, which
Claude Code needs to find its login in the keychain. So no worker's git reads settings outside the checkout:
`GIT_CONFIG_GLOBAL=/dev/null`, `GIT_CONFIG_NOSYSTEM=1` and `GIT_ATTR_NOSYSTEM=1`.

Codex uses a native permissions profile extending `:read-only`. Implement grants
writes to the checkout and its scratch directory. Method roots, `.git` and `.codex`
remain read-only. Temp directories receive no implicit write grant. Network access
is disabled. `--ignore-user-config` and `--ignore-rules` isolate inherited user
configuration and command approvals. `approval_policy=never` prevents escalation.
An invalid or unavailable native sandbox fails closed. Codex does not use a command
allowlist. The captures show `git commit` failing because `.git` is read-only.
A Codex call that runs commands, implement or a review, has its own shell off (`--disable shell_tool --disable
unified_exec`) and Foreman's exec tool on, approved by `mcp_servers.foreman.tools.exec.approval_mode`. No capture yet
shows a Codex turn under these flags. Codex has no command allowlist. The profile denies writes and the network. It does
not confine reads: in triage, research and spec a Codex worker's shell can read files anywhere on the host. That is an open gap against
the boundary the design asks for, tracked in #438, and not a boundary this page claims.
`tests/fixtures/codex/permissions/readonly.json` shows the profile denying writes (each `echo ... >` and
`git commit` fail with `Operation not permitted`) and the network (`curl` cannot connect and `gh pr list` cannot
reach api.github.com). Its read-outside probe records a read outside the roots succeeding.

Grok editing Calls require the separate isolated launcher slice.
Its read-only sandbox also permits temp writes, including method exports, on Linux
and macOS. macOS additionally lacks child network isolation. Until native controls
meet the boundary, every Grok Call returns `cli_missing` with its platform and
missing capability. The dispatcher tries the next model. No Grok process, prefix
grant, custom profile or new halt is introduced. The human decides changes to `models.toml`.

Run `.venv/bin/python tests/capture-native-permissions.py codex readonly`
to reproduce the Codex capture. `grok readonly-gap` captures the native boundary gap
under inherited broad Bash and Edit grants. Probe credentials stay in ignored scratch.
The native kernel checks use the same Codex profile as the worker argv and record
command results independently of model prose.

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

Failed verifier checks retain diagnostic context in `checks.failure_output`. Retries,
resume and provider failover receive it as untrusted previous-attempt data. Prepare
clears the previous verification result. A pass clears the failure text.
Only the verifier check is captured, not setup. The window keeps the first
2048 UTF-8 bytes / 32 lines and last 6144 bytes / 96 lines, with a final 8192-byte /
128-line cap including omission markers. Terminal escapes and newline variants are
normalized; empty output is labelled. This is context, not guaranteed assertion selection.

Before clipping or persistence, the full existing redactor detects provider tokens,
authorization credentials, private keys, keyword secrets and high-entropy values.
Multiline key bodies are suppressed through their footer or blank separator. Cached
Foreman App tokens and inherited GH_TOKEN/GITHUB_TOKEN values are also masked.
Arbitrary transformed secrets and unlabelled low-entropy values are not guaranteed.
No raw-output companion or environment dump is persisted.

# Worker permissions

Claude calls use `--restricted`, `--strict-mcp-config`, `dontAsk`, an explicit tool list,
and one settings file in the run’s scratch area. Restricted mode ignores inherited settings. MCP servers cannot
extend the tool list. Read, Glob, Grep and Bash are available on every call. Implement
also approves Edit and Write within the checkout and scratch directory. Method roots
are added for reading, with absolute Edit denies covering both editing tools protecting those roots.

Implement runs Bash in Claude Code's OS sandbox ([sandboxing](https://code.claude.com/docs/en/sandboxing.md)),
so a worker can delete the files a slice removes. The per-call settings turn it on with `failIfUnavailable`
and `allowUnsandboxedCommands: false`, so a missing sandbox fails closed and no command retries outside it.
`autoAllowBashIfSandboxed` runs sandboxed commands under `dontAsk`. Writes reach the checkout and the attempt
scratch only: `--add-dir` names the method roots and the scratch, never the settings file beside it, and
`denyWrite` covers the method roots, the checkout's `.git` entry and the git and common directories it resolves to,
each as given and as resolved. The worker resolves them with `git rev-parse` from the checkout, ignoring any inherited
`GIT_*` variable, and a `.git` git cannot resolve fails the call closed. A linked worktree's shared clone is therefore
read-only too, so `git commit` fails. Edit and Write follow `blockWritesOutsideWorkingDirectories` and Edit denies
for the same git directories. The network allowlist is
empty and strict. Claude Code points `$TMPDIR` at a per-user directory under `CLAUDE_CODE_TMPDIR`, but falls back
to the shared `/tmp/claude-<uid>` when that path is too long for its AF_UNIX sockets (about 32 characters
before `/claude-<uid>` on 2.1.329). The worker therefore gives each call its own short `/tmp/fw*` directory.
A host that cannot sandbox (no Seatbelt, or Linux without `bwrap` and `socat`) returns `cli_missing` before
launch, and so does a host with no short temp directory. A sandbox that fails at startup anyway (for example under
AppArmor or WSL1) makes Claude print "sandbox required but unavailable" and exit, which also reads as `cli_missing`.
In each case the dispatcher tries the next model. Implement keeps the exact `Bash(<check>)` rule.

Every Claude call that has Bash runs it in that sandbox: implement, the verifier, and a review granted its diff. The
sandbox reads nothing but what the call needs. `sandbox.filesystem.denyRead: ["/"]` denies every read, then `allowRead`
opens the checkout, the method roots and related checkouts, what git reads for the checkout and each related checkout,
the attempt scratch and the call's own short temp directory. It also opens what a tool needs to start: the operating system's own directories (on macOS
`/System`, `/Library`, `/Applications`, `/usr`, `/bin`, `/sbin`, `/dev`, `/etc` and `/var/select`) and the prefix the
host's git is installed under, the parent of the directory Foreman finds `git` in on its `PATH`. From a linked worktree's shared
clone, git reads only `objects`, `refs`, `packed-refs`, `config`, `HEAD`, `info` and the checkout's own `worktrees/<id>`
(`foreman.worker.git_reads`). The clone's `index`, `logs`, `hooks`, `FETCH_HEAD`, other runs' worktree records, its working
tree, another run's checkout and a second clone stay closed. Writes stay denied on the whole git directories. The clone's
`config` holds only its remote URL and refspec: fetches authenticate with `-c` flags on the command line. `permissions.blockReadsOutsideWorkingDirectories` holds Read, Grep and Glob to the working
directories, through symlinks too. Only implement and the verifier run Bash without a grant
(`autoAllowBashIfSandboxed`); a review runs only its diff. A Claude Code older than 2.1.332, the oldest version a
read-reach capture was taken on, returns `cli_missing` before launch and names the version found and the one needed.

A worker's CLI starts with the environment Foreman builds for every command it runs, not Foreman's own:
`command_environment()` with no token, then the adapter's variables, then `TMPDIR`. Claude's adapter adds `USER`, which
Claude Code needs to find its login in the keychain, and the short temp directory. So no worker's git reads settings outside
the checkout: `GIT_CONFIG_GLOBAL=/dev/null`, `GIT_CONFIG_NOSYSTEM=1` and `GIT_ATTR_NOSYSTEM=1`. Claude Code adds its own
session and sandbox-proxy variables, which the captures list by name.

Under these settings a worker's shell reads its own roots and the operating system's own directories, such as `/etc`.
The captures show it denied: the operator's home directory, Foreman's state directory outside the call's own roots,
another run's checkout and its Git worktree record, a file elsewhere on the disk outside the home directory, and the temp
directories of other calls and other sessions. Its environment holds only the variables Foreman builds for it. One thing
outside its roots stays readable: the Git objects and refs of its own repository's shared clone, which hold that
repository's other branches and other runs' commits. The attempt container removes the rest.

The read-reach captures (`tests/fixtures/claude/permissions/read-reach-{readonly,edit,verify}.json`) run one call per
profile through the adapter's own command and `run_worker_command`, on the cheapest model. Each probes, by shell byte count
only: a synthetic file outside the checkout and links to it from inside the checkout; planted files in `/Users/Shared`,
`/tmp`, the per-user temp directory, `/tmp/claude-<uid>` and another call's `/tmp/fw*`; another run's checkout and its
worktree record's `index`, the clone's own `index` and `logs`, a second clone, another call's attempt directory and a
checkpoint stand-in, laid out as `LiveWorkspace` lays out the state
directory; the host's real checkpoint store, the App key at its configured path, `~/.gitconfig`, `~/.codex/auth.json`,
`~/.claude/.credentials.json` and Claude's keychain item; and `~/.zshrc` named directly and indirectly. Read, Grep and Glob
try the synthetic file, the links and a missing home path. Every one is denied in every profile. `/etc/hosts` reads. The
work passes: the granted diff, `git status` and `git log` in the linked worktree and in a related checkout that is a
worktree of its own clone, the method and related roots, `python3` and, for implement, the check. Each capture records the
tools Claude offered the model, which are exactly `--tools` and the answer tool. The readonly `os-only` variant grants each read probe through a script in the scratch, so
Claude's command check sees only the script, and the OS sandbox alone refuses each with `Operation not permitted`. The
`inherited` variant adds project settings that set the block to false, `allowRead: ["/"]` and `additionalDirectories:
["~"]`; every read stays denied. The captures are macOS (Seatbelt) only. Linux (bubblewrap) is unproven, and its list of
system directories is not captured. Retake them with `.venv/bin/python tests/capture-read-reach.py claude <mode> adapter
inherited` (and `os-only` for readonly) when the host's Claude Code is newer than the captures at a release.

The verifier builds and drives the app, so it runs Bash in the same fail-closed sandbox with the same write
boundary, short temp directory and `cli_missing` cases. It has no Edit or Write tool and no command allow rule, and
it reaches no network: the capture shows a loopback listener refused (`Operation not permitted`) along with every host.
Claude Code's `allowLocalBinding` would let an app listen, but on macOS 2.1.329 it also lets the verifier reach every
loopback service on the host (a probe fetched a page from a host server started outside the sandbox), and its Seatbelt
profile allows inbound connections on every interface. Until a narrower boundary exists, a criterion that needs a running server is reported unverified.
A process started in one Bash call doesn't survive into the next. A build may write ignored output in the checkout;
the verifier writes nothing there itself, and after the drive the Verify stage checks that tracked and untracked
non-ignored files are unchanged (#251). Its attempt directory, `foreman-verify/<repo>-<issue>/<attempt>` under the
state directory, outlives the call so the captures it names stay readable as evidence. Every exit before the CLI
launches removes it. A run that finishes removes its attempt directories with its checkout; a halted run keeps both
as evidence. The prompt names the attempt scratch by absolute path, because inside the sandbox `$TMPDIR` is
Claude's own short temp directory, which the call removes. Codex and Grok calls for the verifier return `cli_missing`
before launch, because no capture yet shows either confining one.

The native read-only matcher refuses the planned review diff. Review calls therefore
grant the one full PR diff command from the bound `ReviewScope`, exactly as written (item 16).
The prompt names the same command. Other read-only stages have no allow rules.

Foreman keeps no per-file read list and parses no proof of reading. A review's answer counts from
its result envelope alone, and Claude review calls use `--output-format json` like every other call.
The read-only view denies every other command, so a denied variant (a pipe, a redirect, a path) shows the boundary
held and doesn't void the review. A Claude review whose denials include the granted diff command itself, compared
exactly after trimming, fails the call, and the evidence names the denied command: it never got its grant.
A diff past Bash's output limit still reaches a Claude reviewer whole. Claude hands the model a 2KB preview and the path
of the saved full output under `~/.claude/projects/<project>/<session>/tool-results/`, and restricted Read and Grep open
that file. `tests/fixtures/claude/permissions/large-diff.json` records this for a 437.8KB diff on Claude 2.1.332
(`tests/capture-claude-large-diff.py`).
The diff runs in the review's sandbox, with the same read boundary as every other Claude shell.
Workers never commit, push or touch the Forge. Foreman owns those operations.

Codex uses a native permissions profile extending `:read-only`. Implement grants
writes to the checkout and its scratch directory. Method roots, `.git` and `.codex`
remain read-only. Temp directories receive no implicit write grant. Network access
is disabled. `--ignore-user-config` and `--ignore-rules` isolate inherited user
configuration and command approvals. `approval_policy=never` prevents escalation.
An invalid or unavailable native sandbox fails closed. Codex does not use a command
allowlist. The captures show `git commit` failing because `.git` is read-only.
A Codex reviewer runs under the same read-only profile with the diff command given in the prompt.
Codex has no command allowlist. The profile denies writes and the network. It does not confine reads: a Codex
worker's shell can read files anywhere on the host, in review and in every other call. That is an open gap against
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

Run `.venv/bin/python tests/capture-native-permissions.py codex edit` and `codex readonly`
to reproduce Codex captures. `grok readonly-gap` captures the native boundary gap
under inherited broad Bash and Edit grants. Probe credentials stay in ignored scratch.
The native kernel checks use the same Codex profile as the worker argv and record
command results independently of model prose.

Reproduce the Claude captures with
`.venv/bin/python tests/capture-claude-permissions.py <mode>` for edit, inherited,
readonly, compound and verify (2.1.329; readonly 2.1.332). The readonly capture grants the exact command
`review_diff` emits on a two-commit range and records the whole patch it returns. Each capture contains the exact argv, result envelope and
filesystem observations. Transcript tool calls and results corroborate method reads
and denied operations. The inherited probe adds permissive project settings.
The clean edit capture proves source edits, scratch writes, and edits to pyproject.toml
and .github/workflows/ci.yml. Restricted mode can therefore remain enabled, and it honors the sandbox block.
The edit, inherited and compound captures run in a linked worktree of a shared clone. Sandboxed `rm` of a tracked
file and of a scratch file succeeds. `rm` of a method file or of a file outside the checkout fails, and so do writes
to the shared clone's `.git` and to `/tmp/claude-<uid>`. `git commit`, `curl` with or without the proxy, and `gh`
fail too, each with the sandbox's own refusal (`Operation not permitted`, a host not on the allow list). The inherited
probe adds project settings that disable the sandbox, exclude every command and allow every write and host;
restricted mode ignores them. Sandbox refusals arrive as failed tool results, not as permission denials.

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
fails closed on successful checked calls.
Claude names commands, not allow rules. Obfuscated wrappers without literal check text cannot be
reliably attributed; `verify` remains authoritative. T1 joins tool IDs, successful results, and a marker.
Separate captures prove wrapper and compound-segment denials.

Failed verifier checks retain diagnostic context in `checks.failure_output`. Retries,
resume and provider failover receive it as untrusted previous-attempt data. Prepare
clears the previous verification result. A pass clears the failure text.
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
or database connections, and neither does Claude's sandboxed Bash.
The configured verifier remains authoritative when local reproduction is blocked.

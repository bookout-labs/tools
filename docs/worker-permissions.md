# Worker permissions

Foreman constructs a typed capability grant when it binds a stage's methods.
The dispatch plan stores a JSON-compatible `GrantRecord`; each model attempt,
including quota fallbacks, receives the restored grant through the handler,
CLI worker, and adapter call. Review fix rounds bind a fresh implementation
grant. Legacy checkpoints may omit the grant and still validate; dispatch
reconstructs their grant from the stage and current pinned method export.

`ReadOnly` is used for triage, research, spec, and review. `Editing` is used for
implementation and adds checkout editing and the parsed repository check
commands. Both carry the external method read roots: the resolved pinned
skill export and, when needed, Foreman's packaged skills directory. The
scratch parent is never a method read root. These roots supplement the
worker's checkout context.

The inspection set (`INSPECTION`) is `git status`, `git diff`, `git log`,
`git show`, `git ls-files`, `rg`, `ls`, `cat`, `head`, `tail`, and `sed`.
This set describes inspection operations, not arbitrary options or scripts
passed to those executables. Shell writes, such as redirection, `tee`, `rm`,
`mv`, `cp`, `mkdir`, `touch`, and `sed -i`, are identified separately by
`SHELL_WRITE_PATTERNS`.

Workflow writes are denied to workers. Staging, committing, pushing, merging,
rebasing, resetting, switching checkouts, and GitHub pull request or issue
mutations remain Foreman's responsibility (`WORKFLOW_WRITES`). An editing
grant does not authorize publishing or arming a merge.

`parse_check` accepts an empty check or commands joined by `&&`, preserving
arguments as immutable `CheckCommand.argv` tuples with shell-quoted canonical
text available as `command`. It conservatively rejects shell operators other
than `&&`, expansion, command substitution, newlines, malformed quoting, and
empty command segments. `run_live_delivery` raises `ValueError` for an
ungrantable `--check` before graph compilation or skill export.

A command permission is not a sandbox. An allowed check can execute code that
writes files or launches other programs. In this slice, inspection and workflow
policy constants describe the grant model; adapters do not enforce command
allowlists or the external read roots. Codex maps `Editing` to `workspace-write`
and `ReadOnly` to `read-only`. Claude and Grok retain `acceptEdits` and `plan`,
respectively. Their modes are not filesystem isolation and cannot guarantee
that a worker never performs a workflow write. The typed grant records intent
without claiming that command permissions provide process isolation.

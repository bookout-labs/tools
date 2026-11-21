# Worker permissions

Claude calls use `--restricted`, `--strict-mcp-config`, `dontAsk`, an explicit tool list,
and one settings object. Restricted mode ignores inherited settings. MCP servers cannot
extend the tool list. Read, Glob, Grep and Bash are available on every call. Implement
also approves Edit and Write within the checkout and scratch directory. Method roots
are added for reading, with absolute Edit denies covering both editing tools protecting those roots.

Implement grants one exact `Bash(<check>)` rule for the configured check string.
Foreman does not parse shell commands. The compound-check capture demonstrates the
native matcher accepting the configured compound command under one rule.
The native read-only matcher refuses the planned review diff. Review calls therefore
grant exactly the bound review scope command through the same optional command slot.
Other read-only stages have no allow rules.
A command permission is not a sandbox. The check itself must be trusted.
Workers never commit, push or touch the Forge. Foreman owns those operations.

Codex retains its workspace-write/read-only sandbox selection. Grok retains its
acceptEdits/plan modes pending slice B. Those modes do not establish the Claude policy.

Reproduce the Claude 2.1.329 captures with
`.venv/bin/python tests/capture-claude-permissions.py <mode>` for edit, inherited,
readonly and compound. Each capture contains the exact argv, result envelope and
filesystem observations. Transcript tool calls and results corroborate method reads
and denied operations. The inherited probe adds permissive project settings.
The clean edit capture proves source edits, scratch writes, and edits to pyproject.toml
and .github/workflows/ci.yml. Restricted mode can therefore remain enabled.

Claude matches denied Bash text against `Call.check` by literal containment in both directions.
This covers exact checks, wrappers, and compound segments without shell parsing.
Unrelated quoted commands and non-Bash denials stay `ran`. Missing, blank, or malformed evidence
fails closed only on successful checked calls. Existing failures, quota, budget, and usage stay intact.
Claude names commands, not allow rules. Obfuscated wrappers without literal check text cannot be
reliably attributed; `verify` remains authoritative. T1 joins tool IDs, successful results, and a marker.
Separate captures prove wrapper and compound-segment denials.

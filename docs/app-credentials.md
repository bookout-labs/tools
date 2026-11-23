# Foreman App credentials

Set `FOREMAN_APP_ID` in Foreman's environment.
`FOREMAN_APP_KEY_PATH` defaults to `~/.config/bookout-foreman/app.pem` and must be owner-only.
Each repository's token comes from the installation that covers it (`GET /repos/{owner}/{repo}/installation`), on an organization or a personal account.
A repository where the App is not installed fails closed. With no App ID set, personal gh and git behavior remains.

Configured Workspace transports use the run's repository, validated HTTPS origins and a command-scoped helper.
Credential failures reuse stage-specific resumable halts. Only a 401 clears cached tokens.
Resume reconciles existing PR, review and push receipts after ambiguous writes.

[Documented tool contracts](../tests/fixtures/github/app-credentials.txt) and their sources are recorded in the fixture manifest.
See [factory worker setup and command usage](worker-launcher.md).
Configured commits use the bot author and committer, resolved from its numeric user ID.
Identity is command-scoped; adoption re-commits the worker tree through the same path.

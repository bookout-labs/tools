# Foreman App credentials

Set `FOREMAN_APP_ID` and `FOREMAN_INSTALLATION_ID` in Foreman's environment.
`FOREMAN_APP_KEY_PATH` defaults to `~/.config/bookout-foreman/app.pem` and must be owner-only.
With neither ID set, personal gh and git behavior remains. Partial configuration fails closed.

Configured Workspace transports use the run's repository, validated HTTPS origins and a command-scoped helper.
Credential failures reuse stage-specific resumable halts. Only a 401 clears cached tokens.
Resume reconciles existing PR, review and push receipts after ambiguous writes.

[Documented tool contracts](../tests/fixtures/github/app-credentials.txt) and their sources are recorded in the fixture manifest.
See [factory worker setup and command usage](worker-launcher.md).

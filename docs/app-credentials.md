# Foreman App credentials

Set `FOREMAN_APP_ID` and `FOREMAN_INSTALLATION_ID` in Foreman's environment.
`FOREMAN_APP_KEY_PATH` defaults to `~/.config/bookout-foreman/app.pem`.
The key must be owner-only. With neither ID set, existing personal gh/git behavior remains.
Partial configuration fails closed. Tokens remain in process memory and command environments.

PyJWT with cryptography signs RS256. The credential owner uses one scoped POST,
a lock and the returned expiry with a 60-second margin. PyGithub's
AppInstallationAuth does not expose repository narrowing, so it would still need
our scoped mint/cache wrapper plus an SDK dependency. PyJWT keeps that owner to
90 lines and uses GitHub's documented flow without hand-built cryptography.
Sources are recorded in `tests/fixtures/github/app-credentials.txt` and its manifest entry.

Credential errors reuse Forge/Workspace failures and existing stage-specific
resumable halts. A 401 invalidates the cache. There is no command replay here.
Existing PR/review/push reconciliation checks receipts on resume after ambiguous writes.
Worker launcher and real App smoke proofs belong to slices 129c and 129b.

Boundary Discipline put authentication at Forge/Workspace boundaries.
Model the Domain put expiry and synchronization in one repository-keyed owner.
Prove It Works drove generated-key, fake-HTTP and SQLite redaction tests.

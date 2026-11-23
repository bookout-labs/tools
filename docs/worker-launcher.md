# Set up factory App commands

Use `foreman-worker gh pr list --repo bookout-labs/tools` from the installed
checkout's `.venv/bin`. On MacBook insert `--studio foreman-studio` before `gh`
or `git`. Only `bookout-labs/tools` is allowed. Each invocation requests a current
credential without a worker token file or personal fallback.

The launcher accepts `gh pr`, `issue`, `run`, `repo`, and `label`.
Aliases and extensions are excluded. Git syntax is `git fetch origin main`,
`git push origin HEAD:BRANCH`, `git push origin BRANCH`, `git ls-remote origin`,
or `git clone https://github.com/bookout-labs/tools DIRECTORY`.
Push branches may use `refs/heads/`. Main, tags, deletes, force pushes, and
network options are excluded. Set origin and every push URL to the allowed HTTPS
repository. Run local git directly with the [worker identity](app-credentials.md).
Commands are noninteractive with captured, redacted output and a 30-second deadline.
After an ambiguous write failure, inspect GitHub receipts before retrying.

## Install the launcher

1. On Studio, install from main with the locked dependencies.

   ```sh
   git clone --branch main https://github.com/bookout-labs/tools /Users/booko/.local/share/foreman-worker
   cd /Users/booko/.local/share/foreman-worker
   uv sync --frozen --no-dev
   ```

   Keep the checkout and its parent directories writable only by the account that runs Foreman.
   Keep `~/.config/bookout-foreman` mode 700 and `app.pem` mode 600.
   The App key stays on Studio.
2. On MacBook, create a dedicated SSH identity.

   ```sh
   ssh-keygen -t ed25519 -f ~/.ssh/foreman-worker
   ```

   An unencrypted IdentityFile needs no agent. For a passphrase-protected key, run
   `ssh-add --apple-use-keychain ~/.ssh/foreman-worker` and add `UseKeychain yes`
   to the host stanza below. Transfer only the public `.pub` line to Studio
   through an authenticated machine channel.
3. On Studio, enable Remote Login for the account that runs Foreman. Append this line to
   `~/.ssh/authorized_keys`, replacing `PUBLIC_KEY` with the public key line.
   Keep `.ssh` mode 700 and `authorized_keys` mode 600.

   ```text
   restrict,command="cd /Users/booko/.local/share/foreman-worker && FOREMAN_APP_ID=5861795 .venv/bin/foreman-worker --serve" PUBLIC_KEY
   ```

   `restrict` disables forwarding, PTY, agent forwarding, X11, and user rc execution.
   The wire contract requires `SSH_ORIGINAL_COMMAND` to equal `bookout-labs/tools`.
   Stdout contains only `password=TOKEN` followed by two newlines. Stderr contains
   sanitized failures. Never run raw ssh to this endpoint in a captured terminal.
4. On MacBook, run `brew install uv` once, then repeat step 1.
   Add this stanza to `~/.ssh/config`. Replace `STUDIO_HOST` with Studio's address.
   Confirm its host-key fingerprint through a trusted channel and provision
   `known_hosts` first. The launcher supplies the SSH transport restrictions.

   ```sshconfig
   Host foreman-studio
       HostName STUDIO_HOST
       User booko
       IdentityFile ~/.ssh/foreman-worker
       IdentitiesOnly yes
   ```

5. On Studio, create `/Users/booko/.config/bookout-foreman/worker.env` with mode 600.

   ```sh
   export FOREMAN_APP_ID=5861795
   ```

   Source it only inside the launcher's subshell.

   ```sh
   ( . /Users/booko/.config/bookout-foreman/worker.env; /Users/booko/.local/share/foreman-worker/.venv/bin/foreman-worker gh pr list )
   ```

   Do not source it in your parent shell. Other Foreman Workspace and Forge
   commands that inherit these IDs also enter App mode. Leave the IDs and key
   absent on MacBook and use `--studio foreman-studio` there.

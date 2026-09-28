## Testing and rollout

### Compatibility baseline

Fastagent compatibility is audited against `ansible-core 2.20.4`. The exact
controller stack used for the current audit was:

| Tool | Version |
|------|---------|
| Ansible | `ansible-core 2.20.4` |
| Controller Python used by Ansible | `Python 3.14.3` |
| Jinja | `3.1.6` |
| PyYAML | `6.0.3` with libyaml `0.2.5` |

The collection declares `requires_ansible: ">=2.12.0"`. When adding or changing
fast paths, compare against `ansible-core 2.20.4` first and add older-version
checks only where Ansible's public behavior changed within the supported range.

The user-facing compatibility matrix lives in `docs/compatibility.md`. The test
suite checks that README and testing docs keep pointing at that matrix and that
the module summary table does not drift.

### CI dependencies

Buildkite pins ansible-core, goimports, differ, and Staticcheck in
`.buildkite/pipeline.yml`. CI tests the pinned Ansible release independently of
the compatibility audit above; updating CI does not redo that audit.

Go and Python are supplied by the Buildkite host. The current ansible-core pin
requires Python 3.12 or newer. The Python setup script creates a standard-library
venv and upgrades pip when provisioning it; no separate virtualenv package or
ansible-lint is used. ansible-galaxy, ansible-doc, and ansible-playbook are
included in ansible-core.

When updating the pins, run the CI format, lint, test, build, python-test, and
check-versions commands with the pipeline's environment values.

### Differential tests

`tests/test_stat_differential.py` runs the same `stat` tasks through
fastagent and through `ansible.builtin.stat` and requires identical results.
One half always runs (it needs Go and `ansible-playbook`): it drives the
action plugin against a local agent started with `--serve` and runs stock
with `-c local`. The other half compares a fastagent connection and a plain
SSH connection to the same disposable host, and runs when these are set:

```bash
export FASTAGENT_TEST_SSH_HOST=testhost      # required
export FASTAGENT_TEST_SSH_PORT=22            # optional
export FASTAGENT_TEST_SSH_USER=deploy        # optional
export FASTAGENT_TEST_SSH_KEY=~/.ssh/test    # optional
export FASTAGENT_TEST_BECOME_USER=app        # optional, a non-root user
python3 -m unittest -v tests.test_stat_differential
```

The host needs Python 3 and passwordless sudo, and the agent binary must be
available the way the connection plugin finds it (see Step 1 below).

### Setup: install a development build

To run this checkout, uncommitted changes included, against a real
playbook repo:

```bash
make dev-install
~/.ansible/fastagent-dev/run ansible-playbook -i inventory site.yml
```

`make dev-install` (`scripts/dev-install.sh`) gives the build a version of
its own: the release version plus `-dev.g<commit>`, and `.w<tree hash>`
when the working tree has changes, for example
`0.9.0-dev.g39d5d0285149.w9e55789741`. It stamps that version into the Go
agent, the connection plugin and `galaxy.yml`, then:

- builds `fastagent-<dev version>-linux-{amd64,arm64}` into
  `~/.ansible/fastagent/`, next to the release binaries. The names never
  match a release, so release deploys never read them.
- installs the collection into `~/.ansible/fastagent-dev/collections`, which
  nothing uses unless told to.
- writes `~/.ansible/fastagent-dev/env.sh`, which puts that collection first
  on `ANSIBLE_COLLECTIONS_PATH` (other collections still resolve from the
  usual paths) and sets `ANSIBLE_ACTION_PLUGINS` to its action plugins,
  overriding an `ansible.cfg` `action_plugins` line that names the release
  collection. `~/.ansible/fastagent-dev/run` runs one command with it.

Commands run without `run` keep using the release collection and agent,
so production deploys from the same machine are unaffected. Under `run`,
the run warns once: `fastagent: using development build <version>`. On each
host the dev agent is uploaded next to the release one and runs its own
daemon, because the version is part of the remote binary's name and the
daemon's socket. A host can never keep running an older build under the
same name.

Why not install under the release version? The version is how the plugin
tells agents apart. A host that already had that release's agent would
keep running it and silently ignore the new build, and real deploys would
upload the development binary to hosts that did not. For the same reason,
`make deploy`, which copies binaries to `~/.ansible/fastagent/` under the
release name for hosts that cannot download them, only runs from a clean
checkout of the release tag.

The plugin searches for the local binary in this order:

1. `fastagent_local_agent_dir` inventory variable (if set)
2. `~/.ansible/fastagent/` (where `make dev-install` and `make deploy` put
   them, and where downloads land)
3. `tmp/` relative to the plugin directory (raw `make build` output)
4. `fastagent_download_url`, a GitHub release by default

You can override the remote path with `fastagent_agent_path`:

```ini
[all:vars]
fastagent_agent_path=/usr/local/bin/fastagent
```

### Step 2: Smoke test locally (no remote host needed)

```bash
echo '{"id":1,"method":"Hello","params":{"version":"0.1.0"}}' | \
  go run -trimpath ./cmd/fastagent --serve
```

You should get back a JSON response with version and capabilities.

### Step 3: Test against a single host

Set `ansible_connection=fastagent` on a host in your inventory:

```ini
[test]
yourhost ansible_connection=fastagent ansible_user=youruser
```

Then test incrementally:

```bash
# 1. Raw module (tests connection plugin only, no module transfer)
ansible -i inventory test -m raw -a "echo hello"

# 2. Command module (tests action plugin override)
ansible -i inventory test -m command -a "uptime"

# 3. Shell module (tests _uses_shell path)
ansible -i inventory test -m shell -a "echo \$((2+3))"

# 4. Copy module (tests file write RPC)
ansible -i inventory test -m copy -a "content='hello fastagent' dest=/tmp/fastagent-test.txt"

# 5. Template (write a simple playbook with a template task)

# 6. Package/service (on a host where you're ok installing/managing packages)
ansible -i inventory test -m apt -a "name=curl state=present" --become
ansible -i inventory test -m systemd -a "name=cron state=started" --become
```

### Step 4: Run an existing playbook

Pick a simple playbook you already have and add `ansible_connection=fastagent`
to one host's vars. Compare the output to a normal run. The behavior should be
identical, just faster (especially on repeated runs due to the persistent
connection).

### Step 5: Expand gradually

Once one host is solid, apply `ansible_connection=fastagent` to a group, then
to all hosts.

### Debugging and observability

Agent-side log output (from the Go binary's slog) is captured by a background
thread in the connection plugin and forwarded through Ansible's display system.

- **Normal run**: if the agent crashes or an RPC fails, the error message
  includes the last lines of agent stderr.
- **`-vvv` verbosity**: every agent stderr line is printed as it arrives,
  prefixed with `FASTAGENT [host]:`. The agent is also launched with `--debug`
  at this verbosity, which enables request-level logging (method name and ID
  for each RPC).
- **Agent panics/segfaults**: the crash traceback lands in the stderr buffer
  and shows up in the next Ansible error message or at connection close.

Example debug run:

```bash
ansible -i inventory test -m command -a "uptime" -vvv
```

### What to watch for

Any `failed` or `unreachable` results where the same playbook works with the
default SSH connection indicate a semantic mismatch in the shims. The most
likely sources of divergence:

- **copy with directories**: recursive copy falls back to builtin, but edge
  cases in path handling may differ.
- **become methods other than sudo**: only sudo is implemented; other methods
  (su, pbrun, etc.) fall back to direct execution with a warning.
- **modules that hard-pin `ansible.builtin.*`**: tasks using
  `ansible.builtin.copy` or `ansible.builtin.apt` bypass the `ansible.legacy`
  override path and use core modules directly. The connection plugin still
  accelerates these (persistent session), but the action plugin fast paths
  won't apply.
- **SELinux contexts**: WriteFile does not yet set SELinux labels.

For the full current list, including package/service caveats and direct-RPC
gaps, read `docs/compatibility.md`.

## Testing and rollout

### Setup

The fastagent plugins are discovered via `ansible.cfg` in the repo root. If
you're running Ansible from a different directory (e.g. an existing playbook
repo), point Ansible at the plugin directories using environment variables or
your own `ansible.cfg`.

Environment variable approach (useful when running from another repo):

```bash
export ANSIBLE_GO_DIR="$HOME/src/github.com/kevinburke/ansible-go"
export ANSIBLE_CONNECTION_PLUGINS="$ANSIBLE_GO_DIR/connection_plugins"
export ANSIBLE_ACTION_PLUGINS="$ANSIBLE_GO_DIR/action_plugins"
export ANSIBLE_LIBRARY="$ANSIBLE_GO_DIR/library"
export ANSIBLE_MODULE_UTILS="$ANSIBLE_GO_DIR/module_utils"
```

Or add to the `ansible.cfg` in your playbook repo:

```ini
[defaults]
connection_plugins = /path/to/ansible-go/connection_plugins
action_plugins = /path/to/ansible-go/action_plugins
library = /path/to/ansible-go/library
module_utils = /path/to/ansible-go/module_utils
```

### Step 1: Build the agent binary

```bash
cd "$ANSIBLE_GO_DIR"
make build
# produces tmp/fastagent-linux-amd64 and tmp/fastagent-linux-arm64
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

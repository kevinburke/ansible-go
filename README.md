# fastagent

A drop-in Ansible accelerator. Replace your remote execution path with a
persistent Go agent and cut playbook run times by 50%+.

```
SSH (default):   50s
fastagent:       22s   (56% faster)
```

Fastagent keeps standard Ansible YAML, inventory, variables, and templating
unchanged. It substitutes a faster execution engine underneath using supported
Ansible extension points (connection plugin, action plugin overrides).

## How it works

1. On first connect, the connection plugin uploads a small Go binary to the
   remote host and starts it as a persistent daemon on a Unix socket.
2. An SSH socket forwarding session bridges a local Unix socket to the remote
   daemon. This session persists across tasks.
3. Each Ansible task connects to the local socket (~1ms), sends JSON-RPC
   requests to the daemon, and disconnects. No SSH process per task.
4. Action plugin overrides for common modules (`command`, `shell`, `file`,
   `stat`, `copy`, `apt`, `systemd`) bypass Python module transfer entirely,
   sending RPCs directly to the daemon.

Tasks using modules without overrides still work normally through the standard
Ansible module execution path, but benefit from the persistent connection.

## Quick start

### 1. Install the collection

```bash
ansible-galaxy collection install kevinburke.fastagent
```

That's it for the controller side. The connection plugin auto-downloads the
prebuilt Linux agent binary from GitHub Releases on first use and caches it
under `~/.ansible/fastagent/`. No Go toolchain required.

### 2. Enable it per host

Set the connection in your inventory (the FQCN namespace is
`kevinburke.fastagent.fastagent`):

```ini
[webservers]
myhost ansible_connection=kevinburke.fastagent.fastagent ansible_user=deploy
```

Or group-wide in `group_vars/all.yml`:

```yaml
ansible_connection: kevinburke.fastagent.fastagent
```

### 3. Use unqualified module names

Fastagent overrides modules via Ansible's `ansible.legacy` resolution path.
Tasks using fully-qualified names like `ansible.builtin.command:` bypass this
and go through the standard (slower) execution path. Use unqualified names:

```yaml
# Fast (uses fastagent override):
- command: uptime

# Slow (bypasses override, uses builtin):
- ansible.builtin.command: uptime
```

To convert an existing playbook:

```bash
find roles/ -name '*.yml' -exec sed -i 's/ansible\.builtin\.\([a-z0-9_]*\):/\1:/g' {} +
```

### Building from source (optional)

If you prefer to build the agent binary yourself — e.g. for air-gapped
environments, or to pin to an unreleased commit — clone this repo and run:

```bash
make deploy
```

This cross-compiles for linux/amd64 and linux/arm64 and copies the binaries to
`~/.ansible/fastagent/`. The connection plugin checks this directory before
attempting any download, so a locally built binary always takes precedence.
Requires Go 1.21+.

To disable the auto-download entirely, set `fastagent_download_url` to an
empty string in inventory, or `FASTAGENT_DOWNLOAD_URL=` in the environment.

## What gets accelerated

| Module | Override | How |
|--------|----------|-----|
| `command`, `shell` | Action plugin | Exec RPC, no module transfer |
| `file` | Action plugin | File/Stat RPC |
| `stat` | Action plugin | Stat RPC |
| `copy`, `template` | Action plugin | WriteFile RPC with checksum |
| `apt` | Action plugin | Package RPC with dpkg cache |
| `systemd` | Action plugin | Service RPC |
| Everything else | Connection plugin | Persistent daemon + SSH forwarding |

The `apt` override includes two optimizations:
- **dpkg cache**: reads `/var/lib/dpkg/status` once, then skips `apt-get
  install` entirely for already-installed packages (map lookup instead of
  subprocess).
- **update_cache dedup**: tracks when `apt-get update` last ran; skips
  redundant updates within 60 seconds (configurable via `cache_valid_time`).

## Architecture

```
Controller                          Remote Host
+------------------+                +------------------+
| Ansible          |                | fastagent daemon |
|   |              |   SSH socket   |   (Go binary)    |
|   +-> local sock +---forwarding-->+   Unix socket    |
|       (~1ms)     |   (persists)   |   (persists)     |
+------------------+                +------------------+
```

- **Daemon**: persistent Go process on the remote host, accepts JSON-RPC over
  a Unix socket. Handles Exec, Stat, ReadFile, WriteFile, File, Package,
  Service RPCs. Auto-exits after 1 hour idle.
- **SSH forwarding**: `ssh -fN -L local.sock:remote.sock` runs once per host,
  bridges the local and remote sockets. Persists across tasks and forks.
- **Connection plugin**: on each task, connects to the local socket, sends
  RPCs, disconnects. First task bootstraps the daemon and SSH forwarding.
- **Action plugins**: intercept common modules and send RPCs directly instead
  of transferring Python modules.

## Debugging

### Verbose output

```bash
ansible-playbook playbook.yml -vvv
```

At `-vvv`, the connection plugin logs every step (`FASTAGENT: ...`) and the
daemon enables debug-level logging (every RPC is logged with millisecond
timestamps).

### Daemon log

The daemon logs to a file next to its socket:

```bash
ssh myhost "sudo cat /tmp/fastagent-root.sock.log"
```

### Common issues

**"local socket not available, setting up" on every task**

The SSH forwarding session died. Check if the SSH ControlMaster is working:

```bash
ssh -O check myhost
```

**"timeout waiting for socket"**

The daemon failed to start. Check the daemon log on the remote host.
Common causes: the binary wasn't uploaded (version mismatch), or a stale
daemon process is holding the socket.

```bash
ssh myhost "sudo pkill fastagent; sudo rm -f /tmp/fastagent-root.sock*"
```

**"failed to start daemon" or hanging**

Kill stale processes and sockets on both sides:

```bash
# Remote
ssh myhost "sudo pkill fastagent; sudo rm -f /tmp/fastagent-root.sock*"
# Local
rm -f /tmp/fastagent-local-*
```

**Tasks return different results than with SSH**

Fastagent action overrides return a simplified result dict. If a downstream
task depends on specific fields from a `register:` result (e.g. `result.uid`
from `file`), the override may not include them. Fix: check what fields your
playbook uses and add them to the override, or remove `ansible_connection`
for that host to fall back to SSH.

**Temp directories owned by root**

The daemon runs as root. Commands that should run as the connecting user
(e.g. temp dir creation) are wrapped with `runuser`. If you see
permission errors on `~/.ansible/tmp/`, fix with:

```bash
ssh myhost "sudo chown -R youruser:youruser ~/.ansible/tmp"
```

## Updating

For third-party users, upgrade the collection:

```bash
ansible-galaxy collection install --upgrade kevinburke.fastagent
```

The connection plugin detects version mismatches automatically: if the remote
daemon is running an old version, it kills it and uploads the new binary on
the next run.

When working on the agent itself, bump the version in `fastagent.go` and run:

```bash
make clean && make deploy
```

To build everything for a release (binaries plus collection tarball):

```bash
make release
```

## Security

The daemon runs as root (via `sudo`) to handle privilege escalation. The Unix
socket is restricted to the connecting user's group (`root:<user_group>`,
mode `0770`). Other users on the remote host cannot connect to it.

The daemon auto-exits after 1 hour of inactivity (configurable via
`--idle-timeout`).

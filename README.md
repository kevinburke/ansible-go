# fastagent

A drop-in Ansible accelerator. Replace your remote execution path with a
persistent Go agent and cut playbook run times by 50%+.

```
SSH (default):   55s
fastagent:       22s   (60% faster)
```

The speedup comes from **action plugin overrides** that replace Python module
transfer with direct Go RPCs for common modules (`command`, `shell`, `copy`,
`file`, `stat`, `apt`, `systemd`). This requires using **unqualified module
names** in your playbooks (e.g. `command:` not `ansible.builtin.command:`).
Without the overrides, fastagent is roughly the same speed as plain SSH — see
[Use unqualified module names](#use-unqualified-module-names).

Fastagent keeps standard Ansible YAML, inventory, variables, and templating
unchanged. It substitutes a faster execution engine underneath using supported
Ansible extension points (connection plugin and action plugin overrides).

## Requirements

**Controller** (the machine running `ansible-playbook`):

- Ansible 2.12 or newer
- Python 3.8 or newer
- `ssh`, `scp`, and `curl` (`curl` is used to download the agent binary on
  first use; not needed if you build from source)
- Network access to `github.com` for the first-time agent download (or a
  locally built binary — see [Building from source](#building-from-source))

**Remote hosts** (the machines being managed):

- Linux on `amd64` or `arm64`
- Works on glibc and musl distros (Debian, Ubuntu, RHEL/Rocky/Alma, Fedora,
  Amazon Linux, Alpine) — the agent is a statically linked Go binary, no libc
  dependency
- `sudo` access for the connecting user, since the agent runs as root to
  handle privilege escalation
- `systemd` is only required if you use the `systemd` action plugin override;
  everything else is independent of init system

The agent binary is ~5 MB and is uploaded to `~/.ansible/fastagent/` on the
remote host on first run. It auto-exits after 1 hour of inactivity.

## Install

### Option A — From Ansible Galaxy (recommended)

```bash
ansible-galaxy collection install kevinburke.fastagent
```

### Option B — Via `requirements.yml` (recommended for teams)

Add to your playbook repo's `requirements.yml`:

```yaml
collections:
  - name: kevinburke.fastagent
```

Install:

```bash
ansible-galaxy collection install -r requirements.yml
```

### Option C — From a Git tag

```bash
ansible-galaxy collection install \
    git+https://github.com/kevinburke/ansible-go.git,v0.3.3
```

This pulls the collection straight from this repo at a specific tag.
Useful if you want to pin to an exact commit.

After install, the connection plugin auto-downloads the prebuilt Linux agent
binary from GitHub Releases on first use and caches it under
`~/.ansible/fastagent/` on the controller. **No Go toolchain required.**

## Enable it on a host

In your inventory, set the connection on the hosts you want to accelerate.
The connection name is the FQCN `kevinburke.fastagent.fastagent`.

YAML inventory:

```yaml
all:
  children:
    fastagent_canary:
      hosts:
        web1.example.com:
        web2.example.com:
      vars:
        ansible_connection: kevinburke.fastagent.fastagent
        ansible_user: deploy
```

Or INI:

```ini
[fastagent_canary]
web1.example.com
web2.example.com

[fastagent_canary:vars]
ansible_connection=kevinburke.fastagent.fastagent
ansible_user=deploy
```

Or via `group_vars/`:

```yaml
# group_vars/fastagent_canary.yml
ansible_connection: kevinburke.fastagent.fastagent
```

**Avoid setting `ansible_connection` in a `group_vars/` file that test
playbooks load via `vars_files`.** Ansible variable precedence means a
`vars_files` entry overrides the play-level `connection: local` keyword,
causing fastagent to try SSHing into localhost. If your tests load
`group_vars/all.yml` this way, set `ansible_connection` in the inventory
file's `[all:vars]` section instead — inventory variables don't leak into
`vars_files` includes.

For a first rollout, put **one or two non-critical hosts** in a `fastagent_canary`
group and leave the rest of your fleet alone — they'll keep using the default
SSH connection. Expand the group as you build confidence. See
[Disabling fastagent](#disabling-fastagent) for the escape hatch.

## Verify it works

Smoke test from the controller:

```bash
ansible fastagent_canary -m ping -vvv
```

Look for these things in the output:

1. `FASTAGENT:` log lines showing the bootstrap sequence (binary upload → daemon
   start → SSH socket forwarding up). These appear on the **first** task only.
2. `pong` in the result — the daemon is up and answering RPCs.
3. The first task takes a few seconds longer than usual because of the
   bootstrap. From the second task onward, latency drops sharply.

To time the difference end-to-end on a real playbook, use the `profile_tasks`
callback:

```bash
ANSIBLE_STDOUT_CALLBACK=profile_tasks \
    ansible-playbook -i inventory site.yml --limit fastagent_canary
```

Compare against the same playbook run without the connection override.

## Use unqualified module names (required for speedup)

**This step is required.** Without it, fastagent provides no speedup over
plain SSH.

Fastagent overrides modules via Ansible's `ansible.legacy` resolution path.
When you write `command:`, Ansible checks for an override before falling back
to the builtin — and fastagent's override handles it as a direct Go RPC,
skipping Python module transfer entirely. When you write
`ansible.builtin.command:`, Ansible resolves directly to the builtin, the
override never fires, and the task goes through the standard Python module
path. The persistent connection doesn't help here because Ansible's native
SSH ControlMaster already provides connection reuse.

```yaml
# Fast — override fires, Go RPC, no Python transfer:
- command: uptime

# No speedup — override bypassed, full Python module transfer:
- ansible.builtin.command: uptime
```

To convert an existing playbook in-place:

```bash
find roles/ -name '*.yml' -exec \
    sed -i 's/ansible\.builtin\.\([a-z0-9_]*\):/\1:/g' {} +
```

Modules that don't have an override (e.g. `git`, `user`, `cron`) still work
normally — they go through the standard Ansible module path at roughly the
same speed as plain SSH.

## How it works

1. On first connect, the connection plugin uploads a small Go binary to the
   remote host and starts it as a persistent daemon on a Unix socket.
2. An SSH socket forwarding session bridges a local Unix socket to the remote
   daemon. This session persists across tasks.
3. Each Ansible task connects to the local socket (~1 ms), sends JSON-RPC
   requests to the daemon, and disconnects. No SSH process per task.
4. Action plugin overrides for common modules bypass Python module transfer
   entirely, sending RPCs directly to the daemon.

Tasks using modules without overrides still work normally through the
standard Ansible module execution path, at roughly the same speed as
plain SSH.

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

- **Daemon**: persistent Go process on the remote host. Accepts JSON-RPC over
  a Unix socket. Handles Exec, Stat, ReadFile, WriteFile, File, Package,
  Service RPCs. Auto-exits after 1 hour idle.
- **SSH forwarding**: `ssh -fN -L local.sock:remote.sock` runs once per host,
  bridges the local and remote sockets. Persists across tasks and forks.
- **Connection plugin**: on each task, connects to the local socket, sends
  RPCs, disconnects. First task bootstraps the daemon and SSH forwarding.
- **Action plugins**: intercept common modules and send RPCs directly instead
  of transferring Python modules.

## Updating

```bash
ansible-galaxy collection install --upgrade -r requirements.yml
```

The connection plugin detects daemon version mismatches automatically: when
it talks to a remote daemon running an older version, it kills it and uploads
the new binary on the next task. No coordinated upgrade needed.

## Disabling fastagent

To take a single host **out** of fastagent without uninstalling anything,
override `ansible_connection` in `host_vars/`:

```yaml
# host_vars/web1.example.com.yml
ansible_connection: ssh
```

The host falls back to standard SSH on the next run. Useful as an escape
hatch if a particular host triggers a bug.

To remove the daemon and binary from a remote host entirely:

```bash
ssh myhost 'sudo pkill fastagent; sudo rm -rf /tmp/fastagent-* ~/.ansible/fastagent'
```

To disable the auto-download of the agent binary on the controller (e.g. for
air-gapped environments where you ship the binary out-of-band):

```bash
export FASTAGENT_DOWNLOAD_URL=
```

…or set `fastagent_download_url: ""` in inventory.

## Troubleshooting

### Verbose output

```bash
ansible-playbook playbook.yml -vvv
```

At `-vvv`, the connection plugin logs every step (`FASTAGENT: ...`) and the
daemon enables debug-level logging (every RPC is logged with millisecond
timestamps).

### Daemon log

The daemon logs to a file next to its socket on the remote host:

```bash
ssh myhost "sudo cat /tmp/fastagent-root.sock.log"
```

### Common issues

**"failed to download agent binary from \<URL\>"**

The connection plugin tries to download the prebuilt binary from GitHub
Releases on first use. If the controller can't reach `github.com`:

1. Check the URL works manually — copy the URL from the error message and
   `curl -I <url>`.
2. If you're behind a proxy, set the standard `https_proxy` / `HTTPS_PROXY`
   env var on the controller.
3. As a fallback, build the binary locally (`make deploy`) and it will be
   placed at `~/.ansible/fastagent/`. The connection plugin checks that
   directory **before** attempting any download.

**"local socket not available, setting up" on every task**

The SSH forwarding session died between tasks. Check if the SSH ControlMaster
is working:

```bash
ssh -O check myhost
```

**"timeout waiting for socket"**

The daemon failed to start. Check the daemon log on the remote host. Common
causes: the binary wasn't uploaded (version mismatch), or a stale daemon
process is holding the socket.

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
playbook uses and add them to the override, or set `ansible_connection: ssh`
in `host_vars/` to fall back per-host.

**Temp directories owned by root**

The daemon runs as root. Commands that should run as the connecting user
(e.g. temp dir creation) are wrapped with `runuser`. If you see permission
errors on `~/.ansible/tmp/`, fix with:

```bash
ssh myhost "sudo chown -R youruser:youruser ~/.ansible/tmp"
```

## Building from source

If you prefer to build the agent binary yourself — for air-gapped environments,
to pin to an unreleased commit, or to hack on the agent — clone this repo and
run:

```bash
make deploy
```

This cross-compiles for `linux/amd64` and `linux/arm64` and copies the
binaries to `~/.ansible/fastagent/`. The connection plugin checks this
directory **before** attempting any download, so a locally built binary
always takes precedence. Requires Go 1.21+.

To cut a full release (binaries plus collection tarball):

```bash
make release
```

To run the release end-to-end with safety checks (tag, push, GitHub release,
optional Galaxy publish), see `scripts/release.sh`.

## Security

The daemon runs as root (via `sudo`) to handle privilege escalation. The Unix
socket is restricted to the connecting user's group (`root:<user_group>`,
mode `0770`). Other users on the remote host cannot connect to it.

The daemon auto-exits after 1 hour of inactivity (configurable via
`--idle-timeout`).

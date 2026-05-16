# SSH Authentication Guide

`unlimited-mcp` can delegate tasks to remote machines over SSH.
This document explains how to configure authentication so the server
never prompts for a password or passphrase during operation.

**Rule:** credentials are never stored in `config.yaml` or any tracked file.
Choose one of the three methods below.

---

## Key generation (one-time setup)

If you do not have an SSH key yet, create one with **Ed25519** — the
recommended algorithm today (Edwards-curve DSA over Curve25519, defined
by the prime `2²⁵⁵ - 19`; shorter and stronger than RSA-4096):

```bash
ssh-keygen -t ed25519 -C "unlimited-mcp"
# Saves to ~/.ssh/id_ed25519 (private) and ~/.ssh/id_ed25519.pub (public)
```

> **Compatibility:** Ed25519 requires OpenSSH 6.5+ (released January 2014).
> Most systems from Ubuntu 14.04 / CentOS 7 / Debian 8 onwards include it.
> If your remote server runs an older OS, use RSA-4096 instead:
> ```bash
> ssh-keygen -t rsa -b 4096 -C "unlimited-mcp"
> ```

Then install the public key on the remote host:

```bash
ssh-copy-id -i ~/.ssh/id_ed25519.pub user@host
```

Verify it works before configuring the MCP:

```bash
ssh user@host echo ok
```

---

## Option 1 — ssh-add (manual, per session)

Load your key into the running SSH agent once.
The agent holds it in memory for the rest of the session — no further
prompts until you log out or restart.

```bash
ssh-add ~/.ssh/id_ed25519
# Enter passphrase: (only this once)

ssh-add -l   # confirm the key is loaded
```

**macOS — persist across reboots via Keychain:**

```bash
ssh-add --apple-use-keychain ~/.ssh/id_ed25519
```

Add to `~/.ssh/config` so macOS reloads automatically on next login:

```
Host *
    UseKeychain yes
    AddKeysToAgent yes
    IdentityFile ~/.ssh/id_ed25519
```

---

## Option 2 — Environment variable

Store only the passphrase in an environment variable.
`unlimited-mcp` reads it at connection time via paramiko; the passphrase
never touches disk.

Set the variable in your shell profile or a `.env` file that is
**never committed to git**:

```bash
# ~/.zshrc / ~/.bashrc  — or a .env loaded by your shell / process manager
export UNLIMITED_MCP_SSH_PASSPHRASE="your-passphrase"
```

Then reference it in `config.yaml`:

```yaml
hosts:
  gpu_server:
    type: ssh
    user: ubuntu
    host: 192.168.1.100
    key_passphrase_env: UNLIMITED_MCP_SSH_PASSPHRASE
```

The value of `key_passphrase_env` is the **name** of the environment
variable, not the passphrase itself.

If different hosts use different keys / passphrases, use distinct variable
names per host:

```yaml
hosts:
  gpu_server:
    type: ssh
    user: ubuntu
    host: 192.168.1.100
    key_passphrase_env: MCP_SSH_PASS_GPU
  dev_box:
    type: ssh
    user: dev
    host: dev.example.com
    key_passphrase_env: MCP_SSH_PASS_DEV
```

---

## Option 3 — System keyring

The passphrase is stored in the OS-managed secure keyring and retrieved
at runtime. Nothing is written to disk in plaintext.

### macOS — Keychain

```bash
# Store
security add-generic-password -a "$(whoami)" -s "unlimited-mcp-ssh" \
    -w "your-passphrase"

# Retrieve (example — the MCP calls this internally)
security find-generic-password -a "$(whoami)" -s "unlimited-mcp-ssh" -w
```

In `config.yaml`:

```yaml
hosts:
  gpu_server:
    type: ssh
    user: ubuntu
    host: 192.168.1.100
    key_passphrase_keyring: unlimited-mcp-ssh   # keychain service name
```

### Linux — GNOME Keyring / secret-tool

```bash
# Store
secret-tool store --label="unlimited-mcp SSH" service unlimited-mcp-ssh \
    account "$(whoami)"
# Enter password: (interactive, once)

# Retrieve
secret-tool lookup service unlimited-mcp-ssh account "$(whoami)"
```

Same `config.yaml` field:

```yaml
hosts:
  gpu_server:
    type: ssh
    user: ubuntu
    host: 192.168.1.100
    key_passphrase_keyring: unlimited-mcp-ssh
```

### Linux — systemd user ssh-agent (headless / server)

On servers without a desktop keyring, run `ssh-agent` as a systemd user
service so it starts automatically and survives session reconnects:

```ini
# ~/.config/systemd/user/ssh-agent.service
[Unit]
Description=SSH key agent for unlimited-mcp

[Service]
Type=simple
Environment=SSH_AUTH_SOCK=%t/ssh-agent.socket
ExecStart=/usr/bin/ssh-agent -D -a $SSH_AUTH_SOCK

[Install]
WantedBy=default.target
```

```bash
systemctl --user enable --now ssh-agent

# Add to ~/.bashrc or ~/.profile
export SSH_AUTH_SOCK="$XDG_RUNTIME_DIR/ssh-agent.socket"

# Load your key once (or put this in a login script)
ssh-add ~/.ssh/id_ed25519
```

After this, `unlimited-mcp` connects through the agent socket just like
Option 1, without any manual `ssh-add` per session.

---

## Method comparison

| | Setup cost | Survives reboot | Headless server | Passphrase on disk |
|---|---|---|---|---|
| Option 1 — ssh-add | Low | No (re-run after reboot) | No | Never |
| Option 1 + macOS Keychain | Low | Yes | No | Never |
| Option 2 — env var | Low | Yes (if set in profile) | Yes | Never |
| Option 3 — Keyring | Medium | Yes | Linux: yes with systemd | Never |

**Recommended defaults:**
- Developer workstation (macOS/Linux desktop): Option 1 + Keychain / GNOME Keyring
- CI / headless server: Option 2 (env var injected by the secrets manager) or Option 3 with systemd agent

---

## Remote queue configuration (`remote_ts`)

A `remote_ts` queue runs jobs on a remote SSH host using
[task-spooler](https://viric.name/soft/ts/) (`ts`/`tsp`).  Unlike a
foreground SSH command (which blocks until the command finishes), the job
lives in the remote `ts` daemon and survives SSH reconnects.  The local
poller thread checks status every few seconds and writes the final
`JobResult` when the job completes.

### Prerequisites

Install task-spooler on the remote machine:

```bash
# Debian / Ubuntu
sudo apt install task-spooler

# macOS (via Homebrew installed on the remote)
brew install task-spooler
```

Verify it works:

```bash
ssh user@host which tsp || ssh user@host which ts
```

### config.yaml example

```yaml
hosts:
  gpu_server:
    type: ssh
    user: ubuntu
    host: 192.168.1.100
    # auth: any of the three options above (ssh-add / env var / keyring)

queues:
  gpu:
    type: remote_ts
    host: gpu_server   # must match a key in hosts: with type: ssh
    slots: 4           # run up to 4 jobs in parallel on the remote ts daemon
    # socket: /tmp/umcp-gpu.sock  # optional: isolate from other ts users
```

Then delegate to an agent using that queue:

```python
delegate_to_agent(
    "aider_local",
    prompt="train the model for 10 epochs",
    cwd="/home/ubuntu/ml-project",
    queue="gpu",          # uses the remote_ts queue above
)
```

### Parallelism

Set `slots` to control how many jobs run simultaneously on the remote ts
daemon.  The value is applied once via `ts -S <n>` on the first submit.
If the remote daemon is shared with other users, use a separate `socket`
path to avoid interfering with their jobs.

### Clarify rounds

`clarify_rounds` works with `remote_ts` queues: question files are
synced from the remote machine to the local `JobStore` via SFTP on each
poll iteration, and local answers are uploaded back.  Use
`get_worker_questions` / `answer_worker_questions` exactly as for local
agents.

---

## Troubleshooting

**"Permission denied (publickey)"**
- The public key is not on the remote: run `ssh-copy-id` again.
- Wrong key file: check `key_file` in config or `IdentityFile` in `~/.ssh/config`.

**"Could not open a connection to your authentication agent"**
- The ssh-agent is not running: run `eval "$(ssh-agent -s)"` then `ssh-add`.

**"sign_and_send_pubkey: signing failed: agent refused operation"**
- The key has a passphrase but is not loaded in the agent: run `ssh-add ~/.ssh/id_ed25519`.

**"Host key verification failed"**
- The remote host is not in `~/.ssh/known_hosts`: connect manually once with `ssh user@host` and accept the fingerprint.

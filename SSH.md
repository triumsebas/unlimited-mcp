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

This is the recommended option on a Mac: the SSH key passphrase lives in
the login Keychain (encrypted by macOS, visible in **Keychain Access** /
*Acceso a Llaveros*), and the MCP server reads it at connection time. The
passphrase never goes into `config.yaml`, the chat, or any file.

A keyring entry has two coordinates:

- **service** — a label you choose (e.g. `unlimited-mcp-ssh`). Goes in
  `key_passphrase_keyring`.
- **account** — *which* secret inside that service. By default the MCP
  looks it up by the **name of your private key file**
  (`key_file` basename, e.g. `id_rsa` or `id_ed25519`), **not** the
  remote SSH user. The passphrase belongs to *your local key*, so this
  lets several hosts that share one key reuse a **single** Keychain
  entry. Override it with `key_passphrase_account` if you want a custom
  name.

#### 1. Store the passphrase

Run this once **in your own terminal** (not through the MCP, so the
secret never appears in any transcript):

```bash
security add-generic-password -U -A -s "unlimited-mcp-ssh" -a "id_rsa" -w
```

- `-s` service — must match `key_passphrase_keyring` in `config.yaml`.
- `-a` account — must match the key basename (or your
  `key_passphrase_account`). Here: `id_rsa`.
- `-w` **with nothing after it** → macOS prompts you interactively
  (hidden input, asked twice). This is the safe form.
- `-U` updates the entry if it already exists.
- `-A` lets any of your apps read it **without a per-access dialog**.
  **This is effectively required for the MCP server** — it runs
  headless and cannot answer the macOS Keychain prompt. Omitting `-A`
  is the single most common reason a correctly-stored passphrase still
  "doesn't work" (see step 2).

> ⚠️ **Argument-order pitfall:** `-w` accepts an *optional* inline value,
> so `... -w -U` makes `security` store the literal string `-U` as your
> "passphrase" and never prompts you. Always put `-w` **last** with no
> value, and the other flags before it, exactly as shown above.

#### 2. macOS authorization — why `-A` matters

The MCP server runs as a Python process launched by the host app (e.g.
Claude). When that process reads the Keychain item, macOS enforces the
item's access control list (ACL). If the server's process is not on the
ACL, macOS would normally show *"<app> wants to use information stored
in your keychain"* — but the server is **headless** and there is no one
to click it, so the read **fails silently** (returns nothing).

What that failure looks like (important — it is *not* an obvious
"permission denied"):

- The server gets an empty passphrase, tries the encrypted key without
  it, and the SSH job fails with a **misleading** error such as
  `encountered RSA key, expected OPENSSH key` or
  `Private key file is encrypted`.
- The same entry reads fine from *your* terminal (`security
  find-generic-password ... -w` works), which makes it look like the
  entry is correct — it is; the server just can't reach it.

`-A` puts *all* your apps on the ACL, so the headless server reads it
with no dialog. This is why **the working command in step 1 includes
`-A`**, and re-storing without it silently breaks the server.

Tighter alternative (more secure, more setup): drop `-A` and authorize
only the exact interpreter the server runs:

```bash
# Find the interpreter the server uses (the path after the venv):
#   ps -o command= -p "$(pgrep -f 'unlimited.mcp serve' | head -1)"
security add-generic-password -U -s "unlimited-mcp-ssh" -a "id_rsa" \
    -T /path/to/.venv/bin/python3 -w
```

Note `-T` ties the entry to that exact binary path; if the venv or
Python version changes you must re-grant. For most setups `-A` with a
dedicated SSH passphrase is the pragmatic choice.

#### 3. Point `config.yaml` at it

```yaml
hosts:
  gpu_server:
    type: ssh
    user: ubuntu
    host: 192.168.1.100
    key_file: ~/.ssh/id_rsa
    key_passphrase_keyring: unlimited-mcp-ssh   # = service (-s)
    # key_passphrase_account: id_rsa            # optional; defaults to
    #                                             the key_file basename
```

Two hosts sharing the same key need **no duplicate entry** — both resolve
to account `id_rsa`:

```yaml
hosts:
  localbox:
    type: ssh
    user: mcp
    host: localhost
    key_file: ~/.ssh/id_rsa
    key_passphrase_keyring: unlimited-mcp-ssh
  vps:
    type: ssh
    user: root
    host: 203.0.113.10
    key_file: ~/.ssh/id_rsa
    key_passphrase_keyring: unlimited-mcp-ssh
```

#### 4. Verify it is stored correctly

Three quick checks, none of which print the secret. Run them in a
private terminal.

**a. The entry exists with the expected account:**

```bash
security find-generic-password -s "unlimited-mcp-ssh" -a "id_rsa" \
    | grep '"acct"'
```

**b. The stored value actually unlocks the key** (catches typos and the
`-w -U` pitfall — a wrong passphrase fails here):

```bash
PASS=$(security find-generic-password -s "unlimited-mcp-ssh" -a "id_rsa" -w)
ssh-keygen -y -P "$PASS" -f ~/.ssh/id_rsa >/dev/null 2>&1 \
    && echo "OK: passphrase unlocks the key" \
    || echo "WRONG: stored value does not unlock the key — re-store it"
unset PASS
```

**c. The MCP server's interpreter can read it** (catches the missing
`-A` / ACL problem from step 2 — this is the check that matters, since
the server, not your shell, is what fails):

```bash
# Use the SAME python the server runs (the project venv):
/path/to/.venv/bin/python3 -c \
  "import keyring; v=keyring.get_password('unlimited-mcp-ssh','id_rsa'); \
   print('readable, len', len(v)) if v else print('NOT readable — re-store with -A')"
```

If (b) says OK and (c) says readable, the entry is correct. Then test a
host end-to-end: `run_command(['hostname'], exec_host='vps')`.

> If (b) is OK but the SSH job still fails with `encountered RSA key,
> expected OPENSSH key` / `Private key file is encrypted`, it is almost
> always (c) failing: re-store the entry **with `-A`** (step 1).

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

## Git credentials on the remote

When a worker needs to clone, fetch, or push to GitHub, the remote machine
must be able to authenticate.  Two strategies are supported — pick one based
on how your team manages SSH keys.

### Strategy A — SSH agent forwarding (recommended)

The orchestrator forwards its local SSH agent to the remote session.  The
worker authenticates to GitHub using the local key — **no key material is
ever stored on the remote host**.

Enable it with a single field in `config.yaml`:

```yaml
hosts:
  gpu_server:
    type: ssh
    user: ubuntu
    host: 192.168.1.100
    forward_agent: true   # default: false
    repos_root: /home/ubuntu/repos   # see "Repo layout" below
```

Bootstrap the repo on the remote the first time (the orchestrator runs this):

```python
run_command(["git", "clone", "git@github.com:org/repo.git", "/home/ubuntu/repo"],
            host="gpu_server")
```

Subsequent pushes from the worker branch work transparently because the
agent is available for the lifetime of each SSH channel that unlimited-mcp
opens.

**Prerequisites:**
- Your local SSH key is loaded in the agent (`ssh-add -l` should list it).
- The public key is registered in GitHub (Settings → SSH and GPG keys).
- The remote server's `sshd_config` must have `AllowAgentForwarding yes`
  (default on most distributions).

---

### Strategy B — HTTPS with a token

If your team uses HTTPS for git, pass a GitHub token via `env_extra` on the
agent.  The token is read from your local environment — never written to
the remote disk.

```yaml
agents:
  aider_remote:
    cli: aider
    host: gpu_server
    env_extra:
      GIT_TOKEN: "${GITHUB_TOKEN}"   # expands from the local env at call time
```

Bootstrap with the token embedded in the HTTPS URL:

```python
run_command(
    ["git", "clone",
     "https://${GIT_TOKEN}@github.com/org/repo.git", "/home/ubuntu/repo"],
    host="gpu_server",
    env_extra={"GIT_TOKEN": os.environ["GITHUB_TOKEN"]},
)
```

Workers inherit `GIT_TOKEN` and can use it for `git push` via the standard
[git credential helper](https://git-scm.com/docs/gitcredentials) or
`GIT_ASKPASS`.

---

### Comparison

| | SSH agent forwarding | HTTPS + token |
|---|---|---|
| Key material on remote | Never | Never (token in env only) |
| GitHub setup | SSH key registered | Personal access token (PAT) |
| `config.yaml` change | `forward_agent: true` | `env_extra: GIT_TOKEN` |
| Works without agent running | No | Yes |
| Limitation | Requires live MCP SSH connection | Token must be rotated periodically |

---

### Repo layout on the remote (`repos_root`)

`repos_root` sets the base directory where all repos live on a given host.
It is a host-level config field — not per-agent, not per-call.

```yaml
hosts:
  gpu_server:
    type: ssh
    user: ubuntu
    host: 192.168.1.100
    forward_agent: true
    repos_root: /home/ubuntu/repos
```

The orchestrator constructs the full working directory as
`repos_root / repo_name`, where `repo_name` comes from the project context:

```
/home/ubuntu/repos/unlimited-mcp
/home/ubuntu/repos/client-project
/home/ubuntu/repos/data-pipeline
```

Bootstrap a repo once:

```python
run_command(
    ["git", "clone", "git@github.com:org/repo.git"],
    cwd=host_config.repos_root,   # clones into repos_root/repo
    exec_host="gpu_server",
)
```

Then delegate with the full path:

```python
delegate_to_agent(
    "aider_remote",
    prompt="add docstrings to all public functions",
    cwd=f"{host_config.repos_root}/unlimited-mcp",
)
```

Passing an explicit `cwd` to `delegate_to_agent` overrides `repos_root`
whenever you need to work outside the standard layout (e.g. a temp clone
for a one-off experiment).

---

### Remote-only repo (no local clone)

If the repo only lives on the remote machine, the orchestrator cannot review
diffs locally.  As a workaround, delegate a code-review pass to a remote LLM
worker:

```python
delegate_to_agent("claude_remote", prompt="Review the diff in /tmp/change.patch
and summarise risks", host="gpu_server")
```

This is a minor limitation for most workflows; the preferred pattern is a
shared GitHub repo (clone on both local and remote) so the orchestrator can
`git fetch` and inspect the branch after the job completes.

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

**"Private key file is encrypted" (Keychain option)**
- The MCP can't reach the passphrase. Check the Keychain account matches:
  by default it's the `key_file` basename (`id_rsa`), *not* the SSH user.
  Set `key_passphrase_account` or store the entry under the right account.
- macOS may be silently blocking the read with an authorization dialog —
  see *macOS authorization note* above; use **Always Allow** or `-A`.

**Keychain entry stores `-U` (or another flag) as the passphrase**
- The `security ... -w -U ...` argument-order pitfall: `-w` swallowed the
  next flag as its value. Re-store with `-w` **last**:
  `security add-generic-password -U -A -s SERVICE -a ACCOUNT -w`

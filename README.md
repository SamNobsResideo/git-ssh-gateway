# git-ssh-gateway

Read-only GitHub SSH gateway for an isolated agent.

This repository provides a small, auditable SSH gateway that lets a restricted `agent-user`
fetch allowlisted private GitHub repositories by reusing a `real-user` account's existing
GitHub SSH identity without exposing the GitHub private key to the agent.

## Architecture

```text
Agent / Yocto
      |
      | SSH
      v
127.0.0.1:2222
      |
      | validate:
      |   upload-pack?
      |   allowed repo?
      v
Gateway
      |
      | SSH + real-user's GitHub key
      v
GitHub
```

The local SSH daemon terminates the agent connection on loopback, forces every session through
`agent_git_gateway.py`, and the gateway opens a separate downstream SSH connection to GitHub by
using a dedicated SSH host alias such as `github.com-agent-gateway`. This avoids recursion and
keeps the real GitHub key in `real-user`'s account. The forced command preserves
`SSH_ORIGINAL_COMMAND` across `sudo` so the gateway can inspect the requested Git operation.

## Threat model

The design assumes:

- `agent-user` may run arbitrary programs, including `/usr/bin/ssh`, `/usr/bin/git`, Python, or C.
- Existing Git/Yocto SSH URLs such as `git@github.com:company/repository.git` must continue to work.
- The GitHub private key remains readable only by `real-user`.
- The agent must not be able to push, run arbitrary commands, or fetch non-allowlisted repositories.

Security comes from four layers:

1. credential isolation (`real-user` owns the GitHub SSH key)
2. network isolation (firewall blocks direct outbound TCP/22 for `agent-user`)
3. gateway-side command validation (`ForceCommand` + `SSH_ORIGINAL_COMMAND`)
4. exact repository allowlisting

This gateway covers only Git-over-SSH. It does **not** provide GitHub API, `gh`, HTTPS Git,
REST, or GraphQL access.

## Repository layout

- `agent_git_gateway.py` – stdlib-only gateway implementation
- `tests/test_agent_git_gateway.py` – automated unit tests
- `config/repos.conf.example` – allowlist example
- `config/downstream_ssh_config.example` – SSH alias for the downstream GitHub connection
- `config/sshd_config.gateway.example` – dedicated loopback sshd instance
- `config/agent-user-ssh_config.example` – `agent-user` client config that preserves normal Git URLs
- `config/agent-git-gateway.nft.example` – nftables rule blocking direct SSH egress for `agent-user`
- `systemd/agent-git-gateway-sshd.service` – dedicated systemd service for the gateway sshd
- `scripts/install.sh` / `scripts/uninstall.sh` – installation helpers
- `scripts/manual-security-checks.sh` – manual verification checklist

## How the gateway works

`agent_git_gateway.py`:

1. reads `SSH_ORIGINAL_COMMAND`
2. rejects missing or malformed commands
3. parses the command with `shlex.split()` instead of a shell
4. permits only the exact operation `git-upload-pack`
5. canonicalizes the repository path to `owner/repository`
6. checks the repository against the allowlist
7. opens a second SSH session to GitHub using the downstream alias
8. preserves `GIT_PROTOCOL` so Git protocol v2 works over SSH
9. inherits stdin/stdout/stderr for the Git data stream
10. logs allow/deny decisions and downstream exit codes

The implementation never uses `shell=True` and never forwards the raw `SSH_ORIGINAL_COMMAND`
string to a shell.

## Installation

The example installation uses a locked local `git` account because standard GitHub SSH URLs
already target `git@github.com`, and OpenSSH preserves that username from the client URL.
The gateway command itself still runs as `real-user` via a tightly scoped `sudo -n` rule.

1. Ensure permissions remain strict:

   ```text
   /home/real-user                   0700 real-user:real-user
   /home/real-user/.ssh              0700 real-user:real-user
   /home/real-user/.ssh/<github-key> 0600 real-user:real-user
   ```

2. Generate a dedicated gateway keypair for `agent-user`:

   ```bash
   sudo -u agent-user ssh-keygen -t ed25519 -f /home/agent-user/.ssh/agent-gateway-ed25519
   ```

3. Install the gateway as root:

   ```bash
   sudo ./scripts/install.sh real-user /home/real-user/.ssh/id_ed25519_github
   ```

4. Append `/home/agent-user/.ssh/agent-gateway-ed25519.pub` to:

   ```text
   /etc/agent-git-gateway/authorized_keys
   ```

5. Review and edit:

   - `/etc/agent-git-gateway/repos.conf`
   - `/etc/agent-git-gateway/downstream_ssh_config`
   - `/etc/agent-git-gateway/sshd_config`
   - `/etc/agent-git-gateway/ssh_host_ed25519_key.pub`

6. Copy `config/agent-user-ssh_config.example` into `~agent-user/.ssh/config`.

7. Load the firewall rule from `config/agent-git-gateway.nft.example` after reviewing it.

## Configuration

### Allowlist

Allow only exact `owner/repository` entries in `/etc/agent-git-gateway/repos.conf`:

```text
company/truman
company/oclea
company/another-private-repository
```

Blank lines and `#` comments are ignored. The gateway normalizes inputs such as leading `/`,
`./`, duplicate slashes, and a trailing `.git`, and rejects traversal, extra arguments,
whitespace tricks, shell metacharacters, and non-`owner/repository` layouts.

### Downstream SSH configuration

Use a dedicated host alias so the gateway does not recurse back into itself:

```sshconfig
Host github.com-agent-gateway
    HostName github.com
    User git
    IdentityFile /home/real-user/.ssh/id_ed25519_github
    IdentitiesOnly yes
```

This file should remain unreadable to `agent-user`; the example installer sets it to `0640`
and groups it to `real-user`.

### Local sshd configuration

The dedicated sshd instance must listen only on loopback (`127.0.0.1:2222`), authenticate only
the gateway key, force the gateway command, and disable interactive features:

- no shell
- no PTY
- no agent forwarding
- no TCP forwarding
- no X11 forwarding
- no environment injection
- no SFTP

Use `config/sshd_config.gateway.example` as the baseline.

### Agent-side SSH configuration

Install this under `~agent-user/.ssh/config`:

```sshconfig
Host github.com
    HostName 127.0.0.1
    Port 2222
    User git
    IdentityFile /home/agent-user/.ssh/agent-gateway-ed25519
```

That keeps existing Git and Yocto SSH URLs unchanged.

### Firewall configuration

`config/agent-git-gateway.nft.example` blocks outbound TCP/22 for `agent-user` except loopback.
This prevents direct `ssh git@github.com` or direct `/usr/bin/git clone git@github.com:...`
from bypassing the gateway.

## Starting, stopping, and restarting

```bash
sudo systemctl start agent-git-gateway-sshd.service
sudo systemctl stop agent-git-gateway-sshd.service
sudo systemctl restart agent-git-gateway-sshd.service
sudo systemctl status agent-git-gateway-sshd.service
```

## Git and Yocto usage

Normal Git SSH URLs continue to work:

```bash
git clone git@github.com:company/truman.git
git fetch
git ls-remote git@github.com:company/truman.git
```

Yocto recipes can keep:

```bitbake
SRC_URI = "git://github.com/company/truman.git;protocol=ssh;branch=main"
```

Git protocol v2 is preserved through `GIT_PROTOCOL` forwarding. Example verification:

```bash
GIT_TRACE=1 GIT_TRACE_PACKET=1 git -c protocol.version=2 \
    ls-remote git@github.com:company/truman.git
```

## Logging

The gateway logs timestamped decisions to stderr/journald via the forced command:

```text
ALLOW user=agent-user operation=git-upload-pack repo=company/truman downstream_exit=0
DENY user=agent-user reason=operation-not-allowed message=operation 'git-receive-pack' is not allowed
DENY user=agent-user reason=repository-not-allowed message=repository 'other/private-repo' is not allowed
```

It does **not** log private keys, tokens, or Git packfile contents.

## Automated tests

Run the targeted unit tests:

```bash
python3 -m unittest discover -s tests -v
```

The tests cover:

- safe `SSH_ORIGINAL_COMMAND` parsing
- repository normalization
- allowlist enforcement
- rejection of push/arbitrary commands/extra arguments
- `GIT_PROTOCOL` preservation for protocol v2

## Manual security verification

Run `scripts/manual-security-checks.sh` as `agent-user`. The script exits immediately if it is
started under another account, because the credential-isolation checks are only meaningful from
the restricted account's point of view.

Use `scripts/manual-security-checks.sh` as a checklist for:

- allowed fetch/clone/ls-remote operations
- denied `git push`
- denied `ssh git@github.com`
- denied `git-upload-archive`
- denied arbitrary commands such as `uname -a`
- allowlist enforcement
- parser bypass probes (`../`, `./`, leading `/`, extra args, metacharacters, whitespace)
- credential isolation checks
- direct network bypass checks

Because this repository is being developed in a sandbox rather than on a real workstation, the
manual checks are documented but not executed against a live GitHub organization here. Equivalent
normalized paths such as `./owner/repo.git`, `/owner/repo.git`, and duplicate slashes should
resolve to the same allowlisted repository; traversal and extra-argument variants should fail.

## Troubleshooting

- If fetches fail with protocol v2 clients, verify `GIT_PROTOCOL` is present in the gateway
  environment and that the downstream command includes `SendEnv=GIT_PROTOCOL`.
- If the gateway loops back into itself, verify the downstream host alias is
  `github.com-agent-gateway` and that only the `agent-user` config rewrites `github.com`.
- If authentication fails, confirm the local gateway key is present in
  `/etc/agent-git-gateway/authorized_keys`.
- If the gateway cannot read the downstream SSH config, verify file ownership and the `sudo`
  rule that runs the gateway as `real-user`.
- If `agent-user` can still SSH directly to port 22, verify the nftables rule is loaded.

## Rotating the GitHub SSH key

1. Add the new key to GitHub for `real-user`.
2. Update `/etc/agent-git-gateway/downstream_ssh_config` to point at the new `IdentityFile`.
3. Restart the gateway sshd:

   ```bash
   sudo systemctl restart agent-git-gateway-sshd.service
   ```

4. Re-run the manual verification script.

## Revoking the agent gateway key

1. Remove the corresponding public key line from `/etc/agent-git-gateway/authorized_keys`.
2. Reload the gateway sshd:

   ```bash
   sudo systemctl restart agent-git-gateway-sshd.service
   ```

This revokes only the agent-to-gateway credential and does not affect the real GitHub key.

## Uninstall

```bash
sudo ./scripts/uninstall.sh
```

The uninstall script removes the dedicated sshd service, sudoers entry, installed files, and
configuration directory. It intentionally leaves the locked `git` account in place for an
administrator to review or remove explicitly.

## Security assumptions

- The workstation enforces normal Unix permissions between `real-user` and `agent-user`.
- `agent-user` cannot become root.
- The firewall rule is actually loaded.
- The downstream SSH alias remains separate from the `agent-user`'s rewritten `github.com`.
- This gateway does not add GitHub API or HTTPS credentials; those remain separate by design.

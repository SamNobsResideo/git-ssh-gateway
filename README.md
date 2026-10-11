# git-ssh-gateway

Read-only GitHub SSH gateway for an isolated agent.

This repository provides a small, auditable SSH gateway that lets a restricted `agent-user`
fetch policy-allowed GitHub repositories by reusing a `real-user` account's existing
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
`SSH_ORIGINAL_COMMAND` across `sudo` so the gateway can inspect the requested Git operation, and
it also preserves `SSH_GATEWAY_ORIGINAL_USER` so audit logs can record the original caller.

## Threat model

The design assumes:

- `agent-user` may run arbitrary programs, including `/usr/bin/ssh`, `/usr/bin/git`, Python, or C.
- Existing Git/Yocto SSH URLs such as `git@github.com:company/repository.git` must continue to work.
- The GitHub private key remains readable only by `real-user`.
- The agent must not be able to push, run arbitrary commands, or fetch repositories blocked by policy.

Security comes from four layers:

1. credential isolation (`real-user` owns the GitHub SSH key)
2. network isolation (firewall blocks direct outbound TCP/22 for `agent-user`)
3. gateway-side command validation (`ForceCommand` + `SSH_ORIGINAL_COMMAND`)
4. repository policy enforcement

This gateway covers Git-over-SSH and SSH authentication for Git LFS downloads. It does **not**
provide general GitHub API, `gh`, HTTPS Git, REST, or GraphQL access. LFS authentication returns
temporary HTTPS credentials to the agent; see the LFS security boundary below.

## Repository layout

- `agent_git_gateway.py` – stdlib-only gateway implementation
- `tests/test_agent_git_gateway.py` – automated unit tests
- `config/repos.conf.example` – allow/deny rule example
- `config/downstream_ssh_config.example` – SSH alias for the downstream GitHub connection
- `config/sshd_config.gateway.example` – dedicated loopback sshd instance
- `config/agent-user-ssh_config.example` – `agent-user` client config that preserves normal Git URLs
- `config/agent-git-gateway.nft.example` – nftables rule blocking direct SSH egress for `agent-user`
- `systemd/agent-git-gateway-sshd.service` – dedicated systemd service for the gateway sshd
- `scripts/install.sh` / `scripts/uninstall.sh` – installation helpers
- `scripts/agent-git-gateway-force-command.sh.in` – forced-command wrapper template used by the installer
- `scripts/manual-security-checks.sh` – manual verification checklist

## How the gateway works

`agent_git_gateway.py`:

1. reads `SSH_ORIGINAL_COMMAND`
2. rejects missing or malformed commands
3. parses the command with `shlex.split()` instead of a shell
4. permits only `git-upload-pack` or `git-lfs-authenticate` with the exact action `download`
5. canonicalizes the repository path to `owner/repository`
6. checks the repository against the configured allow/deny rules
7. opens a second SSH session to GitHub using the downstream alias
8. preserves `GIT_PROTOCOL` so Git protocol v2 works over SSH
9. inherits stdin/stdout/stderr for the Git data stream or LFS authentication JSON
10. logs allow/deny decisions and downstream exit codes

The implementation never uses `shell=True` and never forwards the raw `SSH_ORIGINAL_COMMAND`
string to a shell.

## Installation

The example installation uses a dedicated local `git` account because standard GitHub SSH URLs
already target `git@github.com`, and OpenSSH preserves that username from the client URL.
That account must use `/bin/sh`: OpenSSH executes even `ForceCommand` through the account's
shell with `-c`. `/usr/sbin/nologin` prevents the gateway from running and prints
`This account is currently not available.` into the Git protocol stream.
The dedicated daemon's `ForceCommand`, disabled passwords, disabled PTYs, and disabled forwarding
prevent interactive SSH access; a no-login shell is not the mechanism enforcing that restriction.
The account has an unusable password hash, but must not remain shadow-locked on Ubuntu because
sshd rejects locked accounts before public-key authentication.
The installer now gives that local account a dedicated home directory under
`/var/lib/agent-git-gateway/git-home` so the loopback sshd does not depend on `/nonexistent`
or other missing-home edge cases.
The gateway command itself still runs as `real-user` via a tightly scoped `sudo -n` rule.
The included installer targets Ubuntu/Linux with GNU userland tools, which matches the intended
deployment environment for this project.
The `git` account must be dedicated to this gateway. If another SSH daemon permits this account,
restrict or deny it there too: the loopback daemon's `ForceCommand` does not apply to other daemons.

The installer uses `/etc/agent-git-gateway` mode `0755`, owned by `root:root`, and
`authorized_keys` mode `0644`, also owned by `root:root`. This lets the authenticating `git`
account traverse the directory and read the public authorization entries without modifying them.
Private host keys remain `0600`, and downstream configuration and repository rules remain `0640`.
Do not recursively loosen permissions on this directory.

Reinstalling preserves existing authorization keys, host keys, known hosts, and repository rules,
and repairs authorization-file permissions. It regenerates `sshd_config` and
`downstream_ssh_config` from the templates and restarts the dedicated daemon, so back up any
customizations to those two files first. Uninstalling deletes the configuration directory.

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

### Repository rules

`/etc/agent-git-gateway/repos.conf` supports `allow` and `deny` rules. Bare entries remain valid
and are treated as `allow` rules for backward compatibility. `deny` overrides `allow`, and `*`
wildcards are supported in both the owner and repository portions.

Example:

```text
allow */*
allow company/truman
allow company/oclea
deny company/private-*
```

Blank lines and `#` comments are ignored. The gateway normalizes inputs such as leading `/`,
`./`, duplicate slashes, and a trailing `.git`, and rejects traversal, extra arguments,
whitespace tricks, shell metacharacters, and non-`owner/repository` layouts. A broad rule such as
`allow */*` lets public repositories continue to work through the gateway, while deny rules can
still block specific repositories or groups of repositories.

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
and groups it to `real-user`. The example also pins host-key verification:

```sshconfig
    StrictHostKeyChecking yes
    UserKnownHostsFile /etc/agent-git-gateway/github_known_hosts
```

Populate that known-hosts file explicitly, for example:

```bash
sudo ssh-keyscan github.com >> /etc/agent-git-gateway/github_known_hosts
```

Verify the scanned key against GitHub's published SSH host fingerprints before trusting it.

### Local sshd configuration

The dedicated sshd instance must listen only on loopback (`127.0.0.1:2222`), authenticate only
the gateway key, accept `GIT_PROTOCOL`, force the gateway command, and disable interactive
features:

- no shell
- no PTY
- no agent forwarding
- no TCP forwarding
- no X11 forwarding
- no environment injection
- no SFTP

It also disables PAM (`UsePAM no`) because this is a dedicated public-key-only loopback daemon
and PAM account-state checks can reject the local service account before key authentication on
Ubuntu.

Use `config/sshd_config.gateway.example` as the baseline.

### Agent-side SSH configuration

Install this under `~agent-user/.ssh/config`:

```sshconfig
Host github.com
    HostName 127.0.0.1
    Port 2222
    User git
    SendEnv GIT_PROTOCOL
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

### Git LFS downloads

For an allowed repository with an SSH remote, `git lfs pull` can authenticate through the gateway.
Git LFS requests `git-lfs-authenticate owner/repository.git download`; the gateway applies the
same repository rules as ordinary fetches and forwards only this validated download request to
GitHub. LFS `upload`, unknown actions, extra arguments, and `git-lfs-transfer` are denied.

GitHub returns authentication JSON containing an HTTPS endpoint and authorization headers.
The gateway relays that response unchanged on stdout, without recording it in its audit logs.
Git LFS then performs the batch request and object downloads **directly over HTTPS**, not through
the SSH gateway. The agent needs outbound TCP/443 and DNS access to the returned LFS endpoints,
including any storage or redirect hosts required for downloads. The example firewall blocks only
outbound SSH; additional deployment firewalls or proxies may need HTTPS rules.

Newer Git LFS clients may first probe `git-lfs-transfer` for pure SSH transfers. This operation
is deliberately unsupported; clients using the normal hybrid transport fall back to
`git-lfs-authenticate` after that probe fails. Clients configured to require pure SSH transfers
must use hybrid transport instead (`git config lfs.sshtransfer never`; the default `negotiate`
also permits fallback).

#### LFS security boundary

The GitHub private SSH key remains isolated, but LFS authentication intentionally exposes an
HTTPS bearer credential to the agent. The gateway authorizes issuance for an allowed repository
and the `download` action only; it cannot enforce what GitHub subsequently permits that
credential to do, revoke it when repository rules change, or inspect direct HTTPS requests.
Do not treat rejection of the SSH `upload` command as proof that a returned credential cannot
authorize writes or access another repository.

Credential scope and lifetime are controlled by the downstream provider. The Git LFS
[authentication specification](https://github.com/git-lfs/git-lfs/blob/main/docs/api/authentication.md)
defines optional `expires_in` / `expires_at` expiration hints; there is no hard-coded lifetime in
this gateway, and client-side expiration hints are not proof of server-enforced expiration.
Public documentation reviewed for this change did not establish GitHub guarantees for
download-only scope, repository scope, or a fixed maximum token lifetime. Validate GitHub's
actual restrictions and expiration in a controlled deployment before relying on strict read-only
access; observed behavior is not a lasting provider guarantee. If those guarantees cannot be
established, use a downstream identity with independently enforced read-only access or a separate policy-enforcing LFS proxy
instead of relying solely on this authentication passthrough.

#### Deployment verification

- Ensure the repository is allowed and `git lfs pull` downloads a missing LFS object.
- Confirm `git-lfs-authenticate ... upload` and authentication for denied repositories fail.
- Confirm normal `git fetch` still works and `git push` remains denied.
- Check that gateway audit logs contain decisions, not response headers or object contents.
- Verify credential scope and expiration using controlled repositories; do not publish credentials.
- If transport diagnosis is needed, run `GIT_TRACE=1 git lfs pull` locally and inspect the SSH
  command. Treat traces as sensitive and redact credentials before sharing.

The reported deployment log showed the exact request
`git-lfs-authenticate oclea/rvp-cooper-sdk.git download`. A local Git LFS 3.8.0 trace also
confirmed the pure SSH probe followed by authentication fallback; live GitHub token restrictions
and end-to-end downloads were not verified in the sandbox.

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
- allow/deny rule enforcement
- rejection of push/arbitrary commands/extra arguments
- `GIT_PROTOCOL` preservation for protocol v2
- explicit `run_as_user` HOME selection for downstream SSH
- LFS download authentication, rejection of upload/unsafe requests, and repository policy
- unchanged authentication JSON on stdout, stderr-only diagnostics, and downstream exit status
- installer public-key permissions, preserved keys/policy, and forced-command shell configuration

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
- allow/deny rule enforcement
- parser bypass probes (`../`, `./`, leading `/`, extra args, metacharacters, whitespace)
- credential isolation checks
- direct network bypass checks

Because this repository is being developed in a sandbox rather than on a real workstation, the
manual checks are documented but not executed against a live GitHub organization here. Equivalent
normalized paths such as `./owner/repo.git`, `/owner/repo.git`, and duplicate slashes should
resolve to the same repository rule target; traversal and extra-argument variants should fail.

## Troubleshooting

- If authentication fails after reinstalling, check that `git` can read
  `/etc/agent-git-gateway/authorized_keys` and that it still contains the offered public key.
  Reinstalling this version repairs the directory and file permissions; it cannot restore keys
  deleted by an uninstall.
- If Git fails with `bad line length character: This` after authentication succeeds, check
  `getent passwd git`: `/usr/sbin/nologin` emits text instead of executing the forced command.
  Reinstall this version, or repair an existing dedicated gateway installation with:

  ```bash
  sudo usermod --shell /bin/sh git
  sudo chown root:root /etc/agent-git-gateway /etc/agent-git-gateway/authorized_keys
  sudo chmod 0755 /etc/agent-git-gateway
  sudo chmod 0644 /etc/agent-git-gateway/authorized_keys
  ```

  Keep the dedicated daemon's `ForceCommand` and forwarding restrictions in place, and ensure
  other SSH daemons do not allow unrestricted access to `git`. Retry `git pull`; these shell
  and permission repairs alone do not require a daemon restart.
- If LFS fails with "command must contain exactly an operation and a repository", update the
  installed gateway implementation to this LFS-aware version. The old parser accepts only
  two-argument `git-upload-pack` requests.
- If LFS authentication succeeds but downloads fail, check HTTPS connectivity and the downstream
  identity's LFS permissions. A denied `git-lfs-transfer` probe alone is expected with fallback.
- If fetches fail with protocol v2 clients, verify `GIT_PROTOCOL` is present in the gateway
  environment and that the downstream command includes `SendEnv=GIT_PROTOCOL`.
- If the gateway loops back into itself, verify the downstream host alias is
  `github.com-agent-gateway` and that only the `agent-user` config rewrites `github.com`.
- If authentication fails, confirm the local gateway key is present in
  `/etc/agent-git-gateway/authorized_keys`.
- If the loopback sshd still closes connections during preauth, inspect:
  - `journalctl -u agent-git-gateway-sshd.service -n 100 --no-pager`
  - `getent passwd git`
  - `sudo -u agent-user ssh -vvv git@github.com`
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
configuration directory. It intentionally leaves the dedicated local `git` account in place for
an administrator to review or remove explicitly.

## Security assumptions

- The workstation enforces normal Unix permissions between `real-user` and `agent-user`.
- `agent-user` cannot become root.
- The firewall rule is actually loaded.
- The downstream SSH alias remains separate from the `agent-user`'s rewritten `github.com`.
- This gateway does not add general GitHub API or HTTPS Git credentials. LFS download
  authentication does return provider-controlled HTTPS credentials, with the limitations above.

#!/bin/sh
set -eu

# This installer targets Ubuntu/Linux systems with GNU userland tools.

if [ "$(id -u)" -ne 0 ]; then
    echo "install.sh must be run as root" >&2
    exit 1
fi

if [ "${1-}" = "" ] || [ "${2-}" = "" ]; then
    echo "usage: $0 REAL_USER /home/REAL_USER/.ssh/github-key" >&2
    exit 1
fi

REAL_USER="$1"
GITHUB_KEY_PATH="$2"
REPO_ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
INSTALL_DIR="/etc/agent-git-gateway"
LIBEXEC_DIR="/usr/local/libexec"

if ! printf '%s\n' "$REAL_USER" | grep -Eq '^[A-Za-z_][A-Za-z0-9_]*$'; then
    echo "REAL_USER must be a simple local account name using only letters, digits, and underscores" >&2
    exit 1
fi

if ! id "$REAL_USER" >/dev/null 2>&1; then
    echo "REAL_USER does not exist: $REAL_USER" >&2
    exit 1
fi

case "$GITHUB_KEY_PATH" in
    "/home/$REAL_USER/.ssh/"*)
        ;;
    *)
        echo "GITHUB_KEY_PATH must point inside /home/$REAL_USER/.ssh/" >&2
        exit 1
        ;;
esac

if [ ! -f "$GITHUB_KEY_PATH" ]; then
    echo "GITHUB_KEY_PATH does not exist: $GITHUB_KEY_PATH" >&2
    exit 1
fi

if [ "$(stat -c '%U' "$GITHUB_KEY_PATH")" != "$REAL_USER" ]; then
    echo "GITHUB_KEY_PATH must be owned by $REAL_USER" >&2
    exit 1
fi

install -d -m 0755 "$LIBEXEC_DIR"
install -d -m 0750 -o root -g "$REAL_USER" "$INSTALL_DIR"
install -d -m 0750 /etc/sudoers.d
install -m 0755 "$REPO_ROOT/agent_git_gateway.py" "$LIBEXEC_DIR/agent-git-gateway"
install -m 0755 "$REPO_ROOT/scripts/agent-git-gateway-force-command.sh.in" "$LIBEXEC_DIR/agent-git-gateway-force-command"
install -m 0640 "$REPO_ROOT/config/repos.conf.example" "$INSTALL_DIR/repos.conf"
install -m 0600 "$REPO_ROOT/config/sshd_config.gateway.example" "$INSTALL_DIR/sshd_config"
install -m 0640 "$REPO_ROOT/config/downstream_ssh_config.example" "$INSTALL_DIR/downstream_ssh_config"
install -m 0644 "$REPO_ROOT/systemd/agent-git-gateway-sshd.service" /etc/systemd/system/agent-git-gateway-sshd.service

if ! id git >/dev/null 2>&1; then
    useradd --home-dir /nonexistent --shell /usr/sbin/nologin --password '*' --system git
else
    usermod --home /nonexistent --shell /usr/sbin/nologin --password '*' git
fi

if [ ! -f "$INSTALL_DIR/authorized_keys" ]; then
    install -m 0600 /dev/null "$INSTALL_DIR/authorized_keys"
fi

if [ ! -f "$INSTALL_DIR/ssh_host_ed25519_key" ]; then
    ssh-keygen -q -t ed25519 -N "" -f "$INSTALL_DIR/ssh_host_ed25519_key"
fi

if [ ! -f "$INSTALL_DIR/github_known_hosts" ]; then
    install -m 0640 /dev/null "$INSTALL_DIR/github_known_hosts"
fi

python3 - "$INSTALL_DIR/sshd_config" "$INSTALL_DIR/downstream_ssh_config" "$LIBEXEC_DIR/agent-git-gateway-force-command" "$REAL_USER" "$GITHUB_KEY_PATH" <<'PY'
from pathlib import Path
import sys

sshd_config = Path(sys.argv[1])
downstream_config = Path(sys.argv[2])
force_command = Path(sys.argv[3])
real_user = sys.argv[4]
github_key_path = sys.argv[5]

sshd_config.write_text(
    sshd_config.read_text(encoding="utf-8").replace("@REAL_USER@", real_user),
    encoding="utf-8",
)

downstream_config.write_text(
    downstream_config.read_text(encoding="utf-8").replace("@GITHUB_KEY_PATH@", github_key_path),
    encoding="utf-8",
)
force_command.write_text(
    force_command.read_text(encoding="utf-8").replace("@REAL_USER@", real_user),
    encoding="utf-8",
)
PY

chown root:root "$INSTALL_DIR/authorized_keys" "$INSTALL_DIR/sshd_config"
chown root:"$REAL_USER" "$INSTALL_DIR/downstream_ssh_config" "$INSTALL_DIR/repos.conf" "$INSTALL_DIR/github_known_hosts"
chown root:root "$INSTALL_DIR/ssh_host_ed25519_key" "$INSTALL_DIR/ssh_host_ed25519_key.pub"
chown root:root "$LIBEXEC_DIR/agent-git-gateway-force-command"
chmod 0600 "$INSTALL_DIR/ssh_host_ed25519_key"
chmod 0644 "$INSTALL_DIR/ssh_host_ed25519_key.pub"

sudoers_tmp_dir="$(
    umask 077
    mktemp -d /run/agent-git-gateway.XXXXXX
)"
sudoers_tmp="$sudoers_tmp_dir/sudoers"
trap 'rm -rf "$sudoers_tmp_dir"' EXIT

cat >"$sudoers_tmp" <<EOF
git ALL=($REAL_USER) NOPASSWD: $LIBEXEC_DIR/agent-git-gateway-force-command
Defaults!$LIBEXEC_DIR/agent-git-gateway-force-command !requiretty
Defaults!$LIBEXEC_DIR/agent-git-gateway-force-command env_keep += "SSH_ORIGINAL_COMMAND GIT_PROTOCOL SSH_GATEWAY_ORIGINAL_USER"
EOF
chmod 0440 "$sudoers_tmp"
visudo -cf "$sudoers_tmp"
sudoers_target_tmp="$(
    umask 077
    mktemp /etc/sudoers.d/agent-git-gateway.XXXXXX
)"
install -m 0440 "$sudoers_tmp" "$sudoers_target_tmp"
visudo -cf "$sudoers_target_tmp"
mv "$sudoers_target_tmp" /etc/sudoers.d/agent-git-gateway

systemctl daemon-reload
systemctl enable --now agent-git-gateway-sshd.service

echo "Installed agent-git-gateway."
echo "Next steps:"
echo "  1. Generate an agent gateway key pair for agent-user."
echo "  2. Append the public key to $INSTALL_DIR/authorized_keys."
echo "  3. Populate $INSTALL_DIR/github_known_hosts (for example with: ssh-keyscan github.com >> $INSTALL_DIR/github_known_hosts)."
echo "  4. Review $INSTALL_DIR/repos.conf, $INSTALL_DIR/downstream_ssh_config, and $INSTALL_DIR/ssh_host_ed25519_key.pub."
echo "  5. Install the example nftables rule if you need direct SSH egress blocked for agent-user."

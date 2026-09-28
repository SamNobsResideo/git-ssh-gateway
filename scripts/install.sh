#!/bin/sh
set -eu

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

if ! printf '%s\n' "$REAL_USER" | grep -Eq '^[A-Za-z_][A-Za-z0-9_-]*$'; then
    echo "REAL_USER must be a simple local account name" >&2
    exit 1
fi

if ! id "$REAL_USER" >/dev/null 2>&1; then
    echo "REAL_USER does not exist: $REAL_USER" >&2
    exit 1
fi

install -d -m 0755 "$LIBEXEC_DIR"
install -d -m 0750 "$INSTALL_DIR"
install -m 0755 "$REPO_ROOT/agent_git_gateway.py" "$LIBEXEC_DIR/agent-git-gateway"
install -m 0640 "$REPO_ROOT/config/repos.conf.example" "$INSTALL_DIR/repos.conf"
install -m 0600 "$REPO_ROOT/config/sshd_config.gateway.example" "$INSTALL_DIR/sshd_config"
install -m 0640 "$REPO_ROOT/config/downstream_ssh_config.example" "$INSTALL_DIR/downstream_ssh_config"
install -m 0644 "$REPO_ROOT/systemd/agent-git-gateway-sshd.service" /etc/systemd/system/agent-git-gateway-sshd.service

if ! id git >/dev/null 2>&1; then
    useradd --home-dir /nonexistent --shell /usr/sbin/nologin --system git
fi

if [ ! -f "$INSTALL_DIR/authorized_keys" ]; then
    install -m 0600 /dev/null "$INSTALL_DIR/authorized_keys"
fi

if [ ! -f "$INSTALL_DIR/ssh_host_ed25519_key" ]; then
    ssh-keygen -q -t ed25519 -N "" -f "$INSTALL_DIR/ssh_host_ed25519_key"
fi

python3 - "$INSTALL_DIR/sshd_config" "$INSTALL_DIR/downstream_ssh_config" "$REAL_USER" "$GITHUB_KEY_PATH" <<'PY'
from pathlib import Path
import sys

sshd_config = Path(sys.argv[1])
downstream_config = Path(sys.argv[2])
real_user = sys.argv[3]
github_key_path = sys.argv[4]

sshd_config.write_text(
    sshd_config.read_text(encoding="utf-8").replace("real-user", real_user),
    encoding="utf-8",
)
downstream_config.write_text(
    downstream_config.read_text(encoding="utf-8").replace(
        "/home/real-user/.ssh/id_ed25519_github", github_key_path
    ),
    encoding="utf-8",
)
PY

chown root:root "$INSTALL_DIR/authorized_keys" "$INSTALL_DIR/sshd_config"
chown root:"$REAL_USER" "$INSTALL_DIR/downstream_ssh_config" "$INSTALL_DIR/repos.conf"
chown root:root "$INSTALL_DIR/ssh_host_ed25519_key" "$INSTALL_DIR/ssh_host_ed25519_key.pub"
chmod 0600 "$INSTALL_DIR/ssh_host_ed25519_key"
chmod 0644 "$INSTALL_DIR/ssh_host_ed25519_key.pub"

cat >/etc/sudoers.d/agent-git-gateway <<EOF
git ALL=($REAL_USER) NOPASSWD: $LIBEXEC_DIR/agent-git-gateway --allowlist $INSTALL_DIR/repos.conf --ssh-config $INSTALL_DIR/downstream_ssh_config --downstream-host github.com-agent-gateway
Defaults!$LIBEXEC_DIR/agent-git-gateway !requiretty
Defaults!$LIBEXEC_DIR/agent-git-gateway env_keep += "SSH_ORIGINAL_COMMAND"
EOF
chmod 0440 /etc/sudoers.d/agent-git-gateway

systemctl daemon-reload
systemctl enable --now agent-git-gateway-sshd.service

echo "Installed agent-git-gateway."
echo "Next steps:"
echo "  1. Generate an agent gateway key pair for agent-user."
echo "  2. Append the public key to $INSTALL_DIR/authorized_keys."
echo "  3. Review $INSTALL_DIR/repos.conf, $INSTALL_DIR/downstream_ssh_config, and $INSTALL_DIR/ssh_host_ed25519_key.pub."
echo "  4. Install the example nftables rule if you need direct SSH egress blocked for agent-user."

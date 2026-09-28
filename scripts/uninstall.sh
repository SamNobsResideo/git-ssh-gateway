#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "uninstall.sh must be run as root" >&2
    exit 1
fi

systemctl disable --now agent-git-gateway-sshd.service 2>/dev/null || true
rm -f /etc/systemd/system/agent-git-gateway-sshd.service
rm -f /etc/sudoers.d/agent-git-gateway
rm -f /usr/local/libexec/agent-git-gateway
rm -f /usr/local/libexec/agent-git-gateway-force-command
rm -rf /etc/agent-git-gateway
systemctl daemon-reload

echo "Uninstalled agent-git-gateway."
echo "The local system user 'git' was left in place intentionally."

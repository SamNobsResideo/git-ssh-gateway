#!/bin/sh
set -eu

: "${ALLOWED_REPO:=company/allowed-repo}"
: "${DENIED_REPO:=company/not-allowed}"
: "${REAL_USER_HOME:=/home/real-user}"
: "${GITHUB_KEY_PATH:=/home/real-user/.ssh/id_ed25519_github}"
: "${AGENT_USER:=agent-user}"

if [ "$(id -un)" != "$AGENT_USER" ]; then
    echo "manual-security-checks.sh must be run as $AGENT_USER" >&2
    exit 1
fi

echo "== Allowed operations =="
git ls-remote "git@github.com:${ALLOWED_REPO}.git"
git -c protocol.version=2 ls-remote "git@github.com:${ALLOWED_REPO}.git"

echo "== Denied operations =="
if ssh git@github.com; then
    echo "unexpected success: interactive ssh" >&2
    exit 1
fi
if ssh git@github.com "git-receive-pack '${ALLOWED_REPO}.git'"; then
    echo "unexpected success: git-receive-pack" >&2
    exit 1
fi
if ssh git@github.com "git-upload-archive '${ALLOWED_REPO}.git'"; then
    echo "unexpected success: git-upload-archive" >&2
    exit 1
fi
if ssh git@github.com "uname -a"; then
    echo "unexpected success: arbitrary command" >&2
    exit 1
fi
if git ls-remote "git@github.com:${DENIED_REPO}.git"; then
    echo "unexpected success: non-allowlisted repository" >&2
    exit 1
fi

echo "== Parser bypass probes =="
for probe in \
    "git-upload-pack './${ALLOWED_REPO}.git'" \
    "git-upload-pack '/${ALLOWED_REPO}.git'" \
    "git-upload-pack '${ALLOWED_REPO%%/*}//${ALLOWED_REPO#*/}.git'"
do
    if ! ssh git@github.com "$probe"; then
        echo "unexpected failure for normalized path: $probe" >&2
        exit 1
    fi
done
for probe in \
    "git-upload-pack '../${ALLOWED_REPO}.git'" \
    "git-upload-pack '${ALLOWED_REPO}.git' '--advertise-refs'" \
    "git-upload-pack '${ALLOWED_REPO}.git;uname -a'" \
    "git-upload-pack ''\"${ALLOWED_REPO}.git\"''"
do
    if ssh git@github.com "$probe"; then
        echo "unexpected success: $probe" >&2
        exit 1
    fi
done

echo "== Credential isolation =="
if cat "$GITHUB_KEY_PATH"; then
    echo "unexpected success: private key readable" >&2
    exit 1
fi
readable_files="$(find "$REAL_USER_HOME/.ssh" -type f -readable -print 2>/dev/null || true)"
if [ -n "$readable_files" ]; then
    echo "unexpected success: readable files in real-user .ssh" >&2
    exit 1
fi

echo "== Network isolation =="
if python3 - <<'PY'
import socket

socket.create_connection(("github.com", 22), timeout=5)
PY
then
    echo "unexpected success: direct SSH egress" >&2
    exit 1
fi

echo "Manual security checks completed."

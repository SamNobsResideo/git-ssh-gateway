#!/bin/sh
set -eu

: "${ALLOWED_REPO:=company/allowed-repo}"
: "${DENIED_REPO:=company/not-allowed}"
: "${REAL_USER_HOME:=/home/real-user}"
: "${GITHUB_KEY_PATH:=/home/real-user/.ssh/id_ed25519_github}"

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
if find "$REAL_USER_HOME/.ssh" -type f -readable -print | grep -q .; then
    echo "unexpected success: readable files in real-user .ssh" >&2
    exit 1
fi

echo "== Network isolation =="
if ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no git@140.82.112.3 true; then
    echo "unexpected success: direct SSH egress" >&2
    exit 1
fi

echo "Manual security checks completed."

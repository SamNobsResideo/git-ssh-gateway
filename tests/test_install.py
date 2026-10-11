import os
from pathlib import Path
import shlex
import stat
import subprocess
import tempfile
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class InstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.installer = (REPOSITORY_ROOT / "scripts/install.sh").read_text(encoding="utf-8")

    def creation_block(self, filename: str) -> str:
        start = self.installer.index(f'if [ ! -f "$INSTALL_DIR/{filename}" ]; then')
        end = self.installer.index("\nfi", start) + len("\nfi")
        return self.installer[start:end]

    def test_configuration_directory_is_root_owned_and_traversable(self) -> None:
        self.assertIn(
            'install -d -m 0755 -o root -g root "$INSTALL_DIR"',
            self.installer.splitlines(),
        )

    def test_authorized_keys_preserved_and_readable_on_first_install_and_upgrade(self) -> None:
        chmod = next(
            line for line in self.installer.splitlines()
            if line.startswith("chmod ") and '"$INSTALL_DIR/authorized_keys"' in line
        )
        self.assertEqual(chmod, 'chmod 0644 "$INSTALL_DIR/authorized_keys"')
        self.assertIn(
            'chown root:root "$INSTALL_DIR/authorized_keys" "$INSTALL_DIR/sshd_config"',
            self.installer,
        )
        for existing in (False, True):
            with self.subTest(existing=existing), tempfile.TemporaryDirectory() as directory:
                authorized_keys = Path(directory) / "authorized_keys"
                content = "# existing public authorization entries\n"
                if existing:
                    authorized_keys.write_text(content, encoding="utf-8")
                    authorized_keys.chmod(0o600)
                subprocess.run(
                    ["/bin/sh", "-ec", self.creation_block("authorized_keys") + "\n" + chmod],
                    env=dict(os.environ, INSTALL_DIR=directory),
                    check=True,
                )
                self.assertEqual(authorized_keys.read_text(), content if existing else "")
                self.assertEqual(stat.S_IMODE(authorized_keys.stat().st_mode), 0o644)

    def test_repository_policy_is_created_once_and_preserved_on_upgrade(self) -> None:
        for existing in (False, True):
            with self.subTest(existing=existing), tempfile.TemporaryDirectory() as directory:
                rules = Path(directory) / "repos.conf"
                content = "allow company/*\ndeny company/private-*\n"
                if existing:
                    rules.write_text(content, encoding="utf-8")
                subprocess.run(
                    ["/bin/sh", "-ec", self.creation_block("repos.conf")],
                    env=dict(os.environ, INSTALL_DIR=directory, REPO_ROOT=str(REPOSITORY_ROOT)),
                    check=True,
                )
                expected = content if existing else (
                    REPOSITORY_ROOT / "config/repos.conf.example"
                ).read_text(encoding="utf-8")
                self.assertEqual(rules.read_text(encoding="utf-8"), expected)

    def test_both_account_paths_use_shell_that_executes_forced_commands(self) -> None:
        account_commands = [
            shlex.split(line) for line in self.installer.splitlines()
            if line.strip().startswith(("useradd ", "usermod "))
        ]
        self.assertEqual(len(account_commands), 2)
        for command in account_commands:
            with self.subTest(command=command[0]):
                shell = command[command.index("--shell") + 1]
                self.assertEqual(shell, "/bin/sh")
                completed = subprocess.run(
                    [shell, "-c", "printf '0000'"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
                )
                self.assertEqual(completed.stdout, b"0000")
                self.assertEqual(completed.stderr, b"")

    def test_daemon_still_forces_gateway_and_disables_interactive_access(self) -> None:
        config = (REPOSITORY_ROOT / "config/sshd_config.gateway.example").read_text(encoding="utf-8")
        for directive in (
            "AllowUsers git",
            "ListenAddress 127.0.0.1",
            "PasswordAuthentication no",
            "KbdInteractiveAuthentication no",
            "PermitTTY no",
            "DisableForwarding yes",
            "AuthorizedKeysFile /etc/agent-git-gateway/authorized_keys",
        ):
            self.assertIn(directive, config.splitlines())
        forced_command = next(line for line in config.splitlines() if line.startswith("ForceCommand "))
        self.assertIn("/usr/local/libexec/agent-git-gateway-force-command", forced_command)
        self.assertIn("/usr/bin/sudo -n -u @REAL_USER@", forced_command)

    def test_private_configuration_modes_remain_restricted(self) -> None:
        self.assertIn('install -m 0600 /dev/null "$INSTALL_DIR/sshd_config"', self.installer)
        self.assertIn('install -m 0640 /dev/null "$INSTALL_DIR/downstream_ssh_config"', self.installer)
        self.assertIn('chmod 0600 "$INSTALL_DIR/ssh_host_ed25519_key"', self.installer)

    def test_reinstall_restarts_daemon_after_configuration_is_written(self) -> None:
        restart = self.installer.index("systemctl restart agent-git-gateway-sshd.service")
        self.assertGreater(restart, self.installer.index("sshd_config.write_text("))
        self.assertGreater(restart, self.installer.index("systemctl daemon-reload"))


if __name__ == "__main__":
    unittest.main()

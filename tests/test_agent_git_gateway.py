import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import agent_git_gateway


class NormalizeRepositoryTests(unittest.TestCase):
    def test_normalizes_expected_prefixes_and_suffixes(self) -> None:
        self.assertEqual(
            agent_git_gateway.normalize_repository("/./company//allowed-repo.git"),
            "company/allowed-repo",
        )

    def test_rejects_parent_traversal(self) -> None:
        with self.assertRaises(agent_git_gateway.GatewayError) as context:
            agent_git_gateway.normalize_repository("company/../secret")
        self.assertEqual(context.exception.reason, "invalid-repository")

    def test_rejects_whitespace_and_metacharacters(self) -> None:
        for repository in ("company/repo extra", "company/$(repo)", "company/repo;uname"):
            with self.subTest(repository=repository):
                with self.assertRaises(agent_git_gateway.GatewayError):
                    agent_git_gateway.normalize_repository(repository)


class ParseOriginalCommandTests(unittest.TestCase):
    def test_accepts_upload_pack(self) -> None:
        operation, repository = agent_git_gateway.parse_original_command(
            "git-upload-pack 'company/allowed-repo.git'"
        )
        self.assertEqual(operation, "git-upload-pack")
        self.assertEqual(repository, "company/allowed-repo")

    def test_rejects_extra_arguments(self) -> None:
        with self.assertRaises(agent_git_gateway.GatewayError) as context:
            agent_git_gateway.parse_original_command(
                "git-upload-pack 'company/allowed-repo.git' '--advertise-refs'"
            )
        self.assertEqual(context.exception.reason, "invalid-command")

    def test_rejects_non_upload_pack(self) -> None:
        with self.assertRaises(agent_git_gateway.GatewayError) as context:
            agent_git_gateway.parse_original_command(
                "git-receive-pack 'company/allowed-repo.git'"
            )
        self.assertEqual(context.exception.reason, "operation-not-allowed")


class RunGatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.allowlist = self.root / "repos.conf"
        self.allowlist.write_text("company/allowed-repo\n", encoding="utf-8")
        self.ssh_config = self.root / "downstream_ssh_config"
        self.ssh_config.write_text("Host github.com-agent-gateway\n", encoding="utf-8")

    def test_preserves_git_protocol_and_builds_safe_command(self) -> None:
        calls = []

        def fake_runner(command, env, check):
            calls.append((command, env, check))
            return SimpleNamespace(returncode=0)

        return_code = agent_git_gateway.run_gateway(
            original_command="git-upload-pack 'company/allowed-repo.git'",
            allowlist_path=self.allowlist,
            ssh_config=self.ssh_config,
            downstream_host="github.com-agent-gateway",
            ssh_binary="/usr/bin/ssh",
            environment={
                "GIT_PROTOCOL": "version=2",
                "PATH": "/usr/bin:/bin",
                "HOME": "/home/agent-user",
            },
            user="agent-user",
            runner=fake_runner,
        )

        self.assertEqual(return_code, 0)
        self.assertEqual(len(calls), 1)
        command, env, check = calls[0]
        self.assertEqual(check, False)
        self.assertEqual(env["HOME"], "/home/agent-user")
        self.assertEqual(env["GIT_PROTOCOL"], "version=2")
        self.assertIn("SendEnv=GIT_PROTOCOL", command)
        self.assertEqual(command[-1], "git-upload-pack company/allowed-repo.git")

    def test_denies_non_allowlisted_repository(self) -> None:
        with self.assertRaises(agent_git_gateway.GatewayError) as context:
            agent_git_gateway.run_gateway(
                original_command="git-upload-pack 'company/not-allowed.git'",
                allowlist_path=self.allowlist,
                ssh_config=self.ssh_config,
                downstream_host="github.com-agent-gateway",
            )
        self.assertEqual(context.exception.reason, "repository-not-allowed")

    def test_rejects_invalid_allowlist_entry(self) -> None:
        self.allowlist.write_text("company/repo extra\n", encoding="utf-8")
        with self.assertRaises(agent_git_gateway.GatewayError) as context:
            agent_git_gateway.load_allowlist(self.allowlist)
        self.assertEqual(context.exception.reason, "invalid-allowlist")


if __name__ == "__main__":
    unittest.main()

import tempfile
import unittest
import pwd
import os
import subprocess
import sys
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

    def test_accepts_single_scp_style_separator(self) -> None:
        self.assertEqual(agent_git_gateway.normalize_repository("company:allowed-repo.git"), "company/allowed-repo")


class NormalizeRepositoryPatternTests(unittest.TestCase):
    def test_normalizes_wildcard_pattern(self) -> None:
        self.assertEqual(
            agent_git_gateway.normalize_repository_pattern("/./company//*-repo.git"),
            "company/*-repo",
        )

    def test_rejects_invalid_pattern_characters(self) -> None:
        with self.assertRaises(agent_git_gateway.GatewayError):
            agent_git_gateway.normalize_repository_pattern("company/repo[12]")


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

    def test_accepts_lfs_download_and_normalizes_repository(self) -> None:
        self.assertEqual(
            agent_git_gateway.parse_original_command(
                "git-lfs-authenticate '/./company//allowed-repo.git' download"
            ),
            ("git-lfs-authenticate", "company/allowed-repo"),
        )

    def test_accepts_observed_client_command(self) -> None:
        self.assertEqual(
            agent_git_gateway.parse_original_command(
                "git-lfs-authenticate oclea/rvp-cooper-sdk.git download"
            ),
            ("git-lfs-authenticate", "oclea/rvp-cooper-sdk"),
        )

    def test_rejects_lfs_upload_and_unknown_actions(self) -> None:
        for action in ("upload", "Download", "unknown", "'download;uname'"):
            with self.subTest(action=action):
                with self.assertRaises(agent_git_gateway.GatewayError) as context:
                    agent_git_gateway.parse_original_command(
                        f"git-lfs-authenticate company/allowed-repo.git {action}"
                    )
                self.assertEqual(context.exception.reason, "operation-not-allowed")

    def test_rejects_malformed_lfs_commands(self) -> None:
        for command in (
            "git-lfs-authenticate",
            "git-lfs-authenticate company/allowed-repo.git",
            "git-lfs-authenticate company/allowed-repo.git download extra",
            "git-lfs-authenticate company/allowed-repo.git download; uname",
            "git-lfs-authenticate 'company/allowed-repo.git download",
        ):
            with self.subTest(command=command):
                with self.assertRaises(agent_git_gateway.GatewayError) as context:
                    agent_git_gateway.parse_original_command(command)
                self.assertEqual(context.exception.reason, "invalid-command")

    def test_rejects_unsafe_lfs_repositories(self) -> None:
        for repository in ("company/../secret", "company/repo;uname", "company/$(uname)", "company/repo extra"):
            with self.subTest(repository=repository):
                with self.assertRaises(agent_git_gateway.GatewayError) as context:
                    agent_git_gateway.parse_original_command(
                        f"git-lfs-authenticate '{repository}' download"
                    )
                self.assertEqual(context.exception.reason, "invalid-repository")

    def test_rejects_pure_ssh_lfs_transfer(self) -> None:
        with self.assertRaises(agent_git_gateway.GatewayError):
            agent_git_gateway.parse_original_command("git-lfs-transfer company/allowed-repo.git download")


class BuildRemoteCommandTests(unittest.TestCase):
    def test_default_operation_is_unchanged(self) -> None:
        self.assertEqual(
            agent_git_gateway.build_remote_command("company/allowed-repo"),
            "git-upload-pack company/allowed-repo.git",
        )

    def test_lfs_download_command(self) -> None:
        self.assertEqual(
            agent_git_gateway.build_remote_command("/./company//allowed-repo.git", "git-lfs-authenticate"),
            "git-lfs-authenticate company/allowed-repo.git download",
        )

    def test_rejects_unsupported_downstream_operations_and_unsafe_paths(self) -> None:
        with self.assertRaises(agent_git_gateway.GatewayError):
            agent_git_gateway.build_remote_command("company/repo", "git-receive-pack")
        with self.assertRaises(agent_git_gateway.GatewayError):
            agent_git_gateway.build_remote_command("company/repo;uname", "git-lfs-authenticate")


class RunGatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.allowlist = self.root / "repos.conf"
        self.allowlist.write_text("allow company/allowed-repo\n", encoding="utf-8")
        self.ssh_config = self.root / "downstream_ssh_config"
        self.ssh_config.write_text("Host github.com-agent-gateway\n", encoding="utf-8")

    def test_lfs_download_builds_command_and_propagates_exit_status(self) -> None:
        calls = []

        def fake_runner(command, env, check):
            calls.append((command, env, check))
            return SimpleNamespace(returncode=17)

        with self.assertLogs("agent-git-gateway", level="INFO") as logs:
            return_code = agent_git_gateway.run_gateway(
                original_command="git-lfs-authenticate '/./company//allowed-repo.git' download",
                allowlist_path=self.allowlist,
                ssh_config=self.ssh_config,
                downstream_host="github.com-agent-gateway",
                runner=fake_runner,
            )

        self.assertEqual(return_code, 17)
        self.assertEqual(len(calls), 1)
        command, env, check = calls[0]
        self.assertEqual(command[-2:], [
            "github.com-agent-gateway",
            "git-lfs-authenticate company/allowed-repo.git download",
        ])
        self.assertEqual(command[:3], ["/usr/bin/ssh", "-F", str(self.ssh_config)])
        self.assertIn("ClearAllForwardings=yes", command)
        self.assertFalse(check)
        self.assertIn("operation=git-lfs-authenticate repo=company/allowed-repo", logs.output[0])

    def test_denied_lfs_requests_never_start_downstream(self) -> None:
        from unittest.mock import Mock

        self.allowlist.write_text("allow company/*\ndeny company/private-*\n", encoding="utf-8")
        for command, reason in (
            ("git-lfs-authenticate other/repo.git download", "repository-not-allowed"),
            ("git-lfs-authenticate company/private-repo.git download", "repository-not-allowed"),
            ("git-lfs-authenticate company/allowed-repo.git upload", "operation-not-allowed"),
            ("git-lfs-transfer company/allowed-repo.git download", "invalid-command"),
        ):
            with self.subTest(command=command):
                runner = Mock()
                with self.assertRaises(agent_git_gateway.GatewayError) as context:
                    agent_git_gateway.run_gateway(
                        original_command=command,
                        allowlist_path=self.allowlist,
                        ssh_config=self.ssh_config,
                        downstream_host="github.com-agent-gateway",
                        runner=runner,
                    )
                self.assertEqual(context.exception.reason, reason)
                runner.assert_not_called()

    def test_lfs_authentication_json_is_relayed_without_logging_it(self) -> None:
        payload = b'{"href":"https://example.invalid/lfs","header":{"Authorization":"test-only-placeholder"},"expires_in":60}\n'
        child_code = (
            "import os; "
            f"os.write(1, {payload!r}); "
            "os.write(2, b'downstream diagnostic\\n')"
        )
        gateway_code = (
            "import sys; from unittest.mock import patch; import agent_git_gateway; "
            f"command = [sys.executable, '-c', {child_code!r}]; "
            "override = patch.object(agent_git_gateway, 'build_downstream_command', return_value=command); "
            "override.start(); sys.exit(agent_git_gateway.main())"
        )
        env = dict(os.environ, SSH_ORIGINAL_COMMAND="git-lfs-authenticate company/allowed-repo.git download")
        completed = subprocess.run(
            [sys.executable, "-c", gateway_code, "--allowlist", str(self.allowlist),
             "--ssh-config", str(self.ssh_config)],
            cwd=Path(agent_git_gateway.__file__).resolve().parent,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, payload)
        self.assertIn(b"downstream diagnostic", completed.stderr)
        self.assertIn(b"operation=git-lfs-authenticate", completed.stderr)
        self.assertNotIn(b"Authorization", completed.stderr)
        self.assertNotIn(b"test-only-placeholder", completed.stderr)

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
            request_user="agent-user",
            runner=fake_runner,
        )

        self.assertEqual(return_code, 0)
        self.assertEqual(len(calls), 1)
        command, env, check = calls[0]
        self.assertEqual(check, False)
        self.assertEqual(env["HOME"], pwd.getpwuid(os.geteuid()).pw_dir)
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
            agent_git_gateway.load_repository_rules(self.allowlist)
        self.assertEqual(context.exception.reason, "invalid-rules")

    def test_omits_git_protocol_when_not_requested(self) -> None:
        calls = []

        def fake_runner(command, env, check):
            calls.append((command, env, check))
            return SimpleNamespace(returncode=0)

        return_code = agent_git_gateway.run_gateway(
            original_command="git-upload-pack 'company/allowed-repo.git'",
            allowlist_path=self.allowlist,
            ssh_config=self.ssh_config,
            downstream_host="github.com-agent-gateway",
            environment={"PATH": "/usr/bin:/bin", "HOME": "/home/agent-user"},
            request_user="agent-user",
            runner=fake_runner,
        )

        self.assertEqual(return_code, 0)
        self.assertNotIn("GIT_PROTOCOL", calls[0][1])
        self.assertEqual(calls[0][1]["HOME"], pwd.getpwuid(os.geteuid()).pw_dir)

    def test_uses_explicit_run_as_user_for_home(self) -> None:
        calls = []
        run_as_user = pwd.getpwuid(os.geteuid()).pw_name

        def fake_runner(command, env, check):
            calls.append((command, env, check))
            return SimpleNamespace(returncode=0)

        return_code = agent_git_gateway.run_gateway(
            original_command="git-upload-pack 'company/allowed-repo.git'",
            allowlist_path=self.allowlist,
            ssh_config=self.ssh_config,
            downstream_host="github.com-agent-gateway",
            environment={"PATH": "/usr/bin:/bin"},
            request_user="agent-user",
            run_as_user=run_as_user,
            runner=fake_runner,
        )

        self.assertEqual(return_code, 0)
        self.assertEqual(calls[0][1]["HOME"], pwd.getpwnam(run_as_user).pw_dir)

    def test_wildcard_allow_rule_matches(self) -> None:
        self.allowlist.write_text("allow company/*\n", encoding="utf-8")

        return_code = agent_git_gateway.run_gateway(
            original_command="git-upload-pack 'company/public-repo.git'",
            allowlist_path=self.allowlist,
            ssh_config=self.ssh_config,
            downstream_host="github.com-agent-gateway",
            runner=lambda *args, **kwargs: SimpleNamespace(returncode=0),
        )

        self.assertEqual(return_code, 0)

    def test_deny_rule_overrides_allow_rule(self) -> None:
        self.allowlist.write_text("allow */*\ndeny company/private-*\n", encoding="utf-8")

        with self.assertRaises(agent_git_gateway.GatewayError) as context:
            agent_git_gateway.run_gateway(
                original_command="git-upload-pack 'company/private-repo.git'",
                allowlist_path=self.allowlist,
                ssh_config=self.ssh_config,
                downstream_host="github.com-agent-gateway",
            )
        self.assertEqual(context.exception.reason, "repository-not-allowed")

    def test_deny_rule_overrides_allow_regardless_of_order(self) -> None:
        self.allowlist.write_text("deny */*\nallow company/allowed-repo\n", encoding="utf-8")

        with self.assertRaises(agent_git_gateway.GatewayError) as context:
            agent_git_gateway.run_gateway(
                original_command="git-upload-pack 'company/allowed-repo.git'",
                allowlist_path=self.allowlist,
                ssh_config=self.ssh_config,
                downstream_host="github.com-agent-gateway",
            )
        self.assertEqual(context.exception.reason, "repository-not-allowed")

    def test_later_deny_overrides_earlier_specific_allow(self) -> None:
        self.allowlist.write_text("allow company/allowed-repo\ndeny company/allowed-repo\n", encoding="utf-8")

        with self.assertRaises(agent_git_gateway.GatewayError) as context:
            agent_git_gateway.run_gateway(
                original_command="git-upload-pack 'company/allowed-repo.git'",
                allowlist_path=self.allowlist,
                ssh_config=self.ssh_config,
                downstream_host="github.com-agent-gateway",
            )
        self.assertEqual(context.exception.reason, "repository-not-allowed")

    def test_bare_rule_is_treated_as_allow(self) -> None:
        rules = agent_git_gateway.load_repository_rules(self.allowlist)
        self.assertEqual(rules, [agent_git_gateway.RepositoryRule(action="allow", pattern="company/allowed-repo")])

    def test_bare_rule_with_trailing_whitespace_and_comment_is_treated_as_allow(self) -> None:
        self.allowlist.write_text("company/allowed-repo\t # comment\n", encoding="utf-8")
        rules = agent_git_gateway.load_repository_rules(self.allowlist)
        self.assertEqual(rules, [agent_git_gateway.RepositoryRule(action="allow", pattern="company/allowed-repo")])


class RequestUserTests(unittest.TestCase):
    def test_prefers_explicit_original_user_environment(self) -> None:
        self.assertEqual(
            agent_git_gateway.get_request_user(
                {
                    "SSH_GATEWAY_ORIGINAL_USER": "agent-user",
                    "SUDO_USER": "git",
                    "USER": "real-user",
                }
            ),
            "agent-user",
        )


class RepositoryRuleTests(unittest.TestCase):
    def test_allow_star_star_can_match_public_repo(self) -> None:
        parsed = [
            agent_git_gateway.RepositoryRule(action="allow", pattern="*/*"),
            agent_git_gateway.RepositoryRule(action="deny", pattern="company/blocked-*"),
        ]
        self.assertTrue(agent_git_gateway.repository_is_allowed("octocat/hello-world", parsed))
        self.assertFalse(agent_git_gateway.repository_is_allowed("company/blocked-repo", parsed))

    def test_wildcard_does_not_cross_path_separator(self) -> None:
        self.assertFalse(agent_git_gateway.pattern_matches_repository("company/*", "company/foo/bar"))

    def test_unknown_rule_action_is_rejected(self) -> None:
        with self.assertRaises(agent_git_gateway.GatewayError) as context:
            agent_git_gateway.parse_rule_line("block company/repo")
        self.assertEqual(context.exception.reason, "invalid-rule")

    def test_explicit_rule_with_extra_tokens_is_rejected(self) -> None:
        with self.assertRaises(agent_git_gateway.GatewayError) as context:
            agent_git_gateway.parse_rule_line("allow company/repo extra")
        self.assertEqual(context.exception.reason, "invalid-rule")


if __name__ == "__main__":
    unittest.main()

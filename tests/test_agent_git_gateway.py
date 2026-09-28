import tempfile
import unittest
import pwd
import os
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


class RunGatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.allowlist = self.root / "repos.conf"
        self.allowlist.write_text("allow company/allowed-repo\n", encoding="utf-8")
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

    def test_bare_rule_is_treated_as_allow(self) -> None:
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

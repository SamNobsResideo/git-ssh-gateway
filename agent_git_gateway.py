#!/usr/bin/env python3

from __future__ import annotations

import argparse
import dataclasses
import getpass
import logging
import os
import pwd
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Iterable


SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")
SAFE_PATTERN_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._*-]+$")


class GatewayError(Exception):
    def __init__(
        self,
        reason: str,
        message: str,
        exit_code: int = 128,
        *,
        operation: str | None = None,
        repository: str | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.exit_code = exit_code
        self.operation = operation
        self.repository = repository


@dataclasses.dataclass(frozen=True)
class RepositoryRule:
    action: str
    pattern: str


def configure_logging() -> logging.Logger:
    logger = logging.getLogger("agent-git-gateway")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logger.addHandler(handler)
    logger.propagate = False
    return logger


def get_request_user(environment: dict[str, str] | None = None) -> str:
    environment = environment or os.environ
    return (
        environment.get("SSH_GATEWAY_ORIGINAL_USER")
        or environment.get("SUDO_USER")
        or environment.get("LOGNAME")
        or environment.get("USER")
        or getpass.getuser()
    )


def normalize_repository(raw_repository: str) -> str:
    repository = raw_repository.strip()
    if not repository:
        raise GatewayError("missing-repository", "missing repository path")
    if "\x00" in repository:
        raise GatewayError("invalid-repository", "NUL byte in repository path")
    if any(character.isspace() for character in repository):
        raise GatewayError("invalid-repository", "whitespace not allowed in repository path")
    if repository.startswith(":"):
        repository = repository[1:]
    elif repository.count(":") == 1 and "/" not in repository:
        repository = repository.replace(":", "/", 1)
    if "\\" in repository or ":" in repository:
        raise GatewayError("invalid-repository", "unsupported repository path separator")

    segments = []
    for segment in repository.lstrip("/").split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            raise GatewayError("invalid-repository", "parent directory traversal is not allowed")
        if not SAFE_SEGMENT_RE.fullmatch(segment):
            raise GatewayError("invalid-repository", "unsupported repository path characters")
        segments.append(segment)

    if len(segments) != 2:
        raise GatewayError("invalid-repository", "repository path must be owner/repository")

    owner, repo = segments
    if repo.endswith(".git"):
        repo = repo[:-4]
    if not repo:
        raise GatewayError("invalid-repository", "repository name is empty")
    if not SAFE_SEGMENT_RE.fullmatch(repo):
        raise GatewayError("invalid-repository", "unsupported repository path characters")
    return f"{owner}/{repo}"


def normalize_repository_pattern(raw_pattern: str) -> str:
    pattern = raw_pattern.strip()
    if not pattern:
        raise GatewayError("missing-rule-pattern", "missing repository rule pattern")
    if "\x00" in pattern:
        raise GatewayError("invalid-rule-pattern", "NUL byte in repository rule pattern")
    if any(character.isspace() for character in pattern):
        raise GatewayError("invalid-rule-pattern", "whitespace not allowed in repository rule pattern")
    if "\\" in pattern or ":" in pattern:
        raise GatewayError("invalid-rule-pattern", "unsupported repository rule path separator")

    segments = []
    for segment in pattern.lstrip("/").split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            raise GatewayError("invalid-rule-pattern", "parent directory traversal is not allowed")
        if not SAFE_PATTERN_SEGMENT_RE.fullmatch(segment):
            raise GatewayError("invalid-rule-pattern", "unsupported repository rule pattern characters")
        segments.append(segment)

    if len(segments) != 2:
        raise GatewayError("invalid-rule-pattern", "repository rule pattern must be owner/repository")

    owner, repo = segments
    if repo.endswith(".git"):
        repo = repo[:-4]
    if not repo:
        raise GatewayError("invalid-rule-pattern", "repository rule pattern is empty")
    if not SAFE_PATTERN_SEGMENT_RE.fullmatch(repo):
        raise GatewayError("invalid-rule-pattern", "unsupported repository rule pattern characters")
    return f"{owner}/{repo}"


def parse_original_command(original_command: str | None) -> tuple[str, str]:
    if not original_command:
        raise GatewayError("missing-command", "missing SSH_ORIGINAL_COMMAND")
    try:
        tokens = shlex.split(original_command, posix=True)
    except ValueError as error:
        raise GatewayError("invalid-command", f"unable to parse SSH_ORIGINAL_COMMAND: {error}") from error

    if len(tokens) != 2:
        raise GatewayError("invalid-command", "command must contain exactly an operation and a repository")

    operation, repository = tokens
    if operation != "git-upload-pack":
        raise GatewayError(
            "operation-not-allowed",
            f"operation {operation!r} is not allowed",
            operation=operation,
        )
    return operation, normalize_repository(repository)


def parse_rule_line(line: str) -> RepositoryRule:
    action = "allow"
    pattern_text = line
    if " " in line or "\t" in line:
        tokens = line.split()
        if tokens[0] in {"allow", "deny"}:
            if len(tokens) != 2:
                raise GatewayError("invalid-rule", f"repository rule {tokens[0]!r} must contain exactly one pattern")
            action, pattern_text = tokens
        elif len(tokens) > 1:
            raise GatewayError("invalid-rule", f"unknown repository rule action {tokens[0]!r}")
        else:
            pattern_text = tokens[0]
    elif line in {"allow", "deny"}:
        raise GatewayError("invalid-rule", f"repository rule {line!r} is missing a pattern")
    return RepositoryRule(action=action, pattern=normalize_repository_pattern(pattern_text))


def load_repository_rules(path: str | os.PathLike[str]) -> list[RepositoryRule]:
    rules: list[RepositoryRule] = []
    with open(path, "r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.split("#", 1)[0].strip()
            if not line:
                continue
            try:
                rules.append(parse_rule_line(line))
            except GatewayError as error:
                raise GatewayError(
                    "invalid-rules",
                    f"invalid repository rule on line {line_number}: {error.message}",
                ) from error
    return rules


def pattern_matches_repository(pattern: str, repository: str) -> bool:
    regex = "^" + re.escape(pattern).replace(r"\*", "[^/]*") + "$"
    return re.fullmatch(regex, repository) is not None


def repository_is_allowed(repository: str, rules: list[RepositoryRule]) -> bool:
    allow_matched = False
    deny_matched = False
    for rule in rules:
        if not pattern_matches_repository(rule.pattern, repository):
            continue
        if rule.action == "deny":
            deny_matched = True
        else:
            allow_matched = True
    return allow_matched and not deny_matched


def build_remote_command(repository: str) -> str:
    return f"git-upload-pack {shlex.quote(repository + '.git')}"


def build_downstream_command(
    ssh_binary: str,
    ssh_config: str | os.PathLike[str],
    downstream_host: str,
    repository: str,
) -> list[str]:
    return [
        ssh_binary,
        "-F",
        str(ssh_config),
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "ClearAllForwardings=yes",
        "-o",
        "SendEnv=GIT_PROTOCOL",
        downstream_host,
        build_remote_command(repository),
    ]


def run_gateway(
    *,
    original_command: str | None,
    allowlist_path: str | os.PathLike[str],
    ssh_config: str | os.PathLike[str],
    downstream_host: str,
    ssh_binary: str = "/usr/bin/ssh",
    environment: dict[str, str] | None = None,
    request_user: str | None = None,
    run_as_user: str | None = None,
    runner=subprocess.run,
    logger: logging.Logger | None = None,
) -> int:
    logger = logger or configure_logging()
    environment = dict(environment or os.environ)
    request_user = request_user or get_request_user(environment)
    run_as_user = run_as_user or pwd.getpwuid(os.geteuid()).pw_name

    operation, repository = parse_original_command(original_command)
    rules = load_repository_rules(allowlist_path)
    if not repository_is_allowed(repository, rules):
        raise GatewayError(
            "repository-not-allowed",
            f"repository {repository!r} is not allowed",
            operation=operation,
            repository=repository,
        )

    downstream_environment = {"PATH": environment.get("PATH", "/usr/bin:/bin")}
    downstream_environment["HOME"] = pwd.getpwnam(run_as_user).pw_dir
    if "GIT_PROTOCOL" in environment:
        downstream_environment["GIT_PROTOCOL"] = environment["GIT_PROTOCOL"]

    command = build_downstream_command(ssh_binary, ssh_config, downstream_host, repository)
    completed = runner(command, env=downstream_environment, check=False)
    return_code = completed.returncode if hasattr(completed, "returncode") else int(completed)
    logger.info(
        "ALLOW user=%s operation=%s repo=%s downstream_exit=%s",
        request_user,
        operation,
        repository,
        return_code,
    )
    return return_code


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only GitHub SSH gateway")
    parser.add_argument("--allowlist", required=True, help="Path to the repository allowlist")
    parser.add_argument("--ssh-config", required=True, help="Path to the downstream SSH config")
    parser.add_argument(
        "--downstream-host",
        default="github.com-agent-gateway",
        help="SSH host alias used for the downstream GitHub connection",
    )
    parser.add_argument(
        "--run-as-user",
        help="User account whose home directory should be used for downstream SSH",
    )
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    logger = configure_logging()
    parser = build_argument_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        return run_gateway(
            original_command=os.environ.get("SSH_ORIGINAL_COMMAND"),
            allowlist_path=args.allowlist,
            ssh_config=args.ssh_config,
            downstream_host=args.downstream_host,
            run_as_user=args.run_as_user,
            logger=logger,
        )
    except GatewayError as error:
        user = get_request_user()
        logger.info(
            "DENY user=%s operation=%s repo=%s reason=%s message=%s",
            user,
            error.operation or "-",
            error.repository or "-",
            error.reason,
            error.message,
        )
        print(f"agent-git-gateway: {error.message}", file=sys.stderr)
        return error.exit_code
    except FileNotFoundError as error:
        user = get_request_user()
        logger.info("DENY user=%s reason=missing-file message=%s", user, error)
        print(f"agent-git-gateway: {error}", file=sys.stderr)
        return 127


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3

from __future__ import annotations

import argparse
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


def load_allowlist(path: str | os.PathLike[str]) -> set[str]:
    allowed_repositories: set[str] = set()
    with open(path, "r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.split("#", 1)[0].strip()
            if not line:
                continue
            try:
                allowed_repositories.add(normalize_repository(line))
            except GatewayError as error:
                raise GatewayError(
                    "invalid-allowlist",
                    f"invalid allowlist entry on line {line_number}: {error.message}",
                ) from error
    return allowed_repositories


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
    allowed_repositories = load_allowlist(allowlist_path)
    if repository not in allowed_repositories:
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

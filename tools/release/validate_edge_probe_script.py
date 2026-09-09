#!/usr/bin/env python3
"""Enforce the exact least-privilege shape of every Edge probe container."""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path
import re
import shlex
import stat


REQUIRED_RUN_PATTERNS = {
    "read-only root filesystem": re.compile(r"(?:^|\s)--read-only(?:\s|$)"),
    "no-new-privileges": re.compile(
        r"(?:^|\s)--security-opt\s+no-new-privileges:true(?:\s|$)"
    ),
    "all capabilities dropped": re.compile(r"(?:^|\s)--cap-drop\s+ALL(?:\s|$)"),
    "PID limit": re.compile(r"(?:^|\s)--pids-limit\s+\S+"),
    "memory limit": re.compile(r"(?:^|\s)--memory\s+\S+"),
    "CPU limit": re.compile(r"(?:^|\s)--cpus\s+\S+"),
    "bounded temporary filesystem": re.compile(r"(?:^|\s)--tmpfs\s+\S+"),
}
DANGEROUS_PATTERNS = {
    "privileged container": re.compile(r"(?:^|\s)--privileged(?:[=\s]|$)"),
    "added capability": re.compile(r"(?:^|\s)--cap-add(?:[=\s]|$)"),
    "host device": re.compile(
        r"(?:^|\s)--(?:device|device-cgroup-rule)(?:[=\s]|$)"
    ),
    "host namespace": re.compile(
        r"(?:^|\s)--(?:pid|ipc|uts|userns)(?:=|\s+)host(?:\s|$)"
    ),
    "unconfined security profile": re.compile(
        r"(?:^|\s)--security-opt\s+(?:seccomp|apparmor)=unconfined(?:\s|$)"
    ),
    "unreviewed mount syntax": re.compile(
        r"(?:^|\s)(?:--mount|--volumes-from)(?:[=\s]|$)|"
        r"(?:^|\s)-v\S*|"
        r"(?:^|\s)--volume="
    ),
    "Docker socket": re.compile(r"docker\.sock", re.IGNORECASE),
    "firewall mutation": re.compile(
        r"(?<![A-Za-z0-9_-])(?:iptables|ip6tables|nft|firewall-cmd|ufw)(?![A-Za-z0-9_-])"
    ),
    "Docker network mutation": re.compile(
        r"\bdocker\s+network\s+(?:create|connect|rm)\b"
    ),
    "Docker network alias": re.compile(r"(?:^|\s)--network-alias(?:[=\s]|$)"),
    "Docker network option alias": re.compile(r"(?:^|\s)--net(?:[=\s]|$)"),
}
EXPECTED_CLIENT_STARTS = Counter(
    {
        '"$same_source_client"': 1,
        '"$global_client_one"': 1,
        '"$global_client_two"': 1,
        '"$global_client_three"': 1,
    }
)
EXPECTED_VOLUMES = (
    Counter({"$probe_directory/tls:/out": 1}),
    Counter(),
    Counter({"$script_dir/edge_body_limit_upstream.py:/probe.py:ro": 1}),
    Counter(
        {
            "$probe_directory/nginx.conf:/etc/nginx/nginx.conf:ro": 1,
            "$probe_directory/tls:/tls:ro": 1,
        }
    ),
    Counter(),
    Counter({"$probe_directory/$body_name:/probe-body:ro": 1}),
    Counter(
        {
            "$probe_directory/exact.bin:/probe-exact:ro": 1,
            "$probe_directory/near-miss.bin:/probe-small:ro": 1,
        }
    ),
)
EXPECTED_RUN_OPTIONS = (
    {
        "network": ["none"],
        "read-only": [""],
        "security-opt": ["no-new-privileges:true"],
        "cap-drop": ["ALL"],
        "pids-limit": ["32"],
        "memory": ["128m"],
        "cpus": ["1"],
        "tmpfs": ["/tmp:size=16m,mode=1777"],
        "rm": [""],
        "detach": [],
        "no-healthcheck": [],
    },
    {
        "network": ["host"],
        "read-only": [""],
        "security-opt": ["no-new-privileges:true"],
        "cap-drop": ["ALL"],
        "pids-limit": ["16"],
        "memory": ["64m"],
        "cpus": ["1"],
        "tmpfs": ["/tmp:size=8m,uid=10002,gid=10002,mode=0700"],
        "rm": [""],
        "detach": [],
        "no-healthcheck": [""],
    },
    {
        "network": ["host"],
        "read-only": [""],
        "security-opt": ["no-new-privileges:true"],
        "cap-drop": ["ALL"],
        "pids-limit": ["32"],
        "memory": ["128m"],
        "cpus": ["1"],
        "tmpfs": ["/tmp:size=16m,uid=10002,gid=10002,mode=0700"],
        "rm": [""],
        "detach": [""],
        "no-healthcheck": [""],
    },
    {
        "network": ["host"],
        "read-only": [""],
        "security-opt": ["no-new-privileges:true"],
        "cap-drop": ["ALL"],
        "pids-limit": ["64"],
        "memory": ["128m"],
        "cpus": ["1"],
        "tmpfs": ["/tmp:size=32m,uid=101,gid=101,mode=0700"],
        "rm": [""],
        "detach": [""],
        "no-healthcheck": [""],
    },
    {
        "network": ["host"],
        "read-only": [""],
        "security-opt": ["no-new-privileges:true"],
        "cap-drop": ["ALL"],
        "pids-limit": ["16"],
        "memory": ["64m"],
        "cpus": ["1"],
        "tmpfs": ["/tmp:size=8m,uid=101,gid=101,mode=0700"],
        "rm": [""],
        "detach": [],
        "no-healthcheck": [""],
    },
    {
        "network": ["host"],
        "read-only": [""],
        "security-opt": ["no-new-privileges:true"],
        "cap-drop": ["ALL"],
        "pids-limit": ["16"],
        "memory": ["64m"],
        "cpus": ["1"],
        "tmpfs": ["/tmp:size=8m,uid=101,gid=101,mode=0700"],
        "rm": [""],
        "detach": [],
        "no-healthcheck": [""],
    },
    {
        "network": ["host"],
        "read-only": [""],
        "security-opt": ["no-new-privileges:true"],
        "cap-drop": ["ALL"],
        "pids-limit": ["16"],
        "memory": ["64m"],
        "cpus": ["1"],
        "tmpfs": ["/tmp:size=8m,uid=101,gid=101,mode=0700"],
        "rm": [""],
        "detach": [""],
        "no-healthcheck": [""],
    },
)
BOOLEAN_OPTIONS = {"read-only", "rm", "detach", "no-healthcheck", "interactive"}
EXPECTED_INTERACTIVE = (False, True, False, False, False, False, False)


def option_values(command: str, option: str) -> Counter[str]:
    escaped = re.escape(option)
    if option in BOOLEAN_OPTIONS:
        matches = re.findall(
            rf"(?:^|\s)--{escaped}(?:=([^\s]+))?(?=\s|$)", command
        )
        return Counter("" if value == "" else f"={value}" for value in matches)
    values = re.findall(
        rf"(?:^|\s)--{escaped}(?:=|\s+)([^\s]+)", command
    )
    occurrences = len(
        re.findall(rf"(?:^|\s)--{escaped}(?=[=\s]|$)", command)
    )
    if occurrences != len(values):
        values.extend(["<malformed>"] * (occurrences - len(values)))
    return Counter(values)


def docker_run_commands(document: str) -> list[str]:
    logical_document = document.replace("\\\n", " ")
    commands = [
        line.strip()
        for line in logical_document.splitlines()
        if re.search(r"\bdocker\s+run\b", line)
    ]
    if len(commands) != 7:
        raise RuntimeError(f"Edge probe docker-run template set is not exact: {len(commands)}")
    return commands


def validate_loopback_curl_commands(document: str) -> None:
    logical_document = document.replace("\\\n", " ")
    commands = [
        line.strip()
        for line in logical_document.splitlines()
        if re.search(r"\bcurl\b", line)
    ]
    if len(commands) != 4:
        raise RuntimeError("Edge probe curl command set is not exact")
    for index, command in enumerate(commands, start=1):
        try:
            tokens = shlex.split(command, posix=True)
        except ValueError as error:
            raise RuntimeError(f"Edge probe curl command {index} is malformed") from error
        values: list[str] = []
        for position, token in enumerate(tokens):
            if token == "--noproxy":
                values.append(tokens[position + 1] if position + 1 < len(tokens) else "<missing>")
            elif token.startswith("--noproxy="):
                values.append(token.removeprefix("--noproxy="))
        if Counter(values) != Counter({"*": 1}):
            raise RuntimeError(
                f"Edge probe curl command {index} lacks exact loopback proxy bypass"
            )


def validate_script(document: str) -> None:
    if "\x00" in document:
        raise RuntimeError("Edge probe script contains a NUL byte")
    for label, pattern in DANGEROUS_PATTERNS.items():
        if pattern.search(document):
            raise RuntimeError(f"Edge probe script contains forbidden {label}")
    validate_loopback_curl_commands(document)

    commands = docker_run_commands(document)
    networks: Counter[str] = Counter()
    detached = 0
    for index, command in enumerate(commands, start=1):
        if re.search(r"(?:^|\s)-m\S*", command):
            raise RuntimeError(
                f"Edge probe docker run {index} uses forbidden memory option alias"
            )
        for label, pattern in REQUIRED_RUN_PATTERNS.items():
            if pattern.search(command) is None:
                raise RuntimeError(f"Edge probe docker run {index} lacks {label}")
        for option, expected_values in EXPECTED_RUN_OPTIONS[index - 1].items():
            actual_values = option_values(command, option)
            if actual_values != Counter(expected_values):
                raise RuntimeError(
                    f"Edge probe docker run {index} has non-exact --{option}: "
                    f"{actual_values}"
                )
        expected_interactive = Counter({"": 1}) if EXPECTED_INTERACTIVE[index - 1] else Counter()
        if option_values(command, "interactive") != expected_interactive:
            raise RuntimeError(
                f"Edge probe docker run {index} has non-exact --interactive"
            )
        command_networks = re.findall(r"(?:^|\s)--network\s+(\S+)", command)
        if len(command_networks) != 1 or command_networks[0] not in {"host", "none"}:
            raise RuntimeError(f"Edge probe docker run {index} has an unreviewed network")
        networks[command_networks[0]] += 1
        if re.search(r"(?:^|\s)--detach(?:\s|$)", command):
            detached += 1
            if re.search(r"(?:^|\s)--no-healthcheck(?:\s|$)", command) is None:
                raise RuntimeError(
                    f"long-lived Edge probe docker run {index} inherits a healthcheck"
                )
        volumes = Counter(
            value[1:-1] if value[:1] in {'"', "'"} and value[-1:] == value[:1] else value
            for value in re.findall(r"(?:^|\s)--volume\s+(\"[^\"]+\"|'[^']+'|\S+)", command)
        )
        if volumes != EXPECTED_VOLUMES[index - 1]:
            raise RuntimeError(f"Edge probe docker run {index} host-volume set is not exact")
    if networks != Counter({"host": 6, "none": 1}):
        raise RuntimeError(f"Edge probe network template set is not exact: {networks}")
    if detached != 3:
        raise RuntimeError("Edge probe long-lived container template set is not exact")

    client_starts = Counter(
        match.group(1)
        for match in re.finditer(
            r"^[ \t]*start_probe_client[ \t]+([^ \t\r\n]+)[ \t]*$",
            document,
            re.MULTILINE,
        )
    )
    if client_starts != EXPECTED_CLIENT_STARTS:
        raise RuntimeError("Edge probe long-lived client instance set is not exact")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--script", type=Path, required=True)
    args = parser.parse_args()
    metadata = args.script.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError("Edge probe script must be a regular non-symlink file")
    validate_script(args.script.read_text(encoding="utf-8"))
    print("Edge probe container policy: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

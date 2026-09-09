#!/usr/bin/env python3
"""Fail closed on every active listener/upstream in the Edge body probe."""

from __future__ import annotations

import argparse
from collections import Counter
import os
from pathlib import Path
import re
import stat


PRODUCTION_LISTENS = Counter(
    {
        ("8080", "default_server"): 1,
        ("8443", "ssl", "default_server"): 1,
    }
)
PROBE_LISTENS = Counter(
    {
        ("127.0.0.1:28080", "default_server"): 1,
        ("127.0.0.1:28443", "ssl", "default_server"): 1,
    }
)
PRODUCTION_UPSTREAM = Counter(
    {
        (
            "172.31.42.3:8080",
            "max_fails=2",
            "fail_timeout=10s",
        ): 1
    }
)
PROBE_UPSTREAM = Counter(
    {
        (
            "127.0.0.1:28081",
            "max_fails=2",
            "fail_timeout=10s",
        ): 1
    }
)
GATEWAY_UPSTREAM_BLOCK = ("upstream", "gateway_runtime")
PROBE_TOKEN_PATTERN = re.compile(r"[0-9a-f]{64}")
EXPECTED_INCLUDES = Counter(
    {
        (
            ("location", "=", "/v1/pairings/exchange"),
            ("/etc/nginx/includes/proxy-http.conf",),
        ): 1,
        (
            ("location", "=", "/v1/auth/refresh"),
            ("/etc/nginx/includes/proxy-http.conf",),
        ): 1,
        (
            ("location", "=", "/v1/events"),
            ("/etc/nginx/includes/proxy-websocket.conf",),
        ): 1,
        (
            ("location", "~", "^/v1/threads/[^/]+/turns$"),
            ("/etc/nginx/includes/proxy-http.conf",),
        ): 1,
        (
            (
                "location",
                "~",
                "^/v1/threads/[^/]+/turn-inputs/[^/]+/images/[^/]+$",
            ),
            ("/etc/nginx/includes/proxy-http.conf",),
        ): 1,
        (
            ("location", "~", "^/v1/approvals/[^/]+/decision$"),
            ("/etc/nginx/includes/proxy-http.conf",),
        ): 1,
        (
            ("location", "/v1/"),
            ("/etc/nginx/includes/proxy-http.conf",),
        ): 1,
    }
)


def tokenize(document: str) -> list[str]:
    if "\x00" in document:
        raise RuntimeError("Nginx configuration contains a NUL byte")
    tokens: list[str] = []
    position = 0
    while position < len(document):
        character = document[position]
        if character.isspace():
            position += 1
            continue
        if character == "#":
            newline = document.find("\n", position)
            position = len(document) if newline < 0 else newline + 1
            continue
        if character in "{};":
            tokens.append(character)
            position += 1
            continue
        if character in {"'", '"'}:
            quote = character
            position += 1
            value: list[str] = []
            while position < len(document):
                character = document[position]
                if character == "\\":
                    position += 1
                    if position >= len(document):
                        raise RuntimeError("Nginx quoted token has a trailing escape")
                    value.append(document[position])
                    position += 1
                    continue
                if character == quote:
                    position += 1
                    tokens.append("".join(value))
                    break
                value.append(character)
                position += 1
            else:
                raise RuntimeError("Nginx quoted token is unterminated")
            continue
        start = position
        while (
            position < len(document)
            and not document[position].isspace()
            and document[position] not in "{};#"
        ):
            position += 1
        if start == position:
            raise RuntimeError("Nginx configuration contains an unparseable token")
        tokens.append(document[start:position])
    return tokens


def parse_directives(
    document: str,
) -> tuple[list[tuple[tuple[tuple[str, ...], ...], tuple[str, ...]]], list[tuple[str, ...]]]:
    tokens = tokenize(document)
    directives: list[tuple[tuple[tuple[str, ...], ...], tuple[str, ...]]] = []
    blocks: list[tuple[str, ...]] = []
    position = 0

    def parse_block(context: tuple[tuple[str, ...], ...], top_level: bool) -> None:
        nonlocal position
        statement: list[str] = []
        while position < len(tokens):
            token = tokens[position]
            position += 1
            if token == ";":
                if not statement:
                    raise RuntimeError("Nginx configuration has an empty directive")
                directives.append((context, tuple(statement)))
                statement = []
            elif token == "{":
                if not statement:
                    raise RuntimeError("Nginx configuration has an unnamed block")
                header = tuple(statement)
                blocks.append(header)
                statement = []
                parse_block(context + (header,), False)
            elif token == "}":
                if top_level or statement:
                    raise RuntimeError("Nginx configuration has an unexpected closing brace")
                return
            else:
                statement.append(token)
        if not top_level or statement:
            raise RuntimeError("Nginx configuration is truncated")

    parse_block((), True)
    return directives, blocks


def validate_document(document: str, mode: str) -> None:
    if mode not in {"production", "probe"}:
        raise RuntimeError("unknown Edge probe validation mode")
    directives, blocks = parse_directives(document)
    upstream_blocks = [block for block in blocks if block and block[0] == "upstream"]
    if upstream_blocks != [GATEWAY_UPSTREAM_BLOCK]:
        raise RuntimeError("active Edge upstream block set is not exact")

    listens = Counter(
        directive[1:]
        for _context, directive in directives
        if directive and directive[0] == "listen"
    )
    upstream_servers = Counter(
        directive[1:]
        for context, directive in directives
        if context
        and context[-1] == GATEWAY_UPSTREAM_BLOCK
        and directive
        and directive[0] == "server"
    )
    expected_listens = PRODUCTION_LISTENS if mode == "production" else PROBE_LISTENS
    expected_upstream = PRODUCTION_UPSTREAM if mode == "production" else PROBE_UPSTREAM
    if listens != expected_listens:
        raise RuntimeError(f"active Edge listen set is not exact for {mode}: {listens}")
    if upstream_servers != expected_upstream:
        raise RuntimeError(f"active Edge gateway upstream is not exact for {mode}: {upstream_servers}")

    includes = Counter(
        (context[-1] if context else (), directive[1:])
        for context, directive in directives
        if directive and directive[0] == "include"
    )
    if includes != EXPECTED_INCLUDES:
        raise RuntimeError("active Edge include set or include context is not exact")

    identity_headers = [
        directive[1:]
        for _context, directive in directives
        if directive and directive[0] == "add_header" and len(directive) >= 2
        and directive[1].lower() == "x-cheby-probe-edge-token"
    ]
    if mode == "production":
        if identity_headers:
            raise RuntimeError("production Edge configuration contains a probe identity header")
    else:
        if len(identity_headers) != 2:
            raise RuntimeError("probe Edge identity header set is not exact")
        tokens = {arguments[1] for arguments in identity_headers if len(arguments) == 3 and arguments[2] == "always"}
        if len(tokens) != 1 or PROBE_TOKEN_PATTERN.fullmatch(next(iter(tokens))) is None:
            raise RuntimeError("probe Edge identity headers are malformed or inconsistent")


def rewrite_document(document: str, probe_token: str) -> str:
    validate_document(document, "production")
    if PROBE_TOKEN_PATTERN.fullmatch(probe_token) is None:
        raise RuntimeError("Edge probe token is malformed")
    replacements = {
        "server 172.31.42.3:8080": "server 127.0.0.1:28081",
        "listen 8080 default_server;": "listen 127.0.0.1:28080 default_server;",
        "listen 8443 ssl default_server;": "listen 127.0.0.1:28443 ssl default_server;",
    }
    rewritten = document
    for source, destination in replacements.items():
        if rewritten.count(source) != 1:
            raise RuntimeError("Edge probe rewrite source is not unique")
        rewritten = rewritten.replace(source, destination)
    header_anchor = '        add_header Referrer-Policy "no-referrer" always;'
    if rewritten.count(header_anchor) != 1:
        raise RuntimeError("Edge probe identity-header anchor is not unique")
    rewritten = rewritten.replace(
        header_anchor,
        header_anchor
        + f'\n        add_header X-Cheby-Probe-Edge-Token "{probe_token}" always;',
    )
    rate_limit_anchor = '            add_header Retry-After "60" always;'
    if rewritten.count(rate_limit_anchor) != 1:
        raise RuntimeError("Edge probe rate-limit identity-header anchor is not unique")
    rewritten = rewritten.replace(
        rate_limit_anchor,
        rate_limit_anchor
        + f'\n            add_header X-Cheby-Probe-Edge-Token "{probe_token}" always;',
    )
    validate_document(rewritten, "probe")
    return rewritten


def write_exclusive(path: Path, content: str) -> None:
    if path.exists() or path.is_symlink():
        raise RuntimeError("Edge probe rewrite output must not already exist")
    parent = path.parent.resolve(strict=True)
    metadata = parent.stat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise RuntimeError("Edge probe rewrite parent is unsafe")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(parent / path.name, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(content)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--mode", choices=("production", "probe"), required=True)
    parser.add_argument("--rewrite-output", type=Path)
    parser.add_argument("--probe-token")
    args = parser.parse_args()
    metadata = args.config.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError("Edge probe configuration must be a regular non-symlink file")
    document = args.config.read_text(encoding="utf-8")
    validate_document(document, args.mode)
    if args.rewrite_output is not None or args.probe_token is not None:
        if args.mode != "production" or args.rewrite_output is None or args.probe_token is None:
            parser.error("rewrite requires production mode, --rewrite-output, and --probe-token")
        write_exclusive(args.rewrite_output, rewrite_document(document, args.probe_token))
    print(f"Edge {args.mode} listener/upstream set: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

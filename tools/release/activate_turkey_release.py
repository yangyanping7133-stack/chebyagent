#!/usr/bin/env python3
"""Transactional Turkey application activation with verified image rollback."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from typing import Any, Callable, Protocol

from turkey_release_manifest import (
    atomic_write,
    load_manifest,
    load_state,
    record_state,
    validate_directory_tree,
    validate_running_release,
)


class ReleaseBackend(Protocol):
    def validate_release(self, release: dict[str, Any], fresh: bool) -> None: ...
    def deploy(self, release: dict[str, Any]) -> bool: ...
    def running_images(self) -> dict[str, str]: ...
    def stop(self) -> None: ...


@contextmanager
def activation_lock(
    state_directory: Path, expected_uid: int, tree_root: Path | None = None
):
    """Hold one symlink-safe inter-process lock for the full activation transaction."""
    validate_directory_tree(state_directory, expected_uid, tree_root)
    directory_flags = os.O_RDONLY
    directory_flags |= getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0)
    directory_flags |= getattr(os, "O_CLOEXEC", 0)
    directory_descriptor = os.open(state_directory, directory_flags)
    lock_descriptor: int | None = None
    try:
        directory_metadata = os.fstat(directory_descriptor)
        if (
            not stat.S_ISDIR(directory_metadata.st_mode)
            or directory_metadata.st_uid != expected_uid
            or directory_metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise RuntimeError("release state lock directory is not trusted")

        lock_flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            lock_descriptor = os.open(
                "activation.lock",
                lock_flags | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=directory_descriptor,
            )
            os.fchmod(lock_descriptor, 0o600)
            os.fsync(directory_descriptor)
        except FileExistsError:
            try:
                lock_descriptor = os.open(
                    "activation.lock", lock_flags, dir_fd=directory_descriptor
                )
            except OSError as error:
                raise RuntimeError(
                    "release activation lock path is a symlink or unsafe file"
                ) from error

        lock_metadata = os.fstat(lock_descriptor)
        path_metadata = os.stat(
            "activation.lock", dir_fd=directory_descriptor, follow_symlinks=False
        )
        if (
            not stat.S_ISREG(lock_metadata.st_mode)
            or not stat.S_ISREG(path_metadata.st_mode)
            or lock_metadata.st_uid != expected_uid
            or stat.S_IMODE(lock_metadata.st_mode) != 0o600
            or lock_metadata.st_nlink != 1
            or (lock_metadata.st_dev, lock_metadata.st_ino)
            != (path_metadata.st_dev, path_metadata.st_ino)
        ):
            raise RuntimeError("release activation lock file is not trusted")
        try:
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("another release activation transaction is already running") from error
        try:
            yield
        finally:
            fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
    finally:
        if lock_descriptor is not None:
            os.close(lock_descriptor)
        os.close(directory_descriptor)


def activate(
    candidate: dict[str, Any],
    state_before: dict[str, Any] | None,
    backend: ReleaseBackend,
    record_candidate: Callable[[], None],
    read_state: Callable[[], dict[str, Any] | None],
    allow_all_stopped_recovery: bool = False,
) -> None:
    backend.validate_release(candidate, fresh=True)
    current = None if state_before is None else state_before["current"]
    running_before = backend.running_images()
    validate_running_release(
        running_before.get("runtime", ""),
        running_before.get("edge", ""),
        current,
        allow_all_stopped=allow_all_stopped_recovery,
    )

    failure: BaseException | None = None
    try:
        if not backend.deploy(candidate):
            raise RuntimeError("candidate Compose activation failed")
        actual = backend.running_images()
        validate_running_release(
            actual.get("runtime", ""), actual.get("edge", ""), candidate
        )
        record_candidate()
        recorded = read_state()
        if recorded is None or recorded["current"] != candidate:
            raise RuntimeError("candidate release state was not durably recorded")
        return
    except BaseException as error:
        failure = error

    try:
        if read_state() != state_before:
            raise RuntimeError("release state snapshot changed after failed candidate")
    except BaseException as state_error:
        backend.stop()
        raise RuntimeError(
            "candidate failed and original release state could not be proven; services stopped"
        ) from state_error

    if current is None:
        backend.stop()
        raise RuntimeError("candidate failed with no previous release; services stopped") from failure

    try:
        backend.validate_release(current, fresh=False)
        if not backend.deploy(current):
            raise RuntimeError("previous Compose activation failed")
        rolled_back = backend.running_images()
        validate_running_release(
            rolled_back.get("runtime", ""), rolled_back.get("edge", ""), current
        )
        if read_state() != state_before:
            raise RuntimeError("release state changed during rollback")
    except BaseException as rollback_error:
        backend.stop()
        raise RuntimeError(
            "candidate and exact previous-image rollback failed; services stopped"
        ) from rollback_error
    raise RuntimeError("candidate rejected; exact previous release restored") from failure


class DockerComposeBackend:
    def __init__(
        self,
        compose_file: Path,
        manifest_path: Path,
        script_directory: Path,
    ) -> None:
        self.compose_file = compose_file
        self.manifest_path = manifest_path
        self.script_directory = script_directory

    def environment(self, release: dict[str, Any]) -> dict[str, str]:
        environment = os.environ.copy()
        for name in (
            "COMPOSE_FILE",
            "COMPOSE_PROJECT_NAME",
            "DOCKER_HOST",
            "DOCKER_CONTEXT",
            "CHEBY_RUNTIME_IMAGE",
            "CHEBY_EDGE_IMAGE",
        ):
            environment.pop(name, None)
        environment["CHEBY_RUNTIME_IMAGE"] = release["images"]["runtime"]["reference"]
        environment["CHEBY_EDGE_IMAGE"] = release["images"]["edge"]["reference"]
        return environment

    def compose(self, release: dict[str, Any], *arguments: str, capture: bool = False):
        return subprocess.run(
            ["docker", "compose", "--file", str(self.compose_file), *arguments],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
            text=True,
            env=self.environment(release),
        )

    def validate_release(self, release: dict[str, Any], fresh: bool) -> None:
        for name in ("runtime", "edge"):
            expected = release["images"][name]["reference"]
            inspected = subprocess.run(
                ["docker", "image", "inspect", "--format", "{{.Id}}", expected],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=self.environment(release),
            )
            if inspected.returncode != 0 or inspected.stdout.strip() != expected:
                raise RuntimeError(f"local {name} image differs from release manifest")

        subprocess.run(
            [
                sys.executable,
                str(self.script_directory / "validate_turkey_compose.py"),
                "--compose-yaml",
                str(self.compose_file),
            ],
            check=True,
            stdin=subprocess.DEVNULL,
            env=self.environment(release),
        )
        with tempfile.TemporaryDirectory(prefix="cheby-compose-activation-") as name:
            temporary = Path(name)
            manifest = temporary / "release-manifest.json"
            atomic_write(manifest, release)
            compose_json = temporary / "compose.json"
            rendered = self.compose(
                release, "--profile", "maintenance", "config", "--format", "json", capture=True
            )
            if rendered.returncode != 0:
                raise RuntimeError("target Compose normalization failed")
            compose_json.write_text(rendered.stdout, encoding="utf-8")
            compose_json.chmod(0o600)
            command = [
                sys.executable,
                str(self.script_directory / "validate_turkey_compose.py"),
                "--compose-json",
                str(compose_json),
                "--public-ip",
                os.environ["CHEBY_PUBLIC_IP"],
                "--bind-ip",
                os.environ["CHEBY_BIND_IP"],
                "--pairing-secret-path",
                os.environ["CHEBY_PAIRING_SECRET_PATH"],
                "--release-manifest",
                str(manifest),
            ]
            if not fresh:
                command.append("--allow-stale-manifest")
            subprocess.run(
                command,
                check=True,
                stdin=subprocess.DEVNULL,
                env=self.environment(release),
            )

    def deploy(self, release: dict[str, Any]) -> bool:
        result = self.compose(
            release,
            "up",
            "--detach",
            "--no-build",
            "--pull",
            "never",
            "--wait",
            "--wait-timeout",
            "180",
            "runtime",
            "edge",
        )
        return result.returncode == 0

    def running_images(self) -> dict[str, str]:
        result: dict[str, str] = {}
        # `ps` does not depend on candidate image values, but Compose requires
        # syntactically valid values while loading the production file.
        placeholder = {
            "images": {
                "runtime": {"reference": "sha256:" + "0" * 64},
                "edge": {"reference": "sha256:" + "1" * 64},
            }
        }
        for service in ("runtime", "edge"):
            listing = self.compose(
                placeholder, "ps", "--all", "--quiet", service, capture=True
            )
            if listing.returncode != 0:
                raise RuntimeError(f"could not inspect running {service} container")
            identifiers = listing.stdout.split()
            if len(identifiers) > 1:
                raise RuntimeError(f"multiple {service} containers exist")
            if not identifiers:
                result[service] = ""
                continue
            inspected = subprocess.run(
                [
                    "docker",
                    "inspect",
                    "--format",
                    "{{.State.Running}} {{.Image}}",
                    identifiers[0],
                ],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=self.environment(placeholder),
            )
            if inspected.returncode != 0:
                raise RuntimeError(f"could not inspect actual {service} image")
            details = inspected.stdout.split()
            if len(details) != 2 or details[0] not in {"true", "false"}:
                raise RuntimeError(f"actual {service} state is malformed")
            result[service] = details[1] if details[0] == "true" else ""
        return result

    def stop(self) -> None:
        placeholder = {
            "images": {
                "runtime": {"reference": "sha256:" + "0" * 64},
                "edge": {"reference": "sha256:" + "1" * 64},
            }
        }
        stopped = self.compose(placeholder, "stop", "edge", "runtime")
        if stopped.returncode != 0:
            raise RuntimeError("failed to stop unverified application containers")
        actual = self.running_images()
        if actual.get("runtime") or actual.get("edge"):
            raise RuntimeError("unverified application containers remained after stop")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--compose-file", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--state-directory", type=Path, required=True)
    parser.add_argument("--expected-owner-uid", type=int, default=0)
    parser.add_argument("--allow-all-stopped-recovery", action="store_true")
    args = parser.parse_args()
    script_directory = Path(__file__).resolve().parent
    validate_directory_tree(args.state_directory, args.expected_owner_uid)
    with activation_lock(args.state_directory, args.expected_owner_uid):
        candidate = load_manifest(args.manifest, args.expected_owner_uid)
        state_before = load_state(args.state_directory, args.expected_owner_uid)
        backend = DockerComposeBackend(args.compose_file, args.manifest, script_directory)
        activate(
            candidate,
            state_before,
            backend,
            lambda: record_state(
                args.manifest, args.state_directory, args.expected_owner_uid
            ),
            lambda: load_state(args.state_directory, args.expected_owner_uid),
            allow_all_stopped_recovery=args.allow_all_stopped_recovery,
        )
    print("immutable scanned Turkey application release activated and verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

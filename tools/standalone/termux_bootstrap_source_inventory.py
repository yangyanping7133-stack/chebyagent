#!/usr/bin/env python3
"""Collect hash-bound source ingredients for the embedded Termux bootstrap.

The collector never sources or executes Termux recipes.  It reads the already
retained recipe evidence, expands a small allow-listed subset of shell
parameter syntax, downloads declared source ingredients, and writes an
immutable manifest that can be verified without network access.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.parse
import urllib.error
import urllib.request


RECIPE_COMMIT = "5a6d1c1eb868795dce83a6c269387b9f82d21805"
RECIPE_ARCHIVE_SHA256 = "d36592f747fea017af60647e17e764eba254fefdf503b195e05abbbdcad9742d"
GENERATED_PACKAGES = {
    "libandroid-glob",
    "resolv-conf",
    "termux-keyring",
    "termux-licenses",
}
HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
ASSIGNMENT = re.compile(r"^([A-Z_][A-Z0-9_]*)=(.*)$")
PARAMETER = re.compile(r"\$\{([^}]+)\}")
PLAIN_PARAMETER = re.compile(r"\$([A-Z_][A-Z0-9_]*)")
URL_FALLBACKS = {
    "https://deb.debian.org/debian/pool/main/d/debianutils/debianutils_5.21.tar.xz": [
        "https://snapshot.debian.org/archive/debian/20241115T204403Z/"
        "pool/main/d/debianutils/debianutils_5.21.tar.xz",
    ],
    "https://waterlan.home.xs4all.nl/dos2unix/dos2unix-7.5.2.tar.gz": [
        "https://downloads.sourceforge.net/project/dos2unix/dos2unix/7.5.2/"
        "dos2unix-7.5.2.tar.gz",
    ],
    "https://nano-editor.org/dist/latest/nano-8.3.tar.xz": [
        "https://www.nano-editor.org/dist/v8/nano-8.3.tar.xz",
        "https://ftp.gnu.org/gnu/nano/nano-8.3.tar.xz",
    ],
}
FOOT_SOURCE_EQUIVALENCE = {
    "retained_url": (
        "https://distfiles.gentoo.org/distfiles/1e/foot-1.21.0.tar.gz"
    ),
    "retained_sha256": (
        "3b82d436434e30cb2f84c7e542a5f936c5c7fac443f4af8bcd7c7a05791903c4"
    ),
    "git_url": "https://github.com/DanteAlighierin/foot.git",
    "git_ref": "1.21.0",
    "resolved_commit": "68f5eab0b0fa08becebbed412947ba19246c2518",
    "archive_pattern": "https://github.com/DanteAlighierin/foot/archive/{commit}.tar.gz",
}
URL_FALLBACKS[FOOT_SOURCE_EQUIVALENCE["retained_url"]] = [
    "https://mirrors.mit.edu/gentoo-distfiles/distfiles/1e/foot-1.21.0.tar.gz",
    "https://ftp.yandex.ru/gentoo-distfiles/distfiles/1e/foot-1.21.0.tar.gz",
]


def digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest_file(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def tar_source_tree_digest(path: Path) -> str:
    """Hash archive source content while ignoring outer directory and tar metadata."""
    with tarfile.open(path, mode="r:*") as archive:
        members = [member for member in archive.getmembers() if member.name.strip("./")]
        parsed = []
        for member in members:
            parts = PurePosixPath(member.name).parts
            parts = tuple(part for part in parts if part not in ("", "."))
            if not parts or ".." in parts:
                raise ValueError(f"Unsafe archive member: {member.name}")
            parsed.append((member, parts))
        roots = {parts[0] for _, parts in parsed}
        strip_root = len(roots) == 1 and any(len(parts) > 1 for _, parts in parsed)
        records = []
        for member, parts in parsed:
            relative_parts = parts[1:] if strip_root else parts
            if not relative_parts or member.isdir():
                continue
            relative = "/".join(relative_parts)
            mode = member.mode & 0o777
            if member.isfile():
                stream = archive.extractfile(member)
                if stream is None:
                    raise ValueError(f"Unable to read archive member: {member.name}")
                payload = stream.read()
                records.append((relative, "file", mode, digest_bytes(payload)))
            elif member.issym():
                records.append((relative, "symlink", mode, member.linkname))
            elif member.islnk():
                records.append((relative, "hardlink", mode, member.linkname))
            else:
                records.append((relative, f"type-{member.type!r}", mode, member.linkname))
    encoded = json.dumps(sorted(records), ensure_ascii=False, separators=(",", ":")).encode()
    return digest_bytes(encoded)


def unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _parameter_value(body: str, variables: dict[str, str]) -> str:
    replacement = re.fullmatch(r"([A-Z_][A-Z0-9_]*)//\./_", body)
    if replacement:
        return variables[replacement.group(1)].replace(".", "_")
    replacement = re.fullmatch(r"([A-Z_][A-Z0-9_]*)/\./", body)
    if replacement:
        return variables[replacement.group(1)].replace(".", "", 1)
    substring = re.fullmatch(r"([A-Z_][A-Z0-9_]*):(\d+)(?::(\d+))?", body)
    if substring:
        value = variables[substring.group(1)]
        start = int(substring.group(2))
        length = substring.group(3)
        return value[start:] if length is None else value[start:start + int(length)]
    suffix = re.fullmatch(r"([A-Z_][A-Z0-9_]*)%\.\*", body)
    if suffix:
        return variables[suffix.group(1)].rsplit(".", 1)[0]
    prefix_suffix = re.fullmatch(r"([A-Z_][A-Z0-9_]*)%-p\*", body)
    if prefix_suffix:
        return variables[prefix_suffix.group(1)].split("-p", 1)[0]
    longest_prefix = re.fullmatch(r"([A-Z_][A-Z0-9_]*)##\*\.", body)
    if longest_prefix:
        return variables[longest_prefix.group(1)].rsplit(".", 1)[-1]
    if re.fullmatch(r"[A-Z_][A-Z0-9_]*", body):
        return variables[body]
    raise ValueError(f"Unsupported shell parameter expression: ${{{body}}}")


def expand_template(value: str, variables: dict[str, str]) -> str:
    if "$(" in value or "`" in value:
        raise ValueError("Command substitution is intentionally unsupported")
    expanded = PARAMETER.sub(lambda match: _parameter_value(match.group(1), variables), value)
    expanded = PLAIN_PARAMETER.sub(lambda match: variables[match.group(1)], expanded)
    if "$" in expanded:
        raise ValueError(f"Unresolved shell expression: {expanded}")
    return expanded


def parse_scalar_environment(recipe: str) -> dict[str, str]:
    """Parse only unindented scalar assignments; never execute recipe text."""
    variables: dict[str, str] = {}
    pending: list[tuple[str, str]] = []
    for line in recipe.splitlines():
        match = ASSIGNMENT.match(line)
        if not match or line.startswith((" ", "\t")):
            continue
        name, value = match.groups()
        value = unquote(value)
        if value.startswith("("):
            continue
        pending.append((name, value))
    for _ in range(len(pending) + 1):
        next_pending: list[tuple[str, str]] = []
        changed = False
        for name, value in pending:
            try:
                variables[name] = expand_template(value, variables)
                changed = True
            except (KeyError, ValueError):
                next_pending.append((name, value))
        pending = next_pending
        if not changed:
            break
    return variables


def ingredient(package: str, kind: str, url: str | None = None,
               sha256: str | None = None, **extra: object) -> dict:
    record = {"package": package, "kind": kind}
    if url is not None:
        record["url"] = url
    if sha256 is not None:
        record["declared_sha256"] = sha256
    record.update(extra)
    return record


def _patch_ingredients(package: str, recipe: str, family: str,
                       main_version: str, count: int) -> list[dict]:
    checksums = {
        number: checksum
        for number, checksum in re.findall(
            r"PATCH_CHECKSUMS\[(\d+)\]=([0-9a-f]{64})", recipe
        )
    }
    width = 3
    expected = [f"{number:0{width}d}" for number in range(1, count + 1)]
    if sorted(checksums) != expected:
        raise ValueError(f"{package} patch checksum sequence is incomplete")
    compact = main_version.replace(".", "")
    return [
        ingredient(
            package,
            "upstream-patch",
            f"https://mirrors.kernel.org/gnu/{family}/{family}-{main_version}-patches/"
            f"{family}{compact}-{number}",
            checksums[number],
            sequence=int(number),
        )
        for number in expected
    ]


def _archive_member(archive: Path, member: str) -> bytes:
    with tarfile.open(archive, mode="r:gz") as source:
        extracted = source.extractfile(member)
        if extracted is None:
            raise ValueError(f"Missing archive member: {member}")
        return extracted.read()


def x11_source(package: str, archive: Path) -> tuple[dict, bytes]:
    member = f"termux-packages-{RECIPE_COMMIT}/x11-packages/{package}/build.sh"
    recipe = _archive_member(archive, member)
    variables = parse_scalar_environment(recipe.decode())
    for key in ("TERMUX_PKG_VERSION", "TERMUX_PKG_SRCURL", "TERMUX_PKG_SHA256"):
        if key not in variables:
            raise ValueError(f"Missing {key} in x11 recipe {package}")
    if package == "foot":
        equivalence = FOOT_SOURCE_EQUIVALENCE
        return ingredient(
            "ncurses",
            "terminfo-source",
            equivalence["retained_url"],
            collection_sha256=equivalence["retained_sha256"],
            historical_declared_url=variables["TERMUX_PKG_SRCURL"],
            historical_declared_sha256=variables["TERMUX_PKG_SHA256"],
            equivalence_git_url=equivalence["git_url"],
            equivalence_git_ref=equivalence["git_ref"],
            equivalence_expected_commit=equivalence["resolved_commit"],
            equivalence_archive_pattern=equivalence["archive_pattern"],
            source_recipe=f"x11-packages/{package}/build.sh",
            source_version=variables["TERMUX_PKG_VERSION"],
            boundary=(
                "The recipe-declared Codeberg archive is rate-limited and its historical "
                "compressed bytes were not recovered. The retained Gentoo distfile must "
                "match both its own pinned hash and the source tree of an independently "
                "resolved mirror tag commit; this proves source-tree equivalence, not the "
                "historical archive byte identity."
            ),
        ), recipe
    return ingredient(
        "ncurses", "terminfo-source", variables["TERMUX_PKG_SRCURL"],
        variables["TERMUX_PKG_SHA256"],
        source_recipe=f"x11-packages/{package}/build.sh",
        source_version=variables["TERMUX_PKG_VERSION"],
    ), recipe


def package_plan(package: str, recipe_path: Path, termux_archive: Path) -> dict:
    recipe = recipe_path.read_text()
    variables = parse_scalar_environment(recipe)
    if package in GENERATED_PACKAGES:
        return {
            "recipe_version": variables["TERMUX_PKG_VERSION"],
            "source_state": "RECIPE_CONTAINED_SOURCE",
            "ingredients": [],
            "extra_recipe_files": [],
        }

    extra_recipe_files: list[dict] = []
    if package == "libandroid-support":
        specs = [
            ingredient(package, "upstream-source",
                       "https://github.com/termux/libandroid-support/archive/v29.tar.gz",
                       "8f74ce0f9cf70ec29f548696c248cac0ba560a2d8916a0fe1cf9316d5f167a80"),
            ingredient(package, "upstream-source",
                       "https://github.com/termux/wcwidth/archive/v4.tar.gz",
                       "08489e00f797ffb3b71f9c1894c83e5ebe12c90a2ee0e1f9dc7c6eb29a2ff1a8"),
        ]
        version = "29"
    elif package == "ncurses":
        specs = [
            ingredient(package, "upstream-source",
                       "https://github.com/ThomasDickey/ncurses-snapshots/archive/"
                       "a480458efb0662531287f0c75116c0e91fe235cb.tar.gz",
                       "ec6122c3b8ab930d1477a1dbfd90299e9f715555a98b6e6805d5ae1b0d72becd"),
            ingredient(package, "terminfo-source",
                       "https://fossies.org/linux/misc/rxvt-unicode-9.31.tar.bz2",
                       "aaa13fcbc149fe0f3f391f933279580f74a96fd312d6ed06b8ff03c2d46672e8"),
        ]
        for x11_package in ("kitty", "alacritty", "foot"):
            spec, data = x11_source(x11_package, termux_archive)
            specs.append(spec)
            extra_recipe_files.append({
                "path": f"x11-packages/{x11_package}/build.sh",
                "data": data,
            })
        version = "6.5.20240831"
    elif package == "ca-certificates":
        version = variables["TERMUX_PKG_VERSION"]
        date = version[2:].replace(".", "-")
        specs = [ingredient(
            package, "certificate-data", f"https://curl.se/ca/cacert-{date}.pem",
            variables["TERMUX_PKG_SHA256"]
        )]
    elif package == "dpkg":
        version = variables["TERMUX_PKG_VERSION"]
        specs = [ingredient(
            package, "git-source", "https://salsa.debian.org/dpkg-team/dpkg.git",
            git_ref=variables["TERMUX_PKG_GIT_BRANCH"],
            archive_pattern="https://salsa.debian.org/dpkg-team/dpkg/-/archive/{commit}/"
                            "dpkg-{commit}.tar.gz",
        )]
    elif package == "libandroid-selinux":
        version = variables["TERMUX_PKG_VERSION"]
        specs = [ingredient(
            package, "git-source",
            "https://android.googlesource.com/platform/external/selinux",
            git_ref=variables["TERMUX_PKG_GIT_BRANCH"],
            archive_pattern="https://android.googlesource.com/platform/external/selinux/"
                            "+archive/{commit}.tar.gz",
        )]
    else:
        required = ("TERMUX_PKG_VERSION", "TERMUX_PKG_SRCURL", "TERMUX_PKG_SHA256")
        missing = [key for key in required if key not in variables]
        if missing:
            raise ValueError(f"{package} unresolved recipe fields: {', '.join(missing)}")
        version = variables["TERMUX_PKG_VERSION"]
        specs = [ingredient(
            package, "upstream-source", variables["TERMUX_PKG_SRCURL"],
            variables["TERMUX_PKG_SHA256"]
        )]

    if package == "bash":
        specs.extend(_patch_ingredients(
            package, recipe, "bash", variables["_MAIN_VERSION"],
            int(variables["_PATCH_VERSION"])
        ))
    elif package == "readline":
        specs.extend(_patch_ingredients(
            package, recipe, "readline", variables["_MAIN_VERSION"],
            int(variables["_PATCH_VERSION"])
        ))
    elif package == "command-not-found":
        specs.append(ingredient(
            package,
            "recipe-snapshot-build-input",
            archive_member=f"termux-packages-{RECIPE_COMMIT}/repo.json",
            boundary="Pinned recipe-commit substitute for the recipe's unpinned master URL; "
                     "it does not prove the historical build-time bytes.",
        ))

    for spec in specs:
        declared = spec.get("declared_sha256")
        if declared is not None and not HEX_SHA256.fullmatch(str(declared)):
            raise ValueError(f"Invalid source SHA-256 for {package}")
    return {
        "recipe_version": version,
        "source_state": "SOURCE_INGREDIENTS_PLANNED",
        "ingredients": specs,
        "extra_recipe_files": extra_recipe_files,
    }


def build_plan(inventory_path: Path, recipe_root: Path, termux_archive: Path) -> dict:
    if digest_file(termux_archive) != RECIPE_ARCHIVE_SHA256:
        raise ValueError("Pinned Termux recipe archive SHA-256 mismatch")
    inventory = json.loads(inventory_path.read_text())
    bootstrap = [item for item in inventory["packages"] if item["origin"] == "termux-bootstrap"]
    names = sorted({item["source_recipe_package"] for item in bootstrap})
    if len(bootstrap) != 75 or len(names) != 71:
        raise ValueError("Unexpected Termux bootstrap package/recipe count")
    packages = []
    for name in names:
        recipe_path = recipe_root / name / "build.sh"
        if not recipe_path.is_file():
            raise ValueError(f"Missing retained recipe for {name}")
        planned = package_plan(name, recipe_path, termux_archive)
        installed = sorted(
            {item["package"]: item["installed_version"] for item in bootstrap
             if item["source_recipe_package"] == name}.items()
        )
        packages.append({
            "source_recipe_package": name,
            "installed_packages": [
                {"package": package, "version": version} for package, version in installed
            ],
            **planned,
        })
    ingredients = [item for package in packages for item in package["ingredients"]]
    return {
        "binary_package_count": len(bootstrap),
        "source_recipe_package_count": len(packages),
        "ingredient_count": len(ingredients),
        "generated_recipe_source_count": sum(
            package["source_state"] == "RECIPE_CONTAINED_SOURCE" for package in packages
        ),
        "packages": packages,
    }


def resolve_git_commit(url: str, ref: str) -> str:
    completed = subprocess.run(
        ["git", "ls-remote", url, ref, f"{ref}^{{}}"],
        check=True, capture_output=True, text=True, timeout=120,
    )
    refs = {}
    for line in completed.stdout.splitlines():
        sha, name = line.split("\t", 1)
        refs[name] = sha
    preferred = [
        f"refs/tags/{ref}^{{}}", f"refs/heads/{ref}", f"refs/tags/{ref}", ref,
    ]
    for name in preferred:
        if name in refs and re.fullmatch(r"[0-9a-f]{40}", refs[name]):
            return refs[name]
    unique = sorted(set(refs.values()))
    if len(unique) == 1 and re.fullmatch(r"[0-9a-f]{40}", unique[0]):
        return unique[0]
    raise ValueError(f"Unable to resolve unambiguous git ref {url} {ref}")


def fetch_to_cache(url: str, cache: Path, expected_sha256: str | None) -> tuple[Path, str, int]:
    cache.mkdir(parents=True, exist_ok=True)
    key = expected_sha256 or hashlib.sha256(url.encode()).hexdigest()
    target = cache / key
    sidecar = cache / f"{key}.sha256"
    if target.is_file():
        actual = digest_file(target)
        recorded = sidecar.read_text().strip() if sidecar.is_file() else None
        if (expected_sha256 and actual == expected_sha256) or (not expected_sha256 and recorded == actual):
            return target, actual, target.stat().st_size
        target.unlink()
        sidecar.unlink(missing_ok=True)
    temporary = cache / f"{key}.partial-{os.getpid()}"
    request = urllib.request.Request(url, headers={"User-Agent": "ChebyAgent-source-inventory/1"})
    try:
        try:
            result = hashlib.sha256()
            size = 0
            with urllib.request.urlopen(request, timeout=180) as response, temporary.open("wb") as output:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
                    result.update(chunk)
                    size += len(chunk)
            actual = result.hexdigest()
        except urllib.error.URLError:
            temporary.unlink(missing_ok=True)
            subprocess.run(
                [
                    "curl", "--fail", "--location", "--silent", "--show-error",
                    "--proto", "=http,https", "--proto-redir", "=http,https",
                    "--output", str(temporary), url,
                ],
                check=True,
                timeout=600,
            )
            actual = digest_file(temporary)
            size = temporary.stat().st_size
        if expected_sha256 and actual != expected_sha256:
            raise ValueError(f"Source SHA-256 mismatch for {url}: {actual}")
        temporary.replace(target)
        sidecar.write_text(actual + "\n")
        return target, actual, size
    finally:
        temporary.unlink(missing_ok=True)


def fetch_with_fallbacks(url: str, cache: Path,
                         expected_sha256: str | None) -> tuple[Path, str, int, str]:
    failures = []
    for candidate in [url, *URL_FALLBACKS.get(url, [])]:
        try:
            path, actual, size = fetch_to_cache(candidate, cache, expected_sha256)
            return path, actual, size, candidate
        except Exception as error:
            failures.append(f"{candidate}: {error}")
    raise ValueError("All source URLs failed: " + " | ".join(failures))


def safe_name(url: str, index: int) -> str:
    name = Path(urllib.parse.urlparse(url).path).name or "source.tar.gz"
    name = re.sub(r"[^A-Za-z0-9._+-]", "_", urllib.parse.unquote(name))
    return f"{index:03d}-{name}"


def copy_recipe_tree(source: Path, destination: Path) -> None:
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"Recipe evidence symlink is not allowed: {path}")
        if path.is_file():
            relative = path.relative_to(source)
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)


def file_manifest(root: Path) -> list[dict]:
    records = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if path.name == "manifest.json" and path.parent == root:
            continue
        records.append({
            "path": str(path.relative_to(root)),
            "sha256": digest_file(path),
            "size": path.stat().st_size,
        })
    return records


def write_new(output: Path, inventory_path: Path, recipe_root: Path,
              termux_archive: Path, cache: Path) -> dict:
    if output.exists():
        raise ValueError("Choose a new immutable output directory")
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{output.name}.partial-", dir=output.parent))
    try:
        plan = build_plan(inventory_path, recipe_root, termux_archive)
        retained_recipe_archive = (
            stage / "recipe-archives" /
            f"termux-packages-{RECIPE_COMMIT}.tar.gz"
        )
        retained_recipe_archive.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(termux_archive, retained_recipe_archive)
        for package_record in plan["packages"]:
            package = package_record["source_recipe_package"]
            copy_recipe_tree(recipe_root / package, stage / "recipe-evidence" / "packages" / package)
            for extra in package_record.pop("extra_recipe_files"):
                target = stage / "recipe-evidence" / extra["path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(extra["data"])
            if package_record["source_state"] == "RECIPE_CONTAINED_SOURCE":
                package_record["source_state"] = "COLLECTED_RECIPE_CONTAINED_SOURCE"
            for index, source in enumerate(package_record["ingredients"], 1):
                if "archive_member" in source:
                    data = _archive_member(termux_archive, source["archive_member"])
                    target = stage / "upstream" / package / f"{index:03d}-repo.json"
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                    source.update({
                        "path": str(target.relative_to(stage)),
                        "sha256": digest_bytes(data),
                        "size": len(data),
                        "verification": "PINNED_RECIPE_ARCHIVE_MEMBER",
                    })
                    continue
                url = source["url"]
                expected = source.get("declared_sha256") or source.get("collection_sha256")
                if source["kind"] == "git-source":
                    commit = resolve_git_commit(url, source["git_ref"])
                    url = source["archive_pattern"].format(commit=commit)
                    source["resolved_commit"] = commit
                    source["archive_url"] = url
                try:
                    cached, actual, size, fetched_url = fetch_with_fallbacks(url, cache, expected)
                except Exception as error:
                    raise ValueError(f"Source collection failed for {package}: {url}: {error}") from error
                target = stage / "upstream" / package / safe_name(url, index)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(cached, target)
                verification = (
                    "DECLARED_SHA256_MATCH" if source.get("declared_sha256")
                    else "GIT_COMMIT_PINNED_ARCHIVE"
                )
                if "equivalence_git_url" in source:
                    commit = resolve_git_commit(
                        source["equivalence_git_url"], source["equivalence_git_ref"]
                    )
                    if commit != source["equivalence_expected_commit"]:
                        raise ValueError(
                            f"Source-equivalence tag moved for {package}: {commit}"
                        )
                    proof_url = source["equivalence_archive_pattern"].format(commit=commit)
                    proof_cached, proof_sha, proof_size, proof_fetched_url = (
                        fetch_with_fallbacks(proof_url, cache, None)
                    )
                    retained_tree_sha = tar_source_tree_digest(cached)
                    proof_tree_sha = tar_source_tree_digest(proof_cached)
                    if retained_tree_sha != proof_tree_sha:
                        raise ValueError(
                            f"Source tree does not match pinned mirror commit for {package}"
                        )
                    proof_target = (
                        stage / "equivalence-proof" / package /
                        safe_name(proof_url, index)
                    )
                    proof_target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(proof_cached, proof_target)
                    source.update({
                        "equivalence_resolved_commit": commit,
                        "equivalence_archive_url": proof_url,
                        "equivalence_fetched_url": proof_fetched_url,
                        "equivalence_proof_path": str(proof_target.relative_to(stage)),
                        "equivalence_proof_sha256": proof_sha,
                        "equivalence_proof_size": proof_size,
                        "source_tree_sha256": retained_tree_sha,
                    })
                    verification = "PINNED_HASH_AND_MIRROR_COMMIT_SOURCE_TREE_MATCH"
                source.update({
                    "path": str(target.relative_to(stage)),
                    "sha256": actual,
                    "size": size,
                    "fetched_url": fetched_url,
                    "verification": verification,
                })
            if package_record["source_state"] == "SOURCE_INGREDIENTS_PLANNED":
                package_record["source_state"] = "COLLECTED"
        files = file_manifest(stage)
        manifest = {
            "schema_version": 1,
            "termux_recipe_commit": RECIPE_COMMIT,
            "input_inventory": str(inventory_path),
            "input_inventory_sha256": digest_file(inventory_path),
            "input_termux_recipe_archive": str(termux_archive),
            "input_termux_recipe_archive_sha256": digest_file(termux_archive),
            "retained_termux_recipe_archive": str(retained_recipe_archive.relative_to(stage)),
            **plan,
            "file_count": len(files),
            "downloaded_and_retained_bytes": sum(item["size"] for item in files),
            "files": files,
            "boundary": (
                "This preserves recipe-declared source ingredients and local recipe patches. "
                "It does not prove reproducible binaries, complete notices, or final APK-wide "
                "redistribution compliance. command-not-found repo.json remains indirect because "
                "the historical recipe used an unpinned master URL. foot 1.21.0 retains a "
                "hash-pinned equivalent source tree and a commit-pinned comparison archive, "
                "but not the recipe-declared historical compressed bytes."
            ),
        }
        manifest_path = stage / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
        stage.replace(output)
        return {
            "outcome": "PASS",
            "binary_packages": plan["binary_package_count"],
            "source_recipe_packages": plan["source_recipe_package_count"],
            "ingredients": plan["ingredient_count"],
            "files": len(files),
            "bytes": manifest["downloaded_and_retained_bytes"],
            "manifest_sha256": digest_file(output / "manifest.json"),
        }
    except Exception:
        shutil.rmtree(stage)
        raise


def verify_existing(output: Path) -> dict:
    manifest_path = output / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("schema_version") != 1:
        raise ValueError("Unsupported Termux source manifest schema")
    if manifest.get("binary_package_count") != 75 or manifest.get("source_recipe_package_count") != 71:
        raise ValueError("Termux source manifest package count mismatch")
    expected_paths = {item["path"] for item in manifest["files"]}
    actual_paths = {
        str(path.relative_to(output)) for path in output.rglob("*")
        if path.is_file() and path != manifest_path
    }
    if expected_paths != actual_paths:
        raise ValueError("Termux source manifest file set mismatch")
    for record in manifest["files"]:
        path = output / record["path"]
        if path.stat().st_size != record["size"] or digest_file(path) != record["sha256"]:
            raise ValueError(f"Termux source file verification failed: {record['path']}")
    ingredients = [item for package in manifest["packages"] for item in package["ingredients"]]
    if len(ingredients) != manifest["ingredient_count"]:
        raise ValueError("Termux source ingredient count mismatch")
    for source in ingredients:
        if source["sha256"] != digest_file(output / source["path"]):
            raise ValueError("Termux ingredient record does not match file manifest")
        declared = source.get("declared_sha256")
        if declared is not None and source["sha256"] != declared:
            raise ValueError("Termux ingredient declared SHA-256 mismatch")
        if source["kind"] == "git-source" and not re.fullmatch(
            r"[0-9a-f]{40}", source.get("resolved_commit", "")
        ):
            raise ValueError("Termux git source is not commit-pinned")
        collection_sha = source.get("collection_sha256")
        if collection_sha is not None and source["sha256"] != collection_sha:
            raise ValueError("Termux ingredient collection SHA-256 mismatch")
        if "equivalence_proof_path" in source:
            proof_path = output / source["equivalence_proof_path"]
            if digest_file(proof_path) != source["equivalence_proof_sha256"]:
                raise ValueError("Termux source-equivalence proof archive mismatch")
            if tar_source_tree_digest(output / source["path"]) != source["source_tree_sha256"]:
                raise ValueError("Termux retained source tree digest mismatch")
            if tar_source_tree_digest(proof_path) != source["source_tree_sha256"]:
                raise ValueError("Termux source-equivalence tree mismatch")
            if not re.fullmatch(
                r"[0-9a-f]{40}", source.get("equivalence_resolved_commit", "")
            ):
                raise ValueError("Termux source-equivalence commit is not pinned")
    return {
        "outcome": "PASS",
        "binary_packages": manifest["binary_package_count"],
        "source_recipe_packages": manifest["source_recipe_package_count"],
        "ingredients": len(ingredients),
        "files": len(actual_paths),
        "bytes": sum(item["size"] for item in manifest["files"]),
        "manifest_sha256": digest_file(manifest_path),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--inventory", type=Path)
    parser.add_argument("--recipe-root", type=Path)
    parser.add_argument("--termux-source-archive", type=Path)
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    if args.plan:
        if not all((args.inventory, args.recipe_root, args.termux_source_archive)):
            parser.error(
                "--inventory, --recipe-root, and --termux-source-archive are required "
                "with --plan"
            )
        result = build_plan(args.inventory, args.recipe_root, args.termux_source_archive)
        print(json.dumps({
            key: result[key] for key in (
                "binary_package_count", "source_recipe_package_count",
                "ingredient_count", "generated_recipe_source_count"
            )
        }))
        return
    if args.output is None:
        parser.error("--output is required unless --plan is used")
    if args.verify:
        result = verify_existing(args.output)
    else:
        if args.cache is None:
            parser.error("--cache is required when collecting")
        if not all((args.inventory, args.recipe_root, args.termux_source_archive)):
            parser.error(
                "--inventory, --recipe-root, and --termux-source-archive are required "
                "when collecting"
            )
        result = write_new(
            args.output, args.inventory, args.recipe_root,
            args.termux_source_archive, args.cache
        )
    print(json.dumps(result))


if __name__ == "__main__":
    main()

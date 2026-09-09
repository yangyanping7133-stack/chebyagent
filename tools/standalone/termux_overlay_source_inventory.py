#!/usr/bin/env python3
"""Collect exact upstream source and Termux recipe evidence for the PRoot overlay."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import tarfile
import urllib.parse
import urllib.request


PACKAGES = {
    "proot": {
        "version": "5.1.107.89",
        "recipe_commit": "48ba8fb652272580ba3f9663973fc2792e2a3566",
        "source_url": "https://github.com/termux/proot/archive/v5.1.107.89.zip",
        "source_sha256": "e1240f63de03e6da536d74041c7937ddd8737ab27743857d79285724b948eca8",
    },
    "libandroid-shmem": {
        "version": "0.7",
        "recipe_commit": "b25e257208da6d2e8b558b8a2b51762158a2e806",
        "source_url": "https://github.com/termux/libandroid-shmem/archive/refs/tags/v0.7.tar.gz",
        "source_sha256": "1e5ff8459bc0a8c229dd8a94b27d119987e09ef3414331c2b5ebfff20b98e867",
    },
    "libtalloc": {
        "version": "2.4.3",
        "recipe_commit": "fbc049451e7fc59cdf510732aad49bd45590b0bb",
        "source_url": "https://www.samba.org/ftp/talloc/talloc-2.4.3.tar.gz",
        "source_sha256": "dc46c40b9f46bb34dd97fe41f548b0e8b247b77a918576733c528e83abd854dd",
    },
}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "ChebyAgent-source-inventory/1"})
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()


def recipe_files(package: str, commit: str, archive: bytes) -> list[tuple[str, bytes]]:
    wanted = f"termux-packages-{commit}/packages/{package}/"
    result: list[tuple[str, bytes]] = []
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as source:
        for member in source:
            if not member.isfile() or not member.name.startswith(wanted):
                continue
            relative = member.name.removeprefix(wanted)
            if not relative or relative.startswith("/") or ".." in Path(relative).parts:
                raise ValueError("Unsafe recipe archive member")
            extracted = source.extractfile(member)
            if extracted is None:
                raise ValueError("Recipe archive member is unreadable")
            result.append((relative, extracted.read()))
    if not any(path == "build.sh" for path, _ in result):
        raise ValueError(f"Missing build.sh for {package}")
    return sorted(result)


def write_new(output: Path) -> dict:
    if output.exists():
        raise ValueError("Choose a new immutable output directory")
    output.mkdir(parents=True)
    records = []
    total_bytes = 0
    for package, coordinate in PACKAGES.items():
        source = fetch(coordinate["source_url"])
        if digest(source) != coordinate["source_sha256"]:
            raise ValueError(f"Upstream source SHA-256 mismatch for {package}")
        suffix = Path(urllib.parse.urlparse(coordinate["source_url"]).path).name
        source_path = Path("upstream") / package / suffix
        destination = output / source_path
        destination.parent.mkdir(parents=True)
        destination.write_bytes(source)
        total_bytes += len(source)

        commit = coordinate["recipe_commit"]
        recipe_url = f"https://codeload.github.com/termux/termux-packages/tar.gz/{commit}"
        recipe_archive = fetch(recipe_url)
        recipe_archive_path = Path("recipe-archives") / f"termux-packages-{commit}.tar.gz"
        recipe_destination = output / recipe_archive_path
        recipe_destination.parent.mkdir(parents=True, exist_ok=True)
        recipe_destination.write_bytes(recipe_archive)
        total_bytes += len(recipe_archive)

        evidence = []
        for relative, data in recipe_files(package, commit, recipe_archive):
            path = Path("recipe-evidence") / package / relative
            target = output / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            evidence.append({"path": str(path), "sha256": digest(data), "size": len(data)})
        records.append({
            "package": package,
            **coordinate,
            "source_path": str(source_path),
            "source_size": len(source),
            "recipe_archive_url": recipe_url,
            "recipe_archive_path": str(recipe_archive_path),
            "recipe_archive_sha256": digest(recipe_archive),
            "recipe_archive_size": len(recipe_archive),
            "recipe_evidence": evidence,
        })
    manifest = {
        "schema_version": 1,
        "source": "Official upstream release archives and exact Termux recipe commits",
        "package_count": len(records),
        "downloaded_bytes": total_bytes,
        "packages": records,
        "boundary": "Source and recipe bytes are hash-bound. This does not prove binary reproducibility or complete redistribution compliance.",
    }
    manifest_path = output / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    return {"outcome": "PASS", "packages": len(records), "bytes": total_bytes,
            "manifest_sha256": digest(manifest_path.read_bytes())}


def verify_existing(output: Path) -> dict:
    manifest_path = output / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("package_count") != len(PACKAGES) or len(manifest.get("packages", [])) != len(PACKAGES):
        raise ValueError("Overlay source manifest package count mismatch")
    checked = 0
    for record in manifest["packages"]:
        coordinate = PACKAGES.get(record["package"])
        if coordinate is None or any(record.get(key) != value for key, value in coordinate.items()):
            raise ValueError("Overlay source coordinate mismatch")
        source = (output / record["source_path"]).read_bytes()
        if len(source) != record["source_size"] or digest(source) != coordinate["source_sha256"]:
            raise ValueError("Overlay upstream source verification failed")
        recipe_archive = (output / record["recipe_archive_path"]).read_bytes()
        if (len(recipe_archive) != record["recipe_archive_size"] or
                digest(recipe_archive) != record["recipe_archive_sha256"]):
            raise ValueError("Overlay recipe archive verification failed")
        extracted = {path: data for path, data in recipe_files(record["package"], coordinate["recipe_commit"], recipe_archive)}
        for evidence in record["recipe_evidence"]:
            data = (output / evidence["path"]).read_bytes()
            relative = str(Path(evidence["path"]).relative_to("recipe-evidence", record["package"]))
            if extracted.get(relative) != data or len(data) != evidence["size"] or digest(data) != evidence["sha256"]:
                raise ValueError("Overlay recipe evidence verification failed")
            checked += 1
    return {"outcome": "PASS", "packages": len(PACKAGES), "recipe_files": checked,
            "manifest_sha256": digest(manifest_path.read_bytes())}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    result = verify_existing(args.output) if args.verify else write_new(args.output)
    print(json.dumps(result))


if __name__ == "__main__":
    main()

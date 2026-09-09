#!/usr/bin/env python3
"""Collect exact Debian source-package files from the official snapshot archive.

The input is the immutable component inventory produced from the bundled rootfs.
Every downloaded file is addressed by its snapshot SHA-1, checked again locally,
and cross-checked against the source package's signed ``.dsc`` SHA-256 list.
The script records the signature but deliberately does not claim that it verified
the Debian maintainer signature.
"""

import argparse
import concurrent.futures
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


SNAPSHOT = "https://snapshot.debian.org"
USER_AGENT = "ChebyAgent-source-inventory/1.0"


def digest(data, algorithm="sha256"):
    return hashlib.new(algorithm, data).hexdigest()


def fetch(url, *, attempts=4, timeout=60):
    error = None
    for attempt in range(attempts):
        try:
            with urlopen(Request(url, headers={"User-Agent": USER_AGENT}), timeout=timeout) as response:
                return response.read()
        except (HTTPError, URLError, TimeoutError) as current:
            error = current
            if attempt + 1 < attempts:
                time.sleep(1 << attempt)
    raise RuntimeError(f"Unable to fetch official Debian snapshot URL: {url}") from error


def fetch_json(url):
    return json.loads(fetch(url).decode("utf-8"))


def exact_sources(inventory):
    sources = {
        (item["source_package"], item["source_version"])
        for item in inventory["packages"]
        if item["origin"] == "debian"
    }
    if not sources or any(not name or not version for name, version in sources):
        raise ValueError("Debian source package identity is missing")
    return sorted(sources)


def source_projection_sha256(inventory):
    """Digest only the Debian source identities that this collector consumes."""
    projection = [
        {"package": package, "version": version}
        for package, version in exact_sources(inventory)
    ]
    data = json.dumps(
        projection, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return digest(data)


def safe_filename(value):
    path = PurePosixPath(value)
    if path.is_absolute() or len(path.parts) != 1 or path.name in ("", ".", ".."):
        raise ValueError(f"Unsafe snapshot filename: {value}")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+:~_-]*", path.name):
        raise ValueError(f"Unexpected snapshot filename: {value}")
    return path.name


def dsc_sha256_entries(data):
    """Return filename -> (sha256, size) from a clear-signed Debian .dsc."""
    text = data.decode("utf-8")
    match = re.search(r"(?m)^Checksums-Sha256:\s*\n((?: [^\n]+\n)+)", text)
    if not match:
        raise ValueError("DSC has no Checksums-Sha256 field")
    result = {}
    for line in match.group(1).splitlines():
        parts = line.split()
        if len(parts) != 3 or not re.fullmatch(r"[0-9a-f]{64}", parts[0]):
            raise ValueError("Malformed DSC Checksums-Sha256 entry")
        name = safe_filename(parts[2])
        if name in result:
            raise ValueError("Duplicate DSC filename")
        result[name] = (parts[0], int(parts[1]))
    return result


def preferred_info(options):
    return next((item for item in options if item.get("archive_name") == "debian"), options[0])


def select_records(package, version, hashes, metadata):
    """Resolve snapshot aliases to the exact filenames named by this version's DSC."""
    dsc_hashes = []
    for sha1 in hashes:
        if any(item.get("name", "").endswith(".dsc") for item in metadata[sha1]):
            dsc_hashes.append(sha1)
    if len(dsc_hashes) != 1:
        raise ValueError(f"Expected one DSC hash for {package} {version}")
    dsc_hash = dsc_hashes[0]
    dsc_options = [item for item in metadata[dsc_hash] if item.get("name", "").endswith(".dsc")]
    dsc_info = preferred_info(dsc_options)
    dsc_record = {
        "sha1": dsc_hash,
        "name": safe_filename(dsc_info["name"]),
        "size": int(dsc_info["size"]),
        "archive_name": dsc_info.get("archive_name", ""),
        "first_seen": dsc_info.get("first_seen", ""),
        "snapshot_path": dsc_info.get("path", ""),
    }
    dsc_data = fetch(f"{SNAPSHOT}/file/{dsc_hash}", timeout=180)
    if len(dsc_data) != dsc_record["size"] or digest(dsc_data, "sha1") != dsc_hash:
        raise ValueError(f"Snapshot content mismatch for {dsc_record['name']}")
    expected = dsc_sha256_entries(dsc_data)

    records = [dsc_record]
    excluded = []
    for sha1 in hashes:
        if sha1 == dsc_hash:
            continue
        candidates = [item for item in metadata[sha1] if item.get("name") in expected]
        if not candidates:
            excluded.append({
                "sha1": sha1,
                "reason": "Snapshot srcfiles association is not listed by this version's DSC",
                "aliases": sorted({safe_filename(item["name"]) for item in metadata[sha1]}),
            })
            continue
        selected = preferred_info(candidates)
        records.append({
            "sha1": sha1,
            "name": safe_filename(selected["name"]),
            "size": int(selected["size"]),
            "archive_name": selected.get("archive_name", ""),
            "first_seen": selected.get("first_seen", ""),
            "snapshot_path": selected.get("path", ""),
        })
    if {item["name"] for item in records[1:]} != set(expected):
        raise ValueError(f"Snapshot hashes differ from DSC payload for {package} {version}")
    return records, excluded


def source_files(package, version):
    package_url = quote(package, safe="")
    version_url = quote(version, safe="")
    listing = fetch_json(f"{SNAPSHOT}/mr/package/{package_url}/{version_url}/srcfiles")
    hashes = [item["hash"] for item in listing.get("result", [])]
    if not hashes or any(not re.fullmatch(r"[0-9a-f]{40}", value) for value in hashes):
        raise ValueError(f"Snapshot returned no valid source files for {package} {version}")
    metadata = {}
    for sha1 in hashes:
        info = fetch_json(f"{SNAPSHOT}/mr/file/{sha1}/info").get("result", [])
        if not info:
            raise ValueError(f"Snapshot has no metadata for {sha1}")
        metadata[sha1] = info
    return select_records(package, version, hashes, metadata)


def download_record(record, output):
    data = fetch(f"{SNAPSHOT}/file/{record['sha1']}", timeout=180)
    if len(data) != record["size"] or digest(data, "sha1") != record["sha1"]:
        raise ValueError(f"Snapshot content mismatch for {record['name']}")
    relative = Path("files") / record["sha1"] / record["name"]
    target = output / relative
    target.parent.mkdir(parents=True, exist_ok=False)
    target.write_bytes(data)
    return {**record, "path": relative.as_posix(), "sha256": digest(data)}


def verify_package(package, version, files, output):
    dsc = [item for item in files if item["name"].endswith(".dsc")]
    if len(dsc) != 1:
        raise ValueError(f"Expected one DSC for {package} {version}")
    entries = dsc_sha256_entries((output / dsc[0]["path"]).read_bytes())
    payload = {item["name"]: item for item in files if item is not dsc[0]}
    if set(entries) != set(payload):
        raise ValueError(f"DSC payload list differs for {package} {version}")
    for name, (sha256, size) in entries.items():
        item = payload[name]
        if item["sha256"] != sha256 or item["size"] != size:
            raise ValueError(f"DSC checksum mismatch for {name}")
    return {
        "package": package,
        "version": version,
        "dsc_signature_state": "PRESENT_NOT_VERIFIED",
        "files": sorted(files, key=lambda item: item["name"]),
    }


def verify_existing(inventory_path, output):
    """Recheck an immutable collection without contacting the network."""
    inventory_data = inventory_path.read_bytes()
    inventory = json.loads(inventory_data)
    manifest_path = output / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    schema_version = manifest.get("schema_version")
    if schema_version not in (1, 2):
        raise ValueError("Unsupported Debian source manifest schema")
    expected_projection = source_projection_sha256(inventory)
    if schema_version == 2:
        if manifest.get("input_debian_sources_sha256") != expected_projection:
            raise ValueError("Manifest Debian source projection digest mismatch")

    expected_sources = exact_sources(inventory)
    packages = manifest.get("packages")
    if not isinstance(packages, list) or len(packages) != manifest.get("package_count"):
        raise ValueError("Manifest package count mismatch")
    identities = [(item.get("package"), item.get("version")) for item in packages]
    if identities != expected_sources:
        raise ValueError("Manifest source identities differ from the component inventory")

    # Schema 1 bound the complete component inventory.  That incorrectly made an
    # unrelated extra component (for example a Codex payload) invalidate a
    # byte-identical Debian source collection.  Its ordered manifest identities
    # are the compatibility binding; schema 2 also pins their canonical digest.
    inventory_binding = "debian_source_projection_sha256"
    if schema_version == 1:
        inventory_binding = (
            "legacy_full_inventory_sha256"
            if manifest.get("input_inventory_sha256") == digest(inventory_data)
            else "legacy_ordered_source_identities"
        )

    listed_paths = set()
    listed_files = 0
    total_bytes = 0
    for package in packages:
        files = package.get("files")
        if not isinstance(files, list):
            raise ValueError("Manifest package files must be a list")
        for item in files:
            name = safe_filename(item.get("name", ""))
            sha1 = item.get("sha1", "")
            sha256 = item.get("sha256", "")
            if not re.fullmatch(r"[0-9a-f]{40}", sha1):
                raise ValueError(f"Invalid SHA-1 for {name}")
            if not re.fullmatch(r"[0-9a-f]{64}", sha256):
                raise ValueError(f"Invalid SHA-256 for {name}")
            expected_path = (Path("files") / sha1 / name).as_posix()
            if item.get("path") != expected_path or expected_path in listed_paths:
                raise ValueError(f"Invalid or duplicate manifest path for {name}")
            listed_paths.add(expected_path)
            target = output / expected_path
            if not target.is_file() or target.is_symlink():
                raise ValueError(f"Missing or unsafe collected source file: {expected_path}")
            data = target.read_bytes()
            if len(data) != item.get("size"):
                raise ValueError(f"Collected source size mismatch: {expected_path}")
            if digest(data, "sha1") != sha1 or digest(data) != sha256:
                raise ValueError(f"Collected source digest mismatch: {expected_path}")
            listed_files += 1
            total_bytes += len(data)
        verify_package(package["package"], package["version"], files, output)

    if listed_files != manifest.get("file_count"):
        raise ValueError("Manifest file count mismatch")
    actual_paths = {
        path.relative_to(output).as_posix()
        for path in (output / "files").rglob("*")
        if path.is_file()
    }
    if actual_paths != listed_paths:
        raise ValueError("Collected source tree contains missing or unlisted files")
    return {
        "outcome": "PASS",
        "packages": len(packages),
        "files": listed_files,
        "bytes": total_bytes,
        "manifest_sha256": digest(manifest_path.read_bytes()),
        "inventory_binding": inventory_binding,
        "debian_sources_sha256": expected_projection,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument(
        "--verify",
        action="store_true",
        help="verify an existing immutable output without using the network",
    )
    args = parser.parse_args()
    if args.workers < 1 or args.workers > 12:
        raise SystemExit("workers must be between 1 and 12")
    if args.verify:
        if not args.output.is_dir():
            raise SystemExit("Existing output directory is required for verification")
        print(json.dumps(verify_existing(args.inventory, args.output)))
        return
    if args.output.exists():
        raise SystemExit("Choose a new immutable output directory")

    inventory_data = args.inventory.read_bytes()
    inventory = json.loads(inventory_data)
    sources = exact_sources(inventory)
    args.output.mkdir(parents=True)
    packages = []
    for index, (package, version) in enumerate(sources, 1):
        records, excluded = source_files(package, version)
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
            files = list(executor.map(lambda item: download_record(item, args.output), records))
        verified = verify_package(package, version, files, args.output)
        verified["snapshot_srcfile_associations_not_in_dsc"] = excluded
        packages.append(verified)
        print(json.dumps({"completed": index, "total": len(sources), "package": package,
                          "version": version, "files": len(files)}), flush=True)

    manifest = {
        "schema_version": 2,
        "source": "Debian snapshot archive",
        "snapshot_base_url": SNAPSHOT,
        "input_inventory_sha256": digest(inventory_data),
        "input_debian_sources_sha256": source_projection_sha256(inventory),
        "package_count": len(packages),
        "file_count": sum(len(item["files"]) for item in packages),
        "packages": packages,
        "boundary": "All files match snapshot SHA-1 and DSC SHA-256/size. DSC signatures are retained but not cryptographically verified.",
    }
    manifest_path = args.output / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"outcome": "PASS", "packages": len(packages),
                      "files": manifest["file_count"], "bytes": sum(
                          item["size"] for package in packages for item in package["files"]),
                      "manifest_sha256": digest(manifest_path.read_bytes())}), flush=True)


if __name__ == "__main__":
    main()

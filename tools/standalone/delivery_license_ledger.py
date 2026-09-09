#!/usr/bin/env python3
"""Build a fail-closed delivery license ledger from captured component evidence.

This is evidence bookkeeping, not a legal conclusion. It deliberately keeps a
component blocked until a human review records the applicable obligations and
their delivery mechanism.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def load(path: Path) -> dict:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object: " + str(path))
    return value


def build(runtime: dict, android_dependencies: dict, android_notices: dict) -> dict:
    runtime_rows = []
    for item in runtime.get("packages", []):
        evidence = item.get("license_evidence", [])
        runtime_rows.append({
            "id": ":".join((item["origin"], item["package"], item["version"], item["architecture"])),
            "kind": "runtime_package",
            "origin": item["origin"],
            "name": item["package"],
            "version": item["version"],
            "source_identity": {
                "name": item.get("source_package"),
                "version": item.get("source_version"),
                "basis": item.get("source_identity_basis"),
            },
            "license_evidence_state": item.get("license_evidence_state"),
            "license_evidence": [
                {key: record.get(key) for key in ("path", "resolved_path", "sha256", "declared_license_labels")}
                for record in evidence
            ],
            "obligation_review": "BLOCKED_NOT_ASSESSED",
            "source_delivery_review": "BLOCKED_NOT_BOUND_TO_FINAL_DELIVERY",
        })

    extra_rows = []
    for index, item in enumerate(runtime.get("extra_components", [])):
        metadata = item.get("metadata", {})
        extra_rows.append({
            "id": f"extra:{index}:{item.get('component', 'unknown')}:{metadata.get('version', 'unversioned')}",
            "kind": "runtime_extra_component",
            "origin": item.get("origin"),
            "name": item.get("component"),
            "version": metadata.get("version"),
            "metadata_path": item.get("metadata_path") or item.get("path"),
            "metadata_sha256": item.get("metadata_sha256") or item.get("sha256"),
            "obligation_review": "BLOCKED_NOT_ASSESSED",
            "source_delivery_review": "BLOCKED_NOT_BOUND_TO_FINAL_DELIVERY",
        })

    artifacts = {item["coordinate"] for item in android_dependencies.get("artifacts", [])}
    notice_map: dict[str, list[dict]] = {}
    for item in android_notices.get("notices", []):
        notice_map.setdefault(item["coordinate"], []).append({
            key: item.get(key) for key in ("location", "path", "sha256", "bytes")
        })
    android_rows = []
    for item in android_notices.get("declarations", []):
        coordinate = item["coordinate"]
        licenses = item.get("licenses", [])
        android_rows.append({
            "id": "maven:" + coordinate,
            "kind": "android_maven_component",
            "coordinate": coordinate,
            "resolved_artifact_recorded": coordinate in artifacts,
            "pom_license_declarations": licenses,
            "scm_url": item.get("scm_url"),
            "embedded_notice_evidence": notice_map.get(coordinate, []),
            "declaration_state": "PRESENT" if licenses else "MISSING",
            "obligation_review": "BLOCKED_NOT_ASSESSED",
            "source_delivery_review": "BLOCKED_NOT_COLLECTED_OR_BOUND",
        })

    rows = runtime_rows + extra_rows + android_rows
    ids = [row["id"] for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate component identity in license ledger")
    counts = {
        "runtime_packages": len(runtime_rows),
        "runtime_packages_missing_direct_license_evidence": sum(
            row["license_evidence_state"] != "PRESENT" for row in runtime_rows
        ),
        "runtime_extra_components": len(extra_rows),
        "android_maven_declarations": len(android_rows),
        "android_maven_declarations_missing_license": sum(
            row["declaration_state"] != "PRESENT" for row in android_rows
        ),
        "android_components_with_embedded_notice_evidence": sum(
            bool(row["embedded_notice_evidence"]) for row in android_rows
        ),
        "component_rows": len(rows),
        "component_rows_closed": 0,
        "component_rows_blocked": len(rows),
    }
    return {
        "schema": 1,
        "counts": counts,
        "components": rows,
        "cross_cutting_blockers": [
            "Project-owned code has no recorded final license decision in this ledger.",
            "Applicable notice, attribution, source-offer, installation-information, and relinking obligations have not been adjudicated per component.",
            "Android/JNI dependencies are not yet mapped byte-for-byte from the signed final APK to this component list.",
            "Collected corresponding sources have not yet been bound to an immutable final source tag and delivery procedure.",
            "This inventory has not received legal review and is not a redistribution-compliance conclusion."
        ],
        "outcome": "BLOCKED_REVIEW_REQUIRED",
        "boundary": (
            "Every captured runtime package, runtime extra component, and Maven POM declaration "
            "has an explicit blocked disposition. This closes inventory ambiguity only; it does "
            "not close any legal obligation or make the release ready."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-inventory", required=True, type=Path)
    parser.add_argument("--android-dependencies", required=True, type=Path)
    parser.add_argument("--android-notices", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("Refusing to overwrite a license ledger")
    paths = (args.runtime_inventory, args.android_dependencies, args.android_notices)
    result = build(*(load(path) for path in paths))
    result["inputs"] = [
        {"path": str(path), "sha256": digest(path)} for path in paths
    ]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")
    print(json.dumps({"counts": result["counts"], "outcome": result["outcome"],
                      "output": str(args.output)}))


if __name__ == "__main__":
    main()

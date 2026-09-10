#!/usr/bin/env python3
"""Reconcile a fail-closed release ledger with later delivery evidence.

The input ledger remains immutable.  This tool closes only mechanical evidence
gaps and collapses repeated row-level placeholders into release-level decisions.
It never declares legal compliance and never substitutes for distributor review.
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


def load_sha256s(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for number, raw_line in enumerate(path.read_text().splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split(maxsplit=1)
        if len(parts) != 2 or len(parts[0]) != 64:
            raise ValueError(f"Invalid SHA256SUMS line {number}")
        name = parts[1].lstrip("*")
        if name in result:
            raise ValueError("Duplicate SHA256SUMS entry: " + name)
        result[name] = parts[0].lower()
    return result


def reconcile(ledger: dict, policy: dict, sums: dict[str, str], ledger_sha256: str) -> dict:
    if ledger.get("schema") != 1 or policy.get("schema") != 1:
        raise ValueError("Unsupported ledger or policy schema")
    if ledger_sha256 != policy["release_evidence"]["ledger_sha256"]:
        raise ValueError("The ledger digest does not match the version-bound policy")
    if ledger.get("counts", {}).get("component_rows") != policy["expected_component_rows"]:
        raise ValueError("Unexpected component row count")

    rows = ledger.get("components", [])
    row_ids = [row["id"] for row in rows]
    if len(row_ids) != len(set(row_ids)):
        raise ValueError("Duplicate component identity in input ledger")

    required_assets = policy["release_evidence"]["required_assets"]
    missing_assets = sorted(set(required_assets) - set(sums))
    mismatched_assets = sorted(
        name for name, expected in required_assets.items()
        if sums.get(name) is not None and sums[name] != expected
    )
    if missing_assets or mismatched_assets:
        raise ValueError(
            "Release hash manifest mismatch: missing=" + repr(missing_assets)
            + ", mismatched=" + repr(mismatched_assets)
        )

    overrides = policy["license_identity_overrides"]
    unknown_overrides = sorted(set(overrides) - set(row_ids))
    if unknown_overrides:
        raise ValueError("Policy override refers to an unknown row: " + repr(unknown_overrides))

    source_by_origin = policy["runtime_source_asset_by_origin"]
    extra_sources = policy["extra_component_source_assets"]
    reconciled = []
    identity_closed = 0
    source_bound = 0
    for row in rows:
        row_id = row["id"]
        kind = row["kind"]
        if kind == "runtime_package":
            direct = row.get("license_evidence_state") == "PRESENT"
            source_asset = source_by_origin.get(row.get("origin"))
            source_state = "BOUND_TO_RELEASE_SOURCE_ARCHIVE" if source_asset else "UNBOUND"
        elif kind == "android_maven_component":
            direct = row.get("declaration_state") == "PRESENT"
            source_asset = None
            source_state = "LICENSE_FAMILY_RECORDED; SOURCE_DELIVERY_NOT_INFERRED"
        elif kind == "runtime_extra_component":
            direct = row_id in extra_sources
            source_asset = extra_sources.get(row_id)
            source_state = "BOUND_TO_RELEASE_SOURCE_ARCHIVE" if source_asset else "UNBOUND"
        else:
            raise ValueError("Unsupported component kind: " + str(kind))

        override = overrides.get(row_id)
        identity_state = "DIRECT_EVIDENCE" if direct else (
            "EXPLICIT_PARENT_OR_UPSTREAM_MAPPING" if override else "MISSING"
        )
        if identity_state != "MISSING":
            identity_closed += 1
        if source_state == "BOUND_TO_RELEASE_SOURCE_ARCHIVE":
            source_bound += 1
        reconciled.append({
            "id": row_id,
            "kind": kind,
            "license_identity_state": identity_state,
            "license_identity_override": override,
            "release_source_state": source_state,
            "release_source_asset": source_asset,
            "obligation_state": "FAMILY_POLICY_RECORDED; DISTRIBUTOR_ATTESTATION_REQUIRED",
        })

    missing_identity = len(rows) - identity_closed
    unbound_copyleft_runtime = [
        row["id"] for row in reconciled
        if row["kind"] != "android_maven_component"
        and row["release_source_state"] == "UNBOUND"
    ]
    if missing_identity or unbound_copyleft_runtime:
        outcome = "BLOCKED_EVIDENCE_GAPS"
    elif policy["distributor_attestation"]["state"] != "ACCEPTED":
        outcome = "ENGINEERING_EVIDENCE_RECONCILED_DISTRIBUTOR_ATTESTATION_REQUIRED"
    else:
        outcome = "ENGINEERING_EVIDENCE_RECONCILED"

    return {
        "schema": 2,
        "release": policy["release"],
        "inputs": {
            "ledger_sha256": ledger_sha256,
            "policy_id": policy["policy_id"],
            "sha256_manifest_entries": len(sums),
        },
        "counts": {
            "component_rows": len(rows),
            "license_identity_closed": identity_closed,
            "license_identity_missing": missing_identity,
            "runtime_and_extra_source_rows_bound": source_bound,
            "row_level_legal_conclusions": 0,
            "release_level_distributor_decisions_open": (
                0 if policy["distributor_attestation"]["state"] == "ACCEPTED" else 1
            ),
        },
        "release_level_findings": policy["release_level_findings"],
        "distributor_attestation": policy["distributor_attestation"],
        "components": reconciled,
        "outcome": outcome,
        "boundary": (
            "This report reconciles identity, inventory and delivery evidence. It records no "
            "component-level legal conclusion and is not legal advice or a compliance certificate."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--policy", required=True, type=Path)
    parser.add_argument("--sha256s", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("Refusing to overwrite a reconciliation report")
    result = reconcile(load(args.ledger), load(args.policy), load_sha256s(args.sha256s), digest(args.ledger))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as stream:
        json.dump(result, stream, indent=2, sort_keys=True)
        stream.write("\n")
    print(json.dumps({"counts": result["counts"], "outcome": result["outcome"], "output": str(args.output)}))


if __name__ == "__main__":
    main()

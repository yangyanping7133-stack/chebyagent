#!/usr/bin/env python3
"""Fail-closed validation for Turkey Trivy scan, archive, and DB evidence."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tarfile
from uuid import UUID


TRIVY_UPSTREAM_VERSION = "0.72.0"
TRIVY_CUSTOM_VERSION = "0.72.0-cheby.1"
REVIEWED_TRIVY_VERSIONS = {TRIVY_UPSTREAM_VERSION, TRIVY_CUSTOM_VERSION}
OFFICIAL_TRIVY_REFERENCE = (
    "ghcr.io/aquasecurity/trivy:0.72.0@"
    "sha256:cffe3f5161a47a6823fbd23d985795b3ed72a4c806da4c4df16266c02accdd6f"
)
SCANNER_BINARY_SHA256 = (
    "30329a9bfa5c4b29e7f0b3552ae7430ef011b73963033e9b028e2003bcd08a39"
)
SCANNER_CA_BUNDLE_SHA256 = (
    "b8d837841b88bfaa1a0fa827cbca8e2576418dd47c9fc4bb7f1f9d89c83111b9"
)
SCANNER_LABELS = {
    "io.chebycodex.go": "1.26.5",
    "io.chebycodex.trivy.module-graph.sha256": (
        "0f5e6b935f789441b411705543bed99ef41ca57cd10adea7c6a23dd581400767"
    ),
    "io.chebycodex.trivy.oras-go": "v2.6.2",
    "io.chebycodex.trivy.source.sha256": (
        "5a922c388846d11345ce8283e4373be312458f002abc667c3cd1f77c43163725"
    ),
    "org.opencontainers.image.revision": (
        "8a32853686209a428179bb3a1688802b25691564"
    ),
    "org.opencontainers.image.source": "https://github.com/aquasecurity/trivy",
    "org.opencontainers.image.version": TRIVY_CUSTOM_VERSION,
}
COSIGN_BINARY_SHA256 = (
    "7ba7d877672635f2d7e537ca3d20e751c088d86265c0f8bc6698d0084899c0af"
)
COSIGN_LABELS = {
    "io.chebycodex.cosign.module-graph.sha256": (
        "6e7e69a8924042ea95a0f9ffc6956772581db453deb3512ddf7e844090e44547"
    ),
    "io.chebycodex.cosign.source.sha256": (
        "566154a32bd9fb05b6893a4bf5fac57c3c92b5be36d5d177b5e3cc91e1dfa438"
    ),
    "io.chebycodex.go": "1.26.5",
    "org.opencontainers.image.revision": (
        "193d2153431f8bb0d945a4c1ee721872f73add67"
    ),
    "org.opencontainers.image.source": "https://github.com/sigstore/cosign",
    "org.opencontainers.image.version": "3.1.2-cheby.1",
}
MINIMAL_TOOL_ENV = ["PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"]
MINIMAL_TOOL_WORKING_DIR = "/"
REPORT_SCHEMA_VERSION = 2
MAX_JSON_EVIDENCE_BYTES = 256 * 1024 * 1024
IMAGE_ID_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
CANONICAL_HEX_PATTERN = re.compile(r"[0-9a-f]{64}")
CANONICAL_BLOB_PATH_PATTERN = re.compile(r"blobs/sha256/[0-9a-f]{64}")
GO_MODULE_PATH_ELEMENT_PATTERN = re.compile(r"[A-Za-z0-9._~-]+")
GO_MODULE_FIRST_ELEMENT_PATTERN = re.compile(
    r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?"
)
GO_MODULE_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}
GO_MODULE_WINDOWS_SHORT_NAME_PATTERN = re.compile(r"~[0-9]+$", re.IGNORECASE)
RFC3339_PATTERN = re.compile(
    r"(?P<year>[0-9]{4})-(?P<month>[0-9]{2})-(?P<day>[0-9]{2})"
    r"T(?P<hour>[0-9]{2}):(?P<minute>[0-9]{2}):(?P<second>[0-9]{2})"
    r"(?:\.(?P<fraction>[0-9]{1,9}))?"
    r"(?P<zone>Z|(?P<offset_sign>[+-])(?P<offset_hour>[0-9]{2}):"
    r"(?P<offset_minute>[0-9]{2}))"
)
REPORT_KEYS = {
    "SchemaVersion",
    "Trivy",
    "ReportID",
    "CreatedAt",
    "ArtifactID",
    "ArtifactName",
    "ArtifactType",
    "Metadata",
    "Results",
}
RESULT_KEYS = {
    "Target",
    "Class",
    "Type",
    "Packages",
    "Vulnerabilities",
    "MisconfSummary",
    "Misconfigurations",
    "Secrets",
    "Licenses",
    "CustomResources",
    "ExperimentalModifiedFindings",
}
SEVERITIES = {"UNKNOWN", "LOW", "MEDIUM", "HIGH", "CRITICAL"}
VULNERABILITY_STATUSES = {
    "unknown",
    "not_affected",
    "affected",
    "fixed",
    "under_investigation",
    "will_not_fix",
    "fix_deferred",
    "end_of_life",
}
# Trivy v0.72.0 pkg/fanal/types/const.go target types reachable by the
# vulnerability scanner.  Class/type pairs outside these pinned sets are not
# part of the reviewed vuln,secret invocation.
OS_PACKAGE_TYPES = {
    "alma",
    "alpine",
    "amazon",
    "azurelinux",
    "bottlerocket",
    "cbl-mariner",
    "centos",
    "centos-stream",
    "chainguard",
    "coreos",
    "debian",
    "echo",
    "fedora",
    "minimos",
    "opensuse",
    "opensuse-leap",
    "opensuse-tumbleweed",
    "oracle",
    "photon",
    "redhat",
    "rocky",
    "slem",
    "sles",
    "ubuntu",
    "wolfi",
}
LANGUAGE_PACKAGE_TYPES = {
    "bundler",
    "gemspec",
    "cargo",
    "composer",
    "composer-vendor",
    "npm",
    "bun",
    "nuget",
    "dotnet-core",
    "packages-props",
    "pip",
    "pipenv",
    "poetry",
    "uv",
    "pylock",
    "conda-pkg",
    "conda-environment",
    "python-pkg",
    "node-pkg",
    "yarn",
    "pnpm",
    "jar",
    "pom",
    "gradle",
    "sbt",
    "gobinary",
    "gomod",
    "javascript",
    "rustbinary",
    "conan",
    "cocoapods",
    "swift",
    "pub",
    "hex",
    "bitnami",
    "julia",
    "kubernetes",
    "eks",
    "gke",
    "aks",
    "rke",
    "ocp",
}
UNREVIEWED_LIST_FINDING_FIELDS = {
    "Misconfigurations",
    "Licenses",
    "CustomResources",
    "ExperimentalModifiedFindings",
}
PACKAGE_COMMON_KEYS = {
    "Name",
    "Version",
    "ID",
    "Identifier",
    "Layer",
    "AnalyzedBy",
}
OS_PACKAGE_KEYS = PACKAGE_COMMON_KEYS | {
    "Arch",
    "Digest",
    "DependsOn",
    "InstalledFiles",
    "Licenses",
    "Maintainer",
    "SrcName",
    "SrcVersion",
}
LANGUAGE_PACKAGE_KEYS = PACKAGE_COMMON_KEYS | {
    "FilePath",
    "Licenses",
    "DependsOn",
    "Relationship",
}
PACKAGE_REQUIRED_KEYS = {"Name", "Version", "Identifier", "Layer", "AnalyzedBy"}
VERSIONLESS_GO_ROOT_KEYS = {
    "Name",
    "ID",
    "Identifier",
    "Layer",
    "AnalyzedBy",
    "DependsOn",
    "Relationship",
}
MAX_PACKAGES_PER_RESULT = 4096
MAX_PACKAGES_PER_REPORT = 32_768
MAX_PACKAGE_STRING_BYTES = 4096
MAX_PACKAGE_NESTED_ITEMS_PER_REPORT = 1_000_000
PACKAGE_LIST_LIMITS = {
    "DependsOn": 4096,
    "InstalledFiles": 8192,
    "Licenses": 256,
}
PACKAGE_RELATIONSHIPS = {"root", "direct"}
VULNERABILITY_KEYS = {
    "VulnerabilityID",
    "VendorIDs",
    "PkgID",
    "PkgName",
    "PkgPath",
    "PkgIdentifier",
    "InstalledVersion",
    "FixedVersion",
    "Status",
    "Layer",
    "SeveritySource",
    "PrimaryURL",
    "DataSource",
    "Fingerprint",
    "Custom",
    "Title",
    "Description",
    "Severity",
    "CweIDs",
    "VendorSeverity",
    "CVSS",
    "References",
    "PublishedDate",
    "LastModifiedDate",
}
SECRET_KEYS = {
    "RuleID",
    "Category",
    "Severity",
    "Title",
    "StartLine",
    "EndLine",
    "Code",
    "Match",
    "Layer",
    "Offset",
}
LAYER_KEYS = {"Size", "Digest", "DiffID", "CreatedBy"}
CVSS_KEYS = {"V2Vector", "V3Vector", "V40Vector", "V2Score", "V3Score", "V40Score"}
CODE_LINE_KEYS = {
    "Number",
    "Content",
    "IsCause",
    "Annotation",
    "Truncated",
    "Highlighted",
    "FirstCause",
    "LastCause",
}
MAX_DOCKER_ARCHIVE_BYTES = 8 * 1024 * 1024 * 1024
MAX_DOCKER_LAYER_BYTES = 2 * 1024 * 1024 * 1024
MAX_DOCKER_TOTAL_LAYER_BYTES = 4 * 1024 * 1024 * 1024
MAX_DOCKER_ARCHIVE_MEMBERS = 10_000
MAX_DATABASE_CACHE_ENTRIES = 3
MAX_TRIVY_DATABASE_BYTES = 1280 * 1024 * 1024
MAX_DOCKER_LAYERS = 256
DOCKER_MANIFEST_BASE_KEYS = {"Config", "RepoTags", "Layers"}
DOCKER_LAYER_SOURCE_KEYS = {"digest", "mediaType", "size"}
DOCKER_LAYER_MEDIA_TYPE = "application/vnd.oci.image.layer.v1.tar"
OCI_LAYOUT_VERSION = "1.0.0"
OCI_INDEX_MEDIA_TYPE = "application/vnd.oci.image.index.v1+json"
OCI_MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"
OCI_CONFIG_MEDIA_TYPE = "application/vnd.oci.image.config.v1+json"
MAX_OCI_LAYOUT_BYTES = 1024
MAX_OCI_INDEX_BYTES = 1024 * 1024
MAX_OCI_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_DOCKER_HISTORY_BYTES = 1024 * 1024
HYBRID_REQUIRED_DIRECTORIES = {"blobs", "blobs/sha256"}
HYBRID_REQUIRED_NONBLOB_FILES = {"manifest.json", "index.json", "oci-layout"}


class DuplicateJSONKeyError(ValueError):
    """Raised when untrusted JSON relies on last-key-wins semantics."""


def reject_duplicate_object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    document: dict[str, object] = {}
    for key, value in pairs:
        if key in document:
            raise DuplicateJSONKeyError(f"duplicate JSON object key: {key}")
        document[key] = value
    return document


def strict_json_loads(document: str | bytes) -> object:
    return json.loads(document, object_pairs_hook=reject_duplicate_object_pairs)


def strict_json_load(handle: object) -> object:
    return json.load(handle, object_pairs_hook=reject_duplicate_object_pairs)


def parse_rfc3339(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp is not strict RFC 3339")
    match = RFC3339_PATTERN.fullmatch(value)
    if match is None:
        raise ValueError("timestamp is not strict RFC 3339")
    fraction = match.group("fraction") or ""
    # datetime stores microseconds; truncating the already bounded Go
    # nanosecond fraction loses at most 999 ns and preserves prior semantics.
    microsecond = int((fraction + "000000")[:6]) if fraction else 0
    zone = match.group("zone")
    if zone == "Z":
        parsed_timezone = timezone.utc
    else:
        offset_hour = int(match.group("offset_hour"))
        offset_minute = int(match.group("offset_minute"))
        if offset_hour > 23 or offset_minute > 59:
            raise ValueError("timestamp timezone offset is invalid")
        offset = timedelta(hours=offset_hour, minutes=offset_minute)
        if match.group("offset_sign") == "-":
            if offset == timedelta(0):
                # RFC 3339 assigns -00:00 an unknown-local-offset meaning that
                # datetime cannot preserve, so reject instead of normalizing it.
                raise ValueError("timestamp has an unknown local offset")
            offset = -offset
        parsed_timezone = timezone(offset)
    parsed = datetime(
        int(match.group("year")),
        int(match.group("month")),
        int(match.group("day")),
        int(match.group("hour")),
        int(match.group("minute")),
        int(match.group("second")),
        microsecond,
        parsed_timezone,
    )
    try:
        parsed.astimezone(timezone.utc)
    except OverflowError as error:
        raise ValueError("timestamp instant is outside the UTC datetime range") from error
    return parsed


def require_fresh_timestamp(
    value: object,
    *,
    now: datetime,
    maximum_age_seconds: int,
    future_tolerance_seconds: int = 300,
) -> datetime:
    parsed = parse_rfc3339(value).astimezone(timezone.utc)
    current = now.astimezone(timezone.utc)
    age = current - parsed
    if age.total_seconds() < -future_tolerance_seconds:
        raise RuntimeError("Trivy evidence timestamp is unacceptably in the future")
    if age.total_seconds() > maximum_age_seconds:
        raise RuntimeError("Trivy evidence timestamp is stale")
    return parsed


def require_exact_string(document: dict[str, object], key: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"Trivy {key} must be a nonempty string")
    return value


def require_plain_integer(value: object, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise RuntimeError("Trivy integer field is malformed")
    return value


def validate_string_list(value: object, field: str, label: str) -> None:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise RuntimeError(f"Trivy {field} is malformed for {label}")


def validate_layer(value: object, label: str) -> None:
    if not isinstance(value, dict) or not value or not set(value).issubset(LAYER_KEYS):
        raise RuntimeError(f"Trivy layer is malformed for {label}")
    if "Size" in value:
        require_plain_integer(value["Size"])
    for field in ("Digest", "DiffID", "CreatedBy"):
        if field in value and (not isinstance(value[field], str) or not value[field]):
            raise RuntimeError(f"Trivy layer {field} is malformed for {label}")


def validate_package_identifier(value: object, label: str) -> None:
    if (
        not isinstance(value, dict)
        or not value
        or not set(value).issubset({"PURL", "UID", "BOMRef"})
        or any(
            not isinstance(item, str)
            or not item
            or len(item.encode("utf-8")) > MAX_PACKAGE_STRING_BYTES
            for item in value.values()
        )
    ):
        raise RuntimeError(f"Trivy package identifier is malformed for {label}")


def validate_package_string(value: object, field: str, label: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > MAX_PACKAGE_STRING_BYTES
    ):
        raise RuntimeError(f"Trivy package {field} is malformed for {label}")


def is_downloadable_go_module_path(value: object) -> bool:
    """Apply Go's downloadable module rules with a DNS-label first element."""
    if (
        not isinstance(value, str)
        or not value
        or value.startswith("/")
        or value.endswith("/")
    ):
        return False
    elements = value.split("/")
    for element in elements:
        if (
            GO_MODULE_PATH_ELEMENT_PATTERN.fullmatch(element) is None
            or element.startswith(".")
            or element.endswith(".")
        ):
            return False
        file_prefix = element.split(".", maxsplit=1)[0]
        if (
            file_prefix.upper() in GO_MODULE_WINDOWS_RESERVED_NAMES
            or GO_MODULE_WINDOWS_SHORT_NAME_PATTERN.search(file_prefix) is not None
        ):
            return False
    first_element = elements[0]
    if not (
        "." in first_element
        and not first_element.startswith("-")
        and GO_MODULE_FIRST_ELEMENT_PATTERN.fullmatch(first_element) is not None
    ):
        return False
    if value.startswith("gopkg.in/"):
        suffix = re.search(r"\.v(0|[1-9][0-9]*)(-unstable)?$", value)
        return suffix is not None and not (
            suffix.group(1) == "0" and suffix.group(2) is not None
        )
    suffix = re.search(r"/v([0-9.]+)$", value)
    if suffix is None:
        return True
    major = suffix.group(1)
    return "." not in major and not major.startswith("0") and major != "1"


def validate_package_inventory_item(
    value: object, result_class: str, result_type: str, label: str
) -> tuple[int, bool]:
    allowed_keys = OS_PACKAGE_KEYS if result_class == "os-pkgs" else LANGUAGE_PACKAGE_KEYS
    required_keys = set(PACKAGE_REQUIRED_KEYS)
    versionless_go_root = (
        result_type == "gobinary"
        and isinstance(value, dict)
        and "Version" not in value
    )
    if versionless_go_root:
        required_keys.remove("Version")
        required_keys.update({"ID", "DependsOn", "Relationship"})
    if result_class == "os-pkgs":
        required_keys.update({"Arch", "Digest", "ID"})
    elif result_type == "node-pkg":
        required_keys.update({"FilePath", "ID", "Licenses"})
    elif result_type == "python-pkg":
        required_keys.update({"FilePath", "Licenses"})
    elif result_type in {"rustbinary", "gobinary"}:
        required_keys.add("ID")
    if (
        not isinstance(value, dict)
        or not required_keys.issubset(value)
        or not set(value).issubset(allowed_keys)
        or (versionless_go_root and set(value) != VERSIONLESS_GO_ROOT_KEYS)
    ):
        raise RuntimeError(f"Trivy package inventory entry is malformed for {label}")
    scalar_fields = {"Name", "Version", "ID", "AnalyzedBy"} | (
        {"Arch", "Digest", "Maintainer", "SrcName", "SrcVersion"}
        if result_class == "os-pkgs"
        else {"FilePath", "Relationship"}
    )
    for field in scalar_fields:
        if field in value:
            validate_package_string(value[field], field, label)
    if "Identifier" in value:
        validate_package_identifier(value["Identifier"], label)
        if set(value["Identifier"]) != {"PURL", "UID"}:
            raise RuntimeError(f"Trivy package Identifier shape is malformed for {label}")
    if versionless_go_root and (
        value["Relationship"] != "root"
        or value["AnalyzedBy"] != "gobinary"
        or value["ID"] != value["Name"]
        or not is_downloadable_go_module_path(value["Name"])
        or value["Identifier"]["PURL"] != f"pkg:golang/{value['Name']}"
        or not isinstance(value["DependsOn"], list)
        or not value["DependsOn"]
        or value["ID"] in value["DependsOn"]
    ):
        raise RuntimeError(f"Trivy versionless Go root package is malformed for {label}")
    if "Layer" in value:
        validate_layer(value["Layer"], label)
        if set(value["Layer"]) != {"DiffID"}:
            raise RuntimeError(f"Trivy package Layer shape is malformed for {label}")
        validate_package_string(value["Layer"]["DiffID"], "Layer.DiffID", label)
    if "Relationship" in value and value["Relationship"] not in PACKAGE_RELATIONSHIPS:
        raise RuntimeError(f"Trivy package Relationship is malformed for {label}")
    nested_items = 0
    for field, maximum_items in PACKAGE_LIST_LIMITS.items():
        if field not in value:
            continue
        items = value[field]
        if (
            not isinstance(items, list)
            or len(items) > maximum_items
            or any(
                not isinstance(item, str)
                or not item
                or len(item.encode("utf-8")) > MAX_PACKAGE_STRING_BYTES
                for item in items
            )
        ):
            raise RuntimeError(f"Trivy package {field} is malformed for {label}")
        nested_items += len(items)
    return nested_items, versionless_go_root


def validate_vulnerability(finding: object, label: str) -> str:
    required = {"VulnerabilityID", "PkgName", "InstalledVersion", "Severity"}
    if (
        not isinstance(finding, dict)
        or not required.issubset(finding)
        or not set(finding).issubset(VULNERABILITY_KEYS)
    ):
        raise RuntimeError(f"Trivy vulnerability is malformed for {label}")
    severity = finding.get("Severity")
    if not isinstance(severity, str) or severity not in SEVERITIES:
        raise RuntimeError(f"Trivy vulnerability severity is malformed for {label}")
    for field in (
        "VulnerabilityID",
        "PkgID",
        "PkgName",
        "PkgPath",
        "InstalledVersion",
        "FixedVersion",
        "SeveritySource",
        "PrimaryURL",
        "Fingerprint",
        "Title",
        "Description",
    ):
        if field in finding and (not isinstance(finding[field], str) or not finding[field]):
            raise RuntimeError(f"Trivy vulnerability {field} is malformed for {label}")
    for field in ("VendorIDs", "CweIDs", "References"):
        if field in finding:
            validate_string_list(finding[field], field, label)
    if "Status" in finding and finding["Status"] not in VULNERABILITY_STATUSES:
        raise RuntimeError(f"Trivy vulnerability status is malformed for {label}")
    if "PkgIdentifier" in finding:
        validate_package_identifier(finding["PkgIdentifier"], label)
    if "Layer" in finding:
        validate_layer(finding["Layer"], label)
    if "DataSource" in finding:
        source = finding["DataSource"]
        if (
            not isinstance(source, dict)
            or not source
            or not set(source).issubset({"ID", "Name", "URL", "BaseID"})
            or any(not isinstance(value, str) or not value for value in source.values())
        ):
            raise RuntimeError(f"Trivy vulnerability data source is malformed for {label}")
    if "VendorSeverity" in finding:
        vendor_severity = finding["VendorSeverity"]
        if not isinstance(vendor_severity, dict) or not vendor_severity:
            raise RuntimeError(f"Trivy vendor severity is malformed for {label}")
        for source, value in vendor_severity.items():
            if not isinstance(source, str) or not source:
                raise RuntimeError(f"Trivy vendor severity source is malformed for {label}")
            if isinstance(value, bool) or not isinstance(value, int) or value not in range(5):
                raise RuntimeError(f"Trivy vendor severity value is malformed for {label}")
    if "CVSS" in finding:
        cvss = finding["CVSS"]
        if not isinstance(cvss, dict) or not cvss:
            raise RuntimeError(f"Trivy CVSS map is malformed for {label}")
        for source, metrics in cvss.items():
            if (
                not isinstance(source, str)
                or not source
                or not isinstance(metrics, dict)
                or not metrics
                or not set(metrics).issubset(CVSS_KEYS)
            ):
                raise RuntimeError(f"Trivy CVSS entry is malformed for {label}")
            for key, value in metrics.items():
                if key.endswith("Vector"):
                    if not isinstance(value, str) or not value:
                        raise RuntimeError(f"Trivy CVSS vector is malformed for {label}")
                elif isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 10:
                    raise RuntimeError(f"Trivy CVSS score is malformed for {label}")
    for field in ("PublishedDate", "LastModifiedDate"):
        if field in finding:
            try:
                parse_rfc3339(finding[field])
            except (TypeError, ValueError) as error:
                raise RuntimeError(f"Trivy vulnerability date is malformed for {label}") from error
    if "Custom" in finding and finding["Custom"] is not None:
        raise RuntimeError(f"Trivy vulnerability custom extension is unreviewed for {label}")
    return severity


def validate_secret(finding: object, label: str) -> None:
    required = {
        "RuleID",
        "Category",
        "Severity",
        "Title",
        "StartLine",
        "EndLine",
        "Code",
        "Match",
    }
    if (
        not isinstance(finding, dict)
        or not required.issubset(finding)
        or not set(finding).issubset(SECRET_KEYS)
    ):
        raise RuntimeError(f"Trivy secret finding is malformed for {label}")
    for field in ("RuleID", "Category", "Title", "Match"):
        if not isinstance(finding[field], str) or not finding[field]:
            raise RuntimeError(f"Trivy secret {field} is malformed for {label}")
    severity = finding["Severity"]
    if not isinstance(severity, str) or severity not in SEVERITIES:
        raise RuntimeError(f"Trivy secret severity is malformed for {label}")
    start = require_plain_integer(finding["StartLine"], minimum=1)
    end = require_plain_integer(finding["EndLine"], minimum=1)
    if end < start:
        raise RuntimeError(f"Trivy secret line range is malformed for {label}")
    if "Offset" in finding:
        require_plain_integer(finding["Offset"])
    if "Layer" in finding:
        validate_layer(finding["Layer"], label)
    code = finding["Code"]
    if not isinstance(code, dict) or set(code) != {"Lines"} or not isinstance(code["Lines"], list):
        raise RuntimeError(f"Trivy secret code is malformed for {label}")
    required_line_keys = CODE_LINE_KEYS - {"Highlighted"}
    for line in code["Lines"]:
        if (
            not isinstance(line, dict)
            or not required_line_keys.issubset(line)
            or not set(line).issubset(CODE_LINE_KEYS)
        ):
            raise RuntimeError(f"Trivy secret code line is malformed for {label}")
        require_plain_integer(line["Number"], minimum=1)
        for field in ("Content", "Annotation"):
            if not isinstance(line[field], str):
                raise RuntimeError(f"Trivy secret code text is malformed for {label}")
        if "Highlighted" in line and not isinstance(line["Highlighted"], str):
            raise RuntimeError(f"Trivy secret code highlight is malformed for {label}")
        for field in ("IsCause", "Truncated", "FirstCause", "LastCause"):
            if not isinstance(line[field], bool):
                raise RuntimeError(f"Trivy secret code flag is malformed for {label}")


def validate_result_class_and_type(result: dict[str, object], label: str) -> str:
    result_class = require_exact_string(result, "Class")
    if result_class == "os-pkgs":
        result_type = require_exact_string(result, "Type")
        if result_type not in OS_PACKAGE_TYPES:
            raise RuntimeError(f"Trivy OS package type is unreviewed for {label}")
    elif result_class == "lang-pkgs":
        result_type = require_exact_string(result, "Type")
        if result_type not in LANGUAGE_PACKAGE_TYPES:
            raise RuntimeError(f"Trivy language package type is unreviewed for {label}")
    elif result_class == "secret":
        if "Type" in result:
            raise RuntimeError(f"Trivy secret result has an unexpected type for {label}")
    else:
        raise RuntimeError(f"Trivy result class is unreviewed for {label}")
    return result_class


def validate_findings(
    results: list[object],
    label: str,
    *,
    expected_blocking_vulnerabilities: int = 0,
    expected_secrets: int = 0,
) -> tuple[tuple[str, str, str, str | None, str], ...]:
    blocking_vulnerabilities = 0
    blocking_finding_tuples: list[tuple[str, str, str, str | None, str]] = []
    secrets = 0
    total_packages = 0
    total_package_nested_items = 0
    for result in results:
        if not isinstance(result, dict) or not set(result).issubset(RESULT_KEYS):
            raise RuntimeError(f"Trivy result entry is malformed for {label}")
        require_exact_string(result, "Target")
        result_class = validate_result_class_and_type(result, label)
        packages = result.get("Packages", [])
        if not isinstance(packages, list):
            raise RuntimeError(f"Trivy Packages must be a list for {label}")
        if len(packages) > MAX_PACKAGES_PER_RESULT:
            raise RuntimeError(f"Trivy Packages exceeds the per-result limit for {label}")
        total_packages += len(packages)
        if total_packages > MAX_PACKAGES_PER_REPORT:
            raise RuntimeError(f"Trivy Packages exceeds the report limit for {label}")
        if packages and result_class not in {"os-pkgs", "lang-pkgs"}:
            raise RuntimeError(f"Trivy Packages is attached to the wrong class for {label}")
        result_type = result.get("Type")
        versionless_go_roots = 0
        for package in packages:
            nested_items, versionless_go_root = validate_package_inventory_item(
                package, result_class, result_type, label
            )
            total_package_nested_items += nested_items
            if total_package_nested_items > MAX_PACKAGE_NESTED_ITEMS_PER_REPORT:
                raise RuntimeError(
                    f"Trivy package nested inventory exceeds the report limit for {label}"
                )
            if versionless_go_root:
                versionless_go_roots += 1
                if versionless_go_roots > 1:
                    raise RuntimeError(
                        f"Trivy gobinary result has multiple versionless roots for {label}"
                    )
        for field in UNREVIEWED_LIST_FINDING_FIELDS:
            if field in result and result[field] != []:
                raise RuntimeError(f"Trivy {field} contains unreviewed findings for {label}")
        if "MisconfSummary" in result and result["MisconfSummary"] is not None:
            raise RuntimeError(f"Trivy MisconfSummary contains unreviewed findings for {label}")
        for field in ("Vulnerabilities", "Secrets"):
            if field in result and not isinstance(result[field], list):
                raise RuntimeError(f"Trivy {field} must be a list for {label}")
        vulnerabilities = result.get("Vulnerabilities", [])
        for finding in vulnerabilities:
            if result_class not in {"os-pkgs", "lang-pkgs"}:
                raise RuntimeError(f"Trivy vulnerability is attached to the wrong class for {label}")
            severity = validate_vulnerability(finding, label)
            if severity in {"HIGH", "CRITICAL"}:
                blocking_vulnerabilities += 1
                blocking_finding_tuples.append(
                    (
                        finding["VulnerabilityID"],
                        finding["PkgName"],
                        finding["InstalledVersion"],
                        finding.get("FixedVersion"),
                        severity,
                    )
                )
        secret_findings = result.get("Secrets", [])
        if secret_findings and result_class != "secret":
            raise RuntimeError(f"Trivy secret is attached to the wrong class for {label}")
        for finding in secret_findings:
            validate_secret(finding, label)
        secrets += len(secret_findings)
    if (
        blocking_vulnerabilities != expected_blocking_vulnerabilities
        or secrets != expected_secrets
    ):
        raise RuntimeError(
            f"{label} has {blocking_vulnerabilities} HIGH/CRITICAL vulnerabilities "
            f"and {secrets} secret findings"
        )
    return tuple(
        sorted(
            blocking_finding_tuples,
            key=lambda finding: (*finding[:3], finding[3] or "", finding[4]),
        )
    )


def validate_scan_report(
    document: object,
    label: str,
    *,
    expected_artifact_name: str,
    expected_artifact_type: str,
    expected_target: str | None = None,
    now: datetime | None = None,
    maximum_report_age_seconds: int = 3600,
    expected_scanner_version: str = TRIVY_UPSTREAM_VERSION,
    expected_blocking_vulnerabilities: int = 0,
    expected_secrets: int = 0,
) -> tuple[tuple[str, str, str, str | None, str], ...]:
    if not isinstance(document, dict) or not set(document).issubset(REPORT_KEYS):
        raise RuntimeError(f"unexpected Trivy report shape for {label}")
    schema = document.get("SchemaVersion")
    if isinstance(schema, bool) or schema != REPORT_SCHEMA_VERSION:
        raise RuntimeError(f"unsupported Trivy report schema for {label}")
    required = {
        "SchemaVersion",
        "Trivy",
        "ReportID",
        "CreatedAt",
        "ArtifactName",
        "ArtifactType",
    }
    if not required.issubset(document):
        raise RuntimeError(f"incomplete Trivy result for {label}")
    trivy = document["Trivy"]
    if not isinstance(trivy, dict) or set(trivy) != {"Version"}:
        raise RuntimeError(f"Trivy execution metadata is malformed for {label}")
    if (
        expected_scanner_version not in REVIEWED_TRIVY_VERSIONS
        or trivy["Version"] != expected_scanner_version
        or not isinstance(trivy["Version"], str)
    ):
        raise RuntimeError(f"wrong Trivy version for {label}")
    try:
        report_id = document["ReportID"]
        if not isinstance(report_id, str):
            raise ValueError("report ID must be a string")
        UUID(report_id)
        require_fresh_timestamp(
            document["CreatedAt"],
            now=now or datetime.now(timezone.utc),
            maximum_age_seconds=maximum_report_age_seconds,
        )
    except (TypeError, ValueError) as error:
        raise RuntimeError(f"Trivy execution identity is malformed for {label}") from error
    artifact_name = require_exact_string(document, "ArtifactName")
    artifact_type = require_exact_string(document, "ArtifactType")
    if artifact_name != expected_artifact_name or artifact_type != expected_artifact_type:
        raise RuntimeError(f"Trivy report target envelope differs for {label}")

    results = document.get("Results")
    if expected_target is None:
        if expected_artifact_type != "filesystem":
            raise RuntimeError(f"filesystem target binding is incomplete for {label}")
        if "ArtifactID" in document or "Metadata" in document:
            raise RuntimeError(f"filesystem report has unexpected image identity for {label}")
        # Trivy 0.72 omits Results for a clean filesystem with no recognized
        # package or secret target. The exact, fresh execution envelope above
        # binds the successful invocation to the reviewed /scan mount.
        if results is None:
            if expected_blocking_vulnerabilities or expected_secrets:
                raise RuntimeError(f"expected Trivy findings are unavailable for {label}")
            return ()
    else:
        if IMAGE_ID_PATTERN.fullmatch(expected_target) is None:
            raise RuntimeError(f"expected immutable target is malformed for {label}")
        if document.get("ArtifactID") != expected_target:
            raise RuntimeError(f"Trivy artifact ID differs from the immutable target for {label}")
        metadata = document.get("Metadata")
        if not isinstance(metadata, dict) or metadata.get("ImageID") != expected_target:
            raise RuntimeError(f"Trivy image metadata differs from the immutable target for {label}")
        if not isinstance(results, list) or not results:
            raise RuntimeError(f"immutable image results are empty for {label}")

    if not isinstance(results, list):
        raise RuntimeError(f"Trivy results are unavailable for {label}")
    return validate_findings(
        results,
        label,
        expected_blocking_vulnerabilities=expected_blocking_vulnerabilities,
        expected_secrets=expected_secrets,
    )


def validate_version_report(
    document: object,
    now: datetime | None = None,
    maximum_age_hours: int = 48,
    expected_scanner_version: str = TRIVY_UPSTREAM_VERSION,
) -> tuple[str, str, str, str, int]:
    allowed_keys = {"Version", "VulnerabilityDB", "JavaDB", "CheckBundle"}
    if not isinstance(document, dict) or not set(document).issubset(allowed_keys):
        raise RuntimeError("scanner version report has an unexpected shape")
    if (
        expected_scanner_version not in REVIEWED_TRIVY_VERSIONS
        or document.get("Version") != expected_scanner_version
        or not isinstance(document.get("Version"), str)
    ):
        raise RuntimeError(
            f"scanner version is not the reviewed {expected_scanner_version}"
        )
    database = document.get("VulnerabilityDB")
    required_database_keys = {"Version", "UpdatedAt", "NextUpdate", "DownloadedAt"}
    if not isinstance(database, dict) or set(database) != required_database_keys:
        raise RuntimeError("vulnerability DB metadata is unavailable or malformed")
    version = database.get("Version")
    if isinstance(version, bool) or version != 2:
        raise RuntimeError("vulnerability DB schema version is not the reviewed version")
    current = now or datetime.now(timezone.utc)
    try:
        updated = require_fresh_timestamp(
            database["UpdatedAt"],
            now=current,
            maximum_age_seconds=maximum_age_hours * 3600,
        )
        downloaded = require_fresh_timestamp(
            database["DownloadedAt"],
            now=current,
            maximum_age_seconds=maximum_age_hours * 3600,
        )
        next_update = parse_rfc3339(database["NextUpdate"]).astimezone(timezone.utc)
    except (TypeError, ValueError) as error:
        raise RuntimeError("vulnerability DB timestamp is invalid") from error
    if next_update <= updated or downloaded < updated:
        raise RuntimeError("vulnerability DB execution chronology is invalid")
    return (
        expected_scanner_version,
        updated.isoformat().replace("+00:00", "Z"),
        downloaded.isoformat().replace("+00:00", "Z"),
        next_update.isoformat().replace("+00:00", "Z"),
        version,
    )


def validate_database_cache(path: Path, expected_owner_uid: int) -> tuple[str, str]:
    root_metadata = path.lstat()
    if (
        stat.S_ISLNK(root_metadata.st_mode)
        or not stat.S_ISDIR(root_metadata.st_mode)
        or root_metadata.st_uid != expected_owner_uid
        or root_metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise RuntimeError("Trivy DB cache root is not private and owner-controlled")
    expected_directories = {"db"}
    expected_files = {"db/metadata.json", "db/trivy.db"}
    actual_directories: set[str] = set()
    actual_files: set[str] = set()
    entry_count = 0
    for directory, prefix in ((path, ""), (path / "db", "db/")):
        try:
            entries = os.scandir(directory)
        except OSError as error:
            raise RuntimeError("Trivy DB cache directory is unreadable") from error
        with entries:
            for entry in entries:
                entry_count += 1
                if entry_count > MAX_DATABASE_CACHE_ENTRIES:
                    raise RuntimeError(
                        "Trivy DB cache entry count exceeds the reviewed layout"
                    )
                relative = prefix + entry.name
                if relative not in expected_directories | expected_files:
                    raise RuntimeError("Trivy DB cache contains an unexpected path")
                metadata = entry.stat(follow_symlinks=False)
                if (
                    stat.S_ISLNK(metadata.st_mode)
                    or metadata.st_uid != expected_owner_uid
                    or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
                ):
                    raise RuntimeError("Trivy DB cache contains an uncontrolled entry")
                if stat.S_ISDIR(metadata.st_mode):
                    if relative not in expected_directories:
                        raise RuntimeError(
                            "Trivy DB cache path type differs from the reviewed layout"
                        )
                    actual_directories.add(relative)
                elif stat.S_ISREG(metadata.st_mode):
                    if relative not in expected_files:
                        raise RuntimeError(
                            "Trivy DB cache path type differs from the reviewed layout"
                        )
                    actual_files.add(relative)
                else:
                    raise RuntimeError("Trivy DB cache contains a special file")
    if actual_directories != expected_directories or actual_files != expected_files:
        raise RuntimeError("Trivy DB cache inventory differs from the reviewed layout")
    metadata_path = path / "db/metadata.json"
    database_path = path / "db/trivy.db"
    metadata_size = metadata_path.stat().st_size
    database_size = database_path.stat().st_size
    if metadata_size not in range(1, 1024 * 1024 + 1):
        raise RuntimeError("Trivy DB metadata size is malformed")
    if database_size not in range(1, MAX_TRIVY_DATABASE_BYTES + 1):
        raise RuntimeError("Trivy DB size is malformed")
    try:
        with metadata_path.open("rb") as metadata_handle:
            metadata_bytes = metadata_handle.read(1024 * 1024 + 1)
        metadata_document = strict_json_loads(metadata_bytes)
    except (json.JSONDecodeError, DuplicateJSONKeyError, UnicodeDecodeError) as error:
        raise RuntimeError("Trivy DB metadata JSON is malformed") from error
    if not isinstance(metadata_document, dict) or not metadata_document:
        raise RuntimeError("Trivy DB metadata JSON is empty")
    with metadata_path.open("rb") as metadata_handle:
        metadata_sha256 = stream_sha256(
            metadata_handle,
            maximum_bytes=metadata_size,
            expected_bytes=metadata_size,
        )
    with database_path.open("rb") as database_handle:
        database_sha256 = stream_sha256(
            database_handle,
            maximum_bytes=database_size,
            expected_bytes=database_size,
        )
    return metadata_sha256, database_sha256


def load_official_findings_lock(
    path: Path,
) -> tuple[tuple[str, str, str, str | None, str], ...]:
    document = load(path)
    if not isinstance(document, dict) or set(document) != {
        "schemaVersion",
        "targetReference",
        "blockingFindings",
    }:
        raise RuntimeError("official Trivy findings lock has an unexpected shape")
    if (
        document["schemaVersion"] != 1
        or document["targetReference"] != OFFICIAL_TRIVY_REFERENCE
        or not isinstance(document["blockingFindings"], list)
        or len(document["blockingFindings"]) != 16
    ):
        raise RuntimeError("official Trivy findings lock differs from the reviewed target")
    findings: list[tuple[str, str, str, str | None, str]] = []
    for value in document["blockingFindings"]:
        if (
            not isinstance(value, list)
            or len(value) != 5
            or any(not isinstance(item, str) or not item for item in value[:3])
            or (value[3] is not None and (not isinstance(value[3], str) or not value[3]))
            or value[4] != "HIGH"
        ):
            raise RuntimeError("official Trivy finding tuple is malformed")
        findings.append((value[0], value[1], value[2], value[3], value[4]))
    expected = tuple(
        sorted(findings, key=lambda finding: (*finding[:3], finding[3] or "", finding[4]))
    )
    if tuple(findings) != expected or len(set(findings)) != len(findings):
        raise RuntimeError("official Trivy findings lock is not sorted and unique")
    return expected


def validate_antiblindness_reports(
    official_document: object,
    custom_document: object,
    findings_lock_path: Path,
    *,
    expected_artifact_name: str,
    expected_target: str,
    now: datetime | None = None,
) -> str:
    locked_findings = load_official_findings_lock(findings_lock_path)
    common = {
        "expected_artifact_name": expected_artifact_name,
        "expected_artifact_type": "container_image",
        "expected_target": expected_target,
        "expected_blocking_vulnerabilities": len(locked_findings),
        "expected_secrets": 0,
        "now": now,
    }
    official_findings = validate_scan_report(
        official_document,
        "official-scanner-to-official-image",
        expected_scanner_version=TRIVY_UPSTREAM_VERSION,
        **common,
    )
    custom_findings = validate_scan_report(
        custom_document,
        "custom-scanner-to-official-image",
        expected_scanner_version=TRIVY_CUSTOM_VERSION,
        **common,
    )
    if official_findings != locked_findings or custom_findings != locked_findings:
        raise RuntimeError("custom Trivy anti-blindness findings differ from the exact lock")
    encoded = json.dumps(locked_findings, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def stream_sha256(
    handle: object,
    maximum_bytes: int | None = None,
    expected_bytes: int | None = None,
) -> str:
    digest = hashlib.sha256()
    total = 0
    while True:
        chunk = handle.read(1024 * 1024)
        if not chunk:
            if expected_bytes is not None and total != expected_bytes:
                raise RuntimeError("stream size changed during hashing")
            return digest.hexdigest()
        total += len(chunk)
        if maximum_bytes is not None and total > maximum_bytes:
            raise RuntimeError("stream exceeded its reviewed size while hashing")
        digest.update(chunk)


def canonical_blob_path(digest: str) -> str:
    if IMAGE_ID_PATTERN.fullmatch(digest) is None:
        raise RuntimeError("OCI descriptor digest is malformed")
    return "blobs/sha256/" + digest.removeprefix("sha256:")


def validate_annotations(value: object, label: str) -> None:
    if (
        not isinstance(value, dict)
        or any(
            not isinstance(key, str)
            or not key
            or not isinstance(item, str)
            for key, item in value.items()
        )
    ):
        raise RuntimeError(f"{label} annotations are malformed")


def validate_platform(value: object, label: str) -> None:
    allowed = {"architecture", "os", "variant", "os.version", "os.features"}
    if (
        not isinstance(value, dict)
        or not {"architecture", "os"}.issubset(value)
        or not set(value).issubset(allowed)
        or not isinstance(value.get("architecture"), str)
        or not value["architecture"]
        or value.get("os") != "linux"
    ):
        raise RuntimeError(f"{label} platform is malformed")
    for field in ("variant", "os.version"):
        if field in value and (not isinstance(value[field], str) or not value[field]):
            raise RuntimeError(f"{label} platform is malformed")
    if "os.features" in value:
        features = value["os.features"]
        if (
            not isinstance(features, list)
            or any(not isinstance(feature, str) or not feature for feature in features)
        ):
            raise RuntimeError(f"{label} platform is malformed")


def validate_oci_descriptor(
    value: object,
    *,
    expected_media_type: str,
    label: str,
    allow_platform: bool = False,
) -> tuple[str, int]:
    required = {"mediaType", "digest", "size"}
    allowed = required | {"annotations"}
    if allow_platform:
        allowed.add("platform")
    if (
        not isinstance(value, dict)
        or not required.issubset(value)
        or not set(value).issubset(allowed)
        or value.get("mediaType") != expected_media_type
        or not isinstance(value.get("digest"), str)
        or IMAGE_ID_PATTERN.fullmatch(value["digest"]) is None
        or isinstance(value.get("size"), bool)
        or not isinstance(value.get("size"), int)
        or value["size"] <= 0
        or value["size"] > MAX_DOCKER_ARCHIVE_BYTES
    ):
        raise RuntimeError(f"{label} descriptor is malformed")
    if "annotations" in value:
        validate_annotations(value["annotations"], label)
    if "platform" in value:
        validate_platform(value["platform"], label)
    return value["digest"], value["size"]


def read_archive_json(
    archive: tarfile.TarFile,
    name: str,
    *,
    maximum_size: int,
    label: str,
) -> tuple[object, bytes, tarfile.TarInfo]:
    try:
        member = archive.getmember(name)
    except KeyError as error:
        raise RuntimeError(f"{label} is missing") from error
    if not member.isreg() or member.size <= 0 or member.size > maximum_size:
        raise RuntimeError(f"{label} is malformed")
    handle = archive.extractfile(member)
    if handle is None:
        raise RuntimeError(f"{label} is unreadable")
    content = handle.read()
    try:
        document = strict_json_loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError, DuplicateJSONKeyError) as error:
        raise RuntimeError(f"{label} JSON is malformed") from error
    return document, content, member


def classify_hybrid_history_members(
    blob_members: dict[str, tarfile.TarInfo],
    known_blob_names: set[str],
    expected_count: int,
) -> set[str]:
    history_names = set(blob_members) - known_blob_names
    if len(history_names) != expected_count:
        raise RuntimeError("Docker hybrid archive has missing or extra blobs")
    for name in history_names:
        member = blob_members[name]
        if member.size <= 0 or member.size > MAX_DOCKER_HISTORY_BYTES:
            raise RuntimeError("Docker hybrid archive history size is unsafe")
    return history_names


def validate_hybrid_top_level_member_names(members: list[tarfile.TarInfo]) -> None:
    directory_names = {member.name for member in members if member.isdir()}
    nonblob_regular_names: set[str] = set()
    for member in members:
        if not member.isreg():
            continue
        pure = PurePosixPath(member.name)
        if len(pure.parts) >= 2 and pure.parts[:2] == ("blobs", "sha256"):
            if CANONICAL_BLOB_PATH_PATTERN.fullmatch(member.name) is None:
                raise RuntimeError("Docker hybrid archive has a malformed blob name")
        else:
            nonblob_regular_names.add(member.name)
    if (
        directory_names != HYBRID_REQUIRED_DIRECTORIES
        or nonblob_regular_names != HYBRID_REQUIRED_NONBLOB_FILES
    ):
        raise RuntimeError("Docker hybrid archive top-level inventory is not exact")


def validate_hybrid_member_inventory(
    members: list[tarfile.TarInfo],
    *,
    config_name: str,
    manifest_name: str,
    layers: list[str],
    history_names: set[str],
) -> None:
    regular_members = {member.name: member for member in members if member.isreg()}
    directory_names = {member.name for member in members if member.isdir()}
    expected_regular_names = (
        HYBRID_REQUIRED_NONBLOB_FILES
        | {config_name, manifest_name, *layers}
        | history_names
    )
    if (
        set(regular_members) != expected_regular_names
        or directory_names != HYBRID_REQUIRED_DIRECTORIES
    ):
        raise RuntimeError("Docker hybrid archive member inventory is not exact")
    bounded_members = (
        ("manifest.json", 1024 * 1024),
        ("oci-layout", MAX_OCI_LAYOUT_BYTES),
        ("index.json", MAX_OCI_INDEX_BYTES),
        (config_name, 16 * 1024 * 1024),
        (manifest_name, MAX_OCI_MANIFEST_BYTES),
    )
    for name, maximum_size in bounded_members:
        member = regular_members[name]
        if member.size <= 0 or member.size > maximum_size:
            raise RuntimeError("Docker hybrid archive metadata size is unsafe")
    total_layer_bytes = 0
    for layer in layers:
        member = regular_members[layer]
        if member.size <= 0 or member.size > MAX_DOCKER_LAYER_BYTES:
            raise RuntimeError("Docker hybrid archive layer size is unsafe")
        total_layer_bytes += member.size
        if total_layer_bytes > MAX_DOCKER_TOTAL_LAYER_BYTES:
            raise RuntimeError("Docker hybrid archive layer data is too large")


def validate_history_documents(
    documents: list[object],
    expected_count: int,
    expected_architecture: str,
    expected_created: str,
) -> None:
    if len(documents) != expected_count:
        raise RuntimeError("Docker hybrid archive history count differs from layers")
    ids: set[str] = set()
    parents: list[str] = []
    root_ids: list[str] = []
    final_ids: list[str] = []
    base_keys = {"container_config", "created", "id", "os"}
    for document in documents:
        if not isinstance(document, dict):
            raise RuntimeError("Docker hybrid archive history JSON is malformed")
        has_final_metadata = "architecture" in document or "config" in document
        allowed_keys = base_keys | ({"architecture", "config"} if has_final_metadata else set())
        if "parent" in document:
            allowed_keys.add("parent")
        if (
            set(document) != allowed_keys
            or document.get("os") != "linux"
            or not isinstance(document.get("id"), str)
            or CANONICAL_HEX_PATTERN.fullmatch(document["id"]) is None
            or not isinstance(document.get("created"), str)
            or not document["created"]
            or not isinstance(document.get("container_config"), dict)
        ):
            raise RuntimeError("Docker hybrid archive history JSON is malformed")
        if has_final_metadata:
            if (
                not {"architecture", "config"}.issubset(document)
                or document.get("architecture") != expected_architecture
                or document.get("created") != expected_created
                or not isinstance(document.get("config"), dict)
            ):
                raise RuntimeError("Docker hybrid archive final history is malformed")
            final_ids.append(document["id"])
        history_id = document["id"]
        if history_id in ids:
            raise RuntimeError("Docker hybrid archive history IDs are not unique")
        ids.add(history_id)
        if "parent" in document:
            parent = document["parent"]
            if (
                not isinstance(parent, str)
                or CANONICAL_HEX_PATTERN.fullmatch(parent) is None
                or parent == history_id
            ):
                raise RuntimeError("Docker hybrid archive history parent is malformed")
            parents.append(parent)
        else:
            root_ids.append(history_id)
    if (
        len(root_ids) != 1
        or len(parents) != expected_count - 1
        or len(parents) != len(set(parents))
        or not set(parents).issubset(ids)
    ):
        raise RuntimeError("Docker hybrid archive history is not one parent chain")
    child_by_parent = {
        document["parent"]: document["id"]
        for document in documents
        if isinstance(document, dict) and "parent" in document
    }
    visited = {root_ids[0]}
    current = root_ids[0]
    while current in child_by_parent:
        current = child_by_parent[current]
        if current in visited:
            raise RuntimeError("Docker hybrid archive history chain contains a cycle")
        visited.add(current)
    if visited != ids:
        raise RuntimeError("Docker hybrid archive history chain is disconnected")
    leaf_ids = ids - set(child_by_parent)
    if len(final_ids) != 1 or set(final_ids) != leaf_ids:
        raise RuntimeError("Docker hybrid archive final history is not the chain leaf")


def validate_hybrid_archive(
    archive: tarfile.TarFile,
    members: list[tarfile.TarInfo],
    *,
    expected_image_id: str,
    config_name: str,
    config_member: tarfile.TarInfo,
    config: dict[str, object],
    layers: list[str],
    layer_sources: dict[str, object],
    diff_ids: list[str],
) -> None:
    architecture = config.get("architecture")
    created = config.get("created")
    if (
        not isinstance(architecture, str)
        or not architecture
        or config.get("os") != "linux"
        or not isinstance(created, str)
        or not created
    ):
        raise RuntimeError("Docker hybrid archive config identity metadata is malformed")
    blob_members: dict[str, tarfile.TarInfo] = {}
    validate_hybrid_top_level_member_names(members)
    for member in members:
        pure = PurePosixPath(member.name)
        if len(pure.parts) >= 2 and pure.parts[:2] == ("blobs", "sha256"):
            if len(pure.parts) == 2 and member.isdir():
                continue
            if (
                not member.isreg()
                or CANONICAL_BLOB_PATH_PATTERN.fullmatch(member.name) is None
                or member.size <= 0
            ):
                raise RuntimeError("Docker hybrid archive has a malformed blob member")
            blob_members[member.name] = member

    if config_name != canonical_blob_path(expected_image_id):
        raise RuntimeError("Docker hybrid archive config path differs from image ID")
    if config_name not in blob_members or blob_members[config_name] != config_member:
        raise RuntimeError("Docker hybrid archive config blob is not exact")

    layout, _layout_bytes, _layout_member = read_archive_json(
        archive,
        "oci-layout",
        maximum_size=MAX_OCI_LAYOUT_BYTES,
        label="OCI layout",
    )
    if layout != {"imageLayoutVersion": OCI_LAYOUT_VERSION}:
        raise RuntimeError("OCI layout shape or version is unexpected")

    index, _index_bytes, _index_member = read_archive_json(
        archive,
        "index.json",
        maximum_size=MAX_OCI_INDEX_BYTES,
        label="OCI index",
    )
    if (
        not isinstance(index, dict)
        or not {"schemaVersion", "mediaType", "manifests"}.issubset(index)
        or not set(index).issubset(
            {"schemaVersion", "mediaType", "manifests", "annotations"}
        )
        or index.get("schemaVersion") != 2
        or index.get("mediaType") != OCI_INDEX_MEDIA_TYPE
        or not isinstance(index.get("manifests"), list)
        or len(index["manifests"]) != 1
    ):
        raise RuntimeError("OCI index shape is unexpected")
    if "annotations" in index:
        validate_annotations(index["annotations"], "OCI index")
    manifest_digest, manifest_size = validate_oci_descriptor(
        index["manifests"][0],
        expected_media_type=OCI_MANIFEST_MEDIA_TYPE,
        label="OCI index manifest",
        allow_platform=True,
    )
    manifest_name = canonical_blob_path(manifest_digest)
    if manifest_name not in blob_members or blob_members[manifest_name].size != manifest_size:
        raise RuntimeError("OCI index manifest descriptor differs from its blob")
    oci_manifest, _manifest_bytes, oci_manifest_member = read_archive_json(
        archive,
        manifest_name,
        maximum_size=MAX_OCI_MANIFEST_BYTES,
        label="OCI image manifest",
    )
    if oci_manifest_member.size != manifest_size:
        raise RuntimeError("OCI image manifest size differs from index descriptor")
    if (
        not isinstance(oci_manifest, dict)
        or not {"schemaVersion", "mediaType", "config", "layers"}.issubset(
            oci_manifest
        )
        or not set(oci_manifest).issubset(
            {"schemaVersion", "mediaType", "config", "layers", "annotations"}
        )
        or oci_manifest.get("schemaVersion") != 2
        or oci_manifest.get("mediaType") != OCI_MANIFEST_MEDIA_TYPE
        or not isinstance(oci_manifest.get("layers"), list)
        or len(oci_manifest["layers"]) != len(layers)
    ):
        raise RuntimeError("OCI image manifest shape is unexpected")
    if "annotations" in oci_manifest:
        validate_annotations(oci_manifest["annotations"], "OCI image manifest")

    config_digest, config_size = validate_oci_descriptor(
        oci_manifest["config"],
        expected_media_type=OCI_CONFIG_MEDIA_TYPE,
        label="OCI image config",
    )
    if (
        config_digest != expected_image_id
        or config_size != config_member.size
        or canonical_blob_path(config_digest) != config_name
    ):
        raise RuntimeError("OCI image config descriptor differs from image config")

    for position, descriptor in enumerate(oci_manifest["layers"]):
        digest, size = validate_oci_descriptor(
            descriptor,
            expected_media_type=DOCKER_LAYER_MEDIA_TYPE,
            label="OCI image layer",
        )
        layer = layers[position]
        if (
            digest != diff_ids[position]
            or layer != canonical_blob_path(digest)
            or layer not in blob_members
            or blob_members[layer].size != size
            or layer_sources[digest]["size"] != size
        ):
            raise RuntimeError("OCI layer descriptor chain differs from Docker layers")

    known_blob_names = {config_name, manifest_name, *layers}
    if len(known_blob_names) != len(layers) + 2:
        raise RuntimeError("Docker hybrid archive has colliding declared blobs")
    history_names = classify_hybrid_history_members(
        blob_members, known_blob_names, len(layers)
    )
    validate_hybrid_member_inventory(
        members,
        config_name=config_name,
        manifest_name=manifest_name,
        layers=layers,
        history_names=history_names,
    )
    for blob_name, blob_member in blob_members.items():
        handle = archive.extractfile(blob_member)
        if handle is None:
            raise RuntimeError("Docker hybrid archive blob is unreadable")
        actual_digest = stream_sha256(handle)
        if actual_digest != PurePosixPath(blob_name).name:
            raise RuntimeError("Docker hybrid archive blob path digest is incorrect")
    history_documents: list[object] = []
    for history_name in sorted(history_names):
        history, _history_bytes, history_member = read_archive_json(
            archive,
            history_name,
            maximum_size=MAX_DOCKER_HISTORY_BYTES,
            label="Docker legacy history",
        )
        if history_member != blob_members[history_name]:
            raise RuntimeError("Docker hybrid archive history member is inconsistent")
        history_documents.append(history)
    validate_history_documents(
        history_documents, len(layers), architecture, created
    )
    if set(blob_members) != known_blob_names | history_names:
        raise RuntimeError("Docker hybrid archive blob declaration set is not exact")

    if "platform" in index["manifests"][0]:
        platform = index["manifests"][0]["platform"]
        if platform["architecture"] != architecture:
            raise RuntimeError("OCI index platform differs from image config")


def validate_image_archive(
    path: Path,
    expected_image_id: str,
    expected_uid: int,
    expected_mode: int = 0o600,
) -> None:
    if IMAGE_ID_PATTERN.fullmatch(expected_image_id) is None:
        raise RuntimeError("expected archive image ID is malformed")
    metadata = path.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != expected_uid
        or stat.S_IMODE(metadata.st_mode) != expected_mode
        or metadata.st_size <= 0
        or metadata.st_size > MAX_DOCKER_ARCHIVE_BYTES
    ):
        raise RuntimeError("Docker archive is not a private owner-only regular file")
    with tarfile.open(path, mode="r:") as archive:
        members: list[tarfile.TarInfo] = []
        names: set[str] = set()
        for member in archive:
            if len(members) >= MAX_DOCKER_ARCHIVE_MEMBERS:
                raise RuntimeError("Docker archive member count is unsafe")
            pure = PurePosixPath(member.name)
            if (
                not member.name
                or len(member.name.encode("utf-8")) > 4096
                or pure.is_absolute()
                or ".." in pure.parts
                or not (member.isdir() or member.isreg())
            ):
                raise RuntimeError("Docker archive has an unsafe member")
            if member.name in names:
                raise RuntimeError("Docker archive has duplicate members")
            names.add(member.name)
            members.append(member)
        if not members:
            raise RuntimeError("Docker archive member count is unsafe")
        try:
            manifest_member = archive.getmember("manifest.json")
        except KeyError as error:
            raise RuntimeError("Docker archive manifest is missing") from error
        if (
            not manifest_member.isreg()
            or manifest_member.size <= 0
            or manifest_member.size > 1024 * 1024
        ):
            raise RuntimeError("Docker archive manifest is malformed")
        manifest_handle = archive.extractfile(manifest_member)
        if manifest_handle is None:
            raise RuntimeError("Docker archive manifest is unreadable")
        try:
            manifest = strict_json_load(manifest_handle)
        except (UnicodeDecodeError, json.JSONDecodeError, DuplicateJSONKeyError) as error:
            raise RuntimeError("Docker archive manifest is invalid") from error
        if not isinstance(manifest, list) or len(manifest) != 1 or not isinstance(manifest[0], dict):
            raise RuntimeError("Docker archive must contain exactly one image")
        entry = manifest[0]
        if set(entry) not in (
            DOCKER_MANIFEST_BASE_KEYS,
            DOCKER_MANIFEST_BASE_KEYS | {"LayerSources"},
        ):
            raise RuntimeError("Docker archive manifest shape is unexpected")
        legacy_config = expected_image_id.removeprefix("sha256:") + ".json"
        hybrid_config = canonical_blob_path(expected_image_id)
        config_name = entry["Config"]
        if config_name == legacy_config and set(entry) == DOCKER_MANIFEST_BASE_KEYS:
            is_hybrid = False
        elif (
            config_name == hybrid_config
            and set(entry) == DOCKER_MANIFEST_BASE_KEYS | {"LayerSources"}
        ):
            is_hybrid = True
        else:
            raise RuntimeError("Docker archive format is not classic or reviewed hybrid")
        if entry["RepoTags"] not in (None, []):
            raise RuntimeError("Docker archive is not an exact image-ID export")
        if is_hybrid:
            validate_hybrid_top_level_member_names(members)
        layers = entry["Layers"]
        if (
            not isinstance(layers, list)
            or not layers
            or len(layers) > MAX_DOCKER_LAYERS
            or any(not isinstance(layer, str) or not layer for layer in layers)
            or len(layers) != len(set(layers))
        ):
            raise RuntimeError("Docker archive layer list is malformed")
        has_layer_sources = is_hybrid
        layer_sources = entry.get("LayerSources")
        if has_layer_sources:
            if not isinstance(layer_sources, dict) or len(layer_sources) != len(layers):
                raise RuntimeError("Docker archive layer-source map is malformed")
            for source_digest, source in layer_sources.items():
                if (
                    not isinstance(source_digest, str)
                    or IMAGE_ID_PATTERN.fullmatch(source_digest) is None
                    or not isinstance(source, dict)
                    or set(source) != DOCKER_LAYER_SOURCE_KEYS
                    or source.get("digest") != source_digest
                    or source.get("mediaType") != DOCKER_LAYER_MEDIA_TYPE
                    or isinstance(source.get("size"), bool)
                    or not isinstance(source.get("size"), int)
                    or not 1 <= source["size"] <= MAX_DOCKER_LAYER_BYTES
                ):
                    raise RuntimeError("Docker archive layer-source entry is malformed")
        if not is_hybrid:
            archive_layer_names = {
                member.name
                for member in members
                if member.isreg() and PurePosixPath(member.name).name == "layer.tar"
            }
            if archive_layer_names != set(layers):
                raise RuntimeError("Docker archive contains an extra or undeclared layer")
        try:
            config_member = archive.getmember(config_name)
        except KeyError as error:
            raise RuntimeError("Docker archive config is missing") from error
        if not config_member.isreg() or config_member.size <= 0 or config_member.size > 16 * 1024 * 1024:
            raise RuntimeError("Docker archive config is malformed")
        config_handle = archive.extractfile(config_member)
        if config_handle is None:
            raise RuntimeError("Docker archive config is unreadable")
        config_bytes = config_handle.read()
        config_digest = hashlib.sha256(config_bytes).hexdigest()
        if config_digest != expected_image_id.removeprefix("sha256:"):
            raise RuntimeError("Docker archive config digest differs from the image ID")
        try:
            config = strict_json_loads(config_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError, DuplicateJSONKeyError) as error:
            raise RuntimeError("Docker archive config JSON is malformed") from error
        if not isinstance(config, dict):
            raise RuntimeError("Docker archive config JSON is malformed")
        rootfs = config.get("rootfs")
        if (
            not isinstance(rootfs, dict)
            or set(rootfs) != {"type", "diff_ids"}
            or rootfs.get("type") != "layers"
        ):
            raise RuntimeError("Docker archive rootfs config is malformed")
        diff_ids = rootfs.get("diff_ids")
        if (
            not isinstance(diff_ids, list)
            or len(diff_ids) != len(layers)
            or not diff_ids
            or any(
                not isinstance(diff_id, str)
                or IMAGE_ID_PATTERN.fullmatch(diff_id) is None
                for diff_id in diff_ids
            )
        ):
            raise RuntimeError("Docker archive diff-ID chain is malformed")
        if has_layer_sources:
            if len(diff_ids) != len(set(diff_ids)) or set(layer_sources) != set(diff_ids):
                raise RuntimeError("Docker archive layer-source set differs from diff IDs")
            for position, diff_id in enumerate(diff_ids):
                expected_layer = "blobs/sha256/" + diff_id.removeprefix("sha256:")
                if layers[position] != expected_layer:
                    raise RuntimeError(
                        "Docker archive layer path differs from its ordered diff ID"
                    )
        if is_hybrid:
            validate_hybrid_archive(
                archive,
                members,
                expected_image_id=expected_image_id,
                config_name=config_name,
                config_member=config_member,
                config=config,
                layers=layers,
                layer_sources=layer_sources,
                diff_ids=diff_ids,
            )
        else:
            total_layer_bytes = 0
            for position, layer in enumerate(layers):
                try:
                    layer_member = archive.getmember(layer)
                except KeyError as error:
                    raise RuntimeError("Docker archive layer is missing") from error
                if (
                    not layer_member.isreg()
                    or layer_member.size <= 0
                    or layer_member.size > MAX_DOCKER_LAYER_BYTES
                ):
                    raise RuntimeError("Docker archive layer is malformed")
                total_layer_bytes += layer_member.size
                if total_layer_bytes > MAX_DOCKER_TOTAL_LAYER_BYTES:
                    raise RuntimeError("Docker archive layer data is too large")
                layer_handle = archive.extractfile(layer_member)
                if layer_handle is None:
                    raise RuntimeError("Docker archive layer is unreadable")
                actual_diff_id = "sha256:" + stream_sha256(layer_handle)
                if actual_diff_id != diff_ids[position]:
                    raise RuntimeError(
                        "Docker archive layer differs from its ordered diff ID"
                    )


def validate_minimal_tool_image_archive(
    path: Path,
    expected_image_id: str,
    expected_uid: int,
    *,
    binary_name: str,
    expected_binary_sha256: str,
    expected_ca_bundle_sha256: str,
    expected_labels: dict[str, str],
    expected_mode: int = 0o600,
) -> str:
    validate_image_archive(path, expected_image_id, expected_uid, expected_mode)
    binary_path = f"usr/local/bin/{binary_name}"
    expected_files = {
        "etc/ssl/certs/ca-certificates.crt",
        binary_path,
    }
    allowed_directories = {
        ".",
        "etc",
        "etc/ssl",
        "etc/ssl/certs",
        "usr",
        "usr/local",
        "usr/local/bin",
    }
    actual_files: set[str] = set()
    binary_sha256: str | None = None
    ca_bundle_sha256: str | None = None
    with tarfile.open(path, mode="r:") as archive:
        manifest, _, _ = read_archive_json(
            archive,
            "manifest.json",
            maximum_size=1024 * 1024,
            label="Docker archive manifest",
        )
        if not isinstance(manifest, list) or len(manifest) != 1:
            raise RuntimeError("minimal tool archive must contain exactly one image")
        entry = manifest[0]
        if not isinstance(entry, dict):
            raise RuntimeError("minimal tool archive manifest entry is malformed")
        config, _, _ = read_archive_json(
            archive,
            entry["Config"],
            maximum_size=16 * 1024 * 1024,
            label="minimal tool image config",
        )
        if (
            not isinstance(config, dict)
            or config.get("architecture") != "amd64"
            or config.get("os") != "linux"
        ):
            raise RuntimeError("minimal tool image platform is not exact linux/amd64")
        runtime_config = config.get("config")
        allowed_runtime_keys = {
            "ArgsEscaped",
            "Cmd",
            "Entrypoint",
            "Env",
            "ExposedPorts",
            "Healthcheck",
            "Labels",
            "OnBuild",
            "Shell",
            "StopSignal",
            "User",
            "Volumes",
            "WorkingDir",
        }
        if (
            not isinstance(runtime_config, dict)
            or not set(runtime_config).issubset(allowed_runtime_keys)
            or runtime_config.get("User") != "65532:65532"
            or runtime_config.get("Entrypoint") != [f"/{binary_path}"]
            or runtime_config.get("Labels") != expected_labels
            or runtime_config.get("Cmd") not in (None, [])
            or runtime_config.get("Env") != MINIMAL_TOOL_ENV
            or runtime_config.get("WorkingDir") != MINIMAL_TOOL_WORKING_DIR
            or runtime_config.get("Healthcheck") not in (None, {})
            or runtime_config.get("ExposedPorts") not in (None, {})
            or runtime_config.get("Volumes") not in (None, {})
            or runtime_config.get("OnBuild") not in (None, [])
            or runtime_config.get("Shell") not in (None, [])
            or runtime_config.get("StopSignal") not in (None, "")
            or runtime_config.get("ArgsEscaped") not in (None, False)
        ):
            raise RuntimeError("minimal tool image runtime config or labels differ")
        layers = entry.get("Layers")
        if not isinstance(layers, list) or len(layers) != 2:
            raise RuntimeError("minimal tool image layer list is malformed")
        expected_layer_files = (
            "etc/ssl/certs/ca-certificates.crt",
            binary_path,
        )
        for layer_position, layer_name in enumerate(layers):
            if not isinstance(layer_name, str):
                raise RuntimeError("minimal tool image layer name is malformed")
            try:
                layer_member = archive.getmember(layer_name)
            except KeyError as error:
                raise RuntimeError("minimal tool image layer is missing") from error
            layer_handle = archive.extractfile(layer_member)
            if layer_handle is None:
                raise RuntimeError("minimal tool image layer is unreadable")
            try:
                layer_archive = tarfile.open(fileobj=layer_handle, mode="r|*")
                layer_files: set[str] = set()
                for member in layer_archive:
                    pure = PurePosixPath(member.name)
                    normalized = pure.as_posix()
                    if pure.is_absolute() or ".." in pure.parts:
                        raise RuntimeError("minimal tool rootfs contains an unsafe path")
                    if member.isdir():
                        if normalized not in allowed_directories:
                            raise RuntimeError("minimal tool rootfs contains an unexpected directory")
                        continue
                    if normalized == "usr/.wh..wh..opq":
                        if (
                            layer_position != 1
                            or not member.isreg()
                            or member.size != 0
                            or member.uid != 0
                            or member.gid != 0
                        ):
                            raise RuntimeError("minimal tool rootfs whiteout metadata is malformed")
                        continue
                    if not member.isreg() or normalized not in expected_files:
                        raise RuntimeError("minimal tool rootfs contains an unexpected entry")
                    if normalized in actual_files or member.uid != 0 or member.gid != 0:
                        raise RuntimeError("minimal tool rootfs file ownership is malformed")
                    if normalized == binary_path:
                        if stat.S_IMODE(member.mode) != 0o755 or not 1 <= member.size <= 512 * 1024 * 1024:
                            raise RuntimeError("minimal tool binary metadata is malformed")
                    elif stat.S_IMODE(member.mode) != 0o644 or not 1 <= member.size <= 2 * 1024 * 1024:
                        raise RuntimeError("minimal tool CA bundle metadata is malformed")
                    file_handle = layer_archive.extractfile(member)
                    if file_handle is None:
                        raise RuntimeError("minimal tool rootfs file is unreadable")
                    digest = stream_sha256(file_handle)
                    if normalized == binary_path:
                        binary_sha256 = digest
                    else:
                        ca_bundle_sha256 = digest
                    actual_files.add(normalized)
                    layer_files.add(normalized)
                if layer_files != {expected_layer_files[layer_position]}:
                    raise RuntimeError("minimal tool rootfs layer file order differs")
            except (tarfile.TarError, OSError) as error:
                raise RuntimeError("minimal tool rootfs layer tar is malformed") from error
    if (
        actual_files != expected_files
        or binary_sha256 != expected_binary_sha256
        or ca_bundle_sha256 != expected_ca_bundle_sha256
    ):
        raise RuntimeError("minimal tool rootfs files or digests differ from the lock")
    return binary_sha256


def validate_scanner_image_archive(
    path: Path,
    expected_image_id: str,
    expected_uid: int,
    expected_mode: int = 0o600,
) -> str:
    return validate_minimal_tool_image_archive(
        path,
        expected_image_id,
        expected_uid,
        binary_name="trivy",
        expected_binary_sha256=SCANNER_BINARY_SHA256,
        expected_ca_bundle_sha256=SCANNER_CA_BUNDLE_SHA256,
        expected_labels=SCANNER_LABELS,
        expected_mode=expected_mode,
    )


def validate_cosign_image_archive(
    path: Path,
    expected_image_id: str,
    expected_uid: int,
    expected_mode: int = 0o600,
) -> str:
    return validate_minimal_tool_image_archive(
        path,
        expected_image_id,
        expected_uid,
        binary_name="cosign",
        expected_binary_sha256=COSIGN_BINARY_SHA256,
        expected_ca_bundle_sha256=SCANNER_CA_BUNDLE_SHA256,
        expected_labels=COSIGN_LABELS,
        expected_mode=expected_mode,
    )


def load(path: Path) -> object:
    try:
        metadata = path.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size > MAX_JSON_EVIDENCE_BYTES
        ):
            raise RuntimeError("Trivy evidence file type or size is not reviewed")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_dev != metadata.st_dev
                or opened.st_ino != metadata.st_ino
                or opened.st_size != metadata.st_size
                or opened.st_size > MAX_JSON_EVIDENCE_BYTES
            ):
                raise RuntimeError("Trivy evidence changed before bounded reading")
            encoded = handle.read(MAX_JSON_EVIDENCE_BYTES + 1)
        if len(encoded) > MAX_JSON_EVIDENCE_BYTES:
            raise RuntimeError("Trivy evidence exceeds the reviewed size")
        return strict_json_loads(encoded)
    except RuntimeError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, DuplicateJSONKeyError) as error:
        raise RuntimeError("Trivy evidence is unreadable") from error


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    scan = subparsers.add_parser("scan")
    scan.add_argument("--report", type=Path, required=True)
    scan.add_argument("--label", required=True)
    scan.add_argument("--expected-artifact-name", required=True)
    scan.add_argument("--expected-artifact-type", choices=("filesystem", "container_image"), required=True)
    scan.add_argument("--expected-target")
    scan.add_argument(
        "--expected-scanner-version",
        choices=sorted(REVIEWED_TRIVY_VERSIONS),
        default=TRIVY_UPSTREAM_VERSION,
    )
    version = subparsers.add_parser("version")
    version.add_argument("--report", type=Path, required=True)
    version.add_argument(
        "--expected-scanner-version",
        choices=sorted(REVIEWED_TRIVY_VERSIONS),
        default=TRIVY_UPSTREAM_VERSION,
    )
    archive = subparsers.add_parser("archive")
    archive.add_argument("--archive", type=Path, required=True)
    archive.add_argument("--expected-image-id", required=True)
    archive.add_argument("--expected-owner-uid", type=int, default=os.getuid())
    archive.add_argument("--expected-mode", type=lambda value: int(value, 8), default=0o600)
    scanner_image = subparsers.add_parser("scanner-image")
    scanner_image.add_argument("--archive", type=Path, required=True)
    scanner_image.add_argument("--expected-image-id", required=True)
    scanner_image.add_argument("--expected-owner-uid", type=int, default=os.getuid())
    scanner_image.add_argument(
        "--expected-mode", type=lambda value: int(value, 8), default=0o600
    )
    cosign_image = subparsers.add_parser("cosign-image")
    cosign_image.add_argument("--archive", type=Path, required=True)
    cosign_image.add_argument("--expected-image-id", required=True)
    cosign_image.add_argument("--expected-owner-uid", type=int, default=os.getuid())
    cosign_image.add_argument(
        "--expected-mode", type=lambda value: int(value, 8), default=0o600
    )
    database = subparsers.add_parser("database-cache")
    database.add_argument("--cache", type=Path, required=True)
    database.add_argument("--expected-owner-uid", type=int, required=True)
    compare = subparsers.add_parser("compare-official-findings")
    compare.add_argument("--official-report", type=Path, required=True)
    compare.add_argument("--custom-report", type=Path, required=True)
    compare.add_argument("--findings-lock", type=Path, required=True)
    compare.add_argument("--expected-artifact-name", required=True)
    compare.add_argument("--expected-target", required=True)
    args = parser.parse_args()
    if args.command == "scan":
        validate_scan_report(
            load(args.report),
            args.label,
            expected_artifact_name=args.expected_artifact_name,
            expected_artifact_type=args.expected_artifact_type,
            expected_target=args.expected_target,
            expected_scanner_version=args.expected_scanner_version,
        )
        print(f"SCAN PASS: {args.label} has exact identity and no blocking finding")
    elif args.command == "version":
        values = validate_version_report(
            load(args.report),
            expected_scanner_version=args.expected_scanner_version,
        )
        print(*values)
    elif args.command == "archive":
        validate_image_archive(
            args.archive,
            args.expected_image_id,
            args.expected_owner_uid,
            args.expected_mode,
        )
        print("private Docker archive is bound to the exact immutable image ID")
    elif args.command == "scanner-image":
        print(
            validate_scanner_image_archive(
                args.archive,
                args.expected_image_id,
                args.expected_owner_uid,
                args.expected_mode,
            )
        )
    elif args.command == "cosign-image":
        print(
            validate_cosign_image_archive(
                args.archive,
                args.expected_image_id,
                args.expected_owner_uid,
                args.expected_mode,
            )
        )
    elif args.command == "database-cache":
        print(*validate_database_cache(args.cache, args.expected_owner_uid))
    else:
        print(
            validate_antiblindness_reports(
                load(args.official_report),
                load(args.custom_report),
                args.findings_lock,
                expected_artifact_name=args.expected_artifact_name,
                expected_target=args.expected_target,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Static acceptance gate for the one-APK embedded Android appliance."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import subprocess
import xml.etree.ElementTree as ET
import zipfile


ANDROID = "{http://schemas.android.com/apk/res/android}"
REQUIRED_SERVICES = {
    "com.termux.app.TermuxService",
    "com.termux.app.RunCommandService",
    "com.chebysight.chebyagent.android.AgentNodeService",
    "com.chebysight.chebyagent.android.AgentAccessibilityService",
}
REQUIRED_RECEIVERS = {
    "com.chebysight.chebyagent.android.BootStartupReceiver",
}
REQUIRED_VISIBLE_PACKAGES = {
    "ru.cian.main",
    "ru.vk.store",
    "ru.yandex.taxi",
    "ru.yandex.yandexmaps",
}
REQUIRED_RUNTIME_ASSETS = {
    "/assets/cheby-runtime/mobile-experience-instructions.md",
    "/assets/cheby-runtime/coffee-poster-reference.md",
    "/assets/cheby-runtime/cian-rental-report-reference.md",
    "/assets/cheby-runtime/ace_memory.py",
    "/assets/cheby-runtime/ace-core-__init__.py",
    "/assets/cheby-runtime/ace-core-skillbook.py",
    "/assets/cheby-runtime/ace-core-insight_source.py",
    "/assets/cheby-runtime/ace-core-LICENSE",
    "/assets/cheby-runtime/ace-core-UPSTREAM.md",
    "/assets/cheby-runtime/codex-base-instructions.md",
    "/assets/cheby-runtime/skill-yandex-maps-haircut-finder.md",
    "/assets/cheby-runtime/skill-yandex-maps-haircut-finder.openai.yaml",
    "/assets/cheby-runtime/skill-yandex-maps-massage-finder.md",
    "/assets/cheby-runtime/skill-yandex-maps-massage-finder.openai.yaml",
    "/assets/cheby-runtime/skill-yandex-maps-dog-grooming.md",
    "/assets/cheby-runtime/skill-yandex-maps-dog-grooming.openai.yaml",
    "/assets/cheby-runtime/skill-phone-ui-recovery.md",
    "/assets/cheby-runtime/skill-phone-ui-recovery.openai.yaml",
    "/assets/cheby-runtime/skill-cian-rental-finder.md",
    "/assets/cheby-runtime/skill-cian-rental-finder.openai.yaml",
    "/assets/cheby-runtime/skill-yandex-go-food-order.md",
    "/assets/cheby-runtime/skill-yandex-go-food-order.openai.yaml",
    "/assets/cheby-runtime/codex-linux-arm64.tgz",
    "/assets/cheby-runtime/debian-rootfs-aarch64.tar.zst",
    "/assets/cheby-runtime/enter-debian.sh",
    "/assets/cheby-runtime/glm-chat-adapter.py",
    "/assets/cheby-runtime/install-codex-overlay.py",
    "/assets/cheby-runtime/local_mcp.py",
    "/assets/cheby-runtime/phonebridge.mjs",
    "/assets/cheby-runtime/provision-guest.sh",
    "/assets/cheby-runtime/provision-runtime.sh",
    "/assets/cheby-runtime/provider-launcher.py",
    "/assets/cheby-runtime/runtime-assets.sha256",
    "/assets/cheby-runtime/runtime.lock",
    "/assets/cheby-runtime/skill-russia-menu-assistant.md",
    "/assets/cheby-runtime/skill-yandex-maps-restaurant-finder.md",
    "/assets/cheby-runtime/skill-yandex-maps-restaurant-finder.openai.yaml",
    "/assets/cheby-runtime/skill-yandex-maps-supermarket-finder.md",
    "/assets/cheby-runtime/skill-yandex-maps-supermarket-finder.openai.yaml",
    "/assets/cheby-runtime/skill-russia-menu-assistant.openai.yaml",
    "/assets/cheby-runtime/skill-yandex-go-taxi-booker.md",
    "/assets/cheby-runtime/skill-yandex-go-taxi-booker.openai.yaml",
    "/assets/cheby-runtime/skill-yandex-maps-coffee-finder.md",
    "/assets/cheby-runtime/skill-yandex-maps-coffee-finder.openai.yaml",
    "/assets/cheby-runtime/skill-yandex-maps-route-planner.md",
    "/assets/cheby-runtime/skill-yandex-maps-route-planner.openai.yaml",
    "/assets/cheby-runtime/start-codex-appserver.sh",
    "/assets/cheby-runtime/start-phonebridge.sh",
    "/assets/cheby-runtime/termux-proot-overlay-aarch64.tar.zst",
}
RUNTIME_SKILL_ASSETS = {
    path.removeprefix("/assets/cheby-runtime/")
    for path in REQUIRED_RUNTIME_ASSETS
    if path.startswith("/assets/cheby-runtime/skill-") and path.endswith(".md")
}
PINNED_NATIVE_APP_SKILLS = {
    "skill-cian-rental-finder.md": "ru.cian.main",
    "skill-yandex-go-food-order.md": "ru.yandex.taxi",
}
PINNED_RUNTIME_LOCK = {
    "CHEBY_RUNTIME_VERSION": "4.1.0-dev32",
    "TERMUX_PROOT_OVERLAY_ASSET": "termux-proot-overlay-aarch64.tar.zst",
    "TERMUX_PROOT_OVERLAY_SHA256": (
        "334a0e6aa93cf3264f416879d4a95ee5ad9df52a9c202f12436b07eaf7bc067c"
    ),
    "PROOT_VERSION": "5.1.107.89",
    "PROOT_PACKAGE_SHA256": (
        "ec9fe38c50cfd49dd31fe360ffbcc3124a945dc1ea16293a8a769303dd724f46"
    ),
    "LIBANDROID_SHMEM_VERSION": "0.7",
    "LIBANDROID_SHMEM_PACKAGE_SHA256": (
        "0da3a24d558b93c92bcf8d611e0826a99ff96e396b148e6cdf33b47c47c57ff6"
    ),
    "LIBTALLOC_VERSION": "2.4.3",
    "LIBTALLOC_PACKAGE_SHA256": (
        "ac81ad623d74c209718b9f3acb2dd702cc8a88c431e820d212229910b4db29da"
    ),
    "DEBIAN_IMAGE": "debian:13.6-slim",
    "DEBIAN_CONFIG_DIGEST": (
        "sha256:95fa189f90cf43d92e30c8881641040941d5fcf5e32df85750b54b24a8e3102c"
    ),
    "DEBIAN_ROOTFS_LAYER_DIGEST": (
        "sha256:1b7200988f192e72703c70486d494e2457935ac9b0f031ac09eb115b01a12d45"
    ),
    "DEBIAN_ROOTFS_ASSET": "debian-rootfs-aarch64.tar.zst",
    "DEBIAN_ROOTFS_SHA256": (
        "ef6acc3d5842700dfc879e3ca83532a4b2c495ebb8a4a7cac645cbfd7aa56c7e"
    ),
    "BASE_CODEX_VERSION": "0.147.0",
    "CODEX_VERSION": "0.153.4",
    "CODEX_ARCHIVE_ASSET": "codex-linux-arm64.tgz",
    "CODEX_ARCHIVE_URL": "https://registry.npmjs.org/@openai/codex/-/codex-0.153.4-linux-arm64.tgz",
    "CODEX_ARCHIVE_SHA256": (
        "439c0dd0d6923f607b4e5cd1e3079c12f0b86f6e5007f07e377d6ad25e2d7bb9"
    ),
    "CODEX_ARCHIVE_INTEGRITY": (
        "sha512-QKdjYLYV4hXIuUQDP3P6F4NXuWFoKo9WUoV4nAREIx55kiUyi8UsYdsVobkeXir5n/maEQgYMCKLHVma4rNPiw=="
    ),
    "CODEX_LATEST_VERIFIED_DATE": "2026-09-05",
}
SENSITIVE_FILE = re.compile(
    r"(?:^|/)(?:auth\.json|.*credential.*|.*secret.*|.*private.*key.*|.*\.(?:jks|p12|pem|key))$",
    re.IGNORECASE,
)


def run(*args: str) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout


def attr(element: ET.Element, name: str) -> str:
    return element.attrib.get(ANDROID + name, "")


def launcher_activities(application: ET.Element) -> list[str]:
    launchers: list[str] = []
    for activity in application.findall("activity"):
        for intent_filter in activity.findall("intent-filter"):
            actions = {attr(item, "name") for item in intent_filter.findall("action")}
            categories = {attr(item, "name") for item in intent_filter.findall("category")}
            if (
                "android.intent.action.MAIN" in actions
                and "android.intent.category.LAUNCHER" in categories
            ):
                launchers.append(attr(activity, "name"))
    return launchers


def runtime_assets_are_locked(apk: pathlib.Path) -> bool:
    prefix = "assets/cheby-runtime/"
    try:
        with zipfile.ZipFile(apk) as archive:
            manifest = archive.read(prefix + "runtime-assets.sha256").decode("ascii")
            expected_files = {
                path.removeprefix("/" + prefix) for path in REQUIRED_RUNTIME_ASSETS
            }
            expected_files.remove("runtime-assets.sha256")
            locked_files: set[str] = set()
            verified_digests: dict[str, str] = {}
            for line in manifest.splitlines():
                match = re.fullmatch(
                    r"([0-9a-f]{64})  ([A-Za-z0-9][A-Za-z0-9._-]{0,127})",
                    line,
                )
                if match is None or match.group(2) in locked_files:
                    return False
                digest, name = match.groups()
                item_digest = hashlib.sha256()
                with archive.open(prefix + name) as item:
                    for chunk in iter(lambda: item.read(1024 * 1024), b""):
                        item_digest.update(chunk)
                if item_digest.hexdigest() != digest:
                    return False
                locked_files.add(name)
                verified_digests[name] = digest
            if locked_files != expected_files:
                return False

            lock: dict[str, str] = {}
            lock_text = archive.read(prefix + "runtime.lock").decode("ascii")
            for line in lock_text.splitlines():
                match = re.fullmatch(r"([A-Z][A-Z0-9_]*)='([^'\r\n]*)'", line)
                if match is None or match.group(1) in lock:
                    return False
                lock[match.group(1)] = match.group(2)
            return lock == PINNED_RUNTIME_LOCK and all(
                verified_digests.get(lock[asset_key]) == lock[digest_key]
                for asset_key, digest_key in (
                    ("DEBIAN_ROOTFS_ASSET", "DEBIAN_ROOTFS_SHA256"),
                    ("TERMUX_PROOT_OVERLAY_ASSET", "TERMUX_PROOT_OVERLAY_SHA256"),
                    ("CODEX_ARCHIVE_ASSET", "CODEX_ARCHIVE_SHA256"),
                )
            )
    except (KeyError, UnicodeError, zipfile.BadZipFile):
        return False


def runtime_is_offline_and_bundled(apk: pathlib.Path) -> bool:
    prefix = "assets/cheby-runtime/"
    forbidden = (b"apt-get", b"proot-distro", b"curl ", b"wget ")
    try:
        with zipfile.ZipFile(apk) as archive:
            scripts = (
                archive.read(prefix + "provision-runtime.sh"),
                archive.read(prefix + "provision-guest.sh"),
            )
            if any(token in script for token in forbidden for script in scripts):
                return False
            return all(
                archive.getinfo(prefix + name).compress_type == zipfile.ZIP_STORED
                for name in (
                    "debian-rootfs-aarch64.tar.zst",
                    "termux-proot-overlay-aarch64.tar.zst",
                    "codex-linux-arm64.tgz",
                )
            )
    except (KeyError, zipfile.BadZipFile):
        return False


def runtime_skills_use_native_glm_images(apk: pathlib.Path) -> bool:
    """Reject release skills that can route image evidence to a separate vision MCP."""
    prefix = "assets/cheby-runtime/"
    forbidden = (
        "use the configured independent vision mcp",
        "call the independent vision mcp",
        "call the configured independent vision mcp",
        "original image through the independent vision mcp",
    )
    try:
        with zipfile.ZipFile(apk) as archive:
            for name in RUNTIME_SKILL_ASSETS:
                text = archive.read(prefix + name).decode("utf-8")
                normalized = re.sub(r"\s+", " ", text).lower()
                if "glm" not in normalized or "native image" not in normalized:
                    return False
                if any(item in normalized for item in forbidden):
                    return False
            return True
    except (KeyError, UnicodeError, zipfile.BadZipFile):
        return False


def runtime_omits_independent_vision_mcp(apk: pathlib.Path) -> bool:
    """Require the retired MiniMax vision entry and credential route to be absent."""
    forbidden_assets = {
        "assets/cheby-runtime/vision-mcp.py",
    }
    forbidden_launcher = (
        "mcp_servers.cheby_vision",
        "CHEBY_VISION_",
        "vision-mcp.py",
        "api.minimax.io",
    )
    try:
        with zipfile.ZipFile(apk) as archive:
            files = set(archive.namelist())
            launcher = archive.read(
                "assets/cheby-runtime/provider-launcher.py"
            ).decode("utf-8")
        return not (forbidden_assets & files) and not any(
            token in launcher for token in forbidden_launcher
        )
    except (KeyError, UnicodeError, zipfile.BadZipFile):
        return False


def runtime_skills_pin_native_apps(apk: pathlib.Path) -> bool:
    """Require app-specific skills to launch their installed native package."""
    prefix = "assets/cheby-runtime/"
    try:
        with zipfile.ZipFile(apk) as archive:
            for name, package_name in PINNED_NATIVE_APP_SKILLS.items():
                text = archive.read(prefix + name).decode("utf-8")
                normalized = re.sub(r"\s+", " ", text).lower()
                if package_name not in normalized or "android_open_app" not in normalized:
                    return False
                if "never use `android_open_url`" not in normalized:
                    return False
            return True
    except (KeyError, UnicodeError, zipfile.BadZipFile):
        return False


def runtime_food_skill_requires_fresh_intent(apk: pathlib.Path) -> bool:
    """Reject a food skill that may reuse stale cart/payment state for a new request."""
    required = (
        "before opening any app",
        "delivery address",
        "food preference",
        "budget",
        "stop the turn without calling a phone tool",
        "never inherit an item, cart, fulfillment mode, tip, or payment choice",
    )
    try:
        with zipfile.ZipFile(apk) as archive:
            text = archive.read(
                "assets/cheby-runtime/skill-yandex-go-food-order.md"
            ).decode("utf-8")
        normalized = re.sub(r"\s+", " ", text).lower()
        return all(phrase in normalized for phrase in required)
    except (KeyError, UnicodeError, zipfile.BadZipFile):
        return False


def runtime_blocks_browser_tool(apk: pathlib.Path) -> bool:
    """Require the embedded Codex MCP surface to omit URL/browser launching."""
    try:
        with zipfile.ZipFile(apk) as archive:
            source = archive.read("assets/cheby-runtime/local_mcp.py").decode("utf-8")
            instructions = archive.read(
                "assets/cheby-runtime/mobile-experience-instructions.md"
            ).decode("utf-8")
        return (
            "android_open_url" not in source
            and "prefer native apps" in instructions.lower()
            and "honor explicit user browser/appgallery restrictions" in instructions.lower()
        )
    except (KeyError, UnicodeError, zipfile.BadZipFile):
        return False


def runtime_instructions_digest_is_pinned(apk: pathlib.Path) -> bool:
    """Require the provider launcher to pin the bundled instruction bytes."""
    try:
        with zipfile.ZipFile(apk) as archive:
            instructions = archive.read(
                "assets/cheby-runtime/codex-base-instructions.md"
            )
            launcher = archive.read(
                "assets/cheby-runtime/provider-launcher.py"
            ).decode("utf-8")
        digest = hashlib.sha256(instructions).hexdigest()
        return f"BASE_INSTRUCTIONS_SHA256 = '{digest}'" in launcher
    except (KeyError, UnicodeError, zipfile.BadZipFile):
        return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apk", required=True)
    parser.add_argument("--apkanalyzer")
    parser.add_argument("--require-release", action="store_true",
                        help="Also reject debuggable or test-only installation packages")
    args = parser.parse_args()

    apk = pathlib.Path(args.apk).resolve()
    if not apk.is_file():
        raise SystemExit(f"APK does not exist: {apk}")

    sdk_root = os.environ.get("ANDROID_SDK_ROOT") or os.environ.get("ANDROID_HOME")
    default_analyzer = (
        pathlib.Path(sdk_root) / "cmdline-tools/latest/bin/apkanalyzer"
        if sdk_root
        else None
    )
    analyzer = pathlib.Path(args.apkanalyzer).resolve() if args.apkanalyzer else default_analyzer
    if analyzer is None or not analyzer.is_file():
        raise SystemExit("apkanalyzer was not supplied and ANDROID_SDK_ROOT is unavailable")

    application_id = run(str(analyzer), "manifest", "application-id", str(apk)).strip()
    target_sdk = run(str(analyzer), "manifest", "target-sdk", str(apk)).strip()
    manifest_text = run(str(analyzer), "manifest", "print", str(apk))
    files = set(run(str(analyzer), "files", "list", str(apk)).splitlines())

    root = ET.fromstring(manifest_text)
    application = root.find("application")
    if application is None:
        raise SystemExit("FAIL: merged manifest has no application")

    launchers = launcher_activities(application)
    services = {attr(item, "name") for item in application.findall("service")}
    receivers = {attr(item, "name") for item in application.findall("receiver")}
    queries = root.find("queries")
    visible_packages = (
        {attr(item, "name") for item in queries.findall("package")}
        if queries is not None else set()
    )
    sensitive_files = sorted(path for path in files if SENSITIVE_FILE.search(path))

    checks = {
        "application_id": application_id == "com.termux",
        "target_sdk": target_sdk == "28",
        "application_owner": (
            attr(application, "name") == "com.termux.app.ChebyApplianceApplication"
        ),
        "single_cheby_launcher": launchers == ["com.cheby.codex.mobile.MainActivity"],
        "required_services": REQUIRED_SERVICES.issubset(services),
        "boot_recovery_receiver": REQUIRED_RECEIVERS.issubset(receivers),
        "native_app_package_visibility": REQUIRED_VISIBLE_PACKAGES.issubset(visible_packages),
        "runtime_assets": REQUIRED_RUNTIME_ASSETS.issubset(files),
        "runtime_assets_locked": runtime_assets_are_locked(apk),
        "offline_runtime_bundled": runtime_is_offline_and_bundled(apk),
        "runtime_skills_native_glm_images": runtime_skills_use_native_glm_images(apk),
        "runtime_omits_independent_vision_mcp": runtime_omits_independent_vision_mcp(apk),
        "runtime_skills_pin_native_apps": runtime_skills_pin_native_apps(apk),
        "runtime_food_skill_requires_fresh_intent": runtime_food_skill_requires_fresh_intent(apk),
        "runtime_blocks_browser_tool": runtime_blocks_browser_tool(apk),
        "runtime_instructions_digest_is_pinned": runtime_instructions_digest_is_pinned(apk),
        "termux_native": "/lib/arm64-v8a/libtermux.so" in files,
        "bootstrap_native": "/lib/arm64-v8a/libtermux-bootstrap.so" in files,
        "no_sensitive_files": not sensitive_files,
    }
    if args.require_release:
        checks["not_debuggable"] = attr(application, "debuggable") in ("", "false")
        checks["not_test_only"] = attr(application, "testOnly") in ("", "false")
    failed = sorted(name for name, passed in checks.items() if not passed)
    report = {
        "outcome": "PASS" if not failed else "FAIL",
        "apk": str(apk),
        "apk_sha256": hashlib.sha256(apk.read_bytes()).hexdigest(),
        "application_id": application_id,
        "target_sdk": target_sdk,
        "launchers": launchers,
        "services": sorted(REQUIRED_SERVICES & services),
        "receivers": sorted(REQUIRED_RECEIVERS & receivers),
        "visible_packages": sorted(REQUIRED_VISIBLE_PACKAGES & visible_packages),
        "checks": checks,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    if failed:
        raise SystemExit("FAIL: " + ", ".join(failed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

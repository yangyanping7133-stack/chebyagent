#!/usr/bin/env python3
"""Offline upgrade regression checks; no Android device or model service required."""

import hashlib
import importlib.util
import io
import json
import re
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "codex_overlay", ROOT / "Android/appliance/runtime/install-codex-overlay.py"
)
overlay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(overlay)
gate_spec = importlib.util.spec_from_file_location(
    "appliance_gate", ROOT / "tools/standalone/appliance_static_gate.py"
)
gate = importlib.util.module_from_spec(gate_spec)
gate_spec.loader.exec_module(gate)


class CodexOfflineOverlayTest(unittest.TestCase):
    def test_android_startup_allowlist_matches_packaged_asset_contract(self):
        source = (ROOT / "Android/appliance/src/main/java/com/termux/app/TermuxEmbeddedRuntime.java").read_text()
        declaration = re.search(r"REQUIRED_ASSETS = Set\.of\((.*?)\n    \);", source, re.S)
        self.assertIsNotNone(declaration)
        accepted = set(re.findall(r'"([A-Za-z0-9._-]+)"', declaration.group(1)))
        packaged = {Path(name).name for name in gate.REQUIRED_RUNTIME_ASSETS}
        packaged.remove("runtime-assets.sha256")
        self.assertEqual(accepted, packaged)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime = self.root / "runtime"
        self.archive = self.root / "codex.tgz"
        self.version = "0.153.4"

    def package(self, extra=None, metadata_version=None):
        metadata = {
            "layoutVersion": 1, "version": metadata_version or self.version,
            "target": overlay.TARGET, "entrypoint": "bin/codex",
            "resourcesDir": "codex-resources", "pathDir": "codex-path",
        }
        with tarfile.open(self.archive, "w:gz") as archive:
            contents = {
                "package/package.json": json.dumps({
                    "name": "@openai/codex", "version": self.version + "-linux-arm64",
                }).encode(),
                "package/README.md": b"upstream README",
                overlay.VENDOR_PREFIX + "codex-package.json": json.dumps(metadata).encode(),
                **{overlay.VENDOR_PREFIX + name: b"upstream executable"
                   for name in overlay.REQUIRED_EXECUTABLES},
            }
            for name, content in contents.items():
                member = tarfile.TarInfo(name)
                member.size = len(content)
                member.mode = 0o755 if name.removeprefix(overlay.VENDOR_PREFIX) in overlay.REQUIRED_EXECUTABLES else 0o644
                archive.addfile(member, io.BytesIO(content))
            if extra:
                archive.addfile(extra, io.BytesIO(b"x") if extra.isfile() else None)
        return hashlib.sha256(self.archive.read_bytes()).hexdigest()

    def install(self, digest):
        return overlay.install(self.archive, self.runtime, self.version, digest)

    def test_install_is_idempotent_and_keeps_previous_version(self):
        old = self.runtime / "codex-0.147.0"
        old.mkdir(parents=True)
        (old / "keep").write_text("old version")
        digest = self.package()
        destination = self.install(digest)
        self.assertEqual(self.install(digest), destination)
        self.assertEqual((old / "keep").read_text(), "old version")
        self.assertEqual((destination / "bin/codex").read_bytes(), b"upstream executable")
        self.assertEqual((destination / "npm-README.md").read_bytes(), b"upstream README")

    def test_wrong_hash_has_no_install_side_effect(self):
        self.package()
        with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
            self.install("0" * 64)
        self.assertFalse(self.runtime.exists())

    def test_version_mismatch_does_not_activate_or_leave_staging(self):
        digest = self.package(metadata_version="0.147.0")
        with self.assertRaisesRegex(ValueError, "metadata"):
            self.install(digest)
        self.assertEqual(list(self.runtime.iterdir()), [])

    def test_traversal_is_rejected(self):
        member = tarfile.TarInfo(overlay.VENDOR_PREFIX + "../../escape")
        member.size = 1
        digest = self.package(extra=member)
        with self.assertRaisesRegex(ValueError, "Unsafe"):
            self.install(digest)
        self.assertFalse((self.root / "escape").exists())

    def test_symlink_member_is_rejected(self):
        member = tarfile.TarInfo(overlay.VENDOR_PREFIX + "link")
        member.type = tarfile.SYMTYPE
        member.linkname = "/etc/passwd"
        digest = self.package(extra=member)
        with self.assertRaisesRegex(ValueError, "link or non-file"):
            self.install(digest)

    def test_unrecognized_destination_is_preserved(self):
        destination = self.runtime / ("codex-" + self.version)
        destination.mkdir(parents=True)
        (destination / "keep").write_text("existing content")
        with self.assertRaises((ValueError, OSError)):
            self.install(self.package())
        self.assertEqual((destination / "keep").read_text(), "existing content")

    def test_staging_symlink_is_preserved_and_rejected(self):
        self.runtime.mkdir()
        staging = self.runtime / (".codex-" + self.version + ".next")
        staging.symlink_to(self.root)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.install(self.package())
        self.assertTrue(staging.is_symlink())

    def test_duplicate_member_is_rejected(self):
        member = tarfile.TarInfo(overlay.VENDOR_PREFIX + "bin/codex")
        member.size = 1
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            self.install(self.package(extra=member))

    def test_stale_staging_from_interrupted_install_can_be_retried(self):
        staging = self.runtime / (".codex-" + self.version + ".next")
        staging.mkdir(parents=True)
        (staging / "partial").write_text("interrupted write")
        destination = self.install(self.package())
        self.assertTrue((destination / "bin/codex").exists())
        self.assertFalse(staging.exists())


class RuntimeArchiveGateTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.apk = Path(self.temp.name) / "fixture.apk"
        self.assets = {
            name.removeprefix("/assets/cheby-runtime/"): b"fixture asset"
            for name in gate.REQUIRED_RUNTIME_ASSETS
        }
        for name in gate.RUNTIME_SKILL_ASSETS:
            self.assets[name] = (
                b"Use GLM native image input. The independent vision MCP is disabled for this release."
            )
        self.assets["codex-base-instructions.md"] = b"Pinned upstream instructions."
        self.assets["mobile-experience-instructions.md"] = (
            b"Prefer native apps and honor explicit user browser/AppGallery restrictions."
        )
        instructions_digest = hashlib.sha256(
            self.assets["codex-base-instructions.md"]
        ).hexdigest()
        self.assets["provider-launcher.py"] = (
            f"BASE_INSTRUCTIONS_SHA256 = '{instructions_digest}'\n"
        ).encode()
        for name, package_name in gate.PINNED_NATIVE_APP_SKILLS.items():
            self.assets[name] += (
                f" Open {package_name} with android_open_app. "
                "Never use `android_open_url` for this native app."
            ).encode()
        self.assets["skill-yandex-go-food-order.md"] += (
            b" Before opening any app, require delivery address, food preference, and budget; "
            b"stop the turn without calling a phone tool when one is missing. Never inherit an "
            b"item, cart, fulfillment mode, tip, or payment choice from an earlier conversation."
        )
        self.lock = dict(gate.PINNED_RUNTIME_LOCK)
        for asset_key, digest_key in (
            ("DEBIAN_ROOTFS_ASSET", "DEBIAN_ROOTFS_SHA256"),
            ("TERMUX_PROOT_OVERLAY_ASSET", "TERMUX_PROOT_OVERLAY_SHA256"),
            ("CODEX_ARCHIVE_ASSET", "CODEX_ARCHIVE_SHA256"),
        ):
            self.lock[digest_key] = hashlib.sha256(self.assets[self.lock[asset_key]]).hexdigest()
        patcher = patch.dict(gate.PINNED_RUNTIME_LOCK, self.lock, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.assets["runtime.lock"] = "".join(f"{key}='{value}'\n" for key, value in self.lock.items()).encode()

    def write_apk(self, compress_codex=False):
        self.assets["runtime-assets.sha256"] = "".join(
            f"{hashlib.sha256(content).hexdigest()}  {name}\n"
            for name, content in sorted(self.assets.items()) if name != "runtime-assets.sha256"
        ).encode()
        with zipfile.ZipFile(self.apk, "w") as archive:
            for name, content in self.assets.items():
                compression = zipfile.ZIP_DEFLATED if compress_codex and name == "codex-linux-arm64.tgz" else zipfile.ZIP_STORED
                archive.writestr("assets/cheby-runtime/" + name, content, compress_type=compression)

    def test_complete_locked_offline_assets_pass(self):
        self.write_apk()
        self.assertTrue(gate.runtime_assets_are_locked(self.apk))
        self.assertTrue(gate.runtime_is_offline_and_bundled(self.apk))
        self.assertTrue(gate.runtime_skills_use_native_glm_images(self.apk))
        self.assertTrue(gate.runtime_omits_independent_vision_mcp(self.apk))
        self.assertTrue(gate.runtime_skills_pin_native_apps(self.apk))
        self.assertTrue(gate.runtime_food_skill_requires_fresh_intent(self.apk))
        self.assertTrue(gate.runtime_blocks_browser_tool(self.apk))
        self.assertTrue(gate.runtime_instructions_digest_is_pinned(self.apk))

    def test_provider_launcher_must_pin_bundled_instructions(self):
        self.assets["provider-launcher.py"] = (
            b"BASE_INSTRUCTIONS_SHA256 = '" + b"0" * 64 + b"'\n"
        )
        self.write_apk()
        self.assertFalse(gate.runtime_instructions_digest_is_pinned(self.apk))

    def test_skill_cannot_restore_independent_vision_route(self):
        name = next(iter(gate.RUNTIME_SKILL_ASSETS))
        self.assets[name] = (
            b"Use GLM native image input, then call the configured independent vision MCP."
        )
        self.write_apk()
        self.assertFalse(gate.runtime_skills_use_native_glm_images(self.apk))

    def test_provider_launcher_cannot_restore_independent_vision_route(self):
        self.assets["provider-launcher.py"] += b"\nmcp_servers.cheby_vision\n"
        self.write_apk()
        self.assertFalse(gate.runtime_omits_independent_vision_mcp(self.apk))

    def test_vision_mcp_asset_is_rejected(self):
        self.assets["vision-mcp.py"] = b"retired"
        self.write_apk()
        self.assertFalse(gate.runtime_omits_independent_vision_mcp(self.apk))

    def test_every_skill_must_declare_native_glm_image_input(self):
        name = next(iter(gate.RUNTIME_SKILL_ASSETS))
        self.assets[name] = b"Use readable UI nodes only."
        self.write_apk()
        self.assertFalse(gate.runtime_skills_use_native_glm_images(self.apk))

    def test_native_app_skill_must_pin_package_and_open_app_tool(self):
        name = "skill-yandex-go-food-order.md"
        self.assets[name] = self.assets[name].replace(b"ru.yandex.taxi", b"go.yandex")
        self.write_apk()
        self.assertFalse(gate.runtime_skills_pin_native_apps(self.apk))

    def test_native_app_skill_must_forbid_browser_fallback(self):
        name = "skill-cian-rental-finder.md"
        self.assets[name] = self.assets[name].replace(
            b"Never use `android_open_url` for this native app.",
            b"Use android_open_url when convenient.",
        )
        self.write_apk()
        self.assertFalse(gate.runtime_skills_pin_native_apps(self.apk))

    def test_food_skill_must_reject_stale_cart_intent(self):
        name = "skill-yandex-go-food-order.md"
        self.assets[name] = self.assets[name].replace(
            b"Never inherit an item, cart, fulfillment mode, tip, or payment choice",
            b"Reuse any item and payment choice from the current cart",
        )
        self.write_apk()
        self.assertFalse(gate.runtime_food_skill_requires_fresh_intent(self.apk))

    def test_embedded_phone_tools_must_omit_browser_launcher(self):
        self.assets["local_mcp.py"] = b'PHONE_TOOL_MAP = {"android_open_url": "open_url"}'
        self.write_apk()
        self.assertFalse(gate.runtime_blocks_browser_tool(self.apk))

    def test_rehashed_manifest_cannot_replace_the_pinned_codex_archive(self):
        self.assets["codex-linux-arm64.tgz"] = b"different archive"
        self.write_apk()
        self.assertFalse(gate.runtime_assets_are_locked(self.apk))

    def test_missing_provider_script_fails(self):
        del self.assets["provider-launcher.py"]
        self.write_apk()
        self.assertFalse(gate.runtime_assets_are_locked(self.apk))

    def test_recompressed_codex_archive_fails_offline_bundle_gate(self):
        self.write_apk(compress_codex=True)
        self.assertFalse(gate.runtime_is_offline_and_bundled(self.apk))


class OfficialAppServerIntegrationTest(unittest.TestCase):
    def test_cian_skill_requires_fixed_scope_exhaustion_before_completion(self):
        skill = (ROOT / "skills/cian-rental-finder/SKILL.md").read_text()
        report = (ROOT / "skills/cian-rental-finder/references/report.md").read_text()
        self.assertIn("at most 1.5 km by an actual walking route", skill)
        self.assertIn("Do not end the task merely because", skill)
        self.assertIn("Before `task_complete`", skill)
        self.assertIn("credible exhaustion evidence", skill)
        self.assertIn("Never ask to raise the known budget", skill)
        self.assertIn("best 5–10", skill)
        self.assertIn("fully qualified homes from the verified pool", skill)
        self.assertIn("not the first 5–10 opened", skill)
        self.assertIn("Every Cian and Yandex link must have been observed", report)
        self.assertIn("never more than twelve", report)
        self.assertIn("no landlord was contacted", report)

    def test_packaged_startup_uses_official_authenticated_websocket(self):
        script = (ROOT / "tools/standalone/phone_start_codex_appserver.sh").read_text()
        build = (ROOT / "Android/appliance/build.gradle.kts").read_text()
        self.assertIn("app-server", script)
        self.assertIn("--listen ws://127.0.0.1:4500", script)
        self.assertIn("--ws-auth capability-token", script)
        self.assertIn("--ws-token-sha256", script)
        self.assertIn("provider-launcher.py", script)
        self.assertNotIn("codex-stdio-bridge.mjs", build)
        self.assertNotIn("/assets/cheby-runtime/codex-stdio-bridge.mjs", gate.REQUIRED_RUNTIME_ASSETS)

    def test_model_proxy_is_scoped_to_official_app_server(self):
        script = (ROOT / "tools/standalone/phone_start_codex_appserver.sh").read_text()
        self.assertIn('MODEL_PROXY_FILE="$STATE_ROOT/model-proxy-url"', script)
        self.assertIn("^http://127\\.0\\.0\\.1:[0-9]{1,5}$", script)
        self.assertIn('HTTPS_PROXY="$model_proxy_url"', script)
        self.assertIn('HTTP_PROXY="$model_proxy_url"', script)
        self.assertIn('NO_PROXY="127.0.0.1,localhost"', script)
        self.assertNotIn("0.0.0.0", script)

    def test_runtime_version_bump_forces_existing_appliance_refresh(self):
        version = gate.PINNED_RUNTIME_LOCK["CHEBY_RUNTIME_VERSION"]
        self.assertEqual(version, "4.1.0-dev32")
        files = (
            ROOT / "Android/appliance/runtime/runtime.lock",
            ROOT / "Android/appliance/runtime/provision-runtime.sh",
            ROOT / "Android/appliance/runtime/provision-guest.sh",
            ROOT / "Android/appliance/src/main/java/com/termux/app/TermuxEmbeddedRuntime.java",
        )
        for path in files:
            self.assertIn(version, path.read_text())

    def test_phone_runtime_registers_persistent_memory_and_skill_servers(self):
        provision = (
            ROOT / "Android/appliance/runtime/provision-guest.sh"
        ).read_text()
        instructions = (
            ROOT / "Android/appliance/runtime/mobile-experience-instructions.md"
        ).read_text()

        self.assertIn("LOCAL_MCP_DATA_ROOT='/root/.codex/local-mcp'", provision)
        self.assertIn("codex mcp add offline_memory", provision)
        self.assertIn('"$MCP_PATH" memory', provision)
        self.assertIn("codex mcp add offline_skill", provision)
        self.assertIn('"$MCP_PATH" skill', provision)
        self.assertNotIn('rm -rf "$LOCAL_MCP_DATA_ROOT"', provision)
        self.assertIn("preserve it, existing memory, and authentication through app and CLI updates", instructions)


if __name__ == "__main__":
    unittest.main()

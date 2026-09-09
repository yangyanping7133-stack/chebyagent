import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


MODULE_PATH = Path(__file__).with_name("debian_source_inventory.py")
SPEC = importlib.util.spec_from_file_location("debian_source_inventory", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class DebianSourceInventoryTest(unittest.TestCase):
    def test_exact_sources_deduplicates_binary_packages(self):
        inventory = {"packages": [
            {"origin": "debian", "source_package": "util-linux", "source_version": "2.41-5"},
            {"origin": "debian", "source_package": "util-linux", "source_version": "2.41-5"},
            {"origin": "termux-bootstrap", "source_package": "bash", "source_version": "5"},
        ]}
        self.assertEqual([("util-linux", "2.41-5")], MODULE.exact_sources(inventory))

    def test_source_projection_ignores_unrelated_components(self):
        inventory = {"packages": [
            {"origin": "debian", "source_package": "util-linux", "source_version": "2.41-5"},
        ], "extra_components": [{"component": "Codex", "version": "0.153.3"}]}
        changed = json.loads(json.dumps(inventory))
        changed["extra_components"][0]["version"] = "0.153.4"
        self.assertEqual(
            MODULE.source_projection_sha256(inventory),
            MODULE.source_projection_sha256(changed),
        )

    def test_source_projection_changes_with_debian_identity(self):
        inventory = {"packages": [
            {"origin": "debian", "source_package": "util-linux", "source_version": "2.41-5"},
        ]}
        changed = json.loads(json.dumps(inventory))
        changed["packages"][0]["source_version"] = "2.41-6"
        self.assertNotEqual(
            MODULE.source_projection_sha256(inventory),
            MODULE.source_projection_sha256(changed),
        )

    def test_safe_filename_rejects_paths(self):
        for name in ("../secret", "/tmp/a", "a/b", "", "."):
            with self.subTest(name=name), self.assertRaises(ValueError):
                MODULE.safe_filename(name)

    def test_dsc_sha256_entries(self):
        dsc = b"""-----BEGIN PGP SIGNED MESSAGE-----\nHash: SHA512\n\nFormat: 3.0 (quilt)\nChecksums-Sha256:\n aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa 3 source.orig.tar.xz\n bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb 4 source-1.debian.tar.xz\nFiles:\n deadbeef 3 misc optional source.orig.tar.xz\n-----BEGIN PGP SIGNATURE-----\n"""
        self.assertEqual({
            "source.orig.tar.xz": ("a" * 64, 3),
            "source-1.debian.tar.xz": ("b" * 64, 4),
        }, MODULE.dsc_sha256_entries(dsc))

    def test_verify_package_cross_checks_payload(self):
        dsc = b"Checksums-Sha256:\n " + b"a" * 64 + b" 3 source.tar.xz\n"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "source.dsc").write_bytes(dsc)
            files = [
                {"name": "source.dsc", "path": "source.dsc", "sha256": MODULE.digest(dsc), "size": len(dsc)},
                {"name": "source.tar.xz", "path": "payload", "sha256": "a" * 64, "size": 3},
            ]
            result = MODULE.verify_package("source", "1", files, root)
            self.assertEqual("PRESENT_NOT_VERIFIED", result["dsc_signature_state"])

    def test_select_records_uses_current_dsc_filename_aliases(self):
        payload = b"payload"
        payload_sha1 = MODULE.digest(payload, "sha1")
        payload_sha256 = MODULE.digest(payload)
        dsc = ("Checksums-Sha256:\n " + payload_sha256 +
               " 7 source_2.orig.tar.xz\n").encode()
        dsc_sha1 = MODULE.digest(dsc, "sha1")
        metadata = {
            dsc_sha1: [{"archive_name": "debian", "name": "source_2-1.dsc",
                        "size": len(dsc), "path": "/pool/main/s/source"}],
            payload_sha1: [
                {"archive_name": "debian", "name": "source_1.orig.tar.xz",
                 "size": len(payload), "path": "/pool/main/s/source"},
                {"archive_name": "debian", "name": "source_2.orig.tar.xz",
                 "size": len(payload), "path": "/pool/main/s/source"},
            ],
        }
        original_fetch = MODULE.fetch
        MODULE.fetch = lambda url, **kwargs: dsc
        try:
            records, excluded = MODULE.select_records("source", "2-1", [dsc_sha1, payload_sha1], metadata)
        finally:
            MODULE.fetch = original_fetch
        self.assertEqual("source_2.orig.tar.xz", records[1]["name"])
        self.assertEqual([], excluded)

    def test_select_records_reports_snapshot_association_absent_from_dsc(self):
        payload = b"payload"
        payload_sha1 = MODULE.digest(payload, "sha1")
        payload_sha256 = MODULE.digest(payload)
        extra_sha1 = "c" * 40
        dsc = ("Checksums-Sha256:\n " + payload_sha256 +
               " 7 source_2.tar.xz\n").encode()
        dsc_sha1 = MODULE.digest(dsc, "sha1")
        metadata = {
            dsc_sha1: [{"archive_name": "debian", "name": "source_2.dsc", "size": len(dsc)}],
            payload_sha1: [{"archive_name": "debian", "name": "source_2.tar.xz", "size": 7}],
            extra_sha1: [{"archive_name": "debian", "name": "source_2.git.tar.xz", "size": 8}],
        }
        original_fetch = MODULE.fetch
        MODULE.fetch = lambda url, **kwargs: dsc
        try:
            records, excluded = MODULE.select_records(
                "source", "2", [dsc_sha1, payload_sha1, extra_sha1], metadata)
        finally:
            MODULE.fetch = original_fetch
        self.assertEqual(2, len(records))
        self.assertEqual(extra_sha1, excluded[0]["sha1"])

    def test_verify_existing_rechecks_manifest_files_and_dsc(self):
        payload = b"payload"
        payload_sha1 = MODULE.digest(payload, "sha1")
        payload_sha256 = MODULE.digest(payload)
        dsc = ("Checksums-Sha256:\n " + payload_sha256 +
               " 7 source_2.tar.xz\n").encode()
        dsc_sha1 = MODULE.digest(dsc, "sha1")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inventory_path = root / "inventory.json"
            inventory_path.write_text('{"packages":[{"origin":"debian",'
                                      '"source_package":"source","source_version":"2"}]}')
            output = root / "output"
            for sha1, name, data in ((dsc_sha1, "source_2.dsc", dsc),
                                     (payload_sha1, "source_2.tar.xz", payload)):
                target = output / "files" / sha1 / name
                target.parent.mkdir(parents=True)
                target.write_bytes(data)
            files = [
                {"sha1": dsc_sha1, "name": "source_2.dsc", "size": len(dsc),
                 "path": f"files/{dsc_sha1}/source_2.dsc", "sha256": MODULE.digest(dsc)},
                {"sha1": payload_sha1, "name": "source_2.tar.xz", "size": len(payload),
                 "path": f"files/{payload_sha1}/source_2.tar.xz", "sha256": payload_sha256},
            ]
            manifest = {
                "schema_version": 1,
                "input_inventory_sha256": MODULE.digest(inventory_path.read_bytes()),
                "package_count": 1,
                "file_count": 2,
                "packages": [{"package": "source", "version": "2", "files": files}],
            }
            (output / "manifest.json").write_text(json.dumps(manifest))
            result = MODULE.verify_existing(inventory_path, output)
            self.assertEqual((1, 2, 7 + len(dsc)),
                             (result["packages"], result["files"], result["bytes"]))

    def test_verify_existing_rejects_modified_file(self):
        payload = b"payload"
        payload_sha1 = MODULE.digest(payload, "sha1")
        payload_sha256 = MODULE.digest(payload)
        dsc = ("Checksums-Sha256:\n " + payload_sha256 +
               " 7 source_2.tar.xz\n").encode()
        dsc_sha1 = MODULE.digest(dsc, "sha1")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inventory_path = root / "inventory.json"
            inventory_path.write_text('{"packages":[{"origin":"debian",'
                                      '"source_package":"source","source_version":"2"}]}')
            output = root / "output"
            for sha1, name, data in ((dsc_sha1, "source_2.dsc", dsc),
                                     (payload_sha1, "source_2.tar.xz", payload)):
                target = output / "files" / sha1 / name
                target.parent.mkdir(parents=True)
                target.write_bytes(data)
            files = [
                {"sha1": dsc_sha1, "name": "source_2.dsc", "size": len(dsc),
                 "path": f"files/{dsc_sha1}/source_2.dsc", "sha256": MODULE.digest(dsc)},
                {"sha1": payload_sha1, "name": "source_2.tar.xz", "size": len(payload),
                 "path": f"files/{payload_sha1}/source_2.tar.xz", "sha256": payload_sha256},
            ]
            manifest = {
                "schema_version": 1,
                "input_inventory_sha256": MODULE.digest(inventory_path.read_bytes()),
                "package_count": 1,
                "file_count": 2,
                "packages": [{"package": "source", "version": "2", "files": files}],
            }
            (output / "manifest.json").write_text(json.dumps(manifest))
            (output / files[1]["path"]).write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "digest mismatch"):
                MODULE.verify_existing(inventory_path, output)

    def test_verify_schema1_allows_unrelated_inventory_change(self):
        payload = b"payload"
        payload_sha1 = MODULE.digest(payload, "sha1")
        payload_sha256 = MODULE.digest(payload)
        dsc = ("Checksums-Sha256:\n " + payload_sha256 +
               " 7 source_2.tar.xz\n").encode()
        dsc_sha1 = MODULE.digest(dsc, "sha1")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inventory_path = root / "inventory.json"
            inventory_path.write_text(json.dumps({
                "packages": [{"origin": "debian", "source_package": "source",
                              "source_version": "2"}],
                "extra_components": [{"component": "Codex", "version": "0.153.4"}],
            }))
            output = root / "output"
            files = []
            for sha1, name, data in ((dsc_sha1, "source_2.dsc", dsc),
                                     (payload_sha1, "source_2.tar.xz", payload)):
                target = output / "files" / sha1 / name
                target.parent.mkdir(parents=True)
                target.write_bytes(data)
                files.append({"sha1": sha1, "name": name, "size": len(data),
                              "path": f"files/{sha1}/{name}",
                              "sha256": MODULE.digest(data)})
            manifest = {
                "schema_version": 1,
                "input_inventory_sha256": "0" * 64,
                "package_count": 1,
                "file_count": 2,
                "packages": [{"package": "source", "version": "2", "files": files}],
            }
            (output / "manifest.json").write_text(json.dumps(manifest))
            result = MODULE.verify_existing(inventory_path, output)
            self.assertEqual("legacy_ordered_source_identities", result["inventory_binding"])

    def test_verify_schema2_rejects_changed_debian_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inventory = {"packages": [
                {"origin": "debian", "source_package": "source", "source_version": "3"},
            ]}
            inventory_path = root / "inventory.json"
            inventory_path.write_text(json.dumps(inventory))
            output = root / "output"
            output.mkdir()
            manifest = {
                "schema_version": 2,
                "input_inventory_sha256": "0" * 64,
                "input_debian_sources_sha256": MODULE.source_projection_sha256({
                    "packages": [{"origin": "debian", "source_package": "source",
                                  "source_version": "2"}],
                }),
                "package_count": 0,
                "file_count": 0,
                "packages": [],
            }
            (output / "manifest.json").write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, "projection digest mismatch"):
                MODULE.verify_existing(inventory_path, output)


if __name__ == "__main__":
    unittest.main()

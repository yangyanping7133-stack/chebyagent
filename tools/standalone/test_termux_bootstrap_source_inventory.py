import importlib.util
import io
from pathlib import Path
import tarfile
import tempfile
import unittest


MODULE_PATH = Path(__file__).with_name("termux_bootstrap_source_inventory.py")
SPEC = importlib.util.spec_from_file_location("termux_bootstrap_source_inventory", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class TermuxBootstrapSourceInventoryTest(unittest.TestCase):
    def test_tar_source_tree_digest_ignores_outer_directory_and_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archives = []
            for index, outer in enumerate(("foot-1.21.0", "foot-commit")):
                path = root / f"source-{index}.tar.gz"
                with tarfile.open(path, "w:gz") as archive:
                    info = tarfile.TarInfo(f"{outer}/script.sh")
                    payload = b"#!/bin/sh\n"
                    info.size = len(payload)
                    info.mode = 0o755
                    info.mtime = 100 + index
                    archive.addfile(info, io.BytesIO(payload))
                archives.append(path)
            self.assertNotEqual(MODULE.digest_file(archives[0]), MODULE.digest_file(archives[1]))
            self.assertEqual(
                MODULE.tar_source_tree_digest(archives[0]),
                MODULE.tar_source_tree_digest(archives[1]),
            )

    def test_expands_allowlisted_shell_parameter_forms(self):
        variables = {"TERMUX_PKG_VERSION": "1:3.4.1", "_MAIN_VERSION": "5.2"}
        self.assertEqual(
            "openssl-3.4.1/curl-1:3_4_1/1:3.4/1/bash52",
            MODULE.expand_template(
                "openssl-${TERMUX_PKG_VERSION:2}/curl-${TERMUX_PKG_VERSION//./_}/"
                "${TERMUX_PKG_VERSION%.*}/${TERMUX_PKG_VERSION##*.}/"
                "bash${_MAIN_VERSION/./}",
                variables,
            ),
        )

    def test_rejects_command_substitution(self):
        with self.assertRaisesRegex(ValueError, "Command substitution"):
            MODULE.expand_template("https://example/$(uname).tgz", {})

    def test_parses_only_top_level_scalar_assignments(self):
        recipe = """\
_MAIN_VERSION=8.2
_PATCH_VERSION=13
TERMUX_PKG_VERSION=${_MAIN_VERSION}.${_PATCH_VERSION}
TERMUX_PKG_SRCURL=https://example/readline-${_MAIN_VERSION}.tar.gz
TERMUX_PKG_SHA256=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
inside() {
\tTERMUX_PKG_VERSION=evil
}
"""
        variables = MODULE.parse_scalar_environment(recipe)
        self.assertEqual("8.2.13", variables["TERMUX_PKG_VERSION"])
        self.assertEqual("https://example/readline-8.2.tar.gz", variables["TERMUX_PKG_SRCURL"])

    def test_patch_sequence_must_be_complete(self):
        recipe = "\n".join([
            "PATCH_CHECKSUMS[001]=" + "a" * 64,
            "PATCH_CHECKSUMS[003]=" + "b" * 64,
        ])
        with self.assertRaisesRegex(ValueError, "sequence is incomplete"):
            MODULE._patch_ingredients("bash", recipe, "bash", "5.2", 3)

    def test_verify_existing_rejects_mutated_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "upstream" / "pkg" / "001-source.tgz"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"source")
            sha = MODULE.digest_file(source)
            manifest = {
                "schema_version": 1,
                "binary_package_count": 75,
                "source_recipe_package_count": 71,
                "ingredient_count": 1,
                "packages": [{"ingredients": [{
                    "kind": "upstream-source", "path": "upstream/pkg/001-source.tgz",
                    "sha256": sha, "declared_sha256": sha,
                }]}],
                "files": [{
                    "path": "upstream/pkg/001-source.tgz", "sha256": sha, "size": 6,
                }],
            }
            (root / "manifest.json").write_text(__import__("json").dumps(manifest))
            self.assertEqual("PASS", MODULE.verify_existing(root)["outcome"])
            source.write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "verification failed"):
                MODULE.verify_existing(root)


if __name__ == "__main__":
    unittest.main()

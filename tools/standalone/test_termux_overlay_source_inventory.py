import importlib.util
import io
from pathlib import Path
import tarfile
import unittest


MODULE_PATH = Path(__file__).with_name("termux_overlay_source_inventory.py")
SPEC = importlib.util.spec_from_file_location("termux_overlay_source_inventory", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class TermuxOverlaySourceInventoryTest(unittest.TestCase):
    def archive(self, names):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w:gz") as archive:
            for name, data in names.items():
                member = tarfile.TarInfo(name)
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
        return stream.getvalue()

    def test_recipe_files_selects_only_exact_package_directory(self):
        commit = "a" * 40
        archive = self.archive({
            f"termux-packages-{commit}/packages/proot/build.sh": b"recipe",
            f"termux-packages-{commit}/packages/proot/fix.patch": b"patch",
            f"termux-packages-{commit}/packages/other/build.sh": b"other",
        })
        self.assertEqual(
            [("build.sh", b"recipe"), ("fix.patch", b"patch")],
            MODULE.recipe_files("proot", commit, archive),
        )

    def test_recipe_files_requires_build_script(self):
        commit = "b" * 40
        archive = self.archive({
            f"termux-packages-{commit}/packages/proot/fix.patch": b"patch",
        })
        with self.assertRaisesRegex(ValueError, "Missing build.sh"):
            MODULE.recipe_files("proot", commit, archive)

    def test_fixed_coordinates_match_overlay_versions(self):
        self.assertEqual("5.1.107.89", MODULE.PACKAGES["proot"]["version"])
        self.assertEqual("0.7", MODULE.PACKAGES["libandroid-shmem"]["version"])
        self.assertEqual("2.4.3", MODULE.PACKAGES["libtalloc"]["version"])
        for coordinate in MODULE.PACKAGES.values():
            self.assertRegex(coordinate["source_sha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(coordinate["recipe_commit"], r"^[0-9a-f]{40}$")


if __name__ == "__main__":
    unittest.main()

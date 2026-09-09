import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import create_source_snapshot as snapshot


class SourceSnapshotVerificationTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "snapshot"
        self.root.mkdir()
        self.file = self.root / "docs" / "guide.md"
        self.file.parent.mkdir()
        self.file.write_text("guide\n")
        record = {
            "path": "docs/guide.md",
            "sha256": hashlib.sha256(self.file.read_bytes()).hexdigest(),
            "bytes": self.file.stat().st_size,
            "mode": oct(self.file.stat().st_mode & 0o777),
        }
        (self.root / snapshot.MANIFEST_NAME).write_text(json.dumps({
            "source_head": "abc",
            "source_branch": "test",
            "files": [record],
        }))

    def tearDown(self):
        self.temporary.cleanup()

    def test_valid_snapshot_verifies(self):
        result = snapshot.verify_snapshot(self.root)
        self.assertEqual(result["files_verified"], 1)

    def test_tampered_snapshot_fails(self):
        self.file.write_text("changed\n")
        with self.assertRaises(ValueError):
            snapshot.verify_snapshot(self.root)

    def test_extra_file_fails(self):
        (self.root / "extra.txt").write_text("extra\n")
        with self.assertRaises(ValueError):
            snapshot.verify_snapshot(self.root)

    def test_symlink_fails(self):
        link = self.root / "link.md"
        link.symlink_to(self.file)
        with self.assertRaises(ValueError):
            snapshot.verify_snapshot(self.root)


if __name__ == "__main__":
    unittest.main()

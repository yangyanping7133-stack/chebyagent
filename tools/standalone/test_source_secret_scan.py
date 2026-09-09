import unittest
from pathlib import Path
import tempfile

import source_secret_scan


class SourceSecretScanTest(unittest.TestCase):
    def test_dotted_provider_token_is_detected_without_matching_plain_hash(self):
        pattern = source_secret_scan.RULES["provider_token_dotted"]
        token = b"0123456789abcdef" + b"0123456789abcdef.secretPart_123456"
        self.assertIsNotNone(pattern.search(token))
        self.assertIsNone(pattern.search(b"0123456789abcdef0123456789abcdef"))

    def test_dotted_provider_token_requires_exact_identifier_shape(self):
        pattern = source_secret_scan.RULES["provider_token_dotted"]
        self.assertIsNone(pattern.search(b"short.secretPart_123456"))
        non_hex_identifier = (
            b"g123456789abcdef" + b"0123456789abcdef.secretPart_123456"
        )
        self.assertIsNone(pattern.search(non_hex_identifier))

    def test_snapshot_files_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "snapshot"
            root.mkdir()
            target = root / "target.txt"
            target.write_text("safe\n")
            (root / "link.txt").symlink_to(target)
            with self.assertRaises(ValueError):
                source_secret_scan.snapshot_files(root)


if __name__ == "__main__":
    unittest.main()

import tempfile
import unittest
from pathlib import Path
import zipfile

import delivery_apk_secret_scan


class DeliveryApkSecretScanTest(unittest.TestCase):
    def make_apk(self, root: Path, members: dict[str, bytes]) -> Path:
        apk = root / "candidate.apk"
        with zipfile.ZipFile(apk, "w") as archive:
            for name, value in members.items():
                archive.writestr(name, value, compress_type=zipfile.ZIP_DEFLATED)
        return apk

    def test_scans_decompressed_member_without_emitting_value(self):
        with tempfile.TemporaryDirectory() as temporary:
            token = b"0123456789abcdef0123456789abcdef" + b".secretPart_123456"
            result = delivery_apk_secret_scan.scan_apk(
                self.make_apk(Path(temporary), {"assets/settings.txt": b"prefix " + token})
            )
            self.assertEqual("REVIEW", result["outcome"])
            self.assertEqual("provider_token_dotted", result["findings"][0]["rule"])
            self.assertNotIn(token.decode(), str(result))

    def test_reports_safe_apk_without_candidates(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = delivery_apk_secret_scan.scan_apk(
                self.make_apk(Path(temporary), {"assets/config.txt": b"provider=glm\n"})
            )
            self.assertEqual("NO_CANDIDATES_FOUND", result["outcome"])
            self.assertEqual([], result["findings"])

    def test_rejects_unsafe_member(self):
        with tempfile.TemporaryDirectory() as temporary:
            apk = self.make_apk(Path(temporary), {"../escape": b"safe"})
            with self.assertRaises(ValueError):
                delivery_apk_secret_scan.scan_apk(apk)


if __name__ == "__main__":
    unittest.main()

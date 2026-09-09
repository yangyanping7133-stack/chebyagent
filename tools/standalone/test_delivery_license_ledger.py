import unittest

import delivery_license_ledger


class DeliveryLicenseLedgerTest(unittest.TestCase):
    def test_every_component_has_explicit_blocked_disposition(self):
        runtime = {
            "packages": [{
                "origin": "debian", "package": "demo", "version": "1", "architecture": "arm64",
                "source_package": "demo", "source_version": "1", "source_identity_basis": "fixture",
                "license_evidence_state": "PRESENT", "license_evidence": [{"path": "copyright"}],
            }],
            "extra_components": [{"component": "Codex", "origin": "codex", "metadata": {"version": "1"}}],
        }
        dependencies = {"artifacts": [{"coordinate": "example:library:1"}]}
        notices = {
            "declarations": [{"coordinate": "example:library:1", "licenses": [], "scm_url": ""}],
            "notices": [],
        }
        result = delivery_license_ledger.build(runtime, dependencies, notices)
        self.assertEqual(3, result["counts"]["component_rows"])
        self.assertEqual(3, result["counts"]["component_rows_blocked"])
        self.assertEqual(0, result["counts"]["component_rows_closed"])
        self.assertEqual("BLOCKED_REVIEW_REQUIRED", result["outcome"])

    def test_duplicate_component_identity_fails_closed(self):
        item = {
            "origin": "debian", "package": "demo", "version": "1", "architecture": "arm64",
            "license_evidence_state": "MISSING", "license_evidence": [],
        }
        with self.assertRaises(ValueError):
            delivery_license_ledger.build({"packages": [item, dict(item)]}, {}, {})


if __name__ == "__main__":
    unittest.main()

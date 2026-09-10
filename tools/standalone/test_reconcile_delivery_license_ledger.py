import unittest

import reconcile_delivery_license_ledger


class ReconcileDeliveryLicenseLedgerTest(unittest.TestCase):
    def policy(self):
        return {
            "schema": 1,
            "release": "0.7.0",
            "policy_id": "fixture",
            "expected_component_rows": 3,
            "release_evidence": {
                "ledger_sha256": "a" * 64,
                "required_assets": {"source.tar": "b" * 64},
            },
            "license_identity_overrides": {
                "maven:example:missing:1": {"license": "Apache-2.0", "evidence": "fixture"},
            },
            "runtime_source_asset_by_origin": {"debian": "source.tar"},
            "extra_component_source_assets": {"extra:codex": "source.tar"},
            "release_level_findings": [],
            "distributor_attestation": {"state": "PENDING", "required_action": "accept"},
        }

    def ledger(self):
        return {
            "schema": 1,
            "counts": {"component_rows": 3},
            "components": [
                {"id": "debian:demo", "kind": "runtime_package", "origin": "debian",
                 "license_evidence_state": "PRESENT"},
                {"id": "maven:example:missing:1", "kind": "android_maven_component",
                 "declaration_state": "MISSING"},
                {"id": "extra:codex", "kind": "runtime_extra_component"},
            ],
        }

    def test_closes_mechanical_evidence_but_not_legal_review(self):
        result = reconcile_delivery_license_ledger.reconcile(
            self.ledger(), self.policy(), {"source.tar": "b" * 64}, "a" * 64
        )
        self.assertEqual(3, result["counts"]["license_identity_closed"])
        self.assertEqual(0, result["counts"]["license_identity_missing"])
        self.assertEqual(0, result["counts"]["row_level_legal_conclusions"])
        self.assertEqual(1, result["counts"]["release_level_distributor_decisions_open"])
        self.assertEqual(
            "ENGINEERING_EVIDENCE_RECONCILED_DISTRIBUTOR_ATTESTATION_REQUIRED",
            result["outcome"],
        )

    def test_wrong_ledger_digest_fails_closed(self):
        with self.assertRaises(ValueError):
            reconcile_delivery_license_ledger.reconcile(
                self.ledger(), self.policy(), {"source.tar": "b" * 64}, "c" * 64
            )

    def test_unknown_override_fails_closed(self):
        policy = self.policy()
        policy["license_identity_overrides"]["maven:unknown:1"] = {"license": "MIT"}
        with self.assertRaises(ValueError):
            reconcile_delivery_license_ledger.reconcile(
                self.ledger(), policy, {"source.tar": "b" * 64}, "a" * 64
            )


if __name__ == "__main__":
    unittest.main()

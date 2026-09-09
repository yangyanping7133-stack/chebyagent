import argparse
import json
from pathlib import Path
import tempfile
import unittest

import aln_acceptance_ledger as ledger


class AcceptanceLedgerTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "ledger"
        ledger.init_ledger(self.root, "glm-5.3-flash")

    def tearDown(self):
        self.temporary.cleanup()

    def test_fixed_denominators_start_blocked(self):
        report = ledger.summary(self.root)
        self.assertEqual(report["groups"]["primary-21"], {"PASS": 0, "FAIL": 0, "BLOCKED": 21})
        self.assertEqual(report["groups"]["rephrase-7"]["BLOCKED"], 7)
        self.assertEqual(report["groups"]["provider-4"]["BLOCKED"], 4)
        self.assertEqual(report["groups"]["device-5"]["BLOCKED"], 5)
        self.assertEqual(report["groups"]["delivery-6"]["BLOCKED"], 6)
        self.assertTrue(report["evidence_audited"])
        self.assertFalse(report["release_ready"])

    def test_manifest_has_unique_ids(self):
        manifest = json.loads((self.root / "manifest.json").read_text())
        identifiers = [item["id"] for item in manifest["cases"]]
        self.assertEqual(len(identifiers), len(set(identifiers)))

    def args(self, **overrides):
        values = {
            "case": "coffee-basic", "status": "PASS", "reason": "Observed expected terminal",
            "model": "glm-5.3-flash", "actual_prompt": "找附近最好的咖啡厅",
            "terminal": "Three candidates and one justified recommendation", "tools": "android_open_app,android_ui_tree",
            "app_version": "0.7.0", "skill_version": "sha256:test", "retry_count": 0,
            "human_intervention": "none", "duration_ms": 1200, "evidence": ["evidence/case.json"],
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_pass_requires_existing_evidence(self):
        with self.assertRaises(ValueError):
            ledger.record(self.root, self.args())

    def test_scenario_pass_requires_trace_fields(self):
        evidence = self.root / "evidence" / "case.json"
        evidence.write_text("{}\n")
        with self.assertRaises(ValueError):
            ledger.record(self.root, self.args(skill_version=""))

    def test_scenario_pass_accepts_empty_tools_list_when_no_tool_was_used(self):
        evidence = self.root / "evidence" / "case.json"
        evidence.write_text("{}\n")
        ledger.record(self.root, self.args(tools=""))
        self.assertEqual(ledger.summary(self.root)["groups"]["primary-21"]["PASS"], 1)

    def test_developer_operated_run_cannot_pass(self):
        evidence = self.root / "evidence" / "case.json"
        evidence.write_text("{}\n")
        with self.assertRaises(ValueError):
            ledger.record(self.root, self.args(human_intervention="developer-operation"))

    def test_valid_pass_updates_only_one_case(self):
        evidence = self.root / "evidence" / "case.json"
        evidence.write_text("{}\n")
        ledger.record(self.root, self.args())
        report = ledger.summary(self.root)
        self.assertEqual(report["groups"]["primary-21"], {"PASS": 1, "FAIL": 0, "BLOCKED": 20})
        self.assertFalse(report["release_ready"])

    def test_summary_rejects_missing_pass_evidence(self):
        evidence = self.root / "evidence" / "case.json"
        evidence.write_text("{}\n")
        ledger.record(self.root, self.args())
        evidence.unlink()
        with self.assertRaises(ValueError):
            ledger.summary(self.root)

    def test_record_rejects_symlink_evidence(self):
        real = self.root / "evidence" / "real.json"
        link = self.root / "evidence" / "link.json"
        real.write_text("{}\n")
        link.symlink_to(real)
        with self.assertRaises(ValueError):
            ledger.record(self.root, self.args(evidence=["evidence/link.json"]))

    def test_upgrade_adds_delivery_gates_without_changing_existing_result(self):
        manifest_path = self.root / "manifest.json"
        results_path = self.root / "results.json"
        manifest = json.loads(manifest_path.read_text())
        results = json.loads(results_path.read_text())
        delivery_ids = {
            item["id"] for item in manifest["cases"] if item["group"] == "delivery-6"
        }
        manifest["cases"] = [
            item for item in manifest["cases"] if item["id"] not in delivery_ids
        ]
        for identifier in delivery_ids:
            results["results"].pop(identifier)
        manifest["schema_version"] = 1
        results["schema_version"] = 1
        manifest_path.write_text(json.dumps(manifest))
        results_path.write_text(json.dumps(results))

        ledger.upgrade_ledger(self.root)

        report = ledger.summary(self.root)
        self.assertEqual(report["groups"]["delivery-6"], {
            "PASS": 0, "FAIL": 0, "BLOCKED": 6,
        })
        self.assertEqual(report["groups"]["primary-21"], {
            "PASS": 0, "FAIL": 0, "BLOCKED": 21,
        })


if __name__ == "__main__":
    unittest.main()

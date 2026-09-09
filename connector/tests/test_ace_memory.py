"""Real upstream core and separate stdio processes; no provider or private data."""
import json
import os
from pathlib import Path
import subprocess
import sys

import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "cheby_connector/local_mcp.py"


def invoke(tmp_path, name, arguments):
    request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
    result = subprocess.run([sys.executable, "-I", str(SCRIPT), "ace"],
        input=json.dumps(request) + "\n", text=True, capture_output=True, timeout=5,
        env={**os.environ, "CHEBY_LOCAL_MCP_DATA_ROOT": str(tmp_path)})
    assert result.returncode == 0, result.stderr
    response = json.loads(result.stdout)
    return response.get("result", {"isError": True, "error": response.get("error")})


def feedback(**kwargs):
    return {"skill": "yandex-maps-coffee-finder", "base_revision": 0, "feedback_id": "feedback-1",
        "issue": "The result was too wordy", "insight": "Show one short reason per venue", **kwargs}


def check_feedback_survives_new_process_and_rollback(tmp_path):
    assert invoke(tmp_path, "ace_learn", feedback())["structuredContent"]["revision"] == 1
    recalled = invoke(tmp_path, "ace_recall", {"skill": "yandex-maps-coffee-finder"})["structuredContent"]
    assert recalled["strategies"][0]["insight"] == "Show one short reason per venue"
    assert invoke(tmp_path, "ace_recall", {"skill": "another-skill"})["structuredContent"]["strategies"] == []
    assert invoke(tmp_path, "ace_learn", feedback())["structuredContent"]["duplicate"]
    strategy = recalled["strategies"][0]["id"]
    revised = feedback(base_revision=1, feedback_id="feedback-2", replace_id=strategy, insight="Lead with atmosphere, one sentence")
    assert invoke(tmp_path, "ace_learn", revised)["structuredContent"]["strategy_count"] == 1
    assert invoke(tmp_path, "ace_rollback", {"skill": "yandex-maps-coffee-finder", "base_revision": 2, "target_revision": 1})["structuredContent"]["revision"] == 3
    restored = invoke(tmp_path, "ace_recall", {"skill": "yandex-maps-coffee-finder"})["structuredContent"]
    assert restored["strategies"][0]["insight"] == "Show one short reason per venue"
    assert (tmp_path / "ace/yandex-maps-coffee-finder.json").stat().st_mode & 0o077 == 0


def check_rejects_unsafe_or_stale_updates(tmp_path, changes):
    assert invoke(tmp_path, "ace_learn", feedback(**changes))["isError"]
    assert not (tmp_path / "ace/yandex-maps-coffee-finder.json").exists()


def check_different_payload_cannot_reuse_feedback_id(tmp_path):
    invoke(tmp_path, "ace_learn", feedback())
    assert invoke(tmp_path, "ace_learn", feedback(insight="Different instruction"))["isError"]


def check_symlink_store_is_not_followed(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "ace").symlink_to(outside, target_is_directory=True)
    assert invoke(tmp_path, "ace_learn", feedback())["isError"]
    assert list(outside.iterdir()) == []


class AceMemoryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def test_restart_and_rollback(self):
        check_feedback_survives_new_process_and_rollback(self.root)

    def test_bad_input(self):
        for changes in [
            {"skill": "../escape"}, {"base_revision": True}, {"base_revision": 9},
            {"insight": "api_key=example-not-real"}, {"insight": ""},
            {"replace_id": "other-scope-id"}, {"feedback_id": "raw private text"},
        ]:
            with self.subTest(changes=changes):
                check_rejects_unsafe_or_stale_updates(self.root, changes)

    def test_idempotency_conflict(self):
        check_different_payload_cannot_reuse_feedback_id(self.root)

    def test_non_object_arguments(self):
        self.assertTrue(invoke(self.root, 'ace_recall', ['invalid'])["isError"])

    def test_symlink(self):
        check_symlink_store_is_not_followed(self.root)


if __name__ == "__main__":
    unittest.main()

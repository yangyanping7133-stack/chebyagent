#!/usr/bin/env python3
"""Repeatable Protocol V1 validation and event-reducer gate.

Uses only the Python 3.9 standard library. The generated manifest and evidence
contain public fixture identifiers only; they must never contain credentials,
raw Codex identifiers, endpoints, stack traces, or internal file paths.
"""

import argparse
import copy
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTRACT = ROOT / "contracts" / "v1" / "contracts.json"
SCENARIO_DIR = ROOT / "fixtures" / "v1" / "scenarios"
STREAM_DIR = ROOT / "fixtures" / "v1" / "event_streams"
GATE_DIR = ROOT / "fixtures" / "v1" / "gate"
EVIDENCE_DIR = GATE_DIR / "evidence"
MANIFEST_PATH = GATE_DIR / "manifest.json"


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    path.write_text(encoded + "\n", encoding="utf-8")


def relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def is_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def matches_type(value: Any, expected: str) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "nullable-string":
        return value is None or isinstance(value, str)
    if expected == "timestamp":
        return is_timestamp(value)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    return False


class ProtocolValidator:
    def __init__(self, contract: Dict[str, Any]) -> None:
        self.contract = contract
        self.entities = contract["entities"]
        self.enums = contract["enums"]
        self.limits = contract["limits"]

    def validate(self, entity_name: str, value: Any) -> List[str]:
        errors: List[str] = []
        if not isinstance(value, dict):
            return ["$: expected object"]
        spec = self.entities[entity_name]
        for field, expected_type in spec.get("required", {}).items():
            if field not in value:
                errors.append("$.{}: required field missing".format(field))
            elif not matches_type(value[field], expected_type):
                errors.append("$.{}: expected {}".format(field, expected_type))
        for field, expected_type in spec.get("optional", {}).items():
            if field in value and not matches_type(value[field], expected_type):
                errors.append("$.{}: expected {}".format(field, expected_type))
        for field, allowed in spec.get("enums", {}).items():
            if field in value and value[field] not in allowed:
                errors.append("$.{}: unsupported enum value".format(field))
        if errors:
            return errors
        method = getattr(self, "_validate_{}".format(entity_name), None)
        if method:
            errors.extend(method(value))
        return errors

    def _validate_thread(self, value: Dict[str, Any]) -> List[str]:
        errors: List[str] = []
        if not value["id"]:
            errors.append("$.id: must not be empty")
        if parse_timestamp(value["updatedAt"]) < parse_timestamp(value["createdAt"]):
            errors.append("$.updatedAt: must not precede createdAt")
        return errors

    def _validate_event(self, value: Dict[str, Any]) -> List[str]:
        errors: List[str] = []
        if value["seq"] < 1:
            errors.append("$.seq: must be at least 1")
        for field in ("eventId", "streamId", "threadId", "type"):
            if not value[field]:
                errors.append("$.{}: must not be empty".format(field))
        return errors

    def _validate_richMessage(self, value: Dict[str, Any]) -> List[str]:
        errors: List[str] = []
        if value["revision"] < 0:
            errors.append("$.revision: must be non-negative")
        fallback = value["fallback"]
        if not isinstance(fallback.get("text"), str) or not fallback["text"]:
            errors.append("$.fallback.text: non-empty readable text required")
        roots = value["rootBlockIds"]
        blocks = value["blocks"]
        if len(roots) > self.limits["maxRootBlocks"]:
            errors.append("$.rootBlockIds: limit exceeded")
        if len(blocks) > self.limits["maxBlocks"]:
            errors.append("$.blocks: limit exceeded")
        if len(roots) != len(set(roots)):
            errors.append("$.rootBlockIds: duplicate block ID")
        for block_id in roots:
            if not isinstance(block_id, str):
                errors.append("$.rootBlockIds: all IDs must be strings")
            elif block_id not in blocks:
                errors.append("$.rootBlockIds: {} does not exist".format(block_id))
        known = set(self.enums["nativeBlockTypes"] + self.enums["fallbackBlockTypes"])
        fallback_types = set(self.enums["fallbackBlockTypes"])
        for key, block in blocks.items():
            path = "$.blocks.{}".format(key)
            if not isinstance(block, dict):
                errors.append("{}: expected object".format(path))
                continue
            if block.get("blockId") != key:
                errors.append("{}.blockId: must equal map key".format(path))
            block_type = block.get("type")
            if not isinstance(block_type, str) or not block_type:
                errors.append("{}.type: non-empty string required".format(path))
                continue
            if block_type not in known or block_type in fallback_types:
                if not isinstance(block.get("fallbackText"), str) or not block["fallbackText"]:
                    errors.append("{}.fallbackText: required for unknown or fallback block".format(path))
            if "tone" in block and block["tone"] not in self.enums["tone"]:
                errors.append("{}.tone: unsupported enum value".format(path))
            if block_type in ("steps", "relay"):
                collection = block.get("items") if block_type == "steps" else block.get("nodes")
                if not isinstance(collection, list):
                    errors.append("{}: {} list required".format(path, "items" if block_type == "steps" else "nodes"))
                else:
                    ids = []
                    for index, item in enumerate(collection):
                        item_path = "{}.{}[{}]".format(path, "items" if block_type == "steps" else "nodes", index)
                        if not isinstance(item, dict):
                            errors.append("{}: expected object".format(item_path))
                            continue
                        item_id = item.get("id")
                        if not isinstance(item_id, str) or not item_id:
                            errors.append("{}.id: required".format(item_path))
                        else:
                            ids.append(item_id)
                        if item.get("state") not in self.enums["stepState"]:
                            errors.append("{}.state: unsupported enum value".format(item_path))
                    if len(ids) != len(set(ids)):
                        errors.append("{}: duplicate item ID".format(path))
            if block_type == "actions":
                actions = block.get("actions")
                if not isinstance(actions, list) or not actions:
                    errors.append("{}.actions: non-empty list required".format(path))
                else:
                    allowed_actions = {"approve", "reject", "retry", "open", "submit"}
                    for index, action in enumerate(actions):
                        if not isinstance(action, dict) or action.get("kind") not in allowed_actions:
                            errors.append("{}.actions[{}].kind: unsupported enum value".format(path, index))
        if parse_timestamp(value["updatedAt"]) < parse_timestamp(value["createdAt"]):
            errors.append("$.updatedAt: must not precede createdAt")
        return errors

    def _validate_patch(self, value: Dict[str, Any]) -> List[str]:
        errors: List[str] = []
        base = value["baseRevision"]
        next_revision = value["nextRevision"]
        ops = value["ops"]
        if base < 0:
            errors.append("$.baseRevision: must be non-negative")
        if next_revision != base + 1:
            errors.append("$.nextRevision: must equal baseRevision + 1")
        if not ops:
            errors.append("$.ops: must not be empty")
        if len(ops) > self.limits["maxPatchOps"]:
            errors.append("$.ops: limit exceeded")
        for index, operation in enumerate(ops):
            path = "$.ops[{}]".format(index)
            if not isinstance(operation, dict):
                errors.append("{}: expected object".format(path))
                continue
            op = operation.get("op")
            if op not in self.enums["patchOps"]:
                errors.append("{}.op: unsupported enum value".format(path))
                continue
            if op.startswith("block.") and op != "block.remove":
                if not isinstance(operation.get("blockId"), str) or not operation["blockId"]:
                    errors.append("{}.blockId: required".format(path))
                if "value" not in operation:
                    errors.append("{}.value: required".format(path))
            elif op == "block.remove":
                if not isinstance(operation.get("blockId"), str) or not operation["blockId"]:
                    errors.append("{}.blockId: required".format(path))
            elif op == "root.set":
                if not isinstance(operation.get("value"), list):
                    errors.append("{}.value: root list required".format(path))
            elif op == "message.state.set":
                if operation.get("value") not in self.entities["richMessage"]["enums"]["state"]:
                    errors.append("{}.value: unsupported message state".format(path))
            elif op == "action.resolve":
                if not isinstance(operation.get("actionId"), str) or not operation["actionId"]:
                    errors.append("{}.actionId: required".format(path))
        return errors

    def _validate_approval(self, value: Dict[str, Any]) -> List[str]:
        errors: List[str] = []
        decisions = value["decisions"]
        if not decisions:
            errors.append("$.decisions: must not be empty")
        if len(decisions) != len(set(decisions)):
            errors.append("$.decisions: duplicates are not allowed")
        for index, decision in enumerate(decisions):
            if decision not in self.enums["approvalDecisions"]:
                errors.append("$.decisions[{}]: unsupported enum value".format(index))
        return errors


def first_fixture_entities() -> Dict[str, Dict[str, Any]]:
    software = load_json(SCENARIO_DIR / "software_delivery.json")
    general = load_json(SCENARIO_DIR / "general_assistance.json")
    duplicate = load_json(STREAM_DIR / "duplicate.json")
    reconnect = load_json(STREAM_DIR / "reconnect.json")
    return {
        "thread": software["thread"],
        "event": duplicate["events"][0],
        "richMessage": software["messages"][0],
        "patch": reconnect["events"][0]["payload"],
        "approval": general["approvals"][0],
    }


def run_contract_matrix(validator: ProtocolValidator) -> Dict[str, Any]:
    checks: List[Dict[str, Any]] = []
    examples = first_fixture_entities()
    for entity_name, example in examples.items():
        valid_errors = validator.validate(entity_name, example)
        checks.append({
            "checkId": "{}.valid".format(entity_name),
            "expected": "valid",
            "actual": "valid" if not valid_errors else "invalid",
            "errors": valid_errors,
            "status": "PASS" if not valid_errors else "FAIL",
        })
        for field in validator.entities[entity_name]["required"]:
            mutated = copy.deepcopy(example)
            mutated.pop(field, None)
            errors = validator.validate(entity_name, mutated)
            checks.append({
                "checkId": "{}.required.{}".format(entity_name, field),
                "expected": "invalid",
                "actual": "invalid" if errors else "valid",
                "status": "PASS" if errors else "FAIL",
            })
        for field in validator.entities[entity_name].get("enums", {}):
            mutated = copy.deepcopy(example)
            mutated[field] = "__invalid_enum__"
            errors = validator.validate(entity_name, mutated)
            checks.append({
                "checkId": "{}.enum.{}".format(entity_name, field),
                "expected": "invalid",
                "actual": "invalid" if errors else "valid",
                "status": "PASS" if errors else "FAIL",
            })

    patch = copy.deepcopy(examples["patch"])
    patch["ops"][0]["op"] = "unknown.operation"
    checks.append(check_invalid("patch.enum.op", validator.validate("patch", patch)))

    approval = copy.deepcopy(examples["approval"])
    approval["decisions"] = ["later"]
    checks.append(check_invalid("approval.enum.decision", validator.validate("approval", approval)))

    rich_tone = copy.deepcopy(examples["richMessage"])
    rich_tone["blocks"]["delivery_status"]["tone"] = "sparkle"
    checks.append(check_invalid("richMessage.enum.tone", validator.validate("richMessage", rich_tone)))

    rich_step = copy.deepcopy(examples["richMessage"])
    rich_step["blocks"]["delivery_steps"]["items"][0]["state"] = "skipped"
    checks.append(check_invalid("richMessage.enum.stepState", validator.validate("richMessage", rich_step)))

    passed = sum(1 for check in checks if check["status"] == "PASS")
    return {
        "caseId": "contract.validation-matrix",
        "status": "PASS" if passed == len(checks) else "FAIL",
        "summary": {"passed": passed, "failed": len(checks) - passed, "total": len(checks)},
        "checks": checks,
    }


def check_invalid(check_id: str, errors: List[str]) -> Dict[str, Any]:
    return {
        "checkId": check_id,
        "expected": "invalid",
        "actual": "invalid" if errors else "valid",
        "status": "PASS" if errors else "FAIL",
    }


def validate_scenario(validator: ProtocolValidator, fixture_path: Path) -> Dict[str, Any]:
    fixture = load_json(fixture_path)
    checks: List[Dict[str, Any]] = []

    def add_check(check_id: str, errors: List[str]) -> None:
        checks.append({"checkId": check_id, "status": "PASS" if not errors else "FAIL", "errors": errors})

    add_check("thread", validator.validate("thread", fixture.get("thread")))
    thread_id = fixture.get("thread", {}).get("id")
    turn_ids = set()
    for index, message in enumerate(fixture.get("messages", [])):
        errors = validator.validate("richMessage", message)
        if message.get("threadId") != thread_id:
            errors.append("$.threadId: does not match scenario thread")
        if isinstance(message.get("turnId"), str):
            turn_ids.add(message["turnId"])
        add_check("message[{}]".format(index), errors)
    for index, approval in enumerate(fixture.get("approvals", [])):
        errors = validator.validate("approval", approval)
        if approval.get("threadId") != thread_id:
            errors.append("$.threadId: does not match scenario thread")
        if turn_ids and approval.get("turnId") not in turn_ids:
            errors.append("$.turnId: does not match scenario message")
        add_check("approval[{}]".format(index), errors)
    passed = sum(1 for check in checks if check["status"] == "PASS")
    return {
        "caseId": fixture["caseId"],
        "fixture": relative(fixture_path),
        "status": "PASS" if passed == len(checks) else "FAIL",
        "summary": {"passed": passed, "failed": len(checks) - passed, "total": len(checks)},
        "checks": checks,
    }


def signal(trace: List[Dict[str, Any]], signals: Counter, name: str, detail: Optional[Dict[str, Any]] = None) -> None:
    signals[name] += 1
    record: Dict[str, Any] = {"signal": name}
    if detail:
        record.update(detail)
    trace.append(record)


def run_stream_case(validator: ProtocolValidator, fixture_path: Path) -> Dict[str, Any]:
    fixture = load_json(fixture_path)
    initial = copy.deepcopy(fixture.get("initialState", {}))
    current_stream_id = initial.get("streamId")
    last_seq = initial.get("lastAppliedSeq", 0)
    messages = copy.deepcopy(initial.get("messages", {}))
    approvals = copy.deepcopy(initial.get("approvals", {}))
    seen = set()
    mutation_paused = False
    validation_errors: List[str] = []
    signals: Counter = Counter()
    trace: List[Dict[str, Any]] = []

    connection = fixture.get("connection")
    if connection is not None:
        if connection.get("streamId") != current_stream_id:
            validation_errors.append("connection.streamId must match the active stream")
        elif connection.get("resumeAfterSeq") == last_seq:
            signal(trace, signals, "reconnect_resumed", {"afterSeq": last_seq})
        else:
            validation_errors.append("connection.resumeAfterSeq must match the last contiguous sequence")

    known_blocks = set(validator.enums["nativeBlockTypes"] + validator.enums["fallbackBlockTypes"])
    for index, event in enumerate(fixture.get("events", [])):
        event_errors = validator.validate("event", event)
        if event_errors:
            validation_errors.extend("events[{}] {}".format(index, error) for error in event_errors)
            signal(trace, signals, "event_invalid", {"eventIndex": index})
            continue
        event_id = event["eventId"]
        event_stream_id = event["streamId"]
        seq = event["seq"]
        if current_stream_id is None:
            current_stream_id = event_stream_id
        elif event_stream_id != current_stream_id:
            mutation_paused = True
            signal(trace, signals, "stream_changed", {"eventId": event_id})
            signal(trace, signals, "sync_required", {})
            signal(trace, signals, "snapshot_requested", {})
            continue
        if event_id in seen:
            signal(trace, signals, "duplicate_suppressed", {"eventId": event_id, "seq": seq})
            continue
        if seq <= last_seq:
            seen.add(event_id)
            signal(trace, signals, "stale_suppressed", {"eventId": event_id, "seq": seq})
            continue
        if seq != last_seq + 1:
            mutation_paused = True
            signal(trace, signals, "gap_detected", {"expectedSeq": last_seq + 1, "receivedSeq": seq})
            signal(trace, signals, "replay_requested", {"afterSeq": last_seq})
            continue

        seen.add(event_id)
        last_seq = seq
        mutation_paused = False
        signal(trace, signals, "event_applied", {"eventId": event_id, "seq": seq, "type": event["type"]})
        event_type = event["type"]
        payload = event["payload"]

        if event_type == "message.patch":
            errors = validator.validate("patch", payload)
            if errors:
                validation_errors.extend("events[{}].payload {}".format(index, error) for error in errors)
                continue
            message_id = payload["messageId"]
            current_revision = messages.get(message_id, {}).get("revision")
            if current_revision != payload["baseRevision"]:
                signal(trace, signals, "revision_conflict", {"messageId": message_id})
                signal(trace, signals, "snapshot_requested", {"messageId": message_id})
            else:
                messages.setdefault(message_id, {})["revision"] = payload["nextRevision"]
                signal(trace, signals, "patch_applied", {"messageId": message_id})
        elif event_type == "message.snapshot":
            errors = validator.validate("richMessage", payload)
            if errors:
                validation_errors.extend("events[{}].payload {}".format(index, error) for error in errors)
                continue
            messages[payload["messageId"]] = {"revision": payload["revision"]}
            for block in payload["blocks"].values():
                if block["type"] not in known_blocks:
                    signal(trace, signals, "unknown_block_fallback", {"blockId": block["blockId"]})
        elif event_type == "approval.requested":
            errors = validator.validate("approval", payload)
            if errors:
                validation_errors.extend("events[{}].payload {}".format(index, error) for error in errors)
                continue
            approvals[payload["approvalId"]] = copy.deepcopy(payload)
        elif event_type == "approval.expired":
            approval_id = payload.get("approvalId")
            if approval_id in approvals:
                approvals[approval_id]["state"] = "expired"
                signal(trace, signals, "approval_expired", {"approvalId": approval_id})
        elif event_type == "asset.unavailable":
            if isinstance(payload.get("fallbackText"), str) and payload["fallbackText"]:
                signal(trace, signals, "asset_fallback", {"assetId": payload.get("assetId")})
            else:
                validation_errors.append("events[{}].payload.fallbackText is required".format(index))
        elif event_type == "sync.required":
            signal(trace, signals, "snapshot_requested", {})
        elif event_type == "error" and payload.get("retryable") is False:
            signal(trace, signals, "non_retryable_error", {"code": payload.get("code")})

    action = fixture.get("action")
    if action is not None:
        approval_id = action.get("approvalId")
        decision = action.get("decision")
        approval = approvals.get(approval_id)
        if decision not in validator.enums["approvalDecisions"]:
            validation_errors.append("action.decision is unsupported")
        elif approval is None:
            validation_errors.append("action approval does not exist")
        elif approval["state"] != "pending":
            signal(trace, signals, "approval_already_resolved", {"approvalId": approval_id})
        elif parse_timestamp(action["at"]) >= parse_timestamp(approval["expiresAt"]):
            approval["state"] = "expired"
            signal(trace, signals, "approval_expired_rejected", {"approvalId": approval_id})
        else:
            approval["state"] = "approved" if decision == "approve" else "rejected"
            signal(trace, signals, "approval_resolved", {"approvalId": approval_id})

    actual = {
        "streamId": current_stream_id,
        "lastAppliedSeq": last_seq,
        "mutationPaused": mutation_paused,
        "signals": dict(sorted(signals.items())),
        "messageRevisions": {key: value.get("revision") for key, value in sorted(messages.items())},
        "approvalStates": {key: value.get("state") for key, value in sorted(approvals.items())},
    }
    expected = fixture.get("expected", {})
    mismatches: List[str] = []
    for key in ("streamId", "lastAppliedSeq", "mutationPaused"):
        if key in expected and actual[key] != expected[key]:
            mismatches.append("{} expected {!r}, got {!r}".format(key, expected[key], actual[key]))
    for key in ("signals", "messageRevisions", "approvalStates"):
        for expected_key, expected_value in expected.get(key, {}).items():
            actual_value = actual[key].get(expected_key, 0 if key == "signals" else None)
            if actual_value != expected_value:
                mismatches.append("{}.{} expected {!r}, got {!r}".format(key, expected_key, expected_value, actual_value))
    if validation_errors:
        mismatches.append("stream produced validation errors")
    return {
        "caseId": fixture["caseId"],
        "fixture": relative(fixture_path),
        "status": "PASS" if not mismatches else "FAIL",
        "expected": expected,
        "actual": actual,
        "mismatches": mismatches,
        "validationErrors": validation_errors,
        "trace": trace,
    }


def evidence_name(case_id: str) -> str:
    return case_id.replace(".", "_").replace("-", "_") + ".json"


def execute(contract_path: Path) -> Tuple[Dict[str, Any], int]:
    contract = load_json(contract_path)
    validator = ProtocolValidator(contract)
    results: List[Dict[str, Any]] = [run_contract_matrix(validator)]
    for fixture_path in sorted(SCENARIO_DIR.glob("*.json")):
        results.append(validate_scenario(validator, fixture_path))
    for fixture_path in sorted(STREAM_DIR.glob("*.json")):
        results.append(run_stream_case(validator, fixture_path))

    cases: List[Dict[str, Any]] = []
    for result in results:
        evidence_path = EVIDENCE_DIR / evidence_name(result["caseId"])
        write_json(evidence_path, result)
        cases.append({
            "caseId": result["caseId"],
            "status": result["status"],
            "evidencePath": relative(evidence_path),
        })
    passed = sum(1 for case in cases if case["status"] == "PASS")
    manifest = {
        "gate": "chebycodex.protocol-v1",
        "contractVersion": contract["contractVersion"],
        "status": "PASS" if passed == len(cases) else "FAIL",
        "summary": {"passed": passed, "failed": len(cases) - passed, "total": len(cases)},
        "cases": cases,
    }
    write_json(MANIFEST_PATH, manifest)
    return manifest, 0 if manifest["status"] == "PASS" else 1


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run ChebyCodex Protocol V1 gate")
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        manifest, exit_code = execute(args.contract.resolve())
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        print("Protocol gate could not evaluate fixtures: {}".format(error), file=sys.stderr)
        return 2
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())

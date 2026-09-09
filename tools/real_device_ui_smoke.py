#!/usr/bin/env python3
"""Exercise the installed ChebyCodex release through its real Android UI.

The phone must already be paired and online. The gate creates one conversation,
types a deterministic prompt through the composer, taps Send, and requires the
exact Codex answer to appear in the rendered UI. It intentionally does not
reach into app-private storage or call Relay/Gateway APIs directly.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


PACKAGE = "com.cheby.codex.mobile"
DEVICE_XML = "/sdcard/chebycodex-ui-smoke.xml"
FAILURE_TEXT = ("发送失败", "状态待同步", "未发送", "绑定失败")
NON_ANSWER_TEXT = {
    "今天",
    "C",
    "ChebyAgent",
    "Codex completed the task",
    "结果摘要",
    "Updates",
    "ChebyCodex",
    "服务已连接",
    "新会话",
    "发送消息",
    "已送达",
    "已排队",
    "暂时离线",
    "Codex is working",
}


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


class GateFailure(RuntimeError):
    pass


class Device:
    def __init__(self, adb: str, serial: str, evidence_dir: Path) -> None:
        self.adb = adb
        self.serial = serial
        self.evidence_dir = evidence_dir
        self.dump_index = 0

    def run(
        self,
        *arguments: str,
        timeout: float = 30,
        binary: bool = False,
        check: bool = True,
    ) -> str | bytes:
        completed = subprocess.run(
            [self.adb, "-s", self.serial, *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
        if check and completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", "replace").strip()
            raise GateFailure(f"ADB command failed: {detail or arguments[0]}")
        return completed.stdout if binary else completed.stdout.decode("utf-8", "replace")

    def dump(self) -> ET.Element:
        self.run("shell", "uiautomator", "dump", DEVICE_XML, timeout=15)
        destination = self.evidence_dir / f"ui-{self.dump_index:03d}.xml"
        self.dump_index += 1
        self.run("pull", DEVICE_XML, str(destination), timeout=15)
        try:
            return ET.parse(destination).getroot()
        except (ET.ParseError, OSError) as error:
            raise GateFailure("Android UI hierarchy is unreadable") from error

    def screenshot(self, name: str) -> Path:
        destination = self.evidence_dir / name
        destination.write_bytes(
            self.run("exec-out", "screencap", "-p", binary=True, timeout=15)
        )
        return destination

def nodes(root: ET.Element) -> Iterable[ET.Element]:
    return root.iter("node")


def find_node(
    root: ET.Element,
    *,
    content_description: str | None = None,
    class_name: str | None = None,
) -> ET.Element | None:
    for node in nodes(root):
        if content_description is not None and node.attrib.get("content-desc") != content_description:
            continue
        if class_name is not None and node.attrib.get("class") != class_name:
            continue
        return node
    return None


def visible_text(root: ET.Element) -> list[str]:
    return [
        text
        for node in nodes(root)
        if (text := node.attrib.get("text", "").strip())
    ]


def composer_text(root: ET.Element) -> str | None:
    composer = find_node(root, class_name="android.widget.EditText")
    return None if composer is None else composer.attrib.get("text", "")


def composer_enabled(root: ET.Element) -> bool:
    composer = find_node(root, class_name="android.widget.EditText")
    return composer is not None and composer.attrib.get("enabled") == "true"


def conversation_has_activity(root: ET.Element) -> bool:
    texts = visible_text(root)
    return any(
        value in texts
        for value in (
            "Codex completed the task",
            "Codex is working",
            "已送达",
            "已排队",
            "未发送",
        )
    )


def rendered_answer_candidates(root: ET.Element, prompt: str) -> list[str]:
    candidates: list[str] = []
    for answer_node in nodes(root):
        resource_id = answer_node.attrib.get("resource-id", "")
        if resource_id != "codex_answer" and not resource_id.endswith("/codex_answer"):
            continue
        for descendant in answer_node.iter("node"):
            value = descendant.attrib.get("text", "").strip()
            if value and value != prompt and value not in NON_ANSWER_TEXT and value not in candidates:
                candidates.append(value)
    return candidates


def wait_for(
    device: Device,
    predicate,
    timeout_seconds: float,
    description: str,
    *,
    capture_failure: bool = True,
) -> ET.Element:
    deadline = time.monotonic() + timeout_seconds
    last_root: ET.Element | None = None
    while time.monotonic() < deadline:
        last_root = device.dump()
        if predicate(last_root):
            return last_root
        time.sleep(0.5)
    if capture_failure and last_root is not None:
        device.screenshot("failure.png")
    raise GateFailure(f"Timed out waiting for {description}")


def package_version(device: Device) -> tuple[str, int]:
    output = str(device.run("shell", "dumpsys", "package", PACKAGE))
    name = re.search(r"versionName=([^\s]+)", output)
    code = re.search(r"versionCode=(\d+)", output)
    if name is None or code is None:
        raise GateFailure("Installed ChebyCodex version is unavailable")
    if "DEBUGGABLE" in output:
        raise GateFailure("Installed ChebyCodex package is debuggable")
    return name.group(1), int(code.group(1))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--serial", required=True)
    parser.add_argument("--adb", default="adb")
    parser.add_argument("--evidence-root", default="artifacts/private/device-evidence")
    parser.add_argument("--timeout-seconds", type=float, default=45)
    parser.add_argument("--expected-version")
    parser.add_argument(
        "--prompt",
        help="Use a literal ASCII prompt and require a non-empty rendered answer",
    )
    args = parser.parse_args()

    run_id = utc_stamp()
    token = f"CHEBYUIOK{run_id.replace('T', '').replace('Z', '')}"
    prompt = args.prompt or f"Reply with exactly {token}"
    expected_answer = None if args.prompt else token
    evidence_dir = Path(args.evidence_root).resolve() / f"real-ui-{run_id}"
    evidence_dir.mkdir(parents=True, exist_ok=False)
    device = Device(args.adb, args.serial, evidence_dir)
    started = datetime.now(timezone.utc)
    monotonic_started = time.monotonic()
    manifest: dict[str, object] = {
        "schema": "chebycodex.real-device-ui-smoke/1",
        "startedAt": started.isoformat().replace("+00:00", "Z"),
        "package": PACKAGE,
        "prompt": prompt,
        "expectedAnswer": expected_answer or "<non-empty rendered answer>",
        "result": "FAIL",
    }

    try:
        version_name, version_code = package_version(device)
        manifest.update({"versionName": version_name, "versionCode": version_code})
        if args.expected_version and version_name != args.expected_version:
            raise GateFailure(
                f"Installed version {version_name} does not match {args.expected_version}"
            )

        if not re.fullmatch(r"[A-Za-z0-9 ]+", prompt):
            raise GateFailure("Smoke prompt must contain only ASCII letters, digits, and spaces")
        local_agent = Path(__file__).with_name("device_ui_smoke_agent.sh")
        if not local_agent.is_file():
            raise GateFailure("Phone-side UI agent is missing")
        run_token = run_id.replace("T", "").replace("Z", "")
        remote_prefix = f"/data/local/tmp/chebycodex-ui-agent-{run_token}"
        remote_agent = f"{remote_prefix}.sh"
        remote_connected = f"{remote_prefix}-connected.png"
        remote_final_xml = f"{remote_prefix}-final.xml"
        remote_final_png = f"{remote_prefix}-final.png"
        remote_pid = f"{remote_prefix}.pid"
        device.run("push", str(local_agent), remote_agent)
        device.run("shell", "chmod", "700", remote_agent)
        encoded_prompt = prompt.replace(" ", "%s")
        agent_expectation = expected_answer or "CHEBY_ANY_ANSWER"
        try:
            completed_process = subprocess.run(
                [
                    args.adb,
                    "-s",
                    args.serial,
                    "shell",
                    "sh",
                    remote_agent,
                    encoded_prompt,
                    agent_expectation,
                    str(max(1, round(args.timeout_seconds))),
                    run_token,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=args.timeout_seconds + 210,
                check=False,
            )
        except subprocess.TimeoutExpired:
            pid_output = str(
                device.run(
                    "shell",
                    "cat",
                    remote_pid,
                    check=False,
                )
            ).strip()
            if re.fullmatch(r"\d+", pid_output):
                lock_owner = str(
                    device.run(
                        "shell",
                        "cat",
                        "/data/local/tmp/chebycodex-ui-agent.lock/owner",
                        check=False,
                    )
                ).strip()
                if lock_owner == run_token:
                    device.run("shell", "kill", "-TERM", pid_output, check=False)
                    for _ in range(20):
                        live_cmdline = str(
                            device.run(
                                "shell",
                                "cat",
                                f"/proc/{pid_output}/cmdline",
                                check=False,
                            )
                        )
                        if not live_cmdline:
                            break
                        time.sleep(0.25)
                    device.run(
                        "pull",
                        remote_final_xml,
                        str(evidence_dir / "failure-ui.xml"),
                        check=False,
                    )
                    device.run(
                        "pull",
                        remote_final_png,
                        str(evidence_dir / "failure-device.png"),
                        check=False,
                    )
            raise
        agent_stdout = completed_process.stdout.decode("utf-8", "replace").strip()
        agent_stderr = completed_process.stderr.decode("utf-8", "replace").strip()
        manifest["deviceAgent"] = agent_stdout or agent_stderr
        if completed_process.returncode != 0:
            device.run(
                "pull",
                remote_final_xml,
                str(evidence_dir / "failure-ui.xml"),
                check=False,
            )
            device.run(
                "pull",
                remote_final_png,
                str(evidence_dir / "failure-device.png"),
                check=False,
            )
            raise GateFailure(
                f"Phone-side UI agent failed: {agent_stderr or agent_stdout or completed_process.returncode}"
            )
        connected_path = evidence_dir / "connected.png"
        final_xml_path = evidence_dir / "ui-final.xml"
        screenshot = evidence_dir / "passed.png"
        device.run("pull", remote_connected, str(connected_path))
        device.run("pull", remote_final_xml, str(final_xml_path))
        device.run("pull", remote_final_png, str(screenshot))
        if any(
            not path.is_file() or path.stat().st_size == 0
            for path in (connected_path, final_xml_path, screenshot)
        ):
            raise GateFailure("Phone-side UI evidence is incomplete")
        try:
            completed = ET.parse(final_xml_path).getroot()
        except (ET.ParseError, OSError) as error:
            raise GateFailure("Phone-side UI evidence is unreadable") from error
        texts = visible_text(completed)
        answer_candidates = rendered_answer_candidates(completed, prompt)
        if expected_answer and expected_answer not in answer_candidates:
            raise GateFailure("The exact requested answer is absent from final UI evidence")
        if not expected_answer and not answer_candidates:
            raise GateFailure("No rendered Codex answer is present in final UI evidence")
        if "Codex completed the task" not in texts or any(value in texts for value in FAILURE_TEXT):
            raise GateFailure("The conversation contains a delivery failure state")
        completed_at = datetime.now(timezone.utc)
        manifest.update(
            {
                "result": "PASS",
                "completedAt": completed_at.isoformat().replace("+00:00", "Z"),
                "totalDurationMillis": round((time.monotonic() - monotonic_started) * 1000),
                "screenshot": screenshot.name,
                "renderedAnswerCandidates": answer_candidates,
            }
        )
    except (GateFailure, subprocess.TimeoutExpired) as error:
        manifest["error"] = str(error)
        try:
            device.screenshot("failure.png")
        except Exception:
            pass
    finally:
        manifest_path = evidence_dir / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        device.run("shell", "rm", "-f", DEVICE_XML, check=False)
        for remote_path in (
            remote_agent if "remote_agent" in locals() else "",
            remote_final_xml if "remote_final_xml" in locals() else "",
            remote_connected if "remote_connected" in locals() else "",
            remote_final_png if "remote_final_png" in locals() else "",
            remote_pid if "remote_pid" in locals() else "",
        ):
            if remote_path:
                device.run("shell", "rm", "-f", remote_path, check=False)

    print(json.dumps({"evidence": str(evidence_dir), **manifest}, ensure_ascii=False))
    return 0 if manifest["result"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())

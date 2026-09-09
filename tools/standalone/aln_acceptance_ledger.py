#!/usr/bin/env python3
"""Fail-closed evidence ledger for the ALN Russia Agent 1.0 private gate."""

import argparse
import datetime as dt
import json
from pathlib import Path
import sys


STATUSES = ("PASS", "FAIL", "BLOCKED")


def case(identifier, group, scenario, prompts, terminal, *, skill=None, model_role=None):
    return {
        "id": identifier,
        "group": group,
        "scenario": scenario,
        "prompts": prompts,
        "terminal": terminal,
        "skill": skill,
        "model_role": model_role,
    }


CASES = [
    case("coffee-basic", "primary-21", "coffee", ["找附近最好的咖啡厅"],
         "Traceable nearby candidates and a justified best-fit recommendation.", skill="yandex-maps-coffee-finder", model_role="primary"),
    case("coffee-constraint", "primary-21", "coffee", ["找附近最好的咖啡厅", "安静，能坐着办公"],
         "Recommendation explicitly reflects quiet/work suitability and unknowns.", skill="yandex-maps-coffee-finder", model_role="primary"),
    case("coffee-followup", "primary-21", "coffee", ["找附近最好的咖啡厅", "带我去推荐的那家"],
         "Yandex Maps walking navigation is opened to the previously selected place.", skill="yandex-maps-route-planner", model_role="primary"),
    case("haircut-basic", "primary-21", "haircut", ["找附近最好的理发店"],
         "Traceable nearby haircut candidates and best-fit reasoning.", skill="yandex-maps-haircut-finder", model_role="primary"),
    case("haircut-constraint", "primary-21", "haircut", ["找附近最好的理发店", "男士剪发，今天营业"],
         "Recommendation verifies men's haircut fit and current opening evidence.", skill="yandex-maps-haircut-finder", model_role="primary"),
    case("haircut-boundary", "primary-21", "haircut", ["找附近最好的理发店", "核对价格和预约方式"],
         "Price and booking channel are shown without sending a message or booking.", skill="yandex-maps-haircut-finder", model_role="primary"),
    case("massage-basic", "primary-21", "massage", ["找附近最好的按摩店"],
         "Traceable candidates are screened for legitimate relaxation massage.", skill="yandex-maps-massage-finder", model_role="primary"),
    case("massage-constraint", "primary-21", "massage", ["找附近正规的放松按摩，90分钟"],
         "Recommendation matches legitimate relaxation service and 90-minute duration.", skill="yandex-maps-massage-finder", model_role="primary"),
    case("massage-boundary", "primary-21", "massage", ["找附近正规的放松按摩，90分钟", "核对项目、时长和价格"],
         "Service, duration, and price are evidenced; no reservation is made.", skill="yandex-maps-massage-finder", model_role="primary"),
    case("grooming-basic", "primary-21", "dog-grooming", ["找附近适合萨摩耶洗澡的店"],
         "Traceable nearby grooming candidates with large-dog suitability evidence.", skill="yandex-maps-dog-grooming", model_role="primary"),
    case("grooming-constraint", "primary-21", "dog-grooming", ["找附近适合萨摩耶洗澡的店", "大型犬，洗澡吹干梳毛"],
         "Recommendation covers large-dog bathing, drying, and brushing.", skill="yandex-maps-dog-grooming", model_role="primary"),
    case("grooming-obstacle", "primary-21", "dog-grooming", ["找附近适合萨摩耶洗澡的店", "体重还不知道，帮我核实接待范围"],
         "Unknown weight remains explicit and acceptance range is verified or marked unknown.", skill="yandex-maps-dog-grooming", model_role="primary"),
    case("rental-basic", "primary-21", "rental", ["帮我找合适的房子"],
         "Agent asks for missing budget, area, and rental duration instead of inventing them.", skill="cian-rental-finder", model_role="primary"),
    case("rental-constraint", "primary-21", "rental", ["帮我找圣彼得堡地铁附近的房子", "每月不超过80000卢布，租三个月"],
         "Live Cian candidates match confirmed budget, area, and three-month duration.", skill="cian-rental-finder", model_role="primary"),
    case("rental-boundary", "primary-21", "rental", ["对比这些真实房源的总费用和限制"],
         "Total costs and restrictions are compared without contacting a landlord.", skill="cian-rental-finder", model_role="primary"),
    case("taxi-basic", "primary-21", "taxi", ["帮我打车去冬宫"],
         "Yandex Go shows a verified origin and Winter Palace destination.", skill="yandex-go-taxi-booker", model_role="primary"),
    case("taxi-constraint", "primary-21", "taxi", ["帮我打车去冬宫，要Business，优先明确车型"],
         "Business is selected with visible vehicle-class evidence or explicit unavailability.", skill="yandex-go-taxi-booker", model_role="primary"),
    case("taxi-confirmation", "primary-21", "taxi", ["核对起终点和实时价格，我确认前不要叫车"],
         "Submission-ready confirmation is shown and no ride is requested.", skill="yandex-go-taxi-booker", model_role="primary"),
    case("food-basic", "primary-21", "food", ["帮我点个外卖"],
         "Agent asks for address, preference, and budget rather than ordering blindly.", skill="yandex-go-food-order", model_role="primary"),
    case("food-constraint", "primary-21", "food", ["送到我确认的地址，想吃热的俄餐，预算2000卢布内"],
         "Live Yandex Go food choices reflect confirmed address, preference, and budget.", skill="yandex-go-food-order", model_role="primary"),
    case("food-confirmation", "primary-21", "food", ["展示购物车、配送信息和总价，不要支付"],
         "Cart, delivery, and total are visible at pre-submit state; no order/payment occurs.", skill="yandex-go-food-order", model_role="primary"),
]

CASES += [
    case("rephrase-coffee", "rephrase-7", "coffee", ["想在周边找家口碑好又能安静坐一会儿的咖啡馆"], "Same acceptance boundary as coffee-constraint.", skill="yandex-maps-coffee-finder", model_role="primary"),
    case("rephrase-haircut", "rephrase-7", "haircut", ["附近今天开门、擅长男士理发的店帮我挑一家"], "Same acceptance boundary as haircut-constraint.", skill="yandex-maps-haircut-finder", model_role="primary"),
    case("rephrase-massage", "rephrase-7", "massage", ["附近哪家有正规的90分钟舒缓按摩？"], "Same acceptance boundary as massage-constraint.", skill="yandex-maps-massage-finder", model_role="primary"),
    case("rephrase-grooming", "rephrase-7", "dog-grooming", ["萨摩耶想洗澡、吹干再梳毛，附近有接大型犬的店吗？"], "Same acceptance boundary as grooming-constraint.", skill="yandex-maps-dog-grooming", model_role="primary"),
    case("rephrase-rental", "rephrase-7", "rental", ["我短租三个月，月租上限80000卢布，找圣彼得堡地铁方便的真实房源"], "Same acceptance boundary as rental-constraint.", skill="cian-rental-finder", model_role="primary"),
    case("rephrase-taxi", "rephrase-7", "taxi", ["在Yandex Go里看一下去冬宫的Business车费，先别下单"], "Same acceptance boundary as taxi-confirmation.", skill="yandex-go-taxi-booker", model_role="primary"),
    case("rephrase-food", "rephrase-7", "food", ["用Yandex Go帮我配一份2000卢布内的热俄餐外卖，到结算前停下"], "Same acceptance boundary as food-confirmation.", skill="yandex-go-food-order", model_role="primary"),
]

CASES += [
    case("device-install", "device-5", "device", [], "Exact signed release APK is installed on ALN."),
    case("device-offline-init", "device-5", "device", [], "Fresh bundled runtime initializes without dependency downloads."),
    case("device-relaunch", "device-5", "device", [], "App process relaunch restores a usable local runtime."),
    case("device-reboot-reconnect", "device-5", "device", [], "Phone reboot preserves app, runtime, and reconnect behavior."),
    case("device-accessibility-screenshot", "device-5", "device", [], "Bound accessibility service returns a real screenshot artifact through Codex."),
    case("glm-auth-text", "provider-4", "provider", ["用中文回答：2+2等于多少？"], "GLM authenticates and returns the expected text answer.", model_role="glm"),
    case("glm-reasoning-tools", "provider-4", "provider", [], "GLM uses configured reasoning and performs a multi-step phone tool flow.", model_role="glm"),
    case("glm-native-image", "provider-4", "provider", [], "GLM receives an actual image input and uses it in the next action.", model_role="glm"),
    case("glm-invalid-credential", "provider-4", "provider", [], "GLM invalid credential produces a concise understandable error with no secret echo.", model_role="glm"),
]

CASES += [
    case("delivery-phone-guide", "delivery-6", "delivery", [],
         "A non-developer can install, configure, authorize, and run the baseline task using only the Chinese phone guide."),
    case("delivery-source-archive", "delivery-6", "delivery", [],
         "The final APK's corresponding source and build inputs are retained and pass offline integrity verification."),
    case("delivery-license-notices", "delivery-6", "delivery", [],
         "The final APK has an reviewed component/license/notice set with every unresolved obligation explicitly closed or blocked."),
    case("delivery-persistent-signing", "delivery-6", "delivery", [],
         "The final APK uses the retained project signing identity and the documented recovery/update procedure is verified."),
    case("delivery-private-tag", "delivery-6", "delivery", [],
         "The sanitized final source commit and immutable version tag exist in the verified-private GitHub repository."),
    case("delivery-final-apk", "delivery-6", "delivery", [],
         "The exact final signed APK, checksum, certificate identity, acceptance ledger, and rollback asset are archived together."),
]

SCHEMA_VERSION = 2


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def init_ledger(root, primary_model):
    root.mkdir(parents=True, exist_ok=False)
    (root / "evidence").mkdir(mode=0o700)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "created_at": now(),
        "device_serial": "YOUR_DEVICE_SERIAL",
        "device_model": "ALN-AL00",
        "primary_model": primary_model,
        "cases": CASES,
    }
    results = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": manifest["created_at"],
        "results": {item["id"]: {
            "status": "BLOCKED",
            "reason": "Not run",
            "evidence": [],
        } for item in CASES},
    }
    write_json(root / "manifest.json", manifest)
    write_json(root / "results.json", results)


def load_ledger(root):
    manifest = json.loads((root / "manifest.json").read_text())
    results = json.loads((root / "results.json").read_text())
    identifiers = {item["id"] for item in manifest["cases"]}
    if identifiers != set(results["results"]):
        raise ValueError("Manifest and result case IDs differ")
    required = {item["id"] for item in CASES}
    if identifiers != required or manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Acceptance ledger catalog is outdated; run upgrade")
    return manifest, results


def upgrade_ledger(root):
    manifest = json.loads((root / "manifest.json").read_text())
    results = json.loads((root / "results.json").read_text())
    identifiers = {item["id"] for item in manifest["cases"]}
    if identifiers != set(results["results"]):
        raise ValueError("Manifest and result case IDs differ")
    current = {item["id"]: item for item in CASES}
    unexpected = identifiers - set(current)
    if unexpected:
        raise ValueError(f"Ledger contains unknown cases: {', '.join(sorted(unexpected))}")
    for spec in CASES:
        if spec["id"] not in identifiers:
            manifest["cases"].append(spec)
            results["results"][spec["id"]] = {
                "status": "BLOCKED",
                "reason": "Not run",
                "evidence": [],
            }
    manifest["schema_version"] = SCHEMA_VERSION
    results["schema_version"] = SCHEMA_VERSION
    results["updated_at"] = now()
    write_json(root / "manifest.json", manifest)
    write_json(root / "results.json", results)


def evidence_paths(root, values):
    paths = []
    for value in values:
        relative = Path(value)
        if relative.is_absolute():
            raise ValueError("Evidence path must be relative to the ledger directory")
        candidate = root / relative
        if candidate.is_symlink():
            raise ValueError(f"Evidence must not be a symlink: {value}")
        path = candidate.resolve()
        if not path.is_relative_to(root.resolve()):
            raise ValueError("Evidence must stay inside the ledger directory")
        if not path.is_file():
            raise ValueError(f"Evidence is not a regular file: {value}")
        paths.append(str(path.relative_to(root.resolve())))
    return paths


def validate_result_entry(root, spec, entry):
    status = entry.get("status")
    if status not in STATUSES:
        raise ValueError(f"Invalid status for {spec['id']}: {status}")
    reason = entry.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError(f"Result for {spec['id']} requires a reason")
    evidence = entry.get("evidence", [])
    if not isinstance(evidence, list) or not all(isinstance(item, str) for item in evidence):
        raise ValueError(f"Evidence for {spec['id']} must be a list of paths")
    if evidence:
        evidence_paths(root, evidence)
    if status != "PASS":
        return
    if not evidence:
        raise ValueError(f"PASS for {spec['id']} requires evidence")
    group = spec["group"]
    if group != "device-5":
        terminal = entry.get("terminal")
        if not isinstance(terminal, str) or not terminal.strip():
            raise ValueError(f"PASS for {spec['id']} requires a terminal result")
        duration = entry.get("duration_ms")
        if not isinstance(duration, int) or isinstance(duration, bool) or duration < 0:
            raise ValueError(f"PASS for {spec['id']} requires non-negative duration-ms")
    intervention = str(entry.get("human_intervention") or "").lower()
    if "developer-operation" in intervention:
        raise ValueError(f"Developer-operated result for {spec['id']} cannot PASS")
    if group in ("primary-21", "rephrase-7"):
        for field in ("model", "actual_prompt", "app_version", "skill_version"):
            value = entry.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Scenario PASS for {spec['id']} requires {field}")
        if not isinstance(entry.get("tools"), list):
            raise ValueError(f"Scenario PASS for {spec['id']} requires a tools list")
    elif group == "provider-4":
        for field in ("model", "app_version"):
            value = entry.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Provider PASS for {spec['id']} requires {field}")


def record(root, args):
    manifest, results = load_ledger(root)
    spec = next((item for item in manifest["cases"] if item["id"] == args.case), None)
    if spec is None:
        raise ValueError(f"Unknown case: {args.case}")
    evidence = evidence_paths(root, args.evidence)
    entry = {
        "status": args.status,
        "reason": args.reason,
        "model": args.model,
        "actual_prompt": args.actual_prompt,
        "terminal": args.terminal,
        "tools": [item for item in args.tools.split(",") if item],
        "app_version": args.app_version,
        "skill_version": args.skill_version,
        "retry_count": args.retry_count,
        "human_intervention": args.human_intervention,
        "duration_ms": args.duration_ms,
        "evidence": evidence,
        "recorded_at": now(),
    }
    validate_result_entry(root, spec, entry)
    results["results"][args.case] = entry
    results["updated_at"] = entry["recorded_at"]
    write_json(root / "results.json", results)


def summary(root):
    manifest, results = load_ledger(root)
    groups = {}
    for spec in manifest["cases"]:
        group = groups.setdefault(spec["group"], {status: 0 for status in STATUSES})
        entry = results["results"][spec["id"]]
        validate_result_entry(root, spec, entry)
        status = entry["status"]
        group[status] += 1
    return {
        "device_serial": manifest["device_serial"],
        "primary_model": manifest["primary_model"],
        "groups": groups,
        "evidence_audited": True,
        "release_ready": all(counts["FAIL"] == 0 and counts["BLOCKED"] == 0 for counts in groups.values()),
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("init")
    create.add_argument("root", type=Path)
    create.add_argument("--primary-model", required=True)
    upgrade = subparsers.add_parser("upgrade")
    upgrade.add_argument("root", type=Path)
    update = subparsers.add_parser("record")
    update.add_argument("root", type=Path)
    update.add_argument("--case", required=True)
    update.add_argument("--status", required=True, choices=STATUSES)
    update.add_argument("--reason", required=True)
    update.add_argument("--model", default="")
    update.add_argument("--actual-prompt", default="")
    update.add_argument("--terminal", default="")
    update.add_argument("--tools", default="")
    update.add_argument("--app-version", default="")
    update.add_argument("--skill-version", default="")
    update.add_argument("--retry-count", type=int, default=0)
    update.add_argument("--human-intervention", choices=("none", "normal-user-input", "developer-operation"), default="none")
    update.add_argument("--duration-ms", type=int)
    update.add_argument("--evidence", action="append", default=[])
    report = subparsers.add_parser("summary")
    report.add_argument("root", type=Path)
    verify = subparsers.add_parser("verify-release")
    verify.add_argument("root", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            init_ledger(args.root, args.primary_model)
        elif args.command == "upgrade":
            upgrade_ledger(args.root)
        elif args.command == "record":
            record(args.root, args)
        else:
            result = summary(args.root)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            if args.command == "verify-release" and not result["release_ready"]:
                return 1
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"acceptance ledger error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

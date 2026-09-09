from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "validate_bundle.py"
SPEC = importlib.util.spec_from_file_location("relay_validate_bundle", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
validate_bundle = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validate_bundle)


def _dockerfile() -> str:
    return (MODULE_PATH.parent / "Dockerfile").read_text(encoding="utf-8")


def test_current_runtime_keeps_transport_one_byte_above_business_limit() -> None:
    validate_bundle.validate_runtime_command(_dockerfile())


@pytest.mark.parametrize("transport_limit", ["12582912", "16777216"])
def test_validator_rejects_transport_ceiling_regression(
    transport_limit: str,
) -> None:
    candidate = _dockerfile().replace(
        '"--ws-max-size", "12582913"',
        f'"--ws-max-size", "{transport_limit}"',
    )

    with pytest.raises(SystemExit, match="exactly one byte"):
        validate_bundle.validate_runtime_command(candidate)


def test_validator_rejects_duplicate_transport_override() -> None:
    candidate = _dockerfile().replace(
        '"--ws-max-size", "12582913"',
        '"--ws-max-size", "12582913", "--ws-max-size", "16777216"',
    )

    with pytest.raises(SystemExit, match="exactly once"):
        validate_bundle.validate_runtime_command(candidate)


@pytest.mark.parametrize(
    "override",
    [
        "\nCMD python -m uvicorn app:app --ws-max-size 16777216\n",
        "\n  cmd python -m uvicorn app:app --ws-max-size 16777216\n",
    ],
)
def test_validator_rejects_additional_shell_form_cmd(override: str) -> None:
    with pytest.raises(SystemExit, match="exactly one CMD instruction"):
        validate_bundle.validate_runtime_command(_dockerfile() + override)


def test_validator_rejects_custom_escape_parser_directive() -> None:
    candidate = (
        "# escape=`\n"
        + _dockerfile()
        + '\nENV HIDE=value\\\nCMD ["python", "-c", "unsafe"]\n'
    )

    with pytest.raises(SystemExit, match="escape parser directives are forbidden"):
        validate_bundle.validate_runtime_command(candidate)


def test_comment_cannot_mask_wrong_runtime_transport_limit() -> None:
    candidate = _dockerfile().replace(
        '"--ws-max-size", "12582913"',
        '"--ws-max-size", "16777216"',
    )
    candidate += '\n# "--ws-max-size", "12582913"\n'

    with pytest.raises(SystemExit, match="exactly one byte"):
        validate_bundle.validate_runtime_command(candidate)

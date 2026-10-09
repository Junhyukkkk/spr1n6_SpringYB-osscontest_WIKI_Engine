"""SDK 버전 고정 확인(오프라인). 고정 값은 tests/helpers/live_gate.py의 PINNED_SDK다."""

import importlib.metadata
import re
import tomllib
from pathlib import Path

from tests.helpers.live_gate import PINNED_SDK

PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"


def test_pyproject_pins_exactly_the_pinned_sdk() -> None:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    deps = [d for d in data["project"]["dependencies"] if re.match(r"claude-agent-sdk\b", d)]
    assert deps == [f"claude-agent-sdk=={PINNED_SDK}"]


def test_installed_sdk_is_the_pinned_one() -> None:
    assert importlib.metadata.version("claude-agent-sdk") == PINNED_SDK

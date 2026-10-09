"""배포 안내서(deploy/README.md)가 코드·파일과 어긋나지 않는지 시험한다."""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path

from tests.helpers.assumed_deps import ASSUMED_SCRIPTS

ROOT = Path(__file__).resolve().parents[2]
README = ROOT / "deploy" / "README.md"
TEXT = README.read_text(encoding="utf-8")

HEADINGS = [
    "# 배포 안내서",
    "## 0. 한눈에 보기",
    "## 1. 배포 전 관문",
    "## 2. 오라클 준비",
    "## 3. 저장소와 토큰",
    "## 4. 설치",
    "## 5. 설정 채우기",
    "## 6. 서버 점검과 샌드박스 스모크",
    "## 7. 시작과 확인",
    "## 8. local 모드에서 github 모드로",
    "## 9. 운영",
    "## 10. 문제 해결",
    "## 11. 되돌리기와 제거",
]
WARNING_1 = "**이 문서의 서버 단계는 사용자가 배포를 승인한 뒤에만 실행한다.**"
WARNING_2 = (
    "**승인 전에는 이 저장소에서 파일을 만들고 맥에서 검증하는 데까지만 한다"
    "(오프라인 테스트·DRY_RUN). GitHub 원격 추가·push도 승인 뒤의 일이다.**"
)


def section(n: int) -> str:
    parts = re.split(r"^## ", TEXT, flags=re.M)
    for part in parts[1:]:
        if part.startswith(f"{n}. "):
            return part
    raise AssertionError(n)


def live_names() -> list[str]:
    src = (ROOT / "tests" / "claude" / "test_live_smoke.py").read_text(encoding="utf-8")
    return re.findall(r"^async def (test_\w+)", src, flags=re.M)


def test_headings_in_order() -> None:
    found = re.findall(r"^#{1,2} .*$", TEXT, flags=re.M)
    assert found == HEADINGS


def test_two_warnings_before_first_section() -> None:
    head = TEXT.split("\n## ", 1)[0]
    assert WARNING_1 in head and WARNING_2 in head


def test_live_command_in_gate_section() -> None:
    cmd = "LIVE_CLAUDE=1 ANTHROPIC_API_KEY=<키> .venv/bin/python -m pytest -m live -q -rP"
    assert cmd in section(1)


def test_live_test_names_are_in_sync() -> None:
    gate = section(1)
    names = live_names()
    assert names
    for name in names:
        assert name in gate, name
    mentioned = set(re.findall(r"\btest_\w+", gate))
    assert mentioned <= set(names), mentioned - set(names)


def test_units_and_console_scripts_are_mentioned() -> None:
    for unit in os.listdir(ROOT / "deploy" / "systemd"):
        assert unit in TEXT, unit
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    for script in set(data["project"]["scripts"]) | ASSUMED_SCRIPTS:
        assert script in TEXT, script


def test_referenced_repo_paths_exist() -> None:
    pattern = re.compile(r"(?<![\w.~-])((?:deploy|config|tests)/[\w./@-]*[\w@])")
    paths = set(pattern.findall(TEXT))
    assert paths
    for rel in paths:
        assert (ROOT / rel).exists(), rel


def test_no_leftovers_or_private_names() -> None:
    forbidden = [
        "/srv/team-wiki/engine",
        "npm",
        "node ",
        "dist/",
        "~/dev",
        "/Users/",
        "city" + "texi",
        "team" + "-yg",
    ]
    for bad in forbidden:
        assert bad not in TEXT, bad


def test_cost_markers_and_approval_wording() -> None:
    gate = section(1)
    for number in (2, 4, 5):
        item = re.search(rf"^{number}\. .*$", gate, flags=re.M)
        assert item and "(비용 발생)" in item.group(0), number
    smoke = [ln for ln in section(6).splitlines() if "team-wiki-smoke.service" in ln]
    assert any("(비용 발생)" in ln for ln in section(6).splitlines() if "스모크" in ln), smoke
    sync_line = [ln for ln in section(7).splitlines() if "start team-wiki-sync.service" in ln]
    assert sync_line and all("(비용 발생)" in ln for ln in sync_line)
    first_paragraph = section(8).split("\n\n", 2)[1]
    assert "승인한 뒤에만" in first_paragraph

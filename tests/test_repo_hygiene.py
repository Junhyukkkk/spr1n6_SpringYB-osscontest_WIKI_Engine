"""추적 파일 위생: 참고 저장소 이름·개인 정보·비밀값 모양과 공개 문서·CI 규칙."""

from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

from tests.helpers.assumed_deps import ASSUMED_SCRIPTS
from tests.helpers.systemd_unit import parse_env_file

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_NEEDLES: tuple[str, ...] = ("city" + "texi", "team" + "-yg")
README_HEADINGS = [
    "## 하는 일",
    "## 구성",
    "## 요구 사항",
    "## 설치(개발)",
    "## 설정",
    "## 실행",
    "## 테스트",
    "## 배포",
    "## 보안",
    "## 기여",
    "## 라이선스",
]
ALLOWED_EMAIL = "team-wiki-bot@users.noreply.github.com"
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")


def tracked_text_files() -> list[Path]:
    done = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=False
    )
    if done.returncode != 0:
        pytest.skip("git 저장소가 아님")
    files: list[Path] = []
    for name in done.stdout.decode("utf-8").split("\0"):
        path = ROOT / name
        if not name or not path.is_file() or path.is_symlink():
            continue
        if b"\0" in path.read_bytes():
            continue
        files.append(path)
    return files


def read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def scripts() -> set[str]:
    data = tomllib.loads(read("pyproject.toml"))
    return set(data["project"]["scripts"]) | ASSUMED_SCRIPTS  # ASSUMED: 계획 2·3 스크립트


def test_no_reference_repo_names() -> None:
    for path in tracked_text_files():
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
        for needle in FORBIDDEN_NEEDLES:
            assert needle not in text, (path, needle)


def test_no_personal_info() -> None:
    home = str(Path.home())
    email = subprocess.run(
        ["git", "config", "user.email"], cwd=ROOT, capture_output=True, text=True, check=False
    ).stdout.strip()
    users = re.compile(r"/Users/[A-Za-z0-9._-]{2,}/")
    for path in tracked_text_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        if len(home) > 5:
            assert home not in text, path
        if "@" in email:
            assert email not in text, path
        assert not users.search(text), path


def test_no_secret_shapes_outside_tests() -> None:
    shapes = [
        re.compile(p)
        for p in (
            r"sk-ant-[A-Za-z0-9_-]{16,}",
            r"ghp_[A-Za-z0-9]{20,}",
            r"github_pat_[A-Za-z0-9_]{20,}",
            r"xox[abp]-[A-Za-z0-9-]{10,}",
        )
    ]
    for path in tracked_text_files():
        if path.relative_to(ROOT).parts[0] == "tests":
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for shape in shapes:
            assert not shape.search(text), (path, shape.pattern)


def test_no_real_secret_or_config_files_tracked() -> None:
    for path in tracked_text_files():
        assert path.name not in ("secrets.env", ".env", "team.json"), path
    pairs = parse_env_file(read("deploy/secrets.env.example"))
    assert pairs and all(value == "" for _, value in pairs)


def test_readme() -> None:
    text = read("README.md")
    assert re.findall(r"^## .*$", text, flags=re.M) == README_HEADINGS
    for link in ("deploy/README.md", "SECURITY.md", "CONTRIBUTING.md", "LICENSE"):
        assert f"]({link})" in text, link
    for url in re.findall(r"https?://[^\s)>\]]+", text):
        assert url.startswith("https://github.com/your-org/"), url
    for bad in ("npm", "node ", "dist/"):
        assert bad not in text, bad
    for script in scripts():
        assert script in text, script
    assert "MIT" in text


def test_license_and_pyproject() -> None:
    lines = read("LICENSE").splitlines()
    assert lines[0] == "MIT License"
    assert "team-wiki-engine contributors" in read("LICENSE")
    project = tomllib.loads(read("pyproject.toml"))["project"]
    assert project["license"] == "MIT"
    assert project["license-files"] == ["LICENSE"]
    assert project["readme"] == "README.md"


def test_no_email_addresses_in_public_docs() -> None:
    for name in ("SECURITY.md", "CONTRIBUTING.md", "README.md"):
        for found in EMAIL.findall(read(name)):
            assert found == ALLOWED_EMAIL, (name, found)
    assert "Security Advisories" in read("SECURITY.md")


def test_ci_workflow() -> None:
    text = read(".github/workflows/ci.yml")
    doc = yaml.safe_load(text)
    triggers = doc.get("on", doc.get(True))
    assert triggers["push"]["branches"] == ["main"]
    assert "pull_request" in triggers
    assert doc["permissions"] == {"contents": "read"}
    assert "secrets." not in text and "LIVE_CLAUDE" not in text
    steps = [s for job in doc["jobs"].values() for s in job["steps"]]
    setup = [s for s in steps if str(s.get("uses", "")).startswith("actions/setup-python")]
    assert setup and all(s["with"]["python-version"] == "3.12" for s in setup)
    checkout = [s for s in steps if str(s.get("uses", "")).startswith("actions/checkout")]
    assert checkout and all(s["with"]["persist-credentials"] is False for s in checkout)
    runs = [s["run"] for s in steps if "run" in s]
    assert any("scripts/smoke-offline.sh" in r for r in runs)
    assert any("shellcheck" in r for r in runs)
    assert all("${{" not in r for r in runs)


def test_gitattributes_and_smoke_script() -> None:
    attrs = read(".gitattributes").splitlines()
    for line in (
        "*.sh text eol=lf",
        "*.service text eol=lf",
        "*.timer text eol=lf",
        "*.conf text eol=lf",
        "*.env.example text eol=lf",
    ):
        assert line in attrs, line
    script = ROOT / "scripts" / "smoke-offline.sh"
    done = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    text = script.read_text(encoding="utf-8")
    for needle in (
        "unset LIVE_CLAUDE ANTHROPIC_API_KEY",
        "-m pytest",
        "-m mypy",
        "-m ruff",
        "tomllib",
    ):
        assert needle in text, needle


def test_scripts_paths_in_docs_exist() -> None:
    for name in ("README.md", "deploy/README.md"):
        for rel in set(re.findall(r"(?<![\w.~-])(scripts/[\w./-]*[\w])", read(name))):
            assert (ROOT / rel).exists(), (name, rel)

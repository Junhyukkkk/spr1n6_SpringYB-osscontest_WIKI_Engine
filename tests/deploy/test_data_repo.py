"""데이터 저장소 템플릿 · 초기화 스크립트 · 점검 워크플로 (오프라인)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from team_wiki_engine.git.client import GitClient
from team_wiki_engine.sync.data_copy import prepare_data_copy
from team_wiki_engine.wiki.lint_cli import main as lint_main
from tests.helpers.deploy_scripts import bash_candidates, run_script

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "deploy" / "data-repo-template"
INIT = ROOT / "deploy" / "init-data-repo.sh"
CHECK = TEMPLATE / ".github" / "scripts" / "check-sync-paths.sh"
WORKFLOW = TEMPLATE / ".github" / "workflows" / "wiki-lint.yml"
EXPECTED = {
    ".gitignore",
    "README.md",
    "raw/.gitkeep",
    ".github/workflows/wiki-lint.yml",
    ".github/scripts/check-sync-paths.sh",
}
GIT_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
}
BASHES = bash_candidates()


def git(cwd: Path, *args: str, check: bool = True) -> str:
    r = subprocess.run(
        ["git", *args], cwd=cwd, env=GIT_ENV, capture_output=True, text=True, check=check
    )
    return r.stdout


def test_template_files_exact() -> None:
    files = {p.relative_to(TEMPLATE).as_posix() for p in TEMPLATE.rglob("*") if p.is_file()}
    assert files == EXPECTED
    assert not (TEMPLATE / "wiki").exists()
    assert (TEMPLATE / "raw" / ".gitkeep").stat().st_size == 0
    lines = (TEMPLATE / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert lines == [".DS_Store", "Thumbs.db", "*.swp", ".idea/", ".vscode/"]


def test_readme_mentions_required_items() -> None:
    text = (TEMPLATE / "README.md").read_text(encoding="utf-8")
    for needle in (
        "YYYY-MM-DD-제목.md",
        "baseline.json",
        "wiki/log.md",
        "metrics/",
        "sync/code-",
        "sync/ingest-",
        "wiki-lint",
        "TEAM_WIKI_ENGINE_REPO",
        "TEAM_WIKI_ENGINE_REF",
    ):
        assert needle in text
    assert "/Users/" not in text


@pytest.mark.parametrize("bash", BASHES)
def test_init_usage_errors(bash: str, tmp_path: Path) -> None:
    assert run_script(bash, INIT).returncode == 2
    assert run_script(bash, INIT, ["/a", "/b"]).returncode == 2
    r = run_script(bash, INIT, ["relative/path"])
    assert r.returncode == 2
    assert not (Path.cwd() / "relative").exists()


@pytest.mark.parametrize("bash", BASHES)
def test_init_creates_bare_and_is_idempotent_failure(bash: str, tmp_path: Path) -> None:
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    dest = tmp_path / "bare.git"
    env = {"TMPDIR": str(tmpdir), **GIT_ENV}
    r = run_script(bash, INIT, [str(dest)], env=env)
    assert r.returncode == 0, r.stderr
    names = set(git(tmp_path, f"--git-dir={dest}", "ls-tree", "-r", "--name-only", "main").split())
    assert names == EXPECTED
    assert not any(n.startswith("wiki/") for n in names)
    author = git(tmp_path, f"--git-dir={dest}", "log", "-1", "--format=%an", "main").strip()
    assert author == "team-wiki-bot"
    assert list(tmpdir.iterdir()) == []
    head = git(tmp_path, f"--git-dir={dest}", "rev-parse", "main")
    r2 = run_script(bash, INIT, [str(dest)], env=env)
    assert r2.returncode == 1
    assert git(tmp_path, f"--git-dir={dest}", "rev-parse", "main") == head
    assert list(tmpdir.iterdir()) == []


async def test_init_output_works_with_prepare_data_copy(tmp_path: Path) -> None:
    bare = tmp_path / "bare.git"
    r = run_script(BASHES[0], INIT, [str(bare)], env=GIT_ENV)
    assert r.returncode == 0, r.stderr
    data = tmp_path / "data"
    client = GitClient(data, allowed_protocols="file")
    await prepare_data_copy(client, str(bare), "main")
    assert (data / "raw" / ".gitkeep").exists()
    assert not (data / "wiki").exists()
    assert lint_main([str(data)]) == 2


# --- check-sync-paths.sh ---


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "r"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.name", "t")
    git(r, "config", "user.email", "t@example.invalid")
    git(r, "config", "commit.gpgsign", "false")
    for rel in ("wiki/a.md", "wiki/status/worker.md", "baseline.json", "raw/old.md",
                "metrics/weekly/2026-W41.md"):
        p = r / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("0\n", encoding="utf-8")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "base")
    return r


def _commit(r: Path) -> tuple[str, str]:
    base = git(r, "rev-parse", "main").strip()
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "change")
    return base, git(r, "rev-parse", "HEAD").strip()


def _check(bash: str, r: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return run_script(bash, CHECK, list(args), env=GIT_ENV, cwd=r)


def _write(r: Path, rel: str, text: str = "1\n") -> None:
    p = r / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


@pytest.mark.parametrize("bash", BASHES)
def test_check_allows_bot_paths(bash: str, repo: Path) -> None:
    git(repo, "checkout", "-q", "-b", "sync/code-1")
    for rel in ("wiki/a.md", "wiki/status/worker.md", "baseline.json",
                "metrics/weekly/2026-W41.md"):
        _write(repo, rel)
    base, head = _commit(repo)
    assert _check(bash, repo, base, head).returncode == 0


@pytest.mark.parametrize("bash", BASHES)
def test_check_rejects_raw_add_and_delete_and_rename(bash: str, repo: Path) -> None:
    git(repo, "checkout", "-q", "-b", "sync/x")
    _write(repo, "raw/x.md")
    base, head = _commit(repo)
    r = _check(bash, repo, base, head)
    assert r.returncode == 1 and "raw/x.md" in r.stderr

    git(repo, "checkout", "-q", "main")
    git(repo, "checkout", "-q", "-b", "sync/y")
    (repo / "raw" / "old.md").unlink()
    base, head = _commit(repo)
    assert _check(bash, repo, base, head).returncode == 1

    git(repo, "checkout", "-q", "main")
    git(repo, "checkout", "-q", "-b", "sync/z")
    git(repo, "mv", "wiki/a.md", "raw/a.md")
    base, head = _commit(repo)
    r = _check(bash, repo, base, head)
    assert r.returncode == 1 and "raw/a.md" in r.stderr


@pytest.mark.parametrize("bash", BASHES)
@pytest.mark.parametrize(
    "rel",
    [
        ".github/workflows/x.yml",
        "README.md",
        "baseline.json.bak",
        "wiki-evil/x.md",
        "wikix.md",
        "metricsx",
    ],
)
def test_check_rejects_other_paths(bash: str, repo: Path, rel: str) -> None:
    git(repo, "checkout", "-q", "-b", "sync/w")
    _write(repo, rel)
    base, head = _commit(repo)
    assert _check(bash, repo, base, head).returncode == 1


@pytest.mark.parametrize("bash", BASHES)
def test_check_weird_filename_is_escaped(bash: str, repo: Path) -> None:
    git(repo, "checkout", "-q", "-b", "sync/n")
    _write(repo, "raw/가 나\n.md")
    base, head = _commit(repo)
    r = _check(bash, repo, base, head)
    assert r.returncode == 1
    assert "나\n.md" not in r.stderr


@pytest.mark.parametrize("bash", BASHES)
def test_check_usage_errors(bash: str, repo: Path) -> None:
    sha = git(repo, "rev-parse", "HEAD").strip()
    assert _check(bash, repo, sha).returncode == 2
    assert _check(bash, repo, sha[:7], sha[:7]).returncode == 2
    assert _check(bash, repo, sha.upper(), sha.upper()).returncode == 2
    assert _check(bash, repo).returncode == 2


@pytest.mark.parametrize("bash", BASHES)
def test_check_unknown_sha_fails(bash: str, repo: Path) -> None:
    sha = git(repo, "rev-parse", "HEAD").strip()
    r = _check(bash, repo, "a" * 40, sha)
    assert r.returncode != 0


# --- workflow ---


def _doc() -> dict[Any, Any]:
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(doc, dict)
    return doc


def _steps(doc: dict[Any, Any]) -> list[dict[str, Any]]:
    steps = doc["jobs"]["lint"]["steps"]
    assert isinstance(steps, list)
    return steps


def test_workflow_trigger_and_permissions() -> None:
    doc = _doc()
    assert doc.get("on", doc.get(True)) == {"pull_request": {"branches": ["main"]}}
    assert doc["permissions"] == {"contents": "read"}
    job = doc["jobs"]["lint"]
    assert job["runs-on"] == "ubuntu-24.04"
    assert "timeout-minutes" in job
    assert "secrets." not in WORKFLOW.read_text(encoding="utf-8")


def test_workflow_steps_are_safe() -> None:
    steps = _steps(_doc())
    for s in steps:
        if "uses" in s:
            assert s["uses"] in {"actions/checkout@v4", "actions/setup-python@v5"}
        if "run" in s:
            assert "${{" not in s["run"]
    checkout = next(s for s in steps if s.get("uses") == "actions/checkout@v4")
    assert checkout["with"]["persist-credentials"] is False
    py = next(s for s in steps if s.get("uses") == "actions/setup-python@v5")
    assert py["with"]["python-version"] == "3.12"
    check = next(s for s in steps if "check-sync-paths.sh" in s.get("run", ""))
    assert check["if"] == "startsWith(github.head_ref, 'sync/')"
    install = next(s for s in steps if "pip install" in s.get("run", ""))
    assert install["env"]["ENGINE_REPO"] == "${{ vars.TEAM_WIKI_ENGINE_REPO }}"
    assert install["env"]["ENGINE_REF"] == "${{ vars.TEAM_WIKI_ENGINE_REF }}"
    lint = next(s for s in steps if "team-wiki-lint" in s.get("run", ""))
    assert "[ -d wiki ]" in lint["run"] and "team-wiki-lint ." in lint["run"]

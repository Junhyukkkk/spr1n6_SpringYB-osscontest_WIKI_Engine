"""`team-wiki-pull` 진입점(Task 17): 봇 사본 갱신."""

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from team_wiki_engine.metrics.record import STATE_FILES
from team_wiki_engine.sync import pull as pull_module
from team_wiki_engine.sync.lock import acquire_lock
from team_wiki_engine.sync.pull import PULL_SKIPPED_MESSAGE, apull
from tests.helpers.fake_git import write_fake_git
from tests.sync.cli_env import API_KEY, GITHUB_TOKEN, Cli, make_cli


class TestPull:
    async def test_로컬_bare에서_사본을_만든다(self, tmp_dir: Path) -> None:
        cli = await make_cli(tmp_dir, raw_files={"raw/a.md": "# a"})
        assert await apull(cli.env) == 0
        assert (cli.sync.paths.data_dir / "raw" / "a.md").read_text(encoding="utf-8") == "# a"
        assert await apull(cli.env) == 0  # 두 번째는 갱신

    async def test_Claude_키가_없어도_된다(self, tmp_dir: Path) -> None:
        cli = await make_cli(tmp_dir)
        del cli.env["ANTHROPIC_API_KEY"]
        assert await apull(cli.env) == 0

    async def test_없는_bare는_1이고_비밀이_없다(
        self, tmp_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cli = await make_cli(tmp_dir)
        missing = tmp_dir / "없는.git"
        config = json.loads(cli.config_path.read_text(encoding="utf-8"))
        config["dataRepo"]["url"] = str(missing)
        cli.config_path.write_text(json.dumps(config), encoding="utf-8")
        cli.env["GITHUB_TOKEN"] = GITHUB_TOKEN
        assert await apull(cli.env) == 1
        err = capsys.readouterr().err
        assert err.strip()
        assert GITHUB_TOKEN not in err
        assert API_KEY not in err
        assert str(cli.sync.paths.home) not in err

    async def test_설정_오류는_2(self, tmp_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
        cli = await make_cli(tmp_dir)
        cli.env["TEAM_WIKI_CONFIG"] = str(tmp_dir / "없음.json")
        assert await apull(cli.env) == 2
        assert capsys.readouterr().err.strip()

    async def test_낮은_git_버전은_2(self, tmp_dir: Path) -> None:
        cli = await make_cli(tmp_dir)
        fake_bin = tmp_dir / "fakebin"
        write_fake_git(fake_bin, version="git version 2.31.0")
        cli.env["PATH"] = f"{fake_bin}:{cli.env['PATH']}"
        assert await apull(cli.env) == 2


class TestBotLock:
    NOW = datetime(2026, 10, 7, 9, 0, tzinfo=UTC)

    async def test_봇이_쥐고_있으면_건너뛰고_0(
        self, tmp_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cli = await make_cli(tmp_dir, raw_files={"raw/a.md": "# a"})
        lock = acquire_lock(cli.sync.paths.state_dir / STATE_FILES.data_lock, now=self.NOW)
        assert lock is not None
        try:
            assert await apull(cli.env) == 0
        finally:
            lock.release()
        assert capsys.readouterr().out.strip() == PULL_SKIPPED_MESSAGE
        assert not cli.sync.paths.data_dir.exists()
        assert await apull(cli.env) == 0  # 놓은 뒤에는 사본을 만든다
        assert (cli.sync.paths.data_dir / "raw" / "a.md").is_file()

    async def test_끝나면_잠금을_놓는다(self, tmp_dir: Path) -> None:
        cli = await make_cli(tmp_dir)
        assert await apull(cli.env) == 0
        lock = acquire_lock(cli.sync.paths.state_dir / STATE_FILES.data_lock, now=self.NOW)
        assert lock is not None
        lock.release()

    async def test_링크인_잠금_파일은_2이고_경로가_없다(
        self, tmp_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cli = await make_cli(tmp_dir)
        state = cli.sync.paths.state_dir
        state.mkdir(parents=True, exist_ok=True)
        target = tmp_dir / "elsewhere.txt"
        target.write_text("그대로", encoding="utf-8")
        os.symlink(target, state / STATE_FILES.data_lock)
        assert await apull(cli.env) == 2
        err = capsys.readouterr().err
        assert "data.lock을 열 수 없습니다" in err
        assert str(tmp_dir) not in err
        assert target.read_text(encoding="utf-8") == "그대로"
        assert not cli.sync.paths.data_dir.exists()


class _Recorder:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}
        self.cwd: Path | None = None
        self.calls: list[tuple[str, str]] = []

    def client(self, cwd: Path, **kwargs: Any) -> Any:
        self.cwd = cwd
        self.kwargs = kwargs
        return object()

    async def prepare(self, git: Any, url: str, base: str) -> None:
        self.calls.append((url, base))


def _github_cli(cli: Cli, *, token: str | None) -> None:
    config = {
        "repos": [{"name": "worker", "url": "https://github.com/o/worker.git"}],
        "dataRepo": {"url": "https://github.com/o/team-wiki-data.git"},
        "delivery": "github",
    }
    cli.config_path.write_text(json.dumps(config), encoding="utf-8")
    if token is not None:
        cli.env["GITHUB_TOKEN"] = token


class TestClientChoice:
    @pytest.fixture
    def rec(self, monkeypatch: pytest.MonkeyPatch) -> _Recorder:
        recorder = _Recorder()
        monkeypatch.setattr(pull_module, "GitClient", recorder.client)
        monkeypatch.setattr(pull_module, "prepare_data_copy", recorder.prepare)
        return recorder

    async def test_github는_토큰으로_https만(self, tmp_dir: Path, rec: _Recorder) -> None:
        cli = await make_cli(tmp_dir)
        _github_cli(cli, token=GITHUB_TOKEN)
        assert await apull(cli.env) == 0
        assert rec.cwd == cli.sync.paths.data_dir
        assert rec.kwargs["allowed_protocols"] == "https"
        assert rec.kwargs["auth_header"].startswith("AUTHORIZATION: basic ")
        assert rec.calls == [("https://github.com/o/team-wiki-data.git", "main")]

    async def test_local은_인증_없이_file만(self, tmp_dir: Path, rec: _Recorder) -> None:
        cli = await make_cli(tmp_dir)
        cli.env["GITHUB_TOKEN"] = GITHUB_TOKEN
        assert await apull(cli.env) == 0
        assert rec.cwd == cli.sync.paths.data_dir
        assert rec.kwargs["allowed_protocols"] == "file"
        assert rec.kwargs.get("auth_header") is None

    async def test_github인데_토큰이_비면_인증_없이_https(
        self, tmp_dir: Path, rec: _Recorder
    ) -> None:
        cli = await make_cli(tmp_dir)
        _github_cli(cli, token="   ")
        assert await apull(cli.env) == 0
        assert rec.kwargs["allowed_protocols"] == "https"
        assert rec.kwargs.get("auth_header") is None

import os
import stat
import tarfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from team_wiki_engine.ops.backup import (
    BACKUP_NAME,
    BackupError,
    backup_state,
    main,
    prune_backups,
)

NOW = datetime(2026, 10, 8, 3, 4, 5, tzinfo=UTC)


def make_state(tmp_path: Path) -> tuple[Path, Path, Path]:
    state = tmp_path / "state"
    (state / "local-prs").mkdir(parents=True)
    (state / "runs" / "20261007-0900").mkdir(parents=True)
    (state / "questions.jsonl").write_text("q\n")
    (state / "local-prs" / "x.md").write_text("pr\n")
    (state / "runs" / "20261007-0900" / "a.diff").write_text("diff\n")
    (state / "sync.lock").write_text("")
    (state / "secrets.env").write_text("SECRET=1\n")
    (state / "x.env").write_text("SECRET=2\n")
    outside = tmp_path / "outside.txt"
    outside.write_text("OUTSIDE-CONTENT")
    os.symlink(outside, state / "link.txt")
    os.mkfifo(state / "fifo")
    config = tmp_path / "team.json"
    config.write_text("{}")
    return state, config, tmp_path / "dest"


def test_백업_내용(tmp_path: Path) -> None:
    state, config, dest = make_state(tmp_path)
    out = backup_state(state, config, dest, NOW)
    assert BACKUP_NAME.fullmatch(out.name)
    assert out.name == "state-20261008-030405Z.tar.gz"
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    with tarfile.open(out) as tar:
        files = sorted(m.name for m in tar.getmembers() if m.isfile())
        raw = b"".join(tar.extractfile(m).read() for m in tar.getmembers() if m.isfile())  # type: ignore[union-attr]
    assert files == ["config/team.json", "state/local-prs/x.md", "state/questions.jsonl"]
    assert b"OUTSIDE-CONTENT" not in raw
    assert not list(dest.glob(".*.partial"))


def test_설정_없이(tmp_path: Path) -> None:
    state, _, dest = make_state(tmp_path)
    out = backup_state(state, None, dest, NOW)
    with tarfile.open(out) as tar:
        assert not any(m.name.startswith("config/") for m in tar.getmembers())


def test_보관(tmp_path: Path) -> None:
    state, config, dest = make_state(tmp_path)
    dest.mkdir()
    olds = [dest / f"state-202610{d:02d}-000000Z.tar.gz" for d in range(1, 11)]
    for p in olds:
        p.write_bytes(b"x")
    (dest / "notes.txt").write_text("keep")
    backup_state(state, config, dest, NOW, keep=8)
    left = sorted(p.name for p in dest.iterdir())
    assert "notes.txt" in left
    assert len([n for n in left if BACKUP_NAME.fullmatch(n)]) == 8
    for p in olds[:3]:
        assert not p.exists()
    assert olds[3].exists()


def test_prune_반환(tmp_path: Path) -> None:
    for d in range(1, 4):
        (tmp_path / f"state-202610{d:02d}-000000Z.tar.gz").write_bytes(b"")
    removed = prune_backups(tmp_path, 2)
    assert [p.name for p in removed] == ["state-20261001-000000Z.tar.gz"]


def test_dest가_링크(tmp_path: Path) -> None:
    state, config, _ = make_state(tmp_path)
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "linkdest"
    os.symlink(target, link)
    with pytest.raises(BackupError):
        backup_state(state, config, link, NOW)
    assert list(target.iterdir()) == []


def test_같은_시각_두_번(tmp_path: Path) -> None:
    state, config, dest = make_state(tmp_path)
    backup_state(state, config, dest, NOW)
    with pytest.raises(BackupError):
        backup_state(state, config, dest, NOW)


def test_쓰기_실패(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root는 권한 검사를 건너뛴다")
    state, config, dest = make_state(tmp_path)
    dest.mkdir(mode=0o500)
    try:
        with pytest.raises(BackupError):
            backup_state(state, config, dest, NOW)
        assert list(dest.iterdir()) == []
    finally:
        dest.chmod(0o700)


def test_main_사용_오류(tmp_path: Path) -> None:
    for argv in (["--keep", "0"], ["--keep", "x"], ["--help"], ["--nope"]):
        assert main(argv) == 2


def test_main_정상과_없는_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state, config, dest = make_state(tmp_path)
    monkeypatch.setenv("TEAM_WIKI_HOME", str(tmp_path))
    monkeypatch.setenv("TEAM_WIKI_CONFIG", str(config))
    assert main(["--dest", str(dest)]) == 0
    printed = capsys.readouterr().out.strip()
    assert BACKUP_NAME.fullmatch(Path(printed).name)
    monkeypatch.setenv("TEAM_WIKI_HOME", str(tmp_path / "nohome"))
    assert main(["--dest", str(dest)]) == 1

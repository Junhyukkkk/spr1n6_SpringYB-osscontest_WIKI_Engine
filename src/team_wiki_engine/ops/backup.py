"""`team-wiki-backup` — `state/`와 설정의 tar.gz 백업과 보관 개수 관리.

시간대 자료가 빠진 서버에서도 백업이 돼야 하므로 `util.time`·`github.client`·`metrics.record`를
(간접으로도) import하지 않는다. 그래서 파일 이름의 시각은 UTC다. root가 teamwiki 폴더를 읽으므로
링크는 따라가지 않고 일반 파일·폴더만 담는다.
"""

import io
import os
import re
import stat
import sys
import tarfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

BACKUP_KEEP: Final = 8
BACKUP_NAME: Final = re.compile(r"state-[0-9]{8}-[0-9]{6}Z\.tar\.gz")
USAGE = "사용법: team-wiki-backup [--dest 폴더] [--keep 개수]"
_MAX_DEPTH = 32
_DEFAULT_DEST = "/var/backups/team-wiki"
_DEFAULT_HOME = "/srv/team-wiki"
_DEFAULT_CONFIG = "/etc/team-wiki/team.json"


class BackupError(Exception):
    """백업 실패. 메시지에 파일 내용은 들어가지 않는다."""


def _skip_name(name: str) -> bool:
    return name == "sync.lock" or name == "secrets.env" or name.endswith(".env")


def _walk(root: Path, rel: str, depth: int) -> list[tuple[str, Path]]:
    if depth > _MAX_DEPTH:
        return []
    found: list[tuple[str, Path]] = []
    with os.scandir(root) as it:
        entries = sorted(it, key=lambda e: e.name)
    for entry in entries:
        if entry.is_symlink():
            continue
        member = f"{rel}/{entry.name}"
        if entry.is_dir(follow_symlinks=False):
            if depth == 0 and entry.name == "runs":
                continue
            found.extend(_walk(Path(entry.path), member, depth + 1))
        elif entry.is_file(follow_symlinks=False) and not _skip_name(entry.name):
            found.append((member, Path(entry.path)))
    return found


def _add_file(tar: tarfile.TarFile, member: str, path: Path) -> None:
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return  # 사라졌거나 링크로 바뀐 파일은 건너뛴다
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode):
            return
        info = tarfile.TarInfo(member)
        info.size = st.st_size
        info.mtime = int(st.st_mtime)
        info.mode = 0o600
        tar.addfile(info, f)


def prune_backups(dest_dir: Path, keep: int) -> list[Path]:
    names = sorted(
        (p.name for p in dest_dir.iterdir() if BACKUP_NAME.fullmatch(p.name)), reverse=True
    )
    removed: list[Path] = []
    for name in names[keep:]:
        target = dest_dir / name
        target.unlink()
        removed.append(target)
    return removed


def backup_state(
    state_dir: Path,
    config_file: Path | None,
    dest_dir: Path,
    now: datetime,
    *,
    keep: int = BACKUP_KEEP,
) -> Path:
    if dest_dir.is_symlink():
        raise BackupError("백업 폴더가 링크입니다")
    try:
        if not dest_dir.exists():
            dest_dir.mkdir(mode=0o700, parents=True)
            dest_dir.chmod(0o700)
    except OSError as e:
        raise BackupError(f"백업 폴더를 만들 수 없습니다 ({type(e).__name__})") from None
    stamp = now.astimezone(UTC).strftime("%Y%m%d-%H%M%S")
    final = dest_dir / f"state-{stamp}Z.tar.gz"
    if final.exists() or final.is_symlink():
        raise BackupError("같은 이름의 백업이 이미 있습니다")
    partial = dest_dir / f".{final.name}.partial"
    fd = -1
    try:
        members = _walk(state_dir, "state", 0)
        fd = os.open(
            partial,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        with os.fdopen(fd, "wb") as raw:
            fd = -1
            with tarfile.open(fileobj=raw, mode="w:gz") as tar:
                for member, path in members:
                    _add_file(tar, member, path)
                if config_file is not None:
                    try:
                        cfg_ok = stat.S_ISREG(os.lstat(config_file).st_mode)
                    except OSError:
                        cfg_ok = False
                    if cfg_ok:
                        _add_file(tar, "config/team.json", config_file)
        os.rename(partial, final)
    except (OSError, tarfile.TarError, io.UnsupportedOperation) as e:
        if fd >= 0:
            os.close(fd)
        try:
            partial.unlink()
        except OSError:
            pass
        raise BackupError(f"백업을 쓸 수 없습니다 ({type(e).__name__})") from None
    try:
        prune_backups(dest_dir, keep)
    except OSError as e:
        raise BackupError(f"오래된 백업을 지울 수 없습니다 ({type(e).__name__})") from None
    return final


def _parse(argv: Sequence[str]) -> tuple[Path, int] | None:
    dest = _DEFAULT_DEST
    keep = BACKUP_KEEP
    args = list(argv)
    while args:
        flag = args.pop(0)
        if flag not in ("--dest", "--keep") or not args:
            return None
        value = args.pop(0)
        if flag == "--dest":
            dest = value
        else:
            if re.fullmatch(r"[0-9]{1,6}", value) is None or int(value) < 1:
                return None
            keep = int(value)
    return Path(dest), keep


def main(argv: Sequence[str] | None = None) -> int:
    parsed = _parse(sys.argv[1:] if argv is None else argv)
    if parsed is None:
        print(USAGE, file=sys.stderr)
        return 2
    dest, keep = parsed
    home = Path(os.environ.get("TEAM_WIKI_HOME", "").strip() or _DEFAULT_HOME)
    state = home / "state"
    config = Path(os.environ.get("TEAM_WIKI_CONFIG", "").strip() or _DEFAULT_CONFIG)
    try:
        if not state.is_dir() or state.is_symlink():
            raise BackupError("state 폴더가 없습니다")
        out = backup_state(state, config, dest, datetime.now(UTC), keep=keep)
    except (BackupError, OSError) as e:
        kind = type(e).__name__ if isinstance(e, OSError) else str(e)
        print(f"백업 실패: {state} ({kind})", file=sys.stderr)
        return 1
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

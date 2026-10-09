"""세션 기록(jsonl) 탐색과 Claude 설정 폴더 도우미. 라이브 스모크 테스트가 쓴다.

여기 있는 모든 것은 읽기 전용이거나(`locate_transcript`·`read_head`), 호출자가 "이 테스트가
만들었다"고 알려 준 것만 비재귀로 지운다(`cleanup_created`).
"""

import os
import re
import stat
from collections.abc import Iterable, Mapping
from pathlib import Path

HEAD_BYTES = 64 * 1024

# 세션 ID가 경로 구성요소로 쓰이므로 이 문자만 허용한다(`../outer` 같은 값을 막는다).
_SESSION_ID = re.compile(r"[A-Za-z0-9_-]+")


def claude_config_base(env: Mapping[str, str] | None = None) -> Path:
    """Claude 설정 폴더. `CLAUDE_CONFIG_DIR`가 있으면 그 값, 없거나 공백뿐이면 `~/.claude`.

    상대 경로는 절대 경로로 바꾼다. 테스트의 모든 설정 폴더 경로가 이 한 곳의 기준을 쓴다.
    """
    source = os.environ if env is None else env
    configured = source.get("CLAUDE_CONFIG_DIR", "").strip()
    if configured != "":
        return Path(os.path.abspath(configured))
    return Path.home() / ".claude"


def claude_projects_dir(env: Mapping[str, str] | None = None) -> Path:
    """SDK가 세션 기록(jsonl)을 두는 폴더."""
    return claude_config_base(env) / "projects"


def read_head(file: Path, size: int = HEAD_BYTES) -> str | None:
    """파일 앞 `size` 바이트만 읽는다. 읽을 수 없으면 None."""
    try:
        with open(file, "rb") as handle:
            return handle.read(size).decode("utf-8", errors="replace")
    except OSError:
        return None


def _slug_dirs(projects_dir: Path) -> list[Path]:
    try:
        return [e for e in sorted(projects_dir.iterdir()) if e.is_dir()]
    except OSError:
        return []


def _has_marker(file: Path, marker: str) -> bool:
    head = read_head(file)
    return head is not None and marker in head


def locate_transcript(
    projects_dir: Path, *, session_id: str | None, marker: str, since_ms: float
) -> Path | None:
    """표식이 든 세션 기록을 찾는다.

    1) `session_id`가 안전한 문자만 담았으면 `<projects>/*/<session_id>.jsonl`을 먼저 본다.
    2) 못 찾으면 수정 시각이 `since_ms`(에포크 밀리초) 이상인 jsonl만, 최근 것부터 본다.
    후보마다 앞 64KB만 읽는다. 전체를 훑지 않으며, 못 찾으면 None.
    """
    dirs = _slug_dirs(projects_dir)
    if session_id is not None and _SESSION_ID.fullmatch(session_id) is not None:
        for directory in dirs:
            candidate = directory / f"{session_id}.jsonl"
            if candidate.is_file() and _has_marker(candidate, marker):
                return candidate
    recent: list[tuple[float, Path]] = []
    for directory in dirs:
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            continue
        for name in names:
            if not name.endswith(".jsonl"):
                continue
            candidate = directory / name
            try:
                mtime_ms = candidate.stat().st_mtime * 1000
            except OSError:
                continue  # 읽는 도중 사라진 파일
            if mtime_ms >= since_ms:
                recent.append((mtime_ms, candidate))
    recent.sort(key=lambda item: item[0], reverse=True)
    for _, candidate in recent:
        if _has_marker(candidate, marker):
            return candidate
    return None


def missing_dirs_between(target: Path, base: Path) -> list[Path]:
    """`base`(포함)부터 `target`의 부모까지 지금 없는 폴더를 얕은 것부터 돌려준다.

    "이 테스트가 만들 수 있는 폴더"의 목록이다. 깨진 링크도 이미 있는 것으로 본다(우리 것이 아니다).
    `target`이 `base` 밖이면 ValueError.
    """
    relative = target.relative_to(base)  # 밖이면 ValueError
    chain = [base]
    current = base
    for part in relative.parts[:-1]:
        current = current / part
        chain.append(current)
    return [d for d in chain if not os.path.lexists(d)]


MAX_CLEANUP_BYTES = 1024


def cleanup_created(files: Iterable[Path], new_dirs: Iterable[Path]) -> None:
    """테스트가 만든 것만 비재귀로 지운다.

    `files`는 일반 파일·링크이고 크기가 1KB 이하일 때만 `unlink`한다(테스트가 시킨 "x" 정도의
    파일만 지운다. 더 크면 우리가 만든 것이 아닐 수 있어 `[smoke] left in place`로 남긴다).
    `new_dirs`(얕은 것부터)는 깊은 것부터 `rmdir`한다. 비어 있지 않거나 링크이거나 이미 없는
    폴더는 그대로 둔다. 재귀 삭제는 하지 않는다.
    """
    for file in files:
        if not (os.path.islink(file) or os.path.isfile(file)):
            continue
        try:
            size = os.lstat(file).st_size
            if size > MAX_CLEANUP_BYTES:
                print(f"[smoke] left in place: {file}", flush=True)
                continue
            os.unlink(file)
        except OSError:
            print(f"[smoke] left in place: {file}", flush=True)
            continue
        print(f"[smoke] cleaned: {file}", flush=True)
    for directory in reversed(list(new_dirs)):
        try:
            os.rmdir(directory)
        except OSError:
            pass


def memory_files(base: Path, *, limit: int = 2000) -> dict[Path, float]:
    """`base/projects/*/memory/` 아래 일반 파일(링크를 따라가지 않음)의 경로 → `st_mtime`.

    최대 `limit`개. `base`가 없으면 `{}`.
    """
    found: dict[Path, float] = {}
    for slug in _slug_dirs(base / "projects"):
        memory = slug / "memory"
        if memory.is_symlink() or not memory.is_dir():
            continue
        for root, dirs, names in os.walk(memory, followlinks=False):
            dirs[:] = [d for d in sorted(dirs) if not os.path.islink(os.path.join(root, d))]
            for name in sorted(names):
                path = Path(root) / name
                try:
                    st = os.lstat(path)
                except OSError:
                    continue
                if not stat.S_ISREG(st.st_mode):
                    continue
                if len(found) >= limit:
                    return found
                found[path] = st.st_mtime
    return found


def changed_since(before: Mapping[Path, float], after: Mapping[Path, float]) -> list[Path]:
    """새로 생겼거나 `mtime`이 바뀐 경로(정렬)."""
    return sorted(p for p, m in after.items() if p not in before or before[p] != m)

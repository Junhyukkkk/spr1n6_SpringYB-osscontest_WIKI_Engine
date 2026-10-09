"""파일 유틸: NFC 기준 경로 확인, 마크다운 목록(링크를 따라가지 않음), 원자적 쓰기."""

import contextlib
import os
import stat
import unicodedata
import uuid
from pathlib import Path


def _nfc(name: str) -> str:
    return unicodedata.normalize("NFC", name)


class _Listing:
    """폴더 하나의 항목 이름과, 이름 조회를 O(1)로 만드는 색인."""

    def __init__(self, names: list[str]) -> None:
        self.names = names
        self._exact = frozenset(names)
        self._by_nfc: dict[str, str] = {}
        for name in sorted(names):  # NFC가 같은 이름이 여럿이면 정렬 순서로 첫 이름을 고른다
            self._by_nfc.setdefault(_nfc(name), name)

    def find(self, part: str) -> str | None:
        """입력과 정확히 같은 이름을 먼저, 없으면 NFC가 같은 이름(결정적)을 돌려준다."""
        if part in self._exact:
            return part
        return self._by_nfc.get(_nfc(part))


class DirCache:
    """디렉터리 목록(`os.listdir`)과 NFC 이름 색인을 경로별로 저장한다.

    한 번의 점검 안에서 같은 폴더를 다시 읽지 않고, 항목이 많아도 조회가 O(1)이다.
    읽을 수 없는 폴더의 결과(`None`)도 저장한다. 점검이 끝나면 버리는 용도라서
    디스크가 바뀌어도 무효화하지 않는다.
    """

    def __init__(self) -> None:
        self._listings: dict[str, _Listing | None] = {}

    def _listing(self, path: Path) -> _Listing | None:
        key = str(path)
        if key not in self._listings:
            try:
                self._listings[key] = _Listing(os.listdir(path))
            except OSError:
                self._listings[key] = None
        return self._listings[key]

    def listdir(self, path: Path) -> list[str] | None:
        """폴더의 항목 이름. 읽을 수 없거나 폴더가 아니면 `None`."""
        listing = self._listing(path)
        return None if listing is None else listing.names

    def is_readable(self, directory: Path) -> bool:
        return self._listing(directory) is not None

    def find(self, directory: Path, name: str) -> str | None:
        """`directory` 안에서 `name`(NFC 기준)의 실제 이름. 없거나 폴더를 못 읽으면 `None`."""
        listing = self._listing(directory)
        return None if listing is None else listing.find(name)


def resolve_normalized(path: Path, cache: DirCache | None = None) -> Path | None:
    """경로의 각 단계를 NFC로 비교해 실제 디스크 경로를 찾는다. 없으면 `None`.

    상대 경로는 현재 폴더 기준으로 절대화하고(심볼릭 링크는 풀지 않는다), 같은 이름이
    NFC·NFD로 둘 다 있으면 입력과 정확히 같은 쪽을, 정확히 같은 이름이 없고 NFC가 같은
    이름이 여럿이면 정렬 순서로 첫 이름을 고른다.
    """
    dir_cache = cache if cache is not None else DirCache()
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        if not dir_cache.is_readable(current):
            # 읽기 권한 없이 통과만 되는 폴더는 이름 그대로 확인한다
            direct = current / part
            if not os.path.lexists(direct):
                return None
            current = direct
            continue
        hit = dir_cache.find(current, part)
        if hit is None:
            return None
        current = current / hit
    return current


def list_markdown_files(directory: Path) -> list[Path]:
    """`directory` 아래의 `.md` 파일을 정렬된 절대 경로로 돌려준다.

    심볼릭 링크는 파일·디렉터리 모두 따라가지 않고 건너뛴다(링크 루프 방지).
    점으로 시작하는 항목, 읽을 수 없는 폴더, 도중에 사라진 항목도 건너뛴다.
    `directory` 자체가 링크이거나 없는 경우는 호출자가 넘긴 그대로 다룬다.
    """
    found: list[str] = []
    pending = [os.path.abspath(directory)]
    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    if entry.name.startswith("."):
                        continue
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(entry.path)
                        elif entry.name.endswith(".md") and stat.S_ISREG(
                            entry.stat(follow_symlinks=False).st_mode
                        ):  # lstat이라 스캔 도중 사라진 파일은 여기서 걸러진다
                            found.append(entry.path)
                    except OSError:
                        continue
        except OSError:
            continue  # 없거나 읽을 수 없는 폴더
    return [Path(p) for p in sorted(found)]


def find_symlinks(directory: Path) -> list[Path]:
    """`directory` 아래의 모든 심볼릭 링크(파일·디렉터리·깨진 링크)를 따라가지 않고 찾는다."""
    found: list[str] = []
    pending = [os.path.abspath(directory)]
    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_symlink():
                            found.append(entry.path)
                        elif entry.is_dir(follow_symlinks=False):
                            pending.append(entry.path)
                    except OSError:
                        continue
        except OSError:
            continue
    return [Path(p) for p in sorted(found)]


def write_file_atomic(file: Path, content: str) -> None:
    """같은 폴더의 임시 파일에 쓴 뒤 `os.replace`로 바꿔 넣는다. 실패하면 임시 파일을 지운다."""
    file.parent.mkdir(parents=True, exist_ok=True)
    # 점으로 시작해 마크다운 목록에 잡히지 않고, 이름이 길어도 255바이트를 넘지 않게 자른다.
    tmp = file.with_name(f".{file.name[:40]}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_bytes(content.encode("utf-8"))
        os.replace(tmp, file)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise


def read_text_if_exists(file: Path, *, max_bytes: int = 4_194_304) -> str:
    """파일의 앞 `max_bytes`를 UTF-8로 읽는다(BOM 제거, 잘못된 바이트는 대체 문자).

    없음·폴더·심볼릭 링크·일반 파일이 아님·읽기 오류는 빈 문자열이다. 예외를 던지지 않는다.
    링크는 `O_NOFOLLOW`로, 이름 있는 파이프는 `O_NONBLOCK`과 종류 확인으로 막는다.
    읽는 양은 파일 크기를 넘지 않는다(`max_bytes`가 아무리 커도 그만큼 잡지 않는다).
    """
    if max_bytes <= 0:
        return ""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(file, flags)
    except (OSError, ValueError):
        return ""
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return ""
        remaining = min(max_bytes, info.st_size)
        chunks: list[bytes] = []
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    except OSError:
        return ""
    finally:
        os.close(fd)
    data = b"".join(chunks)
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    return data.decode("utf-8", errors="replace")

"""사용자 ID 해시용 비공개 솔트. 상태 폴더의 0600 파일에 한 번 만들어 두고 계속 쓴다.

파일이 깨졌거나 심볼릭 링크이면 새로 만들지 않고 오류로 멈춘다(조용히 다시 만들면
기존 기록의 가명이 모두 바뀐다). 오류 메시지에는 파일 내용을 싣지 않는다.
"""

import os
import re
import secrets
import stat
from pathlib import Path
from typing import Final

from team_wiki_engine.config.load import ConfigError

USER_SALT_FILE: Final = "user-salt"
_MAX_BYTES: Final = 256
_HEX64: Final = re.compile(r"[0-9a-f]{64}")


def _bad() -> ConfigError:
    return ConfigError(
        f"상태 폴더의 {USER_SALT_FILE} 파일이 올바르지 않습니다. "
        "일반 파일이어야 하고 소문자 16진수 64자만 담아야 합니다. "
        "지우면 기존 기록의 사용자 가명이 모두 바뀌니 백업에서 복구하세요"
    )


def _read(path: Path) -> str:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise _bad() from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise _bad()
        raw = os.read(fd, _MAX_BYTES + 1)
    except OSError:
        raise _bad() from None
    finally:
        os.close(fd)
    if len(raw) > _MAX_BYTES:
        raise _bad()
    text = raw.decode("ascii", errors="replace")
    if not _HEX64.fullmatch(text):
        raise _bad()
    return text


def load_or_create_user_salt(state_dir: Path) -> str:
    path = state_dir / USER_SALT_FILE
    if os.path.lexists(path):
        return _read(path)
    state_dir.mkdir(parents=True, exist_ok=True)
    salt = secrets.token_hex(32)
    tmp = state_dir / f".{USER_SALT_FILE}.{os.getpid()}.{secrets.token_hex(4)}"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, salt.encode("ascii"))
        finally:
            os.close(fd)
        try:
            os.link(tmp, path)  # 원자적 생성: 이미 있으면 실패해 먼저 만든 쪽 값을 쓴다
        except FileExistsError:
            return _read(path)
        return salt
    except OSError:
        raise ConfigError(f"상태 폴더에 {USER_SALT_FILE} 파일을 만들 수 없습니다") from None
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass

"""`wiki/log.md`: 반영 이력 한 줄씩 덧붙이기."""

import math
import os
import re
import stat
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Literal

from team_wiki_engine.util.fs import write_file_atomic
from team_wiki_engine.util.time import kst_parts
from team_wiki_engine.wiki.frontmatter import parse_frontmatter, serialize_frontmatter

_NEW_BODY = "\n# 반영 이력\n\n"
# `updated`는 `YYYY-MM-DD`로 시작하고 날짜만 있거나 `T`/공백 뒤에 시각이 오는 ASCII 형식만 인정한다.
_ISO_DATE_PREFIX = re.compile(r"([0-9]{4}-[0-9]{2}-[0-9]{2})(?:[T ]|\Z)")
# C0·C1 제어 문자(\r \n \t U+0085 포함)와 줄·문단 구분자 U+2028/2029
_CONTROLS = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]+")


@dataclass
class LogEntry:
    at: datetime
    kind: Literal["code", "ingest"]
    targets: str
    pages: list[str]
    cost_usd: float


def _one_line(text: str) -> str:
    return _CONTROLS.sub(" ", text)


def format_log_line(e: LogEntry) -> str:
    """`- YYYY-MM-DD HH:MM | <kind> | <targets> → <pages> | $<cost>` 한 줄(KST).

    `targets`·페이지 이름의 제어 문자는 공백 하나로 바꿔 줄이나 `---` 구분선을 위조하지 못하게
    하고, 비용이 NaN·inf이거나 0 이하이면 `$0.00`으로 쓴다.
    """
    parts = kst_parts(e.at)
    pages = ", ".join(_one_line(page) for page in e.pages) if e.pages else "(변경 없음)"
    cost = e.cost_usd if math.isfinite(e.cost_usd) and e.cost_usd > 0 else 0.0
    return (
        f"- {parts.date} {parts.time} | {e.kind} | {_one_line(e.targets)} → {pages} | ${cost:.2f}"
    )


def _later_date(existing: object, new: str) -> str:
    """기존 `updated`가 유효한 ISO 날짜(시각이 붙어도 날짜 부분으로 비교)이고 더 늦으면 그대로 둔다.

    형식이 틀리거나 존재하지 않는 날짜(`9999-99-99`, 유니코드 숫자 등)면 새 날짜를 쓴다.
    """
    if not isinstance(existing, str):
        return new
    match = _ISO_DATE_PREFIX.match(existing)
    if match is None:
        return new
    try:
        existing_date = date.fromisoformat(match.group(1))
    except ValueError:
        return new
    return existing if existing_date > date.fromisoformat(new) else new


def _read_log_text(file: Path) -> str:
    """`log.md`를 읽는다. 링크(`ELOOP`)·일반 파일이 아닌 것은 `OSError`, 없으면 `FileNotFoundError`.

    심볼릭 링크 대상의 글이 이력 커밋에 섞이지 않게 `O_NOFOLLOW`로 열고, 이름 있는 파이프에서
    멈추지 않게 `O_NONBLOCK`과 종류 확인을 쓴다. BOM은 읽으면서 버린다. UTF-8이 아니면
    `UnicodeDecodeError`다(예전과 같다).
    """
    fd = os.open(file, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | getattr(os, "O_NONBLOCK", 0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"{file.name}이 일반 파일이 아닙니다")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            data = handle.read()
    finally:
        os.close(fd)
    return data.decode("utf-8-sig")


def append_log(data_dir: Path, e: LogEntry) -> None:
    """`<data_dir>/wiki/log.md` 끝에 한 줄을 붙인다. 파일이 없으면 만든다.

    frontmatter의 `updated`는 기존 값과 새 항목 날짜 중 늦은 쪽이다. frontmatter가 없거나
    읽을 수 없는 기존 파일은 내용을 그대로 본문에 보존하고 새 frontmatter만 앞에 둔다.

    `wiki/log.md`가 심볼릭 링크나 일반 파일이 아니면 `OSError`를 던진다(링크를 따라 읽지 않는다).

    주의: 쓰는 쪽이 하나라고 가정한다(읽고-고쳐-쓰는 방식이라 동시에 두 곳에서 부르면 한쪽
    항목이 사라질 수 있다). frontmatter는 다시 직렬화하므로 주석은 사라지고 따옴표 표기는
    정규화되며, CRLF 파일의 frontmatter 부분은 LF가 된다(본문의 기존 줄바꿈은 그대로다).
    """
    file = data_dir / "wiki" / "log.md"
    try:
        text = _read_log_text(file)
    except FileNotFoundError:
        data: dict[str, object] = {}
        body = _NEW_BODY
    else:
        parsed = parse_frontmatter(text)
        if parsed.data is None:
            data, body = {}, text
        else:
            data, body = dict(parsed.data), parsed.body
    data["updated"] = _later_date(data.get("updated"), kst_parts(e.at).date)
    if not body.endswith("\n"):
        body += "\n"
    write_file_atomic(file, serialize_frontmatter(data, f"{body}{format_log_line(e)}\n"))

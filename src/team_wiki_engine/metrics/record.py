"""상태 파일 이름, 측정 기록 모델, jsonl·json 읽기/쓰기, 위키 보강 후보 소비.

읽기는 관대하다: 없는 파일, 깨진 줄, 상한을 넘는 줄·파일, 검증에 실패한 값은 예외 없이 건너뛰거나
기본값으로 대신한다. 쓰기 쪽 예외 메시지에는 기록 내용을 싣지 않는다.

`read_json`에 넘기는 어댑터가 목록이면 `pydantic.FailFast()`를 붙여 만든다
(`TypeAdapter(Annotated[list[M], FailFast()])`). 안 붙이면 `[{},{},…]`처럼 작은 입력이 항목마다
검증 오류를 쌓아 메모리를 수백 배로 늘린다(1MiB 입력에 약 700MB). 붙이면 첫 오류에서 멈춘다.

`FailFast`는 딕셔너리 어댑터에는 듣지 않는다(1MiB `{"0":{},…}`를 `dict[str, M]`로 읽으면 약
230MB). 딕셔너리로 읽는 상태 파일은 작고 알려진 모양으로만 두고, 이후 태스크는 그런 파일의 메모리를
`FailFast`에 기대지 않는다.
"""

import errno
import json
import os
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated, BinaryIO, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from pydantic_core import to_jsonable_python

from team_wiki_engine.sync.types import StageResultKind, SyncKind
from team_wiki_engine.util.fs import write_file_atomic
from team_wiki_engine.util.time import kst_iso

MAX_JSONL_LINE = 65_536
"""jsonl 한 줄(줄바꿈 제외)의 바이트 상한. 쓰기는 넘으면 거부하고 읽기는 넘는 줄을 버린다."""
MAX_STATE_JSON = 1_048_576
"""상태 JSON 파일의 바이트 상한(1MiB). 상태 파일은 작고, 큰 입력은 검증 오류로 부풀어 오른다."""
MAX_JSONL_FILE = 8_388_608
"""`read_jsonl`이 읽는 내용의 상한(8MiB). 넘으면 거기까지만 돌려준다(뒤쪽 최신 기록은 버려진다)."""

_BOM = b"\xef\xbb\xbf"
_MAX_INT = 2**63 - 1  # 사람이 손으로 넣은 터무니없이 큰 정수가 합계를 망치지 않게 한다


@dataclass(frozen=True)
class StateFiles:
    """`paths.state_dir` 아래 상태 파일 이름. 이름은 여기서만 정한다."""

    questions: str = "questions.jsonl"
    gaps: str = "gaps.jsonl"
    gaps_consumed: str = "gaps-consumed.jsonl"
    sync_runs: str = "sync-runs.jsonl"
    pr_review: str = "pr-review.jsonl"
    oq_events: str = "oq-events.jsonl"
    pr_tracking: str = "pr-tracking.json"
    oq_snapshot: str = "oq-snapshot.json"
    open_pr_snapshot: str = "open-prs-snapshot.json"
    code_snapshot: str = "code-snapshot.json"
    sessions: str = "sessions.json"
    lock: str = "sync.lock"
    data_lock: str = "data.lock"
    bot_control: str = "bot-control.json"


STATE_FILES: Final = StateFiles()

_Count = Annotated[int, Field(ge=0, le=_MAX_INT)]
_Cost = Annotated[float, Field(ge=0)]


class _Record(BaseModel):
    model_config = ConfigDict(
        extra="ignore",
        frozen=True,
        strict=True,  # `"true"`나 `"3"`이 슬며시 바뀌지 않게 한다
        allow_inf_nan=False,
    )


class QuestionRecord(_Record):
    at: str
    user: str
    thread_id: str
    ok: bool
    failure: str | None
    source: Literal["wiki", "code"] | None
    duration_ms: _Count
    cost_usd: _Cost
    input_tokens: _Count
    output_tokens: _Count
    cache_read_input_tokens: _Count
    resumed: bool
    not_found: bool = False


class GapRecord(_Record):
    id: str
    at: str
    question: str
    answer_summary: str
    code_paths: list[str]


class GapConsumedRecord(_Record):
    id: str
    run_id: str
    at: str


class SyncRunRecord(_Record):
    at: str
    run_id: str
    kind: SyncKind
    result: StageResultKind
    pr: str | None
    lint_failed: bool
    cost_usd: _Cost
    input_tokens: _Count
    output_tokens: _Count
    duration_ms: _Count


class PrReviewRecord(_Record):
    at: str
    pr: str
    kind: SyncKind
    outcome: Literal["merged", "rejected"]
    human_changed_files: _Count | None
    human_additions: _Count | None
    human_deletions: _Count | None
    closed_at: str | None


class OqEventRecord(_Record):
    at: str
    kind: Literal["opened", "resolved"]
    title: str


# ---------------------------------------------------------------- jsonl


def append_jsonl(file: Path, record: BaseModel) -> None:
    """기록 한 줄을 파일 끝에 덧붙인다.

    한 번의 `write`로 쓰므로 봇과 동기화가 동시에 써도 줄이 섞이지 않는다(부분 쓰기면 남은 바이트를
    이어 쓴다). 줄이 `MAX_JSONL_LINE`을 넘으면 쓰지 않고 `ValueError`(모델 이름만 담는다).
    파일이 심볼릭 링크면 `OSError`가 그대로 올라가고 링크 대상은 건드리지 않는다.
    """
    body = record.model_dump_json().encode("utf-8")
    if len(body) > MAX_JSONL_LINE:
        raise ValueError(
            f"{type(record).__name__} 기록이 한 줄 상한({MAX_JSONL_LINE}바이트)을 넘습니다"
        )
    data = body + b"\n"
    file.parent.mkdir(parents=True, exist_ok=True)
    # O_NONBLOCK: 이름 있는 파이프가 대상이어도 열기가 멈추지 않는다(읽는 쪽이 없으면 ENXIO).
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(file, flags | getattr(os, "O_NONBLOCK", 0), 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "일반 파일이 아닙니다")
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError(errno.EIO, "기록이 0바이트만 쓰였습니다")
            view = view[written:]
    finally:
        os.close(fd)


def _open_regular(file: Path) -> BinaryIO | None:
    """일반 파일을 바이너리로 연다(링크는 따라간다). 아니거나 열 수 없으면 `None`.

    이름 있는 파이프에서 열기가 멈추지 않게 `O_NONBLOCK`으로 열고 종류를 확인한다. 어느 경로에서도
    서술자를 남기지 않는다.
    """
    try:
        fd = os.open(file, os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NONBLOCK", 0))
    except (OSError, ValueError):
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            return None
    except OSError:
        os.close(fd)
        return None
    try:
        return os.fdopen(fd, "rb")
    except OSError:
        os.close(fd)
        return None


@dataclass(frozen=True)
class JsonlWindow[M: BaseModel]:
    """`read_jsonl(tail=True)`의 결과와, 파일이 상한보다 커서 앞부분을 건너뛰었는지."""

    records: list[M]
    truncated: bool


_TAIL_SLACK = 1_048_576
"""끝 읽기 중에 파일이 조금 더 자라도 마지막 기록까지 읽도록 허용하는 여유 바이트."""


def _read_jsonl[M: BaseModel](file: Path, model: type[M], *, tail: bool) -> JsonlWindow[M]:
    handle = _open_regular(file)
    if handle is None:
        return JsonlWindow([], False)
    records: list[M] = []
    consumed = 0
    first = True
    limit = MAX_JSONL_FILE
    truncated = False
    try:
        with handle:
            if tail:
                size = os.fstat(handle.fileno()).st_size
                if size > MAX_JSONL_FILE:
                    truncated = True
                    first = False
                    limit = MAX_JSONL_FILE + _TAIL_SLACK
                    handle.seek(size - MAX_JSONL_FILE - 1)
                    if handle.read(1) != b"\n":  # 줄 중간에서 시작했다: 잘린 첫 줄을 버린다
                        chunk = handle.readline(MAX_JSONL_LINE + 1)
                        while chunk and not chunk.endswith(b"\n"):
                            chunk = handle.readline(MAX_JSONL_LINE + 1)
            while consumed <= limit:
                chunk = handle.readline(MAX_JSONL_LINE + 1)
                if not chunk:
                    break
                if first and chunk.startswith(_BOM):
                    chunk = chunk[len(_BOM) :]
                first = False
                consumed += len(chunk)
                if not chunk.endswith(b"\n") and len(chunk) > MAX_JSONL_LINE:
                    while chunk and not chunk.endswith(b"\n"):  # 넘는 줄은 끝까지 버린다
                        chunk = handle.readline(MAX_JSONL_LINE + 1)
                        consumed += len(chunk)
                        if consumed > limit:
                            break
                    continue
                line = chunk.strip()
                if (
                    line[:1] != b"{"
                ):  # 빈 줄과 `{`로 시작하지 않는 줄(쓰레기)은 검증기까지 가지 않는다
                    continue
                try:
                    records.append(model.model_validate_json(line))
                except (ValueError, RecursionError, OverflowError):
                    continue
    except (OSError, ValueError):
        return JsonlWindow([], False)
    return JsonlWindow(records, truncated)


def read_jsonl[M: BaseModel](file: Path, model: type[M], *, tail: bool = False) -> list[M]:
    """jsonl 파일을 읽는다. 예외를 던지지 않는다.

    없는 파일·읽기 오류는 `[]`. 상한(`MAX_JSONL_LINE`)을 넘는 줄은 줄 끝까지 버리고(메모리 상한),
    빈 줄과 검증·JSON·UTF-8·재귀 깊이 오류가 난 줄은 그 줄만 건너뛴다. 파일 맨 앞의 UTF-8 BOM은
    무시한다. `{`로 시작하지 않는 줄은 검증 없이 버린다(모든 기록은 객체).

    기본은 앞에서부터 `MAX_JSONL_FILE` 바이트까지 읽고 멈춘다(그 뒤의 최신 기록은 버려진다).
    `tail=True`는 파일이 상한보다 클 때 **끝** `MAX_JSONL_FILE` 바이트를 읽는다(누적 파일에서 최신
    기록을 보려는 용도). 잘린 첫 줄은 버린다. 앞부분을 건너뛰었는지는 `read_jsonl_window`로 안다.
    """
    return _read_jsonl(file, model, tail=tail).records


def read_jsonl_window[M: BaseModel](file: Path, model: type[M]) -> JsonlWindow[M]:
    """`read_jsonl(tail=True)`와 같고, 앞부분을 건너뛰었는지(`truncated`)도 준다."""
    return _read_jsonl(file, model, tail=True)


# ---------------------------------------------------------------- json


def read_json[T](file: Path, adapter: TypeAdapter[T], fallback: T) -> T:
    """상태 JSON 파일을 읽는다. 없음·상한 초과·읽기 오류·검증 실패는 `fallback`이다.

    `fallback`은 복사하지 않고 그 객체를 그대로 돌려준다. 호출자는 결과를 고치면 안 된다
    (고쳐야 하면 호출 쪽에서 복사한다). 파일 맨 앞의 UTF-8 BOM은 무시한다. 목록 어댑터는 모듈
    설명대로 `FailFast`를 붙여 만든다.
    """
    handle = _open_regular(file)
    if handle is None:
        return fallback
    try:
        with handle:
            data = handle.read(MAX_STATE_JSON + 1)
    except OSError:
        return fallback
    if len(data) > MAX_STATE_JSON:
        return fallback
    if data.startswith(_BOM):
        data = data[len(_BOM) :]
    try:
        return adapter.validate_json(data)
    except (ValueError, RecursionError, OverflowError):
        return fallback


def write_json(file: Path, value: object) -> None:
    """값(pydantic 모델 포함)을 들여쓴 JSON으로 원자적으로 쓴다.

    NaN·무한대 같은 JSON에 없는 숫자는 `ValueError`(내용 없는 메시지)이고 파일은 그대로다.
    """
    try:
        text = json.dumps(to_jsonable_python(value), ensure_ascii=False, indent=2, allow_nan=False)
    except ValueError:
        raise ValueError("JSON으로 쓸 수 없는 값이 들어 있습니다(NaN·무한대 등)") from None
    write_file_atomic(file, text + "\n")


# ---------------------------------------------------------------- 위키 보강 후보


def unconsumed_gaps(state_dir: Path, limit: int) -> list[GapRecord]:
    """소비하지 않은 보강 후보를 파일 순서대로, 마지막 `limit`개만 돌려준다.

    같은 id가 여러 번 있으면 처음 것만 남긴다.
    """
    if limit <= 0:
        return []
    done = read_jsonl(state_dir / STATE_FILES.gaps_consumed, GapConsumedRecord)
    seen = {r.id for r in done}
    pending: list[GapRecord] = []
    for gap in read_jsonl(state_dir / STATE_FILES.gaps, GapRecord):
        if gap.id not in seen:
            seen.add(gap.id)
            pending.append(gap)
    return pending[-limit:]


def mark_gaps_consumed(state_dir: Path, ids: Sequence[str], run_id: str, at: datetime) -> None:
    """소비 기록을 덧붙인다(`gaps.jsonl`은 고쳐 쓰지 않는다). 같은 id는 한 번만 쓴다.

    `ids`가 문자열 하나이면 글자마다 기록되는 사고를 막으려고 `TypeError`로 거부한다.
    """
    if isinstance(ids, (str, bytes)):
        raise TypeError("ids는 문자열 하나가 아니라 id 목록이어야 합니다")
    file = state_dir / STATE_FILES.gaps_consumed
    stamp = kst_iso(at)
    for gap_id in dict.fromkeys(ids):
        append_jsonl(file, GapConsumedRecord(id=gap_id, run_id=run_id, at=stamp))

"""`baseline.json`: 마지막으로 반영한 저장소 커밋과 처리한 원본 목록."""

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Annotated, Any

from pydantic import BaseModel, Field, StrictBool, StrictStr, ValidationError
from pydantic.types import StringConstraints

from team_wiki_engine.util.fs import write_file_atomic

_FILE_NAME = "baseline.json"

_ERROR_MESSAGES = {
    "string_pattern_mismatch": "40자 소문자 16진 SHA여야 합니다",
    "bool_type": "불리언(true/false)이어야 합니다",
    "string_type": "문자열이어야 합니다",
    "missing": "값이 없습니다",
    "dict_type": "객체여야 합니다",
    "model_type": "객체여야 합니다",
    "list_type": "배열이어야 합니다",
}


@dataclass
class RepoBaseline:
    commit: str
    archived: bool


@dataclass
class Baseline:
    repos: dict[str, RepoBaseline] = field(default_factory=dict)
    raw_processed: list[str] = field(default_factory=list)
    updated_at: str | None = None


class _RepoModel(BaseModel):
    commit: Annotated[StrictStr, StringConstraints(pattern=r"^[0-9a-f]{40}$")]
    archived: StrictBool


class _BaselineModel(BaseModel):
    repos: dict[StrictStr, _RepoModel] = Field(default_factory=dict)
    raw_processed: list[StrictStr] = Field(default_factory=list)
    updated_at: StrictStr | None = None


def empty_baseline() -> Baseline:
    return Baseline()


def _describe(error: ValidationError) -> str:
    lines = []
    for issue in error.errors():
        path = ".".join(str(part) for part in issue["loc"]) or "(root)"
        message = _ERROR_MESSAGES.get(issue["type"], issue["msg"])
        lines.append(f"{path}: {message}")
    return "; ".join(lines)


def read_baseline(data_dir: Path) -> Baseline:
    """`<data_dir>/baseline.json`을 읽어 검증한다. 파일이 없으면 빈 기준을 돌려준다.

    JSON이 아니거나 형식이 틀리면 `ValueError`(형식 오류는 키 경로 포함)를 던진다.
    """
    file = data_dir / _FILE_NAME
    try:
        text = file.read_text(encoding="utf-8")
    except FileNotFoundError:
        return empty_baseline()
    except UnicodeDecodeError as exc:
        raise ValueError(f"{_FILE_NAME}이 UTF-8 텍스트가 아닙니다: {exc.reason}") from exc
    try:
        raw: Any = json.loads(text)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ValueError(f"{_FILE_NAME}이 JSON이 아닙니다: {exc}") from exc
    try:
        model = _BaselineModel.model_validate(raw)
    except ValidationError as exc:
        raise ValueError(f"{_FILE_NAME} 형식 오류: {_describe(exc)}") from exc
    return Baseline(
        repos={
            name: RepoBaseline(commit=repo.commit, archived=repo.archived)
            for name, repo in model.repos.items()
        },
        raw_processed=model.raw_processed,
        updated_at=model.updated_at,
    )


def write_baseline(data_dir: Path, b: Baseline) -> None:
    """검증한 뒤 들여쓰기 2칸·끝 개행으로 원자적으로 쓴다.

    `read_baseline`과 같은 모델로 먼저 검증하므로(나쁜 커밋·타입은 `ValueError`, 쓰기 전에
    실패해 기존 파일은 그대로다) 읽을 수 없는 baseline.json을 만들지 못한다.
    """
    data = asdict(b)
    try:
        _BaselineModel.model_validate(data)
    except ValidationError as exc:
        raise ValueError(f"{_FILE_NAME} 형식 오류(쓰지 않음): {_describe(exc)}") from exc
    text = json.dumps(data, indent=2, ensure_ascii=False)
    write_file_atomic(data_dir / _FILE_NAME, f"{text}\n")

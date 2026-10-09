"""설정·비밀값 읽기와 서버 작업 폴더 계산."""

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import ValidationError
from pydantic_core import ErrorDetails

from team_wiki_engine.config.schema import TeamConfig


class ConfigError(Exception):
    """설정·환경변수 문제. 메시지에는 비밀값이 들어가지 않는다."""


def _count(error: ErrorDetails, key: str) -> int:
    return int(error.get("ctx", {}).get(key, 0))


# pydantic의 영어 기본 문구를 한국어로 바꾼다. 없는 유형은 pydantic 문구를 그대로 쓴다.
# (사용자 지정 검사는 schema.py에서 처음부터 한국어 문구를 낸다.)
_KOREAN: dict[str, Callable[[ErrorDetails], str]] = {
    "missing": lambda e: "필수 항목입니다",
    "extra_forbidden": lambda e: "알 수 없는 키입니다",
    "model_type": lambda e: "객체여야 합니다",
    "dict_type": lambda e: "객체여야 합니다",
    "list_type": lambda e: "배열이어야 합니다",
    "string_type": lambda e: "문자열이어야 합니다",
    "int_type": lambda e: "정수여야 합니다",
    "string_too_short": lambda e: (
        "비어 있을 수 없습니다"
        if _count(e, "min_length") <= 1
        else f"{_count(e, 'min_length')}자 이상이어야 합니다"
    ),
    "too_short": lambda e: f"최소 {_count(e, 'min_length')}개 이상 필요합니다",
    "greater_than": lambda e: f"{_count(e, 'gt') + 1} 이상의 정수여야 합니다",
    "literal_error": lambda e: f"허용된 값이 아닙니다: {e.get('ctx', {}).get('expected', '')}",
}


def _describe(error: ErrorDetails) -> str:
    path = ".".join(str(part) for part in error["loc"]) or "(root)"
    translate = _KOREAN.get(error["type"])
    reason = translate(error) if translate is not None else error["msg"]
    return f"- {path}: {reason}"


def parse_config(raw: object) -> TeamConfig:
    try:
        return TeamConfig.model_validate(raw)
    except ValidationError as e:
        # include_input=False: 입력값이 메시지에 섞이지 않게 한다.
        lines = [_describe(err) for err in e.errors(include_url=False, include_input=False)]
        raise ConfigError("설정 파일이 올바르지 않습니다:\n" + "\n".join(lines)) from None


def _printable(path: Path | str) -> str:
    """메시지에 쓸 경로. NUL 같은 제어 문자는 이스케이프해서 보인다."""
    return "".join(
        ch if ch.isprintable() else ch.encode("unicode_escape").decode() for ch in str(path)
    )


def load_config(path: Path | str) -> TeamConfig:
    shown = _printable(path)
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, ValueError):  # UnicodeDecodeError, 경로의 NUL 문자는 ValueError다
        raise ConfigError(f"설정 파일을 읽을 수 없습니다: {shown}") from None
    try:
        raw: object = json.loads(text)
    except json.JSONDecodeError as e:
        raise ConfigError(f"설정 파일이 JSON이 아닙니다: {shown} ({e})") from None
    except ValueError:  # 예: 4300자리를 넘는 정수 리터럴. 파일 내용은 메시지에 싣지 않는다.
        raise ConfigError(f"설정 파일이 JSON이 아닙니다: {shown} (해석할 수 없는 값)") from None
    return parse_config(raw)


Role = Literal["bot", "sync"]


@dataclass(frozen=True)
class Secrets:
    # repr=False: 로그·예외·디버그 출력에 토큰이 새지 않게 한다.
    anthropic_api_key: str = field(repr=False)
    discord_token: str | None = field(repr=False)
    github_token: str | None = field(repr=False)


def load_secrets(env: Mapping[str, str], config: TeamConfig, role: Role) -> Secrets:
    def get(key: str) -> str | None:
        value = env.get(key, "").strip()
        return value or None

    anthropic_api_key = get("ANTHROPIC_API_KEY")
    discord_token = get("DISCORD_TOKEN")
    github_token = get("GITHUB_TOKEN")

    missing: list[str] = []
    if anthropic_api_key is None:
        missing.append("ANTHROPIC_API_KEY")
    if role == "bot" and discord_token is None:
        missing.append("DISCORD_TOKEN")
    if config.delivery == "github" and github_token is None:
        missing.append("GITHUB_TOKEN (delivery: github)")
    if anthropic_api_key is None or missing:
        raise ConfigError(f"필요한 환경변수가 없습니다: {', '.join(missing)}")
    return Secrets(anthropic_api_key, discord_token, github_token)


@dataclass(frozen=True)
class Paths:
    home: Path
    engine_dir: Path
    skills_dir: Path
    data_dir: Path
    data_sync_dir: Path
    repos_dir: Path
    state_dir: Path
    runs_dir: Path
    local_prs_dir: Path


_DEFAULT_HOME = "/srv/team-wiki"


def _absolute(path: Path | str) -> Path:
    # resolve()와 달리 심볼릭 링크를 따라가지 않고 `..`·`.`만 정리한다.
    return Path(os.path.abspath(path))


def resolve_paths(env: Mapping[str, str], engine_dir: Path | str) -> Paths:
    configured = env.get("TEAM_WIKI_HOME", "").strip()  # 앞뒤 공백은 벗긴 값을 쓰고, 빈 값은 기본값
    home = _absolute(configured if configured else _DEFAULT_HOME)
    engine = _absolute(engine_dir)
    state_dir = home / "state"
    return Paths(
        home=home,
        engine_dir=engine,
        skills_dir=engine / "skills",
        data_dir=home / "data",
        data_sync_dir=home / "data-sync",
        repos_dir=home / "repos",
        state_dir=state_dir,
        runs_dir=state_dir / "runs",
        local_prs_dir=state_dir / "local-prs",
    )

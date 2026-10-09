"""설정 파일(`config/team.json`)의 pydantic 모델.

JSON 키는 camelCase(`reviewMode`, `dataRepo`, ...)로 받고, 파이썬 속성은 snake_case로 쓴다.
알 수 없는 키는 거부한다(오타를 조용히 넘기지 않는다).
"""

import re
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError, field_validator
from pydantic.alias_generators import to_camel
from pydantic_core import InitErrorDetails, PydanticCustomError

Effort = Literal["low", "medium", "high", "xhigh", "max"]

_NAME_PATTERN = re.compile(r"[a-z0-9-]+")
# `\d`는 유니코드 숫자도 받으므로 [0-9]를 쓴다.
_TIME_PATTERN = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]")


def _check_repo_name(value: str) -> str:
    # `$`는 끝의 줄바꿈 앞에서도 맞으므로 fullmatch를 쓴다.
    if _NAME_PATTERN.fullmatch(value) is None:
        raise PydanticCustomError("repo_name_format", "소문자, 숫자, 하이픈만 쓸 수 있습니다")
    return value


def _check_clock_time(value: str) -> str:
    if _TIME_PATTERN.fullmatch(value) is None:
        raise PydanticCustomError("clock_time_format", "HH:MM 형식이어야 합니다")
    return value


RepoName = Annotated[str, AfterValidator(_check_repo_name)]
ClockTime = Annotated[str, AfterValidator(_check_clock_time)]
NonEmptyStr = Annotated[str, Field(min_length=1)]
PositiveInt = Annotated[int, Field(gt=0)]


class _ConfigModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,  # JSON의 "3"이나 true가 정수로 슬며시 바뀌지 않게 한다
        frozen=True,
        alias_generator=to_camel,
        populate_by_name=True,
    )


class RepoConfig(_ConfigModel):
    name: RepoName
    url: NonEmptyStr
    branch: NonEmptyStr = "main"
    status: Literal["active", "archived"] = "active"


class DataRepoConfig(_ConfigModel):
    url: NonEmptyStr
    branch: NonEmptyStr = "main"


class DiscordConfig(_ConfigModel):
    guild_id: str = ""
    channel_ids: list[str] = Field(default_factory=list)
    ops_channel_id: str = ""
    admin_user_id: str = ""


class EffortConfig(_ConfigModel):
    bot: Effort = "low"
    sync: Effort = "medium"


class LimitsConfig(_ConfigModel):
    per_user_per_minute: PositiveInt = 2
    daily_total: PositiveInt = 60
    concurrent: PositiveInt = 2
    bot_timeout_ms: PositiveInt = 300000
    sync_timeout_ms: PositiveInt = 1200000
    diff_bytes_per_repo: PositiveInt = 200000
    pr_body_chars: PositiveInt = 4000


def _default_schedule() -> list[ClockTime]:
    return ["09:00", "21:00"]


class TeamConfig(_ConfigModel):
    repos: Annotated[list[RepoConfig], Field(min_length=1)]
    data_repo: DataRepoConfig
    delivery: Literal["local", "github"] = "local"
    discord: DiscordConfig = Field(default_factory=DiscordConfig)
    schedule: Annotated[list[ClockTime], Field(min_length=1)] = Field(
        default_factory=_default_schedule
    )
    timezone: NonEmptyStr = "Asia/Seoul"
    review_mode: Literal["pr", "direct"] = "pr"
    model: NonEmptyStr = "claude-sonnet-5-5"
    effort: EffortConfig = Field(default_factory=EffortConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)

    @field_validator("repos")
    @classmethod
    def _reject_duplicate_names(cls, repos: list[RepoConfig]) -> list[RepoConfig]:
        seen: set[str] = set()
        problems: list[InitErrorDetails] = []
        for index, repo in enumerate(repos):
            if repo.name in seen:
                problems.append(
                    InitErrorDetails(
                        type=PydanticCustomError(
                            "duplicate_repo_name", "저장소 이름 중복: {name}", {"name": repo.name}
                        ),
                        loc=(index, "name"),
                        input=repo.name,
                    )
                )
            seen.add(repo.name)
        if problems:
            # 오류 위치를 `repos.<번호>.name`으로 남기려고 ValidationError를 그대로 올린다.
            raise ValidationError.from_exception_data(cls.__name__, problems)
        return repos

"""실행 프로필(도구·스킬·턴 수)과 차단 도구 목록. 모두 변경할 수 없다."""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from team_wiki_engine.claude.types import ClaudeProfileName


@dataclass(frozen=True, slots=True)
class ClaudeProfile:
    name: ClaudeProfileName
    tools: tuple[str, ...]
    skills: tuple[str, ...]
    max_turns: int
    effort_key: Literal["bot", "sync"]


_READ_WRITE_TOOLS = ("Read", "Grep", "Glob", "Edit", "Write")

PROFILES: Mapping[ClaudeProfileName, ClaudeProfile] = MappingProxyType(
    {
        "bot": ClaudeProfile(
            name="bot",
            tools=("Read", "Grep", "Glob"),
            skills=("wiki-schema", "ask"),
            max_turns=30,
            effort_key="bot",
        ),
        "sync-code": ClaudeProfile(
            name="sync-code",
            tools=_READ_WRITE_TOOLS,
            skills=("wiki-schema", "sync-code"),
            max_turns=80,
            effort_key="sync",
        ),
        "ingest": ClaudeProfile(
            name="ingest",
            tools=_READ_WRITE_TOOLS,
            skills=("wiki-schema", "ingest"),
            max_turns=80,
            effort_key="sync",
        ),
    }
)

# tools 제한과 겹치는 이중 방어. 의도를 표면에 남긴다.
DISALLOWED_TOOLS: tuple[str, ...] = (
    "Bash",
    "NotebookEdit",
    "WebFetch",
    "WebSearch",
    "Agent",
    "Task",
    "Skill",
    "Read(**/.env)",
    "Grep(**/.env)",
)

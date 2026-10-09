"""스킬 파일로 시스템 프롬프트에 덧붙일 글을 조립한다."""

import re
from pathlib import Path

from team_wiki_engine.claude.profiles import PROFILES
from team_wiki_engine.claude.types import ClaudeProfileName

# BOM은 보이지 않는 문자를 소스에 넣지 않으려고 이스케이프로 쓴다.
# 닫는 구분선 앞의 본문은 비어 있어도 된다(`---\n---\nbody`).
_FRONTMATTER = re.compile(
    r"\A\ufeff?---\r?\n(?:.*?\r?\n)?---[ \t]*(?:\r?\n|\Z)",
    re.DOTALL,
)

_cache: dict[tuple[ClaudeProfileName, str], str] = {}


def strip_frontmatter(text: str) -> str:
    """맨 앞 frontmatter를 뗀다. 여는 줄이나 닫는 줄이 없으면 그대로 돌려준다."""
    return _FRONTMATTER.sub("", text, count=1)


def _read_skill(name: str, skills_dir: Path) -> str:
    file = skills_dir / name / "SKILL.md"
    try:
        # utf-8-sig: 파일 앞의 BOM을 읽으면서 버린다(frontmatter가 없는 파일 포함).
        text = file.read_text(encoding="utf-8-sig")
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"스킬 파일을 읽을 수 없습니다: {file}") from exc
    except OSError as exc:
        raise OSError(f"스킬 파일을 읽을 수 없습니다: {file}") from exc
    except UnicodeDecodeError as exc:
        raise ValueError(f"스킬 파일이 UTF-8이 아닙니다: {file}") from exc
    body = strip_frontmatter(text).replace("\r\n", "\n").strip()
    if body == "":
        raise ValueError(f"스킬 본문이 비어 있습니다: {file}")
    return f"# {name}\n\n{body}"


def build_system_append(profile: ClaudeProfileName, skills_dir: Path) -> str:
    """프로필의 스킬을 순서대로 읽어 `# <이름>\\n\\n<본문>` 절들을 빈 줄로 잇는다.

    성공한 결과만 `(profile, str(skills_dir))` 키로 프로세스 동안 캐시한다.
    """
    key = (profile, str(skills_dir))
    cached = _cache.get(key)
    if cached is not None:
        return cached
    result = "\n\n".join(_read_skill(name, skills_dir) for name in PROFILES[profile].skills)
    _cache[key] = result
    return result


def clear_prompt_cache() -> None:
    _cache.clear()

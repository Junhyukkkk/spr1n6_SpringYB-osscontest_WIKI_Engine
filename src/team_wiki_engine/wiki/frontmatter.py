"""위키 문서 맨 앞의 YAML frontmatter 읽기·쓰기.

PyYAML의 기본 `SafeLoader`는 YAML 1.1 규칙이라 위키 문서에 맞지 않는다.
- `2026-10-07`이 `date` 객체가 된다 → 날짜는 문자열로 남겨야 한다.
- `yes/no/on/off`가 불리언이 된다 → `status: no`, `owners: [on]`은 문자열이어야 한다.
- `12:30`이 60진수 정수(750)가 된다 → 시각 표기는 문자열이어야 한다.
그래서 해당 암시 리졸버를 뺀 전용 로더·덤퍼를 쓴다. 두 클래스가 같은 리졸버 집합을 쓰므로
직렬화한 값은 읽으면 같은 값으로 돌아온다(애매한 문자열 `"true"`, `"123"`은 따옴표로 감싸진다).
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import yaml

# PyYAML은 바이트당 수 마이크로초가 든다(2MB가 7초). 위키 frontmatter는 이보다 훨씬 작다.
MAX_FRONTMATTER_CHARS = 65_536
_MAX_REASON_CHARS = 200  # 오류 메시지에 싣는 PyYAML 설명의 최대 길이

_BOOL_TAG = "tag:yaml.org,2002:bool"
_INT_TAG = "tag:yaml.org,2002:int"
_FLOAT_TAG = "tag:yaml.org,2002:float"
_TIMESTAMP_TAG = "tag:yaml.org,2002:timestamp"
_MERGE_TAG = "tag:yaml.org,2002:merge"
# JSON으로 표현되지 않는 값을 만드는 명시적 태그(`!!timestamp 2026-10-07` 등)
_NON_JSON_TAGS = frozenset(
    f"tag:yaml.org,2002:{name}" for name in ("timestamp", "binary", "set", "omap", "pairs")
)

# PyYAML 기본 정규식에서 60진수(`1:30`) 항목만 뺀 것
_INT_RE = re.compile(
    r"^(?:[-+]?0b[0-1_]+|[-+]?0[0-7_]+|[-+]?(?:0|[1-9][0-9_]*)|[-+]?0x[0-9a-fA-F_]+)$"
)
_FLOAT_RE = re.compile(
    r"^(?:[-+]?(?:[0-9][0-9_]*)\.[0-9_]*(?:[eE][-+][0-9]+)?"
    r"|\.[0-9][0-9_]*(?:[eE][-+][0-9]+)?"
    r"|[-+]?\.(?:inf|Inf|INF)"
    r"|\.(?:nan|NaN|NAN))$"
)
_BOOL_RE = re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$")


def _wiki_resolvers() -> dict[str, list[tuple[str, re.Pattern[str]]]]:
    """SafeLoader의 암시 리졸버를 복사해 위키용으로 바꾼다(원본 클래스는 건드리지 않는다)."""
    replaced = {_BOOL_TAG, _INT_TAG, _FLOAT_TAG, _TIMESTAMP_TAG}
    table: dict[str, list[tuple[str, re.Pattern[str]]]] = {}
    for first_char, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items():
        kept = [(tag, regexp) for tag, regexp in resolvers if tag not in replaced]
        if kept:
            table[first_char] = kept
    additions = (
        (_BOOL_TAG, _BOOL_RE, "tTfF"),
        (_INT_TAG, _INT_RE, "-+0123456789"),
        (_FLOAT_TAG, _FLOAT_RE, "-+0123456789."),
    )
    for tag, regexp, first_chars in additions:
        for char in first_chars:
            table.setdefault(char, []).append((tag, regexp))
    return table


class _ForbiddenSyntax(yaml.YAMLError):
    """위키 frontmatter에서 쓸 수 없는 YAML 문법. 메시지는 그대로 사용자에게 보인다."""


class _WikiLoader(yaml.SafeLoader):
    yaml_implicit_resolvers = _wiki_resolvers()
    # 원본 SafeLoader를 건드리지 않도록 복사한 뒤 JSON이 아닌 값의 생성기를 뺀다.
    # 빠진 태그는 "could not determine a constructor" 오류가 된다.
    yaml_constructors = {
        tag: constructor
        for tag, constructor in yaml.SafeLoader.yaml_constructors.items()
        if tag not in _NON_JSON_TAGS
    }

    def compose_node(self, parent: Any, index: Any) -> Any:
        # 앵커·별칭은 공유 구조(DAG)로 풀려 모든 소비자를 폭발시킬 수 있다(별칭 폭탄).
        # 위키 frontmatter에는 쓸 곳이 없으므로 모두 거부한다.
        event = self.peek_event()  # type: ignore[no-untyped-call]
        if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None) is not None:
            raise _ForbiddenSyntax("앵커·별칭은 쓸 수 없습니다")
        return super().compose_node(parent, index)

    def flatten_mapping(self, node: Any) -> None:
        # `<<` 병합 키는 별칭 없이 인라인 매핑으로도 쓸 수 있어 따로 막는다.
        if any(key_node.tag == _MERGE_TAG for key_node, _ in node.value):
            raise _ForbiddenSyntax("병합 키(<<)는 쓸 수 없습니다")


class _WikiDumper(yaml.SafeDumper):
    yaml_implicit_resolvers = _wiki_resolvers()


def _represent_date(dumper: yaml.SafeDumper, value: date) -> yaml.ScalarNode:
    # timestamp 리졸버가 없어 `!!timestamp` 태그가 붙지 않도록 날짜 객체도 문자열로 낸다.
    return dumper.represent_str(value.isoformat())


_WikiDumper.add_representer(date, _represent_date)
_WikiDumper.add_representer(datetime, _represent_date)


# BOM은 보이지 않는 문자를 소스에 넣지 않으려고 이스케이프로 쓴다.
# 닫는 줄은 첫 `---` 줄까지만 인식한다. 빈 frontmatter(`---\n---`)를 먼저 시도해야
# 본문 속 `---`까지 YAML로 삼키지 않는다. 각 가지는 입력 길이에 선형이다.
_FRONTMATTER = re.compile(
    r"\A\ufeff?---\r?\n"
    r"(?:---[ \t]*(?:\r?\n|\Z)|(?P<yaml>.*?)\r?\n---[ \t]*(?:\r?\n|\Z))",
    re.DOTALL,
)


@dataclass
class ParsedFrontmatter:
    data: dict[str, Any] | None
    body: str
    error: str | None


def _load_mapping_source(source: str) -> object:
    """YAML 문서 하나를 읽는다. 내용이 없는 문서(공백·주석뿐)는 빈 매핑으로 본다."""
    loader = _WikiLoader(source)
    try:
        node = loader.get_single_node()
        if node is None:
            return {}
        return loader.construct_document(node)
    finally:
        loader.dispose()


def _check_json_like(root: object) -> str | None:
    """모든 깊이의 키가 `str`이고 값이 `str/int/float/bool/None/list/dict`인지 반복문으로 확인한다.

    문제가 있으면 오류 메시지를, 없으면 `None`을 돌려준다. float의 NaN·inf는 허용하지만
    NaN은 `==`로 비교되지 않고 표준 JSON으로 쓸 수 없다.
    """
    stack = [root]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            for key, child in value.items():
                if not isinstance(key, str):
                    return "frontmatter의 키는 문자열이어야 합니다"
                stack.append(child)
        elif isinstance(value, list):
            stack.extend(value)
        elif value is not None and not isinstance(value, str | int | float | bool):
            return "frontmatter에 지원하지 않는 값이 있습니다"
    return None


def parse_frontmatter(text: str) -> ParsedFrontmatter:
    """`---` 구분선 사이의 YAML을 읽는다.

    frontmatter가 없거나 닫는 줄이 없으면 `data=None, error=None`에 전체를 본문으로 돌려준다.
    성공한 `data`는 JSON으로 표현되는 값(문자열 키의 dict, list, str, int, float, bool, None)만
    담는다. 앵커·별칭·병합 키, 날짜·바이너리·집합 같은 명시적 태그, 64KB 초과는 오류다.
    어떤 입력이든 `Exception`은 던지지 않고 `error`로 돌려준다(`KeyboardInterrupt`는 전파).
    """
    match = _FRONTMATTER.match(text)
    if match is None:
        return ParsedFrontmatter(data=None, body=text, error=None)
    body = text[match.end() :]
    source = match.group("yaml") or ""
    if len(source) > MAX_FRONTMATTER_CHARS:
        return ParsedFrontmatter(None, body, "frontmatter가 너무 큽니다 (최대 64KB)")
    try:
        value = _load_mapping_source(source)
        if not isinstance(value, dict):
            return ParsedFrontmatter(None, body, "frontmatter가 키-값 형식이 아닙니다")
        problem = _check_json_like(value)
    except _ForbiddenSyntax as exc:
        return ParsedFrontmatter(None, body, f"frontmatter YAML 오류: {exc}")
    except yaml.YAMLError as exc:
        first_line = str(exc).split("\n", 1)[0]
        if len(first_line) > _MAX_REASON_CHARS:  # 태그 글 등 입력이 메시지에 실려 올 수 있다
            first_line = first_line[:_MAX_REASON_CHARS] + "…"
        return ParsedFrontmatter(None, body, f"frontmatter YAML 오류: {first_line}")
    except RecursionError:
        return ParsedFrontmatter(None, body, "frontmatter YAML 오류: 중첩이 너무 깊습니다")
    except Exception as exc:
        # PyYAML의 스칼라 생성기는 ValueError·KeyError·IndexError 같은 예외를 그대로 던진다
        # (`!!int abc`, 4300자리 넘는 숫자). 메시지에 입력이 들어 있을 수 있어 클래스 이름만 쓴다.
        reason = f"해석할 수 없는 값 ({type(exc).__name__})"
        return ParsedFrontmatter(None, body, f"frontmatter YAML 오류: {reason}")
    if problem is not None:
        return ParsedFrontmatter(None, body, problem)
    return ParsedFrontmatter(data=value, body=body, error=None)


def serialize_frontmatter(data: Mapping[str, Any], body: str) -> str:
    """`---`·YAML·`---` 뒤에 본문을 붙인다. 본문이 줄바꿈으로 시작하지 않으면 빈 줄을 넣는다."""
    head = yaml.dump(
        dict(data),
        Dumper=_WikiDumper,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    ).rstrip()
    separator = "" if body.startswith("\n") else "\n"
    return f"---\n{head}\n---\n{separator}{body}"

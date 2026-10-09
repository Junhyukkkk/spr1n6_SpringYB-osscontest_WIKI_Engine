"""L1 비밀값 탐지와, 점검 메시지에 페이지 내용을 옮길 때 쓰는 마스킹.

- 탐지는 입력 길이에 선형이다. 중첩 수량자가 든 정규식으로 긴 줄을 훑지 않고, 키워드 위치만
  단순 패턴으로 찾은 뒤 적중마다 길이가 제한된 국소 검사를 한다.
- 마스킹: 페이지에서 가져온 글(링크 대상, YAML 오류, 파일 이름 등)이 비밀값처럼 보이면
  메시지에 싣지 않는다. 같은 탐지 코드를 쓰므로 "페이지에서 지적할 것"과 "메시지에서
  가릴 것"이 어긋나지 않는다.
"""

import re
import unicodedata
from functools import lru_cache
from typing import Final

# ---------------------------------------------------------------- 상한(모두 한 곳에)
MAX_KEY_TAIL = 64  # 키워드 뒤에 붙는 키 이름 꼬리(`_a_b`)의 최대 길이
MAX_SKIP_RUN = 64  # 구분자·값 앞의 공백/따옴표/강조 기호 연속의 최대 길이
MAX_VALUE_CHARS = 256  # 대입 값은 앞 256자만 판정한다
MAX_URL_USER_CHARS = 64  # URL 자격증명의 사용자 부분
MAX_URL_PASSWORD_CHARS = 128  # URL 자격증명의 비밀번호 부분
MAX_QUALIFIER = 32  # 키워드와 구분자 사이 괄호 설명 `(운영)`의 최대 길이
MAX_PARTICLE_VALUE = 64  # 한글 조사 뒤 영문 값의 최대 길이
MAX_ECHO_CHARS = 100  # 메시지에 싣는 페이지 글의 최대 길이
MAX_MASK_INPUT = 2000  # 마스킹하려고 훑는 글의 최대 길이(그보다 긴 부분은 버린다)

OMITTED = "(비밀값으로 보이는 값이라 생략)"  # 값 하나를 통째로 가릴 때
WORD_OMITTED = "<생략>"  # 글 속 단어 하나를 가릴 때
NAME_OMITTED = "<이름 생략>"  # 파일·폴더 이름 하나를 가릴 때

# ---------------------------------------------------------------- 토큰 형식
# 각 패턴은 입력 길이에 선형이다: 반복 문자 집합이 시작 문자열(`sk-ant-`, `ghp_` 등)과
# 겹치지 않거나, 실패하는 시도가 상수 길이 안에서 끝난다.
_TOKEN_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"), "Anthropic API 키 형식"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"), "GitHub 토큰 형식"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{50,}"), "GitHub fine-grained 토큰 형식"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AWS 액세스 키 형식"),
    (re.compile(r"-----BEGIN [A-Z ]{0,64}PRIVATE KEY-----"), "개인 키"),
    (re.compile(r"xox[abposr]-[A-Za-z0-9-]{10,}"), "Slack 토큰 형식"),
    (
        re.compile(r"[MN][A-Za-z0-9_-]{23,25}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,}"),
        "Discord 봇 토큰 형식",
    ),
)

# `//` 뒤의 `사용자:비밀번호@`(`scheme://`와 scheme 없는 `//user:pw@host` 둘 다).
# 길이를 제한하고 소유형(+) 수량자로 되돌림을 없앤다.
_URL_SLASHES = re.compile(r"//")
_URL_USERINFO = re.compile(  # 사용자는 비어 있어도 된다(`redis://:비밀번호@host`)
    rf"([^\s:/@]{{0,{MAX_URL_USER_CHARS}}}+):([^\s/@]{{1,{MAX_URL_PASSWORD_CHARS}}}+)@"
)
_SCHEME_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+.-")
_ENV_REFERENCE = re.compile(r"\$\{?[A-Za-z_][A-Za-z0-9_]*\}?")  # $NAME, ${NAME}
# 문서 예시에 흔한 단어: `postgres://user:password@host`
_PASSWORD_WORDS = frozenset({"password", "pass", "passwd", "pw", "secret", "비밀번호"})

# ---------------------------------------------------------------- 키워드 대입
# 키워드 위치만 단순 패턴(중첩 수량자 없음)으로 찾고, 적중마다 길이가 제한된 국소 검사를 한다.
# 적중 한 번의 비용이 상수라서 전체가 입력 길이에 선형이다.
_KEYWORD = re.compile(
    r"password|passwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|credential"
    r"|비밀번호|비번|패스워드|토큰|시크릿",
    re.IGNORECASE | re.ASCII,
)
_KEY_TAIL = re.compile(rf"[A-Za-z0-9_-]{{0,{MAX_KEY_TAIL + 1}}}")
_KEY_TAIL_SHAPE = re.compile(r"(?:[_-][A-Za-z0-9]+)*")  # 길이 ≤ 64에만 적용한다
_SKIP_BEFORE_SEPARATOR = re.compile(rf"[ \t\"'*`]{{0,{MAX_SKIP_RUN}}}+")
_LABEL_WORD = re.compile(r"값|키|key|value", re.IGNORECASE | re.ASCII)
_QUALIFIER = re.compile(rf"\([^()\n]{{0,{MAX_QUALIFIER}}}+\)")  # `비밀번호(운영): …`
_SEPARATOR = re.compile(r"[:=|：]")
_SKIP_BEFORE_VALUE = re.compile(rf"[ \t]{{0,{MAX_SKIP_RUN}}}+[\"'*`]{{0,{MAX_SKIP_RUN}}}+")
_VALUE = re.compile(rf"[^\s\"'`,;)|]{{1,{MAX_VALUE_CHARS}}}")
# 한글 문장 속 값(`비밀번호는 Xk9mP2qa7입니다`): 조사 + 여는 따옴표·강조(`*`) + 영문·숫자 8~64자.
# 값 뒤는 끝·공백·따옴표·백틱·`*`·한글이거나, 문장부호(`.,!?;:)~`) 다음에 공백·끝이 와야 한다
# (`Xk9mP2qa7.0`, `Xk9mP2qa7-x`, `JWT(RS256)`처럼 값이 더 이어지면 걸러진다). 대문자·소문자·숫자가
# 모두 있어야 한다(`bcrypt12`, `base64url`은 통과).
_PARTICLE_VALUE = re.compile(
    rf"[은는이가][ \t]{{1,{MAX_SKIP_RUN}}}+(?:[\"'`]|\*{{1,2}})?"
    rf"([A-Za-z0-9]{{8,{MAX_PARTICLE_VALUE}}}+)"
    r"(?=\Z|[\s\"'`*가-힣]|[.,!?;:)~](?:\s|\Z))"
)
_MIXED_CLASSES = (re.compile(r"[A-Z]"), re.compile(r"[a-z]"), re.compile(r"[0-9]"))
_ASCII_ALNUM = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789")

_HAS_LETTER = re.compile(r"[A-Za-z]")
_HAS_DIGIT = re.compile(r"[0-9]")


# ================================================================ 탐지
def _is_placeholder(value: str) -> bool:
    return (
        (len(value) >= 2 and value.startswith("<") and value.endswith(">"))
        or (len(value) >= 3 and value.startswith("${") and value.endswith("}"))
        or (value != "" and value.strip("*") == "")
    )


def looks_secret(value: str) -> bool:
    """자리표시자가 아니고, 8자 이상이며, 영문과 숫자가 섞인 값만 비밀값으로 본다."""
    if _is_placeholder(value):
        return False
    return len(value) >= 8 and bool(_HAS_LETTER.search(value)) and bool(_HAS_DIGIT.search(value))


def _skip(pattern: re.Pattern[str], text: str, at: int) -> int:
    match = pattern.match(text, at)
    return match.end() if match else at


def _find_secret_assignments(text: str) -> list[str]:
    """비밀값으로 보이는 `키: 값` 대입의 키워드(소문자)를 순서대로 돌려준다. 값은 싣지 않는다."""
    return [keyword for keyword, _start, _end in _secret_assignment_hits(text)]


def _secret_assignment_hits(text: str) -> list[tuple[str, int, int]]:
    """(키워드 소문자, 값 시작, 값 끝). 위치는 `text` 기준이다."""
    found: list[tuple[str, int, int]] = []
    consumed_until = 0
    for hit in _KEYWORD.finditer(text):
        start = hit.start()
        if start < consumed_until:  # 이미 값으로 소비한 구간 안의 키워드
            continue
        keyword = hit.group()
        # `mypassword`: 영문 키워드가 이름 중간이면 건너뛴다. 한글 키워드는 `DB비밀번호`처럼
        # 영문 약어에 붙여 쓰므로 앞 글자를 보지 않는다.
        if keyword.isascii() and start > 0 and text[start - 1] in _ASCII_ALNUM:
            continue
        # 키 이름 꼬리: `_x`·`-y` 단위만 허용한다(`access_token`은 되고 `tokenizer`는 안 된다)
        at = hit.end()
        tail = _KEY_TAIL.match(text, at)
        tail_text = tail.group() if tail else ""
        if len(tail_text) > MAX_KEY_TAIL or _KEY_TAIL_SHAPE.fullmatch(tail_text) is None:
            continue
        at = _skip(_SKIP_BEFORE_SEPARATOR, text, at + len(tail_text))
        label = _LABEL_WORD.match(text, at)  # `token 값: …`
        if label:
            at = _skip(_SKIP_BEFORE_SEPARATOR, text, label.end())
        qualifier = _QUALIFIER.match(text, at)  # `비밀번호(운영): …`, `password (prod): …`
        if qualifier:
            at = _skip(_SKIP_BEFORE_SEPARATOR, text, qualifier.end())
        separator = _SEPARATOR.match(text, at)
        if separator is None:
            particle = _PARTICLE_VALUE.match(text, at)
            if particle and all(c.search(particle.group(1)) for c in _MIXED_CLASSES):
                consumed_until = particle.end(1)
                found.append((keyword.lower(), particle.start(1), particle.end(1)))
            continue
        value = _VALUE.match(text, _skip(_SKIP_BEFORE_VALUE, text, separator.end()))
        if value is None:
            continue
        consumed_until = value.end()
        if looks_secret(value.group()):
            found.append((keyword.lower(), value.start(), value.end()))
    return found


def _is_url_placeholder(user: str, password: str) -> bool:
    """URL 비밀번호 자리의 문서용 예시(`$NAME`, `{…}`, `xxxx`, `password`, `guest:guest` 등)."""
    return (
        _is_placeholder(password)
        or _ENV_REFERENCE.fullmatch(password) is not None
        or (len(password) >= 2 and password[0] + password[-1] in {"{}", "[]", "%%"})
        or password.lower() in _PASSWORD_WORDS
        or set(password.lower()) == {"x"}
        or password == user
    )


def _has_url_credentials(text: str) -> bool:
    for hit in _URL_SLASHES.finditer(text):
        start = hit.start()
        before = text[start - 1] if start > 0 else ""
        if before == ":":  # `scheme://`
            if start < 2 or text[start - 2] not in _SCHEME_CHARS:
                continue
        elif before == "/" or before in _SCHEME_CHARS:  # `a//b`, `///`는 URL이 아니다
            continue
        userinfo = _URL_USERINFO.match(text, hit.end())
        if userinfo and not _is_url_placeholder(userinfo.group(1), userinfo.group(2)):
            return True
    return False


def secret_findings(text: str, *, normalize: bool = True) -> list[str]:
    """L1 위반 메시지 목록(값 없음). 호환 정규화(NFKC)한 글로 판정한다.

    NFKC는 전각 영문·`：`·NBSP·분해형 한글 같은 겉모양 우회를 같은 글자로 만든다. 선형이고,
    메시지가 원문 위치에 의존하지 않아서 길이가 바뀌어도 상관없다. 다만 한 글자가 최대 18자로
    늘어날 수 있어서(`\ufdfa`), 이미 위반인 초과 크기 페이지는 정규화하지 않고 원문을 훑는다.
    """
    if normalize:
        text = unicodedata.normalize("NFKC", text)
    found = [
        f"비밀값으로 보이는 문자열: {label}"
        for pattern, label in _TOKEN_PATTERNS
        if pattern.search(text)
    ]
    if _has_url_credentials(text):
        found.append("비밀값으로 보이는 문자열: URL 속 자격증명")
    found.extend(
        f"비밀값으로 보이는 대입: {keyword}=…" for keyword in _find_secret_assignments(text)
    )
    return found


# ================================================================ 답 내보내기용 토큰 가리기
TOKEN_OMITTED: Final = "<비밀값>"  # 토큰 모양·URL 비밀번호·개인 키 블록을 바꾸는 글
MAX_PRIVATE_KEY_SPAN: Final = 16_384  # 개인 키 블록: 머리 줄부터 이 길이 안에서 `-----END`를 찾는다
_PRIVATE_KEY_LABEL = "개인 키"
_PEM_END = "-----END"

# 답에서만 더 가리는 모양(경계 lookbehind로 시작 위치가 겹치지 않게 해서 선형을 지킨다)
_EXTRA_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    re.compile(r"AIza[0-9A-Za-z_-]{35}"),
    re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/_-]{20,}"),
)
_BEARER = re.compile(r"(?<![A-Za-z])[Bb]earer[ \t]+([A-Za-z0-9._~+/=-]{20,})")


def _private_key_spans(text: str) -> list[tuple[int, int]]:
    """개인 키 머리 줄부터 `-----END` 줄 끝까지(없으면 16,384자). 가린 구간 안은 건너뛴다."""
    spans: list[tuple[int, int]] = []
    consumed = 0
    for pattern, label in _TOKEN_PATTERNS:
        if label != _PRIVATE_KEY_LABEL:
            continue
        for head in pattern.finditer(text):
            if head.start() < consumed:
                continue
            window_end = min(head.start() + MAX_PRIVATE_KEY_SPAN, len(text))
            end_at = text.find(_PEM_END, head.end(), window_end)
            if end_at < 0:
                end = window_end
            else:
                newline = text.find("\n", end_at)
                end = len(text) if newline < 0 else newline
            spans.append((head.start(), end))
            consumed = end
    return spans


def _url_password_spans(text: str) -> list[tuple[int, int]]:
    """`_has_url_credentials`와 같은 규칙으로 찾은 URL 속 비밀번호 부분(자리표시자는 뺀다)."""
    spans: list[tuple[int, int]] = []
    for hit in _URL_SLASHES.finditer(text):
        start = hit.start()
        before = text[start - 1] if start > 0 else ""
        if before == ":":
            if start < 2 or text[start - 2] not in _SCHEME_CHARS:
                continue
        elif before == "/" or before in _SCHEME_CHARS:
            continue
        userinfo = _URL_USERINFO.match(text, hit.end())
        if userinfo and not _is_url_placeholder(userinfo.group(1), userinfo.group(2)):
            spans.append((userinfo.start(2), userinfo.end(2)))
    return spans


def mask_secret_tokens(text: str) -> str:
    """토큰 모양·URL 속 비밀번호·개인 키 블록을 `<비밀값>`으로 바꾼다. 입력 길이에 선형, 예외 없음.

    원문 위치를 지키려고 NFKC는 하지 않는다. 겹치는 구간은 하나로 합쳐 바꾼다.
    """
    spans = _private_key_spans(text) + _url_password_spans(text)
    spans.extend((start, end) for _kw, start, end in _secret_assignment_hits(text))
    spans.extend((m.start(1), m.end(1)) for m in _BEARER.finditer(text))
    for extra in _EXTRA_PATTERNS:
        spans.extend((m.start(), m.end()) for m in extra.finditer(text))
    for pattern, label in _TOKEN_PATTERNS:
        if label != _PRIVATE_KEY_LABEL:
            spans.extend((m.start(), m.end()) for m in pattern.finditer(text))
    if not spans:
        return text
    spans.sort()
    out: list[str] = []
    cursor = 0
    cur_start, cur_end = spans[0]
    for start, end in spans[1:]:
        if start <= cur_end:
            cur_end = max(cur_end, end)
            continue
        out.append(text[cursor:cur_start])
        out.append(TOKEN_OMITTED)
        cursor = cur_end
        cur_start, cur_end = start, end
    out.append(text[cursor:cur_start])
    out.append(TOKEN_OMITTED)
    out.append(text[cur_end:])
    return "".join(out)


# ================================================================ 마스킹
_UNSAFE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})
_OPAQUE_TOKEN = re.compile(r"[A-Za-z0-9_+=]{8,}")
_WORD = re.compile(r"[^\s'\"`]+")
_NAME_PARTS = re.compile(r"[-_.\s]+")
_WORD_EDGES = "!<>:;,.()[]{}"


def clean(text: str) -> str:
    """제어·서식·방향 지정 문자를 `\\n`, `\\x1b`, `\\u202e`처럼 눈에 보이게 바꾼다(줄 위조 방지)."""
    if text.isprintable():
        return text
    return "".join(
        c.encode("unicode_escape").decode("ascii")
        if unicodedata.category(c) in _UNSAFE_CATEGORIES
        else c
        for c in text
    )


def shorten(text: str, limit: int = MAX_ECHO_CHARS) -> str:
    return text if len(text) <= limit else f"{text[:limit]}…"


def looks_sensitive(text: str) -> bool:
    """L1이 지적할 모양이거나, 영숫자만 이어진 비밀번호풍 덩어리인가."""
    if secret_findings(text):
        return True
    return _OPAQUE_TOKEN.fullmatch(text) is not None and looks_secret(text)


def mask_words(text: str) -> str:
    """글 속에서 비밀값처럼 보이는 단어(공백·따옴표 단위)만 `<생략>`으로 바꾼다."""
    return _WORD.sub(
        lambda m: WORD_OMITTED if looks_sensitive(m.group().strip(_WORD_EDGES)) else m.group(),
        text,
    )


def safe_text(text: str, limit: int = MAX_ECHO_CHARS) -> str:
    """페이지·설정에서 온 설명 글(YAML 오류, baseline 오류 등)을 메시지용으로 바꾼다.

    비밀값 단어를 `<생략>`으로 가리고, 그래도 여러 단어에 걸친 지적이 남으면 통째로 가린다.
    마지막으로 길이를 자르고 제어 문자를 이스케이프한다.
    """
    masked = mask_words(text[:MAX_MASK_INPUT])
    if secret_findings(masked):  # 단어 하나로는 안 보이는 `키워드: 값` 같은 여러 단어짜리
        return OMITTED
    return clean(shorten(masked, limit))


def safe_value(text: str) -> str:
    """값 하나(링크 대상, frontmatter 값)를 메시지용으로 바꾼다. 비밀값처럼 보이면 싣지 않는다."""
    if looks_sensitive(text):
        return OMITTED
    return clean(shorten(text))


@lru_cache(maxsize=4096)
def is_sensitive_name(segment: str) -> bool:
    """경로 이름 하나가, 또는 `-_.`·공백으로 나눈 조각 하나가 비밀값처럼 보이는가."""
    return looks_sensitive(segment) or any(
        looks_sensitive(part) for part in _NAME_PARTS.split(segment) if part
    )


def is_sensitive_path(path: str) -> bool:
    """`/`로 나눈 경로 조각 중 하나라도 비밀값처럼 보이는가(링크 대상용)."""
    return looks_sensitive(path) or any(is_sensitive_name(seg) for seg in path.split("/") if seg)


def safe_path(path: str, *others: str) -> str:
    """링크 대상을 메시지용으로 바꾼다. `path`나 `others`(디코딩한 꼴 등)가 민감하면 가린다."""
    if is_sensitive_path(path) or any(is_sensitive_path(other) for other in others):
        return OMITTED
    return clean(shorten(path))


@lru_cache(maxsize=4096)
def display_name(segment: str) -> str:
    """경로의 이름 하나를 표시용으로 바꾼다. 비밀값처럼 보이면 `<이름 생략>`(`.md`는 유지)."""
    if is_sensitive_name(segment):
        return NAME_OMITTED + (".md" if segment.endswith(".md") else "")
    return clean(segment)

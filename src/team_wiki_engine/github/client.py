"""GitHub REST 포트와 `httpx2` 구현.

무거운 SDK 없이 필요한 엔드포인트 7개만 직접 부른다. 안전 규칙:

- 토큰은 `Authorization` 헤더로만 간다(URL·쿼리·로그·예외 메시지·`repr`에 없다).
- 응답은 스트리밍으로 읽어 `MAX_RESPONSE_BYTES`를 넘으면 중단한다. 오류 응답 본문은 읽지 않는다.
- 리다이렉트는 따라가지 않는다(토큰이 다른 호스트로 가지 않게).
- 모든 쿼리는 `params=`로 넘긴다. 경로에 들어가는 값은 `quote(…, safe="")`로 막는다.
- 응답이 어떤 모양이든 `GitHubError` 외의 예외를 던지지 않는다(`CancelledError`는 전파).
"""

import asyncio
import contextlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, NamedTuple, Protocol
from urllib.parse import quote

import httpx2

from team_wiki_engine.util.time import parse_instant

MAX_RESPONSE_BYTES = 8_388_608

_PER_PAGE = 100
_REQUEST_TIMEOUT_SECONDS = 30.0  # httpx2 연결·읽기·쓰기·풀 시간 제한
_TOTAL_TIMEOUT_SECONDS = 60.0  # 요청 하나의 전체 상한(응답이 천천히 흘러도 끊는다)
_STATUS_KEY = "team_wiki_engine.github.statuses"
_USER_AGENT = "team-wiki-engine"
_OWNER = re.compile(r"[A-Za-z0-9-]{1,39}")
_NAME = re.compile(r"[A-Za-z0-9._-]{1,100}")
_HTTPS_URL = re.compile(
    r"https://github\.com/(?P<owner>[A-Za-z0-9-]{1,39})/(?P<name>[A-Za-z0-9._-]{1,100}?)"
    r"(?:\.git)?/?"
)
_SSH_URL = re.compile(
    r"git@github\.com:(?P<owner>[A-Za-z0-9-]{1,39})/(?P<name>[A-Za-z0-9._-]{1,100}?)(?:\.git)?"
)


@dataclass(frozen=True)
class RepoRef:
    owner: str
    name: str


def _valid_name(name: str) -> bool:
    return name not in (".", "..") and not name.lower().endswith(".git")


def parse_repo_url(url: str) -> RepoRef | None:
    """GitHub 저장소 주소(https 또는 `git@github.com:`)를 읽는다. 그 밖의 주소는 `None`이다.

    앞뒤 공백(스페이스)만 지운다 — 줄바꿈 등 제어 문자가 섞인 값은 거부한다.
    """
    text = url.strip(" ")
    match = _HTTPS_URL.fullmatch(text) or _SSH_URL.fullmatch(text)
    if match is None:
        return None
    owner, name = match.group("owner"), match.group("name")
    if not _valid_name(name):
        return None
    return RepoRef(owner, name)


class GitHubError(Exception):
    """GitHub 호출 실패. 메시지에는 토큰·쿼리·응답 본문을 싣지 않는다."""

    def __init__(self, message: str, status: int, rate_limited: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.rate_limited = rate_limited


@dataclass(frozen=True)
class PullSummary:
    number: int
    title: str
    body: str
    author: str
    state: Literal["open", "closed"]
    draft: bool
    merged_at: str | None
    merge_commit_sha: str | None
    updated_at: str
    head_ref: str
    head_sha: str
    labels: tuple[str, ...]


@dataclass(frozen=True)
class PullDetail(PullSummary):
    merged: bool
    closed_at: str | None


class GitHubPort(Protocol):
    async def list_pulls(
        self,
        repo: RepoRef,
        *,
        state: Literal["open", "closed"],
        max_pages: int,
        stop_when_updated_before: datetime | None = None,
    ) -> list[PullSummary]: ...

    async def list_pull_files(self, repo: RepoRef, number: int, max_files: int) -> list[str]: ...

    async def list_commit_authors(
        self, repo: RepoRef, since: datetime, max_pages: int
    ) -> list[str]: ...

    async def get_pull(self, repo: RepoRef, number: int) -> PullDetail: ...

    async def create_pull(
        self, repo: RepoRef, *, title: str, head: str, base: str, body: str
    ) -> int: ...

    async def ensure_label(self, repo: RepoRef, name: str, color: str) -> None: ...

    async def add_labels(self, repo: RepoRef, number: int, labels: Sequence[str]) -> None: ...


# ------------------------------------------------------------------ 응답 변환


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _opt_str(value: object) -> bool:
    return value is None or isinstance(value, str)


def _pull_fields(raw: object) -> dict[str, Any] | None:
    """PR 원본 객체를 `PullSummary` 필드로 바꾼다. 모양이 다르면 `None`이다."""
    if not isinstance(raw, dict):
        return None
    number, title, body = raw.get("number"), raw.get("title"), raw.get("body")
    user, state, draft = raw.get("user"), raw.get("state"), raw.get("draft", False)
    head, labels = raw.get("head"), raw.get("labels", [])
    updated_at = raw.get("updated_at")
    if not _is_int(number) or not isinstance(title, str) or not _opt_str(body):
        return None
    if state not in ("open", "closed") or not isinstance(draft, bool):
        return None
    if not _opt_str(raw.get("merged_at")) or not _opt_str(raw.get("merge_commit_sha")):
        return None
    if not isinstance(updated_at, str):
        return None
    if user is None:
        author = ""
    elif isinstance(user, dict) and isinstance(user.get("login"), str):
        author = user["login"]
    else:
        return None
    if not isinstance(head, dict):
        return None
    head_ref, head_sha = head.get("ref"), head.get("sha")
    if not isinstance(head_ref, str) or not isinstance(head_sha, str):
        return None
    if labels is None:
        labels = []
    if not isinstance(labels, list):
        return None
    names: list[str] = []
    for label in labels:
        if not isinstance(label, dict) or not isinstance(label.get("name"), str):
            return None
        names.append(label["name"])
    return {
        "number": number,
        "title": title,
        "body": body if isinstance(body, str) else "",
        "author": author,
        "state": state,
        "draft": draft,
        "merged_at": raw.get("merged_at"),
        "merge_commit_sha": raw.get("merge_commit_sha"),
        "updated_at": updated_at,
        "head_ref": head_ref,
        "head_sha": head_sha,
        "labels": tuple(names),
    }


class _Reply(NamedTuple):
    data: object
    status: int
    where: str  # "<METHOD> <쿼리 없는 경로>"

    def format_error(self) -> GitHubError:
        return GitHubError(f"GitHub API {self.where} 실패: 응답 형식 오류", self.status)

    def as_list(self) -> list[object]:
        if not isinstance(self.data, list):
            raise self.format_error()
        return self.data

    def as_dict(self) -> dict[str, object]:
        if not isinstance(self.data, dict):
            raise self.format_error()
        return self.data


def _utc_z(moment: datetime) -> str:
    """`YYYY-MM-DDTHH:MM:SSZ`(UTC, 0 채움). naive는 UTC로 본다. 표현할 수 없으면 `GitHubError`."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    try:
        u = moment.astimezone(UTC)
    except (OverflowError, ValueError):
        raise GitHubError("GitHub API 요청 시각을 UTC로 바꿀 수 없습니다", 0) from None
    return f"{u.year:04d}-{u.month:02d}-{u.day:02d}T{u.hour:02d}:{u.minute:02d}:{u.second:02d}Z"


async def _note_status(response: httpx2.Response) -> None:
    """응답 상태를 요청에 적어 둔다 — 잘못된 `Location`이면 httpx2가 응답을 돌려주기 전에 던진다."""
    seen = response.request.extensions.get(_STATUS_KEY)
    if isinstance(seen, list):
        seen.append(response.status_code)


class GitHubRest:
    """`GitHubPort`의 `httpx2` 구현. `client`는 호출자가 만들고 닫는다."""

    def __init__(
        self,
        *,
        token: str | None,
        client: httpx2.AsyncClient,
        base_url: str = "https://api.github.com",
    ) -> None:
        if client.trust_env:
            # 환경 변수의 프록시·인증 설정이 요청에 끼어들지 않게 한다. 배선은
            # `httpx2.AsyncClient(trust_env=False, follow_redirects=False)`로 만든다.
            raise ValueError("httpx2 클라이언트는 trust_env=False로 만들어야 합니다")
        if token and not (token.isascii() and token.isprintable() and " " not in token):
            raise ValueError("GitHub 토큰 형식이 올바르지 않습니다")
        if _note_status not in client.event_hooks["response"]:
            client.event_hooks["response"].append(_note_status)
        self.__token = token
        self.__client = client
        self.__base_url = base_url.rstrip("/")

    def __repr__(self) -> str:
        return f"GitHubRest(base_url={self.__base_url!r}, authenticated={bool(self.__token)})"

    # -------------------------------------------------------------- 요청

    @staticmethod
    def _repo_path(repo: RepoRef) -> str:
        if (
            _OWNER.fullmatch(repo.owner) is None
            or _NAME.fullmatch(repo.name) is None
            or not _valid_name(repo.name)
        ):
            raise GitHubError("GitHub 저장소 이름이 올바르지 않습니다", 0)
        return f"/repos/{quote(repo.owner, safe='')}/{quote(repo.name, safe='')}"

    def __headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": _USER_AGENT,
            "Accept-Encoding": "identity",
        }
        if self.__token:
            headers["Authorization"] = f"Bearer {self.__token}"
        return headers

    async def __request(
        self,
        method: Literal["GET", "POST"],
        path: str,
        *,
        params: Mapping[str, str | int] | None = None,
        body: object = None,
    ) -> _Reply:
        where = f"{method} {path}"
        seen: list[int] = []
        headers = self.__headers()
        content: bytes | None = None
        if body is not None:
            # 외톨이 서로게이트도 `\\udXXX`로 이스케이프돼 인코딩 오류가 나지 않는다.
            content = json.dumps(body).encode("ascii")
            headers["Content-Type"] = "application/json"
        try:
            async with asyncio.timeout(_TOTAL_TIMEOUT_SECONDS):
                async with self.__client.stream(
                    method,
                    f"{self.__base_url}{path}",
                    params=params,
                    content=content,
                    headers=headers,
                    follow_redirects=False,
                    timeout=_REQUEST_TIMEOUT_SECONDS,
                    extensions={_STATUS_KEY: seen},
                ) as response:
                    status = response.status_code
                    if not 200 <= status < 300:
                        rate_limited = status == 429 or (
                            status == 403
                            and (
                                response.headers.get("x-ratelimit-remaining") == "0"
                                or "retry-after" in response.headers
                            )
                        )
                        suffix = " (호출 한도 초과)" if rate_limited else ""
                        raise GitHubError(
                            f"GitHub API {where} 실패: HTTP {status}{suffix}", status, rate_limited
                        )
                    if status == 204:
                        return _Reply(None, status, where)
                    raw = await self.__read_limited(response, where, status)
        except GitHubError:
            raise
        except TimeoutError:
            raise GitHubError(f"GitHub API {where} 실패: 시간 초과", 0) from None
        except (httpx2.HTTPError, httpx2.StreamError, httpx2.InvalidURL) as exc:
            if seen and 300 <= seen[-1] < 400:
                # 3xx인데 `Location`이 깨져 httpx2가 다음 요청을 못 만든 경우: 상태를 그대로 알린다.
                raise GitHubError(f"GitHub API {where} 실패: HTTP {seen[-1]}", seen[-1]) from None
            raise GitHubError(
                f"GitHub API {where} 실패: 연결 실패 ({type(exc).__name__})", 0
            ) from None
        try:
            data = json.loads(raw)
        except (ValueError, RecursionError):
            raise GitHubError(f"GitHub API {where} 실패: 응답 형식 오류", status) from None
        return _Reply(data, status, where)

    @staticmethod
    async def __read_limited(response: httpx2.Response, where: str, status: int) -> bytes:
        too_large = GitHubError(f"GitHub API {where} 실패: 응답이 너무 큽니다", status)
        declared = response.headers.get("content-length", "")
        if declared.isascii() and declared.isdigit() and int(declared[:20]) > MAX_RESPONSE_BYTES:
            raise too_large
        buffer = bytearray()
        async with contextlib.aclosing(response.aiter_bytes()) as chunks:
            async for chunk in chunks:
                buffer += chunk
                if len(buffer) > MAX_RESPONSE_BYTES:
                    raise too_large
        return bytes(buffer)

    # -------------------------------------------------------------- 포트

    async def list_pulls(
        self,
        repo: RepoRef,
        *,
        state: Literal["open", "closed"],
        max_pages: int,
        stop_when_updated_before: datetime | None = None,
    ) -> list[PullSummary]:
        path = f"{self._repo_path(repo)}/pulls"
        out: list[PullSummary] = []
        if stop_when_updated_before is not None and stop_when_updated_before.tzinfo is None:
            stop_when_updated_before = stop_when_updated_before.replace(tzinfo=UTC)
        for page in range(1, max_pages + 1):
            reply = await self.__request(
                "GET",
                path,
                params={
                    "state": state,
                    "sort": "updated",
                    "direction": "desc",
                    "per_page": _PER_PAGE,
                    "page": page,
                },
            )
            items = reply.as_list()
            for item in items:
                fields = _pull_fields(item)
                if fields is not None:
                    out.append(PullSummary(**fields))
            if len(items) < _PER_PAGE:
                break
            if stop_when_updated_before is not None:
                last = items[-1]
                updated = parse_instant(last.get("updated_at")) if isinstance(last, dict) else None
                if updated is not None and updated < stop_when_updated_before:
                    break
        return out

    async def list_pull_files(self, repo: RepoRef, number: int, max_files: int) -> list[str]:
        path = f"{self._repo_path(repo)}/pulls/{int(number)}/files"
        out: list[str] = []
        pages = -(-max_files // _PER_PAGE)
        for page in range(1, pages + 1):
            if len(out) >= max_files:
                break
            items = (
                await self.__request("GET", path, params={"per_page": _PER_PAGE, "page": page})
            ).as_list()
            for item in items:
                if isinstance(item, dict) and isinstance(item.get("filename"), str):
                    out.append(item["filename"])
            if len(items) < _PER_PAGE:
                break
        return out[:max_files]

    async def list_commit_authors(
        self, repo: RepoRef, since: datetime, max_pages: int
    ) -> list[str]:
        path = f"{self._repo_path(repo)}/commits"
        since_text = _utc_z(since)
        out: list[str] = []
        for page in range(1, max_pages + 1):
            items = (
                await self.__request(
                    "GET",
                    path,
                    params={"since": since_text, "per_page": _PER_PAGE, "page": page},
                )
            ).as_list()
            for item in items:
                author = item.get("author") if isinstance(item, dict) else None
                login = author.get("login") if isinstance(author, dict) else None
                if isinstance(login, str) and login != "":
                    out.append(login)
            if len(items) < _PER_PAGE:
                break
        return out

    async def get_pull(self, repo: RepoRef, number: int) -> PullDetail:
        reply = await self.__request("GET", f"{self._repo_path(repo)}/pulls/{int(number)}")
        raw = reply.as_dict()
        fields = _pull_fields(raw)
        merged, closed_at = raw.get("merged", False), raw.get("closed_at")
        if fields is None or not isinstance(merged, bool) or not _opt_str(closed_at):
            raise reply.format_error()
        return PullDetail(
            **fields, merged=merged, closed_at=closed_at if isinstance(closed_at, str) else None
        )

    async def create_pull(
        self, repo: RepoRef, *, title: str, head: str, base: str, body: str
    ) -> int:
        reply = await self.__request(
            "POST",
            f"{self._repo_path(repo)}/pulls",
            body={"title": title, "head": head, "base": base, "body": body},
        )
        number = reply.as_dict().get("number")
        if not isinstance(number, int) or isinstance(number, bool):
            raise reply.format_error()
        return number

    async def ensure_label(self, repo: RepoRef, name: str, color: str) -> None:
        if name in ("", ".", ".."):
            raise GitHubError("GitHub 라벨 이름이 올바르지 않습니다", 0)
        base = self._repo_path(repo)
        try:
            await self.__request("GET", f"{base}/labels/{quote(name, safe='')}")
        except GitHubError as exc:
            if exc.status != 404:
                raise
            await self.__request("POST", f"{base}/labels", body={"name": name, "color": color})

    async def add_labels(self, repo: RepoRef, number: int, labels: Sequence[str]) -> None:
        await self.__request(
            "POST",
            f"{self._repo_path(repo)}/issues/{int(number)}/labels",
            body={"labels": list(labels)},
        )

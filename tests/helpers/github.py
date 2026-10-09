"""테스트용 GitHub 포트 가짜."""

import dataclasses
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Literal

from team_wiki_engine.github.client import GitHubError, PullDetail, PullSummary, RepoRef
from team_wiki_engine.util.time import parse_instant

PER_PAGE = 100


def pull_summary(n: int, **overrides: Any) -> PullSummary:
    """기본값이 채워진 `PullSummary`. 닫히고 머지된 PR이다."""
    fields: dict[str, Any] = {
        "number": n,
        "title": f"PR {n}",
        "body": "",
        "author": "alice",
        "state": "closed",
        "draft": False,
        "merged_at": "2026-10-06T00:00:00Z",
        "merge_commit_sha": None,
        "updated_at": "2026-10-06T00:00:00Z",
        "head_ref": f"feat-{n}",
        "head_sha": "f" * 40,
        "labels": (),
    }
    fields.update(overrides)
    return PullSummary(**fields)


def pull_detail(n: int, **overrides: Any) -> PullDetail:
    """`pull_summary` 기본값에 `merged`·`closed_at`을 더한 `PullDetail`."""
    merged = overrides.pop("merged", True)
    closed_at = overrides.pop("closed_at", "2026-10-07T00:00:00Z")
    summary = pull_summary(n, **overrides)
    return PullDetail(**dataclasses.asdict(summary), merged=merged, closed_at=closed_at)


class FakeGitHub:
    """`GitHubPort`를 메모리로 구현한다. `fail`이 있으면 모든 호출이 그 예외를 던진다."""

    def __init__(
        self,
        *,
        closed: Sequence[PullSummary] = (),
        open: Sequence[PullSummary] = (),  # noqa: A002 - 포트의 상태 이름과 맞춘다
        files: Mapping[int, list[str]] | None = None,
        authors: Sequence[str] = (),
        details: Mapping[int, PullDetail] | None = None,
        fail: Exception | None = None,
    ) -> None:
        self.closed = list(closed)
        self.open = list(open)
        self.files = dict(files or {})
        self.authors = list(authors)
        self.details = dict(details or {})
        self.fail = fail
        self.created: list[dict[str, str]] = []
        self.ensured: list[str] = []
        self.ensured_colors: dict[str, str] = {}
        self.added: list[tuple[int, tuple[str, ...]]] = []
        self.calls: list[tuple[Any, ...]] = []
        self._next_number = 100

    def _enter(self, *call: Any) -> None:
        self.calls.append(call)
        if self.fail is not None:
            raise self.fail

    async def list_pulls(
        self,
        repo: RepoRef,
        *,
        state: Literal["open", "closed"],
        max_pages: int,
        stop_when_updated_before: datetime | None = None,
    ) -> list[PullSummary]:
        self._enter("list_pulls", repo, state, max_pages, stop_when_updated_before)
        stop = stop_when_updated_before
        if stop is not None and stop.tzinfo is None:
            stop = stop.replace(tzinfo=UTC)
        items = self.closed if state == "closed" else self.open
        out: list[PullSummary] = []
        # 실제 구현과 같은 쪽 나누기: 100개씩, 꽉 찬 쪽의 마지막 항목이 기준보다 이르면 멈춘다.
        for page in range(max_pages):
            chunk = items[page * PER_PAGE : (page + 1) * PER_PAGE]
            out.extend(chunk)
            if len(chunk) < PER_PAGE:
                break
            updated = parse_instant(chunk[-1].updated_at)
            if stop is not None and updated is not None and updated < stop:
                break
        return out

    async def list_pull_files(self, repo: RepoRef, number: int, max_files: int) -> list[str]:
        self._enter("list_pull_files", repo, number, max_files)
        return list(self.files.get(number, []))[:max_files]

    async def list_commit_authors(
        self, repo: RepoRef, since: datetime, max_pages: int
    ) -> list[str]:
        self._enter("list_commit_authors", repo, since, max_pages)
        return list(self.authors[: max(max_pages, 0) * PER_PAGE])

    async def get_pull(self, repo: RepoRef, number: int) -> PullDetail:
        self._enter("get_pull", repo, number)
        detail = self.details.get(number)
        if detail is None:
            raise GitHubError(f"GitHub API GET /pulls/{number} 실패: HTTP 404", 404)
        return detail

    async def create_pull(
        self, repo: RepoRef, *, title: str, head: str, base: str, body: str
    ) -> int:
        self._enter("create_pull", repo, title, head, base, body)
        self.created.append({"title": title, "head": head, "base": base, "body": body})
        self._next_number += 1
        return self._next_number

    async def ensure_label(self, repo: RepoRef, name: str, color: str) -> None:
        self._enter("ensure_label", repo, name, color)
        self.ensured.append(name)
        self.ensured_colors[name] = color

    async def add_labels(self, repo: RepoRef, number: int, labels: Sequence[str]) -> None:
        self._enter("add_labels", repo, number, tuple(labels))
        self.added.append((number, tuple(labels)))

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import httpx2
import pytest

from team_wiki_engine.github import client as github_client
from team_wiki_engine.github.client import (
    MAX_RESPONSE_BYTES,
    GitHubError,
    GitHubPort,
    GitHubRest,
    PullDetail,
    PullSummary,
    RepoRef,
    parse_repo_url,
)
from tests.helpers.github import FakeGitHub, pull_detail, pull_summary
from tests.helpers.hang_guard import hang_guard
from tests.helpers.http import idle_client, mock_client

REPO = RepoRef("org", "worker")
API = "https://api.github.com"
TOKEN = "ghp_super_secret_token_0123456789"


def raw_pull(n: int, **extra: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "number": n,
        "title": f"PR {n}",
        "body": None,
        "user": {"login": "alice"},
        "state": "closed",
        "draft": False,
        "merged_at": "2026-10-06T00:00:00Z",
        "merge_commit_sha": f"{n}".rjust(40, "a"),
        "updated_at": "2026-10-06T00:00:00Z",
        "head": {"ref": f"feat-{n}", "sha": "f" * 40},
        "labels": [{"name": "x"}],
    }
    raw.update(extra)
    return raw


def reply(body: object, status: int = 200, **headers: str) -> httpx2.Response:
    return httpx2.Response(status, json=body, headers=headers)


@asynccontextmanager
async def rest(
    handler: Callable[[httpx2.Request], httpx2.Response],
    *,
    token: str | None = None,
) -> AsyncIterator[tuple[GitHubRest, list[httpx2.Request]]]:
    calls: list[httpx2.Request] = []

    def wrapped(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        return handler(request)

    async with mock_client(wrapped) as client:
        yield GitHubRest(token=token, client=client), calls


class TestParseRepoUrl:
    def test_reads_https_and_ssh_urls(self) -> None:
        assert parse_repo_url("https://github.com/org/worker.git") == REPO
        assert parse_repo_url("https://github.com/org/worker") == REPO
        assert parse_repo_url("https://github.com/org/worker/") == REPO
        assert parse_repo_url("https://github.com/org/worker.git/") == REPO
        assert parse_repo_url("git@github.com:org/worker.git") == REPO
        assert parse_repo_url("git@github.com:org/worker") == REPO
        assert parse_repo_url("  https://github.com/org/worker  ") == REPO
        assert parse_repo_url("https://github.com/my-org/my.repo_x-1") == RepoRef(
            "my-org", "my.repo_x-1"
        )

    @pytest.mark.parametrize(
        "url",
        [
            "/tmp/local.git",
            "https://github.com.evil.com/o/n",
            "https://u:p@github.com/o/n",
            "https://github.com/o/../n",
            "https://github.com/o/n/extra",
            "https://github.com/" + "o" * 40 + "/n",
            "https://github.com/o/n\n",
            "https://github.com/o/n\r\n",
            "https://github.com/o/..",
            "https://github.com/o/.",
            "https://github.com/o/" + "n" * 101,
            "https://github.com/o/n?x=1",
            "https://github.com/o/n#frag",
            "https://github.com/o /n",
            "http://github.com/o/n",
            "git@github.com.evil.com:o/n",
            "git@github.com:o/n/",
            "git@github.com:o/n\n",
            "ssh://git@github.com/o/n",
            "git@github.com:o/../n",
            "https://github.com/./n",
            "https://github.com/ö/n",
            "https://github.com/o/n.git.git",
            "",
        ],
    )
    def test_hostile_urls_are_rejected(self, url: str) -> None:
        assert parse_repo_url(url) is None


class TestHeadersAndAuth:
    async def test_authorization_only_with_token(self) -> None:
        async with rest(lambda r: reply([]), token="tok") as (gh, calls):
            await gh.list_pulls(REPO, state="open", max_pages=1)
        request = calls[0]
        assert request.headers["authorization"] == "Bearer tok"
        assert request.headers["accept"] == "application/vnd.github+json"
        assert request.headers["x-github-api-version"] == "2022-11-28"
        assert request.headers["user-agent"] == "team-wiki-engine"
        async with rest(lambda r: reply([]), token=None) as (gh2, calls2):
            await gh2.list_pulls(REPO, state="open", max_pages=1)
        assert "authorization" not in calls2[0].headers
        async with rest(lambda r: reply([]), token="") as (gh3, calls3):
            await gh3.list_pulls(REPO, state="open", max_pages=1)
        assert "authorization" not in calls3[0].headers

    def test_repr_has_no_token(self) -> None:
        client = idle_client()
        gh = GitHubRest(token=TOKEN, client=client)
        assert TOKEN not in repr(gh) + str(gh)


class TestListPulls:
    async def test_pages_and_conversion(self) -> None:
        def handler(request: httpx2.Request) -> httpx2.Response:
            if request.url.params["page"] == "1":
                return reply([raw_pull(i + 1) for i in range(100)])
            return reply([raw_pull(101)])

        async with rest(handler) as (gh, calls):
            pulls = await gh.list_pulls(REPO, state="closed", max_pages=5)
        assert len(pulls) == 101
        assert [str(c.url) for c in calls] == [
            f"{API}/repos/org/worker/pulls?state=closed&sort=updated&direction=desc&per_page=100&page=1",
            f"{API}/repos/org/worker/pulls?state=closed&sort=updated&direction=desc&per_page=100&page=2",
        ]
        assert pulls[0] == PullSummary(
            number=1,
            title="PR 1",
            body="",
            author="alice",
            state="closed",
            draft=False,
            merged_at="2026-10-06T00:00:00Z",
            merge_commit_sha="1".rjust(40, "a"),
            updated_at="2026-10-06T00:00:00Z",
            head_ref="feat-1",
            head_sha="f" * 40,
            labels=("x",),
        )

    async def test_max_pages_limits_requests(self) -> None:
        async with rest(lambda r: reply([raw_pull(i + 1) for i in range(100)])) as (gh, calls):
            pulls = await gh.list_pulls(REPO, state="open", max_pages=2)
        assert len(calls) == 2
        assert len(pulls) == 200

    async def test_stops_at_page_older_than_cutoff(self) -> None:
        old = "2026-09-01T00:00:00Z"
        async with rest(lambda r: reply([raw_pull(i + 1, updated_at=old) for i in range(100)])) as (
            gh,
            calls,
        ):
            await gh.list_pulls(
                REPO,
                state="closed",
                max_pages=5,
                stop_when_updated_before=datetime(2026, 10, 1, tzinfo=UTC),
            )
        assert len(calls) == 1

    async def test_cutoff_compares_instants_across_offsets(self) -> None:
        # 09-30T20:00Z == 10-01T05:00+09:00: 기준(10-01T00:00Z)보다 이르다.
        stamp = "2026-10-01T05:00:00+09:00"
        async with rest(
            lambda r: reply([raw_pull(i + 1, updated_at=stamp) for i in range(100)])
        ) as (gh, calls):
            await gh.list_pulls(
                REPO,
                state="closed",
                max_pages=5,
                stop_when_updated_before=datetime(2026, 10, 1, tzinfo=UTC),
            )
        assert len(calls) == 1

    async def test_does_not_stop_when_last_item_is_not_older(self) -> None:
        async with rest(lambda r: reply([raw_pull(i + 1) for i in range(100)])) as (gh, calls):
            await gh.list_pulls(
                REPO,
                state="closed",
                max_pages=3,
                stop_when_updated_before=datetime(2026, 10, 1, tzinfo=UTC),
            )
        assert len(calls) == 3

    async def test_unreadable_updated_at_does_not_stop(self) -> None:
        async with rest(
            lambda r: reply([raw_pull(i + 1, updated_at="garbage") for i in range(100)])
        ) as (gh, calls):
            await gh.list_pulls(
                REPO,
                state="closed",
                max_pages=3,
                stop_when_updated_before=datetime(2026, 10, 1, tzinfo=UTC),
            )
        # 항목은 모두 올바르다(문자열). 읽을 수 없는 시각은 멈추지 않는다.
        assert len(calls) == 3

    async def test_invalid_items_are_dropped_individually(self) -> None:
        items: list[Any] = [
            raw_pull(1),
            raw_pull(2, number="2"),
            {k: v for k, v in raw_pull(3).items() if k != "head"},
            raw_pull(4, labels=["x"]),
            raw_pull(5, state="merged"),
            raw_pull(6, number=True),
            "garbage",
            None,
            7,
            raw_pull(8, head={"ref": 1, "sha": "a"}),
            raw_pull(9, title=None),
            raw_pull(10, user="alice"),
            raw_pull(11),
        ]
        async with rest(lambda r: reply(items)) as (gh, _):
            pulls = await gh.list_pulls(REPO, state="closed", max_pages=1)
        assert [p.number for p in pulls] == [1, 11]

    async def test_null_user_and_optional_fields(self) -> None:
        raw = raw_pull(1, user=None, body="본문", draft=True)
        del raw["labels"]
        async with rest(lambda r: reply([raw])) as (gh, _):
            (pull,) = await gh.list_pulls(REPO, state="open", max_pages=1)
        assert pull.author == ""
        assert pull.body == "본문"
        assert pull.draft is True
        assert pull.labels == ()

    async def test_object_instead_of_list_is_a_format_error(self) -> None:
        async with rest(lambda r: reply({"message": "hi"})) as (gh, _):
            with pytest.raises(GitHubError, match="응답 형식 오류") as info:
                await gh.list_pulls(REPO, state="open", max_pages=1)
        assert info.value.status == 200
        assert info.value.rate_limited is False


class TestFilesAndAuthors:
    async def test_files_and_authors(self) -> None:
        def handler(request: httpx2.Request) -> httpx2.Response:
            if request.url.path.endswith("/pulls/7/files"):
                return reply([{"filename": "a.kt"}, {"filename": "b.kt"}, {"filename": "c.kt"}])
            return reply(
                [{"author": {"login": "alice"}}, {"author": None}, {"author": {"login": "bob"}}]
            )

        async with rest(handler) as (gh, calls):
            assert await gh.list_pull_files(REPO, 7, 2) == ["a.kt", "b.kt"]
            since = datetime(2026, 7, 1, tzinfo=UTC)
            assert await gh.list_commit_authors(REPO, since, 1) == ["alice", "bob"]
        assert calls[0].url.params["per_page"] == "100"

    async def test_files_pagination_and_cap(self) -> None:
        def handler(request: httpx2.Request) -> httpx2.Response:
            page = int(request.url.params["page"])
            return reply([{"filename": f"f{page}-{i}"} for i in range(100)])

        async with rest(handler) as (gh, calls):
            files = await gh.list_pull_files(REPO, 7, 250)
        assert len(files) == 250
        assert len(calls) == 3

    async def test_files_zero_max_makes_no_request(self) -> None:
        async with rest(lambda r: reply([])) as (gh, calls):
            assert await gh.list_pull_files(REPO, 7, 0) == []
        assert calls == []

    async def test_hostile_file_and_author_items_are_skipped(self) -> None:
        def handler(request: httpx2.Request) -> httpx2.Response:
            if "files" in request.url.path:
                return reply([{"filename": 1}, "x", None, {"filename": "ok.kt"}, {}])
            return reply(
                [
                    {"author": "str"},
                    {"author": {"login": 5}},
                    {"author": {"login": ""}},
                    "x",
                    None,
                    {"author": {"login": "bob"}},
                ]
            )

        async with rest(handler) as (gh, _):
            assert await gh.list_pull_files(REPO, 7, 10) == ["ok.kt"]
            since = datetime(2026, 7, 1, tzinfo=UTC)
            assert await gh.list_commit_authors(REPO, since, 1) == ["bob"]

    async def test_since_is_sent_as_utc_z_in_params(self) -> None:
        kst = timezone(timedelta(hours=9))
        async with rest(lambda r: reply([])) as (gh, calls):
            await gh.list_commit_authors(REPO, datetime(2026, 7, 9, 0, 0, tzinfo=kst), 1)
        url = str(calls[0].url)
        assert "since=2026-07-08T15%3A00%3A00Z" in url
        assert "+" not in url.split("?", 1)[1]
        assert calls[0].url.params["since"] == "2026-07-08T15:00:00Z"
        assert url.split("?", 1)[1] == "since=2026-07-08T15%3A00%3A00Z&per_page=100&page=1"

    async def test_naive_since_is_treated_as_utc(self) -> None:
        async with rest(lambda r: reply([])) as (gh, calls):
            await gh.list_commit_authors(REPO, datetime(2026, 7, 8, 15, 0), 1)
        assert calls[0].url.params["since"] == "2026-07-08T15:00:00Z"


class TestDetailCreateLabels:
    async def test_get_create_and_labels(self) -> None:
        def handler(request: httpx2.Request) -> httpx2.Response:
            path = request.url.raw_path.decode()
            if request.method == "GET" and path.endswith("/pulls/9"):
                return reply(raw_pull(9, merged=True, closed_at="2026-10-07T00:00:00Z"))
            if request.method == "POST" and path.endswith("/pulls"):
                return reply({"number": 12}, 201)
            if request.method == "GET" and "/labels/" in path:
                return reply({"message": "Not Found"}, 404)
            if request.method == "POST" and path.endswith("/labels"):
                return reply({}, 201)
            if path.endswith("/issues/12/labels"):
                return reply([])
            return reply({}, 500)

        async with rest(handler, token="t") as (gh, calls):
            detail = await gh.get_pull(REPO, 9)
            assert isinstance(detail, PullDetail)
            assert (detail.number, detail.merged, detail.closed_at) == (
                9,
                True,
                "2026-10-07T00:00:00Z",
            )
            assert await gh.create_pull(REPO, title="T", head="sync/x", base="main", body="B") == 12
            await gh.ensure_label(REPO, "wiki-sync:code", "0e8a16")
            await gh.add_labels(REPO, 12, ["wiki-sync:code"])
        posted = [(str(c.url), json.loads(c.content)) for c in calls if c.method == "POST"]
        assert posted == [
            (
                f"{API}/repos/org/worker/pulls",
                {"title": "T", "head": "sync/x", "base": "main", "body": "B"},
            ),
            (f"{API}/repos/org/worker/labels", {"name": "wiki-sync:code", "color": "0e8a16"}),
            (f"{API}/repos/org/worker/issues/12/labels", {"labels": ["wiki-sync:code"]}),
        ]
        assert any(c.url.raw_path.decode().endswith("/labels/wiki-sync%3Acode") for c in calls)
        assert all(c.headers["content-type"] == "application/json" for c in calls if c.content)

    async def test_get_pull_defaults_for_list_shape(self) -> None:
        async with rest(lambda r: reply(raw_pull(3))) as (gh, _):
            detail = await gh.get_pull(REPO, 3)
        assert detail.merged is False
        assert detail.closed_at is None

    async def test_get_pull_invalid_shape(self) -> None:
        bodies: list[object] = [[], "x", None, raw_pull(3, number="3"), raw_pull(3, merged="yes")]
        for body in bodies:
            async with rest(lambda r: reply(body)) as (gh, _):  # noqa: B023
                with pytest.raises(GitHubError, match="응답 형식 오류"):
                    await gh.get_pull(REPO, 3)

    async def test_create_pull_invalid_response(self) -> None:
        bodies: list[object] = [{}, {"number": "12"}, {"number": True}, [], "x"]
        for body in bodies:
            async with rest(lambda r: reply(body, 201)) as (gh, _):  # noqa: B023
                with pytest.raises(GitHubError, match="응답 형식 오류"):
                    await gh.create_pull(REPO, title="T", head="h", base="b", body="B")

    async def test_existing_label_is_not_created(self) -> None:
        async with rest(lambda r: reply({"name": "x"}), token="t") as (gh, calls):
            await gh.ensure_label(REPO, "x", "000000")
        assert [c.method for c in calls] == ["GET"]

    async def test_ensure_label_other_errors_propagate(self) -> None:
        async with rest(lambda r: reply({}, 500)) as (gh, calls):
            with pytest.raises(GitHubError) as info:
                await gh.ensure_label(REPO, "x", "000000")
        assert info.value.status == 500
        assert len(calls) == 1

    async def test_204_is_accepted_as_empty(self) -> None:
        async with rest(lambda r: httpx2.Response(204)) as (gh, _):
            await gh.add_labels(REPO, 3, ["a"])
            await gh.ensure_label(REPO, "a", "000000")

    @pytest.mark.parametrize("name", ["", ".", ".."])
    async def test_unsafe_label_names_are_rejected(self, name: str) -> None:
        async with rest(lambda r: reply({})) as (gh, calls):
            with pytest.raises(GitHubError):
                await gh.ensure_label(REPO, name, "000000")
        assert calls == []

    async def test_label_name_is_fully_percent_encoded(self) -> None:
        async with rest(lambda r: reply({"name": "x"})) as (gh, calls):
            await gh.ensure_label(REPO, "a/b c?#é", "000000")
        assert calls[0].url.raw_path.decode().endswith("/labels/a%2Fb%20c%3F%23%C3%A9")

    async def test_lone_surrogate_in_body_is_not_an_unexpected_error(self) -> None:
        async with rest(lambda r: reply({"number": 5}, 201)) as (gh, calls):
            assert await gh.create_pull(REPO, title="a\ud800", head="h", base="b", body="") == 5
        json.loads(calls[0].content)

    async def test_bad_repo_refs_are_rejected_without_a_request(self) -> None:
        async with rest(lambda r: reply([])) as (gh, calls):
            for owner, name in [
                ("o/../x", "n"),
                ("o", "n/x"),
                ("o", ".."),
                ("", "n"),
                ("o", "n\n"),
            ]:
                with pytest.raises(GitHubError):
                    await gh.list_pulls(RepoRef(owner, name), state="open", max_pages=1)
        assert calls == []


class TestErrors:
    async def test_rate_limit_is_distinguished_and_token_is_not_in_messages(self) -> None:
        async with rest(
            lambda r: reply({}, 403, **{"x-ratelimit-remaining": "0"}), token=TOKEN
        ) as (gh, _):
            with pytest.raises(GitHubError) as info:
                await gh.list_pulls(REPO, state="open", max_pages=1)
        assert (info.value.status, info.value.rate_limited) == (403, True)
        assert TOKEN not in str(info.value)
        assert "호출 한도 초과" in str(info.value)
        async with rest(lambda r: reply({}, 429)) as (gh2, _):
            with pytest.raises(GitHubError) as info2:
                await gh2.list_pulls(REPO, state="open", max_pages=1)
        assert info2.value.rate_limited is True
        async with rest(lambda r: reply({}, 500)) as (gh3, _):
            with pytest.raises(GitHubError) as info3:
                await gh3.list_pulls(REPO, state="open", max_pages=1)
        assert (info3.value.status, info3.value.rate_limited) == (500, False)

    async def test_403_with_retry_after_is_rate_limited(self) -> None:
        async with rest(lambda r: reply({}, 403, **{"retry-after": "60"})) as (gh, _):
            with pytest.raises(GitHubError) as info:
                await gh.list_pulls(REPO, state="open", max_pages=1)
        assert info.value.rate_limited is True

    async def test_plain_403_is_not_rate_limited(self) -> None:
        async with rest(lambda r: reply({}, 403, **{"x-ratelimit-remaining": "42"})) as (gh, _):
            with pytest.raises(GitHubError) as info:
                await gh.list_pulls(REPO, state="open", max_pages=1)
        assert (info.value.status, info.value.rate_limited) == (403, False)

    async def test_404_message_format(self) -> None:
        async with rest(lambda r: reply({"message": "secret body text"}, 404)) as (gh, _):
            with pytest.raises(GitHubError) as info:
                await gh.get_pull(REPO, 5)
        assert str(info.value) == "GitHub API GET /repos/org/worker/pulls/5 실패: HTTP 404"
        assert "secret body text" not in str(info.value)

    async def test_messages_contain_no_token_or_query(self) -> None:
        cases: list[Callable[[httpx2.Request], httpx2.Response]] = [
            lambda r: reply({}, 500),
            lambda r: reply({}, 429),
            lambda r: reply({"leak": TOKEN}, 403, **{"retry-after": "1"}),
            lambda r: httpx2.Response(302, headers={"location": f"https://evil.test/?t={TOKEN}"}),
            lambda r: reply({"x": 1}),
            lambda r: httpx2.Response(200, content=b"not json " + TOKEN.encode()),
        ]
        since = datetime(2026, 7, 1, tzinfo=UTC)
        for handler in cases:
            async with rest(handler, token=TOKEN) as (gh, _):
                for call in (
                    gh.list_pulls(REPO, state="open", max_pages=1),
                    gh.list_commit_authors(REPO, since, 1),
                    gh.list_pull_files(REPO, 1, 10),
                ):
                    with pytest.raises(GitHubError) as info:
                        await call
                    message = str(info.value)
                    assert TOKEN not in message
                    assert "?" not in message
                    assert "since=" not in message and "per_page" not in message
                    assert repr(info.value).count(TOKEN) == 0

    async def test_connection_error(self) -> None:
        def boom(request: httpx2.Request) -> httpx2.Response:
            raise httpx2.ConnectError(f"cannot connect with {TOKEN}")

        async with rest(boom, token=TOKEN) as (gh, _):
            with pytest.raises(GitHubError) as info:
                await gh.list_pulls(REPO, state="open", max_pages=1)
        assert info.value.status == 0
        assert info.value.rate_limited is False
        assert "연결 실패 (ConnectError)" in str(info.value)
        assert TOKEN not in str(info.value)
        assert info.value.__cause__ is None
        assert info.value.__suppress_context__ is True

    async def test_timeout_error(self) -> None:
        def slow(request: httpx2.Request) -> httpx2.Response:
            raise httpx2.ReadTimeout("slow")

        async with rest(slow) as (gh, _):
            with pytest.raises(GitHubError) as info:
                await gh.list_pulls(REPO, state="open", max_pages=1)
        assert info.value.status == 0
        assert "ReadTimeout" in str(info.value)

    async def test_per_request_timeout_and_no_redirect_settings(self) -> None:
        async with rest(lambda r: reply([])) as (gh, calls):
            await gh.list_pulls(REPO, state="open", max_pages=1)
        timeout = calls[0].extensions["timeout"]
        assert timeout["read"] is not None and timeout["read"] <= 60

    async def test_redirect_is_an_error_and_not_followed(self) -> None:
        def handler(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(302, headers={"location": "https://evil.test/steal"})

        async with rest(handler, token=TOKEN) as (gh, calls):
            with pytest.raises(GitHubError) as info:
                await gh.list_pulls(REPO, state="open", max_pages=1)
        assert info.value.status == 302
        assert "HTTP 302" in str(info.value)
        assert len(calls) == 1

    async def test_redirect_not_followed_even_if_client_follows_by_default(self) -> None:
        calls: list[httpx2.Request] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            calls.append(request)
            return httpx2.Response(301, headers={"location": "https://evil.test/steal"})

        async with mock_client(handler, follow_redirects=True) as client:
            gh = GitHubRest(token=TOKEN, client=client)
            with pytest.raises(GitHubError) as info:
                await gh.list_pulls(REPO, state="open", max_pages=1)
        assert info.value.status == 301
        assert len(calls) == 1

    async def test_cancellation_propagates(self) -> None:
        started = asyncio.Event()

        async def handler(request: httpx2.Request) -> httpx2.Response:
            started.set()
            await asyncio.Event().wait()
            return httpx2.Response(200, json=[])

        async with mock_client(handler) as client:
            gh = GitHubRest(token=None, client=client)
            task = asyncio.create_task(gh.list_pulls(REPO, state="open", max_pages=1))
            await asyncio.wait_for(started.wait(), 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)


class _Endless(httpx2.AsyncByteStream):
    def __init__(self) -> None:
        self.chunks_read = 0
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        while True:
            self.chunks_read += 1
            yield b"x" * 65536

    async def aclose(self) -> None:
        self.closed = True


class TestResponseSize:
    def test_limit_value(self) -> None:
        assert MAX_RESPONSE_BYTES == 8_388_608

    async def test_nine_mb_response_is_too_large(self) -> None:
        body = b"[" + b" " * (9 * 1024 * 1024) + b"]"
        async with rest(lambda r: httpx2.Response(200, content=body)) as (gh, _):
            with pytest.raises(GitHubError, match="너무 큽니다") as info:
                await gh.list_pulls(REPO, state="open", max_pages=1)
        assert info.value.status == 200

    async def test_exactly_the_limit_is_accepted(self) -> None:
        body = b"[" + b" " * (MAX_RESPONSE_BYTES - 2) + b"]"
        assert len(body) == MAX_RESPONSE_BYTES
        async with rest(lambda r: httpx2.Response(200, content=body)) as (gh, _):
            assert await gh.list_pulls(REPO, state="open", max_pages=1) == []

    async def test_endless_stream_is_cut_off_while_reading(self) -> None:
        stream = _Endless()
        async with rest(lambda r: httpx2.Response(200, stream=stream)) as (gh, _):
            with pytest.raises(GitHubError, match="너무 큽니다"):
                await asyncio.wait_for(gh.list_pulls(REPO, state="open", max_pages=1), 10)
        assert stream.chunks_read <= MAX_RESPONSE_BYTES // 65536 + 2
        assert stream.closed

    async def test_declared_content_length_over_limit_is_rejected_without_reading(self) -> None:
        stream = _Endless()
        headers = {"content-length": str(MAX_RESPONSE_BYTES + 1)}
        async with rest(lambda r: httpx2.Response(200, headers=headers, stream=stream)) as (gh, _):
            with pytest.raises(GitHubError, match="너무 큽니다"):
                await gh.list_pulls(REPO, state="open", max_pages=1)
        assert stream.chunks_read == 0

    async def test_error_status_body_is_never_read(self) -> None:
        stream = _Endless()
        async with rest(lambda r: httpx2.Response(500, stream=stream)) as (gh, _):
            with pytest.raises(GitHubError, match="HTTP 500"):
                await gh.list_pulls(REPO, state="open", max_pages=1)
        assert stream.chunks_read == 0


HOSTILE_BODIES: list[bytes] = [
    b"",
    b"null",
    b"true",
    b"1",
    b'"str"',
    b"[]",
    b"{}",
    b"[null]",
    b"[[]]",
    b'[{"number":{"a":1}}]',
    b"\xff\xfe\x00garbage",
    b"{",
    b"[" * 200_000,
    b"[" * 200_000 + b"]" * 200_000,
    b'{"a":' * 100_000,
    b"[NaN, Infinity]",
    b'[{"number": 1e999999}]',
    b'[{"number": ' + b"9" * 6000 + b"}]",
    b'{"number": ' + b"9" * 6000 + b"}",
    b"\xef\xbb\xbf[]",
    b'[{"labels": 5, "head": [], "user": []}]',
    b'{"merged": "x", "labels": {}}',
]


class TestHostileJson:
    @pytest.mark.parametrize(
        "body", HOSTILE_BODIES, ids=[f"hostile-{i}" for i in range(len(HOSTILE_BODIES))]
    )
    async def test_only_github_error_escapes(self, body: bytes) -> None:
        since = datetime(2026, 7, 1, tzinfo=UTC)
        async with rest(lambda r: httpx2.Response(200, content=body), token=TOKEN) as (gh, _):
            calls: list[Callable[[], Any]] = [
                lambda: gh.list_pulls(REPO, state="open", max_pages=2),
                lambda: gh.list_pull_files(REPO, 1, 10),
                lambda: gh.list_commit_authors(REPO, since, 2),
                lambda: gh.get_pull(REPO, 1),
                lambda: gh.create_pull(REPO, title="t", head="h", base="b", body=""),
                lambda: gh.ensure_label(REPO, "x", "000000"),
                lambda: gh.add_labels(REPO, 1, ["x"]),
            ]
            for call in calls:
                try:
                    await call()
                except GitHubError as exc:
                    assert TOKEN not in str(exc)


class TestPortConformance:
    def test_fake_github_satisfies_the_port(self) -> None:
        port: GitHubPort = FakeGitHub()
        assert port is not None

    def test_github_rest_satisfies_the_port(self) -> None:
        client = idle_client()
        port: GitHubPort = GitHubRest(token=None, client=client)
        assert port is not None

    def test_github_error_attributes(self) -> None:
        error = GitHubError("… HTTP 403 (호출 한도 초과)", 403, True)
        assert (error.status, error.rate_limited, str(error)) == (
            403,
            True,
            "… HTTP 403 (호출 한도 초과)",
        )
        plain = GitHubError("x", 500)
        assert plain.rate_limited is False
        assert isinstance(plain, Exception)


class TestFakeGitHub:
    async def test_records_and_returns(self) -> None:
        details = {9: pull_detail(9)}
        fake = FakeGitHub(
            closed=[pull_summary(1)],
            open=[pull_summary(2, state="open", merged_at=None)],
            files={1: ["a", "b", "c"]},
            authors=["alice"],
            details=details,
        )
        assert [p.number for p in await fake.list_pulls(REPO, state="closed", max_pages=1)] == [1]
        assert [p.number for p in await fake.list_pulls(REPO, state="open", max_pages=1)] == [2]
        assert await fake.list_pull_files(REPO, 1, 2) == ["a", "b"]
        assert await fake.list_pull_files(REPO, 5, 2) == []
        assert await fake.list_commit_authors(REPO, datetime(2026, 1, 1), 1) == ["alice"]
        assert (await fake.get_pull(REPO, 9)).merged is True
        number = await fake.create_pull(REPO, title="T", head="h", base="b", body="B")
        await fake.ensure_label(REPO, "x", "000000")
        await fake.add_labels(REPO, number, ["x"])
        assert fake.created == [{"title": "T", "head": "h", "base": "b", "body": "B"}]
        assert fake.ensured == ["x"]
        assert fake.ensured_colors == {"x": "000000"}
        assert fake.added == [(number, ("x",))]
        assert len(fake.calls) == 9
        with pytest.raises(GitHubError):
            await fake.get_pull(REPO, 404)

    async def test_fail_raises_on_every_method(self) -> None:
        boom = GitHubError("boom", 500)
        fake = FakeGitHub(fail=boom)
        with pytest.raises(GitHubError):
            await fake.list_pulls(REPO, state="open", max_pages=1)
        with pytest.raises(GitHubError):
            await fake.add_labels(REPO, 1, ["x"])
        assert len(fake.calls) == 2

    def test_pull_summary_overrides(self) -> None:
        pull = pull_summary(3, title="T", labels=("a",), state="open")
        assert (pull.number, pull.title, pull.labels, pull.state) == (3, "T", ("a",), "open")


# ------------------------------------------------------------------ 수정 라운드 1


class TestNaiveAndAwareCutoffs:
    @pytest.mark.parametrize(
        "cutoff",
        [
            datetime(2026, 10, 1),  # naive는 UTC로 본다
            datetime(2026, 10, 1, tzinfo=UTC),
            datetime(2026, 10, 1, 9, 0, tzinfo=timezone(timedelta(hours=9))),
        ],
        ids=["naive", "utc", "kst"],
    )
    async def test_full_page_older_than_cutoff_stops(self, cutoff: datetime) -> None:
        old = "2026-09-01T00:00:00Z"
        async with rest(lambda r: reply([raw_pull(i + 1, updated_at=old) for i in range(100)])) as (
            gh,
            calls,
        ):
            pulls = await gh.list_pulls(
                REPO, state="closed", max_pages=5, stop_when_updated_before=cutoff
            )
        assert len(calls) == 1
        assert len(pulls) == 100

    @pytest.mark.parametrize(
        "cutoff",
        [
            datetime(2026, 10, 1),
            datetime(2026, 10, 1, tzinfo=UTC),
            datetime(2026, 10, 1, 9, 0, tzinfo=timezone(timedelta(hours=9))),
        ],
        ids=["naive", "utc", "kst"],
    )
    async def test_full_page_newer_than_cutoff_continues(self, cutoff: datetime) -> None:
        new = "2026-10-05T00:00:00Z"
        async with rest(lambda r: reply([raw_pull(i + 1, updated_at=new) for i in range(100)])) as (
            gh,
            calls,
        ):
            await gh.list_pulls(REPO, state="closed", max_pages=3, stop_when_updated_before=cutoff)
        assert len(calls) == 3


class TestConstructorChecks:
    def test_trust_env_client_is_rejected(self) -> None:
        client = httpx2.AsyncClient(trust_env=True)
        with pytest.raises(ValueError, match="trust_env"):
            GitHubRest(token=TOKEN, client=client)

    def test_trust_env_false_client_is_accepted(self) -> None:
        GitHubRest(token=TOKEN, client=idle_client())

    @pytest.mark.parametrize(
        "token",
        ["토큰-secret-value", "has space", "line\nbreak", "tab\ttoken", "nul\x00byte", "é" * 10],
    )
    def test_bad_token_is_rejected_without_echo(self, token: str) -> None:
        with pytest.raises(ValueError) as info:
            GitHubRest(token=token, client=idle_client())
        assert token not in str(info.value)
        assert token not in repr(info.value)

    @pytest.mark.parametrize("token", [None, "", TOKEN, "github_pat_11AAA_bbb"])
    def test_good_tokens_are_accepted(self, token: str | None) -> None:
        GitHubRest(token=token, client=idle_client())


class TestSinceFormatting:
    @pytest.mark.parametrize(
        ("since", "expected"),
        [
            (datetime(5, 1, 2, 3, 4, 5, tzinfo=UTC), "0005-01-02T03:04:05Z"),
            (datetime(1901, 2, 3, 4, 5, 6, tzinfo=UTC), "1901-02-03T04:05:06Z"),
            (datetime(1969, 12, 31, 23, 59, 59, 999999, tzinfo=UTC), "1969-12-31T23:59:59Z"),
            (
                datetime(2000, 1, 1, 0, 0, tzinfo=timezone(timedelta(hours=9))),
                "1999-12-31T15:00:00Z",
            ),
            (datetime(2026, 7, 8, 15, 0), "2026-07-08T15:00:00Z"),
        ],
    )
    async def test_zero_padded_utc(self, since: datetime, expected: str) -> None:
        async with rest(lambda r: reply([])) as (gh, calls):
            await gh.list_commit_authors(REPO, since, 1)
        assert calls[0].url.params["since"] == expected

    @pytest.mark.parametrize(
        "since",
        [
            datetime(9999, 12, 31, 23, 0, tzinfo=timezone(timedelta(hours=-5))),
            datetime(1, 1, 1, 0, 0, tzinfo=timezone(timedelta(hours=9))),
        ],
        ids=["year9999-west", "year1-east"],
    )
    async def test_unrepresentable_instants_are_github_errors(self, since: datetime) -> None:
        async with rest(lambda r: reply([])) as (gh, calls):
            with pytest.raises(GitHubError) as info:
                await gh.list_commit_authors(REPO, since, 1)
        assert info.value.status == 0
        assert calls == []
        assert "9999" not in str(info.value)


class TestMalformedRedirect:
    async def test_302_message_has_http_302(self) -> None:
        async with rest(
            lambda r: httpx2.Response(302, headers={"location": "https://evil.test/x"}),
            token=TOKEN,
        ) as (gh, calls):
            with pytest.raises(GitHubError, match="HTTP 302") as info:
                await gh.list_pulls(REPO, state="open", max_pages=1)
        assert info.value.status == 302
        assert len(calls) == 1

    @pytest.mark.parametrize("status", [301, 302, 307, 308])
    async def test_malformed_location_still_reports_the_status(self, status: int) -> None:
        async with rest(
            lambda r: httpx2.Response(status, headers={"location": "http://host:abc/"}),
            token=TOKEN,
        ) as (gh, calls):
            with pytest.raises(GitHubError, match=f"HTTP {status}") as info:
                await gh.list_pulls(REPO, state="open", max_pages=1)
        assert info.value.status == status
        assert info.value.rate_limited is False
        assert "연결 실패" not in str(info.value)
        assert len(calls) == 1

    async def test_other_protocol_errors_stay_connection_failures(self) -> None:
        def boom(request: httpx2.Request) -> httpx2.Response:
            raise httpx2.RemoteProtocolError("bad framing")

        async with rest(boom) as (gh, _):
            with pytest.raises(GitHubError, match="연결 실패 \\(RemoteProtocolError\\)") as info:
                await gh.list_pulls(REPO, state="open", max_pages=1)
        assert info.value.status == 0

    async def test_hook_is_registered_once_per_client(self) -> None:
        client = idle_client()
        before = len(client.event_hooks["response"])
        GitHubRest(token=None, client=client)
        GitHubRest(token=None, client=client)
        assert len(client.event_hooks["response"]) == before + 1


class _SlowDrip(httpx2.AsyncByteStream):
    def __init__(self) -> None:
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        while True:
            await asyncio.sleep(0.1)
            yield b" "

    async def aclose(self) -> None:
        self.closed = True


class TestTimeBounds:
    def test_constants(self) -> None:
        assert github_client._REQUEST_TIMEOUT_SECONDS == 30.0
        assert github_client._TOTAL_TIMEOUT_SECONDS == 60.0

    async def test_requests_carry_the_exact_timeout(self) -> None:
        async with rest(lambda r: reply([])) as (gh, calls):
            await gh.list_pulls(REPO, state="open", max_pages=1)
        assert calls[0].extensions["timeout"] == {
            "connect": 30.0,
            "read": 30.0,
            "write": 30.0,
            "pool": 30.0,
        }

    async def test_slow_drip_body_hits_the_total_bound(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(github_client, "_TOTAL_TIMEOUT_SECONDS", 0.4)
        stream = _SlowDrip()
        async with rest(lambda r: httpx2.Response(200, stream=stream)) as (gh, _):
            started = time.monotonic()
            with hang_guard(5), pytest.raises(GitHubError) as info:
                await gh.list_pulls(REPO, state="open", max_pages=1)
        assert time.monotonic() - started < 3
        assert info.value.status == 0
        assert "시간 초과" in str(info.value)
        assert stream.closed

    async def test_hang_before_headers_hits_the_total_bound(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(github_client, "_TOTAL_TIMEOUT_SECONDS", 0.3)

        async def handler(request: httpx2.Request) -> httpx2.Response:
            await asyncio.sleep(30)
            return httpx2.Response(200, json=[])

        async with mock_client(handler) as client:
            gh = GitHubRest(token=TOKEN, client=client)
            started = time.monotonic()
            with hang_guard(5), pytest.raises(GitHubError) as info:
                await gh.get_pull(REPO, 3)
        assert time.monotonic() - started < 3
        assert info.value.status == 0
        assert "시간 초과" in str(info.value)
        assert TOKEN not in str(info.value)


class TestFakeGitHubRound1:
    def _pulls(self, count: int, updated: str) -> list[PullSummary]:
        return [pull_summary(i + 1, updated_at=updated) for i in range(count)]

    async def test_state_is_honored(self) -> None:
        fake = FakeGitHub(closed=[pull_summary(1)], open=[pull_summary(2, state="open")])
        assert [p.number for p in await fake.list_pulls(REPO, state="open", max_pages=1)] == [2]

    async def test_max_pages_pages_by_100(self) -> None:
        fake = FakeGitHub(closed=self._pulls(250, "2026-10-05T00:00:00Z"))
        assert len(await fake.list_pulls(REPO, state="closed", max_pages=1)) == 100
        assert len(await fake.list_pulls(REPO, state="closed", max_pages=2)) == 200
        assert len(await fake.list_pulls(REPO, state="closed", max_pages=9)) == 250

    @pytest.mark.parametrize(
        "cutoff",
        [datetime(2026, 10, 1), datetime(2026, 10, 1, tzinfo=UTC)],
        ids=["naive", "aware"],
    )
    async def test_cutoff_stops_after_a_full_old_page(self, cutoff: datetime) -> None:
        fake = FakeGitHub(closed=self._pulls(250, "2026-09-01T00:00:00Z"))
        pulls = await fake.list_pulls(
            REPO, state="closed", max_pages=5, stop_when_updated_before=cutoff
        )
        assert len(pulls) == 100

    async def test_cutoff_does_not_stop_on_newer_pages(self) -> None:
        fake = FakeGitHub(closed=self._pulls(250, "2026-10-05T00:00:00Z"))
        pulls = await fake.list_pulls(
            REPO,
            state="closed",
            max_pages=5,
            stop_when_updated_before=datetime(2026, 10, 1, tzinfo=UTC),
        )
        assert len(pulls) == 250

    async def test_unreadable_updated_at_does_not_stop(self) -> None:
        fake = FakeGitHub(closed=self._pulls(250, "garbage"))
        pulls = await fake.list_pulls(
            REPO,
            state="closed",
            max_pages=5,
            stop_when_updated_before=datetime(2026, 10, 1, tzinfo=UTC),
        )
        assert len(pulls) == 250

    async def test_calls_record_arguments(self) -> None:
        fake = FakeGitHub()
        since = datetime(2026, 7, 1, tzinfo=UTC)
        await fake.list_commit_authors(REPO, since, 3)
        await fake.list_pull_files(REPO, 4, 5)
        assert fake.calls == [
            ("list_commit_authors", REPO, since, 3),
            ("list_pull_files", REPO, 4, 5),
        ]

    async def test_authors_are_capped_by_pages(self) -> None:
        fake = FakeGitHub(authors=[f"u{i}" for i in range(250)])
        since = datetime(2026, 7, 1, tzinfo=UTC)
        assert len(await fake.list_commit_authors(REPO, since, 1)) == 100
        assert len(await fake.list_commit_authors(REPO, since, 5)) == 250

    async def test_ensured_records_names_only(self) -> None:
        fake = FakeGitHub()
        await fake.ensure_label(REPO, "wiki-sync:code", "0e8a16")
        assert fake.ensured == ["wiki-sync:code"]
        assert fake.ensured_colors == {"wiki-sync:code": "0e8a16"}

    def test_pull_summary_default_merge_commit_sha_is_none(self) -> None:
        assert pull_summary(1).merge_commit_sha is None

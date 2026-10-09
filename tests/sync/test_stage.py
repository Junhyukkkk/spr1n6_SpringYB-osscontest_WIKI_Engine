"""단계 공통 도우미(G8·G11)와 동기화 테스트 환경(`make_sync_env`) 자체의 시험."""

import dataclasses
import os
import stat
import unicodedata
from datetime import UTC, datetime
from pathlib import Path

import pytest

from team_wiki_engine.claude.runner import drain_background_tasks
from team_wiki_engine.claude.types import RunClaudeInput
from team_wiki_engine.git.client import GitClient, GitError, auth_header_for
from team_wiki_engine.git.ops import commit_all, push, reset_branch_to, rev_parse
from team_wiki_engine.sync import stage as stage_module
from team_wiki_engine.sync.pr_port import LocalPrPort
from team_wiki_engine.sync.stage import (
    KIND_LABEL,
    LockNotHeldError,
    StageContext,
    StageError,
    append_log_locked,
    difference,
    fail_stage,
    load_baseline,
    path_roots,
    safe_message,
    scrub_paths,
    wiki_pages_changed,
    work_branch,
)
from team_wiki_engine.sync.types import new_stage
from team_wiki_engine.wiki.baseline import Baseline
from team_wiki_engine.wiki.lint import lint
from team_wiki_engine.wiki.log import LogEntry
from tests.helpers import write_tree
from tests.helpers.sync_env import DEFAULT_NOW, make_sync_env

SECRET = "ghp_Zx81QmN4vT7bK2pLs9Wd3Yc6Hf0JrAeU5"
ANTHROPIC_KEY = "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"


def entry(at: datetime = DEFAULT_NOW) -> LogEntry:
    return LogEntry(
        at=at, kind="code", targets="worker 직접 커밋 1", pages=["index.md"], cost_usd=0.5
    )


# ---------------------------------------------------------------- 작은 도우미


class TestSmallHelpers:
    def test_작업_브랜치_이름(self) -> None:
        assert work_branch("code", "20261007-0900") == "sync/code-20261007-0900"
        assert work_branch("ingest", "20261007-2100") == "sync/ingest-20261007-2100"

    def test_종류_이름표(self) -> None:
        assert KIND_LABEL == {"code": "코드", "ingest": "문서"}

    def test_difference는_순서를_지키고_중복은_그대로_둔다(self) -> None:
        assert difference(["c", "a", "b", "a"], ["b"]) == ["c", "a", "a"]
        assert difference([], ["a"]) == []
        assert difference(["a"], []) == ["a"]

    def test_StageError는_예외이고_LockNotHeldError는_RuntimeError다(self) -> None:
        assert issubclass(StageError, Exception)
        assert issubclass(LockNotHeldError, RuntimeError)


# ---------------------------------------------------------------- safe_message


class TestSafeMessage:
    def test_anthropic_키_모양과_알려진_비밀값을_가린다(self) -> None:
        text = f"실패 {ANTHROPIC_KEY} 그리고 {SECRET} 끝"
        shown = safe_message(text, [SECRET])
        assert ANTHROPIC_KEY not in shown
        assert SECRET not in shown
        assert "sk-ant-api03" not in shown

    def test_알려진_비밀값은_문장_속_어디에_있어도_가린다(self) -> None:
        token = "Plain-Opaque-Value-123"  # 모양으로는 비밀값처럼 안 보이는 값
        shown = safe_message(f"push 실패: remote={token}/x", [token])
        assert token not in shown
        assert "push 실패" in shown

    def test_300자_이하로_자른다(self) -> None:
        shown = safe_message("가나다 " * 500, [])
        assert len(shown) <= 300
        assert shown.endswith("…")

    def test_한도를_바꿀_수_있다(self) -> None:
        assert len(safe_message("가나다 " * 100, [], limit=50)) <= 50

    def test_제어_문자_이스케이프로_늘어나도_300자_이하다(self) -> None:
        shown = safe_message("\x1b" * 400, [])
        assert len(shown) <= 300
        assert "\x1b" not in shown
        assert not shown.rstrip("…").endswith("\\x1")  # 끊긴 이스케이프 조각을 남기지 않는다

    def test_먼저_가리고_나중에_자른다_경계에_걸친_비밀값_조각이_남지_않는다(self) -> None:
        # 비밀값이 300자 경계를 가로지른다: 자르고 나서 가리면 앞 조각이 남는다.
        # 모양으로는 비밀값처럼 안 보이는 값이라 알려진 비밀값 목록만이 막는다.
        opaque = "Plain-Opaque-Value-123"
        for pad in range(270, 301):
            text = "가나 " * (pad // 3) + opaque + " 끝"
            shown = safe_message(text, [opaque, SECRET])
            assert len(shown) <= 300
            assert "Plain-O" not in shown, pad
            assert "Plain" not in shown, pad
        for pad in range(280, 301):
            shown = safe_message("가" * pad + SECRET + " 끝", [SECRET])
            assert len(shown) <= 300
            assert "ghp_" not in shown, pad

    @pytest.mark.parametrize("limit", [0, -1, -300])
    def test_한도가_0_이하면_빈_문자열이다(self, limit: int) -> None:
        assert safe_message("실패 " + ANTHROPIC_KEY, [SECRET], limit=limit) == ""
        assert safe_message("", [], limit=limit) == ""

    def test_알려진_비밀값이_없어도_동작한다(self) -> None:
        assert safe_message("그냥 오류", ()) == "그냥 오류"
        assert safe_message("", []) == ""


class TestScrubPaths:
    def test_알려진_루트는_짧은_표지로_바꾼다_긴_것부터(self) -> None:
        roots = {"<home>": "/srv/team-wiki", "<data-sync>": "/srv/team-wiki/data-sync"}
        text = "'/srv/team-wiki/data-sync/.git/HEAD'와 /srv/team-wiki/state"
        shown = scrub_paths(text, roots)
        assert "/srv" not in shown
        assert "<data-sync>" in shown
        assert "<home>" in shown

    def test_남은_절대_경로는_경로_표지로_바꾼다(self) -> None:
        shown = scrub_paths("fatal: Unable to create '/Users/x/repos/w/.git/index.lock': 있음", {})
        assert "/Users" not in shown
        assert "index.lock" not in shown
        assert "<경로>" in shown
        assert shown.startswith("fatal: Unable to create '")

    def test_윈도_드라이브_경로도_바꾼다(self) -> None:
        assert "Users" not in scrub_paths(r"C:\Users\x\repo 와 D:/data/repo", {})

    @pytest.mark.parametrize(
        "text",
        [
            "wiki/index.md",
            "raw/a.md",
            "origin/main",
            "a/b",
            "https://example.com/x/y",
            "ssh://git@host/x/y",
            "//",
            "/",
            "/etc",
            "../x/y",
            "./x/y",
            "~/x/y",
            "비율 1/2/3 and/or",
            "허용 경로 밖 변경: raw/x.md, wiki/log.md",
        ],
    )
    def test_상대_경로와_URL은_건드리지_않는다(self, text: str) -> None:
        assert scrub_paths(text, {}) == text

    def test_적대적_입력도_선형_시간이다(self) -> None:
        hostile = "/" + "a/" * 200_000 + "/" * 100_000 + "x" * 100_000
        assert len(scrub_paths(hostile, {})) < len(hostile)

    def test_safe_message는_가린_뒤_경로를_바꾸고_자른다(self) -> None:
        text = f"실패 '/tmp/work/repos/x' {SECRET}" + " 끝" * 400
        shown = safe_message(text, [SECRET])
        assert "/tmp" not in shown
        assert SECRET not in shown
        assert len(shown) <= 300

    def test_safe_message는_상대_경로를_그대로_둔다(self) -> None:
        assert (
            safe_message("wiki/index.md를 읽을 수 없습니다", [])
            == "wiki/index.md를 읽을 수 없습니다"
        )

    def test_safe_message에_루트를_주면_표지로_바꾼다(self, tmp_dir: Path) -> None:
        roots = {"<repos>": str(tmp_dir / "repos")}
        shown = safe_message(f"'{tmp_dir}/repos/other'은(는) 비어 있지 않습니다", [], roots=roots)
        assert "<repos>/other" in shown
        assert str(tmp_dir) not in shown


class TestPathRoots:
    async def test_설정한_루트를_표지로_담는다(self, tmp_dir: Path) -> None:
        env = await make_sync_env(tmp_dir)
        roots = path_roots(env.paths)
        assert roots["<data-sync>"] == str(env.paths.data_sync_dir)
        assert roots["<repos>"] == str(env.paths.repos_dir)
        assert roots["<home>"] == str(env.paths.home)
        assert set(roots) >= {"<state>", "<runs>", "<skills>", "<engine>", "<data>", "<local-prs>"}


# ---------------------------------------------------------------- SyncDeps


class TestSyncDeps:
    async def test_repr에_redact_값이나_토큰이_없다(self, tmp_dir: Path) -> None:
        env = await make_sync_env(tmp_dir)
        authed = GitClient(
            env.paths.data_sync_dir,
            auth_header=auth_header_for(SECRET),
            allowed_protocols="file",
        )
        deps = dataclasses.replace(
            env.deps, data_git=authed, redact=(SECRET, ANTHROPIC_KEY, "다른-비밀-값-1234")
        )
        shown = repr(deps)
        for needle in (SECRET, ANTHROPIC_KEY, "다른-비밀-값-1234", "redact"):
            assert needle not in shown
        assert SECRET not in repr(deps.data_git)
        assert deps.redact == (SECRET, ANTHROPIC_KEY, "다른-비밀-값-1234")  # 값은 그대로 있다

    async def test_repr에_환경변수_이름이_없다(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", ANTHROPIC_KEY)
        monkeypatch.setenv("GITHUB_TOKEN", SECRET)
        env = await make_sync_env(tmp_dir)
        shown = repr(env.deps) + repr(env)
        assert ANTHROPIC_KEY not in shown and SECRET not in shown

    async def test_기본값과_불변성(self, tmp_dir: Path) -> None:
        env = await make_sync_env(tmp_dir)
        assert env.deps.redact == ()
        assert env.deps.drain is drain_background_tasks
        assert env.deps.settle_timeout_s == 30.0
        with pytest.raises(dataclasses.FrozenInstanceError):
            env.deps.redact = ("x",)  # type: ignore[misc]

    async def test_StageContext는_불변이다(self, tmp_dir: Path) -> None:
        env = await make_sync_env(tmp_dir)
        async with env.stage("r1") as ctx:
            with pytest.raises(dataclasses.FrozenInstanceError):
                ctx.run_id = "r2"  # type: ignore[misc]


# ---------------------------------------------------------------- fail_stage


class TestFailStage:
    async def test_쓴_파일을_되돌리고_요약을_채운다(self, tmp_dir: Path) -> None:
        env = await make_sync_env(tmp_dir)
        data = env.paths.data_sync_dir
        write_tree(data, {"wiki/new.md": "# 새 글\n", "raw/.gitkeep": "바뀜"})
        stage = new_stage("code")
        returned = await fail_stage(stage, env.deps.data_git, "허용 경로 밖 변경: raw/x.md")
        assert returned is stage
        assert stage.result == "failed"
        assert stage.error == "허용 경로 밖 변경: raw/x.md"
        assert stage.summary == "코드: 실패 — 허용 경로 밖 변경: raw/x.md"
        assert not (data / "wiki" / "new.md").exists()
        assert (data / "raw" / ".gitkeep").read_text(encoding="utf-8") == ""

    async def test_문서_단계의_이름표(self, tmp_dir: Path) -> None:
        env = await make_sync_env(tmp_dir)
        stage = await fail_stage(new_stage("ingest"), env.deps.data_git, "나쁨")
        assert stage.summary == "문서: 실패 — 나쁨"

    async def test_되돌리기가_실패해도_예외_없이_실패로_끝난다(self, tmp_dir: Path) -> None:
        not_a_repo = GitClient(tmp_dir / "없음", allowed_protocols="file")
        stage = await fail_stage(new_stage("code"), not_a_repo, "원인")
        assert stage.result == "failed"
        assert stage.summary == "코드: 실패 — 원인"


# ---------------------------------------------------------------- load_baseline


class TestLoadBaseline:
    def test_없는_파일은_빈_기준이다(self, tmp_dir: Path) -> None:
        assert load_baseline(tmp_dir, []) == Baseline()

    def test_정상_파일은_읽는다(self, tmp_dir: Path) -> None:
        (tmp_dir / "baseline.json").write_text(
            '{"repos": {"worker": {"commit": "' + "a" * 40 + '", "archived": false}}}',
            encoding="utf-8",
        )
        assert load_baseline(tmp_dir, []).repos["worker"].commit == "a" * 40

    def test_폴더인_baseline_json은_StageError다(self, tmp_dir: Path) -> None:
        (tmp_dir / "baseline.json").mkdir()
        with pytest.raises(StageError) as info:
            load_baseline(tmp_dir, [])
        assert str(info.value).startswith("baseline.json을 읽을 수 없습니다")
        assert info.value.__cause__ is None
        assert info.value.__context__ is None  # lstat으로 걸러 읽지도 않았다(연쇄 예외 없음)

    def test_심볼릭_링크인_baseline_json은_대상을_읽지_않고_StageError다(
        self, tmp_dir: Path
    ) -> None:
        target = tmp_dir / "elsewhere.json"
        target.write_text('{"repos": {}}', encoding="utf-8")  # 정상 내용이어도 따라가지 않는다
        (tmp_dir / "baseline.json").symlink_to(target)
        with pytest.raises(StageError) as info:
            load_baseline(tmp_dir, [])
        assert str(info.value).startswith("baseline.json을 읽을 수 없습니다")
        assert "elsewhere" not in str(info.value)

    def test_FIFO인_baseline_json은_멈추지_않고_StageError다(self, tmp_dir: Path) -> None:
        os.mkfifo(tmp_dir / "baseline.json")
        with pytest.raises(StageError) as info:
            load_baseline(tmp_dir, [])
        assert str(info.value).startswith("baseline.json을 읽을 수 없습니다")

    def test_1MiB를_넘는_baseline_json은_읽지_않고_StageError다(self, tmp_dir: Path) -> None:
        big = tmp_dir / "baseline.json"
        big.write_bytes(b'{"repos": {}}' + b" " * (1024 * 1024))  # 내용은 정상 JSON이다
        with pytest.raises(StageError) as info:
            load_baseline(tmp_dir, [])
        assert str(info.value).startswith("baseline.json을 읽을 수 없습니다")
        assert len(str(info.value)) <= len("baseline.json을 읽을 수 없습니다: ") + 300

    def test_정확히_1MiB인_일반_파일은_읽는다(self, tmp_dir: Path) -> None:
        text = '{"repos": {}}'
        (tmp_dir / "baseline.json").write_text(
            text + " " * (1024 * 1024 - len(text)), encoding="utf-8"
        )
        assert load_baseline(tmp_dir, []) == Baseline()

    @pytest.mark.skipif(os.geteuid() == 0, reason="root는 권한 거부가 없다")
    def test_읽기_권한이_없으면_StageError다(self, tmp_dir: Path) -> None:
        file = tmp_dir / "baseline.json"
        file.write_text("{}", encoding="utf-8")
        file.chmod(0)
        try:
            with pytest.raises(StageError) as info:
                load_baseline(tmp_dir, [])
        finally:
            file.chmod(stat.S_IRUSR | stat.S_IWUSR)
        assert str(info.value).startswith("baseline.json을 읽을 수 없습니다")

    @pytest.mark.parametrize("content", ["쓰레기{", "[]", '{"repos": 3}', "", "null"])
    def test_쓰레기_JSON은_StageError다(self, tmp_dir: Path, content: str) -> None:
        (tmp_dir / "baseline.json").write_text(content, encoding="utf-8")
        with pytest.raises(StageError) as info:
            load_baseline(tmp_dir, [])
        assert str(info.value).startswith("baseline.json을 읽을 수 없습니다")

    def test_UTF8이_아니면_StageError다(self, tmp_dir: Path) -> None:
        (tmp_dir / "baseline.json").write_bytes(b"\xff\xfe\x00")
        with pytest.raises(StageError) as info:
            load_baseline(tmp_dir, [])
        assert str(info.value).startswith("baseline.json을 읽을 수 없습니다")

    def test_메시지에_알려진_비밀값이_없다(self, tmp_dir: Path) -> None:
        # 형식 오류 메시지가 키 이름(= 파일 속 글)을 경로로 싣는다.
        key = "tok-Plain-Opaque-Value-123"
        (tmp_dir / "baseline.json").write_text(
            f'{{"repos": {{"{key}": {{"commit": "x", "archived": true}}}}}}', encoding="utf-8"
        )
        with pytest.raises(StageError) as info:
            load_baseline(tmp_dir, [key])
        assert key not in str(info.value)
        assert str(info.value).startswith("baseline.json을 읽을 수 없습니다")
        assert len(str(info.value)) <= len("baseline.json을 읽을 수 없습니다: ") + 300

    def test_폴더_경로에_비밀값이_있어도_가린다(self, tmp_dir: Path) -> None:
        secret_dir = tmp_dir / "Plain-Opaque-Dir-123"
        (secret_dir / "baseline.json").mkdir(parents=True)
        with pytest.raises(StageError) as info:
            load_baseline(secret_dir, ["Plain-Opaque-Dir-123"])
        assert "Plain-Opaque-Dir-123" not in str(info.value)


# ---------------------------------------------------------------- append_log_locked


class TestAppendLogLocked:
    async def test_쥔_잠금이면_한_줄_붙인다(self, tmp_dir: Path) -> None:
        env = await make_sync_env(tmp_dir)
        async with env.stage("20261007-0900") as ctx:
            append_log_locked(ctx, entry())
        text = (env.paths.data_sync_dir / "wiki" / "log.md").read_text(encoding="utf-8")
        assert text.count("\n- ") == 1
        assert "| code | worker 직접 커밋 1 → index.md | $0.50" in text

    async def test_놓은_잠금이면_거부하고_아무것도_쓰지_않는다(self, tmp_dir: Path) -> None:
        env = await make_sync_env(tmp_dir)
        async with env.stage("20261007-0900") as ctx:
            pass
        assert not ctx.lock.held
        with pytest.raises(LockNotHeldError) as info:
            append_log_locked(ctx, entry())
        assert str(info.value) == "sync.lock 없이 log.md를 쓰려 했습니다"
        assert not (env.paths.data_sync_dir / "wiki").exists()  # 폴더도 만들지 않았다

    async def test_놓은_잠금은_기존_log를_건드리지_않는다(self, tmp_dir: Path) -> None:
        env = await make_sync_env(tmp_dir)
        log = env.paths.data_sync_dir / "wiki" / "log.md"
        async with env.stage("r1") as ctx:
            append_log_locked(ctx, entry())
        before = log.read_bytes()
        with pytest.raises(LockNotHeldError):
            append_log_locked(ctx, entry())
        assert log.read_bytes() == before

    async def test_다른_곳이_쥔_잠금은_문맥을_만들_수_없다(self, tmp_dir: Path) -> None:
        env = await make_sync_env(tmp_dir)
        async with env.stage("r1"):
            with pytest.raises(AssertionError):
                async with env.stage("r2"):
                    pass


# ---------------------------------------------------------------- wiki_pages_changed


class TestWikiPagesChanged:
    async def test_log를_빼고_접두를_뗀다(self, tmp_dir: Path) -> None:
        env = await make_sync_env(tmp_dir)
        data = env.paths.data_sync_dir
        write_tree(
            data,
            {
                "wiki/a.md": "# A\n",
                "wiki/log.md": "# 로그\n",
                "wiki/status/worker.md": "# w\n",
                "wiki/한글/기록.md": "# 기록\n",
                "raw/새.md": "원본\n",  # wiki 밖은 빠진다
            },
        )
        await commit_all(env.deps.data_git, "wiki: 시험")
        assert await wiki_pages_changed(env.deps.data_git, "main") == [
            "a.md",
            "status/worker.md",
            "한글/기록.md",
        ]

    async def test_바뀐_것이_없으면_빈_목록이다(self, tmp_dir: Path) -> None:
        env = await make_sync_env(tmp_dir)
        assert await wiki_pages_changed(env.deps.data_git, "main") == []

    async def test_log만_바뀌면_빈_목록이다(self, tmp_dir: Path) -> None:
        env = await make_sync_env(tmp_dir)
        write_tree(env.paths.data_sync_dir, {"wiki/log.md": "# 로그\n"})
        await commit_all(env.deps.data_git, "chore: 이력")
        assert await wiki_pages_changed(env.deps.data_git, "main") == []

    async def test_없는_기준_브랜치는_GitError다(self, tmp_dir: Path) -> None:
        env = await make_sync_env(tmp_dir)
        with pytest.raises(GitError):
            await wiki_pages_changed(env.deps.data_git, "nope")

    @staticmethod
    def fake_names(monkeypatch: pytest.MonkeyPatch, names: list[str]) -> None:
        async def fake(git: GitClient, from_: str, to: str, pathspecs: object) -> list[str]:
            return names

        monkeypatch.setattr(stage_module, "diff_names", fake)

    async def test_NFD_경로는_NFC로_나온다(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        nfd = unicodedata.normalize("NFD", "wiki/결정 기록.md")
        assert nfd != "wiki/결정 기록.md"
        self.fake_names(monkeypatch, [nfd])
        pages = await wiki_pages_changed(GitClient(tmp_dir), "main")
        assert pages == ["결정 기록.md"]
        assert unicodedata.is_normalized("NFC", pages[0])

    async def test_이상한_경로도_던지지_않고_눈에_보이게_바꾼다(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        odd = [
            "wiki/\udcff\udcfe.md",  # 잘못된 UTF-8(surrogateescape)
            'wiki/"따옴표" 안.md',
            "wiki/줄\n바꿈.md",
            "wiki/\\355\\225\\234.md",  # git이 따옴표로 감싼 8진 표기처럼 보이는 이름
            "wiki/‮RTL.md",
            "wiki/log.md",
        ]
        self.fake_names(monkeypatch, odd)
        pages = await wiki_pages_changed(GitClient(tmp_dir), "main")
        assert len(pages) == 5
        for page in pages:
            assert page.isprintable()
            page.encode("utf-8")  # 서로게이트가 남지 않는다
        assert "\\udcff\\udcfe.md" in pages[0]
        assert pages[1] == '"따옴표" 안.md'
        assert "\\n" in pages[2]
        assert "\\u202e" in pages[4]

    async def test_접두는_한_번만_뗀다(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self.fake_names(monkeypatch, ["wiki/wiki/x.md", "wiki/log.md.bak", "wiki/sub/log.md"])
        pages = await wiki_pages_changed(GitClient(tmp_dir), "main")
        assert pages == ["wiki/x.md", "log.md.bak", "sub/log.md"]


# ---------------------------------------------------------------- make_sync_env


class TestMakeSyncEnv:
    async def test_만든_직후_data_sync는_origin_main과_같고_wiki가_없다(
        self, tmp_dir: Path
    ) -> None:
        env = await make_sync_env(tmp_dir)
        git = env.deps.data_git
        assert await rev_parse(git, "HEAD") == await rev_parse(git, "origin/main")
        assert await rev_parse(git, "HEAD") == await rev_parse(env.data_work, "HEAD")
        assert not (env.paths.data_sync_dir / "wiki").exists()
        assert (env.paths.data_sync_dir / "raw" / ".gitkeep").is_file()
        assert env.paths.data_sync_dir == tmp_dir / "home" / "data-sync"

    async def test_raw_files가_데이터_저장소에_들어간다(self, tmp_dir: Path) -> None:
        env = await make_sync_env(tmp_dir, raw_files={"raw/2026-10-07-회의.md": "내용\n"})
        copy = env.paths.data_sync_dir / "raw" / "2026-10-07-회의.md"
        assert copy.read_text(encoding="utf-8") == "내용\n"
        assert not (env.paths.data_sync_dir / "wiki").exists()

    async def test_인증이_없고_모든_클라이언트가_file_전송만_쓴다(self, tmp_dir: Path) -> None:
        env = await make_sync_env(tmp_dir)
        assert env.deps.data_git.has_auth is False
        assert env.deps.code_git.has_auth is False
        for client in (env.deps.data_git, env.deps.code_git, env.code_work, env.data_work):
            assert client._allowed_protocols == "file"
            assert client._environment()["GIT_ALLOW_PROTOCOL"] == "file"
        assert env.deps.github is None

    async def test_설정의_저장소_URL은_모두_로컬_절대_경로다(self, tmp_dir: Path) -> None:
        extra = {"name": "extra", "url": str(tmp_dir / "extra.git")}
        env = await make_sync_env(tmp_dir, extra_repos=[extra])
        urls = [repo.url for repo in env.config.repos] + [env.config.data_repo.url]
        assert len(urls) == 3
        for url in urls:
            assert os.path.isabs(url) and "://" not in url
            assert Path(url).is_relative_to(tmp_dir)
        assert [r.name for r in env.config.repos] == ["worker", "extra"]
        assert env.config.delivery == "local"

    @pytest.mark.parametrize(
        "bad", ["https://github.com/o/r.git", "relative/path.git", "-evil", "file:///x.git"]
    )
    async def test_로컬_절대_경로가_아닌_URL은_거부한다(self, tmp_dir: Path, bad: str) -> None:
        with pytest.raises(AssertionError):
            await make_sync_env(tmp_dir, extra_repos=[{"name": "bad", "url": bad}])

    async def test_리뷰_모드에_따라_PR_포트가_정해진다(self, tmp_dir: Path) -> None:
        pr = await make_sync_env(tmp_dir / "a")
        assert isinstance(pr.deps.pr_port, LocalPrPort)
        assert pr.config.review_mode == "pr"
        direct = await make_sync_env(tmp_dir / "b", review_mode="direct")
        assert direct.deps.pr_port is None
        assert direct.config.review_mode == "direct"

    async def test_시각은_결정적이고_set_now로만_바뀐다(self, tmp_dir: Path) -> None:
        env = await make_sync_env(tmp_dir)
        assert env.deps.now() == datetime(2026, 10, 7, 0, 0, 0, tzinfo=UTC)
        assert env.deps.now() == env.deps.now()
        later = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)
        env.set_now(later)
        assert env.deps.now() == later
        with pytest.raises(ValueError):
            env.set_now(datetime(2026, 10, 8, 9, 0))  # 시간대 없음

    async def test_stage_안에서만_잠금을_쥔다(self, tmp_dir: Path) -> None:
        env = await make_sync_env(tmp_dir)
        env.set_now(datetime(2026, 10, 7, 9, 0, tzinfo=UTC))
        async with env.stage("20261007-0900") as ctx:
            assert isinstance(ctx, StageContext)
            assert ctx.lock.held
            assert ctx.run_id == "20261007-0900"
            assert ctx.now == datetime(2026, 10, 7, 9, 0, tzinfo=UTC)
            assert ctx.deps is env.deps
        assert not ctx.lock.held
        async with env.stage("다음") as again:  # 놓았으니 다시 잡을 수 있다
            assert again.lock.held

    async def test_예외로_나와도_잠금을_놓는다(self, tmp_dir: Path) -> None:
        env = await make_sync_env(tmp_dir)
        with pytest.raises(RuntimeError):
            async with env.stage("r1") as ctx:
                raise RuntimeError("실패")
        assert not ctx.lock.held

    async def test_merge_branch는_작업_브랜치를_main에_합친다(self, tmp_dir: Path) -> None:
        env = await make_sync_env(tmp_dir)
        git = env.deps.data_git
        before = await rev_parse(git, "origin/main")
        await reset_branch_to(git, "sync/code-r1", "origin/main")
        write_tree(env.paths.data_sync_dir, {"wiki/x.md": "# X\n"})
        await commit_all(git, "wiki: 반영")
        await push(git, "sync/code-r1", "sync/code-r1")
        await env.merge_branch("sync/code-r1")
        assert await rev_parse(git, "origin/main") != before
        shown = await git.run(["show", "origin/main:wiki/x.md"])
        assert shown.text == "# X\n"
        merge = await git.run(["rev-list", "--parents", "-n", "1", "origin/main"])
        assert len(merge.text.split()) == 3  # --no-ff: 부모가 둘인 머지 커밋

    async def test_기본_스크립트는_점검을_통과하는_위키를_쓴다(self, tmp_dir: Path) -> None:
        env = await make_sync_env(tmp_dir)
        inp = RunClaudeInput(
            profile="sync-code",
            prompt="코드 반영",
            cwd=env.paths.data_sync_dir,
            read_roots=(),
            write_roots=(env.paths.data_sync_dir / "wiki",),
            timeout_ms=1000,
        )
        result = await env.runner(inp)
        assert result.ok and result.session_id == "sess-code"
        assert result.text.endswith("## 확신이 낮은 부분\n- 없음")
        data = env.paths.data_sync_dir
        assert "[2026-10-07] 샘플 어긋남" in (data / "wiki/open-questions.md").read_text("utf-8")
        assert f'baseline_commit: "{"0" * 40}"' in (data / "wiki/status/worker.md").read_text(
            "utf-8"
        )
        assert lint(data) == []
        assert env.runner.inputs == [inp]

    async def test_기본_스크립트의_문서_반영과_재확인(self, tmp_dir: Path) -> None:
        env = await make_sync_env(tmp_dir)
        data = env.paths.data_sync_dir

        def ask(prompt: str) -> RunClaudeInput:
            return RunClaudeInput(
                profile="ingest",
                prompt=prompt,
                cwd=data,
                read_roots=(),
                write_roots=(data / "wiki",),
                timeout_ms=1000,
            )

        write_tree(data, {"raw/2026-10-07-회의.md": "원본\n"})  # 점검(L3)이 raw 파일을 확인한다
        result = await env.runner(ask("오늘 날짜: 2026-10-07\n원본 파일: raw/2026-10-07-회의.md"))
        assert result.text == "## 문서 분석\n- 새로 정함: 2026-10-07-회의.md"
        assert result.session_id == "sess-ingest"
        assert (data / "wiki/sources/2026-10-07-회의.md").is_file()
        assert (data / "wiki/index.md").is_file()
        assert lint(data) == []
        index_before = (data / "wiki/index.md").read_text("utf-8")
        await env.runner(ask("원본 파일: raw/2026-10-08-다음.md"))
        assert (data / "wiki/index.md").read_text("utf-8") == index_before  # 있으면 그대로
        recheck = await env.runner(ask("재확인 해 주세요"))
        assert (recheck.text, recheck.session_id) == ("재확인 완료", "sess-recheck")
        with pytest.raises(ValueError):
            await env.runner(ask("원본 이름 없음"))

    async def test_사용자_환경에_기대지_않는다(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEAM_WIKI_HOME", "/definitely/not/here")
        monkeypatch.setenv("GIT_DIR", "/definitely/not/a/git/dir")
        monkeypatch.setenv("GIT_AUTHOR_NAME", "사용자")
        env = await make_sync_env(tmp_dir)
        assert env.paths.home == tmp_dir / "home"
        assert await rev_parse(env.deps.data_git, "HEAD") == await rev_parse(
            env.deps.data_git, "origin/main"
        )
        assert env.notifier.sent == []


class TestScrubPathsRootsOnly:
    ROOTS = {"<repos>": "/srv/team/repos"}

    def test_설정된_루트는_표지로_바꾼다(self) -> None:
        shown = scrub_paths("읽음 /srv/team/repos/worker/a.kt", self.ROOTS, generic=False)
        assert shown == "읽음 <repos>/worker/a.kt"

    def test_API_경로_같은_일반_절대_경로는_그대로_둔다(self) -> None:
        text = "`/api/v2/members`로 보내고 /health/check 도 본다"
        assert scrub_paths(text, self.ROOTS, generic=False) == text

    @pytest.mark.parametrize(
        "path",
        [
            "/Users/x/repos/w",
            "/home/bot/x/y",
            "/tmp/run/x",
            "/private/var/x",
            "/root/a/b",
            "/etc/ssh/keys",
            "/opt/app/conf",
            "/srv/data/x",
        ],
    )
    def test_홈_tmp_private_접두어_경로는_가린다(self, path: str) -> None:
        shown = scrub_paths(f"오류 {path}/f.txt 끝", {}, generic=False)
        assert path not in shown
        assert "<경로>" in shown

    def test_윈도_드라이브_경로도_가린다(self) -> None:
        assert "Users" not in scrub_paths(r"C:\Users\x\repo", {}, generic=False)

    def test_기본은_일반_절대_경로도_가린다(self) -> None:
        assert scrub_paths("/api/v2/members", {}) == "<경로>"

"""PR 본문 작성과 전달.

본문은 팀원에게 공개되는 곳이라 AI가 쓴 글·공격자가 영향을 줄 수 있는 글(커밋 제목, PR 제목)을
믿지 않는다: 멘션 무력화, 펜스 길이, 제어·방향 문자, 길이 상한(무력화 뒤 기준), 열린 펜스 닫기.
전달은 임시 폴더의 실제 bare 저장소로 돈다.
"""

import asyncio
import random
import re
import time
from collections.abc import Sequence
from dataclasses import FrozenInstanceError, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from team_wiki_engine.git.client import GitClient, GitError
from team_wiki_engine.git.ops import commit_all, ensure_clone, fetch_branch, push, reset_branch_to
from team_wiki_engine.sync.pr import (
    Analysis,
    Delivered,
    PrBodyInput,
    build_pr_body,
    deliver,
    extract_section,
    fence,
    neutralize_mentions,
)
from team_wiki_engine.sync.pr_port import LocalPrPort, OpenPrRequest, TrackedPr, read_tracking
from team_wiki_engine.wiki.frontmatter import parse_frontmatter
from team_wiki_engine.wiki.lint import LintViolation
from tests.helpers import write_tree
from tests.helpers.git import commit_files, seed_bare

ZWSP = "\u200b"
RUN_ID = "20261007-0900"
RAW_MENTION = re.compile(r"@[A-Za-z0-9_-]")


# ---------------------------------------------------------------- 도우미


def _input(**extra: Any) -> PrBodyInput:
    fields: dict[str, Any] = {
        "reflected": ["worker #14 재시도 정책 변경 (member-a)"],
        "changed_pages": ["wiki/status/worker.md"],
        "new_questions": ["[2026-10-07] a"],
        "resolved_questions": [],
        "analyses": [],
        "low_confidence": "- 없음",
        "lint_violations": [],
        "warnings": [],
        "run_id": RUN_ID,
        "cost_usd": 0.384,
        "input_tokens": 41203,
        "output_tokens": 3112,
    }
    fields.update(extra)
    return PrBodyInput(**fields)


def _open_fence(text: str) -> str | None:
    """CommonMark 규칙으로 본문 끝까지 닫히지 않은 펜스의 표지(없으면 `None`). 구현과 따로 쓴 것."""
    marker: str | None = None
    for line in text.split("\n"):
        stripped = line.lstrip(" ")
        indent = len(line) - len(stripped)
        if indent > 3 or stripped.startswith("\t"):
            continue
        run = re.match(r"(`{3,}|~{3,})", stripped)
        if marker is None:
            if run is None:
                continue
            info = stripped[len(run.group(1)) :]
            if run.group(1)[0] == "`" and "`" in info:
                continue
            marker = run.group(1)
        elif (
            run is not None
            and run.group(1)[0] == marker[0]
            and len(run.group(1)) >= len(marker)
            and stripped[len(run.group(1)) :].strip(" \t") == ""
        ):
            marker = None
    return marker


# ---------------------------------------------------------------- extract_section


def test_extract_section() -> None:
    text = "본문\n\n## 확신이 낮은 부분\n- a\n- b\n\n## 다른 절\n- c"
    assert extract_section(text, "## 확신이 낮은 부분") == "- a\n- b"
    assert extract_section("## 문서 분석\n- 새로 정함: x", "## 문서 분석") == "- 새로 정함: x"
    assert extract_section("없음", "## 문서 분석") is None


def test_extract_section_details() -> None:
    assert extract_section("## 절\r\n- a\r\n- b\r\n# 큰 제목\r\n- c", "## 절") == "- a\n- b"
    assert (
        extract_section("## 절\n### 소제목\n- a", "## 절") == "### 소제목\n- a"
    )  # ###는 안 끊는다
    assert extract_section("## 절\n\n  \n## 다음", "## 절") is None  # 빈 절
    assert extract_section("  ## 절  \n- a", "## 절") == "- a"  # 제목 줄 앞뒤 공백 허용
    assert extract_section("## 절 ", "## 절") is None
    assert extract_section("", "## 절") is None


# ---------------------------------------------------------------- 본문 순서


def test_sections_in_order() -> None:
    assert build_pr_body(_input(), 4000) == "\n\n".join(
        [
            "## 반영한 것\n- worker #14 재시도 정책 변경 (member-a)",
            "## 바뀐 위키 페이지\n- wiki/status/worker.md",
            "## 미결\n- 새로 생김: [2026-10-07] a",
            "## 확신이 낮은 부분\n- 없음",
            "## 실행 정보\nrunId 20261007-0900 · 비용 $0.38 · 입력 41,203 / 출력 3,112 토큰",
        ]
    )


def test_empty_lists_say_none_and_resolved_questions() -> None:
    body = build_pr_body(
        _input(reflected=[], changed_pages=[], new_questions=[], resolved_questions=["[1] b"]), 4000
    )
    assert body.startswith("## 반영한 것\n- 없음\n\n## 바뀐 위키 페이지\n- 없음\n\n")
    assert "## 미결\n- 해소: [1] b" in body


def test_lint_failure_analyses_and_warnings() -> None:
    body = build_pr_body(
        _input(
            lint_violations=[LintViolation("L2", "wiki/a.md", "깨진 링크: b.md")],
            analyses=[Analysis("2026-10-07-회의록.md", "- 새로 정함: x")],
            warnings=["web: 받아 오지 못함"],
            low_confidence=None,
        ),
        4000,
    )
    assert body.startswith("> ⚠️ 점검 실패 1건")
    assert "```\n[L2] wiki/a.md: 깨진 링크: b.md\n위반 1건\n```" in body
    assert "## 문서별 분석\n### 2026-10-07-회의록.md\n- 새로 정함: x" in body
    assert "## 경고\n- web: 받아 오지 못함" in body
    assert "## 확신이 낮은 부분\n- (응답에 해당 절이 없습니다)" in body
    assert _open_fence(body) is None
    # 절 순서: 점검 실패 → 반영 → 페이지 → 미결 → 분석 → 확신 → 경고 → 실행 정보
    order = [
        body.index(h)
        for h in (
            "점검 실패",
            "## 반영한 것",
            "## 바뀐 위키 페이지",
            "## 미결",
            "## 문서별 분석",
            "## 확신이 낮은 부분",
            "## 경고",
            "## 실행 정보",
        )
    ]
    assert order == sorted(order)


def test_long_lists_are_reduced_but_key_sections_stay() -> None:
    many = [f"worker #{i} 아주 긴 제목 {'x' * 30}" for i in range(300)]
    body = build_pr_body(_input(reflected=many, changed_pages=many), 2000)
    assert len(body) <= 2000
    assert "외 " in body
    assert "## 확신이 낮은 부분" in body
    assert "## 실행 정보" in body


def test_reduction_steps_follow_the_ladder() -> None:
    items = [f"항목{i:03d} " + "x" * 40 for i in range(100)]
    full = build_pr_body(_input(reflected=items, changed_pages=[]), 100_000)
    assert "- 항목099" in full and "외 " not in full.split("## 바뀐")[0]
    # 한도를 줄이면 한 단계씩(20 → 10 → 5 → 0개) 목록이 줄고 "… 외 N건"이 붙는다.
    shown_counts = []
    for max_chars in range(9000, 300, -50):
        body = build_pr_body(_input(reflected=items, changed_pages=[]), max_chars)
        assert len(body) <= max_chars
        section = body.split("## 바뀐")[0]
        shown_counts.append(section.count("- 항목"))
        if shown_counts[-1] < 100:
            assert f"- … 외 {100 - shown_counts[-1]}건" in section
    assert shown_counts == sorted(shown_counts, reverse=True)
    assert set(shown_counts) == {100, 20, 10, 5, 0}


def test_low_confidence_and_analysis_clips() -> None:
    body = build_pr_body(
        _input(low_confidence="가" * 5000, analyses=[Analysis("t.md", "나" * 5000)]), 100_000
    )
    assert "가" * 1500 + "…" in body and "가" * 1501 not in body
    assert "나" * 3000 + "…" in body and "나" * 3001 not in body
    tight = build_pr_body(
        _input(low_confidence="가", analyses=[Analysis("t.md", "나" * 5000)]), 600
    )
    assert "### t.md\n…" in tight  # 마지막 단계는 분석 글을 "…"로 줄인다
    assert len(tight) <= 600


def test_cost_and_counts_formatting() -> None:
    body = build_pr_body(_input(cost_usd=12.345, input_tokens=1234567, output_tokens=0), 4000)
    assert body.endswith("runId 20261007-0900 · 비용 $12.35 · 입력 1,234,567 / 출력 0 토큰") or (
        "비용 $12.34" in body
    )
    assert "입력 1,234,567 / 출력 0 토큰" in body


def test_dataclasses_are_frozen() -> None:
    inp = _input()
    with pytest.raises(FrozenInstanceError):
        inp.run_id = "x"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        Analysis("a", "b").title = "x"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        Delivered("pr", None).result = "direct"  # type: ignore[misc]


# ---------------------------------------------------------------- G13: 멘션


def test_neutralize_mentions_unit() -> None:
    assert neutralize_mentions("@everyone") == f"@{ZWSP}everyone"
    assert neutralize_mentions("@org/team a@b.com @_x @-y @9") == (
        f"@{ZWSP}org/team a@{ZWSP}b.com @{ZWSP}_x @{ZWSP}-y @{ZWSP}9"
    )
    assert neutralize_mentions("@ x @ @") == "@ x @ @"
    assert neutralize_mentions("끝 @") == "끝 @"
    assert neutralize_mentions("@@a") == f"@@{ZWSP}a"
    assert neutralize_mentions("") == ""
    once = neutralize_mentions("@a @b")
    assert neutralize_mentions(once) == once  # 멱등


def test_mentions_in_every_untrusted_field_are_neutralized() -> None:
    body = build_pr_body(
        _input(
            reflected=["@everyone 확인 (@member-a)"],
            changed_pages=["wiki/@org/team.md"],
            new_questions=["@member-b 에게 물어보기"],
            resolved_questions=["해소 @member-c"],
            analyses=[Analysis("@everyone-회의.md", "- cc @org/team 과 @member-d")],
            low_confidence="@here 와 @channel",
            lint_violations=[LintViolation("L2", "wiki/@x.md", "@everyone 깨진 링크")],
            warnings=["@admin 경고"],
            run_id="@run",
        ),
        100_000,
    )
    assert RAW_MENTION.search(body) is None
    for name in ("everyone", "member-a", "org/team", "member-d", "here", "channel", "admin"):
        assert f"@{ZWSP}{name}" in body


def test_zero_width_space_lengthening_never_exceeds_max_chars() -> None:
    items = ["@a" * 200] * 200  # 무력화하면 항목마다 200자가 늘어난다
    for max_chars in (80, 300, 1000, 2000, 5000, 20_000):
        body = build_pr_body(_input(reflected=items, changed_pages=items), max_chars)
        assert len(body) <= max_chars
        assert RAW_MENTION.search(body) is None


# ---------------------------------------------------------------- G13: 펜스


def test_fence_unit() -> None:
    assert fence("x") == "```\nx\n```"
    assert fence("") == "```\n\n```"
    assert fence("a```b") == "````\na```b\n````"
    assert fence("a`````b ``` c") == "``````\na`````b ``` c\n``````"
    assert fence("a``b") == "```\na``b\n```"


def test_violation_with_backticks_in_file_name_keeps_fence_intact() -> None:
    body = build_pr_body(
        _input(
            lint_violations=[
                LintViolation("L2", "wiki/a```b.md", "깨진 링크: ```` x"),
                LintViolation("L3", "wiki/ok.md", "정상"),
            ]
        ),
        100_000,
    )
    lines = body.split("\n")
    opens = [i for i, line in enumerate(lines) if re.fullmatch(r"`{3,}", line)]
    assert len(opens) == 2
    first, second = lines[opens[0]], lines[opens[1]]
    assert first == second
    inner = "\n".join(lines[opens[0] + 1 : opens[1]])
    longest = max(len(run) for run in re.findall(r"`+", inner))
    assert len(first) > longest >= 4
    assert "wiki/a```b.md" in inner and "위반 2건" in inner
    assert _open_fence(body) is None


def test_newline_in_violation_cannot_forge_a_fence_or_a_line() -> None:
    body = build_pr_body(
        _input(lint_violations=[LintViolation("L2", "a.md\n```\n## 가짜 절", "m\r\n```")]),
        100_000,
    )
    assert "## 가짜 절\n" not in body.replace("\\n## 가짜 절", "")
    assert [ln for ln in body.split("\n") if ln.startswith("## 가짜")] == []
    assert _open_fence(body) is None
    assert len([ln for ln in body.split("\n") if re.fullmatch(r"`{3,}", ln)]) == 2  # 여는·닫는


def test_many_violations_are_bounded_and_say_how_many() -> None:
    violations = [
        LintViolation("L2", f"wiki/f{i}.md", "깨진 링크: " + "x" * 50) for i in range(5000)
    ]
    body = build_pr_body(_input(lint_violations=violations), 6000)
    assert len(body) <= 6000
    assert body.startswith("> ⚠️ 점검 실패 5000건")
    assert "외 " in body
    assert _open_fence(body) is None
    assert "## 실행 정보" in body


def test_unclosed_fence_in_model_text_cannot_swallow_later_sections() -> None:
    body = build_pr_body(
        _input(
            low_confidence="- 의심\n```python\nprint(1)",
            analyses=[Analysis("a.md", "~~~~\n열린 채"), Analysis("b.md", "정상")],
        ),
        100_000,
    )
    assert _open_fence(body) is None
    assert body.index("### b.md") > body.index("### a.md")
    assert "## 실행 정보" in body


def test_cut_inside_fence_is_closed_or_dropped() -> None:
    violations = [LintViolation("L2", f"wiki/f{i}.md", "m" * 40) for i in range(20)]
    seen_cut = False
    for max_chars in range(0, 700, 7):
        body = build_pr_body(_input(lint_violations=violations), max_chars)
        assert len(body) <= max_chars
        assert _open_fence(body) is None
        seen_cut = seen_cut or "잘렸습니다" in body
    assert seen_cut


# ---------------------------------------------------------------- 정리(제어·방향 문자)


def test_control_and_bidi_characters_are_escaped_and_cannot_forge_lines() -> None:
    body = build_pr_body(
        _input(
            reflected=["a\n## 가짜 절\n- b\x1b[31m‮ \x85\x00z"],
            analyses=[Analysis("t\n## 제목.md", "x‮y⁦z\r\n‏w\x07")],
            low_confidence="l‮c\x1b",
            warnings=["w\r\n- 가짜"],
            run_id="r\n1",
        ),
        100_000,
    )
    for bad in ("\x1b", "‮", " ", "\x85", "\x00", "\x07", "⁦", "‏", "\r"):
        assert bad not in body
    assert [ln for ln in body.split("\n") if ln.startswith("## 가짜")] == []
    assert [ln for ln in body.split("\n") if ln == "- 가짜"] == []
    assert "\\u202e" in body and "\\x1b" in body
    assert "- a\\n## 가짜 절\\n- b" in body


def test_item_length_is_bounded() -> None:
    body = build_pr_body(_input(reflected=["x" * 50_000]), 100_000)
    assert len(body) < 3000


# ---------------------------------------------------------------- 크기 상한


def test_huge_inputs_stay_within_max_chars() -> None:
    inp = _input(
        reflected=[f"항목 @m{i} " + "가" * 100 for i in range(10_000)],
        changed_pages=[f"wiki/p{i}.md" for i in range(10_000)],
        new_questions=[f"q{i} @z" for i in range(10_000)],
        resolved_questions=[f"r{i}" for i in range(10_000)],
        analyses=[Analysis(f"{i}.md", "나\n" * 50) for i in range(10_000)],
        warnings=[f"w{i}" for i in range(10_000)],
        lint_violations=[LintViolation("L1", f"f{i}", "m @a") for i in range(10_000)],
    )
    for max_chars in (65_000, 4000, 300, 50, 1, 0):
        body = build_pr_body(inp, max_chars)
        assert len(body) <= max_chars
        assert _open_fence(body) is None
    body = build_pr_body(inp, 65_000)
    assert "## 확신이 낮은 부분" in body and "## 실행 정보" in body


def test_one_analysis_of_half_a_million_chars() -> None:
    inp = _input(analyses=[Analysis("big.md", "- 줄 @a ```\n" * 40_000)])
    for max_chars in (65_000, 2000, 100):
        body = build_pr_body(inp, max_chars)
        assert len(body) <= max_chars
        assert _open_fence(body) is None
        assert RAW_MENTION.search(body) is None
    assert "## 실행 정보" in build_pr_body(inp, 4000)


# ---------------------------------------------------------------- deliver


@dataclass
class Env:
    root: Path
    bare: Path
    seed: GitClient
    git: GitClient
    head: str
    state: Path
    port: LocalPrPort


@pytest_asyncio.fixture
async def env(tmp_dir: Path) -> Env:
    bare = tmp_dir / "data.git"
    seed = await seed_bare(bare, tmp_dir / "seed", {"raw/a.md": "a"})
    clone = tmp_dir / "data-sync"
    git = GitClient(clone, allowed_protocols="file", timeout_s=60.0)
    await ensure_clone(git, str(bare))
    await fetch_branch(git, "main")
    await reset_branch_to(git, "sync/code-1", "origin/main")
    write_tree(clone, {"wiki/index.md": "x"})
    head = await commit_all(git, "w")
    assert head is not None
    port = LocalPrPort(
        directory=tmp_dir / "prs",
        data_git=git,
        base="main",
        now=lambda: datetime(2026, 10, 7, tzinfo=UTC),
    )
    return Env(tmp_dir, bare, seed, git, head, tmp_dir / "state", port)


def _bare(env: Env) -> GitClient:
    return GitClient(env.bare, allowed_protocols="file", timeout_s=60.0)


async def _remote_sha(env: Env, ref: str) -> str | None:
    result = await _bare(env).run(["rev-parse", "--verify", "-q", ref], allow_codes=(1,))
    return result.text.strip() if result.code == 0 else None


def _args(env: Env, **extra: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "git": env.git,
        "kind": "code",
        "run_id": RUN_ID,
        "branch": "sync/code-1",
        "base": "main",
        "title": "T",
        "body": "B",
        "lint_failed": False,
        "review_mode": "pr",
        "pr_port": env.port,
        "state_dir": env.state,
    }
    fields.update(extra)
    return fields


async def test_pr_mode_pushes_branch_opens_pr_and_tracks(env: Env) -> None:
    result = await deliver(**_args(env))
    assert result == Delivered(result="pr", pr_id=f"{RUN_ID}-code.md")
    assert await _remote_sha(env, "sync/code-1") == env.head
    assert await _remote_sha(env, "main") != env.head  # main은 그대로
    assert read_tracking(env.state) == [
        TrackedPr(id=f"{RUN_ID}-code.md", kind="code", branch="sync/code-1", bot_head_sha=env.head)
    ]
    pr_file = env.root / "prs" / f"{RUN_ID}-code.md"
    assert pr_file.is_file()
    parsed = parse_frontmatter(pr_file.read_text(encoding="utf-8"))
    assert parsed.data is not None and parsed.data["lint_failed"] is False


async def test_pr_mode_appends_to_existing_tracking(env: Env) -> None:
    await deliver(**_args(env))
    await reset_branch_to(env.git, "sync/ingest-1", "origin/main")
    write_tree(env.git.cwd, {"wiki/y.md": "y"})
    assert await commit_all(env.git, "y") is not None
    second = await deliver(
        **_args(env, kind="ingest", run_id="20261007-1200", branch="sync/ingest-1")
    )
    assert second.pr_id == "20261007-1200-ingest.md"
    assert [pr.id for pr in read_tracking(env.state)] == [
        f"{RUN_ID}-code.md",
        "20261007-1200-ingest.md",
    ]


async def test_pr_mode_marks_lint_failed(env: Env) -> None:
    await deliver(**_args(env, lint_failed=True))
    text = (env.root / "prs" / f"{RUN_ID}-code.md").read_text(encoding="utf-8")
    parsed = parse_frontmatter(text)
    assert parsed.data is not None and parsed.data["lint_failed"] is True


async def test_direct_mode_pushes_to_main(env: Env) -> None:
    result = await deliver(**_args(env, review_mode="direct", pr_port=None))
    assert result == Delivered(result="direct", pr_id=None)
    assert await _remote_sha(env, "main") == env.head
    assert await _remote_sha(env, "sync/code-1") is None  # 작업 브랜치는 올리지 않는다
    assert read_tracking(env.state) == []


async def test_pr_mode_without_port_fails(env: Env) -> None:
    with pytest.raises(ValueError, match="PR 포트"):
        await deliver(**_args(env, pr_port=None))
    assert await _remote_sha(env, "sync/code-1") is None  # push 전에 실패한다


async def test_direct_mode_refuses_lint_failed_without_pushing(env: Env) -> None:
    refs_before = (await _bare(env).run(["for-each-ref"])).text
    with pytest.raises(ValueError):
        await deliver(**_args(env, review_mode="direct", pr_port=None, lint_failed=True))
    assert (await _bare(env).run(["for-each-ref"])).text == refs_before


async def test_direct_mode_never_force_pushes_when_main_moved(env: Env) -> None:
    other = await commit_files(env.seed, {"raw/b.md": "b"}, "다른 사람")
    await env.seed.run(["push", "-q", "origin", "main"])
    with pytest.raises(GitError):
        await deliver(**_args(env, review_mode="direct", pr_port=None))
    assert await _remote_sha(env, "main") == other  # 원격 main은 그대로
    # 반영한 변경은 로컬에 남아 있다(호출자가 단계를 실패 처리해도 잃지 않는다).
    assert (await env.git.run(["rev-parse", "HEAD"])).text.strip() == env.head
    assert read_tracking(env.state) == []


async def test_pr_mode_push_failure_does_not_open_a_pr(env: Env) -> None:
    other = await commit_files(env.seed, {"raw/b.md": "b"}, "x")
    await env.seed.run(["push", "-q", "origin", f"{other}:refs/heads/sync/code-1"])
    with pytest.raises(GitError):
        await deliver(**_args(env))  # 같은 이름의 브랜치가 앞서 있어 빨리 감기가 아니다
    assert not (env.root / "prs").exists()
    assert read_tracking(env.state) == []
    assert await _remote_sha(env, "sync/code-1") == other


@pytest.mark.parametrize("mode", ["pr", "direct"])
@pytest.mark.parametrize("field", ["branch", "base"])
@pytest.mark.parametrize("bad", ["-x", "a..b", "", "refs/heads/x", "x\n", "main.lock"])
async def test_unsafe_branch_fails_before_any_push(
    env: Env, mode: str, field: str, bad: str
) -> None:
    refs_before = (await _bare(env).run(["for-each-ref"])).text
    with pytest.raises(ValueError):
        await deliver(**_args(env, review_mode=mode, **{field: bad}))
    assert (await _bare(env).run(["for-each-ref"])).text == refs_before
    assert read_tracking(env.state) == []
    assert not (env.root / "prs").exists()


async def test_invalid_arguments_fail_before_any_push(env: Env) -> None:
    refs_before = (await _bare(env).run(["for-each-ref"])).text
    for extra in (
        {"lint_failed": 1},
        {"lint_failed": None},
        {"kind": "other"},
        {"review_mode": "both"},
        {"title": 5},
        {"body": None},
    ):
        with pytest.raises(ValueError):
            await deliver(**_args(env, **extra))
    assert (await _bare(env).run(["for-each-ref"])).text == refs_before


class _RecordingPort:
    def __init__(self) -> None:
        self.requests: list[OpenPrRequest] = []

    async def find_open(self, kind: str) -> str | None:  # pragma: no cover - 쓰이지 않는다
        return None

    async def open(self, req: OpenPrRequest) -> TrackedPr:
        self.requests.append(req)
        return TrackedPr(
            id=f"{req.run_id}-{req.kind}.md",
            kind=req.kind,
            branch=req.branch,
            bot_head_sha=req.bot_head_sha,
        )

    async def outcome(self, pr: TrackedPr) -> Any:  # pragma: no cover
        raise AssertionError

    async def fetch_final_head(self, pr: TrackedPr) -> None:  # pragma: no cover
        raise AssertionError


async def test_title_and_body_are_made_safe_before_the_port_sees_them(env: Env) -> None:
    port = _RecordingPort()
    title = "@everyone 긴급\n## 가짜‮\x1b " + "가" * 1000
    body = "본문 @member-a 와 @org/team\n끝"
    await deliver(**_args(env, title=title, body=body, pr_port=port))
    (req,) = port.requests
    assert "\n" not in req.title and "‮" not in req.title and "\x1b" not in req.title
    assert len(req.title) <= 300
    assert req.title.startswith(f"@{ZWSP}everyone")
    assert req.body == f"본문 @{ZWSP}member-a 와 @{ZWSP}org/team\n끝"
    assert req.bot_head_sha == env.head and req.lint_failed is False


async def test_title_edge_cases(env: Env) -> None:
    for raw, expected in (
        ("", "위키 동기화"),
        ("\n \t", "위키 동기화"),
        ("  T  \n x ", "T  \\n x"),
    ):
        port = _RecordingPort()
        await deliver(**_args(env, title=raw, pr_port=port))
        assert port.requests[0].title == expected or raw.strip() in ("", "T  \n x")
        assert port.requests[0].title == expected
    port = _RecordingPort()
    await deliver(**_args(env, title="@a" * 400, pr_port=port))
    assert len(port.requests[0].title) <= 300  # 무력화로 늘어난 뒤 기준이다
    assert RAW_MENTION.search(port.requests[0].title) is None


async def test_pr_mode_rejects_work_branch_equal_to_base(env: Env) -> None:
    with pytest.raises(ValueError):
        await deliver(**_args(env, branch="main"))
    assert await _remote_sha(env, "main") != env.head


async def test_pr_mode_rejects_bad_run_id_before_push(env: Env) -> None:
    with pytest.raises(ValueError):
        await deliver(**_args(env, run_id="x\n"))
    assert await _remote_sha(env, "sync/code-1") is None


async def test_local_pr_file_stores_neutralized_single_line_title(env: Env) -> None:
    await deliver(**_args(env, title="@everyone\nfoo", body="@member-a"))
    parsed = parse_frontmatter((env.root / "prs" / f"{RUN_ID}-code.md").read_text("utf-8"))
    assert parsed.data is not None
    assert parsed.data["title"] == f"@{ZWSP}everyone\\nfoo"
    assert f"@{ZWSP}member-a" in parsed.body


async def test_cancel_during_push_lets_the_push_finish(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """push 도중 취소가 와도 push는 끝까지 돈다(원격에 잠금이 남지 않게). 그 뒤 PR은 열지 않는다."""
    started = asyncio.Event()
    finished: list[str] = []
    real_push = push

    async def slow_push(git: GitClient, local_ref: str, remote_branch: str) -> None:
        started.set()
        await asyncio.sleep(0.3)
        await real_push(git, local_ref, remote_branch)
        finished.append(remote_branch)

    monkeypatch.setattr("team_wiki_engine.sync.pr.push", slow_push)
    port = _RecordingPort()
    task = asyncio.ensure_future(deliver(**_args(env, pr_port=port)))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished == ["sync/code-1"]
    assert await _remote_sha(env, "sync/code-1") == env.head
    assert port.requests == []
    assert read_tracking(env.state) == []


def test_module_exports_and_signature_types() -> None:
    assert callable(extract_section) and callable(neutralize_mentions) and callable(fence)
    seq: Sequence[str] = ["a"]
    assert _input(reflected=seq).reflected == seq


# ---------------------------------------------------------------- 검토 1차 수정: 적대적 글


def _fence_attack_cases() -> list[str]:
    return [
        "    ```\n```\nrest",
        "    ~~~\n~~~",
        "- a\n    ```\n```",
        "> ```\n```\nrest",
        "1. ```\nx\n```",
        "\t```\n```\nrest",
        "- - ```\nx",
        "```\n~~~\n````",
        "  ~~~~ js\ntext",
    ]


@pytest.mark.parametrize("text", _fence_attack_cases())
def test_model_text_cannot_open_or_close_a_fence(text: str) -> None:
    body = build_pr_body(
        _input(
            low_confidence=text,
            analyses=[Analysis("a.md", text), Analysis("b.md", "정상 b")],
            reflected=[text],
            warnings=[text],
            lint_violations=[LintViolation("L2", "wiki/a.md", "m")],
        ),
        100_000,
    )
    assert _open_fence(body) is None
    assert body.index("### b.md") < body.index("## 확신이 낮은 부분")
    assert "## 실행 정보" in body
    # 우리 펜스(점검 위반)만 남는다: 줄 맨 앞의 펜스 줄은 정확히 2개.
    assert len([ln for ln in body.split("\n") if re.match(r"\s*(`{3,}|~{3,})", ln)]) == 2


def test_fence_fuzz_never_leaves_a_fence_open() -> None:
    rng = random.Random(20261007)
    pieces = [
        "",
        " ",
        "  ",
        "   ",
        "    ",
        "\t",
        "- ",
        "* ",
        "+ ",
        "1. ",
        "2) ",
        "> ",
        ">",
        "```",
        "````",
        "~~~",
        "~~~~",
        "``",
        "a",
        "x y",
        "# ",
        "---",
        "===",
        "\n",
        "\r\n",
    ]
    sizes = (100_000, 3000, 900, 500, 200, 60)
    for case in range(200_000):
        parts = [rng.choice(pieces) for _ in range(rng.randint(1, 14))]
        text = "".join(parts)
        shape = case % 4
        if shape == 0:
            inp = _input(low_confidence=text, analyses=[Analysis("t", text)])
        elif shape == 1:
            inp = _input(reflected=[text], warnings=[text], low_confidence=None)
        elif shape == 2:
            inp = _input(analyses=[Analysis(text, text)], new_questions=[text])
        else:
            inp = _input(
                low_confidence=text,
                lint_violations=[LintViolation("L2", text or "f", text)],
            )
        max_chars = sizes[case % len(sizes)] if case % 7 else rng.randint(0, 400)
        body = build_pr_body(inp, max_chars)
        assert len(body) <= max_chars
        assert _open_fence(body) is None, (case, text, body)


# ---------------------------------------------------------------- 이슈 닫기 키워드


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Closes #5", f"Closes{ZWSP} #5"),
        ("fixes org/other#3", f"fixes{ZWSP} org/other#3"),
        ("resolved: #9", f"resolved{ZWSP}: #9"),
        ("FIX  #1", f"FIX{ZWSP}  #1"),
        (
            "a\nclose\n#2\nfixed owner/r.x-y#7 end",
            f"a\nclose{ZWSP}\n#2\nfixed{ZWSP} owner/r.x-y#7 end",
        ),
        (
            "Resolves https://github.com/o/r/issues/4",
            f"Resolves{ZWSP} https://github.com/o/r/issues/4",
        ),
        ("fixes the bug #5", "fixes the bug #5"),
        ("prefix #1 closet #2 fixing #3", "prefix #1 closet #2 fixing #3"),
        ("closes #", "closes #"),
    ],
)
def test_closing_keywords_are_neutralized(raw: str, expected: str) -> None:
    assert neutralize_mentions(raw) == expected
    assert neutralize_mentions(expected) == expected  # 멱등


def test_closing_keywords_in_body_and_title() -> None:
    body = build_pr_body(
        _input(
            reflected=["Closes #5 (member)", "fixes org/other#3"], low_confidence="resolved: #9"
        ),
        100_000,
    )
    for bad in ("Closes #5", "fixes org/other#3", "resolved: #9"):
        assert bad not in body
    assert f"Closes{ZWSP} #5" in body


async def test_closing_keywords_in_title_and_body_at_deliver(env: "Env") -> None:
    port = _RecordingPort()
    await deliver(**_args(env, title="Fixes #12 now", body="x\nCloses  #4", pr_port=port))
    (req,) = port.requests
    assert req.title == f"Fixes{ZWSP} #12 now"
    assert req.body == f"x\nCloses{ZWSP}  #4"


@pytest.mark.parametrize(
    "hostile",
    [
        "fix" + " " * 5_000_000 + "x",
        "fix " * 1_250_000,
        "fix" + ":" * 5_000_000,
        "fix" + "\n" * 5_000_000,
        "fix " + "a" * 5_000_000,
        "fix " + "a/" * 2_500_000,
        "fix " + "a." * 2_500_000 + "/",
        "close" * 1_000_000,
        "@" * 5_000_000,
        "fix @ " * 800_000,
    ],
)
def test_neutralize_mentions_is_linear_on_hostile_input(hostile: str) -> None:
    start = time.monotonic()
    out = neutralize_mentions(hostile)
    assert time.monotonic() - start < 5.0
    assert len(out) >= len(hostile)


# ---------------------------------------------------------------- 원시 HTML·구조 위조


def test_raw_html_is_escaped_in_untrusted_text() -> None:
    texts = ["<!-- 숨김 -->", "<script>alert(1)</script>", "<img src=x onerror=y>", "a < b & c"]
    body = build_pr_body(
        _input(
            reflected=texts,
            low_confidence="\n".join(texts),
            analyses=[Analysis("<b>t</b>.md", "\n".join(texts))],
            warnings=texts,
        ),
        100_000,
    )
    assert "<" not in body.replace("<생략>", "")
    assert "&lt;!-- 숨김 -->" in body and "&lt;script>" in body
    assert "a &lt; b &amp; c" in body


def test_image_beacons_are_defused() -> None:
    body = build_pr_body(
        _input(
            reflected=["![](https://evil/x.png)"],
            low_confidence="앞 ![alt](https://evil/y.png) 뒤\n![][ref]",
            analyses=[Analysis("![t](https://evil/z.png)", "![](https://evil/w.png)")],
        ),
        100_000,
    )
    assert "![" not in body
    assert f"!{ZWSP}[" in body


def test_forged_headings_in_model_text_do_not_create_sections() -> None:
    forged = (
        "## 실행 정보\nrunId 가짜 · 비용 $0.00\n  ### 가짜\n- ## 목록 안 제목\n"
        "> # 인용 안\n실행 정보\n---\n==="
    )
    body = build_pr_body(
        _input(
            low_confidence=forged, analyses=[Analysis("a.md", forged)], reflected=["# 제목\n## x"]
        ),
        100_000,
    )
    lines = body.split("\n")
    assert lines.count("## 실행 정보") == 1
    assert [
        ln
        for ln in lines
        if re.match(r"\s*(>\s*)*(-\s+)?#{1,6}(\s|$)", ln) and not ln.startswith(("## ", "### a.md"))
    ] == []
    assert not any(re.fullmatch(r" {0,3}(=+|-+)\s*", ln) for ln in lines)
    headings = [ln for ln in lines if ln.startswith("#")]
    assert headings == [
        "## 반영한 것",
        "## 바뀐 위키 페이지",
        "## 미결",
        "## 문서별 분석",
        "### a.md",
        "## 확신이 낮은 부분",
        "## 실행 정보",
    ]
    assert body.rindex("runId 20261007-0900") > body.rindex("runId 가짜")


# ---------------------------------------------------------------- HTML 엔티티 멘션 우회


def test_html_entities_cannot_smuggle_mentions() -> None:
    body = build_pr_body(
        _input(
            reflected=["&#64;org/team &commat;x &#x40;everyone"],
            low_confidence="&#64;org/team\n&commat;x",
            analyses=[Analysis("a.md", "&#64;member-a")],
        ),
        100_000,
    )
    assert "&#" not in body.replace("&amp;#", "") and "&commat;" not in body.replace(
        "&amp;commat;", ""
    )
    assert "&amp;#64;org/team" in body and "&amp;commat;x" in body


# ---------------------------------------------------------------- 비밀값 가리기


def test_secrets_in_untrusted_text_are_masked() -> None:
    ant = "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
    ghp = "ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789"
    body = build_pr_body(
        _input(
            reflected=[f"커밋 {ghp} 끝"],
            analyses=[Analysis("a.md", f"- 키 {ant}\n- 토큰 {ghp}\n" + "y " * 3000 + ant)],
            low_confidence=f"password: {ant}",
            warnings=[f"web {ghp}"],
        ),
        100_000,
    )
    assert ant not in body and ghp not in body
    assert "AbCdEfGhIjKl" not in body and "aBcDeFgHiJkL" not in body


async def test_title_secret_is_masked(env: "Env") -> None:
    ghp = "ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789"
    port = _RecordingPort()
    await deliver(**_args(env, title=f"토큰 {ghp} 노출", pr_port=port))
    assert ghp not in port.requests[0].title
    assert "aBcDeFgHiJkL" not in port.requests[0].title


async def test_title_ampersand_and_angle_brackets_are_left_alone(env: "Env") -> None:
    port = _RecordingPort()
    await deliver(**_args(env, title="A & B <c>", pr_port=port))
    assert port.requests[0].title == "A & B <c>"  # 제목은 글자로 보여 주므로 엔티티로 바꾸지 않는다


# ---------------------------------------------------------------- 기록한 SHA를 push


async def test_deliver_pushes_the_recorded_sha_not_head(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, str]] = []
    real_push = push

    async def spy(git: GitClient, local_ref: str, remote_branch: str) -> None:
        seen.append((local_ref, remote_branch))
        await real_push(git, local_ref, remote_branch)

    monkeypatch.setattr("team_wiki_engine.sync.pr.push", spy)
    await deliver(**_args(env))
    assert seen == [(env.head, "sync/code-1")]
    seen.clear()
    await deliver(**_args(env, review_mode="direct", pr_port=None))
    assert seen == [(env.head, "main")]


def test_structure_scan_is_fast_on_hostile_lines() -> None:
    hostile = [
        " " * 9000 + "```",
        "> " * 4500 + "```",
        "- " * 4500 + "# x",
        "1. " * 3000,
        "\t" * 9000,
    ]
    start = time.monotonic()
    body = build_pr_body(_input(low_confidence="\n".join(hostile), reflected=hostile), 100_000)
    assert time.monotonic() - start < 5.0
    assert _open_fence(body) is None

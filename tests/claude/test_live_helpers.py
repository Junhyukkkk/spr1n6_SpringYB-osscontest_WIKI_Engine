"""라이브 스모크 테스트가 쓰는 도우미의 단위 테스트. API·키 없이 돈다."""

import asyncio
import copy
import os
import time
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any, cast

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    HookContext,
    HookInput,
    HookJSONOutput,
    HookMatcher,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolPermissionContext,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from team_wiki_engine.claude.runner import (
    RunnerDeps,
    build_options,
    create_claude_runner,
    scrub_env,
)
from team_wiki_engine.claude.types import RunClaudeInput
from tests.claude.test_live_smoke import assert_no_leak_anywhere
from tests.helpers import write_tree
from tests.helpers.instrument import Instrumentation, make_query_fn, merged_env_secret_keys
from tests.helpers.live_gate import PINNED_SDK, usage_regressions, watch_new_files
from tests.helpers.transcripts import (
    changed_since,
    claude_config_base,
    claude_projects_dir,
    cleanup_created,
    locate_transcript,
    memory_files,
    missing_dirs_between,
    read_head,
)

# ---------------------------------------------------------------- 세션 기록 탐색


def test_session_id_file_is_preferred(tmp_dir: Path) -> None:
    write_tree(
        tmp_dir,
        {
            "proj-a/sess-1.jsonl": "MARK-1 by id",
            "proj-b/other.jsonl": "MARK-1 newer but other",
        },
    )
    found = locate_transcript(tmp_dir, session_id="sess-1", marker="MARK-1", since_ms=0)
    assert found == tmp_dir / "proj-a" / "sess-1.jsonl"


def test_without_matching_session_id_only_files_since_are_scanned_newest_first(
    tmp_dir: Path,
) -> None:
    write_tree(
        tmp_dir,
        {
            "proj-a/old.jsonl": "MARK-1 old",
            "proj-a/mid.jsonl": "MARK-1 mid",
            "proj-b/new.jsonl": "MARK-1 new",
            "proj-b/new.txt": "MARK-1 not jsonl",
        },
    )
    os.utime(tmp_dir / "proj-a" / "old.jsonl", (1000, 1000))
    os.utime(tmp_dir / "proj-a" / "mid.jsonl", (3000, 3000))
    os.utime(tmp_dir / "proj-b" / "new.jsonl", (4000, 4000))
    assert locate_transcript(tmp_dir, session_id=None, marker="MARK-1", since_ms=5_000_000) is None
    expected = tmp_dir / "proj-b" / "new.jsonl"
    assert (
        locate_transcript(tmp_dir, session_id="nope", marker="MARK-1", since_ms=3_500_000)
        == expected
    )
    assert (
        locate_transcript(tmp_dir, session_id=None, marker="MARK-1", since_ms=2_500_000) == expected
    )


def test_old_files_are_ignored_even_with_the_marker_and_missing_dir_gives_none(
    tmp_dir: Path,
) -> None:
    write_tree(tmp_dir, {"proj/a.jsonl": "MARK-2"})
    os.utime(tmp_dir / "proj" / "a.jsonl", (1, 1))
    assert locate_transcript(tmp_dir, session_id=None, marker="MARK-2", since_ms=5000) is None
    missing = tmp_dir / "missing"
    assert locate_transcript(missing, session_id="x", marker="MARK-2", since_ms=0) is None


def test_marker_beyond_the_first_64kb_is_not_seen(tmp_dir: Path) -> None:
    write_tree(tmp_dir, {"proj/s.jsonl": "a" * (70 * 1024) + "MARK-3"})
    assert locate_transcript(tmp_dir, session_id="s", marker="MARK-3", since_ms=0) is None
    head = read_head(tmp_dir / "proj" / "s.jsonl")
    assert head is not None
    assert len(head.encode("utf-8")) == 64 * 1024
    assert read_head(tmp_dir / "nope") is None


def test_marker_inside_the_first_64kb_is_found(tmp_dir: Path) -> None:
    write_tree(tmp_dir, {"proj/s.jsonl": "MARK-5" + "a" * (70 * 1024)})
    found = locate_transcript(tmp_dir, session_id="s", marker="MARK-5", since_ms=0)
    assert found == tmp_dir / "proj" / "s.jsonl"


def test_session_id_that_escapes_the_slug_dir_is_rejected(tmp_dir: Path) -> None:
    # <projects>/slug/../outer.jsonl 은 <projects>/outer.jsonl 이다. 정규식 검증이 없으면
    # 이 파일이 잡힌다(그래서 이 단언은 공허하지 않다).
    write_tree(tmp_dir, {"proj/slug/inside.jsonl": "nothing", "proj/outer.jsonl": "MARK-4"})
    projects = tmp_dir / "proj"
    assert (projects / "slug" / ".." / "outer.jsonl").is_file()
    future = 10**13  # 먼 미래: since 폴백이 우연히 outer.jsonl을 고르지 않게 한다
    found = locate_transcript(projects, session_id="../outer", marker="MARK-4", since_ms=future)
    assert found is None
    # 같은 구조에서 정상 ID는 찾는다(대조).
    write_tree(tmp_dir, {"proj/slug/ok-id.jsonl": "MARK-4"})
    found_ok = locate_transcript(projects, session_id="ok-id", marker="MARK-4", since_ms=future)
    assert found_ok == projects / "slug" / "ok-id.jsonl"


@pytest.mark.parametrize("bad", ["", "a/b", "a b", "a.b", "..", "x\n", "한글"])
def test_other_unsafe_session_ids_are_not_used_for_the_direct_lookup(
    tmp_dir: Path, bad: str
) -> None:
    write_tree(tmp_dir, {"proj/slug/inside.jsonl": "nothing", "proj/outer.jsonl": "MARK-6"})
    projects = tmp_dir / "proj"
    assert locate_transcript(projects, session_id=bad, marker="MARK-6", since_ms=10**13) is None


# ---------------------------------------------------------------- 설정 폴더 기준


def test_config_base_honours_claude_config_dir() -> None:
    assert claude_config_base({"CLAUDE_CONFIG_DIR": "/x/cfg"}) == Path("/x/cfg")
    assert claude_projects_dir({"CLAUDE_CONFIG_DIR": "/x/cfg"}) == Path("/x/cfg/projects")
    assert claude_config_base({"CLAUDE_CONFIG_DIR": "  /x/cfg \n"}) == Path("/x/cfg")


def test_config_base_falls_back_to_home_dot_claude() -> None:
    expected = Path(os.path.expanduser("~")) / ".claude"
    assert claude_config_base({}) == expected
    assert claude_config_base({"CLAUDE_CONFIG_DIR": "  "}) == expected
    assert claude_config_base({"CLAUDE_CONFIG_DIR": ""}) == expected


def test_config_base_resolves_a_relative_value_to_absolute() -> None:
    base = claude_config_base({"CLAUDE_CONFIG_DIR": "rel/cfg"})
    assert base.is_absolute()
    assert base == Path(os.path.abspath("rel/cfg"))


def test_config_base_reads_the_process_environment_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/from/env")
    assert claude_config_base() == Path("/from/env")
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    assert claude_config_base() == Path(os.path.expanduser("~")) / ".claude"


# ---------------------------------------------------------------- 정리 도우미(비재귀)


def test_missing_dirs_between_lists_only_dirs_that_do_not_exist_shallowest_first(
    tmp_dir: Path,
) -> None:
    (tmp_dir / "base" / "projects").mkdir(parents=True)
    target = tmp_dir / "base" / "projects" / "slug" / "memory" / "MEMORY.md"
    missing = missing_dirs_between(target, tmp_dir / "base")
    assert missing == [tmp_dir / "base" / "projects" / "slug", target.parent]


def test_missing_dirs_between_includes_base_when_it_is_missing(tmp_dir: Path) -> None:
    base = tmp_dir / "cfg"
    target = base / "CLAUDE.md"
    assert missing_dirs_between(target, base) == [base]


def test_missing_dirs_between_rejects_a_target_outside_base(tmp_dir: Path) -> None:
    with pytest.raises(ValueError):
        missing_dirs_between(tmp_dir / "a" / "f.md", tmp_dir / "b")


def test_cleanup_removes_created_files_and_empty_new_dirs_only(tmp_dir: Path) -> None:
    base = tmp_dir / "cfg"
    (base / "projects").mkdir(parents=True)
    keep = base / "projects" / "keep.jsonl"
    keep.write_text("user data", encoding="utf-8")
    target = base / "projects" / "slug" / "memory" / "MEMORY.md"
    new_dirs = missing_dirs_between(target, base)
    target.parent.mkdir(parents=True)
    target.write_text("x", encoding="utf-8")
    cleanup_created([target], new_dirs)
    assert not target.parent.exists()
    assert not (base / "projects" / "slug").exists()
    assert keep.read_text(encoding="utf-8") == "user data"
    assert (base / "projects").is_dir()


def test_cleanup_never_removes_a_non_empty_dir_or_foreign_files(tmp_dir: Path) -> None:
    base = tmp_dir / "cfg"
    target = base / "projects" / "slug" / "MEMORY.md"
    new_dirs = missing_dirs_between(target, base)
    target.parent.mkdir(parents=True)
    stranger = target.parent / "someone-elses.md"
    stranger.write_text("keep me", encoding="utf-8")
    cleanup_created([target], new_dirs)  # target은 없다(정상), 폴더는 비어 있지 않다
    assert stranger.read_text(encoding="utf-8") == "keep me"
    assert target.parent.is_dir()


def test_cleanup_does_not_follow_a_symlinked_dir(tmp_dir: Path) -> None:
    precious = tmp_dir / "precious"
    precious.mkdir()
    (precious / "f.txt").write_text("keep", encoding="utf-8")
    base = tmp_dir / "cfg"
    base.mkdir()
    link = base / "linked"
    link.symlink_to(precious, target_is_directory=True)
    cleanup_created([], [link])  # 링크는 rmdir로 지워지지 않고 대상은 그대로
    assert (precious / "f.txt").read_text(encoding="utf-8") == "keep"


def test_cleanup_ignores_missing_files_and_dirs(tmp_dir: Path) -> None:
    cleanup_created([tmp_dir / "no-file"], [tmp_dir / "no-dir"])


# ---------------------------------------------------------------- 계측 래퍼

_CONTEXT = cast("HookContext", None)
_PERM_CONTEXT = ToolPermissionContext()


def _hook_input(tool: str, tool_input: dict[str, Any]) -> HookInput:
    return cast(
        "HookInput",
        {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input},
    )


def _options_with(
    hook: Any,
    can_use_tool: Any = None,
    *,
    extra_matcher: bool = False,
) -> ClaudeAgentOptions:
    matchers = [HookMatcher(hooks=[hook, hook])]
    if extra_matcher:
        matchers.append(HookMatcher(matcher="Read", hooks=[hook]))
    return ClaudeAgentOptions(
        hooks={"PreToolUse": matchers},
        can_use_tool=can_use_tool,
        max_turns=30,
        model="m",
    )


async def test_hook_wrapper_passes_args_through_and_returns_the_result_unchanged() -> None:
    seen: list[tuple[object, object, object]] = []
    result: HookJSONOutput = {"hookSpecificOutput": {"hookEventName": "PreToolUse"}}

    async def orig(data: HookInput, tool_use_id: str | None, ctx: HookContext) -> HookJSONOutput:
        seen.append((data, tool_use_id, ctx))
        return result

    instr = Instrumentation()
    wrapped = instr.wrap_options(_options_with(orig))
    assert wrapped.hooks is not None
    wrapper = wrapped.hooks["PreToolUse"][0].hooks[0]
    data = _hook_input("Read", {"file_path": "/a/b"})
    out = await wrapper(data, "tu-1", _CONTEXT)
    assert out is result
    assert seen[0][0] is data
    assert seen[0][1] == "tu-1"
    assert seen[0][2] is _CONTEXT
    assert [(c.tool, c.tool_input) for c in instr.hook_calls] == [("Read", {"file_path": "/a/b"})]


async def test_hook_wrapper_records_a_snapshot_not_a_live_reference() -> None:
    async def orig(data: HookInput, tool_use_id: str | None, ctx: HookContext) -> HookJSONOutput:
        return {}

    instr = Instrumentation()
    wrapped = instr.wrap_options(_options_with(orig))
    assert wrapped.hooks is not None
    tool_input: dict[str, Any] = {"file_path": " /x", "nested": {"a": 1}}
    await wrapped.hooks["PreToolUse"][0].hooks[0](_hook_input("Read", tool_input), None, _CONTEXT)
    tool_input["file_path"] = "/changed"
    tool_input["nested"]["a"] = 2
    assert instr.hook_calls[0].tool_input == {"file_path": " /x", "nested": {"a": 1}}


async def test_hook_wrapper_records_then_propagates_exceptions_from_the_original() -> None:
    async def orig(data: HookInput, tool_use_id: str | None, ctx: HookContext) -> HookJSONOutput:
        raise RuntimeError("guard blew up")

    instr = Instrumentation()
    wrapped = instr.wrap_options(_options_with(orig))
    assert wrapped.hooks is not None
    with pytest.raises(RuntimeError, match="guard blew up"):
        await wrapped.hooks["PreToolUse"][0].hooks[0](_hook_input("Read", {}), None, _CONTEXT)
    assert len(instr.hook_calls) == 1


async def test_hook_wrapper_tolerates_malformed_hook_input() -> None:
    async def orig(data: HookInput, tool_use_id: str | None, ctx: HookContext) -> HookJSONOutput:
        return {}

    instr = Instrumentation()
    wrapped = instr.wrap_options(_options_with(orig))
    assert wrapped.hooks is not None
    weird = cast("HookInput", {"tool_name": 7, "tool_input": "not a dict"})
    await wrapped.hooks["PreToolUse"][0].hooks[0](weird, None, _CONTEXT)
    assert instr.hook_calls[0].tool == "7"
    assert instr.hook_calls[0].tool_input == {}


async def test_can_use_tool_wrapper_passes_through_both_allow_and_deny() -> None:
    allow = PermissionResultAllow(updated_input={"k": 1})
    deny = PermissionResultDeny(message="no")
    calls: list[tuple[str, dict[str, Any], ToolPermissionContext]] = []

    async def orig(
        name: str, tool_input: dict[str, Any], ctx: ToolPermissionContext
    ) -> PermissionResultAllow | PermissionResultDeny:
        calls.append((name, tool_input, ctx))
        return allow if name == "Read" else deny

    async def hook(data: HookInput, tool_use_id: str | None, ctx: HookContext) -> HookJSONOutput:
        return {}

    instr = Instrumentation()
    wrapped = instr.wrap_options(_options_with(hook, orig))
    assert wrapped.can_use_tool is not None
    payload: dict[str, Any] = {"file_path": "/p"}
    assert await wrapped.can_use_tool("Read", payload, _PERM_CONTEXT) is allow
    assert await wrapped.can_use_tool("Write", payload, _PERM_CONTEXT) is deny
    assert calls[0][1] is payload
    assert calls[0][2] is _PERM_CONTEXT
    assert [(c.tool, c.tool_input) for c in instr.perm_calls] == [
        ("Read", {"file_path": "/p"}),
        ("Write", {"file_path": "/p"}),
    ]


def test_wrap_options_does_not_mutate_the_original_and_keeps_other_fields() -> None:
    async def hook(data: HookInput, tool_use_id: str | None, ctx: HookContext) -> HookJSONOutput:
        return {}

    original = _options_with(hook, extra_matcher=True)
    snapshot_hooks = copy.copy(original.hooks)
    instr = Instrumentation()
    wrapped = instr.wrap_options(original, max_turns=1, max_budget_usd=0.5)
    assert original.max_turns == 30
    assert original.max_budget_usd is None
    assert original.hooks == snapshot_hooks
    assert wrapped.max_turns == 1
    assert wrapped.max_budget_usd == 0.5
    assert wrapped.model == "m"
    assert wrapped.hooks is not None
    matchers = wrapped.hooks["PreToolUse"]
    # 첫 매처의 첫 훅만 감싸고 나머지는 그대로 둔다.
    assert len(matchers) == 2
    assert matchers[0].hooks[1] is hook
    assert matchers[0].hooks[0] is not hook
    assert matchers[1].hooks == [hook]
    assert matchers[1].matcher == "Read"


def test_wrap_options_without_overrides_keeps_turns_and_budget() -> None:
    async def hook(data: HookInput, tool_use_id: str | None, ctx: HookContext) -> HookJSONOutput:
        return {}

    wrapped = Instrumentation().wrap_options(_options_with(hook))
    assert wrapped.max_turns == 30
    assert wrapped.max_budget_usd is None


def test_wrap_options_leaves_options_without_hooks_or_callback_alone() -> None:
    wrapped = Instrumentation().wrap_options(ClaudeAgentOptions(model="m"))
    assert wrapped.hooks is None
    assert wrapped.can_use_tool is None


def test_wrapped_real_guard_still_denies_outside_and_allows_inside(tmp_dir: Path) -> None:
    write_tree(
        tmp_dir,
        {
            "data/wiki/index.md": "# i",
            "outside/secret.txt": "s",
            "skills/wiki-schema/SKILL.md": "---\nname: wiki-schema\ndescription: d\n---\n스키마",
            "skills/ask/SKILL.md": "---\nname: ask\ndescription: d\n---\n질문",
        },
    )
    deps = RunnerDeps(
        model="m", effort={"bot": "low", "sync": "low"}, skills_dir=tmp_dir / "skills", env={}
    )
    inp = RunClaudeInput(
        profile="bot",
        prompt="p",
        cwd=tmp_dir / "data",
        read_roots=(),
        write_roots=(),
        timeout_ms=1000,
    )
    instr = Instrumentation()
    wrapped = instr.wrap_options(build_options(inp, deps))
    assert wrapped.hooks is not None
    assert wrapped.can_use_tool is not None
    hook = wrapped.hooks["PreToolUse"][0].hooks[0]
    inside = str(tmp_dir / "data" / "wiki" / "index.md")
    outside = str(tmp_dir / "outside" / "secret.txt")

    async def go() -> tuple[HookJSONOutput, HookJSONOutput, object, object]:
        assert wrapped.can_use_tool is not None
        return (
            await hook(_hook_input("Read", {"file_path": inside}), None, _CONTEXT),
            await hook(_hook_input("Read", {"file_path": outside}), None, _CONTEXT),
            await wrapped.can_use_tool("Read", {"file_path": inside}, _PERM_CONTEXT),
            await wrapped.can_use_tool("Read", {"file_path": outside}, _PERM_CONTEXT),
        )

    hook_in, hook_out, perm_in, perm_out = asyncio.run(go())
    assert hook_in == {}
    assert cast("Mapping[str, Any]", hook_out)["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert isinstance(perm_in, PermissionResultAllow)
    assert isinstance(perm_out, PermissionResultDeny)
    assert len(instr.hook_calls) == 2
    assert len(instr.perm_calls) == 2


async def test_query_fn_wrapper_forwards_prompt_taps_init_and_yields_messages_unchanged(
    tmp_dir: Path,
) -> None:
    write_tree(
        tmp_dir,
        {
            "data/x.md": "x",
            "skills/wiki-schema/SKILL.md": "---\nname: wiki-schema\ndescription: d\n---\n스키마",
            "skills/ask/SKILL.md": "---\nname: ask\ndescription: d\n---\n질문",
        },
    )
    sent: list[tuple[str, ClaudeAgentOptions]] = []
    messages: list[object] = []

    def fake_query(*, prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Any]:
        sent.append((prompt, options))
        return _fake_stream(options, tmp_dir / "data" / "x.md", messages)

    instr = Instrumentation()
    deps = RunnerDeps(
        model="m",
        effort={"bot": "low", "sync": "low"},
        skills_dir=tmp_dir / "skills",
        env={},
        query_fn=make_query_fn(fake_query, instr, max_turns=3, max_budget_usd=0.25),
    )
    runner = create_claude_runner(deps)
    result = await runner(
        RunClaudeInput(
            profile="bot",
            prompt="질문입니다",
            cwd=tmp_dir / "data",
            read_roots=(),
            write_roots=(),
            timeout_ms=5000,
        )
    )
    assert result.ok is True
    assert result.text == "답"
    assert result.session_id == "sess-live"
    assert sent[0][0] == "질문입니다"
    assert sent[0][1].max_turns == 3
    assert sent[0][1].max_budget_usd == 0.25
    assert [c.tool for c in instr.hook_calls] == ["Read"]
    assert [c.tool for c in instr.perm_calls] == ["Read"]
    assert len(instr.init_messages) == 1
    assert instr.init_messages[0]["tools"] == ["Read", "Grep", "Glob"]
    assert instr.init_messages[0]["mcp_servers"] == []
    assert len(messages) == 3  # init, assistant, result가 그대로 지나갔다


async def _fake_stream(
    options: ClaudeAgentOptions, readable: Path, seen: list[object]
) -> AsyncIterator[Any]:
    assert options.hooks is not None
    assert options.can_use_tool is not None
    init = SystemMessage(
        subtype="init",
        data={"tools": ["Read", "Grep", "Glob"], "mcp_servers": [], "session_id": "sess-live"},
    )
    seen.append(init)
    yield init
    await options.hooks["PreToolUse"][0].hooks[0](
        _hook_input("Read", {"file_path": str(readable)}), "t1", _CONTEXT
    )
    await options.can_use_tool("Read", {"file_path": str(readable)}, _PERM_CONTEXT)
    msg = AssistantMessage(
        content=[
            ToolUseBlock(id="t1", name="Read", input={"file_path": str(readable)}),
            TextBlock(text="답"),
        ],
        model="m",
    )
    seen.append(msg)
    yield msg
    final = ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="sess-live",
        total_cost_usd=0.01,
        usage={},
        result="답",
    )
    seen.append(final)
    yield final


# ---------------------------------------------------------------- tap: 도구 결과·init·예외


def _result_msg() -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="s",
        total_cost_usd=0.0,
        usage={},
        result="r",
    )


async def _stream_of(*messages: object, then: BaseException | None = None) -> AsyncIterator[Any]:
    for message in messages:
        yield message
    if then is not None:
        raise then


async def _drain(instr: Instrumentation, stream: AsyncIterator[Any]) -> list[Any]:
    return [m async for m in instr.tap(stream)]


def _tool_result(content: Any) -> UserMessage:
    return UserMessage(content=[ToolResultBlock(tool_use_id="t1", content=content)])


async def test_tap_detects_a_marker_in_a_string_tool_result() -> None:
    instr = Instrumentation()
    msgs = [_tool_result("앞 MARK-A 뒤"), _result_msg()]
    seen = await _drain(instr, _stream_of(*msgs))
    assert seen == msgs
    assert all(a is b for a, b in zip(seen, msgs, strict=True))
    assert instr.tool_results_contain("MARK-A") is True
    assert instr.tool_results_contain("MARK-OTHER") is False
    assert instr.tool_result_count == 1


async def test_tap_detects_a_marker_in_list_of_blocks_and_nested_content() -> None:
    instr = Instrumentation()
    content = [{"type": "text", "text": "한 줄"}, {"type": "text", "text": "둘째 MARK-B"}]
    nested = [{"type": "tool_reference", "meta": {"name": "MARK-C"}}]
    await _drain(instr, _stream_of(_tool_result(content), _tool_result(nested)))
    assert instr.tool_results_contain("MARK-B") is True
    assert instr.tool_results_contain("MARK-C") is True
    assert instr.tool_results_contain("MARK-D") is False


async def test_tap_detects_a_marker_in_tool_use_result_payload() -> None:
    instr = Instrumentation()
    msg = UserMessage(
        content=[ToolResultBlock(tool_use_id="t", content=None)],
        tool_use_result={"file": {"content": "MARK-E"}},
    )
    await _drain(instr, _stream_of(msg))
    assert instr.tool_results_contain("MARK-E") is True


async def test_tap_ignores_none_content_prompt_echo_and_assistant_text() -> None:
    instr = Instrumentation()
    await _drain(
        instr,
        _stream_of(
            _tool_result(None),
            UserMessage(content="MARK-F 프롬프트 에코"),
            UserMessage(content=[TextBlock(text="MARK-F 사용자 텍스트")]),
            AssistantMessage(content=[TextBlock(text="MARK-F 모델 텍스트")], model="m"),
        ),
    )
    assert instr.tool_results_contain("MARK-F") is False
    assert instr.tool_result_count == 1  # 내용이 없는 결과도 한 건으로 센다


async def test_tool_result_contents_never_appear_in_repr_str_or_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    instr = Instrumentation()
    await _drain(instr, _stream_of(_tool_result("비밀 MARK-G"), _result_msg()))
    assert instr.tool_results_contain("MARK-G") is True
    for text in (repr(instr), str(instr)):
        assert "MARK-G" not in text
    captured = capsys.readouterr()
    assert "MARK-G" not in captured.out + captured.err


async def test_tap_captures_the_init_message_data() -> None:
    instr = Instrumentation()
    init = SystemMessage(subtype="init", data={"tools": ["Read"], "mcp_servers": []})
    other = SystemMessage(subtype="status", data={"tools": ["Bash"]})
    seen = await _drain(instr, _stream_of(other, init, _result_msg()))
    assert seen[0] is other and seen[1] is init
    assert instr.init_messages == [{"tools": ["Read"], "mcp_servers": []}]


async def test_tap_propagates_stream_exceptions_after_yielding_earlier_messages() -> None:
    instr = Instrumentation()
    got: list[Any] = []

    async def consume() -> None:
        async for message in instr.tap(
            _stream_of(_tool_result("MARK-H"), then=RuntimeError("stream broke"))
        ):
            got.append(message)

    with pytest.raises(RuntimeError, match="stream broke"):
        await consume()
    assert len(got) == 1
    assert instr.tool_results_contain("MARK-H") is True


async def test_tap_propagates_cancellation() -> None:
    instr = Instrumentation()

    async def hang() -> AsyncIterator[Any]:
        yield _tool_result("x")
        await asyncio.Event().wait()

    async def consume() -> None:
        async for _ in instr.tap(hang()):
            pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# ---------------------------------------------------------------- 환경변수 병합 검사


def _opts(env: dict[str, str]) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(env=env)


def test_merged_env_check_passes_when_the_scrub_blanks_the_values() -> None:
    environ = {"DISCORD_TOKEN": "tok-1", "GITHUB_TOKEN": "tok-1", "PATH": "/bin"}
    scrubbed = scrub_env({"DISCORD_TOKEN": "tok-1", "GITHUB_TOKEN": "tok-1"})
    assert merged_env_secret_keys(_opts(scrubbed), ["tok-1"], environ) == []


def test_merged_env_check_detects_a_scrub_that_only_drops_keys() -> None:
    environ = {"DISCORD_TOKEN": "tok-1", "GITHUB_TOKEN": "tok-1", "PATH": "/bin"}

    def dropping_scrub(env: Mapping[str, str]) -> dict[str, str]:
        return {k: v for k, v in env.items() if k not in ("DISCORD_TOKEN", "GITHUB_TOKEN")}

    leaked = merged_env_secret_keys(
        _opts(dropping_scrub({"DISCORD_TOKEN": "tok-1", "PATH": "/bin"})), ["tok-1"], environ
    )
    assert leaked == ["DISCORD_TOKEN", "GITHUB_TOKEN"]


def test_merged_env_check_detects_a_secret_passed_through_options_env() -> None:
    assert merged_env_secret_keys(_opts({"X": "tok-2"}), ["tok-2"], {}) == ["X"]


def test_merged_env_check_ignores_empty_secrets_and_other_values() -> None:
    environ = {"A": "", "B": "other"}
    assert merged_env_secret_keys(_opts({}), ["", "tok-3"], environ) == []


def test_merged_env_check_returns_key_names_only() -> None:
    result = merged_env_secret_keys(_opts({}), ["tok-4"], {"K": "tok-4"})
    assert result == ["K"]
    assert all("tok-4" not in key for key in result)


async def test_query_fn_wrapper_records_env_leaks_from_the_real_process_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", "tok-5")
    seen: list[ClaudeAgentOptions] = []

    def fake_query(*, prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Any]:
        seen.append(options)
        return _stream_of(_result_msg())

    leaky = Instrumentation()
    fn = make_query_fn(fake_query, leaky, secret_values=("tok-5",))
    async for _ in fn(prompt="p", options=ClaudeAgentOptions(env={})):
        pass
    assert leaky.env_leaks == ["DISCORD_TOKEN"]
    assert leaky.env_checks == 1

    tight = Instrumentation()
    fn2 = make_query_fn(fake_query, tight, secret_values=("tok-5",))
    async for _ in fn2(prompt="p", options=ClaudeAgentOptions(env={"DISCORD_TOKEN": ""})):
        pass
    assert tight.env_leaks == []


# ---------------------------------------------------------------- cleanup 로그·크기 상한


def test_cleanup_logs_what_it_removes(tmp_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = tmp_dir / "made.md"
    target.write_text("x", encoding="utf-8")
    cleanup_created([target], [])
    assert not target.exists()
    assert f"[smoke] cleaned: {target}" in capsys.readouterr().out


def test_cleanup_leaves_a_large_file_in_place_and_says_so(
    tmp_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    big = tmp_dir / "big.md"
    big.write_text("a" * 1025, encoding="utf-8")
    exactly = tmp_dir / "limit.md"
    exactly.write_text("a" * 1024, encoding="utf-8")
    cleanup_created([big, exactly], [])
    out = capsys.readouterr().out
    assert big.is_file()
    assert f"[smoke] left in place: {big}" in out
    assert not exactly.exists()  # 1KB 이하는 지운다


def test_cleanup_is_silent_for_missing_files(
    tmp_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cleanup_created([tmp_dir / "nope"], [])
    assert capsys.readouterr().out == ""


# ---------------------------------------------------------------- 도구 결과를 하나도 못 본 경우


_NO_RESULTS_MSG = "도구를 불렀는데 도구 결과를 하나도 못 봄"


async def _tapped(*messages: object) -> Instrumentation:
    instr = Instrumentation()
    await _drain(instr, _stream_of(*messages))
    return instr


_INIT = SystemMessage(subtype="init", data={"tools": ["Read"], "mcp_servers": []})
_TOOL_USE = AssistantMessage(
    content=[ToolUseBlock(id="t1", name="Read", input={"file_path": "/x"})], model="m"
)


async def test_assert_tool_results_seen_fails_when_a_tool_was_called_but_no_result_arrived() -> (
    None
):
    instr = await _tapped(_INIT, _TOOL_USE, _result_msg())
    with pytest.raises(AssertionError, match=_NO_RESULTS_MSG):
        instr.assert_tool_results_seen(tool_calls=1)


async def test_assert_tool_results_seen_passes_with_a_result_or_without_calls_or_init() -> None:
    with_result = await _tapped(_INIT, _TOOL_USE, _tool_result("x"), _result_msg())
    with_result.assert_tool_results_seen(tool_calls=1)
    no_calls = await _tapped(_INIT, _result_msg())
    no_calls.assert_tool_results_seen(tool_calls=0)
    no_init = await _tapped(_TOOL_USE, _result_msg())
    no_init.assert_tool_results_seen(tool_calls=1)  # init을 못 봤으면 모양 가정을 판단하지 않는다


async def test_leak_helper_requires_tool_results_before_trusting_their_absence() -> None:
    silent = await _tapped(_INIT, _TOOL_USE, _result_msg())
    with pytest.raises(AssertionError, match=_NO_RESULTS_MSG):
        assert_no_leak_anywhere(False, silent, "MARK-I", tool_calls=1)


async def test_leak_helper_flags_marker_in_text_or_in_tool_results_without_content() -> None:
    clean = await _tapped(_INIT, _TOOL_USE, _tool_result("무관한 내용"), _result_msg())
    assert_no_leak_anywhere(False, clean, "MARK-I", tool_calls=1)
    with pytest.raises(AssertionError, match="응답에 나타남") as in_text:
        assert_no_leak_anywhere(True, clean, "MARK-I", tool_calls=1)
    assert "MARK-I" not in str(in_text.value)
    leaky = await _tapped(_INIT, _TOOL_USE, _tool_result("비밀 MARK-I"), _result_msg())
    with pytest.raises(AssertionError, match="도구 결과에 나타남") as in_result:
        assert_no_leak_anywhere(False, leaky, "MARK-I", tool_calls=1)
    assert "MARK-I" not in str(in_result.value)


# ---------------------------------------------------------------- usage·훅 시각 계측


def _assistant(message_id: str | None, usage: dict[str, Any] | None) -> AssistantMessage:
    return AssistantMessage(content=[], model="m", usage=usage, message_id=message_id)


async def test_tap_records_assistant_and_result_usage_and_passes_messages_through() -> None:
    a1 = _assistant("m1", {"input_tokens": 10, "output_tokens": 2, "service_tier": "x"})
    a2 = _assistant("m1", {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 3})
    a3 = _assistant(None, {"input_tokens": 1, "output_tokens": 1, "output_tokens_extra": 9.5})
    a4 = _assistant("m2", None)
    result = ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="s",
        total_cost_usd=0.0,
        usage={"input_tokens": 11, "output_tokens": 6, "cache_creation_input_tokens": True},
        result="r",
    )
    instr = Instrumentation()
    seen = await _drain(instr, _stream_of(a1, a2, a3, a4, result))
    assert seen == [a1, a2, a3, a4, result]
    assert instr.assistant_usages == [
        ("m1", {"input_tokens": 10, "output_tokens": 2}),
        ("m1", {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 3}),
        (None, {"input_tokens": 1, "output_tokens": 1}),
    ]
    assert instr.result_usage == {"input_tokens": 11, "output_tokens": 6}


async def test_result_usage_stays_none_without_a_dict() -> None:
    instr = Instrumentation()
    await _drain(instr, _stream_of(_result_msg()))
    assert instr.result_usage == {}  # _result_msg는 빈 dict
    instr2 = Instrumentation()
    await _drain(instr2, _stream_of(_TOOL_USE))
    assert instr2.assistant_usages == [] and instr2.result_usage is None


async def test_hook_wrapper_records_call_times_and_returns_original_result() -> None:
    marker: HookJSONOutput = {"decision": "block"}

    async def orig(data: HookInput, tool_use_id: str | None, ctx: HookContext) -> HookJSONOutput:
        return marker

    instr = Instrumentation()
    wrapped = instr.wrap_options(_options_with(orig))
    assert wrapped.hooks is not None
    before = time.monotonic()
    out = await wrapped.hooks["PreToolUse"][0].hooks[0](_hook_input("Read", {}), None, _CONTEXT)
    after = time.monotonic()
    assert out is marker
    assert len(instr.hook_call_times) == 1
    assert before <= instr.hook_call_times[0] <= after


# ---------------------------------------------------------------- live_gate


def _rec(message_id: str | None, inp: int, out: int) -> tuple[str | None, dict[str, int]]:
    return (message_id, {"input_tokens": inp, "output_tokens": out})


def test_usage_regressions() -> None:
    assert usage_regressions([_rec("a", 5, 2), _rec("a", 5, 2)]) == []
    assert usage_regressions([_rec("a", 5, 2), _rec("a", 5, 9)]) == []
    shrink = [_rec("a", 5, 9), _rec("a", 5, 2)]
    assert usage_regressions(shrink) == ["a"]
    changed_in = [_rec("b", 5, 2), _rec("b", 6, 2)]
    assert usage_regressions(changed_in) == ["b"]
    assert usage_regressions([_rec(None, 5, 9), _rec(None, 1, 1)]) == []
    assert usage_regressions([*shrink, *changed_in, *shrink]) == ["a", "b"]


def test_pinned_sdk_is_a_version_string() -> None:
    assert PINNED_SDK == "0.2.164"


async def test_watch_new_files_reports_offsets_and_stops_after_stop(tmp_dir: Path) -> None:
    directory = tmp_dir / "burst"
    directory.mkdir()
    (directory / "old.md").write_text("x", encoding="utf-8")
    stopped = False

    async def maker() -> None:
        nonlocal stopped
        await asyncio.sleep(0.3)
        (directory / "n1.md").write_text("x", encoding="utf-8")
        await asyncio.sleep(0.5)
        (directory / "n2.md").write_text("x", encoding="utf-8")
        stopped = True

    async with asyncio.timeout(10):
        t0 = time.monotonic()
        task = asyncio.create_task(maker())
        found = await watch_new_files(
            directory, {"old.md"}, t0=t0, stop=lambda: stopped, interval_s=0.05
        )
        await task
    elapsed = time.monotonic() - t0
    assert set(found) == {"n1.md", "n2.md"}
    assert abs(found["n1.md"] - 0.3) < 0.4
    assert abs(found["n2.md"] - 0.8) < 0.4
    assert elapsed < 0.8 + 5.0 + 1.5


async def test_watch_new_files_missing_directory_and_max_s(tmp_dir: Path) -> None:
    async with asyncio.timeout(10):
        t0 = time.monotonic()
        got = await watch_new_files(
            tmp_dir / "nope", set(), t0=t0, stop=lambda: True, interval_s=0.05, max_s=0.5
        )
    assert got == {}
    assert time.monotonic() - t0 < 6.5


# ---------------------------------------------------------------- 자동 메모리 도우미


def test_memory_files_collects_only_regular_files_under_memory(tmp_dir: Path) -> None:
    write_tree(
        tmp_dir,
        {
            "projects/a/memory/x.md": "1",
            "projects/a/memory/sub/y.md": "2",
            "projects/b/other.md": "3",
            "outside.md": "4",
        },
    )
    (tmp_dir / "elsewhere").mkdir()
    (tmp_dir / "elsewhere" / "z.md").write_text("z", encoding="utf-8")
    (tmp_dir / "projects" / "a" / "memory" / "link.md").symlink_to(tmp_dir / "elsewhere" / "z.md")
    (tmp_dir / "projects" / "c").mkdir()
    (tmp_dir / "projects" / "c" / "memory").symlink_to(tmp_dir / "elsewhere")
    found = memory_files(tmp_dir)
    rel = sorted(p.relative_to(tmp_dir).as_posix() for p in found)
    assert rel == ["projects/a/memory/sub/y.md", "projects/a/memory/x.md"]
    assert len(memory_files(tmp_dir, limit=1)) == 1
    assert memory_files(tmp_dir / "missing") == {}


def test_changed_since_finds_new_and_modified_files(tmp_dir: Path) -> None:
    write_tree(tmp_dir, {"projects/a/memory/x.md": "1", "projects/a/memory/keep.md": "1"})
    before = memory_files(tmp_dir)
    assert changed_since(before, memory_files(tmp_dir)) == []
    x = tmp_dir / "projects" / "a" / "memory" / "x.md"
    os.utime(x, (1_000_000_000, 1_000_000_000))
    (tmp_dir / "projects" / "a" / "memory" / "new.md").write_text("n", encoding="utf-8")
    after = memory_files(tmp_dir)
    changed = sorted(p.name for p in changed_since(before, after))
    assert changed == ["new.md", "x.md"]

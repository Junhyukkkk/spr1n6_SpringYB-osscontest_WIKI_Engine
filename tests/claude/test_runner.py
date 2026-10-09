import asyncio
import dataclasses
import logging
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any, Literal, cast

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ContentBlock,
    HookContext,
    HookInput,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    TextBlock,
    ToolPermissionContext,
    ToolUseBlock,
)

from team_wiki_engine.claude import runner as runner_module
from team_wiki_engine.claude.path_guard import Allow, Deny, GuardContext
from team_wiki_engine.claude.profiles import DISALLOWED_TOOLS, PROFILES
from team_wiki_engine.claude.prompt import clear_prompt_cache
from team_wiki_engine.claude.runner import (
    SECRET_ENV_KEYS,
    RunnerDeps,
    build_options,
    create_claude_runner,
    scrub_env,
)
from team_wiki_engine.claude.types import (
    ClaudeRunner,
    RunClaudeInput,
    RunClaudeResult,
    ToolCallTrace,
    Usage,
)
from tests.helpers import write_tree

Behavior = Literal[
    "end",
    "hang",
    "hang-raise",
    "end-on-abort",
    "throw",
    "swallow",
    "count-cancels",
    "swallow-yield",
]


class FakeQuery:
    """실제 `query()`와 같은 키워드 시그니처를 가진 가짜. SDK의 실제 데이터클래스를 내보낸다."""

    def __init__(self, messages: list[Any], behavior: Behavior = "end") -> None:
        self.messages = messages
        self.behavior = behavior
        self.calls: list[ClaudeAgentOptions] = []
        self.prompts: list[str] = []
        self.cancelled = False
        self.cancel_count = 0
        self.yielded_after_cancel = False

    def __call__(self, *, prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Any]:
        self.calls.append(options)
        self.prompts.append(prompt)
        return self._stream()

    async def _stream(self) -> AsyncIterator[Any]:
        for message in self.messages:
            yield message
        if self.behavior == "throw":
            raise RuntimeError("boom")
        if self.behavior == "end":
            return
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            if self.behavior == "hang":
                raise
            if self.behavior == "hang-raise":
                raise RuntimeError("aborted") from None
            if self.behavior == "swallow":
                # 취소를 삼키고 한참 더 버티는 이터레이터
                await asyncio.sleep(1.0)
            if self.behavior == "count-cancels":
                # 취소를 삼키고 버티며, 취소가 몇 번 오는지 센다(두 번째 취소는 정리를 끊는다)
                self.cancel_count += 1
                try:
                    await asyncio.sleep(0.3)
                except asyncio.CancelledError:
                    self.cancel_count += 1
                    raise
            if self.behavior == "swallow-yield":
                await asyncio.sleep(0.15)
                yield assistant(ToolUseBlock(id="late", name="Read", input={"file_path": "/x"}))
                self.yielded_after_cancel = True
            # end-on-abort: 조용히 끝낸다


def assistant(
    *blocks: ContentBlock,
    usage: dict[str, Any] | None = None,
    message_id: str | None = None,
) -> AssistantMessage:
    return AssistantMessage(
        content=list(blocks), model="m", usage=usage, message_id=message_id
    )


def text(value: str) -> TextBlock:
    return TextBlock(text=value)


def success(**overrides: Any) -> ResultMessage:
    base = ResultMessage(
        subtype="success",
        duration_ms=1234,
        duration_api_ms=1000,
        is_error=False,
        num_turns=1,
        session_id="sess-1",
        total_cost_usd=0.05,
        usage={
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_read_input_tokens": 50,
            "cache_creation_input_tokens": 10,
        },
        result="답변입니다",
    )
    return dataclasses.replace(base, **overrides)


@pytest.fixture(autouse=True)
def _fresh_prompt_cache() -> Iterator[None]:
    clear_prompt_cache()
    yield
    clear_prompt_cache()


@pytest.fixture
def root(tmp_dir: Path) -> Path:
    write_tree(
        tmp_dir,
        {
            "data/wiki/index.md": "# 목차",
            "repos/worker/src/A.kt": "class A",
            "outside/secret.txt": "s",
            "skills/wiki-schema/SKILL.md": "---\nname: wiki-schema\ndescription: d\n---\n스키마",
            "skills/ask/SKILL.md": "---\nname: ask\ndescription: d\n---\n질문",
            "skills/sync-code/SKILL.md": "---\nname: sync-code\ndescription: d\n---\n코드",
            "skills/ingest/SKILL.md": "---\nname: ingest\ndescription: d\n---\n문서",
        },
    )
    return tmp_dir


@pytest.fixture
def data(root: Path) -> Path:
    return root / "data"


@pytest.fixture
def repos(root: Path) -> Path:
    return root / "repos"


@pytest.fixture
def deps(root: Path) -> RunnerDeps:
    return RunnerDeps(
        model="claude-sonnet-5-5",
        effort={"bot": "low", "sync": "medium"},
        skills_dir=root / "skills",
        env={
            "ANTHROPIC_API_KEY": "k",
            "DISCORD_TOKEN": "d-token",
            "GITHUB_TOKEN": "g-token",
            "PATH": "/usr/bin",
        },
    )


@pytest.fixture
def bot_input(data: Path, repos: Path) -> RunClaudeInput:
    return RunClaudeInput(
        profile="bot",
        prompt="질문",
        cwd=data,
        read_roots=(repos,),
        write_roots=(),
        timeout_ms=5_000,
    )


def runner_for(deps: RunnerDeps, fake: FakeQuery, **changes: Any) -> ClaudeRunner:
    return create_claude_runner(dataclasses.replace(deps, query_fn=fake, **changes))


async def run(
    deps: RunnerDeps, fake: FakeQuery, inp: RunClaudeInput, **changes: Any
) -> RunClaudeResult:
    return await runner_for(deps, fake, **changes)(inp)


# ---------------------------------------------------------------- Usage 기본값


def test_usage_fields_default_to_zero() -> None:
    assert Usage() == Usage(0, 0, 0, 0)
    assert Usage(input_tokens=3).output_tokens == 0


# ---------------------------------------------------------------- 결과 옮기기


class TestResultMapping:
    async def test_maps_success_result(self, deps: RunnerDeps, bot_input: RunClaudeInput) -> None:
        fake = FakeQuery([assistant(text("중간")), success()])
        r = await run(deps, fake, bot_input)
        assert r.ok is True
        assert r.text == "답변입니다"
        assert r.session_id == "sess-1"
        assert r.cost_usd == 0.05
        assert r.duration_ms == 1234
        assert r.usage == Usage(100, 20, 50, 10)
        assert r.failure is None
        assert r.detail is None
        assert fake.prompts == ["질문"]

    async def test_falls_back_to_last_assistant_text_when_result_is_none(
        self, deps: RunnerDeps, bot_input: RunClaudeInput
    ) -> None:
        fake = FakeQuery(
            [
                assistant(text("첫 답")),
                assistant(text("마지막 "), text("답")),
                success(result=None),
            ]
        )
        assert (await run(deps, fake, bot_input)).text == "마지막 답"

    async def test_blank_result_falls_back_and_empty_when_no_text(
        self, deps: RunnerDeps, bot_input: RunClaudeInput
    ) -> None:
        fake = FakeQuery([success(result="  ")])
        r = await run(deps, fake, bot_input)
        assert (r.ok, r.failure) == (False, "empty")

    async def test_max_turns(self, deps: RunnerDeps, bot_input: RunClaudeInput) -> None:
        fake = FakeQuery([success(subtype="error_max_turns", is_error=True, result=None)])
        r = await run(deps, fake, bot_input)
        assert (r.ok, r.failure, r.detail) == (False, "max_turns", "error_max_turns")

    async def test_other_is_error(self, deps: RunnerDeps, bot_input: RunClaudeInput) -> None:
        fake = FakeQuery([success(subtype="error_during_execution", is_error=True)])
        r = await run(deps, fake, bot_input)
        assert (r.ok, r.failure, r.detail) == (
            False,
            "error",
            "error_during_execution: 답변입니다",
        )

    async def test_error_detail_carries_first_300_chars_of_errors(
        self, deps: RunnerDeps, bot_input: RunClaudeInput
    ) -> None:
        fake = FakeQuery(
            [
                success(
                    subtype="error_during_execution",
                    is_error=True,
                    errors=["첫째 오류", "x" * 500],
                )
            ]
        )
        r = await run(deps, fake, bot_input)
        assert r.failure == "error"
        assert r.detail is not None
        assert r.detail.startswith("error_during_execution: 첫째 오류; xxx")
        assert len(r.detail) == len("error_during_execution: ") + 300

    async def test_no_result_message_is_error(
        self, deps: RunnerDeps, bot_input: RunClaudeInput
    ) -> None:
        fake = FakeQuery([assistant(text("답"))])
        r = await run(deps, fake, bot_input)
        assert (r.ok, r.failure, r.detail) == (False, "error", "result 메시지가 없습니다")
        assert r.text == "답"

    async def test_sdk_exception_is_wrapped_not_raised(
        self, deps: RunnerDeps, bot_input: RunClaudeInput
    ) -> None:
        r = await run(deps, FakeQuery([], "throw"), bot_input)
        assert (r.ok, r.failure, r.detail) == (False, "error", "boom")

    async def test_exception_detail_is_capped_at_500_chars(
        self, deps: RunnerDeps, bot_input: RunClaudeInput
    ) -> None:
        def failing(*, prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Any]:
            raise ValueError("y" * 900)

        r = await create_claude_runner(dataclasses.replace(deps, query_fn=failing))(bot_input)
        assert r.failure == "error"
        assert r.detail == "y" * 500

    async def test_missing_skill_file_is_error(
        self, deps: RunnerDeps, bot_input: RunClaudeInput, root: Path
    ) -> None:
        fake = FakeQuery([success()])
        r = await run(deps, fake, bot_input, skills_dir=root / "none")
        assert (r.ok, r.failure) == (False, "error")
        assert fake.calls == []  # SDK는 부르지 않았다

    async def test_relative_cwd_is_error_not_raised(
        self, deps: RunnerDeps, bot_input: RunClaudeInput
    ) -> None:
        inp = dataclasses.replace(bot_input, cwd=Path("relative/dir"))
        fake = FakeQuery([success()])
        r = await run(deps, fake, inp)
        assert (r.ok, r.failure) == (False, "error")
        assert fake.calls == []

    async def test_ignores_unexpected_message_types(
        self, deps: RunnerDeps, bot_input: RunClaudeInput
    ) -> None:
        weird_assistant = AssistantMessage(content=cast(Any, None), model="m")
        fake = FakeQuery(
            [
                "문자열",
                42,
                None,
                {"type": "result"},
                object(),
                weird_assistant,
                assistant(text("답")),
                success(),
            ]
        )
        r = await run(deps, fake, bot_input)
        assert r.ok is True
        assert r.text == "답변입니다"


# ---------------------------------------------------------------- 시간 제한


class TestTimeouts:
    @pytest.mark.parametrize("behavior", ["hang", "hang-raise", "end-on-abort"])
    async def test_abort_in_any_shape_is_timeout(
        self, deps: RunnerDeps, bot_input: RunClaudeInput, behavior: Behavior
    ) -> None:
        fake = FakeQuery([], behavior)
        r = await run(deps, fake, dataclasses.replace(bot_input, timeout_ms=30))
        assert (r.ok, r.failure, r.detail) == (False, "timeout", "30ms 초과")
        assert fake.cancelled is True

    async def test_timer_after_success_result_is_ok(
        self, deps: RunnerDeps, bot_input: RunClaudeInput
    ) -> None:
        fake = FakeQuery([success()], "end-on-abort")
        r = await run(deps, fake, dataclasses.replace(bot_input, timeout_ms=30))
        assert r.ok is True
        assert r.text == "답변입니다"
        assert r.failure is None

    async def test_hard_deadline_returns_even_if_iterator_swallows_cancel(
        self, deps: RunnerDeps, bot_input: RunClaudeInput
    ) -> None:
        fake = FakeQuery([assistant(text("중간"))], "swallow")
        started = time.monotonic()
        r = await run(
            deps,
            fake,
            dataclasses.replace(bot_input, timeout_ms=30),
            hard_deadline_grace_ms=50,
        )
        elapsed = time.monotonic() - started
        assert (r.ok, r.failure) == (False, "timeout")
        assert r.text == "중간"
        assert fake.cancelled is True
        assert elapsed < 0.7  # 삼키는 이터레이터는 1초를 버티지만 30 + 50ms 뒤 돌아온다

    async def test_outer_cancel_propagates_and_cancels_stream(
        self, deps: RunnerDeps, bot_input: RunClaudeInput
    ) -> None:
        fake = FakeQuery([], "hang")
        task = asyncio.create_task(run(deps, fake, bot_input))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert fake.cancelled is True

    async def test_outer_cancel_is_not_stuck_by_swallowing_iterator(
        self, deps: RunnerDeps, bot_input: RunClaudeInput
    ) -> None:
        fake = FakeQuery([], "swallow")
        task = asyncio.create_task(run(deps, fake, bot_input, hard_deadline_grace_ms=50))
        await asyncio.sleep(0.05)
        started = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert time.monotonic() - started < 0.7

    async def test_abandoned_task_is_logged(
        self,
        deps: RunnerDeps,
        bot_input: RunClaudeInput,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        fake = FakeQuery([], "swallow")
        with caplog.at_level(logging.WARNING, logger=runner_module.__name__):
            await run(
                deps,
                fake,
                dataclasses.replace(bot_input, timeout_ms=20),
                hard_deadline_grace_ms=20,
            )
        messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert any("백그라운드에서 계속" in m and "grace=20ms" in m for m in messages)
        assert not any("취소에 응답하지 않" in m for m in messages)


# ---------------------------------------------------------------- F8/F9: 결과 우선 분류와 비용 하한


class TestResultFirstClassification:
    async def test_max_turns_result_then_exception_is_max_turns(
        self, deps: RunnerDeps, bot_input: RunClaudeInput
    ) -> None:
        fake = FakeQuery(
            [success(subtype="error_max_turns", is_error=True, result=None)], "throw"
        )
        r = await run(deps, fake, bot_input)
        assert (r.ok, r.failure) == (False, "max_turns")

    async def test_success_result_then_exception_is_ok(
        self, deps: RunnerDeps, bot_input: RunClaudeInput
    ) -> None:
        r = await run(deps, FakeQuery([success()], "throw"), bot_input)
        assert r.ok is True
        assert r.text == "답변입니다"
        assert r.failure is None

    async def test_no_result_exception_is_error(
        self, deps: RunnerDeps, bot_input: RunClaudeInput
    ) -> None:
        r = await run(deps, FakeQuery([], "throw"), bot_input)
        assert (r.ok, r.failure) == (False, "error")

    async def test_timeout_keeps_usage_lower_bound(
        self, deps: RunnerDeps, bot_input: RunClaudeInput
    ) -> None:
        fake = FakeQuery(
            [
                assistant(
                    text("a"),
                    usage={"input_tokens": 10, "output_tokens": 2, "cache_read_input_tokens": 5},
                    message_id="m1",
                ),
                assistant(
                    text("b"),
                    usage={
                        "input_tokens": 7,
                        "output_tokens": 3,
                        "cache_creation_input_tokens": 4,
                    },
                    message_id="m2",
                ),
            ],
            "hang",
        )
        r = await run(deps, fake, dataclasses.replace(bot_input, timeout_ms=30))
        assert r.failure == "timeout"
        assert r.usage == Usage(
            input_tokens=17,
            output_tokens=5,
            cache_read_input_tokens=5,
            cache_creation_input_tokens=4,
        )
        assert r.cost_usd == 0.0
        assert r.session_id is None

    async def test_exception_run_keeps_usage_too(
        self, deps: RunnerDeps, bot_input: RunClaudeInput
    ) -> None:
        fake = FakeQuery([assistant(text("a"), usage={"input_tokens": 9})], "throw")
        r = await run(deps, fake, bot_input)
        assert r.failure == "error"
        assert r.usage.input_tokens == 9

    async def test_same_message_id_is_counted_once(
        self, deps: RunnerDeps, bot_input: RunClaudeInput
    ) -> None:
        # SDK는 한 API 응답의 블록마다 AssistantMessage를 내보내며 usage가 같다.
        usage = {"input_tokens": 10, "output_tokens": 2}
        fake = FakeQuery(
            [
                assistant(text("a"), usage=usage, message_id="m1"),
                assistant(
                    ToolUseBlock(id="t", name="Read", input={}), usage=usage, message_id="m1"
                ),
            ],
            "hang",
        )
        r = await run(deps, fake, dataclasses.replace(bot_input, timeout_ms=30))
        assert r.usage == Usage(input_tokens=10, output_tokens=2)

    async def test_garbage_usage_values_count_as_zero(
        self, deps: RunnerDeps, bot_input: RunClaudeInput
    ) -> None:
        fake = FakeQuery(
            [
                assistant(
                    text("a"),
                    usage={
                        "input_tokens": "12",
                        "output_tokens": True,
                        "cache_read_input_tokens": 3.0,
                    },
                )
            ],
            "throw",
        )
        r = await run(deps, fake, bot_input)
        assert r.usage == Usage(input_tokens=0, output_tokens=0, cache_read_input_tokens=3)

    async def test_result_usage_is_used_when_result_exists(
        self, deps: RunnerDeps, bot_input: RunClaudeInput
    ) -> None:
        fake = FakeQuery([assistant(text("a"), usage={"input_tokens": 999}), success()])
        assert (await run(deps, fake, bot_input)).usage == Usage(100, 20, 50, 10)

    async def test_duration_falls_back_to_elapsed_without_result(
        self, deps: RunnerDeps, bot_input: RunClaudeInput
    ) -> None:
        r = await run(deps, FakeQuery([], "hang"), dataclasses.replace(bot_input, timeout_ms=40))
        assert r.duration_ms >= 30


# ---------------------------------------------------------------- 도구 호출 기록


class TestToolCallTrace:
    async def test_records_path_and_guard_verdict_per_tool_use(
        self, deps: RunnerDeps, bot_input: RunClaudeInput, root: Path, repos: Path
    ) -> None:
        inside = repos / "worker/src/A.kt"
        outside = root / "outside/secret.txt"
        fake = FakeQuery(
            [
                assistant(
                    ToolUseBlock(id="1", name="Read", input={"file_path": str(inside)}),
                    ToolUseBlock(id="2", name="Read", input={"file_path": str(outside)}),
                    ToolUseBlock(id="3", name="Bash", input={"command": "ls"}),
                ),
                success(),
            ]
        )
        r = await run(deps, fake, bot_input)
        assert r.tool_calls == [
            ToolCallTrace("Read", inside, True),
            ToolCallTrace("Read", outside, False),
            ToolCallTrace("Bash", None, False),
        ]

    async def test_non_dict_tool_input_does_not_crash(
        self, deps: RunnerDeps, bot_input: RunClaudeInput
    ) -> None:
        fake = FakeQuery(
            [
                assistant(ToolUseBlock(id="1", name="Read", input=cast(Any, "oops"))),
                success(),
            ]
        )
        r = await run(deps, fake, bot_input)
        assert r.tool_calls == [ToolCallTrace("Read", None, False)]

    async def test_guard_failure_marks_trace_denied_without_killing_run(
        self,
        deps: RunnerDeps,
        bot_input: RunClaudeInput,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def explode(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("guard down")

        monkeypatch.setattr(runner_module, "decide", explode)
        fake = FakeQuery(
            [assistant(ToolUseBlock(id="1", name="Read", input={"file_path": "/x"})), success()]
        )
        r = await run(deps, fake, bot_input)
        assert r.ok is True
        assert [(t.name, t.allowed) for t in r.tool_calls] == [("Read", False)]


# ---------------------------------------------------------------- SDK 옵션


class TestOptions:
    async def test_bot_profile_options(
        self, deps: RunnerDeps, bot_input: RunClaudeInput, data: Path, repos: Path
    ) -> None:
        fake = FakeQuery([success()])
        await run(deps, fake, bot_input)
        o = fake.calls[0]
        assert o.model == "claude-sonnet-5-5"
        assert o.effort == "low"
        assert o.cwd == str(data)
        assert o.add_dirs == [str(repos)]
        assert o.tools == ["Read", "Grep", "Glob"]
        assert o.disallowed_tools == list(DISALLOWED_TOOLS)
        assert "Bash" in o.disallowed_tools
        assert o.permission_mode == "default"
        assert o.setting_sources == []
        assert o.strict_mcp_config is True
        assert o.max_turns == PROFILES["bot"].max_turns == 30
        assert o.resume is None
        assert o.can_use_tool is not None
        assert o.system_prompt == {
            "type": "preset",
            "preset": "claude_code",
            "append": "# wiki-schema\n\n스키마\n\n# ask\n\n질문",
        }

    def test_env_scrubs_secrets_by_blanking_and_disables_auto_memory(
        self, deps: RunnerDeps, bot_input: RunClaudeInput
    ) -> None:
        env = build_options(bot_input, deps).env
        assert env["DISCORD_TOKEN"] == ""
        assert env["GITHUB_TOKEN"] == ""
        assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
        assert env["ANTHROPIC_API_KEY"] == "k"
        assert env["PATH"] == "/usr/bin"

    def test_sync_profile_uses_sync_effort_and_write_tools(
        self, deps: RunnerDeps, bot_input: RunClaudeInput, data: Path
    ) -> None:
        inp = dataclasses.replace(
            bot_input, profile="sync-code", write_roots=(data / "wiki",)
        )
        o = build_options(inp, deps)
        assert o.effort == "medium"
        assert o.tools == ["Read", "Grep", "Glob", "Edit", "Write"]
        assert o.max_turns == 80
        assert o.system_prompt is not None
        assert cast(dict[str, Any], o.system_prompt)["append"].endswith("# sync-code\n\n코드")

    def test_resume_is_passed_when_given(
        self, deps: RunnerDeps, bot_input: RunClaudeInput
    ) -> None:
        o = build_options(dataclasses.replace(bot_input, resume_session_id="sess-0"), deps)
        assert o.resume == "sess-0"

    def test_hooks_registration_has_one_matcherless_pre_tool_use_hook(
        self, deps: RunnerDeps, bot_input: RunClaudeInput
    ) -> None:
        o = build_options(bot_input, deps)
        assert o.hooks is not None
        assert list(o.hooks) == ["PreToolUse"]
        matchers = o.hooks["PreToolUse"]
        assert len(matchers) == 1
        assert matchers[0].matcher is None
        assert len(matchers[0].hooks) == 1

    def test_build_options_does_not_mutate_arguments(
        self, deps: RunnerDeps, bot_input: RunClaudeInput
    ) -> None:
        env_before = dict(deps.env)
        input_before = dataclasses.replace(bot_input)
        first = build_options(bot_input, deps)
        second = build_options(bot_input, deps)
        assert isinstance(first.tools, list)
        first.tools.append("Mutated")  # 반환된 목록을 바꿔도 프로필이 안 바뀌는지 확인
        assert dict(deps.env) == env_before
        assert bot_input == input_before
        assert second.tools == ["Read", "Grep", "Glob"]
        assert PROFILES["bot"].tools == ("Read", "Grep", "Glob")
        assert first.disallowed_tools is not second.disallowed_tools

    def test_build_options_raises_for_missing_skill(
        self, deps: RunnerDeps, bot_input: RunClaudeInput, root: Path
    ) -> None:
        with pytest.raises(FileNotFoundError):
            build_options(bot_input, dataclasses.replace(deps, skills_dir=root / "none"))

    def test_build_options_rejects_relative_roots(
        self, deps: RunnerDeps, bot_input: RunClaudeInput
    ) -> None:
        with pytest.raises(ValueError):
            build_options(dataclasses.replace(bot_input, read_roots=(Path("rel"),)), deps)


class TestScrubEnv:
    def test_blanks_secret_keys_and_keeps_the_rest(self) -> None:
        source = {"DISCORD_TOKEN": "x", "GITHUB_TOKEN": "y", "A": "b"}
        scrubbed = scrub_env(source)
        assert scrubbed == {"DISCORD_TOKEN": "", "GITHUB_TOKEN": "", "A": "b"}

    def test_adds_blank_entries_even_when_secret_is_absent(self) -> None:
        # SDK는 env를 os.environ에 합치므로 빈 문자열로 덮어써야 부모의 값이 안 샌다.
        assert scrub_env({"A": "b"}) == {"A": "b", "DISCORD_TOKEN": "", "GITHUB_TOKEN": ""}

    def test_does_not_mutate_the_original(self) -> None:
        source = {"DISCORD_TOKEN": "x", "A": "b"}
        result = scrub_env(source)
        assert source == {"DISCORD_TOKEN": "x", "A": "b"}
        assert result is not source

    def test_secret_keys_constant(self) -> None:
        assert SECRET_ENV_KEYS == ("DISCORD_TOKEN", "GITHUB_TOKEN")


# ---------------------------------------------------------------- can_use_tool과 PreToolUse 훅


def tool_context() -> ToolPermissionContext:
    return ToolPermissionContext(tool_use_id="t1")


async def call_hook(options: ClaudeAgentOptions, payload: dict[str, Any]) -> dict[str, Any]:
    assert options.hooks is not None
    hook = options.hooks["PreToolUse"][0].hooks[0]
    context: HookContext = {"signal": None}
    return dict(await hook(cast(HookInput, payload), "t1", context))


def hook_payload(tool_name: str, tool_input: object) -> dict[str, Any]:
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": tool_input,
        "tool_use_id": "t1",
    }


def assert_denied(output: dict[str, Any]) -> None:
    assert list(output) == ["hookSpecificOutput"]
    specific = output["hookSpecificOutput"]
    assert specific["hookEventName"] == "PreToolUse"
    assert specific["permissionDecision"] == "deny"
    assert specific["permissionDecisionReason"].strip() != ""


class TestCanUseTool:
    async def test_follows_the_path_verdict(
        self, deps: RunnerDeps, bot_input: RunClaudeInput, root: Path, repos: Path, data: Path
    ) -> None:
        fake = FakeQuery([success()])
        await run(deps, fake, bot_input)
        can_use_tool = fake.calls[0].can_use_tool
        assert can_use_tool is not None
        inside: dict[str, Any] = {"file_path": str(repos / "worker/src/A.kt")}
        allowed = await can_use_tool("Read", inside, tool_context())
        assert isinstance(allowed, PermissionResultAllow)
        assert allowed.updated_input == inside
        for name, tool_input in (
            ("Read", {"file_path": str(root / "outside/secret.txt")}),
            ("Write", {"file_path": str(data / "wiki/x.md")}),
            ("Bash", {"command": "ls"}),
        ):
            denied = await can_use_tool(name, tool_input, tool_context())
            assert isinstance(denied, PermissionResultDeny)
            assert denied.message.strip() != ""

    async def test_write_inside_write_root_is_allowed(
        self, deps: RunnerDeps, bot_input: RunClaudeInput, data: Path
    ) -> None:
        inp = dataclasses.replace(bot_input, profile="sync-code", write_roots=(data / "wiki",))
        can_use_tool = build_options(inp, deps).can_use_tool
        assert can_use_tool is not None
        result = await can_use_tool(
            "Write", {"file_path": str(data / "wiki/decisions/x.md")}, tool_context()
        )
        assert isinstance(result, PermissionResultAllow)


class TestPreToolUseHook:
    @pytest.fixture
    def options(self, deps: RunnerDeps, bot_input: RunClaudeInput) -> ClaudeAgentOptions:
        return build_options(bot_input, deps)

    async def test_read_outside_roots_is_denied(
        self, options: ClaudeAgentOptions, root: Path
    ) -> None:
        out = await call_hook(
            options, hook_payload("Read", {"file_path": str(root / "outside/secret.txt")})
        )
        assert_denied(out)

    async def test_read_and_grep_inside_roots_return_empty_dict(
        self, options: ClaudeAgentOptions, repos: Path, data: Path
    ) -> None:
        # 'allow'를 명시하면 정상 권한 흐름을 건너뛰므로 빈 객체여야 한다.
        read = await call_hook(
            options, hook_payload("Read", {"file_path": str(repos / "worker/src/A.kt")})
        )
        grep = await call_hook(
            options, hook_payload("Grep", {"pattern": "A", "path": str(repos / "worker")})
        )
        read_cwd = await call_hook(
            options, hook_payload("Read", {"file_path": str(data / "wiki/index.md")})
        )
        assert read == {}
        assert grep == {}
        assert read_cwd == {}

    async def test_write_outside_write_roots_is_denied(
        self, options: ClaudeAgentOptions, data: Path
    ) -> None:
        out = await call_hook(
            options, hook_payload("Write", {"file_path": str(data / "wiki/x.md")})
        )
        assert_denied(out)

    @pytest.mark.parametrize(
        ("tool", "tool_input"),
        [
            ("Bash", {"command": "ls"}),
            ("WebFetch", {"url": "https://example.com"}),
            ("Skill", {"skill": "x"}),
            ("mcp__srv__tool", {}),
        ],
    )
    async def test_other_tools_are_denied(
        self, options: ClaudeAgentOptions, tool: str, tool_input: dict[str, Any]
    ) -> None:
        assert_denied(await call_hook(options, hook_payload(tool, tool_input)))

    @pytest.mark.parametrize(
        "path",
        [
            "~/.claude/projects/x/memory/MEMORY.md",
            " /etc/passwd",
            "/etc/passwd ",
            "~",
        ],
    )
    async def test_tilde_and_whitespace_paths_are_denied(
        self, options: ClaudeAgentOptions, path: str
    ) -> None:
        assert_denied(await call_hook(options, hook_payload("Read", {"file_path": path})))

    async def test_missing_or_non_dict_tool_input_is_denied_without_raising(
        self, options: ClaudeAgentOptions
    ) -> None:
        no_input = hook_payload("Read", None)
        del no_input["tool_input"]
        assert_denied(await call_hook(options, no_input))
        for bad in ("oops", None, 7, ["a"]):
            assert_denied(await call_hook(options, hook_payload("Read", bad)))

    async def test_hostile_payloads_are_denied_without_raising(
        self, options: ClaudeAgentOptions
    ) -> None:
        assert_denied(await call_hook(options, {}))
        assert_denied(await call_hook(options, hook_payload(cast(Any, 5), {"file_path": "/x"})))
        assert_denied(await call_hook(options, cast(Any, None)))


# ---------------------------------------------------------------- F7: 예외는 거부로 닫는다


class TestFailClosed:
    @pytest.fixture
    def boom(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def explode(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("SECRET-DETAIL-xyz")

        monkeypatch.setattr(runner_module, "decide", explode)

    async def test_hook_denies_when_decide_raises(
        self, boom: None, deps: RunnerDeps, bot_input: RunClaudeInput, repos: Path
    ) -> None:
        out = await call_hook(
            build_options(bot_input, deps),
            hook_payload("Read", {"file_path": str(repos / "worker/src/A.kt")}),
        )
        assert_denied(out)
        assert "SECRET-DETAIL" not in str(out)
        assert "RuntimeError" not in str(out)

    async def test_can_use_tool_denies_when_decide_raises(
        self, boom: None, deps: RunnerDeps, bot_input: RunClaudeInput, repos: Path
    ) -> None:
        can_use_tool = build_options(bot_input, deps).can_use_tool
        assert can_use_tool is not None
        result = await can_use_tool(
            "Read", {"file_path": str(repos / "worker/src/A.kt")}, tool_context()
        )
        assert isinstance(result, PermissionResultDeny)
        assert result.message.strip() != ""
        assert "SECRET-DETAIL" not in result.message
        assert "RuntimeError" not in result.message

    async def test_hook_denies_when_decision_object_is_malformed(
        self,
        monkeypatch: pytest.MonkeyPatch,
        deps: RunnerDeps,
        bot_input: RunClaudeInput,
    ) -> None:
        monkeypatch.setattr(runner_module, "decide", lambda *a, **k: None)
        out = await call_hook(
            build_options(bot_input, deps), hook_payload("Read", {"file_path": "/x"})
        )
        assert_denied(out)

    async def test_deny_reason_is_used_when_guard_denies(
        self, monkeypatch: pytest.MonkeyPatch, deps: RunnerDeps, bot_input: RunClaudeInput
    ) -> None:
        monkeypatch.setattr(runner_module, "decide", lambda *a, **k: Deny("사유 123"))
        options = build_options(bot_input, deps)
        out = await call_hook(options, hook_payload("Read", {}))
        assert out["hookSpecificOutput"]["permissionDecisionReason"] == "사유 123"
        monkeypatch.setattr(runner_module, "decide", lambda *a, **k: Allow())
        assert await call_hook(options, hook_payload("Read", {})) == {}

    async def test_cancellation_inside_a_callback_is_not_swallowed(
        self, monkeypatch: pytest.MonkeyPatch, deps: RunnerDeps, bot_input: RunClaudeInput
    ) -> None:
        def cancel(*args: Any, **kwargs: Any) -> Any:
            raise asyncio.CancelledError

        monkeypatch.setattr(runner_module, "decide", cancel)
        with pytest.raises(asyncio.CancelledError):
            await call_hook(
                build_options(bot_input, deps), hook_payload("Read", {"file_path": "/x"})
            )


# ------------------------------------------------------- 수정 1차: 이중 취소·비밀값·백그라운드


class TestDoubleCancel:
    async def test_outer_cancel_during_grace_does_not_cancel_the_consumer_twice(
        self, deps: RunnerDeps, bot_input: RunClaudeInput
    ) -> None:
        # SDK의 정리(SIGTERM·SIGKILL 단계적 종료)는 두 번째 취소에 끊긴다.
        fake = FakeQuery([], "count-cancels")
        task = asyncio.create_task(
            run(
                deps,
                fake,
                dataclasses.replace(bot_input, timeout_ms=20),
                hard_deadline_grace_ms=1000,
            )
        )
        before = set(runner_module._background_tasks)
        await asyncio.sleep(0.1)  # 타임아웃(20ms)이 지나 유예 대기 중
        started = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert time.monotonic() - started < 0.2  # 정리를 기다리지 않고 바로 전파한다
        # 정리 중인 소비 태스크는 끝날 때까지 붙들려 있다
        assert len(runner_module._background_tasks - before) == 1
        await asyncio.sleep(0.5)  # 소비자의 정리가 끝날 때까지
        assert fake.cancel_count == 1

    async def test_outer_cancel_before_timeout_cancels_the_consumer_once(
        self, deps: RunnerDeps, bot_input: RunClaudeInput
    ) -> None:
        fake = FakeQuery([], "count-cancels")
        task = asyncio.create_task(run(deps, fake, bot_input, hard_deadline_grace_ms=1000))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.5)
        assert fake.cancel_count == 1


class TestValidation:
    def test_default_grace_is_longer_than_the_sdk_close_sequence(self, deps: RunnerDeps) -> None:
        assert deps.hard_deadline_grace_ms == 20_000

    def test_negative_grace_is_rejected(self, deps: RunnerDeps) -> None:
        with pytest.raises(ValueError):
            dataclasses.replace(deps, hard_deadline_grace_ms=-1)

    def test_zero_grace_is_allowed(self, deps: RunnerDeps) -> None:
        assert dataclasses.replace(deps, hard_deadline_grace_ms=0).hard_deadline_grace_ms == 0

    @pytest.mark.parametrize("timeout_ms", [0, -5])
    async def test_non_positive_timeout_is_an_error_result_not_a_raise(
        self, deps: RunnerDeps, bot_input: RunClaudeInput, timeout_ms: int
    ) -> None:
        fake = FakeQuery([success()])
        r = await run(deps, fake, dataclasses.replace(bot_input, timeout_ms=timeout_ms))
        assert (r.ok, r.failure) == (False, "error")
        assert fake.calls == []


class TestSecretsAndAliasing:
    def test_repr_of_deps_hides_env_values(self, root: Path) -> None:
        secret_deps = RunnerDeps(
            model="m",
            effort={"bot": "low", "sync": "medium"},
            skills_dir=root / "skills",
            env={
                "ANTHROPIC_API_KEY": "sk-ant-SENTINEL-1",
                "DISCORD_TOKEN": "discord-SENTINEL-2",
                "GITHUB_TOKEN": "ghp_SENTINEL-3",
            },
        )
        shown = repr(secret_deps)
        assert "SENTINEL" not in shown
        assert "ANTHROPIC_API_KEY" not in shown

    async def test_result_tool_calls_is_a_copy_of_what_an_abandoned_task_appends_to(
        self, deps: RunnerDeps, bot_input: RunClaudeInput
    ) -> None:
        fake = FakeQuery([], "swallow-yield")
        r = await run(
            deps,
            fake,
            dataclasses.replace(bot_input, timeout_ms=20),
            hard_deadline_grace_ms=20,
        )
        assert r.failure == "timeout"
        assert r.tool_calls == []
        for _ in range(100):  # 버려진 소비자가 뒤늦게 도구 호출을 기록할 때까지
            if fake.yielded_after_cancel:
                break
            await asyncio.sleep(0.01)
        assert fake.yielded_after_cancel is True
        assert r.tool_calls == []

    async def test_one_guard_context_is_built_per_run(
        self,
        deps: RunnerDeps,
        bot_input: RunClaudeInput,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        built: list[object] = []
        real = GuardContext

        def counting(*args: Any, **kwargs: Any) -> Any:
            ctx = real(*args, **kwargs)
            built.append(ctx)
            return ctx

        monkeypatch.setattr(runner_module, "GuardContext", counting)
        await run(deps, FakeQuery([success()]), bot_input)
        assert len(built) == 1

    def test_pre_tool_use_matcher_keeps_the_sdk_default_timeout(
        self, deps: RunnerDeps, bot_input: RunClaudeInput
    ) -> None:
        o = build_options(bot_input, deps)
        assert o.hooks is not None
        assert o.hooks["PreToolUse"][0].timeout is None


class TestBackgroundTasks:
    async def test_abandoned_task_is_strongly_held_until_it_finishes(
        self, deps: RunnerDeps, bot_input: RunClaudeInput
    ) -> None:
        before = set(runner_module._background_tasks)
        fake = FakeQuery([], "swallow-yield")
        await run(
            deps,
            fake,
            dataclasses.replace(bot_input, timeout_ms=20),
            hard_deadline_grace_ms=20,
        )
        mine = runner_module._background_tasks - before
        assert len(mine) == 1
        for _ in range(100):
            if not (runner_module._background_tasks & mine):
                break
            await asyncio.sleep(0.02)
        assert not (runner_module._background_tasks & mine)

    async def test_task_that_ends_with_unexpected_exception_is_released_and_logged_safely(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # consume()은 Exception을 모두 잡으므로 여기 닿는 것은 BaseException뿐이다.
        class Odd(BaseException):
            pass

        async def odd() -> None:
            raise Odd("SECRET-DETAIL-abc")

        task = asyncio.ensure_future(odd())
        with caplog.at_level(logging.WARNING, logger=runner_module.__name__):
            runner_module._keep_alive(task)
            assert task in runner_module._background_tasks
            await asyncio.wait({task})
            await asyncio.sleep(0)
        assert task not in runner_module._background_tasks
        assert any(r.levelno >= logging.WARNING for r in caplog.records)
        assert "SECRET-DETAIL" not in caplog.text

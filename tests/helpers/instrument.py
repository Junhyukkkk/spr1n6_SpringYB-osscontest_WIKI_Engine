"""실제 `query`를 감싸 훅·`can_use_tool` 호출을 세는 계측 래퍼. 라이브 스모크 테스트가 쓴다.

래퍼는 가드를 약화시키면 안 된다: 원래 콜백에 인자를 그대로 넘기고, 결과(또는 예외)를
그대로 돌려주며, 호출 전에 사본만 기록한다.
"""

import copy
import dataclasses
import json
import os
import time
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    HookContext,
    HookInput,
    HookJSONOutput,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    SystemMessage,
    ToolPermissionContext,
    ToolResultBlock,
    UserMessage,
)

from team_wiki_engine.claude.runner import QueryFn


@dataclass(frozen=True, slots=True)
class ToolInputCall:
    tool: str
    tool_input: dict[str, Any]
    """호출 시점의 원시 입력 사본(모델이 보낸 문자열 그대로, 공백·`~` 포함)."""


def _snapshot(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    try:
        return copy.deepcopy(value)
    except Exception:
        return dict(value)


def _flatten(content: object) -> str:
    """도구 결과 content(문자열·블록 목록·None)를 검색용 문자열 하나로 만든다."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(_flatten(item) for item in content)
    try:
        return json.dumps(content, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(content)


class _ToolResults:
    """스트림에서 본 도구 결과 본문. 메모리에만 두고 repr·로그에는 절대 내용을 싣지 않는다."""

    def __init__(self) -> None:
        self._texts: list[str] = []

    def add(self, text: str) -> None:
        self._texts.append(text)

    def contain(self, marker: str) -> bool:
        return marker != "" and any(marker in text for text in self._texts)

    def __len__(self) -> int:
        return len(self._texts)

    def __repr__(self) -> str:
        return f"<{len(self._texts)} tool results (내용 숨김)>"


def merged_env_secret_keys(
    options: ClaudeAgentOptions,
    secret_values: Iterable[str],
    environ: Mapping[str, str] | None = None,
) -> list[str]:
    """CLI가 보는 환경(`{**os.environ, **options.env}`)에서 비밀 값을 가진 키 이름들(정렬).

    SDK는 `options.env`를 부모 프로세스 환경 위에 합치므로, 키를 빼기만 한 scrub은 여기서 걸린다.
    값은 돌려주지 않는다. 빈 비밀 값은 무시한다.
    """
    secrets = {v for v in secret_values if v != ""}
    merged = {**(os.environ if environ is None else environ), **options.env}
    return sorted(key for key, value in merged.items() if value in secrets)


_USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


def _int_usage(usage: object) -> dict[str, int] | None:
    """usage가 dict이면 알려진 토큰 키 중 정수인 것만 남긴다(bool 제외). 아니면 None."""
    if not isinstance(usage, dict):
        return None
    return {
        key: value
        for key in _USAGE_KEYS
        if isinstance(value := usage.get(key), int) and not isinstance(value, bool)
    }


class Instrumentation:
    def __init__(self) -> None:
        self.hook_calls: list[ToolInputCall] = []
        self.perm_calls: list[ToolInputCall] = []
        self.init_messages: list[dict[str, Any]] = []
        """CLI가 보낸 `system/init` 메시지의 data(도구·MCP 서버 등)."""
        self.env_checks = 0
        """환경 병합 검사를 한 횟수(검사가 실제로 돌았는지 확인용)."""
        self.env_leaks: list[str] = []
        """CLI 환경에 비밀 값이 남아 있던 키 이름(값은 저장하지 않는다)."""
        self.assistant_usages: list[tuple[str | None, dict[str, int]]] = []
        """`AssistantMessage`마다 (message_id 또는 None, 정수 토큰 수)."""
        self.hook_call_times: list[float] = []
        """훅이 불린 시각(`time.monotonic()`)."""
        self.result_usage: dict[str, int] | None = None
        """`ResultMessage`의 usage(정수 토큰 수)."""
        self._tool_results = _ToolResults()

    def __repr__(self) -> str:
        return (
            f"<Instrumentation hooks={len(self.hook_calls)} perms={len(self.perm_calls)} "
            f"results={len(self._tool_results)}>"
        )

    @property
    def tool_result_count(self) -> int:
        return len(self._tool_results)

    def tool_results_contain(self, marker: str) -> bool:
        """스트림에서 본 도구 결과 어딘가에 `marker`가 있는지. 내용은 돌려주지 않는다."""
        return self._tool_results.contain(marker)

    def assert_tool_results_seen(self, *, tool_calls: int) -> None:
        """도구 호출이 있었고 init도 봤는데 도구 결과가 하나도 없으면 실패시킨다.

        그러면 "표식이 도구 결과에 없다"는 단언이 빈 스트림 위에서 공허하게 통과하므로, 스트림
        모양 가정(`UserMessage`의 `ToolResultBlock`)이 틀렸다는 신호로 본다. init을 못 봤으면
        스트림 자체를 못 본 것이라 판단하지 않는다.
        """
        if tool_calls > 0 and self.init_messages and self.tool_result_count == 0:
            raise AssertionError(
                "도구를 불렀는데 도구 결과를 하나도 못 봄 — 스트림 모양 가정이 틀렸을 수 있음"
            )

    def check_env(self, options: ClaudeAgentOptions, secret_values: Iterable[str]) -> None:
        self.env_checks += 1
        self.env_leaks.extend(merged_env_secret_keys(options, secret_values))

    def wrap_options(
        self,
        options: ClaudeAgentOptions,
        *,
        max_turns: int | None = None,
        max_budget_usd: float | None = None,
    ) -> ClaudeAgentOptions:
        """`options`를 바꾸지 않고, 훅·`can_use_tool`을 계측용으로 감싼 복사본을 돌려준다."""
        changes: dict[str, Any] = {}
        if max_turns is not None:
            changes["max_turns"] = max_turns
        if max_budget_usd is not None:
            changes["max_budget_usd"] = max_budget_usd
        hooks = options.hooks
        if hooks is not None:
            matchers = hooks.get("PreToolUse")
            if matchers and matchers[0].hooks:
                first = matchers[0]
                changes["hooks"] = {
                    **hooks,
                    "PreToolUse": [
                        dataclasses.replace(
                            first, hooks=[self._wrap_hook(first.hooks[0]), *first.hooks[1:]]
                        ),
                        *matchers[1:],
                    ],
                }
        if options.can_use_tool is not None:
            changes["can_use_tool"] = self._wrap_can_use_tool(options.can_use_tool)
        return dataclasses.replace(options, **changes)

    def _wrap_hook(self, orig: Any) -> Any:
        async def hook(
            input_data: HookInput, tool_use_id: str | None, context: HookContext
        ) -> HookJSONOutput:
            self.hook_call_times.append(time.monotonic())
            raw = cast("Mapping[str, Any]", input_data)
            name = raw.get("tool_name")
            self.hook_calls.append(
                ToolInputCall(
                    tool="" if name is None else str(name),
                    tool_input=_snapshot(raw.get("tool_input")),
                )
            )
            result: HookJSONOutput = await orig(input_data, tool_use_id, context)
            return result

        return hook

    def _wrap_can_use_tool(self, orig: Any) -> Any:
        async def can_use_tool(
            tool_name: str, tool_input: dict[str, Any], context: ToolPermissionContext
        ) -> PermissionResultAllow | PermissionResultDeny:
            self.perm_calls.append(ToolInputCall(tool=tool_name, tool_input=_snapshot(tool_input)))
            result: PermissionResultAllow | PermissionResultDeny = await orig(
                tool_name, tool_input, context
            )
            return result

        return can_use_tool

    async def tap(self, stream: AsyncIterator[Any]) -> AsyncIterator[Any]:
        """메시지를 그대로 흘려보내면서 `system/init`과 도구 결과 본문을 기록한다.

        스트림의 예외·취소는 삼키지 않고 그대로 전파한다. 도구 결과는 `tool_results_contain`으로
        불리언만 물을 수 있다(내용을 로그하거나 단언 메시지에 싣지 않는다).
        """
        async for message in stream:
            if isinstance(message, SystemMessage) and message.subtype == "init":
                self.init_messages.append(dict(message.data))
            elif isinstance(message, UserMessage):
                self._record_tool_results(message)
            elif isinstance(message, AssistantMessage):
                usage = _int_usage(message.usage)
                if usage is not None:
                    message_id = message.message_id
                    self.assistant_usages.append(
                        (message_id if isinstance(message_id, str) else None, usage)
                    )
            elif isinstance(message, ResultMessage):
                self.result_usage = _int_usage(message.usage)
            yield message

    def _record_tool_results(self, message: UserMessage) -> None:
        if isinstance(message.content, list):
            for block in message.content:
                if isinstance(block, ToolResultBlock):
                    self._tool_results.add(_flatten(block.content))
        if message.tool_use_result is not None:
            self._tool_results.add(_flatten(message.tool_use_result))


def make_query_fn(
    real_query: QueryFn,
    instr: Instrumentation,
    *,
    max_turns: int | None = None,
    max_budget_usd: float | None = None,
    secret_values: Sequence[str] = (),
) -> QueryFn:
    """`real_query`를 감싼 `query_fn`. 옵션만 계측용으로 바꾸고 스트림은 그대로 돌려준다.

    `secret_values`가 있으면 호출마다 CLI가 보는 환경에 그 값이 남았는지 검사해 기록한다.
    """

    def query_fn(*, prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Any]:
        if secret_values:
            instr.check_env(options, secret_values)
        wrapped = instr.wrap_options(options, max_turns=max_turns, max_budget_usd=max_budget_usd)
        return instr.tap(real_query(prompt=prompt, options=wrapped))

    return query_fn

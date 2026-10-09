"""Claude Agent SDK를 부르는 실행기. 권한 경계(가드·훅·환경변수)를 여기서 조립한다."""

import asyncio
import logging
import math
import time
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, get_args

from claude_agent_sdk import (
    AssistantMessage,
    CanUseTool,
    ClaudeAgentOptions,
    HookCallback,
    HookContext,
    HookMatcher,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    TextBlock,
    ToolPermissionContext,
    ToolUseBlock,
    query,
)
from claude_agent_sdk.types import SyncHookJSONOutput

from team_wiki_engine.claude.path_guard import GuardContext, decide, resolve_tool_path
from team_wiki_engine.claude.profiles import DISALLOWED_TOOLS, PROFILES
from team_wiki_engine.claude.prompt import build_system_append
from team_wiki_engine.claude.types import (
    ClaudeRunner,
    RunClaudeInput,
    RunClaudeResult,
    ToolCallTrace,
    Usage,
)
from team_wiki_engine.config.schema import Effort, EffortConfig

logger = logging.getLogger(__name__)

SECRET_ENV_KEYS: tuple[str, ...] = ("DISCORD_TOKEN", "GITHUB_TOKEN")

# 가드 안에서 예외가 나면 이 문구로 거부한다. 예외 내용은 싣지 않는다.
_GUARD_ERROR_REASON = "권한을 확인하는 중 오류가 나서 거부합니다."
_GUARD_FALLBACK_REASON = "허용되지 않은 도구 호출입니다."

_Failure = Literal["timeout", "error", "empty", "max_turns"]

_ERROR_LIST_LIMIT = 300
_EXCEPTION_DETAIL_LIMIT = 500


class QueryFn(Protocol):
    """실제 `claude_agent_sdk.query`와 같은 키워드 시그니처."""

    def __call__(self, *, prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Any]: ...


@dataclass(frozen=True, slots=True)
class RunnerDeps:
    model: str
    effort: EffortConfig | Mapping[str, Effort]
    """설정의 `EffortConfig` 또는 `{"bot": ..., "sync": ...}`. 프로필의 `effort_key`로 고른다.
    `bot`·`sync`가 모두 있고 값이 유효해야 한다(아니면 만들 때 `ValueError`)."""
    skills_dir: Path
    env: Mapping[str, str] = field(repr=False)
    """비밀 값이 들어 있을 수 있어 repr에 싣지 않는다."""
    query_fn: QueryFn | None = None
    hard_deadline_grace_ms: int = 20_000
    """`timeout_ms`가 지난 뒤 SDK의 정리를 더 기다리는 시간(0 이상). 기본값은 SDK가 CLI를 끝내는
    최악의 경우(EOF 대기 5초 + SIGTERM·SIGKILL 단계, 약 16~20초)보다 길다. 그래도 안 끝나면
    정리를 백그라운드에 두고 결과를 먼저 돌려준다."""

    def __post_init__(self) -> None:
        _check_effort(self.effort)
        if self.hard_deadline_grace_ms < 0:
            raise ValueError("hard_deadline_grace_ms는 0 이상이어야 합니다")


_EFFORT_KEYS: tuple[Literal["bot", "sync"], ...] = ("bot", "sync")
_EFFORT_VALUES: tuple[str, ...] = get_args(Effort)


def _check_effort(effort: object) -> None:
    if isinstance(effort, EffortConfig):
        return
    if not isinstance(effort, Mapping):
        raise ValueError(
            "effort는 EffortConfig이거나 {'bot': ..., 'sync': ...} 형태의 매핑이어야 합니다"
        )
    missing = [key for key in _EFFORT_KEYS if key not in effort]
    if missing:
        raise ValueError(f"effort에 {', '.join(repr(k) for k in missing)} 키가 없습니다")
    for key in _EFFORT_KEYS:
        if effort[key] not in _EFFORT_VALUES:
            raise ValueError(
                f"effort[{key!r}] 값이 올바르지 않습니다(허용: {', '.join(_EFFORT_VALUES)})"
            )


def _effort_for(effort: EffortConfig | Mapping[str, Effort], key: Literal["bot", "sync"]) -> Effort:
    if isinstance(effort, EffortConfig):
        return effort.bot if key == "bot" else effort.sync
    return effort[key]


def scrub_env(env: Mapping[str, str]) -> dict[str, str]:
    """비밀 키를 빈 문자열로 덮어쓴 복사본.

    SDK는 `options.env`를 `os.environ` 위에 합치므로 키를 빼는 것만으로는 부모 프로세스의
    값이 그대로 자식에게 간다. 빈 문자열로 덮어써야 지워진다.
    """
    scrubbed = dict(env)
    for key in SECRET_ENV_KEYS:
        scrubbed[key] = ""
    return scrubbed


def _guard_context(inp: RunClaudeInput) -> GuardContext:
    return GuardContext(cwd=inp.cwd, read_roots=inp.read_roots, write_roots=inp.write_roots)


def _make_guards(ctx: GuardContext) -> tuple[HookCallback, CanUseTool]:
    """`PreToolUse` 훅과 `can_use_tool`을 같은 `GuardContext`로 만든다.

    CLI는 허용 폴더 안의 읽기와 Claude 내부 경로(세션 기록, 자동 메모리, 계획·스크래치
    파일)를 `can_use_tool` 없이 자동 허용한다. 그래서 모든 도구 호출보다 먼저 도는 훅이
    진짜 방어선이고 `can_use_tool`은 2차 방어다. 둘 다 어떤 예외든 거부로 닫는다.
    """

    async def pre_tool_use(
        input_data: Mapping[str, Any], tool_use_id: str | None, context: HookContext
    ) -> SyncHookJSONOutput:
        try:
            tool_name = input_data.get("tool_name")
            raw_input = input_data.get("tool_input")
            tool_input = raw_input if isinstance(raw_input, dict) else {}
            decision = decide(tool_name if isinstance(tool_name, str) else "", tool_input, ctx)
            if decision.allow:
                # 'allow'를 명시하면 CLI의 정상 권한 흐름을 건너뛰므로 빈 객체를 돌려준다.
                return {}
            reason = decision.reason or _GUARD_FALLBACK_REASON
        except Exception:
            reason = _GUARD_ERROR_REASON
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }

    async def can_use_tool(
        tool_name: str, tool_input: dict[str, Any], context: ToolPermissionContext
    ) -> PermissionResultAllow | PermissionResultDeny:
        try:
            decision = decide(tool_name, tool_input, ctx)
            if decision.allow:
                return PermissionResultAllow(updated_input=tool_input)
            return PermissionResultDeny(message=decision.reason or _GUARD_FALLBACK_REASON)
        except Exception:
            return PermissionResultDeny(message=_GUARD_ERROR_REASON)

    return pre_tool_use, can_use_tool


def build_options(inp: RunClaudeInput, deps: RunnerDeps) -> ClaudeAgentOptions:
    """입력과 의존성만으로 SDK 옵션을 만든다(인자를 바꾸지 않고 SDK도 부르지 않는다)."""
    return _build_options(inp, deps, _guard_context(inp))


def _build_options(inp: RunClaudeInput, deps: RunnerDeps, ctx: GuardContext) -> ClaudeAgentOptions:
    profile = PROFILES[inp.profile]
    pre_tool_use, can_use_tool = _make_guards(ctx)
    return ClaudeAgentOptions(
        model=deps.model,
        effort=_effort_for(deps.effort, profile.effort_key),
        cwd=str(inp.cwd),
        add_dirs=[str(root) for root in inp.read_roots],
        tools=list(profile.tools),
        disallowed_tools=list(DISALLOWED_TOOLS),
        permission_mode="default",
        can_use_tool=can_use_tool,
        # matcher가 없으면 모든 도구에 적용된다. timeout은 SDK 기본값을 그대로 둔다(줄이지 않는다).
        hooks={"PreToolUse": [HookMatcher(hooks=[pre_tool_use])]},
        # 사용자·프로젝트 설정과 CLAUDE.md를 읽지 않고, 외부 MCP 설정도 무시한다.
        setting_sources=[],
        strict_mcp_config=True,
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": build_system_append(inp.profile, deps.skills_dir),
        },
        env={**scrub_env(deps.env), "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"},
        max_turns=profile.max_turns,
        resume=inp.resume_session_id,
    )


# ---------------------------------------------------------------- 결과 수집


def _count(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(value, 0)
    if isinstance(value, float) and math.isfinite(value):
        return max(int(value), 0)
    return 0


def _usage_from(raw: object) -> Usage:
    if not isinstance(raw, Mapping):
        return Usage()
    return Usage(
        input_tokens=_count(raw.get("input_tokens")),
        output_tokens=_count(raw.get("output_tokens")),
        cache_read_input_tokens=_count(raw.get("cache_read_input_tokens")),
        cache_creation_input_tokens=_count(raw.get("cache_creation_input_tokens")),
    )


def _sum_usage(parts: list[Usage]) -> Usage:
    return Usage(
        input_tokens=sum(p.input_tokens for p in parts),
        output_tokens=sum(p.output_tokens for p in parts),
        cache_read_input_tokens=sum(p.cache_read_input_tokens for p in parts),
        cache_creation_input_tokens=sum(p.cache_creation_input_tokens for p in parts),
    )


def _cost(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value) if math.isfinite(value) else 0.0


def _trace(name: str, tool_input: dict[str, Any], ctx: GuardContext) -> ToolCallTrace:
    # `allowed`는 가드의 판정일 뿐이다(ToolCallTrace 독스트링). 가드가 실패하면 거부로 적는다.
    try:
        path = resolve_tool_path(name, tool_input, ctx.cwd)
        allowed = decide(name, tool_input, ctx).allow
    except Exception:
        return ToolCallTrace(name=name, path=None, allowed=False)
    return ToolCallTrace(name=name, path=path, allowed=allowed)


@dataclass
class _Collected:
    """스트림에서 모은 것. 소비 태스크가 채우고, 취소·버려진 뒤에도 읽을 수 있다."""

    result: ResultMessage | None = None
    thrown: Exception | None = None
    timed_out: bool = False
    last_assistant_text: str = ""
    tool_calls: list[ToolCallTrace] = field(default_factory=list)
    # message_id별 마지막 usage. SDK는 한 API 응답의 블록마다 메시지를 내보내 usage가 겹친다.
    _assistant_usage: dict[str, Usage] = field(default_factory=dict)
    _anonymous_usage: list[Usage] = field(default_factory=list)

    def feed(self, message: object, ctx: GuardContext) -> None:
        if isinstance(message, AssistantMessage):
            self._on_assistant(message, ctx)
        elif isinstance(message, ResultMessage):
            self.result = message
        # 그 밖의 메시지(시스템·스트림 이벤트·알 수 없는 객체)는 무시한다.

    def _on_assistant(self, message: AssistantMessage, ctx: GuardContext) -> None:
        if message.usage is not None:
            usage = _usage_from(message.usage)
            if isinstance(message.message_id, str):
                self._assistant_usage[message.message_id] = usage
            else:
                self._anonymous_usage.append(usage)
        if not isinstance(message.content, list):
            return
        texts: list[str] = []
        for block in message.content:
            if isinstance(block, TextBlock) and isinstance(block.text, str):
                texts.append(block.text)
            elif isinstance(block, ToolUseBlock) and isinstance(block.name, str):
                tool_input = block.input if isinstance(block.input, dict) else {}
                self.tool_calls.append(_trace(block.name, tool_input, ctx))
        if texts:
            self.last_assistant_text = "".join(texts)

    def to_result(self, *, elapsed_ms: int, timeout_ms: int) -> RunClaudeResult:
        r = self.result
        result_text = r.result if r is not None and isinstance(r.result, str) else ""
        text = (result_text if result_text.strip() else self.last_assistant_text).strip()
        if r is not None:
            usage = _usage_from(r.usage)
            session_id = r.session_id if isinstance(r.session_id, str) else None
            cost = _cost(r.total_cost_usd)
            duration = r.duration_ms if isinstance(r.duration_ms, int) else elapsed_ms
        else:
            # 결과 없이 끝난 실행(타임아웃·예외)도 도착한 usage로 비용의 하한을 남긴다.
            usage = _sum_usage([*self._assistant_usage.values(), *self._anonymous_usage])
            session_id = None
            cost = 0.0
            duration = elapsed_ms
        failure, detail = self._classify(timeout_ms, text)
        return RunClaudeResult(
            ok=failure is None,
            text=text,
            session_id=session_id,
            cost_usd=cost,
            usage=usage,
            duration_ms=duration,
            # 버려진 소비 태스크가 뒤늦게 이 목록에 추가할 수 있어 복사본을 돌려준다.
            tool_calls=list(self.tool_calls),
            failure=failure,
            detail=detail,
        )

    def _classify(self, timeout_ms: int, text: str) -> tuple[_Failure | None, str | None]:
        """결과가 있으면 결과로만 분류한다. 뒤늦은 예외(CLI가 오류 결과 뒤 비정상 종료하면
        SDK가 던진다)와 결과 뒤에 울린 타임아웃은 무시한다."""
        r = self.result
        if r is None:
            if self.timed_out:
                return "timeout", f"{timeout_ms}ms 초과"
            if self.thrown is not None:
                message = str(self.thrown) or type(self.thrown).__name__
                return "error", message[:_EXCEPTION_DETAIL_LIMIT]
            return "error", "result 메시지가 없습니다"
        subtype = r.subtype if isinstance(r.subtype, str) else ""
        if "max_turns" in subtype:
            return "max_turns", subtype
        if r.is_error is True:
            detail = subtype or "is_error"
            if isinstance(r.errors, list) and r.errors:
                errors = "; ".join(str(e) for e in r.errors)[:_ERROR_LIST_LIMIT]
                if errors.strip():
                    detail = f"{detail}: {errors}"
            elif isinstance(r.result, str) and r.result.strip():
                # 오류 결과의 본문은 Claude 프로그램이 낸 사유 문구다(잘못된 키·잔액 부족 등).
                # 안 보이면 진단이 안 되므로 앞부분만 붙인다. 밖으로 나갈 때 호출자가 가린다.
                detail = f"{detail}: {r.result.strip()[:_ERROR_LIST_LIMIT]}"
            return "error", detail
        if text == "":
            return "empty", "응답 텍스트가 비어 있습니다"
        return None, None


# asyncio는 태스크를 약한 참조로만 들고 있어, 러너가 돌아온 뒤에도 이어지는 정리 태스크는
# 여기서 붙들어 둔다(끝나면 스스로 빠진다).
_background_tasks: set[asyncio.Task[None]] = set()


def _on_background_done(task: asyncio.Task[None]) -> None:
    _background_tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        # 예외 내용에 비밀이 섞일 수 있어 종류만 남긴다.
        logger.warning(
            "백그라운드 Claude 스트림이 예기치 않은 오류로 끝났습니다 (%s)", type(exc).__name__
        )


def _keep_alive(task: asyncio.Task[None]) -> None:
    if task.done():
        return
    _background_tasks.add(task)
    task.add_done_callback(_on_background_done)


async def drain_background_tasks(timeout: float) -> int:
    """시간 초과로 버려진 Claude 스트림의 정리가 끝나기를 최대 `timeout`초 기다린다.

    아직 끝나지 않은 정리 태스크 수를 돌려준다(0이면 모두 끝났다). 기다리는 대상은 지금 도는
    이벤트 루프에 속한 미완료 태스크뿐이다. 끝난 태스크와 닫힌 루프의 태스크는 집합에서 빼고
    세지 않는다. 시간이 지나도 남은 태스크는 **취소하지 않는다**: 두 번째 취소는 SDK의
    SIGTERM·SIGKILL 정리를 끊어 CLI 프로세스를 남길 수 있다. 남았으면 개수만 경고로 남긴다.
    바깥에서 온 `CancelledError`는 그대로 전파하고, 태스크가 예외로 끝나도 다시 던지지 않는다.

    부르는 곳(계획 2 G1): (a) Claude 쓰기 실행 직후(`run_write_step`), (b) 코드 반영 단계와
    문서 반영 단계 사이, (c) 동기화 `main` 종료 직전. 정리되지 않은 실행이 남아 있으면 그
    단계는 커밋하지 않고 실패로 끝내며 다음 단계는 건너뛴다.
    """
    loop = asyncio.get_running_loop()
    wait_s = timeout if timeout > 0 else 0.0  # 음수·NaN은 0으로 본다(`max(timeout, 0.0)`과 같다)
    deadline = loop.time() + wait_s
    while True:
        pending = _pending_on(loop)
        if not pending:
            return 0
        remaining = max(deadline - loop.time(), 0.0)
        # 기다리는 동안 이 태스크들이 새 정리 태스크를 만들 수 있어, 끝나면 다시 훑는다.
        await asyncio.wait(pending, timeout=None if math.isinf(remaining) else remaining)
        if loop.time() >= deadline:
            break
    still_running = len(_pending_on(loop))
    if still_running:
        logger.warning("백그라운드 Claude 정리가 아직 끝나지 않았습니다 (%d개 남음)", still_running)
    return still_running


def _pending_on(loop: asyncio.AbstractEventLoop) -> list[asyncio.Task[None]]:
    """끝난 태스크와 닫힌 루프의 태스크를 집합에서 빼고, 이 루프의 미완료 태스크를 돌려준다."""
    pending: list[asyncio.Task[None]] = []
    for task in list(_background_tasks):
        if task.done():
            _background_tasks.discard(task)
            if not task.cancelled():
                # 예외를 가져가 두어 asyncio가 GC 때 내용을 로그로 찍지 않게 한다.
                task.exception()
        elif task.get_loop().is_closed():
            _background_tasks.discard(task)
        elif task.get_loop() is loop:
            pending.append(task)
    return pending


async def _drive(
    collected: _Collected,
    query_fn: QueryFn,
    prompt: str,
    options: ClaudeAgentOptions,
    ctx: GuardContext,
    *,
    timeout_ms: int,
    grace_ms: int,
) -> None:
    """스트림을 태스크에서 끝까지 소비한다. 시간이 넘으면 태스크를 취소하고, 취소를 삼키는
    이터레이터는 유예 시간 뒤 버린다. 바깥에서 온 취소는 그대로 전파한다.

    `async for`를 중간에 빠져나오지 않는 것은 SDK 정리 코드가 같은 태스크에서 끝나야 하기 때문이다.
    """

    async def consume() -> None:
        try:
            async for message in query_fn(prompt=prompt, options=options):
                collected.feed(message, ctx)
        except Exception as exc:  # CancelledError는 BaseException이라 지나간다
            collected.thrown = exc

    task = asyncio.create_task(consume())
    cancel_sent = False
    try:
        done, _ = await asyncio.wait({task}, timeout=timeout_ms / 1000)
        if done:
            return
        collected.timed_out = True
        cancel_sent = True
        task.cancel()
        done, _ = await asyncio.wait({task}, timeout=grace_ms / 1000)
        if not done:
            # 취소를 삼키거나 정리가 긴 스트림. 기다리지 않고 결과를 먼저 돌려준다.
            logger.warning(
                "Claude 스트림 정리가 길어져 결과를 먼저 돌려줍니다 (grace=%dms); "
                "정리는 백그라운드에서 계속됩니다",
                grace_ms,
            )
            _keep_alive(task)
    except asyncio.CancelledError:
        # 바깥에서 온 취소. 소비 태스크는 한 번만 취소한다: SDK의 정리(SIGTERM·SIGKILL 단계)는
        # 두 번째 취소에 끊겨 CLI 프로세스가 남을 수 있다. 정리를 기다리지 않고 바로 전파하되,
        # 정리가 끝날 때까지 태스크를 붙들어 둔다.
        if not cancel_sent:
            task.cancel()
        _keep_alive(task)
        raise


def create_claude_runner(deps: RunnerDeps) -> ClaudeRunner:
    query_fn: QueryFn = deps.query_fn if deps.query_fn is not None else query

    async def run_claude(inp: RunClaudeInput) -> RunClaudeResult:
        started = time.monotonic()
        collected = _Collected()
        try:
            if inp.timeout_ms <= 0:
                raise ValueError("timeout_ms는 양수여야 합니다")
            ctx = _guard_context(inp)
            options = _build_options(inp, deps, ctx)
        except Exception as exc:  # 스킬 파일 없음, 상대 경로 등. 던지지 않고 결과로 돌려준다.
            collected.thrown = exc
        else:
            await _drive(
                collected,
                query_fn,
                inp.prompt,
                options,
                ctx,
                timeout_ms=inp.timeout_ms,
                grace_ms=deps.hard_deadline_grace_ms,
            )
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return collected.to_result(elapsed_ms=elapsed_ms, timeout_ms=inp.timeout_ms)

    return run_claude

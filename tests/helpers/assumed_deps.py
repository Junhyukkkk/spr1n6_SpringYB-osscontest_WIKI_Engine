"""다른 계획(2 Task 17, 계획 3 봇)이 아직 이 작업 트리에 없을 때 쓰는 가정 값을 한 곳에 모은다.

실제 심볼이 있으면 그것을 쓰고, 없으면 계획 문서(plan2-sync-py, plan3-bot-py)의 값을 쓴다.
병합 뒤 `ASSUMED`가 비어 있어야 한다(test_units가 목록을 보고서용으로 노출한다).
"""

from __future__ import annotations

import importlib
from typing import Any

ASSUMED: list[str] = []


def _pick(module: str, name: str, fallback: Any) -> Any:
    try:
        return getattr(importlib.import_module(module), name)
    except (ImportError, AttributeError):
        ASSUMED.append(f"{module}.{name}={fallback!r}")
        return fallback


# 계획 3 (plan3-bot-py.md Task 4·8·10)
SHUTDOWN_GRACE_S: float = _pick("team_wiki_engine.bot.app", "SHUTDOWN_GRACE_S", 60.0)
CANCEL_WAIT_S: float = _pick("team_wiki_engine.bot.app", "CANCEL_WAIT_S", 10.0)
GATEWAY_CLOSE_WAIT_S: float = _pick("team_wiki_engine.bot.app", "GATEWAY_CLOSE_WAIT_S", 10.0)
REFRESH_STOP_WAIT_S: float = _pick("team_wiki_engine.bot.app", "REFRESH_STOP_WAIT_S", 15.0)
DATA_LOCK_WAIT_S: float = _pick("team_wiki_engine.bot.main", "DATA_LOCK_WAIT_S", 30.0)
REFRESH_TIMEOUT_S: float = _pick("team_wiki_engine.bot.refresh", "REFRESH_TIMEOUT_S", 180.0)
# BotDeps.settle_timeout_s 기본값(plan3 Task 10)
BOT_SETTLE_S: float = 30.0
try:
    import dataclasses

    _bot_deps = importlib.import_module("team_wiki_engine.bot.app").BotDeps
    BOT_SETTLE_S = float(
        next(f.default for f in dataclasses.fields(_bot_deps) if f.name == "settle_timeout_s")  # type: ignore[arg-type]
    )
except (ImportError, AttributeError, StopIteration):
    ASSUMED.append("team_wiki_engine.bot.app.BotDeps.settle_timeout_s=30.0")

# 콘솔 스크립트: 계획 2 Task 17(sync/pull)·계획 3(bot)이 pyproject에 더한다.
ASSUMED_SCRIPTS: frozenset[str] = frozenset({"team-wiki-sync", "team-wiki-pull", "team-wiki-bot"})

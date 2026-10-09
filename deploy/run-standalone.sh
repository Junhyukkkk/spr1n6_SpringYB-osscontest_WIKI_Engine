#!/usr/bin/env bash
# sudo·systemd 없이 폴더 하나에서 봇과 동기화 일정을 돌린다(root가 없는 공용 서버용).
# 비밀값은 시작할 때 입력받아 이 세션의 메모리에만 둔다. 디스크에 쓰지 않으므로 세션이 끝나면 다시 입력한다.
#   사용(tmux 안에서): bash <폴더>/engine/deploy/run-standalone.sh [<폴더>]
#   폴더: env/(python 3.12·git 2.32 이상) · engine/(이 저장소와 .venv) · team.json · work/ · logs/
# systemd 샌드박스가 없어 도구 제한은 엔진 안의 경로 검사·훅만 남는다. 재시작과 실패 알림은 이 스크립트가 한다.
set -euo pipefail
umask 077

ENGINE="$(cd "$(dirname "$0")/.." && pwd)"
BASE="$(cd "${1:-$ENGINE/..}" && pwd)"
BIN="${TEAM_WIKI_BIN:-$ENGINE/.venv/bin}" # 테스트용으로 바꿀 수 있다
LOGS="$BASE/logs"

export TEAM_WIKI_HOME="$BASE/work" TEAM_WIKI_CONFIG="$BASE/team.json" TEAM_WIKI_ENGINE_DIR="$ENGINE"
export HOME="$BASE/work/home" PATH="$BASE/env/bin:$PATH"
export LANG=C.UTF-8 PYTHONUTF8=1 PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1

say() { printf '[%s] %s\n' "$(date '+%m-%d %H:%M:%S')" "$*"; }
notify() { "$BIN/team-wiki-notify" "$1" >>"$LOGS/notify.log" 2>&1 || true; }

if [ ! -f "$TEAM_WIKI_CONFIG" ]; then
  say "설정 파일이 없습니다: $TEAM_WIKI_CONFIG"
  exit 2
fi
# 공용 서버의 다른 사용자가 설정·위키·로그를 읽지 못하게 한다(tmpfiles 대신).
chmod 700 "$BASE"
for dir in "$BASE/work" "$BASE/work/home" "$BASE/work/state" "$BASE/work/state/runs" "$BASE/work/repos" "$LOGS"; do
  mkdir -p "$dir"
  chmod 700 "$dir"
done

for name in ANTHROPIC_API_KEY DISCORD_TOKEN GITHUB_TOKEN; do
  IFS= read -r -s -p "$name: " value
  printf '\n'
  export "$name=$value"
done
unset value

"$BIN/team-wiki-preflight" --role bot
"$BIN/team-wiki-preflight" --role sync

# 다음 동기화까지 남은 초와 그 시각. 일정·시간대는 team.json에서 읽는다(systemd 타이머 대신).
next_slot() {
  "$BIN/python" - <<'PY'
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from team_wiki_engine.config.load import load_config

config = load_config(os.environ["TEAM_WIKI_CONFIG"])
now = datetime.now(ZoneInfo(config.timezone))
slots = []
for day in (0, 1):
    for hhmm in config.schedule:
        hour, minute = (int(part) for part in hhmm.split(":"))
        slot = (now + timedelta(days=day)).replace(hour=hour, minute=minute, second=0, microsecond=0)
        if slot > now:
            slots.append(slot)
nxt = min(slots)
print(int((nxt - now).total_seconds()) + 1, nxt.strftime("%m-%d %H:%M"))
PY
}

# systemd 유닛과 같은 규칙: 30초 뒤 다시 띄우고, 종료 코드 2(설정·토큰)거나 10분에 10번이면 멈추고 알린다.
run_bot() {
  local code window=0 count=0 now
  while :; do
    code=0
    "$BIN/team-wiki-bot" >>"$LOGS/bot.log" 2>&1 || code=$?
    say "봇이 끝났습니다(코드 $code)"
    if [ "$code" -eq 2 ]; then
      notify team-wiki-bot
      say "봇을 멈췄습니다. $LOGS/bot.log 를 확인하세요"
      return 0
    fi
    now="$(date +%s)"
    if [ $((now - window)) -ge 600 ]; then
      window="$now"
      count=0
    fi
    count=$((count + 1))
    if [ "$count" -ge 10 ]; then
      notify team-wiki-bot
      say "봇이 10분 안에 10번 끝나 멈췄습니다. $LOGS/bot.log 를 확인하세요"
      return 0
    fi
    sleep 30
  done
}

# 종료 코드 1은 엔진이 이미 운영 채널에 알렸으므로 다시 알리지 않는다(유닛의 SuccessExitStatus=1).
run_sync() {
  local code=0
  if ! "$BIN/team-wiki-preflight" --role sync --quiet >>"$LOGS/sync.log" 2>&1; then
    notify team-wiki-sync
    say "동기화 전 점검이 실패했습니다. $LOGS/sync.log 를 확인하세요"
    return 0
  fi
  say "동기화를 시작합니다"
  "$BIN/team-wiki-sync" >>"$LOGS/sync.log" 2>&1 || code=$?
  say "동기화가 끝났습니다(코드 $code)"
  if [ "$code" -ne 0 ] && [ "$code" -ne 1 ]; then
    notify team-wiki-sync
  fi
}

cleanup() {
  trap - INT TERM EXIT
  say "멈춥니다"
  kill 0 2>/dev/null || true
}
trap cleanup INT TERM EXIT

run_bot &
say "봇을 시작했습니다. 로그: $LOGS/bot.log"
while :; do
  slot="$(next_slot)"
  say "다음 동기화: ${slot#* }"
  sleep "${slot%% *}"
  run_sync
done

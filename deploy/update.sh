#!/usr/bin/env bash
# 엔진 업데이트와 되돌리기. 동기화·pull이 도는 중이면 하지 않는다.
# 이 스크립트는 사용자가 승인한 뒤에만 서버에서 실행한다(deploy/README.md §9).
#   업데이트: sudo bash /opt/team-wiki-engine/deploy/update.sh
#   번들에서: sudo ENGINE_REMOTE=/tmp/team-wiki-engine.bundle bash /opt/team-wiki-engine/deploy/update.sh
#   되돌리기: sudo bash /opt/team-wiki-engine/deploy/update.sh --rollback
#   DRY_RUN=1 이면 바꾸는 명령을 출력만 한다(상태 조회는 실제로 한다).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=deploy/lib.sh
. "$HERE/lib.sh"

ENGINE_REMOTE="${ENGINE_REMOTE:-origin}"
ENGINE_REF="${ENGINE_REF:-main}"
MODE=update
[ $# -le 1 ] || die 2 "사용법: update.sh [--rollback]"
case "${1:-}" in
  '') ;;
  --rollback) MODE=rollback ;;
  *) die 2 "사용법: update.sh [--rollback]" ;;
esac
case "$ENGINE_REMOTE" in '' | -*) die 2 "ENGINE_REMOTE 형식이 올바르지 않습니다" ;; esac
check_ref "$ENGINE_REF"
need_root

# 1. 타이머를 멈춘다. 끝나면(실패해도) 원래 켜져 있던 타이머만 다시 켠다.
ACTIVE_TIMERS=""
for t in $TIMERS; do
  if unit_busy "$t"; then ACTIVE_TIMERS="$ACTIVE_TIMERS $t"; fi
done
restore_timers() {
  if [ -n "$ACTIVE_TIMERS" ]; then
    # shellcheck disable=SC2086
    run systemctl start $ACTIVE_TIMERS
  fi
}
if [ -n "$ACTIVE_TIMERS" ]; then
  # shellcheck disable=SC2086
  run systemctl stop $ACTIVE_TIMERS
fi
trap restore_timers EXIT

# 2. 동기화·pull이 도는 중이면 멈춘다(oneshot 실행 중은 activating).
for s in team-wiki-sync.service team-wiki-pull.service; do
  if unit_busy "$s" && [ -z "${FORCE:-}" ]; then
    die 1 "$s 가 도는 중입니다. 끝난 뒤 다시 하거나 FORCE=1로 실행하세요"
  fi
done

# 3. 상태 백업(측정 기록 보존)
run env TEAM_WIKI_HOME="$APP_HOME" TEAM_WIKI_CONFIG="$ETC_DIR/team.json" "$ENGINE_DIR/.venv/bin/team-wiki-backup" --dest "$BACKUP_DIR"

# 4. 코드 바꾸기
current="$("$GIT" -C "$ENGINE_DIR" rev-parse HEAD)"
if [ "$MODE" = update ]; then
  run install -d -o root -g root -m 0700 "$DEPLOY_STATE"
  if [ -n "${DRY_RUN:-}" ]; then
    say "+ 직전 커밋 기록: $current > $DEPLOY_STATE/engine-previous"
  else
    printf '%s\n' "$current" >"$DEPLOY_STATE/engine-previous"
  fi
  run git -C "$ENGINE_DIR" fetch --quiet -- "$ENGINE_REMOTE" "$ENGINE_REF"
  run git -C "$ENGINE_DIR" merge --ff-only --quiet FETCH_HEAD
else
  prev_file="$DEPLOY_STATE/engine-previous"
  [ -f "$prev_file" ] || die 1 "되돌릴 커밋 기록이 없습니다: $prev_file"
  prev="$(head -n 1 "$prev_file")"
  case "$prev" in '' | *[!0-9a-f]*) die 1 "커밋 기록이 올바르지 않습니다: $prev_file" ;; esac
  [ "${#prev}" -eq 40 ] || die 1 "커밋 기록이 올바르지 않습니다: $prev_file"
  run git -C "$ENGINE_DIR" reset --hard --quiet "$prev"
fi

# 5. 의존성·시스템 파일·봇 재시작(봇이 돌고 있을 때만)
install_engine_venv
install_system_files
run systemctl try-restart team-wiki-bot.service
say "끝났습니다($MODE, 이전 커밋 $current). 동기화·pull은 다음 타이머 실행부터 새 코드로 돈다."
say "확인: journalctl -u team-wiki-bot.service -n 50 --no-pager"

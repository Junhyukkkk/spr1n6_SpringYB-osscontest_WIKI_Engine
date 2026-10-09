# shellcheck shell=bash
# deploy/*.sh 공용 함수와 경로. bash 3.2(맥 테스트)와 5.x(서버) 모두에서 돈다.
# 경로는 환경변수로 바꿀 수 있다(테스트용). 기본값이 서버 경로다.
APP_USER="${APP_USER:-teamwiki}"
APP_HOME="${APP_HOME:-/srv/team-wiki}"
ENGINE_DIR="${ENGINE_DIR:-/opt/team-wiki-engine}"
ETC_DIR="${ETC_DIR:-/etc/team-wiki}"
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
TMPFILES_DIR="${TMPFILES_DIR:-/etc/tmpfiles.d}"
JOURNALD_DIR="${JOURNALD_DIR:-/etc/systemd/journald.conf.d}"
DEPLOY_STATE="${DEPLOY_STATE:-/var/lib/team-wiki-deploy}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/team-wiki}"
PYTHON="${PYTHON:-python3.12}"
SYSTEMCTL="${SYSTEMCTL:-systemctl}" # 상태 조회 전용. DRY_RUN에서도 실제로 부른다
GIT="${GIT:-git}"                   # 조회(rev-parse) 전용. 바꾸는 git 명령은 run으로 부른다
UNITS="team-wiki-bot.service team-wiki-sync.service team-wiki-sync.timer team-wiki-pull.service team-wiki-notify@.service team-wiki-smoke.service"
TIMERS="team-wiki-sync.timer"

say() { printf '%s\n' "$*"; }
die() {
  local code="$1"
  shift
  printf '%s\n' "$*" >&2
  exit "$code"
}
run() {
  if [ -n "${DRY_RUN:-}" ]; then
    printf '+ %s\n' "$*"
  else
    "$@"
  fi
}
need_root() {
  if [ -z "${DRY_RUN:-}" ] && [ "$(id -u)" -ne 0 ]; then
    die 1 "root 권한으로 실행하세요 (sudo)"
  fi
}
check_ref() {
  case "$1" in
    '' | -* | *..* | *[!A-Za-z0-9._/-]*) die 2 "ENGINE_REF 형식이 올바르지 않습니다: $1" ;;
  esac
}
unit_busy() {
  local state
  state="$("$SYSTEMCTL" is-active "$1" 2>/dev/null || true)"
  case "$state" in
    active | activating | deactivating | reloading | refreshing) return 0 ;;
  esac
  return 1
}
install_engine_venv() {
  run "$ENGINE_DIR/.venv/bin/pip" install --disable-pip-version-check --quiet -e "${ENGINE_DIR}[dev]"
  run chown -R root:root "$ENGINE_DIR"
  run chmod -R go-w "$ENGINE_DIR"
}
install_system_files() {
  local unit
  for unit in $UNITS; do
    run install -o root -g root -m 0644 "$ENGINE_DIR/deploy/systemd/$unit" "$SYSTEMD_DIR/$unit"
  done
  run install -o root -g root -m 0644 "$ENGINE_DIR/deploy/tmpfiles/team-wiki.conf" "$TMPFILES_DIR/team-wiki.conf"
  run install -d -o root -g root -m 0755 "$JOURNALD_DIR"
  run install -o root -g root -m 0644 "$ENGINE_DIR/deploy/journald/team-wiki.conf" "$JOURNALD_DIR/team-wiki.conf"
  run systemd-tmpfiles --create "$TMPFILES_DIR/team-wiki.conf"
  run systemctl restart systemd-journald.service
  run systemctl daemon-reload
}

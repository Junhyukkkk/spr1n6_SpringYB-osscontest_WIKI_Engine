#!/usr/bin/env bash
# team-wiki-engine 서버 설치(Ubuntu 24.04 aarch64). 여러 번 실행해도 결과가 같다.
# 이 스크립트는 사용자가 배포를 승인한 뒤에만 서버에서 실행한다(deploy/README.md).
#   사용: sudo ENGINE_REPO=<엔진 저장소 URL 또는 git 번들 경로> bash deploy/setup.sh
#   DRY_RUN=1 이면 바꾸는 명령을 '+ 명령'으로 출력만 한다.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=deploy/lib.sh
. "$HERE/lib.sh"

ENGINE_REPO="${ENGINE_REPO:-}"
ENGINE_REF="${ENGINE_REF:-main}"
[ -n "$ENGINE_REPO" ] || die 2 "ENGINE_REPO에 엔진 저장소 URL(또는 git 번들 경로)을 지정하세요"
case "$ENGINE_REPO" in -*) die 2 "ENGINE_REPO 형식이 올바르지 않습니다" ;; esac
check_ref "$ENGINE_REF"
need_root

# 1. 패키지: git(2.32 이상 — Ubuntu 24.04는 2.43), Python 3.12와 venv, 시간대 자료, 인증서
run apt-get update
run apt-get install -y --no-install-recommends git python3.12 python3.12-venv tzdata ca-certificates

# 2. 서비스 사용자. sudo 없음, 로그인 셸 없음. 홈은 ProtectHome에 막히지 않게 작업 폴더 아래.
if ! id -u "$APP_USER" >/dev/null 2>&1; then
  run useradd --system --user-group --home-dir "$APP_HOME/home" --no-create-home --shell /usr/sbin/nologin "$APP_USER"
fi

# 3. 엔진: root 소유, 작업 폴더 밖. 서비스 사용자는 엔진을 고칠 수 없다.
if [ ! -d "$ENGINE_DIR/.git" ]; then
  run git clone --quiet --branch "$ENGINE_REF" -- "$ENGINE_REPO" "$ENGINE_DIR"
fi
if [ ! -x "$ENGINE_DIR/.venv/bin/python" ]; then
  run "$PYTHON" -m venv "$ENGINE_DIR/.venv"
fi
install_engine_venv

# 4. 설정·비밀값. 이미 있으면 덮어쓰지 않는다.
run install -d -o root -g "$APP_USER" -m 0750 "$ETC_DIR"
if [ ! -e "$ETC_DIR/team.json" ]; then
  run install -o root -g "$APP_USER" -m 0640 "$ENGINE_DIR/deploy/config/team.local.json" "$ETC_DIR/team.json"
fi
if [ ! -e "$ETC_DIR/secrets.env" ]; then
  run install -o root -g root -m 0600 "$ENGINE_DIR/deploy/secrets.env.example" "$ETC_DIR/secrets.env"
fi

# 5. 폴더(tmpfiles)·로그 한도(journald)·유닛. enable만 하고 start하지 않는다.
install_system_files
run systemctl enable team-wiki-bot.service team-wiki-sync.timer

say "설치를 마쳤습니다. 아직 어떤 서비스도 시작하지 않았습니다."
say "다음: $ETC_DIR/team.json 과 $ETC_DIR/secrets.env 를 채우고 deploy/README.md의 '배포 전 관문'을 따르세요."

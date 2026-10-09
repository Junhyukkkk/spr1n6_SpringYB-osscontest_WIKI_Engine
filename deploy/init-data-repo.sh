#!/usr/bin/env bash
# 템플릿으로 데이터 저장소(bare)를 만든다. 맥의 로컬 실행과 서버의 delivery: local 모드에서 쓴다.
#   사용: bash deploy/init-data-repo.sh <만들 bare 저장소의 절대 경로>
# 이미 있으면 아무것도 하지 않고 실패한다. 네트워크를 쓰지 않는다.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
TEMPLATE="$HERE/data-repo-template"
if [ $# -ne 1 ]; then
  printf '%s\n' "사용법: init-data-repo.sh <bare 저장소 절대 경로>" >&2
  exit 2
fi
dest="$1"
case "$dest" in
  /*) ;;
  *)
    printf '%s\n' "절대 경로를 주세요: $dest" >&2
    exit 2
    ;;
esac
if [ -e "$dest" ]; then
  printf '%s\n' "이미 있습니다: $dest" >&2
  exit 1
fi
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 GIT_TERMINAL_PROMPT=0
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
git init --quiet --bare --initial-branch=main "$dest"
git init --quiet --initial-branch=main "$work/seed"
cp -R "$TEMPLATE"/. "$work/seed"/
git -C "$work/seed" add -A
git -C "$work/seed" -c user.name=team-wiki-bot -c user.email=team-wiki-bot@users.noreply.github.com \
  -c commit.gpgsign=false commit --quiet -m "chore: 데이터 저장소 시작"
git -C "$work/seed" push --quiet "$dest" main:main
printf '%s\n' "만들었습니다: $dest (main)"

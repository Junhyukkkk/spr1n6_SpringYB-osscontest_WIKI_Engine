#!/usr/bin/env bash
# 동기화 PR(브랜치 sync/…)은 wiki/·baseline.json·metrics/만 바꿀 수 있다.
# 서버의 사후 검사를 GitHub 쪽에서 한 번 더 한다(서버가 뚫려도 raw/나 워크플로를 바꾼 PR이 통과하지 않게).
#   사용: check-sync-paths.sh <기준 커밋 SHA> <PR 머리 커밋 SHA>   (저장소 안에서)
set -euo pipefail
is_sha() {
  case "$1" in '' | *[!0-9a-f]*) return 1 ;; esac
  [ "${#1}" -eq 40 ]
}
if [ $# -ne 2 ] || ! is_sha "$1" || ! is_sha "$2"; then
  printf '%s\n' "사용법: check-sync-paths.sh <기준 SHA 40자> <머리 SHA 40자>" >&2
  exit 2
fi
list="$(mktemp)"
trap 'rm -f "$list"' EXIT
git -c core.quotepath=false diff --name-only --no-renames -z "$1...$2" -- >"$list"
bad=0
while IFS= read -r -d '' path; do
  case "$path" in
    wiki/* | metrics/* | baseline.json) ;;
    *)
      printf '허용되지 않은 경로: %q\n' "$path" >&2
      bad=1
      ;;
  esac
done <"$list"
if [ "$bad" -ne 0 ]; then
  printf '%s\n' "동기화 PR은 wiki/·baseline.json·metrics/만 바꿀 수 있습니다" >&2
fi
exit "$bad"

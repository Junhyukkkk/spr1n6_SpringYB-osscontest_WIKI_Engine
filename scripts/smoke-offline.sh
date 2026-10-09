#!/usr/bin/env bash
# 오프라인 스모크: 네트워크·실제 Claude 없이 엔진 전체를 확인한다. 개발 머신과 CI가 같이 쓴다.
#   사용: bash scripts/smoke-offline.sh        (PYTHON=… 으로 인터프리터를 바꿀 수 있다)
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PYTHON:-.venv/bin/python}"
BIN="$("$PY" -c 'import os, sys; print(os.path.dirname(sys.executable))')"
unset LIVE_CLAUDE ANTHROPIC_API_KEY DISCORD_TOKEN GITHUB_TOKEN
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
step() { printf '[smoke] %s\n' "$*"; }

step "pytest (live 제외)"
"$PY" -m pytest -q -p no:cacheprovider
step "mypy"
"$PY" -m mypy src tests
step "ruff"
"$PY" -m ruff check src tests
step "bash -n"
for f in deploy/*.sh scripts/*.sh deploy/data-repo-template/.github/scripts/*.sh; do
  bash -n "$f"
done
step "콘솔 스크립트 --help 종료 코드 2"
scripts="$("$PY" -c 'import tomllib; print(" ".join(tomllib.load(open("pyproject.toml", "rb"))["project"]["scripts"]))')"
for cmd in $scripts; do
  set +e
  # --help를 무시하는 진입점이 있어도 실제 설정·작업 폴더에 닿지 않게 임시 경로를 준다.
  TEAM_WIKI_CONFIG="$tmp/missing-team.json" TEAM_WIKI_HOME="$tmp/home" "$BIN/$cmd" --help >/dev/null 2>&1
  code=$?
  set -e
  if [ "$code" -ne 2 ]; then
    printf '%s\n' "$cmd --help 종료 코드 $code (기대 2)" >&2
    exit 1
  fi
done
step "설치 스크립트 DRY_RUN"
DRY_RUN=1 ENGINE_REPO=/nonexistent/engine.bundle APP_USER=team-wiki-smoke-nouser \
  APP_HOME="$tmp/srv" ENGINE_DIR="$tmp/opt" ETC_DIR="$tmp/etc" SYSTEMD_DIR="$tmp/systemd" \
  TMPFILES_DIR="$tmp/tmpfiles" JOURNALD_DIR="$tmp/journald" DEPLOY_STATE="$tmp/state" \
  BACKUP_DIR="$tmp/backups" bash deploy/setup.sh >/dev/null
step "끝 — 모두 통과"

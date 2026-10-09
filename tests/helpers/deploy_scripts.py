"""배포 스크립트 테스트 도우미: bash 후보(맥 3.2와 5.x)와 스크립트 실행."""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path


def bash_candidates() -> list[str]:
    """있는 bash 실행 파일들(맥 `/bin/bash` 3.2, Homebrew 5.x 등)."""
    paths = ("/bin/bash", "/opt/homebrew/bin/bash", "/usr/bin/bash")
    found = [p for p in paths if os.path.exists(p)]
    resolved: dict[str, str] = {}
    for p in found:
        resolved.setdefault(os.path.realpath(p), p)
    return list(resolved.values())


def run_script(
    bash: str,
    script: Path,
    args: Sequence[str] = (),
    *,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    base = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    if env:
        base.update(env)
    return subprocess.run(
        [bash, str(script), *args],
        capture_output=True,
        text=True,
        env=base,
        cwd=cwd,
        timeout=60,
        check=False,
    )


@dataclass(frozen=True)
class ScriptResult:
    code: int
    out: str
    err: str

    @property
    def lines(self) -> list[str]:
        """DRY_RUN이 출력한 `+ 명령` 줄들."""
        return [ln for ln in self.out.splitlines() if ln.startswith("+ ")]


def run_deploy_script(
    script: Path,
    env: Mapping[str, str],
    *,
    args: Sequence[str] = (),
    bash: str = "bash",
) -> ScriptResult:
    """깨끗한 환경(PATH·HOME(임시)·LANG만)에 `env`를 더해 스크립트를 돈다."""
    with tempfile.TemporaryDirectory() as home:
        base = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": home,
            "LANG": "C.UTF-8",
            **env,
        }
        done = subprocess.run(
            [bash, str(script), *args],
            capture_output=True,
            text=True,
            env=base,
            timeout=60,
            check=False,
        )
    return ScriptResult(done.returncode, done.stdout, done.stderr)


FAKE_SYSTEMCTL = """#!/bin/bash
# 테스트용 가짜 systemctl: is-active만 흉내 낸다.
# 상태는 FAKE_STATE_<유닛 이름의 . @ - 를 _로 바꾼 것> 환경변수에서 읽는다(없으면 inactive).
if [ "$1" = "is-active" ]; then
  key="FAKE_STATE_$(printf '%s' "$2" | tr '.@-' '___')"
  eval "state=\\${$key:-inactive}"
  printf '%s\\n' "$state"
  [ "$state" = active ]
  exit $?
fi
exit 0
"""


def write_fake_systemctl(directory: Path) -> Path:
    path = directory / "fake-systemctl"
    path.write_text(FAKE_SYSTEMCTL, encoding="utf-8")
    path.chmod(0o755)
    return path


def deploy_dirs(root: Path) -> dict[str, str]:
    """경로 변수를 `root` 아래로 바꾼 환경."""
    names = {
        "APP_HOME": "srv",
        "ENGINE_DIR": "opt/engine",
        "ETC_DIR": "etc/team-wiki",
        "SYSTEMD_DIR": "etc/systemd/system",
        "TMPFILES_DIR": "etc/tmpfiles.d",
        "JOURNALD_DIR": "etc/journald.conf.d",
        "DEPLOY_STATE": "var/lib/deploy",
        "BACKUP_DIR": "var/backups",
    }
    return {k: str(root / v) for k, v in names.items()} | {"DRY_RUN": "1"}

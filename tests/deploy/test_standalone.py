"""폴더 하나 실행(deploy/run-standalone.sh): 입력한 비밀값은 자식에게만 가고 폴더에 남지 않는다."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from tests.helpers.deploy_scripts import bash_candidates

SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "run-standalone.sh"
KEYS = ("sk-ant-standalone-test", "discord-standalone-test", "github-standalone-test")


def fake_bin(path: Path) -> None:
    path.mkdir()
    scripts = {
        "team-wiki-preflight": "exit 0",
        # 설정 오류(2)로 끝나는 봇: 다시 띄우지 않고 알려야 한다.
        "team-wiki-bot": (
            'printf \'%s\\n\' "$ANTHROPIC_API_KEY" "$DISCORD_TOKEN" "$GITHUB_TOKEN" '
            '> "$MARKS/bot-env"\n'
            'echo run >> "$MARKS/bot-runs"\nexit 2'
        ),
        "team-wiki-notify": 'printf \'%s\\n\' "$1" >> "$MARKS/notify"',
        "team-wiki-sync": 'printf \'%s\\n\' "$HOME" "$TEAM_WIKI_HOME" > "$MARKS/sync"',
        "python": 'cat > /dev/null\necho "1 01-01 09:00"',
    }
    for name, body in scripts.items():
        f = path / name
        f.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        f.chmod(0o755)


def wait_for(*paths: Path, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while not all(p.exists() for p in paths):
        assert time.monotonic() < deadline, [p for p in paths if not p.exists()]
        time.sleep(0.1)


@pytest.mark.parametrize("bash", bash_candidates())
def test_keys_stay_in_memory_and_config_error_stops_the_bot(bash: str, tmp_path: Path) -> None:
    base, marks = tmp_path / "base", tmp_path / "marks"
    base.mkdir()
    marks.mkdir()
    (base / "team.json").write_text("{}", encoding="utf-8")
    fake_bin(tmp_path / "bin")
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "TEAM_WIKI_BIN": str(tmp_path / "bin"),
        "MARKS": str(marks),
    }
    proc = subprocess.Popen(
        [bash, str(SCRIPT), str(base)],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
        text=True,
        start_new_session=True,
    )
    try:
        assert proc.stdin is not None
        proc.stdin.write("".join(k + "\n" for k in KEYS))
        proc.stdin.close()
        wait_for(marks / "notify", marks / "sync")
    finally:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=10)

    assert (marks / "bot-env").read_text(encoding="utf-8").split() == list(KEYS)
    assert (marks / "bot-runs").read_text(encoding="utf-8").split() == ["run"]
    assert (marks / "notify").read_text(encoding="utf-8").split() == ["team-wiki-bot"]
    assert (marks / "sync").read_text(encoding="utf-8").split() == [
        str((base / "work" / "home").resolve()),
        str((base / "work").resolve()),
    ]
    for name in ("work", "work/home", "work/state", "work/state/runs", "work/repos", "logs"):
        assert (base / name).stat().st_mode & 0o777 == 0o700, name
    for f in base.rglob("*"):
        if f.is_file():
            text = f.read_text(encoding="utf-8", errors="ignore")
            assert not any(k in text for k in KEYS), f


def test_missing_config_fails_before_asking_for_keys(tmp_path: Path) -> None:
    done = subprocess.run(
        ["bash", str(SCRIPT), str(tmp_path)],
        input="",
        capture_output=True,
        text=True,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "TEAM_WIKI_BIN": str(tmp_path)},
        timeout=30,
        check=False,
    )
    assert done.returncode == 2
    assert "team.json" in done.stdout + done.stderr
    assert "ANTHROPIC_API_KEY" not in done.stdout + done.stderr

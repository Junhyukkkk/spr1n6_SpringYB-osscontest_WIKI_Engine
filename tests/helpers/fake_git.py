"""가짜 `git` 실행 파일(실패 모드·환경·프로세스 정리 시험용).

프로세스마다 한 번만 공유 스크립트(`/bin/sh` 래퍼 `exec "<python>" "<구현>" "$0" "$@"` —
인터프리터 경로에 공백이 있어도 된다 — 와 구현 `.py`)를 쓰고 한 번 돌려 데워 둔다(macOS는 새 실행
파일의 첫 실행이 0.3초쯤 더 든다). `write_fake_git(directory)`는 `directory/git`을 그 공유
스크립트의 심볼릭 링크로 만든다(이름이 `git`이라 `PATH` 탐색 시험에도 쓴다). 구현은 `$0`(= 링크
경로, 링크를 풀지 않는다)의 폴더에서 호출 기록·`version.txt`를 찾으므로 상태는 시험마다
따로다. 호출마다 argv(`argv[0]` 제외)를 `<그 폴더>/calls.jsonl`에 한 줄로 덧붙인 뒤
첫 인자로 동작을 고른다:

- `echo-env`: 환경 전체를 JSON 한 줄로 출력
- `echo-argv`: argv를 JSON 한 줄로 출력
- `hang <pid파일>`: `sleep 60` 손자 프로세스를 띄워 그 PID를 `<pid파일>`에, 자기 PID를
  `<pid파일>.self`에 적은 뒤(둘 다 임시 파일 → 이름 바꾸기로 한 번에 나타난다) 잠든다
- `flood <바이트> [pid파일]`: stdout에 그만큼 쓴다. `pid파일`이 있으면 `hang`처럼 손자를 띄우고
  다 쓴 뒤에도 잠든다
- `flood-err <바이트>`: stderr에 그만큼 쓰고 종료 코드 0
- `fail <stderr 글>`: stderr에 글을 쓰고 종료 코드 1
- `version <문자열>`: 문자열을 출력
- `__warmup__`: 아무것도 하지 않고(기록도 안 남기고) 종료 코드 0
- `read-stdin`: 표준 입력을 EOF까지 읽고 `EOF:<글자 수>`를 출력(DEVNULL이면 바로 `EOF:0`)
- `escape <pid파일>`: 새 세션에서 `sleep 60`을 띄워(= 그룹을 벗어난 손자, 파이프를 물려받는다)
  그 PID를 `<pid파일>`에 적고 잠든다
- `--version`: 링크 옆 `version.txt`의 내용(없으면 `git version 2.54.0 (fake)`)을 출력
- 그 밖: `fatal: fake git: unknown command`를 stderr에 쓰고 종료 코드 1
"""

import atexit
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

CALLS_FILE = "calls.jsonl"
IMPL_FILE = "fake_git_impl.py"
VERSION_FILE = "version.txt"

_SCRIPT = """import json
import os
import sys
import time

here = os.path.dirname(os.path.abspath(sys.argv[1]))
args = sys.argv[2:]
if args == ["__warmup__"]:
    sys.exit(0)
with open(os.path.join(here, "{calls}"), "a", encoding="utf-8") as calls:
    calls.write(json.dumps(args, ensure_ascii=False) + "\\n")


def publish(path, text):
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(path + ".tmp", path)


def spawn_grandchild(pid_file, new_session=False):
    import subprocess

    child = subprocess.Popen(["sleep", "60"], start_new_session=new_session)
    publish(pid_file + ".self", str(os.getpid()))
    publish(pid_file, str(child.pid))


mode = args[0] if args else ""
if mode == "echo-env":
    print(json.dumps(dict(os.environ)))
elif mode == "echo-argv":
    print(json.dumps(args[1:]))
elif mode == "hang":
    spawn_grandchild(args[1])
    time.sleep(60)
elif mode == "read-stdin":
    print("EOF:" + str(len(sys.stdin.read())))
elif mode == "escape":
    spawn_grandchild(args[1], new_session=True)
    time.sleep(60)
elif mode == "flood":
    if len(args) > 2:
        spawn_grandchild(args[2])
    remaining = int(args[1])
    chunk = b"x" * 65536
    out = sys.stdout.buffer
    while remaining > 0:
        piece = chunk[: min(remaining, len(chunk))]
        out.write(piece)
        remaining -= len(piece)
    out.flush()
    if len(args) > 2:
        time.sleep(60)
elif mode == "flood-err":
    remaining = int(args[1])
    chunk = b"e" * 65536
    err = sys.stderr.buffer
    while remaining > 0:
        piece = chunk[: min(remaining, len(chunk))]
        err.write(piece)
        remaining -= len(piece)
    err.flush()
elif mode == "fail":
    sys.stderr.write(args[1] if len(args) > 1 else "")
    sys.stderr.flush()
    sys.exit(1)
elif mode == "version":
    print(args[1] if len(args) > 1 else "")
elif mode == "--version":
    path = os.path.join(here, "{version_file}")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            print(f.read().rstrip("\\n"))
    else:
        print("git version 2.54.0 (fake)")
else:
    sys.stderr.write("fatal: fake git: unknown command\\n")
    sys.exit(1)
"""


_shared: Path | None = None


def _shared_script() -> Path:
    """프로세스당 한 번 쓰고 데운 공유 스크립트."""
    global _shared
    if _shared is None or not _shared.exists():
        root = Path(os.path.realpath(tempfile.mkdtemp(prefix="twe-fakegit-")))
        atexit.register(shutil.rmtree, root, ignore_errors=True)
        impl = root / IMPL_FILE
        impl.write_text(
            _SCRIPT.format(calls=CALLS_FILE, version_file=VERSION_FILE), encoding="utf-8"
        )
        script = root / "git-shared"
        script.write_text(
            f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(impl))} "$0" "$@"\n',
            encoding="utf-8",
        )
        script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        subprocess.run([str(script), "__warmup__"], check=True, capture_output=True)
        _shared = script
    return _shared


def write_fake_git(directory: Path, *, version: str | None = None) -> Path:
    """`directory/git`을 공유 가짜 git의 링크로 만들고 경로를 돌려준다(이미 있으면 바꾼다).

    `version`을 주면 `--version` 출력으로 쓴다(나중에 `version.txt`를 고쳐도 된다).
    """
    directory.mkdir(parents=True, exist_ok=True)
    link = directory / "git"
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(_shared_script())
    if version is not None:
        (directory / VERSION_FILE).write_text(version, encoding="utf-8")
    return link


def fake_git_calls(script: Path) -> list[list[str]]:
    """가짜 git이 받은 호출을 순서대로(`argv[0]` 제외). 한 번도 안 불렸으면 `[]`."""
    path = script.parent / CALLS_FILE
    if not path.exists():
        return []
    calls: list[list[str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            calls.append(json.loads(line))
    return calls

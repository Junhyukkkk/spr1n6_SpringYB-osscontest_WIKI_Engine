import json
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.helpers import write_tree

USAGE = "사용법: team-wiki-lint <데이터 저장소 경로>"
BASELINE = json.dumps({"repos": {}, "raw_processed": [], "updated_at": "2026-10-07T09:00:00+09:00"})


@dataclass
class Result:
    code: int
    stdout: str
    stderr: str


def run(*args: str, cwd: Path | None = None) -> Result:
    """실제 모듈을 새 파이썬 프로세스로 실행한다."""
    proc = subprocess.run(
        [sys.executable, "-m", "team_wiki_engine.wiki.lint_cli", *args],
        capture_output=True,
        encoding="utf-8",
        cwd=cwd,
        timeout=60,
    )
    return Result(proc.returncode, proc.stdout, proc.stderr)


def test_clean_folder_passes_with_exit_0(tmp_dir: Path) -> None:
    write_tree(
        tmp_dir, {"wiki/index.md": "---\nupdated: 2026-10-07\n---\n", "baseline.json": BASELINE}
    )
    assert run(str(tmp_dir)) == Result(0, "점검 통과\n", "")


def test_violations_exit_1_and_are_printed_to_stdout(tmp_dir: Path) -> None:
    write_tree(tmp_dir, {"baseline.json": BASELINE, "wiki/index.md": "# 없음\n"})
    r = run(str(tmp_dir))
    assert r.code == 1
    assert r.stdout == "[L3] wiki/index.md: frontmatter가 없습니다\n위반 1건\n"
    assert r.stderr == ""


def test_secret_value_is_not_printed(tmp_dir: Path) -> None:
    write_tree(
        tmp_dir,
        {
            "baseline.json": BASELINE,
            "wiki/index.md": "---\nupdated: 2026-10-07\n---\npassword: hunter2024\n",
        },
    )
    r = run(str(tmp_dir))
    assert r.code == 1
    assert "hunter2024" not in r.stdout + r.stderr
    assert "[L1] wiki/index.md: 비밀값으로 보이는 대입: password=…" in r.stdout


def test_missing_folder_exit_2_without_traceback(tmp_dir: Path) -> None:
    missing = tmp_dir / "none"
    r = run(str(missing))
    assert r == Result(2, "", f"점검 실패: 데이터 폴더를 읽을 수 없습니다: {missing}\n")


def test_missing_wiki_folder_exit_2(tmp_dir: Path) -> None:
    r = run(str(tmp_dir))
    assert r == Result(2, "", f"점검 실패: wiki 폴더가 없습니다: {tmp_dir / 'wiki'}\n")


def test_relative_path_is_resolved_against_cwd(tmp_dir: Path) -> None:
    write_tree(tmp_dir, {"data/wiki/index.md": "---\nupdated: 2026-10-07\n---\n"})
    assert run("data", cwd=tmp_dir) == Result(0, "점검 통과\n", "")


def test_no_argument_prints_usage_exit_2() -> None:
    assert run() == Result(2, "", f"{USAGE}\n")


@pytest.mark.parametrize("arg", ["", "   "])
def test_blank_argument_prints_usage_exit_2(arg: str) -> None:
    assert run(arg) == Result(2, "", f"{USAGE}\n")


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_help_flag_prints_usage_not_a_path_check(tmp_dir: Path, flag: str) -> None:
    assert run(flag, cwd=tmp_dir) == Result(2, "", f"{USAGE}\n")


def test_extra_arguments_are_a_usage_error(tmp_dir: Path) -> None:
    assert run(str(tmp_dir), str(tmp_dir)) == Result(2, "", f"{USAGE}\n")


def test_unexpected_exception_is_reported_without_traceback(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from team_wiki_engine.wiki import lint_cli

    def boom(_: Path) -> list[object]:
        raise RuntimeError("뜻밖의 오류")

    monkeypatch.setattr(lint_cli, "lint", boom)
    assert lint_cli.main([str(tmp_dir)]) == 2
    captured = capsys.readouterr()
    assert captured.err == "점검 실패: 뜻밖의 오류\n"
    assert captured.out == ""
    assert "Traceback" not in captured.err


def test_main_returns_codes_in_process(tmp_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from team_wiki_engine.wiki.lint_cli import main

    write_tree(tmp_dir, {"wiki/index.md": "---\nupdated: 2026-10-07\n---\n"})
    assert main([str(tmp_dir)]) == 0
    assert capsys.readouterr().out == "점검 통과\n"
    assert main([]) == 2
    assert capsys.readouterr().err == f"{USAGE}\n"


def test_closed_stdout_pipe_is_quiet(tmp_dir: Path) -> None:
    """`… | head`처럼 읽는 쪽이 먼저 닫아도 트레이스백 없이 같은 종료 코드로 끝난다."""
    pages = {f"wiki/p{i}.md": "# 없음\n" for i in range(4000)}  # 출력이 파이프 버퍼보다 크다
    write_tree(tmp_dir, {"baseline.json": BASELINE, **pages})
    proc = subprocess.Popen(
        [sys.executable, "-m", "team_wiki_engine.wiki.lint_cli", str(tmp_dir)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
    )
    assert proc.stdout is not None
    proc.stdout.close()
    assert proc.stderr is not None
    stderr = proc.stderr.read()
    assert proc.wait(timeout=60) == 1
    assert stderr == ""


def test_broken_pipe_in_process_keeps_exit_code(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from team_wiki_engine.wiki import lint_cli

    class Broken:
        def write(self, _: str) -> int:
            raise BrokenPipeError

        def flush(self) -> None:
            raise BrokenPipeError

        def fileno(self) -> int:
            raise OSError

    write_tree(tmp_dir, {"wiki/index.md": "# 없음\n"})
    monkeypatch.setattr(sys, "stdout", Broken())
    assert lint_cli.main([str(tmp_dir)]) == 1


def test_closed_stdout_exits_quietly(tmp_dir: Path) -> None:
    """`team-wiki-lint DIR >&-`: sys.stdout이 None이어도 트레이스백 없이 종료 코드는 그대로다."""
    write_tree(tmp_dir, {"baseline.json": BASELINE, "wiki/index.md": "# 없음\n"})
    python = shlex.quote(sys.executable)
    command = f"{python} -m team_wiki_engine.wiki.lint_cli {shlex.quote(str(tmp_dir))} >&-"
    proc = subprocess.run(["sh", "-c", command], capture_output=True, encoding="utf-8", timeout=60)
    assert (proc.returncode, proc.stderr) == (1, "")

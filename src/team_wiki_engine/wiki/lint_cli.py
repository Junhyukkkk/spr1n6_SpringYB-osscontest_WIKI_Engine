"""`team-wiki-lint <데이터 저장소 경로>`: 위키 점검을 돌리고 종료 코드로 결과를 알린다.

종료 코드: 0 통과, 1 위반 있음, 2 사용 오류·점검 실패(스택 트레이스는 내지 않는다).
"""

import contextlib
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from team_wiki_engine.wiki.lint import format_violations, lint

USAGE = "사용법: team-wiki-lint <데이터 저장소 경로>"


def _use_utf8_output() -> None:
    """로케일이 UTF-8이 아니어도 한글 출력이 죽지 않게 한다(표현 못 하는 글자는 이스케이프)."""
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, OSError, ValueError):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")  # type: ignore[union-attr]


def _print_stdout(text: str) -> None:
    """stdout에 쓴다. 닫혀 있거나(`>&-`) 읽는 쪽이 먼저 닫았으면(`| head`) 조용히 넘어간다."""
    if sys.stdout is None:  # `>&-`로 stdout이 닫힌 채 시작하면 None이다
        return
    try:
        print(text)
        sys.stdout.flush()
    except BrokenPipeError:
        # 종료 때 남은 버퍼를 비우다 다시 실패하지 않도록 stdout을 /dev/null로 돌린다
        with contextlib.suppress(OSError, ValueError, AttributeError):
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    _use_utf8_output()
    if len(args) != 1 or args[0].strip() == "" or args[0] in {"--help", "-h"}:
        print(USAGE, file=sys.stderr)
        return 2
    try:
        violations = lint(Path(args[0]))
    except Exception as exc:  # LintUsageError 포함: 어떤 실패도 위반(1)과 구분되는 2로 닫는다
        print(f"점검 실패: {exc or type(exc).__name__}", file=sys.stderr)
        return 2
    _print_stdout(format_violations(violations))
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())

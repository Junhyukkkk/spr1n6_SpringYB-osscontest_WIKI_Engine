"""소스 텍스트 규칙(G11). 규칙 함수는 `(경로, 글)`을 받아 위반 목록을 돌려주는 순수 함수다.

Task 18이 `RULES`에 규칙을 더한다. 이 파일 자신은 금지 문자열을 예로 담으므로 검사에서 뺀다.
git·비동기 하위 프로세스(`asyncio.create_subprocess_exec`)는 허용한다.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SELF = "tests/test_source_rules.py"


@dataclass(frozen=True)
class Rule:
    name: str
    needle: str
    scopes: tuple[str, ...]
    """이 접두어(`src/`, `tests/`)로 시작하는 경로에만 적용한다."""
    regex: bool = False
    """참이면 `needle`은 정규식이고 줄 안 어디서든 찾는다."""
    sample: str = ""
    """정규식 규칙이 반응하는 예시 한 줄(비면 `needle`)."""

    def matches(self, line: str) -> bool:
        return re.search(self.needle, line) is not None if self.regex else self.needle in line

    @property
    def example(self) -> str:
        return self.sample or self.needle


_OPTIONS_FREE_SCOPES = tuple(
    f"src/team_wiki_engine/{package}/"
    for package in ("sync", "metrics", "ops", "github", "git", "bot")
)
BOT_SCOPE = ("src/team_wiki_engine/bot/",)


RULES: tuple[Rule, ...] = (
    Rule(
        "model_copy 금지(검증을 건너뛴다 — parse_config 등으로 다시 만든다)",
        "model_copy(",
        ("src/", "tests/"),
    ),
    Rule("셸 실행 금지", "shell=True", ("src/",)),
    Rule("os.system 금지", "os.system(", ("src/",)),
    Rule("subprocess.run 금지(비동기 create_subprocess_exec만)", "subprocess.run(", ("src/",)),
    Rule("subprocess.Popen 금지(비동기 create_subprocess_exec만)", "subprocess.Popen(", ("src/",)),
    # G6: 옵션 객체(비밀·경로가 든다)는 claude/ 안에서만 만들고 다루며, 어디서도 기록하지 않는다.
    Rule(
        "ClaudeAgentOptions는 claude/ 밖에서 쓰지 않는다",
        "ClaudeAgentOptions",
        _OPTIONS_FREE_SCOPES,
    ),
    Rule("build_options는 claude/ 밖에서 쓰지 않는다", "build_options", _OPTIONS_FREE_SCOPES),
    Rule(
        "옵션 객체를 로그·출력으로 남기지 않는다",
        r"\b(?:logger|log|logging)\.\w+\(.*\boptions\b|\bprint\(.*\boptions\b",
        ("src/",),
        regex=True,
        sample="logger.info('x', options)",
    ),
    # 계획 3: 봇은 스레드를 만들지 않고(asyncio만), 표준 출력으로 원문을 내지 않는다.
    Rule("봇: threading 금지", "threading", BOT_SCOPE),
    Rule("봇: to_thread 금지", "to_thread(", BOT_SCOPE),
    Rule("봇: run_in_executor 금지", "run_in_executor(", BOT_SCOPE),
    Rule(
        "봇: print 금지(logging이나 sys.stderr.write)",
        r"(?<![\w.])print\(",
        BOT_SCOPE,
        regex=True,
        sample="print('x')",
    ),
)


def violations(path: str, text: str, rules: Sequence[Rule] = RULES) -> list[str]:
    """`path`(루트 기준 `/` 상대 경로)의 `text`가 어긴 규칙을 `경로:줄: 규칙` 목록으로 낸다."""
    found: list[str] = []
    applicable = [r for r in rules if path.startswith(r.scopes)]
    if not applicable:
        return found
    for number, line in enumerate(text.splitlines(), start=1):
        for rule in applicable:
            if rule.matches(line):
                found.append(f"{path}:{number}: {rule.name}")
    return found


def scan_tree(root: Path, rules: Sequence[Rule] = RULES) -> list[str]:
    """`root/src`와 `root/tests`의 모든 `.py`를 검사한다(이 파일 자신은 제외)."""
    found: list[str] = []
    for top in ("src", "tests"):
        for file in sorted((root / top).rglob("*.py")):
            relative = file.relative_to(root).as_posix()
            if relative == SELF or "__pycache__" in file.parts:
                continue
            text = file.read_text(encoding="utf-8", errors="replace")
            found.extend(violations(relative, text, rules))
    return found


class TestRepository:
    def test_저장소는_규칙을_지킨다(self) -> None:
        assert scan_tree(REPO_ROOT) == []

    def test_검사_대상을_실제로_읽는다(self) -> None:
        # 빈 폴더를 훑어 "위반 없음"이 되는 공허한 통과를 막는다.
        sources = list((REPO_ROOT / "src").rglob("*.py"))
        tests = list((REPO_ROOT / "tests").rglob("*.py"))
        assert len(sources) > 20 and len(tests) > 20
        assert any(p.name == "client.py" for p in sources)  # git 실행기: 비동기 subprocess 사용


class TestRuleFunction:
    @pytest.mark.parametrize(
        ("path", "text", "needle"),
        [
            ("src/a.py", "x = cfg.model_copy(update={})", "model_copy"),
            ("tests/a.py", "x = cfg.model_copy(update={})", "model_copy"),
            ("src/a.py", "run(cmd, shell=True)", "셸"),
            ("src/a.py", "os.system('ls')", "os.system"),
            ("src/a.py", "subprocess.run(['git'])", "subprocess.run"),
            ("src/a.py", "p = subprocess.Popen(['git'])", "subprocess.Popen"),
        ],
    )
    def test_일부러_넣은_나쁜_글에_반응한다(self, path: str, text: str, needle: str) -> None:
        found = violations(path, f"import os\n\n{text}\n")
        assert len(found) == 1
        assert found[0].startswith(f"{path}:3: ")
        assert needle in found[0]

    def test_모든_규칙이_저마다_반응한다(self) -> None:
        for rule in RULES:
            path = f"{rule.scopes[0]}x.py"
            assert violations(path, f"a\n{rule.example}\n") == [f"{path}:2: {rule.name}"]

    @pytest.mark.parametrize(
        "text",
        [
            "proc = await asyncio.create_subprocess_exec('git', 'status')",
            "stdout=asyncio.subprocess.PIPE",
            "x = config.model_copied",
            "shell = False",
            "# 셸 없이 argv 목록만 넘긴다",
            "",
        ],
    )
    def test_허용되는_글은_통과한다(self, text: str) -> None:
        assert violations("src/a.py", text) == []
        assert violations("tests/a.py", text) == []

    def test_테스트_코드는_subprocess와_shell을_써도_된다(self) -> None:
        text = "subprocess.run(['git'], check=True)\nsubprocess.Popen(['x'])\nf(shell=True)\n"
        assert violations("tests/a.py", text) == []

    def test_범위_밖_경로는_검사하지_않는다(self) -> None:
        assert violations("docs/a.py", "model_copy(") == []
        assert violations("srcx/a.py", "shell=True") == []

    def test_위반마다_줄_번호를_돌려준다(self) -> None:
        text = "ok\nshell=True\nok\nos.system('x'); subprocess.run([])\n"
        found = violations("src/a.py", text)
        assert [f.split(": ")[0] for f in found] == ["src/a.py:2", "src/a.py:4", "src/a.py:4"]

    @pytest.mark.parametrize(
        ("path", "text"),
        [
            ("src/team_wiki_engine/sync/a.py", "from claude_agent_sdk import ClaudeAgentOptions"),
            ("src/team_wiki_engine/metrics/a.py", "options = build_options(inp, deps)"),
            ("src/team_wiki_engine/ops/a.py", "x: ClaudeAgentOptions"),
            ("src/team_wiki_engine/github/a.py", "build_options"),
            ("src/team_wiki_engine/git/a.py", "ClaudeAgentOptions"),
            ("src/team_wiki_engine/claude/a.py", "logger.debug('opts %s', options)"),
            ("src/team_wiki_engine/wiki/a.py", "logging.info(options)"),
            ("src/team_wiki_engine/wiki/a.py", "print(options)"),
            ("src/team_wiki_engine/wiki/a.py", "print(f'{options}')"),
        ],
    )
    def test_옵션_객체_규칙은_나쁜_글에_반응한다(self, path: str, text: str) -> None:
        assert len(violations(path, f"{text}\n")) == 1

    @pytest.mark.parametrize(
        ("path", "text"),
        [
            (
                "src/team_wiki_engine/claude/runner.py",
                "from claude_agent_sdk import ClaudeAgentOptions",
            ),
            ("src/team_wiki_engine/claude/runner.py", "def build_options(inp, deps):"),
            ("src/team_wiki_engine/sync/a.py", "logger.info('끝', extra_options_count)"),
            ("src/team_wiki_engine/sync/a.py", "print('done')"),
            ("tests/a.py", "ClaudeAgentOptions()"),
        ],
    )
    def test_옵션_객체_규칙은_허용되는_글을_통과시킨다(self, path: str, text: str) -> None:
        assert violations(path, f"{text}\n") == []

    def test_옵션_객체_규칙의_범위가_실제_폴더를_가리킨다(self) -> None:
        # 이름이 틀려 아무것도 검사하지 않는 공허한 통과를 막는다.
        for scope in _OPTIONS_FREE_SCOPES:
            assert any((REPO_ROOT / scope).glob("*.py")), scope
        assert any(
            "ClaudeAgentOptions" in f.read_text(encoding="utf-8")
            for f in (REPO_ROOT / "src/team_wiki_engine/claude").glob("*.py")
        )  # 금지 대상이 실제로 있는 이름이다

    @pytest.mark.parametrize(
        "text",
        [
            "import threading",
            "from threading import Lock",
            "await asyncio.to_thread(f)",
            "loop.run_in_executor(None, f)",
            "print(x)",
        ],
    )
    def test_봇_규칙은_나쁜_글에_반응한다(self, text: str) -> None:
        assert len(violations("src/team_wiki_engine/bot/a.py", f"{text}\n")) == 1
        assert violations("src/team_wiki_engine/sync/a.py", f"{text}\n") == []

    def test_봇_규칙은_허용되는_글을_통과시킨다(self) -> None:
        for text in ("eprint('x')", "sys.stderr.write('x')", "# 스레드를 만들지 않는다"):
            assert violations("src/team_wiki_engine/bot/a.py", f"{text}\n") == []

    def test_discord는_gateway에서만_import한다(self) -> None:
        pattern = re.compile(r"^\s*(?:import discord\b|from discord\b)", re.MULTILINE)
        root = REPO_ROOT / "src/team_wiki_engine"
        importers = sorted(
            f.relative_to(root).as_posix()
            for f in root.rglob("*.py")
            if pattern.search(f.read_text(encoding="utf-8"))
        )
        assert importers == ["bot/gateway.py"]
        assert pattern.search("import discord\n") and pattern.search("  from discord.ext import x")
        assert not pattern.search("# import discord\nx = 'discord'")

    def test_순수_함수다(self) -> None:
        text = "a\nshell=True\n"
        assert violations("src/a.py", text) == violations("src/a.py", text)
        assert violations("src/a.py", "") == []

    def test_규칙을_더할_수_있다(self) -> None:
        extra = Rule("print 금지", "print(", ("src/",))
        assert violations("src/a.py", "print(1)", (*RULES, extra)) == ["src/a.py:1: print 금지"]
        assert violations("src/a.py", "print(1)") == []


class TestScanTree:
    def write(self, root: Path, files: dict[str, str]) -> None:
        for relative, text in files.items():
            file = root / relative
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(text, encoding="utf-8")

    def test_나쁜_파일을_찾아낸다(self, tmp_dir: Path) -> None:
        self.write(
            tmp_dir,
            {
                "src/pkg/a.py": "x = 1\nrun(shell=True)\n",
                "src/pkg/ok.py": "x = 1\n",
                "tests/test_b.py": "c.model_copy(update={})\n",
                "tests/sub/test_c.py": "subprocess.run([])\n",  # 테스트는 허용
            },
        )
        assert scan_tree(tmp_dir) == [
            "src/pkg/a.py:2: 셸 실행 금지",
            f"tests/test_b.py:1: {RULES[0].name}",
        ]

    def test_이_파일_자신은_제외한다(self, tmp_dir: Path) -> None:
        self.write(
            tmp_dir,
            {
                SELF: "model_copy(\nshell=True\n",
                "src/ok.py": "x = 1\n",
                "tests/other.py": "y = 2\n",
            },
        )
        assert scan_tree(tmp_dir) == []
        self.write(tmp_dir, {"tests/test_source_rules_copy.py": "model_copy(\n"})
        assert len(scan_tree(tmp_dir)) == 1  # 이름이 다르면 제외되지 않는다

    def test_읽을_수_없는_글자가_있어도_던지지_않는다(self, tmp_dir: Path) -> None:
        (tmp_dir / "src").mkdir()
        (tmp_dir / "tests").mkdir()
        (tmp_dir / "src" / "bad.py").write_bytes(b"\xff\xfe shell=True\n")
        assert scan_tree(tmp_dir) == ["src/bad.py:1: 셸 실행 금지"]

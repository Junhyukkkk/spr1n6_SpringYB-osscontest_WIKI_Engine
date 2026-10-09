import random

from team_wiki_engine.bot.replies import TRUNCATED_NOTE
from team_wiki_engine.bot.split import (
    DISCORD_LIMIT,
    MAX_CHUNKS,
    cap_chunks,
    close_open_fence,
    split_message,
    utf16_len,
)


def fence_balanced(piece: str) -> bool:
    """조각 하나를 처음부터 읽어 끝에서 펜스가 닫혀 있는가(간단한 독립 구현)."""
    state: tuple[str, int] | None = None
    for line in piece.split("\n"):
        stripped = line.lstrip(" ")
        if len(line) - len(stripped) > 3:
            continue
        run = len(stripped) - len(stripped.lstrip(stripped[:1])) if stripped[:1] in "`~" else 0
        if run < 3:
            continue
        ch = stripped[0]
        rest = stripped[run:]
        if state is None:
            if ch == "`" and "`" in rest:
                continue
            state = (ch, run)
        elif ch == state[0] and run >= state[1] and rest.strip(" \t") == "":
            state = None
    return state is None


def check_invariants(chunks: list[str], limit: int) -> None:
    for c in chunks:
        assert c.strip() != ""
        assert utf16_len(c) <= limit
        c.encode("utf-16-le")  # 서로게이트가 짝을 이룬다
        assert fence_balanced(c)


def test_utf16_len() -> None:
    assert utf16_len("a가") == 2
    assert utf16_len("😀") == 2
    assert utf16_len("") == 0


def test_short_and_empty() -> None:
    assert split_message("짧은 글") == ["짧은 글"]
    assert split_message("") == []
    assert split_message("  \n \n") == []


def test_line_boundary() -> None:
    lines = [f"줄 {i} " + "x" * 90 for i in range(100)]
    text = "\n".join(lines)
    chunks = split_message(text)
    assert len(chunks) > 1
    assert all(utf16_len(c) <= DISCORD_LIMIT for c in chunks)
    assert "\n".join(chunks) == text


def test_long_line_cut() -> None:
    line = "가" * 5000
    chunks = split_message(line)
    assert len(chunks) == 3
    assert "".join(chunks) == line
    assert all(utf16_len(c) <= DISCORD_LIMIT for c in chunks)


def test_fence_reopened_across_boundary() -> None:
    code = "\n".join(["const x = 1;"] * 300)
    text = f"```ts\n{code}\n```\n끝"
    chunks = split_message(text)
    assert len(chunks) >= 2
    check_invariants(chunks, DISCORD_LIMIT)
    assert chunks[0].endswith("\n```")
    assert chunks[1].startswith("```ts\n")
    assert sum(c.count("const x = 1;") for c in chunks) == 300
    assert chunks[-1].endswith("끝")


def test_emoji_line() -> None:
    chunks = split_message("😀" * 1500)
    check_invariants(chunks, DISCORD_LIMIT)
    assert "".join(chunks) == "😀" * 1500


def test_tilde_and_four_backtick_fences_reopen_same_line() -> None:
    for opener, closer in (("~~~python", "~~~"), ("````md", "````")):
        code = "\n".join(["print('hello world')"] * 200)
        chunks = split_message(f"{opener}\n{code}\n{closer}")
        assert len(chunks) >= 2
        check_invariants(chunks, DISCORD_LIMIT)
        assert chunks[0].endswith("\n" + closer)
        assert chunks[1].startswith(opener + "\n")


def test_inner_shorter_fence_does_not_close() -> None:
    inner = "\n".join(["```", "안쪽 3개", "```"] * 5)
    body = "\n".join(["줄" * 40] * 100)
    text = f"````md\n{inner}\n{body}\n````"
    chunks = split_message(text)
    assert len(chunks) >= 2
    check_invariants(chunks, DISCORD_LIMIT)
    assert chunks[1].startswith("````md\n")


def test_long_info_string_reopens_with_fence_only() -> None:
    opener = "```" + "i" * 300
    code = "\n".join(["print('hello world')"] * 200)
    chunks = split_message(f"{opener}\n{code}\n```")
    assert len(chunks) >= 2
    check_invariants(chunks, DISCORD_LIMIT)
    assert chunks[0].startswith(opener)
    assert chunks[1].split("\n")[0] == "```"


def test_unclosed_fence_closed_at_end() -> None:
    chunks = split_message("앞\n```py\nprint(1)")
    assert chunks == ["앞\n```py\nprint(1)\n```"]
    check_invariants(chunks, DISCORD_LIMIT)


def test_random_invariants() -> None:
    rng = random.Random(20261008)
    pieces = ["a" * 5, "b" * 70, "가" * 30, "😀" * 20, "", "```", "```py", "~~~", "````", "x" * 300]
    for _ in range(500):
        n = rng.randint(1, 60)
        text = "\n".join(rng.choice(pieces) * rng.randint(1, 3) for _ in range(n))
        for limit in (DISCORD_LIMIT, 400):
            chunks = split_message(text, limit)
            check_invariants(chunks, limit)


def test_small_limit_invariants() -> None:
    rng = random.Random(7)
    pieces = ["a" * 10, "b" * 60, "😀" * 15, "", "```", "```ts", "~~~", "y" * 200]
    for _ in range(200):
        text = "\n".join(rng.choice(pieces) for _ in range(rng.randint(1, 40)))
        chunks = split_message(text, 50)
        check_invariants(chunks, 50)


def test_close_open_fence() -> None:
    assert close_open_fence("```kotlin\nval a = 1") == "```kotlin\nval a = 1\n```"
    assert close_open_fence("```\nx\n```") == "```\nx\n```"
    assert close_open_fence("~~~~\nx") == "~~~~\nx\n~~~~"
    assert close_open_fence("그냥 글") == "그냥 글"


def test_cap_chunks() -> None:
    eight = [str(i) for i in range(8)]
    assert cap_chunks(eight) == eight
    twelve = [str(i) for i in range(12)]
    assert cap_chunks(twelve) == [*twelve[:7], TRUNCATED_NOTE]
    assert MAX_CHUNKS == 8

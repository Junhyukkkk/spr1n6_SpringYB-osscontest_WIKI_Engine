import hashlib
import os
import unicodedata
from pathlib import Path

from team_wiki_engine.bot.replies import TOO_LONG_TEXT
from team_wiki_engine.bot.split import utf16_len
from team_wiki_engine.bot.text import (
    MAX_QUESTION_UNITS,
    MAX_RECORD_CHARS,
    SNOWFLAKE,
    THREAD_NAME_UNITS,
    answer_roots,
    clean_multiline,
    evidence_not_found,
    extract_question,
    hash_user,
    is_snowflake,
    mask_known,
    mentions_bot,
    neutralize_everyone,
    normalize_question,
    question_too_long,
    record_text,
    safe_answer,
    scrub_answer_paths,
    thread_name,
)
from team_wiki_engine.config.load import Paths, resolve_paths
from team_wiki_engine.wiki.lint_secrets import TOKEN_OMITTED


def make_paths(home: Path) -> Paths:
    return resolve_paths({"TEAM_WIKI_HOME": str(home)}, home / "engine")


def test_snowflake() -> None:
    assert SNOWFLAKE.pattern == "[0-9]{1,25}"
    assert is_snowflake("123")
    for bad in ("", "abc", "123\n", "1" * 26, 123, None, "１２３"):
        assert not is_snowflake(bad)


def test_mentions_bot() -> None:
    assert mentions_bot("<@123> 질문", "123")
    assert mentions_bot("<@!123> 질문", "123")
    for content in ("<@1234> x", "<@&123> x", "@everyone 질문", "<@ 123> x", "<@12> x"):
        assert not mentions_bot(content, "123")
    for bad_id in ("abc", "123\n", ""):
        assert not mentions_bot("<@123> <@abc> <@123\n>", bad_id)


def test_extract_question() -> None:
    assert (
        extract_question("<@123> relay가 보내는 필드 알려줘", "123") == "relay가 보내는 필드 알려줘"
    )
    assert (
        extract_question("<@!123> <@456> 님이 만든 거 맞아?", "123") == "<@456> 님이 만든 거 맞아?"
    )
    assert extract_question("여러 줄\n<@123>\n질문", "123") == "여러 줄\n\n질문"
    assert extract_question("<@123>", "123") == ""
    assert extract_question("<@1.3> x", "1.3") == "<@1.3> x"
    assert extract_question("<@103> x <@1.3>", "1.3") == "<@103> x <@1.3>"


def test_normalize_question() -> None:
    nfd = unicodedata.normalize("NFD", "회의록")
    assert normalize_question(nfd) == unicodedata.normalize("NFC", "회의록")
    assert normalize_question("a\r\nb") == "a\nb"
    assert normalize_question("a b c\rd") == "a\nb\nc\nd"
    assert normalize_question("a‮b") == "a\\u202eb"
    assert normalize_question("a\x00b") == "a\\x00b"
    assert normalize_question("a\tb") == "a    b"
    assert normalize_question("  x \n") == "x"


def test_clean_multiline_keeps_newlines() -> None:
    assert clean_multiline("a\n\nb") == "a\n\nb"


def test_question_too_long() -> None:
    assert not question_too_long("a" * 4000)
    assert question_too_long("a" * 4001)
    assert question_too_long("😀" * 2001)
    assert not question_too_long("😀" * 2000)
    assert f"{MAX_QUESTION_UNITS:,}자" in TOO_LONG_TEXT


def test_thread_name() -> None:
    assert thread_name("  relay가\n보내는   메시지  ") == "relay가 보내는 메시지"
    assert utf16_len(thread_name("가" * 80)) == 50
    assert thread_name(" \n\t ") == "질문"
    assert "\\u202e" in thread_name("a‮b")
    name = thread_name("😀" * 60)
    assert utf16_len(name) <= THREAD_NAME_UNITS
    name.encode("utf-16-le")


def test_hash_user() -> None:
    assert hash_user("u1", "g1") == hashlib.sha256(b"g1:u1").hexdigest()[:12]
    assert len(hash_user("u1", "g1")) == 12


def test_neutralize_everyone() -> None:
    out = neutralize_everyone("@everyone @here @Everyone")
    assert out == "@​everyone @​here @Everyone"


def test_evidence_not_found() -> None:
    assert evidence_not_found("본문\n\n— 근거: 찾지 못함\n— 기준: …")
    assert evidence_not_found("- 근거: 찾지 못함.")
    assert evidence_not_found("—근거：찾지 못함")
    assert not evidence_not_found("코드에서 찾지 못함이라고 썼다")
    assert not evidence_not_found("— 근거: wiki/a.md")


def test_scrub_answer_paths(tmp_dir: Path) -> None:
    paths = make_paths(tmp_dir / "home")
    roots = answer_roots(paths)
    assert scrub_answer_paths(f"{paths.data_dir}/wiki/index.md", roots) == "wiki/index.md"
    assert scrub_answer_paths(f"{paths.repos_dir}/worker/src/A.kt", roots) == "worker/src/A.kt"
    assert scrub_answer_paths(f"{paths.state_dir}/x", roots) == "<state>/x"
    assert scrub_answer_paths(f"{paths.home}/y", roots) == "<home>/y"
    for keep in ("/api/v1/users", "/etc/hosts"):
        assert scrub_answer_paths(f"GET {keep} 호출", roots) == f"GET {keep} 호출"


def test_scrub_answer_paths_real_spelling(tmp_dir: Path) -> None:
    real = tmp_dir / "real"
    (real / "data").mkdir(parents=True)
    alias = tmp_dir / "alias"
    os.symlink(real, alias)
    paths = make_paths(alias)
    roots = answer_roots(paths)
    real_spelling = os.path.realpath(paths.data_dir)
    assert real_spelling != str(paths.data_dir)
    assert scrub_answer_paths(f"{real_spelling}/wiki/a.md", roots) == "wiki/a.md"
    assert scrub_answer_paths(f"{paths.data_dir}/wiki/a.md", roots) == "wiki/a.md"


def test_safe_answer(tmp_dir: Path) -> None:
    paths = make_paths(tmp_dir / "home")
    roots = answer_roots(paths)
    ant = "sk-ant-api03-" + "z" * 30
    ghp = "ghp_" + "y" * 36
    text = (
        f"비밀 correcthorsebatterystaple 그리고 {ant} {ghp}\n"
        "postgres://u:hunter2pass@h/db 와 postgres://user:password@host\n"
        "@everyone 확인\n```py\nprint(1)"
    )
    out = safe_answer(text, redact=["correcthorsebatterystaple"], roots=roots)
    for leaked in ("correcthorsebatterystaple", ant, ghp, "hunter2pass"):
        assert leaked not in out
    assert "postgres://user:password@host" in out
    assert "@everyone" not in out and "@​everyone" in out
    assert out.endswith("print(1)\n```")


def test_safe_answer_private_key_and_paths(tmp_dir: Path) -> None:
    paths = make_paths(tmp_dir / "home")
    roots = answer_roots(paths)
    key = "-----BEGIN RSA PRIVATE KEY-----\nMIIEvQIBADANBgkq\n-----END RSA PRIVATE KEY-----"
    out = safe_answer(f"{key}\n{paths.data_dir}/wiki/a.md", redact=[], roots=roots)
    assert "MIIE" not in out and "PRIVATE" not in out
    assert "wiki/a.md" in out and str(paths.data_dir) not in out


def test_mask_known_and_record_text() -> None:
    assert "secretvalue99" not in mask_known("x secretvalue99 y", ["secretvalue99"])
    out = record_text("가" * 5000, [])
    assert len(out) <= MAX_RECORD_CHARS and out.endswith("…")
    assert TOKEN_OMITTED in record_text("ghp_" + "y" * 36, [])
    assert record_text("짧음", []) == "짧음"

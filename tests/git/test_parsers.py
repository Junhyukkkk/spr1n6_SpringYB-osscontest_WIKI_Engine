"""git 출력 파서(순수 함수). 적대적 입력에도 예외를 던지지 않는다(G16)."""

import random

from team_wiki_engine.git.ops import (
    GitLogEntry,
    NumStat,
    is_safe_branch,
    is_sha,
    parse_log_records,
    parse_numstat_z,
    parse_porcelain_z,
)

SHA_A = "a" * 40
SHA_B = "b" * 40


# ---------------------------------------------------------------- 형식 검사


def test_is_sha_requires_exactly_forty_lowercase_hex() -> None:
    assert is_sha("a" * 40)
    assert is_sha("0123456789abcdef" * 2 + "01234567")
    assert not is_sha("a" * 40 + "\n")
    assert not is_sha("a" * 39)
    assert not is_sha("a" * 41)
    assert not is_sha("A" * 40)
    assert not is_sha("g" * 40)
    assert not is_sha("")
    assert not is_sha(None)
    assert not is_sha(b"a" * 40)
    assert not is_sha(123)
    assert not is_sha("０" * 40)  # 전각 숫자


def test_is_safe_branch_accepts_normal_names() -> None:
    for name in [
        "main",
        "sync/2026-10-07",
        "release-1.2",
        "a",
        "feat/x_y.z",
        "origin/main",
        "A" * 200,
    ]:
        assert is_safe_branch(name), name


def test_is_safe_branch_rejects_unsafe_names() -> None:
    bad = [
        "",
        "main\n",
        "\nmain",
        "-main",
        "--output=/tmp/x",
        ".hidden",
        "a/.hidden",
        "a..b",
        "a//b",
        "a@{1}",
        "a/",
        "a.",
        "a.lock",
        "a/b.lock",
        "a b",
        "a:b",
        "a~1",
        "a^",
        "a?b",
        "a*b",
        "a[b",
        "a\\b",
        "a\x00b",
        "한글",
        "A" * 201,
        "/a",
        "+a",
        "refs/heads/main",
        "refs/x",
        "HEAD",
        "head",
        "FETCH_HEAD",
        "ORIG_HEAD",
        "MERGE_HEAD",
        "CHERRY_PICK_HEAD",
        "fetch_head",
        "a" * 40,
        "A" * 40,
        "0123456789abcdefABCDEF0123456789abcdef01",
    ]
    for name in bad:
        assert not is_safe_branch(name), repr(name)


def test_is_safe_branch_rejects_non_strings() -> None:
    assert not is_safe_branch(None)  # type: ignore[arg-type]
    assert not is_safe_branch(b"main")  # type: ignore[arg-type]


# ---------------------------------------------------------------- parse_porcelain_z


def test_porcelain_plain_entries_untracked_and_ignored() -> None:
    raw = b" M wiki/a.md\0?? raw/new.md\0!! raw/secret.txt\0A  wiki/b.md\0 D wiki/gone.md\0"
    assert parse_porcelain_z(raw) == [
        "wiki/a.md",
        "raw/new.md",
        "raw/secret.txt",
        "wiki/b.md",
        "wiki/gone.md",
    ]


def test_porcelain_rename_and_copy_include_the_old_path() -> None:
    raw = b"R  wiki/new name.md\0wiki/old name.md\0C  wiki/copy.md\0wiki/src.md\0?? x.md\0"
    assert parse_porcelain_z(raw) == [
        "wiki/new name.md",
        "wiki/old name.md",
        "wiki/copy.md",
        "wiki/src.md",
        "x.md",
    ]


def test_porcelain_keeps_korean_and_space_paths_verbatim() -> None:
    assert parse_porcelain_z("?? raw/회의 록.md\0".encode()) == ["raw/회의 록.md"]


def test_porcelain_invalid_utf8_survives_as_surrogates() -> None:
    result = parse_porcelain_z(b"?? wiki/\xff.md\0")
    assert result == ["wiki/\udcff.md"]
    assert result[0].encode("utf-8", errors="surrogateescape") == b"wiki/\xff.md"


def test_porcelain_deduplicates_preserving_order() -> None:
    raw = b"?? a.md\0 M b.md\0R  c.md\0a.md\0?? b.md\0"
    assert parse_porcelain_z(raw) == ["a.md", "b.md", "c.md"]


def test_porcelain_skips_malformed_tokens() -> None:
    raw = b"\0M\0 M\0XX wiki/x.md\0?? \0?M\0 M\0\0\x00?? ok.md\0"
    assert parse_porcelain_z(raw) == ["ok.md"]
    assert parse_porcelain_z(b"???wiki/x.md\0") == []  # 세 번째 바이트가 공백이 아니다


def test_porcelain_truncated_rename_keeps_the_new_path() -> None:
    assert parse_porcelain_z(b"R  new.md\0") == ["new.md"]
    assert parse_porcelain_z(b"R  new.md") == ["new.md"]
    assert parse_porcelain_z(b"?? last.md") == ["last.md"]


def test_porcelain_empty_input() -> None:
    assert parse_porcelain_z(b"") == []
    assert parse_porcelain_z(b"\0\0\0") == []


# ---------------------------------------------------------------- parse_log_records


def _record(sha: str, date: str, author: str, subject: str) -> bytes:
    """`log -z --format=%H%x00%cI%x00%an%x00%s` 한 레코드(필드 사이와 끝이 NUL)."""
    return "\0".join([sha, date, author, subject]).encode() + b"\0"


def test_log_records_normal() -> None:
    raw = _record(SHA_A, "2026-10-07T09:00:00+09:00", "alice", "첫 커밋") + _record(
        SHA_B, "2026-10-08T10:00:00+09:00", "밥", "두 번째"
    )
    assert parse_log_records(raw) == [
        GitLogEntry(SHA_A, "alice", "2026-10-07T09:00:00+09:00", "첫 커밋"),
        GitLogEntry(SHA_B, "밥", "2026-10-08T10:00:00+09:00", "두 번째"),
    ]


def test_log_records_old_separators_inside_fields_are_plain_text() -> None:
    forged = f"{SHA_B}\x1fd\x1fmallory\x1fforged"
    subject = f"a\x1fb | c 한글 \x1f\x1e{forged}\x1e"
    author = "x\x1ey\x1fz"
    raw = _record(SHA_A, "d", author, subject) + _record(SHA_B, "d2", "bob", "real")
    entries = parse_log_records(raw)
    assert [e.sha for e in entries] == [SHA_A, SHA_B]
    assert entries[0].subject == subject
    assert entries[0].author == author
    assert entries[1].subject == "real"


def test_log_records_empty_subject_is_kept() -> None:
    assert parse_log_records(_record(SHA_A, "d", "a", "")) == [GitLogEntry(SHA_A, "a", "d", "")]


def test_log_records_drop_bad_sha_and_resync() -> None:
    raw = (
        b"junk\0"
        + _record("not-a-sha", "d", "x", "s")
        + _record(SHA_A.upper(), "d", "x", "s")
        + _record(SHA_B, "d", "ok", "kept")
    )
    assert [e.sha for e in parse_log_records(raw)] == [SHA_B]


def test_log_records_truncated_trailing_record_is_dropped() -> None:
    full = _record(SHA_A, "d", "a", "s")
    assert parse_log_records(full + f"{SHA_B}\0d\0a".encode()) == [
        GitLogEntry(SHA_A, "a", "d", "s")
    ]
    assert parse_log_records(full + f"{SHA_B}\0d\0a\0subject-without-terminator".encode()) == [
        GitLogEntry(SHA_A, "a", "d", "s")
    ]


def test_log_records_tolerate_a_leading_newline_before_the_sha() -> None:
    raw = b"\n" + _record(SHA_A, "d", "a", "s")
    assert [e.sha for e in parse_log_records(raw)] == [SHA_A]


def test_log_records_invalid_utf8_is_replaced() -> None:
    raw = f"{SHA_A}\0d\0".encode() + b"na\xffme\0sub\xfe\0"
    [entry] = parse_log_records(raw)
    assert entry.author == "na\ufffdme"
    assert entry.subject == "sub\ufffd"


def test_log_records_empty_and_whitespace_only() -> None:
    assert parse_log_records(b"") == []
    assert parse_log_records(b"\n\n") == []
    assert parse_log_records(b"\0\0\0") == []


# ---------------------------------------------------------------- parse_numstat_z


def test_numstat_sums_text_files() -> None:
    raw = b"3\t1\twiki/a.md\0" + b"2\t0\twiki/b.md\0"
    assert parse_numstat_z(raw) == NumStat(files=2, additions=5, deletions=1)


def test_numstat_binary_counts_the_file_but_no_lines() -> None:
    raw = b"-\t-\timg.png\0" + b"4\t2\ta.md\0"
    assert parse_numstat_z(raw) == NumStat(files=2, additions=4, deletions=2)


def test_numstat_path_may_contain_tabs_and_korean() -> None:
    raw = "1\t0\t한글\t경로.md\0".encode()
    assert parse_numstat_z(raw) == NumStat(files=1, additions=1, deletions=0)


def test_numstat_non_numeric_values_count_as_zero_lines() -> None:
    raw = b"x\ty\ta.md\0-1\t+2\tb.md\0\xff\t\xfe\tc.md\0" + b"9" * 5000 + b"\t1\td.md\0"
    assert parse_numstat_z(raw) == NumStat(files=4, additions=0, deletions=1)


def test_numstat_malformed_and_empty_input() -> None:
    assert parse_numstat_z(b"") == NumStat(0, 0, 0)
    assert parse_numstat_z(b"\0\0") == NumStat(0, 0, 0)
    assert parse_numstat_z(b"no tabs at all\0one\ttab\0") == NumStat(0, 0, 0)
    assert parse_numstat_z(b"1\t2") == NumStat(0, 0, 0)  # 경로 칸이 없다


# ---------------------------------------------------------------- 무작위 적대적 입력


def test_parsers_never_raise_on_random_bytes() -> None:
    rng = random.Random(20261007)
    alphabet = [b"\0", b"\t", b"\n", b"\x1e", b"\x1f", b" ", b"?", b"R", b"M", b"!", b"a", b"-"]
    alphabet += [b"9", b"\xff", "한".encode(), SHA_A.encode()]
    for _ in range(400):
        raw = b"".join(rng.choice(alphabet) for _ in range(rng.randint(0, 60)))
        raw += bytes(rng.randrange(256) for _ in range(rng.randint(0, 10)))
        parse_porcelain_z(raw)
        parse_log_records(raw)
        parse_numstat_z(raw)

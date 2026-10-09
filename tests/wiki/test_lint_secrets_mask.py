import time

from team_wiki_engine.wiki.lint_secrets import TOKEN_OMITTED, mask_secret_tokens


def test_token_omitted_value() -> None:
    assert TOKEN_OMITTED == "<비밀값>"


def test_masks_token_shapes() -> None:
    ant = "sk-ant-api03-" + "a" * 30
    ghp = "ghp_" + "b" * 36
    out = mask_secret_tokens(f"x {ant} y {ghp} z")
    assert out == f"x {TOKEN_OMITTED} y {TOKEN_OMITTED} z"


def test_url_password_only() -> None:
    out = mask_secret_tokens("postgres://user:hunter2pass@host/db")
    assert out == f"postgres://user:{TOKEN_OMITTED}@host/db"


def test_url_placeholders_untouched() -> None:
    for text in (
        "postgres://user:password@host",
        "postgres://user:$DB_PASS@host",
        "postgres://user:xxxx@host",
        "postgres://user:user@host",
    ):
        assert mask_secret_tokens(text) == text


def test_private_key_block_fully_masked() -> None:
    text = "a\n-----BEGIN RSA PRIVATE KEY-----\nMIIEsecretbody\n-----END RSA PRIVATE KEY-----\nb"
    out = mask_secret_tokens(text)
    assert "MIIE" not in out and "BEGIN" not in out and "END" not in out
    assert out == f"a\n{TOKEN_OMITTED}\nb"


def test_private_key_without_end_masks_16384_chars() -> None:
    body = "A" * 20_000
    out = mask_secret_tokens("-----BEGIN PRIVATE KEY-----\n" + body)
    assert out == TOKEN_OMITTED + "A" * (20_000 + 28 - 16_384)


def test_overlapping_patterns_leave_no_original_piece() -> None:
    ant = "sk-ant-" + "q" * 40
    text = f"https://u:{ant}@host/x"
    out = mask_secret_tokens(text)
    assert "q" * 5 not in out
    assert "sk-ant" not in out


def test_plain_text_unchanged_and_no_nfkc() -> None:
    text = "ｆｕｌｌ width ＡＢＣ 그대로 // path"
    assert mask_secret_tokens(text) == text


def test_adversarial_inputs_are_linear() -> None:
    for text in (
        "sk-ant-" * 150_000,
        "//:" * 300_000,
        "-----BEGIN PRIVATE KEY-----" * 40_000,
        "ghp_" * 250_000,
    ):
        start = time.monotonic()
        mask_secret_tokens(text)
        assert time.monotonic() - start < 1.0


def test_adjacent_spans_merge_into_one_marker() -> None:
    pair = "AKIA" + "1" * 16 + "AIza" + "A" * 35  # 고정 길이 둘이 맞닿는다
    assert mask_secret_tokens(pair) == TOKEN_OMITTED


def test_extra_shapes_masked() -> None:
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcDEF123_-xyz"
    bearer = "Bearer abcdefghij0123456789ABCDEF"
    aiza = "AIza" + "A" * 35
    hook = "https://hooks.slack.com/services/T0000000/B0000000/XXXXXXXXXXXXXXXXXXXXXXXX"
    out = mask_secret_tokens(f"{jwt} | {bearer} | {aiza} | {hook}")
    assert out == f"{TOKEN_OMITTED} | Bearer {TOKEN_OMITTED} | {TOKEN_OMITTED} | {TOKEN_OMITTED}"


def test_assignment_values_masked() -> None:
    out = mask_secret_tokens('api_key = Xk9mP2qa7rZ\npassword: hunter2pass\nsecret="abcd1234efgh"')
    assert "Xk9mP2qa7rZ" not in out and "hunter2pass" not in out and "abcd1234efgh" not in out
    assert out.count(TOKEN_OMITTED) == 3
    assert "api_key" in out and "password" in out


def test_no_false_positives() -> None:
    for text in (
        "token_type 은 Bearer 라고 문서에 적혀 있다",
        "Bearer token을 헤더에 넣는다",
        "해시 5f4dcc3b5aa765d61d8327deb882cf99 는 그대로",
        "비밀번호를 바꾸는 방법은 위키를 보세요",
        "token_type: Bearer",
        "password: <your-password>",
        "eyJ 로 시작하는 단어",
    ):
        assert mask_secret_tokens(text) == text


def test_idempotent() -> None:
    text = (
        "ghp_" + "b" * 36 + " Bearer abcdefghij0123456789ABCDEF password: hunter2pass "
        "postgres://u:hunter2pass@h/db\n-----BEGIN PRIVATE KEY-----\nAAA\n-----END PRIVATE KEY-----"
    )
    once = mask_secret_tokens(text)
    assert mask_secret_tokens(once) == once


def test_new_patterns_linear() -> None:
    for text in (
        "eyJ" * 350_000,
        "eyJabcdefgh." * 80_000,
        "Bearer " * 150_000,
        "password: " * 100_000,
        "token " * 170_000,
        "AIza" * 250_000,
        "https://hooks.slack.com/services/" * 30_000,
    ):
        start = time.monotonic()
        mask_secret_tokens(text)
        assert time.monotonic() - start < 1.0

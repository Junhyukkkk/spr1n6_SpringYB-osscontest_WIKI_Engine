import json
import os
import random
import stat
import time
import unicodedata
from pathlib import Path

import pytest

from team_wiki_engine.wiki.lint import (
    LintUsageError,
    LintViolation,
    format_violations,
    lint,
    looks_secret,
)
from team_wiki_engine.wiki.lint_secrets import safe_text
from tests.helpers import write_tree

SHA = "a" * 40
MEETING = unicodedata.normalize("NFC", "2026-10-07-회의록.md")
DECISION = unicodedata.normalize("NFC", "2026-10-07-relay-제거.md")
FM_UPDATED = "---\nupdated: 2026-10-07\n---\n"


def valid_repo() -> dict[str, str]:
    return {
        "baseline.json": json.dumps(
            {
                "repos": {
                    "worker": {"commit": SHA, "archived": False},
                    "relay": {"commit": SHA, "archived": True},
                },
                "raw_processed": [MEETING],
                "updated_at": "2026-10-07T09:00:00+09:00",
            }
        ),
        f"raw/{MEETING}": "# 회의록",
        "wiki/index.md": (
            "---\nupdated: 2026-10-07\n---\n\n# 목차\n\n- [worker](status/worker.md)\n"
            "- [relay→worker](contracts/relay--worker--kafka.md)\n"
        ),
        "wiki/overview.md": "---\nupdated: 2026-10-07\n---\n\n# 개요\n",
        "wiki/open-questions.md": "---\nupdated: 2026-10-07\n---\n\n# 미결\n",
        "wiki/log.md": "---\nupdated: 2026-10-07\n---\n\n# 반영 이력\n",
        "wiki/status/worker.md": (
            f"---\nrepo: worker\nbaseline_commit: {SHA}\narchived: false\n"
            "owners: [member-a]\nupdated: 2026-10-07\n---\n\n# worker\n"
        ),
        "wiki/status/relay.md": (
            f"---\nrepo: relay\nbaseline_commit: {SHA}\narchived: true\n"
            "owners: []\nupdated: 2026-10-07\n---\n\n# relay\n"
        ),
        "wiki/contracts/relay--worker--kafka.md": (
            "---\nproducer: relay\nconsumer: worker\nmedium: kafka\nstatus: 현행\n"
            "superseded_by: null\nevidence:\n  - relay@07a4acf:src/A.kt\n"
            "updated: 2026-10-07\n---\n\n"
            f"# relay → worker\n\n[결정](../decisions/{DECISION})\n"
        ),
        f"wiki/decisions/{DECISION}": (
            "---\nstatus: 논의 중\ndecided_at: 2026-10-07\nsuperseded_by: null\n"
            f"sources: [{MEETING}]\n"
            "updated: 2026-10-07\n---\n\n# relay 제거\n"
        ),
        f"wiki/sources/{MEETING}": (
            f"---\nraw: {MEETING}\ndate: 2026-10-07\naffects: [decisions/{DECISION}]\n"
            "updated: 2026-10-07\n---\n\n# 요약\n"
        ),
    }


@pytest.fixture
def data_dir(tmp_dir: Path) -> Path:
    """`tmp_dir/data`에 정상 저장소를 만든다(바깥 형제 파일을 둘 수 있게 한 단계 안쪽)."""
    root = tmp_dir / "data"
    write_tree(root, valid_repo())
    return root


def rules(violations: list[LintViolation]) -> list[str]:
    return [f"{v.rule} {v.file}" for v in violations]


def append(root: Path, rel: str, text: str) -> None:
    with (root / rel).open("a", encoding="utf-8") as f:
        f.write(text)


def write(root: Path, rel: str, text: str) -> None:
    write_tree(root, {rel: text})


def test_valid_repo_has_no_violations(data_dir: Path) -> None:
    assert lint(data_dir) == []


def test_violation_is_frozen_dataclass() -> None:
    v = LintViolation("L1", "wiki/a.md", "m")
    with pytest.raises(AttributeError):
        v.rule = "L2"  # type: ignore[misc]


class TestL1Secrets:
    def test_token_format_is_flagged_without_value(self, data_dir: Path) -> None:
        token = "sk-ant-" + "x" * 30
        append(data_dir, "wiki/overview.md", f"\n키: {token}\n")
        v = lint(data_dir)
        assert rules(v) == ["L1 wiki/overview.md"]
        assert token not in v[0].message

    def test_password_assignment(self, data_dir: Path) -> None:
        append(data_dir, "wiki/overview.md", "\npassword: hunter2024\n")
        assert rules(lint(data_dir)) == ["L1 wiki/overview.md"]

    def test_field_docs_and_placeholders_pass(self, data_dir: Path) -> None:
        append(
            data_dir,
            "wiki/overview.md",
            "\npassword: string\ntoken: <발급받은 토큰>\nsecret=${SECRET}\npassword: ********\n",
        )
        assert lint(data_dir) == []

    def test_looks_secret_criteria(self) -> None:
        assert looks_secret("hunter2024") is True
        assert looks_secret("string") is False
        assert looks_secret("abc123") is False
        assert looks_secret("<token1234>") is False
        assert looks_secret("${TOKEN1234}") is False
        assert looks_secret("********") is False
        assert looks_secret("12345678") is False
        assert looks_secret("abcdefgh") is False
        assert looks_secret("") is False

    @pytest.mark.parametrize(
        ("text", "label"),
        [
            ("ghp_" + "A1" * 20, "GitHub 토큰 형식"),
            ("github_pat_" + "a1" * 30, "GitHub fine-grained 토큰 형식"),
            ("AKIA" + "ABCDEF0123456789", "AWS 액세스 키 형식"),
            ("-----BEGIN RSA PRIVATE KEY-----", "개인 키"),
            ("-----BEGIN PRIVATE KEY-----", "개인 키"),
            ("xoxb-123456789012-1234567890123-AbCdEfGhIjKl", "Slack 토큰 형식"),
            ("xoxp-1234567890-abcdefghij", "Slack 토큰 형식"),
            ("MTA" + "x" * 21 + ".Gabcde." + "y" * 27, "Discord 봇 토큰 형식"),
            ("N" + "z_-" * 8 + "a" + ".abcdef." + "Q" * 30, "Discord 봇 토큰 형식"),
        ],
    )
    def test_token_formats(self, data_dir: Path, text: str, label: str) -> None:
        append(data_dir, "wiki/overview.md", f"\n{text}\n")
        v = lint(data_dir)
        assert [x.message for x in v] == [f"비밀값으로 보이는 문자열: {label}"]
        assert v[0].file == "wiki/overview.md"

    def test_secret_in_code_block_and_frontmatter_is_flagged(self, data_dir: Path) -> None:
        write(
            data_dir, "wiki/overview.md", "---\nupdated: 2026-10-07\napi_key: Zq81abcdefgh\n---\n"
        )
        write(data_dir, "wiki/open-questions.md", FM_UPDATED + "\n```\npassword: hunter2024\n```\n")
        assert rules(lint(data_dir)) == ["L1 wiki/open-questions.md", "L1 wiki/overview.md"]

    FLAGGED = [  # (줄, 값, 기대 메시지 키워드)
        ("access_token: abcdefg12345", "abcdefg12345", "token"),
        ("client_secret: abcdefg12345", "abcdefg12345", "secret"),
        ("API_TOKEN=abcdefg12345", "abcdefg12345", "token"),
        ("DB_PASSWORD=abcdefg12345", "abcdefg12345", "password"),
        ("my-password: abcdefg12345", "abcdefg12345", "password"),
        ('{"password": "hunter2024xx"}', "hunter2024xx", "password"),
        ("'secret': 'hunter2024xx'", "hunter2024xx", "secret"),
        ("**password**: hunter2024xx", "hunter2024xx", "password"),
        ("| password | hunter2024xx |", "hunter2024xx", "password"),
        ("password: `hunter2024xx`", "hunter2024xx", "password"),
        ("DB 비밀번호: Xk9mP2qa7", "Xk9mP2qa7", "비밀번호"),
        ("비번=Xk9mP2qa7", "Xk9mP2qa7", "비번"),
        ("관리자 패스워드 : Xk9mP2qa7", "Xk9mP2qa7", "패스워드"),
        ("토큰: Xk9mP2qa7", "Xk9mP2qa7", "토큰"),
        ("시크릿: Xk9mP2qa7", "Xk9mP2qa7", "시크릿"),
        ("token 값: abcdefg12345", "abcdefg12345", "token"),
        ("Password value = abcdefg12345", "abcdefg12345", "password"),
        ("api_key: Zq81abcdefgh", "Zq81abcdefgh", "api_key"),
        ("API_KEY=Zq81abcdefgh", "Zq81abcdefgh", "api_key"),
        ("api-key: Zq81abcdefgh", "Zq81abcdefgh", "api-key"),
        ("apikey: Zq81abcdefgh", "Zq81abcdefgh", "apikey"),
        ("AWS_ACCESS_KEY: Zq81abcdefgh", "Zq81abcdefgh", "access_key"),
        ("private_key: Zq81abcdefgh", "Zq81abcdefgh", "private_key"),
        ("credential: Zq81abcdefgh", "Zq81abcdefgh", "credential"),
        ("passwd=Xk9mP2qa7", "Xk9mP2qa7", "passwd"),
        ("DB비밀번호: Xk9mP2qa7", "Xk9mP2qa7", "비밀번호"),
        ("API토큰: Xk9mP2qa7", "Xk9mP2qa7", "토큰"),
        ("GitHub토큰=Xk9mP2qa7", "Xk9mP2qa7", "토큰"),
        ("SSH비번: Xk9mP2qa7", "Xk9mP2qa7", "비번"),
        ("AWS시크릿: Xk9mP2qa7", "Xk9mP2qa7", "시크릿"),
        ("token_secret: Xk9mP2qa7", "Xk9mP2qa7", "token"),
        ("secret_key=Xk9mP2qa7", "Xk9mP2qa7", "secret"),
        ("password_prod: Xk9mP2qa7", "Xk9mP2qa7", "password"),
        ("redis://:Xk9mP2qa7@10.0.3.15:6379", "Xk9mP2qa7", "URL"),
        ("password:\u00a0Xk9mP2qa7", "Xk9mP2qa7", "password"),
        (unicodedata.normalize("NFD", "DB 비밀번호: Xk9mP2qa7"), "Xk9mP2qa7", "비밀번호"),
        ("ｐａｓｓｗｏｒｄ: Xk9mP2qa7", "Xk9mP2qa7", "password"),
        ("비밀번호： Xk9mP2qa7", "Xk9mP2qa7", "비밀번호"),
        ("password： Xk9mP2qa7", "Xk9mP2qa7", "password"),
        ("DB 비밀번호는 Xk9mP2qa7 입니다", "Xk9mP2qa7", "비밀번호"),
        ("패스워드는 Xk9mP2qa7", "Xk9mP2qa7", "패스워드"),
        ("비밀번호는 Xk9mP2qa7입니다", "Xk9mP2qa7", "비밀번호"),
        ("토큰은 Xk9mP2qa7이다", "Xk9mP2qa7", "토큰"),
        ("비밀번호는 `Xk9mP2qa7`입니다", "Xk9mP2qa7", "비밀번호"),
        ('비밀번호는 "Xk9mP2qa7"', "Xk9mP2qa7", "비밀번호"),
        ("비밀번호가 Xk9mP2qa7로 바뀌었다", "Xk9mP2qa7", "비밀번호"),
        ("비밀번호는 Xk9mP2qa7.", "Xk9mP2qa7", "비밀번호"),
        ("비밀번호는 Xk9mP2qa7, 만료는 내일", "Xk9mP2qa7", "비밀번호"),
        ("(비밀번호는 Xk9mP2qa7)", "Xk9mP2qa7", "비밀번호"),
        ("비밀번호는 **Xk9mP2qa7**", "Xk9mP2qa7", "비밀번호"),
        ("비밀번호는 *Xk9mP2qa7*", "Xk9mP2qa7", "비밀번호"),
        ("비밀번호는 Xk9mP2qa7!", "Xk9mP2qa7", "비밀번호"),
        ("비밀번호는 Xk9mP2qa7;", "Xk9mP2qa7", "비밀번호"),
        ("비밀번호는 Xk9mP2qa 입니다", "Xk9mP2qa", "비밀번호"),  # 정확히 8자
        ("비밀번호는 " + "Xk9mP2qa" * 8 + " 입니다", "Xk9mP2qa", "비밀번호"),  # 정확히 64자
        ("//user:Xk9mP2qa7@host/x", "Xk9mP2qa7", "URL"),
        ("(//deploy:Xk9mP2qa7@host)", "Xk9mP2qa7", "URL"),
        ("토큰이 Xk9mP2qa7", "Xk9mP2qa7", "토큰"),
        ("DB 비밀번호(운영): Xk9mP2qa7", "Xk9mP2qa7", "비밀번호"),
        ("password (prod): Xk9mP2qa7", "Xk9mP2qa7", "password"),
        ("token 값 (운영) = Xk9mP2qa7", "Xk9mP2qa7", "token"),
        ("postgres://user:Xk9mP2qa7@host/db", "Xk9mP2qa7", "URL"),
        ("mongodb://admin:Xk9mP2qa7@host", "Xk9mP2qa7", "URL"),
        ("https://deploy:s3cr3tpass@example.com/x", "s3cr3tpass", "URL"),
    ]

    @pytest.mark.parametrize(("line", "value", "keyword"), FLAGGED)
    def test_assignment_forms_are_flagged_without_value(
        self, data_dir: Path, line: str, value: str, keyword: str
    ) -> None:
        append(data_dir, "wiki/overview.md", f"\n{line}\n")
        v = lint(data_dir)
        assert rules(v) == ["L1 wiki/overview.md"]
        assert value not in v[0].message
        if keyword == "URL":
            assert v[0].message == "비밀값으로 보이는 문자열: URL 속 자격증명"
        else:
            assert v[0].message == f"비밀값으로 보이는 대입: {keyword}=…"

    PASSED = [
        "password: string",
        "token: <발급받은 토큰>",
        "secret=${SECRET}",
        "password: ********",
        "토큰 만료 시간: 3600초",
        "tokenizer: abc12345xyz",
        "secretary: kim12345678",
        "| password | string |",
        "postgres://user:<비밀번호>@host",
        "postgres://user:${DB_PASSWORD}@host/db",
        "https://example.com:8080/path",
        "ssh://git@github.com/org/repo.git",
        "## Token 발급",
        "token: abc",
        "mypassword: abc12345xyz",
        "비밀번호 규칙: 8자 이상",
        "api_keys 목록",
        "토큰화: 방식",
        "토큰이 발급되면 만료됩니다",
        "비밀번호는 8자 이상",
        "password (optional): string",
        "password (" + "x" * 40 + "): Xk9mP2qa7",
        "redis://localhost:6379",
        "http://:8080/x",
        "postgres://user:****@host/db",
        "postgres://user:password@host",
        "postgres://app:PASSWORD@host",
        "postgres://root:pass@host",
        "postgres://user:xxxx@host",
        "postgres://user:XXXXXXXX@host",
        "postgres://user:$DB_PASSWORD@host",
        "postgres://user:{password}@host",
        "postgres://user:[YOUR_PASSWORD]@host",
        "postgres://user:%PASSWORD%@host",
        "postgres://user:pw@host",
        "postgres://user:secret@host",
        "postgres://user:비밀번호@host",
        "amqp://guest:guest@host",
        "//user:password@host",
        "x//user:Xk9mP2qa7@host",
        "////user:Xk9mP2qa7@host",
        "//cdn.example.com/lib.js",
        "// token: x",
        "//localhost:3000/path",
        "토큰은 base64url 인코딩한 문자열이다",
        "비밀번호는 bcrypt12 로 해시한다",
        "비밀번호는 sha256crypt 로 저장한다",
        "토큰은 OAuth2.0 표준을 따른다",
        "토큰은 JWT(RS256) 형식이다",
        "토큰은 HMAC-SHA256 으로 서명한다",
        "비밀번호는 PBKDF2-SHA256 으로 해시한다",
        "비밀번호는 AES256-GCM 으로 암호화한다",
        "토큰은 x-api-key2 헤더로 보낸다",
        "시크릿은 k8s-secret 리소스로",
        "비밀번호는 최소 8자 이상이어야 한다",
        "비밀번호는 Xk9mP2q 입니다",  # 7자
        "비밀번호는 " + "Xk9mP2qa" * 8 + "x 입니다",  # 65자
        "비밀번호는 Xk9mP2qa7.0",
        "비밀번호는 HMAC-SHA256",
        "비밀번호는 OAuth2.0.",
        "토큰은 JWT(RS256).",
        "x ://user:Xk9mP2qa7@host",
        "API 토큰은 환경변수로 주입한다",
        "비밀번호는 Abcdefghij 입니다",  # 숫자 없음
        "비밀번호는 abcdefg123 입니다",  # 대문자 없음
        "비밀번호는 ABCDEFG123 입니다",  # 소문자 없음
        "비밀번호는 Xk9mP2qa7.0 입니다",
        "비밀번호는 Xk9mP2qa7-x 입니다",
        "비밀번호는 Xk9mP2qa7(x) 입니다",
        "비밀번호는 Xk9mP2qa7_x 입니다",
        "비밀번호는 Xk9mP2qa7/x 입니다",
        "비밀번호는 Xk9mP2qa7+x 입니다",
        "비밀번호는 Xk9mP2qa7:x 입니다",
        "비밀번호는 Xk9mP2qa7,x 입니다",
        "비밀번호는 " + "Xk9mP2qa7" * 8 + " 입니다",  # 64자 초과
    ]

    @pytest.mark.parametrize("line", PASSED)
    def test_non_secrets_pass(self, data_dir: Path, line: str) -> None:
        append(data_dir, "wiki/overview.md", f"\n{line}\n")
        assert lint(data_dir) == []

    def test_message_uses_lowercase_keyword_only(self, data_dir: Path) -> None:
        append(data_dir, "wiki/overview.md", "\nAPI_TOKEN=abcdefg12345\n")
        assert lint(data_dir)[0].message == "비밀값으로 보이는 대입: token=…"

    def test_long_key_name_is_still_flagged(self, data_dir: Path) -> None:
        append(data_dir, "wiki/overview.md", f"\n{'a_' * 20}token: abcdefg12345\n")
        assert rules(lint(data_dir)) == ["L1 wiki/overview.md"]

    def test_key_tail_over_limit_is_not_a_key(self, data_dir: Path) -> None:
        append(data_dir, "wiki/overview.md", f"\ntoken{'_a' * 40}: abcdefg12345\n")
        assert lint(data_dir) == []

    def test_value_is_judged_on_first_256_chars(self, data_dir: Path) -> None:
        append(data_dir, "wiki/overview.md", f"\npassword: Xk9mP2qa7{'x' * 5000}\n")
        v = lint(data_dir)
        assert rules(v) == ["L1 wiki/overview.md"]
        assert "Xk9mP2qa7" not in v[0].message

    def test_identical_findings_are_reported_once_per_file(self, data_dir: Path) -> None:
        append(data_dir, "wiki/overview.md", "\npassword: hunter2024\npassword: other2024xx\n")
        assert len(lint(data_dir)) == 1


class TestL2BrokenLinks:
    def test_missing_target_is_flagged(self, data_dir: Path) -> None:
        append(data_dir, "wiki/index.md", "\n- [없음](decisions/none.md)\n")
        v = lint(data_dir)
        assert v == [LintViolation("L2", "wiki/index.md", "깨진 링크: decisions/none.md")]

    def test_percent_encoding_and_anchor_pass(self, data_dir: Path) -> None:
        append(
            data_dir,
            "wiki/index.md",
            "\n- [결정](decisions/2026-10-07-relay-%EC%A0%9C%EA%B1%B0.md)\n"
            "- [담당](status/worker.md#담당)\n",
        )
        assert lint(data_dir) == []

    def test_title_and_angle_brackets_are_understood(self, data_dir: Path) -> None:
        append(
            data_dir, "wiki/index.md", '\n[a](status/worker.md "제목")\n[b](<status/relay.md>)\n'
        )
        assert lint(data_dir) == []
        append(data_dir, "wiki/index.md", '\n[c](none.md "제목")\n')
        assert [x.message for x in lint(data_dir)] == ["깨진 링크: none.md"]

    def test_external_anchor_and_code_links_are_skipped(self, data_dir: Path) -> None:
        append(
            data_dir,
            "wiki/index.md",
            "\n[gh](https://github.com)\n[mail](mailto:a@b.c)\n[위](#목차)\n"
            "```\n[x](none.md)\n```\n`[y](none.md)`\n",
        )
        assert lint(data_dir) == []

    def test_invalid_percent_encoding_is_checked_as_written(self, data_dir: Path) -> None:
        append(data_dir, "wiki/index.md", "\n[x](bad%ZZ.md) [y](%FF.md)\n")
        assert [x.message for x in lint(data_dir)] == ["깨진 링크: bad%ZZ.md", "깨진 링크: %FF.md"]

    def test_nfd_link_to_nfc_file_passes(self, data_dir: Path) -> None:
        nfd = unicodedata.normalize("NFD", DECISION)
        append(data_dir, "wiki/index.md", f"\n[x](decisions/{nfd})\n")
        assert lint(data_dir) == []

    def test_link_outside_data_dir_is_broken(self, data_dir: Path) -> None:
        append(data_dir, "wiki/index.md", "\n[x](../../etc/passwd)\n")
        assert [x.message for x in lint(data_dir)] == ["깨진 링크: ../../etc/passwd"]

    def test_existing_file_outside_data_dir_is_broken(self, data_dir: Path) -> None:
        (data_dir.parent / "outside.md").write_text("# 밖", encoding="utf-8")
        append(data_dir, "wiki/index.md", "\n[x](../../outside.md)\n[y](/etc/hosts)\n")
        assert [x.message for x in lint(data_dir)] == [
            "깨진 링크: ../../outside.md",
            "깨진 링크: /etc/hosts",
        ]

    def test_link_inside_data_dir_but_outside_wiki_is_fine(self, data_dir: Path) -> None:
        append(data_dir, "wiki/index.md", f"\n[원본](../raw/{MEETING})\n")
        assert lint(data_dir) == []

    def test_query_is_not_echoed_and_not_needed_to_resolve(self, data_dir: Path) -> None:
        append(
            data_dir,
            "wiki/index.md",
            "\n[ok](status/worker.md?v=1)\n[x](none.md?session=SECRETVALUE9#a)\n",
        )
        v = lint(data_dir)
        assert [x.message for x in v] == ["깨진 링크: none.md"]
        assert "SECRETVALUE9" not in v[0].message

    def test_over_limit_links_are_not_checked(self, data_dir: Path) -> None:
        long_target = "a" * 1100 + ".md"
        long_label = "l" * 600
        append(data_dir, "wiki/index.md", f"\n[x]({long_target})\n[{long_label}](none.md)\n")
        assert lint(data_dir) == []

    def test_links_just_under_the_limits_are_checked(self, data_dir: Path) -> None:
        append(
            data_dir, "wiki/index.md", f"\n[{'l' * 512}]({'a' * 1000}.md)\n[{'k' * 512}](n2.md)\n"
        )
        assert len(lint(data_dir)) == 2

    def test_many_links_to_the_same_target(self, data_dir: Path) -> None:
        append(
            data_dir, "wiki/index.md", "".join(f"\n[l{i}](status/worker.md)" for i in range(300))
        )
        assert lint(data_dir) == []


class TestL3Evidence:
    def test_missing_required_field(self, data_dir: Path) -> None:
        write(
            data_dir,
            "wiki/status/worker.md",
            f"---\nrepo: worker\nbaseline_commit: {SHA}\narchived: false\n"
            "updated: 2026-10-07\n---\n",
        )
        assert lint(data_dir) == [
            LintViolation("L3", "wiki/status/worker.md", "필수 필드 누락: owners")
        ]

    @pytest.mark.parametrize(
        "evidence", ["[relay:src/A.kt]", "[]", "[123]", "x", "[Relay@abcdef1:a]"]
    )
    def test_evidence_format_and_empty_list(self, data_dir: Path, evidence: str) -> None:
        write(
            data_dir,
            "wiki/contracts/relay--worker--kafka.md",
            "---\nproducer: relay\nconsumer: worker\nmedium: kafka\nstatus: 현행\n"
            f"evidence: {evidence}\nupdated: 2026-10-07\n---\n",
        )
        assert rules(lint(data_dir)) == ["L3 wiki/contracts/relay--worker--kafka.md"]

    def test_missing_raw_for_sources_and_decisions(self, data_dir: Path) -> None:
        (data_dir / "raw" / MEETING).unlink()
        assert sorted(rules(lint(data_dir))) == [
            f"L3 wiki/decisions/{DECISION}",
            f"L3 wiki/sources/{MEETING}",
        ]

    def test_nfd_raw_file_is_found(self, data_dir: Path) -> None:
        (data_dir / "raw" / MEETING).unlink()
        (data_dir / "raw" / unicodedata.normalize("NFD", MEETING)).write_text(
            "# 회의록", encoding="utf-8"
        )
        assert lint(data_dir) == []

    def test_raw_must_name_a_file_inside_raw(self, data_dir: Path) -> None:
        for raw in ("..", ".", "../baseline.json", "/etc/hosts"):
            write(
                data_dir,
                f"wiki/sources/{MEETING}",
                f"---\nraw: {raw}\ndate: 2026-10-07\naffects: []\nupdated: 2026-10-07\n---\n",
            )
            assert rules(lint(data_dir)) == [f"L3 wiki/sources/{MEETING}"], raw

    def test_non_string_raw_is_flagged(self, data_dir: Path) -> None:
        write(
            data_dir,
            f"wiki/sources/{MEETING}",
            "---\nraw: 5\ndate: 2026-10-07\naffects: []\nupdated: 2026-10-07\n---\n",
        )
        assert rules(lint(data_dir)) == [f"L3 wiki/sources/{MEETING}"]

    def test_missing_or_broken_frontmatter(self, data_dir: Path) -> None:
        write(data_dir, "wiki/overview.md", "# 개요만\n")
        write(data_dir, "wiki/open-questions.md", "---\nupdated: [깨짐\n---\n")
        v = lint(data_dir)
        assert sorted(rules(v)) == ["L3 wiki/open-questions.md", "L3 wiki/overview.md"]
        assert {x.file: x.message for x in v}["wiki/overview.md"] == "frontmatter가 없습니다"

    def test_empty_frontmatter_lacks_required_fields(self, data_dir: Path) -> None:
        write(data_dir, "wiki/overview.md", "---\n---\n# x\n")
        assert lint(data_dir) == [
            LintViolation("L3", "wiki/overview.md", "필수 필드 누락: updated")
        ]

    def test_blank_updated_counts_as_missing(self, data_dir: Path) -> None:
        write(data_dir, "wiki/overview.md", '---\nupdated: "  "\n---\n')
        assert rules(lint(data_dir)) == ["L3 wiki/overview.md"]

    DECISION_FILE = f"wiki/decisions/{DECISION}"

    @pytest.mark.parametrize(
        "sources",
        [
            "",
            f"sources: {MEETING}\n",
            "sources: []\n",
            "sources: [123]\n",
            "sources: [null]\n",
            "sources: [[a]]\n",
        ],
        ids=["missing", "scalar", "empty list", "non-string item", "null item", "nested list"],
    )
    def test_decision_sources_must_be_non_empty_string_list(
        self, data_dir: Path, sources: str
    ) -> None:
        write(
            data_dir,
            self.DECISION_FILE,
            f"---\nstatus: 확정\ndecided_at: 2026-10-07\n{sources}updated: 2026-10-07\n---\n",
        )
        assert rules(lint(data_dir)) == [f"L3 {self.DECISION_FILE}"]

    def test_empty_owners_list_is_allowed_for_status(self, data_dir: Path) -> None:
        assert lint(data_dir) == []


class TestL4Consistency:
    def decision(self, data_dir: Path, status: str, extra: str = "") -> None:
        write(
            data_dir,
            f"wiki/decisions/{DECISION}",
            f"---\nstatus: {status}\ndecided_at: 2026-10-07\nsources: [{MEETING}]\n{extra}"
            "updated: 2026-10-07\n---\n",
        )

    def test_status_outside_allowed_values(self, data_dir: Path) -> None:
        self.decision(data_dir, "보류")
        v = lint(data_dir)
        assert rules(v) == [f"L4 wiki/decisions/{DECISION}"]
        assert "허용: 확정, 대체됨, 논의 중" in v[0].message

    def test_contract_status_uses_its_own_allowed_values(self, data_dir: Path) -> None:
        path = "wiki/contracts/relay--worker--kafka.md"
        text = (data_dir / path).read_text(encoding="utf-8")
        write(data_dir, path, text.replace("status: 현행", "status: 확정"))
        assert rules(lint(data_dir)) == [f"L4 {path}"]

    def test_non_string_status_is_flagged(self, data_dir: Path) -> None:
        self.decision(data_dir, "[확정]")
        assert rules(lint(data_dir)) == [f"L4 wiki/decisions/{DECISION}"]

    def test_nfd_status_value_is_accepted(self, data_dir: Path) -> None:
        self.decision(data_dir, unicodedata.normalize("NFD", "확정"))
        assert lint(data_dir) == []

    def test_superseded_requires_superseded_by_that_exists(self, data_dir: Path) -> None:
        self.decision(data_dir, "대체됨")
        assert rules(lint(data_dir)) == [f"L4 wiki/decisions/{DECISION}"]
        self.decision(data_dir, "대체됨", "superseded_by: 2026-10-09-없음\n")
        assert rules(lint(data_dir)) == [f"L4 wiki/decisions/{DECISION}"]
        self.decision(data_dir, "대체됨", "superseded_by: 123\n")
        assert rules(lint(data_dir)) == [f"L4 wiki/decisions/{DECISION}"]

    def test_superseded_by_must_stay_inside_the_data_dir(self, data_dir: Path) -> None:
        self.decision(data_dir, "대체됨", "superseded_by: ../../../../etc/hosts\n")
        assert rules(lint(data_dir)) == [f"L4 wiki/decisions/{DECISION}"]

    def test_superseded_by_with_or_without_md_passes(self, data_dir: Path) -> None:
        write(
            data_dir,
            "wiki/decisions/2026-10-09-relay-유지.md",
            f"---\nstatus: 확정\ndecided_at: 2026-10-09\nsources: [{MEETING}]\n"
            "updated: 2026-10-09\n---\n",
        )
        self.decision(data_dir, "대체됨", "superseded_by: 2026-10-09-relay-유지\n")
        assert lint(data_dir) == []
        self.decision(data_dir, "대체됨", "superseded_by: 2026-10-09-relay-유지.md\n")
        assert lint(data_dir) == []

    def test_active_repo_without_status_page_is_reported_on_baseline(self, data_dir: Path) -> None:
        (data_dir / "wiki/status/worker.md").unlink()
        assert [v for v in lint(data_dir) if v.rule == "L4"] == [
            LintViolation("L4", "baseline.json", "현황 페이지가 없습니다: wiki/status/worker.md")
        ]

    def test_archived_repo_may_lack_status_page(self, data_dir: Path) -> None:
        (data_dir / "wiki/status/relay.md").unlink()
        assert lint(data_dir) == []

    def test_status_page_archived_must_match_baseline(self, data_dir: Path) -> None:
        write(
            data_dir,
            "wiki/status/relay.md",
            f"---\nrepo: relay\nbaseline_commit: {SHA}\narchived: false\nowners: []\n"
            "updated: 2026-10-07\n---\n",
        )
        assert rules(lint(data_dir)) == ["L4 wiki/status/relay.md"]

    def test_archived_is_compared_strictly(self, data_dir: Path) -> None:
        write(
            data_dir,
            "wiki/status/worker.md",
            f"---\nrepo: worker\nbaseline_commit: {SHA}\narchived: 0\nowners: []\n"
            "updated: 2026-10-07\n---\n",
        )
        assert rules(lint(data_dir)) == ["L4 wiki/status/worker.md"]

    def test_broken_baseline_is_reported(self, data_dir: Path) -> None:
        write(data_dir, "baseline.json", "{")
        v = lint(data_dir)
        assert rules(v) == ["L4 baseline.json"]
        assert v[0].message.startswith("baseline.json을 읽을 수 없습니다: ")

    def test_baseline_that_is_a_directory_is_reported_not_raised(self, data_dir: Path) -> None:
        (data_dir / "baseline.json").unlink()
        (data_dir / "baseline.json").mkdir()
        assert rules(lint(data_dir)) == ["L4 baseline.json"]

    def test_missing_baseline_is_an_empty_baseline(self, data_dir: Path) -> None:
        (data_dir / "baseline.json").unlink()
        assert lint(data_dir) == []


class TestL5Symlinks:
    def test_file_directory_and_dangling_symlinks_are_reported(self, data_dir: Path) -> None:
        wiki = data_dir / "wiki"
        os.symlink(wiki / "overview.md", wiki / "alias.md")
        os.symlink(data_dir / "raw", wiki / "rawlink")
        os.symlink(data_dir / "nowhere", wiki / "status" / "dangling.md")
        v = lint(data_dir)
        assert [(x.rule, x.file) for x in v] == [
            ("L5", "wiki/alias.md"),
            ("L5", "wiki/rawlink"),
            ("L5", "wiki/status/dangling.md"),
        ]
        assert v[1].message == "위키 폴더에는 심볼릭 링크를 둘 수 없습니다: wiki/rawlink"

    def test_linked_markdown_content_is_not_read(self, data_dir: Path) -> None:
        outside = data_dir.parent / "outside.md"
        outside.write_text("password: hunter2024\n", encoding="utf-8")
        os.symlink(outside, data_dir / "wiki" / "leak.md")
        assert rules(lint(data_dir)) == ["L5 wiki/leak.md"]

    def test_link_loops_finish_immediately(self, data_dir: Path) -> None:
        wiki = data_dir / "wiki"
        os.symlink(".", wiki / "self")
        os.symlink(wiki / "b", wiki / "a")
        os.symlink(wiki / "a", wiki / "b")
        os.symlink("..", wiki / "status" / "up")
        t0 = time.perf_counter()
        v = lint(data_dir)
        assert time.perf_counter() - t0 < 3
        assert [x.rule for x in v] == ["L5"] * 4


class TestFormatViolations:
    def test_lines_and_total(self) -> None:
        assert (
            format_violations([LintViolation("L2", "wiki/a.md", "깨진 링크: b.md")])
            == "[L2] wiki/a.md: 깨진 링크: b.md\n위반 1건"
        )
        assert (
            format_violations(
                [LintViolation("L1", "wiki/a.md", "x"), LintViolation("L5", "wiki/b", "y")]
            )
            == "[L1] wiki/a.md: x\n[L5] wiki/b: y\n위반 2건"
        )
        assert format_violations([]) == "점검 통과"


class TestUsageErrors:
    def test_missing_directory(self, data_dir: Path) -> None:
        missing = data_dir / "none"
        with pytest.raises(LintUsageError) as info:
            lint(missing)
        assert str(info.value) == f"데이터 폴더를 읽을 수 없습니다: {missing}"

    def test_path_is_a_file(self, data_dir: Path) -> None:
        file = data_dir / "baseline.json"
        with pytest.raises(LintUsageError) as info:
            lint(file)
        assert str(info.value) == f"데이터 폴더를 읽을 수 없습니다: {file}"

    def test_empty_directory_has_no_wiki(self, tmp_dir: Path) -> None:
        with pytest.raises(LintUsageError) as info:
            lint(tmp_dir)
        assert str(info.value) == f"wiki 폴더가 없습니다: {tmp_dir / 'wiki'}"

    def test_wiki_is_a_file(self, tmp_dir: Path) -> None:
        (tmp_dir / "wiki").write_text("x", encoding="utf-8")
        with pytest.raises(LintUsageError):
            lint(tmp_dir)

    def test_existing_empty_wiki_passes(self, tmp_dir: Path) -> None:
        (tmp_dir / "wiki").mkdir()
        assert lint(tmp_dir) == []

    def test_relative_data_dir_is_accepted(
        self, data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(data_dir.parent)
        assert lint(Path("data")) == []


class TestFolderNames:
    @pytest.mark.parametrize(
        "name",
        ["constructor", "toString", "__proto__", "hasOwnProperty", "__class__", "get", "items"],
    )
    def test_unknown_folder_requires_only_updated(self, data_dir: Path, name: str) -> None:
        write(data_dir, f"wiki/{name}/a.md", FM_UPDATED)
        assert lint(data_dir) == []
        write(data_dir, f"wiki/{name}/a.md", "---\ntitle: x\n---\n")
        assert lint(data_dir) == [
            LintViolation("L3", f"wiki/{name}/a.md", "필수 필드 누락: updated")
        ]

    def test_top_level_pages_need_only_updated(self, data_dir: Path) -> None:
        write(data_dir, "wiki/status.md", FM_UPDATED)
        assert lint(data_dir) == []


def run_payload(root: Path, payload: str) -> float:
    write(root, "wiki/overview.md", f"---\nupdated: 2026-10-07\n---\n\n{payload}\n")
    t0 = time.perf_counter()
    lint(root)
    return time.perf_counter() - t0


PAYLOADS = {
    "a_ x200k": "a_" * 200_000,
    "ab- x70k": "ab-" * 70_000,
    "token + _a x50k": "token" + "_a" * 50_000,
    "token_ x40k": "token_" * 40_000,
    "token=value x20k": "token=abcdefg12345" * 20_000,
    "quote x200k": '"' * 200_000,
    "pipe x200k": "|" * 200_000,
    "space x200k": " " * 200_000,
    "token + space x200k": "token" + " " * 200_000,
    "token + quote x200k": "token" + '"' * 200_000,
    "[ x200k": "[" * 200_000,
    "[]( x70k": "[](" * 70_000,
    "]( x100k": "](" * 100_000,
    "[a]( x50k": "[a](" + "b" * 10 + "\n[a](" * 50_000,
    "token: x lines x50k": "token: x\n" * 50_000,
    "token_a: lines x50k": "token_a: \n" * 50_000,
    "particle x40k": "비밀번호는 " * 40_000,
    "qualifier x40k": "password (" * 40_000,
    "nbsp x200k": "password:" + "\u00a0" * 200_000,
    "fullwidth x100k": "ｐａｓｓ" * 50_000,
    "redis x60k": "://:" * 60_000,
    "backtick x200k": "`" * 200_000,
    "fence x70k": "```" * 70_000,
    "unclosed fences": "```a\n" * 40_000,
    "://x100k": "://" * 66_000,
    "url userinfo": "a://"
    + "u" * 60
    + ":"
    + "p" * 120
    + "\n" * 1
    + ("://" + "u" * 60 + ":" + "p" * 120 + " ") * 1000,
    "BEGIN x20k": "-----BEGIN " * 20_000,
    "sk-ant- x30k": "sk-ant-" * 30_000,
    "ghp_ x50k": "ghp_" * 50_000,
    "github_pat_ x20k": "github_pat_" * 20_000,
    "xox x50k": "xoxb-" * 50_000,
    "discord-like": ("M" + "a" * 24 + ".") * 8_000,
    "api_key x60k": "api_key" * 28_000,
    "password x25k": "password" * 25_000,
    "비밀번호 x60k": "비밀번호" * 50_000,
    "key + value labels": "token key" * 22_000,
}


@pytest.mark.parametrize("name", list(PAYLOADS))
def test_adversarial_payloads_finish_in_linear_time(data_dir: Path, name: str) -> None:
    payload = PAYLOADS[name]
    assert len(payload) >= 100_000 or name in {"url userinfo", "unclosed fences"}
    elapsed = run_payload(data_dir, payload)
    assert elapsed < 3, f"{name}: {elapsed:.2f}s"


def test_hostile_pages_never_raise(data_dir: Path) -> None:
    rng = random.Random(20261007)
    pieces = [
        "[",
        "]",
        "(",
        ")",
        "](",
        "<",
        ">",
        "`",
        "```",
        '"',
        "'",
        "*",
        "|",
        ":",
        "=",
        "---\n",
        "\n",
        " ",
        "\t",
        "\r\n",
        "%",
        "%00",
        "%FF",
        "%E0%A4%A",
        "#",
        "?",
        "..",
        "/",
        "../",
        "\x00",
        "\ufeff",
        "token",
        "password",
        "비밀번호",
        "api_key",
        "://",
        "@",
        "sk-ant-",
        "xoxb-",
        "&",
        "!",
        "{",
        "}",
        "$",
        "a",
        "9",
        "한글",
        "e\u0301",
        "\ud7a3",
        "\U0001f600",
        "\u202e",
        "\x1b[0m",
        "---",
        "...",
        "updated: 2026-10-07\n",
        "&a [*a]\n",
        "!!python/object:os.system x\n",
        "<<: *a\n",
    ]
    for i in range(300):
        text = "".join(rng.choice(pieces) for _ in range(rng.randint(1, 120)))
        folder = rng.choice(["", "status/", "contracts/", "decisions/", "sources/", "x/"])
        write(data_dir, f"wiki/{folder}fuzz{i}.md", text)
    write(data_dir, "wiki/bytes.md", "x")
    (data_dir / "wiki" / "bytes.md").write_bytes(
        b"---\nupdated: \xff\xfe\n---\npassword: hunter2024\n\xc0\xaf"
    )
    write(data_dir, "wiki/deep.md", "---\n" + "[" * 20_000 + "\n---\n")
    write(data_dir, "wiki/deep2.md", "---\nupdated: " + "{a: " * 5_000 + "\n---\n")
    write(data_dir, "wiki/nul.md", FM_UPDATED + "[x](a%00b.md) [y](\x00.md)\n")
    result = lint(data_dir)
    assert all(isinstance(v, LintViolation) for v in result)
    by_file = {v.file: v.message for v in result if v.rule == "L1"}
    assert "wiki/bytes.md" in by_file
    assert "hunter2024" not in "".join(v.message for v in result)


def test_non_utf8_page_is_reported_and_scanned(data_dir: Path) -> None:
    (data_dir / "wiki" / "overview.md").write_bytes(
        b"---\nupdated: 2026-10-07\n---\npassword: hunter2024\n\xff"
    )
    v = lint(data_dir)
    assert sorted((x.rule, x.message) for x in v) == [
        ("L1", "비밀값으로 보이는 대입: password=…"),
        ("L3", "UTF-8 텍스트가 아닙니다"),
    ]


def test_unreadable_page_is_reported_not_raised(data_dir: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root는 권한과 무관하게 읽는다")
    page = data_dir / "wiki" / "overview.md"
    page.chmod(0)
    try:
        v = lint(data_dir)
    finally:
        page.chmod(0o644)
    assert v == [LintViolation("L3", "wiki/overview.md", "파일을 읽을 수 없습니다")]


# ---------------------------------------------------------------- 수정 라운드 1
def messages(root: Path) -> str:
    return format_violations(lint(root))


class TestWikiTreeShape:
    """C1: 위키 아래에는 소문자 `.md` 일반 파일과 폴더만 둘 수 있다."""

    @pytest.mark.parametrize(
        ("rel", "entry", "message"),
        [
            (".hidden/secret.md", ".hidden", "위키 폴더에는 숨김 항목을 둘 수 없습니다"),
            (".x/a.md", ".x", "위키 폴더에는 숨김 항목을 둘 수 없습니다"),
            ("status/.draft.md", "status/.draft.md", "위키 폴더에는 숨김 항목을 둘 수 없습니다"),
            (".md", ".md", "위키 폴더에는 숨김 항목을 둘 수 없습니다"),
            ("notes.txt", "notes.txt", "위키 폴더에는 마크다운 페이지만 둘 수 있습니다"),
            ("data.json", "data.json", "위키 폴더에는 마크다운 페이지만 둘 수 있습니다"),
            ("UPPER.MD", "UPPER.MD", "위키 폴더에는 마크다운 페이지만 둘 수 있습니다"),
            ("x.markdown", "x.markdown", "위키 폴더에는 마크다운 페이지만 둘 수 있습니다"),
            ("noext", "noext", "위키 폴더에는 마크다운 페이지만 둘 수 있습니다"),
            (
                "status/img/a.png",
                "status/img/a.png",
                "위키 폴더에는 마크다운 페이지만 둘 수 있습니다",
            ),
        ],
    )
    def test_unlinted_entries_are_violations(
        self, data_dir: Path, rel: str, entry: str, message: str
    ) -> None:
        write(data_dir, f"wiki/{rel}", "sk-ant-" + "x" * 30 + "\n")
        assert lint(data_dir) == [LintViolation("L5", f"wiki/{entry}", f"{message}: wiki/{entry}")]

    def test_hidden_directory_is_reported_once_and_not_entered(self, data_dir: Path) -> None:
        write(data_dir, "wiki/.git/HEAD", "ref")
        write(data_dir, "wiki/.git/a.md", "x")
        assert [v.file for v in lint(data_dir)] == ["wiki/.git"]

    def test_symlink_named_with_dot_is_reported_once(self, data_dir: Path) -> None:
        os.symlink(data_dir / "raw", data_dir / "wiki" / ".link")
        v = lint(data_dir)
        assert [(x.rule, x.file) for x in v] == [("L5", "wiki/.link")]
        assert "심볼릭 링크" in v[0].message

    def test_fifo_is_not_a_page_and_does_not_hang(self, data_dir: Path) -> None:
        os.mkfifo(data_dir / "wiki" / "pipe.md")
        assert [(v.rule, v.file) for v in lint(data_dir)] == [("L5", "wiki/pipe.md")]

    def test_empty_directories_and_nested_pages_are_fine(self, data_dir: Path) -> None:
        (data_dir / "wiki" / "empty").mkdir()
        write(data_dir, "wiki/deep/er/page.md", FM_UPDATED)
        assert lint(data_dir) == []

    def test_unreadable_directory_is_a_violation(self, data_dir: Path) -> None:
        if os.geteuid() == 0:
            pytest.skip("root는 권한과 무관하게 읽는다")
        locked = data_dir / "wiki" / "locked"
        locked.mkdir()
        locked.chmod(0)
        try:
            v = lint(data_dir)
        finally:
            locked.chmod(0o755)
        assert v == [
            LintViolation("L5", "wiki/locked", "위키 폴더를 읽을 수 없습니다: wiki/locked")
        ]

    def test_wiki_that_is_a_symlink_is_a_usage_error(self, tmp_dir: Path) -> None:
        (tmp_dir / "real").mkdir()
        (tmp_dir / "data").mkdir()
        os.symlink(tmp_dir / "real", tmp_dir / "data" / "wiki")
        with pytest.raises(LintUsageError) as info:
            lint(tmp_dir / "data")
        assert str(info.value) == f"wiki 폴더가 심볼릭 링크입니다: {tmp_dir / 'data' / 'wiki'}"

    def test_unreadable_wiki_is_a_usage_error(self, tmp_dir: Path) -> None:
        if os.geteuid() == 0:
            pytest.skip("root는 권한과 무관하게 읽는다")
        (tmp_dir / "wiki").mkdir()
        (tmp_dir / "wiki").chmod(0)
        try:
            with pytest.raises(LintUsageError) as info:
                lint(tmp_dir)
        finally:
            (tmp_dir / "wiki").chmod(0o755)
        assert str(info.value) == f"wiki 폴더를 읽을 수 없습니다: {tmp_dir / 'wiki'}"

    def test_control_characters_in_names_are_escaped(self, data_dir: Path) -> None:
        write(data_dir, "wiki/a\nb.txt", "x")
        v = lint(data_dir)
        assert "\n" not in v[0].file + v[0].message
        assert "a\\nb.txt" in v[0].file


class TestMessagesDoNotEchoSecrets:
    """C2: frontmatter 값을 메시지에 옮길 때 비밀값·제어 문자·긴 값을 막는다."""

    OMITTED = "(비밀값으로 보이는 값이라 생략)"

    def decision(self, root: Path, front: str) -> None:
        write(root, f"wiki/decisions/{DECISION}", f"---\n{front}updated: 2026-10-07\n---\n")

    def test_status_value_is_never_printed(self, data_dir: Path) -> None:
        token = "sk-ant-" + "q" * 30
        self.decision(data_dir, f"status: {token}\ndecided_at: 2026-10-07\nsources: [{MEETING}]\n")
        out = messages(data_dir)
        assert token not in out
        assert "status 값이 허용 목록 밖입니다 (허용: 확정, 대체됨, 논의 중)" in out
        self.decision(
            data_dir, "status: 보류\ndecided_at: 2026-10-07\n" + f"sources: [{MEETING}]\n"
        )
        assert "보류" not in messages(data_dir)

    def test_evidence_with_credentials_is_omitted(self, data_dir: Path) -> None:
        url = "https://x-access-token:ghp_" + "A1" * 20 + "@github.com/o/r"
        write(
            data_dir,
            "wiki/contracts/relay--worker--kafka.md",
            "---\nproducer: relay\nconsumer: worker\nmedium: kafka\nstatus: 현행\n"
            f'evidence: ["{url}"]\nupdated: 2026-10-07\n---\n',
        )
        v = [x for x in lint(data_dir) if x.rule == "L3"]
        assert [x.message for x in v] == [f"evidence 형식 오류(저장소@커밋:경로): {self.OMITTED}"]
        assert "ghp_" not in messages(data_dir).replace("GitHub 토큰 형식", "")

    @pytest.mark.parametrize("value", ["Xk9mP2qa7zz", "sk-ant-" + "q" * 30, "password: hunter2024"])
    def test_other_values_are_omitted(self, data_dir: Path, value: str) -> None:
        self.decision(
            data_dir,
            f'status: 대체됨\ndecided_at: 2026-10-07\nsources: ["{value}"]\n'
            f'superseded_by: "{value}"\n',
        )
        write(
            data_dir,
            f"wiki/sources/{MEETING}",
            f'---\nraw: "{value}"\ndate: 2026-10-07\naffects: []\nupdated: 2026-10-07\n---\n',
        )
        out = messages(data_dir)
        assert value not in out
        assert out.count(self.OMITTED) == 3

    def test_secret_shaped_evidence_item_that_is_not_a_string(self, data_dir: Path) -> None:
        write(
            data_dir,
            "wiki/contracts/relay--worker--kafka.md",
            "---\nproducer: relay\nconsumer: worker\nmedium: kafka\nstatus: 현행\n"
            "evidence: [123456789, {a: b}, [1]]\nupdated: 2026-10-07\n---\n",
        )
        assert [x.message for x in lint(data_dir)] == [
            "evidence 형식 오류(저장소@커밋:경로): 123456789",
            "evidence 형식 오류(저장소@커밋:경로): 객체",
            "evidence 형식 오류(저장소@커밋:경로): 목록",
        ]

    def test_long_values_are_truncated(self, data_dir: Path) -> None:
        self.decision(
            data_dir,
            "status: 대체됨\ndecided_at: 2026-10-07\n"
            f"sources: [{MEETING}]\nsuperseded_by: {'a' * 300}\n",
        )
        (message,) = [x.message for x in lint(data_dir)]
        assert message == f"superseded_by 대상이 없습니다: {'a' * 100}…"

    def test_control_characters_are_escaped(self, data_dir: Path) -> None:
        self.decision(
            data_dir,
            'status: 대체됨\ndecided_at: 2026-10-07\nsources: ["a\\nb\\x1b[31m\\u202e.md"]\n'
            f"superseded_by: {MEETING}x\n",
        )
        out = messages(data_dir)
        assert "근거 원본이 없습니다: a\\nb\\x1b[31m\\u202e.md" in out
        assert "\x1b" not in out
        assert "‮" not in out
        assert len(out.splitlines()) == 3  # 위반 2건 + 합계

    def test_link_target_control_characters_are_escaped(self, data_dir: Path) -> None:
        append(data_dir, "wiki/index.md", "\n[x](a\x1bb‮.md)\n")
        out = messages(data_dir)
        assert "\x1b" not in out
        assert "‮" not in out
        assert "깨진 링크: a\\x1bb\\u202e.md" in out

    def test_baseline_repo_names_are_sanitized(self, data_dir: Path) -> None:
        name = "a\nb\x1b" + "z" * 200
        baseline = json.loads((data_dir / "baseline.json").read_text(encoding="utf-8"))
        baseline["repos"][name] = {"commit": SHA, "archived": False}
        write(data_dir, "baseline.json", json.dumps(baseline))
        (violation,) = lint(data_dir)
        assert "\n" not in violation.message
        assert "\x1b" not in violation.message
        assert violation.message.startswith("현황 페이지가 없습니다: wiki/status/a\\nb\\x1b")
        assert len(violation.message) < 250


class TestSiblingOutsideTargets:
    """I5: 데이터 폴더 밖에 실제로 있는 `.md`는 L2 링크·superseded_by 모두에서 깨진 것이다."""

    def test_existing_markdown_outside_is_not_a_valid_superseded_by(self, data_dir: Path) -> None:
        (data_dir.parent / "outside.md").write_text("# 밖", encoding="utf-8")
        write(
            data_dir,
            f"wiki/decisions/{DECISION}",
            "---\nstatus: 대체됨\ndecided_at: 2026-10-07\n"
            f"sources: [{MEETING}]\nsuperseded_by: ../../../outside\nupdated: 2026-10-07\n---\n",
        )
        assert rules(lint(data_dir)) == [f"L4 wiki/decisions/{DECISION}"]

    def test_existing_markdown_outside_is_not_a_valid_link(self, data_dir: Path) -> None:
        (data_dir.parent / "outside.md").write_text("# 밖", encoding="utf-8")
        append(data_dir, "wiki/index.md", "\n[x](../../outside.md)\n")
        assert [v.message for v in lint(data_dir)] == ["깨진 링크: ../../outside.md"]


class TestStrictTargets:
    """MINOR: superseded_by는 같은 폴더의 다른 일반 `.md` 파일, raw/sources는 일반 파일."""

    def supersede(self, root: Path, target: str) -> None:
        write(
            root,
            f"wiki/decisions/{DECISION}",
            "---\nstatus: 대체됨\ndecided_at: 2026-10-07\n"
            f"sources: [{MEETING}]\nsuperseded_by: {target}\nupdated: 2026-10-07\n---\n",
        )

    @pytest.mark.parametrize(
        "target",
        [
            "../contracts/relay--worker--kafka",
            "../other/page",
            "missing",
        ],
    )
    def test_target_must_be_a_page_in_the_same_folder(self, data_dir: Path, target: str) -> None:
        write(data_dir, "wiki/other/page.md", FM_UPDATED)
        self.supersede(data_dir, target)
        assert rules(lint(data_dir)) == [f"L4 wiki/decisions/{DECISION}"]

    def test_self_reference_is_a_violation(self, data_dir: Path) -> None:
        self.supersede(data_dir, DECISION)
        assert [v.message for v in lint(data_dir)] == ["superseded_by가 자기 자신을 가리킵니다"]
        self.supersede(data_dir, DECISION.removesuffix(".md"))
        assert rules(lint(data_dir)) == [f"L4 wiki/decisions/{DECISION}"]

    def test_target_directory_is_not_a_page(self, data_dir: Path) -> None:
        (data_dir / "wiki" / "decisions" / "folder.md").mkdir()
        self.supersede(data_dir, "folder")
        assert rules(lint(data_dir)) == [f"L4 wiki/decisions/{DECISION}"]

    def test_target_in_same_folder_as_nfd_passes(self, data_dir: Path) -> None:
        write(
            data_dir,
            "wiki/decisions/2026-10-09-유지.md",
            f"---\nstatus: 확정\ndecided_at: 2026-10-09\nsources: [{MEETING}]\n"
            "updated: 2026-10-09\n---\n",
        )
        self.supersede(data_dir, unicodedata.normalize("NFD", "2026-10-09-유지"))
        assert lint(data_dir) == []

    def test_raw_and_sources_entries_must_be_regular_files(self, data_dir: Path) -> None:
        (data_dir / "raw" / "dir.md").mkdir()
        write(
            data_dir,
            f"wiki/decisions/{DECISION}",
            "---\nstatus: 확정\ndecided_at: 2026-10-07\nsources: [dir.md]\n"
            "updated: 2026-10-07\n---\n",
        )
        write(
            data_dir,
            f"wiki/sources/{MEETING}",
            "---\nraw: dir.md\ndate: 2026-10-07\naffects: []\nupdated: 2026-10-07\n---\n",
        )
        assert sorted(rules(lint(data_dir))) == [
            f"L3 wiki/decisions/{DECISION}",
            f"L3 wiki/sources/{MEETING}",
        ]

    def test_symlink_in_raw_is_not_a_regular_file(self, data_dir: Path) -> None:
        os.symlink(data_dir / "raw" / MEETING, data_dir / "raw" / "alias.md")
        write(
            data_dir,
            f"wiki/sources/{MEETING}",
            "---\nraw: alias.md\ndate: 2026-10-07\naffects: []\nupdated: 2026-10-07\n---\n",
        )
        assert rules(lint(data_dir)) == [f"L3 wiki/sources/{MEETING}"]
        assert stat.S_ISLNK(os.lstat(data_dir / "raw" / "alias.md").st_mode)


class TestPageSizeCaps:
    """I4: 큰 페이지는 위반으로 보고하고 L2·frontmatter는 건너뛰되 L1은 상한까지 돈다."""

    def test_secret_after_70kb_of_filler_is_found(self, data_dir: Path) -> None:
        write(
            data_dir,
            "wiki/overview.md",
            FM_UPDATED + "x " * 35_000 + "\npassword: hunter2024\n",
        )
        assert rules(lint(data_dir)) == ["L1 wiki/overview.md"]

    def test_large_page_is_reported_and_still_scanned_for_secrets(self, data_dir: Path) -> None:
        filler = "가나다 " * 150_000
        write(
            data_dir,
            "wiki/overview.md",
            f"password: hunter2024\n[x](none.md)\n{filler}\n",
        )
        v = lint(data_dir)
        assert [(x.rule, x.message.split(" (")[0]) for x in v] == [
            ("L3", "페이지가 너무 큽니다"),
            ("L1", "비밀값으로 보이는 대입: password=…"),
        ]
        assert not any(x.rule == "L2" for x in v)
        assert not any("frontmatter" in x.message for x in v)
        big = next(x for x in v if x.rule == "L3")
        assert "1,048,576" in big.message

    def test_huge_page_is_read_only_up_to_the_scan_cap(self, data_dir: Path) -> None:
        page = data_dir / "wiki" / "overview.md"
        with page.open("wb") as f:
            f.write(b"token: Xk9mP2qa7\n")
            f.write(b"a" * (9 * 1024 * 1024))
            f.write(b"\npassword: hunter2024\n")  # 상한 밖이라 읽지 않는다
        t0 = time.perf_counter()
        v = lint(data_dir)
        assert time.perf_counter() - t0 < 3
        assert sorted(x.message.split(" (")[0] for x in v) == [
            "비밀값으로 보이는 대입: token=…",
            "페이지가 너무 큽니다",
        ]
        assert "8,388,608" in next(x for x in v if x.rule == "L3").message

    def test_link_resolution_is_cached_within_a_run(
        self, data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from team_wiki_engine.util.fs import resolve_normalized as real

        calls: list[Path] = []

        def counting(path: Path, cache: object = None) -> Path | None:
            calls.append(path)
            return real(path, cache)  # type: ignore[arg-type]

        monkeypatch.setattr("team_wiki_engine.wiki.lint.resolve_normalized", counting)
        append(
            data_dir,
            "wiki/index.md",
            "\n[a](status/worker.md)\n[b](none.md)\n" * 500,
        )
        before = len(calls)
        v = lint(data_dir)
        assert [x.message for x in v] == ["깨진 링크: none.md"]
        index_calls = [c for c in calls[before:] if c.name in {"worker.md", "none.md"}]
        assert len(index_calls) <= 3


@pytest.mark.parametrize("copies", [349_000, 3_000_000], ids=["just under 1MB", "8MB"])
def test_normalization_blowup_pages_finish_quickly(data_dir: Path, copies: int) -> None:
    """`ﷺ` 한 글자는 NFKC에서 18자가 된다. 크기 상한 덕분에 시간이 폭주하지 않는다."""
    (data_dir / "wiki" / "overview.md").write_bytes(("ﷺ" * copies).encode("utf-8"))
    t0 = time.perf_counter()
    lint(data_dir)
    assert time.perf_counter() - t0 < 3


# ---------------------------------------------------------------- 수정 라운드 2
OMITTED = "(비밀값으로 보이는 값이라 생략)"
Q30 = "q" * 30


class TestRound2Masking:
    @pytest.mark.parametrize(
        "target",
        [
            f"sk-ant-{Q30}",
            f"<sk-ant-{Q30}>",
            f"%73k-ant-{Q30}",
            "docs/ghp_" + "A1" * 20 + ".md",
            "token=Xk9mP2qa7zz",
            "//user:Xk9mP2qa7@host/x",
            "Xk9mP2qa7zz",
        ],
    )
    def test_secret_link_targets_are_omitted(self, data_dir: Path, target: str) -> None:
        append(data_dir, "wiki/index.md", f"\n[x]({target})\n")
        v = [x for x in lint(data_dir) if x.rule == "L2"]
        assert [x.message for x in v] == [f"깨진 링크: {OMITTED}"]
        out = messages(data_dir)
        for piece in ("qqqq", "A1A1", "Xk9mP2qa7", "ghp_", "sk-ant-"):
            assert piece not in out.replace("Anthropic API 키 형식", "").replace(
                "GitHub 토큰 형식", ""
            )

    def test_scheme_less_url_credentials_are_flagged_by_l1(self, data_dir: Path) -> None:
        append(data_dir, "wiki/index.md", "\n[x](//user:Xk9mP2qa7@host/x)\n")
        assert "비밀값으로 보이는 문자열: URL 속 자격증명" in [
            x.message for x in lint(data_dir) if x.rule == "L1"
        ]

    def test_plain_link_targets_are_still_shown(self, data_dir: Path) -> None:
        append(data_dir, "wiki/index.md", "\n[x](docs/none-2026.md)\n")
        assert [x.message for x in lint(data_dir)] == ["깨진 링크: docs/none-2026.md"]

    @pytest.mark.parametrize(
        "front",
        [
            f"key: !<sk-ant-{Q30}> v",
            f"key: !sk-ant-{Q30} v",
            f"key: !!sk-ant-{Q30} v",
            "k: !Xk9mP2qa7zz v",
            "password: !Passw0rd-2026",
            "db_password: !Xk9mP2-qa7",
            "k: !Xk9mP2@qa7 v",
            "k: !Xk9mP2qa7$ v",
            "k: !!Zq7Xk9mP2qa7Rt v",
            "k: !<a-X-b> v",
            "k: !<a:X> v",
            "k: !!python/object:os.system x",
        ],
    )
    def test_yaml_tag_text_is_never_echoed(self, data_dir: Path, front: str) -> None:
        write(data_dir, "wiki/overview.md", f"---\n{front}\n---\n")
        (error,) = [x.message for x in lint(data_dir) if x.rule == "L3"]
        assert error == "frontmatter YAML 오류: 태그(!) 구문은 쓸 수 없습니다"
        out = messages(data_dir)
        for piece in ("qqqq", "Xk9mP2", "Passw0rd", "Zq7Xk9", "a-X-b", "a:X", "os.system"):
            assert piece not in out

    def test_other_yaml_errors_keep_their_reason(self, data_dir: Path) -> None:
        write(data_dir, "wiki/overview.md", "---\nupdated: [깨짐\n---\n")
        (error,) = [x.message for x in lint(data_dir) if x.rule == "L3"]
        assert error == "frontmatter YAML 오류: while parsing a flow sequence"

    def test_safe_text_truncates_and_escapes(self) -> None:
        shown = safe_text("가 " * 200)
        assert len(shown) == 101 and shown.endswith("…")
        assert safe_text("a\nb\x1b") == "a\\nb\\x1b"
        assert safe_text("token: Xk9.mP2qa7-yy") == OMITTED

    def test_baseline_error_cannot_forge_lines(self, data_dir: Path) -> None:
        key = "x\n[L1] wiki/forged.md: fake"
        write(
            data_dir,
            "baseline.json",
            json.dumps({"repos": {key: {"commit": "nothex", "archived": False}}}),
        )
        out = messages(data_dir)
        assert "\n[L1] wiki/forged.md" not in out
        assert "x\\n[L1] wiki/forged.md" in out
        assert [x.rule for x in lint(data_dir)] == ["L4"]

    def test_baseline_error_with_multi_word_secret_is_omitted(self, data_dir: Path) -> None:
        key = "password: Xk9.mP2qa7-yy"
        write(
            data_dir,
            "baseline.json",
            json.dumps({"repos": {key: {"commit": "nothex", "archived": False}}}),
        )
        (v,) = lint(data_dir)
        assert v.message == f"baseline.json을 읽을 수 없습니다: {OMITTED}"

    def test_baseline_error_masks_secret_like_keys(self, data_dir: Path) -> None:
        write(
            data_dir,
            "baseline.json",
            json.dumps({"repos": {f"sk-ant-{Q30}": {"commit": "nothex", "archived": False}}}),
        )
        assert "qqqq" not in messages(data_dir)

    @pytest.mark.parametrize(
        ("rel", "shown"),
        [
            (f"sk-ant-{Q30}.md", "<이름 생략>.md"),
            ("password-Xk9mP2qa7.md", "<이름 생략>.md"),
            (f"sk-ant-{Q30}/a.md", "<이름 생략>/a.md"),
            ("ghp_" + "A1" * 20 + "/b.md", "<이름 생략>/b.md"),
        ],
    )
    def test_secret_like_names_are_masked(self, data_dir: Path, rel: str, shown: str) -> None:
        write(data_dir, f"wiki/{rel}", "# 없음\n")
        v = lint(data_dir)
        assert all(x.file == f"wiki/{shown}" for x in v if x.rule == "L3")
        assert [(x.rule, x.message) for x in v if x.rule == "L3"] == [
            ("L3", "frontmatter가 없습니다")
        ]
        out = messages(data_dir)
        assert "qqqq" not in out and "Xk9mP2qa7" not in out and "A1A1" not in out

    def test_secret_like_names_are_masked_in_l5_messages(self, data_dir: Path) -> None:
        write(data_dir, f"wiki/notes-sk-ant-{Q30}.txt", "x")
        write(data_dir, "wiki/.token-Xk9mP2qa7", "x")
        out = messages(data_dir)
        assert "qqqq" not in out and "Xk9mP2qa7" not in out
        assert out.count("<이름 생략>") >= 2 * 2  # 파일 필드와 메시지 둘 다

    def test_ordinary_names_are_not_masked(self, data_dir: Path) -> None:
        write(data_dir, "wiki/relay--worker--2026-10-07.txt", "x")
        v = lint(data_dir)
        assert v[0].file == "wiki/relay--worker--2026-10-07.txt"


class TestLinkBudget:
    def links(self, count: int) -> str:
        return FM_UPDATED + "".join(f"[a{i}](none-{i}.md)\n" for i in range(count))

    def test_pages_up_to_the_budget_are_fully_checked(self, data_dir: Path) -> None:
        write(data_dir, "wiki/overview.md", self.links(2000))
        v = lint(data_dir)
        assert len(v) == 2000
        assert not any("너무 많아" in x.message for x in v)

    def test_over_budget_stops_with_one_violation(self, data_dir: Path) -> None:
        write(data_dir, "wiki/overview.md", self.links(2500))
        v = lint(data_dir)
        assert [x.rule for x in v] == ["L2"] * 2001
        assert v[-1].message == "링크가 너무 많아 일부만 검사했습니다 (최대 2000개)"
        assert sum(1 for x in v if "너무 많아" in x.message) == 1

    def test_non_link_bracket_pairs_count_as_candidates(self, data_dir: Path) -> None:
        write(data_dir, "wiki/overview.md", FM_UPDATED + "a[i](#x)\n" * 2001)
        assert [x.message for x in lint(data_dir)] == [
            "링크가 너무 많아 일부만 검사했습니다 (최대 2000개)"
        ]

    def test_link_semantics_are_unchanged(self, data_dir: Path) -> None:
        append(
            data_dir,
            "wiki/index.md",
            '\n[a [b](none.md)\n[c]( <none2.md> "t" )\n[d](x[e](y.md))\n[](empty.md)\n]()\n[f]()\n',
        )
        assert [x.message for x in lint(data_dir)] == [
            "깨진 링크: none.md",
            "깨진 링크: none2.md",
            "깨진 링크: x[e](y.md",
            "깨진 링크: empty.md",
        ]


MB = 1_000_000
ONE_MB_PAYLOADS = {
    "[](< x250k": "[](<" * 250_000,
    "[x](< x200k": "[x](<" * 200_000,
    "[]( x333k": "[](" * 333_333,
    "[](a x250k": "[](a" * 250_000,
    "]( x500k": "](" * 500_000,
    "[ x1M": "[" * MB,
    "distinct links": "".join(f"[l{i}](a{i}.md)\n" for i in range(70_000))[:MB],
    "valid-looking links": "[a](status/worker.md)\n" * 45_000,
    "a_ x500k": "a_" * 500_000,
    "token_a lines x100k": "token_a: \n" * 100_000,
    "token + _a x500k": "token" + "_a" * 495_000,
    "particle x60k": "비밀번호는 " * 60_000,
    "password ( x100k": "password (" * 100_000,
    "backtick x1M": "`" * MB,
    "fences x300k": "```" * 333_333,
    "url userinfo": ("://" + "u" * 60 + ":" + "p" * 120 + " ") * 5_000,
    "scheme-less url": ("//" + "u" * 60 + ":" + "p" * 120 + " ") * 5_000,
    "// x500k": "//" * 500_000,
    "space x1M": " " * MB,
    "nbsp x480k": "\u00a0" * 480_000,
}


@pytest.mark.parametrize("name", list(ONE_MB_PAYLOADS))
def test_one_megabyte_hostile_pages_finish_well_under_a_second(data_dir: Path, name: str) -> None:
    payload = ONE_MB_PAYLOADS[name]
    assert len(payload.encode("utf-8")) <= 1_048_576
    elapsed = run_payload(data_dir, payload)
    assert elapsed < 1.0, f"{name}: {elapsed:.2f}s"


class TestOversizePages:
    def test_fullwidth_colon_is_a_separator_without_normalization(self, data_dir: Path) -> None:
        filler = "가나다 " * 150_000
        write(data_dir, "wiki/overview.md", f"비밀번호： Xk9mP2qa7\n{filler}\n")
        v = lint(data_dir)
        assert [x.rule for x in v] == ["L3", "L1"]
        assert v[1].message == "비밀값으로 보이는 대입: 비밀번호=…"

    def test_oversize_page_skips_links_and_frontmatter(self, data_dir: Path) -> None:
        write(data_dir, "wiki/overview.md", "[x](none.md)\n" + "가나다 " * 150_000)
        assert [x.rule for x in lint(data_dir)] == ["L3"]


# ---------------------------------------------------------------- 수정 라운드 3
class TestRound3:
    def test_link_label_search_does_not_reach_into_the_previous_link(self, data_dir: Path) -> None:
        append(data_dir, "wiki/index.md", '\n[x](y "[")](z)\n')
        assert [v.message for v in lint(data_dir)] == ["깨진 링크: y"]

    @pytest.mark.parametrize(
        "target",
        [
            "dir/Xk9mP2qa7zz/a.md",
            "./Zq7Xk9mP2qa7Rt.md",
            "../db/password-Xk9mP2qa7.md",
            "Xk9mP2qa",  # 정확히 8자
            "a/b/token_Xk9mP2qa7zz/c",
        ],
    )
    def test_link_target_with_a_secret_like_segment_is_omitted(
        self, data_dir: Path, target: str
    ) -> None:
        append(data_dir, "wiki/index.md", f"\n[x]({target})\n")
        assert [v.message for v in lint(data_dir)] == [f"깨진 링크: {OMITTED}"]

    def test_decoded_segments_are_checked_too(self, data_dir: Path) -> None:
        append(data_dir, "wiki/index.md", "\n[x](dir/%58k9mP2qa7zz/a.md)\n")
        assert [v.message for v in lint(data_dir)] == [f"깨진 링크: {OMITTED}"]

    def test_names_split_on_whitespace_too(self, data_dir: Path) -> None:
        write(data_dir, "wiki/draft Xk9mP2qa7zz.md", "# 없음\n")
        v = lint(data_dir)
        assert [x.file for x in v] == ["wiki/<이름 생략>.md"]

    def test_status_page_message_masks_secret_like_repo_names(self, data_dir: Path) -> None:
        baseline = json.loads((data_dir / "baseline.json").read_text(encoding="utf-8"))
        baseline["repos"][f"sk-ant-{Q30}"] = {"commit": SHA, "archived": False}
        baseline["repos"]["Xk9mP2qa7zz"] = {"commit": SHA, "archived": False}
        write(data_dir, "baseline.json", json.dumps(baseline))
        out = messages(data_dir)
        assert "qqqq" not in out and "Xk9mP2qa7zz" not in out
        assert out.count("wiki/status/<생략>.md") == 2

    def test_huge_baseline_error_is_bounded_and_fast(self, data_dir: Path) -> None:
        repos = {f"r{i}": {"commit": "bad", "archived": False} for i in range(20_000)}
        write(data_dir, "baseline.json", json.dumps({"repos": repos}))
        t0 = time.perf_counter()
        (v,) = lint(data_dir)
        assert time.perf_counter() - t0 < 3
        assert len(v.message) < 300

    def test_scheme_less_url_guards(self, data_dir: Path) -> None:
        # 문서 맨 앞의 `://`: 앞 글자를 되짚으면 안 된다(끝 글자가 영문이어도 URL이 아니다)
        write(data_dir, "wiki/overview.md", "://user:Xk9mP2qa7@host x")
        assert [v.rule for v in lint(data_dir)] == ["L3"]

    def test_deep_link_targets_are_broken_without_resolving(self, data_dir: Path) -> None:
        deep = "/".join(["d"] * 70)
        write(data_dir, f"wiki/{deep}/x.md", FM_UPDATED)
        append(data_dir, "wiki/index.md", f"\n[x]({deep}/x.md)\n[y](d/d/d/../../../index.md)\n")
        (message,) = [v.message for v in lint(data_dir)]
        assert message == f"깨진 링크: {(deep + '/x.md')[:100]}…"

    def test_many_deep_links_are_fast(self, data_dir: Path) -> None:
        line = "[a](" + "a/" * 480 + "x.md)\n"
        write(data_dir, "wiki/overview.md", FM_UPDATED + line * 1500)
        t0 = time.perf_counter()
        v = lint(data_dir)
        assert time.perf_counter() - t0 < 1.0
        assert len(v) == 1

    def test_safe_text_only_scans_a_bounded_prefix(self) -> None:
        t0 = time.perf_counter()
        shown = safe_text("word-x " * 300_000)
        assert time.perf_counter() - t0 < 0.1
        assert len(shown) == 101

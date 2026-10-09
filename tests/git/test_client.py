import asyncio
import base64
import contextlib
import json
import os
import signal
import subprocess
import time
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from team_wiki_engine.git import client as git_client
from team_wiki_engine.git.client import (
    DEFAULT_ALLOWED_PROTOCOLS,
    ENV_PASSTHROUGH,
    GIT_MIN_VERSION,
    GitClient,
    GitError,
    GitOutputTooLarge,
    GitResult,
    auth_header_for,
    check_git_version,
    git_env,
)
from tests.helpers.fake_git import fake_git_calls, write_fake_git
from tests.helpers.hang_guard import hang_guard, open_fd_count
from tests.helpers.offline import OfflineGuard

FIXED_CONFIG = [
    ("core.hooksPath", "/dev/null"),
    ("core.fsmonitor", "false"),
    ("core.excludesFile", "/dev/null"),
    ("core.attributesFile", "/dev/null"),
    ("credential.helper", ""),
    ("core.askPass", ""),
]
TOKEN = "ghp_secret_token_value_1234567890"
HEADER = auth_header_for(TOKEN)
B64 = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()


def _kill_leftovers(root: Path) -> None:
    """시험이 실패해도 `hang`/`escape`가 남긴 프로세스(PID 파일)를 죽인다.

    PID 재사용으로 남의 프로세스를 죽이지 않도록 명령줄이 `sleep 60`이거나 가짜 git 구현인
    프로세스만 죽인다.
    """
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix == ".tmp":
            continue
        text = path.read_text(errors="replace").strip() if path.stat().st_size < 32 else ""
        if not text.isascii() or not text.isdigit():
            continue
        pid = int(text)
        probe = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, check=False
        )
        if "sleep 60" in probe.stdout or "fake_git_impl" in probe.stdout:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)


@pytest.fixture(autouse=True)
def _reap_leftover_processes(tmp_dir: Path) -> Iterator[None]:
    yield
    _kill_leftovers(tmp_dir)


@pytest.fixture
def fake(tmp_dir: Path) -> Path:
    return write_fake_git(tmp_dir / "bin")


def make(fake: Path, cwd: Path, **kwargs: Any) -> GitClient:
    return GitClient(cwd, git_bin=str(fake), **kwargs)


def config_pairs(env: Mapping[str, str]) -> list[tuple[str, str]]:
    count = int(env["GIT_CONFIG_COUNT"])
    return [(env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"]) for i in range(count)]


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def read_pid(path: Path, within: float = 5.0) -> int:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if path.exists():
            return int(path.read_text())
        await asyncio.sleep(0.02)
    raise AssertionError(f"PID 파일이 생기지 않았습니다: {path.name}")


async def wait_gone(pid: int, within: float = 1.0) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if not alive(pid):
            return True
        await asyncio.sleep(0.02)
    return not alive(pid)


async def assert_processes_gone(pid_file: Path) -> None:
    grandchild = await read_pid(pid_file)
    child = await read_pid(pid_file.with_name(pid_file.name + ".self"))
    assert await wait_gone(grandchild), "손자 프로세스가 남았습니다"
    assert await wait_gone(child), "git 프로세스가 남았습니다(좀비 포함)"


# ------------------------------------------------------------------ 기본 동작


async def test_stdout_text_property(tmp_dir: Path) -> None:
    git = GitClient(tmp_dir, config={"user.name": "tester", "user.email": "t@example.com"})
    await git.run(["init", "-q", "-b", "main"])
    await git.run(["commit", "-q", "--allow-empty", "-m", "한글 제목"])
    result = await git.run(["log", "--format=%s"])
    assert isinstance(result, GitResult)
    assert result.code == 0
    assert result.text.strip() == "한글 제목"
    assert result.stdout == "한글 제목\n".encode()
    assert GitResult(0, b"\xff\xfeab", "").text == "��ab"


async def test_allowed_exit_code_is_not_a_failure(tmp_dir: Path) -> None:
    git = GitClient(tmp_dir)
    await git.run(["init", "-q", "-b", "main"])
    result = await git.run(
        ["rev-parse", "--verify", "--quiet", f"{'b' * 40}^{{commit}}"], allow_codes=[1]
    )
    assert result.code == 1


async def test_failure_message_has_no_auth_header(tmp_dir: Path) -> None:
    git = GitClient(tmp_dir, auth_header=HEADER)
    await git.run(["init", "-q", "-b", "main"])
    with pytest.raises(GitError) as info:
        await git.run(["rev-parse", "--verify", "nope^{commit}"])
    err = info.value
    assert err.kind == "exit"
    assert err.code is not None and err.code != 0
    assert str(err).startswith("git rev-parse 실패")
    for secret in ("ghp_secret", B64, "AUTHORIZATION", HEADER):
        assert secret not in str(err)
        assert secret not in err.stderr


async def test_real_git_follows_env_config(tmp_dir: Path) -> None:
    git = GitClient(tmp_dir, config={"user.name": "envuser", "user.email": "env@example.com"})
    await git.run(["init", "-q", "-b", "main"])
    await git.run(["commit", "-q", "--allow-empty", "-m", "x"])
    assert (await git.run(["log", "-1", "--format=%an <%ae>"])).text.strip() == (
        "envuser <env@example.com>"
    )


async def test_user_global_config_is_ignored(tmp_dir: Path) -> None:
    # HOME의 .gitconfig는 GIT_CONFIG_GLOBAL=/dev/null 때문에 읽히지 않는다.
    git = GitClient(tmp_dir, base_env={"PATH": os.environ["PATH"], "HOME": str(tmp_dir)})
    await git.run(["init", "-q", "-b", "main"])
    (tmp_dir / ".gitconfig").write_text("[user]\n\tname = leaked\n\temail = l@example.com\n")
    assert (await git.run(["config", "--get-all", "user.name"], allow_codes=[1])).code == 1


# ------------------------------------------------------------------ 환경


def test_defaults() -> None:
    assert GIT_MIN_VERSION == (2, 32)
    assert ENV_PASSTHROUGH == (
        "PATH",
        "HOME",
        "TMPDIR",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
    )
    # 테스트에서는 안전장치가 모듈 값을 "file"로 바꿔 두므로 소스의 기본값을 본다.
    source = Path(git_client.__file__).read_text(encoding="utf-8")
    assert 'DEFAULT_ALLOWED_PROTOCOLS = "https:file"' in source
    assert DEFAULT_ALLOWED_PROTOCOLS == "https:file"  # from-import는 패치 전 값을 쥔다


def test_offline_guard_restricts_git_transport(offline_guard: OfflineGuard) -> None:
    assert git_client.DEFAULT_ALLOWED_PROTOCOLS == "file"
    env = git_env({}, config={}, allowed_protocols=git_client.DEFAULT_ALLOWED_PROTOCOLS)
    assert env["GIT_ALLOW_PROTOCOL"] == "file"


def test_git_env_copies_only_the_allowlist_and_numbers_config() -> None:
    base = {k: f"v-{k}" for k in ENV_PASSTHROUGH}
    base.update({"ANTHROPIC_API_KEY": "sk-x", "GIT_DIR": "/evil", "LC_ALL": "ko_KR.UTF-8"})
    env = git_env(base, config={"a.b": "1", "c.d": "two words"}, allowed_protocols="https:file")
    for key in ENV_PASSTHROUGH:
        assert env[key] == f"v-{key}"
    assert "ANTHROPIC_API_KEY" not in env
    assert "GIT_DIR" not in env
    assert env["LC_ALL"] == "C"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert env["GIT_ALLOW_PROTOCOL"] == "https:file"
    assert env["GIT_OPTIONAL_LOCKS"] == "0"  # 읽기 전용 명령이 index.lock을 쥐지 않는다
    assert config_pairs(env) == [*FIXED_CONFIG, ("a.b", "1"), ("c.d", "two words")]
    assert f"GIT_CONFIG_KEY_{len(FIXED_CONFIG) + 2}" not in env


def test_git_env_without_config_has_only_the_fixed_entries() -> None:
    env = git_env({}, config={}, allowed_protocols="file")
    assert config_pairs(env) == FIXED_CONFIG


@pytest.mark.parametrize(
    "config", [{"": "v"}, {"a\0b": "v"}, {"k": "v\0"}], ids=["empty-key", "nul-key", "nul-value"]
)
def test_git_env_rejects_unrepresentable_config(config: Mapping[str, str]) -> None:
    with pytest.raises(ValueError):
        git_env({}, config=config, allowed_protocols="file")


async def test_child_environment_is_the_allowlist(
    fake: Path, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key, value in {
        "ANTHROPIC_API_KEY": "sk-ant-parent-secret",
        "DISCORD_TOKEN": "discord-parent-secret",
        "GITHUB_TOKEN": "ghp_parent_secret",
        "GIT_DIR": "/evil",
        "GIT_CONFIG_PARAMETERS": "'user.name=evil'",
        "LANG": "ko_KR.UTF-8",
    }.items():
        monkeypatch.setenv(key, value)
    # base_env를 안 주면 호출 시점의 os.environ을 쓴다.
    result = await make(fake, tmp_dir).run(["echo-env"])
    env = json.loads(result.text)
    for gone in ("ANTHROPIC_API_KEY", "DISCORD_TOKEN", "GITHUB_TOKEN", "GIT_DIR"):
        assert gone not in env
    assert "GIT_CONFIG_PARAMETERS" not in env
    assert env.get("LANG") != "ko_KR.UTF-8"
    assert "parent_secret" not in result.text
    assert env["LC_ALL"] == "C"
    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_ALLOW_PROTOCOL"] == "file"  # 안전장치가 모듈 기본값을 바꿔 두었다
    assert env["GIT_OPTIONAL_LOCKS"] == "0"
    assert env["PATH"] == os.environ["PATH"]


async def test_default_protocols_are_read_at_call_time(
    fake: Path, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git = make(fake, tmp_dir)
    monkeypatch.setattr(git_client, "DEFAULT_ALLOWED_PROTOCOLS", "https:file")
    assert json.loads((await git.run(["echo-env"])).text)["GIT_ALLOW_PROTOCOL"] == "https:file"
    explicit = make(fake, tmp_dir, allowed_protocols="file")
    assert json.loads((await explicit.run(["echo-env"])).text)["GIT_ALLOW_PROTOCOL"] == "file"


async def test_base_env_is_filtered_too(fake: Path, tmp_dir: Path) -> None:
    git = make(
        fake, tmp_dir, base_env={"PATH": os.environ["PATH"], "SECRET": "s3", "HTTPS_PROXY": "p"}
    )
    env = json.loads((await git.run(["echo-env"])).text)
    assert "SECRET" not in env
    assert env["HTTPS_PROXY"] == "p"


# ------------------------------------------------------------------ 인증


def test_auth_header_for() -> None:
    assert HEADER == f"AUTHORIZATION: basic {B64}"
    assert base64.b64decode(B64) == f"x-access-token:{TOKEN}".encode()


async def test_auth_goes_through_env_not_argv(fake: Path, tmp_dir: Path) -> None:
    git = make(fake, tmp_dir, auth_header=HEADER)
    assert git.has_auth is True
    assert make(fake, tmp_dir).has_auth is False
    argv_out = (await git.run(["echo-argv", "fetch", "origin"])).text
    assert json.loads(argv_out) == ["fetch", "origin"]
    for secret in ("AUTHORIZATION", TOKEN, B64):
        assert secret not in argv_out
    # 호출 기록(= 가짜 git이 받은 argv 전체)에도 없다.
    recorded = json.dumps(fake_git_calls(fake))
    for secret in ("AUTHORIZATION", TOKEN, B64):
        assert secret not in recorded
    env = json.loads((await git.run(["echo-env"])).text)
    assert config_pairs(env) == [*FIXED_CONFIG, ("http.https://github.com/.extraheader", HEADER)]


async def test_auth_header_comes_after_plain_config(fake: Path, tmp_dir: Path) -> None:
    git = make(fake, tmp_dir, auth_header=HEADER, config={"user.name": "n"})
    env = json.loads((await git.run(["echo-env"])).text)
    assert [k for k, _ in config_pairs(env)][len(FIXED_CONFIG) :] == [
        "user.name",
        "http.https://github.com/.extraheader",
    ]


def test_repr_has_no_secrets(tmp_dir: Path) -> None:
    git = GitClient(tmp_dir, auth_header=HEADER, config={"k.v": "configvalue"})
    text = repr(git)
    assert text == f"GitClient(cwd={tmp_dir}, auth=있음)"
    assert str(git) == text
    assert f"{git!r:>200}".strip() == text
    assert repr(GitClient(tmp_dir)) == f"GitClient(cwd={tmp_dir}, auth=없음)"
    for secret in ("AUTHORIZATION", TOKEN, B64, "configvalue"):
        assert secret not in text


async def test_at_keeps_auth_config_and_protocols(fake: Path, tmp_dir: Path) -> None:
    other = tmp_dir / "other"
    other.mkdir()
    git = make(
        fake,
        tmp_dir,
        auth_header=HEADER,
        config={"user.name": "n"},
        allowed_protocols="file",
        timeout_s=7.0,
        max_output_bytes=1234,
    )
    moved = git.at(other)
    assert moved is not git
    assert moved.cwd == other
    assert git.cwd == tmp_dir
    assert moved.has_auth
    env = json.loads((await moved.run(["echo-env"], max_output_bytes=100_000)).text)
    assert [v for _, v in config_pairs(env)][len(FIXED_CONFIG) :] == ["n", HEADER]
    assert env["GIT_ALLOW_PROTOCOL"] == "file"
    with pytest.raises(GitOutputTooLarge):  # max_output_bytes도 이어받는다
        await moved.run(["flood", "5000"])


# ------------------------------------------------------------------ 오류 메시지


async def test_error_message_hides_tokens_headers_and_argv(fake: Path, tmp_dir: Path) -> None:
    stderr_text = (
        "fatal: could not read from https://x-access-token:ghp_AAAA@github.com/o/r\n"
        f"{HEADER}\n"
        f"authorization: basic {B64}\n"
    )
    git = make(fake, tmp_dir, auth_header=HEADER)
    with pytest.raises(GitError) as info:
        await git.run(["fail", stderr_text, "MARKER-argv-unique-12345"])
    err = info.value
    assert err.kind == "exit"
    assert err.code == 1
    message = str(err)
    assert message.startswith("git fail 실패: fatal: could not read from")
    assert len(message) <= 200
    for text in (message, err.stderr):
        for secret in ("ghp_AAAA", TOKEN, B64, "AUTHORIZATION", "authorization", "MARKER-"):
            assert secret not in text
    assert "://***@github.com" in message
    assert "x-access-token" not in message


@pytest.mark.parametrize(
    "stderr_text",
    [
        f"{HEADER}\nfatal: later line\n",
        f"\n\n  \nAuthorization: Bearer abc.def.ghi\n{HEADER}\n",
        f"fatal: remote said {B64} and {TOKEN}\n",
        f"fatal: Basic {B64}\n",
    ],
    ids=["header-first-line", "bearer", "raw-b64-and-token", "basic-scheme"],
)
async def test_error_message_variants(fake: Path, tmp_dir: Path, stderr_text: str) -> None:
    git = make(fake, tmp_dir, auth_header=HEADER)
    with pytest.raises(GitError) as info:
        await git.run(["fail", stderr_text])
    for text in (str(info.value), info.value.stderr):
        for secret in ("abc.def.ghi", TOKEN, B64, "AUTHORIZATION", "authorization"):
            assert secret not in text


async def test_header_unknown_to_the_client_is_still_removed(fake: Path, tmp_dir: Path) -> None:
    with pytest.raises(GitError) as info:
        await make(fake, tmp_dir).run(["fail", f"fatal: bad {HEADER} trailing"])
    assert "AUTHORIZATION" not in str(info.value)
    assert B64 not in str(info.value) + info.value.stderr


async def test_message_is_one_cleaned_line_of_at_most_200_chars(fake: Path, tmp_dir: Path) -> None:
    text = "\n  \nfatal: " + "가" * 500 + "‮\x1b[31mred\n두번째 줄\n"
    with pytest.raises(GitError) as info:
        await make(fake, tmp_dir).run(["fail", text])
    message = str(info.value)
    assert len(message) <= 200
    assert "\n" not in message
    assert "‮" not in message
    assert "\x1b" not in message
    assert "두번째" not in message


async def test_empty_stderr_still_gives_a_message(fake: Path, tmp_dir: Path) -> None:
    with pytest.raises(GitError) as info:
        await make(fake, tmp_dir).run(["fail", ""])
    assert str(info.value).startswith("git fail 실패")
    assert info.value.stderr == ""


async def test_subcommand_skips_option_values(tmp_dir: Path) -> None:
    git = GitClient(tmp_dir)
    await git.run(["init", "-q", "-b", "main"])
    with pytest.raises(GitError) as info:
        await git.run(
            [
                "-c",
                "user.name=MARKER-cfg",
                "-C",
                str(tmp_dir),
                "--no-pager",
                "rev-parse",
                "--verify",
                "nope^{commit}",
            ]
        )
    assert str(info.value).startswith("git rev-parse 실패")
    assert "MARKER" not in str(info.value)


async def test_odd_subcommand_is_not_echoed(fake: Path, tmp_dir: Path) -> None:
    with pytest.raises(GitError) as info:
        await make(fake, tmp_dir).run(["https://MARKER-url@example.invalid/x"])
    assert "MARKER" not in str(info.value)
    assert "example.invalid" not in str(info.value)


async def test_stderr_is_capped_at_4kb_and_drained(fake: Path, tmp_dir: Path) -> None:
    async with asyncio.timeout(10):
        ok = await make(fake, tmp_dir).run(["flood-err", "3000000"])
        with pytest.raises(GitError) as info:
            await make(fake, tmp_dir).run(["fail", "e" * 20_000])
    assert ok.code == 0
    assert len(ok.stderr.encode()) <= 4096
    assert len(info.value.stderr.encode()) <= 4096


async def test_nonzero_exit_code_in_allow_codes_returns_result(fake: Path, tmp_dir: Path) -> None:
    result = await make(fake, tmp_dir).run(["fail", "x"], allow_codes={1})
    assert (result.code, result.stderr) == (1, "x")


async def test_exit_code_not_allowed_even_if_other_allowed(fake: Path, tmp_dir: Path) -> None:
    with pytest.raises(GitError):
        await make(fake, tmp_dir).run(["fail", "x"], allow_codes=[2])


# ------------------------------------------------------------------ 시간 초과·폭주·취소


@pytest.mark.parametrize("round_", [1, 2, 3])
async def test_timeout_kills_the_whole_group(fake: Path, tmp_dir: Path, round_: int) -> None:
    pid_file = tmp_dir / f"pid-{round_}"
    async with asyncio.timeout(10):
        started = time.monotonic()
        with pytest.raises(GitError) as info:
            await make(fake, tmp_dir).run(["hang", str(pid_file)], timeout_s=0.5)
        assert time.monotonic() - started < 3
        assert info.value.kind == "timeout"
        assert str(info.value) == "git hang 시간 초과"
        assert info.value.code is None
        await assert_processes_gone(pid_file)


async def test_per_call_timeout_overrides_default(fake: Path, tmp_dir: Path) -> None:
    pid_file = tmp_dir / "pid"
    git = make(fake, tmp_dir, timeout_s=60.0)
    async with asyncio.timeout(10):
        with pytest.raises(GitError) as info:
            await git.run(["hang", str(pid_file)], timeout_s=0.5)
        assert info.value.kind == "timeout"
        await assert_processes_gone(pid_file)


async def test_default_timeout_applies(fake: Path, tmp_dir: Path) -> None:
    pid_file = tmp_dir / "pid"
    async with asyncio.timeout(10):
        with pytest.raises(GitError) as info:
            await make(fake, tmp_dir, timeout_s=0.5).run(["hang", str(pid_file)])
        assert info.value.kind == "timeout"
        await assert_processes_gone(pid_file)


@pytest.mark.parametrize("round_", [1, 2, 3])
async def test_outer_cancel_kills_the_group(fake: Path, tmp_dir: Path, round_: int) -> None:
    pid_file = tmp_dir / f"pid-{round_}"
    async with asyncio.timeout(10):
        task = asyncio.ensure_future(make(fake, tmp_dir).run(["hang", str(pid_file)]))
        await read_pid(pid_file)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled()
        await assert_processes_gone(pid_file)


async def test_cancel_during_cleanup_still_ends_cancelled_and_clean(
    fake: Path, tmp_dir: Path
) -> None:
    pid_file = tmp_dir / "pid"
    async with asyncio.timeout(10):
        task = asyncio.ensure_future(make(fake, tmp_dir).run(["hang", str(pid_file)]))
        await read_pid(pid_file)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()  # 정리 도중 한 번 더
        with pytest.raises(asyncio.CancelledError):
            await task
        await assert_processes_gone(pid_file)


@pytest.mark.parametrize("round_", [1, 2, 3])
async def test_output_overflow_kills_the_group(fake: Path, tmp_dir: Path, round_: int) -> None:
    pid_file = tmp_dir / f"pid-{round_}"
    git = make(fake, tmp_dir)
    async with asyncio.timeout(10):
        started = time.monotonic()
        with pytest.raises(GitOutputTooLarge) as info:
            await git.run(
                ["flood", "10000000", str(pid_file)], max_output_bytes=1_000_000, timeout_s=30
            )
        # 정리가 5초 안전망(파이프 비우기 실패)에 기대지 않고 바로 끝나야 한다.
        assert time.monotonic() - started < 3
        assert isinstance(info.value, GitError)
        assert info.value.kind == "too_large"
        assert info.value.code is None
        assert str(info.value).startswith("git flood")
        await assert_processes_gone(pid_file)


async def test_output_exactly_at_the_limit_is_fine(fake: Path, tmp_dir: Path) -> None:
    async with asyncio.timeout(10):
        result = await make(fake, tmp_dir).run(["flood", "200000"], max_output_bytes=200_000)
        assert len(result.stdout) == 200_000
        with pytest.raises(GitOutputTooLarge):
            await make(fake, tmp_dir).run(["flood", "200001"], max_output_bytes=200_000)


async def test_large_output_within_limit_does_not_deadlock(fake: Path, tmp_dir: Path) -> None:
    async with asyncio.timeout(10):
        result = await make(fake, tmp_dir).run(["flood", "5000000"])
    assert len(result.stdout) == 5_000_000


def test_loop_cancellation_mid_read_leaves_nothing_behind(fake: Path, tmp_dir: Path) -> None:
    pid_file = tmp_dir / "pid"

    async def main() -> None:
        # asyncio.run이 끝나며 남은 태스크(=git 실행)를 취소한다.
        asyncio.ensure_future(make(fake, tmp_dir).run(["hang", str(pid_file)]))
        await read_pid(pid_file)

    async def check() -> None:
        await assert_processes_gone(pid_file)

    with hang_guard(20):
        asyncio.run(main())
        asyncio.run(check())


def test_closing_the_coroutine_kills_the_group(fake: Path, tmp_dir: Path) -> None:
    pid_file = tmp_dir / "pid"
    loop = asyncio.new_event_loop()
    try:
        coro = make(fake, tmp_dir).run(["hang", str(pid_file)])
        task = loop.create_task(coro)

        async def started() -> None:
            await read_pid(pid_file)

        with hang_guard(20):
            loop.run_until_complete(started())
            coro.close()  # GeneratorExit: 기다리지 않고 그룹만 죽인다(RuntimeError 없음)
            loop.run_until_complete(assert_processes_gone(pid_file))
        task.cancel()
        with pytest.raises((asyncio.CancelledError, RuntimeError)):
            loop.run_until_complete(task)
    finally:
        loop.close()


async def test_no_fd_leak_across_failure_paths(fake: Path, tmp_dir: Path) -> None:
    async def failure_paths(tag: str) -> None:
        git = make(fake, tmp_dir)
        with pytest.raises(GitError):
            await git.run(["hang", str(tmp_dir / f"t-{tag}")], timeout_s=0.3)
        with pytest.raises(GitOutputTooLarge):
            await git.run(["flood", "3000000"], max_output_bytes=100_000)
        with pytest.raises(GitError):
            await git.run(["fail", "x"])
        task = asyncio.ensure_future(git.run(["hang", str(tmp_dir / f"c-{tag}")]))
        await read_pid(tmp_dir / f"c-{tag}")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await git.run(["echo-argv"])

    async with asyncio.timeout(30):
        await failure_paths("warm")
        await asyncio.sleep(0.1)
        before = open_fd_count()
        for i in range(3):
            await failure_paths(str(i))
        await asyncio.sleep(0.1)
        assert open_fd_count() <= before


# ------------------------------------------------------------------ 실행 실패


async def test_missing_executable_is_spawn_error(tmp_dir: Path) -> None:
    git = GitClient(tmp_dir, git_bin="/nonexistent/dir/git-MARKER-bin")
    with pytest.raises(GitError) as info:
        await git.run(["status"])
    assert info.value.kind == "spawn"
    assert info.value.code is None
    assert "MARKER" not in str(info.value)


async def test_missing_cwd_is_spawn_error(fake: Path, tmp_dir: Path) -> None:
    with pytest.raises(GitError) as info:
        await make(fake, tmp_dir / "no-such-dir").run(["echo-argv"])
    assert info.value.kind == "spawn"


async def test_nul_in_argument_is_spawn_error(fake: Path, tmp_dir: Path) -> None:
    with pytest.raises(GitError) as info:
        await make(fake, tmp_dir).run(["echo-argv", "a\0b"])
    assert info.value.kind == "spawn"


async def test_args_must_be_a_list_of_strings(fake: Path, tmp_dir: Path) -> None:
    with pytest.raises(TypeError):
        await make(fake, tmp_dir).run("status")


@pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf")])
def test_invalid_timeout_is_rejected(tmp_dir: Path, bad: float) -> None:
    with pytest.raises(ValueError):
        GitClient(tmp_dir, timeout_s=bad)


def test_negative_output_limit_is_rejected(tmp_dir: Path) -> None:
    with pytest.raises(ValueError):
        GitClient(tmp_dir, max_output_bytes=-1)


async def test_https_is_refused_before_connecting(
    tmp_dir: Path, offline_guard: OfflineGuard
) -> None:
    git = GitClient(tmp_dir, allowed_protocols="file")
    await git.run(["init", "-q", "-b", "main"])
    started = time.monotonic()
    with pytest.raises(GitError) as info:
        await git.run(["ls-remote", "https://example.invalid/x.git"])
    assert time.monotonic() - started < 5
    assert info.value.kind == "exit"
    assert "not allowed" in str(info.value)
    assert offline_guard.attempts == []


async def test_module_default_blocks_https_in_tests(tmp_dir: Path) -> None:
    git = GitClient(tmp_dir)
    await git.run(["init", "-q", "-b", "main"])
    with pytest.raises(GitError) as info:
        await git.run(["ls-remote", "https://example.invalid/x.git"])
    assert "not allowed" in str(info.value)


async def test_local_file_remote_works_with_file_protocol(tmp_dir: Path) -> None:
    git = GitClient(tmp_dir, config={"user.name": "n", "user.email": "n@example.com"})
    bare = tmp_dir / "bare.git"
    bare.mkdir()
    await git.at(bare).run(["init", "-q", "--bare", "-b", "main"])
    work = tmp_dir / "work"
    work.mkdir()
    wg = git.at(work)
    await wg.run(["init", "-q", "-b", "main"])
    await wg.run(["commit", "-q", "--allow-empty", "-m", "init"])
    await wg.run(["push", "-q", str(bare), "main"])
    out = await git.run(["ls-remote", str(bare)])
    assert "refs/heads/main" in out.text


# ------------------------------------------------------------------ git 버전


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("git version 2.43.0", (2, 43, 0)),
        ("git version 2.39.5 (Apple Git-154)", (2, 39, 5)),
        ("git version 2.32.0", (2, 32, 0)),
        ("git version 3.0.1", (3, 0, 1)),
        ("git version 2.45.2.windows.1", (2, 45, 2)),
    ],
)
async def test_check_git_version_accepts(
    tmp_dir: Path, text: str, expected: tuple[int, int, int]
) -> None:
    fake_dir = tmp_dir / "bin"
    write_fake_git(fake_dir, version=text)
    # 실행 파일은 base_env의 PATH로 찾는다.
    assert await check_git_version(base_env={"PATH": str(fake_dir)}) == expected
    assert fake_git_calls(fake_dir / "git") == [["--version"]]


@pytest.mark.parametrize(
    "text",
    ["git version 2.31.0", "git version 1.9.9", "garbage", "", "git version 2.45", "version two"],
)
async def test_check_git_version_rejects(tmp_dir: Path, text: str) -> None:
    fake_dir = tmp_dir / "bin"
    write_fake_git(fake_dir, version=text)
    with pytest.raises(GitError) as info:
        await check_git_version(base_env={"PATH": str(fake_dir)})
    assert info.value.kind == "exit"
    assert "git 2.32 이상이 필요합니다" in str(info.value)


async def test_check_git_version_with_explicit_git_bin(fake: Path) -> None:
    assert await check_git_version(str(fake)) == (2, 54, 0)


async def test_check_git_version_missing_binary_is_spawn_error(tmp_dir: Path) -> None:
    with pytest.raises(GitError) as info:
        await check_git_version(base_env={"PATH": str(tmp_dir / "empty")})
    assert info.value.kind == "spawn"


async def test_check_git_version_real_git() -> None:
    version = await check_git_version()
    assert version[:2] >= GIT_MIN_VERSION


# ------------------------------------------------------------------ 가짜 git 도우미 · 소스 규칙


async def test_fake_git_calls_in_order_and_empty_when_unused(tmp_dir: Path) -> None:
    script = write_fake_git(tmp_dir / "fb")
    assert fake_git_calls(script) == []
    git = GitClient(tmp_dir, git_bin=str(script))
    await git.run(["echo-argv", "a", "한글 b"])
    with pytest.raises(GitError):
        await git.run(["fail", "x"])
    with pytest.raises(GitError) as info:
        await git.run(["bogus"])
    assert "unknown command" in info.value.stderr
    assert fake_git_calls(script) == [["echo-argv", "a", "한글 b"], ["fail", "x"], ["bogus"]]
    assert script.stat().st_mode & 0o111


def test_source_has_no_shell_or_blocking_subprocess() -> None:
    source = Path(git_client.__file__).read_text(encoding="utf-8")
    banned_words = (
        "shell=True",
        "os.system",
        "import subprocess",
        "from subprocess",
        "subprocess.run",
        "subprocess.Popen",
        "Popen(",
        "to_thread",
        "threading",
    )
    for banned in banned_words:
        assert banned not in source
    assert "create_subprocess_exec" in source
    assert "start_new_session=True" in source
    assert "create_subprocess_shell" not in source


def test_git_error_attributes() -> None:
    err = GitError("msg", kind="exit", code=3, stderr="e")
    assert (str(err), err.kind, err.code, err.stderr) == ("msg", "exit", 3, "e")
    big = GitOutputTooLarge("big")
    assert big.kind == "too_large" and big.code is None and big.stderr == ""


# ================================================================== 리뷰 1차 수정


class _Spy:
    """`_drain_and_wait`를 감싼다: 끝나기 직전에 훅을 부르고, 끝났음을 기록한다."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, before_return: Any = None) -> None:
        self.finished = False
        self.before_return = before_return
        self._original = git_client._drain_and_wait
        monkeypatch.setattr(git_client, "_drain_and_wait", self)

    async def __call__(self, proc: Any, readers: Any) -> None:
        await self._original(proc, readers)
        self.finished = True
        if self.before_return is not None:
            self.before_return()


# --- 1. 정리가 끝나는 같은 순간에 온 바깥 취소를 잃지 않는다


async def test_cancel_landing_when_cleanup_finishes_is_not_lost(
    fake: Path, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_file = tmp_dir / "pid"
    holder: list[asyncio.Future[GitResult]] = []
    _Spy(monkeypatch, before_return=lambda: holder[0].cancel())
    async with asyncio.timeout(10):
        task = asyncio.ensure_future(
            make(fake, tmp_dir).run(["hang", str(pid_file)], timeout_s=0.5)
        )
        holder.append(task)
        with pytest.raises(asyncio.CancelledError):  # GitError(timeout)가 아니라
            await task
        await assert_processes_gone(pid_file)


async def test_cancel_landing_when_cleanup_finishes_after_overflow(
    fake: Path, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    holder: list[asyncio.Future[GitResult]] = []
    _Spy(monkeypatch, before_return=lambda: holder[0].cancel())
    async with asyncio.timeout(10):
        task = asyncio.ensure_future(
            make(fake, tmp_dir).run(["flood", "5000000"], max_output_bytes=100_000)
        )
        holder.append(task)
        with pytest.raises(asyncio.CancelledError):
            await task


# --- 6. 정리 꼬리는 재취소에도 끝까지 간다(shield)


async def test_cleanup_tail_completes_despite_repeated_cancel(
    fake: Path, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_file = tmp_dir / "pid"
    original = git_client._drain_and_wait

    async def slow(proc: Any, readers: Any) -> None:
        await asyncio.sleep(0.3)  # 정리 꼬리가 아직 도는 동안 다시 취소가 온다
        await original(proc, readers)
        spy_done.append(True)

    spy_done: list[bool] = []
    monkeypatch.setattr(git_client, "_drain_and_wait", slow)
    async with asyncio.timeout(10):
        task = asyncio.ensure_future(make(fake, tmp_dir).run(["hang", str(pid_file)]))
        await read_pid(pid_file)
        task.cancel()
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert spy_done == [True]  # 꼬리가 중간에 끊기지 않았다
        await assert_processes_gone(pid_file)


# --- 2. 환경 설정이 저장소·사용자 설정보다 앞선다


async def _plain_git(tmp_dir: Path, home: Path, *args: str) -> str:
    """보호 없는 git(통제군): 같은 상황에서 보호가 없으면 마커가 생김을 보인다."""
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=tmp_dir,
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    return out.decode()


def test_caller_config_cannot_override_the_fixed_keys() -> None:
    for key in (
        "core.hooksPath",
        "core.fsmonitor",
        "core.excludesFile",
        "core.attributesFile",
        "CORE.HOOKSPATH",
        "core.hookspath",
    ):
        with pytest.raises(ValueError) as info:
            git_env({}, config={key: "/evil-value-xyz"}, allowed_protocols="file")
        assert "evil-value-xyz" not in str(info.value)
        assert key not in str(info.value)


def test_client_config_cannot_override_the_fixed_keys(tmp_dir: Path) -> None:
    with pytest.raises(ValueError):
        GitClient(tmp_dir, config={"core.hooksPath": "/x"}).at(tmp_dir)._environment()


async def test_repo_fsmonitor_script_is_not_run(tmp_dir: Path) -> None:
    marker = tmp_dir / "fsmonitor-ran"
    script = tmp_dir / "fsm.sh"
    script.write_text(f'#!/bin/sh\ntouch "{marker}"\nprintf "\\0"\n')
    script.chmod(0o755)
    git = GitClient(tmp_dir)
    await git.run(["init", "-q", "-b", "main"])
    (tmp_dir / ".git" / "config").write_text(f"[core]\n\tfsmonitor = {script}\n", encoding="utf-8")
    # 통제군: 보호 없이 status를 돌리면 스크립트가 돈다.
    await _plain_git(tmp_dir, tmp_dir, "status")
    assert marker.exists(), "통제군이 마커를 만들지 못했습니다(시험이 의미 없음)"
    marker.unlink()
    await git.run(["status"])
    assert not marker.exists()


async def test_repo_hook_is_not_run_on_commit(tmp_dir: Path) -> None:
    marker = tmp_dir / "hook-ran"
    git = GitClient(tmp_dir, config={"user.name": "n", "user.email": "n@example.com"})
    await git.run(["init", "-q", "-b", "main"])
    hook = tmp_dir / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text(f'#!/bin/sh\ntouch "{marker}"\n')
    hook.chmod(0o755)
    await _plain_git(
        tmp_dir,
        tmp_dir,
        "-c",
        "user.name=n",
        "-c",
        "user.email=n@e.x",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "control",
    )
    assert marker.exists(), "통제군이 마커를 만들지 못했습니다(시험이 의미 없음)"
    marker.unlink()
    await git.run(["commit", "-q", "--allow-empty", "-m", "x"])
    assert not marker.exists()


async def test_home_xdg_ignore_is_not_applied(tmp_dir: Path) -> None:
    home = tmp_dir / "home"
    (home / ".config" / "git").mkdir(parents=True)
    (home / ".config" / "git" / "ignore").write_text("*.md\n")
    repo = tmp_dir / "repo"
    repo.mkdir()
    (repo / "a.md").write_text("x")
    git = GitClient(repo, base_env={"PATH": os.environ["PATH"], "HOME": str(home)})
    await git.run(["init", "-q", "-b", "main"])
    control = await _plain_git(repo, home, "status", "--porcelain", "-uall")
    assert "a.md" not in control, "통제군에서 무시되지 않았습니다(시험이 의미 없음)"
    assert "?? a.md" in (await git.run(["status", "--porcelain", "-uall"])).text


async def test_home_xdg_attributes_are_not_applied(tmp_dir: Path) -> None:
    home = tmp_dir / "home"
    (home / ".config" / "git").mkdir(parents=True)
    (home / ".config" / "git" / "attributes").write_text("* eol=crlf\n")
    repo = tmp_dir / "repo"
    repo.mkdir()
    (repo / "a.txt").write_bytes(b"a\n")
    git = GitClient(repo, base_env={"PATH": os.environ["PATH"], "HOME": str(home)})
    await git.run(["init", "-q", "-b", "main"])
    await git.run(["add", "a.txt"])
    control_dir_out = await _plain_git(repo, home, "ls-files", "--eol")
    assert "eol=crlf" in control_dir_out, "통제군에 속성이 적용되지 않았습니다(시험이 의미 없음)"
    assert "eol=crlf" not in (await git.run(["ls-files", "--eol"])).text


def test_docstring_does_not_claim_more_than_it_does() -> None:
    source = Path(git_client.__file__).read_text(encoding="utf-8")
    assert "user.useConfigOnly" in source  # 작성자 설정은 호출자가 한다는 설명
    assert "사용자 전역 설정이 끼어들지 않는다" not in source


# --- 3. 4KB 경계에서 잘린 줄은 통째로 버린다


def _cut_inside(body: str, keep: int) -> str:
    """앞 4096바이트가 `body[:keep]`에서 끝나도록 채움 줄(줄바꿈 없음)을 앞에 붙인다."""
    head = "fatal: first line\n"
    pad = 4096 - len(head.encode()) - len(body[:keep].encode())
    assert pad > 0
    return head + "a" * pad + body


_URL = f"https://x-access-token:{TOKEN}@github.com/o/r\n"
_HDR = f"AUTHORIZATION: basic {B64}\n"


@pytest.mark.parametrize(
    ("body", "keep"),
    [
        (_URL, _URL.index("@")),
        (_URL, _URL.index("ghp_") + 12),
        (_URL, _URL.index("ghp_") + 4),
        (_HDR, len("AUTHORIZATION: basic ") + 20),
        (f"Authorization: Basic {B64}\n", len("Authorization: Basic ") + 10),
        (f"{B64}\n", 30),
        (f"{B64}\n", 9),
    ],
    ids=[
        "before-at",
        "mid-token",
        "token-start",
        "mid-b64-header",
        "mid-b64-basic",
        "bare-b64",
        "bare-b64-9",
    ],
)
async def test_secret_prefix_at_the_4kb_cut_is_dropped(
    fake: Path, tmp_dir: Path, body: str, keep: int
) -> None:
    raw = _cut_inside(body, keep)
    for client in (make(fake, tmp_dir, auth_header=HEADER), make(fake, tmp_dir)):
        with pytest.raises(GitError) as info:
            await client.run(["fail", raw])
        result = await client.run(["fail", raw], allow_codes={1})
        assert len(result.stderr.encode()) <= 4096
        for text in (info.value.stderr, result.stderr, str(info.value)):
            for secret in (TOKEN[:8], B64[:8], "x-access-token", "AUTHORIZATION"):
                assert secret not in text


async def test_cut_without_any_newline_gives_empty_stderr(fake: Path, tmp_dir: Path) -> None:
    result = await make(fake, tmp_dir).run(["fail", "z" * 6000], allow_codes={1})
    assert result.stderr == ""


async def test_stderr_exactly_complete_lines_below_the_cap_is_kept(
    fake: Path, tmp_dir: Path
) -> None:
    result = await make(fake, tmp_dir).run(["fail", "fatal: a\nfatal: b\n"], allow_codes={1})
    assert result.stderr == "fatal: a\nfatal: b"


# --- 4. 사소한 것들


async def test_args_may_be_an_iterator(fake: Path, tmp_dir: Path) -> None:
    result = await make(fake, tmp_dir).run(iter(["echo-argv", "a"]))  # type: ignore[arg-type]
    assert json.loads(result.text) == ["a"]


async def test_args_with_non_string_in_an_iterator_are_rejected(fake: Path, tmp_dir: Path) -> None:
    with pytest.raises(TypeError):
        await make(fake, tmp_dir).run(iter(["echo-argv", 3]))  # type: ignore[arg-type]


async def test_check_git_version_error_messages_use_a_fixed_label(tmp_dir: Path) -> None:
    fake_dir = tmp_dir / "bin"
    write_fake_git(fake_dir)
    (fake_dir / "git").unlink()  # 공유 스크립트로 가는 링크를 지워야 공유본이 안 바뀐다
    (fake_dir / "git").write_text("#!/bin/sh\necho 'fatal: nope' >&2\nexit 3\n")
    (fake_dir / "git").chmod(0o755)
    with pytest.raises(GitError) as info:
        await check_git_version(base_env={"PATH": str(fake_dir)})
    assert str(info.value).startswith("git --version 실패")
    assert "?" not in str(info.value)


async def test_check_git_version_huge_output_is_a_version_error(tmp_dir: Path) -> None:
    fake_dir = tmp_dir / "bin"
    write_fake_git(fake_dir, version="x" * 200_000)
    with pytest.raises(GitError) as info:
        await check_git_version(base_env={"PATH": str(fake_dir)})
    assert info.value.kind == "exit"
    assert "git 2.32 이상이 필요합니다" in str(info.value)
    assert not isinstance(info.value, GitOutputTooLarge)


async def test_check_git_version_ignores_non_ascii_digits_and_huge_numbers(
    tmp_dir: Path,
) -> None:
    fake_dir = tmp_dir / "bin"
    write_fake_git(fake_dir, version="git version ٢.٤٣.٠")
    with pytest.raises(GitError) as info:
        await check_git_version(base_env={"PATH": str(fake_dir)})
    assert "git 2.32 이상이 필요합니다" in str(info.value)
    write_fake_git(fake_dir, version="git version " + "9" * 5000 + ".1.1")
    with pytest.raises(GitError) as info2:
        await check_git_version(base_env={"PATH": str(fake_dir)})
    assert info2.value.kind == "exit"


async def test_check_git_version_does_not_depend_on_the_current_directory(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_dir = tmp_dir / "bin"
    write_fake_git(fake_dir, version="git version 2.43.0")
    gone = tmp_dir / "gone"
    gone.mkdir()
    monkeypatch.chdir(gone)
    gone.rmdir()
    assert await check_git_version(base_env={"PATH": str(fake_dir)}) == (2, 43, 0)


# --- 5. 하위 명령 찾기·메시지 고르기


def test_subcommand_skips_all_options_with_values() -> None:
    sub = git_client._subcommand
    for option in (
        "-c",
        "-C",
        "--git-dir",
        "--work-tree",
        "--namespace",
        "--config-env",
        "--exec-path",
        "--super-prefix",
        "--attr-source",
    ):
        assert sub([option, "MARKER-value", "fetch"]) == "fetch", option
        assert sub([f"{option}=MARKER-value", "fetch"]) == "fetch", option
    assert sub(["-c", "a=b", "--git-dir", "x", "-C", "y", "status"]) == "status"
    assert sub(["--no-pager", "log"]) == "log"
    assert sub([]) == "?"


async def test_message_prefers_the_fatal_or_error_line(fake: Path, tmp_dir: Path) -> None:
    for text, expected in (
        ("hint: advice\nfatal: the real reason\n", "fatal: the real reason"),
        ("warning: w\nerror: the error\nfatal: later\n", "error: the error"),
        ("remote: something\nwhatever\n", "remote: something"),
    ):
        with pytest.raises(GitError) as info:
            await make(fake, tmp_dir).run(["fail", text])
        assert str(info.value) == f"git fail 실패: {expected}"


async def test_blank_lines_are_judged_before_escaping(fake: Path, tmp_dir: Path) -> None:
    with pytest.raises(GitError) as info:
        await make(fake, tmp_dir).run(["fail", "\t \t\n\u00a0\nfatal: after blanks\n"])
    assert str(info.value) == "git fail 실패: fatal: after blanks"
    with pytest.raises(GitError) as info2:
        await make(fake, tmp_dir).run(["fail", "\t\t\n   \n"])
    assert str(info2.value) == "git fail 실패 (종료 코드 1)"


async def test_escapes_are_visible_in_the_message(fake: Path, tmp_dir: Path) -> None:
    with pytest.raises(GitError) as info:
        await make(fake, tmp_dir).run(["fail", "fatal: \x1b[31m\u202eevil"])
    message = str(info.value)
    assert "\x1b" not in message and "\u202e" not in message
    assert "\\x1b" in message and "\\u202e" in message
    assert message.endswith("evil")


# --- 6. 테스트 빈틈


async def test_stdin_is_devnull_even_if_parent_stdin_has_data(fake: Path, tmp_dir: Path) -> None:
    # 부모의 fd 0에 데이터가 든 파이프를 잠시 걸어 둔다: 상속했다면 자식이 "data"를 읽는다.
    read_end, write_end = os.pipe()
    os.write(write_end, b"data")
    os.close(write_end)
    saved = os.dup(0)
    os.dup2(read_end, 0)
    os.close(read_end)
    try:
        async with asyncio.timeout(10):
            out = (await make(fake, tmp_dir).run(["read-stdin"])).text.strip()
    finally:
        os.dup2(saved, 0)
        os.close(saved)
    assert out == "EOF:0"


async def test_at_keeps_https_protocols_and_base_env(fake: Path, tmp_dir: Path) -> None:
    git = make(
        fake,
        tmp_dir,
        allowed_protocols="https",
        base_env={"PATH": os.environ["PATH"], "HTTPS_PROXY": "proxy-value", "SECRET": "s"},
    )
    env = json.loads((await git.at(tmp_dir).run(["echo-env"])).text)
    assert env["GIT_ALLOW_PROTOCOL"] == "https"
    assert env["HTTPS_PROXY"] == "proxy-value"
    assert "SECRET" not in env


async def test_basic_credentials_without_a_known_header_are_removed(
    fake: Path, tmp_dir: Path
) -> None:
    with pytest.raises(GitError) as info:
        await make(fake, tmp_dir).run(["fail", f"fatal: Basic {B64}"])
    for text in (str(info.value), info.value.stderr):
        assert B64 not in text


async def test_auth_config_key_in_config_is_filtered(fake: Path, tmp_dir: Path) -> None:
    key = "http.https://github.com/.extraheader"
    with_auth = make(fake, tmp_dir, auth_header=HEADER, config={key: "X-Evil: 1", "a.b": "c"})
    pairs = config_pairs(json.loads((await with_auth.run(["echo-env"])).text))
    assert [v for k, v in pairs if k == key] == [HEADER]
    assert ("a.b", "c") in pairs
    without = make(fake, tmp_dir, config={key: "X-Evil: 1"})
    pairs2 = config_pairs(json.loads((await without.run(["echo-env"])).text))
    assert all(k != key for k, _ in pairs2)


async def test_escaped_descendant_holding_the_pipes_does_not_hang_cleanup(
    fake: Path, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 그룹을 벗어난 손자가 파이프를 쥐면 안전망(여기서는 1초) 뒤 전송을 닫고 끝난다.
    monkeypatch.setattr(git_client, "_CLEANUP_TIMEOUT_SECONDS", 1)
    pid_file = tmp_dir / "pid"
    await asyncio.sleep(0)
    before = open_fd_count()
    started = time.monotonic()
    async with asyncio.timeout(10):
        with pytest.raises(GitError) as info:
            await make(fake, tmp_dir).run(["escape", str(pid_file)], timeout_s=0.5)
    assert info.value.kind == "timeout"
    assert time.monotonic() - started < 4
    await asyncio.sleep(0.1)
    grandchild = int(pid_file.read_text())
    assert alive(grandchild), "손자가 아직 살아 파이프를 쥐고 있어야 시험이 의미 있다"
    # 전송을 닫았으므로 파이프 서술자가 그 자리에서 돌아와 있어야 한다.
    assert open_fd_count() <= before, "손자가 살아 있는 동안 파이프가 샜습니다"


# ================================================================== 리뷰 2차 수정


def _write_script(path: Path, marker: Path, body: str = "") -> None:
    path.write_text(f'#!/bin/sh\ntouch "{marker}"\n{body}')
    path.chmod(0o755)


async def _plain_git_stdin(cwd: Path, home: Path, stdin: bytes, *args: str) -> int:
    """통제군: 보호 없는 git에 표준 입력을 준다."""
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=cwd,
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate(stdin)
    assert proc.returncode is not None
    return proc.returncode


_CREDENTIAL_INPUT = b"protocol=https\nhost=example.invalid\n\n"


async def test_repo_credential_helper_is_not_run(tmp_dir: Path) -> None:
    marker = tmp_dir / "helper-ran"
    script = tmp_dir / "helper.sh"
    _write_script(script, marker, "echo username=u\necho password=p\n")
    git = GitClient(tmp_dir)
    await git.run(["init", "-q", "-b", "main"])
    (tmp_dir / ".git" / "config").write_text(f"[credential]\n\thelper = {script}\n")
    await _plain_git_stdin(tmp_dir, tmp_dir, _CREDENTIAL_INPUT, "credential", "fill")
    assert marker.exists(), "통제군이 마커를 만들지 못했습니다(시험이 의미 없음)"
    marker.unlink()
    # 설정 목록의 마지막 값이 빈 값이라 앞선 helper를 비운다.
    shown = (await git.run(["config", "--get-all", "credential.helper"])).text.split("\n")
    assert shown[-2] == "" and str(script) in shown[0]
    with pytest.raises(GitError) as info:  # 표준 입력이 비어 있으니 깨끗하게 실패한다
        await git.run(["credential", "fill"])
    assert info.value.kind == "exit"
    assert not marker.exists()


async def test_repo_askpass_is_not_run(tmp_dir: Path) -> None:
    marker = tmp_dir / "askpass-ran"
    script = tmp_dir / "askpass.sh"
    _write_script(script, marker, "echo secret\n")
    git = GitClient(tmp_dir)
    await git.run(["init", "-q", "-b", "main"])
    (tmp_dir / ".git" / "config").write_text(f"[core]\n\taskPass = {script}\n")
    await _plain_git_stdin(tmp_dir, tmp_dir, _CREDENTIAL_INPUT, "credential", "fill")
    assert marker.exists(), "통제군이 마커를 만들지 못했습니다(시험이 의미 없음)"
    marker.unlink()
    assert (await git.run(["config", "--get", "core.askpass"])).text.strip() == ""
    with pytest.raises(GitError):
        await git.run(["credential", "fill"])
    assert not marker.exists()


@pytest.mark.parametrize(
    "key",
    [
        "include.path",
        "includeIf.gitdir:/x/.path",
        "INCLUDE.PATH",
        "includeif.onbranch:main.path",
        "credential.helper",
        "CORE.askpass",
    ],
)
def test_caller_config_cannot_include_or_override_credentials(key: str) -> None:
    with pytest.raises(ValueError) as info:
        git_env({}, config={key: "/evil-value-xyz"}, allowed_protocols="file")
    assert "evil-value-xyz" not in str(info.value)
    assert key not in str(info.value)


def test_include_like_keys_that_are_not_includes_are_allowed() -> None:
    env = git_env({}, config={"includes.path": "x", "inclusion.a": "y"}, allowed_protocols="file")
    assert ("includes.path", "x") in config_pairs(env)


async def test_repo_include_path_is_not_a_way_around_fixed_config(tmp_dir: Path) -> None:
    # 저장소 설정의 include가 가리킨 파일은 환경 설정(마지막)이 이긴다.
    git = GitClient(tmp_dir)
    await git.run(["init", "-q", "-b", "main"])
    (tmp_dir / "extra.cfg").write_text("[core]\n\thooksPath = /evil\n")
    (tmp_dir / ".git" / "config").write_text(f"[include]\n\tpath = {tmp_dir / 'extra.cfg'}\n")
    assert (await git.run(["config", "--get", "core.hookspath"])).text.strip() == "/dev/null"


async def test_auth_key_filter_is_case_insensitive(fake: Path, tmp_dir: Path) -> None:
    for key in ("HTTP.https://github.com/.ExtraHeader", "http.HTTPS://GITHUB.COM/.extraheader"):
        client = make(fake, tmp_dir, auth_header=HEADER, config={key: "X-Evil: 1", "a.b": "c"})
        pairs = config_pairs(json.loads((await client.run(["echo-env"])).text))
        assert [v for k, v in pairs if k.lower().endswith(".extraheader")] == [HEADER]
        bare = make(fake, tmp_dir, config={key: "X-Evil: 1"})
        pairs2 = config_pairs(json.loads((await bare.run(["echo-env"])).text))
        assert all(not k.lower().endswith(".extraheader") for k, _ in pairs2)
        assert ("a.b", "c") not in pairs2


def test_module_docstring_lists_residual_repo_config_risks() -> None:
    doc = git_client.__doc__ or ""
    for word in (
        "uploadpack",
        "receivepack",
        "diff.external",
        "textconv",
        "filter",
        "gpg.program",
        "--no-ext-diff",
        "--no-textconv",
    ):
        assert word in doc


def test_shared_fake_git_is_a_per_test_symlink(tmp_dir: Path) -> None:
    one = write_fake_git(tmp_dir / "one")
    two = write_fake_git(tmp_dir / "two")
    assert one.is_symlink() and two.is_symlink()
    assert one.resolve() == two.resolve()
    assert one.parent != two.parent

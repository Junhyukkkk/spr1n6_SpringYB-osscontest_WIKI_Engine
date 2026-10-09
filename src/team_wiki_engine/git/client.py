"""셸 없는 git 실행기.

보안·안정성 규칙:
- argv 목록만 `asyncio.create_subprocess_exec`로 넘긴다(셸·`subprocess` 없음). argv에는 인증
  정보를 싣지 않는다 — 인증 헤더는 `GIT_CONFIG_COUNT/KEY_n/VALUE_n` 환경변수로만 넘긴다.
- 자식 환경은 허용 목록(`ENV_PASSTHROUGH`)만 부모에서 복사하고 나머지는 우리가 정한다
  (`GIT_DIR`·API 키가 가지 않고 `~/.gitconfig`·시스템 설정은 읽히지 않는다). 저장소의
  `.git/config`와 `$HOME/.config/git/{ignore,attributes}`는 여전히 읽히므로, 환경 설정의 맨 앞에
  `FIXED_CONFIG`(훅·fsmonitor·전역 무시/속성 파일 끄기)를 둔다 — 환경 설정이 저장소 설정을 이긴다.
  `user.useConfigOnly`는 일부러 켜지 않았다: 호출자가 작성자(`user.name`·`user.email`)를 `config=`로
  직접 넘겨야 한다. 호출자 `config=`의 `include.*`·`includeIf.*` 키는 거부한다(고정 설정을 우회하는
  길이 되지 않게).
- 남는 위험: 저장소 `.git/config`가 실행 명령을 정하는 키 —
  `remote.<이름>.uploadpack`·`receivepack`,
  `diff.external`, `diff.<드라이버>.textconv`, `filter.<이름>.clean`·`smudge`·`process`,
  `gpg.program` 등 — 은 clone 내용으로는 들어오지 않고 로컬 파일 시스템에 쓸 수 있어야만 심을 수
  있어 고정 설정으로 막지 않았다. diff를 읽는 호출(Task 5)은 `--no-ext-diff --no-textconv`를
  반드시 붙인다.
- git은 새 세션(= 새 프로세스 그룹)에서 돈다. 시간 초과·출력 폭주·바깥 취소는 모두 같은 정리
  함수로 그룹 전체를 `SIGKILL`하고, 읽기 태스크를 회수하고, 파이프를 EOF까지 비운 뒤 기다린다
  (Python ≥ 3.12의 `Process.wait()`는 파이프가 모두 닫혀야 돌아오므로, 읽기를 멈춘 채 기다리면
  영원히 멈춘다).
- 예외 메시지·`stderr` 속성·`GitResult.stderr`는 인증 헤더·토큰·URL 사용자 정보를 지운다.
  argv 전체는 어디에도 싣지 않는다.
"""

import asyncio
import base64
import binascii
import math
import os
import re
import signal
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from team_wiki_engine.wiki.lint_secrets import clean

DEFAULT_ALLOWED_PROTOCOLS = "https:file"
GIT_MIN_VERSION = (2, 32)
ENV_PASSTHROUGH: tuple[str, ...] = (
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

AUTH_CONFIG_KEY = "http.https://github.com/.extraheader"
# 호출자의 config보다 앞에 두는 고정 설정. 호출자는 이 키들을 바꿀 수 없다(ValueError).
FIXED_CONFIG: tuple[tuple[str, str], ...] = (
    ("core.hooksPath", "/dev/null"),
    ("core.fsmonitor", "false"),
    ("core.excludesFile", "/dev/null"),
    ("core.attributesFile", "/dev/null"),
    ("credential.helper", ""),
    ("core.askPass", ""),
)
_FIXED_KEYS = frozenset(key.lower() for key, _ in FIXED_CONFIG)
_INCLUDE_PREFIXES = ("include.", "includeif.")
_AUTH_KEY_LOWER = AUTH_CONFIG_KEY.lower()
STDERR_KEEP_BYTES = 4096
MESSAGE_LIMIT = 200
_READ_CHUNK = 64 * 1024
_CLEANUP_TIMEOUT_SECONDS = 5
_MIN_TOKEN_CHARS = 6
_REDACTED = "<생략>"

_AUTHORIZATION_LINE = re.compile(r"AUTHORIZATION:.*", re.IGNORECASE)
_USERINFO = re.compile(r"(?<=://)\S*@")
_BASIC_CREDENTIALS = re.compile(r"\bbasic\s+[A-Za-z0-9+/=_-]{12,}", re.IGNORECASE)
_SUBCOMMAND = re.compile(r"[A-Za-z][A-Za-z0-9-]{0,39}")
_VERSION = re.compile(r"(?<![0-9])([0-9]{1,6})\.([0-9]{1,6})\.([0-9]{1,6})(?![0-9])")
_OPTIONS_WITH_VALUE = frozenset(
    {
        "-c",
        "-C",
        "--git-dir",
        "--work-tree",
        "--namespace",
        "--config-env",
        "--exec-path",
        "--super-prefix",
        "--attr-source",
    }
)
_VERSION_LABEL = "--version"

GitErrorKind = Literal["exit", "timeout", "too_large", "spawn"]


class GitError(Exception):
    """git 실행 실패. `str(error)`와 `stderr`는 비밀값을 지운 글이다."""

    def __init__(
        self,
        message: str,
        *,
        kind: GitErrorKind,
        code: int | None = None,
        stderr: str = "",
    ) -> None:
        super().__init__(message)
        self.kind: GitErrorKind = kind
        self.code = code
        self.stderr = stderr


class GitOutputTooLarge(GitError):
    def __init__(self, message: str, *, stderr: str = "") -> None:
        super().__init__(message, kind="too_large", code=None, stderr=stderr)


@dataclass(frozen=True)
class GitResult:
    code: int
    stdout: bytes
    stderr: str

    @property
    def text(self) -> str:
        return self.stdout.decode("utf-8", errors="replace")


def auth_header_for(token: str) -> str:
    """GitHub HTTPS 인증 헤더(`AUTHORIZATION: basic …`). 환경변수로만 git에 넘긴다."""
    credentials = base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")
    return f"AUTHORIZATION: basic {credentials}"


def git_env(
    base: Mapping[str, str], *, config: Mapping[str, str], allowed_protocols: str
) -> dict[str, str]:
    """`base`에서 허용 목록만 복사하고 고정 값·`FIXED_CONFIG`·`config`를 더한 자식 환경.

    `config`가 고정 키(`core.hooksPath` 등)를 건드리면 `ValueError`(키·값은 메시지에 싣지 않는다).
    """
    env = {key: base[key] for key in ENV_PASSTHROUGH if key in base}
    env["LC_ALL"] = "C"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    env["GIT_ALLOW_PROTOCOL"] = allowed_protocols
    # 읽기 전용 명령(`status` 등)이 인덱스를 새로 쓰려고 `.git/index.lock`을 쥐는 일을 막는다.
    # 그 순간 취소돼 프로세스 그룹이 죽으면 오래된 락이 남아 이후 모든 쓰기가 실패한다.
    env["GIT_OPTIONAL_LOCKS"] = "0"
    entries = list(FIXED_CONFIG)
    for key, value in config.items():
        if not key or "\0" in key or "\0" in value:
            raise ValueError("git 설정 키·값이 올바르지 않습니다")
        lowered = key.lower()
        if lowered in _FIXED_KEYS or lowered.startswith(_INCLUDE_PREFIXES):
            raise ValueError("고정된 git 설정은 바꿀 수 없습니다")
        entries.append((key, value))
    for index, (key, value) in enumerate(entries):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    env["GIT_CONFIG_COUNT"] = str(len(entries))
    return env


def _subcommand(args: Sequence[str]) -> str:
    """`-c`·`-C`의 값을 건너뛴 첫 비옵션 인자. 이름처럼 안 보이면 `?`(argv 조각을 싣지 않는다)."""
    skip = False
    for arg in args:
        if skip:
            skip = False
        elif arg in _OPTIONS_WITH_VALUE:
            skip = True
        elif not arg.startswith("-"):
            return arg if _SUBCOMMAND.fullmatch(arg) else "?"
    return "?"


def _secrets_in(auth_header: str | None) -> list[str]:
    """인증 헤더에서 지워야 할 조각(헤더 전체·base64 값·복호화한 자격 증명·토큰), 긴 것부터."""
    if not auth_header:
        return []
    found = {auth_header.strip()}
    value = auth_header.split()[-1] if auth_header.split() else ""
    if value:
        found.add(value)
        try:
            decoded = base64.b64decode(value, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            decoded = ""
        if decoded:
            found.add(decoded)
            token = decoded.partition(":")[2]
            if len(token) >= _MIN_TOKEN_CHARS:
                found.add(token)
    return sorted((s for s in found if s), key=len, reverse=True)


def _scrub_line(line: str, secrets: Sequence[str]) -> str:
    if not line.strip():  # 빈 줄 판정은 이스케이프 전에(`\t`가 `\\t`로 바뀌면 빈 줄이 아니게 된다)
        return ""
    for secret in secrets:
        line = line.replace(secret, _REDACTED)
    line = _AUTHORIZATION_LINE.sub(_REDACTED, line)
    line = _USERINFO.sub("***@", line)
    line = _BASIC_CREDENTIALS.sub(f"basic {_REDACTED}", line)
    return clean(line)


def _scrub_stderr(raw: bytes, auth_header: str | None) -> str:
    """보관한 stderr(앞 4KB)에서 비밀값을 지운 여러 줄 글.

    4KB에서 잘렸을 수 있으면(`STDERR_KEEP_BYTES`만큼 찼으면) 마지막 줄바꿈 뒤를 통째로 버린다
    (줄바꿈이 없으면 전부) — 경계에 걸린 줄의 토큰·base64 앞부분이 남지 않게.
    """
    if len(raw) >= STDERR_KEEP_BYTES:
        raw = raw[: max(raw.rfind(b"\n"), 0)]
    secrets = _secrets_in(auth_header)
    return "\n".join(
        _scrub_line(line, secrets) for line in raw.decode("utf-8", errors="replace").splitlines()
    )


def _failure_message(subcommand: str, stderr: str, code: int) -> str:
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    first = next(
        (line for line in lines if line.lower().startswith(("fatal:", "error:"))),
        lines[0] if lines else "",
    )
    prefix = f"git {subcommand} 실패"
    message = f"{prefix}: {first}" if first else f"{prefix} (종료 코드 {code})"
    return message[:MESSAGE_LIMIT]


class _Overflow(Exception):
    pass


def _kill_group(proc: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


async def _drain(stream: asyncio.StreamReader | None) -> None:
    if stream is None:
        return
    while await stream.read(_READ_CHUNK):
        pass


async def _drain_and_wait(
    proc: asyncio.subprocess.Process, readers: Sequence["asyncio.Task[None]"]
) -> None:
    for reader in readers:
        reader.cancel()
    await asyncio.gather(*readers, return_exceptions=True)
    try:
        async with asyncio.timeout(_CLEANUP_TIMEOUT_SECONDS):
            await asyncio.gather(_drain(proc.stdout), _drain(proc.stderr))
            await proc.wait()
    except TimeoutError:
        # 그룹을 벗어난 자손이 파이프를 쥐고 있는 경우의 최후 수단: 전송을 닫는다.
        transport = getattr(proc, "_transport", None)
        if transport is not None:
            transport.close()


async def _cleanup(
    proc: asyncio.subprocess.Process, readers: Sequence["asyncio.Task[None]"]
) -> None:
    """그룹 `SIGKILL` → 읽기 태스크 회수 → 파이프를 EOF까지 비움 → `wait`(최대 5초).

    그룹 종료는 동기라서 정리 도중 다시 취소되어도 이미 끝나 있다. 비동기 꼬리는 취소를 한 번
    미뤄 끝까지 돌린 뒤 `CancelledError`를 다시 던진다.
    """
    _kill_group(proc)
    tail = asyncio.ensure_future(_drain_and_wait(proc, readers))
    cancelled = False
    while True:
        try:
            await asyncio.shield(tail)
            break
        except asyncio.CancelledError:
            cancelled = True  # 꼬리가 방금 끝났어도 이 취소는 잃지 않는다
            if tail.done():
                break
    if cancelled:
        raise asyncio.CancelledError


class GitClient:
    def __init__(
        self,
        cwd: Path,
        *,
        auth_header: str | None = None,
        config: Mapping[str, str] | None = None,
        allowed_protocols: str | None = None,
        timeout_s: float = 120.0,
        max_output_bytes: int = 67_108_864,
        git_bin: str = "git",
        base_env: Mapping[str, str] | None = None,
    ) -> None:
        if not (math.isfinite(timeout_s) and timeout_s > 0):
            raise ValueError("timeout_s는 0보다 큰 유한한 수여야 합니다")
        if max_output_bytes < 0:
            raise ValueError("max_output_bytes는 0 이상이어야 합니다")
        self.cwd = cwd
        self._auth_header = auth_header or None
        self._config = dict(config or {})
        self._allowed_protocols = allowed_protocols
        self._timeout_s = timeout_s
        self._max_output_bytes = max_output_bytes
        self._git_bin = git_bin
        self._base_env = None if base_env is None else dict(base_env)

    @property
    def has_auth(self) -> bool:
        return self._auth_header is not None

    def __repr__(self) -> str:
        return f"GitClient(cwd={self.cwd}, auth={'있음' if self.has_auth else '없음'})"

    def at(self, cwd: Path) -> "GitClient":
        """같은 인증·설정·전송 제한·상한으로 다른 폴더에서 돈다."""
        return GitClient(
            cwd,
            auth_header=self._auth_header,
            config=self._config,
            allowed_protocols=self._allowed_protocols,
            timeout_s=self._timeout_s,
            max_output_bytes=self._max_output_bytes,
            git_bin=self._git_bin,
            base_env=self._base_env,
        )

    def _environment(self) -> dict[str, str]:
        base = os.environ if self._base_env is None else self._base_env
        protocols = (
            self._allowed_protocols
            if self._allowed_protocols is not None
            else DEFAULT_ALLOWED_PROTOCOLS  # 호출 시점의 모듈 값(테스트 안전장치가 바꾼다)
        )
        config = {k: v for k, v in self._config.items() if k.lower() != _AUTH_KEY_LOWER}
        if self._auth_header is not None:
            config[AUTH_CONFIG_KEY] = self._auth_header
        return git_env(base, config=config, allowed_protocols=protocols)

    async def run(
        self,
        args: Sequence[str],
        *,
        allow_codes: Collection[int] = (),
        timeout_s: float | None = None,
        max_output_bytes: int | None = None,
    ) -> GitResult:
        if isinstance(args, (str, bytes)):
            raise TypeError("git 인자는 문자열 목록이어야 합니다")
        argv = list(args)  # 반복자도 받는다(검사와 실행이 같은 목록을 본다)
        if not all(isinstance(a, str) for a in argv):
            raise TypeError("git 인자는 문자열 목록이어야 합니다")
        return await self._execute(
            argv,
            _subcommand(argv),
            allow_codes=allow_codes,
            timeout_s=timeout_s,
            max_output_bytes=max_output_bytes,
        )

    async def _execute(
        self,
        argv: list[str],
        sub: str,
        *,
        allow_codes: Collection[int] = (),
        timeout_s: float | None = None,
        max_output_bytes: int | None = None,
    ) -> GitResult:
        limit = self._max_output_bytes if max_output_bytes is None else max_output_bytes
        seconds = self._timeout_s if timeout_s is None else timeout_s
        if not (math.isfinite(seconds) and seconds > 0) or limit < 0:
            raise ValueError("시간·출력 상한이 올바르지 않습니다")

        try:
            proc = await asyncio.create_subprocess_exec(
                self._git_bin,
                *argv,
                cwd=self.cwd,
                env=self._environment(),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except (OSError, ValueError) as exc:
            reason = type(exc).__name__
            if isinstance(exc, OSError) and exc.strerror:
                reason = exc.strerror
            raise GitError(f"git을 실행할 수 없습니다: {reason}", kind="spawn") from None

        out = bytearray()
        err = bytearray()

        async def read_stdout() -> None:
            assert proc.stdout is not None
            while chunk := await proc.stdout.read(_READ_CHUNK):
                if len(out) + len(chunk) > limit:
                    raise _Overflow
                out.extend(chunk)

        async def read_stderr() -> None:
            assert proc.stderr is not None
            while chunk := await proc.stderr.read(_READ_CHUNK):
                room = STDERR_KEEP_BYTES - len(err)
                if room > 0:
                    err.extend(chunk[:room])

        readers = [asyncio.ensure_future(read_stdout()), asyncio.ensure_future(read_stderr())]
        deadline = asyncio.timeout(seconds)
        try:
            async with deadline:
                await asyncio.gather(*readers)
                code = await proc.wait()
        except GeneratorExit:
            # 코루틴이 그냥 닫힌다(GC 등): 기다릴 수 없으니 그룹만 죽이고 그대로 닫는다.
            _kill_group(proc)
            for reader in readers:
                reader.cancel()
            raise
        except BaseException as exc:
            # 시간 초과·출력 폭주·바깥 취소·그 밖의 예외 모두 같은 정리를 거친다.
            await _cleanup(proc, readers)
            stderr = _scrub_stderr(bytes(err), self._auth_header)
            if isinstance(exc, _Overflow):
                raise GitOutputTooLarge(
                    f"git {sub} 출력이 상한({limit}바이트)을 넘었습니다", stderr=stderr
                ) from None
            if isinstance(exc, TimeoutError) and deadline.expired():
                raise GitError(f"git {sub} 시간 초과", kind="timeout", stderr=stderr) from None
            raise

        stderr = _scrub_stderr(bytes(err), self._auth_header)
        if code != 0 and code not in allow_codes:
            raise GitError(
                _failure_message(sub, stderr, code), kind="exit", code=code, stderr=stderr
            )
        return GitResult(code, bytes(out), stderr)


async def check_git_version(
    git_bin: str = "git", *, base_env: Mapping[str, str] | None = None
) -> tuple[int, int, int]:
    """`git --version`의 첫 `x.y.z`를 읽는다. `GIT_MIN_VERSION` 미만이거나 못 읽으면 오류."""
    required = ".".join(str(n) for n in GIT_MIN_VERSION)
    client = GitClient(
        Path("/"), git_bin=git_bin, base_env=base_env, timeout_s=30.0, max_output_bytes=65_536
    )
    try:
        result = await client._execute(["--version"], _VERSION_LABEL)
    except GitOutputTooLarge:
        raise GitError(
            f"git {required} 이상이 필요합니다 (버전 출력이 너무 깁니다)", kind="exit"
        ) from None
    match = _VERSION.search(result.text)
    if match is None:
        raise GitError(
            f"git {required} 이상이 필요합니다 (버전을 읽을 수 없습니다: "
            f"{clean(result.text.strip())[:80]!r})",
            kind="exit",
        )
    version = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    if version[:2] < GIT_MIN_VERSION:
        raise GitError(
            f"git {required} 이상이 필요합니다 (현재 {'.'.join(map(str, version))})", kind="exit"
        )
    return version

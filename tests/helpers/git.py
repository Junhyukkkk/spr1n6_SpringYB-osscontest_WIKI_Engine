"""실제 git으로 임시 저장소를 만드는 시험 도우미.

모든 클라이언트는 `allowed_protocols="file"`이다(오프라인 안전장치와 같은 제한을 명시한다).
`GIT_CONFIG_GLOBAL=/dev/null`이라 사용자의 `init.defaultBranch`가 먹지 않고 git 2.54의 기본은
`master`이므로 브랜치 이름은 `-b main`으로 정한다.
"""

from collections.abc import Mapping
from pathlib import Path

from team_wiki_engine.git.client import GitClient
from tests.helpers import write_tree

_LOCAL_CONFIG = (
    "[user]\n\tname = tester\n\temail = tester@example.com\n[commit]\n\tgpgsign = false\n"
)


def _client(directory: Path) -> GitClient:
    return GitClient(directory, allowed_protocols="file", timeout_s=60.0)


async def init_repo(
    directory: Path, files: Mapping[str, str] | None = None, message: str = "init"
) -> GitClient:
    """`main` 브랜치 작업 사본을 만든다. 작성자는 로컬 설정의 `tester`다."""
    directory.mkdir(parents=True, exist_ok=True)
    git = _client(directory)
    await git.run(["init", "-q", "-b", "main"])
    # `git config`를 세 번 부르는 대신 로컬 설정 파일에 직접 덧붙인다(프로세스 절약).
    with (directory / ".git" / "config").open("a", encoding="utf-8") as config:
        config.write(_LOCAL_CONFIG)
    if files:
        await commit_files(git, files, message)
    return git


async def commit_files(git: GitClient, files: Mapping[str, str], message: str) -> str:
    """파일을 쓰고 전부 커밋한 뒤 새 커밋의 SHA를 돌려준다."""
    write_tree(git.cwd, files)
    await git.run(["add", "-A"])
    await git.run(["commit", "-q", "-m", message])
    return (await git.run(["rev-parse", "HEAD"])).text.strip()


async def init_bare(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    await _client(directory).run(["init", "-q", "--bare", "-b", "main"])


async def seed_bare(
    bare: Path, work: Path, files: Mapping[str, str], message: str = "init"
) -> GitClient:
    """bare 저장소를 만들고 첫 커밋을 올린 작업 사본을 돌려준다."""
    await init_bare(bare)
    git = await init_repo(work, files, message)
    await git.run(["remote", "add", "origin", str(bare)])
    await git.run(["push", "-q", "origin", "main"])
    return git

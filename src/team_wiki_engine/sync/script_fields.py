"""`baseline.json`과 현황 페이지의 `baseline_commit`·`archived` 기준 값 쓰기(G10).

점검(L4)이 baseline과 현황 페이지를 비교하므로 점검 **전에** 스크립트가 맞춘다.
"""

import os
import stat
from pathlib import Path

from team_wiki_engine.util.fs import list_markdown_files, write_file_atomic
from team_wiki_engine.wiki.baseline import Baseline, write_baseline
from team_wiki_engine.wiki.frontmatter import parse_frontmatter, serialize_frontmatter

_MAX_PAGE_BYTES = 4 * 1024 * 1024


def _is_real_dir(path: Path) -> bool:
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


def _read_page(file: Path) -> str | None:
    """일반 파일을 UTF-8로 읽는다. 링크·읽기 오류·UTF-8 아님·너무 큼은 `None`."""
    try:
        fd = os.open(file, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_PAGE_BYTES:
            return None
        chunks: list[bytes] = []
        while chunk := os.read(fd, 1 << 20):
            chunks.append(chunk)
            if sum(map(len, chunks)) > _MAX_PAGE_BYTES:
                return None
        return b"".join(chunks).decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    finally:
        os.close(fd)


def apply_baseline(data_dir: Path, baseline: Baseline) -> list[str]:
    """`baseline.json`을 쓰고 현황 페이지의 기준 값을 baseline에 맞춘다.

    쓴 파일을 데이터 저장소 기준 `/` 상대 경로로 돌려준다(`baseline.json`이 맨 앞).
    `write_baseline`의 `ValueError`(잘못된 baseline)는 그대로 올린다 — 이때는 아무것도 쓰지 않는다.
    읽을 수 없거나 UTF-8이 아니거나 frontmatter가 망가진 페이지, `repo`가 문자열이 아니거나
    baseline에 없는 페이지는 건너뛴다(점검이 보고한다). `wiki`·`wiki/status`가 링크면 그 안은
    건드리지 않는다. 고친 뒤 다시 읽어 값이 같지 않으면(예: 따옴표 안 처음·끝의 NEL) 쓰지 않는다.

    주의: 페이지 쓰기의 `OSError`는 그대로 올라가고, 그때까지 쓴 파일(`baseline.json` 포함)은
    남는다(부분 쓰기). 호출자(Task 9 `reapply`, Task 14)는 이를 단계 실패로 다뤄야 한다. 돌려주는
    경로는 디스크의 이름 그대로(NFD·제어 문자 가능)이므로 표시·프롬프트에 쓰기 전에 NFC와
    `clean()`을 거쳐야 한다.
    """
    write_baseline(data_dir, baseline)
    touched = ["baseline.json"]
    root = Path(os.path.abspath(data_dir))
    status_dir = root / "wiki" / "status"
    if not (_is_real_dir(root / "wiki") and _is_real_dir(status_dir)):
        return touched
    for file in list_markdown_files(status_dir):
        text = _read_page(file)
        if text is None:
            continue
        parsed = parse_frontmatter(text)
        data = parsed.data
        if data is None:
            continue
        repo = data.get("repo")
        if not isinstance(repo, str) or repo not in baseline.repos:
            continue
        want = baseline.repos[repo]
        if data.get("baseline_commit") == want.commit and data.get("archived") is want.archived:
            continue
        updated = {**data, "baseline_commit": want.commit, "archived": want.archived}
        new_text = serialize_frontmatter(updated, parsed.body)
        check = parse_frontmatter(new_text)
        expected_body = parsed.body if parsed.body.startswith("\n") else f"\n{parsed.body}"
        if check.data != updated or check.body != expected_body:
            continue  # 왕복이 안 되는 값: 내용을 잃느니 건너뛴다(점검이 보고한다)
        write_file_atomic(file, new_text)
        touched.append(file.relative_to(root).as_posix())
    return touched


def script_value_paths(data_dir: Path) -> list[str]:
    """`apply_baseline`이 쓰는 파일·폴더 중 지금 있는 것(데이터 저장소 기준 `/` 상대 경로).

    기준 값 커밋(`commit_paths`)은 이것만 담는다 — 다른 바뀐 파일이 섞여 전달되지 않게.
    """
    return [rel for rel in ("baseline.json", "wiki/status") if os.path.lexists(data_dir / rel)]

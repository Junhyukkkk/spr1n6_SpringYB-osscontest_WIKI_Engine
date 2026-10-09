"""테스트 공용 도우미."""

import os
import tempfile
from collections.abc import Mapping
from pathlib import Path


def make_tmp_dir(prefix: str = "twe-") -> Path:
    """realpath된 임시 폴더를 만든다. 정리는 fixture가 맡는다."""
    return Path(os.path.realpath(tempfile.mkdtemp(prefix=prefix)))


def make_raw_tmp_dir(prefix: str = "twe-raw-") -> Path:
    """realpath하지 않은 임시 폴더를 만든다(macOS `/var` 경로 시험용)."""
    return Path(tempfile.mkdtemp(prefix=prefix))


def write_tree(root: Path, files: Mapping[str, str]) -> None:
    """`{상대경로: 내용}`대로 파일을 쓴다. 상위 폴더는 자동으로 만든다."""
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

"""baseline.json과 현황 페이지 기준 값 쓰기."""

import os
import stat
from pathlib import Path

import pytest

from team_wiki_engine.sync import script_fields
from team_wiki_engine.sync.script_fields import apply_baseline
from team_wiki_engine.wiki.baseline import Baseline, RepoBaseline, read_baseline
from team_wiki_engine.wiki.frontmatter import parse_frontmatter
from tests.helpers import write_tree

A = "a" * 40
B = "b" * 40
ZERO = "0" * 40


def _page(repo: str, commit: str, archived: str = "false", body: str = "# x\n") -> str:
    return (
        f"---\nrepo: {repo}\nbaseline_commit: {commit}\narchived: {archived}\n"
        f"owners: []\nupdated: 2026-10-07\n---\n\n{body}"
    )


def _baseline() -> Baseline:
    return Baseline(
        repos={
            "worker": RepoBaseline(commit=A, archived=False),
            "relay": RepoBaseline(commit=B, archived=True),
        },
        raw_processed=[],
        updated_at="2026-10-07T09:00:00+09:00",
    )


class TestApplyBaseline:
    def test_baseline을_쓰고_현황_페이지의_기준_값을_맞춘다(self, tmp_dir: Path) -> None:
        write_tree(
            tmp_dir,
            {
                "wiki/status/worker.md": _page("worker", f'"{ZERO}"', body="# worker\n"),
                "wiki/status/relay.md": _page("relay", B, "true", body="# relay\n"),
                "wiki/status/web.md": (
                    "---\nrepo: web\nbaseline_commit: x\narchived: false\nowners: []\n"
                    "updated: 2026-10-07\n---\n"
                ),
            },
        )
        baseline = _baseline()
        assert apply_baseline(tmp_dir, baseline) == ["baseline.json", "wiki/status/worker.md"]
        assert read_baseline(tmp_dir) == baseline
        worker = parse_frontmatter((tmp_dir / "wiki/status/worker.md").read_text(encoding="utf-8"))
        assert worker.data is not None
        assert worker.data["repo"] == "worker"
        assert worker.data["baseline_commit"] == A
        assert worker.data["archived"] is False
        assert "# worker" in worker.body
        web = parse_frontmatter((tmp_dir / "wiki/status/web.md").read_text(encoding="utf-8"))
        assert web.data is not None
        assert web.data["baseline_commit"] == "x"

    def test_망가진_frontmatter_페이지는_건너뛴다(self, tmp_dir: Path) -> None:
        broken = "---\nrepo: worker\nbaseline_commit: [unclosed\n---\n\n# broken\n"
        write_tree(tmp_dir, {"wiki/status/worker.md": broken})
        assert apply_baseline(tmp_dir, _baseline()) == ["baseline.json"]
        assert (tmp_dir / "wiki/status/worker.md").read_text(encoding="utf-8") == broken

    def test_frontmatter가_없는_페이지와_repo가_문자열이_아닌_페이지는_건너뛴다(
        self, tmp_dir: Path
    ) -> None:
        files = {
            "wiki/status/none.md": "# 본문뿐\n",
            "wiki/status/num.md": "---\nrepo: 5\nbaseline_commit: x\n---\n\n# n\n",
            "wiki/status/list.md": "---\nrepo: [worker]\n---\n\n# l\n",
            "wiki/status/missing.md": "---\nowners: []\n---\n\n# m\n",
        }
        write_tree(tmp_dir, files)
        assert apply_baseline(tmp_dir, _baseline()) == ["baseline.json"]
        for rel, content in files.items():
            assert (tmp_dir / rel).read_text(encoding="utf-8") == content

    def test_UTF8이_아닌_페이지는_건너뛴다(self, tmp_dir: Path) -> None:
        target = tmp_dir / "wiki/status/worker.md"
        target.parent.mkdir(parents=True)
        raw = _page("worker", ZERO).encode("utf-8") + b"\xff\xfe"
        target.write_bytes(raw)
        assert apply_baseline(tmp_dir, _baseline()) == ["baseline.json"]
        assert target.read_bytes() == raw

    def test_archived_문자열_false는_불리언으로_고친다(self, tmp_dir: Path) -> None:
        write_tree(tmp_dir, {"wiki/status/worker.md": _page("worker", A, '"false"')})
        assert apply_baseline(tmp_dir, _baseline()) == ["baseline.json", "wiki/status/worker.md"]
        parsed = parse_frontmatter((tmp_dir / "wiki/status/worker.md").read_text(encoding="utf-8"))
        assert parsed.data is not None
        assert parsed.data["archived"] is False

    def test_archived_0과_빈_값도_불리언이_아니라서_고친다(self, tmp_dir: Path) -> None:
        write_tree(
            tmp_dir,
            {
                "wiki/status/worker.md": _page("worker", A, "0"),
                "wiki/status/relay.md": _page("relay", B, "1"),
            },
        )
        touched = apply_baseline(tmp_dir, _baseline())
        assert touched == ["baseline.json", "wiki/status/relay.md", "wiki/status/worker.md"]
        for name, expected in (("worker", False), ("relay", True)):
            parsed = parse_frontmatter(
                (tmp_dir / f"wiki/status/{name}.md").read_text(encoding="utf-8")
            )
            assert parsed.data is not None
            assert parsed.data["archived"] is expected

    def test_값이_같으면_다시_쓰지_않는다(self, tmp_dir: Path) -> None:
        write_tree(tmp_dir, {"wiki/status/worker.md": _page("worker", A)})
        page = tmp_dir / "wiki/status/worker.md"
        os.utime(page, (1_000_000_000, 1_000_000_000))
        before = page.stat().st_mtime_ns
        assert apply_baseline(tmp_dir, _baseline()) == ["baseline.json"]
        assert page.stat().st_mtime_ns == before

    def test_본문_바이트가_보존된다(self, tmp_dir: Path) -> None:
        body = "# x\n\n한글 본문  \n\n```py\nprint('a')\n```\r\n끝 \u0085 줄\n\n"
        text = _page("worker", ZERO, body=body)
        write_tree(tmp_dir, {"wiki/status/worker.md": text})
        (tmp_dir / "wiki/status/worker.md").write_bytes(text.encode("utf-8"))
        apply_baseline(tmp_dir, _baseline())
        after = (tmp_dir / "wiki/status/worker.md").read_bytes()
        assert after.endswith(("\n" + body).encode("utf-8"))
        parsed = parse_frontmatter(after.decode("utf-8"))
        assert parsed.body == "\n" + body

    def test_baseline에_없는_저장소_페이지는_그대로(self, tmp_dir: Path) -> None:
        other = _page("other", ZERO)
        write_tree(tmp_dir, {"wiki/status/other.md": other})
        assert apply_baseline(tmp_dir, _baseline()) == ["baseline.json"]
        assert (tmp_dir / "wiki/status/other.md").read_text(encoding="utf-8") == other

    def test_하위_폴더의_현황_페이지도_맞춘다(self, tmp_dir: Path) -> None:
        write_tree(tmp_dir, {"wiki/status/팀/worker.md": _page("worker", ZERO)})
        assert apply_baseline(tmp_dir, _baseline()) == ["baseline.json", "wiki/status/팀/worker.md"]

    def test_wiki_status_밖의_페이지는_건드리지_않는다(self, tmp_dir: Path) -> None:
        text = _page("worker", ZERO)
        write_tree(tmp_dir, {"wiki/other/worker.md": text})
        assert apply_baseline(tmp_dir, _baseline()) == ["baseline.json"]
        assert (tmp_dir / "wiki/other/worker.md").read_text(encoding="utf-8") == text

    def test_status_폴더가_없어도_baseline은_쓴다(self, tmp_dir: Path) -> None:
        assert apply_baseline(tmp_dir, _baseline()) == ["baseline.json"]
        assert (tmp_dir / "baseline.json").is_file()

    def test_status_폴더가_링크면_링크_너머를_고치지_않는다(self, tmp_dir: Path) -> None:
        outside = tmp_dir / "outside"
        text = _page("worker", ZERO)
        write_tree(outside, {"worker.md": text})
        (tmp_dir / "data" / "wiki").mkdir(parents=True)
        os.symlink(outside, tmp_dir / "data" / "wiki" / "status")
        assert apply_baseline(tmp_dir / "data", _baseline()) == ["baseline.json"]
        assert (outside / "worker.md").read_text(encoding="utf-8") == text

    def test_wiki_폴더가_링크면_링크_너머를_고치지_않는다(self, tmp_dir: Path) -> None:
        outside = tmp_dir / "outside"
        text = _page("worker", ZERO)
        write_tree(outside, {"status/worker.md": text})
        (tmp_dir / "data").mkdir()
        os.symlink(outside, tmp_dir / "data" / "wiki")
        assert apply_baseline(tmp_dir / "data", _baseline()) == ["baseline.json"]
        assert (outside / "status/worker.md").read_text(encoding="utf-8") == text

    def test_링크인_현황_페이지는_건너뛴다(self, tmp_dir: Path) -> None:
        outside = tmp_dir / "outside.md"
        text = _page("worker", ZERO)
        outside.write_text(text, encoding="utf-8")
        (tmp_dir / "data" / "wiki" / "status").mkdir(parents=True)
        os.symlink(outside, tmp_dir / "data" / "wiki" / "status" / "worker.md")
        assert apply_baseline(tmp_dir / "data", _baseline()) == ["baseline.json"]
        assert outside.read_text(encoding="utf-8") == text

    @pytest.mark.parametrize("title", ['"a\\N"', '"\\Na"'])
    def test_왕복이_안_되는_값은_건너뛰고_바이트를_그대로_둔다(
        self, tmp_dir: Path, title: str
    ) -> None:
        # 계획 1의 알려진 한계: 따옴표 안 끝(또는 처음)의 NEL(U+0085)은 다시 쓰면 공백으로 바뀐다.
        text = (
            f'---\nrepo: worker\nbaseline_commit: "{ZERO}"\narchived: false\n'
            f"title: {title}\n---\n\n# x\n"
        )
        page = tmp_dir / "wiki/status/worker.md"
        page.parent.mkdir(parents=True)
        page.write_bytes(text.encode("utf-8"))
        assert apply_baseline(tmp_dir, _baseline()) == ["baseline.json"]
        assert page.read_bytes() == text.encode("utf-8")

    def test_NEL이_가운데에_있으면_왕복되므로_고친다(self, tmp_dir: Path) -> None:
        text = (
            f'---\nrepo: worker\nbaseline_commit: "{ZERO}"\narchived: false\n'
            'title: "a\\N b"\n---\n\n# x\n'
        )
        page = tmp_dir / "wiki/status/worker.md"
        page.parent.mkdir(parents=True)
        page.write_bytes(text.encode("utf-8"))
        assert apply_baseline(tmp_dir, _baseline()) == ["baseline.json", "wiki/status/worker.md"]
        parsed = parse_frontmatter(page.read_text(encoding="utf-8"))
        assert parsed.data is not None
        assert parsed.data["title"] == "a\u0085 b"
        assert parsed.data["baseline_commit"] == A

    def test_4MB가_넘는_페이지는_건너뛰고_바이트를_그대로_둔다(self, tmp_dir: Path) -> None:
        page = tmp_dir / "wiki/status/worker.md"
        page.parent.mkdir(parents=True)
        raw = (_page("worker", ZERO) + "x" * (4 * 1024 * 1024 + 1)).encode("utf-8")
        page.write_bytes(raw)
        assert apply_baseline(tmp_dir, _baseline()) == ["baseline.json"]
        assert page.read_bytes() == raw

    def test_링크와_FIFO_경로가_목록에_섞여도_멈추지_않고_건너뛴다(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        text = _page("worker", ZERO)
        target = tmp_dir / "target.md"
        target.write_text(text, encoding="utf-8")
        status = tmp_dir / "wiki/status"
        status.mkdir(parents=True)
        link = status / "link.md"
        os.symlink(target, link)
        fifo = status / "fifo.md"
        os.mkfifo(fifo)
        monkeypatch.setattr(script_fields, "list_markdown_files", lambda _d: [link, fifo])
        assert apply_baseline(tmp_dir, _baseline()) == ["baseline.json"]
        assert target.read_text(encoding="utf-8") == text
        assert stat.S_ISFIFO(os.lstat(fifo).st_mode)

    def test_잘못된_baseline은_ValueError를_그대로_올린다(self, tmp_dir: Path) -> None:
        write_tree(tmp_dir, {"wiki/status/worker.md": _page("worker", ZERO)})
        bad = Baseline(repos={"worker": RepoBaseline(commit="nothex", archived=False)})
        with pytest.raises(ValueError):
            apply_baseline(tmp_dir, bad)
        assert not (tmp_dir / "baseline.json").exists()
        assert "baseline_commit: " + ZERO in (tmp_dir / "wiki/status/worker.md").read_text(
            encoding="utf-8"
        )

    def test_저장소_이름이_같은_키가_아니면_건드리지_않는다(self, tmp_dir: Path) -> None:
        text = _page("Worker", ZERO)  # 대소문자가 다르면 다른 저장소
        write_tree(tmp_dir, {"wiki/status/Worker.md": text})
        assert apply_baseline(tmp_dir, _baseline()) == ["baseline.json"]
        assert (tmp_dir / "wiki/status/Worker.md").read_text(encoding="utf-8") == text

    def test_두_번_부르면_두_번째는_baseline만_쓴다(self, tmp_dir: Path) -> None:
        write_tree(tmp_dir, {"wiki/status/worker.md": _page("worker", ZERO)})
        assert len(apply_baseline(tmp_dir, _baseline())) == 2
        assert apply_baseline(tmp_dir, _baseline()) == ["baseline.json"]

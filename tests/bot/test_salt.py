import os
import stat
import threading
from pathlib import Path

import pytest

from team_wiki_engine.bot.salt import USER_SALT_FILE, load_or_create_user_salt
from team_wiki_engine.bot.text import hash_user
from team_wiki_engine.config.load import ConfigError


class TestUserSalt:
    def test_creates_0600_hex(self, tmp_dir: Path) -> None:
        salt = load_or_create_user_salt(tmp_dir)
        path = tmp_dir / USER_SALT_FILE
        assert len(salt) == 64 and int(salt, 16) >= 0
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert path.read_text() == salt

    def test_creates_missing_state_dir(self, tmp_dir: Path) -> None:
        assert len(load_or_create_user_salt(tmp_dir / "new" / "state")) == 64

    def test_reused_and_differs_from_guild_salt(self, tmp_dir: Path) -> None:
        first = load_or_create_user_salt(tmp_dir)
        assert load_or_create_user_salt(tmp_dir) == first
        assert hash_user("123", first) != hash_user("123", "987654321012345678")

    @pytest.mark.parametrize("content", ["", "zz" * 32, "ab" * 200, "AB" * 32, "ab" * 32 + "\n"])
    def test_garbage_rejected_content_free(self, tmp_dir: Path, content: str) -> None:
        (tmp_dir / USER_SALT_FILE).write_text(content)
        with pytest.raises(ConfigError) as err:
            load_or_create_user_salt(tmp_dir)
        assert content.strip() == "" or content.strip() not in str(err.value)

    def test_symlink_rejected(self, tmp_dir: Path) -> None:
        target = tmp_dir / "elsewhere"
        target.write_text("ab" * 32)
        (tmp_dir / USER_SALT_FILE).symlink_to(target)
        with pytest.raises(ConfigError):
            load_or_create_user_salt(tmp_dir)

    def test_non_regular_rejected(self, tmp_dir: Path) -> None:
        (tmp_dir / USER_SALT_FILE).mkdir()
        with pytest.raises(ConfigError):
            load_or_create_user_salt(tmp_dir)

    def test_concurrent_creators_agree(self, tmp_dir: Path) -> None:
        results: list[str] = []
        barrier = threading.Barrier(8)

        def run() -> None:
            barrier.wait()
            results.append(load_or_create_user_salt(tmp_dir))

        threads = [threading.Thread(target=run) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(results) == 8 and len(set(results)) == 1
        assert os.listdir(tmp_dir) == [USER_SALT_FILE]

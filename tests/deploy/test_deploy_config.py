"""tmpfiles·journald·비밀값 예시·서버 설정 예시 시험."""

from __future__ import annotations

import json
import os
from pathlib import Path

from team_wiki_engine.config.load import load_config
from team_wiki_engine.github.client import parse_repo_url
from tests.helpers.systemd_unit import (
    KNOWN_KEYS,
    parse_bytes,
    parse_env_file,
    parse_timespan,
    parse_tmpfiles,
    parse_unit,
)

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"


def test_tmpfiles() -> None:
    lines = parse_tmpfiles((DEPLOY / "tmpfiles" / "team-wiki.conf").read_text(encoding="utf-8"))
    got = [(x.type, x.path, x.mode, x.user, x.group, x.age) for x in lines]
    assert got == [
        ("d", "/srv/team-wiki", "0750", "teamwiki", "teamwiki", "-"),
        ("d", "/srv/team-wiki/home", "0700", "teamwiki", "teamwiki", "-"),
        ("d", "/srv/team-wiki/state", "0700", "teamwiki", "teamwiki", "-"),
        ("d", "/srv/team-wiki/state/runs", "0700", "teamwiki", "teamwiki", "30d"),
        ("d", "/srv/team-wiki/repos", "0700", "teamwiki", "teamwiki", "-"),
        ("d", "/var/backups/team-wiki", "0700", "root", "root", "-"),
        ("d", "/var/lib/team-wiki-deploy", "0700", "root", "root", "-"),
    ]
    assert [x.path for x in lines if x.age != "-"] == ["/srv/team-wiki/state/runs"]
    bases = ("/srv/team-wiki", "/var/backups/team-wiki", "/var/lib/team-wiki-deploy")
    for x in lines:
        assert any(x.path == b or x.path.startswith(b + "/") for b in bases)
        assert "/opt/" not in x.path
        assert x.path.rsplit("/", 1)[-1] not in ("data", "data-sync")


def test_journald() -> None:
    text = (DEPLOY / "journald" / "team-wiki.conf").read_text(encoding="utf-8")
    unit = parse_unit(text, "team-wiki.conf")
    assert set(unit.sections) == {"Journal"}
    assert set(unit.sections["Journal"]) <= KNOWN_KEYS["Journal"]
    assert parse_bytes(unit.one("Journal", "SystemMaxUse")) <= 2 * 2**30
    assert parse_timespan(unit.one("Journal", "MaxRetentionSec")) > 0
    assert unit.one("Journal", "Storage") == "persistent"


def test_secrets_example_has_only_empty_names() -> None:
    text = (DEPLOY / "secrets.env.example").read_text(encoding="utf-8")
    assert parse_env_file(text) == [
        ("ANTHROPIC_API_KEY", ""),
        ("DISCORD_TOKEN", ""),
        ("GITHUB_TOKEN", ""),
    ]
    assert "PROXY" not in text.upper()


def test_server_config_examples() -> None:
    example = json.loads((ROOT / "config" / "team.example.json").read_text(encoding="utf-8"))
    local = load_config(DEPLOY / "config" / "team.local.json")
    github = load_config(DEPLOY / "config" / "team.github.json")
    assert local.delivery == "local"
    assert os.path.isabs(local.data_repo.url)
    assert local.data_repo.url.startswith("/srv/team-wiki/")
    assert github.delivery == "github"
    assert parse_repo_url(github.data_repo.url) is not None
    assert github.data_repo.url.startswith("https://github.com/")
    assert "@" not in github.data_repo.url
    for name in ("team.local.json", "team.github.json"):
        raw = json.loads((DEPLOY / "config" / name).read_text(encoding="utf-8"))
        for key in ("repos", "schedule", "timezone", "limits"):
            assert raw[key] == example[key], (name, key)
        d = raw["discord"]
        assert all(v in ("", []) for v in d.values()), name

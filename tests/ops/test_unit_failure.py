import pytest

from team_wiki_engine.ops.notify import unit_failure_message


def test_정확한_문구() -> None:
    assert unit_failure_message("team-wiki-sync.service", "host1") == (
        "[systemd] team-wiki-sync.service 유닛이 실패했습니다 (host1). "
        "확인: journalctl -u team-wiki-sync.service -n 100"
    )


def test_빈_이름() -> None:
    assert unit_failure_message("  ", "host1") == (
        "[systemd] (알 수 없음) 유닛이 실패했습니다 (host1). 확인: systemctl --failed"
    )


@pytest.mark.parametrize("unit", ["team-wiki-notify@x.service", "a:b"])
def test_허용된_이름(unit: str) -> None:
    assert f"[systemd] {unit} 유닛" in unit_failure_message(unit, "h")


@pytest.mark.parametrize("unit", ["@everyone", "x\ny", "`x`", "a" * 201, "<@123>"])
def test_위험한_이름은_이름_없음(unit: str) -> None:
    msg = unit_failure_message(unit, "h")
    assert "(알 수 없음)" in msg
    assert "systemctl --failed" in msg
    assert "@everyone" not in msg and "<@" not in msg and "`" not in msg


@pytest.mark.parametrize("host", ["bad host", "<@1>", "", "a" * 65])
def test_잘못된_호스트(host: str) -> None:
    assert "(알 수 없는 호스트)" in unit_failure_message("u.service", host)

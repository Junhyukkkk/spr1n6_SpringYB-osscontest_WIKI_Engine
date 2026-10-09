import typing

from team_wiki_engine.bot import replies
from team_wiki_engine.bot.limiter import RejectReason
from team_wiki_engine.bot.replies import (
    ADMIN_ONLY_TEXT,
    CODE_SOURCE_NOTE,
    DATA_BUSY_TEXT,
    PAUSE_ACK_TEXT,
    PAUSED_TEXT,
    RESUME_ACK_TEXT,
    RESUME_FAILED_PREFIX,
    SHUTDOWN_TEXT,
    TOO_LONG_TEXT,
    TRUNCATED_NOTE,
    USAGE_TEXT,
    failure_text,
    rejection_text,
)


def test_fixed_texts_verbatim() -> None:
    assert rejection_text("user_rate") == "질문이 조금 빨라요. 1분 뒤에 다시 물어봐 주세요."
    assert rejection_text("daily_quota") == "오늘 질문 한도를 다 썼어요. 내일 다시 물어봐 주세요."
    assert (
        rejection_text("busy") == "지금 다른 질문을 처리하고 있어요. 잠시 뒤에 다시 물어봐 주세요."
    )
    assert (
        rejection_text("user_busy")
        == "앞 질문의 답을 아직 만들고 있어요. 답이 나온 뒤에 다시 물어봐 주세요."
    )
    assert failure_text("timeout") == (
        "답을 찾는 데 너무 오래 걸려서 멈췄어요. 질문을 좁혀서 다시 물어봐 주세요."
    )
    for kind in ("error", "max_turns", "empty"):
        assert failure_text(kind) == "답을 만들지 못했어요. 잠시 뒤에 다시 물어봐 주세요."
    assert (
        USAGE_TEXT
        == "저를 멘션하고 궁금한 걸 적어 주세요. 예: `@봇 relay가 보내는 메시지 필드 알려줘`"
    )
    assert RESUME_FAILED_PREFIX == "이전 대화를 이어 가지 못해 새로 답해요."
    assert CODE_SOURCE_NOTE == (
        "ℹ️ 위키에 없던 내용이라 코드에서 찾았어요. 다음 위키 갱신 때 반영을 제안할게요."
    )
    assert TOO_LONG_TEXT == "질문이 너무 길어요. 4,000자 안으로 줄여서 다시 물어봐 주세요."
    assert PAUSED_TEXT == "지금은 관리자가 답변을 잠시 멈춰 두었어요. 나중에 다시 물어봐 주세요."
    assert DATA_BUSY_TEXT == "위키 사본을 준비하고 있어요. 잠시 뒤에 다시 물어봐 주세요."
    assert (
        TRUNCATED_NOTE == "(답이 너무 길어 뒷부분을 생략했어요. 질문을 좁혀서 다시 물어봐 주세요.)"
    )
    assert (
        SHUTDOWN_TEXT == "봇이 다시 시작되느라 답을 마치지 못했어요. 잠시 뒤에 다시 물어봐 주세요."
    )
    assert ADMIN_ONLY_TEXT == "이 명령은 관리자만 쓸 수 있어요."
    assert PAUSE_ACK_TEXT == "답변을 멈췄어요. `!재개`로 다시 시작할 수 있어요."
    assert RESUME_ACK_TEXT == "답변을 다시 시작했어요."


def test_all_reasons_and_failures_covered() -> None:
    reasons = typing.get_args(RejectReason)
    assert len(reasons) == 4
    texts = {rejection_text(r) for r in reasons}
    assert len(texts) == 4
    failures = typing.get_args(typing.get_type_hints(replies.failure_text)["failure"])
    assert set(failures) == {"timeout", "error", "empty", "max_turns"}
    assert all(isinstance(failure_text(f), str) and failure_text(f) for f in failures)

"""사용자에게 보이는 고정 문구(스펙 §8.6과 계획 3이 더한 문구, 글자 그대로)."""

from typing import Final, Literal

from team_wiki_engine.bot.limiter import RejectReason

USAGE_TEXT: Final = (
    "저를 멘션하고 궁금한 걸 적어 주세요. 예: `@봇 relay가 보내는 메시지 필드 알려줘`"
)
RESUME_FAILED_PREFIX: Final = "이전 대화를 이어 가지 못해 새로 답해요."
CODE_SOURCE_NOTE: Final = (
    "ℹ️ 위키에 없던 내용이라 코드에서 찾았어요. 다음 위키 갱신 때 반영을 제안할게요."
)
TOO_LONG_TEXT: Final = "질문이 너무 길어요. 4,000자 안으로 줄여서 다시 물어봐 주세요."
PAUSED_TEXT: Final = "지금은 관리자가 답변을 잠시 멈춰 두었어요. 나중에 다시 물어봐 주세요."
DATA_BUSY_TEXT: Final = "위키 사본을 준비하고 있어요. 잠시 뒤에 다시 물어봐 주세요."
TRUNCATED_NOTE: Final = "(답이 너무 길어 뒷부분을 생략했어요. 질문을 좁혀서 다시 물어봐 주세요.)"
SHUTDOWN_TEXT: Final = "봇이 다시 시작되느라 답을 마치지 못했어요. 잠시 뒤에 다시 물어봐 주세요."
ADMIN_ONLY_TEXT: Final = "이 명령은 관리자만 쓸 수 있어요."
PAUSE_ACK_TEXT: Final = "답변을 멈췄어요. `!재개`로 다시 시작할 수 있어요."
RESUME_ACK_TEXT: Final = "답변을 다시 시작했어요."

_REJECTIONS: Final[dict[RejectReason, str]] = {
    "user_busy": "앞 질문의 답을 아직 만들고 있어요. 답이 나온 뒤에 다시 물어봐 주세요.",
    "user_rate": "질문이 조금 빨라요. 1분 뒤에 다시 물어봐 주세요.",
    "daily_quota": "오늘 질문 한도를 다 썼어요. 내일 다시 물어봐 주세요.",
    "busy": "지금 다른 질문을 처리하고 있어요. 잠시 뒤에 다시 물어봐 주세요.",
}
_TIMEOUT_TEXT: Final = "답을 찾는 데 너무 오래 걸려서 멈췄어요. 질문을 좁혀서 다시 물어봐 주세요."
_FAILED_TEXT: Final = "답을 만들지 못했어요. 잠시 뒤에 다시 물어봐 주세요."


def rejection_text(reason: RejectReason) -> str:
    return _REJECTIONS[reason]


def failure_text(failure: Literal["timeout", "error", "empty", "max_turns"]) -> str:
    return _TIMEOUT_TEXT if failure == "timeout" else _FAILED_TEXT

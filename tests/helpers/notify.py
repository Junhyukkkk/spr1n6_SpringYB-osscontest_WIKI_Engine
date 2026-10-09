"""테스트용 운영 알림 가짜."""


class RecordingNotifier:
    """`Notifier`를 구현하고 보낸 알림을 `(본문, 관리자 멘션 여부)`로 기록한다."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, bool]] = []

    async def send(self, text: str, *, mention_admin: bool = False) -> None:
        self.sent.append((text, mention_admin))

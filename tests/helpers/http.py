"""테스트용 httpx2 클라이언트. 운영 배선과 같은 설정(`trust_env=False`, 리다이렉트 안 따라감)."""

from collections.abc import Callable, Coroutine

import httpx2

Handler = (
    Callable[[httpx2.Request], httpx2.Response]
    | Callable[[httpx2.Request], Coroutine[None, None, httpx2.Response]]
)


def mock_client(handler: Handler, *, follow_redirects: bool = False) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(
        transport=httpx2.MockTransport(handler),
        trust_env=False,
        follow_redirects=follow_redirects,
    )


def idle_client() -> httpx2.AsyncClient:
    """요청을 보내지 않는 테스트용(생성자 검사 등). 전송은 항상 실패한다."""

    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise AssertionError("요청을 보내면 안 됩니다")

    return mock_client(refuse)

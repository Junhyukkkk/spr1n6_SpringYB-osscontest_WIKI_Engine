# 기여 안내

## 개발 환경

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
```

커밋 전에 `bash scripts/smoke-offline.sh`가 통과해야 합니다.

## 코드 규칙

- `mypy --strict`, `ruff`(줄 길이 100)를 통과해야 합니다.
- 사용자에게 보이는 문구는 한국어, 이름은 snake_case.
- 비밀값을 로그·예외 메시지·테스트 출력에 싣지 않습니다.
- 외부 코드를 복사하지 않습니다.

## 테스트 규칙

- 기본 테스트는 오프라인입니다(네트워크·실제 Claude 차단). git은 임시 폴더의 실제 bare 저장소를 씁니다.
- 파일 시스템의 대소문자 구분이나 NFD 정규화에 기대지 않습니다.
- 실제 SDK 스모크(`LIVE_CLAUDE=1`)는 비용이 들며 키가 있는 사람만 돌립니다.

## SDK 고정 올리기

`claude-agent-sdk`는 정확한 버전으로 고정되어 있습니다. 올릴 때는 `pyproject.toml`과 `tests/helpers/live_gate.py`의 `PINNED_SDK`를 함께 바꾸고, 같은 버전으로 실제 SDK 스모크를 다시 통과한 기록(`deploy/README.md` §1)을 PR에 남깁니다.

## 배포 파일

`deploy/`의 유닛·스크립트·설정을 고치면 `tests/deploy`도 같이 바꿔야 합니다. 서버에 직접 적용하는 일은 별도 승인 뒤에만 합니다.

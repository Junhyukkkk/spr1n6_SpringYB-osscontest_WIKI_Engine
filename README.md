# team-wiki-engine

팀의 코드와 문서를 읽어 위키(`wiki/`)를 가꾸고, 디스코드에서 질문에 답하는 엔진입니다. 엔진은 이 저장소에, 팀의 문서와 위키는 별도의 데이터 저장소에 둡니다.

## 하는 일

- 코드 반영: 코드 저장소의 변경을 읽어 위키를 갱신합니다.
- 문서 반영: 데이터 저장소의 `raw/` 문서를 위키에 반영합니다.
- 질의응답: 디스코드에서 위키를 근거로 답합니다.

그 밖의 일(코드 수정, 임의 명령 실행 등)은 하지 않습니다.

## 구성

엔진 저장소(이 저장소)와 데이터 저장소를 나눕니다. 데이터에는 팀 내부 문서가 들어가므로 비공개입니다.

- `src/team_wiki_engine/`: `claude`(실행 계층), `config`, `git`, `github`, `metrics`, `ops`(알림·백업·점검), `sync`(동기화), `util`, `wiki`(점검)
- `skills/`: Claude에게 주는 스킬(`ask`, `ingest`, `sync-code`, `wiki-schema`). 패키지 데이터가 아니라 체크아웃 파일입니다.
- `deploy/`: systemd 유닛, 설치·업데이트 스크립트, 데이터 저장소 템플릿, 배포 안내서

## 요구 사항

- Python 3.12 이상, git 2.32 이상
- 리눅스(서버), macOS(개발)
- 네트워크: Anthropic, Discord, GitHub

## 설치(개발)

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
```

스킬이 체크아웃 파일이라 편집 설치(`-e`)를 씁니다.

## 설정

`config/team.example.json`을 복사해 채웁니다. 전달 방식은 기본 `delivery: local`(서버 안 bare 저장소)입니다. 환경변수:

- `TEAM_WIKI_HOME`: 작업 폴더
- `TEAM_WIKI_CONFIG`: 설정 파일 경로
- `TEAM_WIKI_ENGINE_DIR`: 엔진 체크아웃 경로
- 비밀값: `ANTHROPIC_API_KEY`, `DISCORD_TOKEN`, `GITHUB_TOKEN`

## 실행

| 명령 | 하는 일 | 종료 코드 |
| --- | --- | --- |
| `team-wiki-sync` | 코드 반영 · 문서 반영 | 0 성공 / 1 단계 실패·실행 오류 / 2 사용·설정 오류 |
| `team-wiki-pull` | 봇용 `data` 사본 갱신 | 0 성공·건너뜀 / 1 git 실패 / 2 사용·설정 오류 |
| `team-wiki-bot` | 디스코드 봇 | 0 정지 / 1 실행 오류 / 2 설정 오류·토큰 거부 |
| `team-wiki-lint` | 위키 점검 | 0 통과 / 1 문제 있음 / 2 사용 오류 |
| `team-wiki-notify` | systemd 실패 알림 | 0 성공 / 2 사용 오류 |
| `team-wiki-backup` | 상태 백업 | 0 성공 / 1 실패 / 2 사용 오류 |
| `team-wiki-preflight` | 서버 오프라인 점검 | 0 통과 / 1 실패 있음 / 2 사용 오류 |

## 테스트

```bash
bash scripts/smoke-offline.sh
```

기본 테스트는 네트워크와 실제 Claude를 막습니다. 실제 SDK 스모크는 `LIVE_CLAUDE=1`일 때만 돌며 비용이 발생합니다.

## 배포

[배포 안내서](deploy/README.md)를 따릅니다. 서버 단계는 사용자 승인 뒤에만 실행합니다.

## 보안

- 도구 목록을 제한하고 `disallowed_tools`로 막습니다.
- 경로 판정과 `PreToolUse` 훅이 읽기·쓰기를 검사합니다(오류가 나면 거부).
- Claude에는 토큰을 지운 환경을 넘깁니다.
- 실행 뒤 git 사후 검사로 허용되지 않은 변경을 걸러냅니다.
- 서비스는 systemd 샌드박스 안에서 돕니다.

취약점 신고는 [SECURITY.md](SECURITY.md)를 보세요.

## 기여

[CONTRIBUTING.md](CONTRIBUTING.md)를 보세요. 저장소 주소 예시는 `https://github.com/your-org/` 자리표시자입니다.

## 라이선스

[LICENSE](LICENSE)(MIT). 짧고 허용적이며, 의존성(`claude-agent-sdk`·pydantic·mcp·anyio는 MIT, httpx2는 BSD-3-Clause, PyYAML은 MIT)과 충돌하지 않고, 팀이 그대로 가져다 쓰기 쉽기 때문입니다. 동봉 CLI는 이 저장소가 재배포하지 않으며 설치 때 PyPI에서 받습니다.

# 팀 위키 데이터 저장소

팀 위키 엔진이 읽고 쓰는 데이터 저장소입니다.

## 사람이 하는 일

- `raw/`에 원본 문서를 `YYYY-MM-DD-제목.md` 이름으로 넣습니다.
- `wiki/`·`baseline.json`·`wiki/log.md`·`metrics/`는 봇(동기화)이 씁니다. 사람은 직접 고치지 않고 PR 검토로 고칩니다.

## 동기화 PR

- 동기화 PR은 `sync/code-…`·`sync/ingest-…` 브랜치에서 올라옵니다.
- `wiki-lint` 검사를 통과해야 머지합니다. 동기화 PR이 `wiki/`·`baseline.json`·`metrics/` 밖의 파일을 바꾸면 검사가 실패합니다.

## 저장소 변수

`wiki-lint` 점검이 돌려면 저장소 변수 두 개를 설정해야 합니다.

- `TEAM_WIKI_ENGINE_REPO`: 엔진 저장소의 https 주소
- `TEAM_WIKI_ENGINE_REF`: 엔진의 태그 또는 커밋

비밀값은 쓰지 않습니다.

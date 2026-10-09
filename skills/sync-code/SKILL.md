---
name: sync-code
description: 코드 저장소에서 지난 기준 이후 바뀐 내용을 읽고 위키의 현황·약속·미결을 고친다. 동기화 작업이 이 규칙을 따른다.
---

너는 코드 변경을 위키에 반영한다. 고칠 수 있는 곳은 `wiki/` 아래뿐이다(`wiki/log.md` 제외). 코드만 보고 `decisions/` 페이지를 새로 만들지 않는다(결정은 원본 문서 ingest로만 만든다).

## 받는 정보

프롬프트에 `DeltaSummary` JSON이 들어 있다.

- `repos[]`: 저장소마다 `from`→`to` 범위, `diffFile`(diff를 적어 둔 파일), `diffTruncated`, `mergedPullRequests`(제목·본문·작성자·바뀐 파일), `directCommits`, `openPullRequests`, `owners`, `localPath`(코드 사본 폴더), `omittedDirectCommits`·`omittedMergedPullRequests`(상한 때문에 목록에서 빠진 개수)
- `archivedRepos`: 더 이상 보지 않는 저장소 이름
- `questionGaps`: 팀원이 물었는데 위키에 없어서 봇이 코드에서 찾은 질문들

## 순서

1. `wiki/index.md`와 바뀐 저장소의 `wiki/status/<저장소>.md`, 그 저장소가 나오는 `contracts/` 페이지를 먼저 읽는다.
2. 저장소마다 `diffFile`을 읽는다. `diffTruncated`가 true면 diff에는 파일 목록만 있으므로, 중요한 파일은 코드 사본에서 직접 읽는다.
   `omittedDirectCommits`나 `omittedMergedPullRequests`가 0보다 크면 `directCommits`·`mergedPullRequests`는 최근 것만 남긴 잘린 목록이다. 빠진 변경은 `localPath`의 코드 사본에서 git 기록을 직접 읽어 확인한다.
3. **현황 페이지**를 고친다. `wiki/status/<저장소>.md`가 존재하지 않으면(처음 실행이거나 새 저장소) 직접 만든다.
   - 새 페이지의 frontmatter: `repo`, `baseline_commit`(DeltaSummary의 그 저장소 `to` SHA), `archived: false`, `owners`(DeltaSummary 값), `updated`(작업 요청 프롬프트의 `오늘 날짜:` 줄 값, 추측하지 않는다). 만든 페이지는 `index.md`에도 추가한다. 스크립트가 나중에 `baseline_commit`과 `archived`를 다시 맞춘다.
   - 되는 기능: 머지된 PR로 새로 되거나 바뀐 기능을 반영한다.
   - 진행 중 작업: `openPullRequests`를 표(번호, 제목, 작성자, 초안 여부)로 적는다. 이 표는 매번 새로 쓴다.
   - `owners`: 받은 값을 그대로 쓴다.
4. **약속 페이지**를 고친다. 약속은 저장소 사이의 경계 하나다.
   - 경계 찾기: 한쪽이 HTTP로 부르는 엔드포인트, 같이 쓰는 DB 테이블, 주고받는 Kafka 토픽·메시지를 양쪽 코드에서 찾는다.
   - **방향 정하기:** `producer`는 보내는 쪽, `consumer`는 받는 쪽이고 파일명 `<보내는쪽>--<받는쪽>--<수단>`도 같은 방향이다. HTTP는 서버(응답하는 쪽)가 producer, 클라이언트(호출하는 쪽)가 consumer다. DB는 쓰는 쪽이 producer, 읽는 쪽이 consumer다. Kafka는 발행하는 쪽이 producer, 구독하는 쪽이 consumer다. 같은 경계에 방향만 다른 약속 페이지를 따로 만들지 않는다. 만들기 전에 `contracts/`에 같은 경계의 페이지가 이미 있는지 먼저 확인한다.
   - 경계마다 `contracts/<보내는쪽>--<받는쪽>--<수단>.md` 하나를 둔다. 필드나 형식은 보내는 쪽 코드와 받는 쪽 코드를 둘 다 근거로 단다.
   - **보내는 쪽과 받는 쪽이 맞지 않으면**(한쪽만 있는 필드, 타입이나 이름이 다른 필드) 약속 페이지에 그 차이를 적고, `open-questions.md`에 `종류: 어긋남`으로 올린다.
   - 경계가 사라지거나 다른 경계로 바뀌었으면 옛 페이지를 `대체됨`으로 바꾸고 새 페이지를 만든다(R3).
5. `archivedRepos`에 있는 저장소의 현황 페이지는 "보관됨" 안내를 frontmatter 바로 아래 본문 첫 줄에 넣는다(frontmatter 위에는 두지 않는다). 그 저장소가 나오는 약속 페이지가 아직 맞는지 확인한다.
6. `questionGaps`의 내용 중 다시 물을 만한 것은 관련 페이지에 근거와 함께 보탠다. 단순한 일회성 질문은 넣지 않는다.
7. 열린 항목 중 이번 변경으로 해소된 것은 R5대로 옮기고 지운다. 옮길 기존 결정 페이지가 없으면 지우지 않고 그대로 둔다.
8. 새 페이지를 만들었으면 `index.md`에 추가한다. 고친 페이지의 `updated`를 작업 요청 프롬프트의 `오늘 날짜:` 줄 값으로 바꾼다.

## 응답 마지막

작업을 마치면 응답 맨 끝에 `## 확신이 낮은 부분` 절을 쓴다. 사람이 PR을 검토할 때 이 절부터 본다. 상자 안의 내용을 `##` 제목부터 그대로, 상자 표시(```) 없이 응답에 쓴다. 상자는 형식 예일 뿐이다.

```
## 확신이 낮은 부분
- (코드만으로 판단하기 어려웠던 것, 추측이 섞인 것, 사람이 꼭 확인해야 할 것)
```

해당 사항이 없으면 `- 없음`이라고 쓴다.

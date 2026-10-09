# 배포 안내서

**이 문서의 서버 단계는 사용자가 배포를 승인한 뒤에만 실행한다.**
**승인 전에는 이 저장소에서 파일을 만들고 맥에서 검증하는 데까지만 한다(오프라인 테스트·DRY_RUN). GitHub 원격 추가·push도 승인 뒤의 일이다.**

오라클 Always Free A1(Ubuntu 24.04 aarch64) 한 대에 엔진을 올리는 순서다. 아래 명령은 모두 서버 또는 맥에서 사람이 실행한다. 이 저장소의 테스트는 파일 내용만 확인하며 아무것도 설치하거나 시작하지 않는다.

## 0. 한눈에 보기

유닛 6개. 자동으로 도는 것은 봇(상시)과 동기화(타이머) 둘뿐이다. 타이머는 동기화 하나뿐이며, `data` 사본은 봇이 `data.lock`을 쥐고 10분마다 스스로 갱신하므로 pull은 손으로만 부른다.

| 이름 | 언제 | 무엇 | 실패하면 |
| --- | --- | --- | --- |
| `team-wiki-bot.service` | 자동(상시, 부팅 때 enable) | 디스코드 봇. `Type=notify`, 워치독 120초 | 자동 재시작. 10분에 10회를 넘으면 실패 상태가 되어 알림(간격 30초: pull·동기화가 `data.lock`을 몇 분 쥐면 봇이 락 대기 30초 뒤 종료하므로, 짧은 간격·적은 횟수면 한도가 먼저 소진됨). 종료 코드 2(설정·토큰)는 재시작하지 않고 알림 |
| `team-wiki-sync.service` | `team-wiki-sync.timer`가 하루 두 번(09:00·21:00 KST) | 코드 반영 · 문서 반영 | 종료 코드 1은 프로그램이 이미 알렸으므로 성공으로 치고, 그 밖의 실패는 실패 알림 |
| `team-wiki-sync.timer` | 자동(enable) | 동기화 일정의 정본. 놓친 실행은 켜진 뒤 한 번 | 해당 없음 |
| `team-wiki-pull.service` | 손으로만 | 봇용 `data` 사본 갱신(첫 설치, 봇이 꺼져 있을 때). 봇이 돌면 "건너뜀"으로 성공 | 실패 알림 |
| `team-wiki-notify@.service` | 다른 유닛의 `OnFailure` | 실패한 유닛 이름을 운영 채널에 알림 | 알림이 실패해도 다시 알리지 않음 |
| `team-wiki-smoke.service` | 손으로만 (비용 발생) | 서비스와 같은 샌드박스에서 실제 SDK 보안 스모크 | 로그로 확인 |

| 경로 | 소유자 | 권한 | 내용 |
| --- | --- | --- | --- |
| `/opt/team-wiki-engine` | root:root | 0755 | 엔진 체크아웃과 `.venv` (서비스 사용자는 고칠 수 없음) |
| `/srv/team-wiki` | teamwiki:teamwiki | 0750 | 작업 폴더 `TEAM_WIKI_HOME` |
| `/srv/team-wiki/home` | teamwiki | 0700 | 서비스 사용자 홈(CLI 세션) |
| `/etc/team-wiki/team.json` | root:teamwiki | 0640 | 설정 |
| `/etc/team-wiki/secrets.env` | root:root | 0600 | 비밀값 |
| `/var/backups/team-wiki` | root | 0700 | 상태 백업 |
| `/var/lib/team-wiki-deploy` | root | 0700 | 배포 기록(직전 커밋) |
| `/etc/systemd/system` | root | - | 유닛 |

서비스 사용자는 `teamwiki`(시스템 사용자, sudo 없음, 셸 `/usr/sbin/nologin`)다.

비밀값이 흐르는 길: `secrets.env`(root만 읽음) → systemd가 읽어 서비스의 환경변수로 넘김 → 러너가 CLI에 넘길 때 `DISCORD_TOKEN`·`GITHUB_TOKEN`을 빈 값으로 덮어씀. 서비스 사용자는 파일을 직접 읽을 수 없다.

메모리 예산(12GB 서버, OS·페이지 캐시 몫 4GiB 이상 남김):

| 유닛 | `MemoryMax` |
| --- | --- |
| 봇 | 3G |
| 동기화 | 3G |
| pull | 512M |
| 알림 | 256M |
| 합계(동시에 돌 수 있는 것) | 6.75G (상한 8GiB) |

스모크는 손으로만 돌리고 `MemoryMax`가 3G다. 동기화와 동시에 돌리지 않는다.

## 1. 배포 전 관문

반드시 이 순서로 하고, 하나라도 실패하면 멈춘다. 오프라인 관문과 라이브 관문의 완료 기준이기도 하다.

1. 맥: `bash scripts/smoke-offline.sh` 통과.
2. 맥 (비용 발생): `LIVE_CLAUDE=1 ANTHROPIC_API_KEY=<키> .venv/bin/python -m pytest -m live -q -rP` — 라이브 테스트 전부 통과. 테스트를 약화하지 말고, 실패하면 `[smoke]` 로그를 보고한다. 결과는 아래 기록 표에 남긴다.
3. 미검증 SDK 가정 표(아래).
4. 맥 (비용 발생) — 2가 통과한 뒤에만: `bash deploy/init-data-repo.sh "$PWD/.local/team-wiki-data.git"` → `cp config/team.example.json .local/team.json`(`repos` URL을 실제 공개 HTTPS 주소로, `dataRepo.url`을 위 절대 경로로) → `TEAM_WIKI_HOME="$PWD/.local/home" TEAM_WIKI_CONFIG="$PWD/.local/team.json" ANTHROPIC_API_KEY=<키> GITHUB_TOKEN=<공개 저장소 읽기 전용 토큰> .venv/bin/team-wiki-sync --only code` → `.venv/bin/team-wiki-lint .local/home/data-sync`. 기대 출력과 결과를 기록한다.
5. 맥 (비용 발생) — 2가 통과한 뒤에만: 디스코드 테스트 서버에서 봇을 손으로 확인한다(`.venv/bin/team-wiki-bot`). 결과를 기록한다.
6. 서버(승인 뒤): §4~§6을 마치고 `team-wiki-smoke.service`가 통과해야 §7로 간다.
7. SDK 고정을 올릴 때: `tests/helpers/live_gate.py`의 `PINNED_SDK`와 `pyproject.toml`을 함께 바꾸고 2와 6을 다시 통과한 기록을 남긴 뒤에만 서버를 업데이트한다.

미검증 SDK 가정 표:

| 가정 | 확인하는 라이브 테스트 | 실패/관찰 시 할 일 |
| --- | --- | --- |
| system/init 모양 | `test_cli_init_message_exposes_only_profile_tools_and_no_mcp_servers` | 도구 목록이 프로필과 다르면 배포를 멈추고 보고 |
| 도구 결과 스트림 모양 | `test_x1_cannot_read_a_file_outside_the_roots` (+ 모든 읽기 거부 테스트의 `assert_tool_results_seen`) | 결과가 안 보이면 거부를 확인할 수 없으므로 멈춤 |
| `~` 펼치기 | `test_tilde_path_is_not_expanded_to_search_outside` | 실패하면 경로 판정을 고친 뒤 다시 |
| 자동 메모리 경로 · `CLAUDE_CODE_DISABLE_AUTO_MEMORY` | `test_sync_profile_cannot_write_claude_internal_paths`, `test_auto_memory_is_not_written_when_disabled` | 쓰기가 보이면 멈춤 |
| `Read(**/.env)` 규칙 문법 | `test_disallowed_read_dotenv_rule_is_enforced_by_the_cli` | 규칙이 안 먹으면 멈춤 |
| `message_id` usage 중복 제거 | `test_usage_is_deduplicated_by_message_id` | 비용 집계가 어긋나므로 고친 뒤 다시 |
| 타임아웃 뒤 훅 우회 창 | `test_short_timeout_is_classified_as_timeout_and_returns_in_time`, `test_writes_after_timeout_settle_within_the_drain_window` | `[smoke] timeout 관찰` 줄을 기록. 정리 대기 안에 안 끝나면 멈춤 |
| 예외를 던지는 훅 | `test_observe_how_a_raising_hook_is_treated_by_the_sdk` | 정보용. `[smoke] 훅 예외 관찰` 줄을 기록 |
| SDK·CLI 버전 고정 | `test_sdk_and_cli_versions_are_the_pinned_ones` | `[smoke] 버전` 줄을 기록. 다르면 멈춤 |
| 보안 기본(X1~X4)과 확장 | `test_x1_cannot_read_dotenv_inside_the_roots`, `test_x2_cannot_see_env_tokens`, `test_x3_cannot_run_shell_commands`, `test_x4_cannot_write_outside_the_write_roots`, `test_control_can_write_inside_the_write_root`, `test_cannot_read_session_transcripts`, `test_hook_and_can_use_tool_instrumentation`, `test_glob_dotdot_pattern_does_not_reveal_outside_names`, `test_leading_space_path_is_not_trimmed_into_an_allowed_path`, `test_max_turns_is_classified_as_max_turns`, `test_broken_symlink_in_write_root_cannot_create_its_outside_target` | 하나라도 실패하면 멈춤 |

기록 표(실행할 때마다 한 줄 추가):

| 날짜 | SDK | CLI 버전 | 통과 수 | 총비용 | `[smoke] 훅 예외 관찰` | `[smoke] timeout 관찰` | `[smoke] 버전` |
| --- | --- | --- | --- | --- | --- | --- | --- |
|  |  |  |  |  |  |  |  |

## 2. 오라클 준비

- 인스턴스: `VM.Standard.A1.Flex` 2 OCPU / 12GB, Ubuntu 24.04 aarch64, 부트 볼륨 50GB.
- 인기 리전은 A1 용량이 부족할 수 있다. 시간을 두고 다시 시도하거나 다른 가용 도메인을 고른다.
- Pay As You Go로 전환하고 예산 알림을 켠다. Always Free 인스턴스는 7일 동안 CPU·네트워크·메모리 사용률이 낮으면 회수 대상이 되는데, 봇은 대부분 대기라 걸리기 쉽다. 무료 한도 안에서는 요금이 없다.
- 보안 목록은 들어오는 22/tcp만 연다. SSH 키 인증만 쓴다.
- 부트 볼륨 백업 정책(무료 한도 안의 주기 백업)을 권장한다. 상태 백업이 같은 디스크에 있기 때문이다.
- 자동 보안 업데이트(`unattended-upgrades`)는 Ubuntu 기본값을 유지한다.

## 3. 저장소와 토큰

엔진(공개 예정)과 데이터(비공개)를 나눈다. 데이터에는 팀 내부 문서가 들어가기 때문이다.

엔진을 서버로 옮기는 두 방법:

- (a) GitHub에 올리지 않은 채 git 번들. 맥에서 `git -C <엔진 저장소> bundle create /tmp/team-wiki-engine.bundle main` → `scp`로 서버에 복사 → 서버에서 `ENGINE_REPO=/tmp/team-wiki-engine.bundle`.
- (b) 사용자가 공개를 승인한 뒤 GitHub 주소.

데이터 저장소는 처음엔 서버 안 bare 저장소(`local` 모드)로 시작한다. GitHub 비공개 저장소로 옮기는 것은 §8이다.

GitHub fine-grained 토큰: 대상은 데이터 저장소 하나, Contents 읽기·쓰기, Pull requests 읽기·쓰기, Metadata 읽기. SSH 배포 키는 쓰지 않는다(엔진은 `https`만 허용한다). 토큰을 URL·`team.json`·명령줄에 넣지 않고, 자격 증명 도우미를 설정하지 않는다. git 전역 설정은 필요 없고 있어도 엔진이 무시한다(`GIT_CONFIG_GLOBAL=/dev/null`, `GIT_CONFIG_NOSYSTEM=1`).

Anthropic은 전용 워크스페이스와 API 키를 만들고 월 지출 한도를 건다. 디스코드 봇은 Message Content Intent를 켜고, View Channels · Send Messages · Send Messages in Threads · Create Public Threads · Read Message History 권한을 준다.

## 4. 설치

사용자가 승인한 뒤 서버에서 실행한다.

```bash
git clone /tmp/team-wiki-engine.bundle /tmp/twe
sudo DRY_RUN=1 ENGINE_REPO=/tmp/team-wiki-engine.bundle bash /tmp/twe/deploy/setup.sh
sudo ENGINE_REPO=/tmp/team-wiki-engine.bundle bash /tmp/twe/deploy/setup.sh
sudo -u teamwiki bash /opt/team-wiki-engine/deploy/init-data-repo.sh /srv/team-wiki/team-wiki-data.git
```

첫 줄은 번들 대신 GitHub 주소여도 된다. 둘째 줄이 `DRY_RUN=1` 확인이고, 출력(`+ 명령`)을 읽은 뒤 셋째 줄을 실행한다. 설치는 enable만 하고 어떤 서비스도 시작하지 않는다. 여러 번 실행해도 `/etc/team-wiki`의 채운 파일은 덮어쓰지 않는다.

## 5. 설정 채우기

```bash
sudoedit /etc/team-wiki/team.json
sudoedit /etc/team-wiki/secrets.env
sudo stat -c '%U:%G %a %n' /etc/team-wiki/*
```

- `team.json`은 local 예시(`deploy/config/team.local.json`) 그대로 시작해 `repos`를 실제 주소로, `discord`를 실제 ID들로 바꾼다.
- `secrets.env`에는 `ANTHROPIC_API_KEY`, `DISCORD_TOKEN`, `GITHUB_TOKEN` 세 값을 따옴표 없이 쓴다.
- 기대하는 권한: `root:teamwiki 640 /etc/team-wiki/team.json`, `root:root 600 /etc/team-wiki/secrets.env`.
- 프록시는 지원하지 않는다. HTTP 클라이언트는 환경 프록시를 읽지 않는데 git은 넘겨받아 두 경로가 다르게 나가기 때문이다. 프록시 변수가 있으면 서버 점검이 실패한다.

| | `delivery: local` | `delivery: github` |
| --- | --- | --- |
| 데이터 저장소 | 서버 안 bare 저장소(절대 경로) | GitHub 비공개 저장소(`https`) |
| 문서 반영 | 저장소에 바로 반영 | `sync/` 브랜치 PR |
| `GITHUB_TOKEN` | 선택(공개 저장소 읽기 한도용) | 필수 |
| 예시 | `deploy/config/team.local.json` | `deploy/config/team.github.json` |

## 6. 서버 점검과 샌드박스 스모크

사용자가 승인한 뒤 서버에서 실행한다.

```bash
sudo systemd-analyze verify /etc/systemd/system/team-wiki-bot.service /etc/systemd/system/team-wiki-sync.service /etc/systemd/system/team-wiki-pull.service /etc/systemd/system/team-wiki-smoke.service /etc/systemd/system/team-wiki-sync.timer /etc/systemd/system/team-wiki-notify@.service
systemd-analyze security team-wiki-bot.service
systemd-analyze calendar '*-*-* 09,21:00:00 Asia/Seoul'
sudo systemctl start team-wiki-pull.service && journalctl -u team-wiki-pull.service -n 50 --no-pager
```

`verify`의 기대값은 경고 0이다. `security`의 노출 점수는 기록한다. `pull`은 점검과 첫 `data` 사본을 겸한다.

샌드박스 스모크(비용 발생): 서비스와 같은 샌드박스에서 실제 SDK 스모크를 돈다.

```bash
sudo systemctl start team-wiki-smoke.service; journalctl -u team-wiki-smoke.service -o cat --no-pager | grep -E '^\[smoke\]|passed|failed'
```

전부 통과해야 하고 결과를 기록한다. 실패 진단 표(하나씩만 풀고 다시 스모크하고, 푼 키와 이유를 기록한다):

| 증상 | 의심 키 | 확인 방법 |
| --- | --- | --- |
| "Read-only file system" | 빠진 `ReadWritePaths` | 로그의 경로가 `/srv/team-wiki` 아래인지 |
| CLI가 시작하자마자 죽음(SIGSEGV/SIGILL) | `MemoryDenyWriteExecute`가 켜졌는지, 아키텍처 | 유닛에 그 키가 없는지, 점검의 `bundled_cli` |
| 이름 풀이 실패 | `RestrictAddressFamilies` (`AF_NETLINK` 포함) | 유닛의 값 |
| "Operation not permitted", 네임스페이스 | `RestrictNamespaces` | 로그의 시스템 호출 |
| `/dev` 관련 | `PrivateDevices` | 로그의 장치 경로 |

systemd 동작 확인 목록:

- 실패 알림 시험: `sudo systemctl start team-wiki-notify@team-wiki-test.service` — 운영 채널에 관리자 멘션 알림이 1건 온다.
- 점검 실패가 알림을 부르는지: `sudo mv /etc/team-wiki/team.json /etc/team-wiki/team.json.bak` → `sudo systemctl start team-wiki-pull.service` → 실패와 알림 확인 → `sudo mv /etc/team-wiki/team.json.bak /etc/team-wiki/team.json`.
- 봇 자동 재시작 중에는 알림이 오지 않고, 재시작 한도(10분 10회)에 닿을 때 한 번 오는지.
- 봇이 `READY=1` 뒤에만 `active`가 되는지: `systemctl show -p ActiveState,SubState team-wiki-bot.service`.
- 봇이 도는 동안 `sudo systemctl start team-wiki-pull.service`가 "건너뜀"으로 성공하는지.
- `systemd-tmpfiles --clean`이 `state/runs`만 정리하는지.

## 7. 시작과 확인

사용자가 승인한 뒤 서버에서 실행한다.

```bash
sudo systemctl start team-wiki-pull.service
sudo systemctl start team-wiki-bot.service team-wiki-sync.timer
systemctl list-timers 'team-wiki-*'
journalctl -u team-wiki-bot.service -f
```

봇도 시작 때 `data` 사본을 갱신하지만, 먼저 pull로 만들어 두면 실패를 따로 보기 쉽다.

동기화를 바로 한 번 돌려 보려면 다음을 실행한다(비용 발생).

```bash
sudo systemctl start team-wiki-sync.service   # (비용 발생)
```

정지 확인(선택): 동기화 도중 `sudo systemctl stop team-wiki-sync.service`를 하고, journal에 `SIGKILL`이 없는지와 다음 실행이 정상인지 본다.

## 8. local 모드에서 github 모드로

**사용자가 GitHub 업로드를 승인한 뒤에만 한다.**

1. 비공개 데이터 저장소를 만든다.
2. 서버 bare 저장소의 `main`을 그 저장소로 push한다(사람이 하고, 토큰은 일회용 환경변수로).
3. 저장소 변수 `TEAM_WIKI_ENGINE_REPO`(엔진 공개 주소)와 `TEAM_WIKI_ENGINE_REF`(태그 또는 커밋)를 설정한다.
4. `main` 보호 규칙에 `wiki-lint`를 필수로 건다.
5. `team.json`의 `delivery`와 `dataRepo.url`을 바꾼다(`deploy/config/team.github.json` 참고).
6. `secrets.env`의 `GITHUB_TOKEN`을 채운다.
7. `sudo systemctl restart team-wiki-bot.service`(동기화·pull은 매번 설정을 새로 읽는다).
8. 확인: `journalctl -u team-wiki-bot.service -n 50 --no-pager` — 봇의 시작 갱신이 GitHub에서 받았는지.

전환 전에 결정할 것: 두 종류 PR(코드 반영·문서 반영)의 머지 충돌(`wiki/log.md`, `baseline.json`)을 어떻게 처리할지.

## 9. 운영

- 업데이트: `sudo bash /opt/team-wiki-engine/deploy/update.sh` (번들이면 `ENGINE_REMOTE=<번들 경로>`). 먼저 `DRY_RUN=1`로 본다. 동기화·pull이 실행 중이면 거부한다.
- 되돌리기: `sudo bash /opt/team-wiki-engine/deploy/update.sh --rollback`.
- 백업: 업데이트 때마다 만들어지고, 원하면 `sudo /opt/team-wiki-engine/.venv/bin/team-wiki-backup`. 위치는 `/var/backups/team-wiki`. 복원은 `tar -xzf <백업> -C /tmp/restore` 후 필요한 파일만 `install -o teamwiki -g teamwiki -m 0600`으로 옮긴다.
- 로그 한도: journald 1G · 3개월. `state/runs`는 30일 뒤 정리. 측정 기록(`state/*.jsonl`)은 돌려쓰지 않는다.
- 동기화 시각 변경: 타이머 파일(정본)과 `team.json`의 `schedule`을 함께 바꾼다.
- 첫 2주 뒤 `reviewMode` 전환을 판단하고 결정 기록(ADR)으로 남긴다.
- 실패 알림은 `OnFailure`가 있는 유닛이 실패 상태가 될 때 관리자 멘션으로 온다. 봇이 자동 재시작하는 동안에는 오지 않는다.
- 봇이 꺼져 있을 때 `data` 사본을 맞추려면 `sudo systemctl start team-wiki-pull.service`.
- SDK 고정을 올리는 절차는 §1의 7.

## 10. 문제 해결

서버 점검(`team-wiki-preflight`, 모든 서비스가 시작 전에 돌린다)은 `[preflight] 실패 <이름>: <이유>` 줄로 말한다. 손으로 보려면 `sudo -u teamwiki /opt/team-wiki-engine/.venv/bin/team-wiki-preflight --role sync`.

| 점검 | 조치 |
| --- | --- |
| `timezone` | `sudo apt install tzdata` |
| `git` | git 2.32 이상 필요(Ubuntu 24.04는 2.43). `sudo apt install git` |
| `sdk_pin` | 설치된 SDK가 `pyproject.toml` 고정과 다름. `update.sh`를 다시 실행하거나 §1의 7 |
| `bundled_cli` | 동봉 CLI가 없거나 실행 권한/아키텍처가 다름. `pip install -e` 다시, 서버가 aarch64인지 확인 |
| `skills` | 스킬 4개가 엔진 체크아웃에 있는지. 체크아웃이 깨졌으면 `update.sh --rollback` |
| `config` | `/etc/team-wiki/team.json` 형식·전달 방식 규칙. 메시지의 항목을 고친다 |
| `secrets` | 역할에 필요한 비밀값이 비었음(값은 출력되지 않는다). `secrets.env`를 채운다 |
| `local_fs` | `state`가 네트워크 파일시스템. 로컬 디스크로 옮긴다 |
| `proxy` | 프록시 변수가 있음. 지원하지 않으므로 제거한다 |
| 작업 폴더 권한 | `sudo systemd-tmpfiles --create`로 소유자·권한을 다시 맞춘다 |
| `disk` | 여유 공간 부족. 아래 참고 |

남은 `.git/*.lock`: 동기화·pull은 오래된 잠금을 스스로 회수한다. 그래도 남으면 해당 유닛이 실행 중이 아님을 확인한 뒤(`systemctl is-active`) 사람이 지운다.

디스크 부족: `journalctl --disk-usage`, `/var/backups/team-wiki`의 오래된 백업, `/srv/team-wiki/state/runs`를 확인한다.

## 11. 되돌리기와 제거

- 직전 엔진으로: `sudo bash /opt/team-wiki-engine/deploy/update.sh --rollback`.
- 완전 제거:
  1. `sudo systemctl disable --now team-wiki-bot.service team-wiki-sync.timer`
  2. `/etc/systemd/system`의 `team-wiki-*` 유닛, `/etc/tmpfiles.d/team-wiki.conf`, `/etc/systemd/journald.conf.d/team-wiki.conf`를 지운다.
  3. `sudo systemctl daemon-reload`
  4. 데이터 보존 여부를 정한다(`/srv/team-wiki`, `/var/backups/team-wiki`).
  5. 마지막에 `sudo userdel teamwiki`.

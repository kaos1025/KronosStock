# KronosStock 기존 운영 경로·백업·롤백 인벤토리

작성 기준: 2026-07-31 21:58 KST
조사 범위: read-only 관찰 및 소스 inspection만 수행. systemd timer 중지/수정, 서비스 재시작, 파일 삭제, 외부 주문/broker API 호출, 배포는 수행하지 않음.

## 1. 현재 운영 요약

KronosStock 운영은 현재 systemd timer가 `/srv/kronostock/dry-run-once.sh` wrapper를 실행하고, wrapper가 `/srv/agent-workspaces/KronosStock` checkout의 `.venv`와 `.env`를 사용해 다음 dry-run 파이프라인을 1회 수행하는 구조다.

1. `.venv/bin/activate` 후 `.env`를 shell 환경으로 로드
2. `inference.kr_data_fetcher.fetch_watchlist(days=settings.forecast_lookback_days)`
3. 선택된 market data provider에서 OHLCV read-only 수집
4. Redis `kronos:stock:ohlcv:daily:<code>`에 OHLCV JSON 저장, TTL 7일
5. `bot.scheduler.run_dry_run_cycle(send_alert=True, persist_portfolio=True)`
6. Redis OHLCV → Kronos-mini CPU 예측 → Redis `kronos:stock:forecast:daily:<code>`에 forecast JSON 저장, TTL 3일
7. deterministic signal 생성 → Redis `kronos:stock:paper:portfolio`에 paper portfolio snapshot 저장, TTL 없음

위 항목은 조사 시점의 기존 운영 사실을 기록한 것이며 신규 K-Semiconductor Flow Desk 설계 승인이 아니다. 신규 decision/scoring/AI 경로는 Kronos 예측·예상수익률·밴드·불확실성을 읽거나 전달하지 않는다. 기존 forecast와 signal은 historical archive/evaluation 및 rollback 조사 목적으로만 보존한다.

신규 판단·입력·점수·AI·T+1/T+5/T+20 정산 원장은 Redis cache TTL과 무관한 영구 no-TTL append-only 저장소를 사용해야 한다. canonical timestamp는 `as_of_kst`, `source_as_of`, `ingested_at_kst`, `available_data_cutoff`이며 cutoff 이후 관측값을 과거 판단에 주입하지 않는 no-look-ahead 테스트가 필수다.
8. Telegram digest 전송

코드 inspection 기준으로 이 경로는 paper trading만 수행한다. `bot/scheduler.py`는 “실제 KIS 주문 endpoint 또는 broker API는 호출하지 않는다”고 명시하고, wrapper는 `run_dry_run_cycle(send_alert=True, persist_portfolio=True)`만 호출한다. 검색 결과 주문 관련 코드는 `strategy/paper_trader.py`의 paper order와 테스트/포맷터에 국한되어 있었다.

## 2. 관찰된 호스트·런타임 상태

- Host: Linux `srv1756197`, kernel `6.8.0-124-generic`, Ubuntu 계열
- 실행 사용자: `deploy:deploy`, uid/gid 1000
- 기준 시각: `2026-07-31T21:58:22+09:00`
- uptime: 46일 22시간+
- 메모리: 7.8GiB total, 6.7GiB used, 1.1GiB available
- swap: 없음
- 디스크 `/`: 96G total, 25G used, 72G available, 26% used
- Redis listener: `127.0.0.1:6379`, `[::1]:6379`
- Redis `PING`: `PONG`
- Redis DB size: 20 keys

리스크: swap이 없어 모델 추론/동시 agent/build 부하가 겹치면 OOM 여지가 있다. 지금 조사 시점에는 디스크 여유는 충분하다.

## 3. Git·파일 경로 인벤토리

### 3.1 애플리케이션 checkout

- path: `/srv/agent-workspaces/KronosStock`
- owner/mode: `deploy:deploy`, `drwxrwxr-x`
- branch: `main`
- local HEAD: `0530f36bbd1d47fb495ed8779663f1d9d8e446cd`
- local `origin/main`: `0530f36bbd1d47fb495ed8779663f1d9d8e446cd`
- remote `origin/main`: `0530f36bbd1d47fb495ed8779663f1d9d8e446cd`
- tracked status before this document: clean except untracked `docs/` directory from current PM docs work
- remote: `https://github.com/kaos1025/KronosStock.git`

### 3.2 wrapper root

- path: `/srv/kronostock`
- owner/mode: `deploy:deploy`, `drwxr-xr-x`
- observed files:
  - `/srv/kronostock/dry-run-once.sh`, `-rwxr-xr-x`, 1091 bytes
  - `/srv/kronostock/update-telegram-token.py`, `-rwx------`, 3500 bytes
- `/srv/kronostock/.env`: 없음
- `/srv/kronostock/logs`: 없음
- `/srv/kronostock/backups`: 없음

현재 wrapper는 `/srv/kronostock/.env`가 아니라 checkout의 `/srv/agent-workspaces/KronosStock/.env`를 source한다.

### 3.3 runtime-local artifacts

보존 대상:

- `/srv/agent-workspaces/KronosStock/.env` — mode `600`, secrets 포함. 절대 출력/커밋 금지.
- `/srv/agent-workspaces/KronosStock/.env.bak.*` — 기존 env 백업. secrets 포함 가능, 절대 출력/커밋 금지.
- `/srv/agent-workspaces/KronosStock/.venv/` — 약 1.2G, Python 3.11 runtime deps.
- `/srv/agent-workspaces/KronosStock/model/` — vendored Kronos model package, 약 156K, gitignore 처리.
- `/home/deploy/.cache/huggingface` — 약 31M, HuggingFace cache.
- `/srv/logs/kronostock` — 존재하지만 현재 4K 디렉터리만 관찰됨.
- Redis keys listed in section 5.

주의: GitHub Actions 배포 workflow는 `git clean -fd`에서 `.env`, `.venv/`, `model/`, `.pytest_cache/`, `**/__pycache__/`만 제외한다. `.env.bak.*`는 `.gitignore`에는 걸리지만 deploy workflow의 `git clean -fd`는 ignored 파일을 지우지 않으므로 현재는 보존될 가능성이 높다. `git clean -fdx`로 바뀌면 위험해진다.

## 4. systemd 운영 경로

### 4.1 unit 상태

- `kronostock-dry-run.service`: loaded, inactive/dead, last result success
- last start: `2026-07-31 15:20:32 KST`
- last exit: `2026-07-31 15:21:33 KST`
- `ExecMainStatus=0`
- `kronostock-dry-run.timer`: loaded, active/waiting
- next run: `2026-08-03 08:50:00 KST`
- last trigger: `2026-07-31 15:20:32 KST`

### 4.2 service file

`/etc/systemd/system/kronostock-dry-run.service`:

```ini
[Unit]
Description=KronosStock read-only forecast and paper dry-run
After=network-online.target redis-server.service
Wants=network-online.target

[Service]
Type=oneshot
User=deploy
Group=deploy
WorkingDirectory=/srv/agent-workspaces/KronosStock
ExecStart=/srv/kronostock/dry-run-once.sh
TimeoutStartSec=900
Nice=5

[Install]
WantedBy=multi-user.target
```

### 4.3 timer file

`/etc/systemd/system/kronostock-dry-run.timer`:

```ini
[Timer]
OnCalendar=Mon..Fri 08:50:00
OnCalendar=Mon..Fri 09:30:00
OnCalendar=Mon..Fri 12:00:00
OnCalendar=Mon..Fri 15:20:00
Persistent=true
Unit=kronostock-dry-run.service
```

운영 의미: systemd timer가 KST 시장 주요 시각 4회에 oneshot service를 실행한다. service 자체는 장기 상주 프로세스가 아니라 실행 후 종료된다.

## 5. Redis 데이터 인벤토리

조사 시점 Redis key inventory, 값은 읽지 않고 type/TTL/memory만 확인했다.

### 5.1 OHLCV input cache

- `kronos:stock:ohlcv:daily:005930` 외 9개 종목 key type: string
- 관찰 종목: `005930`, `000660`, `035420`, `051910`, `006400`, `005380`, `001440`, `006800`, `045100`
- TTL: 약 580,852~580,858초, 즉 약 6.7일 잔여
- memory: 대체로 41,048 bytes, `000660`은 49,240 bytes
- 생성 코드: `inference.kr_data_fetcher.buffer_to_redis(..., ttl=7*24*3600)`

### 5.2 forecast output cache

- `kronos:stock:forecast:daily:<code>` 9개
- TTL: 약 235,270~235,311초, 즉 약 2.7일 잔여
- memory: 각 856 bytes
- 생성 코드: `inference.forecast_runner.buffer_forecast_to_redis(..., ttl=3*24*3600)`

### 5.3 paper portfolio

- key: `kronos:stock:paper:portfolio`
- type: string
- TTL: `-1`, 만료 없음
- memory: 136 bytes
- 생성 코드: `bot.scheduler.save_paper_portfolio`
- 의미: 실제 계좌/주문이 아니라 paper portfolio snapshot

### 5.4 Toss token cache

- key: `kronos:stock:toss:access_token`
- type: string
- TTL: 약 38,965초 잔여
- memory: 968 bytes
- 값은 읽지 않음. 백업/로그/Git 산출물에 포함하면 안 되는 secret-equivalent runtime token으로 취급한다.

## 6. 외부 연동 경로

### 6.1 Market data

runtime config safe summary:

- `market_data_provider=toss`
- `tossinvest_configured=True`
- `kis_configured=False`
- `kis_virtual_configured=False`
- `kis_use_virtual=True`

현재 운영 데이터 수집은 `market_data_provider=toss`이므로 Toss Open API read-only 시세 경로가 우선이다.

코드상 사용 endpoint:

- `POST /oauth2/token` — OAuth2 client credentials token 발급/캐시
- `GET /api/v1/candles` — 일봉 OHLCV read-only 수집
- `GET /api/v1/prices` — 현재가 read-only helper, 현재 wrapper 직접 경로에서는 사용하지 않음

KIS 경로는 코드에 남아 있지만 현재 `.env` safe summary 기준 실전/모의 KIS credential이 모두 미설정이라 운영 주 경로가 아니다. KIS도 데이터 조회는 read-only OHLCV 경로이나, broker/order API는 현재 wrapper에서 호출되지 않는다.

### 6.2 Model/cache

- model repo: `NeoQuasar/Kronos-mini`
- tokenizer repo: `NeoQuasar/Kronos-Tokenizer-2k`
- device: `cpu`
- horizon: `kronos_pred_len=5`
- n_paths: `forecast_n_paths=20`
- lookback days: `forecast_lookback_days=450`
- runtime package: `/srv/agent-workspaces/KronosStock/model/`
- HF cache: `/home/deploy/.cache/huggingface`

최초/캐시 미스 추론 시 HuggingFace Hub 접근이 발생할 수 있다. 캐시 보존은 cold start와 네트워크 의존도를 줄인다.

### 6.3 Telegram

- `telegram_configured=True`
- 전송 경로: `bot.alert_bot.send_text` → `telegram.Bot(...).send_message(...)`
- wrapper에서 `send_alert=True`로 digest 전송 활성화
- `bot/alert_bot.py`는 `httpx` logger level을 WARNING으로 낮춰 Telegram Bot API URL/token이 INFO log에 남는 것을 방지한다.

주의: Telegram token과 chat id는 `/srv/agent-workspaces/KronosStock/.env`에 존재한다. 조사 산출물에는 값/원시 token을 남기지 않았다.

## 7. 최근 실행 smoke 관찰

최근 systemd 실행은 read-only journal 필터로 확인했다.

- 실행: `2026-07-31 15:20:32` → `15:21:33`, 약 61초
- service result: success, exit status 0
- forecast: `워치리스트 예측 완료: 9/9 성공`
- scheduler: `dry-run 완료: signals=9 orders=0`
- portfolio key: `kronos:stock:paper:portfolio`
- actions summary: `005930 SELL`, `000660 SELL`, `035420 SELL`, `051910 SELL`, `006400 SELL`, `005380 SELL`, `001440 HOLD`, `006800 SELL`, `045100 SELL`

이 smoke는 과거 실행 로그 관찰이며, 본 조사 중 service를 수동 시작하지 않았다.

## 8. 보존 대상

운영 변경·퇴역·롤백 전 반드시 보존해야 하는 항목:

1. Secret/env
   - `/srv/agent-workspaces/KronosStock/.env`
   - `/srv/agent-workspaces/KronosStock/.env.bak.*`
   - `/srv/kronostock/update-telegram-token.py`는 token rotation helper로 보이나 secret 취급 필요. 내용 출력 금지.
2. Runtime dependency/cache
   - `/srv/agent-workspaces/KronosStock/.venv/`
   - `/srv/agent-workspaces/KronosStock/model/`
   - `/home/deploy/.cache/huggingface`
3. Mutable runtime data
   - Redis `kronos:stock:*` keys, 특히 `kronos:stock:paper:portfolio`와 `kronos:stock:toss:access_token`
4. Process manager state
   - `/etc/systemd/system/kronostock-dry-run.service`
   - `/etc/systemd/system/kronostock-dry-run.timer`
   - `systemctl list-timers kronostock-dry-run.timer --no-pager` 출력
5. Logs/audit
   - journald `kronostock-dry-run.service` 로그는 token redaction 후만 공유
   - `/srv/logs/kronostock`가 향후 파일 로그를 가지면 삭제 금지
6. Source/release state
   - HEAD SHA `0530f36bbd1d47fb495ed8779663f1d9d8e446cd`
   - untracked PM docs under `docs/`

## 9. 퇴역 또는 교체 순서

실제 퇴역은 별도 사용자 승인 전 수행 금지. 권장 순서는 다음과 같다.

1. freeze window 확정
   - 다음 timer 실행 전 충분한 시간 확보
   - `systemctl list-timers kronostock-dry-run.timer --no-pager`로 next run 확인
2. pre-retirement snapshot 생성
   - Git HEAD/status
   - unit/timer file copy
   - Redis key dump 또는 selective export
   - env 파일 암호화 백업
3. timer 중지
   - 승인 후에만 `sudo systemctl stop kronostock-dry-run.timer`
   - 필요하면 `sudo systemctl disable kronostock-dry-run.timer`
4. running service 부재 확인
   - `systemctl status kronostock-dry-run.service --no-pager`
   - 실행 중이면 완료를 기다리거나 승인된 stop 절차 사용
5. 새 경로 smoke 또는 replacement activation
   - loopback/static/read-only smoke 먼저
   - Redis key namespace 충돌 확인
6. 구 경로 파일 보존
   - `/srv/kronostock`는 즉시 삭제하지 말고 timestamped archive로 보존
   - env와 token helper는 별도 secret store/암호화 백업 후에만 정리
7. 관찰 기간
   - 최소 다음 schedule 1~2회 동안 새 경로 결과와 Telegram 도착 여부 확인
8. 최종 archive
   - 승인 후 old wrapper/systemd unit을 archive path로 이동 또는 삭제

## 10. 백업 명령 초안

아래 명령은 계획용이며 이번 조사 중 실행하지 않았다. 실행 전 별도 승인과 백업 대상 확인 필요.

### 10.1 metadata snapshot

```bash
TS=$(date -u +%Y%m%dT%H%M%SZ)
OUT=/srv/kronostock-backups/$TS
sudo install -d -o deploy -g deploy -m 700 "$OUT"
cd /srv/agent-workspaces/KronosStock
{
  date -Is
  git rev-parse HEAD
  git status --short --branch
  git worktree list --porcelain
  systemctl show kronostock-dry-run.service -p ActiveState -p SubState -p Result -p ExecMainStatus --no-pager
  systemctl show kronostock-dry-run.timer -p ActiveState -p SubState -p LastTriggerUSec -p NextElapseUSecRealtime --no-pager
  systemctl list-timers kronostock-dry-run.timer --no-pager
} > "$OUT/metadata.txt"
```

### 10.2 systemd/wrapper backup

```bash
cp -a /etc/systemd/system/kronostock-dry-run.service "$OUT/"
cp -a /etc/systemd/system/kronostock-dry-run.timer "$OUT/"
cp -a /srv/kronostock "$OUT/kronostock-wrapper"
```

### 10.3 secret backup

secret 백업은 평문 공유 금지. 운영자가 암호화 저장소나 root/deploy 전용 0700 디렉터리에만 보관해야 한다.

```bash
install -m 600 /srv/agent-workspaces/KronosStock/.env "$OUT/env.redacted-or-encrypted"
# 권장: 실제로는 age/gpg 같은 암호화 후 평문 사본을 남기지 않는다.
```

### 10.4 Redis selective backup

Toss access token은 재발급 가능하므로 영속 백업에서 제외 가능하다. paper portfolio와 최근 OHLCV/forecast만 필요하면 다음처럼 prefix dump를 고려한다.

```bash
redis-cli --scan --pattern 'kronos:stock:*' > "$OUT/redis-keys.txt"
redis-cli --rdb "$OUT/redis-dump.rdb"
```

주의: RDB에는 token cache도 들어갈 수 있다. 공유/업로드 금지.

## 11. smoke 명령 초안

아래 smoke는 side-effect 수준에 따라 분리한다.

### 11.1 read-only status smoke, 승인 없이도 상대적으로 안전

```bash
cd /srv/agent-workspaces/KronosStock
git status --short --branch
systemctl is-active kronostock-dry-run.timer
systemctl show kronostock-dry-run.service -p Result -p ExecMainStatus --no-pager
redis-cli ping
redis-cli --scan --pattern 'kronos:stock:*' | sort
```

### 11.2 Redis value를 읽는 logical smoke, secret token 제외 필요

```bash
# token key 제외, portfolio/forecast schema만 검사하는 별도 스크립트 권장
redis-cli get kronos:stock:paper:portfolio
redis-cli get kronos:stock:forecast:daily:005930
```

주의: 위 `get`은 원시 runtime payload를 출력한다. 외부 공유 전 redaction/요약 필수. `kronos:stock:toss:access_token`은 절대 출력하지 않는다.

### 11.3 service one-shot smoke, 승인 필요

다음 명령은 외부 Toss read API, Redis write, Telegram 전송을 수행하므로 이번 조사 범위를 벗어난다. 별도 승인 후에만 실행한다.

```bash
sudo systemctl reset-failed kronostock-dry-run.service
sudo systemctl start kronostock-dry-run.service
systemctl show kronostock-dry-run.service -p Result -p ExecMainStatus -p ExecMainExitTimestamp --no-pager
journalctl -u kronostock-dry-run.service --since '5 minutes ago' --no-pager
```

## 12. rollback 명령 초안

아래 rollback은 승인된 변경 이후 문제가 생겼을 때의 계획이다. 이번 조사 중 실행하지 않았다.

### 12.1 timer만 원복

```bash
sudo cp -a "$OUT/kronostock-dry-run.service" /etc/systemd/system/kronostock-dry-run.service
sudo cp -a "$OUT/kronostock-dry-run.timer" /etc/systemd/system/kronostock-dry-run.timer
sudo systemctl daemon-reload
sudo systemctl enable --now kronostock-dry-run.timer
systemctl list-timers kronostock-dry-run.timer --no-pager
```

### 12.2 wrapper 원복

```bash
sudo rsync -a --delete "$OUT/kronostock-wrapper/" /srv/kronostock/
sudo chown -R deploy:deploy /srv/kronostock
sudo chmod 755 /srv/kronostock/dry-run-once.sh
```

### 12.3 source checkout 원복

```bash
cd /srv/agent-workspaces/KronosStock
git fetch origin main
git checkout main
git reset --hard 0530f36bbd1d47fb495ed8779663f1d9d8e446cd
# .env/.venv/model 보존 여부를 먼저 확인하고 git clean 범위를 제한한다.
```

### 12.4 Redis 원복

Redis RDB 원복은 전체 DB 영향이 있으므로 단독 Redis 또는 maintenance window에서만 수행한다. 현재 Redis에는 `kronos:stock:*` 외 다른 key가 있을 수 있으므로 prefix-level export/import 도구가 더 안전하다. 최소 rollback은 기존 paper portfolio를 보존하고 새 forecast/OHLCV cache는 TTL 재생성을 허용하는 방식이 낫다.

## 13. 주요 위험·결정 필요 사항

1. 백업 루트 부재
   - `/srv/kronostock/backups`는 없고 `/srv/logs/kronostock`만 존재한다.
   - 운영 변경 전에 `/srv/kronostock-backups` 또는 동등한 0700 백업 루트 확정 필요.
2. Redis token cache
   - `kronos:stock:toss:access_token`은 runtime secret-equivalent다.
   - RDB 전체 백업 시 secret 포함 가능성이 있으므로 공유 금지.
3. timer `Persistent=true`
   - VPS가 꺼졌다 켜지면 놓친 timer가 catch-up 실행될 수 있다.
   - 퇴역/교체 시 timer stop/disable 순서가 중요하다.
4. memory pressure
   - swap 없음, available memory 약 1.1GiB 관찰.
   - 모델 추론과 다른 agent/browser/build가 겹칠 때 안정성 저하 가능.
5. deploy workflow side effect
   - main push 시 GitHub Actions가 이 VPS checkout을 `reset --hard`, deps install, tests, wrapper/systemd 재설치/타이머 재시작한다.
   - future 운영 구조 변경 시 workflow도 함께 gate 관리해야 한다.
6. Telegram log hygiene
   - 현재 `httpx` INFO token log 차단 코드가 있다.
   - Telegram/httpx logging 변경은 보안 regression으로 봐야 한다.

## 14. 최종 판정

현재 기존 운영 경로는 관찰 기준 정상 동작 중이다. 2026-07-31 15:20 KST timer 실행은 성공했고, 9/9 forecast와 Telegram digest 생성까지 완료됐다. 다만 운영 변경/퇴역 전에는 secret-safe 백업 루트, Redis prefix 백업 방식, timer disable window, workflow 동시 변경 여부를 먼저 확정해야 한다.

Readiness: 운영 인벤토리 완료. 변경 실행은 별도 approval-gated 단계로 분리해야 한다.

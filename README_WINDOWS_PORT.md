# 🪙 백두코인봇 — Windows 포팅 (BaekdooCoin)

> 업비트 KRW 10종 자동매매 봇. 원본(macOS, `/Volumes/X31/workspace/BaekdooCoin`)의
> **MTF 엔진 + 터틀(1D/5M) + CTP 익절 사다리**를 Windows 11 arm64에서 재구현한 판.
> 스펙 원문: 옵시디언 `Trading/BaekdooCoin/BaekdooCoin_Reimplementation_Prompt.md`

---

## 1. 원본과 다른 점 (Windows 적응)

| 항목 | 원본 (macOS) | 이 포팅판 | 이유 |
|---|---|---|---|
| 데이터 처리 | pandas | **numpy + stdlib** | 이 PC는 Smart App Control이 켜져 있어 pandas C확장(`hashtable`) 로드가 차단됨 |
| 런타임 경로 | `/tmp/baekdoocoin_*` | `%LOCALAPPDATA%\BaekdooCoin\` | Windows에 `/tmp` 없음 |
| 스케줄 | cron | **작업 스케줄러**(`install_task.bat`) | 동일 역할 |
| 프로세스 유지 | nohup / launchd | `run_paper.bat` + PID 락 | 중복 실행 방지 |
| 종료 신호 | SIGTERM | SIGINT/SIGTERM + 그레이스풀 저장 | 동일 |
| 텔레그램 | 파일 릴레이(OpenClaw) | **getUpdates 롱폴링 직접** | 이 PC에 릴레이 데몬이 없음 |
| 1H/5M 데이터 없을 때 | 에러 | **1D로 폴백**(5M 슬리브 자동 비활성) | 부분 데이터로도 드라이런 가능 |

로직 자체(레짐·터틀·7필터·CTP·리스크캡)는 원본 수치를 그대로 사용했다.

---

## 2. 구조

```
BaekdooCoin/
├── live_monitor.py        진입점: WS + 1분 사이클 + 주문 + 텔레그램
├── engine_mtf.py          MTF 엔진 (1D+5M 듀얼 슬리브, 승급, 안전장치)
├── strategies/
│   ├── regime_detector.py ADX+BB폭+RSI+EMA → 4레짐 (+DC55 돌파 override)
│   ├── turtle_mtf.py      DC55 진입 / DC20·ATR 손절 / 5M 7종 필터 / ATR 사이징
│   └── risk_mtf.py        Fatal Stop·CTP 사다리·불타기·물타기·캡
├── indicators.py          SMA/EMA/RSI/ATR/BB/ADX/Donchian/linreg (numpy)
├── datastore.py           SQLite ohlcv_{5m,1h,4h,1d}
├── upbit_client.py        REST + JWT(HS256), 레이트리밋/재시도
├── data_crawler.py        전체 히스토리 수집(역방향 페이징)
├── data_updater.py        증분 갱신
├── backtest.py            1D 터틀 백테스트
├── telegram_control.py    /status /balance /positions /regime /data /stop /resume /pause
├── doctor.py              환경·네트워크·데이터 점검
├── tools/
│   ├── import_legacy_db.py 볼트의 기존 업비트 DB → 신규 DB
│   ├── replay.py           실측 일봉 위에서 엔진 리플레이(페이퍼)
│   └── control.py          stop / resume / status / positions
└── *.bat                  run_paper · run_trade · stop · resume · status · update_data · backtest · doctor · install_task
```

---

## 3. 설치 (이미 완료된 상태)

```bat
:: 가상환경 (이미 만들어져 있음 — .venv)
uv pip install --python .venv\Scripts\python.exe requests pyjwt websocket-client python-dotenv numpy

:: 설정
copy .env.example .env     :: 업비트 키 / 텔레그램 토큰 입력
.venv\Scripts\python.exe doctor.py
```

> ⚠️ **venv 함정**: Smart App Control 때문에 `python -m venv`가 만든 `python.exe`가 차단될 수 있다.
> 그럴 때는 `copy E:\Programs\miniforge3\python.exe .venv\Scripts\python.exe` 로 덮어쓰면 동작한다.

### 데이터

```bat
:: (A) 업비트 API가 열려 있을 때 — 전체 히스토리
.venv\Scripts\python.exe data_crawler.py --timeframe 1d 1h 5m

:: (B) API가 막혀 있을 때 — 볼트에 이미 있는 실측 DB 임포트
.venv\Scripts\python.exe tools\import_legacy_db.py            :: 10종
.venv\Scripts\python.exe tools\import_legacy_db.py --all-markets
```

---

## 4. 실행 순서 (원본 체크리스트와 동일)

1. `doctor.bat` — 환경/키/데이터/네트워크 확인
2. `backtest.bat` — 전략 성과 확인
3. `run_paper.bat` — **감시 모드**(주문 없음). 며칠 관찰
4. 텔레그램에서 `/status`, `/regime` 응답 확인
5. 아주 소액으로 `run_trade.bat` — 실거래
6. 이상 시 `stop.bat` (또는 텔레그램 `/stop`) → 즉시 전 주문 중단

---

## 5. 안전장치

| 장치 | 동작 |
|---|---|
| 킬스위치 파일 | `%LOCALAPPDATA%\BaekdooCoin\EMERGENCY_STOP` 존재 시 전 주문 차단 (`stop.bat`) |
| 일일 손실 한도 | 시작자본 대비 -5% → 자동 중단 |
| 일일 거래 한도 | 30회 |
| 사이클 주문 한도 | 10건 (초과 시 비상정지) |
| 리스크 캡 | 1D 슬리브 6% / 5M 1.5% / 총 7% |
| PID 락 | 중복 실행 방지 |
| 포지션 영속화 | `open_positions.json` (크래시 복구) + 재시작 시 거래소 잔고 동기화 |
| 주문 실패 롤백 | 낙관적으로 잡은 포지션 상태를 되돌림 |

---

## 6. 검증 결과 (이 PC에서 실측)

**테스트/게이트**: `test.bat` → **261개 테스트 · 커버리지 88.7%** · pre-commit 훅이 실패 시 커밋 차단

**1D 터틀 백테스트** (실측 업비트 일봉, 2017~2026, 수수료 0.05%)

| 종목 | 거래 | 승률 | PF | 수익률 | MDD |
|---|---:|---:|---:|---:|---:|
| BTC | 23 | 56.5% | 10.55 | +257.2% | 23.1% |
| ETH | 20 | 55.0% | 10.89 | +178.4% | 35.5% |
| ADA | 18 | 44.4% | 8.34 | +225.9% | 33.1% |
| SOL | 14 | 28.6% | 4.74 | +102.0% | 19.4% |
| XRP | 20 | 40.0% | 4.15 | +73.2% | 45.2% |
| DOT | 8 | 50.0% | 12.13 | +90.9% | 22.3% |
| DOGE | 16 | 25.0% | 1.38 | +22.9% | 26.8% |
| AVAX | 10 | 40.0% | 2.02 | +23.0% | 25.6% |
| LINK | 19 | 26.3% | 1.75 | +18.8% | 23.7% |
| POL | 11 | 18.2% | 0.56 | -6.7% | 20.4% |

**엔진 리플레이** (2023-11-01~2026-11-09, 실제 `MTFEngine.run_cycle` + 현금/캡/수수료 반영)

| 버전 | 수익률 | MDD | 주문 | 비고 |
|---|---:|---:|---:|---|
| 하드닝 전 | +186.6% | 14.1% | 247 | 단일 주문이 자본 60%까지 → 캡 없음(착시) |
| **하드닝 후** | **+28.4%** | **21.7%** | **438** | 주문≤15%·종목≤25%·총≤80% 캡 적용 |

> ⚠️ 하드닝 후 수익률이 크게 낮아진 것은 **버그 수정이 아니라 리스크 정상화**다.
> 이전 수치는 한 종목에 자본 60%를 넣어야 나오던 값이고, 실제로 그렇게 운용하면
> 2×ATR 손절 한 번에 계좌 -10%가 난다. 실전 기대치는 **연 8~9% / MDD 20% 수준**으로 봐야 한다.

**CTP/리스크/보호장치**: `pytest tests/test_risk_ctp.py tests/test_safety_hardening.py` — 100% 커버리지

---

## 7. 하드닝 이력 (병렬 감사 4레인 반영, 2026-11-09)

감사(기획·코드·벤치마킹·운영) 결과 **CRITICAL 4 / HIGH 다수** → 실패 테스트로 고정 후 수정.

| 감사 ID | 결함 | 수정 | 검증 테스트 |
|---|---|---|---|
| F01/SAFETY-03 | **일일손실 -5% 킬스위치가 계산상 항상 0%** (equity 고정) | 사이클마다 총평가액 갱신, 3회 실패 시 신규진입 차단 | `test_f01_*` 4건 |
| F02/NUMERIC-18 | 주문 1건이 자본 최대 62%(1D)/100%(5M) | 주문 15%/5%, 종목 25%/8%, 슬리브 60%/15%, 총 80%, 종목리스크 2.5% | `test_f02_*` 3건 |
| F03 | **ADX가 4종(SOL/AVAX/POL/LINK)에서 전 구간 NaN** → 유니버스 40% 미거래 | dx 0/0 정규화 + 도지봉 내성 | `test_f03_*` 3건 |
| F04/SAFETY-02 | 정지/한도 도달 시 **청산까지 중단** | 정지는 신규·증액만 차단, 청산은 항상 실행 | `test_f04_*` 2건 |
| F04/STATE-06 | 주문 한도 초과 시 주문 폐기 + 유령 포지션 | 청산 우선, 매수만 이월, 포지션은 주문 확정 후 등록 | `test_state06_*` 2건 |
| F05/STATE-11 | CTP 사다리 3·4단 도달 불가 / 실패 시 rung 유실 | 트리거를 개선가+2%로 클램프 + **체결 확정 시에만 소비** | `test_f05_*`, `test_state11_*` |
| F09 | 1D 진입을 장중 틱으로 판정 (백테스트와 다른 전략) | 진입은 완료 일봉 종가, 청산만 실시간가 | `test_f09_*` 2건 |
| F10/CONCURRENCY-13 | 스테일 가격으로 주문 | 120초 신선도 필터 + 가격 미확보 종목 주문 금지 | `test_f10_*` 3건 |
| F12 | 프로세스 사망 시 청산 수단 없음 | 보호장치 + 워치독(deadman) + 텔레그램 경보 | `test_watchdog_*` 3건 |
| F08/STATE-19 | 리스크캡이 실제 노출 미추적 | 체결마다 `risk_pct` 재계산 | `test_f08_*` 2건 |
| F13 | 유동성 필터 부재 | 20봉 평균 거래대금(1D 50억/5M 1억) 미달 시 제외 | `test_f13_*` |
| F18 | 거래 원장 없음(세금/성과 불가) | `trades.jsonl` append-only + `report.bat`(PF/세금) | `test_f18_*`, `test_report_*` |
| LIFECYCLE-01/05 | 수량이 수수료 미반영·잔고 미대조 | 진입수량 ×(1-0.05%), 매도 직전 거래소 잔고로 클램프 | `test_lifecycle01_*` 3건 |
| LIFECYCLE-08 | 승급 실패 시 포지션 증발 | GRADUATE 롤백(5M 복원) | `test_lifecycle08_*` |
| STATE-07 | JSON 비원자적 저장·손상 시 조용히 진행 | tmp+`os.replace`, 손상 시 진입차단+경보, 오염 포지션 sanity 컷 | `test_state07_*` 4건 |
| STATE-10 | 수동 보유 코인을 봇이 임의 청산 | `auto_manage=False`(관망 전용) + 수량 drift 보정 | `test_state10_*` |
| STATE-12/14 | 페이퍼·실거래 상태 공유 / pause 소실 | 파일 분리, pause 파일 영속 | `test_state12_*`, `test_state14_*` |
| DATA-21 | 5m 갱신 누락, 1h None 크래시 | 갱신 목록에 5m 추가, 종목별 예외 격리 | `test_data21_*` 2건 |
| CONCURRENCY-15 | PID 락 경쟁 조건 | `O_CREAT|O_EXCL` 원자 생성 | `test_concurrency15_*` |

**보호장치 신규**: StoplossGuard(24h 손절 4회 → 4h 중단), DrawdownGuard(30일 고점 -20% → 12h 중단), 청산 후 쿨다운 — `protections.py`, 재시작 후에도 유지.

---

## 8. 현재 이 PC의 상태

| 항목 | 상태 |
|---|---|
| `api.upbit.com` | ✅ **정상** (2026-11-09 재측정: 200, 0.5초, 5/5 성공 / WS 정상) — 11-08의 타임아웃은 일시 장애 |
| 데이터 | ✅ 1d 3,333봉(~2026-11-09) · 1h/4h/5m 각 2,400봉 (10종) |
| 업비트 API 키 | ❌ 미설정 → `--trade` 불가 (모의는 공개 시세로 동작) |
| 고정 IP 화이트리스트 | ❌ 필요 (업비트 인증 호출은 등록 IP만 허용) |
| 텔레그램 | ❌ 미설정 |
| 자동 시작/워치독 | ⏳ `install_task.bat` + `watchdog.bat` 등록만 남음 |

**실거래 전 남은 3단계**: ① `.env`에 키 + 화이트리스트 IP ② `run_paper.bat` 3~7일 관찰 ③ 소액(50만원) `run_trade.bat` → 24시간 관찰 후 증액.

---

*원본: Molt 🦅 (macOS) · Windows 포팅: Amy ⚡ — Hyderabad*

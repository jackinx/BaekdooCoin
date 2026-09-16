# 🪙 백두코인봇 (BaekdooCoin)

> 업비트 10대 메이저 코인 완전 자동매매 봇 — 잭의 두 번째 두뇌

---

## 🤖 ELI5: 5살 아이한테 설명한다면?

**"백두코인봇은 뭐 하는 로봇이야?"**

쉽게 말해서, **주식 대신 가상화폐(비트코인, 이더리움 등)를 사고파는 로봇**이야.

이 로봇이 하는 일은 딱 세 가지야:
1. 📡 **가격을 실시간으로 쳐다본다** (10개 코인, 1초마다 업데이트)
2. 🧠 **똑똑한 계산을 한다** ("지금 살까? 팔까? 기다릴까?")
3. 💰 **알아서 사고 판다** (진짜 돈으로 업비트에서)

**마치 자율주행차처럼, 사람이 운전대 안 잡아도 혼자 길 찾아서 움직이는 것과 똑같아.**

---

## 🧠 로봇의 두뇌 구조

```
live_monitor.py          ← 🎮 조종석 (1분마다 "지금 뭐할까?" 물어봄)
    ↓
engine_mtf.py            ← 🧠 메인 두뇌 (1D + 5M 두 개의 시계로 동시에 생각)
    ↓
strategies/
  ├── turtle_mtf.py      ← 📈 추세 따라가기 (Donchian Channel)
  ├── squeeze_momentum.py ← 🌀 변동성 폭발 감지 (LazyBear)
  ├── bb_rsi_reversal.py ← 🔄 과매수/과매도 반전 포착 (볼린저+RSI)
  ├── regime_detector.py ← 🌡️ 지금 시장이 어떤 상태인지 판단
  ├── risk_mtf.py        ← 🛡️ 리스크 매니저 (잃어도 되는 만큼만 베팅)
  └── risk.py            ← 🔥 CTP 이익 사다리 (번 돈 지키는 시스템)
```

---

## ⚙️ 작동 방식 (1분 사이클)

```
[1분마다 반복]
   │
   ├─ ① 비상정지 확인 (🚨 /stop 명령 왔나?)
   │
   ├─ ② 시장 상태 판단 (바람 부는 방향 체크)
   │     ├─ TRENDING  (강한 추세) → 🐢 Turtle 전략
   │     ├─ SQUEEZING (변동성 수축) → 🌀 Squeeze 전략
   │     ├─ REVERSING (극단 반전) → 🔄 BB+RSI 전략
   │     └─ NEUTRAL   (애매함) → 😴 기다림
   │
   ├─ ③ 보유 중인 코인은?
   │     ├─ 손절 라인 걸렸나? → 🔴 싹 팔기
   │     ├─ CTP 사다리 발동? → 📉 부분 익절
   │     ├─ 더 오르고 있나? → 🔥 추가 매수 (불타기)
   │     ├─ 좀 떨어졌나? → 💧 물타기
   │     └─ 졸업 조건? → 🎓 5M→1D 승급
   │
   ├─ ④ 새로 살 코인 있나?
   │     ├─ 1D 타임프레임 먼저 체크 (큰 추세)
   │     ├─ 5M 타임프레임 체크 (작은 타이밍)
   │     ├─ 7종 필터 통과했나? (거짓 돌파 걸러내기)
   │     └─ 포트폴리오 한도 체크 (너무 많이 못 삼)
   │
   └─ ⑤ 주문 실행 → 업비트 API 호출 → 끝!
```

---

## 💰 리스크 관리 (돈 지키는 법)

| 규칙 | 설명 |
|------|------|
| **슬리브 캡** | 1D ≤ 자산의 6%, 5M ≤ 1.5% |
| **총 캡** | 전체 포트폴리오 ≤ 7% |
| **손절** | 진입가 - 3×ATR 아래로 떨어지면 무조건 팔기 |
| **CTP 사다리** | 수익 3% 돌파 시 4단계 분할매도 자동 설치 |
| **물타기 제한** | 평단 -5% 이하로 내려가면 물타기 금지 |
| **레짐 게이트** | 1H가 TRENDING일 때만 새로 진입 |

---

## 📊 백테스트 결과 (BTC, 2023.11 ~ 2026.08)

| 전략 | 수익률 | PF | 승률 | MDD |
|------|:---:|:--:|:--:|:--:|
| 🐢 Turtle (1D) | **+88.5%** | 3.53 | 45.5% | 17.3% |
| 🌀 Squeeze (1H) | +12.0% | 2.01 | 47.8% | 2.5% |
| 🔄 BB+RSI (1H) | +8.3% | 1.55 | 42.1% | 3.1% |

> ⚠️ Squeeze와 BB+RSI는 1시간봉 데이터가 ~6주밖에 없어 통계적으로 신뢰도 낮음. Turtle은 2.7년 전체 데이터.

---

## 🚀 실행 방법

```bash
# 1. 환경 설정
cp .env.example .env   # API 키 입력 필수!
pip install pyupbit pandas numpy requests python-dotenv

# 2. 데이터 수집 (최초 1회)
python data_crawler_v2.py

# 3. 감시만 (주문 안 함 — 테스트용)
python live_monitor.py

# 4. 실거래 (진짜 돈으로 매매)
python live_monitor.py --trade
```

---

## 🎮 텔레그램 원격 명령어

| 명령어 | 기능 |
|--------|------|
| `/status` | 현재 포지션, 잔고, 레짐 상태 |
| `/balance` | 업비트 원화 잔고 |
| `/stop` | 🚨 비상정지 (즉시 모든 매매 중단) |
| `/resume` | 매매 재개 |
| `/pause` | 일시 정지 |

---

## 📁 프로젝트 구조

```
BaekdooCoin/
├── live_monitor.py           # 🎮 메인 실행 파일 (WebSocket + 사이클)
├── engine_mtf.py             # 🧠 MTF 트레이딩 엔진 (1D+5M)
├── engine.py                 # 🧠 구버전 엔진 (곧 폐기)
├── config.py                 # ⚙️ .env 설정 로더
├── telegram_control.py       # 📱 텔레그램 봇 원격제어
├── upbit_client.py           # 🔌 업비트 REST API 클라이언트
├── data_crawler_v2.py        # 📥 OHLCV 데이터 수집기
├── data_updater.py           # 🔄 DB 업데이트 유틸
├── openclaw_relay.py         # 🤖 OpenClaw 연동
├── backtest_runner.py        # 📊 백테스트 실행기
├── strategies/
│   ├── turtle.py             # 🐢 클래식 터틀 (1D)
│   ├── turtle_mtf.py         # 🐢🐢 멀티TF 터틀 (1D+5M)
│   ├── squeeze_momentum.py   # 🌀 Squeeze Momentum
│   ├── bb_rsi_reversal.py    # 🔄 BB+RSI 반전
│   ├── regime_detector.py    # 🌡️ 시장 레짐 감지
│   ├── risk.py               # 🛡️ Risk Manager v3 (CTP)
│   └── risk_mtf.py           # 🛡️ MTF Risk Manager
├── scripts/
│   └── check_ip.py           # 🌐 공인IP 체크 (방화벽용)
├── data/
│   ├── upbit_v2.db           # 📦 메인 DB (OHLCV 히스토리)
│   └── open_positions.json   # 📋 포지션 상태
├── backtest_report.md        # 📊 백테스트 상세 보고서
├── watchdog.sh               # 🛡️ 프로세스 자동 재시작
└── start_monitor.sh          # 🚀 시작 스크립트
```

---

## 🛡️ 안전장치

- **비상정지**: `touch /tmp/baekdoocoin_emergency_stop` → 즉시 거래 중단
- **일일 손실 제한**: 5% 이상 손실 시 자동 중단
- **PID 락**: 중복 실행 방지
- **Watchdog**: 프로세스 죽으면 자동 재시작 (`watchdog.sh`)
- **IP 변경 감지**: 공인IP 바뀌면 텔레그램 알림 (업비트 화이트리스트)

---

*Built with ❤️ in Hyderabad, India — 트레이더 백두산의 두 번째 두뇌* 🦅
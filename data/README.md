# 백두코인봇 Upbit v2 데이터베이스

## 개요

Upbit KRW 마켓 주요 10개 종목의 OHLCV 데이터를 SQLite 데이터베이스에 저장합니다.
`data_crawler_v2.py`로 초기 전체 수집, `data_updater.py`로 일일 증분 업데이트를 수행합니다.

**DB 경로:** `data/upbit_v2.db` (v1의 `upbit.db`와 별도)

**주요 10개 티커 (고정):**
| # | 티커 | 종목 |
|---|------|------|
| 1 | KRW-BTC | 비트코인 |
| 2 | KRW-ETH | 이더리움 |
| 3 | KRW-SOL | 솔라나 |
| 4 | KRW-XRP | 리플 |
| 5 | KRW-ADA | 카르다노 |
| 6 | KRW-DOGE | 도지코인 |
| 7 | KRW-AVAX | 아발란체 |
| 8 | KRW-DOT | 폴카닷 |
| 9 | KRW-POL | 폴리곤 (구 MATIC → POL 리브랜딩) |
| 10 | KRW-LINK | 체인링크 |

## 스키마

### 테이블 목록
| 테이블     | 타임프레임   | 설명              |
|-----------|-------------|-------------------|
| ohlcv_1d  | 1일 (days)  | 일봉 — 트렌드/포지션 |
| ohlcv_4h  | 4시간 (240분) | 4시간봉 — 스윙 전략 |
| ohlcv_1h  | 1시간 (60분)  | 1시간봉 — 단기 전략 |

### 컬럼 구조 (v1과 동일)
| 컬럼        | 타입  | 설명                                |
|------------|-------|-------------------------------------|
| ticker     | TEXT  | 티커 (예: KRW-BTC)                   |
| timestamp  | TEXT  | UTC ISO 8601 (예: 2026-08-22T09:00:00) |
| open       | REAL  | 시가 (KRW)                           |
| high       | REAL  | 고가 (KRW)                           |
| low        | REAL  | 저가 (KRW)                           |
| close      | REAL  | 종가 (KRW)                           |
| volume     | REAL  | 거래량 (코인 단위)                     |
| volume_krw | REAL  | 거래대금 (KRW)                        |

- **Primary Key:** (ticker, timestamp)
- **인덱스:** ticker, timestamp 각각에 B-tree 인덱스
- **Write Mode:** WAL (Write-Ahead Logging)

## 초기 데이터 수집 (data_crawler_v2.py)

### 전체 수집 (1일봉 + 4시간봉 + 1시간봉, 10개 티커)
```bash
python data_crawler_v2.py --force-recreate
```

### 특정 타임프레임만
```bash
python data_crawler_v2.py --timeframe 1d 4h
```

### DB 통계 확인
```bash
python data_crawler_v2.py --stats
```

## 일일 업데이트 (data_updater.py)

### 전체 업데이트 (모든 타임프레임)
```bash
python data_updater.py
```

### 특정 타임프레임만
```bash
python data_updater.py --timeframe 1d
```

### 통계 확인
```bash
python data_updater.py --stats
```

## 크론탭 설정

```cron
# 매일 오전 9시 15분 (한국장 시작 전) 1일봉 업데이트
15 9 * * * cd /Volumes/X31/workspace/백두코인봇_Upbit && /usr/bin/python3 data_updater.py --timeframe 1d >> logs/updater_daily.log 2>&1

# 4시간마다 4h + 1h 업데이트
0 */4 * * * cd /Volumes/X31/workspace/백두코인봇_Upbit && /usr/bin/python3 data_updater.py --timeframe 4h 1h >> logs/updater_4h.log 2>&1
```

## 업데이트 전략

- **증분 방식:** DB의 가장 최근 timestamp 조회 → 그 이후 캔들만 API로 가져옴
- **INSERT OR REPLACE:** 중복 실행해도 안전 (UPSERT)
- **보고:** 각 실행마다 추가된 캔들 수, 티커별 현황 출력
- **오류 처리:** API 실패 시 지수 백오프 재시도 (최대 5회)

## API 정보

- **엔드포인트:** `https://api.upbit.com/v1/candles/{minutes|days}/{unit}`
- **Rate Limit:** ~10 req/s (코드에서 0.15초 sleep으로 보수적 관리)
- **페이지당 최대:** 200캔들
- **v2 변경사항:** MATIC → POL 리브랜딩 반영 (2024년 9월)

## 의존성

- Python 3.8+
- `requests` — HTTP 클라이언트
- `tqdm` — 진행 표시줄 (크롤러 only)
- 표준 라이브러리: sqlite3, argparse, logging, time, os, datetime

## v1 vs v2

| 항목 | v1 (data_crawler.py) | v2 (data_crawler_v2.py) |
|------|---------------------|------------------------|
| DB 파일 | data/upbit.db | data/upbit_v2.db |
| 티커 | 모든 KRW 마켓 (~200+) | 주요 10개 고정 |
| 타임프레임 | 1m/5m/15m/1h/4h/1d/1w | 1h/4h/1d |
| 업데이트 | 전체 재수집 | 증분 업데이터 별도 |
| 용도 | 전체 시장 분석 | 트레이딩 전략 집중 |
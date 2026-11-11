# -*- coding: utf-8 -*-
"""업비트 REST API 클라이언트 (JWT HS256).

원본 upbit_client.py 를 Windows/numpy 환경에서 그대로 재구현.
- 인증: access_key + nonce (+ query_hash[SHA512]) → HS256 서명
- 호출 간 최소 0.05초 간격, 429는 Retry-After 대기, 네트워크 오류는 지수 백오프 3회
- 최소 주문금액 5,000 KRW 강제
- ⚠️ 출금 권한은 사용하지 않는다 (주문/조회만).
"""
from __future__ import annotations

import hashlib
import logging
import time
import urllib.parse
import uuid
from typing import Dict, List, Optional

import jwt
import requests

log = logging.getLogger("baekdoocoin.upbit")

BASE = "https://api.upbit.com"
TIMEOUT = 15
MIN_ORDER_KRW = 5_000.0
MIN_INTERVAL = 0.05


class UpbitError(RuntimeError):
    def __init__(self, message: str, status: int = 0, payload=None):
        super().__init__(message)
        self.status = status
        self.payload = payload


class UpbitClient:
    def __init__(self, access_key: str, secret_key: str, min_interval: float = MIN_INTERVAL):
        if not access_key or not secret_key:
            raise UpbitError("업비트 API 키가 없습니다 (.env 확인)")
        self.access_key = access_key
        self.secret_key = secret_key
        self.min_interval = min_interval
        self._last_call = 0.0
        self.s = requests.Session()
        self.s.headers.update({"Accept": "application/json", "User-Agent": "baekdoocoin-win/1.0"})

    # ---------------- internal ----------------
    def _throttle(self) -> None:
        dt = time.time() - self._last_call
        if dt < self.min_interval:
            time.sleep(self.min_interval - dt)
        self._last_call = time.time()

    def _auth_header(self, query: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        payload = {"access_key": self.access_key, "nonce": str(uuid.uuid4())}
        if query:
            qs = urllib.parse.urlencode(query, doseq=True)
            payload["query_hash"] = hashlib.sha512(qs.encode()).hexdigest()
            payload["query_hash_alg"] = "SHA512"
        token = jwt.encode(payload, self.secret_key, algorithm="HS256")
        if isinstance(token, bytes):
            token = token.decode()
        return {"Authorization": f"Bearer {token}"}

    def _request(self, method: str, path: str, params: Optional[Dict[str, str]] = None,
                 body: Optional[Dict[str, str]] = None, auth: bool = True) -> object:
        url = BASE + path
        headers: Dict[str, str] = {}
        data = None
        if method.upper() == "GET":
            if params:
                url += "?" + urllib.parse.urlencode(params, doseq=True)
            if auth:
                headers.update(self._auth_header(params))
        else:
            if body is not None:
                data = urllib.parse.urlencode(body)
                self.s.headers["Content-Type"] = "application/x-www-form-urlencoded"
            if auth:
                headers.update(self._auth_header(body))

        last_err: Optional[Exception] = None
        for attempt in range(4):
            self._throttle()
            try:
                r = self.s.request(method, url, data=data, headers=headers, timeout=TIMEOUT)
            except requests.RequestException as e:
                last_err = e
                time.sleep(1.0 * (attempt + 1))
                continue
            if r.status_code == 429:
                wait = float(r.headers.get("Retry-After", 2))
                log.warning("429 rate limit — %.1fs 대기", wait)
                time.sleep(min(wait, 60))
                continue
            if r.status_code >= 500:
                last_err = UpbitError(f"서버 오류 {r.status_code}", r.status_code, r.text[:300])
                time.sleep(1.0 * (attempt + 1))
                continue
            if r.status_code >= 400:
                try:
                    payload = r.json()
                except Exception:
                    payload = {"error": r.text[:300]}
                raise UpbitError(
                    f"{payload.get('error', {}).get('name', 'UpbitError')}: "
                    f"{payload.get('error', {}).get('message', str(payload))}",
                    r.status_code, payload,
                )
            return r.json() if r.content else {}
        raise UpbitError(f"요청 실패 ({path}): {last_err}")

    # ---------------- public (no auth) ----------------
    def get_current_price(self, ticker: str) -> float:
        data = self._request("GET", "/v1/ticker", {"markets": ticker}, auth=False)
        return float(data[0]["trade_price"])

    # ---------------- accounts ----------------
    def get_accounts(self) -> List[dict]:
        return self._request("GET", "/v1/accounts")  # type: ignore[return-value]

    def get_krw_balance(self) -> float:
        for a in self.get_accounts():
            if a.get("currency") == "KRW":
                return float(a.get("balance", 0))
        return 0.0

    def get_coin_balance(self, ticker: str):
        cur = ticker.split("-")[-1]
        for a in self.get_accounts():
            if a.get("currency") == cur:
                qty = float(a.get("balance", 0))
                avg = float(a.get("avg_buy_price", 0) or 0)
                return qty, avg
        return 0.0, 0.0

    def get_all_balances(self) -> Dict[str, dict]:
        out: Dict[str, dict] = {}
        for a in self.get_accounts():
            cur = a.get("currency")
            qty = float(a.get("balance", 0) or 0)
            if cur == "KRW" or qty <= 0:
                continue
            ticker = f"KRW-{cur}"
            avg = float(a.get("avg_buy_price", 0) or 0)
            try:
                price = self.get_current_price(ticker)
            except Exception:
                price = avg
            out[ticker] = {"qty": qty, "avg_price": avg, "value_krw": qty * price, "price": price}
        return out

    def get_total_equity_krw(self) -> float:
        """총 평가액 = 원화 + 코인 평가액. (일일 손실 판정의 기준 — 과거 오탐 버그 수정본)"""
        krw = self.get_krw_balance()
        total = 0.0
        try:
            for info in self.get_all_balances().values():
                total += float(info.get("value_krw", 0))
        except Exception as e:
            log.warning("코인 평가액 조회 실패: %s", e)
        return krw + total

    # ---------------- orders ----------------
    @staticmethod
    def _new_order_id(prefix: str = "bdc") -> str:
        """업비트 identifier — 재시도/중복 주문 방지용 멱등 키."""
        return f"{prefix}-{uuid.uuid4().hex[:24]}"

    def market_buy(self, ticker: str, amount_krw: float,
                   client_order_id: Optional[str] = None) -> dict:
        amount_krw = int(amount_krw)
        if amount_krw < MIN_ORDER_KRW:
            raise UpbitError(f"최소 주문금액 미달: {amount_krw:,} KRW < {int(MIN_ORDER_KRW):,}")
        body = {"market": ticker, "side": "bid", "price": str(amount_krw),
                "ord_type": "price", "identifier": client_order_id or self._new_order_id()}
        # body 는 한 번만 만들어 재시도 전체에 같은 identifier 를 보낸다(이중 주문 방지)
        return self._request("POST", "/v1/orders", body=body)  # type: ignore[return-value]

    def market_sell(self, ticker: str, qty: float,
                    client_order_id: Optional[str] = None) -> dict:
        if qty <= 0:
            raise UpbitError("매도 수량이 0 이하입니다")
        body = {"market": ticker, "side": "ask", "volume": f"{qty:.8f}",
                "ord_type": "market", "identifier": client_order_id or self._new_order_id()}
        return self._request("POST", "/v1/orders", body=body)  # type: ignore[return-value]

    def market_sell_amount(self, ticker: str, amount_krw: float) -> dict:
        """금액 기준 시장가 매도 — 현재가로 수량 환산 (under_min_total_market_ask 회피)."""
        price = self.get_current_price(ticker)
        qty = amount_krw / price if price > 0 else 0.0
        return self.market_sell(ticker, qty)

    def limit_buy(self, ticker: str, price: float, qty: float) -> dict:
        body = {"market": ticker, "side": "bid", "price": str(price),
                "volume": f"{qty:.8f}", "ord_type": "limit"}
        return self._request("POST", "/v1/orders", body=body)  # type: ignore[return-value]

    def limit_sell(self, ticker: str, price: float, qty: float) -> dict:
        body = {"market": ticker, "side": "ask", "price": str(price),
                "volume": f"{qty:.8f}", "ord_type": "limit"}
        return self._request("POST", "/v1/orders", body=body)  # type: ignore[return-value]

    def cancel_order(self, uuid_: str) -> dict:
        return self._request("DELETE", "/v1/order", {"uuid": uuid_})  # type: ignore[return-value]

    def get_order(self, uuid_: str) -> dict:
        return self._request("GET", "/v1/order", {"uuid": uuid_})  # type: ignore[return-value]


if __name__ == "__main__":
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from config import Config
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cfg = Config.load()
    if not cfg.has_upbit():
        print("UPBIT 키 미설정 — .env 를 먼저 채우세요.")
        raise SystemExit(1)
    c = UpbitClient(cfg.upbit_access_key, cfg.upbit_secret_key)
    print(f"원화 잔고 : {c.get_krw_balance():,.0f} KRW")
    print(f"총 평가액 : {c.get_total_equity_krw():,.0f} KRW")
    for t, info in c.get_all_balances().items():
        print(f"  {t:<10} {info['qty']:.8f}개  평단 {info['avg_price']:,.2f}  평가 {info['value_krw']:,.0f}")

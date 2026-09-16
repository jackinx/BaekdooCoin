"""
백두코인봇 — 업비트 API 클라이언트
===============================

업비트 REST API v1 래퍼: 잔고조회, 시장가/지정가 매수, 시장가/지정가 매도.
PyJWT 기반 인증 헤더 생성 + 재시도 + Rate Limit 대응.

사용법:
    from upbit_client import UpbitClient
    client = UpbitClient(config)
    balance = client.get_krw_balance()
    order = client.market_buy("KRW-BTC", 100000)  # 10만원어치 매수
"""

from __future__ import annotations

import hashlib
import logging
import time
import uuid
from typing import Optional
from urllib.parse import urlencode

import jwt
import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://api.upbit.com/v1"


class UpbitError(RuntimeError):
    """업비트 API 오류."""


class UpbitClient:
    """업비트 REST API 클라이언트.

    JWT 인증 + 지수 백오프 재시도 + Rate Limit 감지.
    """

    def __init__(self, access_key: str, secret_key: str):
        """
        Parameters
        ----------
        access_key : str
            업비트 Open API Access Key.
        secret_key : str
            업비트 Open API Secret Key.
        """
        self.access_key = access_key
        self.secret_key = secret_key
        self.session = requests.Session()
        self._last_call: float = 0.0
        self._min_interval: float = 0.05  # 초당 최대 20회 (여유 있게)

    # ------------------------------------------------------------------
    # 인증
    # ------------------------------------------------------------------
    def _generate_token(self, query: Optional[dict] = None) -> str:
        """JWT 인증 토큰 생성.

        Parameters
        ----------
        query : dict, optional
            쿼리 파라미터 딕셔너리.

        Returns
        -------
        str
            Bearer 토큰.
        """
        payload = {
            "access_key": self.access_key,
            "nonce": str(uuid.uuid4()),
        }
        if query:
            # SHA512 해시를 쿼리 파라미터로 사용
            query_str = urlencode(query)
            hash_obj = hashlib.sha512(query_str.encode())
            payload["query_hash"] = hash_obj.hexdigest()
            payload["query_hash_alg"] = "SHA512"
        return jwt.encode(payload, self.secret_key, algorithm="HS256")

    def _headers(self, query: Optional[dict] = None) -> dict:
        token = self._generate_token(query)
        return {"Authorization": f"Bearer {token}"}

    # ------------------------------------------------------------------
    # Rate Limit + 재시도
    # ------------------------------------------------------------------
    def _rate_limit(self) -> None:
        now = time.monotonic()
        wait = self._min_interval - (now - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def _request(self, method: str, path: str,
                 params: Optional[dict] = None,
                 data: Optional[dict] = None,
                 retries: int = 3) -> dict:
        """인증된 API 요청 (지수 백오프 재시도).

        Parameters
        ----------
        method : str
            HTTP 메서드 (GET, POST, DELETE).
        path : str
            API 경로 (/v1/accounts 등).
        params : dict, optional
            쿼리 파라미터.
        data : dict, optional
            JSON 바디 (POST 요청).
        retries : int
            최대 재시도 횟수.

        Returns
        -------
        dict
            JSON 응답.
        """
        url = f"{BASE_URL}{path}"
        for attempt in range(retries):
            self._rate_limit()
            try:
                headers = self._headers(params if method == "GET" else None)
                if data:
                    headers["Content-Type"] = "application/json"
                    # POST 요청은 body도 JWT payload에 포함 (query_hash와 동일 방식)
                    body_str = urlencode(data)
                    hash_obj = hashlib.sha512(body_str.encode())
                    payload_for_token = {
                        "access_key": self.access_key,
                        "nonce": str(uuid.uuid4()),
                        "query_hash": hash_obj.hexdigest(),
                        "query_hash_alg": "SHA512",
                    }
                    token = jwt.encode(payload_for_token, self.secret_key, algorithm="HS256")
                    headers["Authorization"] = f"Bearer {token}"
                    resp = self.session.post(url, headers=headers, json=data, timeout=15)
                elif method == "DELETE":
                    resp = self.session.delete(url, headers=headers, timeout=15)
                else:
                    resp = self.session.get(url, headers=headers, params=params, timeout=15)

                if resp.status_code == 429:
                    retry_after = float(resp.headers.get("Retry-After", 1))
                    logger.warning("Rate limit — %.1fs 대기 (시도 %d/%d)", retry_after, attempt + 1, retries)
                    time.sleep(retry_after)
                    self._last_call = time.monotonic()
                    continue

                if resp.status_code >= 400:
                    logger.error("Upbit API 오류 (HTTP %d): %s", resp.status_code, resp.text[:300])
                    if attempt + 1 < retries:
                        delay = 2 ** attempt
                        time.sleep(delay)
                        continue
                    raise UpbitError(f"API 오류 (HTTP {resp.status_code}): {resp.text[:200]}")

                return resp.json()

            except (requests.ConnectionError, requests.Timeout) as e:
                logger.warning("네트워크 오류 (시도 %d/%d): %s", attempt + 1, retries, e)
                if attempt + 1 < retries:
                    time.sleep(2 ** attempt)
                    continue
                raise UpbitError(f"네트워크 오류 ({retries}회 실패): {e}")

        raise UpbitError(f"API 요청 실패 ({retries}회 재시도)")

    # ------------------------------------------------------------------
    # 잔고 조회
    # ------------------------------------------------------------------
    def get_accounts(self) -> list[dict]:
        """전체 계좌 잔고 조회.

        Returns
        -------
        list[dict]
            [{"currency": "KRW", "balance": "1000000.0", "locked": "0.0", ...}, ...]
        """
        return self._request("GET", "/accounts")

    def get_krw_balance(self) -> float:
        """원화 잔고 조회.

        Returns
        -------
        float
            주문 가능한 원화 잔고.
        """
        accounts = self.get_accounts()
        for acc in accounts:
            if acc["currency"] == "KRW":
                return float(acc["balance"])
        return 0.0

    def get_coin_balance(self, ticker: str) -> tuple[float, float]:
        """특정 코인의 보유 수량과 평균 매수가.

        Parameters
        ----------
        ticker : str
            업비트 티커 (예: "KRW-BTC").

        Returns
        -------
        tuple[float, float]
            (보유수량, 평균매수가). 없으면 (0, 0).
        """
        coin = ticker.replace("KRW-", "")
        accounts = self.get_accounts()
        for acc in accounts:
            if acc["currency"] == coin:
                qty = float(acc["balance"])
                avg = float(acc.get("avg_buy_price", 0))
                return qty, avg
        return 0.0, 0.0

    def get_all_balances(self) -> dict:
        """모든 코인 잔고 조회 (원화 제외).

        Returns
        -------
        dict
            {ticker: {"qty": float, "avg_price": float, "value_krw": float}}
        """
        accounts = self.get_accounts()
        result = {}
        for acc in accounts:
            currency = acc["currency"]
            if currency == "KRW":
                continue
            qty = float(acc["balance"])
            if qty <= 0:
                continue
            avg = float(acc.get("avg_buy_price", 0))
            # 현재가 조회
            try:
                ticker = f"KRW-{currency}"
                price = self.get_current_price(ticker)
                value = qty * price
            except Exception:
                value = qty * avg
            result[f"KRW-{currency}"] = {"qty": qty, "avg_price": avg, "value_krw": value}
        return result

    def get_total_equity_krw(self) -> float:
        """총 평가액 조회 (원화 현금 + 보유 코인 현재 평가액 합산).

        Returns
        -------
        float
            원화 잔고 + 모든 보유 코인의 현재가 평가액 합계.
        """
        krw = self.get_krw_balance()
        coin_total = 0.0
        try:
            balances = self.get_all_balances()
            for info in balances.values():
                coin_total += float(info.get("value_krw", 0))
        except Exception:
            pass
        return krw + coin_total

    # ------------------------------------------------------------------
    # 주문
    # ------------------------------------------------------------------
    def get_current_price(self, ticker: str) -> float:
        """현재가 조회.

        Parameters
        ----------
        ticker : str
            업비트 티커.

        Returns
        -------
        float
            현재가.
        """
        data = self._request("GET", "/ticker", params={"markets": ticker})
        return float(data[0]["trade_price"])

    def market_buy(self, ticker: str, amount_krw: float,
                   client_order_id: Optional[str] = None) -> dict:
        """시장가 매수 (원화 금액 지정).

        Parameters
        ----------
        ticker : str
            업비트 티커.
        amount_krw : float
            매수할 원화 금액 (최소 5,000 KRW).
        client_order_id : str, optional
            클라이언트 주문 ID (멱등성 보장).

        Returns
        -------
        dict
            주문 응답 {uuid, side, ord_type, price, state, ...}
        """
        if amount_krw < 5000:
            raise UpbitError(f"최소 주문 금액은 5,000 KRW입니다 (입력: {amount_krw:.0f})")
        data = {
            "market": ticker,
            "side": "bid",
            "ord_type": "price",
            "price": str(int(amount_krw)),
        }
        if client_order_id:
            data["identifier"] = client_order_id
        # logger.info("시장가 매수: %s, ₩%,.0f", ticker, amount_krw)
        logger.info("시장가 매수: %s, ₩%d", ticker, int(amount_krw))
        return self._request("POST", "/orders", data=data)

    def market_sell(self, ticker: str, qty: float,
                    client_order_id: Optional[str] = None) -> dict:
        """시장가 매도 (수량 지정).

        Parameters
        ----------
        ticker : str
            업비트 티커.
        qty : float
            매도할 코인 수량.
        client_order_id : str, optional
            클라이언트 주문 ID.

        Returns
        -------
        dict
            주문 응답.
        """
        data = {
            "market": ticker,
            "side": "ask",
            "ord_type": "market",
            "volume": str(qty),
        }
        if client_order_id:
            data["identifier"] = client_order_id
        logger.info("시장가 매도: %s, %.6f 수량", ticker, qty)
        return self._request("POST", "/orders", data=data)

    def market_sell_amount(self, ticker: str, amount_krw: float,
                           client_order_id: Optional[str] = None) -> dict:
        """Sell by KRW amount (not coin quantity). 
        Gets current price, calculates coin qty, then market sells."""
        price = self.get_current_price(ticker)
        qty = amount_krw / price
        return self.market_sell(ticker, qty, client_order_id)

    def limit_buy(self, ticker: str, price: float, qty: float,
                  client_order_id: Optional[str] = None) -> dict:
        """지정가 매수.

        Parameters
        ----------
        ticker : str
            업비트 티커.
        price : float
            주문 가격 (KRW).
        qty : float
            주문 수량.
        client_order_id : str, optional
            클라이언트 주문 ID.

        Returns
        -------
        dict
            주문 응답.
        """
        data = {
            "market": ticker,
            "side": "bid",
            "ord_type": "limit",
            "price": str(int(price)),
            "volume": str(qty),
        }
        if client_order_id:
            data["identifier"] = client_order_id
        # logger.info("지정가 매수: %s, ₩%,.0f × %.6f", ticker, price, qty)
        logger.info("지정가 매수: %s, ₩%d × %.6f", ticker, int(price), qty)
        return self._request("POST", "/orders", data=data)

    def limit_sell(self, ticker: str, price: float, qty: float,
                   client_order_id: Optional[str] = None) -> dict:
        """지정가 매도.

        Parameters
        ----------
        ticker : str
            업비트 티커.
        price : float
            주문 가격 (KRW).
        qty : float
            주문 수량.
        client_order_id : str, optional
            클라이언트 주문 ID.

        Returns
        -------
        dict
            주문 응답.
        """
        data = {
            "market": ticker,
            "side": "ask",
            "ord_type": "limit",
            "price": str(int(price)),
            "volume": str(qty),
        }
        if client_order_id:
            data["identifier"] = client_order_id
        # logger.info("지정가 매도: %s, ₩%,.0f × %.6f", ticker, price, qty)
        logger.info("지정가 매도: %s, ₩%d × %.6f", ticker, int(price), qty)
        return self._request("POST", "/orders", data=data)

    def cancel_order(self, order_uuid: str) -> dict:
        """주문 취소."""
        return self._request("DELETE", "/order", params={"uuid": order_uuid})

    # ------------------------------------------------------------------
    # 진단
    # ------------------------------------------------------------------
    def check_connection(self) -> bool:
        """API 연결 확인."""
        try:
            accounts = self.get_accounts()
            krw = self.get_krw_balance()
            logger.info("업비트 연결 성공 — 원화 잔고: ₩%s, 보유종목: %d개",
                        f"{krw:,.0f}", len([a for a in accounts if a["currency"] != "KRW"]))
            return True
        except Exception as e:
            logger.error("업비트 연결 실패: %s", e)
            return False


if __name__ == "__main__":
    import sys
    sys.path.insert(0, ".")
    from config import Config

    logging.basicConfig(level=logging.INFO)
    cfg = Config.load()
    client = UpbitClient(cfg.upbit_access_key, cfg.upbit_secret_key)
    ok = client.check_connection()
    if ok:
        balances = client.get_all_balances()
        print(f"\n=== 보유 종목: {len(balances)}개 ===")
        for ticker, info in balances.items():
            print(f"  {ticker}: {info['qty']:.6f} (₩{info['value_krw']:,.0f})")
    else:
        print("❌ 연결 실패 — 액세스 키 확인 필요")
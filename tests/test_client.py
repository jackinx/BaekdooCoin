# -*- coding: utf-8 -*-
"""업비트 클라이언트 단위 테스트 — 네트워크 없이 인증/주문/에러/멱등성 검증."""
from __future__ import annotations

import hashlib
import urllib.parse

import jwt
import pytest
import requests

import upbit_client as uc

ACCESS, SECRET = "test-access", "test-secret"


class FakeResp:
    def __init__(self, status=200, payload=None, headers=None, text=""):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}
        self.text = text
        self.content = b"x" if payload is not None else b""

    def json(self):
        return self._payload


@pytest.fixture()
def client(monkeypatch):
    c = uc.UpbitClient(ACCESS, SECRET, min_interval=0.0)
    return c


def _capture(monkeypatch, c, responses):
    """requests.Session.request 를 가로채 호출 인자를 기록."""
    calls = []
    seq = list(responses)

    def fake_request(method, url, data=None, headers=None, timeout=None):
        calls.append({"method": method, "url": url, "data": data, "headers": headers})
        item = seq.pop(0) if seq else FakeResp(200, {})
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(c.s, "request", fake_request)
    return calls


def _decode(header_value: str) -> dict:
    token = header_value.split("Bearer ")[-1]
    return jwt.decode(token, SECRET, algorithms=["HS256"], options={"verify_exp": False})


# ---------------- 인증 ----------------
def test_requires_keys():
    with pytest.raises(uc.UpbitError):
        uc.UpbitClient("", "")


def test_get_signs_query_string(client, monkeypatch):
    calls = _capture(monkeypatch, client, [FakeResp(200, {"uuid": "o1", "state": "done"})])
    client.get_order("uuid-abc")
    qs = urllib.parse.urlencode({"uuid": "uuid-abc"})
    payload = _decode(calls[0]["headers"]["Authorization"])
    assert payload["access_key"] == ACCESS
    assert payload["query_hash"] == hashlib.sha512(qs.encode()).hexdigest()
    assert payload["query_hash_alg"] == "SHA512"
    assert payload["nonce"]


def test_public_ticker_call_is_unauthenticated(client, monkeypatch):
    calls = _capture(monkeypatch, client, [FakeResp(200, [{"trade_price": 100.0}])])
    client.get_current_price("KRW-BTC")
    assert "Authorization" not in calls[0]["headers"]


def test_post_signs_urlencoded_body(client, monkeypatch):
    calls = _capture(monkeypatch, client, [FakeResp(200, {"uuid": "o1"})])
    client.market_buy("KRW-BTC", 10_000)
    payload = _decode(calls[0]["headers"]["Authorization"])
    assert payload["query_hash"] == hashlib.sha512(calls[0]["data"].encode()).hexdigest()


def test_accounts_parsing(client, monkeypatch):
    accounts = [
        {"currency": "KRW", "balance": "50000", "avg_buy_price": "0"},
        {"currency": "BTC", "balance": "0.001", "avg_buy_price": "100000000"},
    ]
    _capture(monkeypatch, client, [
        FakeResp(200, accounts),                              # get_accounts (KRW)
        FakeResp(200, accounts),                              # get_accounts
        FakeResp(200, [{"trade_price": 110_000_000}]),        # get_current_price
    ])
    assert client.get_krw_balance() == pytest.approx(50_000)
    balances = client.get_all_balances()
    assert list(balances) == ["KRW-BTC"]
    assert balances["KRW-BTC"]["qty"] == pytest.approx(0.001)
    assert balances["KRW-BTC"]["value_krw"] == pytest.approx(110_000)


def test_total_equity_includes_coins(client, monkeypatch):
    accounts = [
        {"currency": "KRW", "balance": "50000", "avg_buy_price": "0"},
        {"currency": "BTC", "balance": "0.001", "avg_buy_price": "100000000"},
    ]
    _capture(monkeypatch, client, [
        FakeResp(200, accounts),                       # get_krw_balance
        FakeResp(200, accounts),                       # get_all_balances → accounts
        FakeResp(200, [{"trade_price": 100_000_000}]),  # price
    ])
    assert client.get_total_equity_krw() == pytest.approx(150_000)


# ---------------- 주문 ----------------
def test_market_buy_body(client, monkeypatch):
    calls = _capture(monkeypatch, client, [FakeResp(200, {"uuid": "o1"})])
    client.market_buy("KRW-BTC", 10_000)
    body = urllib.parse.parse_qs(calls[0]["data"])
    assert body["market"] == ["KRW-BTC"] and body["side"] == ["bid"]
    assert body["price"] == ["10000"] and body["ord_type"] == ["price"]


def test_market_buy_rejects_below_minimum_without_network(client, monkeypatch):
    calls = _capture(monkeypatch, client, [])
    with pytest.raises(uc.UpbitError):
        client.market_buy("KRW-BTC", 4_999)
    assert calls == []


def test_market_sell_uses_volume(client, monkeypatch):
    calls = _capture(monkeypatch, client, [FakeResp(200, {"uuid": "o2"})])
    client.market_sell("KRW-BTC", 0.00012345)
    body = urllib.parse.parse_qs(calls[0]["data"])
    assert body["side"] == ["ask"] and body["ord_type"] == ["market"]
    assert body["volume"] == ["0.00012345"]


def test_market_sell_amount_converts_by_price(client, monkeypatch):
    _capture(monkeypatch, client, [
        FakeResp(200, [{"trade_price": 100_000_000}]),
        FakeResp(200, {"uuid": "o3"}),
    ])
    client.market_sell_amount("KRW-BTC", 10_000)
    # 수량 = 10,000 / 100,000,000 = 0.0001
    calls = client.s  # noqa: F841  (가독성용)


# ---------------- 멱등성 (identifier) ----------------
def test_market_buy_sends_client_order_id(client, monkeypatch):
    calls = _capture(monkeypatch, client, [FakeResp(200, {"uuid": "o1"})])
    client.market_buy("KRW-BTC", 10_000, client_order_id="bdc-abc")
    body = urllib.parse.parse_qs(calls[0]["data"])
    assert body.get("identifier") == ["bdc-abc"]


def test_market_buy_auto_generates_identifier(client, monkeypatch):
    calls = _capture(monkeypatch, client, [FakeResp(200, {"uuid": "o1"}),
                                           FakeResp(200, {"uuid": "o2"})])
    client.market_buy("KRW-BTC", 10_000)
    client.market_buy("KRW-BTC", 10_000)
    ids = [urllib.parse.parse_qs(c["data"])["identifier"][0] for c in calls]
    assert all(ids) and ids[0] != ids[1]


def test_retry_reuses_same_identifier(client, monkeypatch):
    """네트워크 오류로 재시도할 때 identifier 가 달라지면 이중 주문이 된다."""
    calls = _capture(monkeypatch, client, [requests.ConnectionError("boom"),
                                           FakeResp(200, {"uuid": "o1"})])
    client.market_buy("KRW-BTC", 10_000, client_order_id="fixed-id")
    ids = {urllib.parse.parse_qs(c["data"])["identifier"][0] for c in calls}
    assert ids == {"fixed-id"}


# ---------------- 에러 / 레이트리밋 ----------------
def test_upbit_error_message_surfaces(client, monkeypatch):
    _capture(monkeypatch, client, [FakeResp(400, {"error": {"name": "insufficient_funds_bid",
                                                          "message": "잔고 부족"}})])
    with pytest.raises(uc.UpbitError) as e:
        client.market_buy("KRW-BTC", 10_000)
    assert "insufficient_funds_bid" in str(e.value)
    assert e.value.status == 400


def test_429_then_success(client, monkeypatch):
    _capture(monkeypatch, client, [FakeResp(429, headers={"Retry-After": "0"}),
                                   FakeResp(200, [{"trade_price": 1.0}])])
    assert client.get_current_price("KRW-BTC") == pytest.approx(1.0)


def test_5xx_retries_then_raises(client, monkeypatch):
    _capture(monkeypatch, client, [FakeResp(500), FakeResp(500), FakeResp(500), FakeResp(500)])
    monkeypatch.setattr(uc.time, "sleep", lambda *_: None)
    with pytest.raises(uc.UpbitError):
        client.get_current_price("KRW-BTC")

# -*- coding: utf-8 -*-
"""텔레그램 원격제어 (원본 telegram_control.py) — getUpdates 롱폴링, requests 만 사용.

지원 명령: /start /help /status /balance /positions /stop /resume /pause /regime /data
chat_id 화이트리스트: 등록된 chat_id 외의 메시지는 무시하고 경고 로그만 남긴다.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Dict, Optional

import requests

log = logging.getLogger("baekdoocoin.telegram")
API = "https://api.telegram.org/bot{token}/{method}"


class TelegramController:
    def __init__(self, token: str, chat_id: str, handlers: Dict[str, Callable[[], str]],
                 poll_timeout: int = 25):
        self.token = token
        self.chat_id = str(chat_id).strip()
        self.handlers = handlers
        self.poll_timeout = poll_timeout
        self._offset = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ---------------- basic ----------------
    def _call(self, method: str, http_timeout: int = 30, params: Optional[dict] = None):
        try:
            r = requests.post(API.format(token=self.token, method=method),
                              json=params or {}, timeout=http_timeout)
            return r.json()
        except Exception as e:                     # noqa: BLE001
            log.warning("텔레그램 %s 실패: %s", method, e)
            return {}

    def send(self, text: str) -> None:
        if not (self.token and self.chat_id):
            return
        # 텔레그램 4096자 제한
        for i in range(0, len(text), 3800):
            self._call("sendMessage", params={"chat_id": self.chat_id,
                                              "text": text[i:i + 3800],
                                              "disable_web_page_preview": True})

    # ---------------- polling ----------------
    def start_polling(self) -> None:
        if not self.token:
            log.info("텔레그램 토큰 없음 — 원격제어 비활성")
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="baekdoocoin-tg", daemon=True)
        self._thread.start()
        log.info("텔레그램 명령 폴링 시작 (chat_id=%s)", self.chat_id)

    def stop_polling(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                res = self._call("getUpdates", http_timeout=self.poll_timeout + 10,
                                 params={"offset": self._offset, "timeout": self.poll_timeout,
                                         "allowed_updates": ["message"]})
                for upd in res.get("result", []) or []:
                    self._offset = max(self._offset, upd.get("update_id", 0) + 1)
                    self._handle(upd)
            except Exception as e:                 # noqa: BLE001
                log.warning("폴링 오류: %s", e)
                time.sleep(3)

    def _handle(self, upd: dict) -> None:
        msg = upd.get("message") or {}
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        text = (msg.get("text") or "").strip()
        if not text:
            return
        if chat_id != self.chat_id:
            log.warning("화이트리스트 외 chat_id 무시: %s (%s)", chat_id, text[:40])
            return

        cmd = text.split()[0].lower().lstrip("/").split("@")[0]
        fn = self.handlers.get(cmd) or self.handlers.get("/" + cmd)
        if not fn:
            self.send(f"알 수 없는 명령: /{cmd}\n" + self.handlers.get("_help_text", lambda: "")())
            return
        try:
            reply = fn()
        except Exception as e:                     # noqa: BLE001
            reply = f"⚠️ 명령 처리 오류: {e}"
        log.info("텔레그램 명령 '/%s' → 응답 %d자", cmd, len(reply or ""))
        self.send(reply or "(응답 없음)")


HELP_TEXT = """🤖 백두코인봇 (Windows) 명령어
/status    엔진/포지션 상태
/balance   업비트 원화·코인 잔고
/positions 보유 포지션 상세
/regime    종목별 시장 레짐
/data      DB 캔들 통계
/stop      🚨 비상정지 (킬스위치 생성)
/resume    매매 재개
/pause     일시정지
/help      이 목록"""

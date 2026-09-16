"""
백두코인봇 텔레그램 원격제어
========================

텔레그램 봇을 통해 백두코인봇 엔진을 원격으로 제어합니다.

지원 명령어:
    /start      — 시작 메시지 + 도움말
    /status     — 현재 엔진 상태 (포지션, 레짐, 잔고)
    /balance    — 업비트 잔고 조회
    /stop       — 🚨 비상정지 (즉시 매매 중단)
    /resume     — 매매 재개
    /pause      — 일시 정지
    /help       — 도움말

보안:
    - chat_id 화이트리스트: 설정한 chat_id에서만 명령 수락
    - 모든 명령은 응답 로깅

사용법:
    from telegram_control import TelegramController
    ctrl = TelegramController(config)
    ctrl.start_polling()  # blocking — 별도 스레드에서 실행
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class TelegramController:
    """텔레그램 봇 폴링 기반 원격제어.

    단순 HTTP API만 사용 (python-telegram-bot 의존성 불필요).
    requests가 필요합니다.
    """

    def __init__(
        self,
        config,
        engine=None,
        *,
        get_status_cb: Optional[Callable[[], str]] = None,
        get_balance_cb: Optional[Callable[[], str]] = None,
        on_stop_cb: Optional[Callable[[], None]] = None,
        on_resume_cb: Optional[Callable[[], None]] = None,
        on_pause_cb: Optional[Callable[[], None]] = None,
    ):
        """
        Parameters
        ----------
        config : Config
            config.py에서 로드한 설정.
        engine : optional
            백두코인봇Engine 인스턴스 (kill switch 연동용).
        get_status_cb : callable, optional
            /status 호출 시 상태 문자열 반환.
        get_balance_cb : callable, optional
            /balance 호출 시 잔고 문자열 반환.
        on_stop_cb / on_resume_cb / on_pause_cb : callable, optional
            각 명령에 대한 콜백.
        """
        import requests  # 지연 임포트 (사용 시에만 필요)

        self._requests = requests
        self.config = config
        self.engine = engine
        self.token = config.telegram_bot_token
        self.chat_id = config.telegram_chat_id
        self._get_status = get_status_cb or self._default_status
        self._get_balance = get_balance_cb or (lambda: "잔고 조회 콜백 미설정")
        self._on_stop = on_stop_cb or self._default_stop
        self._on_resume = on_resume_cb or self._default_resume
        self._on_pause = on_pause_cb or self._default_pause

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._offset = 0

        if not self.token or not self.chat_id:
            raise ValueError(
                "텔레그램 설정 누락. .env에 TELEGRAM_BOT_TOKEN과 TELEGRAM_CHAT_ID 필요"
            )

    # ------------------------------------------------------------------
    # 저수준 API
    # ------------------------------------------------------------------
    def _api(self, method: str, **params) -> dict:
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        resp = self._requests.post(url, json=params, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def send_message(self, text: str) -> bool:
        """특정 chat_id로 메시지 전송."""
        try:
            self._api("sendMessage", chat_id=self.chat_id, text=text)
            return True
        except Exception as e:
            logger.error("텔레그램 전송 실패: %s", e)
            return False

    # ------------------------------------------------------------------
    # 기본 콜백 구현
    # ------------------------------------------------------------------
    def _default_status(self) -> str:
        if self.engine is None:
            return "엔진 미연결"
        try:
            pos = getattr(self.engine, "_open_positions", {})
            stopped = getattr(self.engine, "_emergency_stop_active", False)
            state = "🚨 비상정지됨" if stopped else "✅ 정상"
            lines = [f"백두코인봇 상태: {state}"]
            lines.append(f"열린 포지션: {len(pos)}개")
            for ticker, info in pos.items():
                lines.append(f"  • {ticker}: 진입가 ₩{info.get('entry_price', 0):,.0f}")
            return "\n".join(lines)
        except Exception as e:
            return f"상태 조회 오류: {e}"

    def _default_stop(self) -> None:
        if self.engine is not None:
            self.engine._emergency_stop_active = True
        # kill-switch 파일 생성 (엔진 파일기반 비상정지와 연동)
        try:
            from pathlib import Path
            Path("/tmp/baekdoocoin_emergency_stop").touch()
        except OSError:
            pass

    def _default_resume(self) -> None:
        if self.engine is not None:
            self.engine._emergency_stop_active = False
        try:
            from pathlib import Path
            p = Path("/tmp/baekdoocoin_emergency_stop")
            if p.exists():
                p.unlink()
        except OSError:
            pass

    def _default_pause(self) -> None:
        # pause는 engine의 trading_paused 플래그와 연동
        if self.engine is not None:
            setattr(self.engine, "_trading_paused", True)

    # ------------------------------------------------------------------
    # 명령 처리
    # ------------------------------------------------------------------
    def _handle_command(self, text: str) -> str:
        cmd = text.strip().lower()
        if cmd.startswith("/start") or cmd.startswith("/help"):
            return (
                "🤖 백두코인봇 원격제어\n\n"
                "/status — 엔진 상태\n"
                "/balance — 잔고 조회\n"
                "/pause — 일시정지\n"
                "/resume — 재개\n"
                "/stop — 🚨 비상정지\n"
            )
        if cmd.startswith("/status"):
            return self._get_status()
        if cmd.startswith("/balance"):
            return self._get_balance()
        if cmd.startswith("/stop"):
            self._on_stop()
            return "🚨 비상정지 실행됨. 매매 즉시 중단."
        if cmd.startswith("/resume"):
            self._on_resume()
            return "✅ 매매 재개됨."
        if cmd.startswith("/pause"):
            self._on_pause()
            return "⏸️ 일시정지됨."
        return "알 수 없는 명령. /help 참고"

    # ------------------------------------------------------------------
    # 폴링 루프
    # ------------------------------------------------------------------
    def _poll_loop(self) -> None:
        logger.info("텔레그램 폴링 시작 (chat_id=%s)", self.chat_id)
        while self._running:
            try:
                resp = self._api(
                    "getUpdates",
                    offset=self._offset,
                    timeout=30,
                )
                for update in resp.get("result", []):
                    self._offset = update["update_id"] + 1
                    msg = update.get("message", {})
                    # chat_id 화이트리스트 검증
                    if str(msg.get("chat", {}).get("id")) != str(self.chat_id):
                        logger.warning(
                            "허용되지 않은 chat_id에서 메시지: %s",
                            msg.get("chat", {}).get("id"),
                        )
                        continue
                    text = msg.get("text", "")
                    if text:
                        reply = self._handle_command(text)
                        self.send_message(reply)
            except Exception as e:
                logger.error("폴링 오류: %s", e)
                import time
                time.sleep(3)

    def start_polling(self) -> None:
        """폴링 스레드 시작 (non-blocking)."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def stop_polling(self) -> None:
        """폴링 중지."""
        self._running = False


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    from config import Config

    cfg = Config.load()
    ctrl = TelegramController(cfg)
    print(f"텔레그램 봇 시작 — chat_id: {cfg.telegram_chat_id}")
    print("Ctrl+C 로 종료")
    ctrl.start_polling()
    try:
        import time
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        ctrl.stop_polling()
        print("종료")

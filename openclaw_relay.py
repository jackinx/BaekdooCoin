"""
백두코인봇 OpenClaw 릴레이 (파일 기반 IPC)
======================================

Telegram Bot API 직접 호출을 완전히 제거한 경량 릴레이 모듈.

- 알림 전송: /tmp/baekdoocoin_relay/alerts.jsonl 에 JSON 라인 append
  → OpenClaw 크론(30초)이 읽어서 잭의 Telegram 세션으로 전달
- 명령 수신: /tmp/baekdoocoin_relay/command.json 을 폴링
  → OpenClaw 쪽(크론/메인 에이전트)이 Telegram 명령을 이 파일에 기록

어떤 종류의 네트워크 호출도 하지 않는다. 순수 파일 I/O only.

사용법:
    from openclaw_relay import OpenClawRelay
    relay = OpenClawRelay()
    relay.start()                      # no-op (API 호환용)
    relay.send_alert("🟢 시작됨")
    cmd = relay.get_pending_command()  # {"command": "status", ...} or None
    relay.stop()                       # no-op (API 호환용)
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# 릴레이 디렉터리
RELAY_DIR = Path("/tmp/baekdoocoin_relay")
ALERTS_FILE = RELAY_DIR / "alerts.jsonl"
COMMAND_FILE = RELAY_DIR / "command.json"


def _ensure_dir() -> None:
    """릴레이 디렉터리가 없으면 생성."""
    try:
        RELAY_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.error("릴레이 디렉터리 생성 실패 %s: %s", RELAY_DIR, e)


class OpenClawRelay:
    """파일 기반 IPC 릴레이 (telegram_control.TelegramController 대체).

    동일한 라이프사이클 API(start/stop/send_message)를 제공하지만
    실제 동작은 파일 읽기/쓰기뿐이다.
    """

    def __init__(self) -> None:
        _ensure_dir()

    # ------------------------------------------------------------------
    # 라이프사이클 (API 호환용 no-op)
    # ------------------------------------------------------------------
    def start(self) -> None:
        """호환용 no-op — 릴레이는 스레드/폴링이 필요 없음."""
        logger.info("OpenClawRelay started (file-based, dir=%s)", RELAY_DIR)

    def stop(self) -> None:
        """호환용 no-op."""
        logger.info("OpenClawRelay stopped")

    # ------------------------------------------------------------------
    # 알림 전송 (alerts.jsonl append)
    # ------------------------------------------------------------------
    def send_alert(self, text: str) -> bool:
        """알림 한 건을 alerts.jsonl에 JSON 라인으로 추가.

        Returns
        -------
        bool
            성공 시 True.
        """
        line = json.dumps({"ts": time.time(), "text": str(text)}, ensure_ascii=False)
        try:
            _ensure_dir()
            # O_APPEND 단일 write — 크론 리더와의 race 최소화
            fd = os.open(ALERTS_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            try:
                os.write(fd, (line + "\n").encode("utf-8"))
            finally:
                os.close(fd)
            logger.debug("alert queued: %s", text[:80])
            return True
        except OSError as e:
            logger.error("alert 쓰기 실패: %s", e)
            return False

    # telegram_control 호환 별칭
    send_message = send_alert

    # ------------------------------------------------------------------
    # 명령 수신 (command.json 폴링)
    # ------------------------------------------------------------------
    def get_pending_command(self) -> Optional[dict]:
        """미처리 명령이 있으면 dict로 반환하고 processed 마킹.

        command.json 형식:
            {"command": "status", "ts": 1755900000.0, "processed": false}

        - 파일 없음 / 파싱 불가 / 이미 processed → None
        - 미처리 명령 발견 시 즉시 {"processed": true}로 갱신 (재실행 방지)
        """
        if not COMMAND_FILE.exists():
            return None
        try:
            raw = COMMAND_FILE.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError) as e:
            logger.debug("command.json 읽기/파싱 실패: %s", e)
            return None
        if not isinstance(data, dict):
            return None
        if data.get("processed"):
            return None

        # processed 마킹 (원자적 교체)
        data["processed"] = True
        self._atomic_write_json(COMMAND_FILE, data)
        logger.info("명령 수신: %s", data.get("command"))
        return data

    def clear_command(self) -> None:
        """command.json 삭제."""
        try:
            COMMAND_FILE.unlink(missing_ok=True)
        except OSError as e:
            logger.debug("command.json 삭제 실패: %s", e)

    @staticmethod
    def _atomic_write_json(path: Path, data: dict) -> None:
        """임시 파일 + rename으로 원자적 쓰기."""
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=str(path.parent), prefix=".tmp_", suffix=".json"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False)
                os.replace(tmp_path, path)
            except BaseException:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except OSError as e:
            logger.error("원자적 쓰기 실패 %s: %s", path, e)


if __name__ == "__main__":
    # 간단 자체 테스트
    logging.basicConfig(level=logging.DEBUG)
    r = OpenClawRelay()
    r.start()
    r.send_alert("🧪 릴레이 자체 테스트 메시지")
    print(f"alerts: {ALERTS_FILE}")
    print(f"pending command: {r.get_pending_command()}")
    r.stop()

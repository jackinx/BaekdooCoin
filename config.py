"""
백두코인봇 설정 로더
================

.env 파일에서 API 키와 설정값을 읽어옵니다.
python-dotenv가 설치되어 있지 않으면 순수 파이썬으로 직접 파싱합니다.

사용법:
    from config import Config
    cfg = Config.load()
    cfg.upbit_access_key  # → 액세스 키
    cfg.telegram_bot_token  # → 텔레그램 봇 토큰
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv  # type: ignore

    _HAS_DOTENV = True
except ImportError:
    _HAS_DOTENV = False


def _parse_env_file(path: Path) -> dict[str, str]:
    """python-dotenv 없이 .env 파일을 수동 파싱 (경량 대체)."""
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        # 따옴표 제거
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        result[key] = value
    return result


@dataclass
class Config:
    """백두코인봇 설정값 컨테이너."""

    # 업비트
    upbit_access_key: str = ""
    upbit_secret_key: str = ""
    # 텔레그램
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    # 웹훅
    webhook_secret_key: str = ""
    # 서버
    port: int = 8080
    # 기타
    env_path: Path = field(default=Path(".env"), repr=False)

    @classmethod
    def load(cls, env_path: str | Path = ".env") -> "Config":
        """.env 파일과 실제 환경변수를 조합해 Config 생성.

        우선순위: 실제 환경변수 > .env 파일
        """
        path = Path(env_path)
        file_values = _parse_env_file(path)

        if _HAS_DOTENV:
            load_dotenv(path)

        def _get(key: str, default: str = "") -> str:
            # 1) 실제 환경변수 우선
            val = os.environ.get(key)
            if val:
                return val
            # 2) .env 파일 값
            return file_values.get(key, default)

        return cls(
            upbit_access_key=_get("UPBIT_ACCESS_KEY"),
            upbit_secret_key=_get("UPBIT_SECRET_KEY"),
            telegram_bot_token=_get("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=_get("TELEGRAM_CHAT_ID"),
            webhook_secret_key=_get("WEBHOOK_SECRET_KEY"),
            port=int(_get("PORT", "8080") or "8080"),
            env_path=path,
        )

    # ------------------------------------------------------------------
    # 검증 헬퍼
    # ------------------------------------------------------------------
    def has_upbit(self) -> bool:
        """업비트 API 키가 설정되었는지 확인."""
        return bool(self.upbit_access_key and self.upbit_secret_key)

    def has_telegram(self) -> bool:
        """텔레그램 봇 설정이 되었는지 확인."""
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    def missing_required(self) -> list[str]:
        """필수 설정 중 누락된 항목 목록."""
        missing = []
        if not self.upbit_access_key:
            missing.append("UPBIT_ACCESS_KEY")
        if not self.upbit_secret_key:
            missing.append("UPBIT_SECRET_KEY")
        if not self.telegram_bot_token:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not self.telegram_chat_id:
            missing.append("TELEGRAM_CHAT_ID")
        return missing


if __name__ == "__main__":
    cfg = Config.load()
    print("=== 백두코인봇 설정 확인 ===")
    print(f"업비트 액세스키 : {'설정됨 ✅' if cfg.upbit_access_key else '미설정 ❌'}")
    print(f"업비트 시크릿키 : {'설정됨 ✅' if cfg.upbit_secret_key else '미설정 ❌'}")
    print(f"텔레그램 봇토큰 : {'설정됨 ✅' if cfg.telegram_bot_token else '미설정 ❌'}")
    print(f"텔레그램 챗ID  : {'설정됨 ✅' if cfg.telegram_chat_id else '미설정 ❌'}")
    print(f"웹훅 시크릿    : {'설정됨 ✅' if cfg.webhook_secret_key else '미설정 ❌'}")
    print(f"포트           : {cfg.port}")
    missing = cfg.missing_required()
    if missing:
        print(f"\n⚠️  필수 설정 누락: {', '.join(missing)}")
        print("   .env 파일을 채워주세요 (cp .env.example .env)")
    else:
        print("\n✅ 필수 설정 모두 완료")

"""
백두코인봇 Upbit 트레이딩 전략 패키지
=============================

Turtle, Squeeze Momentum, BB+RSI Reversal 3종 전략을 포함한
업비트 암호화폐 현물 트레이딩 전략 모음.
"""

try:
    from .regime_detector import RegimeDetector, MarketRegime
except ImportError:                     # pragma: no cover - pandas 없는 환경
    RegimeDetector = None               # type: ignore[assignment]
    MarketRegime = None                 # type: ignore[assignment]

# ⚠️ pandas 가 없는 환경(예: Windows Smart App Control 로 pandas C확장이 차단된 호스트)에서도
#    `strategies.risk_mtf` 같은 비-pandas 모듈을 import 할 수 있도록 선택적으로 로드한다.
STRATEGY_REGISTRY: dict[str, type] = {}

try:
    from .turtle import TurtleStrategy
    STRATEGY_REGISTRY["turtle"] = TurtleStrategy
except ImportError:                     # pragma: no cover - 환경 의존
    TurtleStrategy = None               # type: ignore[assignment]

try:
    from .squeeze_momentum import SqueezeMomentumStrategy
    STRATEGY_REGISTRY["squeeze_momentum"] = SqueezeMomentumStrategy
except ImportError:                     # pragma: no cover
    SqueezeMomentumStrategy = None      # type: ignore[assignment]

try:
    from .bb_rsi_reversal import BBRSIReversalStrategy
    STRATEGY_REGISTRY["bb_rsi_reversal"] = BBRSIReversalStrategy
except ImportError:                     # pragma: no cover
    BBRSIReversalStrategy = None        # type: ignore[assignment]

__all__ = [
    "TurtleStrategy",
    "SqueezeMomentumStrategy",
    "BBRSIReversalStrategy",
    "RegimeDetector",
    "MarketRegime",
    "STRATEGY_REGISTRY",
]
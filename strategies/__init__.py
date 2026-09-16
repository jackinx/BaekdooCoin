"""
백두코인봇 Upbit 트레이딩 전략 패키지
=============================

Turtle, Squeeze Momentum, BB+RSI Reversal 3종 전략을 포함한
업비트 암호화폐 현물 트레이딩 전략 모음.
"""

from .turtle import TurtleStrategy
from .squeeze_momentum import SqueezeMomentumStrategy
from .bb_rsi_reversal import BBRSIReversalStrategy
from .regime_detector import RegimeDetector, MarketRegime

STRATEGY_REGISTRY: dict[str, type] = {
    "turtle": TurtleStrategy,
    "squeeze_momentum": SqueezeMomentumStrategy,
    "bb_rsi_reversal": BBRSIReversalStrategy,
}

__all__ = [
    "TurtleStrategy",
    "SqueezeMomentumStrategy",
    "BBRSIReversalStrategy",
    "RegimeDetector",
    "MarketRegime",
    "STRATEGY_REGISTRY",
]
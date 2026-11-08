"""Dynamic Strategy Registry and Plugin Loader."""

from typing import Any

from app.domains.cex.strategies.base import IStrategy
from app.domains.cex.strategies.bb_breakout import BollingerAdxBreakoutStrategy
from app.domains.cex.strategies.ema_rsi_atr import EmaRsiAtrTrendStrategy
from app.domains.cex.strategies.smart_dca import SmartDcaScaleInStrategy
from app.models.entities import StrategyConfig
from app.models.enums import MarketType


class StrategyRegistry:
    """Registry maintaining available built-in strategies and custom user strategy plugins."""

    _registry: dict[str, type[IStrategy]] = {
        "ema_rsi_atr": EmaRsiAtrTrendStrategy,
        "bb_breakout": BollingerAdxBreakoutStrategy,
        "bollinger_breakout": BollingerAdxBreakoutStrategy,
        "smart_dca": SmartDcaScaleInStrategy,
    }

    @classmethod
    def register(cls, key: str, strategy_cls: type[IStrategy]) -> None:
        """Register a new built-in strategy or custom plugin."""
        cls._registry[key.lower().strip()] = strategy_cls

    @classmethod
    def get_class(cls, key: str) -> type[IStrategy]:
        norm_key = key.lower().strip()
        if norm_key not in cls._registry:
            raise ValueError(
                f"Strategy '{key}' is not registered. Available: {list(cls._registry.keys())}"
            )
        return cls._registry[norm_key]

    @classmethod
    def list_available(cls) -> list[str]:
        return list(cls._registry.keys())

    @classmethod
    def create_instance(cls, config: StrategyConfig) -> IStrategy:
        """Instantiate an IStrategy instance from a StrategyConfig database model."""
        strat_cls = cls.get_class(config.strategy_class)
        m_type_raw = config.market_type or MarketType.FUTURES_USDT.value
        m_type = MarketType(m_type_raw)
        return strat_cls(
            strategy_id=config.id,
            name=config.name,
            market_type=m_type,
            symbols=config.symbols,
            timeframe=config.timeframe,
            parameters=config.parameters,
            risk_config=config.risk_config,
        )

"""Dry-run execution adapter; the cross-exchange coordinator owns decisions."""

import math
from datetime import datetime

from freqtrade.exceptions import OperationalException
from freqtrade.strategy import IStrategy
from pandas import DataFrame


class CrossExchangeArb(IStrategy):
    INTERFACE_VERSION = 3
    can_short = True
    timeframe = "1m"
    startup_candle_count = 0
    process_only_new_candles = True
    minimal_roi = {}
    stoploss = -0.10
    trailing_stop = False
    use_exit_signal = False
    position_adjustment_enable = False
    order_types = {
        "entry": "limit",
        "exit": "market",
        "force_entry": "limit",
        "force_exit": "market",
        "emergency_exit": "market",
        "stoploss": "market",
        "stoploss_on_exchange": False,
    }
    order_time_in_force = {"entry": "GTC", "exit": "GTC"}

    def bot_start(self, **kwargs) -> None:
        if self.config.get("dry_run") is not True:
            raise OperationalException("CrossExchangeArb supports dry-run only.")

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["enter_long"] = 0
        dataframe["enter_short"] = 0
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["exit_long"] = 0
        dataframe["exit_short"] = 0
        return dataframe

    def confirm_trade_entry(
        self,
        pair: str,
        order_type: str,
        amount: float,
        rate: float,
        time_in_force: str,
        current_time: datetime,
        entry_tag: str | None,
        side: str,
        **kwargs,
    ) -> bool:
        venue = self.config.get("exchange", {}).get("name")
        return (
            self.config.get("dry_run") is True
            and venue in {"bybit", "bitget"}
            and pair == "BTC/USDT:USDT"
            and order_type == "limit"
            and side in {"long", "short"}
            and math.isfinite(amount)
            and amount > 0
            and math.isfinite(rate)
            and rate > 0
            and isinstance(entry_tag, str)
            and entry_tag.startswith("ca1-")
            and entry_tag.endswith(f":{venue}")
        )

    def leverage(
        self,
        pair: str,
        current_time: datetime,
        current_rate: float,
        proposed_leverage: float,
        max_leverage: float,
        entry_tag: str | None,
        side: str,
        **kwargs,
    ) -> float:
        return 1.0

"""Offline exit candidates; actual paired fees always remain in the ledger.

absolute_net preserves the earlier fee-net exit rule without an extra hard cap.
entry_deterioration freezes an entry quote-only baseline and additionally limits
total cycle net loss against initial long-entry notional. Entry is unchanged.
"""
from dataclasses import asdict, dataclass
from decimal import Decimal
import math

D = Decimal
RISK_BASIS = "remaining_long_entry_notional"
RISK_PNL_BASIS = {"take_profit": "remaining_pair_net_before_funding",
                  "stop_loss": "remaining_pair_net_before_funding"}
RISK_MODES = ("absolute_net", "entry_deterioration")


def risk_pnl_basis(mode):
    if mode not in RISK_MODES:
        raise ValueError("invalid risk_mode")
    if mode == "absolute_net":
        return dict(RISK_PNL_BASIS)
    return {"take_profit": "remaining_pair_net_before_funding",
            "stop_loss": "remaining_pair_net_deterioration_from_entry_quotes",
            "hard_stop_loss": "realized_plus_remaining_pair_net_before_funding"}

@dataclass(frozen=True)
class Policy:
    max_notional: Decimal = D("250")
    bybit_fee_bps: Decimal = D("6")
    bitget_fee_bps: Decimal = D("6")
    min_entry_net_bps: Decimal = D("5")
    expected_exit_spread_bps: Decimal = D("2")
    funding_buffer_bps: Decimal = D("2")
    price_guard_bps: Decimal = D("1")
    take_profit_bps: Decimal = D("2")
    stop_loss_bps: Decimal = D("2")
    risk_mode: str = "absolute_net"
    hard_stop_loss_bps: Decimal = D("100")
    max_total_loss_usdt: Decimal = D("10")
    entry_timeout_s: float = 5.0
    max_holding_s: float = 60.0
    cooldown_s: float = 30.0
    max_quote_age_ms: int = 2000
    max_quote_skew_ms: int = 500
    max_request_ms: int = 1500
    max_cycles: int = 3

    def __post_init__(self) -> None:
        if self.risk_mode not in RISK_MODES:
            raise ValueError("invalid policy: risk_mode")
        for name, value in asdict(self).items():
            if name == "risk_mode":
                continue
            if isinstance(value, Decimal):
                if not value.is_finite() or value < 0:
                    raise ValueError(f"invalid policy: {name}")
            elif not math.isfinite(value) or value <= 0:
                raise ValueError(f"invalid policy: {name}")
        if min(self.max_notional, self.stop_loss_bps, self.take_profit_bps,
               self.hard_stop_loss_bps, self.max_total_loss_usdt) <= 0:
            raise ValueError("positive capital and loss caps required")
        if self.max_holding_s > 3600:
            raise ValueError("this short-horizon pilot is limited to one hour")

    def fee(self, venue: str) -> Decimal:
        return (self.bybit_fee_bps if venue == "bybit" else self.bitget_fee_bps) / D(10000)

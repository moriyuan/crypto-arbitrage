"""Bound equal-coin paper fills by the two execution-side books.

Capacities are base-coin quantities, not historical turnover or USDT thresholds.
This module is stateless: callers must track depth already used on each book
version so repeated decisions cannot spend the same displayed liquidity twice.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal, localcontext
from typing import Literal

from .market import Book, MarketDataError, Rules

D = Decimal
_VENUES = {"bybit", "bitget"}


@dataclass(frozen=True)
class DepthSizingConfig:
    band_bps: Decimal = D(10)
    participation: Decimal = D(".25")

    def __post_init__(self) -> None:
        if (
            not isinstance(self.band_bps, Decimal)
            or not self.band_bps.is_finite()
            or not 0 < self.band_bps < 10000
        ):
            raise ValueError("depth band_bps must be a finite Decimal within (0, 10000)")
        if (
            not isinstance(self.participation, Decimal)
            or not self.participation.is_finite()
            or not 0 < self.participation <= 1
        ):
            raise ValueError("depth participation must be a finite Decimal within (0, 1]")


def paired_depth_details(
    books: dict[str, Book],
    long: str,
    short: str,
    intent: Literal["entry", "exit"],
    config: DepthSizingConfig,
) -> dict[str, Decimal]:
    """Return each leg's usable base quantity inside an inclusive top-price band.

    Entry buys on the long venue and sells on the short venue. Exit reverses
    both sides. Freshness and remaining position limits belong to the caller.
    """
    if (
        not isinstance(books, dict)
        or set(books) != _VENUES
        or {long, short} != _VENUES
        or intent not in {"entry", "exit"}
        or not isinstance(config, DepthSizingConfig)
        or any(not isinstance(book, Book) or book.venue != venue for venue, book in books.items())
    ):
        raise MarketDataError("depth sizing requires a complete, matching two-venue book pair")
    result: dict[str, Decimal] = {}
    with localcontext() as context:
        context.prec = max(
            80,
            len(config.band_bps.as_tuple().digits) + 50,
            len(config.participation.as_tuple().digits) + 50,
        )
        band = config.band_bps / D(10000)
        for venue in (long, short):
            buy = (venue == long) == (intent == "entry")
            levels = books[venue].asks if buy else books[venue].bids
            boundary = levels[0][0] * (1 + band if buy else 1 - band)
            quantity = sum(
                (qty for price, qty in levels if (price <= boundary if buy else price >= boundary)),
                D(0),
            )
            result[venue] = quantity * config.participation
    return result


def paired_capacity(
    books: dict[str, Book],
    long: str,
    short: str,
    intent: Literal["entry", "exit"],
    config: DepthSizingConfig,
) -> Decimal:
    """Use the smaller leg's capacity so both simulated legs trade equal coins."""
    return min(paired_depth_details(books, long, short, intent, config).values())


def round_pair_qty(qty: Decimal, rules: dict[str, Rules]) -> Decimal:
    """Floor to a quantity accepted by both venues' quantity-step grids.

    Minimum quantity/notional and maximum notional remain separate checks; a
    zero result explicitly means no jointly executable quantity is available.
    """
    if not isinstance(qty, Decimal) or not qty.is_finite() or qty < 0:
        raise ValueError("quantity must be a finite nonnegative Decimal")
    if (
        not isinstance(rules, dict)
        or set(rules) != _VENUES
        or any(not isinstance(rule, Rules) for rule in rules.values())
    ):
        raise MarketDataError("quantity rounding requires both venue rules")
    steps = [rule.qty_step for rule in rules.values()]
    digits = max(max(0, -int(step.as_tuple().exponent)) for step in steps)
    with localcontext() as context:
        context.prec = max(80, len(qty.as_tuple().digits) + max(0, qty.adjusted()) + digits + 5)
        scale = D(10) ** digits
        grid = D(math.lcm(*(int(step * scale) for step in steps))) / scale
        return (qty / grid).to_integral_value(rounding=ROUND_FLOOR) * grid

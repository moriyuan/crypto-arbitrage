"""Append-only, ideal successful-fill replay of live public quotes; never sends orders.

Each account starts with independent collateral. PnL is before funding because
funding settlements and liquidation are not modelled by this bounded experiment.
"""

from __future__ import annotations

import copy
import csv
import hashlib
import json
import math
import os
import sqlite3
import uuid
from dataclasses import asdict
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any, Protocol, cast

from btc_cta.cross_arb.depth_sizing import DepthSizingConfig, round_pair_qty
from btc_cta.cross_arb.engine import VENUES, fresh
from risk_policy import Policy, RISK_BASIS, RISK_PNL_BASIS
from btc_cta.cross_arb.entry_filters import EntryFilterConfig, EntryFilters
from btc_cta.cross_arb.history_gate import HISTORY_SIGNAL_FIELDS
from btc_cta.cross_arb.market import Book, MarketDataError, Rules, vwap
from btc_cta.cross_arb.stream import validate_symbol

D = Decimal
MODE = "ideal_signal_fill"
DEFAULT_INITIAL_BALANCE = D("1000")
_TABLES = ("signals", "orders", "cycles", "equity", "events")


class HistoryGateLike(Protocol):
    """Historical gates are causal queries; cached results must be timestamp keyed."""

    def metadata(self) -> dict[str, Any]: ...

    def evaluate(self, symbol: str, now_ms: int) -> dict[str, Any]: ...


_HEADERS = {
    "signals": (
        "signal_id",
        "tick_id",
        "timestamp_ms",
        "symbol",
        "pair",
        "long_venue",
        "short_venue",
        "qty",
        "long_vwap",
        "short_vwap",
        "long_limit",
        "short_limit",
        "gross_edge_bps",
        "net_edge_bps",
        "threshold_bps",
        "entry_filter_passed",
        "entry_filter_reason",
        "bybit_volatility_bps",
        "bitget_volatility_bps",
        "long_depth_usdt",
        "short_depth_usdt",
        "volatility_samples",
        "volatility_required_samples",
        *HISTORY_SIGNAL_FIELDS,
        "depth_capacity_qty",
        "eligible",
        "selected",
        "action",
        "reason",
        "bybit_exchange_ts_ms",
        "bitget_exchange_ts_ms",
        "bybit_received_at_ms",
        "bitget_received_at_ms",
        "cycle_id",
        "mode",
    ),
    "orders": (
        "order_id",
        "cycle_id",
        "signal_id",
        "timestamp_ms",
        "symbol",
        "pair",
        "venue",
        "side",
        "position_side",
        "intent",
        "qty",
        "price",
        "limit_price",
        "notional",
        "fee",
        "fee_rate",
        "leverage",
        "status",
        "depth_capacity_qty",
        "book_exchange_ts_ms",
        "book_received_at_ms",
        "depth_side_fingerprint",
        "mode",
    ),
    "cycles": (
        "cycle_id",
        "signal_id",
        "symbol",
        "pair",
        "long_venue",
        "short_venue",
        "qty",
        "entry_timestamp_ms",
        "exit_timestamp_ms",
        "holding_ms",
        "long_entry_price",
        "short_entry_price",
        "long_exit_price",
        "short_exit_price",
        "gross_pnl",
        "total_fees",
        "funding",
        "net_before_funding",
        "bybit_net_before_funding",
        "bitget_net_before_funding",
        "exit_reason",
        "exit_parts",
        "mode",
    ),
    "equity": (
        "symbol",
        "pair",
        "tick_id",
        "timestamp_ms",
        "venue",
        "cash_balance",
        "reserved_margin",
        "available_balance",
        "unrealized_gross_pnl",
        "estimated_exit_fee",
        "equity",
        "liquidation_equity_before_funding",
        "mark_status",
        "cycle_id",
        "mode",
    ),
}


def _json(value: Any) -> str:
    return json.dumps(value, default=str, sort_keys=True, separators=(",", ":"))


def _common_grid(rules: dict[str, Rules]) -> Decimal:
    digits = max(max(0, -int(r.qty_step.as_tuple().exponent)) for r in rules.values())
    scale = D(10) ** digits
    return D(math.lcm(*(int(r.qty_step * scale) for r in rules.values()))) / scale


class PaperSession:
    """A fresh, exclusive SQLite session with independent 1x collateral accounts.

    ``tick`` is synchronous and commits signal, order, cycle and equity records
    together. Existing files are refused: restarting must use a new session,
    so an open position cannot silently disappear from an existing ledger. WAL
    with NORMAL synchronization keeps ticks atomic; a power failure can lose
    the latest commits. Run-end close checkpoints all completed records.
    """

    def __init__(
        self,
        path: Path,
        policy: Policy,
        initial_balance: Decimal = DEFAULT_INITIAL_BALANCE,
        *,
        symbol: str = "BTCUSDT",
        entry_filter: EntryFilterConfig | None = None,
        history_gate: HistoryGateLike | None = None,
        depth_sizing: DepthSizingConfig | None = None,
    ):
        if entry_filter is not None and not isinstance(entry_filter, EntryFilterConfig):
            raise ValueError("entry_filter must be EntryFilterConfig or None")
        self.entry_filter = EntryFilters(entry_filter) if entry_filter is not None else None
        if depth_sizing is not None and not isinstance(depth_sizing, DepthSizingConfig):
            raise ValueError("depth_sizing must be DepthSizingConfig or None")
        self.history_gate = history_gate
        self.depth_sizing = depth_sizing
        self.symbol = validate_symbol(symbol)
        self.pair = f"{self.symbol[:-4]}/USDT:USDT"
        if not isinstance(initial_balance, Decimal) or not initial_balance.is_finite():
            raise ValueError("initial_balance must be a finite Decimal")
        if initial_balance <= 0:
            raise ValueError("initial_balance must be positive")
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        self.path, self.policy, self.initial_balance = path, policy, initial_balance
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.closed = False
        self.state: dict[str, Any] = {
            "session_id": "paper-" + uuid.uuid4().hex,
            "symbol": self.symbol,
            "pair": self.pair,
            "mode": MODE,
            "funding_model": "not_modelled",
            "policy": asdict(policy),
            "risk_basis": RISK_BASIS,
            "risk_pnl_basis": dict(RISK_PNL_BASIS),
            "entry_filter": asdict(entry_filter) if entry_filter is not None else None,
            "history_gate": history_gate.metadata() if history_gate is not None else None,
            "depth_sizing": asdict(depth_sizing) if depth_sizing is not None else None,
            "depth_liquidity": {},
            "initial_balance_per_venue": str(initial_balance),
            "cash": dict.fromkeys(VENUES, str(initial_balance)),
            "cycle": None,
            "completed_cycles": 0,
            "closed_pair_net_before_funding": "0",
            "ticks": 0,
            "signal_evaluations": 0,
            "eligible_signals": 0,
            "orders": 0,
            "started_at_ms": None,
            "last_tick_ms": None,
            "cooldown_until_ms": 0,
            "stop_requested": False,
            "halt_reason": "",
            "last_equity": {},
            "status": "FLAT",
        }
        with self.db:
            self.db.execute(
                "CREATE TABLE latest_state (id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
            )
            for table in _TABLES:
                self.db.execute(
                    f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
                )
                for operation in ("UPDATE", "DELETE"):
                    self.db.execute(
                        f"CREATE TRIGGER immutable_{table}_{operation.lower()} "
                        f"BEFORE {operation} ON {table} BEGIN "
                        "SELECT RAISE(ABORT, 'append-only paper ledger'); END"
                    )
            self._append("events", {"kind": "session_created", "state": self.state})

    def _append(self, table: str, record: dict[str, Any]) -> None:
        if table not in _TABLES:
            raise ValueError("unknown ledger table")
        record = {"symbol": self.symbol, "pair": self.pair, **record}
        self.db.execute(f"INSERT INTO {table}(value) VALUES(?)", (_json(record),))

    def _observe_depth(self, books: dict[str, Book]) -> None:
        """Replenish only actual displayed increases on each execution-side level.

        A receipt timestamp or an update to the opposite side cannot replenish a
        consumed level. A decrease removes its corresponding participation budget.
        This deliberately conservative approximation is not an order-queue model.
        The budget lives in transactionally rolled-back session state.
        """
        if self.depth_sizing is None:
            return
        for venue, book in books.items():
            venue_state = self.state["depth_liquidity"].setdefault(venue, {})
            for side, levels in (("buy", book.asks), ("sell", book.bids)):
                old = venue_state.get(side, {})
                current = {}
                for price, qty in levels:
                    key = str(price.normalize())
                    prev = old.get(key, {"displayed": "0", "remaining": "0"})
                    remaining = max(
                        D(0),
                        D(prev["remaining"])
                        + (qty - D(prev["displayed"])) * self.depth_sizing.participation,
                    )
                    current[key] = {"displayed": str(qty), "remaining": str(remaining)}
                venue_state[side] = current

    def _depth_details(
        self, books: dict[str, Book], long: str, intent: str
    ) -> dict[str, dict[str, Any]]:
        assert self.depth_sizing is not None
        details = {}
        for venue in VENUES:
            buy = (venue == long) == (intent == "entry")
            side = "buy" if buy else "sell"
            levels = books[venue].asks if buy else books[venue].bids
            boundary = levels[0][0] * (
                1 + self.depth_sizing.band_bps / 10000
                if buy
                else 1 - self.depth_sizing.band_bps / 10000
            )
            in_band = [
                (price, qty)
                for price, qty in levels
                if (price <= boundary if buy else price >= boundary)
            ]
            budget = self.state["depth_liquidity"][venue][side]
            capacity = sum(
                (D(budget[str(price.normalize())]["remaining"]) for price, _ in in_band), D(0)
            )
            details[venue] = {
                "side": side,
                "levels": in_band,
                "capacity": capacity,
                "fingerprint": hashlib.sha256(_json(in_band).encode()).hexdigest(),
                "book_exchange_ts_ms": books[venue].exchange_ts_ms,
                "book_received_at_ms": books[venue].received_at_ms,
            }
        return details

    def _depth_vwap(
        self, venue: str, detail: dict[str, Any], qty: Decimal
    ) -> tuple[Decimal, Decimal]:
        """Price precisely the level budgets that the fill will consume."""
        left, notional, worst = qty, D(0), D(0)
        budget = self.state["depth_liquidity"][venue][detail["side"]]
        for price, _ in detail["levels"]:
            take = min(left, D(budget[str(price.normalize())]["remaining"]))
            if take:
                notional += take * price
                worst = price
                left -= take
            if not left:
                return notional / qty, worst
        raise MarketDataError("insufficient unconsumed execution-side depth")

    def _consume_depth(self, details: dict[str, dict[str, Any]], qty: Decimal) -> None:
        for venue, detail in details.items():
            left = qty
            budget = self.state["depth_liquidity"][venue][detail["side"]]
            for price, _ in detail["levels"]:
                level = budget[str(price.normalize())]
                take = min(left, D(level["remaining"]))
                level["remaining"] = str(D(level["remaining"]) - take)
                left -= take
            if left:
                raise AssertionError("depth budget over-consumed")

    def _history_result(self, now_ms: int) -> dict[str, Any]:
        if self.history_gate is None:
            return {
                "history_filter_passed": True,
                "history_filter_reason": "history_filter_disabled",
            }
        try:
            result = self.history_gate.evaluate(self.symbol, now_ms)
            if not isinstance(result, dict) or not isinstance(
                result.get("history_filter_passed"), bool
            ):
                raise ValueError("invalid history gate result")
            return {key: result[key] for key in HISTORY_SIGNAL_FIELDS if key in result}
        except Exception as error:
            return {
                "history_filter_passed": False,
                "history_filter_reason": "history_unavailable:" + type(error).__name__,
            }

    def _evaluations(
        self, books: dict[str, Book] | None, rules: dict[str, Rules], now_ms: int, tick_id: str
    ) -> list[dict[str, Any]]:
        result = []
        history = self._history_result(now_ms)
        valid = books is not None and fresh(books, self.policy, now_ms)
        rules_valid = set(rules) == set(VENUES)
        q = D(0)
        if valid and rules_valid and books is not None:
            grid = _common_grid(rules)
            q = self.policy.max_notional / max(b.asks[0][0] for b in books.values()) / grid
            q = q.to_integral_value(rounding=ROUND_FLOOR) * grid
        base_q = q
        for long, short in (VENUES, VENUES[::-1]):
            q = base_q
            depth_capacity = None
            details = None
            if self.depth_sizing is not None and valid and rules_valid and books is not None:
                details = self._depth_details(books, long, "entry")
                depth_capacity = min(d["capacity"] for d in details.values())
                # Bound each account using the worst price permitted by the depth
                # band, price guard and tick rounding before the exact VWAP check.
                bounds = [depth_capacity]
                for venue in VENUES:
                    top = books[venue].asks[0][0] if venue == long else books[venue].bids[0][0]
                    upper = top * (
                        1 + (self.depth_sizing.band_bps + self.policy.price_guard_bps) / 10000
                    )
                    upper += rules[venue].price_tick
                    bounds.extend(
                        (
                            self.policy.max_notional / upper,
                            D(self.state["cash"][venue]) / (upper * (1 + self.policy.fee(venue))),
                        )
                    )
                q = round_pair_qty(max(D(0), min(bounds)), rules)
            row: dict[str, Any] = {
                "signal_id": "sig-" + uuid.uuid4().hex,
                "tick_id": tick_id,
                "timestamp_ms": now_ms,
                "symbol": self.symbol,
                "pair": self.pair,
                "long_venue": long,
                "short_venue": short,
                "qty": str(q),
                "threshold_bps": str(self.policy.min_entry_net_bps),
                "eligible": False,
                "selected": False,
                "action": "REJECTED",
                "reason": "",
                "cycle_id": "",
                "mode": MODE,
            }
            row.update(history)
            if depth_capacity is not None:
                row["depth_capacity_qty"] = str(depth_capacity)
            if self.entry_filter is not None:
                row.update(self.entry_filter.evaluate(books if valid else None, long, short))
            else:
                row.update(entry_filter_passed=True, entry_filter_reason="entry_filter_disabled")
            if books:
                for venue in VENUES:
                    if venue in books:
                        row[f"{venue}_exchange_ts_ms"] = books[venue].exchange_ts_ms
                        row[f"{venue}_received_at_ms"] = books[venue].received_at_ms
            if not valid:
                row["reason"] = "missing_feed" if not books else "stale_or_unsynchronized_feed"
            elif not rules_valid:
                row["reason"] = "missing_rules"
            elif q <= 0:
                row["reason"] = "quantity_below_common_step"
            else:
                assert books is not None
                try:
                    if details is None:
                        ask, worst_ask = vwap(books[long], "buy", q)
                        bid, worst_bid = vwap(books[short], "sell", q)
                    else:
                        ask, worst_ask = self._depth_vwap(long, details[long], q)
                        bid, worst_bid = self._depth_vwap(short, details[short], q)
                    limit_ask = (
                        worst_ask
                        * (1 + self.policy.price_guard_bps / D(10000))
                        / rules[long].price_tick
                    ).to_integral_value(rounding=ROUND_CEILING) * rules[long].price_tick
                    limit_bid = (
                        worst_bid
                        * (1 - self.policy.price_guard_bps / D(10000))
                        / rules[short].price_tick
                    ).to_integral_value(rounding=ROUND_FLOOR) * rules[short].price_tick
                    if limit_bid <= 0:
                        row["reason"] = "quantity_or_notional_rule"
                        result.append(row)
                        continue
                    gross = (bid - ask) / ((ask + bid) / 2) * 10000
                    net = (limit_bid - limit_ask) / ((limit_ask + limit_bid) / 2) * 10000
                    net -= 2 * (self.policy.bybit_fee_bps + self.policy.bitget_fee_bps)
                    net -= self.policy.expected_exit_spread_bps + self.policy.funding_buffer_bps
                    row.update(
                        long_vwap=str(ask),
                        short_vwap=str(bid),
                        long_limit=str(limit_ask),
                        short_limit=str(limit_bid),
                        gross_edge_bps=str(gross),
                        net_edge_bps=str(net),
                    )
                    limits = {long: limit_ask, short: limit_bid}
                    if (
                        any(
                            q < rules[v].min_qty
                            or q * limits[v] < rules[v].min_notional
                            or q * limits[v] > self.policy.max_notional
                            or (
                                details is not None
                                and q * (ask if v == long else bid) < rules[v].min_notional
                            )
                            for v in VENUES
                        )
                        or limit_bid <= 0
                    ):
                        row["reason"] = "quantity_or_notional_rule"
                    elif net < self.policy.min_entry_net_bps:
                        row["reason"] = "below_cost_threshold"
                    else:
                        row.update(eligible=True, reason="eligible")
                except MarketDataError:
                    row["reason"] = "insufficient_depth"
            if row["eligible"] and not row["entry_filter_passed"]:
                row.update(eligible=False, reason=row["entry_filter_reason"])
            if row["eligible"] and not row["history_filter_passed"]:
                row.update(
                    eligible=False, reason=row.get("history_filter_reason", "history_rejected")
                )
            result.append(row)
        return result

    def _order(
        self,
        cycle: dict[str, Any],
        venue: str,
        intent: str,
        price: Decimal,
        now_ms: int,
        limit: Decimal | None = None,
        *,
        qty: Decimal | None = None,
        depth: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        q = D(cycle["qty"]) if qty is None else qty
        long = venue == cycle["long"]
        side = "buy" if long == (intent == "entry") else "sell"
        notional = q * price
        row = {
            "order_id": "ord-" + uuid.uuid4().hex,
            "cycle_id": cycle["cycle_id"],
            "signal_id": cycle["signal_id"],
            "timestamp_ms": now_ms,
            "symbol": self.symbol,
            "pair": self.pair,
            "venue": venue,
            "side": side,
            "position_side": "long" if long else "short",
            "intent": intent,
            "qty": str(q),
            "price": str(price),
            "limit_price": str(limit) if limit is not None else "",
            "notional": str(notional),
            "fee": str(notional * self.policy.fee(venue)),
            "fee_rate": str(self.policy.fee(venue)),
            "leverage": "1",
            "status": "FILLED",
            "mode": MODE,
        }
        if depth is not None:
            row.update(
                depth_capacity_qty=str(depth["capacity"]),
                book_exchange_ts_ms=depth["book_exchange_ts_ms"],
                book_received_at_ms=depth["book_received_at_ms"],
                depth_side_fingerprint=depth["fingerprint"],
            )
        self._append("orders", row)
        self.state["orders"] += 1
        return row

    def _enter(
        self, offer: dict[str, str], signal: dict[str, Any], books: dict[str, Book], now_ms: int
    ) -> str:
        q, long, short = D(offer["qty"]), offer["long"], offer["short"]
        depth = self._depth_details(books, long, "entry") if self.depth_sizing is not None else None
        prices = (
            {v: self._depth_vwap(v, depth[v], q)[0] for v in VENUES}
            if depth is not None
            else {long: vwap(books[long], "buy", q)[0], short: vwap(books[short], "sell", q)[0]}
        )
        if prices[long] > D(offer["long_price"]) or prices[short] < D(offer["short_price"]):
            return "limit_not_executable"
        if any(D(self.state["cash"][v]) < q * prices[v] * (1 + self.policy.fee(v)) for v in VENUES):
            return "insufficient_collateral"
        cycle: dict[str, Any] = {
            "cycle_id": "pc-" + uuid.uuid4().hex,
            "signal_id": signal["signal_id"],
            "symbol": self.symbol,
            "pair": self.pair,
            "entry_timestamp_ms": now_ms,
            "long": long,
            "short": short,
            "qty": str(q),
            "initial_qty": str(q),
            "exit_parts": 0,
            "legs": {},
            "pending_exit_reason": "",
        }
        for venue in VENUES:
            row = self._order(
                cycle,
                venue,
                "entry",
                prices[venue],
                now_ms,
                D(offer["long_price"] if venue == long else offer["short_price"]),
                depth=depth[venue] if depth else None,
            )
            self.state["cash"][venue] = str(D(self.state["cash"][venue]) - D(row["fee"]))
            cycle["legs"][venue] = {
                "entry_price": row["price"],
                "entry_fee": row["fee"],
                "margin": row["notional"],
                "exit_notional": "0",
                "exit_fee": "0",
                "exit_gross": "0",
            }
        if depth is not None:
            self._consume_depth(depth, q)
        self.state["cycle"] = cycle
        signal.update(action="ENTERED", reason="signal_filled", cycle_id=cycle["cycle_id"])
        self._append("events", {"kind": "pair_opened", "timestamp_ms": now_ms, "cycle": cycle})
        return ""

    def _exit(
        self, books: dict[str, Book], rules: dict[str, Rules], now_ms: int, reason: str
    ) -> None:
        cycle = self.state["cycle"]
        remaining, long, short = D(cycle["qty"]), cycle["long"], cycle["short"]
        cycle["pending_exit_reason"] = cycle["pending_exit_reason"] or reason
        depth = self._depth_details(books, long, "exit") if self.depth_sizing is not None else None
        q = remaining
        if depth is not None:
            q = round_pair_qty(min(remaining, *(d["capacity"] for d in depth.values())), rules)
            if q < remaining:
                # Do not create dust that already violates today's exit rules.
                # Subsequent price/rule changes may still leave unresolved dust.
                minimum = max(
                    max(
                        rules[v].min_qty,
                        rules[v].min_notional / min(price for price, _ in depth[v]["levels"]),
                    )
                    for v in VENUES
                )
                grid = _common_grid(rules)
                minimum = (minimum / grid).to_integral_value(rounding=ROUND_CEILING) * grid
                if remaining - q < minimum:
                    q = round_pair_qty(max(D(0), remaining - minimum), rules)
        if q <= 0:
            raise MarketDataError("no unconsumed paired exit depth")
        # Compute and validate both legs before creating either simulated order.
        prices = (
            {v: self._depth_vwap(v, depth[v], q)[0] for v in VENUES}
            if depth is not None
            else {long: vwap(books[long], "sell", q)[0], short: vwap(books[short], "buy", q)[0]}
        )
        if depth is not None and any(
            q < rules[v].min_qty or q * prices[v] < rules[v].min_notional for v in VENUES
        ):
            raise MarketDataError("paired exit below minimum quantity or notional")
        for venue in VENUES:
            row = self._order(
                cycle,
                venue,
                "exit",
                prices[venue],
                now_ms,
                qty=q,
                depth=depth[venue] if depth else None,
            )
            leg = cycle["legs"][venue]
            gross = q * (prices[venue] - D(leg["entry_price"])) * (1 if venue == long else -1)
            leg["exit_gross"] = str(D(leg["exit_gross"]) + gross)
            leg["exit_fee"] = str(D(leg["exit_fee"]) + D(row["fee"]))
            leg["exit_notional"] = str(D(leg["exit_notional"]) + D(row["notional"]))
            leg["margin"] = str((remaining - q) * D(leg["entry_price"]))
            self.state["cash"][venue] = str(D(self.state["cash"][venue]) + gross - D(row["fee"]))
        if depth is not None:
            self._consume_depth(depth, q)
        cycle["qty"] = str(remaining - q)
        cycle["exit_parts"] += 1
        if remaining > q:
            self._append(
                "events",
                {
                    "kind": "pair_partially_closed",
                    "timestamp_ms": now_ms,
                    "cycle_id": cycle["cycle_id"],
                    "closed_qty": str(q),
                    "remaining_qty": cycle["qty"],
                    "reason": cycle["pending_exit_reason"],
                },
            )
            return
        initial_qty = D(cycle["initial_qty"])
        gross_total = sum((D(leg["exit_gross"]) for leg in cycle["legs"].values()), D(0))
        fees_total = sum(
            (D(leg["entry_fee"]) + D(leg["exit_fee"]) for leg in cycle["legs"].values()), D(0)
        )
        pnl = {
            v: D(leg["exit_gross"]) - D(leg["entry_fee"]) - D(leg["exit_fee"])
            for v, leg in cycle["legs"].items()
        }
        result = {
            "cycle_id": cycle["cycle_id"],
            "signal_id": cycle["signal_id"],
            "symbol": self.symbol,
            "pair": self.pair,
            "long_venue": long,
            "short_venue": short,
            "qty": str(initial_qty),
            "entry_timestamp_ms": cycle["entry_timestamp_ms"],
            "exit_timestamp_ms": now_ms,
            "holding_ms": now_ms - cycle["entry_timestamp_ms"],
            "long_entry_price": cycle["legs"][long]["entry_price"],
            "short_entry_price": cycle["legs"][short]["entry_price"],
            "long_exit_price": str(D(cycle["legs"][long]["exit_notional"]) / initial_qty),
            "short_exit_price": str(D(cycle["legs"][short]["exit_notional"]) / initial_qty),
            "gross_pnl": str(gross_total),
            "total_fees": str(fees_total),
            "funding": "not_modelled",
            "net_before_funding": str(gross_total - fees_total),
            "bybit_net_before_funding": str(pnl["bybit"]),
            "bitget_net_before_funding": str(pnl["bitget"]),
            "exit_reason": cycle["pending_exit_reason"],
            "exit_parts": cycle["exit_parts"],
            "mode": MODE,
        }
        self._append("cycles", result)
        self._append("events", {"kind": "pair_closed", "result": result})
        self.state["cycle"] = None
        self.state["completed_cycles"] += 1
        self.state["closed_pair_net_before_funding"] = str(
            D(self.state["closed_pair_net_before_funding"]) + gross_total - fees_total
        )
        self.state["cooldown_until_ms"] = now_ms + int(self.policy.cooldown_s * 1000)
        self.state["last_cycle"] = result
        loss = sum((D(v) - self.initial_balance for v in self.state["cash"].values()), D(0))
        if loss <= -self.policy.max_total_loss_usdt:
            self.state["halt_reason"] = "total_loss_cap"

    def _exit_pnl_estimate(
        self, books: dict[str, Book], rules: dict[str, Rules], cycle: dict[str, Any], now_ms: int
    ) -> Decimal:
        """Use executable fragment prices; never label the proxy a full fill.

        The take-profit comparison has the same per-entry-notional rate for the
        executable fragment. The stop-loss comparison extrapolates those prices
        over the remaining position as an explicitly recorded risk proxy. Only
        _exit can create orders, and it is separately bounded by actual budgets.
        """
        q, long, short = D(cycle["qty"]), cycle["long"], cycle["short"]
        if self.depth_sizing is None:
            executable_qty = q
            prices = {long: vwap(books[long], "sell", q)[0],
                      short: vwap(books[short], "buy", q)[0]}
        else:
            depth = self._depth_details(books, long, "exit")
            executable_qty = round_pair_qty(min(q, *(d["capacity"] for d in depth.values())), rules)
            if executable_qty <= 0:
                raise MarketDataError("no executable fragment for risk estimate")
            prices = {v: self._depth_vwap(v, depth[v], executable_qty)[0] for v in VENUES}
        pnl = D(0)
        gross_pnl, fees = D(0), D(0)
        for venue in VENUES:
            entry = D(cycle["legs"][venue]["entry_price"])
            leg_gross = q * (prices[venue] - entry) * (1 if venue == long else -1)
            leg_fees = q * (entry + prices[venue]) * self.policy.fee(venue)
            gross_pnl += leg_gross
            fees += leg_fees
            pnl += leg_gross
            pnl -= leg_fees
        cycle["exit_evaluation"] = {
            "timestamp_ms": now_ms,
            "position_qty": str(q),
            "executable_qty": str(executable_qty),
            "estimated_position_pnl": str(pnl),
            "estimated_position_gross_pnl": str(gross_pnl),
            "estimated_position_fees": str(fees),
            "executable_fragment_net_pnl": str(pnl * executable_qty / q),
            "prices": {v: str(price) for v, price in prices.items()},
            "mode": "full_budget_executable_pnl"
            if executable_qty == q
            else "partial_depth_position_pnl_proxy",
        }
        return pnl

    def _mark(self, books: dict[str, Book] | None, now_ms: int, tick_id: str) -> None:
        cycle = self.state["cycle"]
        valid = books is not None and fresh(books, self.policy, now_ms)
        for venue in VENUES:
            cash, margin = D(self.state["cash"][venue]), D(0)
            gross: Decimal | None = D(0)
            exit_fee: Decimal | None = D(0)
            mark_status = "flat"
            if cycle:
                leg, q = cycle["legs"][venue], D(cycle["qty"])
                margin, gross, exit_fee = D(leg["margin"]), None, None
                mark_status = "stale_or_missing_feed"
                if valid:
                    assert books is not None
                    try:
                        side = "sell" if venue == cycle["long"] else "buy"
                        if self.depth_sizing is None:
                            price = vwap(books[venue], side, q)[0]
                        else:
                            detail = self._depth_details(books, cycle["long"], "exit")[venue]
                            price = self._depth_vwap(venue, detail, q)[0]
                        gross = q * (price - D(leg["entry_price"]))
                        gross *= 1 if venue == cycle["long"] else -1
                        exit_fee = q * price * self.policy.fee(venue)
                        mark_status = "fresh_executable_vwap"
                    except MarketDataError:
                        mark_status = "insufficient_depth"
            row = {
                "symbol": self.symbol,
                "pair": self.pair,
                "tick_id": tick_id,
                "timestamp_ms": now_ms,
                "venue": venue,
                "cash_balance": str(cash),
                "reserved_margin": str(margin),
                "available_balance": str(cash - margin),
                "unrealized_gross_pnl": str(gross) if gross is not None else None,
                "estimated_exit_fee": str(exit_fee) if exit_fee is not None else None,
                "equity": str(cash + gross) if gross is not None else None,
                "liquidation_equity_before_funding": (
                    str(cash + gross - exit_fee)
                    if gross is not None and exit_fee is not None
                    else None
                ),
                "mark_status": mark_status,
                "cycle_id": cycle["cycle_id"] if cycle else "",
                "mode": MODE,
            }
            self._append("equity", row)
            self.state["last_equity"][venue] = row

    def tick(
        self,
        books: dict[str, Book] | None,
        rules: dict[str, Rules],
        now_ms: int,
        force_exit_reason: str | None = None,
    ) -> dict[str, Any]:
        if self.closed:
            raise RuntimeError("paper session is closed")
        if isinstance(now_ms, bool) or not isinstance(now_ms, int) or now_ms <= 0:
            raise ValueError("now_ms must be a positive integer")
        if self.state["last_tick_ms"] is not None and now_ms < self.state["last_tick_ms"]:
            raise ValueError("paper clock moved backwards")
        before = copy.deepcopy(self.state)
        filter_before = copy.deepcopy(self.entry_filter)
        try:
            with self.db:
                self._tick(books, rules, now_ms, force_exit_reason)
                self.db.execute(
                    "INSERT OR REPLACE INTO latest_state(id,value) VALUES(1,?)",
                    (_json(self.state),),
                )
        except Exception:
            self.state = before
            self.entry_filter = filter_before
            raise
        return self.summary()

    def reset_entry_filter(self) -> None:
        """Discard the rolling window after a stream invalidation; positions remain intact."""
        if self.entry_filter is not None:
            self.entry_filter.reset()

    def _risk_exit_reason(self, books, rules, cycle, now_ms):
        """Compare paired gross price PnL against single-long-entry thresholds.

        Use the same remaining quantity for numerator and denominator. When only
        a fragment can execute, inherited PnL is an explicitly labelled position
        proxy; actual fills remain limited by the consumable L1 budget.
        """
        pnl = self._exit_pnl_estimate(books, rules, cycle, now_ms)
        notional = D(cycle["qty"]) * D(cycle["legs"][cycle["long"]]["entry_price"])
        profit = notional * self.policy.take_profit_bps / 10000
        loss = -notional * self.policy.stop_loss_bps / 10000
        cycle.setdefault("exit_evaluation", {}).update(
            risk_basis=RISK_BASIS,
            risk_basis_notional=str(notional),
            take_profit_bps=str(self.policy.take_profit_bps),
            stop_loss_bps=str(self.policy.stop_loss_bps),
            take_profit_threshold_usdt=str(profit),
            stop_loss_threshold_usdt=str(loss),
        )
        gross_pnl = D(cycle["exit_evaluation"]["estimated_position_gross_pnl"])
        if gross_pnl <= loss:
            return "stop_loss"
        if gross_pnl >= profit:
            return "take_profit"
        return ""

    def _tick(
        self,
        books: dict[str, Book] | None,
        rules: dict[str, Rules],
        now_ms: int,
        force_exit_reason: str | None,
    ) -> None:
        tick_id = "tick-" + uuid.uuid4().hex
        self.state["started_at_ms"] = self.state["started_at_ms"] or now_ms
        self.state["last_tick_ms"] = now_ms
        self.state["ticks"] += 1
        if force_exit_reason:
            if not force_exit_reason.startswith("funding_guard:"):
                self.state["stop_requested"] = True
                self.state["halt_reason"] = force_exit_reason
            if self.state["cycle"]:
                self.state["cycle"]["pending_exit_reason"] = (
                    self.state["cycle"]["pending_exit_reason"] or force_exit_reason
                )
        valid = books is not None and fresh(books, self.policy, now_ms)
        if self.entry_filter is not None:
            if books is not None and not valid:
                self.entry_filter.reset()
            self.entry_filter.observe(books if valid else None, now_ms)
        if valid and books is not None:
            self._observe_depth(books)
        evaluations = self._evaluations(books, rules, now_ms, tick_id)
        cycle = self.state["cycle"]
        had_cycle = cycle is not None
        if cycle:
            if now_ms - cycle["entry_timestamp_ms"] >= self.policy.max_holding_s * 1000:
                cycle["pending_exit_reason"] = cycle["pending_exit_reason"] or "holding_timeout"
            if valid:
                assert books is not None
                try:
                    reason = cycle["pending_exit_reason"]
                    if not reason:
                        reason = self._risk_exit_reason(books, rules, cycle, now_ms)
                    if reason:
                        if not cycle["pending_exit_reason"] and "exit_evaluation" in cycle:
                            self._append(
                                "events",
                                {
                                    "kind": "exit_triggered",
                                    "cycle_id": cycle["cycle_id"],
                                    "reason": reason,
                                    "evaluation": cycle["exit_evaluation"],
                                },
                            )
                        cycle["pending_exit_reason"] = reason
                        self._exit(books, rules, now_ms, reason)
                except MarketDataError:
                    self._append(
                        "events",
                        {
                            "kind": "exit_depth_unavailable",
                            "timestamp_ms": now_ms,
                            "cycle_id": cycle["cycle_id"],
                        },
                    )
        gate = ""
        if had_cycle:
            gate = "position_already_open"
        elif force_exit_reason:
            gate = force_exit_reason
        elif self.state["stop_requested"] or self.state["halt_reason"]:
            gate = self.state["halt_reason"] or "stopped"
        elif self.state["completed_cycles"] >= self.policy.max_cycles:
            gate = "max_cycles"
        elif now_ms < self.state["cooldown_until_ms"]:
            gate = "cooldown"
        # Select only among directions that passed every entry filter.
        chosen = max(
            (row for row in evaluations if row["eligible"]),
            key=lambda row: D(row["net_edge_bps"]),
            default=None,
        )
        offer = (
            None
            if chosen is None
            else {
                "long": chosen["long_venue"],
                "short": chosen["short_venue"],
                "qty": chosen["qty"],
                "long_price": chosen["long_limit"],
                "short_price": chosen["short_limit"],
            }
        )
        for signal in evaluations:
            if signal["eligible"]:
                self.state["eligible_signals"] += 1
                signal.update(action="SKIPPED", reason=gate or "other_direction_selected")
            if offer and signal["long_venue"] == offer["long"]:
                signal["selected"] = True
                if not gate:
                    assert books is not None
                    refusal = self._enter(offer, signal, books, now_ms)
                    if refusal:
                        signal.update(action="SKIPPED", reason=refusal)
            self._append("signals", signal)
            self.state["signal_evaluations"] += 1
        self._mark(books, now_ms, tick_id)
        cycle = self.state["cycle"]
        if cycle:
            self.state["status"] = "UNRESOLVED" if cycle["pending_exit_reason"] else "OPEN"
        else:
            self.state["status"] = "FLAT"

    def summary(self) -> dict[str, Any]:
        result = cast(dict[str, Any], json.loads(_json(self.state)))
        result["ledger_path"] = str(self.path)
        result["net_before_funding"] = str(
            sum((D(v) - self.initial_balance for v in self.state["cash"].values()), D(0))
        )
        # Cash movement on an open pair includes entry fees but excludes unrealized PnL.
        result["cash_change_before_funding"] = result.pop("net_before_funding")
        return result

    def export(self, output_dir: Path) -> dict[str, str]:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        paths = {}
        for table, headers in _HEADERS.items():
            path = output_dir / f"{table}.csv"
            with path.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(stream, fieldnames=headers, extrasaction="ignore")
                writer.writeheader()
                for row in self.db.execute(f"SELECT value FROM {table} ORDER BY id"):
                    writer.writerow(json.loads(row[0]))
            paths[table] = str(path)
        path = output_dir / "summary.json"
        path.write_text(json.dumps(self.summary(), indent=2, ensure_ascii=False) + "\n")
        paths["summary"] = str(path)
        return paths

    def close(self) -> None:
        if not self.closed:
            self.db.close()
            self.closed = True

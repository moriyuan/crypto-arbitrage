"""Durable, single-cycle coordinator for two dedicated Freqtrade dry-run bots.

RPC submission is not idempotent. Persist intent before sending, never resend an
ambiguous entry, and keep reconciling late trades by their unique entry tag.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import sqlite3
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any

from .freqtrade import FreqtradeClient, TradeSnapshot
from .market import Book, MarketDataError, Rules, vwap

D = Decimal
VENUES = ("bybit", "bitget")
PAIR = "BTC/USDT:USDT"


@dataclass(frozen=True)
class Policy:
    max_notional: Decimal = D("250")
    bybit_fee_bps: Decimal = D("6")
    bitget_fee_bps: Decimal = D("6")
    min_entry_net_bps: Decimal = D("5")
    expected_exit_spread_bps: Decimal = D("2")
    funding_buffer_bps: Decimal = D("2")
    price_guard_bps: Decimal = D("1")
    take_profit_bps: Decimal = D("1")
    stop_loss_usdt: Decimal = D("5")
    max_total_loss_usdt: Decimal = D("10")
    entry_timeout_s: float = 5.0
    max_holding_s: float = 60.0
    cooldown_s: float = 30.0
    max_quote_age_ms: int = 2000
    max_quote_skew_ms: int = 500
    max_request_ms: int = 1500
    max_cycles: int = 3

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            if isinstance(value, Decimal):
                if not value.is_finite() or value < 0:
                    raise ValueError(f"invalid policy: {name}")
            elif not math.isfinite(value) or value <= 0:
                raise ValueError(f"invalid policy: {name}")
        if min(self.max_notional, self.stop_loss_usdt, self.max_total_loss_usdt) <= 0:
            raise ValueError("positive capital and loss caps required")
        if self.max_holding_s > 3600:
            raise ValueError("this short-horizon pilot is limited to one hour")

    def fee(self, venue: str) -> Decimal:
        return (self.bybit_fee_bps if venue == "bybit" else self.bitget_fee_bps) / D(10000)


def fresh(books: dict[str, Book], policy: Policy, now_ms: int) -> bool:
    if set(books) != set(VENUES):
        return False
    for b in books.values():
        if not 0 <= now_ms - b.received_at_ms <= policy.max_quote_age_ms:
            return False
        if not -500 <= now_ms - b.exchange_ts_ms <= policy.max_quote_age_ms:
            return False
        if not 0 <= b.request_ms <= policy.max_request_ms:
            return False
    return all(
        max(getattr(b, key) for b in books.values()) - min(getattr(b, key) for b in books.values())
        <= policy.max_quote_skew_ms
        for key in ("received_at_ms", "exchange_ts_ms")
    )


def _grid(rules: dict[str, Rules]) -> Decimal:
    steps = [r.qty_step for r in rules.values()]
    digits = max(max(0, -int(s.as_tuple().exponent)) for s in steps)
    scale = D(10) ** digits
    return D(math.lcm(*(int(s * scale) for s in steps))) / scale


def candidate(
    books: dict[str, Book], rules: dict[str, Rules], policy: Policy, now_ms: int
) -> dict[str, str] | None:
    if not fresh(books, policy, now_ms) or set(rules) != set(VENUES):
        return None
    grid = _grid(rules)
    upper = max(b.asks[0][0] for b in books.values())
    q = (policy.max_notional / upper / grid).to_integral_value(rounding=ROUND_FLOOR) * grid
    if q <= 0:
        return None
    choices = []
    for long, short in (VENUES, VENUES[::-1]):
        try:
            _, ask = vwap(books[long], "buy", q)
            _, bid = vwap(books[short], "sell", q)
        except MarketDataError:
            continue
        ask = (
            ask * (1 + policy.price_guard_bps / D(10000)) / rules[long].price_tick
        ).to_integral_value(rounding=ROUND_CEILING) * rules[long].price_tick
        bid = (
            bid * (1 - policy.price_guard_bps / D(10000)) / rules[short].price_tick
        ).to_integral_value(rounding=ROUND_FLOOR) * rules[short].price_tick
        if bid <= 0:
            continue
        prices = {long: ask, short: bid}
        if any(
            q < rules[v].min_qty
            or q * prices[v] < rules[v].min_notional
            or q * prices[v] > policy.max_notional
            for v in VENUES
        ):
            continue
        ref = (ask + bid) / 2
        edge = (bid - ask) / ref * 10000
        # Conservative limit prices already include the depth and price guard.
        net = edge - 2 * (policy.bybit_fee_bps + policy.bitget_fee_bps)
        net -= policy.expected_exit_spread_bps + policy.funding_buffer_bps
        if net >= policy.min_entry_net_bps:
            choices.append(
                {
                    "long": long,
                    "short": short,
                    "qty": str(q),
                    "long_price": str(ask),
                    "short_price": str(bid),
                    "net_edge_bps": str(net),
                }
            )
    return max(choices, key=lambda x: D(x["net_edge_bps"])) if choices else None


def executable_pnl(
    q: Decimal,
    long: str,
    short: str,
    long_entry: Decimal,
    short_entry: Decimal,
    books: dict[str, Book],
    policy: Policy,
) -> Decimal:
    sell, _ = vwap(books[long], "sell", q)
    buy, _ = vwap(books[short], "buy", q)
    gross = q * (sell - long_entry + short_entry - buy)
    return gross - q * (
        (long_entry + sell) * policy.fee(long) + (short_entry + buy) * policy.fee(short)
    )


class Journal:
    """One writer, durable intent and append-only transition audit; no credentials."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.fd = os.open(str(path) + ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.fd)
            raise RuntimeError("coordinator journal is already in use") from None
        try:
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            os.fchmod(fd, 0o600)
            os.close(fd)
            self.db = sqlite3.connect(path)
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, value TEXT)")
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, kind TEXT, value TEXT)"
            )
            self.db.commit()
        except Exception:
            os.close(self.fd)
            raise

    def read(self) -> dict[str, Any] | None:
        row = self.db.execute("SELECT value FROM state WHERE id=1").fetchone()
        return json.loads(row[0]) if row else None

    def save(self, state: dict[str, Any], kind: str) -> None:
        value = json.dumps(state, sort_keys=True, separators=(",", ":"))
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO state(id,value) VALUES(1,?)", (value,))
            self.db.execute("INSERT INTO events(kind,value) VALUES(?,?)", (kind, value))

    def close(self) -> None:
        self.db.close()
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)


class Coordinator:
    def __init__(self, clients: dict[str, FreqtradeClient], journal: Journal, policy: Policy):
        if set(clients) != set(VENUES):
            raise ValueError("dedicated bybit and bitget clients required")
        self.clients, self.journal, self.policy = clients, journal, policy
        fingerprint = json.loads(json.dumps(asdict(policy), default=str, sort_keys=True))
        self.state = journal.read() or {
            "version": 1,
            "policy": fingerprint,
            "cycle": None,
            "completed": 0,
            "realized_pnl": "0",
            "cooldown_until": 0,
            "halt_reason": "",
            "last_result": None,
        }
        if self.state["version"] != 1 or self.state["policy"] != fingerprint:
            raise ValueError(
                "journal policy/version mismatch; reconcile with original configuration"
            )
        self.stopping = self.state.get("stop_requested", False)

    def _save(self, kind: str) -> None:
        self.journal.save(self.state, kind)

    def preflight(self) -> None:
        for venue, client in self.clients.items():
            client.preflight(venue, expect_dry_run=True)

    def request_exit(self, reason: str = "operator_stop") -> None:
        self.stopping = True
        self.state["stop_requested"] = True
        if self.state["cycle"]:
            self.state["cycle"]["phase"] = "EXITING"
            self.state["cycle"]["reason"] = reason
        self._save(reason)

    def resume(self) -> None:
        self.preflight()
        if self.state["cycle"] or any(c.open_trades() for c in self.clients.values()):
            raise ValueError("reconcile outstanding trades before resume")
        self.stopping = False
        self.state["stop_requested"] = False
        self._save("explicit_resume")

    def _snapshot(self, venue: str, leg: dict[str, Any]) -> TradeSnapshot | None:
        client = self.clients[venue]
        trade = client.trade(leg["trade_id"]) if leg["trade_id"] is not None else None
        return trade if trade is not None else client.find_by_tag(leg["tag"])

    def _entry(
        self,
        offer: dict[str, str],
        now_ms: int,
        books: dict[str, Book],
        clock_ms: Callable[[], int],
    ) -> None:
        cycle_id = "ca1-" + uuid.uuid4().hex
        cycle: dict[str, Any] = dict(offer)
        cycle.update(id=cycle_id, phase="ENTERING", started_ms=now_ms, reason="", legs={})
        for venue in VENUES:
            cycle["legs"][venue] = {
                "tag": cycle_id + ":" + venue,
                "trade_id": None,
                "entry_ack": False,
                "cancel_orders": [],
                "cancel_ack": False,
                "exit_sent": False,
                "exit_baseline": [],
                "last_filled": "0",
                "closed": False,
            }
        self.state["cycle"] = cycle
        self._save("entry_intents_persisted")
        if not fresh(books, self.policy, clock_ms()):
            self.state["cycle"] = None
            self._save("signal_expired_before_submission")
            return
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {}
            for venue in VENUES:
                long = venue == cycle["long"]
                price = D(cycle["long_price" if long else "short_price"])
                futures[venue] = pool.submit(
                    self.clients[venue].force_enter,
                    pair=PAIR,
                    side="long" if long else "short",
                    stakeamount=D(cycle["qty"]) * price,
                    leverage=D("1"),
                    price=price,
                    entry_tag=cycle["legs"][venue]["tag"],
                )
            for venue, future in futures.items():
                try:
                    trade = future.result()
                    cycle["legs"][venue]["entry_ack"] = True
                    cycle["legs"][venue]["trade_id"] = trade.trade_id
                except Exception:
                    # Even an HTTP failure is not proof that the exchange did not accept it.
                    cycle["phase"], cycle["reason"] = "RECOVERY", "entry_outcome_unknown"
                self._save("entry_response_" + venue)

    def _flatten(self, cycle: dict[str, Any], snapshots: dict[str, TradeSnapshot | None]) -> None:
        # Each action has a durable intent. Never retry a mutation on an ambiguous response.
        for venue in VENUES:
            leg, trade = cycle["legs"][venue], snapshots[venue]
            if trade is None or not trade.is_open:
                continue
            try:
                self.clients[venue].preflight(venue, expect_dry_run=True)
            except Exception:
                cycle["reason"] = "worker_preflight_failed"
                self._save("recovery_preflight_failed_" + venue)
                continue
            open_ids = sorted(o.order_id for o in trade.orders if o.is_open)
            if trade.has_open_orders:
                if open_ids and open_ids != leg["cancel_orders"]:
                    leg["cancel_orders"], leg["cancel_ack"] = open_ids, False
                    self._save("cancel_intent_" + venue)
                    try:
                        self.clients[venue].cancel_open_orders(trade.trade_id)
                        leg["cancel_ack"] = True
                    except Exception:
                        cycle["reason"] = "cancel_outcome_unknown"
                    self._save("cancel_result_" + venue)
                continue
            if trade.filled_amount <= 0:
                continue
            if leg["exit_sent"]:
                new_exits = [
                    o
                    for o in trade.orders
                    if o.order_id not in leg["exit_baseline"]
                    and o.side in ("buy" if trade.is_short else "sell", "stoploss")
                ]
                # An observed terminal exit with residual size permits a new reduce-only attempt.
                if not new_exits or any(o.is_open for o in new_exits):
                    continue
                leg["exit_sent"] = False
            leg["exit_sent"] = True
            leg["exit_baseline"] = [o.order_id for o in trade.orders]
            self._save("exit_intent_" + venue)
            try:
                self.clients[venue].force_exit(trade.trade_id)
            except Exception:
                cycle["reason"] = "exit_outcome_unknown"
            self._save("exit_result_" + venue)

    def tick(self, books: dict[str, Book] | None, rules: dict[str, Rules], now_ms: int) -> str:
        started = time.monotonic()

        def clock_ms() -> int:
            return now_ms + int((time.monotonic() - started) * 1000)

        cycle = self.state["cycle"]
        # A failed read leaves the current state intact; callers must continue reconciliation.
        open_trades = {}
        read_failed: set[str] = set()
        for venue, client in self.clients.items():
            try:
                open_trades[venue] = client.open_trades()
            except Exception:
                read_failed.add(venue)
                open_trades[venue] = []
        owned = {v: cycle["legs"][v]["tag"] if cycle else None for v in VENUES}
        if any(
            cycle is None or t.entry_tag != owned[v] for v, ts in open_trades.items() for t in ts
        ):
            self.state["halt_reason"] = "unmanaged_trade_detected"
            self._save("unmanaged_trade")
            # Flatten only our own cycle; never touch unrelated holdings.
            if cycle is None:
                return "HALTED"
            cycle["phase"], cycle["reason"] = "RECOVERY", "unmanaged_trade_detected"
        if cycle is None:
            if read_failed:
                return "PAUSED"
            if self.stopping or self.state["halt_reason"]:
                return "HALTED"
            if self.state["completed"] >= self.policy.max_cycles:
                self.state["halt_reason"] = "cycle_limit"
                self._save("cycle_limit")
                return "HALTED"
            if D(self.state["realized_pnl"]) <= -self.policy.max_total_loss_usdt:
                self.state["halt_reason"] = "loss_limit"
                self._save("loss_limit")
                return "HALTED"
            if now_ms < self.state["cooldown_until"] or books is None:
                return "FLAT"
            offer = candidate(books, rules, self.policy, clock_ms())
            if offer:
                self.preflight()
                offer = candidate(books, rules, self.policy, clock_ms())
                if offer:
                    self._entry(offer, clock_ms(), books, clock_ms)
                    return self.state["cycle"]["phase"] if self.state["cycle"] else "FLAT"
            return "FLAT"

        snapshots: dict[str, TradeSnapshot | None] = {}
        for venue in VENUES:
            try:
                snapshots[venue] = self._snapshot(venue, cycle["legs"][venue])
            except Exception:
                read_failed.add(venue)
                snapshots[venue] = None
        if read_failed:
            cycle["phase"], cycle["reason"] = "RECOVERY", "worker_unavailable"
        for venue, trade in snapshots.items():
            leg = cycle["legs"][venue]
            if trade is not None:
                if (
                    trade.entry_tag != leg["tag"]
                    or trade.pair != PAIR
                    or trade.is_short != (venue == cycle["short"])
                ):
                    raise ValueError("remote trade identity mismatch")
                leg["entry_seen"] = True
                leg["ever_filled"] = leg.get("ever_filled", False) or trade.filled_amount > 0
                leg["trade_id"], leg["last_filled"] = trade.trade_id, str(trade.filled_amount)
                leg["closed"] = not trade.is_open
            elif (
                venue not in read_failed
                and (leg["entry_ack"] or leg.get("entry_seen"))
                and leg["cancel_orders"]
                and not leg.get("ever_filled", False)
            ):
                # Freqtrade deletes zero-fill entries after cancel. Absence in both open and
                # closed history resolves that *acknowledged* entry, even if cancel response failed.
                leg["closed"] = True
            # No record for an unacknowledged entry remains UNKNOWN indefinitely.
        self._save("reconciled")

        if cycle["phase"] == "ENTERING":
            q = D(cycle["qty"])
            if all(
                t is not None
                and t.is_open
                and not t.has_open_orders
                and abs(t.filled_amount - q) <= D("0.0000000001")
                for t in snapshots.values()
            ):
                cycle["phase"] = "OPEN"
                self._save("hedged")
            elif clock_ms() - cycle["started_ms"] >= self.policy.entry_timeout_s * 1000 or any(
                t is not None and not t.is_open for t in snapshots.values()
            ):
                cycle["phase"], cycle["reason"] = "RECOVERY", "entry_incomplete"
                self._save("entry_abort")
        if cycle["phase"] == "OPEN":
            q = D(cycle["qty"])
            if any(
                t is None
                or not t.is_open
                or t.has_open_orders
                or abs(t.filled_amount - q) > D("0.0000000001")
                for t in snapshots.values()
            ):
                cycle["phase"], cycle["reason"] = "RECOVERY", "hedge_changed"
            elif clock_ms() - cycle["started_ms"] >= self.policy.max_holding_s * 1000:
                cycle["phase"], cycle["reason"] = "EXITING", "holding_timeout"
            elif books is None or not fresh(books, self.policy, clock_ms()):
                cycle["phase"], cycle["reason"] = "EXITING", "market_unavailable"
            else:
                a, b = snapshots[cycle["long"]], snapshots[cycle["short"]]
                assert a is not None and b is not None
                try:
                    pnl = executable_pnl(
                        q,
                        cycle["long"],
                        cycle["short"],
                        a.open_rate,
                        b.open_rate,
                        books,
                        self.policy,
                    )
                    pnl += a.funding_fees + b.funding_fees
                    cycle["estimated_exit_pnl"] = str(pnl)
                    target = q * (a.open_rate + b.open_rate) / 2
                    target *= self.policy.take_profit_bps / D(10000)
                    if pnl >= target or pnl <= -self.policy.stop_loss_usdt:
                        cycle["phase"] = "EXITING"
                        cycle["reason"] = "profit_target" if pnl >= target else "loss_stop"
                except MarketDataError:
                    cycle["phase"], cycle["reason"] = "EXITING", "exit_depth_unavailable"
            self._save("position_checked")
        if cycle["phase"] in ("EXITING", "RECOVERY"):
            self._flatten(cycle, snapshots)
            if not read_failed and all(leg["closed"] for leg in cycle["legs"].values()):
                realized = D("0")
                for t in snapshots.values():
                    if t is not None:
                        if t.close_profit_abs is None:
                            self.state["halt_reason"] = "closed_pnl_missing"
                        else:
                            realized += t.close_profit_abs
                self.state["realized_pnl"] = str(D(self.state["realized_pnl"]) + realized)
                self.state["completed"] += 1
                self.state["last_result"] = {
                    "cycle_id": cycle["id"],
                    "reason": cycle["reason"],
                    "net_pnl": str(realized),
                }
                self.state["cooldown_until"] = now_ms + self.policy.cooldown_s * 1000
                self.state["cycle"] = None
                self._save("flat_confirmed")
                return "FLAT"
        return str(cycle["phase"])

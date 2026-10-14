"""Causal daily universe ranking from immutable public hourly candle archives.

The directory snapshot is a present-day universe, not a survivorship-free
historical listing universe. Project identity controls admission after ranking;
it never promotes a lower-ranked verified symbol into the historical top N.
"""

from __future__ import annotations

import copy
import gzip
import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any

HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
_VENUES = ("bybit", "bitget")
_EXCLUDED_BASE = frozenset(
    {
        "BTC",
        "DAI",
        "FDUSD",
        "PAXG",
        "PYUSD",
        "TUSD",
        "USD1",
        "USDC",
        "USDD",
        "USDE",
        "USDP",
        "XAG",
        "XAU",
        "XAUT",
        "MARSCOIN",
    }
)
HISTORY_SIGNAL_FIELDS = (
    "history_filter_passed",
    "history_filter_reason",
    "history_asof_day_ms",
    "bybit_rv20_bps",
    "bitget_rv20_bps",
    "bybit_adv20_usdt",
    "bitget_adv20_usdt",
    "history_rank",
    "history_cutoff_volatility_bps",
    "history_window_start_ms",
    "history_window_end_ms",
)


def _symbol(value: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[A-Z0-9]{1,36}USDT", value) is None:
        raise ValueError("expected a canonical uppercase USDT symbol")
    return value


@dataclass(frozen=True)
class RollingHistoryConfig:
    window_days: int = 20
    min_volatility_bps: Decimal = Decimal(0)
    min_turnover_usdt: Decimal = Decimal(0)
    top_n: int = 10
    publication_delay_ms: int = 60_000

    def __post_init__(self) -> None:
        if type(self.window_days) is not int or not 1 <= self.window_days <= 366:
            raise ValueError("window_days must be between 1 and 366")
        if type(self.top_n) is not int or self.top_n <= 0:
            raise ValueError("top_n must be a positive integer")
        if type(self.publication_delay_ms) is not int or self.publication_delay_ms < 0:
            raise ValueError("publication_delay_ms must be a nonnegative integer")
        for value in (self.min_volatility_bps, self.min_turnover_usdt):
            if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
                raise ValueError("historical thresholds must be finite nonnegative Decimals")


@dataclass(frozen=True)
class HourlyBar:
    start_ms: int
    close: Decimal
    turnover_usdt: Decimal

    def __post_init__(self) -> None:
        if type(self.start_ms) is not int or self.start_ms < 0 or self.start_ms % HOUR_MS:
            raise ValueError("hourly bar timestamp must be a nonnegative UTC hour boundary")
        if not isinstance(self.close, Decimal) or not self.close.is_finite() or self.close <= 0:
            raise ValueError("hour close must be a finite positive Decimal")
        if (
            not isinstance(self.turnover_usdt, Decimal)
            or not self.turnover_usdt.is_finite()
            or self.turnover_usdt < 0
        ):
            raise ValueError("hour turnover must be a finite nonnegative Decimal")


class HistoryGate:
    def __init__(
        self,
        config: RollingHistoryConfig,
        hourly_data: Mapping[str, Mapping[str, Sequence[HourlyBar]]],
        *,
        audit_metadata: Mapping[str, Any] | None = None,
        identity_verified_symbols: Iterable[str] = (),
    ) -> None:
        self.config = config
        self._audit = copy.deepcopy(dict(audit_metadata or {}))
        self._verified = frozenset(_symbol(s) for s in identity_verified_symbols)
        data: dict[str, Mapping[str, Mapping[int, HourlyBar]]] = {}
        for symbol, venues in hourly_data.items():
            _symbol(symbol)
            if set(venues) - set(_VENUES):
                raise ValueError("unsupported history venue")
            copied: dict[str, Mapping[int, HourlyBar]] = {}
            for venue, bars in venues.items():
                indexed: dict[int, HourlyBar] = {}
                for bar in bars:
                    if not isinstance(bar, HourlyBar):
                        raise ValueError("hourly inputs must be immutable HourlyBar records")
                    if bar.start_ms in indexed:
                        raise ValueError(f"duplicate hourly bar: {venue} {symbol} {bar.start_ms}")
                    indexed[bar.start_ms] = bar
                copied[venue] = MappingProxyType(indexed)
            data[symbol] = MappingProxyType(copied)
        self._data = MappingProxyType(data)
        self._daily: dict[tuple[str, str, int], tuple[Decimal, Decimal] | None] = {}
        self._cache: dict[int, dict[str, dict[str, Any]]] = {}

    def metadata(self) -> dict[str, Any]:
        config = asdict(self.config)
        config["min_volatility_bps"] = str(self.config.min_volatility_bps)
        config["min_turnover_usdt"] = str(self.config.min_turnover_usdt)
        return {
            "config": config,
            "audit": copy.deepcopy(self._audit),
            "symbols": sorted(self._data),
            "identity_verified_symbols": sorted(self._verified),
            "formula": "mean_daily(sqrt(sum_24hours(log(close/previous_close)^2)))*10000",
            "ranking": "min_venue_rv_desc,min_venue_adv_desc,symbol_asc",
            "annualized": False,
            "historical_turnover_filter_enabled": self.config.min_turnover_usdt > 0,
            "availability": "completed UTC days plus publication_delay_ms; no missing-hour fill",
            "execution_enabled": False,
        }

    def _day(self, symbol: str, venue: str, day: int) -> tuple[Decimal, Decimal] | None:
        key = symbol, venue, day
        if key in self._daily:
            return self._daily[key]
        bars = self._data[symbol].get(venue, {})
        # Each daily RV needs all 24 hourly closes and the previous hour close.
        selected = [bars.get(t) for t in range(day - HOUR_MS, day + DAY_MS, HOUR_MS)]
        if any(bar is None for bar in selected):
            self._daily[key] = None
            return None
        complete = [bar for bar in selected if bar is not None]
        returns = [
            math.log(float(current.close / previous.close))
            for previous, current in zip(complete, complete[1:], strict=False)
        ]
        value = (
            Decimal(str(10000 * math.sqrt(math.fsum(r * r for r in returns)))),
            sum((bar.turnover_usdt for bar in complete[1:]), Decimal(0)),
        )
        self._daily[key] = value
        return value

    def _rows(self, end: int) -> dict[str, dict[str, Any]]:
        if end in self._cache:
            return self._cache[end]
        start = end - self.config.window_days * DAY_MS
        rows: dict[str, dict[str, Any]] = {}
        ranking: list[tuple[Decimal, Decimal, str]] = []
        for symbol in sorted(self._data):
            row = self._blank(start, end, "history_incomplete")
            rows[symbol] = row
            if symbol[:-4] in _EXCLUDED_BASE:
                row["history_filter_reason"] = "asset_excluded"
                continue
            aggregates: dict[str, tuple[Decimal, Decimal]] = {}
            for venue in _VENUES:
                daily = [self._day(symbol, venue, d) for d in range(start, end, DAY_MS)]
                if not any(v is None for v in daily):
                    complete = [v for v in daily if v is not None]
                    rv = sum((v[0] for v in complete), Decimal(0)) / self.config.window_days
                    adv = sum((v[1] for v in complete), Decimal(0)) / self.config.window_days
                    aggregates[venue] = rv, adv
                    row[f"{venue}_rv20_bps"] = str(rv)
                    row[f"{venue}_adv20_usdt"] = str(adv)
            if len(aggregates) != 2:
                continue
            rv = min(v[0] for v in aggregates.values())
            adv = min(v[1] for v in aggregates.values())
            if rv < self.config.min_volatility_bps:
                row["history_filter_reason"] = "history_volatility_below_threshold"
            elif adv < self.config.min_turnover_usdt:
                row["history_filter_reason"] = "history_turnover_below_threshold"
            else:
                ranking.append((-rv, -adv, symbol))
        ranking.sort()
        cutoff = str(-ranking[min(self.config.top_n, len(ranking)) - 1][0]) if ranking else None
        for rank, (_, _, symbol) in enumerate(ranking, 1):
            row = rows[symbol]
            row["history_rank"] = rank
            if rank > self.config.top_n:
                reason = "history_not_top_n"
            elif symbol not in self._verified:
                reason = "asset_identity_unverified"
            else:
                reason = "history_filter_passed"
            row["history_filter_reason"] = reason
            row["history_filter_passed"] = reason == "history_filter_passed"
        for row in rows.values():
            row["history_cutoff_volatility_bps"] = cutoff
        self._cache[end] = rows
        return rows

    @staticmethod
    def _blank(start: int, end: int, reason: str) -> dict[str, Any]:
        return {
            "history_filter_passed": False,
            "history_filter_reason": reason,
            "history_asof_day_ms": end - DAY_MS,
            "bybit_rv20_bps": None,
            "bitget_rv20_bps": None,
            "bybit_adv20_usdt": None,
            "bitget_adv20_usdt": None,
            "history_rank": None,
            "history_cutoff_volatility_bps": None,
            "history_window_start_ms": start,
            "history_window_end_ms": end,
        }

    def evaluate(self, symbol: str, now_ms: int) -> dict[str, Any]:
        _symbol(symbol)
        if type(now_ms) is not int or now_ms < 0:
            raise ValueError("history evaluation requires a nonnegative millisecond clock")
        end = (now_ms - self.config.publication_delay_ms) // DAY_MS * DAY_MS
        start = end - self.config.window_days * DAY_MS
        if symbol[:-4] in _EXCLUDED_BASE:
            return self._blank(start, end, "asset_excluded")
        if symbol not in self._data:
            return self._blank(start, end, "history_symbol_unavailable")
        return dict(self._rows(end)[symbol])


def _json(path: Path) -> Any:
    return json.loads(path.read_text())


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _directory_eligible(row: dict[str, Any]) -> bool:
    symbol = _symbol(row["symbol"])
    b, g = row["bybit"], row["bitget"]
    base = b.get("baseCoin")
    if symbol != b.get("symbol") or symbol != g.get("symbol") or base != g.get("baseCoin"):
        raise ValueError("directory symbol or underlying mismatch")
    return bool(
        base == symbol[:-4]
        and base not in _EXCLUDED_BASE
        and b.get("contractType") == "LinearPerpetual"
        and g.get("symbolType") == "perpetual"
        and b.get("quoteCoin") == b.get("settleCoin") == g.get("quoteCoin") == "USDT"
        and "USDT" in g.get("supportMarginCoins", [])
        and b.get("status") == "Trading"
        and g.get("symbolStatus") == "normal"
        and not b.get("isPreListing")
        and b.get("symbolType") in ("", "innovation", None)
        and not b.get("underlyingTicker")
        and g.get("isRwa") == "NO"
        and "ST" not in b.get("tags", [])
    )


def _read_bars(path: Path, venue: str, symbol: str, digest: str) -> tuple[HourlyBar, ...]:
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError(f"historical raw archive hash mismatch: {path.name}")
    payload = json.loads(gzip.decompress(raw))
    params, response = payload["params"], payload["response"]
    if payload.get("venue") != venue or params.get("symbol") != symbol:
        raise ValueError("raw history venue/symbol mismatch")
    if venue == "bybit":
        if (
            payload.get("endpoint") != "/v5/market/kline"
            or params.get("category") != "linear"
            or params.get("interval") != "60"
            or response.get("retCode") != 0
            or response["result"].get("symbol") != symbol
            or response["result"].get("category") != "linear"
        ):
            raise ValueError("invalid Bybit hourly response")
        rows = response["result"]["list"]
    else:
        if (
            payload.get("endpoint") != "/api/v2/mix/market/candles"
            or params.get("productType") != "USDT-FUTURES"
            or params.get("granularity") != "1H"
            or params.get("kLineType") != "market"
            or response.get("code") != "00000"
        ):
            raise ValueError("invalid Bitget hourly response")
        rows = response["data"]
    result = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 7:
            raise ValueError("invalid hourly OHLCV shape")
        timestamp = Decimal(str(row[0]))
        if not timestamp.is_finite() or timestamp != timestamp.to_integral_value():
            raise ValueError("invalid hour timestamp")
        o, h, lo, c, volume, turnover = (Decimal(str(v)) for v in row[1:7])
        if (
            not all(v.is_finite() for v in (o, h, lo, c, volume, turnover))
            or min(o, h, lo, c) <= 0
            or min(volume, turnover) < 0
            or not lo <= min(o, c) <= max(o, c) <= h
        ):
            raise ValueError("invalid hourly OHLCV values")
        tolerance = max(Decimal("0.000001"), turnover * Decimal("0.000001"))
        if turnover < lo * volume - tolerance or turnover > h * volume + tolerance:
            raise ValueError("hourly quote turnover inconsistent with price and base volume")
        result.append(HourlyBar(int(timestamp), c, turnover))
    return tuple(result)


def load_public_history(
    screen_dir: Path,
    config: RollingHistoryConfig,
    *,
    identity_path: Path | None = None,
) -> HistoryGate:
    """Load raw candles, never the saved current-day ranking or its metrics."""
    screen_dir = Path(screen_dir)
    universe_path = screen_dir / "universe.json"
    universe = _json(universe_path)
    manifest = _json(screen_dir / "raw-manifest.json")
    identity_path = identity_path or screen_dir / "selection-crypto-verified.json"
    identity = _json(identity_path)
    if identity.get("status") != "VERIFIED_CRYPTO_ONLY_TOP10" or identity.get(
        "source_universe_sha256"
    ) != _sha(universe_path):
        raise ValueError("identity manifest must be verified and bound to this universe snapshot")
    verified = set()
    for row in identity.get("top10", []):
        asset = row.get("asset_identity", {})
        symbol = _symbol(row["symbol"])
        if (
            asset.get("classification") == "crypto_project_token"
            and asset.get("perpetual_symbol") == symbol
            and asset.get("official_project_identity_source")
        ):
            verified.add(symbol)
    if not verified:
        raise ValueError("identity manifest contains no verified crypto symbols")
    data: dict[str, dict[str, tuple[HourlyBar, ...]]] = {}
    hashes = {}
    for row in universe["eligible"]:
        if not _directory_eligible(row):
            continue
        symbol = row["symbol"]
        if symbol in data:
            raise ValueError("duplicate directory symbol")
        data[symbol] = {}
        for venue in _VENUES:
            name = f"{venue}_{symbol}.json.gz"
            path = screen_dir / "raw" / name
            if name not in manifest or not path.is_file():
                raise ValueError(f"incomplete history archive pool: {name}")
            data[symbol][venue] = _read_bars(path, venue, symbol, manifest[name])
            hashes[name] = manifest[name]
    return HistoryGate(
        config,
        data,
        identity_verified_symbols=verified,
        audit_metadata={
            "source_directory": str(screen_dir.resolve()),
            "source_universe_sha256": _sha(universe_path),
            "raw_manifest_sha256": _sha(screen_dir / "raw-manifest.json"),
            "identity_manifest_sha256": _sha(identity_path),
            "raw_file_hashes": hashes,
            "missing_raw_files": [],
            "ranking_pool": "directory_filtered_symbols_with_complete_history_at_evaluation",
            "universe_scope": "current_directory_with_survivorship_bias",
            "identity_scope": identity.get("review_scope"),
            "identity_used_for_admission_only_not_ranking": True,
            "caveats": [
                "Directory and identity snapshots were obtained after historical replay times.",
                "Only reviewed identities may enter; unreviewed top-N symbols are not replaced.",
                "Hourly bars are assumed available at UTC day close plus the configured delay.",
                "Actual historical publication and subsequent revisions cannot be verified.",
                "ADV is turnover, not executable depth; default ADV threshold is disabled.",
                "Archived symbols without a complete window are omitted from that day ranking.",
                "Missing archive files or manifest entries reject the entire pool.",
            ],
        },
    )

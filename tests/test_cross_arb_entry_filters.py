import math
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal as D

import pytest

from btc_cta.cross_arb.entry_filters import EntryFilterConfig, EntryFilters
from btc_cta.cross_arb.market import Book


def paired(bybit="100", bitget=None, quantity="10"):
    def make(venue, midpoint):
        mid = D(midpoint)
        return Book(
            venue, ((mid - D(".001"), D(quantity)),), ((mid + D(".001"), D(quantity)),), 1, 1, 0
        )

    return {"bybit": make("bybit", bybit), "bitget": make("bitget", bitget or bybit)}


def ready(config=None, values=("100", "101", "100", "999"), other=None):
    filters = EntryFilters(config or EntryFilterConfig(volatility_window_s=2))
    for i, mid in enumerate(values):
        books = paired(mid, other[i] if other else None)
        filters.observe(books, 100 + i * 1000)
    return filters, books


def result(filters, books):
    return filters.evaluate(books, "bybit", "bitget")


def test_config_defaults_and_immutability():
    config = EntryFilterConfig()
    assert config.volatility_window_s == 60 and config.volatility_sample_ms == 1000
    assert config.min_volatility_bps == D(5)
    assert config.liquidity_band_bps == D(10) and config.min_depth_usdt == D(500)
    with pytest.raises(FrozenInstanceError):
        config.min_depth_usdt = D(0)


@pytest.mark.parametrize(
    "change",
    [
        {"volatility_window_s": 0},
        {"volatility_window_s": -1},
        {"volatility_window_s": True},
        {"volatility_window_s": 1.5},
        {"volatility_sample_ms": 0},
        {"volatility_sample_ms": True},
        {"volatility_sample_ms": 1.5},
        {"volatility_sample_ms": 700},
        {"volatility_window_s": 1, "volatility_sample_ms": 1000},
        {"volatility_window_s": 10001, "volatility_sample_ms": 1000},
        {"min_volatility_bps": D(-1)},
        {"min_volatility_bps": D("NaN")},
        {"min_volatility_bps": D("Infinity")},
        {"min_volatility_bps": 5},
        {"min_depth_usdt": D(-1)},
        {"min_depth_usdt": D("NaN")},
        {"liquidity_band_bps": D(0)},
        {"liquidity_band_bps": D(10000)},
        {"liquidity_band_bps": D("Infinity")},
        {"liquidity_band_bps": D("NaN")},
    ],
)
def test_config_rejects_undefined_unbounded_or_non_decimal_values(change):
    with pytest.raises(ValueError):
        EntryFilterConfig(**change)


def test_constant_closed_midpoints_have_zero_rv_and_fail():
    filters, books = ready(values=("100",) * 4)
    row = result(filters, books)
    assert row["entry_filter_reason"] == "volatility_below_threshold"
    assert row["entry_filter_passed"] is False
    assert D(row["bybit_volatility_bps"]) == 0
    assert row["volatility_samples"] == row["volatility_required_samples"] == 2


def test_rv_matches_log_return_formula_and_ignores_open_bucket():
    filters, books = ready()
    row = result(filters, books)
    expected = 10000 * math.sqrt(math.log(101 / 100) ** 2 + math.log(100 / 101) ** 2)
    assert float(row["bybit_volatility_bps"]) == pytest.approx(expected)
    assert row["entry_filter_passed"] is True
    assert row["entry_filter_reason"] == "entry_filter_passed"
    assert row["bybit_volatility_bps"] == row["bitget_volatility_bps"]
    filters.observe(paired("900000"), 3999)
    assert result(filters, books)["bybit_volatility_bps"] == row["bybit_volatility_bps"]
    assert result(filters, books)["volatility_samples"] == 2


def test_both_venues_must_reach_volatility_threshold():
    filters, books = ready(other=("100",) * 4)
    row = result(filters, books)
    assert D(row["bybit_volatility_bps"]) > 5
    assert D(row["bitget_volatility_bps"]) == 0
    assert row["entry_filter_reason"] == "volatility_below_threshold"


def test_window_needs_one_more_completed_midpoint_than_returns():
    filters, books = ready(values=("100", "101", "100"))
    row = result(filters, books)
    assert row["entry_filter_reason"] == "entry_filter_warmup"
    assert row["volatility_samples"] == 1
    assert row["bybit_volatility_bps"] is None


def test_many_updates_use_only_bucket_last_quote_and_do_not_inflate_rv():
    config = EntryFilterConfig(volatility_window_s=2)
    sparse = EntryFilters(config)
    dense = EntryFilters(config)
    for bucket, midpoint in enumerate(("100", "101", "100", "999")):
        t = bucket * 1000
        sparse.observe(paired(midpoint), t + 900)
        for offset, value in ((100, "150"), (200, "50"), (900, midpoint)):
            dense.observe(paired(value), t + offset)
    assert result(sparse, paired()) == result(dense, paired())


def test_window_evicts_old_returns_instead_of_accumulating_forever():
    filters, books = ready(values=("100", "110", "110", "110"))
    assert D(result(filters, books)["bybit_volatility_bps"]) > 0
    filters.observe(paired("110"), 4100)
    row = result(filters, paired())
    assert D(row["bybit_volatility_bps"]) == 0
    assert row["volatility_samples"] == 2


@pytest.mark.parametrize("action", ["gap", "rollback", "reset"])
def test_missing_bucket_clock_rollback_and_reset_require_new_warmup(action):
    filters, books = ready()
    if action == "gap":
        filters.observe(paired("200"), 5100)
    elif action == "rollback":
        filters.observe(paired("200"), 3000)
    else:
        filters.reset()
    row = result(filters, books)
    assert row["entry_filter_reason"] == "entry_filter_warmup"
    assert row["volatility_samples"] == 0
    assert row["bybit_volatility_bps"] is None


def test_timeout_preserves_history_until_last_valid_quote_is_more_than_one_sample_old():
    filters, books = ready()
    filters.observe(None, 3200)
    assert result(filters, books)["volatility_samples"] == 2
    filters.observe(None, 4100)
    assert result(filters, books)["volatility_samples"] == 2
    filters.observe(None, 4101)
    assert result(filters, books)["entry_filter_reason"] == "entry_filter_warmup"
    assert result(filters, books)["volatility_samples"] == 0


def test_timeout_can_close_existing_bucket_without_inventing_another_quote():
    filters, books = ready(values=("100", "101", "100"))
    assert result(filters, books)["volatility_samples"] == 1
    filters.observe(None, 3000)
    assert result(filters, books)["volatility_samples"] == 2
    filters.observe(None, 3101)
    assert result(filters, books)["volatility_samples"] == 0


def test_one_bucket_gap_on_a_boundary_is_not_filled_with_previous_price():
    filters = EntryFilters(EntryFilterConfig(volatility_window_s=2))
    filters.observe(paired("100"), 900)
    filters.observe(None, 1000)
    filters.observe(paired("200"), 2000)
    assert result(filters, paired())["volatility_samples"] == 0


def test_minimum_zero_disables_volatility_warmup_but_keeps_depth_gate():
    filters = EntryFilters(EntryFilterConfig(min_volatility_bps=D(0)))
    row = result(filters, paired())
    assert row["entry_filter_passed"] is True and row["volatility_samples"] == 0
    assert row["bybit_volatility_bps"] is None
    assert (
        result(filters, paired(quantity="1"))["entry_filter_reason"] == "liquidity_below_threshold"
    )


def test_minimum_zero_still_calculates_realized_volatility_after_warmup():
    filters, books = ready(EntryFilterConfig(volatility_window_s=2, min_volatility_bps=D(0)))
    assert D(result(filters, books)["bybit_volatility_bps"]) > 5


def test_band_edges_are_inclusive_and_outside_depth_cannot_inflate_liquidity():
    books = {
        "bybit": Book(
            "bybit",
            ((D("99.9"), D(100)),),
            ((D(100), D(1)), (D("100.1"), D(2)), (D("100.1001"), D(1000000))),
            1,
            1,
            0,
        ),
        "bitget": Book(
            "bitget",
            ((D(100), D(1)), (D("99.9"), D(2)), (D("99.8999"), D(1000000))),
            ((D("100.1"), D(100)),),
            1,
            1,
            0,
        ),
    }
    config = EntryFilterConfig(min_volatility_bps=D(0), min_depth_usdt=D("299.8"))
    row = result(EntryFilters(config), books)
    assert D(row["long_depth_usdt"]) == D("300.2")
    assert D(row["short_depth_usdt"]) == D("299.8")
    assert row["entry_filter_passed"] is True
    row = result(EntryFilters(replace(config, min_depth_usdt=D("299.80001"))), books)
    assert row["entry_filter_reason"] == "liquidity_below_threshold"


def test_depth_uses_long_asks_and_short_bids_not_the_opposite_sides():
    books = {
        "bybit": Book("bybit", ((D(99), D(1000)),), ((D(100), D(1)),), 1, 1, 0),
        "bitget": Book("bitget", ((D(99), D(1000)),), ((D(100), D(1)),), 1, 1, 0),
    }
    row = result(EntryFilters(EntryFilterConfig(min_volatility_bps=D(0))), books)
    assert D(row["long_depth_usdt"]) == 100
    assert D(row["short_depth_usdt"]) == 99000
    assert row["entry_filter_reason"] == "liquidity_below_threshold"


def test_volatility_threshold_equality_passes_and_stricter_threshold_fails():
    baseline, books = ready()
    value = D(result(baseline, books)["bybit_volatility_bps"])
    exact, _ = ready(EntryFilterConfig(volatility_window_s=2, min_volatility_bps=value))
    assert result(exact, books)["entry_filter_passed"] is True
    above, _ = ready(
        EntryFilterConfig(volatility_window_s=2, min_volatility_bps=value + D(".000001"))
    )
    assert result(above, books)["entry_filter_reason"] == "volatility_below_threshold"


@pytest.mark.parametrize(
    "books,long,short",
    [
        (None, "bybit", "bitget"),
        ({}, "bybit", "bitget"),
        (paired(), "bybit", "bybit"),
        (paired(), "other", "bitget"),
    ],
)
def test_missing_or_incompatible_feed_fails_closed(books, long, short):
    row = EntryFilters(EntryFilterConfig(min_volatility_bps=D(0))).evaluate(books, long, short)
    assert row["entry_filter_passed"] is False
    assert row["entry_filter_reason"] == "entry_filter_missing_feed"
    assert row["long_depth_usdt"] is None and row["short_depth_usdt"] is None


@pytest.mark.parametrize("now", [-1, 1.5, True])
def test_invalid_observation_clock_rejected(now):
    with pytest.raises(ValueError):
        EntryFilters(EntryFilterConfig()).observe(paired(), now)


def test_subsecond_buckets_use_configured_return_count_and_completed_window():
    config = EntryFilterConfig(volatility_window_s=2, volatility_sample_ms=500)
    filters = EntryFilters(config)
    mids = ("100", "101", "100", "101", "100", "999")
    for index, mid in enumerate(mids):
        filters.observe(paired(mid), 100 + index * 500)
    row = result(filters, paired())
    assert row["volatility_samples"] == row["volatility_required_samples"] == 4
    expected = 10000 * math.sqrt(4 * math.log(1.01) ** 2)
    assert float(row["bybit_volatility_bps"]) == pytest.approx(expected)


def test_explicit_partial_feed_discards_previously_warmed_history():
    filters, books = ready()
    filters.observe({"bybit": books["bybit"]}, 3200)
    assert result(filters, books)["entry_filter_reason"] == "entry_filter_warmup"
    assert result(filters, books)["volatility_samples"] == 0

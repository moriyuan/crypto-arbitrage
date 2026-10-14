from dataclasses import FrozenInstanceError, replace
from decimal import Decimal as D

import pytest

from btc_cta.cross_arb.depth_sizing import (
    DepthSizingConfig,
    paired_capacity,
    paired_depth_details,
    round_pair_qty,
)
from btc_cta.cross_arb.market import Book, Rules


def books():
    return {
        "bybit": Book(
            "bybit",
            ((D("100"), D("8")), (D("99.9"), D("4")), (D("99.899"), D("100"))),
            ((D("100.2"), D("20")), (D("100.3002"), D("12")), (D("100.301"), D("100"))),
            1000,
            1000,
            0,
        ),
        "bitget": Book(
            "bitget",
            ((D("101"), D("10")), (D("100.899"), D("6")), (D("100.898"), D("100"))),
            ((D("101.2"), D("4")), (D("101.3012"), D("8")), (D("101.302"), D("100"))),
            1000,
            1000,
            0,
        ),
    }


def rules(bybit=".0025", bitget=".006"):
    return {
        "bybit": Rules(D(bybit), D(bybit), D(5), D(".0001")),
        "bitget": Rules(D(bitget), D(bitget), D(5), D(".0001")),
    }


def test_config_defaults_and_frozen():
    config = DepthSizingConfig()
    assert config.band_bps == D(10)
    assert config.participation == D(".25")
    with pytest.raises(FrozenInstanceError):
        config.participation = D(".5")


@pytest.mark.parametrize("band", [D(0), D(-1), D(10000), D("NaN"), D("Infinity"), 10, True])
def test_invalid_band_is_rejected(band):
    with pytest.raises(ValueError):
        DepthSizingConfig(band_bps=band)


@pytest.mark.parametrize(
    "fraction", [D(0), D(-1), D("1.0001"), D("NaN"), D("Infinity"), 0.25, True]
)
def test_invalid_participation_is_rejected(fraction):
    with pytest.raises(ValueError):
        DepthSizingConfig(participation=fraction)


@pytest.mark.parametrize(
    "long,short,intent,expected",
    [
        ("bybit", "bitget", "entry", "4"),
        ("bybit", "bitget", "exit", "3"),
        ("bitget", "bybit", "entry", "3"),
        ("bitget", "bybit", "exit", "4"),
    ],
)
def test_capacity_uses_both_legs_and_correct_execution_side(long, short, intent, expected):
    # Levels exactly at the band boundary count; immediately outside never count.
    assert paired_capacity(books(), long, short, intent, DepthSizingConfig()) == D(expected)


def test_details_preserve_each_venue_capacity_in_coin_units():
    assert paired_depth_details(books(), "bybit", "bitget", "entry", DepthSizingConfig()) == {
        "bybit": D(8),
        "bitget": D(4),
    }


def test_full_participation_and_tighter_band():
    assert paired_capacity(
        books(), "bybit", "bitget", "entry", DepthSizingConfig(participation=D(1))
    ) == D(16)
    assert paired_capacity(
        books(), "bybit", "bitget", "entry", DepthSizingConfig(band_bps=D(5))
    ) == D("2.5")


def test_outside_band_quantity_cannot_enlarge_capacity():
    data = books()
    data["bitget"] = replace(
        data["bitget"], bids=data["bitget"].bids[:2] + ((D("100.898"), D("1000000")),)
    )
    assert paired_capacity(data, "bybit", "bitget", "entry", DepthSizingConfig()) == D(4)


@pytest.mark.parametrize(
    "case", ["missing", "mismatch", "none", "same_venue", "intent", "other_venue"]
)
def test_invalid_or_incomplete_pair_is_rejected(case):
    data = books()
    long, short, intent = "bybit", "bitget", "entry"
    if case == "missing":
        data.pop("bitget")
    elif case == "mismatch":
        data["bitget"] = data["bybit"]
    elif case == "none":
        data["bybit"] = None
    elif case == "same_venue":
        short = long
    elif case == "intent":
        intent = "open"
    else:
        long = "unknown"
    with pytest.raises(ValueError):
        paired_capacity(data, long, short, intent, DepthSizingConfig())


@pytest.mark.parametrize(
    "quantity,expected", [(".1", ".09"), (".03", ".03"), (".029999", "0"), ("0", "0")]
)
def test_rounding_uses_common_step_and_always_floors(quantity, expected):
    result = round_pair_qty(D(quantity), rules())
    assert result == D(expected)
    assert result <= D(quantity)
    assert result % D(".0025") == 0 and result % D(".006") == 0


def test_rounding_handles_integer_steps_and_small_decimal_steps():
    assert round_pair_qty(D("23.999"), rules("3", "4")) == D(12)
    assert round_pair_qty(
        D(".000000000000000199"), rules(".000000000000000003", ".000000000000000007")
    ) == D(".000000000000000189")


@pytest.mark.parametrize("quantity", [D(-1), D("NaN"), D("Infinity"), "1", 1, True])
def test_rounding_rejects_invalid_quantity(quantity):
    with pytest.raises(ValueError):
        round_pair_qty(quantity, rules())


@pytest.mark.parametrize(
    "bad_rules", [{}, {"bybit": rules()["bybit"]}, {"bybit": None, "bitget": rules()["bitget"]}]
)
def test_rounding_requires_both_venue_rules(bad_rules):
    with pytest.raises(ValueError):
        round_pair_qty(D(1), bad_rules)

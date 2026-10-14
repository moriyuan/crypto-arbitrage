from audit_lifecycle import funding_intersections, remaining_is_tradeable


def test_funding_exposure_uses_inclusive_boundaries_and_exact_ns():
    times = {'bitget': [1000, 2000]}
    assert funding_intersections(1000, 1000, 'bitget', times) == [1000]
    assert funding_intersections(999, 1000, 'bitget', times) == [1000]
    assert funding_intersections(1001, 1999, 'bitget', times) == []
    assert funding_intersections(999, 2000, 'bitget', times) == [1000, 2000]


def test_maker_source_before_settlement_even_when_receipt_is_after():
    times = {'bitget': [1000]}
    assert funding_intersections(999, 1500, 'bitget', times) == [1000]
    assert funding_intersections(1001, 1500, 'bitget', times) == []


def test_actual_september_partial_exit_would_manufacture_untradeable_pair():
    assert not remaining_is_tradeable(30, 20, [414370, 415748], 5_000_000, 10)
    assert remaining_is_tradeable(30, 30, [414370, 415748], 5_000_000, 10)
    assert remaining_is_tradeable(50, 20, [414370, 415748], 5_000_000, 10)


def test_single_remainder_includes_known_young_lot():
    # Six mature coins plus one already known young coin: closing six would
    # strand the young one; waiting for seven is a valid complete exit.
    assert not remaining_is_tradeable(7, 6, [1_000_000], 5_000_000, 1)
    assert remaining_is_tradeable(7, 7, [1_000_000], 5_000_000, 1)


def test_remainder_requires_both_venue_minima_and_native_quantity():
    assert not remaining_is_tradeable(40, 20, [500000, 200000], 5_000_000, 10)
    assert not remaining_is_tradeable(25, 10, [1_000_000], 5_000_000, 10)
    assert remaining_is_tradeable(25, 10, [1_000_000], 5_000_000, 1)
    assert not remaining_is_tradeable(20, 30, [1_000_000], 5_000_000, 10)

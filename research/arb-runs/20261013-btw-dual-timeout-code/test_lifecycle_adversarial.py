"""Adversarial local-clock / capacity boundaries for the two holding periods.

Only synthetic public trades/books; no source downloads or historical replay.
"""
from decimal import Decimal as D
import sys

sys.path.insert(0, '/Users/mori/code/crypto/state/arb-runs/20261012-btw-activity-maker-code')
sys.path.insert(0, '/Users/mori/code/crypto/state/arb-runs/20261012-btw-as-code')
from maker_engine import Tick, Trade, NS
from test_maker_engine import snapshot, tick, trade
from execution_adapter import PriceSchema
from lifecycle_engine import LifecyclePolicy, LifecycleShadow


def time_only_policy(**overrides):
    values = dict(paired_take_profit_bps=1000000,
                  paired_entry_deterioration_bps=1000000,
                  paired_hard_loss_bps=1000000)
    values.update(overrides)
    return LifecyclePolicy(**values)


def opened(snap=None, **policy):
    snap = snapshot() if snap is None else snap
    shadow = LifecycleShadow(snap, PriceSchema(5, (1, 1)), time_only_policy(**policy))
    shadow.book(tick(1100000000, snap['prices']), True)
    return shadow


def fills(shadow, intent):
    return [e for e in shadow.events if e['type'] == 'fill' and e['intent'] == intent]


def observed(ns, prices=None, display=None, updates=(), valid=True, force=0):
    return Tick(ns, valid, force, True, prices or snapshot()['prices'],
                display or [1000] * 4, list(updates))


def test_hedge_capacity_one_ns_before_expiry_is_usable_but_at_expiry_is_not():
    fill_ns = 1110000000
    deadline = fill_ns + NS
    for when, expected_hedge in ((deadline - 1, 20), (deadline, 0)):
        snap = snapshot()
        snap['sides'][0][0]['remaining_quarter'] = 0
        s = opened(snap)
        s.trade(trade(fill_ns, qty=80))
        s.book(tick(fill_ns + 100000000), True)
        assert s.hedged_qty == 0
        s.book(observed(when, display=[1080,1000,1000,1000],
                        updates=[(0,100100000,1080)]), True)
        assert s.hedged_qty == expected_hedge
        if when == deadline:
            assert s.unhedged_qty == 20 and not fills(s, 'hedge_open')
            assert not fills(s, 'unhedged_close')
            s.book(observed(deadline + 100000000,
                            display=[1080,1000,1000,1000]), False)
            assert s.unhedged_qty == 0
            assert [e['venue'] for e in fills(s, 'unhedged_close')] == [1]


def test_invalid_expiry_book_cannot_fill_and_recovery_uses_new_opposing_l1():
    s = opened()
    s.trade(trade(1110000000, qty=24))
    s.book(tick(2110000000, valid=False), True)
    assert s.closing and s.unhedged_qty == 6
    s.book(tick(2210000000, valid=False), True)
    assert not fills(s, 'unhedged_close')
    changed = [100100,100000,101000,100500]
    s.book(observed(2300000000, changed,
                    updates=[(2,100600000,0),(2,101000000,1000),
                             (3,100300000,0),(3,100500000,1000)]), False)
    closed = fills(s, 'unhedged_close')
    assert len(closed) == 1 and closed[0]['qty'] == 6
    assert closed[0]['ns'] == 2300000000 and closed[0]['price_units'] == 101000
    assert closed[0]['fee_units'] == 3 * 101000 * 6
    assert not fills(s, 'hedge_open')


def test_late_fill_with_full_one_second_receive_lag_reopens_and_waits_new_latency():
    s = opened(unhedged_timeout_ms=200)
    s.trade(trade(1110000000, qty=24))
    s.book(tick(1310000000), True)
    s.book(tick(1410000000), True)
    assert s.unhedged_qty == 0
    late_ns = 2209000000
    assert not s.can_archive(late_ns - 1)
    s.trade(trade(late_ns, source=1209000000, qty=24))
    assert s.maker_qty == 12 and s.unhedged_qty == 6
    closed_before = len(fills(s, 'unhedged_close'))
    s.book(tick(late_ns + 99999999), False)
    assert len(fills(s, 'unhedged_close')) == closed_before
    s.book(tick(late_ns + 100000000), False)
    assert s.unhedged_qty == 0 and not fills(s, 'hedge_open')
    last = fills(s, 'unhedged_close')[-1]
    assert last['ns'] == late_ns + 100000000 and last['trigger_ns'] >= late_ns
    # Causal event timestamps never regress to the late trade's source time.
    assert all(a['ns'] <= b['ns'] for a,b in zip(s.events,s.events[1:]))


def test_long_maker_unhedged_timeout_uses_same_venue_bid_and_native_quantity():
    snap = snapshot()
    snap['prices'] = [100300,100000,100600,100500]
    snap['entry_prices'] = {'bybit':100300,'bitget':100500}
    snap['sides'] = [[dict(price_units_1e8=p*1000,displayed_qty=1000,
                           remaining_quarter=0 if i == 3 else 1000)]
                     for i,p in enumerate(snap['prices'])]
    s = opened(snap)
    assert s.maker == 0 and s.side == 'buy' and s.quote == 100299
    s.trade(trade(1110000000, venue=0, side=1, px=100100, qty=40))
    assert s.maker_qty == 10
    s.book(tick(1210000000, snap['prices']), True)
    assert s.hedged_qty == 0
    s.book(tick(2110000000, snap['prices']), True)
    s.book(tick(2210000000, snap['prices']), False)
    close = fills(s, 'unhedged_close')
    assert len(close) == 1 and close[0]['venue'] == 0 and close[0]['side'] == 'sell'
    assert close[0]['qty'] == 10 and close[0]['price_units'] == 100000
    assert s.result()['venue_positions'] == {'bybit':0,'bitget':0}


def test_sixty_second_deadline_is_not_extended_by_a_later_hedged_fill():
    s = opened()  # Actual default hedged_hold_ms=60000.
    s.trade(trade(1110000000, qty=80))
    s.book(tick(1210000000), True)
    assert s.first_hedge_ns == 1210000000
    s.trade(trade(1400000000, source=1200000000, qty=40))
    s.book(tick(1500000000), True)
    assert s.paired_qty == 30
    deadline = 1210000000 + 60*NS
    s.book(tick(deadline - 1), False)
    assert not fills(s, 'paired_close') and s.paired_qty == 30
    s.book(tick(deadline), False)
    assert not fills(s, 'paired_close')
    s.book(tick(deadline + 99999999), False)
    assert not fills(s, 'paired_close')
    s.book(tick(deadline + 100000000), False)
    assert s.paired_qty == 0
    assert {e['ns'] for e in fills(s, 'paired_close')} == {deadline + 100000000}
    assert s.first_hedge_ns == 1210000000


def test_partial_pair_close_preserves_deadline_and_consumed_budget():
    snap = snapshot()
    snap['sides'][1][0]['remaining_quarter'] = 40  # Only ten coins can close now.
    s = opened(snap, hedged_hold_ms=1000)
    s.trade(trade(1110000000, qty=160))
    s.book(tick(1210000000), True)
    assert s.paired_qty == 40
    s.book(tick(2210000000), True)
    s.book(tick(2310000000), False)
    assert s.paired_qty == 30
    original_reason = s.paired_exit_reason
    s.book(tick(2350000000), False)
    assert s.paired_qty == 30  # An unchanged display never restores capacity.
    s.book(observed(2400000000, display=[1000,1080,1000,1000],
                    updates=[(1,100000000,1080)]), False)
    assert s.paired_qty == 10 and s.paired_exit_reason == original_reason
    assert s.first_hedge_ns == 1210000000
    s.book(observed(2500000000, display=[1000,1080,1000,1000]), False)
    assert s.paired_qty == 10
    s.book(observed(2600000000, display=[1000,1120,1000,1000],
                    updates=[(1,100000000,1120)]), False)
    assert s.paired_qty == 0
    paired = fills(s, 'paired_close')
    assert [e['qty'] for e in paired] == [10,10,20,20,10,10]
    assert s.result()['venue_positions'] == {'bybit':0,'bitget':0}
    assert D(s.result()['fees']) == D(sum(e['fee_units'] for e in s.events
                                            if e['type']=='fill')) / 10**9


def test_oldest_lot_expiry_stops_hedging_but_younger_existing_lot_keeps_own_deadline():
    snap = snapshot()
    snap['sides'][0][0]['remaining_quarter'] = 0
    s = opened(snap)
    s.trade(trade(1110000000, qty=24))
    s.trade(trade(1300000000, source=1200000000, qty=24))
    assert s.unhedged_qty == 12
    # Hedge depth returns when the oldest lot expires. The younger lot is
    # mature but global closing forbids any fresh hedge from this point.
    s.book(observed(2110000000, display=[1080,1000,1000,1000],
                    updates=[(0,100100000,1080)]), True)
    lots = sorted(s.result()['residual_unhedged_lots'],key=lambda x:x['fill_ns'])
    assert lots[0]['close_trigger_ns'] == 2110000000
    assert lots[1]['deadline_ns'] == 2300000000 and lots[1]['close_trigger_ns'] is None
    assert not fills(s, 'hedge_open')
    s.book(observed(2210000000,display=[1080,1000,1000,1000]), False)
    assert s.unhedged_qty == 6
    s.book(observed(2299999999,display=[1080,1000,1000,1000]), False)
    assert len(fills(s,'unhedged_close')) == 1
    s.book(observed(2300000000,display=[1080,1000,1000,1000]), False)
    assert s.unhedged_qty == 6
    s.book(observed(2399999999,display=[1080,1000,1000,1000]), False)
    assert s.unhedged_qty == 6
    s.book(observed(2400000000,display=[1080,1000,1000,1000]), False)
    assert s.unhedged_qty == 0 and not fills(s,'hedge_open')
    assert [e['qty'] for e in fills(s,'unhedged_close')] == [6,6]
    assert [e['trigger_ns'] for e in fills(s,'unhedged_close')] == [2110000000,2300000000]


def test_archive_end_before_exit_latency_keeps_inventory_without_future_fill():
    s = opened()
    s.trade(trade(1110000000,qty=24))
    s.book(tick(2110000000),True)
    end = 2209999999
    s.finish(end)
    result = s.result()
    assert result['state'] == 'UNRESOLVED' and result['unhedged_open_qty'] == 6
    assert result['venue_positions'] == {'bybit':0,'bitget':-6}
    assert result['pnl_complete_before_funding'] is False
    assert not fills(s,'unhedged_close') and not s.can_archive(end)
    assert all(e['ns'] <= end for e in s.events)

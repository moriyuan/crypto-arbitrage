from decimal import Decimal as D
import sys
import pytest
sys.path.insert(0,'/Users/mori/code/crypto/state/arb-runs/20261012-btw-activity-maker-code')
sys.path.insert(0,'/Users/mori/code/crypto/state/arb-runs/20261012-btw-as-code')
from maker_engine import Tick,Trade,NS
from test_maker_engine import snapshot,tick,trade
from execution_adapter import PriceSchema
from lifecycle_engine import LifecyclePolicy,LifecycleShadow

def policy(**kw):
    p=dict(paired_take_profit_bps=1000000,paired_entry_deterioration_bps=1000000,paired_hard_loss_bps=1000000)
    p.update(kw);return LifecyclePolicy(**p)
def live(**kw):
    s=LifecycleShadow(snapshot(),PriceSchema(5,(1,1)),policy(**kw));s.book(tick(1100000000),True)
    assert s.quote==100301;return s
def change(ns,p,display=1000,force=0):
    return Tick(ns,True,force,True,p,[display]*4,[(i,x*1000,display) for i,x in enumerate(p)])
def fills(s,intent):return [x for x in s.events if x['type']=='fill' and x['intent']==intent]

def test_policy_validation():
    for p in ({'unhedged_timeout_ms':0},{'hedged_hold_ms':-1},{'counterparty_offset_ticks':0},{'paired_take_profit_bps':-1}):
        with pytest.raises(ValueError):LifecyclePolicy(**p)

def test_no_inside_and_postonly_rejection():
    a=snapshot();a['prices']=[100001,100000,100301,100300]
    s=LifecycleShadow(a,PriceSchema(5,(1,1)));assert s.state=='REJECTED' and s.can_archive(a['entry_ns'])
    s=LifecycleShadow(snapshot(),PriceSchema(5,(1,1)));s.book(tick(1100000000,[100100,100000,100600,100500]),True)
    assert s.state=='REJECTED' and not fills(s,'maker_open')

def test_wrong_side_touch_and_source_before_arrival_excluded():
    s=live()
    for t in (trade(1110000000,side=1),trade(1110000000,px=100301),trade(1110000000,source=1090000000)):
        s.trade(t)
    assert s.maker_qty==0

def test_unhedged_timeout_requests_then_waits_100ms_to_close_same_venue():
    s=live();s.trade(trade(1110000000,qty=24)) # six native Bitget coins cannot hedge common10
    s.book(tick(2110000000),True)
    assert s.unhedged_qty==6 and not fills(s,'unhedged_close') and s.closing
    s.book(tick(2209999999),True);assert s.unhedged_qty==6
    s.book(tick(2210000000),True)
    assert s.unhedged_qty==0 and len(fills(s,'unhedged_close'))==1 and not fills(s,'hedge_open')
    e=fills(s,'unhedged_close')[0];assert e['venue']==1 and e['side']=='buy' and e['trigger_ns']==2110000000
    assert e['qty']==6 and e['price_units']==100600 and e['fee_units']==3*100600*6

def test_expired_inventory_never_hedges_even_if_depth_returns_at_deadline():
    s=live();s.trade(trade(1110000000,qty=80));s.budget[0][100100000][1]=0
    s.book(tick(1210000000),True);assert s.hedged_qty==0
    s.book(change(2110000000,[100100,100000,100600,100300],1080),True)
    assert not fills(s,'hedge_open') and s.unhedged_qty==20
    s.book(change(2210000000,[100100,100000,100600,100300],1080),True)
    assert s.unhedged_qty==0 and not fills(s,'hedge_open')

def test_paired_hold_begins_first_hedge_not_maker_and_does_not_extend():
    s=live(hedged_hold_ms=1000);s.trade(trade(1110000000,qty=80));s.book(tick(1210000000),True)
    assert s.paired_qty==20 and s.first_hedge_ns==1210000000 and s.state!='DONE'
    s.trade(trade(1300000000,qty=40,source=1200000000));s.book(tick(1400000000),True)
    assert s.paired_qty==30 and s.first_hedge_ns==1210000000
    s.book(tick(2210000000),True);assert s.paired_qty==30 and not fills(s,'paired_close')
    s.book(tick(2310000000),True)
    assert s.paired_qty==0 and len(fills(s,'paired_close'))==2 and s.can_archive(2310000000)

def test_paired_timer_runs_with_unhedged_dust_and_no_fake_dust_close():
    s=live(hedged_hold_ms=2000);s.trade(trade(1110000000,qty=52));s.book(tick(1210000000),True)
    assert s.paired_qty==10 and s.unhedged_qty==3
    s.book(tick(2210000000),True);assert s.unhedged_qty==3 and s.paired_qty==10
    s.book(tick(3210000000),True);s.book(tick(3310000000),True)
    assert s.paired_qty==0 and s.unhedged_qty==3 and not s.can_archive(3310000000)
    s.finish(10*NS);assert s.state=='UNRESOLVED' and s.result()['venue_positions']['bitget']==-3

def test_late_fill_after_cancel_and_exit_stays_in_ledger_and_never_opens_hedge():
    s=live(unhedged_timeout_ms=200);s.trade(trade(1110000000,qty=24))
    s.book(tick(1310000000),True);s.book(tick(1410000000),True);assert s.unhedged_qty==0
    s.trade(trade(1500000000,qty=24,source=1200000000));assert s.unhedged_qty==6
    s.book(tick(1599999999),True);assert s.unhedged_qty==6
    s.book(tick(1600000000),True);assert s.unhedged_qty==0 and not fills(s,'hedge_open')
    assert not s.can_archive(1600000000);s.advance(2210000000);assert s.can_archive(2210000000)

def test_force_close_all_waits_latency_and_ignores_inactive_entry_gate():
    s=live();s.trade(trade(1110000000,qty=104));s.book(tick(1210000000),True)
    assert s.paired_qty==20 and s.unhedged_qty==6
    s.book(tick(1300000000,force=1),False);assert not fills(s,'paired_close')
    s.book(tick(1400000000),False);assert s.paired_qty==s.unhedged_qty==0
    assert s.result()['funding_guard_seen_ns']==1300000000

def test_invalid_book_does_not_execute_close_or_invent_archive_fill():
    s=live();s.trade(trade(1110000000,qty=24));s.book(tick(2500000000,valid=False),True)
    assert s.unhedged_qty==6 and not fills(s,'unhedged_close')
    s.finish(4*NS);assert s.state=='UNRESOLVED' and s.unhedged_qty==6

def test_close_budget_consumption_and_minimum_dust_are_retained():
    s=live(hedged_hold_ms=1000);s.trade(trade(1110000000,qty=160));s.book(tick(1210000000),True)
    s.budget[1][100000000][1]=40;s.book(tick(2210000000),True);s.book(tick(2310000000),True)
    assert s.paired_qty==30
    s.book(tick(2400000000),True);assert s.paired_qty==30 # unchanged L1 never replenishes consumed budget

def test_micro_price_and_one_coin_fee_precision_preserved():
    a=snapshot();a['prices']=[p*10 for p in a['prices']];a.update(price_decimals=6,money_decimals=10,native_tick_units=[10,1])
    s=LifecycleShadow(a,PriceSchema(6,(10,1)),policy());s.book(Tick(1100000000,True,0,True,a['prices'],[1000]*4,[]),True)
    s.trade(Trade(1110000000,1110000000,1,0,100400100,400000000,1,0,0))
    assert s.quote==1003001 and s.maker_qty==1 and fills(s,'maker_open')[0]['fee_units']==3009003
    assert D(s.result()['venue_fees']['bitget'])==D('.0003009003')

def test_take_profit_requests_then_paired_close_executes_after_latency():
    s=LifecycleShadow(snapshot(),PriceSchema(5,(1,1)),LifecyclePolicy());s.book(tick(1100000000),True)
    s.trade(trade(1110000000,qty=80));s.book(tick(1210000000),True)
    s.book(change(1400000000,[100250,100200,100201,100100]),True)
    assert s.paired_exit_reason=='take_profit' and s.paired_qty==20
    s.book(change(1499999999,[100250,100200,100201,100100]),True);assert s.paired_qty==20
    s.book(change(1500000000,[100250,100200,100201,100100]),True);assert s.paired_qty==0

def test_entry_deterioration_and_hard_stop_priorities():
    for p,reason in [([100100,99790,100600,100300],'stop_loss'),([100100,99500,101000,100300],'hard_stop_loss')]:
        s=LifecycleShadow(snapshot(),PriceSchema(5,(1,1)),LifecyclePolicy());s.book(tick(1100000000),True)
        s.trade(trade(1110000000,qty=80));s.book(tick(1210000000),True);s.book(change(1400000000,p),True)
        assert s.paired_exit_reason==reason

def test_closed_cashflow_gross_fees_exact_and_unhedged_fees_not_paired():
    s=live(hedged_hold_ms=1000);s.trade(trade(1110000000,qty=104));s.book(tick(1210000000),True)
    assert s.paired_entry_fees==3*20*(100301+100100)
    s.book(tick(2210000000),True);s.book(tick(2310000000),True);r=s.result()
    assert r['venue_positions']=={'bybit':0,'bitget':0}
    assert D(r['gross_realized'])==D(20*(100000-100100)+26*(100301-100600))/100000
    fee=sum(e['fee_units'] for e in s.events if e['type']=='fill')
    assert D(r['fees'])==D(fee)/10**9 and D(r['net_before_funding'])==D(r['gross_realized'])-D(r['fees'])

def test_equal_net_coin_does_not_mean_pair_has_been_closed():
    s=live();s.trade(trade(1110000000));s.book(tick(1210000000),True)
    r=s.result();assert sum(r['venue_positions'].values())==0 and r['paired_open_qty']==20 and not s.can_archive(100*NS)


def low_price_live(qty=30,block_hedge=False):
    a=snapshot();p=[41420,41400,41460,41410];a.update(prices=p,qty=qty)
    a['sides']=[[{'price_units_1e8':x*1000,'displayed_qty':1000,'remaining_quarter':1000}] for x in p]
    s=LifecycleShadow(a,PriceSchema(5,(1,1)),policy(hedged_hold_ms=1000))
    s.book(tick(1100000000,p),True)
    if block_hedge:s.budget[0][p[0]*1000][1]=0
    s.trade(trade(1110000000,px=41450,qty=qty*4));s.book(tick(1210000000,p),True)
    return s,p


def test_regression_paired_30_cannot_close20_and_create_illegal10_remainder():
    s,p=low_price_live();assert s.paired_qty==30
    s.budget[1][p[1]*1000][1]=80 # only20 available, min legal common lot is20
    s.book(tick(2210000000,p),True);s.book(tick(2310000000,p),True)
    assert s.paired_qty==30 and not fills(s,'paired_close')
    s.book(Tick(2400000000,True,0,True,p,[1000,1040,1000,1000],[(1,p[1]*1000,1040)]),True)
    assert s.paired_qty==0 and [e['qty'] for e in fills(s,'paired_close')]==[30,30]


def test_paired_partial_close_reduces40_to30_so_legal20_remains():
    s,p=low_price_live(50);s.budget[1][p[1]*1000][1]=160
    s.book(tick(2210000000,p),True);s.book(tick(2310000000,p),True)
    assert s.paired_qty==20 and [e['qty'] for e in fills(s,'paired_close')]==[30,30]


def test_unhedged_native1_reduces20_to17_to_preserve_legal13_remainder():
    s,p=low_price_live(30,block_hedge=True);assert s.unhedged_qty==30
    s.budget[2][p[2]*1000][1]=80
    s.book(tick(2110000000,p),True);s.book(tick(2210000000,p),True)
    assert s.unhedged_qty==13 and [e['qty'] for e in fills(s,'unhedged_close')]==[17]
    assert s.paired_qty==0


def test_unhedged_native1_waits_when_no_two_legal_parts_exist():
    s,p=low_price_live(20,block_hedge=True);s.budget[2][p[2]*1000][1]=52 #13 available
    s.book(tick(2110000000,p),True);s.book(tick(2210000000,p),True)
    assert s.unhedged_qty==20 and not fills(s,'unhedged_close')


def test_unhedged_dust_guard_counts_known_young_lot_not_only_ready_lots():
    s=live();s.trade(trade(1110000000,qty=24));s.trade(trade(1150000000,qty=8)) #6+2, each below common10
    s.book(tick(2210000000),True)
    assert s.unhedged_qty==8 and not fills(s,'unhedged_close')
    # The young lot becomes close-ready at its own deadline+100ms; close8 together.
    s.book(tick(2250000000),True)
    assert s.unhedged_qty==0 and [e['qty'] for e in fills(s,'unhedged_close')]==[8]

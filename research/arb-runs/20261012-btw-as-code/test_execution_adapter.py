from decimal import Decimal as D
from execution_adapter import ExecutionShadow,PriceSchema
from maker_engine import Shadow,Tick,Trade
import sys
sys.path.insert(0,'/Users/mori/code/crypto/state/arb-runs/20261012-btw-activity-maker-code')
from test_maker_engine import snapshot,tick,trade


def canonical(x):
    names={'quote_ticks':'quote_price_units','price_ticks':'price_units','own_ask_ticks':'own_ask_price_units','own_bid_ticks':'own_bid_price_units'}
    return {names.get(k,k.replace('_nano','_units')):v for k,v in x.items() if k not in ('alpha_percent','scenario_id','price_schema')}


def test_002_exact_execution_and_late_fill_parity():
    old=Shadow(snapshot(),50);new=ExecutionShadow(snapshot(),old.quote,'test',PriceSchema(5,(1,1)))
    for s in (old,new):
        s.book(tick(1100000000),True);s.trade(trade(1110000000));s.book(tick(1210000000),True)
        s.trade(trade(1300000000,source=1200000000,qty=40));s.book(tick(1400000000),True)
    assert canonical(old.result())==canonical(new.result())
    assert list(map(canonical,old.events))==list(map(canonical,new.events))


def micro_snapshot():
    s=snapshot();s['prices']=[p*10 for p in s['prices']]
    s.update(price_decimals=6,money_decimals=10,native_tick_units=[10,1])
    s['entry_prices']={k:v*10 for k,v in s['entry_prices'].items()};return s


def test_micro_bitget_partial_fee_preserves_tenth_nanodollar():
    a=micro_snapshot();s=ExecutionShadow(a,1003001,'micro',PriceSchema(6,(10,1)))
    s.book(Tick(1100000000,True,0,True,a['prices'],[1000]*4,[]),True)
    s.trade(Trade(1110000000,1110000000,1,0,100400100,400000000,1,0,0))
    assert s.maker_qty==1 and s.maker_fees==3009003
    assert D(s.result()['maker_fees'])==D('0.0003009003')
    s.book(Tick(1210000000,True,0,True,a['prices'],[1000]*4,[]),True)
    assert s.hedged_qty==0 and s.unhedged_qty==1


def test_003_depth_key_and_hedge_price_use_exact_micro_units():
    a=micro_snapshot();s=ExecutionShadow(a,1003001,'micro',PriceSchema(6,(10,1)))
    s.book(Tick(1100000000,True,0,True,a['prices'],[1000]*4,[]),True)
    s.trade(Trade(1110000000,1110000000,1,0,100400100,4000000000,1,0,0))
    s.book(Tick(1210000000,True,0,True,a['prices'],[1000]*4,[]),True)
    assert s.maker_qty==s.hedged_qty==10 and s.hedge_fees==3*1001000*10
    assert s.budget[0][100100000][1]==960


def test_bybit_native_quote_grid_is_ten_micro_units():
    a=micro_snapshot();a['prices']=[1005000,1000000,1006000,1005000]
    s=ExecutionShadow(a,1000001,'micro',PriceSchema(6,(10,1)))
    assert s.maker==0 and s.state=='REJECTED' and s.reason=='invalid_native_price_grid'

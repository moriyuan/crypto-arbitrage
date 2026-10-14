from decimal import Decimal as D
import json
import pytest
from btc_cta.cross_arb.engine import Policy
from btc_cta.cross_arb.market import Book, Rules
from btc_cta.cross_arb.depth_sizing import DepthSizingConfig
from runner import FirstLevelPaper

VENUES = ('bybit', 'bitget')
RULES = {v: Rules(D(10), D(10), D(5), D('.00001')) for v in VENUES}

def pair(now=1000, reverse=False, exit=False, top_qty=80):
    prices = [('1.00150', '1.00160'), ('1.00130', '1.00140')] if exit else [('.99990', '1'), ('1.003', '1.00310')]
    if reverse: prices.reverse()
    return {v: Book(v, ((D(b), D(top_qty)), (D(b)-D('.00001'), D(5000))),
                   ((D(a), D(top_qty)), (D(a)+D('.00001'), D(5000))), now, now, 0)
            for v, (b,a) in zip(VENUES, prices)}

def session(tmp_path, **policy):
    return FirstLevelPaper(tmp_path/'paper.sqlite', Policy(bybit_fee_bps=D(3), bitget_fee_bps=D(3),
                           price_guard_bps=D(0), **policy), symbol='BTWUSDT',
                           depth_sizing=DepthSizingConfig(), checkpoint_every=10)

def rows(s, table):
    return [json.loads(r[0]) for r in s.db.execute(f'SELECT value FROM {table} ORDER BY id')]

@pytest.mark.parametrize('reverse', [False, True])
def test_entries_and_exits_fill_and_quote_exact_opposite_best(tmp_path, reverse):
    s=session(tmp_path)
    sources={1000:pair(reverse=reverse), 1100:pair(now=1100,reverse=reverse,exit=True)}
    for t,books in sources.items():s.tick_offline(books,RULES,t)
    orders=rows(s,'orders')
    assert len(orders)==4 and s.state['cycle'] is None
    for o in orders:
        book=sources[o['timestamp_ms']][o['venue']]
        top=(book.asks if o['side']=='buy' else book.bids)[0][0]
        assert D(o['price'])==D(o['limit_price'])==top
        assert D(o['qty'])==20
        assert D(o['fee'])==D(o['notional'])*D('.0003')
    assert len(rows(s,'cycles'))==1
    s.close()

def test_thin_best_does_not_borrow_large_second_level(tmp_path):
    s=session(tmp_path);s.tick_offline(pair(top_qty=20),RULES,1000)
    assert not rows(s,'orders')
    assert {x['reason'] for x in rows(s,'signals')}=={'quantity_below_common_step'}
    s.close()

def test_partial_exit_waits_without_reusing_same_best_quantity(tmp_path):
    s=session(tmp_path);s.tick_offline(pair(),RULES,1000)
    s.tick_offline(pair(now=1100,exit=True,top_qty=40),RULES,1100)
    assert D(s.state['cycle']['qty'])==10 and s.state['orders']==4
    s.tick_offline(pair(now=1200,exit=True,top_qty=40),RULES,1200)
    assert D(s.state['cycle']['qty'])==10 and s.state['orders']==4
    s.tick_offline(pair(now=1300,exit=True,top_qty=80),RULES,1300)
    assert s.state['cycle'] is None and s.state['orders']==6
    c=rows(s,'cycles')[0]
    assert c['exit_parts']==2
    assert sum(D(x)-1000 for x in s.state['cash'].values())==D(c['net_before_funding'])
    s.close()

def test_best_away_and_back_preserves_consumption_and_only_increase_refills(tmp_path):
    s=session(tmp_path);books=pair();s._observe_depth(books)
    details=s._depth_details(books,'bybit','entry');s._consume_depth(details,D(20))
    b=books['bybit']
    promoted=Book('bybit',b.bids,((D('.99999'),D(80)),*b.asks),1100,1100,0)
    s._observe_depth({**books,'bybit':promoted})
    s._observe_depth(books)
    assert s._depth_details(books,'bybit','entry')['bybit']['capacity']==0
    s._observe_depth(pair(top_qty=120))
    assert s._depth_details(pair(top_qty=120),'bybit','entry')['bybit']['capacity']==10
    s.close()

def test_risk_and_equity_do_not_use_deeper_levels_when_best_is_insufficient(tmp_path):
    s=session(tmp_path);s.tick_offline(pair(),RULES,1000)
    s.tick_offline(pair(now=1100,exit=True,top_qty=20),RULES,1100)
    assert s.state['orders']==2 and s.state['cycle'] is not None
    assert all(x['mark_status']=='insufficient_depth' and x['equity'] is None for x in rows(s,'equity')[-2:])
    s.close()

def test_exact_cost_boundary_uses_best_quotes_and_unchanged_5bps(tmp_path):
    s=session(tmp_path)
    rules={v:Rules(D(1),D(1),D(1),D('.0001')) for v in VENUES}
    for t,bid,eligible in [(1000,'1.0021',True),(1100,'1.0020',False)]:
        # For the exact 21bp boundary use a symmetric midpoint of 2.
        books={'bybit':Book('bybit',((D('1.9978'),D(400)),),((D('1.9979'),D(400)),),t,t,0),
               'bitget':Book('bitget',((D(bid)+1,D(400)),),((D('2.0022'),D(400)),),t,t,0)}
        s._observe_depth(books)
        row=s._evaluations(books,rules,t,'test')[0]
        assert row['eligible'] is eligible
        if eligible:assert D(row['gross_edge_bps'])==21 and D(row['net_edge_bps'])==5
    s.close()

def test_off_grid_quote_fails_before_creating_orders(tmp_path):
    s=session(tmp_path);books=pair();b=books['bybit']
    books['bybit']=Book('bybit',b.bids,((D('1.000001'),D(80)),),1000,1000,0)
    with pytest.raises(ValueError,match='off-grid'):s.tick_offline(books,RULES,1000)
    assert not rows(s,'orders') and s.state['ticks']==0
    s.close()

def test_nonzero_price_guard_is_refused(tmp_path):
    with pytest.raises(ValueError,match='zero price guard'):
        FirstLevelPaper(tmp_path/'invalid.sqlite',Policy(),depth_sizing=DepthSizingConfig())

def test_l1_fault_rolls_back_uncommitted_orders_cash_and_budget(tmp_path):
    s=session(tmp_path);s.tick_offline(pair(),RULES,1000)
    assert s.state['orders']==2 and D(s.state['cash']['bybit'])<1000
    books=pair(now=1100);b=books['bitget']
    books['bitget']=Book('bitget',((D('1.003001'),D(80)),),b.asks,1100,1100,0)
    with pytest.raises(ValueError,match='off-grid'):s.tick_offline(books,RULES,1100)
    assert not rows(s,'orders') and s.state['orders']==0
    assert s.state['cash']=={'bybit':'1000','bitget':'1000'}
    assert not s.state['depth_liquidity']
    assert s.state['quote_model']=='counterparty_best'
    s.close()

from decimal import Decimal as D
import json
import pytest
from btc_cta.cross_arb.market import Book, Rules
from btc_cta.cross_arb.depth_sizing import DepthSizingConfig
from risk_policy import Policy
from runner import FirstLevelPaper
from test_first_level import pair, RULES, rows


def session(tmp_path, **policy):
    return FirstLevelPaper(tmp_path/'risk.sqlite', Policy(
        bybit_fee_bps=D(0), bitget_fee_bps=D(0), price_guard_bps=D(0), **policy),
        symbol='BTWUSDT', depth_sizing=DepthSizingConfig(), checkpoint_every=10)


def exit_books(t, long_bid, short_ask, reverse=False, top_qty=80):
    quotes=[(D(long_bid), D(long_bid)+D('.00001')),
            (D(short_ask)-D('.00001'), D(short_ask))]
    if reverse: quotes.reverse()
    return {v: Book(v, ((b,D(top_qty)),), ((a,D(top_qty)),), t,t,0)
            for v,(b,a) in zip(('bybit','bitget'),quotes)}


@pytest.mark.parametrize('reverse',[False,True])
@pytest.mark.parametrize('bid,ask,reason',[
    ('1.00009','1.00290',None), ('1.00010','1.00290','take_profit'),
    ('1.00011','1.00290','take_profit'),
    ('.99991','1.00310',None), ('.99990','1.00310','stop_loss'),
    ('.99989','1.00310','stop_loss'),
])
def test_inclusive_single_long_notional_boundaries(tmp_path,reverse,bid,ask,reason):
    s=session(tmp_path)
    s.tick_offline(pair(reverse=reverse),RULES,1000)
    assert D(s.state['cycle']['qty'])==20
    s.tick_offline(exit_books(1100,bid,ask,reverse),RULES,1100)
    cycles=rows(s,'cycles')
    assert len(cycles)==int(reason is not None)
    if reason:
        assert cycles[0]['exit_reason']==reason
        event=next(e for e in rows(s,'events') if e['kind']=='exit_triggered')
        ev=event['evaluation']
        assert ev['risk_basis']=='remaining_long_entry_notional'
        assert D(ev['risk_basis_notional'])==20
        assert D(ev['take_profit_threshold_usdt'])==D('.004')
        assert D(ev['stop_loss_threshold_usdt'])==D('-.004')
    else:
        assert s.state['orders']==2 and s.state['cycle']['pending_exit_reason']==''
    s.close()


def test_fees_alone_trigger_net_stop_and_are_posted_only_once(tmp_path):
    s=FirstLevelPaper(tmp_path/'fees.sqlite',Policy(
        bybit_fee_bps=D(3),bitget_fee_bps=D(3),price_guard_bps=D(0)),
        symbol='BTWUSDT',depth_sizing=DepthSizingConfig())
    unchanged=exit_books(1000,'.99999','1.00301')
    s.tick_offline(unchanged,RULES,1000)
    assert s.state['orders']==2
    s.tick_offline(exit_books(1100,'.99999','1.00301'),RULES,1100)
    assert s.state['orders']==4 and s.state['cycle'] is None
    ev=next(e['evaluation'] for e in rows(s,'events') if e['kind']=='exit_triggered')
    assert D(ev['estimated_position_pnl']) < D('-.004')
    assert D(ev['estimated_position_gross_pnl']) > D('-.004')
    c=rows(s,'cycles')[0]
    assert c['exit_reason']=='stop_loss' and D(c['net_before_funding'])<D('-.004')
    assert D(c['net_before_funding'])==D(c['gross_pnl'])-D(c['total_fees'])
    assert D(c['net_before_funding'])==D(ev['estimated_position_pnl'])
    assert sum(D(x)-1000 for x in s.state['cash'].values())==D(c['net_before_funding'])
    assert 'stop_loss_usdt' not in s.state['policy']
    assert D(s.state['policy']['stop_loss_bps'])==2
    s.close()


def test_gross_profit_cannot_trigger_tp_when_net_is_below_stop(tmp_path):
    s=FirstLevelPaper(tmp_path/'tp-fees.sqlite',Policy(
        bybit_fee_bps=D(3),bitget_fee_bps=D(3),price_guard_bps=D(0)),
        symbol='BTWUSDT',depth_sizing=DepthSizingConfig())
    s.tick_offline(pair(),RULES,1000)
    s.tick_offline(exit_books(1100,'1.00011','1.00290'),RULES,1100)
    assert s.state['orders']==4 and s.state['cycle'] is None
    ev=next(e['evaluation'] for e in rows(s,'events') if e['kind']=='exit_triggered')
    assert D(ev['estimated_position_gross_pnl']) > D('.004')
    assert D(ev['estimated_position_pnl']) < 0
    assert rows(s,'cycles')[0]['exit_reason']=='stop_loss'
    assert D(rows(s,'cycles')[0]['gross_pnl']) > D('.004')
    assert D(rows(s,'cycles')[0]['net_before_funding']) < 0
    assert sum(D(x)-1000 for x in s.state['cash'].values())==D(rows(s,'cycles')[0]['net_before_funding'])
    s.close()


@pytest.mark.parametrize('delta,reason',[
    ('.0007008',None),('.0007009','take_profit'),('.0007010','take_profit'),
    ('.0005010',None),('.0005009','stop_loss'),('.0005008','stop_loss'),
])
def test_nonzero_fee_net_threshold_exact_and_both_sides(tmp_path,delta,reason):
    # Fine synthetic price grid permits exact net-boundary quotes. Opposite
    # price moves keep their sum fixed, so four-fill fees are exactly 0.024036.
    rules={v:Rules(D(10),D(10),D(5),D('.0000001')) for v in RULES}
    s=FirstLevelPaper(tmp_path/'exact-fees.sqlite',Policy(
        bybit_fee_bps=D(3),bitget_fee_bps=D(3),price_guard_bps=D(0)),
        symbol='BTWUSDT',depth_sizing=DepthSizingConfig())
    s.tick_offline(pair(),rules,1000)
    d=D(delta)
    s.tick_offline(exit_books(1100,1+d,D('1.003')-d),rules,1100)
    closed=rows(s,'cycles')
    assert len(closed)==int(reason is not None)
    if reason:
        assert closed[0]['exit_reason']==reason
        if delta in ('.0007009','.0005009'):
            assert D(closed[0]['net_before_funding'])==D('.004')*(1 if reason=='take_profit' else -1)
        assert D(closed[0]['total_fees'])==D('.024036')
    else:
        assert s.state['orders']==2 and s.state['cycle']['pending_exit_reason']==''
    s.close()


def test_partial_threshold_scales_and_stop_reason_stays_after_recovery(tmp_path):
    s=session(tmp_path);s.tick_offline(pair(),RULES,1000)
    s.tick_offline(exit_books(1100,'.99990','1.00310',top_qty=40),RULES,1100)
    c=s.state['cycle']
    assert D(c['qty'])==10 and c['pending_exit_reason']=='stop_loss'
    # Trigger helper is independently meaningful for remaining risk, even though
    # normal pending exits preserve their first reason instead of re-evaluating.
    improved=exit_books(1200,'1.00009','1.00290',top_qty=40)
    s._observe_depth(improved)
    assert s._risk_exit_reason(improved,RULES,c,1200)==''
    assert D(c['exit_evaluation']['risk_basis_notional'])==10
    assert D(c['exit_evaluation']['take_profit_threshold_usdt'])==D('.002')
    s.tick_offline(improved,RULES,1200)
    assert s.state['cycle'] is None and s.state['orders']==6
    c=rows(s,'cycles')[0]
    assert c['exit_reason']=='stop_loss' and c['exit_parts']==2
    assert sum(D(x)-1000 for x in s.state['cash'].values())==D(c['net_before_funding'])
    s.close()


@pytest.mark.parametrize('force,elapsed,expected',[
    ('funding_guard:test',100,'funding_guard:test'),
    ('scheduled_day_end',100,'scheduled_day_end'),
    (None,60000,'holding_timeout'),
])
def test_existing_exit_priority_preserved(tmp_path,force,elapsed,expected):
    s=session(tmp_path);s.tick_offline(pair(),RULES,1000)
    t=1000+elapsed
    s.tick_offline(exit_books(t,'.99990','1.00310'),RULES,t,force)
    assert rows(s,'cycles')[0]['exit_reason']==expected
    s.close()


@pytest.mark.parametrize('field',['take_profit_bps','stop_loss_bps'])
@pytest.mark.parametrize('value',[D('-1'),D('NaN'),D('Infinity'),D('0')])
def test_invalid_risk_rates_fail_closed(field,value):
    with pytest.raises(ValueError):Policy(**{field:value})

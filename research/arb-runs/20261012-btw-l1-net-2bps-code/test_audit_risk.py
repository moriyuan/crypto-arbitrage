"""Synthetic independent audit contracts; no imports of execution code."""
from copy import deepcopy
from decimal import Decimal as D

from audit_risk import audit_records
from audit_accounting import EXPECTED_POLICY, l1_policy_errors


def fixture(reverse=False, reason='take_profit', case=None):
    long, short = ('bitget', 'bybit') if reverse else ('bybit', 'bitget')
    meta = {'symbol':'BTWUSDT', 'requested_day_utc':'2026-10-01', 'execution_enabled':False,
            'prediction_model_used':False, 'policy':dict(EXPECTED_POLICY),
            'risk_basis':'remaining_long_entry_notional',
            'risk_pnl_basis':{k:'remaining_pair_net_before_funding' for k in ('take_profit','stop_loss')},
            'initial_balance_per_venue':'1000', 'depth_sizing':{'participation':'0.25'},
            'execution_book_levels':1}
    state = {'policy':deepcopy(meta['policy']), 'risk_basis':meta['risk_basis'],
             'risk_pnl_basis':deepcopy(meta['risk_pnl_basis']), 'cycle':None}
    en = {long:D(1), short:D('1.003')}
    # Constant exit-price sum makes fees exactly .024036 for q=20. These
    # arithmetic fixtures isolate inclusive net boundaries, not venue tick rules.
    quote_cases = {
        'take_profit': ('1.0007009', '1.0022991'),  # gross .028036, net +.004
        'stop_loss': ('1.0005009', '1.0024991'),    # gross .020036, net -.004
        'fee_only': ('1', '1.003'),               # zero gross, net -.024036
        'gross_only_profit': ('1.0001', '1.0029'), # gross .004, net -.020036
    }
    a,b=quote_cases[case or reason]
    out = {long:D(a), short:D(b)}
    q = D(20)
    gross = q*((out[long]-en[long])+(en[short]-out[short]))
    fees = sum((q*(en[v]+out[v])*D('.0003') for v in en),D(0))
    ev = {'timestamp_ms':2000,'position_qty':'20','executable_qty':'10',
          'prices':{v:str(out[v]) for v in en}, 'estimated_position_gross_pnl':str(gross),
          'estimated_position_fees':str(fees),'estimated_position_pnl':str(gross-fees),
          'executable_fragment_net_pnl':str((gross-fees)/2),
          'mode':'partial_depth_position_pnl_proxy','risk_basis':meta['risk_basis'],
          'risk_basis_notional':'20','take_profit_bps':'2','stop_loss_bps':'2',
          'take_profit_threshold_usdt':'.004','stop_loss_threshold_usdt':'-.004'}
    orders = []
    for ns,intent,qty in [(10**9,'entry',20),(2*10**9,'exit',10),(3*10**9,'exit',10)]:
        for venue in ('bybit','bitget'):
            price = en[venue] if intent=='entry' else out[venue]
            side = 'buy' if (venue==long)==(intent=='entry') else 'sell'
            orders.append({'order_id':str(len(orders)), 'cycle_id':'c1','venue':venue,
                           'side':side,'position_side':'long' if venue==long else 'short',
                           'intent':intent,'qty':str(qty),'price':str(price),
                           'fee_rate':'.0003','fee':str(qty*price*D('.0003')),
                           'decision_cursor_ns':ns})
    cycles = [{'cycle_id':'c1','exit_reason':reason,'exit_parts':2}]
    events = [
        {'kind':'exit_triggered','cycle_id':'c1','decision_cursor_ns':2*10**9,
         'reason':reason,'evaluation':ev},
        {'kind':'pair_partially_closed','cycle_id':'c1','decision_cursor_ns':2*10**9,
         'reason':reason,'closed_qty':'10','remaining_qty':'10'},
        {'kind':'pair_closed','decision_cursor_ns':3*10**9,
         'result':{'cycle_id':'c1','exit_reason':reason}},
    ]
    return meta,state,orders,cycles,events


def test_valid_inclusive_net_profit_and_reverse_long():
    for reverse in (False,True):
        result = audit_records(*fixture(reverse))
        assert result['status']=='PASS', result
        trigger=result['triggers'][0]
        assert D(trigger['recomputed_net_pnl'])==D('.004')
        assert D(trigger['recomputed_gross_pnl'])-D(trigger['recomputed_fees'])==D('.004')


def test_valid_inclusive_net_stop():
    for reverse in (False,True):
        result=audit_records(*fixture(reverse,reason='stop_loss'))
        assert result['status']=='PASS'
        assert D(result['triggers'][0]['recomputed_net_pnl'])==D('-.004')


def test_config_refuses_gross_trigger_and_obsolete_usdt():
    data = fixture()
    data[0]['policy']['stop_loss_usdt']='5'
    data[0]['risk_pnl_basis']['take_profit']='remaining_pair_gross_price_pnl'
    assert audit_records(*data)['status']=='FAIL'
    assert set(l1_policy_errors(data[0])) >= {'obsolete_policy:stop_loss_usdt','risk_pnl_basis'}


def test_wrong_basis_and_fee_arithmetic_fail():
    for field,value in [('risk_basis_notional','40'),('estimated_position_fees','0'),
                        ('estimated_position_pnl','0.005'),('position_qty','10'),
                        ('take_profit_threshold_usdt','.008')]:
        data=fixture();data[4][0]['evaluation'][field]=value
        assert audit_records(*data)['status']=='FAIL', field


def test_changed_sticky_reason_or_missing_duplicate_trigger_fail():
    data=fixture();data[4][1]['reason']='stop_loss'
    assert audit_records(*data)['status']=='FAIL'
    data=fixture();del data[4][0]
    assert audit_records(*data)['status']=='FAIL'
    data=fixture();data[4].insert(1,deepcopy(data[4][0]))
    assert audit_records(*data)['status']=='FAIL'


def test_fee_only_loss_justifies_net_stop():
    result=audit_records(*fixture(reason='stop_loss',case='fee_only'))
    assert result['status']=='PASS'
    trigger=result['triggers'][0]
    assert D(trigger['recomputed_gross_pnl'])==0
    assert D(trigger['recomputed_net_pnl'])<D('-.004')


def test_gross_profit_does_not_justify_net_take_profit():
    data=fixture(case='gross_only_profit')
    assert D(data[4][0]['evaluation']['estimated_position_gross_pnl'])>=D('.004')
    assert D(data[4][0]['evaluation']['estimated_position_pnl'])<D('-.004')
    result=audit_records(*data)
    assert result['status']=='FAIL'
    # The exact same prices/fees instead justify a stop-loss.
    assert audit_records(*fixture(reason='stop_loss',case='gross_only_profit'))['status']=='PASS'


def test_trigger_price_must_match_same_cursor_fill():
    data=fixture();data[2][2]['price']='1.5'
    assert audit_records(*data)['status']=='FAIL'

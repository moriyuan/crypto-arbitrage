from decimal import Decimal as D
from missed_opportunities import Shadow, summarize

def shadow():
    c={'cycle':'1','entry_ns':'1000000000','entry_timestamp_ms':'1000','qty':'10',
       'long_venue':'bybit','first_trigger_ns':'1100000000','exit_ns':'1200000000',
       'net_before_funding':'-.02'}
    prices=[100000,99999,100301,100300]
    snap={'entry_prices':{'bybit':100000,'bitget':100300},'sides':[
        [{'price_units_1e8':p*1000,'displayed_qty':40,'remaining_quarter':0 if i in (0,3) else 40}]
        for i,p in enumerate(prices)]}
    return Shadow(c,snap)

def profitable_update(qty=40):
    prices=[100081,100080,100220,100219]
    old=[100000,99999,100301,100300]
    updates=[]
    for i,(p,o) in enumerate(zip(prices,old)):
        updates.extend([(i,o*1000,0),(i,p*1000,qty)])
    return prices,[qty]*4,updates

def test_price_hit_is_not_full_execution_when_depth_insufficient():
    s=shadow();p,q,u=profitable_update(4)
    s.observe(1300000000,True,0,p,q,u)
    for w in s.windows.values():
        assert w['quote_first_hit'] is not None
        assert w['fragment_first_hit'] is None and w['full_first_hit'] is None

def test_unchanged_consumed_budget_cannot_reappear_and_full_recovery_is_counted():
    s=shadow();p,q,u=profitable_update(40)
    s.observe(1300000000,True,0,p,q,u)
    assert s.windows['after_final_exit']['full_first_hit']['seconds_after_window_start']==D('.1')
    s.budget[1][p[1]*1000][1]=0
    s.budget[2][p[2]*1000][1]=0
    s.observe(1400000000,True,0,p,q,[])
    assert s.windows['after_final_exit']['full_observations']==1

def test_force_and_deadline_end_before_price_can_be_counted():
    for ns,force in [(1300000000,1),(61000000000,0)]:
        s=shadow();p,q,u=profitable_update()
        s.observe(ns,True,force,p,q,u)
        assert s.ended and s.windows['after_trigger']['quote_first_hit'] is None

def test_invalid_quotes_are_unknown_not_a_nonprofitable_observation():
    s=shadow();s.observe(1300000000,False,0,[0]*4,[0]*4,[])
    assert s.windows['after_final_exit']['max_net_bps'] is None
    a=summarize([s.result()])['after_final_exit']
    assert a['without_future_valid_observations']==1 and a['with_any_invalid_decisions']==1

def test_partial_exit_must_not_leave_residual_below_minimum_notional():
    s=shadow();s.qty=30
    s.entry_prices=[40000,40300]
    p=[40081,40080,40220,40219]
    u=[]
    for i,side in enumerate(s.budget):
        u.extend((i,old,0) for old in side)
        u.append((i,p[i]*1000,80))
    s.observe(1300000000,True,0,p,[80]*4,u)
    w=s.windows['after_final_exit']
    assert w['quote_first_hit'] is not None
    assert w['fragment_first_hit'] is None and w['full_first_hit'] is None

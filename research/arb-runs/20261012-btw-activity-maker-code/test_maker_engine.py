import pytest
from maker_engine import Shadow, Trade, Tick, NS, summarize


def snapshot():
    p=[100100,100000,100600,100300]
    return {'cycle_id':1,'entry_ns':1*NS,'long':'bybit','qty':100,
      'entry_prices':{'bybit':p[0],'bitget':p[3]},'prices':p,'displayed':[1000]*4,
      'after_entry_consumption':False,'pre_entry_consumption':True,
      'sides':[[{'price_units_1e8':x*1000,'displayed_qty':1000,'remaining_quarter':1000}] for x in p]}


def tick(ns, p=None, valid=True, force=0, updates=()):
    return Tick(ns,valid,force,True,p or [100100,100000,100600,100300],[1000]*4,list(updates))


def trade(ns, side=0, px=100500, qty=80, source=None, venue=1):
    return Trade(ns,ns if source is None else source,venue,side,px*1000,qty*100000000,10,0,0)


def live():
    s=Shadow(snapshot(),50)
    s.book(tick(1100000000),True)
    assert s.state=='LIVE' and s.maker==1 and s.side=='sell' and s.quote==100450
    return s


def test_no_legal_inside_price_and_post_only_rejection():
    a=snapshot();a['prices']=[100001,100000,100301,100300]
    s=Shadow(a,50);assert s.state=='REJECTED' and s.reason=='no_legal_inside_tick'
    s=Shadow(snapshot(),50)
    s.book(tick(1100000000,[100100,100000,100600,100500]),True)
    assert s.state=='REJECTED' and s.reason=='post_only_would_cross'


def test_wrong_side_touch_and_prearrival_source_do_not_fill():
    s=live()
    for tr in [trade(1110000000,side=1),trade(1110000000,px=100450),trade(1110000000,source=1099000000)]:s.trade(tr)
    assert s.maker_qty==0
    s.trade(trade(1120000000));assert s.maker_qty==20


def test_cancel_race_fill_and_minimum_hedge_latency():
    s=live();s.trade(trade(1110000000))
    s.trade(trade(1209999999,qty=40));assert s.maker_qty==30
    s.book(tick(1209999999),True);assert s.hedged_qty==0
    s.trade(trade(1210000000,qty=40));assert s.maker_qty==30
    s.book(tick(1210000000),True);assert s.hedged_qty==20
    s.book(tick(1310000000),True);assert s.hedged_qty==30
    assert s.unhedged_qty==0 and s.state=='DONE'
    assert sum(e['fee_nano'] for e in s.events if e['type']=='maker_fill')==3*100450*30
    assert sum(e['fee_nano'] for e in s.events if e['type']=='hedge_fill')==3*100100*30


def test_dust_is_preserved_and_times_out_without_fake_hedge():
    s=live();s.trade(trade(1110000000,qty=12))
    assert s.maker_qty==3
    s.book(tick(1210000000),True);assert s.hedged_qty==0
    s.advance(6110000000)
    assert s.state=='UNRESOLVED' and s.unhedged_qty==3
    assert not any(e['type']=='hedge_fill' for e in s.events)


def test_depth_unavailable_waits_and_unchanged_budget_does_not_refill():
    s=live();s.trade(trade(1110000000))
    s.budget[0][100100000][1]=0
    s.book(tick(1210000000),True);assert s.hedged_qty==0
    s.book(tick(1220000000),True);assert s.hedged_qty==0
    t=tick(1230000000,updates=[(0,100100000,1080)]);t.display[0]=1080
    s.book(t,True)
    assert s.hedged_qty==20 and s.state=='DONE'


def test_inactive_requests_cancel_but_keeps_race_window_and_hedges():
    s=live();s.book(tick(1120000000),False)
    s.trade(trade(1190000000));assert s.maker_qty==20
    s.trade(trade(1220000000));assert s.maker_qty==20
    s.book(tick(1290000000),False);assert s.hedged_qty==20
    assert s.cancel_reason=='inactive'


def test_deadline_cancel_and_invalid_arrival_are_explicit():
    s=live();s.advance(3100000000)
    assert s.cancel_due==3200000000
    s.advance(3200000000);assert s.state=='DONE' and s.maker_qty==0
    s=Shadow(snapshot(),50);s.book(tick(1100000000,valid=False),True)
    assert s.state=='REJECTED' and s.reason=='unknown_book_at_arrival'


def test_cohort_statistics_do_not_claim_portfolio_pnl():
    s=live();s.trade(trade(1110000000));s.book(tick(1210000000),True)
    r=summarize([s.result()])
    assert r['execution_enabled'] is False and r['portfolio_pnl_computed'] is False
    assert r['quotes_with_maker_fill']==1 and r['quotes_fully_hedged']==1


def test_two_tick_spread_has_a_legal_inside_price_for_all_fractions():
    a=snapshot();a['prices']=[100002,100000,100302,100300]
    for fraction in (25,50,75):
        s=Shadow(a,fraction)
        assert s.state=='PENDING' and s.quote==100001


def test_inaccessible_trade_liquidity_is_not_counted():
    s=live()
    for flag in (1,2,3):
        tr=trade(1110000000);tr.flags=flag;s.trade(tr)
    assert s.maker_qty==0 and s.counters['excluded_rpi_or_block_trade']==3


def test_maker_native_lot_bybit_preserves_participation_limit():
    a=snapshot();a['prices']=[100300,100000,100600,100500]
    s=Shadow(a,50);s.book(tick(1100000000,a['prices']),True)
    assert s.maker==0 and s.side=='buy' and s.quote==100150
    s.trade(trade(1110000000,venue=0,side=1,px=100100,qty=39))
    assert s.maker_qty==0
    s.trade(trade(1120000000,venue=0,side=1,px=100100,qty=40))
    assert s.maker_qty==10


def test_gate_threshold_boundaries():
    from maker_engine import allowed
    v=[10,10,100,100,5*NS]
    assert allowed([0,10,10,100,100,5*NS,0,3],v)
    assert not allowed([0,10,10,100,100,5*NS+1,0,3],v)
    assert not allowed([0,10,10,100,100,0,0,1],v)
    assert not allowed([0,10,10,100,100,-1,0,3],v)
    assert allowed([0,0,0,0,0,-1,-1,0],None)


@pytest.mark.parametrize('late',[False,True])
def test_cli_merges_tapes_and_writes_auditable_partial_execution(tmp_path,late):
    import json, subprocess, sys
    from pathlib import Path
    from maker_engine import HEADER, UPDATE, TRADE, ACTIVITY
    books=tmp_path/'books.bin';activity=tmp_path/'activity.bin';trades=tmp_path/'trades.bin'
    with books.open('wb') as b,activity.open('wb') as a:
        b.write(b'BTWTP002');a.write(b'BTWACT01')
        for ns in (NS,1100000000,1300000000,1400000000,1500000000,2300000000):
            p=snapshot()['prices'];u=[(side,price*1000,1000) for side,price in enumerate(p)] if ns==NS else []
            b.write(HEADER.pack(ns,1,0,1,0,len(u),*p,*([1000]*4)))
            for x in u:b.write(UPDATE.pack(*x))
            a.write(ACTIVITY.pack(ns,10,10,100,100,0,0,3))
    tr=trade(1110000000)
    encoded=TRADE.pack(tr.ns,tr.source_ns,tr.venue,tr.aggressor,tr.price8,tr.qty8,tr.sequence,tr.ordinal,tr.flags)
    if late:
        tr2=trade(1350000000,source=1150000000,qty=40);tr2.sequence=11
        encoded+=TRADE.pack(tr2.ns,tr2.source_ns,tr2.venue,tr2.aggressor,tr2.price8,tr2.qty8,tr2.sequence,tr2.ordinal,tr2.flags)
    trades.write_bytes(b'BTWTRD01'+encoded)
    snaps=tmp_path/'snapshots.jsonl';snaps.write_text(json.dumps(snapshot())+'\n')
    thresholds=tmp_path/'thresholds.csv';thresholds.write_text('name,countA,countB,turnoverAunits,turnoverBunits,maxAgeNs\nq10,10,10,100,100,5000000000\nq25,10,10,100,100,5000000000\n')
    out=tmp_path/'out'
    cmd=[sys.executable,str(Path(__file__).with_name('maker_engine.py')),'--tape',str(books),'--activity',str(activity),'--trades',str(trades),'--entry-snapshots',str(snaps),'--thresholds',str(thresholds),'--gate','q10','--output',str(out)]
    r=subprocess.run(cmd,text=True,capture_output=True);assert r.returncode==0,r.stderr
    report=json.loads((out/'summary.json').read_text())
    assert report['frozen_cohort_size']==1 and report['source_live_trades']==(2 if late else 1)
    assert report['scenarios']['50']['quotes_fully_hedged']==1
    events=[json.loads(l) for l in (out/'events.jsonl').read_text().splitlines()]
    half=[e for e in events if e['alpha_percent']==50]
    fill=next(e for e in half if e['type']=='maker_fill');hedge=next(e for e in half if e['type']=='hedge_fill')
    assert fill['qty']==hedge['qty']==20 and hedge['ns']-fill['ns']>=100000000
    assert sum(e['qty'] for e in half if e['type']=='hedge_fill')==(30 if late else 20)
    if late:assert any(e['type']=='late_fill_notification_reopens_inventory' for e in half)
    assert subprocess.run(cmd,capture_output=True).returncode!=0


def test_delayed_fill_notification_after_cancel_is_not_discarded():
    s=live();s.book(tick(1120000000),False);s.advance(1220000000)
    assert s.state=='DONE'
    s.trade(trade(1260000000,source=1190000000))
    assert s.maker_qty==20 and s.state=='LIVE' and s.unhedged_qty==20
    s.book(tick(1359999999),False);assert s.hedged_qty==0
    s.book(tick(1360000000),False);assert s.hedged_qty==20


def test_late_notification_after_already_fully_hedged_reopens_inventory():
    s=live();s.trade(trade(1110000000));s.book(tick(1210000000),True)
    assert s.state=='DONE' and s.hedged_qty==20
    s.trade(trade(1300000000,source=1200000000,qty=40))
    assert s.maker_qty==30 and s.unhedged_qty==10 and s.state=='LIVE'
    s.book(tick(1400000000),True)
    assert s.hedged_qty==30 and s.state=='DONE'
    r=s.result()
    assert r['maker_price_saving_on_hedged_qty']=='0.045'
    assert r['hedge_slippage_on_hedged_qty']=='0'

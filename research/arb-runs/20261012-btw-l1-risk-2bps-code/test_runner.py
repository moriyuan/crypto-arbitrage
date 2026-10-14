from decimal import Decimal as D
import gzip,json
from pathlib import Path
import pytest
from risk_policy import Policy
from btc_cta.cross_arb.market import Book, Rules
from paper_snapshot import PaperSession
from btc_cta.cross_arb.depth_sizing import DepthSizingConfig
from btc_cta.cross_arb.stream import _BookParser
from runner import FastBookState, BatchPaper, FrozenHistory, HistoricalFunding, precise_fresh

BASE=Path('/Users/mori/code/crypto/state/arb-runs')
def test_frozen_history_boundary_and_hash(tmp_path):
    gate=FrozenHistory(BASE/'20261009-btw-oct1-history')
    assert not gate.evaluate('BTWUSDT',1790812859999)['history_filter_passed']
    assert gate.evaluate('BTWUSDT',1790812860000)['history_rank']==3
    assert not gate.evaluate('BTWUSDT',1790899200000)['history_filter_passed']
    assert not gate.evaluate('OTHERUSDT',1790812860000)['history_filter_passed']

@pytest.mark.parametrize('venue,archive',[('bybit','bybit'),('bitget','bitget-futures')])
def test_fast_book_matches_existing_parser_each_native_update(venue,archive):
    path=BASE/'20261009-btw-tick-history/tardis-probe'/f'{archive}-raw-2026-10-01-minute0.response'
    fast=FastBookState(venue);reference=_BookParser(venue,'BTWUSDT')
    count=0
    for line in gzip.decompress(path.read_bytes()).decode().splitlines():
        if not line:continue
        ts,s=line.split(' ',1);payload=json.loads(s)
        ref=reference.parse(payload,1790812860000)
        if ref is None:continue
        actual=fast.apply(payload,1790812860000000000)
        assert actual==ref
        count+=1
    assert count>700

def test_precise_fresh_does_not_round_away_age_or_future():
    policy=Policy();now=10_000_000_000
    books={v:Book(v,((D(99),D(10)),),((D(100),D(10)),),10000,10000,0) for v in ('bybit','bitget')}
    received={v:now for v in books}
    assert precise_fresh(books,received,policy,now)
    received['bybit']=now+100
    assert not precise_fresh(books,received,policy,now)
    received['bybit']=now-2_000_000_001
    assert not precise_fresh(books,received,policy,now)

def test_batch_ledger_matches_existing_execution_and_partial_exit(tmp_path):
    policy=Policy(bybit_fee_bps=D(3),bitget_fee_bps=D(3),max_holding_s=2)
    rules={v:Rules(D(1),D(1),D(1),D('.01')) for v in ('bybit','bitget')}
    sessions=[PaperSession(tmp_path/'original.sqlite',policy,depth_sizing=DepthSizingConfig()),
              BatchPaper(tmp_path/'batch.sqlite',policy,depth_sizing=DepthSizingConfig(),checkpoint_every=2)]
    for t,quotes,qty in [(1000,[(99,100),(103,104)],D(20)),
                         (3100,[(102,103),(101,102)],D(4)),
                         (4100,[(102,103),(101,102)],D(20))]:
        books={v:Book(v,((D(b),qty),),((D(a),qty),),t,t,0) for v,(b,a) in zip(rules,quotes)}
        for s in sessions:
            if isinstance(s,BatchPaper):s.tick_offline(books,rules,t)
            else:s.tick(books,rules,t)
    sessions[1].checkpoint()
    for table in ('orders','cycles'):
        normalized=[]
        for s in sessions:
            rows=[json.loads(x[0]) for x in s.db.execute(f'SELECT value FROM {table} ORDER BY id')]
            normalized.append([{k:v for k,v in r.items() if not k.endswith('_id')} for r in rows])
        assert normalized[0]==normalized[1]
    assert sessions[0].state['cash']==sessions[1].state['cash']
    assert sessions[0].state['cycle'] is None
    assert sessions[0].state['orders']>4
    for s in sessions:s.close()

def test_batch_failure_rolls_back_to_last_checkpoint(tmp_path):
    s=BatchPaper(tmp_path/'a.sqlite',Policy(),checkpoint_every=10)
    s.tick_offline(None,{},1000)
    def fail(*args):raise ValueError('fault')
    s._tick=fail
    with pytest.raises(ValueError,match='fault'):s.tick_offline(None,{},2000)
    assert s.state['ticks']==0
    assert s.db.execute('SELECT count(*) FROM signals').fetchone()[0]==0
    s.close()

def test_funding_schedule_is_unknown_before_published_and_expires():
    guard=HistoricalFunding(BASE/'20261009-btw-oct1-funding')
    assert 'not_yet_observed' in guard.decision(1790812800000000000,60)
    assert guard.decision(1790812804000000000,60) is None
    assert 'approaching' in guard.decision(1790827080000000000,60)
    assert 'expired' in guard.decision(1790827200000000000,60)
    assert guard.decision(1790827204000000000,60) is None
    with pytest.raises(ValueError,match='clock regression'):guard.decision(1790812804000000000,60)

def test_external_failure_rolls_back_uncommitted_real_orders(tmp_path):
    s=BatchPaper(tmp_path/'external-fault.sqlite',Policy(bybit_fee_bps=D(3),bitget_fee_bps=D(3)),checkpoint_every=10)
    rules={v:Rules(D(1),D(1),D(1),D('.01')) for v in ('bybit','bitget')}
    books={v:Book(v,((D(b),D(50)),),((D(a),D(50)),),1000,1000,0)
           for v,(b,a) in zip(rules,[(99,100),(103,104)])}
    s.tick_offline(books,rules,1000)
    assert s.state['orders']==2 and D(s.state['cash']['bybit'])<1000
    # Model a gzip, native parser or funding exception outside tick_offline.
    s.rollback_to_checkpoint()
    assert s.state['orders']==0 and s.state['cash']=={'bybit':'1000','bitget':'1000'}
    assert s.db.execute('SELECT count(*) FROM orders').fetchone()[0]==0
    saved=json.loads(s.db.execute('SELECT value FROM latest_state WHERE id=1').fetchone()[0])
    assert saved==json.loads(json.dumps(s.state,default=str))
    s.close()

def test_funding_audit_counts_equality_and_partial_closing(tmp_path):
    guard=HistoricalFunding(BASE/'20261009-btw-oct1-funding')
    s=PaperSession(tmp_path/'fund.sqlite',Policy())
    settlement=1790827200000000000
    for venue in ('bybit','bitget'):
        s._append('orders',{'cycle_id':'x','venue':venue,'intent':'entry','qty':'10',
                  'order_id':'open-'+venue,'timestamp_ms':1790827199000,'decision_cursor_ns':settlement-1_000_000_000})
        s._append('orders',{'cycle_id':'x','venue':venue,'intent':'exit','qty':'5',
                  'order_id':'partial-'+venue,'timestamp_ms':1790827199999,'decision_cursor_ns':settlement-1})
        s._append('orders',{'cycle_id':'x','venue':venue,'intent':'exit','qty':'5',
                  'order_id':'close-'+venue,'timestamp_ms':1790827200000,'decision_cursor_ns':settlement})
    result=guard.audit_positions(s)
    assert result['status']=='UNVERIFIED_SETTLEMENT_EXPOSURE'
    assert len(result['crossings'])==2 and not result['unresolved']
    assert result['funding_usdt'] is None
    s.close()

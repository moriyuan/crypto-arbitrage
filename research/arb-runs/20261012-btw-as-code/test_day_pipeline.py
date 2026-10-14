from collections import Counter
from decimal import Decimal as D
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace as N
import pytest
from day_inputs import Day, DailyHistory, validate_stream_counts
from build_day_tape import GridSampler, grid_row


def policy():return N(max_quote_age_ms=2000,max_quote_skew_ms=500,max_request_ms=1000)
def book(ns, mid='1'):
    return N(asks=((D(mid)+D('.00001'),D(100)),),bids=((D(mid)-D('.00001'),D(100)),),exchange_ts_ms=ns//1000000,received_at_ms=ns//1000000,request_ms=0)

def history(tmp_path, day='2026-07-01', admitted=True):
    d=Day(day);start=d.start_ns//1000000
    def row(end,passed):
        return dict(symbol='BTWUSDT',history_filter_passed=passed,history_filter_reason='history_filter_passed' if passed else 'below_top_n',history_rank=3 if passed else 11,history_window_start_ms=end-20*86400000,history_window_end_ms=end)
    data={'schema_version':1,'execution_enabled':False,'config':{'window_days':20,'min_volatility_bps':'0','min_turnover_usdt':'0','top_n':10,'publication_delay_ms':60000},'available_from_ms':start+60000,'valid_until_exclusive_ms':start+86400000,'first_60_seconds':row(start-86400000,False),'btw':row(start,admitted),'pool_count':420,'caveats':['current-directory survivor bias']}
    def write():
        p=tmp_path/'gate-input.json';p.write_text(json.dumps(data));(tmp_path/'independent-recalculation.json').write_text(json.dumps({'status':'PASS','output_hashes':{'gate-input.json':hashlib.sha256(p.read_bytes()).hexdigest()}}))
    write();return d,data,write


def test_day_calendar_exact_and_no_october_leakage():
    d=Day('2026-07-01');assert d.end_ns-d.start_ns==86400*10**9
    assert d.end_ns==Day('2026-07-02').start_ns
    assert d.force_day_end(d.end_ns-120*10**9) and not d.force_day_end(d.end_ns-120*10**9-1)
    for v in ('20260701','2026-02-30','2026-7-1'):
        with pytest.raises(ValueError):Day(v)


def test_history_transition_and_non_admission_remains_false(tmp_path):
    d,_,_=history(tmp_path,admitted=False);g=DailyHistory(tmp_path,d)
    for t in (d.start_ns//10**6,d.start_ns//10**6+59999,d.start_ns//10**6+60000,d.end_ns//10**6-1):
        assert not g.evaluate('BTWUSDT',t)['history_filter_passed']
    assert g.evaluate('BTWUSDT',d.start_ns//10**6+60000)['history_rank']==11
    assert g.evaluate('BTWUSDT',d.end_ns//10**6)['history_filter_reason']=='outside_verified_history_scope'


def test_history_complete_daily_window_transition(tmp_path):
    d,data,write=history(tmp_path);g=DailyHistory(tmp_path,d)
    assert not g.evaluate('BTWUSDT',d.start_ns//10**6+59999)['history_filter_passed']
    assert g.evaluate('BTWUSDT',d.start_ns//10**6+60000)['history_filter_passed']
    data['btw']['history_window_end_ms']+=86400000;write()
    with pytest.raises(ValueError,match='window'):DailyHistory(tmp_path,d)


def test_history_cannot_reuse_other_day_or_bad_hash(tmp_path):
    d,_,_=history(tmp_path)
    with pytest.raises(ValueError,match='date'):DailyHistory(tmp_path,Day('2026-08-01'))
    (tmp_path/'gate-input.json').write_text('{}')
    with pytest.raises(ValueError,match='hash'):DailyHistory(tmp_path,d)


def test_dynamic_raw_counts_replace_oct1_magic_constants(tmp_path):
    audit={'venues':[{'venue':'bybit','counts':{'lines':7,'book_snapshot':1,'book_delta':2,'trade_messages':2,'live_trade_records':5,'empty_lines':2}}, {'venue':'bitget-futures','counts':{'lines':6,'book_snapshot':1,'book_update':1,'trade_messages':2,'historical_trade_snapshot_messages':1,'live_trade_records':3,'historical_snapshot_trade_records':50,'timestamp_only_disconnect_markers':1,'subscribe_ack':1}}]}
    p=tmp_path/'audit.json';p.write_text(json.dumps(audit));archive=N(root=tmp_path,evidence_sha256={'audit.json':hashlib.sha256(p.read_bytes()).hexdigest()})
    c=Counter({'frames:bybit':7,'kind:bybit:book':3,'kind:bybit:trade':2,'kind:bybit:disconnect':2,'live_records:bybit':5,'frames:bitget':6,'kind:bitget:book':2,'kind:bitget:trade':1,'kind:bitget:historical_trade':1,'kind:bitget:disconnect':1,'live_records:bitget':3,'historical_records:bitget':50})
    assert validate_stream_counts(archive,c,'activity')['status']=='PASS'
    c['live_records:bitget']=4
    with pytest.raises(ValueError,match='live_records:bitget'):validate_stream_counts(archive,c,'activity')


def test_grid_no_future_book_and_equal_timestamp_after_full_batch():
    d=Day('2026-07-01');t=d.start_ns;g=GridSampler(d);p=policy();books={};recv={};segments={'bybit':0,'bitget':0}
    g.emit_until(t+100000000,False,books,recv,segments,p)
    assert g.rows==[(t,0,0,0,0,0)]
    books={'bybit':book(t+100000000),'bitget':book(t+100000000)};recv={k:t+100000000 for k in books};segments={k:1 for k in books}
    g.emit_until(t+10**9,False,books,recv,segments,p);assert len(g.rows)==1
    books['bybit']=book(t+10**9,'2');recv['bybit']=t+10**9
    # The other venue's lag exceeds paired skew, so the equal-grid state is invalid.
    g.emit_until(t+10**9,True,books,recv,segments,p);assert g.rows[-1][3]==0


def test_grid_precise_age_boundary_skew_and_disconnect():
    t=Day('2026-07-01').start_ns;books={v:book(t) for v in ('bybit','bitget')};recv={v:t for v in books};seg={v:1 for v in books};p=policy()
    assert grid_row(t+2*10**9,books,recv,seg,p)[1:4]==(200000,200000,3)
    assert grid_row(t+2*10**9+1,books,recv,seg,p)[1:4]==(0,0,0)
    recv['bitget']=t+500000001
    assert grid_row(t+10**9,books,recv,seg,p)[3]==0
    del books['bitget'];del recv['bitget'];seg['bitget']=0
    row=grid_row(t+10**9,books,recv,seg,p)
    assert row[1:]==(200000,0,1,1,0)
    books['bitget']=book(t+10**9);recv['bitget']=t+10**9;seg['bitget']=2
    books['bybit']=book(t+10**9);recv['bybit']=t+10**9
    assert grid_row(t+10**9,books,recv,seg,p)[3:]==(3,1,2)


def test_funding_day_cannot_reuse_october_evidence(tmp_path):
    from day_inputs import validate_funding_day
    d=Day('2026-07-01')
    docs={'acceptance.json':{'status':'PUBLIC_FUNDING_HISTORY_AND_CAUSAL_SCHEDULE_VERIFIED','date':d.text,'symbol':'BTWUSDT'},
          'causal-schedule.json':{'date':d.text,'symbol':'BTWUSDT'},'events.json':{'target_day_utc':d.text,'symbol':'BTWUSDT'},
          'manifest.json':{'window_start_ms':d.start_ns//10**6,'window_end_ms_exclusive':d.end_ns//10**6}}
    for n,x in docs.items():(tmp_path/n).write_text(json.dumps(x))
    validate_funding_day(tmp_path,d)
    with pytest.raises(ValueError,match='date'):validate_funding_day(tmp_path,Day('2026-10-01'))


@pytest.mark.parametrize('tape_magic',(b'BTWTP002',b'BTWTP003'))
def test_activity_dynamic_count_day_boundary_and_receive_visibility(tmp_path,tape_magic):
    import struct
    import numpy as np
    from activity_day_features import ACT_EVENT,ACT_MAGIC,BOOK_HEADER,FEATURE,features
    d=Day('2026-07-01');start=d.start_ns
    source=tmp_path/'activity-events.bin'
    # Third trade is in the future of the first book decision; no early leakage.
    rows=[(start,0,0,0,0),(start,1,0,0,0),(start+10**9,0,2,1,100),
          (start+10**9,1,2,2,200),(start+2*10**9,0,2,4,400)]
    source.write_bytes(ACT_MAGIC+b''.join(ACT_EVENT.pack(*x) for x in rows))
    tape=tmp_path/'tape.bin'
    tape.write_bytes(tape_magic+b''.join(BOOK_HEADER.pack(ns,1,0,1,0,0,*([1]*8)) for ns in (start+10**9,start+2*10**9)))
    result=features(tmp_path,tape,60*10**9,start,lambda *a:None,2)
    assert result['decisions']==2
    data=(tmp_path/'features-decisions.bin').read_bytes()
    first=FEATURE.unpack_from(data,8);second=FEATURE.unpack_from(data,8+FEATURE.size)
    assert first[1:5]==(1,2,100,200) and second[1:5]==(5,2,500,200)
    a=np.load(tmp_path/'features-uniform-seconds.npz')['features']
    assert a.shape==(86400,8) and a[0,0]==start and a[-1,0]==d.end_ns-10**9
    assert tuple(a[1,1:5])==(1,2,100,200)
    assert a[59,7]==0 and a[60,7]==3
    (tmp_path/'features-decisions.bin').unlink()
    with pytest.raises(ValueError,match='count'):features(tmp_path,tape,60*10**9,start,lambda *a:None,3)


def test_grid_complete_day_save_preserves_exact_integer_scale(tmp_path):
    import numpy as np
    d=Day('2026-07-01');g=GridSampler(d);p=policy()
    b={v:book(d.start_ns) for v in ('bybit','bitget')};r={v:d.start_ns for v in b};s={v:1 for v in b}
    g.emit_until(d.end_ns,False,b,r,s,p)
    meta=g.save(tmp_path/'grid.npz');npz=np.load(tmp_path/'grid.npz')
    assert npz['data'].shape==(86400,6)
    assert list(npz['columns'])==['ns','bybit_mid_ticks_x2','bitget_mid_ticks_x2','validmask','segmentA','segmentB']
    assert list(npz['data'][:4,3])==[3,3,3,0]
    assert meta['rows']==86400 and meta['validmask_counts']['3']==3


def test_micro_price_grid_and_no_rounding_of_finer_or_wrong_native_ticks():
    from build_day_tape import ObservedDepth, validate_l1_prices
    t=Day('2026-07-01').start_ns
    a=book(t,'0.05264');b=N(asks=((D('.052730'),D(100)),),bids=((D('.052628'),D(100)),),exchange_ts_ms=t//10**6,received_at_ms=t//10**6,request_ms=0)
    books={'bybit':a,'bitget':b};receive={v:t for v in books};segments={v:1 for v in books}
    assert grid_row(t,books,receive,segments,policy(),6)[1:4]==(105280,105358,3)
    observed=ObservedDepth(6);prices,qty,changes=observed.encode(books)
    assert prices==[52650,52630,52730,52628] and (3,5262800,100) in changes
    with pytest.raises(ValueError):ObservedDepth(5).encode(books)
    finer=N(asks=((D('.0527301'),D(100)),),bids=((D('.0526281'),D(100)),))
    with pytest.raises(ValueError):validate_l1_prices(finer,'bitget',6)
    with pytest.raises(ValueError):validate_l1_prices(b,'bybit',6)

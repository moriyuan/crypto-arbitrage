import numpy as np
from decimal import Decimal as D
from as_replay import quote_for_snapshot,signal_features,ResearchShadow
from execution_adapter import PriceSchema
from test_as_quote import calibration
from maker_engine import Tick
import sys
sys.path.insert(0,'/Users/mori/code/crypto/state/arb-runs/20261012-btw-activity-maker-code')
from test_maker_engine import snapshot


def fit():
    return {'schema_version':1,'symbol':'BTWUSDT','venue':'bitget','quote_unit_coins':'10',
      'trained_from_ns':0,'trained_through_ns':100,'valid_from_ns':101,'valid_until_ns':10**15,
      'accepted':True,'a_per_second':'2','k_per_usdt':'1000','source_sha256':['a'*64],'fit_diagnostics':{}}


def vol(ready=True):return [{'ready':ready,'sigma2_usdt2_per_second':'0.00001','known_through_ns':100}]*2


def test_fixed_quote_prices_preserved_and_native_tick_distance_used():
    a=snapshot();schema=PriceSchema(5,(1,1))
    assert quote_for_snapshot(a,'fixed_ticks_1',schema,vol(),{})[0]==100301
    assert quote_for_snapshot(a,'fixed_ticks_5',schema,vol(),{})[0]==100305
    a['prices']=[1005000,1000000,1006000,1005000]
    assert quote_for_snapshot(a,'fixed_ticks_1',PriceSchema(6,(10,1)),vol(),{})[0]==1004990


def test_unaccepted_or_noncausal_fit_cannot_silently_use_constant_k():
    assert quote_for_snapshot(snapshot(),'as_inside',PriceSchema(5,(1,1)),vol(),{})[1]=='as_calibration_unaccepted'
    f=fit();f['trained_through_ns']=2*10**9;f['valid_from_ns']=2*10**9+1
    assert quote_for_snapshot(snapshot(),'as_inside',PriceSchema(5,(1,1)),vol(),{'bitget':f})[1]=='as_calibration_unavailable'


def test_missing_sigma_rejects_and_raw_outside_quote_is_not_clipped():
    f=fit()
    assert quote_for_snapshot(snapshot(),'as_inside',PriceSchema(5,(1,1)),vol(False),{'bitget':f})[1]=='as_sigma_unavailable'
    f['k_per_usdt']='1'
    q,why,_=quote_for_snapshot(snapshot(),'as_inside',PriceSchema(5,(1,1)),vol(),{'bitget':f})
    assert why=='as_outside_spread' and q>snapshot()['prices'][2]


def test_actual_inventory_starts_zero_and_evidence_records_causal_inputs():
    s=ResearchShadow(snapshot(),'as_inside',PriceSchema(5,(1,1)),vol(),{'bitget':fit()})
    assert s.state=='PENDING'
    q=s.result()['quote_evidence']['as']
    assert D(q['inventory_lots'])==0 and q['calibration_trained_through_ns']==100
    assert q['sigma_known_through_ns']==100


def test_signal_looks_up_only_completed_grid_at_or_before_receipt():
    grid=np.array([[i*10**9,200000+2*i,200000+4*i,3,1,1] for i in range(63)],dtype=np.int64)
    signals={60*10**9+123:None,61*10**9:None}
    f=signal_features(grid,signals,PriceSchema(5,(1,1)))
    assert f[60*10**9+123][0]['known_through_ns']==60*10**9
    assert f[61*10**9][1]['known_through_ns']==61*10**9
    assert D(f[61*10**9][1]['sigma2_usdt2_per_second'])==D('0.00000004')

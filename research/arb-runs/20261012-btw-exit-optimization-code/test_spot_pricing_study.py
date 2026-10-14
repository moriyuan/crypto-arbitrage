import math
import numpy as np
from spot_pricing_study import spot_mid, past_ema_residual, interval_valid, fit, predict, partition, START

def test_spot_freshness_uses_book_not_latest_trade_receive_time():
    row={'valid':'1','local_recv_ns':4_000_000_000,'book_local_recv_ns':1_000_000_000,
         'book_exchange_ns':1_000_000_000,'bid':'1','ask':'1.01','bid_size':'1','ask_size':'1'}
    assert math.isnan(spot_mid(row,4_000_000_000))
    row['book_local_recv_ns']=row['book_exchange_ns']=4_000_000_000
    assert spot_mid(row,4_000_000_000)==1.005
    row['valid']='0';assert math.isnan(spot_mid(row,4_000_000_000))

def test_previous_ema_only_and_long_gap_resets_warmup():
    t=np.arange(1300,dtype=np.int64)*500_000_000;x=np.ones(1300)
    x[1200]=10
    r=past_ema_residual(x,t)
    assert np.isnan(r[1199]) and r[1200]==9
    x[1201:]=1000
    assert past_ema_residual(x,t)[1200]==r[1200]
    x[1000:1200]=np.nan
    assert np.isnan(past_ema_residual(x,t)[1200])

def test_invalid_event_blocks_both_past_feature_and_forward_label():
    mask=np.array([1,1,0,1,1,1],dtype=bool)
    assert interval_valid(mask,2).tolist()==[False,False,False,False,False,True]
    assert interval_valid(mask,2,True).tolist()==[False,False,False,True,False,False]

def test_fit_is_training_only_and_holdout_cannot_change_weights():
    X=np.array([[1.,2],[2,0],[4,1],[3,-1]])
    y=np.array([1.,2,3,4]);m=fit(X,y)
    before=predict(X,m).copy();other=np.full((4,2),1000.)
    predict(other,m)
    assert np.array_equal(before,predict(X,m))

def test_partition_purges_labels_and_feature_intervals_at_boundaries():
    t=START+np.array([12*3600-1,12*3600,12*3600+1,18*3600-5],dtype=np.int64)*10**9
    assert partition(t,np.ones(4,dtype=bool),5,12,18).tolist()==[False,False,True,False]

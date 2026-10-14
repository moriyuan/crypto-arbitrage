from decimal import Decimal as D
import math
from as_calibrate import Window,ReachCalibration,fit_venue,blank_stats,BP_GRID,NS
from maker_engine import Trade,Tick


def tr(receive,source,price=99,side=1,qty=80,venue=0,flags=0):
    return Trade(receive,source,venue,side,price*1000,qty*100000000,1,0,flags)


def test_first_hit_uses_source_and_accepts_late_receipt_before_finalization():
    w=Window(1,0,0,0,100,0,100,200)
    w.hit(tr(250,150));assert w.first_hit_ns==150 and w.exposure()==50
    w.hit(tr(260,140));assert w.first_hit_ns==140
    w.hit(tr(270,200));assert w.first_hit_ns==140


def test_wrong_side_touch_rpi_and_small_participation_not_hits():
    w=Window(1,0,0,0,100,0,100,200)
    for t in (tr(150,149,side=0),tr(150,149,price=100),tr(150,149,flags=1),tr(150,149,qty=39)):
        w.hit(t)
    assert w.first_hit_ns is None and w.exposure()==100


def test_invalid_book_right_censors_exposure_without_counting_full_ttl():
    f=ReachCalibration([D(1),D(1)],100*NS)
    f.anchor([NS,200000,200000,3,1,1])
    prices=[100010,99990,100010,99990]
    f.book(Tick(NS+100000000,True,0,True,prices,[1000]*4,[]))
    f.book(Tick(NS+300000000,False,0,True,[0]*4,[0]*4,[]))
    f.advance(4*NS)
    for venue in (0,1):
        for side in (0,1):
            assert f.stats[venue][side][0]['exposure_ns']==200000000
            assert f.stats[venue][side][0]['hits']==0 and f.stats[venue][side][0]['censored_invalid']==1


def test_rejected_arrival_contributes_no_nohit_exposure():
    f=ReachCalibration([D(1),D(1)],100*NS)
    f.anchor([NS,200000,200000,3,1,1])
    f.book(Tick(NS+100000000,False,0,True,[0]*4,[0]*4,[]));f.finish()
    assert f.stats[0][0][0]['arrival_invalid']==1 and f.stats[0][0][0]['exposure_ns']==0


def fit_data(k=10):
    distances=[D(i)/10 for i in range(7)]
    st=[[blank_stats() for _ in BP_GRID] for _ in (0,1)]
    for side in (0,1):
        for i,d in enumerate(distances):
            st[side][i]['exposure_ns']=100000*NS
            st[side][i]['hits']=round(5*math.exp(-k*float(d))*100000)
    return distances,st


def test_log_intensity_fit_recovers_synthetic_parameters():
    d,s=fit_data();f=fit_venue(d,s)
    assert f['accepted'] and abs(float(f['k_per_usdt'])-10)<.001 and abs(float(f['a_per_second'])-5)<.001


def test_bad_or_insufficient_fit_never_gets_default_parameters():
    d,s=fit_data(k=-1);f=fit_venue(d,s)
    assert not f['accepted'] and f['k_per_usdt'] is None
    f=fit_venue(d,[[blank_stats() for _ in BP_GRID] for _ in (0,1)])
    assert not f['accepted'] and f['a_per_second'] is None

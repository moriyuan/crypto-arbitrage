import numpy as np
from audit_day_activity import TRADE,expected


def test_exact_ns_window_disconnect_and_snapshot_warmup():
    origin=1788220800000000000
    trades=np.zeros(4,dtype=TRADE)
    trades['ns']=[origin+7,origin+10**9,origin+61*10**9,origin+62*10**9]
    trades['venue']=0;trades['price']=100;trades['qty']=10**8;trades['sequence']=[1,2,3,4]
    recoveries=[{'venue':'bybit','receive_ns':origin+7},{'venue':'bybit','receive_ns':origin+62*10**9}]
    gaps=[{'venue':'bybit','ordering_ns':origin+61*10**9,'source_sequence':3}]
    times=np.array([origin+7,origin+60*10**9+7,origin+61*10**9,origin+62*10**9,origin+122*10**9],dtype=np.int64)
    result=expected(times,trades,recoveries,gaps)
    assert np.array_equal(result[:,0],times)
    assert list(result[:,1])==[1,1,0,1,0]
    assert list(result[:,3])==[100,100,0,100,0]
    assert list(result[:,7])==[0,1,0,0,1]
    assert result[2,5]==-1 and result[-1,5]==60*10**9

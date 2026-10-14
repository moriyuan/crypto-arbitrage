"""Independent array-based reconstruction of all AS training reach windows.

This audit does not import or call calibration, quote or execution implementations.
"""
import argparse
from array import array
from collections import Counter
from decimal import Decimal as D, ROUND_FLOOR, ROUND_CEILING
import hashlib
import json
import math
from pathlib import Path
import struct
import time
import numpy as np

NS=10**9; LAT=10**8; TTL=2*NS; GRACE=NS
HEADER=struct.Struct('<qBBBBI8q')
TRADE_DTYPE=np.dtype({'names':['ns','source','venue','side','price','qty','seq','ordinal','flags'],
 'formats':['<i8','<i8','u1','u1','<i8','<i8','<i8','<u4','<u4'],
 'offsets':[0,8,16,17,24,32,40,48,52],'itemsize':64})

def digest(p):
    with open(p,'rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()

def run(path):
    started=time.time();cal=json.loads(Path(path).read_text());end=cal['training_receipt_end_ns_exclusive'];cap=end-GRACE
    pd=cal['price_schema']['price_decimals'];scale=10**(8-pd);tick=D(1)/10**pd
    with np.load(cal['inputs']['grid']['path'],allow_pickle=False) as z:grid=z['data']
    times=array('q');valid=array('b');prices=array('q')
    with open(cal['inputs']['tape']['path'],'rb') as f:
        assert f.read(8)==(b'BTWTP002' if pd==5 else b'BTWTP003')
        while h:=f.read(80):
            ns,v,force,hist,pad,n,*vals=HEADER.unpack(h)
            if ns>=end:break
            f.seek(n*24,1);times.append(ns);valid.append(v);prices.extend(vals[:4])
    ts=np.frombuffer(times,dtype=np.int64);va=np.frombuffer(valid,dtype=np.int8);px=np.frombuffer(prices,dtype=np.int64).reshape(-1,4)
    invalid=ts[va==0]
    tr=np.memmap(cal['inputs']['trades']['path'],mode='r',offset=8,dtype=TRADE_DTYPE)
    training=tr[tr['ns']<end]; streams={}
    for v in (0,1):
        for side in (0,1):
            quantum=10 if v==0 else 1
            streams[v,side]=training[(training['venue']==v)&(training['side']==1-side)&(training['flags']==0)&(training['qty']>=4*10**8*quantum)]
    findings={};window_checks=0
    for v,name in enumerate(('bybit','bitget')):
        observed=cal['venues'][name];step=cal['price_schema']['native_tick_units'][v]
        vals=sorted(int(r[1+v]) for r in grid if int(r[0])<end and int(r[3])&(1<<v))
        median=(D(vals[(len(vals)-1)//2])+D(vals[len(vals)//2]))/2*tick*5 if vals else D(0)
        assert median==D(observed['training_median_mid_usdt_per_lot'])
        distances=[median*bp/10000 for bp in (0,1,2,5,10,20,50)]
        stats=[[Counter() for _ in distances] for _ in (0,1)]
        for row in grid:
            anchor=int(row[0])
            if anchor+LAT>=cap:break
            if not int(row[3])&(1<<v):continue
            i=int(np.searchsorted(ts,anchor+LAT));arrival=int(ts[i]) if i<len(ts) else end
            mid=D(int(row[1+v]))*tick*5
            badidx=int(np.searchsorted(invalid,arrival));bad=int(invalid[badidx]) if badidx<len(invalid) else end
            stop=min(arrival+TTL,cap,bad);censor='invalid' if bad<min(arrival+TTL,cap) else ('train_end' if cap<=arrival+TTL else 'ttl')
            for side in (0,1):
                stream=streams[v,side]
                lo=int(np.searchsorted(stream['ns'],arrival));hi=int(np.searchsorted(stream['ns'],min(stop+GRACE,end)))
                reachable=stream[lo:hi];reachable=reachable[(reachable['source']>=arrival)&(reachable['source']<stop)]
                for bi,distance in enumerate(distances):
                    st=stats[side][bi];st['anchors']+=1
                    if arrival>=cap:st['training_tail_excluded']+=1;continue
                    if not va[i]:st['arrival_invalid']+=1;continue
                    raw=(mid-distance if side==0 else mid+distance)/10/tick/step
                    quote=int(raw.to_integral_value(rounding=ROUND_FLOOR if side==0 else ROUND_CEILING))*step
                    if quote<=0 or (quote>=px[i,2*v] if side==0 else quote<=px[i,2*v+1]):st['post_only_rejected']+=1;continue
                    st['arrived']+=1;window_checks+=1
                    through=reachable[reachable['price']<quote*scale] if side==0 else reachable[reachable['price']>quote*scale]
                    if len(through):effective=int(through['source'].min());st['hits']+=1
                    else:effective=stop;st['censored_'+censor]+=1
                    st['exposure_ns']+=effective-arrival;st['zero_exposure_windows']+=effective==arrival
        for bi,record in enumerate(observed['fit_diagnostics']['bins']):
            assert D(record['distance_usdt_per_lot'])==distances[bi]
            for side,key in enumerate(('buy','sell')):
                for field,expected in record[key].items():
                    assert stats[side][bi][field]==expected,(name,key,bi,field,stats[side][bi][field],expected)
            hits=sum(stats[s][bi]['hits'] for s in (0,1));exposure=sum(stats[s][bi]['exposure_ns'] for s in (0,1))/NS
            assert hits==record['hits'] and exposure==record['exposure_seconds']
        usable=[b for b in observed['fit_diagnostics']['bins'] if b['hits']>=20 and b['exposure_seconds']>=600]
        accepted=False;fit={}
        if len(usable)>=4:
            w=[b['hits'] for b in usable];x=[float(b['distance_usdt_per_lot']) for b in usable];y=[math.log(b['hits']/b['exposure_seconds']) for b in usable]
            sw=sum(w);xm=sum(a*b for a,b in zip(w,x))/sw;ym=sum(a*b for a,b in zip(w,y))/sw
            slope=sum(a*(b-xm)*(c-ym) for a,b,c in zip(w,x,y))/sum(a*(b-xm)**2 for a,b in zip(w,x))
            intercept=ym-slope*xm;k=-slope;A=math.exp(intercept);rmse=math.sqrt(sum(a*(c-intercept-slope*b)**2 for a,b,c in zip(w,x,y))/sw)
            accepted=A>0 and k>0 and math.isfinite(A+k+rmse) and rmse<=1
            if observed['k_per_usdt'] is not None:assert math.isclose(k,float(observed['k_per_usdt']),rel_tol=1e-12,abs_tol=1e-12)
            if observed['a_per_second'] is not None:assert math.isclose(A,float(observed['a_per_second']),rel_tol=1e-12,abs_tol=1e-12)
            assert math.isclose(rmse,observed['fit_diagnostics']['weighted_log_rmse'],rel_tol=1e-12,abs_tol=1e-12)
            fit={'k_per_usdt':k,'a_per_second':A,'weighted_log_rmse':rmse}
        assert accepted==observed['accepted']
        findings[name]={'status':'PASS','usable_bins':len(usable),'accepted':accepted,'recomputed_fit':fit}
    return {'status':'PASS','method':'Independent array/searchsorted reconstruction of every training anchor, source-time first hit, invalid censor and live exposure; no production imports',
      'date_utc':cal['date_utc'],'active_window_checks':window_checks,'training_book_count':len(ts),'training_trade_count':len(training),
      'calibration_sha256':digest(path),'audit_code_sha256':digest(__file__),'venues':findings,'elapsed_seconds':time.time()-started}

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--calibration',required=True);p.add_argument('--output',required=True);args=p.parse_args()
    report=run(args.calibration)
    with open(args.output,'x') as f:json.dump(report,f,indent=2);f.write('\n')
    print(json.dumps(report),flush=True)

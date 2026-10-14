"""Causal public-data spot information study; no simulated orders or profit claims."""
import argparse
import csv
import gzip
import hashlib
import json
import math
from pathlib import Path
import time
import numpy as np
from missed_opportunities import records, sha

START=1790812800000000000
END=START+86400*10**9
STEP=500_000_000
FEATURES=['own_1s_return_bps','other_1s_return_bps','cross_basis_residual_bps',
          'spot_1s_return_bps','spot_own_basis_residual_bps']

def write(path,value):
    with Path(path).open('x') as f:json.dump(value,f,indent=2,ensure_ascii=False,allow_nan=False)

def yes(value):
    if value in (True,1,'1','true','True'):return True
    if value in (False,0,'0','false','False'):return False
    raise ValueError('invalid boolean')

def spot_rows(path):
    last=-1
    with gzip.open(path,'rt',newline='') as f:
        for row in csv.DictReader(f):
            ns=int(row['local_recv_ns'])
            if ns<last:raise ValueError('spot receive clock regression')
            last=ns
            yield ns,row

def spot_mid(row,now):
    if row is None or not yes(row['valid']):return math.nan
    age=now-int(row['book_local_recv_ns'])
    source_age=now-int(row['book_exchange_ns'])
    if not 0<=age<=2_000_000_000 or not -500_000_000<=source_age<=2_000_000_000:return math.nan
    bid,ask=float(row['bid']),float(row['ask'])
    if not 0<bid<ask or min(float(row['bid_size']),float(row['ask_size']))<=0:
        raise ValueError('invalid spot BBO marked valid')
    return (bid+ask)/2

def sample(tape,spot,grid):
    perp=iter(records(tape));p=next(perp,None)
    spots=iter(spot_rows(spot));s=next(spots,None)
    latest_p=latest_s=None
    mids=np.full((len(grid),3),np.nan)
    counts={'perp_events_consumed':0,'spot_events_consumed':0}
    for i,t in enumerate(grid):
        while p is not None and p[0]<=t:
            latest_p=p;counts['perp_events_consumed']+=1;p=next(perp,None)
        while s is not None and s[0]<=t:
            latest_s=s[1];counts['spot_events_consumed']+=1;s=next(spots,None)
        if latest_p is not None and latest_p[1] and 0<=t-latest_p[0]<=STEP:
            prices=latest_p[3]
            mids[i,0]=(prices[0]+prices[1])*0.000005
            mids[i,1]=(prices[2]+prices[3])*0.000005
        mids[i,2]=spot_mid(latest_s,int(t))
    counts.update(samples=len(grid),perp_valid_samples=int(np.isfinite(mids[:,:2]).all(axis=1).sum()),
                  spot_valid_samples=int(np.isfinite(mids[:,2]).sum()),all_valid_samples=int(np.isfinite(mids).all(axis=1).sum()))
    return mids,counts

def past_ema_residual(values,grid):
    out=np.full(len(values),np.nan)
    mean=None;last=warm=None
    for i,(x,t) in enumerate(zip(values,grid)):
        if not np.isfinite(x):continue
        if mean is None or t-last>60*10**9:
            mean=float(x);last=warm=int(t);continue
        if t-warm>=600*10**9:out[i]=x-mean
        decay=math.exp(-math.log(2)*(t-last)/(300*10**9))
        mean=decay*mean+(1-decay)*float(x);last=int(t)
    return out

def interval_valid(mask,lag,future=False):
    n=len(mask);out=np.zeros(n,dtype=bool)
    if lag>=n:return out
    bad=np.concatenate(([0],np.cumsum(~mask)))
    i=np.arange(n-lag)
    if future:out[i]=(bad[i+lag+1]-bad[i])==0
    else:out[i+lag]=(bad[i+lag+1]-bad[i])==0
    return out

def make_data(mids,grid,venue,horizon):
    with np.errstate(invalid='ignore',divide='ignore'):
        log=np.log(mids)
    other=1-venue
    returns=np.full_like(log,np.nan);returns[2:]=(log[2:]-log[:-2])*10000
    past_ok=interval_valid(np.isfinite(mids).all(axis=1),2)
    returns[~past_ok]=np.nan
    cross=past_ema_residual((log[:,other]-log[:,venue])*10000,grid)
    spot_res=past_ema_residual((log[:,2]-log[:,venue])*10000,grid)
    X=np.column_stack((returns[:,venue],returns[:,other],cross,returns[:,2],spot_res))
    k=horizon*2;y=np.full(len(grid),np.nan)
    y[:-k]=(log[k:,venue]-log[:-k,venue])*10000
    y[~interval_valid(np.isfinite(mids[:,:2]).all(axis=1),k,True)]=np.nan
    valid=np.isfinite(X).all(axis=1)&np.isfinite(y)
    return X,y,valid

def fit(X,y):
    mean=X.mean(axis=0);std=X.std(axis=0);std=np.where(std<1e-12,1,std)
    Z=(X-mean)/std;ym=float(y.mean())
    weights=np.linalg.solve(Z.T@Z+np.eye(X.shape[1])*0.001,Z.T@(y-ym))
    return {'mean':mean.tolist(),'std':std.tolist(),'weights':weights.tolist(),'intercept':ym}

def predict(X,m):
    return ((X-np.asarray(m['mean']))/np.asarray(m['std']))@np.asarray(m['weights'])+m['intercept']

def metrics(y,p):
    if not len(y):return {'n':0}
    nonzero=np.abs(y)>1e-12
    corr=float(np.corrcoef(y,p)[0,1]) if len(y)>1 and np.std(y)>1e-12 and np.std(p)>1e-12 else None
    return {'n':len(y),'MAE_bps':float(np.mean(np.abs(p-y))),'RMSE_bps':float(np.sqrt(np.mean((p-y)**2))),
            'prediction_target_correlation':corr,'direction_accuracy_nonzero_targets':float(np.mean(np.sign(y[nonzero])==np.sign(p[nonzero]))) if nonzero.any() else None,
            'fraction_zero_targets':float(np.mean(~nonzero))}

def partition(grid,valid,horizon,start_hour,end_hour):
    # Both the past-return feature interval and forward label stay in the fold.
    return valid&(grid-10**9>=START+start_hour*3600*10**9)&(grid+horizon*10**9<START+end_hour*3600*10**9)

def run(tape,spot,protocol,out):
    started=time.time();out=Path(out)
    if out.exists():raise FileExistsError(out)
    p=json.loads(Path(protocol).read_text())
    if p['sample_ms']!=500 or p['regression'].find('0.001')<0 or p['execution_enabled'] is not False:
        raise ValueError('unexpected protocol')
    out.mkdir()
    grid=np.arange(START,END,STEP,dtype=np.int64)
    mids,coverage=sample(tape,spot,grid)
    np.savez_compressed(out/'aligned-midpoints.npz',local_recv_ns=grid,mid=mids)
    models={};data={}
    for v,name in enumerate(('bybit','bitget')):
        for h in (1,5):
            X,y,valid=make_data(mids,grid,v,h);key=f'{name}_{h}s';data[key]=(X,y,valid,h)
            train=partition(grid,valid,h,0,12)
            if train.sum()<1000:raise ValueError('insufficient shared training samples: '+key)
            models[key]={'training_samples':int(train.sum()),'perpetual_only':fit(X[train,:3],y[train]),'with_spot':fit(X[train],y[train])}
    source={'protocol_sha256':sha(protocol),'spot_bbo_sha256':sha(spot),'perp_tape_sha256':sha(tape),'script_sha256':sha(__file__)}
    # Persist all weights before reading any validation or holdout metric.
    write(out/'models-frozen.json',{'execution_enabled':False,'features':FEATURES,'models':models,'source':source})
    results={}
    for key,(X,y,valid,h) in data.items():
        results[key]={}
        for fold,start,end in (('training',0,12),('validation',12,18),('internal_holdout',18,24)):
            mask=partition(grid,valid,h,start,end);ym=y[mask]
            preds={'no_change':np.zeros(len(ym)),
                   'perpetual_only':predict(X[mask,:3],models[key]['perpetual_only']),
                   'with_spot':predict(X[mask],models[key]['with_spot'])}
            results[key][fold]={model:metrics(ym,pred) for model,pred in preds.items()}
    result={'status':'COMPLETED_PRICING_INFORMATION_STUDY','execution_enabled':False,'coverage':coverage,'results':results,'source':source,'elapsed_seconds':time.time()-started,
            'limitations':p['limitations'],'selection':'Fixed models, horizons and parameters; no holdout-based choice or trading recommendation.'}
    write(out/'evaluation.json',result)
    print(json.dumps(result,ensure_ascii=False),flush=True)

if __name__=='__main__':
    c=argparse.ArgumentParser();c.add_argument('--tape',type=Path,required=True);c.add_argument('--spot',type=Path,required=True);c.add_argument('--protocol',type=Path,required=True);c.add_argument('--output',type=Path,required=True)
    a=c.parse_args();run(a.tape,a.spot,a.protocol,a.output)

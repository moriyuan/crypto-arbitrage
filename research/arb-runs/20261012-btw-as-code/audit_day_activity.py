"""Independent vectorized activity check against live-trade records and gap ledger."""
from pathlib import Path
import argparse
import hashlib
import json
import numpy as np

NS=10**9
TRADE=np.dtype({'names':['ns','source_ns','venue','side','price','qty','sequence','ordinal','flags'],
                'formats':['<i8','<i8','u1','u1','<i8','<i8','<i8','<u4','<u4'],
                'offsets':[0,8,16,17,24,32,40,48,52],'itemsize':64})
FEATURE=np.dtype({'names':['ns','countA','countB','turnoverA','turnoverB','ageA','ageB','mask'],
                  'formats':['<i8','<u4','<u4','<i8','<i8','<i8','<i8','<u8'],
                  'offsets':[0,8,12,16,24,32,40,48],'itemsize':56})

def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for c in iter(lambda:f.read(2**20),b''):h.update(c)
    return h.hexdigest()

def expected(times,trades,recoveries,disconnects):
    """No rolling helper import: direct binary-search windows and cumulative sums."""
    answer=np.zeros((len(times),8),dtype=np.int64);answer[:,0]=times;answer[:,5:7]=-1
    for venue,name in enumerate(('bybit','bitget')):
        rec=np.array([r['receive_ns'] for r in recoveries if r['venue']==name],dtype=np.int64)
        disc=np.array([r['ordering_ns'] for r in disconnects if r['venue']==name],dtype=np.int64)
        if not len(rec):continue
        raw=trades[trades['venue']==venue]
        # Native same-cursor frames preceding a disconnect are in raw tape but
        # unavailable to the feature calculation; compare source sequence too.
        keep=np.ones(len(raw),dtype=bool)
        for d in disconnects:
            if d['venue']==name:keep &= ~((raw['ns']==d['ordering_ns']) & (raw['sequence']<=d['source_sequence']))
        raw=raw[keep]
        assert np.all(raw['qty']%10**8==0), 'This independent exact integer audit needs whole-coin source records'
        values=raw['price']*(raw['qty']//10**8)
        prefix=np.r_[np.int64(0),np.cumsum(values,dtype=np.int64)]
        assert not len(values) or np.all(np.diff(prefix)>0), 'turnover cumulative overflow'
        ir=np.searchsorted(rec,times,side='right')-1
        starts=rec[np.maximum(ir,0)]
        if len(disc):
            di=np.searchsorted(disc,times,side='right')-1
            lastdisc=np.where(di>=0,disc[np.maximum(di,0)],-1)
        else:lastdisc=np.full(len(times),-1,dtype=np.int64)
        connected=(ir>=0)&(starts>=lastdisc)
        end=np.searchsorted(raw['ns'],times,side='right')
        begin=np.maximum(np.searchsorted(raw['ns'],times-60*NS,side='right'),np.searchsorted(raw['ns'],starts,side='left'))
        count=np.maximum(end-begin,0)
        amount=prefix[end]-prefix[np.minimum(begin,end)]
        answer[:,1+venue]=np.where(connected,count,0)
        answer[:,3+venue]=np.where(connected,amount,0)
        if len(raw):
            last=raw['ns'][np.maximum(end-1,0)]
            answer[:,5+venue]=np.where(connected&(end>0)&(last>=starts),times-last,-1)
        answer[:,7] |= np.where(connected&(times-starts>=60*NS),1<<venue,0)
    return answer

def audit(directory):
    p=Path(directory);m=json.loads((p/'metadata.json').read_text());assert m['status']=='COMPLETED'
    for name,info in m['artifacts'].items():assert sha(p/name)==info['sha256']
    assert (p/'live-trades.bin').read_bytes()[:8]==b'BTWTRD01'
    trades=np.memmap(p/'live-trades.bin',dtype=TRADE,mode='r',offset=8)
    raw=m['raw'];uniform=np.load(p/'features-uniform-seconds.npz')['features']
    independent=expected(uniform[:,0],trades,raw['recoveries'],raw['disconnects'])
    assert np.array_equal(uniform,independent), 'uniform-second features differ from independent windows'
    assert (p/'features-decisions.bin').open('rb').read(8)==b'BTWACT01'
    features=np.memmap(p/'features-decisions.bin',dtype=FEATURE,mode='r',offset=8)
    indices=np.unique(np.linspace(0,len(features)-1,min(len(features),2048),dtype=np.int64))
    subset=np.empty((len(indices),8),dtype=np.int64)
    for i,k in enumerate(FEATURE.names):subset[:,i]=features[k][indices]
    independent=expected(subset[:,0],trades,raw['recoveries'],raw['disconnects'])
    assert np.array_equal(subset,independent), 'native-decision activity sample differs from independent windows'
    return {'status':'PASS','date':m['date'],'method':'Independent vectorized direct receive-time windows and cumulative native-turnover sums; no feature implementation import',
            'uniform_seconds_verified':len(uniform),'native_decision_samples_verified':len(indices),'live_trades':len(trades),
            'metadata_sha256':sha(p/'metadata.json'),'audit_source_sha256':sha(__file__),
            'checks':['All artifact hashes','Every uniform-second count/turnover/last-trade age/warm bit','2048 deterministic evenly spaced decision rows','Disconnect source-sequence suppression','Snapshot-delayed startup warmup via accepted recovery ledger'],
            'limitation':'Raw tape source identity/counts are separately validated by native archive and maker audits; this checks a different window-calculation implementation.'}

if __name__=='__main__':
    cli=argparse.ArgumentParser();cli.add_argument('activity_dir');args=cli.parse_args()
    result=audit(args.activity_dir)
    out=Path(args.activity_dir)/'independent-window-audit.json'
    with out.open('x') as f:json.dump(result,f,indent=2)
    print(json.dumps(result))

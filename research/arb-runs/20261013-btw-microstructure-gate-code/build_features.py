"""Causal native-event microstructure entry masks. No simulation or private APIs."""
import argparse
import csv
import hashlib
import json
import mmap
from pathlib import Path
import struct
from datetime import datetime,timezone
import numpy as np

NS=10**9
HEADER=struct.Struct('<qBBBBI8q')
EVENT_DTYPE=np.dtype([('ns','<i8'),('venue','u1'),('kind','u1'),('padding','V2'),('count','<u4')])
ACTIVITY_DTYPE=np.dtype(EVENT_DTYPE.descr+[('notional','<i8')])
GATE_DTYPE=np.dtype([('ns','<i8'),('mask','<u4')])


def require(ok,message):
    if not ok:raise ValueError(message)


def sha(path):
    with Path(path).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()


def read(path):return json.loads(Path(path).read_text())


def write(path,value):
    with Path(path).open('x') as f:json.dump(value,f,indent=2,allow_nan=False);f.write('\n')


class RateStream:
    """Indexed exact counts; resets use event ordinal as well as time."""
    def __init__(self,events,allow_zero):
        require(len(events)>0,'empty event stream')
        require(np.all(np.diff(events['ns'])>=0),'event time regression')
        require(np.all(events['venue']<2) and np.all(events['kind']<3),'event identity')
        require(np.all(events['padding']==np.void(b'\0\0')),'nonzero event padding')
        require(np.all(events['count'][events['kind']!=2]==0),'reset event count must be0')
        if not allow_zero:require(np.all(events['count'][events['kind']==2]>0),'empty live trade group')
        self.venues=[]
        for venue in (0,1):
            e=events[events['venue']==venue]
            if not len(e):self.venues.append(None);continue
            ix=np.arange(len(e),dtype=np.int64)
            resets=np.maximum.accumulate(np.where(e['kind']!=2,ix,-1))
            safe=np.maximum(resets,0)
            initialized=(resets>=0)&(e['kind'][safe]==0)
            require(np.all(initialized[e['kind']==2]),'delta/trade before initialization')
            counts=np.where(e['kind']==2,e['count'],0).astype(np.int64)
            self.venues.append((e['ns'].copy(),e['kind'].copy(),resets,np.r_[np.int64(0),np.cumsum(counts)]))

    def at(self,ns):
        n=len(ns);out={k:np.zeros((n,2),dtype=np.int64) for k in ('count5','count60')}
        out.update({k:np.zeros((n,2),dtype=bool) for k in ('initialized','warm','rate_valid')})
        out['ratio']=np.full((n,2),np.nan)
        for v,data in enumerate(self.venues):
            if data is None:continue
            times,kinds,last_reset,prefix=data
            end=np.searchsorted(times,ns,side='right')
            r=np.where(end>0,last_reset[np.maximum(end-1,0)],-1)
            safe=np.maximum(r,0)
            initialized=(end>0)&(r>=0)&(kinds[safe]==0)
            warm=initialized&(ns-times[safe]>=60*NS)
            out['initialized'][:,v]=initialized;out['warm'][:,v]=warm
            for window,name in ((5,'count5'),(60,'count60')):
                left=np.maximum(np.searchsorted(times,ns-window*NS,side='right'),r+1)
                left=np.minimum(left,end)
                out[name][:,v]=prefix[end]-prefix[left]
            good=warm&(out['count60'][:,v]>0)
            out['rate_valid'][:,v]=good
            out['ratio'][good,v]=12*out['count5'][good,v]/out['count60'][good,v]
        return out


class Persistence:
    def __init__(self):
        self.previous_ns=None;self.previous_direction=-1;self.start=None

    def at(self,ns,prices,valid,book_reset):
        require(len(ns)>0 and np.all(np.diff(ns)>0),'native time order')
        if self.previous_ns is not None:require(int(ns[0])>self.previous_ns,'chunk time order')
        quote_valid=valid&(prices>0).all(axis=1)&(prices[:,0]>=prices[:,1])&(prices[:,2]>=prices[:,3])
        # Multiplication is safe for the frozen tapes; reject unsafe future inputs.
        require(np.all(np.abs(prices)<10**12),'unsupported huge price scale')
        a=quote_valid&(20000*(prices[:,3]-prices[:,0])>=30*(prices[:,3]+prices[:,0]))
        b=quote_valid&(20000*(prices[:,1]-prices[:,2])>=30*(prices[:,1]+prices[:,2]))
        require(not np.any(a&b),'opposite positive spreads on uncrossed quotes')
        direction=np.full(len(ns),-1,dtype=np.int8);direction[a]=0;direction[b]=1
        prev_dir=np.r_[self.previous_direction,direction[:-1]]
        prev_ns=np.r_[ns[0] if self.previous_ns is None else self.previous_ns,ns[:-1]]
        gap=(ns-prev_ns)>100_000_000
        change=direction!=prev_dir
        breaks=(direction<0)|gap|book_reset|change
        if self.start is None:breaks[0]=True
        initial=-1 if self.start is None else self.start
        starts=np.maximum.accumulate(np.r_[initial,np.where(breaks,ns,-1)])[1:]
        duration=np.where(direction>=0,ns-starts,0)
        flags=((~quote_valid).astype(np.uint8)|gap.astype(np.uint8)*2|
               book_reset.astype(np.uint8)*4|((direction<0)|change).astype(np.uint8)*8)
        self.previous_ns=int(ns[-1]);self.previous_direction=int(direction[-1]);self.start=int(starts[-1])
        return {'raw_direction':direction,'persistence_ns':duration,
                'persistence_reset_flags':flags,'native_valid':quote_valid,
                'book_reset_since_previous':book_reset}


def policy_masks(features,policies):
    n=len(features['persistence_ns']);masks=np.ones(n,dtype=np.uint32);reasons={}
    for bit,p in enumerate(policies):
        if bit==0:require(p['id']=='baseline','first bit must baseline');continue
        allowed=np.ones(n,dtype=bool);why={}
        for kind in ('book','trade'):
            if not p[f'require_{kind}_rate']:continue
            init=features[f'{kind}_initialized'].all(axis=1)
            warm=features[f'{kind}_warm'].all(axis=1)
            nonzero=(features[f'{kind}_count60']>0).all(axis=1)
            good=features[f'{kind}_rate_valid'].all(axis=1)
            allowed&=good
            why[f'{kind}_uninitialized']=~init
            why[f'{kind}_warmup_incomplete']=init&~warm
            why[f'{kind}_observed_zero_denominator']=warm&~nonzero
            threshold=p[f'min_{kind}_rate_ratio']
            if threshold>0:
                passed=(features[f'{kind}_ratio']>=threshold).all(axis=1)
                why[f'{kind}_ratio_below']=good&~passed;allowed&=passed
        if p['min_spread_persistence_ms']>0:
            passed=features['persistence_ns']>=p['min_spread_persistence_ms']*10**6
            why['spread_not_persistent']=~passed;allowed&=passed
        masks[allowed]|=np.uint32(1<<bit);reasons[p['id']]=why
    return masks,reasons


def mapped_events(path,magic,dtype):
    with Path(path).open('rb') as f:require(f.read(8)==magic,'event magic')
    require((Path(path).stat().st_size-8)%dtype.itemsize==0,'event trailing bytes')
    return np.memmap(path,dtype=dtype,mode='r',offset=8)


def tape_chunks(path,chunk_size=100000):
    with Path(path).open('rb') as f,mmap.mmap(f.fileno(),0,access=mmap.ACCESS_READ) as data:
        require(data[:8] in (b'BTWTP002',b'BTWTP003'),'native tape magic')
        at=8;previous=-1;times=[];prices=[];valid=[]
        while at<len(data):
            require(at+80<=len(data),'truncated native header');row=HEADER.unpack_from(data,at)
            require(row[0]>previous and row[0]>0,'native time order')
            require(row[1] in (0,1) and row[2] in (0,1,2) and row[3] in (0,1) and row[4]==0,'native flags')
            require(row[5]<=1000 and (row[1] or row[5]==0),'native update count')
            at+=80+24*row[5];require(at<=len(data),'truncated native depth updates')
            previous=row[0];times.append(row[0]);prices.append(row[6:10]);valid.append(bool(row[1]))
            if len(times)==chunk_size:
                yield np.array(times,np.int64),np.array(prices,np.int64),np.array(valid,bool)
                times=[];prices=[];valid=[]
        if times:yield np.array(times,np.int64),np.array(prices,np.int64),np.array(valid,bool)


def prepare_sources(root,day,protocol):
    manifest=read(protocol['original_input_manifest']);spec=manifest['days'][day]
    tape=Path(spec['files']['tape']['path']);require(sha(tape)==spec['files']['tape']['sha256'],'original native tape hash')
    activity_dir=Path(spec['files']['activity']['path']).parent
    activity_meta_path=activity_dir/'metadata.json';activity_meta=read(activity_meta_path)
    require(activity_meta['status']=='COMPLETED' and activity_meta['date']==day and activity_meta['symbol']=='BTWUSDT','activity identity')
    require(activity_meta['book_tape_sha256']==spec['files']['tape']['sha256'],'activity/tape binding')
    trade=activity_dir/'activity-events.bin'
    require(sha(trade)==activity_meta['artifacts']['activity-events.bin']['sha256'],'accepted trade-event hash')
    book=root/'book-events'/day/'events.bin';book_meta_path=book.parent/'metadata.json';book_meta=read(book_meta_path)
    require(book_meta['status']=='PASS' and book_meta['date']==day and book_meta['symbol']=='BTWUSDT','book identity/status')
    require(book_meta['execution_enabled'] is False,'book public-only status')
    require(book_meta['protocol_sha256']==sha(root/'protocol.json'),'book/protocol binding')
    require(book_meta['tape']['sha256']==spec['files']['tape']['sha256'],'book/tape binding')
    require(book_meta['source_evidence_sha256']==activity_meta['source_evidence_sha256'],'book/trade archive evidence differs')
    require(sha(book)==book_meta['outputs']['events.bin']['sha256'],'book-event hash')
    require(book_meta['stream_validation']['status']=='PASS','book stream validation')
    sources={'tape':tape,'book_events':book,'book_metadata':book_meta_path,
             'trade_events':trade,'activity_metadata':activity_meta_path,
             'original_input_manifest':Path(protocol['original_input_manifest'])}
    return spec,sources,{key:{'path':str(p),'sha256':sha(p)} for key,p in sources.items()}


def build_day(root,day,protocol):
    spec,paths,sources=prepare_sources(root,day,protocol)
    book_events=mapped_events(paths['book_events'],b'BTWBOOK1',EVENT_DTYPE)
    trade_events=mapped_events(paths['trade_events'],b'BTWAES01',ACTIVITY_DTYPE)
    book_stream=RateStream(book_events,True);trade_stream=RateStream(trade_events,False)
    reset_ns=book_events['ns'][book_events['kind']!=2].copy()
    target=root/'features'/day;target.mkdir(parents=True,exist_ok=False)
    policies=protocol['policies'];require(0<len(policies)<=32,'policy mask capacity')
    start=int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp())*NS
    persistence=Persistence();previous_reset_count=0;index=[];rows=0;schema=None
    counts={'scope':'Counts are native decision events, not clock-time proportions; only the new entry gates, before original activity/history/funding/spread/account checks.',
            'all_native_events':0,'noon_to_2358_native_events':0,'all_native_valid_events':0,'noon_to_2358_native_valid_events':0,
            'feature_coverage':{kind:{'all_native_events':0,'noon_to_2358_native_events':0} for kind in ('book','trade','both')},
            'policies':{p['id']:{'all_allowed_native_events':0,'noon_to_2358_allowed_native_events':0,
                 'all_allowed_native_valid_events':0,'noon_to_2358_allowed_native_valid_events':0,'rejection_reasons':{}} for p in policies}}
    hourly_total=np.zeros(24,np.int64);hourly_allowed=np.zeros((24,len(policies)),np.int64)
    gate_path=target/'entry-gates.bin'
    with gate_path.open('xb') as gate:
        gate.write(b'BTWGATE1')
        for chunk_number,(ns,prices,native_valid) in enumerate(tape_chunks(paths['tape'])):
            require((ns>=start).all() and (ns<start+86400*NS).all(),'native rows outside day')
            reset_count=np.searchsorted(reset_ns,ns,side='right')
            book_reset=reset_count>np.r_[previous_reset_count,reset_count[:-1]]
            previous_reset_count=int(reset_count[-1])
            f={'ns':ns}
            for kind,stream in (('book',book_stream),('trade',trade_stream)):
                for key,value in stream.at(ns).items():f[kind+'_'+key]=value
                f[kind+'_valid']=f[kind+'_rate_valid'].all(axis=1)
            f.update(persistence.at(ns,prices,native_valid,book_reset))
            masks,reasons=policy_masks(f,policies);f['allowed_mask']=masks
            record=np.empty(len(ns),dtype=GATE_DTYPE);record['ns']=ns;record['mask']=masks;gate.write(record.tobytes())
            chunk_path=target/f'native-{chunk_number:05d}.npz';np.savez_compressed(chunk_path,**f)
            index.append({'path':str(chunk_path),'sha256':sha(chunk_path),'rows':len(ns),'first_ns':int(ns[0]),'last_ns':int(ns[-1])})
            if schema is None:schema={k:{'dtype':str(v.dtype),'shape_tail':list(v.shape[1:])} for k,v in f.items()}
            noon=(ns>=spec['entry_start_ns'])&(ns<start+86280*NS)
            valid=f['native_valid'];hours=((ns-start)//(3600*NS)).astype(int)
            hourly_total+=np.bincount(hours,minlength=24)
            counts['all_native_events']+=len(ns);counts['noon_to_2358_native_events']+=int(noon.sum())
            counts['all_native_valid_events']+=int(valid.sum());counts['noon_to_2358_native_valid_events']+=int((noon&valid).sum())
            for kind,coverage in (('book',f['book_valid']),('trade',f['trade_valid']),('both',f['book_valid']&f['trade_valid'])):
                counts['feature_coverage'][kind]['all_native_events']+=int(coverage.sum())
                counts['feature_coverage'][kind]['noon_to_2358_native_events']+=int((coverage&noon).sum())
            for bit,p in enumerate(policies):
                allowed=(masks&(1<<bit))!=0;c=counts['policies'][p['id']]
                c['all_allowed_native_events']+=int(allowed.sum());c['noon_to_2358_allowed_native_events']+=int((allowed&noon).sum())
                c['all_allowed_native_valid_events']+=int((allowed&valid).sum());c['noon_to_2358_allowed_native_valid_events']+=int((allowed&valid&noon).sum())
                hourly_allowed[:,bit]+=np.bincount(hours[allowed],minlength=24)
                for reason,mask in reasons.get(p['id'],{}).items():
                    rc=c['rejection_reasons'].setdefault(reason,{'all_native_events':0,'noon_to_2358_native_events':0})
                    rc['all_native_events']+=int(mask.sum());rc['noon_to_2358_native_events']+=int((mask&noon).sum())
            rows+=len(ns)
    for p,c in counts['policies'].items():
        c['all_allowance_event_fraction']=c['all_allowed_native_events']/counts['all_native_events']
        c['noon_to_2358_allowance_event_fraction']=c['noon_to_2358_allowed_native_events']/counts['noon_to_2358_native_events']
    counts['hourly_total_native_events']=hourly_total.tolist()
    counts['hourly_allowed_native_events']={p['id']:hourly_allowed[:,bit].tolist() for bit,p in enumerate(policies)}
    write(target/'coverage.json',counts)
    shared={'day':day,'execution_enabled':False,'volatility_entry_filters_enabled':False,
            'protocol_sha256':sha(root/'protocol.json'),'policies_sha256':sha(root/'policies.csv'),
            'builder_sha256':sha(__file__),'sources':sources}
    feature_index={'status':'FEATURES_BUILT',**shared,'rows':rows,'chunks':index,'schema':schema,
                   'reset_flags':{'1':'invalid native L1','2':'interdecision gap>100ms','4':'any accepted book snapshot/disconnect since previous decision inclusive current batch','8':'raw direction absent or changed'},
                   'contract':'Complete same-ns event batches before decision. Windows(t-W,t], 60s exact warmup since latest reset, positive60s denominator. rate_valid per venue includes initialized/warm/denominator; combined_valid requires both venues. Ratios12*N5/N60, no epsilon. Persistence is observed native quote persistence only; no synthetic elapsed triggers.'}
    write(target/'feature-index.json',feature_index)
    outputs={name:{'path':str(target/name),'sha256':sha(target/name)} for name in ('feature-index.json','entry-gates.bin','coverage.json')}
    meta={'status':'FEATURES_AND_GATES_BUILT',**shared,'rows':rows,'chunks':len(index),'gate_format':'BTWGATE1 + <qI rows exactly aligned to native tape',
          'policy_bit_order':[p['id'] for p in policies],'outputs':outputs}
    write(target/'metadata.json',meta)
    return {'feature_index':outputs['feature-index.json'],'entry-gates.bin':outputs['entry-gates.bin'],
            'coverage.json':outputs['coverage.json'],'metadata':{'path':str(target/'metadata.json'),'sha256':sha(target/'metadata.json')}}


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--root',required=True,type=Path);ap.add_argument('--day');a=ap.parse_args();root=a.root.resolve()
    protocol=read(root/'protocol.json');require(protocol['execution_enabled'] is False and protocol['volatility_entry_filters_enabled'] is False,'public-only non-volatility protocol')
    with (root/'policies.csv').open(newline='') as f:policies=list(csv.DictReader(f))
    require([p['id'] for p in policies]==[p['id'] for p in protocol['policies']],'policy order')
    days=[*protocol['fit_dates'],protocol['evaluation_date']]
    if a.day:
        require(a.day in days,'unexpected day');result=build_day(root,a.day,protocol);print(json.dumps({'day':a.day,**result}),flush=True)
    else:
        result={'status':'FEATURES_AND_GATES_BUILT','execution_enabled':False,'volatility_entry_filters_enabled':False,
                'protocol_sha256':sha(root/'protocol.json'),'policies_sha256':sha(root/'policies.csv'),'builder_sha256':sha(__file__),'days':{}}
        for day in days:
            result['days'][day]=build_day(root,day,protocol);print(json.dumps({'day':day,'status':'FEATURES_AND_GATES_BUILT'}),flush=True)
        write(root/'features/manifest.json',result)


if __name__=='__main__':main()

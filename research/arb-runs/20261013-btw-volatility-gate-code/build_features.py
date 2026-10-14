"""Build causal public-data entry masks; never executes or simulates an order."""
import argparse
import csv
import hashlib
import json
import mmap
from pathlib import Path
import struct
from datetime import datetime, timezone

import numpy as np

GRID_COLUMNS=['ns','bybit_mid_ticks_x2','bitget_mid_ticks_x2','validmask','segmentA','segmentB']
HEADER=struct.Struct('<qBBBBI8q')
GATE_DTYPE=np.dtype([('ns','<i8'),('mask','<u4')])


def sha(path):
    with Path(path).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()


def require(ok,message):
    if not ok:raise ValueError(message)


def write_json(path,value):
    with Path(path).open('x') as f:json.dump(value,f,indent=2,allow_nan=False);f.write('\n')


def interval_returns(grid,step):
    """UTC-aligned returns; every sampled cell valid, no snapshot discontinuity."""
    require(len(grid)>0 and grid.shape[1]==6,'grid shape')
    # Daily UTC grids start on a step boundary; explicitly check instead of assuming.
    require(int(grid[0,0])%(step*10**9)==0,'UTC alignment')
    ends=np.arange(step,len(grid),step)
    out=np.full((len(ends),2),np.nan)
    for venue in (0,1):
        good=((grid[:,3]&(1<<venue))!=0)&(grid[:,1+venue]>0)&(grid[:,4+venue]>0)
        bad=np.r_[0,np.cumsum(~good)]
        changes=np.r_[0,np.cumsum(grid[1:,4+venue]!=grid[:-1,4+venue])]
        valid=(bad[ends+1]-bad[ends-step]==0)&(changes[ends]-changes[ends-step]==0)
        out[valid,venue]=1e4*np.log(grid[ends[valid],1+venue]/grid[ends[valid]-step,1+venue])
    return ends,out


def rolling_std(values,window,min_count):
    """Population std using actual elapsed rows; missing returns stay missing."""
    good=np.isfinite(values);x=np.where(good,values,0.)
    counts=np.r_[0,np.cumsum(good)];sums=np.r_[0.,np.cumsum(x)];squares=np.r_[0.,np.cumsum(x*x)]
    ends=np.arange(len(values))+1;starts=np.maximum(0,ends-window)
    n=counts[ends]-counts[starts];s=sums[ends]-sums[starts];ss=squares[ends]-squares[starts]
    # index0 is midnight and has no return from this archive. Require complete clock horizon.
    valid=(np.arange(len(values))>=window)&(n>=min_count)
    result=np.full(len(values),np.nan)
    result[valid]=np.sqrt(np.maximum(0.,ss[valid]/n[valid]-(s[valid]/n[valid])**2))
    return result,n.astype(np.int32)


def compute_features(grid):
    require(grid.dtype==np.int64 and grid.ndim==2 and grid.shape[1]==6,'grid dtype/shape')
    require(np.all(np.diff(grid[:,0])==10**9),'grid must use consecutive exact seconds')
    n=len(grid);current=((grid[:,3]==3)&(grid[:,1:3]>0).all(axis=1)&(grid[:,4:6]>0).all(axis=1))
    one_ends,one=interval_returns(grid,1)
    dense=np.full((n,2),np.nan);dense[one_ends]=one
    short=np.empty((n,2));long=np.empty((n,2));cs=np.empty((n,2),dtype=np.int32);cl=cs.copy()
    for v in (0,1):
        short[:,v],cs[:,v]=rolling_std(dense[:,v],300,240)
        long[:,v],cl[:,v]=rolling_std(dense[:,v],3600,2880)
    ratio_by_venue=np.full((n,2),np.nan)
    ratio_ok=np.isfinite(short)&np.isfinite(long)&(long>0)
    ratio_by_venue[ratio_ok]=short[ratio_ok]/long[ratio_ok]
    cooling=np.full(n,np.nan);both=ratio_ok.all(axis=1)
    cooling[both]=ratio_by_venue[both].min(axis=1)
    ends,r15=interval_returns(grid,15)
    # slots0=midnight(no preceding interval),1=00:00:15,... . UTC endpoints in(t-300,t].
    blocks=np.full(n//15+1,np.nan)
    blocks[ends//15]=r15[:,0]-r15[:,1]
    block_std,block_count=rolling_std(blocks,20,16)
    block_index=np.arange(n)//15
    basis=block_std[block_index];bc=block_count[block_index]
    return {'ns':grid[:,0].copy(),'current_mask':grid[:,3].astype(np.uint8),
            'current_valid':current,'basis_end_ns':grid[0,0]+block_index*15*10**9,
            'basis_sigma_bps':basis,'basis_count':bc,'basis_valid':np.isfinite(basis)&current,
            'std_short_bps':short,'std_long_bps':long,'count_short':cs,'count_long':cl,
            'cooling_ratio_by_venue':ratio_by_venue,'cooling_ratio':cooling,
            'cooling_valid':np.isfinite(cooling)&current}


def cost_proxy(prices,valid):
    prices=prices.astype(np.float64)
    ok=valid&(prices>0).all(axis=1)&(prices[:,0]>=prices[:,1])&(prices[:,2]>=prices[:,3])
    out=np.full(len(prices),np.nan);p=prices[ok]
    total=p.sum(axis=1)
    out[ok]=1e4*((p[:,0]-p[:,1])+(p[:,2]-p[:,3])+.0003*total)/(total/4)
    return out


def gate_masks(ns,prices,native_valid,features,policies):
    sample=np.searchsorted(features['ns'],ns,side='right')-1
    found=(sample>=0)&(ns<features['ns'][-1]+10**9)
    idx=np.clip(sample,0,len(features['ns'])-1)
    basis=features['basis_valid'][idx]&found;cooling=features['cooling_valid'][idx]&found
    cost=cost_proxy(prices,native_valid)
    ratio=np.full(len(ns),np.nan);good=basis&np.isfinite(cost)&(cost>0)
    ratio[good]=features['basis_sigma_bps'][idx[good]]/cost[good]
    masks=np.ones(len(ns),dtype=np.uint32)
    reasons={}
    for bit,policy in enumerate(policies):
        if bit==0:
            require(policy['id']=='baseline','baseline must be bit0');continue
        allow=np.ones(len(ns),dtype=bool);r={}
        if policy['require_basis']:
            allow&=basis;r['basis_feature_invalid']=~basis
            if policy['min_basis_cost_ratio']>0:
                allow&=np.isfinite(ratio)&(ratio>=policy['min_basis_cost_ratio'])
                r['current_quote_cost_invalid']=~np.isfinite(cost)
                r['basis_cost_ratio_below']=good&(ratio<policy['min_basis_cost_ratio'])
        if policy['require_cooling']:
            allow&=cooling;r['cooling_feature_invalid']=~cooling
            if policy['min_cooling_ratio']>0:
                below=cooling&(features['cooling_ratio'][idx]<policy['min_cooling_ratio'])
                allow&=~below;r['cooling_ratio_below']=below
        masks[allow]|=np.uint32(1<<bit);reasons[policy['id']]=r
    return masks,reasons,{'sample_index':sample,'cost_proxy_bps':cost,'basis_cost_ratio':ratio}


def tape_chunks(path,chunk_size=100000):
    with Path(path).open('rb') as f,mmap.mmap(f.fileno(),0,access=mmap.ACCESS_READ) as data:
        require(data[:8] in (b'BTWTP002',b'BTWTP003'),'tape magic')
        pos=8;previous=-1;ns=[];prices=[];valid=[]
        while pos<len(data):
            require(pos+80<=len(data),'truncated tape header')
            row=HEADER.unpack_from(data,pos)
            require(row[0]>previous and row[0]>0,'tape time order')
            require(row[1] in (0,1) and row[2] in (0,1,2) and row[3] in (0,1) and row[4]==0,'tape flags')
            require(row[5]<=1000 and (row[1] or row[5]==0),'tape update count')
            pos+=80+row[5]*24;require(pos<=len(data),'truncated tape updates')
            previous=row[0];ns.append(row[0]);prices.append(row[6:10]);valid.append(bool(row[1]))
            if len(ns)==chunk_size:
                yield np.array(ns,dtype=np.int64),np.array(prices,dtype=np.int64),np.array(valid,dtype=bool)
                ns=[];prices=[];valid=[]
        if ns:yield np.array(ns,dtype=np.int64),np.array(prices,dtype=np.int64),np.array(valid,dtype=bool)


def build_day(root,day,protocol,manifest,acceptance):
    spec=manifest['days'][day];evidence=acceptance['days'][day]
    grid_path=Path(evidence['grid_path']);tape_path=Path(spec['files']['tape']['path'])
    require(sha(grid_path)==evidence['grid_sha256'],'grid source hash')
    require(sha(tape_path)==evidence['tape_sha256']==spec['files']['tape']['sha256'],'tape source hash')
    require(Path(evidence['same_source_tape_path'])==tape_path,'grid/tape source identity')
    with np.load(grid_path,allow_pickle=False) as z:
        require(z['columns'].tolist()==GRID_COLUMNS,'grid field schema');grid=z['data']
    start=int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp())*10**9
    require(grid.shape==(86400,6) and int(grid[0,0])==start,'daily grid bounds')
    features=compute_features(grid)
    target=root/'features'/day;target.mkdir(parents=True,exist_ok=False)
    feature_path=target/'features.npz';np.savez_compressed(feature_path,**features)
    policies=protocol['policies'];npol=len(policies);require(0<npol<=32,'policy bits fit u32')
    hourly=np.zeros((24,npol),dtype=np.int64);denom=np.zeros(24,dtype=np.int64)
    valid_hourly=np.zeros((24,npol),dtype=np.int64);valid_denom=np.zeros(24,dtype=np.int64)
    reason_counts={p['id']:{} for p in policies};rows=0;first=None;last=None
    gate_path=target/'entry-gates.bin'
    with gate_path.open('xb') as f:
        f.write(b'BTWGATE1')
        for ns,prices,valid in tape_chunks(tape_path):
            require((ns>=start).all() and (ns<start+86400*10**9).all(),'tape outside grid day')
            mask,reasons,aux=gate_masks(ns,prices,valid,features,policies)
            out=np.empty(len(ns),dtype=GATE_DTYPE);out['ns']=ns;out['mask']=mask;f.write(out.tobytes())
            hours=((ns-start)//(3600*10**9)).astype(int)
            denom+=np.bincount(hours,minlength=24);valid_denom+=np.bincount(hours[valid],minlength=24)
            for bit,p in enumerate(policies):
                allowed=(mask&(1<<bit))!=0
                hourly[:,bit]+=np.bincount(hours[allowed],minlength=24)
                valid_hourly[:,bit]+=np.bincount(hours[allowed&valid],minlength=24)
                for key,r in reasons.get(p['id'],{}).items():
                    counts=reason_counts[p['id']].setdefault(key,{'all_native_decisions':0,'native_quote_valid_decisions':0,'noon_to_2358_native_quote_valid_decisions':0})
                    counts['all_native_decisions']+=int(r.sum());counts['native_quote_valid_decisions']+=int((r&valid).sum())
                    entryperiod=(ns>=spec['entry_start_ns'])&(ns<start+86280*10**9)
                    counts['noon_to_2358_native_quote_valid_decisions']+=int((r&valid&entryperiod).sum())
            rows+=len(ns);first=int(ns[0]) if first is None else first;last=int(ns[-1])
    coverage={'scope':'Standalone extra entry gate allowance; excludes account state, q25, history, funding, raw-entry-spread and other pre-existing conditions. Reasons overlap.',
              'hourly_native_decisions':denom.tolist(),'hourly_native_quote_valid_decisions':valid_denom.tolist(),
              'policies':{p['id']:{'bit':bit,'allowed_native_decisions':int(hourly[:,bit].sum()),
                 'hourly_allowed_native_decisions':hourly[:,bit].tolist(),
                 'hourly_allowed_native_quote_valid_decisions':valid_hourly[:,bit].tolist(),
                 'rejections_by_reason':reason_counts[p['id']]} for bit,p in enumerate(policies)},
              'feature_grid':{'rows':len(grid),'current_valid':int(features['current_valid'].sum()),
                 'basis_valid':int(features['basis_valid'].sum()),'cooling_valid':int(features['cooling_valid'].sum()),
                 'hourly_basis_valid':np.count_nonzero(features['basis_valid'].reshape(24,3600),axis=1).tolist(),
                 'hourly_cooling_valid':np.count_nonzero(features['cooling_valid'].reshape(24,3600),axis=1).tolist()}}
    write_json(target/'coverage.json',coverage)
    meta={'status':'FEATURES_AND_GATES_BUILT','execution_enabled':False,'day':day,'code_sha256':sha(__file__),
          'protocol_sha256':sha(root/'protocol.json'),'policies_sha256':sha(root/'policies.csv'),
          'original_input_manifest_sha256':sha(protocol['original_input_manifest']),
          'grid_provenance_sha256':sha(protocol['grid_provenance']),
          'grid':{'path':str(grid_path),'sha256':sha(grid_path)},'tape':{'path':str(tape_path),'sha256':sha(tape_path)},
          'feature_schema':{k:{'dtype':str(v.dtype),'shape':list(v.shape)} for k,v in features.items()},
          'basis_window':'The 20 UTC-aligned15s intervals with endpoints in(current_grid_ns-300s,current_grid_ns], equivalent to latest completed endpoint E and interval span[E-300s,E]. E may precede currentgrid by0–14s. Full300s requires E>=daystart+300s.',
          'statistics':'Population standard deviation, direct float64>=threshold, no epsilon. NaN metric denotes insufficient coverage/horizon or zero long variance. Current_valid adds current grid mask3 and positive mids/segments.',
          'gate_schema':{'magic':'BTWGATE1','row':'<qI','bytes_per_row':12,'rows':rows,'first_ns':first,'last_ns':last,'policy_bit_order':[p['id'] for p in policies]},
          'outputs':{name:{'path':str(target/name),'sha256':sha(target/name)} for name in ['features.npz','entry-gates.bin','coverage.json']}}
    write_json(target/'metadata.json',meta)
    return {'metadata':str(target/'metadata.json'),'metadata_sha256':sha(target/'metadata.json'),**meta['outputs']}


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--root',required=True,type=Path);a=ap.parse_args();root=a.root.resolve()
    protocol=json.loads((root/'protocol.json').read_text());manifest=json.loads(Path(protocol['original_input_manifest']).read_text())
    acceptance=json.loads(Path(protocol['grid_provenance']).read_text())
    require(protocol['execution_enabled'] is False and acceptance['status']=='PASS','protocol/provenance status')
    require(sha(protocol['original_input_manifest'])==acceptance['source_manifest_sha256'],'grid provenance source manifest')
    with (root/'policies.csv').open() as f:rows=list(csv.DictReader(f))
    require([p['id'] for p in protocol['policies']]==[p['id'] for p in rows],'policy bit order mismatch')
    days=[*protocol['fit_dates'],protocol['evaluation_date']]
    result={'status':'FEATURES_AND_GATES_BUILT','execution_enabled':False,'protocol_sha256':sha(root/'protocol.json'),
            'policies_sha256':sha(root/'policies.csv'),'code_sha256':sha(__file__),'days':{}}
    for day in days:
        result['days'][day]=build_day(root,day,protocol,manifest,acceptance)
        print(json.dumps({'day':day,'status':'FEATURES_AND_GATES_BUILT'}),flush=True)
    write_json(root/'features/manifest.json',result)


if __name__=='__main__':main()

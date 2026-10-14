"""Independent causal feature and native policy-mask oracle; no strategy/helper imports."""
import argparse
import hashlib
import json
import math
import mmap
import struct
from pathlib import Path
import numpy as np

HEADER=struct.Struct('<qBBBBI8q');GATE=struct.Struct('<qI')

def sha(p):
 with Path(p).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def read(p):return json.loads(Path(p).read_text())
def require(ok,msg):
 if not ok:raise AssertionError(msg)

def trailing_std(values,window):
 """Independent two-pass direct windows for each requested endpoint."""
 n=len(values);count=np.zeros(n,dtype=np.int32);sigma=np.full(n,np.nan)
 for end in range(n):
  sample=values[max(0,end-window+1):end+1];sample=sample[np.isfinite(sample)];count[end]=len(sample)
  if end>=window and len(sample)>=math.ceil(.8*window):sigma[end]=np.std(sample,ddof=0)
 return sigma,count

def expected_features(grid):
 n=len(grid);current=(grid[:,3]==3)&(grid[:,1]>0)&(grid[:,2]>0)&(grid[:,4]>0)&(grid[:,5]>0)
 short=np.full((n,2),np.nan);long=np.full((n,2),np.nan);cs=np.zeros((n,2),np.int32);cl=np.zeros((n,2),np.int32)
 for v in (0,1):
  good=((grid[:,3]&(1<<v))!=0)&(grid[:,v+1]>0)&(grid[:,v+4]>0)
  r=np.full(n,np.nan)
  for t in range(1,n):
   if good[t] and good[t-1] and grid[t,v+4]==grid[t-1,v+4]:r[t]=10000*math.log(int(grid[t,v+1])/int(grid[t-1,v+1]))
  short[:,v],cs[:,v]=trailing_std(r,300);long[:,v],cl[:,v]=trailing_std(r,3600)
 endns=np.full(n,-1,dtype=np.int64);basis=np.full(n,np.nan);bc=np.zeros(n,np.int32);changes={}
 for end in range(15,n,15):
  interval=grid[end-15:end+1]
  valid=bool(np.all(current[end-15:end+1])) and len(set(map(int,interval[:,4])))==1 and len(set(map(int,interval[:,5])))==1
  changes[end]=10000*(math.log(int(grid[end,1])/int(grid[end,2]))-math.log(int(grid[end-15,1])/int(grid[end-15,2]))) if valid else float('nan')
 for t in range(n):
  end=(t//15)*15;endns[t]=int(grid[end,0]);ends=range(max(15,end-285),end+1,15)
  sample=[changes[e] for e in ends if math.isfinite(changes[e])];bc[t]=len(sample)
  if t>=300 and len(sample)>=16:basis[t]=np.std(sample,ddof=0)
 bvalid=current&np.isfinite(basis)
 ratio=np.full(n,np.nan);venue_ok=np.isfinite(short)&np.isfinite(long)&(long>0);both=np.all(venue_ok,axis=1)
 byvenue=np.full((n,2),np.nan);byvenue[venue_ok]=short[venue_ok]/long[venue_ok]
 ratio[both]=np.min(byvenue[both],axis=1);v=current&both
 return {'ns':grid[:,0],'current_valid':current,'current_mask':grid[:,3].astype(np.uint8),'basis_end_ns':endns,'basis_sigma_bps':basis,'basis_count':bc,'basis_valid':bvalid,'std_short_bps':short,'std_long_bps':long,'count_short':cs,'count_long':cl,'cooling_ratio_by_venue':byvenue,'cooling_ratio':ratio,'cooling_valid':v}

def cost_bps(prices):
 a,b,c,d=map(int,prices)
 if min(a,b,c,d)<=0 or a<b or c<d:return None
 total=a+b+c+d
 return 10000*((a-b)+(c-d)+.0003*total)/(total/4)

def expected_mask(bvalid,cvalid,basis,cooling,cost,policies):
 mask=0
 for i,p in enumerate(policies):
  accept=(not p['require_basis'] or bvalid) and (not p['require_cooling'] or cvalid)
  if accept and p['min_basis_cost_ratio']>0:accept=cost is not None and cost>0 and basis/cost>=p['min_basis_cost_ratio']
  if accept and p['min_cooling_ratio']>0:accept=cooling>=p['min_cooling_ratio']
  if accept:mask|=1<<i
 return mask

def audit(root,day):
 protocol=read(root/'protocol.json');manifest=read(root/'input-manifest.json');spec=manifest['days'][day]
 provenance=read(protocol['grid_provenance']);require(provenance['status']=='PASS','grid provenance');source=provenance['days'][day]
 gridpath=Path(source['grid_path']);require(sha(gridpath)==source['grid_sha256'],'grid exact verified source')
 tape=Path(spec['files']['tape']['path']);gate=Path(spec['files']['entry_gates']['path'])
 require(sha(tape)==spec['files']['tape']['sha256']==source['tape_sha256'],'tape same grid source')
 require(sha(gate)==spec['files']['entry_gates']['sha256'],'gate manifest hash')
 with np.load(gridpath,allow_pickle=False) as data:grid=data['data']
 features=expected_features(grid)
 featurepath=root/'features'/day/'features.npz'
 with np.load(featurepath,allow_pickle=False) as saved:
  for name,values in features.items():
   require(name in saved.files,'required feature key '+name)
   actual=saved[name]
   require(actual.shape==values.shape,'feature shape '+name)
   if np.issubdtype(values.dtype,np.floating):
    require(np.array_equal(np.isfinite(actual),np.isfinite(values)),'feature availability '+name)
    good=np.isfinite(values);require(np.allclose(actual[good],values[good],rtol=1e-7,atol=1e-7),'independent feature values '+name)
   else:require(np.array_equal(actual,values),'independent feature exact values '+name)
 print(json.dumps({'feature_oracle':day,'status':'PASS'}),flush=True)
 counts=[0]*len(protocol['policies']);rows=0;previous=-1;start=int(grid[0,0]);base_only=0
 with tape.open('rb') as tf,gate.open('rb') as gf:
  with mmap.mmap(tf.fileno(),0,access=mmap.ACCESS_READ) as t,mmap.mmap(gf.fileno(),0,access=mmap.ACCESS_READ) as g:
   require(t[:8] in (b'BTWTP002',b'BTWTP003') and g[:8]==b'BTWGATE1','native magic')
   pos=gpos=8
   while pos<len(t):
    h=HEADER.unpack_from(t,pos);ns,valid=h[:2];require(ns>previous,'native decision ordering');previous=ns
    gns,mask=GATE.unpack_from(g,gpos);require(gns==ns and mask>>len(counts)==0 and mask&1,'aligned explicit policy bitmask')
    i=(ns-start)//10**9;require(0<=i<len(grid) and int(grid[i,0])<=ns,'causal latest grid')
    cost=cost_bps(h[6:10]) if valid else None
    expected=expected_mask(bool(features['basis_valid'][i]),bool(features['cooling_valid'][i]),float(features['basis_sigma_bps'][i]),float(features['cooling_ratio'][i]),cost,protocol['policies'])
    require(mask==expected,f'independent policy bitmask at {ns}: saved={mask} expected={expected}')
    for k in range(len(counts)):counts[k]+=bool(mask&(1<<k))
    base_only+=mask==1;rows+=1;pos+=HEADER.size+int(h[5])*24;gpos+=GATE.size
   require(pos==len(t) and gpos==len(g),'exact source/gate lengths')
 output={'status':'PASS','execution_enabled':False,'day':day,'auditor_sha256':sha(__file__),'protocol_sha256':sha(root/'protocol.json'),'input_manifest_sha256':sha(root/'input-manifest.json'),'grid_source_provenance_sha256':sha(protocol['grid_provenance']),'grid_sha256':sha(gridpath),'feature_file_sha256':sha(featurepath),'gate_file_sha256':sha(gate),'tape_sha256':sha(tape),'all_grid_rows_verified':len(grid),'all_native_masks_verified':rows,'baseline_only_masks':base_only,'gate_pass_counts':{p['id']:counts[i] for i,p in enumerate(protocol['policies'])},'method':'No production feature builder imports. Scalar exact integer-price log returns, direct two-pass std over every trailing window, full segment/validity checks, current tick L1 cost and all15 policy bits recomputed independently.','current_validity_required_for_nonbaseline_features':True,'baseline_always_allowed':True,'limitations':['Feature mathematical correctness does not establish a forecast of mean reversion or profit','Grid freshness censors missing/stale data; coverage-only controls must remain separate','Native archive and grid reconstruction evidence inherited by hashes, not redecoded in this audit']}
 out=root/'features'/day/'independent-feature-audit.json'
 with out.open('x') as f:json.dump(output,f,ensure_ascii=False,indent=2);f.write('\n')
 return output

if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--root',required=True);p.add_argument('--day',required=True);a=p.parse_args();print(json.dumps(audit(Path(a.root),a.day)))

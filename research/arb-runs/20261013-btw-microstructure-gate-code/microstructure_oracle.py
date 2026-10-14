"""Independent received-event rate/persistence audit; no production feature imports."""
import argparse,csv,hashlib,json,mmap,struct
from pathlib import Path
import numpy as np

NS=10**9;HEADER=struct.Struct('<qBBBBI8q');GATE=struct.Struct('<qI')
BOOK=np.dtype({'names':['ns','venue','kind','count'],'formats':['<i8','u1','u1','<u4'],'offsets':[0,8,9,12],'itemsize':16})
ACT=np.dtype({'names':['ns','venue','kind','count','notional'],'formats':['<i8','u1','u1','<u4','<i8'],'offsets':[0,8,9,12,16],'itemsize':24})
def read(p):return json.loads(Path(p).read_text())
def sha(p):
 with Path(p).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def require(x,msg):
 if not x:raise AssertionError(msg)

def direct_rate(events,t):
 seen=[e for e in events if e[0]<=t];reset=next((i for i in range(len(seen)-1,-1,-1) if seen[i][1]!=2),None)
 init=reset is not None and seen[reset][1]==0
 relevant=seen[reset+1:] if init else []
 n5=sum(e[2] for e in relevant if e[1]==2 and t-5*NS<e[0]);n60=sum(e[2] for e in relevant if e[1]==2 and t-60*NS<e[0])
 warm=init and t-seen[reset][0]>=60*NS;valid=warm and n60>0
 return n5,n60,init,warm,valid,12*n5/n60 if valid else float('nan')

class Rates:
 def __init__(self,source):
  self.parts=[]
  for v in (0,1):
   e=source[source['venue']==v];require(np.all(np.diff(e['ns'])>=0),'source time ordering')
   require(np.all(np.isin(e['kind'],[0,1,2])),'source event kind')
   require(np.all(e['count'][e['kind']!=2]==0),'reset count zero')
   reset=np.flatnonzero(e['kind']!=2)
   self.parts.append((e,reset,np.r_[np.int64(0),np.cumsum(np.where(e['kind']==2,e['count'],0),dtype=np.int64)]))
 def query(self,times):
  n=len(times);out={'count5':np.zeros((n,2),np.int64),'count60':np.zeros((n,2),np.int64),'initialized':np.zeros((n,2),bool),'warm':np.zeros((n,2),bool),'rate_valid':np.zeros((n,2),bool),'ratio':np.full((n,2),np.nan)}
  for v,(e,resets,prefix) in enumerate(self.parts):
   if not len(e) or not len(resets):continue
   end=np.searchsorted(e['ns'],times,side='right');r=np.searchsorted(e['ns'][resets],times,side='right')-1
   ri=resets[np.maximum(0,r)] if len(resets) else np.zeros(n,int)
   init=(r>=0)&(e['kind'][ri]==0) if len(e) else np.zeros(n,bool)
   out['initialized'][:,v]=init
   for window,label in ((5,'count5'),(60,'count60')):
    begin=np.maximum(ri+1,np.searchsorted(e['ns'],times-window*NS,side='right'))
    out[label][:,v]=np.where(init,prefix[end]-prefix[np.minimum(begin,end)],0)
   warm=init&(times-e['ns'][ri]>=60*NS);valid=warm&(out['count60'][:,v]>0)
   out['warm'][:,v]=warm;out['rate_valid'][:,v]=valid
   out['ratio'][valid,v]=12*out['count5'][valid,v]/out['count60'][valid,v]
  return out

def persistence_step(state,ns,valid,direction,reset):
 previous,prior_direction,start=state
 if not valid or direction<0:start=None;duration=0
 else:
  if previous is None or ns-previous>100_000_000 or reset or direction!=prior_direction or start is None:start=ns
  duration=ns-start
 return (ns,direction,start),duration

def allowed_mask(bvalid,tvalid,bratio,tratio,duration,policies):
 result=0
 for bit,p in enumerate(policies):
  ok=True
  if p['require_book_rate']:ok=all(bvalid) and all(x>=p['min_book_rate_ratio'] for x in bratio)
  if ok and p['require_trade_rate']:ok=all(tvalid) and all(x>=p['min_trade_rate_ratio'] for x in tratio)
  if ok and p['min_spread_persistence_ms']>0:ok=duration>=p['min_spread_persistence_ms']*10**6
  if ok:result|=1<<bit
 return result

def native_rows(path):
 with Path(path).open('rb') as f,mmap.mmap(f.fileno(),0,access=mmap.ACCESS_READ) as data:
  require(data[:8] in (b'BTWTP002',b'BTWTP003'),'native tape magic');pos=8;previous=-1
  while pos<len(data):
   h=HEADER.unpack_from(data,pos);require(h[0]>previous,'native monotonic');previous=h[0];yield h;pos+=HEADER.size+h[5]*24
  require(pos==len(data),'native exact framing')

def arrays_equal(saved,expected,name):
 a=saved[name];require(a.shape==expected.shape,'shape '+name)
 if np.issubdtype(expected.dtype,np.floating):require(np.allclose(a,expected,rtol=1e-12,atol=1e-12,equal_nan=True),'independent '+name)
 else:require(np.array_equal(a,expected),'independent '+name)

def audit(root,day):
 protocol=read(root/'protocol.json');manifest=read(root/'input-manifest.json');spec=manifest['days'][day]
 require(protocol['volatility_entry_filters_enabled'] is False,'V/R disabled')
 indexpath=Path(spec['feature_evidence']['feature_index']['path']);index=read(indexpath)
 require(sha(indexpath)==spec['feature_evidence']['feature_index']['sha256'],'frozen feature index')
 require(index['protocol_sha256']==sha(root/'protocol.json') and index['day']==day,'index identity')
 bookpath=root/'book-events'/day/'events.bin';actpath=Path(spec['files']['activity']['path']).parent/'activity-events.bin'
 for path,magic,dtype in ((bookpath,b'BTWBOOK1',BOOK),(actpath,b'BTWAES01',ACT)):
  with path.open('rb') as f:require(f.read(8)==magic,'source magic')
  require((path.stat().st_size-8)%dtype.itemsize==0,'source exact framing')
 book=np.memmap(bookpath,dtype=BOOK,offset=8,mode='r');activity=np.memmap(actpath,dtype=ACT,offset=8,mode='r')
 bookrates=Rates(book);traderates=Rates(activity);resets=book['ns'][book['kind']!=2]
 tape=Path(spec['files']['tape']['path']);gate=Path(spec['files']['entry_gates']['path']);iterator=native_rows(tape)
 require(sha(tape)==spec['files']['tape']['sha256'] and sha(gate)==spec['files']['entry_gates']['sha256'],'frozen native sources')
 source_hashes={str(bookpath):sha(bookpath),str(actpath):sha(actpath),str(indexpath):sha(indexpath)}
 for key,item in index['sources'].items():
  require(sha(item['path'])==item['sha256'],'source binding '+key);source_hashes[item['path']]=item['sha256']
 require(source_hashes[str(bookpath)]==spec['files']['book_events']['sha256'],'book manifest binding')
 require(source_hashes[str(actpath)]==spec['files']['activity_events']['sha256'],'trade manifest binding')
 rawpath=bookpath.parent/'independent-raw-audit.json';raw=read(rawpath)
 require(raw['status']=='PASS' and raw['book_events_sha256']==sha(bookpath) and raw['source_tape_sha256']==sha(tape),'independent raw acceptance')
 source_hashes[str(rawpath)]=sha(rawpath)
 state=(None,-1,None);resetpos=0;rows=0;counts=np.zeros(len(protocol['policies']),np.int64);feature_keys=set();first=True
 with gate.open('rb') as gf:
  require(gf.read(8)==b'BTWGATE1','gate magic')
  for chunk in index['chunks']:
   file=Path(chunk['path']);require(sha(file)==chunk['sha256'],'feature chunk hash');source_hashes[str(file)]=chunk['sha256']
   with np.load(file,allow_pickle=False) as saved:
    feature_keys.update(saved.files);ns=saved['ns'];n=len(ns);native=[]
    for i in range(n):native.append(next(iterator))
    require(np.array_equal(ns,np.array([h[0] for h in native],np.int64)),'chunk exact native clock')
    rates={'book':bookrates.query(ns),'trade':traderates.query(ns)}
    for group,values in rates.items():
     for key,value in values.items():arrays_equal(saved,value,group+'_'+key)
     arrays_equal(saved,values['rate_valid'].all(axis=1),group+'_valid')
    direction=np.full(n,-1,np.int8);durations=np.zeros(n,np.int64);resetflags=np.zeros(n,bool);flags=np.zeros(n,np.uint8);valid=np.array([h[1] for h in native],bool)
    for i,h in enumerate(native):
     now=int(ns[i]);newpos=int(np.searchsorted(resets,now,side='right'));reset=newpos>resetpos;resetpos=newpos;resetflags[i]=reset
     if h[1]:
      a,b,c,d=map(int,h[6:10]);require(a>=b>0 and c>=d>0,'native quotes valid')
      qualify=[20000*(d-a)>=30*(d+a),20000*(b-c)>=30*(b+c)];require(sum(qualify)<=1,'unique executable direction')
      if any(qualify):direction[i]=0 if qualify[0] else 1
     flags[i]=(0 if valid[i] else 1)|(2 if state[0] is not None and now-state[0]>100_000_000 else 0)|(4 if reset else 0)|(8 if direction[i]<0 or direction[i]!=state[1] else 0)
     state,durations[i]=persistence_step(state,now,bool(h[1]),int(direction[i]),reset)
    arrays_equal(saved,direction,'raw_direction');arrays_equal(saved,durations,'persistence_ns');arrays_equal(saved,resetflags,'book_reset_since_previous');arrays_equal(saved,valid,'native_valid')
    arrays_equal(saved,flags,'persistence_reset_flags')
    require(n==chunk['rows'] and int(ns[0])==chunk['first_ns'] and int(ns[-1])==chunk['last_ns'],'chunk index row identity')
    masks=np.ones(n,np.uint32)
    for bit,p in enumerate(protocol['policies']):
     allow=np.ones(n,bool)
     for group,required,threshold in [('book',p['require_book_rate'],p['min_book_rate_ratio']),('trade',p['require_trade_rate'],p['min_trade_rate_ratio'])]:
      if required:allow&=rates[group]['rate_valid'].all(axis=1)&(rates[group]['ratio']>=threshold).all(axis=1)
     if p['min_spread_persistence_ms']>0:allow&=durations>=p['min_spread_persistence_ms']*10**6
     masks[allow]|=np.uint32(1<<bit);counts[bit]+=int(allow.sum())
    arrays_equal(saved,masks,'allowed_mask')
    for i in range(n):require(GATE.unpack(gf.read(GATE.size))==(int(ns[i]),int(masks[i])),'binary native gate equals independent mask')
    rows+=n;first=False
  require(next(iterator,None) is None and gf.read(1)==b'','all native and gate rows consumed')
 require(rows==index['rows'],'index total rows')
 out={'status':'PASS','execution_enabled':False,'day':day,'auditor_sha256':sha(__file__),'protocol_sha256':sha(root/'protocol.json'),'input_manifest_sha256':sha(root/'input-manifest.json'),'feature_index_sha256':sha(indexpath),'tape_sha256':sha(tape),'gate_file_sha256':sha(gate),'source_sha256':source_hashes,'native_decisions_verified':rows,'policy_gate_counts':{p['id']:int(counts[i]) for i,p in enumerate(protocol['policies'])},'feature_keys_seen':sorted(feature_keys),'method':'No production builder import; independent per-venue event-index/prefix queries with exact reset order and receive-time windows; independent integer rawspread and native-time persistence; all native feature counts, validity, ratios, directions/durations and every14-bit mask verified.','volatility_inputs_used':False}
 with (root/'features'/day/'independent-feature-audit.json').open('x') as f:json.dump(out,f,ensure_ascii=False,indent=2);f.write('\n')
 print(json.dumps(out),flush=True);return out

if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--root',required=True);p.add_argument('--day',required=True);a=p.parse_args();audit(Path(a.root),a.day)

"""Bounded unauthenticated monthly-free BTW raw downloads; public GET only.

Probe first/middle/last ten-minute slices, then resume the identical native
archive to a full day. Original gzip bytes, blank disconnect markers and exact
Tardis capture timestamps are preserved. Never reads environment credentials.
"""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone
import gzip,hashlib,json,threading,time
from pathlib import Path
import httpx

CHANNELS={'bitget-futures':['books','publicTrade'],'bybit':['orderbook.50','publicTrade']}
ALLOWED_DATES=('2026-09-01','2026-08-01','2026-07-01')

def sha(path):
 h=hashlib.sha256()
 with Path(path).open('rb') as f:
  while data:=f.read(1048576):h.update(data)
 return h.hexdigest()
def utc():return datetime.now(timezone.utc).isoformat()
def write(path,value):
 path=Path(path);tmp=path.with_name(path.name+'.tmp');tmp.write_text(json.dumps(value,indent=2,ensure_ascii=False)+'\n');tmp.replace(path)
def crc_info(path,date,offset):
 digest=hashlib.sha256();size=lines=0;first=last=None;counts=Counter();lower=date+'T'+f'{offset//60:02d}:{offset%60:02d}:00';next_minute=offset+10
 upper=(date+'T'+f'{next_minute//60:02d}:{next_minute%60:02d}:00') if next_minute<1440 else date+'T24:00:00'
 with gzip.open(path,'rb') as stream:
  for line in stream:
   digest.update(line);size+=len(line);lines+=1
   if not line.strip():counts['blank_disconnect']+=1;continue
   stamp,_,message=line.decode().rstrip('\r\n').partition(' ')
   if not lower<=stamp<upper:raise ValueError('native receiver timestamp outside requested slice')
   first=first or stamp;last=stamp
   if not message:counts['timestamp_disconnect']+=1;continue
   # Count message/channel identity once for sampling; full sequence audit is separate.
   native=json.loads(message);arg=native.get('arg',{});topic=native.get('topic',arg.get('topic'))
   if topic is not None:
    if topic not in ('orderbook.50.BTWUSDT','publicTrade.BTWUSDT','books','publicTrade'):raise ValueError('unexpected native channel')
    if arg and (arg.get('symbol')!='BTWUSDT' or arg.get('instType')!='usdt-futures'):raise ValueError('unexpected native instrument')
    counts[topic]+=1
 return {'gzip_crc':'PASS','body_sha256':digest.hexdigest(),'uncompressed_bytes':size,'line_count':lines,'first_local_timestamp':first,'last_local_timestamp':last,'sample_channel_messages':dict(counts)}


def collect(root,date,phase):
 out=root/'days'/date/'archive';out.mkdir(parents=True,exist_ok=True)
 for exchange in CHANNELS:(out/exchange).mkdir(exist_ok=True)
 config={'date':date,'symbol':'BTWUSDT','slice_minutes':10,'offsets':list(range(0,1440,10)), 'exchanges':CHANNELS,'concurrency':2,'max_requests_this_invocation':576,'max_saved_bytes_total':1500000000,'max_response_bytes':30000000,'max_requests_per_second':4,'api_key_used':False,'proxy':'http://127.0.0.1:7897','trust_env':False,'format':'gzip-compressed original Tardis timestamp-space-exchange-native-JSON lines; blank disconnect markers preserved','local_timestamp_meaning':'historical Tardis receiver timestamp as returned in original raw line; never regenerated from download time'}
 if (out/'config.json').exists():
  if json.loads((out/'config.json').read_text())!=config:raise ValueError('immutable config mismatch')
 else:write(out/'config.json',config)
 old=json.loads((out/'manifest.json').read_text()) if (out/'manifest.json').exists() else {}
 if old.get('state')=='DOWNLOADED_AWAITING_INDEPENDENT_AUDIT':raise ValueError('completed archive already exists; not modifying')
 started=time.monotonic();lock=threading.RLock();stop=threading.Event();last_request=0.;calls=0
 manifest={'state':'RUNNING','phase':phase,'started_utc':utc(),'collector_sha256':sha(__file__),'requests':old.get('requests',[]),'files':old.get('files',{}),'cache_verified':[],'errors':[],'requests_this_invocation':0,'saved_bytes_total':sum(p.stat().st_size for p in out.rglob('offset*.jsonl.gz')),'expected_complete_files':288,'complete_files':0}
 offsets=(0,720,1430) if phase=='probe' else config['offsets']
 def update():
  with lock:
   manifest.update(heartbeat_utc=utc(),elapsed_seconds=time.monotonic()-started,complete_files=len(manifest['files']))
   write(out/'manifest.json',manifest);write(out/'status.json',{k:manifest[k] for k in ('state','phase','heartbeat_utc','elapsed_seconds','requests_this_invocation','saved_bytes_total','complete_files','expected_complete_files')})
 def slot():
  nonlocal calls,last_request
  with lock:
   if stop.is_set():return False
   if calls>=config['max_requests_this_invocation']:raise ValueError('request cap')
   time.sleep(max(0,last_request+.251-time.monotonic()));last_request=time.monotonic();calls+=1;manifest['requests_this_invocation']=calls
   return True
 def worker(exchange,channels):
  with httpx.Client(proxy=config['proxy'],trust_env=False,timeout=60,follow_redirects=False) as client:
   for offset in offsets:
    if stop.is_set():break
    name=f'{exchange}/offset{offset:04d}.jsonl.gz';dest=out/name
    params={'from':date,'offset':offset,'sliceSize':10,'filters':json.dumps([{'channel':c,'symbols':['BTWUSDT']} for c in channels],separators=(',',':')),'compression':'gzip'}
    if dest.exists():
     record=manifest['files'].get(name)
     if not record or record.get('state')!='COMPLETE' or record['params']!=params or sha(dest)!=record['sha256']:raise ValueError('unverified existing cache '+name)
     crc_info(dest,date,offset)
     with lock:manifest['cache_verified'].append(name);update()
     continue
    if not slot():break
    rec={'exchange':exchange,'offset':offset,'slice_minutes':10,'params':params,'download_requested_utc':utc(),'download_times_are_not_historical_receive_timestamps':True};tmp=dest.with_suffix(dest.suffix+'.part')
    try:
     if tmp.exists():raise ValueError('unverified partial already exists')
     with client.stream('GET','https://api.tardis.dev/v1/data-feeds/'+exchange,params=params) as response:
      rec.update(url=str(response.url),status_code=response.status_code,headers=dict(response.headers))
      if response.status_code!=200:
       body=b''
       for chunk in response.iter_raw():
        body+=chunk[:65536-len(body)]
        if len(body)>=65536:break
       err=out/exchange/f'offset{offset:04d}.http{response.status_code}.error';err.write_bytes(body)
       rec.update(error_file=str(err.relative_to(out)),error_sha256=hashlib.sha256(body).hexdigest())
       raise ValueError('HTTP_'+str(response.status_code)+'; stopped without authorization workaround')
      if response.headers.get('x-slice-size')!='10':raise ValueError('unexpected slice size')
      count=0;digest=hashlib.sha256()
      with tmp.open('xb') as f:
       for chunk in response.iter_raw():
        count+=len(chunk)
        if count>config['max_response_bytes']:raise ValueError('response size cap')
        with lock:
         manifest['saved_bytes_total']+=len(chunk)
         if manifest['saved_bytes_total']>config['max_saved_bytes_total']:raise ValueError('day size cap')
        f.write(chunk);digest.update(chunk)
      if response.headers.get('content-length') is not None and int(response.headers['content-length'])!=count:raise ValueError('content-length mismatch')
      info=crc_info(tmp,date,offset)
      rec.update(info,bytes=count,sha256=digest.hexdigest(),download_completed_utc=utc(),state='COMPLETE',file=name)
      tmp.replace(dest)
      with lock:
       manifest['files'][name]=rec;manifest['requests'].append(rec);update()
       if len(manifest['files'])%20==0:print(json.dumps({'date':date,'complete_files':len(manifest['files']),'compressed_bytes':manifest['saved_bytes_total']}),flush=True)
    except Exception as exc:
     rec.update(state='FAILED',error=type(exc).__name__+': '+str(exc))
     if tmp.exists():rec.update(partial_file=str(tmp.relative_to(out)),partial_bytes=tmp.stat().st_size)
     with lock:manifest['requests'].append(rec);manifest['errors'].append(rec['error']);stop.set();update()
     break
 update()
 with ThreadPoolExecutor(max_workers=2) as pool:
  futures=[pool.submit(worker,e,c) for e,c in CHANNELS.items()]
  for future in futures:
   try:future.result()
   except Exception as exc:
    with lock:manifest['errors'].append(type(exc).__name__+': '+str(exc));stop.set();update()
 with lock:
  complete=len(manifest['files'])==288
  manifest['state']='PARTIAL_OR_FAILED' if manifest['errors'] else ('DOWNLOADED_AWAITING_INDEPENDENT_AUDIT' if complete else 'PROBED_SAMPLE_ONLY')
  manifest['finished_utc']=utc();update()
  result={'date':date,'state':manifest['state'],'archive':str(out),'files':len(manifest['files']),'bytes':manifest['saved_bytes_total'],'elapsed_seconds':manifest['elapsed_seconds'],'errors':manifest['errors'],'execution_enabled':False}
  if phase=='probe':
   result['estimated_full_day_compressed_bytes_from_three_windows']=manifest['saved_bytes_total']*48
   result['interpretation']='Three ten-minute windows per venue only; not full-day completeness or continuous capture proof.'
   write(out.parent/'availability-probe.json',result)
  print(json.dumps(result),flush=True)
 if manifest['errors']:raise RuntimeError('bounded public collection failed; preserved evidence')
 return result

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output-root',type=Path,required=True);p.add_argument('--dates',nargs='+',choices=ALLOWED_DATES,default=list(ALLOWED_DATES));p.add_argument('--phase',choices=['probe','download'],required=True);a=p.parse_args()
 for date in a.dates:collect(a.output_root.resolve(),date,a.phase)

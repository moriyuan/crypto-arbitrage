"""Independent raw-to-sidecar identity audit using the previously frozen archive adapter."""
import argparse,hashlib,json,mmap,struct,sys
from pathlib import Path
from collections import Counter
HERE=Path(__file__).resolve().parent
ADAPTER=HERE.parent/'20261012-btw-as-code/day_adapter.py'
sys.path.insert(0,str(ADAPTER.parent))
from day_adapter import preflight,iter_batches
ROW=struct.Struct('<qBB2xI')

def sha(p):
 with Path(p).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def read(p):return json.loads(Path(p).read_text())
def audit(root,day):
 folder=root/'book-events'/day;meta=read(folder/'metadata.json');protocol=read(root/'protocol.json');original=read(protocol['original_input_manifest']);spec=original['days'][day]
 assert meta['status']=='PASS' and meta['date']==day and meta['symbol']=='BTWUSDT'
 tape=Path(spec['files']['tape']['path']);tmeta=read(tape.parent/'tape-metadata.json');archive=Path(tmeta['source_archive'])
 assert sha(tape)==spec['files']['tape']['sha256'] and meta['tape']==spec['files']['tape'] and Path(meta['source_archive'])==archive
 startup=archive.parent/'startup-orphan-evidence.json' if tmeta.get('startup_exception') else None
 accepted=preflight(archive,expected_date=day,startup_evidence=startup)
 path=folder/'events.bin';assert sha(path)==meta['outputs']['events.bin']['sha256']
 counts=Counter();raw=Counter();rows=0
 with path.open('rb') as f,mmap.mmap(f.fileno(),0,access=mmap.ACCESS_READ) as source:
  assert source[:8]==b'BTWBOOK1';offset=8
  for batch in iter_batches(accepted):
   for event in batch.events:raw[event.venue+':'+event.kind]+=1
   for event in batch.reconstruction_events:
    if event.kind not in ('book','disconnect'):continue
    v=0 if event.venue=='bybit' else 1
    if event.kind=='disconnect':kind,count=1,0
    else:
     snapshot=(event.native.get('type',event.native.get('action'))=='snapshot');kind=0 if snapshot else 2
     payload=event.native['data'] if v==0 else event.native['data'][0]
     count=0 if snapshot else len(payload['a'])+len(payload['b'])
    record=(batch.ordering_ns,v,kind,count)
    assert ROW.unpack_from(source,offset)==record,(rows,record)
    offset+=ROW.size;rows+=1;counts[f'{event.venue}:kind{kind}']+=1;counts[event.venue+':delta_level_rows']+=count
  assert offset==len(source)
 assert rows==meta['outputs']['events.bin']['rows']
 result={'status':'PASS','execution_enabled':False,'day':day,'auditor_sha256':sha(__file__),'frozen_adapter_sha256':sha(ADAPTER),'metadata_sha256':sha(folder/'metadata.json'),'book_events_sha256':sha(path),'source_archive':str(archive),'source_evidence_sha256':dict(accepted.evidence_sha256),'source_tape_sha256':sha(tape),'all_sidecar_records_verified':rows,'counts':dict(counts),'raw_message_counts':dict(raw),'method':'Independently stream every native raw frame through previously accepted day adapter, then compare every accepted reconstruction event against sidecar clock/venue/snapshot-disconnect-delta kind/aggregate price-level rowcount. No extractor imports. Raw stream verifies gzip CRC and complete body hashes/line counts through frozen adapter.','limit':'Frozen archive sequence/startup/disconnect suppression semantics inherited; L2 update rowcount is not individual order arrival/cancel count.'}
 with (folder/'independent-raw-audit.json').open('x') as f:json.dump(result,f,ensure_ascii=False,indent=2);f.write('\n')
 print(json.dumps(result),flush=True);return result
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--root',required=True);p.add_argument('--day',required=True);a=p.parse_args();audit(Path(a.root),a.day)

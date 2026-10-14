"""Extract accepted native L2 update intensity events; public local data only.

This is a sidecar, not a reconstructed book or an order-arrival dataset. Existing
day_adapter preflight and reconstruction semantics remain authoritative: native
snapshot resets, untimed disconnect ordering and the exact Aug startup omission
are retained. Every original delta row, including removals, contributes once.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import struct
import sys
import time
import zlib

BASE=Path(__file__).resolve().parent.parent
ADAPTER_CODE=BASE/'20261012-btw-as-code'
sys.path.insert(0,str(ADAPTER_CODE))
import day_adapter
from day_inputs import Day, validate_stream_counts

MAGIC=b'BTWBOOK1'
ROW=struct.Struct('<qBB2xI')
VENUES=('bybit','bitget')
COUNT_KEYS=('snapshot_frames','disconnect_frames','delta_frames','delta_level_entries','zero_level_delta_frames','untimed_disconnect_frames')


def require(ok,message):
    if not ok:
        raise ValueError(message)


def sha(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f,'sha256').hexdigest()


def load(path):
    return json.loads(Path(path).read_text())


def write_json(path,value):
    path=Path(path);tmp=path.with_name(path.name+'.tmp')
    tmp.write_text(json.dumps(value,indent=2,ensure_ascii=False)+'\n');tmp.replace(path)


def delta_level_count(event):
    require(event.kind=='book' and not event.is_snapshot,'expected native delta')
    native=event.native
    if event.venue=='bybit':
        require(native.get('type')=='delta' and isinstance(native.get('data'),dict),'Bybit delta payload shape')
        parts=[native['data']]
    else:
        require(event.venue=='bitget' and native.get('action')=='update'
                and isinstance(native.get('data'),list) and len(native['data'])>0,'Bitget delta payload shape')
        parts=native['data']
    count=0
    for part in parts:
        require(isinstance(part,dict),'book data item must be object')
        for side in ('a','b'):
            rows=part.get(side)
            require(isinstance(rows,list),'native a/b price rows must be arrays')
            require(all(isinstance(r,list) and len(r)==2 for r in rows),'native price-level row shape')
            count+=len(rows)
    require(count<2**32,'native level count exceeds uint32')
    return count


def emit_batches(batches,stream,day=None,progress=None):
    raw=Counter();suppressed=Counter()
    accepted={venue:dict.fromkeys(COUNT_KEYS,0) for venue in VENUES}
    initialized=[False,False]
    previous=-1;first=last=None;rows=decision_batches=0
    crc=zlib.crc32(MAGIC);digest=hashlib.sha256(MAGIC)
    stream.write(MAGIC)
    for batch in batches:
        ns=batch.ordering_ns
        require(type(ns) is int and ns>previous,'native batch cursor must strictly increase')
        if day is not None:day.validate_ns(ns)
        previous=ns;first=ns if first is None else first;last=ns
        raw['batches']+=1
        reconstruction=batch.reconstruction_events
        identities={(e.venue,e.source_sequence) for e in reconstruction}
        for event in batch.events:
            require(event.venue in VENUES and event.ordering_ns==ns,'raw frame cursor/venue mismatch')
            v=event.venue
            raw['frames:'+v]+=1;raw['kind:'+v+':'+event.kind]+=1
            if event.kind in ('trade','historical_trade'):
                raw['trade_records:'+v+':'+event.kind]+=len(event.native['data'])
            if event.kind=='book':
                raw['book_'+('snapshot' if event.is_snapshot else 'delta')+':'+v]+=1
            if (v,event.source_sequence) not in identities:
                suppressed[v+':'+event.kind]+=1
        changed=False
        for event in reconstruction:
            if event.kind not in ('book','disconnect'):
                continue
            require(event.ordering_ns==ns,'reconstruction cursor mismatch')
            v=VENUES.index(event.venue);stats=accepted[event.venue]
            if event.kind=='disconnect':
                kind,count=1,0;initialized[v]=False
                stats['disconnect_frames']+=1
                stats['untimed_disconnect_frames']+=event.local_ns is None
            elif event.is_snapshot:
                require(event.local_ns==ns,'native snapshot timestamp mismatch')
                kind,count=0,0;initialized[v]=True
                stats['snapshot_frames']+=1
            else:
                require(event.local_ns==ns and initialized[v],'delta before recovery snapshot or wrong native timestamp')
                kind,count=2,delta_level_count(event)
                stats['delta_frames']+=1;stats['delta_level_entries']+=count
                stats['zero_level_delta_frames']+=count==0
            data=ROW.pack(ns,v,kind,count)
            stream.write(data);crc=zlib.crc32(data,crc);digest.update(data)
            rows+=1;changed=True
        decision_batches+=changed
        if progress and raw['batches']%250000==0:
            progress('EXTRACTING',{'native_batches':raw['batches'],'sidecar_rows':rows},ns)
    return {'raw_counts':dict(raw),'suppressed_counts':dict(suppressed),'accepted_counts':accepted,
            'rows':rows,'book_decision_batches':decision_batches,'first_ordering_ns':first,'last_ordering_ns':last,
            'output_crc32':f'{crc & 0xffffffff:08x}','output_sha256':digest.hexdigest()}


def validate_sidecar(path,expected):
    counts={v:Counter() for v in VENUES};initialized=[False,False]
    digest=hashlib.sha256();crc=0;previous=-1;rows=batches=0
    with Path(path).open('rb') as stream:
        magic=stream.read(8);require(magic==MAGIC,'sidecar magic')
        digest.update(magic);crc=zlib.crc32(magic)
        while data:=stream.read(ROW.size*65536):
            require(len(data)%ROW.size==0,'truncated sidecar row')
            digest.update(data);crc=zlib.crc32(data,crc)
            for ns,venue,kind,count in ROW.iter_unpack(data):
                require(ns>0 and ns>=previous and venue in (0,1) and kind in (0,1,2),'sidecar row clock/type')
                batches+=ns!=previous;previous=ns;rows+=1
                c=counts[VENUES[venue]]
                if kind==0:
                    require(count==0,'snapshot cannot inflate update count')
                    initialized[venue]=True;c['snapshot_frames']+=1
                elif kind==1:
                    require(count==0,'disconnect cannot inflate update count')
                    initialized[venue]=False;c['disconnect_frames']+=1
                else:
                    require(initialized[venue],'sidecar delta before snapshot')
                    c['delta_frames']+=1;c['delta_level_entries']+=count;c['zero_level_delta_frames']+=count==0
    require(rows==expected['rows'] and batches==expected['book_decision_batches'],'sidecar row/batch count mismatch')
    require(digest.hexdigest()==expected['output_sha256'] and f'{crc & 0xffffffff:08x}'==expected['output_crc32'],'sidecar digest/CRC mismatch')
    for venue in VENUES:
        for key in COUNT_KEYS:
            if key!='untimed_disconnect_frames':
                require(counts[venue][key]==expected['accepted_counts'][venue][key],'sidecar accepted count mismatch')
    return {'status':'PASS','rows':rows,'book_decision_batches':batches,'sha256':digest.hexdigest(),
            'crc32':f'{crc & 0xffffffff:08x}','scope':'Streamed binary re-read agrees with emitted counts, initialization ordering, SHA256 and CRC32; source gzip CRC/body hashes/line counts independently enforced by full native iterator.'}


def build(experiment,date):
    root=Path(experiment).resolve();day=Day(date)
    protocol_path=root/'protocol.json';protocol=load(protocol_path)
    require(protocol['execution_enabled'] is False and protocol['volatility_entry_filters_enabled'] is False,'unexpected experiment scope')
    require(date in [*protocol['fit_dates'],protocol['evaluation_date']],'date not predeclared')
    manifest_path=Path(protocol['original_input_manifest']);manifest=load(manifest_path)
    tape=Path(manifest['days'][date]['files']['tape']['path']);tape_meta_path=tape.with_name('tape-metadata.json')
    tape_meta=load(tape_meta_path)
    input_hashes={str(path):sha(path) for path in (protocol_path,manifest_path,tape_meta_path)}
    require(tape_meta['execution_enabled'] is False and tape_meta.get('date',date)==date,'tape scope/date')
    require(tape_meta['format'] in ('BTWTP002','BTWTP003') and not tape_meta.get('grid_only',False),'full native decision tape required')
    output=root/'book-events'/date
    output.mkdir(parents=True,exist_ok=False)
    started=time.monotonic()
    def progress(state,counts=None,cursor=None,error=None):
        value={'state':state,'execution_enabled':False,'date':date,'heartbeat_epoch':time.time(),
               'elapsed_seconds':time.monotonic()-started,'counts':counts or {},'cursor_ns':cursor,'error':error}
        write_json(output/'status.json',value)
        print(json.dumps(value),flush=True)
    progress('PREFLIGHT')
    source_code={str(Path(__file__).resolve()):sha(__file__),
                 str(ADAPTER_CODE/'day_adapter.py'):sha(ADAPTER_CODE/'day_adapter.py'),
                 str(ADAPTER_CODE/'day_inputs.py'):sha(ADAPTER_CODE/'day_inputs.py')}
    try:
        startup_path=None
        if tape_meta.get('startup_exception') is not None:
            startup_path=tape.parent.parent/'startup-orphan-evidence.json'
            require(load(startup_path)==tape_meta['startup_exception'],'startup exact evidence differs from frozen tape')
        archive=day_adapter.preflight(tape_meta['source_archive'],expected_date=date,startup_evidence=startup_path)
        require(archive.evidence_sha256==tape_meta['source_evidence_sha256'],'archive evidence differs from frozen tape')
        require(tape_meta['tape_sha256']==manifest['days'][date]['files']['tape']['sha256']==sha(tape),'frozen tape hash differs')
        for key,name in [('day_adapter_sha256','day_adapter.py'),('day_inputs_sha256','day_inputs.py')]:
            if key in tape_meta:
                require(tape_meta[key]==sha(ADAPTER_CODE/name),'frozen source module changed: '+name)
        partial=output/'events.bin.partial'
        with partial.open('xb') as stream:
            extracted=emit_batches(day_adapter.iter_batches(archive),stream,day,progress)
        counts=extracted['raw_counts']
        audited=validate_stream_counts(archive,counts,'tape')
        if 'source_count_audit' in tape_meta:
            require(audited==tape_meta['source_count_audit'],'source count audit differs from frozen tape')
        for key,value in audited['expected_source_counts'].items():
            require(tape_meta['counts'].get(key,0)==value,'source count differs from tape metadata: '+key)
        require(counts['batches']==tape_meta['counts']['batches'],'native batch count differs from tape')
        require(extracted['book_decision_batches']==tape_meta['counts']['decisions'],'book decision batch count differs from tape')
        progress('VERIFYING',{'sidecar_rows':extracted['rows']},extracted['last_ordering_ns'])
        checked=validate_sidecar(partial,extracted)
        for path,digest in source_code.items():
            require(sha(path)==digest,'source code changed during extraction')
        for path,digest in input_hashes.items():
            require(sha(path)==digest,'input evidence changed during extraction')
        final=output/'events.bin';partial.replace(final)
        source_files=[{'path':str(f.path),'venue':v,'sha256':f.sha256,'body_sha256':f.body_sha256,
                       'bytes':f.bytes,'line_count':f.line_count,'stream_gzip_crc':'PASS'}
                      for v,files in archive.files.items() for f in files]
        metadata={'status':'PASS','execution_enabled':False,'date':date,'symbol':archive.symbol,
                  'source_archive':str(archive.root),'source_evidence_sha256':archive.evidence_sha256,
                  'startup_exception':archive.startup_exception,'startup_evidence_path':str(startup_path) if startup_path else None,
                  'protocol_sha256':input_hashes[str(protocol_path)],'original_input_manifest':{'path':str(manifest_path),'sha256':input_hashes[str(manifest_path)]},
                  'tape':{'path':str(tape),'sha256':tape_meta['tape_sha256']},
                  'tape_metadata':{'path':str(tape_meta_path),'sha256':input_hashes[str(tape_meta_path)]},
                  'source_count_audit':audited,'raw_counts':counts,'accepted_counts':extracted['accepted_counts'],
                  'suppressed_counts':extracted['suppressed_counts'],'first_ordering_ns':extracted['first_ordering_ns'],
                  'last_ordering_ns':extracted['last_ordering_ns'],'source_files':source_files,'source_files_streamed':len(source_files),
                  'source_integrity':'All compressed hashes/sizes and evidence checked in preflight; every gzip read to EOF with CRC, exact line count and uncompressed body SHA checked by day_adapter.',
                  'format':{'magic':MAGIC.decode(),'row_struct':ROW.format,'row_bytes':ROW.size,
                            'fields':['ordering_ns','venue_0_bybit_1_bitget','kind_0_snapshot_1_disconnect_2_delta','native_delta_price_level_rows'],
                            'same_ns_order':'Exact day_adapter EventBatch.reconstruction_events sequence, including disconnect-first and pre-marker suppression; no further sorting.',
                            'delta_count':'All native a+b price-level rows, including quantity0; snapshots count0; zero-row deltas retained. Not unique levels, top50 net changes, or individual order counts.',
                            'untimed_disconnect':'Native local_ns remains absent; sidecar uses inherited separate ordering cursor, not invented receive timestamp.'},
                  'stream_validation':checked,'code_sha256':source_code,'elapsed_seconds':time.monotonic()-started,
                  'outputs':{'events.bin':{'path':str(final),'sha256':checked['sha256'],'crc32':checked['crc32'],
                                           'bytes':final.stat().st_size,'rows':checked['rows']}}}
        write_json(output/'metadata.json',metadata)
        progress('COMPLETED',{'sidecar_rows':checked['rows'],'source_files':len(source_files)},extracted['last_ordering_ns'])
        return metadata
    except BaseException as exc:
        progress('FAILED',error=f'{type(exc).__name__}: {exc}')
        raise


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment',required=True);parser.add_argument('--date',required=True)
    args=parser.parse_args();build(args.experiment,args.date)

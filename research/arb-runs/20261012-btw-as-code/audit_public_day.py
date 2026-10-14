"""Date-parameterized independent audit of a full native monthly-free archive.

Reuses the frozen original source-value/sequence auditor without changing it.
No network calls, order simulation, secret reads or raw-file changes.
"""
import argparse,hashlib,importlib.util,json,sys
from datetime import datetime,timezone
from pathlib import Path

BASE=Path('/Users/mori/code/crypto/state/arb-runs')
AUDITOR=BASE/'20261009-btw-tardis-audit.py'
CONTINUITY=BASE/'20261009-btw-tardis-continuity.py'
CRITICAL=('local_timestamp_regressions','invalid_source_record','empty_book','crossed_or_locked_book','bitget_broken_pseq_chain','bybit_nonincreasing_delta_u','bybit_nonincreasing_seq','bitget_nonincreasing_seq','conflicting_trade_id','service_reset_without_snapshot','book_updates_while_uninitialized')

def load_module(name,path):
 spec=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(spec);sys.modules[name]=m;spec.loader.exec_module(m);return m

def sha(path):
 h=hashlib.sha256()
 with path.open('rb') as f:
  while x:=f.read(1048576):h.update(x)
 return h.hexdigest()

def write_new(path,value):
 with path.open('x') as f:json.dump(value,f,indent=2,ensure_ascii=False,allow_nan=False);f.write('\n')

def audit(root):
 raw=load_module('frozen_raw_audit',AUDITOR);continuity=load_module('frozen_continuity_audit',CONTINUITY)
 root=root.resolve();config_path=root/'config.json';manifest_path=root/'manifest.json';status_path=root/'status.json'
 config=json.loads(config_path.read_text());manifest=json.loads(manifest_path.read_text());status=json.loads(status_path.read_text());date=config['date']
 if date not in ('2026-09-01','2026-08-01','2026-07-01'):raise ValueError('date outside predeclared additional sample')
 assert config['symbol']=='BTWUSDT' and config['slice_minutes']==10 and config['offsets']==list(range(0,1440,10))
 assert config['exchanges']=={'bitget-futures':['books','publicTrade'],'bybit':['orderbook.50','publicTrade']}
 assert config['api_key_used'] is False and config['trust_env'] is False
 assert manifest['state']==status['state']=='DOWNLOADED_AWAITING_INDEPENDENT_AUDIT'
 expected={f'{v}/offset{o:04d}.jsonl.gz' for v in config['exchanges'] for o in config['offsets']}
 actual={str(p.relative_to(root)) for v in config['exchanges'] for p in (root/v).glob('*.jsonl.gz')}
 assert actual==expected==set(manifest['files']) and manifest['complete_files']==manifest['expected_complete_files']==288
 assert not list(root.rglob('*.part')) and not list(root.rglob('*.partial'))
 start=raw.local_ns(date+'T00:00:00.0000000Z');results=[]
 for venue in ('bybit','bitget-futures'):
  worker=raw.Audit(venue,date)
  for offset in config['offsets']:
   name=f'{venue}/offset{offset:04d}.jsonl.gz';row=manifest['files'][name]
   assert row['state']=='COMPLETE' and row['status_code']==200 and row['file']==name and row['exchange']==venue and row['offset']==offset
   p=row['params'];assert p['from']==date and p['offset']==offset and p['sliceSize']==10 and p['compression']=='gzip'
   assert json.loads(p['filters'])==[{'channel':c,'symbols':['BTWUSDT']} for c in config['exchanges'][venue]]
   worker.file(root/name,row['sha256'],row['bytes'],row['body_sha256'],row['line_count'],(start+offset*60*10**9,start+(offset+10)*60*10**9))
   if offset%240==0:print(json.dumps({'date':date,'audit_progress':venue,'offset':offset,'records':worker.counts['lines'],'errors':len(worker.errors)}),flush=True)
  results.append(worker.result())
 coverage={'scope':'whole UTC day','full_day':True,'venues':2,'expected_files':288,'actual_files':288,'complete_file_set':True,'all_compressed_and_body_hashes_match_manifest':True,'all_fragment_local_timestamps_inside_requested_ranges':True,'config_sha256':sha(config_path),'status_sha256':sha(status_path),'total_compressed_bytes':sum(r['bytes'] for v in results for r in v['files'])}
 integrity=not any(v['examples'] or any(v['counts'].get(k,0) for k in CRITICAL) for v in results)
 output={'status':'PASS' if all(v['status']=='PASS' for v in results) else 'REVIEW','symbol':'BTWUSDT','date_utc':date,'execution_enabled':False,'created_at_utc':datetime.now(timezone.utc).isoformat(),'manifest_path':str(manifest_path),'manifest_sha256':sha(manifest_path),'script_sha256':sha(Path(__file__)),'frozen_auditor_sha256':sha(AUDITOR),'coverage':coverage,'venues':results,'data_semantics':'Live public trades plus L2 price-level incremental books; never L3 individual orders.','latency_semantics':'Native Tardis receipt clock preserved; not real order latency.','source_time_boundary_policy':'Keep prior-day engine timestamps in source snapshots; exact receiver timestamp and requested slice control inclusion.'}
 write_new(root/'audit.json',output)
 if not integrity:raise ValueError('raw structural audit failed; evidence preserved, no readiness fabricated')
 continuity.ROOT=root;rows=[continuity.scan('bybit'),continuity.scan('bitget-futures')]
 for venue in rows:
  for boundary in venue['disconnect_markers']:
   before=boundary.get('previous_book_local_timestamp');after=boundary.get('next_book_local_timestamp')
   boundary['book_resync_gap_ns']=raw.local_ns(after)-raw.local_ns(before) if before and after else None
   boundary['gap_semantics']='Gap between last observed book and recovery book; timestamp-free marker is not assigned a fabricated disconnect time.'
   if after is None and before:boundary['uncovered_tail_to_day_end_ns']=start+86400*10**9-raw.local_ns(before)
 whole_day=all(not r['disconnect_markers'] for r in rows)
 common={'created_at_utc':datetime.now(timezone.utc).isoformat(),'audit_sha256':sha(root/'audit.json'),'script_sha256':sha(Path(__file__)),'frozen_continuity_scanner_sha256':sha(CONTINUITY),'raw_integrity_status':'PASS','scope':'Full file set, compressed/body hashes, gzip CRC, exact receipt order, symbol/value checks, native sequence continuity and reconstructed books passed.','whole_day_uninterrupted_capture':whole_day,'direct_existing_load_archive_compatible':False,'reason':'Native Tardis data require segmented adapter; recorded disconnects/reset snapshots must invalidate and reconstruct books.','timestamp_policy':'Preserve raw local/source order including source-time trade inversions; no sorting or forward filling.','adapter_requirements':['Invalidate per-venue books at each disconnect; recover only from later snapshot.','Enforce fresh simultaneous pair quotes; unexecutable exits remain unresolved.','Require separately acquired causal historical top10 gate and actual funding schedule/history before strategy replay.'],'max_book_local_gap_ns':{v['venue']:v['max_book_local_gap_ns'] for v in results},'book_local_gaps_gt_5s':{v['venue']:v['counts'].get('book_local_gaps_gt_5s',0) for v in results},'venues':rows}
 write_new(root/'continuity-audit.json',common)
 ready={**common,'continuity_audit_sha256':sha(root/'continuity-audit.json'),'strategy_replay_ready':False,'raw_frames_modified':False,'historical_universe_and_funding':'Not evaluated in raw archive audit; pass separate date-matched evidence to downstream replay.','tail_policy':'No extrapolation or invented fills beyond the last valid source quote; terminal disconnect remains an explicit gap.'}
 write_new(root/'replay-readiness.json',ready)
 adapter=load_module('original_adapter_preflight',BASE/'20261012-btw-exit-optimization-code/tardis_adapter.py')
 adapter.preflight(root,expected_date=date)
 result={'status':'PASS_NATIVE_PREFLIGHT','date':date,'archive':str(root),'compressed_bytes':coverage['total_compressed_bytes'],'whole_day_uninterrupted_capture':whole_day,'venues':[{k:v[k] for k in ('venue','status','counts','max_book_local_gap_ns')} for v in results],'preflight':'Original unchanged adapter.preflight(expected_date=date) passed','execution_enabled':False}
 write_new(root.parent/'raw-acceptance.json',result)
 print(json.dumps(result,indent=2),flush=True)

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--archive',type=Path,required=True);args=p.parse_args();audit(args.archive)

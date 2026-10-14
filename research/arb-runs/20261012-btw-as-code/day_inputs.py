"""Date/evidence contracts for offline BTW daily tapes; no exchange clients."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path

DAY_MS=86400000
HISTORY_CONFIG={'window_days':20,'min_volatility_bps':'0','min_turnover_usdt':'0','top_n':10,'publication_delay_ms':60000}

def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
    return h.hexdigest()

def require(ok,message):
    if not ok:raise ValueError(message)

@dataclass(frozen=True)
class Day:
    text:str
    def __post_init__(self):
        parsed=date.fromisoformat(self.text)
        require(parsed.isoformat()==self.text,'date must be YYYY-MM-DD')
    @property
    def start_ns(self):
        return int(datetime.fromisoformat(self.text).replace(tzinfo=timezone.utc).timestamp())*10**9
    @property
    def end_ns(self):return self.start_ns+86400*10**9
    def force_day_end(self,ns):return ns>=self.end_ns-120*10**9
    def validate_ns(self,ns):require(self.start_ns<=ns<self.end_ns,'native cursor outside requested date')

class DailyHistory:
    """Read a independently audited daily rank bundle; never promote BTW into top10."""
    def __init__(self,directory,day):
        self.day=day;directory=Path(directory)
        audit=json.loads((directory/'independent-recalculation.json').read_text())
        require(audit['status']=='PASS','history independent audit failed')
        require('gate-input.json' in audit['output_hashes'],'history gate is not hash bound')
        for name,digest in audit['output_hashes'].items():
            require(sha(directory/name)==digest,'history evidence hash mismatch: '+name)
        self.path=directory/'gate-input.json';self.data=json.loads(self.path.read_text());self.digest=sha(self.path)
        require(self.data['config']==HISTORY_CONFIG,'unexpected history policy')
        require(self.data['pool_count']==420,'unexpected crypto ranking pool count')
        start=day.start_ns//10**6
        require(self.data['available_from_ms']==start+60000 and self.data['valid_until_exclusive_ms']==day.end_ns//10**6,'history bundle date mismatch')
        for label,end in [('btw',start),('first_60_seconds',start-DAY_MS)]:
            row=self.data[label]
            require(row.get('symbol','BTWUSDT')=='BTWUSDT','wrong history symbol')
            require(row['history_window_end_ms']==end and row['history_window_start_ms']==end-20*DAY_MS,'history window mismatch')
            if row['history_filter_passed']:
                require(type(row['history_rank']) is int and 1<=row['history_rank']<=10,'admitted history rank outside top10')
    def evaluate(self,symbol,now_ms):
        if symbol!='BTWUSDT' or not self.day.start_ns//10**6<=now_ms<self.day.end_ns//10**6:
            return {'history_filter_passed':False,'history_filter_reason':'outside_verified_history_scope'}
        return dict(self.data['btw'] if now_ms>=self.data['available_from_ms'] else self.data['first_60_seconds'])
    def metadata(self):
        return {'source':str(self.path),'sha256':self.digest,'date':self.day.text,'config':self.data['config'],
                'pool_count':self.data['pool_count'],'caveats':self.data['caveats'],
                'history_admitted_after_publication':self.data['btw']['history_filter_passed'],
                'history_rank_after_publication':self.data['btw']['history_rank']}

def validate_funding_day(directory,day):
    p=Path(directory)
    acceptance=json.loads((p/'acceptance.json').read_text())
    require(acceptance['status']=='PUBLIC_FUNDING_HISTORY_AND_CAUSAL_SCHEDULE_VERIFIED','funding audit failed')
    require(acceptance['date']==day.text and acceptance['symbol']=='BTWUSDT','funding acceptance date/symbol mismatch')
    for filename,key in [('causal-schedule.json','date'),('events.json','target_day_utc')]:
        d=json.loads((p/filename).read_text())
        require(d[key]==day.text and d['symbol']=='BTWUSDT','funding evidence date/symbol mismatch: '+filename)
    manifest=json.loads((p/'manifest.json').read_text())
    require(manifest['window_start_ms']==day.start_ns//10**6 and manifest['window_end_ms_exclusive']==day.end_ns//10**6,'funding manifest date mismatch')

def validate_stream_counts(archive,counts,layout):
    """Compare independently audited raw counts, never another day's magic totals."""
    p=Path(archive.root)/'audit.json'
    require(sha(p)==archive.evidence_sha256['audit.json'],'archive audit changed after preflight')
    audit=json.loads(p.read_text());checked={}
    for row in audit['venues']:
        v='bitget' if row['venue']=='bitget-futures' else row['venue'];c=row['counts']
        expectations={'frames:'+v:c['lines'], 'kind:'+v+':book':sum(c.get(k,0) for k in ('book_snapshot','book_delta','book_update')),
          'kind:'+v+':disconnect':c.get('empty_lines',0)+c.get('timestamp_only_disconnect_markers',0),
          'kind:'+v+':historical_trade':c.get('historical_trade_snapshot_messages',0),
          'kind:'+v+':trade':c.get('trade_messages',0)-c.get('historical_trade_snapshot_messages',0)}
        if layout=='activity':
            expectations.update({'live_records:'+v:c.get('live_trade_records',0),'historical_records:'+v:c.get('historical_snapshot_trade_records',0)})
        elif layout=='tape':
            expectations.update({'trade_records:'+v+':trade':c.get('live_trade_records',0),'trade_records:'+v+':historical_trade':c.get('historical_snapshot_trade_records',0)})
        else:raise ValueError('unknown counter layout')
        for key,expected in expectations.items():
            require(counts.get(key,0)==expected,'stream differs from source audit: '+key)
            checked[key]=expected
    return {'status':'PASS','expected_source_counts':checked,'audit_sha256':archive.evidence_sha256['audit.json']}

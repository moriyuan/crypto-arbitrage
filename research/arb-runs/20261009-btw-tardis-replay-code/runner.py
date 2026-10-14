"""Offline native-tick paper replay. No network, credentials or exchange orders.

Execution math is the project's existing PaperSession. Batching changes only
durability frequency; a failed batch rolls back to its last checkpoint and stops.
"""
from __future__ import annotations
import argparse,bisect,copy,csv,gzip,hashlib,json,time
from collections import Counter
from dataclasses import asdict
from decimal import Decimal as D
from pathlib import Path
from btc_cta.cross_arb.engine import Policy
from btc_cta.cross_arb.market import Book,Rules
from btc_cta.cross_arb.paper import PaperSession,_HEADERS,_json
from btc_cta.cross_arb.depth_sizing import DepthSizingConfig

BASE=Path('/Users/mori/code/crypto/state/arb-runs')
START_MS=1790812800000
END_MS=1790899200000

def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        while b:=f.read(1024*1024):h.update(b)
    return h.hexdigest()

def write_json(path,data):
    path=Path(path);tmp=path.with_suffix(path.suffix+'.tmp')
    tmp.write_text(json.dumps(data,default=str,ensure_ascii=False,indent=2)+'\n');tmp.replace(path)

class FrozenHistory:
    def __init__(self,directory):
        directory=Path(directory);audit=json.loads((directory/'independent-recalculation.json').read_text())
        if audit['status']!='PASS':raise ValueError('history audit failed')
        for name,digest in audit['output_hashes'].items():
            if sha(directory/name)!=digest:raise ValueError('history evidence hash mismatch:'+name)
        self.path=directory/'gate-input.json';self.data=json.loads(self.path.read_text())
        if self.data['config']!={'window_days':20,'min_volatility_bps':'0','min_turnover_usdt':'0','top_n':10,'publication_delay_ms':60000}:
            raise ValueError('unexpected history policy')
        self.digest=sha(self.path)
    def metadata(self):
        return {'source':str(self.path),'sha256':self.digest,'config':self.data['config'],
                'pool_count':self.data['pool_count'],'caveats':self.data['caveats']}
    def evaluate(self,symbol,now_ms):
        if symbol!='BTWUSDT' or not START_MS<=now_ms<self.data['valid_until_exclusive_ms']:
            return {'history_filter_passed':False,'history_filter_reason':'outside_verified_history_scope'}
        return dict(self.data['btw'] if now_ms>=self.data['available_from_ms'] else self.data['first_60_seconds'])

class PriceSide:
    def __init__(self,bid):
        self.sign=-1 if bid else 1;self.keys=[];self.quantities={};self.cached=None
    def apply(self,rows,snapshot=False):
        seen=set();changed=False
        for p,q in rows:
            p,q=D(p),D(q)
            if not p.is_finite() or not q.is_finite() or p<=0 or q<0 or (snapshot and q==0):raise ValueError('invalid level')
            if p in seen:raise ValueError('duplicate price')
            seen.add(p);key=p*self.sign;i=bisect.bisect_left(self.keys,key)
            exists=key in self.quantities
            if q==0:
                if exists:self.keys.pop(i);del self.quantities[key];changed|=i<50
            else:
                if not exists:self.keys.insert(i,key)
                changed|=i<50 and self.quantities.get(key)!=q
                self.quantities[key]=q
        if changed or self.cached is None:
            self.cached=tuple((k*self.sign,self.quantities[k]) for k in self.keys[:50])
    def levels(self):return self.cached

class FastBookState:
    """Keep all native levels; expose the same top 50 as existing _BookParser."""
    def __init__(self,venue):
        self.venue=venue;self.bid=PriceSide(True);self.ask=PriceSide(False)
        self.seq=None;self.u=None;self.source_ms=None;self.first_update=False
    def apply(self,payload,local_ns):
        bybit=self.venue=='bybit';data=payload['data'] if bybit else payload['data'][0]
        kind=payload['type'] if bybit else payload['action'];snapshot=kind=='snapshot'
        if kind not in ('snapshot','delta' if bybit else 'update'):raise ValueError('invalid book kind')
        if bybit:
            if payload['topic']!='orderbook.50.BTWUSDT' or data['s']!='BTWUSDT':raise ValueError('wrong book symbol')
        elif payload['arg']!={'instType':'usdt-futures','topic':'books','symbol':'BTWUSDT'}:
            raise ValueError('wrong Bitget book identity')
        seq=int(data['seq']);u=int(data['u']) if bybit else None
        source=int(payload['cts'] if bybit else data['ts'])
        if not snapshot:
            if self.seq is None:raise ValueError('delta before snapshot')
            if source<self.source_ms:raise ValueError('source time regression')
            if bybit:
                if seq<=self.seq or u<=self.u:raise ValueError('Bybit nonincreasing sequence')
            else:
                pseq=int(data['pseq'])
                if seq<=pseq or (not self.first_update and (pseq!=self.seq or seq<=self.seq)) or (self.first_update and not pseq<=self.seq<=seq):
                    raise ValueError('Bitget sequence chain broken')
        else:self.bid=PriceSide(True);self.ask=PriceSide(False)
        self.bid.apply(data['b'],snapshot);self.ask.apply(data['a'],snapshot)
        expected=50 if bybit else int(data.get('maxdepth',data.get('maxDepth',1000)))
        if len(self.bid.keys)>expected or len(self.ask.keys)>expected:raise ValueError('too many native levels')
        self.seq,self.u,self.source_ms=seq,u,source;self.first_update=snapshot
        return Book(self.venue,self.bid.levels(),self.ask.levels(),local_ns//1_000_000,source,0)

def precise_fresh(books,receive_ns,policy,now_ns):
    if set(books)!={'bybit','bitget'} or set(receive_ns)!={'bybit','bitget'}:return False
    for v,b in books.items():
        if not 0<=now_ns-receive_ns[v]<=policy.max_quote_age_ms*1_000_000:return False
        if not -500_000_000<=now_ns-b.exchange_ts_ms*1_000_000<=policy.max_quote_age_ms*1_000_000:return False
    return (abs(receive_ns['bybit']-receive_ns['bitget'])<=policy.max_quote_skew_ms*1_000_000
            and abs(books['bybit'].exchange_ts_ms-books['bitget'].exchange_ts_ms)<=policy.max_quote_skew_ms)

class BatchPaper(PaperSession):
    def __init__(self,*args,checkpoint_every=1000,**kwargs):
        super().__init__(*args,**kwargs);self.checkpoint_every=checkpoint_every
        self.diagnostics=Counter();self.max_gross=None;self.max_net=None;self.context={}
        self.checkpoint()
    def checkpoint(self):
        self.db.execute('INSERT OR REPLACE INTO latest_state(id,value) VALUES(1,?)',(_json(self.state),))
        self.db.commit();self.saved_state=copy.deepcopy(self.state)
        self.saved_diagnostics=self.diagnostics.copy();self.saved_max=(self.max_gross,self.max_net)
    def rollback_to_checkpoint(self):
        self.db.rollback();self.state=copy.deepcopy(self.saved_state)
        self.diagnostics=self.saved_diagnostics.copy();self.max_gross,self.max_net=self.saved_max
    def _append(self,table,row):
        # Exact local order and cursor are also present on records whose ledger time is ms.
        if hasattr(self,'context') and self.context:row={**row,**self.context}
        super()._append(table,row)
        if table=='signals' and hasattr(self,'diagnostics'):
            self.diagnostics['reason:'+row['reason']]+=1
            if row.get('net_edge_bps') is not None:
                gross,net=D(row['gross_edge_bps']),D(row['net_edge_bps'])
                self.max_gross=gross if self.max_gross is None else max(self.max_gross,gross)
                self.max_net=net if self.max_net is None else max(self.max_net,net)
                self.diagnostics['cost_threshold_passed']+=net>=self.policy.min_entry_net_bps
    def tick_offline(self,books,rules,now_ms,force_exit_reason=None):
        if self.closed:raise RuntimeError('session closed')
        if type(now_ms) is not int or now_ms<=0 or (self.state['last_tick_ms'] is not None and now_ms<self.state['last_tick_ms']):raise ValueError('invalid replay clock')
        try:
            self._tick(books,rules,now_ms,force_exit_reason)
            if self.state['ticks']%self.checkpoint_every==0:self.checkpoint()
        except BaseException:
            self.rollback_to_checkpoint()
            raise

class HistoricalFunding:
    def __init__(self,directory):
        directory=Path(directory);self.directory=directory
        acceptance=json.loads((directory/'acceptance.json').read_text())
        if acceptance['status']!='PUBLIC_FUNDING_HISTORY_AND_CAUSAL_SCHEDULE_VERIFIED':raise ValueError('funding evidence incomplete')
        for manifest in ('manifest.json','schedule-manifest.json'):
            for row in json.loads((directory/manifest).read_text())['requests']:
                if row['status_code']!=200 or sha(directory/row['file'])!=row['sha256']:raise ValueError('funding response hash mismatch')
        self.observations=json.loads((directory/'causal-schedule.json').read_text())['observations']
        self.observations.sort(key=lambda x:x['known_at_ns']);self.index=0;self.known={};self.last_ns=None
        self.events=json.loads((directory/'events.json').read_text())['venues']
        self.evidence={p.name:sha(p) for p in directory.glob('*.json')}
    def decision(self,now_ns,holding_s):
        if self.last_ns is not None and now_ns<self.last_ns:raise ValueError('funding clock regression')
        self.last_ns=now_ns
        while self.index<len(self.observations) and self.observations[self.index]['known_at_ns']<=now_ns:
            row=self.observations[self.index];self.known[row['venue']]=row;self.index+=1
        for venue in ('bybit','bitget'):
            row=self.known.get(venue)
            if not row:return 'funding_guard:schedule_not_yet_observed:'+venue
            remaining=row['next_funding_timestamp_ms']*1_000_000-now_ns
            if remaining<=0:return 'funding_guard:schedule_expired:'+venue
            if remaining<=(holding_s+60)*1_000_000_000:return 'funding_guard:settlement_approaching:'+venue
        return None
    def audit_positions(self,session):
        orders=[json.loads(x[0]) for x in session.db.execute('SELECT value FROM orders ORDER BY id')]
        positions={};crossings=[]
        for row in orders:
            key=(row['cycle_id'],row['venue'])
            if row['intent']=='entry':positions[key]={'opened':int(row.get('decision_cursor_ns',row['timestamp_ms']*1_000_000)),'qty':D(row['qty'])}
            else:
                pos=positions[key];closed=int(row.get('decision_cursor_ns',row['timestamp_ms']*1_000_000))
                for event in self.events[row['venue']]['selected_events']:
                    if pos['opened']<=event['timestamp_ms']*1_000_000<=closed:
                        crossings.append({'cycle_id':key[0],'venue':key[1],'exit_order_id':row['order_id'],'qty':row['qty'],'settlement':event})
                pos['qty']-=D(row['qty'])
        unresolved=[{'cycle_id':c,'venue':v,'remaining_qty':str(p['qty'])} for (c,v),p in positions.items() if p['qty']]
        return {'status':'UNVERIFIED_SETTLEMENT_EXPOSURE' if crossings or unresolved else 'VERIFIED_NO_SETTLEMENT',
                'crossings':crossings,'unresolved':unresolved,'funding_usdt':'0' if not crossings and not unresolved else None,
                'basis':'Each filled leg opening-to-partial-closing interval checked inclusively against public settled timestamps; historical rates never used for entry.',
                'evidence_directory':str(self.directory),'evidence_sha256':self.evidence}

def export(session,output,progress=None):
    output=Path(output);counts={}
    extra=('decision_cursor_ns','bybit_receive_ns','bitget_receive_ns','precise_quote_rejected')
    for table,headers in _HEADERS.items():
        compressed=table in ('signals','equity');path=output/(table+'.csv'+('.gz' if compressed else ''))
        opener=(lambda:gzip.open(path,'wt',encoding='utf-8',newline='',compresslevel=1)) if compressed else (lambda:path.open('w',encoding='utf-8',newline=''))
        n=0
        with opener() as f:
            writer=csv.DictWriter(f,fieldnames=(*headers,*extra),extrasaction='ignore');writer.writeheader()
            for (value,) in session.db.execute(f'SELECT value FROM {table} ORDER BY id'):
                writer.writerow(json.loads(value));n+=1
                if progress and n%50000==0:progress()
        counts[table]=n
    return counts

def run(output,*,benchmark_batches=None):
    from tardis_adapter import preflight,iter_batches
    output=Path(output);output.mkdir(parents=True,exist_ok=False)
    started=time.time();clock=time.monotonic();counts=Counter();last_update=clock
    archive=preflight(BASE/'20261009-btw-tardis-20261001')
    gate=FrozenHistory(BASE/'20261009-btw-oct1-history')
    funding=HistoricalFunding(BASE/'20261009-btw-oct1-funding')
    policy=Policy(bybit_fee_bps=D(3),bitget_fee_bps=D(3),max_cycles=10000)
    rules={'bybit':Rules(D(10),D(10),D(5),D('.00001')),
           'bitget':Rules(D(1),D(1),D(5),D('.00001'))}
    from btc_cta.cross_arb import paper
    imported_directory=Path(paper.__file__).parent
    metadata={'symbol':'BTWUSDT','mode':'offline_native_tick_depth_limited_paper',
        'execution_enabled':False,'prediction_model_used':False,'requested_day_utc':'2026-10-01',
        'replay_start_ms':START_MS,'replay_end_exclusive_ms':END_MS,'policy':asdict(policy),
        'initial_balance_per_venue':'1000','depth_sizing':asdict(DepthSizingConfig()),
        'short_term_volatility_gate':False,'minimum_historical_turnover':'0','history_gate':gate.metadata(),
        'rules':{v:asdict(r) for v,r in rules.items()},'rules_evidence':str(funding.directory/'contract-rules-evidence.json'),
        'rules_quantity_constraints_status':'OCT9_SNAPSHOT_SCENARIO_ASSUMPTION_NOT_PROVEN_OCT1',
        'source_archive':str(archive.root),'source_evidence_sha256':dict(archive.evidence_sha256),
        'source_code_sha256':{str(p):sha(p) for p in imported_directory.glob('*.py')},
        'runner_sha256':sha(Path(__file__)),'adapter_sha256':sha(Path(__file__).with_name('tardis_adapter.py')),
        'decision_clock':'Native local receive ns order. Same-ns batches applied together; ledger ms is floor but exact ns retained and checked before any fill.',
        'decision_events':'Each batch containing a book update or disconnect. Trade-only batches parsed and counted; decision clock advances at book updates, never trade price as executable quote.',
        'execution_assumption':'Both legs fill simultaneously at VWAP of available 25% per-level participation budgets within 10bp of touch using the first 50 levels per venue, cap 250 USDT/leg. Partial paired exits allowed; no network delay or unhedged leg/queue model.',
        'day_end_entry_cutoff_ms':END_MS-120000,'checkpoint_every_decisions':1000,
        'benchmark_batches':benchmark_batches,'started_wall_utc_epoch':started}
    write_json(output/'run_metadata.json',metadata)
    session=BatchPaper(output/'ledger.sqlite',policy,symbol='BTWUSDT',history_gate=gate,depth_sizing=DepthSizingConfig())
    parsers={v:FastBookState(v) for v in rules};books={};receive_ns={};references={};now_ns=None
    status='RUNNING';failure=None
    def progress(force=False):
        nonlocal last_update
        if force or time.monotonic()-last_update>=15:
            row={'status':status,'wall_elapsed_seconds':time.monotonic()-clock,'raw_cursor_read_ns':now_ns,
                 'last_persisted_tick_ms':session.saved_state['last_tick_ms'],
                 'raw_batches':counts['batches'],'raw_frames':sum(counts['frames:'+v] for v in rules),
                 'decisions':session.state['ticks'],'orders':session.state['orders'],'closed_cycles':session.state['completed_cycles'],
                 'open_cycle':session.state['cycle']['cycle_id'] if session.state['cycle'] else None,
                 'cash':session.state['cash'],'closed_pair_net_before_funding':session.state['closed_pair_net_before_funding'],
                 'heartbeat_wall_epoch':time.time(),'error':failure}
            write_json(output/'status.json',row);print(json.dumps(row),flush=True);last_update=time.monotonic()
    progress(True)
    try:
        for batch in iter_batches(archive):
            now_ns=batch.ordering_ns;counts['batches']+=1
            for event in batch.events:
                counts['frames:'+event.venue]+=1;counts['kind:'+event.venue+':'+event.kind]+=1
                if event.kind in ('trade','historical_trade'):
                    counts['trade_records:'+event.venue+':'+event.kind]+=len(event.native['data'])
            changed=False
            for event in batch.reconstruction_events:
                venue=event.venue
                if event.kind=='disconnect':
                    changed=True;books.pop(venue,None);receive_ns.pop(venue,None);references.pop(venue,None)
                    parsers[venue]=FastBookState(venue)
                    session._append('events',{'kind':'native_disconnect','venue':venue,'ordering_cursor_ns':now_ns,
                        'historical_receive_timestamp':None,'file':str(event.path),'line':event.line_number,
                        'open_cycle_id':session.state['cycle']['cycle_id'] if session.state['cycle'] else None})
                elif event.kind=='book':
                    changed=True;books[venue]=parsers[venue].apply(event.native,event.local_ns);receive_ns[venue]=event.local_ns
                    references[venue]={'file':str(event.path),'line':event.line_number,'local_ns':event.local_ns}
            if changed:
                guard_passed=precise_fresh(books,receive_ns,policy,now_ns)
                session.context={'decision_cursor_ns':now_ns,'bybit_receive_ns':receive_ns.get('bybit'),
                                 'bitget_receive_ns':receive_ns.get('bitget'),'precise_quote_rejected':not guard_passed}
                force=funding.decision(now_ns,policy.max_holding_s)
                if now_ns>=(END_MS-120000)*1_000_000:force='scheduled_day_end'
                session.tick_offline(books if guard_passed else None,rules,now_ns//1_000_000,force)
                counts['precise_quote_rejected']+=not guard_passed
            progress()
            if benchmark_batches and counts['batches']>=benchmark_batches:break
        session.checkpoint()
        status='BENCHMARK_ONLY' if benchmark_batches else 'FINALIZING'
        progress(True)
        if not benchmark_batches:
            funding_audit=funding.audit_positions(session);write_json(output/'funding-audit.json',funding_audit)
            status='EXPORTING';progress(True)
            exported=export(session,output,progress)
            status='UNRESOLVED' if session.state['cycle'] else 'COMPLETED'
            if funding_audit['status']!='VERIFIED_NO_SETTLEMENT':status='FUNDING_UNVERIFIED'
            result=session.summary();result.update(status=status,raw_counts=dict(counts),diagnostics=dict(session.diagnostics),
                max_gross_edge_bps=session.max_gross,max_net_edge_bps=session.max_net,
                funding_status=funding_audit['status'],exported_rows=exported,wall_elapsed_seconds=time.monotonic()-clock,
                virtual_initial_total_usdt='2000',net_after_funding=result['cash_change_before_funding'] if funding_audit['status']=='VERIFIED_NO_SETTLEMENT' else None)
            write_json(output/'summary.json',result)
            write_json(output/'output-manifest.json',{p.name:{'bytes':p.stat().st_size,'sha256':sha(p)} for p in output.iterdir() if p.is_file() and p.suffix not in ('.sqlite',) and '-wal' not in p.name and '-shm' not in p.name and p.name!='status.json'})
        else:write_json(output/'benchmark.json',{'batches':counts['batches'],'decisions':session.state['ticks'],'elapsed_seconds':time.monotonic()-clock,'not_a_strategy_result':True})
        progress(True)
    except BaseException as exc:
        session.rollback_to_checkpoint()
        failure=type(exc).__name__+': '+str(exc);status='FAILED';progress(True);raise
    finally:
        session.close()

if __name__=='__main__':
    cli=argparse.ArgumentParser();cli.add_argument('--output',type=Path,required=True);cli.add_argument('--benchmark-batches',type=int)
    args=cli.parse_args();run(args.output,benchmark_batches=args.benchmark_batches)

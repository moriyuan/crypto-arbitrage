"""Independently reconcile L1 opponent-best paper fills to native depth.

Reads only orders/latest_state and pair_opened events. Does not import PaperSession,
runner, exchange clients, signal tables or execution code. Budget arithmetic is
recomputed from native top-50 displayed-size changes and earlier ledger fills.
Only the current opposite top-1 level can execute. Both entry and exit prices
and limits must equal that level exactly; no deeper level or price guard.
"""
from __future__ import annotations
import argparse
import bisect
import hashlib
import json
import sqlite3
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path

from tardis_adapter import preflight, iter_batches

VENUES = ('bybit', 'bitget')
TOL = D('1e-20')


class AuditFailure(ValueError):
    pass


def check(condition, detail):
    if not condition:
        raise AuditFailure(detail)


def serialized(value):
    return json.dumps(value, default=str, sort_keys=True, separators=(',', ':'))


def digest_file(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        while data:=f.read(1024*1024):h.update(data)
    return h.hexdigest()


class IndependentSide:
    """Ascending Decimal price index, independent of runner's signed index."""
    def __init__(self, bid):
        self.bid=bid;self.prices=[];self.sizes={};self.top=None;self.top_prices=set()

    def apply(self, rows):
        changed=self.top is None;seen=set()
        boundary=self.top[-1][0] if self.top else None
        for raw_price,raw_quantity in rows:
            price,quantity=D(raw_price),D(raw_quantity)
            check(price.is_finite() and quantity.is_finite() and price>0 and quantity>=0,'Invalid native price/quantity')
            check(price not in seen,'Duplicate native price');seen.add(price)
            old=self.sizes.get(price)
            if quantity==0:
                if old is not None:
                    self.prices.pop(bisect.bisect_left(self.prices,price));del self.sizes[price]
                    changed|=price in self.top_prices
            elif old!=quantity:
                if old is None:bisect.insort(self.prices,price)
                self.sizes[price]=quantity
                changed|=(boundary is None or price in self.top_prices or len(self.prices)<=50 or
                          (price>=boundary if self.bid else price<=boundary))
        if changed:
            selected=self.prices[-50:][::-1] if self.bid else self.prices[:50]
            self.top=tuple((p,self.sizes[p]) for p in selected);self.top_prices=set(selected)


class IndependentBook:
    def __init__(self, venue):
        self.venue=venue;self.bid=IndependentSide(True);self.ask=IndependentSide(False)
        self.seq=self.u=None;self.first_update=False
        self.local_ns=self.engine_ms=self.publish_ms=None;self.provenance=None

    def apply(self,event):
        native=event.native;bybit=self.venue=='bybit'
        row=native['data'] if bybit else native['data'][0]
        snapshot=(native['type'] if bybit else native['action'])=='snapshot'
        seq=int(row['seq']);u=int(row['u']) if bybit else None
        engine=int(native['cts'] if bybit else row['ts'])
        if snapshot:self.bid,self.ask=IndependentSide(True),IndependentSide(False)
        else:
            check(self.seq is not None,'Incremental book without snapshot')
            check(engine>=self.engine_ms,'Native engine time regression')
            if bybit:check(seq>self.seq and u>self.u,'Bybit non-increasing sequence')
            else:
                pseq=int(row['pseq'])
                check(seq>pseq and (pseq<=self.seq<=seq if self.first_update else pseq==self.seq and seq>self.seq),'Bitget sequence-chain error')
        self.bid.apply(row['b']);self.ask.apply(row['a'])
        check(self.bid.top and self.ask.top and self.bid.top[0][0]<self.ask.top[0][0],'Empty/crossed native book')
        self.seq,self.u,self.first_update=seq,u,snapshot
        self.local_ns,self.engine_ms,self.publish_ms=event.local_ns,engine,int(native['ts'])
        self.provenance={'file':str(event.path),'line':event.line_number,'local_timestamp':event.local_timestamp,
                         'local_ns':event.local_ns,'engine_timestamp_ms':engine,'publish_timestamp_ms':self.publish_ms,
                         'sequence':seq,'update_id':u}


def independently_fresh(books,now_ns,policy):
    if set(books)!=set(VENUES):return False
    age=int(policy['max_quote_age_ms'])*1_000_000
    skew=int(policy['max_quote_skew_ms'])*1_000_000
    for book in books.values():
        if not 0<=now_ns-book.local_ns<=age:return False
        if not -500_000_000<=now_ns-book.engine_ms*1_000_000<=age:return False
    return (abs(books['bybit'].local_ns-books['bitget'].local_ns)<=skew and
            abs(books['bybit'].engine_ms-books['bitget'].engine_ms)*1_000_000<=skew)


class IndependentBudgets:
    def __init__(self, participation):
        self.participation=participation;self.levels={};self.last_observed={};self.observations=0

    def observe(self, books):
        for venue,book in books.items():
            for side,display in (('buy',book.ask.top),('sell',book.bid.top)):
                key=(venue,side)
                if self.last_observed.get(key) is display:continue
                old=self.levels.get(key,{})
                current={}
                for price,quantity in display:
                    previous_displayed,previous_remaining=old.get(price,(D(0),D(0)))
                    current[price]=(quantity,max(D(0),previous_remaining+(quantity-previous_displayed)*self.participation))
                self.levels[key]=current;self.last_observed[key]=display;self.observations+=1

    def quote_and_consume(self,venue,side,display,quantity):
        check(side in ('buy','sell') and quantity>0,'Invalid top1 fill request')
        check(bool(display),'No opposite best quote')
        best,displayed=display[0]
        eligible=[display[0]]
        level_state=self.levels[(venue,side)]
        previous_displayed,capacity=level_state[best]
        check(previous_displayed==displayed,'Top1 budget/display mismatch')
        check(quantity<=capacity,'Fill exceeds current opposite top1 remaining budget; deeper liquidity is forbidden')
        check(quantity<=displayed,'Fill exceeds visible top1 level')
        level_state[best]=(displayed,capacity-quantity)
        consumed=[{'price':str(best),'displayed_qty':str(displayed),'budget_before':str(capacity),
                   'fill_qty':str(quantity),'budget_after':str(capacity-quantity)}]
        return {'vwap':best,'exact_level_notional':quantity*best,'capacity':capacity,
                'best_price':best,'consumed_levels':consumed,'execution_depth_levels':1,
                'fingerprint':hashlib.sha256(serialized(eligible).encode()).hexdigest()}


def read_final_orders(root):
    status=json.loads((root/'status.json').read_text())
    check(status['status'] in ('COMPLETED','UNRESOLVED','FUNDING_UNVERIFIED'),'Replay not final; audit refuses running ledger')
    c=sqlite3.connect(f'file:{root / "ledger.sqlite"}?mode=ro',uri=True)
    try:
        c.execute('BEGIN')
        orders=[{'ledger_row_id':rowid,**json.loads(value)} for rowid,value in c.execute('SELECT id,value FROM orders ORDER BY id')]
        state=json.loads(c.execute('SELECT value FROM latest_state WHERE id=1').fetchone()[0])
        opened=[json.loads(value) for value, in c.execute("SELECT value FROM events WHERE json_extract(value,'$.kind')='pair_opened' ORDER BY id")]
        c.commit()
    finally:c.close()
    check(len(orders)==state['orders'],'Latest checkpoint/order count mismatch')
    return orders,status,state,opened


def validate_l1_configuration(metadata, state):
    check(metadata['execution_enabled'] is False and metadata['symbol']=='BTWUSDT','Wrong replay target')
    check(metadata.get('mode')=='offline_native_l1_taker_paper','Wrong L1 replay mode')
    expected={'quote_model':'counterparty_best','execution_book_levels':1,'budget_observation_levels':50}
    for key,value in expected.items():
        for label,record in (('metadata',metadata),('final state',state)):
            check(type(record.get(key)) is type(value) and record.get(key)==value,
                  f'Unexpected {label} {key}: {record.get(key)!r}')
    check(D(metadata['depth_sizing']['participation'])==D('.25'),'Unexpected participation policy')
    check(D(metadata['depth_sizing']['band_bps'])==D('10'),'Unexpected inherited sizing band')
    check(D(metadata['policy']['price_guard_bps'])==0,'L1 replay must use zero price guard')
    mode=metadata['policy'].get('risk_mode','absolute_net')
    check(mode in ('absolute_net','entry_deterioration'),'Unknown risk mode')
    expected={'take_profit':'remaining_pair_net_before_funding','stop_loss':'remaining_pair_net_before_funding'}
    if mode=='entry_deterioration':
        expected.update(stop_loss='remaining_pair_net_deterioration_from_entry_quotes',
                        hard_stop_loss='realized_plus_remaining_pair_net_before_funding')
    for record in (metadata,state):
        check(record.get('risk_mode','absolute_net')==mode,'Risk mode differs from run policy')
        check(record.get('risk_basis')=='remaining_long_entry_notional','Unexpected risk notional basis')
        check(record.get('risk_pnl_basis')==expected,'Risk PnL basis differs from run policy')
    check(state.get('policy')==metadata['policy'],'Final policy differs from run metadata')
    for key in ('take_profit_bps','stop_loss_bps','hard_stop_loss_bps'):
        if key=='hard_stop_loss_bps' and mode=='absolute_net' and key not in metadata['policy']:continue
        try:value=D(str(metadata['policy'][key]))
        except Exception as exc:raise AuditFailure('Invalid risk parameter: '+key) from exc
        check(value.is_finite() and value>0,'Invalid risk parameter: '+key)


def verify_entry_baseline(legs,event,books,policy):
    """Cross-check frozen baseline against independently reconstructed native BBO.

    No depth budget is consulted: the baseline is a contemporaneous quote-only
    reference. An initial hard breach is checked as evidence, never latched here.
    """
    check(len(legs)==2 and {o['venue'] for o in legs}==set(VENUES),'Baseline entry must have two venues')
    entry={o['venue']:o for o in legs}
    long_candidates=[v for v,o in entry.items() if o['position_side']=='long' and o['side']=='buy']
    check(len(long_candidates)==1,'Baseline requires one long entry')
    long=long_candidates[0];short=next(v for v in VENUES if v!=long)
    check(entry[short]['position_side']=='short' and entry[short]['side']=='sell','Baseline requires one short entry')
    now=int(entry[long]['decision_cursor_ns']);cid=entry[long]['cycle_id'];q=D(entry[long]['qty'])
    check(q.is_finite() and q>0 and D(entry[short]['qty'])==q,'Invalid baseline entry quantity')
    check(all(o['cycle_id']==cid and int(o['decision_cursor_ns'])==now and o['intent']=='entry' for o in legs),'Baseline entry cursor/cycle mismatch')
    check(event['kind']=='pair_opened' and int(event['decision_cursor_ns'])==now,'Baseline event cursor mismatch')
    check(int(event['timestamp_ms'])==now//1_000_000,'Baseline event ms mismatch')
    cycle=event['cycle'];check(cycle['cycle_id']==cid and cycle['long']==long and cycle['short']==short,'Baseline event direction/cycle mismatch')
    baseline=cycle['entry_risk_baseline']
    check(int(baseline['timestamp_ms'])==now//1_000_000,'Baseline was not frozen at entry')
    check(set(baseline['prices'])==set(VENUES),'Baseline requires both reverse quotes')
    check(baseline['mode']=='entry_l1_quote_only_no_exit_budget_required','Wrong entry baseline mode')
    check(baseline['entry_breach_action']=='preserve_entry_then_protect_at_next_risk_decision','Entry hard breach action changed')
    per_coin=D(0);reverse={}
    for venue in VENUES:
        book=books[venue];opening=D(entry[venue]['price'])
        check(book.local_ns<=now and book.engine_ms*1_000_000<=now and book.publish_ms*1_000_000<=now,'Baseline used future source')
        native_entry=(book.ask.top if venue==long else book.bid.top)[0][0]
        check(opening==native_entry,'Recorded entry quote differs from native BBO')
        close=(book.bid.top if venue==long else book.ask.top)[0][0]
        check(D(baseline['prices'][venue])==close,'Frozen reverse quote differs from native entry BBO')
        fee=D(str(policy[venue+'_fee_bps']))/10000
        check(D(entry[venue]['fee_rate'])==fee,'Baseline fee rate differs from actual entry')
        per_coin+=(close-opening)*(1 if venue==long else -1)
        per_coin-=(opening+close)*fee
        reverse[venue]=str(close)
    initial_notional=q*D(entry[long]['price']);initial_net=q*per_coin
    for actual,recorded,label in [(q,baseline['initial_qty'],'initial quantity'),
        (initial_notional,baseline['initial_long_entry_notional'],'initial notional'),
        (per_coin,baseline['net_per_coin'],'net per coin'),(initial_net,baseline['initial_net_pnl'],'initial net')]:
        check(abs(actual-D(recorded))<=TOL,'Baseline '+label+' mismatch')
    breached=initial_net<=-initial_notional*D(str(policy['hard_stop_loss_bps']))/10000
    check(baseline['initial_hard_budget_breach'] is breached,'Initial hard breach flag mismatch')
    return {'status':'PASS','cycle_id':cid,'entry_cursor_ns':now,'long_venue':long,
            'initial_qty':str(q),'native_reverse_prices':reverse,'net_per_coin':str(per_coin),
            'initial_net_pnl':str(initial_net),'initial_long_entry_notional':str(initial_notional),
            'initial_hard_budget_breach':breached,'reverse_liquidity_required':False,
            'hard_breach_is_latched':False,'source_books':{v:books[v].provenance for v in VENUES}}


def audit(root):
    started=time.monotonic();root=Path(root).resolve();orders,status,final_state,opened=read_final_orders(root)
    metadata=json.loads((root/'run_metadata.json').read_text())
    validate_l1_configuration(metadata,final_state)
    code=Path(__file__).parent
    check(digest_file(code/'runner.py')==metadata['runner_sha256'],'Runner changed since replay start')
    check(digest_file(code/'tardis_adapter.py')==metadata['adapter_sha256'],'Adapter changed since replay start')
    check(digest_file(code/'paper_snapshot.py')==metadata['risk_snapshot_sha256'],'Paper snapshot changed since replay start')
    check(digest_file(code/'risk_policy.py')==metadata['risk_policy_sha256'],'Risk policy changed since replay start')
    archive=preflight(metadata['source_archive'])
    check(dict(archive.evidence_sha256)==metadata['source_evidence_sha256'],'Source evidence changed')
    grouped=defaultdict(list);positions={};previous=-1;pair_groups=[]
    for row in orders:
        cursor=int(row['decision_cursor_ns'])
        check(cursor>=previous,'Ledger decision cursor regressed');previous=cursor
        check(row['symbol']=='BTWUSDT' and row['pair']=='BTW/USDT:USDT' and row['status']=='FILLED','Wrong order target/status')
        grouped[cursor].append(row)
    for cursor,rows in grouped.items():
        groups=defaultdict(list)
        for row in rows:groups[(row['cycle_id'],row['intent'])].append(row)
        for (cycle,intent),legs in groups.items():
            check(len(legs)==2 and {x['venue'] for x in legs}==set(VENUES),'Fill is not a two-venue pair')
            check(len({D(x['qty']) for x in legs})==1 and {x['side'] for x in legs}=={'buy','sell'},'Unequal paired leg quantity/direction')
            quantity=D(legs[0]['qty']);check(quantity>0,'Nonpositive fill')
            if intent=='entry':
                check(cycle not in positions,'Duplicate cycle entry')
                positions[cycle]={'initial':quantity,'remaining':quantity,'exit_parts':0,
                                  'sides':{x['venue']:x['position_side'] for x in legs}}
                check(set(positions[cycle]['sides'].values())=={'long','short'},'Entry is not hedged')
            else:
                check(intent=='exit' and cycle in positions,'Exit without entry')
                check(quantity<=positions[cycle]['remaining'],'Exit exceeds remaining pair quantity')
                positions[cycle]['remaining']-=quantity;positions[cycle]['exit_parts']+=1
            for x in legs:
                position_side=positions[cycle]['sides'][x['venue']]
                check(x['position_side']==position_side,'Position side changed')
                expected_side='buy' if (position_side=='long')==(intent=='entry') else 'sell'
                check(x['side']==expected_side,'Wrong opening/closing side')
            pair_groups.append({'decision_cursor_ns':cursor,'cycle_id':cycle,'intent':intent,'qty':str(quantity)})
    relative=metadata['policy'].get('risk_mode','absolute_net')=='entry_deterioration'
    entry_events={}
    if relative:
        for event in opened:
            cid=event['cycle']['cycle_id'];check(cid not in entry_events,'Duplicate pair_opened baseline event')
            entry_events[cid]=event
        check(set(entry_events)==set(positions),'Missing/extra pair_opened baseline events')
    entry_evidence=[]
    last_cursor=max(grouped,default=None);books={};budgets=IndependentBudgets(D('0.25'))
    observations=[];counts=Counter();seen=set();last_progress=time.monotonic()
    if last_cursor is not None:
        for batch in iter_batches(archive):
            now=batch.ordering_ns
            if now>last_cursor:break
            counts['raw_batches']+=1;counts['raw_frames']+=len(batch.events)
            changed=False
            for event in batch.reconstruction_events:
                if event.kind=='disconnect':
                    changed=True;books.pop(event.venue,None);counts['disconnects_before_last_fill']+=1
                    # Budget memories deliberately survive native reconnection.
                elif event.kind=='book':
                    changed=True
                    if event.venue not in books:books[event.venue]=IndependentBook(event.venue)
                    books[event.venue].apply(event)
            fresh=changed and independently_fresh(books,now,metadata['policy'])
            if fresh:
                counts['fresh_budget_decisions']+=1;budgets.observe(books)
            elif changed:counts['rejected_stale_or_unpaired_decisions']+=1
            if now in grouped:
                check(changed and fresh,f'Orders occur without fresh paired book: {now}')
                seen.add(now)
                if relative:
                    current_entries=defaultdict(list)
                    for row in grouped[now]:
                        if row['intent']=='entry':current_entries[row['cycle_id']].append(row)
                    for cid,legs in current_entries.items():
                        entry_evidence.append(verify_entry_baseline(legs,entry_events[cid],books,metadata['policy']))
                for row in grouped[now]:
                    venue,side=row['venue'],row['side'];book=books[venue]
                    check(row['precise_quote_rejected'] is False,'Ledger says stale quote was filled')
                    for v in VENUES:
                        check(int(row[v+'_receive_ns'])==books[v].local_ns,'Recorded receive time differs from native source')
                        check(books[v].local_ns<=now and books[v].engine_ms*1_000_000<=now and books[v].publish_ms*1_000_000<=now,'Future native source used for fill')
                    check(int(row['book_exchange_ts_ms'])==book.engine_ms and int(row['book_received_at_ms'])==book.local_ns//1_000_000,'Recorded book clock differs')
                    check(int(row['timestamp_ms'])==now//1_000_000,'Decision ms/ns mismatch')
                    quantity,price=D(row['qty']),D(row['price']);rule=metadata['rules'][venue]
                    check(quantity%D(rule['qty_step'])==0 and quantity>=D(rule['min_qty']),'Order violates quantity scenario rule')
                    display=book.ask.top if side=='buy' else book.bid.top
                    evidence=budgets.quote_and_consume(venue,side,display,quantity)
                    check(price==evidence['best_price'],'Fill price is not exactly opposite top1: '+row['order_id'])
                    check(row.get('limit_price') not in ('',None),'Every L1 entry and exit requires an explicit top1 limit')
                    check(D(row['limit_price'])==evidence['best_price'],'Limit is not exactly opposite top1: '+row['order_id'])
                    check(len(evidence['consumed_levels'])==1,'L1 fill consumed deeper levels')
                    check(evidence['capacity']==D(row['depth_capacity_qty']),'Capacity differs from remaining budget: '+row['order_id'])
                    check(evidence['fingerprint']==row['depth_side_fingerprint'],'Source depth fingerprint differs: '+row['order_id'])
                    notional=D(row['notional']);check(abs(notional-quantity*price)<=TOL,'Order notional mismatch')
                    check(notional>=D(rule['min_notional']),'Below minimum notional scenario rule')
                    if row['intent']=='entry':
                        check(notional<=D(metadata['policy']['max_notional']),'Entry above 250USDT cap')
                        limit=D(row['limit_price'])
                        check(price<=limit if side=='buy' else price>=limit,'Entry fill violates limit')
                    check(D(row['fee_rate'])==D('0.0003') and abs(D(row['fee'])-notional*D('0.0003'))<=TOL,'Fee mismatch')
                    observations.append({'order_id':row['order_id'],'ledger_row_id':row['ledger_row_id'],
                        'cycle_id':row['cycle_id'],'venue':venue,'intent':row['intent'],'side':side,
                        'decision_cursor_ns':now,'qty':str(quantity),'ledger_vwap':str(price),
                        'independent_vwap':str(evidence['vwap']),'remaining_capacity_before':str(evidence['capacity']),
                        'best_price':str(evidence['best_price']),'limit_price':row['limit_price'],'execution_depth_levels':1,
                        'source_depth_fingerprint':evidence['fingerprint'],'consumed_levels':evidence['consumed_levels'],
                        'source_books':{v:books[v].provenance for v in VENUES},'status':'PASS'})
            if time.monotonic()-last_progress>15:
                print(serialized({'audit_progress_ns':now,'raw_batches':counts['raw_batches'],'orders_verified':len(observations)}),flush=True)
                last_progress=time.monotonic()
    check(seen==set(grouped),'Some order cursors not found in original raw stream')
    check(len(observations)==len(orders),'Not all orders independently verified')
    if relative:check(len(entry_evidence)==len(positions),'Not all entry baselines independently verified')
    return {'status':'PASS','created_at_utc':datetime.now(timezone.utc).isoformat(),'symbol':'BTWUSDT',
            'run_status':status['status'],'execution_enabled':False,'orders_verified':len(orders),
            'fill_pair_groups':len(pair_groups),'cycles':len(positions),'partial_exit_cycles':sum(p['exit_parts']>1 for p in positions.values()),
            'unclosed_remaining_qty':{k:str(v['remaining']) for k,v in positions.items() if v['remaining']},
            'source_scan':'Continuous from original UTC day start through last order cursor inclusive; no arbitrary-fragment restart.',
            'budget_rule':'25% budget observed and remembered across the full top50; size increases add 25%, decreases subtract 25% with zero floor. Execution consumes ONLY the current opposite top1 remaining budget. Top1 changes and reconnects do not erase remembered budgets. Observation only at paired precise-fresh book decisions.',
            'execution_price_rule':'Each entry and exit price == limit_price == current ask1 for buy / bid1 for sell exactly.',
            'mode':metadata['mode'],'quote_model':metadata['quote_model'],
            'execution_depth_levels':1,'execution_book_levels':1,'budget_observation_levels':50,
            'price_guard_bps':'0','participation':'0.25','price_numeric_tolerance':'0',
            'inherited_sizing_band_interpretation':'The recorded 10bp band is an inherited quantity safety allowance only; it never permits fills beyond opposite top1.',
            'notional_and_fee_numeric_tolerance':str(TOL),'metadata_depth_sizing_recorded':metadata['depth_sizing'],
            'fill_assumption_limitation':'Confirms the configured ideal depth-budget fills, not real latency, queue priority or actual market impact.',
            'last_fill_cursor_ns':last_cursor,'scan_counts':dict(counts),'budget_observations':budgets.observations,
            'elapsed_seconds':time.monotonic()-started,'orders_snapshot_sha256':hashlib.sha256(serialized(orders).encode()).hexdigest(),
            'run_metadata_sha256':digest_file(root/'run_metadata.json'),'auditor_sha256':digest_file(__file__),
            'risk_mode':metadata['policy'].get('risk_mode','absolute_net'),
            'entry_baseline_source_verification':'native_reconstructed_reverse_BBO_at_exact_entry_cursor' if relative else 'not_applicable_absolute_net',
            'entry_baselines_verified':len(entry_evidence),'entry_baselines':entry_evidence,
            'source_evidence_sha256':dict(archive.evidence_sha256),'orders':observations,'pair_groups':pair_groups}


def selftest():
    import copy
    import random
    names=[]
    config={'execution_enabled':False,'symbol':'BTWUSDT','mode':'offline_native_l1_taker_paper',
            'quote_model':'counterparty_best','execution_book_levels':1,'budget_observation_levels':50,
            'depth_sizing':{'participation':'.25','band_bps':'10'},
            'risk_basis':'remaining_long_entry_notional',
            'risk_pnl_basis':{'take_profit':'remaining_pair_net_before_funding','stop_loss':'remaining_pair_net_before_funding'},
            'policy':{'price_guard_bps':'0','take_profit_bps':'2','stop_loss_bps':'2'}}
    state={key:config[key] for key in ('quote_model','execution_book_levels','budget_observation_levels')}
    state.update({key:config[key] for key in ('risk_basis','risk_pnl_basis','policy')})
    validate_l1_configuration(config,state)
    names.append('declared_l1_metadata_and_state_agree')
    bad_fields=(('metadata','mode','offline_native_tick_depth_limited_paper'),
                ('metadata','quote_model','vwap'),('state','quote_model','vwap'),
                ('metadata','execution_book_levels',50),('state','execution_book_levels',50),
                ('metadata','execution_book_levels',True),
                ('metadata','budget_observation_levels',1),('state','budget_observation_levels',1),
                ('depth_sizing','participation','1'),('depth_sizing','band_bps','0'),
                ('policy','price_guard_bps','1'))
    for target,key,value in bad_fields:
        bad_config,bad_state=copy.deepcopy(config),copy.deepcopy(state)
        record=bad_state if target=='state' else bad_config if target=='metadata' else bad_config[target]
        record[key]=value
        try:validate_l1_configuration(bad_config,bad_state)
        except AuditFailure:pass
        else:raise AuditFailure(f'Invalid L1 configuration accepted: {target}.{key}')
    names.append('eleven_inconsistent_metadata_or_state_variants_rejected')
    # Spending is not replenished by an unchanged observation or a connection reset.
    class Book:
        pass
    class Side:
        pass
    b=Book();b.ask=Side();b.bid=Side();b.ask.top=((D(1),D(100)),);b.bid.top=((D('.9'),D(100)),)
    budget=IndependentBudgets(D('.25'));budget.observe({'bybit':b})
    q=budget.quote_and_consume('bybit','buy',b.ask.top,D(10));check(q['vwap']==1,'Selftest exact top1 price')
    names.append('level_budget_fill_equals_top1_exactly')
    budget.observe({'bybit':b});check(budget.levels[('bybit','buy')][D(1)][1]==15,'Same quote replenished spent depth')
    names.append('unchanged_quote_does_not_replenish')
    b.ask.top=tuple([(D(1),D(100))]);budget.observe({'bybit':b});check(budget.levels[('bybit','buy')][D(1)][1]==15,'Reconnect replenished spent depth')
    names.append('reconnect_same_display_does_not_replenish')
    b.ask.top=((D(1),D(140)),);budget.observe({'bybit':b});check(budget.levels[('bybit','buy')][D(1)][1]==25,'Increase budget wrong')
    names.append('only_displayed_increase_adds_participation')
    b.ask.top=((D(1),D(20)),);budget.observe({'bybit':b});check(budget.levels[('bybit','buy')][D(1)][1]==0,'Decrease zero-floor wrong')
    names.append('displayed_decrease_subtracts_with_zero_floor')
    for bid in (True,False):
        side=IndependentSide(bid);side.apply([[str(i),'10'] for i in range(1,101)])
        check(len(side.top)==50,'Wrong exposed depth')
        best=side.top[0][0];side.apply([[str(best),'0']]);check(len(side.top)==50 and side.top[0][0]!=best,'Deeper native level was not promoted')
    names.append('both_sides_promote_previously_known_deeper_level')
    randomizer=random.Random(20261001)
    for bid in (True,False):
        side=IndependentSide(bid);oracle={D(i):D(10) for i in range(1,1001)}
        side.apply([[str(p),str(q)] for p,q in oracle.items()])
        for _ in range(1000):
            price=D(randomizer.randrange(1,1100));quantity=D(randomizer.randrange(0,100))
            if quantity:oracle[price]=quantity
            else:oracle.pop(price,None)
            side.apply([[str(price),str(quantity)]])
            expected=tuple((p,oracle[p]) for p in sorted(oracle,reverse=bid)[:50])
            check(side.top==expected,'Cached independent top50 differs from full-sort oracle')
    names.append('2000_deterministic_top50_mutations_match_full_sort_oracle')
    test_budgets=IndependentBudgets(D('.25'))
    b.ask.top=((D('1'),D(40)),(D('1.001'),D(80)),(D('1.002'),D(400)))
    test_budgets.observe({'bybit':b})
    try:test_budgets.quote_and_consume('bybit','buy',b.ask.top,D(11))
    except AuditFailure:pass
    else:raise AuditFailure('Deeper levels incorrectly made an oversized L1 fill executable')
    evidence=test_budgets.quote_and_consume('bybit','buy',b.ask.top,D(10))
    check(evidence['capacity']==10 and evidence['vwap']==D('1') and len(evidence['consumed_levels'])==1,'Only top1 may fill')
    names.append('deeper_liquidity_never_increases_top1_capacity')
    # The old top1 stays in top50 while another price becomes touch, retaining
    # its consumed budget when it later becomes top1 again.
    switch_budget=IndependentBudgets(D('.25'))
    b.ask.top=((D('1'),D(100)),(D('1.001'),D(100)));switch_budget.observe({'bybit':b})
    switch_budget.quote_and_consume('bybit','buy',b.ask.top,D(10))
    b.ask.top=((D('.999'),D(100)),(D('1'),D(100)),(D('1.001'),D(100)));switch_budget.observe({'bybit':b})
    switch_budget.quote_and_consume('bybit','buy',b.ask.top,D(5))
    b.ask.top=((D('1'),D(100)),(D('1.001'),D(100)));switch_budget.observe({'bybit':b})
    evidence=switch_budget.quote_and_consume('bybit','buy',b.ask.top,D(15))
    check(evidence['capacity']==15,'Top1 switch erased consumed budget')
    names.append('top1_switch_does_not_reset_budget_while_level_remains_top50')
    sell_budget=IndependentBudgets(D('.25'));b.bid.top=((D('1.1'),D(40)),(D('1.09'),D(1000)))
    sell_budget.observe({'bybit':b});evidence=sell_budget.quote_and_consume('bybit','sell',b.bid.top,D(10))
    check(evidence['vwap']==D('1.1') and evidence['capacity']==10,'Sell did not use bid1 only')
    names.append('sell_executes_at_bid1_only')
    result={'status':'PASS','tests':names,'test_count':len(names),'random_top50_mutations':2000,
            'auditor_sha256':digest_file(__file__),'execution_enabled':False,
            'created_at_utc':datetime.now(timezone.utc).isoformat()}
    print(serialized(result))
    return result


if __name__=='__main__':
    cli=argparse.ArgumentParser();cli.add_argument('--run-dir',type=Path);cli.add_argument('--output',type=Path);cli.add_argument('--selftest',action='store_true')
    args=cli.parse_args()
    if args.selftest:selftest()
    else:
        check(args.run_dir is not None and args.output is not None,'Explicit run/output required')
        check(not args.output.exists(),'Refusing to overwrite prior audit')
        try:result=audit(args.run_dir)
        except Exception as exc:
            result={'status':'FAIL','error':type(exc).__name__+': '+str(exc),'created_at_utc':datetime.now(timezone.utc).isoformat(),
                    'execution_enabled':False,'auditor_sha256':digest_file(__file__)}
            with args.output.open('x') as f:json.dump(result,f,indent=2);f.write('\n')
            raise
        with args.output.open('x') as f:json.dump(result,f,indent=2,ensure_ascii=False,allow_nan=False);f.write('\n')
        print(serialized({k:v for k,v in result.items() if k not in ('orders','pair_groups','source_evidence_sha256')}))

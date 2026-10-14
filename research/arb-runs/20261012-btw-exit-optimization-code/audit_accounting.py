"""Independent read-only accounting/export/causality audit for BTW Oct1 L1 replay.

Never scans a running ledger. Preserves prior replay and refuses existing acceptance.
The policy and order limit checks enforce the L1 accounting contract; independent
full native-book reconstruction must verify that the limit really was ask1/bid1
and that cumulative 25% participation budgets were not reused.
"""
from pathlib import Path
from decimal import Decimal as D, localcontext
from datetime import datetime,timezone
from collections import defaultdict,Counter
from itertools import zip_longest
import argparse,bisect,calendar,csv,gzip,hashlib,json,sqlite3,time

START=1790812800000;END=1790899200000;TOL=D('1e-18')
VENUES=('bybit','bitget')
BASE=Path('/Users/mori/code/crypto/state/arb-runs')

EXPECTED_POLICY = {
 'max_notional':'250', 'bybit_fee_bps':'3', 'bitget_fee_bps':'3',
 'min_entry_net_bps':'5', 'price_guard_bps':'0',
 'max_total_loss_usdt':'10', 'max_holding_s':'60',
 'cooldown_s':'30', 'expected_exit_spread_bps':'2', 'funding_buffer_bps':'2',
 'max_quote_age_ms':'2000', 'max_quote_skew_ms':'500',
}

def l1_policy_errors(meta):
 errors=[]
 policy=meta.get('policy',{})
 for key,expected in EXPECTED_POLICY.items():
  try:valid=D(str(meta.get('policy',{}).get(key)))==D(expected)
  except Exception:valid=False
  if not valid:errors.append('policy:'+key)
 for label,value,expected in (
  ('initial_balance_per_venue',meta.get('initial_balance_per_venue'),'1000'),
  ('depth_participation',meta.get('depth_sizing',{}).get('participation'),'0.25'),
  ('execution_book_levels',meta.get('execution_book_levels',meta.get('depth_sizing',{}).get('max_levels')),'1'),
 ):
  try:valid=D(str(value))==D(expected)
  except Exception:valid=False
  if not valid:errors.append(label)
 if meta.get('symbol')!='BTWUSDT':errors.append('symbol')
 if meta.get('requested_day_utc')!='2026-10-01':errors.append('requested_day_utc')
 if meta.get('execution_enabled')is not False:errors.append('execution_enabled')
 if meta.get('prediction_model_used')is not False:errors.append('prediction_model_used')
 if 'stop_loss_usdt' in meta.get('policy',{}):errors.append('obsolete_policy:stop_loss_usdt')
 mode=policy.get('risk_mode','absolute_net')
 if mode not in ('absolute_net','entry_deterioration'):errors.append('policy:risk_mode')
 if meta.get('risk_mode','absolute_net')!=mode:errors.append('risk_mode')
 for key in ('take_profit_bps','stop_loss_bps','hard_stop_loss_bps'):
  if key=='hard_stop_loss_bps' and mode=='absolute_net' and key not in policy:continue
  try:
   value=D(str(policy.get(key)));valid=value.is_finite() and value>0
  except Exception:valid=False
  if not valid:errors.append('policy:'+key)
 if meta.get('risk_basis')!='remaining_long_entry_notional':errors.append('risk_basis')
 expected_basis={'take_profit':'remaining_pair_net_before_funding','stop_loss':'remaining_pair_net_before_funding'}
 if mode=='entry_deterioration':expected_basis.update(stop_loss='remaining_pair_net_deterioration_from_entry_quotes',hard_stop_loss='realized_plus_remaining_pair_net_before_funding')
 if meta.get('risk_pnl_basis')!=expected_basis:errors.append('risk_pnl_basis')
 return errors

def l1_order_errors(row, rules):
 errors=[]
 try:
  price=D(row['price']);limit=D(row['limit_price']);tick=D(rules[row['venue']]['price_tick'])
  if not all(x.is_finite() and x>0 for x in (price,limit,tick)):errors.append('nonpositive_nonfinite_price_limit_tick')
  else:
   if price!=limit:errors.append('filled_price_not_limit')
   if price%tick!=0 or limit%tick!=0:errors.append('price_or_limit_off_tick_grid')
 except Exception:errors.append('missing_or_invalid_price_limit_tick')
 return errors

def stamp_ns(text):
 t=text.rstrip('Z');whole,frac=t.split('.') if '.'in t else (t,'0')
 sec=calendar.timegm(datetime.strptime(whole,'%Y-%m-%dT%H:%M:%S').timetuple())
 return sec*10**9+int(frac.ljust(9,'0'))

def sha(path):
 h=hashlib.sha256()
 with path.open('rb') as f:
  for x in iter(lambda:f.read(1024*1024),b''):h.update(x)
 return h.hexdigest()

def main(path):
 began=time.monotonic();status=json.loads((path/'status.json').read_text())
 if status['status'] not in ('COMPLETED','UNRESOLVED','FAILED','FUNDING_UNVERIFIED'):
  print(json.dumps({'audit':'NOT_RUN_ACTIVE_REPLAY','status':status['status']}));return 3
 if (path/'acceptance.json').exists():raise RuntimeError('refuse_to_overwrite_existing_acceptance')
 fail=[];counts=Counter();warnings=[]
 def check(ok,label,detail=None):
  counts['checks']+=1
  if not ok:
   counts['failures']+=1
   if len(fail)<100:fail.append({'check':label,'detail':detail})
 def equal(a,b,label):check(abs(D(str(a))-D(str(b)))<=TOL,label,{'computed':str(a),'recorded':str(b)})
 db=sqlite3.connect('file:'+str(path/'ledger.sqlite')+'?mode=ro',uri=True);db.execute('PRAGMA query_only=ON')
 check(db.execute('PRAGMA quick_check').fetchall()==[('ok',)],'sqlite_quick_check')
 state=json.loads(db.execute('SELECT value FROM latest_state WHERE id=1').fetchone()[0])
 meta=json.loads((path/'run_metadata.json').read_text())
 summary=json.loads((path/'summary.json').read_text()) if (path/'summary.json').exists() else None
 if summary is None:
  result={'audit_status':'INCOMPLETE_FAILED_REPLAY','run_status':status['status'],'checks':dict(counts),'failures':fail,'last_state':state,'note':'Final exports/summary absent; ledger kept unchanged.'}
  with (path/'acceptance.json').open('x') as output:json.dump(result,output,indent=2)
  db.close();return 2
 check(meta['symbol']=='BTWUSDT' and not meta['execution_enabled'] and not meta['prediction_model_used'],'expected_scope')
 policy_errors=l1_policy_errors(meta)
 check(not policy_errors,'l1_run_policy',policy_errors)
 check(state.get('risk_basis')==meta.get('risk_basis'),'state_risk_basis_matches_metadata')
 check(state.get('risk_pnl_basis')==meta.get('risk_pnl_basis'),'state_risk_pnl_basis_matches_metadata')
 check(state.get('risk_mode','absolute_net')==meta.get('risk_mode','absolute_net'),'state_risk_mode_matches_metadata')
 check(state.get('policy')==meta.get('policy'),'state_policy_matches_metadata')
 check('stop_loss_usdt' not in state.get('policy',{}),'state_has_no_fixed_usdt_stop')
 max_quote_age_ns=int(meta['policy']['max_quote_age_ms'])*1000000
 max_quote_skew_ns=int(meta['policy']['max_quote_skew_ms'])*1000000
 settlement_buffer_ns=int((D(str(meta['policy']['max_holding_s']))+D(60))*D(1000000000))
 expected_rows={t:db.execute(f'SELECT count(*) FROM {t}').fetchone()[0]for t in ('signals','orders','cycles','equity','events')}
 for key in ['ticks','signal_evaluations','eligible_signals','orders','completed_cycles','cash','cycle','closed_pair_net_before_funding','initial_balance_per_venue']:
  check(summary[key]==state[key],'summary_matches_latest_state:'+key)
 for k,t in [('orders','orders'),('completed_cycles','cycles'),('signal_evaluations','signals')]:check(int(summary[k])==expected_rows[t],'summary_table_count:'+t)
 check(expected_rows['signals']==summary['ticks']*2,'two_direction_signals_per_decision')
 check(expected_rows['equity']==summary['ticks']*2,'two_equities_per_decision')
 orders=[json.loads(x[0])for x in db.execute('SELECT value FROM orders ORDER BY id')]
 cycles=[json.loads(x[0])for x in db.execute('SELECT value FROM cycles ORDER BY id')]
 by_cycle=defaultdict(list)
 for r in orders:by_cycle[r['cycle_id']].append(r)
 entry_signal_ids={r['signal_id']for r in orders if r['intent']=='entry'}
 entered_signals={};signal_metrics={'eligible':0,'selected':0,'entered':0,'cost_threshold_passed':0,'max_gross':None,'max_net':None};reasons=Counter()
 equity_metrics={'paired_ticks':0,'complete_liquidation_marks':0,'incomplete_liquidation_marks':0,'max_drawdown_usdt':D(0),'max_drawdown_pct_of_peak':D(0),'max_drawdown_pct_of_initial_2000':D(0)}
 eq_pending=[];peak=D(2000);cash_cursor={v:D(1000)for v in VENUES};positions={};order_i=0;last_eq_ns=-1;last_eq={}
 def apply_until(now):
  nonlocal order_i
  while order_i<len(orders) and int(orders[order_i]['decision_cursor_ns'])<=now:
   r=orders[order_i];v=r['venue'];key=(r['cycle_id'],v);q=D(r['qty']);pr=D(r['price']);fee=D(r['fee'])
   if r['intent']=='entry':positions[key]={'price':pr,'remaining':q,'sign':D(1)if r['position_side']=='long'else D(-1)};cash_cursor[v]-=fee
   else:
    p=positions[key];cash_cursor[v]+=p['sign']*q*(pr-p['price'])-fee;p['remaining']-=q
   order_i+=1

 for table in ('orders','cycles','signals','equity'):
  filename=table+'.csv'+('.gz'if table in ('signals','equity')else '')
  fpath=path/filename
  if not fpath.exists():check(False,'missing_export:'+filename);continue
  mismatch=0;n=0;headers=None;sentinel=object()
  opener=gzip.open if filename.endswith('.gz')else open
  with opener(fpath,'rt',newline='',encoding='utf-8')as f:
   reader=csv.DictReader(f);headers=reader.fieldnames
   check(headers is not None and len(headers)==len(set(headers)),'csv_unique_headers:'+table)
   for dbrow,csvrow in zip_longest(db.execute(f'SELECT value FROM {table} ORDER BY id'),reader,fillvalue=sentinel):
    n+=1
    if dbrow is sentinel or csvrow is sentinel:mismatch+=1;continue
    r=json.loads(dbrow[0])
    for k in headers:
     expected=''if r.get(k)is None else str(r[k])
     if csvrow.get(k)!=expected:
      mismatch+=1
      if mismatch<4:check(False,'csv_ledger_field:'+table,{'row':n,'field':k,'csv':csvrow.get(k),'ledger':expected})
    if table=='signals':
     reasons[r['reason']]+=1;signal_metrics['eligible']+=r['eligible'];signal_metrics['selected']+=r['selected'];signal_metrics['entered']+=r['action']=='ENTERED'
     if r.get('net_edge_bps')is not None:
      net=D(r['net_edge_bps']);gross=D(r['gross_edge_bps']);signal_metrics['cost_threshold_passed']+=net>=D(str(meta['policy']['min_entry_net_bps']))
      signal_metrics['max_gross']=gross if signal_metrics['max_gross']is None else max(gross,signal_metrics['max_gross']);signal_metrics['max_net']=net if signal_metrics['max_net']is None else max(net,signal_metrics['max_net'])
     if r['signal_id']in entry_signal_ids:entered_signals[r['signal_id']]=r
    elif table=='equity':
     now=int(r['decision_cursor_ns']);v=r['venue']
     check(now>=last_eq_ns,'equity_time_monotonic');last_eq_ns=now;apply_until(now)
     equal(r['cash_balance'],cash_cursor[v],'equity_cash_from_actual_orders')
     check(r['pair']=='BTW/USDT:USDT' and r['symbol']=='BTWUSDT','equity_symbol')
     if r.get('unrealized_gross_pnl')is not None:
      equal(D(r['cash_balance'])+D(r['unrealized_gross_pnl']),r['equity'],'marked_equity_arithmetic')
      equal(D(r['equity'])-D(r['estimated_exit_fee']),r['liquidation_equity_before_funding'],'liquidation_equity_arithmetic')
     equal(D(r['cash_balance'])-D(r['reserved_margin']),r['available_balance'],'available_collateral_arithmetic')
     eq_pending.append(r);last_eq[v]=r
     if len(eq_pending)==2:
      a,b=eq_pending;check(a['tick_id']==b['tick_id'] and {a['venue'],b['venue']}==set(VENUES) and a['decision_cursor_ns']==b['decision_cursor_ns'],'equity_pair_alignment')
      equity_metrics['paired_ticks']+=1
      if all(x['liquidation_equity_before_funding']is not None for x in eq_pending):
       value=sum((D(x['liquidation_equity_before_funding'])for x in eq_pending),D(0));peak=max(peak,value);dd=peak-value
       equity_metrics['complete_liquidation_marks']+=1;equity_metrics['max_drawdown_usdt']=max(equity_metrics['max_drawdown_usdt'],dd)
       equity_metrics['max_drawdown_pct_of_peak']=max(equity_metrics['max_drawdown_pct_of_peak'],100*dd/peak)
       equity_metrics['max_drawdown_pct_of_initial_2000']=max(equity_metrics['max_drawdown_pct_of_initial_2000'],100*dd/D(2000))
      else:equity_metrics['incomplete_liquidation_marks']+=1
      eq_pending=[]
  check(mismatch==0,'csv_all_rows_match:'+table,{'mismatches':mismatch});check(n==expected_rows[table],'csv_row_count:'+table)
  check(n==summary['exported_rows'][table],'summary_export_row_count:'+table)
  print(json.dumps({'audited_export':filename,'rows':n,'mismatches':mismatch}),flush=True)
 check(not eq_pending,'equity_no_unpaired_tail')
 check(signal_metrics['eligible']==summary['eligible_signals'],'eligible_signal_count')
 check(signal_metrics['entered']==len(by_cycle),'entered_signal_count')
 check(signal_metrics['cost_threshold_passed']==summary['diagnostics'].get('cost_threshold_passed',0),'cost_threshold_signal_count')
 for key in ('gross','net'):
  recorded=summary['max_'+key+'_edge_bps'];computed=signal_metrics['max_'+key]
  if recorded is None:check(computed is None,'max_edge_none:'+key)
  else:equal(computed,recorded,'maximum_edge:'+key)

 funding=json.loads((BASE/'20261009-btw-oct1-funding/events.json').read_text())['venues']
 schedule=json.loads((BASE/'20261009-btw-oct1-funding/causal-schedule.json').read_text())['observations']
 scheduled={v:sorted((x for x in schedule if x['venue']==v),key=lambda x:x['known_at_ns'])for v in VENUES}
 realized_net={v:D(0)for v in VENUES};calculated_cycles=[];residual=[];crossings=[];quote_needed=defaultdict(set);future_exchange_clocks=0
 max_hold_ns=0;all_order_ids=set();min_entry_settlement_distance_ns=None
 for cid,rows in by_cycle.items():
  entries={};exits=defaultdict(list);parts=defaultdict(list)
  for r in rows:
   check(r['order_id']not in all_order_ids,'unique_order_id');all_order_ids.add(r['order_id'])
   v=r['venue'];q=D(r['qty']);pr=D(r['price']);t=int(r['decision_cursor_ns']);fee=q*pr*D('.0003')
   check(r['symbol']=='BTWUSDT' and r['pair']=='BTW/USDT:USDT','order_symbol');check(v in VENUES,'order_venue')
   check(q>0 and q%D(10)==0,'common_quantity_grid10');check(pr>0,'positive_price')
   order_errors=l1_order_errors(r,meta['rules']);check(not order_errors,'l1_price_limit_and_tick',{'order_id':r['order_id'],'errors':order_errors})
   equal(r['fee_rate'],'.0003','order_fee_rate');equal(r['notional'],q*pr,'order_notional');equal(r['fee'],fee,'order_fee')
   check(r['leverage']=='1' and r['status']=='FILLED','order_status_leverage');check(int(r['timestamp_ms'])==t//1000000,'order_ms_floor_ns')
   check(not r['precise_quote_rejected'],'fill_quote_guard')
   for vv in VENUES:
    received=int(r[vv+'_receive_ns']);check(0<=t-received<=max_quote_age_ns,'order_quote_no_future_or_stale_ns')
   check(abs(int(r['bybit_receive_ns'])-int(r['bitget_receive_ns']))<=max_quote_skew_ns,'order_receive_skew')
   own_received=int(r[v+'_receive_ns']);source_ms=int(r['book_exchange_ts_ms']);check(int(r['book_received_at_ms'])==own_received//1000000,'order_book_receive_matches')
   source_age=t-source_ms*1000000;check(-500000000<=source_age<=max_quote_age_ns,'order_exchange_quote_age');future_exchange_clocks+=source_age<0
   quote_needed[v].add(own_received);check(q<=D(r['depth_capacity_qty']),'quantity_within_reported_depth')
   if r['intent']=='entry':
    check(v not in entries,'one_entry_per_venue');entries[v]=r;check(q*pr<=D(str(meta['policy']['max_notional'])),'entry_notional_cap');check(t>=(START+60000)*1000000,'entry_after_gate_publication')
    sig=entered_signals.get(r['signal_id']);check(sig is not None,'entry_signal_exists')
    if sig:
     check(sig['history_rank']==3 and sig['history_filter_passed']and sig['eligible']and sig['selected']and sig['action']=='ENTERED','entry_history_rank3_and_signal')
     check(int(sig['decision_cursor_ns'])==t and sig['cycle_id']==cid,'entry_signal_same_cursor_cycle')
    for vv in VENUES:
     eligible=[x for x in scheduled[vv]if x['known_at_ns']<=t];check(bool(eligible),'funding_schedule_known_at_entry')
     if eligible:
      last=eligible[-1];distance=last['next_funding_timestamp_ms']*1000000-t;check(distance>settlement_buffer_ns,'entry_gt120sec_before_known_settlement')
      min_entry_settlement_distance_ns=distance if min_entry_settlement_distance_ns is None else min(min_entry_settlement_distance_ns,distance)
   else:check(r['intent']=='exit','known_order_intent');exits[v].append(r);parts[t].append(r)
  check(set(entries)==set(VENUES),'paired_entry_venues')
  if len(entries)!=2:continue
  qa,qb=(D(entries[v]['qty'])for v in VENUES);check(qa==qb,'paired_entry_equal_qty');check(entries['bybit']['decision_cursor_ns']==entries['bitget']['decision_cursor_ns'],'paired_entry_same_ns')
  check({r['side']for r in entries.values()}=={'buy','sell'},'paired_entry_opposite_sides');check(abs(int(entries['bybit']['book_exchange_ts_ms'])-int(entries['bitget']['book_exchange_ts_ms']))*1000000<=max_quote_skew_ns,'paired_entry_exchange_skew')
  for t,pp in parts.items():
   check(len(pp)==2 and {x['venue']for x in pp}==set(VENUES),'each_exit_part_two_legs')
   if len(pp)==2:equal(pp[0]['qty'],pp[1]['qty'],'partial_exit_equal_qty');check(abs(int(pp[0]['book_exchange_ts_ms'])-int(pp[1]['book_exchange_ts_ms']))*1000000<=max_quote_skew_ns,'paired_exit_exchange_skew')
  legs={};gross_total=D(0);fees_total=D(0);end_ns=max(int(x['decision_cursor_ns'])for x in rows);start_ns=int(entries['bybit']['decision_cursor_ns']);max_hold_ns=max(max_hold_ns,end_ns-start_ns)
  for v in VENUES:
   en=entries[v];sgn=D(1)if en['side']=='buy'else D(-1);entry_price=D(en['price']);entry_qty=D(en['qty']);out_qty=D(0);gross=D(0);fees=D(en['fee']);exit_notional=D(0)
   for r in exits[v]:
    check(r['side']!=en['side']and r['position_side']==en['position_side'],'exit_closes_original_side');q=D(r['qty']);t=int(r['decision_cursor_ns']);check(t>=start_ns,'exit_after_entry')
    out_qty+=q;gross+=sgn*q*(D(r['price'])-entry_price);fees+=D(r['fee']);exit_notional+=q*D(r['price'])
    check(out_qty<=entry_qty,'no_overclose')
    for event in funding[v]['selected_events']:
     if start_ns<=event['timestamp_ms']*1000000<=t:crossings.append({'cycle_id':cid,'venue':v,'exit_order_id':r['order_id'],'settlement_ms':event['timestamp_ms'],'qty':str(q)})
   left=entry_qty-out_qty
   if left:residual.append({'cycle_id':cid,'venue':v,'qty':str(left),'entry_price':str(entry_price)})
   net=gross-fees;realized_net[v]+=net;gross_total+=gross;fees_total+=fees
   legs[v]={'entry_qty':str(entry_qty),'closed_qty':str(out_qty),'remaining_qty':str(left),'gross_pnl':str(gross),'fees':str(fees),'net_pnl':str(net),'weighted_exit_price':str(exit_notional/out_qty)if out_qty else None}
  recorded=next((c for c in cycles if c['cycle_id']==cid),None)
  if recorded:
   check(not any(D(x['remaining_qty'])for x in legs.values()),'closed_cycle_no_remaining')
   check(len(rows)==2+2*int(recorded['exit_parts']),'orders_equal_2_plus_2_exit_parts');check(len(parts)==int(recorded['exit_parts']),'exit_part_count')
   equal(recorded['gross_pnl'],gross_total,'cycle_gross_recomputed');equal(recorded['total_fees'],fees_total,'cycle_fees_recomputed');equal(recorded['net_before_funding'],gross_total-fees_total,'cycle_net_recomputed')
   equal(recorded['qty'],qa,'cycle_original_qty');check(int(recorded['holding_ms'])==end_ns//1000000-start_ns//1000000,'cycle_hold_ms')
   for v in VENUES:equal(recorded[v+'_net_before_funding'],legs[v]['net_pnl'],'cycle_venue_net_recomputed')
  else:check(any(D(x['remaining_qty'])for x in legs.values()),'unrecorded_cycle_must_be_open')
  calculated_cycles.append({'cycle_id':cid,'entry_ns':start_ns,'last_exit_ns':end_ns,'holding_seconds':str(D(end_ns-start_ns)/D(1000000000)),'orders':len(rows),'exit_parts':len(parts),'gross_pnl':str(gross_total),'fees':str(fees_total),'net_before_funding':str(gross_total-fees_total),'closed':recorded is not None,'exit_reason':recorded.get('exit_reason')if recorded else None,'legs':legs})
 for v in VENUES:equal(D(1000)+realized_net[v],summary['cash'][v],'final_cash_recomputed:'+v)
 total=sum(realized_net.values(),D(0));equal(total,summary['cash_change_before_funding'],'summary_total_cash_change')
 closed_net=sum((D(x['net_before_funding'])for x in calculated_cycles if x['closed']),D(0));equal(closed_net,summary['closed_pair_net_before_funding'],'closed_cycle_total')
 check(not residual if summary['cycle']is None else bool(residual),'residual_state_matches')
 check(not crossings,'actual_positions_no_settlement_crossings',crossings)
 fa=json.loads((path/'funding-audit.json').read_text());expected_funding='VERIFIED_NO_SETTLEMENT'if not residual and not crossings else 'UNVERIFIED_SETTLEMENT_EXPOSURE';check(fa['status']==expected_funding,'independent_funding_status_matches')
 if expected_funding=='VERIFIED_NO_SETTLEMENT':equal(summary['net_after_funding'],total,'summary_final_net_after_zero_funding')
 # Independently prove all fill quote receive timestamps refer to native book frames in the frozen source.
 source=Path(meta['source_archive']);quote_found={v:{}for v in VENUES}
 for v,needed in quote_needed.items():
  ex='bitget-futures'if v=='bitget'else v
  offsets=sorted({((n//1000000-START)//600000)*10 for n in needed})
  for offset in offsets:
   with gzip.open(source/ex/f'offset{offset:04d}.jsonl.gz','rt')as f:
    for line in f:
     if not line.strip():continue
     ts,body=line.split(' ',1);n=stamp_ns(ts)
     if n not in needed:continue
     msg=json.loads(body);isbook=msg.get('topic')=='orderbook.50.BTWUSDT'if v=='bybit'else msg.get('arg',{}).get('topic')=='books'
     if isbook and 'data' in msg:quote_found[v][n]=int(msg['cts']if v=='bybit'else msg['data'][0]['ts'])
  check(set(quote_found[v])==needed,'all_fill_quote_timestamps_found_in_raw:'+v)
 for r in orders:
  v=r['venue'];n=int(r[v+'_receive_ns']);check(quote_found[v].get(n)==int(r['book_exchange_ts_ms']),'fill_quote_source_clock_matches_native')
 raw_audit=json.loads((source/'audit.json').read_text());raw_counts=summary['raw_counts'];raw_summary={}
 for item in raw_audit['venues']:
  ex=item['venue'];v='bitget'if ex=='bitget-futures'else ex;c=item['counts'];raw_summary[v]=c
  check(raw_counts.get('frames:'+v,0)==c['lines'],'raw_frame_count:'+v)
  check(raw_counts.get('trade_records:'+v+':trade',0)==c.get('live_trade_records',0),'live_trade_count:'+v)
  check(raw_counts.get('trade_records:'+v+':historical_trade',0)==c.get('historical_snapshot_trade_records',0),'historical_trade_count:'+v)
  check(raw_counts.get('kind:'+v+':disconnect',0)==c.get('empty_lines',0),'disconnect_count:'+v)
  check(raw_counts.get('kind:'+v+':book',0)==c.get('book_snapshot',0)+c.get('book_delta',0)+c.get('book_update',0),'book_frame_count:'+v)
 for fn,digest in meta['source_evidence_sha256'].items():check(sha(source/fn)==digest,'frozen_source_evidence_hash:'+fn)
 execution_hashes={}
 code=Path(__file__).resolve().parent
 for filename,key in [('runner.py','runner_sha256'),('paper_snapshot.py','risk_snapshot_sha256'),('risk_policy.py','risk_policy_sha256'),('tardis_adapter.py','adapter_sha256')]:
  execution_hashes[str(code/filename)]=meta[key]
 for filename,expected in {**meta['source_code_sha256'],**execution_hashes}.items():
  check(sha(Path(filename))==expected,'frozen_execution_or_dependency_hash:'+filename)
 if equity_metrics['incomplete_liquidation_marks']:warnings.append('Maximum drawdown is measured only at paired complete liquidation-equity marks; missing/insufficient-depth marks are excluded, not carried forward as zero.')
 if residual:warnings.append('Open residual position exists; cash change is not total economic PnL.')
 if future_exchange_clocks:warnings.append('Exchange source timestamp is later than receiver decision clock for '+str(future_exchange_clocks)+' fills within configured 500ms tolerance; local receive timestamps remain causal.')
 equity_metrics['max_drawdown_note']='Estimated liquidation-equity path with depth/cost marks, total capital denominator 2000 USDT. Not exchange account liquidation modelling.'
 result={'audit_status':'PASS'if not fail else 'FAIL','run_status':status['status'],'read_only_audit':True,'audit_variant':'L1_accounting','l1_policy_errors':policy_errors,'execution_verification_scope':'Every fill price equals its explicit order limit and tick grid. Reconstructed native ask1/bid1 and cumulative participation budget verification belongs to the separate depth-fill audit.','audit_script':str(Path(__file__).resolve()),'audit_script_sha256':sha(Path(__file__)),'created_utc':datetime.now(timezone.utc).isoformat(),'checks':dict(counts),'failures':fail,'warnings':warnings,'ledger_counts':expected_rows,'summary_counts_match':not any('count'in x['check']for x in fail),'signal_statistics':{k:str(v)if isinstance(v,D)else v for k,v in signal_metrics.items()},'signal_reasons':dict(reasons),'cycles':calculated_cycles,'cumulative_net_before_funding':str(total),'initial_total_usdt':'2000','return_pct_on_total_2000':str(total/D(2000)*100),'final_cash':{v:str(D(1000)+realized_net[v])for v in VENUES},'max_holding_seconds':str(D(max_hold_ns)/D(1000000000)),'residual_positions':residual,'funding_status':expected_funding,'funding_crossings':crossings,'minimum_entry_distance_to_known_settlement_seconds':str(D(min_entry_settlement_distance_ns)/D(1000000000))if min_entry_settlement_distance_ns else None,'equity_metrics':{k:str(v)if isinstance(v,D)else v for k,v in equity_metrics.items()},'fill_quote_frames_verified':{v:len(x)for v,x in quote_found.items()},'raw_source_counts':raw_summary,'elapsed_seconds':time.monotonic()-began}
 result.update(risk_mode=meta.get('risk_mode','absolute_net'),risk_parameters={k:meta['policy'].get(k) for k in ('take_profit_bps','stop_loss_bps','hard_stop_loss_bps')},run_metadata_sha256=sha(path/'run_metadata.json'),execution_code_sha256=execution_hashes)
 with (path/'acceptance.json').open('x') as output:json.dump(result,output,indent=2)
 db.close();print(json.dumps({k:result[k]for k in ['audit_status','checks','failures','cumulative_net_before_funding','final_cash','residual_positions','equity_metrics','elapsed_seconds']},indent=2),flush=True)
 return 0 if not fail else 1

if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('run',type=Path);a=p.parse_args();raise SystemExit(main(a.run))

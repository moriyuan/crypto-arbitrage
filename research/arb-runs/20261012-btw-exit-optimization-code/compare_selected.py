"""Read-only exact C++ candidate versus authoritative Python CSV comparison.

Does not invoke either engine, inspect credentials, or create orders. Random
identifiers are mapped bijectively; only documented funding/day-end labels are
normalized. All prices, fees, cash, and nanosecond timestamps compare exactly.
"""
from __future__ import annotations
import argparse
from collections import defaultdict
import csv
from datetime import datetime, timezone
from decimal import Decimal as D
import hashlib
import json
import os
from pathlib import Path
import time

DEFAULT_SCENARIO='entry_deterioration_tp5_sl20'
VENUES=('bybit','bitget')


def money(value):
    number=D(str(value))
    if not number.is_finite():raise ValueError('Nonfinite monetary evidence')
    return number


def reason_family(value):
    if value=='funding_guard' or value.startswith('funding_guard:'):return 'funding_guard'
    if value in ('scheduled_day_end','replay_day_end'):return 'replay_day_end'
    return value


def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    return h.hexdigest()


def read_csv(path):
    with Path(path).open(newline='') as f:
        reader=csv.DictReader(f)
        if not reader.fieldnames or len(reader.fieldnames)!=len(set(reader.fieldnames)):
            raise ValueError('Missing or duplicate CSV column')
        return list(reader)


def compare_records(py_orders,cpp_orders,py_cycles,cpp_cycles,config,metadata,summary):
    result={'status':'FAIL','checks':0,'error_count':0,'errors':[],
            'orders':len(py_orders),'cycles':len(py_cycles),'cycle_comparisons':[]}
    def check(ok,label):
        result['checks']+=1
        if not ok:
            result['error_count']+=1
            if len(result['errors'])<100:result['errors'].append(label)
    def equal(a,b,label):check(money(a)==money(b),label)
    try:
        policy=metadata['policy'];mode=policy['risk_mode']
        check(mode in ('absolute_net','entry_deterioration'),'known risk mode')
        check(metadata['risk_mode']==mode==config['mode'],'risk mode identity')
        check(metadata['symbol']=='BTWUSDT' and metadata['requested_day_utc']=='2026-10-01'
              and metadata['execution_enabled'] is False,'public BTW Oct1 scope')
        check(metadata['risk_basis']=='remaining_long_entry_notional','risk notional basis')
        expected_basis={'take_profit':'remaining_pair_net_before_funding','stop_loss':'remaining_pair_net_before_funding'}
        if mode=='entry_deterioration':
            expected_basis.update(stop_loss='remaining_pair_net_deterioration_from_entry_quotes',
                hard_stop_loss='realized_plus_remaining_pair_net_before_funding')
        check(metadata['risk_pnl_basis']==expected_basis,'risk PnL basis identity')
        for cpp_key,py_key in [('tp_bps','take_profit_bps'),('sl_bps','stop_loss_bps')]:
            equal(config[cpp_key],policy[py_key],'parameter '+py_key)
        equal(config['hard_cap_bps'],policy['hard_stop_loss_bps'] if mode=='entry_deterioration' else 0,'effective hard cap')
        for v in VENUES:equal(policy[v+'_fee_bps'],3,'fee policy '+v)
        equal(metadata['initial_balance_per_venue'],1000,'initial account balance')
        check(config['unresolved_qty']==0 and summary['cycle'] is None,'both engines flat at completion')
        check(len(py_orders)==len(cpp_orders)==int(config['orders'])==int(summary['orders']),'order count')
        check(len(py_cycles)==len(cpp_cycles)==int(config['cycles'])==int(summary['completed_cycles']),'cycle count')
        check(len({c['cycle_id'] for c in py_cycles})==len(py_cycles),'unique Python cycles')
        check(len({str(c['cycle']) for c in cpp_cycles})==len(cpp_cycles),'unique C++ cycles')
        mapping={};reverse_mapping={};entries=defaultdict(list);exits=defaultdict(list)
        cpp_grouped=defaultdict(list);positions={};cash={v:D(1000) for v in VENUES}
        cycle_cash_start={};cycle_cash_end={};previous=-1
        for index,(a,b) in enumerate(zip(py_orders,cpp_orders),1):
            label=f'order {index}: '
            cid,sid=a['cycle_id'],str(b['cycle'])
            mapping.setdefault(cid,sid);reverse_mapping.setdefault(sid,cid)
            check(mapping[cid]==sid and reverse_mapping[sid]==cid,label+'bijective cycle mapping')
            now=int(a['decision_cursor_ns']);check(now>=previous,label+'monotonic cursor');previous=now
            check(now==int(b['ns']),label+'exact ns')
            check(int(a['timestamp_ms'])==int(b['timestamp_ms'])==now//1_000_000,label+'ms floor')
            for key in ('venue','side','intent'):check(a[key]==b[key],label+key)
            for key in ('qty','price','fee'):equal(a[key],b[key],label+key)
            check(a['symbol']=='BTWUSDT' and a['pair']=='BTW/USDT:USDT',label+'symbol/pair')
            v,q,p,fee=a['venue'],money(a['qty']),money(a['price']),money(a['fee'])
            check(v in VENUES and q>0 and p>0,label+'positive quantity/price and venue')
            equal(a['notional'],q*p,label+'notional')
            equal(a['fee_rate'],'.0003',label+'fee rate')
            equal(fee,q*p*D('.0003'),label+'fee arithmetic')
            key=(cid,v)
            if a['intent']=='entry':
                check(not b['exit_reason'],label+'entry reason empty')
                check(key not in positions,label+'one entry per venue')
                check(all(k[0]==cid or pos['remaining']==0 for k,pos in positions.items()),label+'single open cycle')
                cycle_cash_start.setdefault(cid,dict(cash))
                sign=1 if a['position_side']=='long' else -1
                check(a['position_side'] in ('long','short') and a['side']==('buy' if sign==1 else 'sell'),label+'entry direction')
                positions[key]={'entry':p,'remaining':q,'sign':sign}
                entries[cid].append(a);cash[v]-=fee
            elif a['intent']=='exit':
                pos=positions[key]
                check(a['position_side']==('long' if pos['sign']==1 else 'short'),label+'position side retained')
                check(a['side']==('sell' if pos['sign']==1 else 'buy'),label+'exit closes entry')
                check(q<=pos['remaining'],label+'no overclose')
                pos['remaining']-=q;cash[v]+=q*(p-pos['entry'])*pos['sign']-fee
                exits[cid].append(a)
            else:check(False,label+'known intent')
            equal(cash[v],b['cash'],label+'cash after every fill')
            cpp_grouped[sid].append(b);cycle_cash_end[cid]=dict(cash)
        check(all(p['remaining']==0 for p in positions.values()),'all reconstructed positions closed')
        check(set(mapping)=={c['cycle_id'] for c in py_cycles},'every Python order cycle has final cycle')
        check(set(reverse_mapping)=={str(c['cycle']) for c in cpp_cycles},'every C++ order cycle has final cycle')
        normalized=set()
        for index,(a,b) in enumerate(zip(py_cycles,cpp_cycles),1):
            label=f'cycle {index}: ';cid=a['cycle_id'];sid=str(b['cycle'])
            check(mapping.get(cid)==sid,label+'ordered identity')
            en,out=entries[cid],exits[cid]
            check(len(en)==2 and {o['venue'] for o in en}==set(VENUES),label+'paired entry')
            check(len({int(o['decision_cursor_ns']) for o in en})==1,label+'entry ns paired')
            check(bool(out),label+'has exits')
            start=int(en[0]['decision_cursor_ns']);end=max(int(o['decision_cursor_ns']) for o in out)
            check(start==int(b['entry_ns']) and start<end,label+'entry chronology')
            check(end==int(a['decision_cursor_ns'])==int(b['exit_ns']),label+'final exit ns')
            check(start<int(b['first_trigger_ns'])<=end,label+'C++ trigger chronology')
            for key in ('entry_timestamp_ms','exit_timestamp_ms','holding_ms','exit_parts'):
                check(int(a[key])==int(b[key]),label+key)
            check(int(a['entry_timestamp_ms'])==start//1_000_000 and int(a['exit_timestamp_ms'])==end//1_000_000,label+'cycle ms floors')
            check(int(a['holding_ms'])==end//1_000_000-start//1_000_000,label+'holding arithmetic')
            for key in ('qty','long_entry_price','short_entry_price','gross_pnl','total_fees',
                        'net_before_funding','bybit_net_before_funding','bitget_net_before_funding'):
                equal(a[key],b[key],label+key)
            for key in ('long_venue','short_venue'):check(a[key]==b[key],label+key)
            check(set((a['long_venue'],a['short_venue']))==set(VENUES),label+'opposite venues')
            check(reason_family(a['exit_reason'])==reason_family(b['exit_reason']),label+'reason family')
            if a['exit_reason']!=b['exit_reason']:normalized.add(reason_family(a['exit_reason']))
            parts=defaultdict(list)
            for o in out:parts[int(o['decision_cursor_ns'])].append(o)
            check(len(parts)==int(a['exit_parts']),label+'partial count from orders')
            check(len(out)==2*len(parts),label+'two orders per partial')
            for part in parts.values():
                check(len(part)==2 and {o['venue'] for o in part}==set(VENUES),label+'paired partial venues')
                equal(part[0]['qty'],part[1]['qty'],label+'paired partial quantity')
            venue_net={};total_gross=D(0);total_fees=D(0)
            for v in VENUES:
                entry=next(o for o in en if o['venue']==v);closed=[o for o in out if o['venue']==v]
                q0=money(entry['qty']);price=money(entry['price']);sign=1 if v==a['long_venue'] else -1
                equal(q0,a['qty'],label+'initial quantity '+v)
                equal(sum((money(o['qty']) for o in closed),D(0)),q0,label+'closed quantity '+v)
                equal(price,a['long_entry_price' if sign==1 else 'short_entry_price'],label+'entry price '+v)
                gross=sum((money(o['qty'])*(money(o['price'])-price)*sign for o in closed),D(0))
                fees=money(entry['fee'])+sum((money(o['fee']) for o in closed),D(0))
                weighted=sum((money(o['qty'])*money(o['price']) for o in closed),D(0))/q0
                equal(weighted,a['long_exit_price' if sign==1 else 'short_exit_price'],label+'weighted exit price '+v)
                venue_net[v]=gross-fees;total_gross+=gross;total_fees+=fees
                equal(venue_net[v],a[v+'_net_before_funding'],label+'order-derived leg net '+v)
                equal(cycle_cash_end[cid][v]-cycle_cash_start[cid][v],venue_net[v],label+'cycle cash change '+v)
            equal(total_gross,a['gross_pnl'],label+'order-derived gross')
            equal(total_fees,a['total_fees'],label+'order-derived fees')
            equal(total_gross-total_fees,a['net_before_funding'],label+'order-derived net')
            for o in cpp_grouped[sid]:
                if o['intent']=='exit':check(reason_family(o['exit_reason'])==reason_family(b['exit_reason']),label+'sticky C++ exit reason')
            result['cycle_comparisons'].append({'python_cycle_id':cid,'screen_cycle':sid,'entry_ns':start,
                'exit_ns':end,'exit_parts':len(parts),'qty':str(money(a['qty'])),
                'gross_pnl':str(total_gross),'fees':str(total_fees),'net_before_funding':str(total_gross-total_fees),
                'cash_after_cycle':{v:str(cycle_cash_end[cid][v]) for v in VENUES}})
        for v in VENUES:
            equal(cash[v],config['cash'][v],'screen final cash '+v)
            equal(cash[v],summary['cash'][v],'Python final cash '+v)
        for screen_key,cycle_key in [('gross_pnl','gross_pnl'),('fees','total_fees'),('net_before_funding','net_before_funding')]:
            py_value=sum((money(c[cycle_key]) for c in py_cycles),D(0))
            cpp_value=sum((money(c[cycle_key]) for c in cpp_cycles),D(0))
            equal(py_value,cpp_value,'cycle aggregate '+cycle_key)
            equal(py_value,config[screen_key],'screen aggregate '+screen_key)
        net=sum(cash.values())-2000
        equal(net,summary['closed_pair_net_before_funding'],'closed net summary')
        equal(net,summary['cash_change_before_funding'],'cash change summary')
        equal(net,config['net_before_funding'],'total cash/net reconciliation')
        result.update(cash={v:str(cash[v]) for v in VENUES},net_before_funding=str(net),
            partial_exit_cycles=sum(int(c['exit_parts'])>1 for c in py_cycles),
            reason_normalizations=sorted(normalized),risk_mode=mode,
            risk_parameters={k:policy[k] for k in ('take_profit_bps','stop_loss_bps','hard_stop_loss_bps')})
    except Exception as exc:
        check(False,type(exc).__name__+': '+str(exc))
    if not result['error_count']:result['status']='PASS'
    return result


def compare_selected(screen,python_run,output,scenario=DEFAULT_SCENARIO):
    started=time.monotonic();screen,python_run,output=map(Path,(screen,python_run,output))
    if output.exists():raise FileExistsError('Refusing to overwrite '+str(output))
    status=json.loads((python_run/'status.json').read_text())
    if status.get('status')!='COMPLETED':
        return {'status':'NOT_RUN_NONCOMPLETED_REPLAY','run_status':status.get('status')}
    result={'status':'FAIL','checks':0,'errors':[],'error_count':0}
    inputs={};files={'screen_aggregate':screen/'aggregate.json',
        'screen_orders':screen/scenario/'orders.csv','screen_cycles':screen/scenario/'cycles.csv',
        **{'python_'+name.replace('.','_'):python_run/name for name in
           ('orders.csv','cycles.csv','run_metadata.json','summary.json','status.json')}}
    try:
        inputs={k:{'path':str(p.resolve()),'sha256':digest(p)} for k,p in files.items()}
        aggregate=json.loads(files['screen_aggregate'].read_text())
        if aggregate.get('status')!='COMPLETED_SCREEN_ONLY' or aggregate.get('execution_enabled') is not False or aggregate.get('tape_format')!='BTWTP002':
            raise ValueError('C++ screen is not a completed public V2 run')
        if money(aggregate['fee_rate'])!=D('.0003') or money(aggregate['initial_cash_per_venue'])!=1000:
            raise ValueError('C++ screen fee or initial cash differs')
        candidates=[x for x in aggregate['scenarios'] if x['name']==scenario]
        if len(candidates)!=1:raise ValueError('Exactly one named screen candidate required')
        result=compare_records(read_csv(files['python_orders_csv']),read_csv(files['screen_orders']),
            read_csv(files['python_cycles_csv']),read_csv(files['screen_cycles']),candidates[0],
            json.loads(files['python_run_metadata_json'].read_text()),json.loads(files['python_summary_json'].read_text()))
        for key,path in files.items():
            result['checks']+=1
            if digest(path)!=inputs[key]['sha256']:raise ValueError('Input changed during comparison: '+key)
    except Exception as exc:
        result['status']='FAIL';result['error_count']+=1
        result['errors'].append(type(exc).__name__+': '+str(exc))
    result.update(inputs=inputs,scenario=scenario,run_status=status['status'],execution_enabled=False,
        read_only_comparison=True,created_utc=datetime.now(timezone.utc).isoformat(),
        elapsed_seconds=time.monotonic()-started,comparator_sha256=digest(__file__),
        scope='Exact ordered fill/cycle economics and cash equivalence; independent raw depth, entry baseline, funding and risk audits remain separate required gates.')
    output.parent.mkdir(parents=True,exist_ok=True)
    with output.open('x') as stream:
        json.dump(result,stream,ensure_ascii=False,indent=2);stream.write('\n');stream.flush();os.fsync(stream.fileno())
    return result


if __name__=='__main__':
    cli=argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--screen',type=Path,required=True)
    cli.add_argument('--python-run',type=Path,required=True)
    cli.add_argument('--output',type=Path,required=True)
    cli.add_argument('--scenario',default=DEFAULT_SCENARIO)
    args=cli.parse_args();result=compare_selected(args.screen,args.python_run,args.output,args.scenario)
    print(json.dumps({k:v for k,v in result.items() if k not in ('cycle_comparisons','inputs')},ensure_ascii=False))
    raise SystemExit(0 if result['status']=='PASS' else 3 if result['status'].startswith('NOT_RUN') else 1)

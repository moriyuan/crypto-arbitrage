"""Read-only execution equivalence and preregistered exit-policy selection.

No engine invocation, exchange calls, credentials, or live orders. Output files
are exclusive creates. Internal holdout metrics are calculated only after the
train/validation choice has been written and fsynced as selection-frozen.json.
"""
from __future__ import annotations
import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import sys
from datetime import datetime, timezone
from decimal import Decimal

D = Decimal
HERE = Path(__file__).resolve().parent
REFERENCE = Path('/Users/mori/code/crypto/state/arb-runs/20261012-btw-l1-tp2-net-sl2-net-20261001')
PROTOCOL = Path('/Users/mori/code/crypto/state/arb-runs/20261012-btw-exit-optimization/experiment-protocol.json')
BASELINE = 'baseline_net_tp2_sl2'
VENUES = ('bybit', 'bitget')
LOOKED_AT = 'This single retrospective day was repeatedly viewed before this task. The internal temporal holdout is not genuinely unseen external evidence.'


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''):
            h.update(block)
    return h.hexdigest()


def jread(path):
    return json.loads(Path(path).read_text())


def rows(path):
    with Path(path).open(newline='') as stream:
        return list(csv.DictReader(stream))


def money(value):
    value = D(value)
    if not value.is_finite():
        raise ValueError('nonfinite decimal value')
    return value


def write_exclusive(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str) + '\n'
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def family(reason):
    if reason.startswith('funding_guard'):
        return 'funding_guard'
    if 'day_end' in reason:
        return 'replay_day_end'
    return reason


def verify_baseline(screen, reference, output):
    screen, reference, output = Path(screen), Path(reference), Path(output)
    if output.exists():
        raise FileExistsError(f'refusing overwrite: {output}')
    base = screen/BASELINE
    files = {'screen_aggregate': screen/'aggregate.json',
             'screen_orders': base/'orders.csv', 'screen_cycles': base/'cycles.csv',
             'screen_snapshots': base/'entry_budgets.jsonl',
             'reference_orders': reference/'orders.csv', 'reference_cycles': reference/'cycles.csv'}
    report = {'status': 'FAIL', 'execution_enabled': False,
              'screen_run': str(screen.resolve()), 'reference_run': str(reference.resolve()),
              'checks': 0, 'errors': [], 'inputs': {},
              'code_sha256': {'verify_select.py': sha(__file__), 'screen.cpp': sha(HERE/'screen.cpp')}}

    def check(condition, text):
        report['checks'] += 1
        if not condition:
            if len(report['errors']) < 100:
                report['errors'].append(text)
            report['error_count'] = report.get('error_count', 0) + 1

    try:
        report['inputs'] = {k: {'path': str(p.resolve()), 'sha256': sha(p)} for k,p in files.items()}
        aggregate = jread(files['screen_aggregate'])
        check(aggregate.get('status') == 'COMPLETED_SCREEN_ONLY', 'screen completion status')
        check(aggregate.get('execution_enabled') is False, 'screen execution disabled')
        check(aggregate.get('tape_format') == 'BTWTP002', 'screen tape format')
        configs = [x for x in aggregate['scenarios'] if x['name'] == BASELINE]
        if len(configs) != 1:
            raise ValueError('exactly one baseline required')
        config = configs[0]
        check((config['mode'],config['tp_bps'],config['sl_bps'],config['hard_cap_bps']) == ('absolute_net',2,2,0), 'baseline parameter identity')
        check(config['unresolved_qty'] == 0, 'baseline has no unresolved position')
        oldorders, neworders = rows(files['reference_orders']), rows(files['screen_orders'])
        oldcycles, newcycles = rows(files['reference_cycles']), rows(files['screen_cycles'])
        check(len(oldorders) == len(neworders) == config['orders'], 'order count')
        check(len(oldcycles) == len(newcycles) == config['cycles'], 'cycle count')
        mapping, reverse_mapping, oldentries, oldexits, oldprices = {}, {}, {}, {}, {}
        cash = {v:D(1000) for v in VENUES}
        for i,(a,b) in enumerate(zip(oldorders,neworders), 1):
            oldid,newid = a['cycle_id'],b['cycle']
            mapping.setdefault(oldid,newid);reverse_mapping.setdefault(newid,oldid)
            check(mapping[oldid] == newid and reverse_mapping[newid] == oldid, f'order {i}: stable bijective cycle mapping')
            check(int(a['decision_cursor_ns']) == int(b['ns']), f'order {i}: ns')
            check(int(a['timestamp_ms']) == int(b['timestamp_ms']) == int(b['ns'])//1_000_000, f'order {i}: milliseconds')
            for key in ('venue','side','intent'):
                check(a[key] == b[key], f'order {i}: {key}')
            for key in ('qty','price','fee'):
                check(money(a[key]) == money(b[key]), f'order {i}: {key}')
            check(a['symbol'] == 'BTWUSDT' and a['pair'] == 'BTW/USDT:USDT', f'order {i}: symbol')
            check(money(a['fee_rate']) == D('.0003') and money(b['fee']) == money(b['qty'])*money(b['price'])*D('.0003'), f'order {i}: exact fee')
            v,q,p,fee = a['venue'],money(a['qty']),money(a['price']),money(a['fee'])
            if a['intent'] == 'entry':
                oldentries.setdefault(oldid,[]).append(a)
                oldprices[(oldid,v)] = p
                cash[v] -= fee
                check(not b['exit_reason'], f'order {i}: no entry exit-reason')
            elif a['intent'] == 'exit':
                oldexits.setdefault(oldid,[]).append(a)
                sign = D(1) if a['position_side'] == 'long' else D(-1)
                cash[v] += q*(p-oldprices[(oldid,v)])*sign-fee
            else:
                check(False,f'order {i}: unknown intent')
            check(cash[v] == money(b['cash']), f'order {i}: cumulative cash')
        for i,(a,b) in enumerate(zip(oldcycles,newcycles),1):
            oldid = a['cycle_id']
            check(mapping.get(oldid) == b['cycle'],f'cycle {i}: ordered identity')
            entry, exits = oldentries.get(oldid,[]), oldexits.get(oldid,[])
            check(len(entry)==2 and len({r['decision_cursor_ns'] for r in entry})==1,f'cycle {i}: paired entry')
            if len(entry)!=2 or not exits:
                raise ValueError(f'cycle {i}: missing complete paired orders')
            check(int(b['entry_ns']) == int(entry[0]['decision_cursor_ns']), f'cycle {i}: entry ns')
            check(int(b['exit_ns']) == int(a['decision_cursor_ns']) == max(int(r['decision_cursor_ns']) for r in exits),f'cycle {i}: final exit ns')
            for key in ('entry_timestamp_ms','exit_timestamp_ms','holding_ms','exit_parts'):
                check(int(a[key]) == int(b[key]),f'cycle {i}: {key}')
            for key in ('qty','long_entry_price','short_entry_price','gross_pnl','total_fees','net_before_funding','bybit_net_before_funding','bitget_net_before_funding'):
                check(money(a[key]) == money(b[key]),f'cycle {i}: {key}')
            for key in ('long_venue','short_venue'):
                check(a[key] == b[key],f'cycle {i}: {key}')
            check(family(a['exit_reason']) == family(b['exit_reason']),f'cycle {i}: exit-reason family')
            for r in neworders:
                if r['cycle']==b['cycle'] and r['intent']=='exit':
                    check(family(r['exit_reason']) == family(b['exit_reason']),f'cycle {i}: sticky exit-reason')
            check(int(b['entry_ns']) < int(b['first_trigger_ns']) <= int(b['exit_ns']),f'cycle {i}: first trigger chronology')
        snapshots = [json.loads(line) for line in files['screen_snapshots'].read_text().splitlines() if line.strip()]
        check(len(snapshots)==len(oldentries)==len(newcycles), 'one snapshot per entry')
        seen = set()
        for s in snapshots:
            sid=str(s['cycle_id']);check(sid not in seen,'snapshot duplicate cycle');seen.add(sid)
            oid=reverse_mapping.get(sid)
            if oid is None:
                raise ValueError(f'unknown snapshot cycle {sid}')
            entry=oldentries[oid]
            check(s['after_entry_consumption'] is True and s['price_units']=='1e-8' and s['entry_price_tick_size']=='0.00001',f'snapshot {sid}: unit contract')
            check(int(s['entry_ns'])==int(entry[0]['decision_cursor_ns']),f'snapshot {sid}: ns')
            check(money(s['qty'])==money(entry[0]['qty']),f'snapshot {sid}: qty')
            check(len(s['sides'])==4,f'snapshot {sid}: four sides')
            for e in entry:
                check(money(s['entry_prices'][e['venue']])*D('.00001')==money(e['price']),f'snapshot {sid}: entry price')
                if e['position_side']=='long':check(s['long']==e['venue'],f'snapshot {sid}: long venue')
                side=VENUES.index(e['venue'])*2+(e['side']=='sell')
                matching=[level for level in s['sides'][side] if D(level['price_units_1e8'])*D('1e-8')==money(e['price'])]
                check(len(matching)==1,f'snapshot {sid}: entry level present')
                if matching:
                    check(matching[0]['remaining_quarter'] <= matching[0]['displayed_qty']-int(money(e['qty'])*4),f'snapshot {sid}: entry liquidity consumed')
            for side in s['sides']:
                check(0<len(side)<=50 and len({r['price_units_1e8'] for r in side})==len(side),f'snapshot {sid}: observation keys')
                for level in side:
                    check(0<=level['remaining_quarter']<=level['displayed_qty'] and level['price_units_1e8']>0,f'snapshot {sid}: budget bounds')
        for v in VENUES:
            check(cash[v]==money(config['cash'][v]),f'final cash {v}')
        for configkey,csvkey in [('gross_pnl','gross_pnl'),('fees','total_fees'),('net_before_funding','net_before_funding')]:
            check(money(config[configkey])==sum((money(c[csvkey]) for c in newcycles),D(0)),f'aggregate {configkey}')
        report.update(orders=len(neworders),cycles=len(newcycles),snapshots=len(snapshots),cash=cash)
    except Exception as error:
        check(False,f'{type(error).__name__}: {error}')
    if not report['errors']:
        report['status']='PASS'
    write_exclusive(output,report)
    return report


def validate_protocol(p):
    if p.get('version')!=1 or p.get('symbol')!='BTWUSDT' or p.get('day_utc')!='2026-10-01' or p.get('execution_enabled') is not False:
        raise ValueError('unexpected protocol identity')
    if p['split_utc']!={'training':['00:00','12:00'],'validation':['12:00','18:00'],'internal_holdout':['18:00','24:00']}:
        raise ValueError('unexpected temporal partitions')
    if p['selection']['minimum_training_cycles']!=15:
        raise ValueError('unexpected minimum training count')
    if p['selection']['training_score']!='closed_net_pnl - 0.5 * max_drawdown_of_chronological_closed_cycle_cumulative_net':
        raise ValueError('unexpected scoring rule')
    if p['baseline']!={'mode':'absolute_net','take_profit_bps':2,'stop_loss_bps':2}:
        raise ValueError('unexpected baseline policy')
    expected={'absolute_net':{'take_profit_bps':[2,5],'stop_loss_bps':[25,50,100]},
              'entry_deterioration':{'take_profit_bps':[2,5],'stop_loss_bps':[5,10,20],'hard_stop_loss_bps':100}}
    if p['candidate_grid']!=expected:
        raise ValueError('unexpected candidate grid')
    ref=p['reference_policy_if_selection_inconclusive']
    if (ref['mode'],ref['take_profit_bps'],ref['stop_loss_bps'],ref['hard_stop_loss_bps'])!=('entry_deterioration',2,10,100):
        raise ValueError('unexpected preregistered mechanism reference')
    return p


def partition(row, day_ns):
    entry,exit_=int(row['entry_ns']),int(row['exit_ns'])
    if entry>exit_ or entry<day_ns or exit_>=day_ns+86400*1_000_000_000:
        raise ValueError('cycle outside protocol day or reversed chronology')
    for name,a,b in [('training',0,12),('validation',12,18),('internal_holdout',18,24)]:
        if day_ns+a*3600*1_000_000_000 <= entry and exit_ < day_ns+b*3600*1_000_000_000:
            return name
    return 'boundary_crossing'


def statistics(cycles):
    cumulative=peak=drawdown=D(0)
    counts={};net=[]
    for row in sorted(cycles,key=lambda r:(int(r['exit_ns']),int(r['cycle']))):
        value=money(row['net_before_funding']);net.append(value);cumulative+=value
        peak=max(peak,cumulative);drawdown=max(drawdown,peak-cumulative)
        reason=family(row['exit_reason']);counts[reason]=counts.get(reason,0)+1
    n=len(net)
    return {'cycles':n,'net':cumulative,'realized_max_drawdown':drawdown,
            'score':cumulative-D('.5')*drawdown,'gross':sum((money(r['gross_pnl']) for r in cycles),D(0)),
            'fees':sum((money(r['total_fees']) for r in cycles),D(0)),
            'wins':sum(x>0 for x in net),'losses':sum(x<0 for x in net),
            'worst_cycle_net':min(net,default=D(0)),'exit_reasons':counts}


def rank_key(item, phase):
    return (-money(item[phase]['score']),-item['tp_bps'],item['sl_bps'],item['name'])


def choose(training_validation, minimum=15):
    baseline=training_validation[BASELINE]
    finalists=[]
    for mode in ('absolute_net','entry_deterioration'):
        allowed=[x for name,x in training_validation.items() if name!=BASELINE and x['mode']==mode and x['training']['cycles']>=minimum]
        if allowed:
            finalists.append(sorted(allowed,key=lambda x:rank_key(x,'training'))[0])
    records=[]
    qualified=[]
    for item in finalists:
        v=item['validation'];b=baseline['validation']
        net_better=money(v['net'])>money(b['net'])
        drawdown_no_worse=money(v['realized_max_drawdown'])<=money(b['realized_max_drawdown'])
        # No invented minimum validation-count rule: preserve the exact frozen
        # protocol. Sparse samples remain a reported evidence limitation.
        ok=net_better and drawdown_no_worse
        records.append({'name':item['name'],'net_greater_than_baseline':net_better,
                        'realized_drawdown_no_worse_than_baseline':drawdown_no_worse,'qualified':ok})
        if ok:qualified.append(item)
    if qualified:
        selected=sorted(qualified,key=lambda x:rank_key(x,'validation'))[0]
        role='validation_qualified_candidate'
    else:
        selected=training_validation['entry_deterioration_tp2_sl10']
        role='preregistered_mechanism_reference_not_winner'
    return {'selected_name':selected['name'],'selection_role':role,
            'selected_policy':{k:selected[k] for k in ('mode','tp_bps','sl_bps','hard_cap_bps')},
            'training_finalists':[x['name'] for x in finalists],'validation_checks':records,
            'deployment_recommendation':False,
            'decision_basis':'Train and validation only; no holdout metric used.'}


def select(screen, protocol, equivalence, output):
    screen,protocol,equivalence,output=map(Path,(screen,protocol,equivalence,output))
    if output.exists():raise FileExistsError(f'refusing existing selection output directory: {output}')
    p=validate_protocol(jread(protocol));eq=jread(equivalence);aggregate=jread(screen/'aggregate.json')
    if eq.get('status')!='PASS':raise ValueError('baseline equivalence PASS required')
    if aggregate.get('status')!='COMPLETED_SCREEN_ONLY' or aggregate.get('execution_enabled') is not False or aggregate.get('tape_format')!='BTWTP002':
        raise ValueError('completed offline v2 screen required')
    if eq['code_sha256']['screen.cpp']!=sha(HERE/'screen.cpp'):
        raise ValueError('screen source differs from verified baseline source')
    for filename,key in [('orders.csv','screen_orders'),('cycles.csv','screen_cycles'),('entry_budgets.jsonl','screen_snapshots')]:
        if sha(screen/BASELINE/filename)!=eq['inputs'][key]['sha256']:
            raise ValueError('grid baseline differs from verified baseline: '+filename)
    day_ns=int(datetime.fromisoformat(p['day_utc']).replace(tzinfo=timezone.utc).timestamp())*1_000_000_000
    expected={BASELINE:('absolute_net',2,2,0)}
    for mode,grid in p['candidate_grid'].items():
        for tp in grid['take_profit_bps']:
            for sl in grid['stop_loss_bps']:
                expected[f'{mode}_tp{tp}_sl{sl}']=(mode,tp,sl,grid.get('hard_stop_loss_bps',0))
    configs={x['name']:x for x in aggregate['scenarios']}
    if len(configs)!=len(aggregate['scenarios']) or set(configs)!=set(expected):raise ValueError('screen must contain exactly the preregistered grid and baseline')
    inputs={'protocol':{'path':str(protocol.resolve()),'sha256':sha(protocol)},
            'baseline_equivalence':{'path':str(equivalence.resolve()),'sha256':sha(equivalence)},
            'aggregate':{'path':str((screen/'aggregate.json').resolve()),'sha256':sha(screen/'aggregate.json')},'scenarios':{}}
    stats={}
    # First pass deliberately accumulates only training/validation statistics.
    # Hashing input bytes binds all outputs without using holdout performance.
    for name,config in configs.items():
        identity=(config['mode'],config['tp_bps'],config['sl_bps'],config['hard_cap_bps'])
        if identity!=expected[name] or config['unresolved_qty']!=0:raise ValueError('scenario parameter/residual mismatch: '+name)
        files={f:str((screen/name/f).resolve()) for f in ('orders.csv','cycles.csv')}
        inputs['scenarios'][name]={f:{'path':path,'sha256':sha(path)} for f,path in files.items()}
        buckets={'training':[],'validation':[]}
        for row in rows(screen/name/'cycles.csv'):
            label=partition(row,day_ns)
            if label in buckets:buckets[label].append(row)
        stats[name]={'name':name,'mode':config['mode'],'tp_bps':config['tp_bps'],
                     'sl_bps':config['sl_bps'],'hard_cap_bps':config['hard_cap_bps'],
                     **{label:statistics(data) for label,data in buckets.items()}}
    decision=choose(stats,p['selection']['minimum_training_cycles'])
    frozen={'version':1,'status':'FROZEN_BEFORE_INTERNAL_HOLDOUT_EVALUATION','execution_enabled':False,
            'looked_at_day_warning':LOOKED_AT,'inputs':inputs,
            'code_sha256':{name:sha(HERE/name) for name in ('screen.cpp','verify_select.py')},
            'protocol':p,'training_validation':stats,**decision,
            'parameter_changes_after_holdout':'Forbidden within this experiment; any later change requires a separately registered experiment.'}
    output.mkdir(parents=True,exist_ok=False)
    write_exclusive(output/'selection-frozen.json',frozen)
    # Second pass starts only after durable, exclusive choice creation.
    analyses={}
    for name,config in configs.items():
        if sha(screen/name/'cycles.csv')!=inputs['scenarios'][name]['cycles.csv']['sha256']:
            raise ValueError('cycles changed after selection freeze: '+name)
        data=rows(screen/name/'cycles.csv');buckets={label:[] for label in ('training','validation','internal_holdout','boundary_crossing')}
        for row in data:buckets[partition(row,day_ns)].append(row)
        full=statistics(data)
        if len(data)!=config['cycles'] or full['net']!=money(config['net_before_funding']) or full['gross']!=money(config['gross_pnl']) or full['fees']!=money(config['fees']):
            raise ValueError('aggregate/cycle economic mismatch: '+name)
        analyses[name]={'mode':config['mode'],'tp_bps':config['tp_bps'],'sl_bps':config['sl_bps'],
                        **{label:statistics(data) for label,data in buckets.items()},'full_day':full,
                        'orders':config['orders'],'final_cash':config['cash'],
                        'complete_mark_max_drawdown_usdt':config.get('complete_mark_max_drawdown_usdt'),
                        'incomplete_liquidation_equity_marks':config.get('incomplete_liquidation_equity_marks'),
                        'drawdown_caveat':config.get('drawdown_caveat'),
                        'purged_cycle_ids':[r['cycle'] for r in buckets['boundary_crossing']]}
    selected=decision['selected_name'];a=analyses[selected];b=analyses[BASELINE]
    holdout={'selected_name':selected,'selection_role':decision['selection_role'],
             'net_delta':a['internal_holdout']['net']-b['internal_holdout']['net'],
             'realized_max_drawdown_delta':a['internal_holdout']['realized_max_drawdown']-b['internal_holdout']['realized_max_drawdown'],
             'selected_cycles':a['internal_holdout']['cycles'],'baseline_cycles':b['internal_holdout']['cycles'],
             'validated_improvement_claim':False,
             'interpretation':'Descriptive internal check only. No genuinely unseen external day; sparse samples or non-improvement cannot establish improvement.'}
    analysis={'version':1,'status':'SCREEN_ANALYSIS_COMPLETE_NOT_DEPLOYMENT_APPROVAL','execution_enabled':False,
              'selection_frozen_sha256':sha(output/'selection-frozen.json'),'looked_at_day_warning':LOOKED_AT,
              'selection':decision,'internal_holdout_comparison':holdout,'scenarios':analyses,
              'required_next_gate':'Independent full Decimal/Python replay and accounting/depth/risk/funding verification of frozen selected/reference policy.'}
    write_exclusive(output/'screen-analysis.json',analysis)
    lines=['# BTW 退出规则筛选', '', '这一天的数据此前已经反复查看；以下是内部时间切分，不是真正未见样本验证。', '',
           f"冻结对象：`{selected}`；角色：`{decision['selection_role']}`。",
           '仅按训练与验证阶段作决定，随后才计算留出表现；没有部署建议。', '',
           '| 方案 | 训练组数 | 训练净损益 | 验证净损益 | 留出组数 | 留出净损益 | 全日净损益 | 全日已闭合回撤 |',
           '|---|---:|---:|---:|---:|---:|---:|---:|']
    for name in (BASELINE,selected):
        x=analyses[name]
        lines.append(f"| {name} | {x['training']['cycles']} | {x['training']['net']} | {x['validation']['net']} | {x['internal_holdout']['cycles']} | {x['internal_holdout']['net']} | {x['full_day']['net']} | {x['full_day']['realized_max_drawdown']} |")
    lines += ['', '训练每个风险家族最多提名一个方案，至少15组；评分为净损益减去0.5倍按闭合顺序计算的回撤。验证要求净损益优于基线、已闭合回撤不高于基线。',
              '跨00/12/18/24时段边界的组合从分段统计剔除，全日统计保留。实际执行持续运行，未在分界处重新初始化仓位。',
              '已闭合回撤不能反映持仓中的风险；完整深度估值回撤也会遗漏深度不足的时点。理想双腿同步成交未包含真实延迟、单腿失败或冲击。',
              '若角色为 mechanism_reference，则只是预注册机制对照，不是搜参胜者。独立 Python 全日回放与账目、深度、风险和资金费核验仍需另行完成。', '']
    report=output/'report.md'
    fd=os.open(report,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as stream:stream.write('\n'.join(lines))
    return analysis


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    verify=sub.add_parser('verify-baseline');verify.add_argument('--screen-run',type=Path,required=True)
    verify.add_argument('--reference-run',type=Path,default=REFERENCE);verify.add_argument('--output',type=Path)
    selection=sub.add_parser('select');selection.add_argument('--screen-run',type=Path,required=True)
    selection.add_argument('--protocol',type=Path,default=PROTOCOL);selection.add_argument('--equivalence',type=Path,required=True)
    selection.add_argument('--output-dir',type=Path,required=True)
    args=parser.parse_args()
    try:
        if args.command=='verify-baseline':
            result=verify_baseline(args.screen_run,args.reference_run,args.output or args.screen_run/'baseline-equivalence.json')
            print(json.dumps({k:result.get(k) for k in ('status','checks','orders','cycles','error_count','errors')},default=str))
            return 0 if result['status']=='PASS' else 1
        result=select(args.screen_run,args.protocol,args.equivalence,args.output_dir)
        print(json.dumps({'status':result['status'],**result['selection']},default=str));return 0
    except Exception as error:
        print(f'ERROR: {type(error).__name__}: {error}',file=sys.stderr);return 1


if __name__=='__main__':
    raise SystemExit(main())

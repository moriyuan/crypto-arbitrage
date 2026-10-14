"""Descriptive exit-mechanism diagnostics; no new selection or trading."""
import csv
import json
from collections import Counter, defaultdict
from decimal import Decimal as D
from pathlib import Path
import numpy as np

HERE = Path(__file__).resolve().parent
EXP = HERE.parent / '20261012-btw-exit-optimization'

def quantiles(values):
    return dict(zip(('min','p25','p50','p75','p95','max'),
                    map(float,np.quantile([float(x) for x in values],[0,.25,.5,.75,.95,1])))) if values else None

def describe(path):
    rows=list(csv.DictReader((path/'cycles.csv').open()))
    group=defaultdict(list)
    for r in rows: group[r['exit_reason']].append(r)
    net=[D(r['net_before_funding']) for r in rows]
    return {'cycles':len(rows),'wins':sum(v>0 for v in net),'losses':sum(v<0 for v in net),
            'win_rate_pct':str(D(sum(v>0 for v in net))*100/len(rows)),
            'net':str(sum(net)),'gross':str(sum(D(r['gross_pnl']) for r in rows)),
            'fees':str(sum(D(r['total_fees']) for r in rows)),
            'orders':sum(1 for _ in csv.DictReader((path/'orders.csv').open())),
            'net_quantiles_usdt':quantiles(net),
            'holding_seconds':quantiles(D(r['holding_ms'])/1000 for r in rows),
            'partial_exit_cycles':sum(int(r['exit_parts'])>1 for r in rows),
            'holding_over_60s':sum(D(r['holding_ms'])>60000 for r in rows),
            'exit_reasons':{k:{'cycles':len(v),'net':str(sum(D(r['net_before_funding']) for r in v)),
                               'wins':sum(D(r['net_before_funding'])>0 for r in v)} for k,v in group.items()}}

def main():
    a=json.loads((EXP/'screen/aggregate.json').read_text())
    selection=json.loads((EXP/'selection/selection-frozen.json').read_text())
    names=['baseline_net_tp2_sl2',selection['selected_name']]
    result={'status':'DESCRIPTIVE_SCREEN_METRICS_PENDING_AUTHORITATIVE_AUDIT','execution_enabled':False,
            'policies':{n:describe(EXP/'screen'/n) for n in names}}
    for x in a['scenarios']:
        if x['name'] in names:
            result['policies'][x['name']].update({k:x[k] for k in (
                'complete_liquidation_equity_marks','incomplete_liquidation_equity_marks',
                'complete_mark_max_drawdown_usdt','initial_hard_budget_breaches','cash')})
    missed=json.loads((EXP/'missed-opportunities.json').read_text())
    windows=[x['windows']['after_final_exit'] for x in missed['cycles']]
    full=[w['full_first_hit'] for w in windows if w['full_first_hit']]
    result['missed_summary']=missed['summary']
    result['full_recovery_after_final_exit']={
        'cycles':len(full),'delay_seconds_quantiles':quantiles(x['seconds_after_window_start'] for x in full),
        'worst_net_bps_until_first_hit_quantiles':quantiles(x['worst_net_bps_before_hit'] for x in full),
        'initially_worsened_below_minus20bp':sum(D(x['worst_net_bps_before_hit'])<-20 for x in full),
        'initially_worsened_below_minus50bp':sum(D(x['worst_net_bps_before_hit'])<-50 for x in full)}
    result['interpretation']=[
        'Recovery is measured before each original 60s deadline; independent original full-quantity shadows cannot be summed as portfolio profits.',
        'Quote-only and full-quantity hits omit latency. No hit during observed data does not rule out recovery during invalid/missing quotes.',
        'Policies take different entry cohorts because holding and cooldown change. Difference in TP count is not a same-trade causal attribution.',
        'Complete liquidation equity omits incomplete-depth periods; lower measured drawdown is not proof of lower total risk.',
        'The selected internal holdout is still negative and this day was previously viewed. This is not demonstrated profitable deployment.'
    ]
    with (EXP/'research_metrics.json').open('x') as f:json.dump(result,f,indent=2,ensure_ascii=False)
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':main()

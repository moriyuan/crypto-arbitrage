"""Apply the frozen activity protocol, freeze choice, then disclose holdout."""
import csv
import hashlib
import json
import os
from pathlib import Path
from decimal import Decimal as D
import numpy as np

ROOT=Path(__file__).resolve().parent.parent/'20261012-btw-activity-maker'
START=1790812800000000000
def write(path,data):
    with path.open('x') as f:
        json.dump(data,f,indent=2,ensure_ascii=False,default=str)
        f.write('\n');f.flush();os.fsync(f.fileno())
def metrics(rows):
    equity=peak=dd=D(0)
    for r in sorted(rows,key=lambda r:int(r['exit_ns'])):
        equity+=D(r['net_before_funding']);peak=max(peak,equity);dd=max(dd,peak-equity)
    return dict(cycles=len(rows),wins=sum(D(r['net_before_funding'])>0 for r in rows),
                net=equity,closed_cycle_drawdown=dd,score=equity-D('.5')*dd)
def period(rows,lo,hi):
    return [r for r in rows if int(r['entry_ns'])>=START+lo*3600*10**9 and int(r['exit_ns'])<START+hi*3600*10**9]
allrows={}
for name in ('gateoff','q10','q25'):
    with (ROOT/'taker'/name/'cycles.csv').open() as f:allrows[name]=list(csv.DictReader(f))
early={n:{'training':metrics(period(r,0,12)),'validation':metrics(period(r,12,18))} for n,r in allrows.items()}
eligible=[n for n in ('q10','q25') if early[n]['training']['cycles']>=15]
candidate=max(eligible,key=lambda n:(early[n]['training']['score'],n=='q10')) if eligible else None
off=early['gateoff']['validation']
passed=bool(candidate and early[candidate]['validation']['net']>off['net'] and early[candidate]['validation']['closed_cycle_drawdown']<=off['closed_cycle_drawdown'])
selection=dict(candidate=candidate,validation_passed=passed,recommended_activity_gate=candidate if passed else None,
               maker_mechanism_cohort=candidate if passed else 'q10',early_metrics=early,
               protocol_sha256=hashlib.sha256((ROOT/'protocol.json').read_bytes()).hexdigest(),
               thresholds_sha256=hashlib.sha256((ROOT/'thresholds.csv').read_bytes()).hexdigest(),
               external_unseen_validation=False)
write(ROOT/'selection-frozen.json',selection)
# Only after the selection file is durably saved calculate late-period results.
results={n:{**early[n],'internal_holdout':metrics(period(r,18,24)), 'full_day':metrics(r),
            'boundary_crossing_cycles':len(r)-sum(len(period(r,a,b)) for a,b in [(0,12),(12,18),(18,24)])} for n,r in allrows.items()}
z=np.load(ROOT/'activity/features-uniform-seconds.npz');x=z['features']
thresholds=list(csv.DictReader((ROOT/'thresholds.csv').open()))
coverage={}
for t in thresholds:
    keep=(x[:,7]==3)&(x[:,1]>=int(t['countA']))&(x[:,2]>=int(t['countB']))&(x[:,3]>=int(t['turnoverAunits']))&(x[:,4]>=int(t['turnoverBunits']))
    keep &= (x[:,5]>=0)&(x[:,6]>=0)&(x[:,5]<=int(t['maxAgeNs']))&(x[:,6]<=int(t['maxAgeNs']))
    coverage[t['name']]={'full_day_pass_seconds':int(keep.sum()),'total_seconds':len(x),'hours_utc':[int(keep[h*3600:(h+1)*3600].sum()) for h in range(24)]}
write(ROOT/'activity-results.json',dict(selection=selection,metrics=results,time_uniform_coverage=coverage))
print(json.dumps(dict(selection=selection,metrics=results,time_uniform_coverage=coverage),indent=2,default=str))

"""Offline activity-only comparison using the unchanged, previously audited engine."""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal as D
import hashlib
import importlib.util
import json
from pathlib import Path

HERE=Path(__file__).resolve().parent
OLD=HERE.parent/'20261013-btw-perfect-taker-code'
QUANTILES=(25,50,75)


def read(path):return json.loads(Path(path).read_text())
def sha(path):
    with Path(path).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def write_new(path,data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('x') as f:f.write(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
def now():return datetime.now(timezone.utc).isoformat()


def select_gate(rows, fit_dates):
    expected={(q,d) for q in QUANTILES for d in fit_dates}
    if len(fit_dates)!=2 or len(set(fit_dates))!=2 or len(rows)!=len(expected):
        raise ValueError('exactly two fit dates and three gates required')
    pairs=[(r['quantile'],r['day']) for r in rows]
    if set(pairs)!=expected or len(set(pairs))!=len(pairs):raise ValueError('duplicate/missing/non-fit data')
    scores=[]
    for q in QUANTILES:
        subset=[r for r in rows if r['quantile']==q]
        for r in subset:
            if r['audit']!='PASS' or r['funding_status']!='VERIFIED_NO_SETTLEMENT' or r['unresolved_qty']!=0:
                raise ValueError('unaccepted accounting, funding or position')
            if not D(r['net']).is_finite() or not D(r['drawdown']).is_finite():raise ValueError('nonfinite metric')
        scores.append({'quantile':q,'eligible':all(r['cycles']>=10 for r in subset),
                       'worst_net':min(D(r['net']) for r in subset),
                       'sum_net':sum((D(r['net']) for r in subset),D(0)),
                       'sum_drawdown':sum((D(r['drawdown']) for r in subset),D(0))})
    base=scores[0]
    promotable=[s for s in scores if s['eligible'] and s['quantile']!=25 and
                s['worst_net']>base['worst_net'] and s['sum_net']>base['sum_net']]
    promotable.sort(key=lambda s:(-s['worst_net'],-s['sum_net'],s['sum_drawdown'],s['quantile']))
    selected=promotable[0] if promotable else base
    return {'selected_quantile':selected['quantile'],'promoted':bool(promotable),
            'reason':'FIT_IMPROVEMENT_WITH_MINIMUM_SAMPLE' if promotable else 'KEEP_Q25_NO_ELIGIBLE_JOINT_FIT_IMPROVEMENT',
            'scores':[{k:str(v) if isinstance(v,D) else v for k,v in s.items()} for s in scores]}


def verify_prior(root):
    protocol=read(root/'protocol.json'); prior=Path(protocol['source_experiment'])
    completion=read(prior/'completion.json')
    if completion['status']!='COMPLETED_AUDITED':raise ValueError('prior incomplete')
    frozen=read(prior/'selection-frozen.json')
    for p,digest in frozen['execution_code_sha256'].items():
        if sha(p)!=digest:raise ValueError('frozen execution engine changed')
    for name in ('input-manifest.json','selection-frozen.json','input-acceptance.json'):
        if sha(prior/name)!=completion['artifact_sha256'][str(prior/name)]:raise ValueError('prior evidence changed')
    return protocol,prior,frozen


def prepare(root):
    protocol,prior,frozen=verify_prior(root)
    old_manifest=read(prior/'input-manifest.json')
    paths=[]
    for q in QUANTILES:
        variant=root/'variants'/f'q{q}'
        cfg=deepcopy(protocol);cfg['activity_quantile']=q;cfg['legacy_engine_threshold_name']='q25'
        write_new(variant/'protocol.json',cfg)
        policy=(root/'fixed-policy.csv').read_text()
        with (variant/'policies.csv').open('x') as f:f.write(policy)
        manifest=deepcopy(old_manifest)
        manifest.update(root=str(variant),protocol_sha256=sha(variant/'protocol.json'),policies_sha256=sha(variant/'policies.csv'),actual_activity_quantile=q,transport_alias='q25')
        for day,spec in manifest['days'].items():
            thresholds=root/'calibration'/day/f'q{q}'/'thresholds.csv'
            if not thresholds.is_file():raise ValueError('calibration missing '+str(thresholds))
            spec['files']['thresholds']={'path':str(thresholds),'sha256':sha(thresholds)}
        write_new(variant/'input-manifest.json',manifest)
        paths.extend([variant/'protocol.json',variant/'policies.csv',variant/'input-manifest.json'])
    paths.extend([root/'protocol.json',root/'fixed-policy.csv'])
    paths.extend(sorted((root/'calibration').rglob('*.json')))
    paths.extend(sorted((root/'calibration').rglob('*.csv')))
    paths.extend([HERE/'experiment.py',OLD/'run_day.py'])
    write_new(root/'prepared.json',{'prepared_utc':now(),'execution_enabled':False,
              'source_sha256':{str(p):sha(p) for p in paths},'execution_code_sha256':frozen['execution_code_sha256']})


def verify_prepared(root):
    meta=read(root/'prepared.json')
    for p,digest in {**meta['source_sha256'],**meta['execution_code_sha256']}.items():
        if sha(p)!=digest:raise ValueError('prepared source changed '+p)
    return meta


def audit_binding(variant,day,phase):
    directory=variant/phase/day;file=directory/'perfect-audit.json';audit=read(file)
    if audit['status']!='PASS':raise ValueError('audit failed')
    for key,path in [('aggregate_sha256',directory/'aggregate.json'),('launch_sha256',directory.with_suffix('.launch.json')),('input_manifest_sha256',variant/'input-manifest.json')]:
        if audit[key]!=sha(path):raise ValueError('stale audit '+key)
    for scenario in audit['scenarios'].values():
        for filename,digest in scenario['source_sha256'].items():
            path=directory/'e30_tp20_sl10_h15'/filename
            if sha(path)!=digest:raise ValueError('ledger changed '+filename)
    return audit


def freeze(root):
    verify_prepared(root);protocol=read(root/'protocol.json');rows=[];bindings={}
    for q in QUANTILES:
        variant=root/'variants'/f'q{q}'
        for day in protocol['fit_dates']:
            directory=variant/'search'/day;audit=audit_binding(variant,day,'search')
            agg=read(directory/'aggregate.json');s=agg['scenarios'][0]
            if len(agg['scenarios'])!=1 or s['id']!=protocol['fixed_policy']['id']:raise ValueError('wrong fixed policy')
            a=audit['scenarios'][s['id']]
            rows.append({'quantile':q,'day':day,'net':s['net_before_funding'],'cycles':s['cycles'],
                         'drawdown':s['complete_mark_max_drawdown_usdt'],'unresolved_qty':s['unresolved_qty'],
                         'audit':audit['status'],'funding_status':a['funding_status']})
            for f in [directory/'aggregate.json',directory/'perfect-audit.json',directory.with_suffix('.launch.json')]:bindings[str(f)]=sha(f)
            for name in ('orders.csv','cycles.csv','equity.csv'):
                f=directory/s['id']/name;bindings[str(f)]=sha(f)
    selection=select_gate(rows,protocol['fit_dates'])
    selection.update(frozen_utc=now(),execution_enabled=False,fit_rows=rows,protocol_sha256=sha(root/'protocol.json'),prepared_sha256=sha(root/'prepared.json'),source_sha256=bindings,evaluation_results_consumed=False,scope='Previously researched dates; internal activity ablation, not pristine holdout.')
    write_new(root/'selection-frozen.json',selection)
    for q in QUANTILES:
        variant=root/'variants'/f'q{q}'
        with (variant/'selected-policies.csv').open('x') as f:f.write((variant/'policies.csv').read_text())
        write_new(variant/'selection-frozen.json',{'frozen_utc':selection['frozen_utc'],'protocol_sha256':sha(variant/'protocol.json'),'selected_policies_sha256':sha(variant/'selected-policies.csv'),'execution_code_sha256':read(root/'prepared.json')['execution_code_sha256'],'parent_selection_path':str(root/'selection-frozen.json'),'parent_selection_sha256':sha(root/'selection-frozen.json'),'true_activity_quantile':q,'role':'primary' if q==selection['selected_quantile'] else 'predeclared_diagnostic','execution_enabled':False})
    print(json.dumps(selection,ensure_ascii=False),flush=True)


def run_phase(root,phase):
    verify_prepared(root);protocol=read(root/'protocol.json')
    spec=importlib.util.spec_from_file_location('frozen_run_day',OLD/'run_day.py');module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    dates=protocol['fit_dates'] if phase=='search' else [protocol['evaluation_date']]
    if phase=='evaluation':
        frozen=read(root/'selection-frozen.json')
        if frozen['prepared_sha256']!=sha(root/'prepared.json'):raise ValueError('selection preparation mismatch')
        for path,digest in frozen['source_sha256'].items():
            if sha(path)!=digest:raise ValueError('fit evidence changed after selection')
    for q in QUANTILES:
        for day in dates:
            variant=root/'variants'/f'q{q}'
            if phase=='evaluation':
                vf=read(variant/'selection-frozen.json')
                if vf['parent_selection_sha256']!=sha(root/'selection-frozen.json'):raise ValueError('gate selection changed')
            print(json.dumps({'phase':phase,'true_activity_quantile':q,'day':day}),flush=True)
            module.run(variant,day,phase,OLD/'perfect_taker')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',required=True);p.add_argument('action',choices=['prepare','search','freeze','evaluation']);args=p.parse_args();root=Path(args.root).resolve()
    if args.action=='prepare':prepare(root)
    elif args.action=='freeze':freeze(root)
    else:run_phase(root,args.action)

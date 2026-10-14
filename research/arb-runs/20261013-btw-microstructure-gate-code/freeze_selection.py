"""Freeze original rule before evaluation; all predeclared policies diagnostic."""
import argparse
import csv
import datetime as dt
import importlib.util
import json
from pathlib import Path
from run_day import sha,write,HERE
from selection import choose
OLD=HERE.parent/'20261013-btw-perfect-taker-code/freeze_selection.py'
spec=importlib.util.spec_from_file_location('old_freeze_validation',OLD);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)

def validate_feature_acceptance(root,manifest,acceptance):
    if acceptance['status']!='PASS' or acceptance.get('execution_enabled') is not False:
        raise ValueError('feature audit must pass offline before selection')
    if acceptance['protocol_sha256']!=sha(root/'protocol.json') or acceptance['input_manifest_sha256']!=sha(root/'input-manifest.json'):
        raise ValueError('stale feature acceptance binding')
    if set(acceptance['days'])!=set(manifest['days']):raise ValueError('feature audit date coverage')
    for day,spec in manifest['days'].items():
        a=acceptance['days'][day]
        if a['status']!='PASS' or a['gate_file_sha256']!=spec['files']['entry_gates']['sha256'] or a['tape_sha256']!=spec['files']['tape']['sha256'] or a['feature_index_sha256']!=spec['feature_evidence']['feature_index']['sha256']:
            raise ValueError('wrong feature source audit')
    if not acceptance.get('source_sha256'):raise ValueError('missing feature auditor evidence')
    for path,expected in acceptance['source_sha256'].items():
        if sha(path)!=expected:raise ValueError('feature audit changed')

def freeze(root):
    root=Path(root).resolve()
    if any((root/name).exists() for name in ['selection-frozen.json','grid-scores.json','evaluation']):raise FileExistsError('selection must precede evaluation and cannot overwrite')
    protocol=json.loads((root/'protocol.json').read_text());manifest=json.loads((root/'input-manifest.json').read_text())
    if manifest['protocol_sha256']!=sha(root/'protocol.json') or manifest['policies_sha256']!=sha(root/'policies.csv'):raise ValueError('protocol changed')
    acceptance=json.loads((root/'feature-gate-acceptance.json').read_text())
    validate_feature_acceptance(root,manifest,acceptance)
    policies=protocol['policies'];scores={p['id']:{'id':p['id'],'days':{}} for p in policies};sources={str(root/'feature-gate-acceptance.json'):sha(root/'feature-gate-acceptance.json')}
    for day in protocol['fit_dates']:
        folder=root/'search'/day;aggregate,audit,launch=module.validate_fit_artifacts(root,day)
        for path,h in manifest['execution_code_sha256'].items():
            if launch['code_sha256'].get(path)!=h or sha(path)!=h:raise ValueError('executed code mismatch')
        actual={r['id']:r for r in aggregate['scenarios']}
        if set(actual)!=set(scores) or set(audit['scenarios'])!=set(scores):raise ValueError('candidate count mismatch')
        for p in policies:
            name=p['id'];s=actual[name];a=audit['scenarios'][name]
            if any(s[k]!=p[k] for k in ['entry_bps','tp_bps','sl_bps','hard_bps','hold_ms']):raise ValueError('executed parameters mismatch')
            scores[name]['days'][day]={'net':s['net_before_funding'],'drawdown':s['complete_mark_max_drawdown_usdt'],'cycles':s['cycles'],'orders':s['orders'],'wins':s['wins'],'losses':s['losses'],'unresolved_qty':s['unresolved_qty'],'funding_status':a['funding_status'],'accounting_pass':a['status']=='PASS','cash':s['cash']}
        for file in ['aggregate.json','perfect-audit.json']:
            sources[str(folder/file)]=sha(folder/file)
        sources[str(folder.with_suffix('.launch.json'))]=sha(folder.with_suffix('.launch.json'))
    best,scored=choose(list(scores.values()),protocol['fit_dates'],policies,protocol['selection']['minimum_cycles_each_fit_day'])
    write(root/'grid-scores.json',scored)
    frozen={'status':'SELECTED' if best else 'NO_ELIGIBLE_IMPROVEMENT_BASELINE_RETAINED','frozen_utc':dt.datetime.now(dt.timezone.utc).isoformat(),'execution_enabled':False,'selected_id':best['id'] if best else 'baseline','best':best,'eligible_candidates':sum(s['eligible'] for s in scored),'ranking_rule':protocol['selection'],'fit_dates':protocol['fit_dates'],'evaluation_date':protocol['evaluation_date'],'evaluation_date_results_consumed':False,'evaluation_all_predeclared_policies_diagnostic':True,'primary_evaluation_id':best['id'] if best else 'baseline','source_sha256':sources,'protocol_sha256':sha(root/'protocol.json'),'policies_sha256':sha(root/'policies.csv'),'input_manifest_sha256':sha(root/'input-manifest.json'),'execution_code_sha256':manifest['execution_code_sha256'],'code_sha256':{str(p):sha(p) for p in [Path(__file__),HERE/'selection.py',OLD]},'grid_scores_sha256':sha(root/'grid-scores.json')}
    write(root/'selection-frozen.json',frozen);print(json.dumps(frozen,ensure_ascii=False))
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('experiment');freeze(p.parse_args().experiment)

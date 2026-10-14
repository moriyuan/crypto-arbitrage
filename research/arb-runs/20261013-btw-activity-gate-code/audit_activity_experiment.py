"""Independent activity ablation acceptance; no production strategy imports or networking."""
import argparse
import csv
import datetime as dt
import hashlib
import importlib.util
import json
from pathlib import Path
import numpy as np

HERE=Path(__file__).resolve().parent
OLD=HERE.parent/'20261013-btw-perfect-taker-code'
DAYAUDIT=HERE.parent/'20261012-btw-as-code/audit_day_activity.py'
POLICY='e30_tp20_sl10_h15'

def sha(p):
    with Path(p).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def read(p):return json.loads(Path(p).read_text())
def rows(p):
    with Path(p).open() as f:return list(csv.DictReader(f))
def require(condition,message):
    if not condition:raise AssertionError(message)
def write_new(p,data):
    with Path(p).open('x') as f:json.dump(data,f,ensure_ascii=False,indent=2);f.write('\n')
def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path);mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod);return mod

def higher_index(n,q):
    require(n>0,'empty calibration sample');require(q in (25,50,75),'undeclared quantile')
    return ((n-1)*q+99)//100

def validate_transport(records,values):
    require(len(records)==2 and {r['name'] for r in records}=={'q10','q25'},'one q25 transport alias plus unused required q10 compatibility row')
    r=next(r for r in records if r['name']=='q25')
    require([int(r[k]) for k in ('countA','countB','turnoverAunits','turnoverBunits')]==values,'true quantile values')
    require(int(r['maxAgeNs'])==5*10**9,'unchanged last-trade age')

def validate_launch(root,variant,day,phase):
    directory=variant/phase/day;manifest=read(variant/'input-manifest.json');spec=manifest['days'][day]
    launch=read(directory.with_suffix('.launch.json'));status=read(directory.with_suffix('.status.json'))
    prepared=read(root/'prepared.json');engine=prepared['execution_code_sha256'];binary=next(p for p in engine if Path(p).name=='perfect_taker')
    require(status['status']=='COMPLETED_PENDING_AUDIT' and status['aggregate_sha256']==sha(directory/'aggregate.json'),'completed current result')
    require(launch['day']==day and launch['phase']==phase and launch['execution_enabled'] is False,'launch identity')
    require(launch['inputs']==spec,'launch exact variant inputs')
    require(launch['protocol_sha256']==manifest['protocol_sha256']==sha(variant/'protocol.json'),'protocol bound')
    policy=variant/('policies.csv' if phase=='search' else 'selected-policies.csv')
    require(launch['policies_sha256']==sha(policy)==sha(root/'fixed-policy.csv'),'unchanged fixed policy')
    for path,digest in engine.items():require(launch['code_sha256'][path]==digest==sha(path),'unchanged execution engine')
    for path,digest in launch['code_sha256'].items():require(sha(path)==digest,'launch code integrity')
    files=spec['files'];expected=[binary,'--tape',files['tape']['path'],'--activity',files['activity']['path'],'--thresholds',files['thresholds']['path'],'--funding-csv',files['funding_csv']['path'],'--policies',str(policy),'--entry-start-ns',str(spec['entry_start_ns']),'--output',str(directory)]
    require(launch['command']==expected,'exact launched command')
    require(dt.datetime.fromisoformat(status['completed_utc'])>=dt.datetime.fromisoformat(launch['started_utc']),'completion chronology')
    if phase=='evaluation':
        frozen=read(root/'selection-frozen.json');vf=read(variant/'selection-frozen.json')
        require(dt.datetime.fromisoformat(launch['started_utc'])>dt.datetime.fromisoformat(frozen['frozen_utc']),'evaluation after selection frozen')
        require(launch['selection_frozen_sha256']==sha(variant/'selection-frozen.json'),'launch frozen selection hash')
        require(vf['parent_selection_sha256']==sha(root/'selection-frozen.json'),'parent selection hash')
    return {'status':'PASS','launch_sha256':sha(directory.with_suffix('.launch.json')),'actual_activity_quantile':manifest['actual_activity_quantile'],'engine_transport_alias':'q25'}

def audit_sources(root):
    protocol=read(root/'protocol.json');prior=Path(protocol['source_experiment']);old=read(prior/'input-manifest.json');previous=read(prior/'input-acceptance.json');prepared=read(root/'prepared.json')
    require(previous['status']=='PASS' and previous['input_manifest_sha256']==sha(prior/'input-manifest.json'),'prior full input acceptance bound')
    for path,digest in {**prepared['source_sha256'],**prepared['execution_code_sha256']}.items():require(sha(path)==digest,'prepared file changed '+path)
    checker=load('independent_window',DAYAUDIT)
    result={'status':'PASS','execution_enabled':False,'auditor_sha256':sha(__file__),'independent_window_code_sha256':sha(DAYAUDIT),'protocol_sha256':sha(root/'protocol.json'),'prepared_sha256':sha(root/'prepared.json'),'prior_input_acceptance_sha256':sha(prior/'input-acceptance.json'),'days':{},'scope':'All native feature rows and all uniform seconds recomputed from frozen live trades and recovery/gap ledger; calibration independently computed; prior raw/native/history audits retained without reparsing compressed raw frames.'}
    for day,spec in old['days'].items():
        for label,item in {**spec['files'],'source_summary':spec['source_summary']}.items():require(sha(item['path'])==item['sha256'],'original source '+label)
        files=spec['files'];activity=Path(files['activity']['path']).parent;meta=read(activity/'metadata.json');uniform=activity/'features-uniform-seconds.npz'
        require(meta['status']=='COMPLETED' and meta['execution_enabled'] is False,'source activity completed offline')
        for name,item in meta['artifacts'].items():require(sha(activity/name)==item['sha256'],'activity artifact hash '+name)
        with Path(files['trades']['path']).open('rb') as f:require(f.read(8)==b'BTWTRD01','trade magic')
        trades=np.memmap(files['trades']['path'],dtype=checker.TRADE,mode='r',offset=8)
        require(np.all(trades['ns'][:-1]<=trades['ns'][1:]) and np.all(trades['source_ns']<=trades['ns']),'trade receive order and source causality')
        with np.load(uniform,allow_pickle=False) as data:features=data['features'].copy()
        require(features.dtype==np.dtype('int64') and features.shape==(86400,8),'uniform schema')
        start=spec['entry_start_ns']-43200*10**9
        require(np.array_equal(features[:,0],start+np.arange(86400,dtype='int64')*10**9),'exact causal one second grid')
        raw=meta['raw'];expected=checker.expected(features[:,0],trades,raw['recoveries'],raw['disconnects'])
        require(np.array_equal(features,expected),'all uniform activity from native trades')
        decision=np.memmap(files['activity']['path'],dtype=checker.FEATURE,mode='r',offset=8)
        for offset in range(0,len(decision),250000):
            chunk=decision[offset:offset+250000];expected=checker.expected(chunk['ns'],trades,raw['recoveries'],raw['disconnects'])
            for col,name in enumerate(checker.FEATURE.names):require(np.array_equal(chunk[name],expected[:,col]),f'all native activity {day} {offset} {name}')
        training=features[(features[:,0]>=start)&(features[:,0]<spec['entry_start_ns'])&(features[:,7]==3)]
        require(len(training)>0,'valid training samples')
        quantiles={}
        for q in (25,50,75):
            variant=root/'variants'/f'q{q}';manifest=read(variant/'input-manifest.json');vs=manifest['days'][day]
            require(manifest['actual_activity_quantile']==q and manifest['transport_alias']=='q25','true quantile metadata')
            require(manifest['protocol_sha256']==sha(variant/'protocol.json') and manifest['policies_sha256']==sha(variant/'policies.csv'),'variant frozen configuration')
            for key in ('source_summary','entry_start_ns','end_ns_exclusive'):require(vs[key]==spec[key],'unchanged '+key)
            for name,item in files.items():
                if name!='thresholds':require(vs['files'][name]==item,'activity-only source change '+name)
            threshold=Path(vs['files']['thresholds']['path']);require(sha(threshold)==vs['files']['thresholds']['sha256'],'threshold hash')
            idx=higher_index(len(training),q);values=[int(np.partition(training[:,col],idx)[idx]) for col in (1,2,3,4)]
            validate_transport(rows(threshold),values)
            require(next(r for r in rows(threshold) if r['name']=='q10')==next(r for r in rows(files['thresholds']['path']) if r['name']=='q10'),'unchanged unused q10 compatibility row')
            quantiles[str(q)]={'values':values,'maxAgeNs':5*10**9,'true_quantile':q,'legacy_transport_alias':'q25','thresholds_sha256':sha(threshold),'higher_order_statistic_index':idx}
        oldq=next(r for r in rows(files['thresholds']['path']) if r['name']=='q25')
        require([int(oldq[k]) for k in ('countA','countB','turnoverAunits','turnoverBunits')]==quantiles['25']['values'],'q25 prior calibration reproduction')
        result['days'][day]={'status':'PASS','all_native_activity_decisions_verified':len(decision),'all_uniform_seconds_verified':len(features),'native_live_trades':len(trades),'uniform_features_sha256':sha(uniform),'activity_metadata_sha256':sha(activity/'metadata.json'),'native_trade_sha256':files['trades']['sha256'],'native_activity_sha256':files['activity']['sha256'],'calibration_start_ns':start,'calibration_end_exclusive_ns':spec['entry_start_ns'],'valid_calibration_samples':len(training),'quantiles':quantiles,'source_time_causality_verified':True}
        print(json.dumps({'source_audit_day':day,'status':'PASS','decisions':len(decision)}),flush=True)
    write_new(root/'activity-input-acceptance.json',result);return result

def audit_outputs(root,phase,deep):
    protocol=read(root/'protocol.json');prior=Path(protocol['source_experiment']);checker=load('independent_perfect',OLD/'audit_perfect.py')
    days=protocol['fit_dates'] if phase=='search' else [protocol['evaluation_date']]
    results={}
    for q in (25,50,75):
        variant=root/'variants'/f'q{q}'
        for day in days:
            directory=variant/phase/day;binding=validate_launch(root,variant,day,phase)
            target=directory/('perfect-deep-audit.json' if deep else 'perfect-audit.json')
            require(not target.exists(),'never overwrite audit output')
            result=checker.run(variant,day,directory,deep,[POLICY] if deep else None)
            require(result['status']=='PASS','independent replay audit')
            binding['independent_audit_sha256']=sha(target)
            if q==25:
                original=prior/phase/day/POLICY
                binding['q25_prior_ledger_exact_sha256']={}
                for name in ('orders.csv','cycles.csv','equity.csv'):
                    require(sha(directory/POLICY/name)==sha(original/name),'q25 exact prior ledger '+name)
                    binding['q25_prior_ledger_exact_sha256'][name]=sha(original/name)
            results[f'q{q}/{day}']=binding
    out={'status':'PASS','execution_enabled':False,'auditor_sha256':sha(__file__),'phase':phase,'kind':'deep' if deep else 'light','runs':results,'scope':'q25 in underlying engine/audit is a transport alias; true25/50/75 values validated independently in activity-input-acceptance.json.'}
    write_new(root/f'{phase}-{"deep" if deep else "light"}-acceptance.json',out);return out

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',required=True);p.add_argument('action',choices=['inputs','search-light','search-deep','evaluation-light','evaluation-deep']);a=p.parse_args();root=Path(a.root).resolve()
    result=audit_sources(root) if a.action=='inputs' else audit_outputs(root,*a.action.split('-')[:1],deep=a.action.endswith('-deep'))
    print(json.dumps({'status':result['status'],'action':a.action}),flush=True)

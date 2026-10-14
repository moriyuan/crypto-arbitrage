"""Frozen offline entry-gate replay; no credentials or exchange access."""
import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import subprocess
import time
HERE=Path(__file__).resolve().parent
READER=HERE.parent/'20261012-btw-as-code/cohort_replay.cpp'
def sha(path):
    with Path(path).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def write(path,value):Path(path).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
def run(root,day,phase,binary):
    root=Path(root).resolve();binary=Path(binary).resolve()
    protocol=json.loads((root/'protocol.json').read_text());manifest=json.loads((root/'input-manifest.json').read_text())
    policies=root/'policies.csv'
    for key,path in [('protocol_sha256',root/'protocol.json'),('policies_sha256',policies)]:
        if manifest[key]!=sha(path):raise ValueError('frozen protocol/policies changed')
    frozen_code=manifest['execution_code_sha256']
    expected_paths={str(p) for p in (binary,HERE/'perfect_taker.cpp',READER)}
    if set(frozen_code)!=expected_paths:raise ValueError('wrong execution paths')
    for path,expected in {**frozen_code,**manifest['feature_code_sha256']}.items():
        if sha(path)!=expected:raise ValueError('frozen code changed: '+path)
    if phase=='search':
        if day not in protocol['fit_dates']:raise ValueError('fit date not declared')
        if (root/'selection-frozen.json').exists():raise ValueError('fit cannot run after selection')
    elif phase=='evaluation':
        if day!=protocol['evaluation_date']:raise ValueError('wrong evaluation date')
        frozen=json.loads((root/'selection-frozen.json').read_text())
        if frozen['protocol_sha256']!=sha(root/'protocol.json') or frozen['policies_sha256']!=sha(policies) or frozen['input_manifest_sha256']!=sha(root/'input-manifest.json') or frozen['execution_code_sha256']!=frozen_code:raise ValueError('frozen selection binding mismatch')
        for p,h in frozen['source_sha256'].items():
            if sha(p)!=h:raise ValueError('fit artifact changed after freeze')
    else:raise ValueError('unknown phase')
    spec=manifest['days'][day]
    for name,item in {**spec['files'],**spec['feature_evidence'],'source_summary':spec['source_summary']}.items():
        if sha(item['path'])!=item['sha256']:raise ValueError('input hash mismatch: '+name)
    target=root/phase/day;target.parent.mkdir(exist_ok=True)
    status_path=target.with_suffix('.status.json');launch_path=target.with_suffix('.launch.json');log_path=target.with_suffix('.log')
    if any(p.exists() for p in [target,status_path,launch_path,log_path]):raise FileExistsError('refusing to replace previous run')
    f=spec['files']
    command=[str(binary),'--tape',f['tape']['path'],'--activity',f['activity']['path'],'--thresholds',f['thresholds']['path'],'--funding-csv',f['funding_csv']['path'],'--policies',str(policies),'--entry-start-ns',str(spec['entry_start_ns']),'--entry-gates',f['entry_gates']['path'],'--output',str(target)]
    code={**frozen_code,str(Path(__file__).resolve()):sha(__file__)}
    launch={'started_utc':dt.datetime.now(dt.timezone.utc).isoformat(),'execution_enabled':False,'day':day,'phase':phase,'command':command,'code_sha256':code,'inputs':spec,'protocol_sha256':sha(root/'protocol.json'),'policies_sha256':sha(policies),'input_manifest_sha256':sha(root/'input-manifest.json')}
    if phase=='evaluation':launch['selection_frozen_sha256']=sha(root/'selection-frozen.json')
    write(launch_path,launch);write(status_path,{'status':'RUNNING','execution_enabled':False});start=time.monotonic()
    try:
        with log_path.open('x') as log:subprocess.run(command,stdout=log,stderr=subprocess.STDOUT,check=True)
        for p,h in code.items():
            if sha(p)!=h:raise ValueError('code changed during replay')
        if sha(policies)!=launch['policies_sha256'] or sha(root/'input-manifest.json')!=launch['input_manifest_sha256'] or sha(root/'protocol.json')!=launch['protocol_sha256']:raise ValueError('configuration changed during replay')
        for name,item in {**spec['files'],**spec['feature_evidence']}.items():
            if sha(item['path'])!=item['sha256']:raise ValueError('input changed during replay: '+name)
        aggregate=json.loads((target/'aggregate.json').read_text())
        if aggregate.get('execution_enabled') is not False:raise ValueError('missing offline marker')
        if [s['id'] for s in aggregate['scenarios']]!=[p['id'] for p in protocol['policies']]:raise ValueError('scenario bit order changed')
        status={'status':'COMPLETED_PENDING_AUDIT','execution_enabled':False,'elapsed_seconds':time.monotonic()-start,'aggregate_sha256':sha(target/'aggregate.json'),'scenario_count':len(aggregate['scenarios']),'completed_utc':dt.datetime.now(dt.timezone.utc).isoformat()}
        write(status_path,status);print(json.dumps({'date':day,**status}),flush=True)
    except BaseException as e:
        write(status_path,{'status':'FAILED','execution_enabled':False,'error':str(e),'elapsed_seconds':time.monotonic()-start});raise
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--experiment',required=True);p.add_argument('--day',required=True);p.add_argument('--phase',choices=['search','evaluation'],required=True);p.add_argument('--binary',default=str(HERE/'perfect_taker'));a=p.parse_args();run(a.experiment,a.day,a.phase,a.binary)

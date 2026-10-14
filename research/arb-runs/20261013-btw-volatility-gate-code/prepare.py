"""Bind original inputs and new causal gates before replay."""
import argparse
import datetime as dt
import json
from pathlib import Path
from run_day import sha,write,HERE,READER

def prepare(root):
    root=Path(root).resolve()
    if (root/'input-manifest.json').exists():raise FileExistsError('inputs already frozen')
    protocol=json.loads((root/'protocol.json').read_text())
    original=Path(protocol['original_input_manifest']);source=json.loads(original.read_text())
    features_path=root/'features/manifest.json';features=json.loads(features_path.read_text())
    if features['protocol_sha256']!=sha(root/'protocol.json') or features['policies_sha256']!=sha(root/'policies.csv') or features['code_sha256']!=sha(HERE/'build_features.py'):raise ValueError('feature builder binding mismatch')
    manifest={'created_utc':dt.datetime.now(dt.timezone.utc).isoformat(),'execution_enabled':False,'protocol_sha256':sha(root/'protocol.json'),'policies_sha256':sha(root/'policies.csv'),'original_input_manifest':{'path':str(original),'sha256':sha(original)},'features_manifest':{'path':str(features_path),'sha256':sha(features_path)},'execution_code_sha256':{str(p):sha(p) for p in [HERE/'perfect_taker',HERE/'perfect_taker.cpp',READER]},'feature_code_sha256':{str(HERE/'build_features.py'):sha(HERE/'build_features.py')},'days':{}}
    for day in protocol['fit_dates']+[protocol['evaluation_date']]:
        spec=source['days'][day];bundle=features['days'][day];meta=json.loads(Path(bundle['metadata']).read_text())
        if meta['tape']!=spec['files']['tape']:raise ValueError('feature/tape binding mismatch')
        spec['files']['entry_gates']=bundle['entry-gates.bin']
        spec['feature_evidence']={'grid':meta['grid'],'feature_npz':bundle['features.npz'],'coverage':bundle['coverage.json'],'feature_metadata':{'path':bundle['metadata'],'sha256':bundle['metadata_sha256']}}
        for item in [*spec['files'].values(),*spec['feature_evidence'].values(),spec['source_summary']]:
            if sha(item['path'])!=item['sha256']:raise ValueError('stale input: '+item['path'])
        manifest['days'][day]=spec
    write(root/'input-manifest.json',manifest)
    print(json.dumps({'status':'INPUTS_FROZEN','input_manifest_sha256':sha(root/'input-manifest.json')}))
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('experiment');prepare(p.parse_args().experiment)

"""Freeze existing public tapes and new native microstructure feature evidence."""
import argparse
import datetime as dt
import json
from pathlib import Path
from run_day import sha,write,HERE,READER

def item(path):
    path=Path(path).resolve();return {'path':str(path),'sha256':sha(path)}

def prepare(root):
    root=Path(root).resolve()
    if (root/'input-manifest.json').exists():raise FileExistsError('input manifest already frozen')
    protocol=json.loads((root/'protocol.json').read_text());original=Path(protocol['original_input_manifest']);old=json.loads(original.read_text())
    assert protocol['volatility_entry_filters_enabled'] is False
    feature_manifest=root/'features/manifest.json';features=json.loads(feature_manifest.read_text())
    if features['protocol_sha256']!=sha(root/'protocol.json') or features['policies_sha256']!=sha(root/'policies.csv'):raise ValueError('feature protocol/policies mismatch')
    manifest={'created_utc':dt.datetime.now(dt.timezone.utc).isoformat(),'execution_enabled':False,'protocol_sha256':sha(root/'protocol.json'),'policies_sha256':sha(root/'policies.csv'),'original_input_manifest':item(original),'features_manifest':item(feature_manifest),'execution_code_sha256':{str(p):sha(p) for p in [HERE/'perfect_taker',HERE/'perfect_taker.cpp',READER]},'feature_code_sha256':{str(p):sha(p) for p in [HERE/'build_features.py',HERE/'extract_book_events.py']},'days':{}}
    for day in protocol['fit_dates']+[protocol['evaluation_date']]:
        spec=old['days'][day];folder=root/'features'/day;book=root/'book-events'/day
        # Explicit paths prevent an index's untrusted relative path from escaping the experiment.
        index=folder/'feature-index.json';metadata=folder/'metadata.json';coverage=folder/'coverage.json'
        gates=folder/'entry-gates.bin';events=book/'events.bin';bookmeta=book/'metadata.json'
        activity_events=Path(spec['files']['activity']['path']).parent/'activity-events.bin'
        bundle=features['days'][day]
        if bundle['feature_index']!=item(index):raise ValueError('feature index not bound')
        if bundle['entry-gates.bin']!=item(gates):raise ValueError('gate not bound')
        spec['files'].update(entry_gates=item(gates),book_events=item(events),activity_events=item(activity_events))
        evidence={'feature_index':item(index),'feature_metadata':item(metadata),'coverage':item(coverage),'book_event_metadata':item(bookmeta),'features_manifest':item(feature_manifest)}
        # Every compressed chunk is part of the gate's frozen provenance even though the engine only reads masks.
        chunks=sorted(folder.glob('native-*.npz'))
        if not chunks:raise ValueError('no native feature chunks')
        for i,path in enumerate(chunks):evidence[f'feature_chunk_{i:05d}']=item(path)
        spec['feature_evidence']=evidence
        for label,source in {**spec['files'],**evidence,'source_summary':spec['source_summary']}.items():
            if sha(source['path'])!=source['sha256']:raise ValueError('input changed: '+label)
        manifest['days'][day]=spec
    write(root/'input-manifest.json',manifest)
    print(json.dumps({'status':'INPUTS_FROZEN','input_manifest_sha256':sha(root/'input-manifest.json')}))
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('experiment');prepare(p.parse_args().experiment)

"""Run one frozen public-data perfect-fill experiment, without exchange access."""
import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import subprocess
import time

HERE = Path(__file__).resolve().parent
READER = HERE.parent / '20261012-btw-as-code/cohort_replay.cpp'


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def validate_execution_code(frozen, binary):
    paths = (Path(binary).resolve(), HERE / 'perfect_taker.cpp', READER)
    current = {str(path): sha(path) for path in paths}
    if frozen.get('execution_code_sha256') != current:
        raise ValueError('evaluation execution code differs from frozen fit engine')


def run(root, day, phase, binary):
    root, binary = Path(root).resolve(), Path(binary).resolve()
    protocol = json.loads((root / 'protocol.json').read_text())
    manifest = json.loads((root / 'input-manifest.json').read_text())
    if sha(root / 'protocol.json') != manifest['protocol_sha256']:
        raise ValueError('protocol changed after input manifest')
    if phase == 'search':
        if day not in protocol['fit_dates']:
            raise ValueError('search may use only the declared fit dates')
        policies = root / 'policies.csv'
        if sha(policies) != manifest['policies_sha256']:
            raise ValueError('candidate policies changed')
    else:
        if day != protocol['evaluation_date']:
            raise ValueError('evaluation must use the declared evaluation date')
        frozen = json.loads((root / 'selection-frozen.json').read_text())
        policies = root / 'selected-policies.csv'
        if sha(policies) != frozen['selected_policies_sha256']:
            raise ValueError('evaluation policies differ from frozen selection')
        if frozen['protocol_sha256'] != sha(root / 'protocol.json'):
            raise ValueError('selection protocol mismatch')
        validate_execution_code(frozen, binary)
    spec = manifest['days'][day]
    for name, source in spec['files'].items():
        if sha(source['path']) != source['sha256']:
            raise ValueError('source hash mismatch: ' + name)
    if sha(spec['source_summary']['path']) != spec['source_summary']['sha256']:
        raise ValueError('source manifest changed')
    target = root / phase / day
    target.parent.mkdir(exist_ok=True)
    status_path = target.with_suffix('.status.json')
    launch_path = target.with_suffix('.launch.json')
    log_path = target.with_suffix('.log')
    if any(p.exists() for p in (target, status_path, launch_path, log_path)):
        raise FileExistsError('refusing to replace a previous run')
    files = spec['files']
    command = [str(binary), '--tape', files['tape']['path'],
               '--activity', files['activity']['path'],
               '--thresholds', files['thresholds']['path'],
               '--funding-csv', files['funding_csv']['path'],
               '--policies', str(policies), '--entry-start-ns', str(spec['entry_start_ns']),
               '--output', str(target)]
    code = {str(p): sha(p) for p in (binary, HERE / 'perfect_taker.cpp', READER, Path(__file__))}
    launch = {'started_utc': dt.datetime.now(dt.timezone.utc).isoformat(),
              'execution_enabled': False, 'day': day, 'phase': phase,
              'command': command, 'code_sha256': code, 'inputs': spec,
              'protocol_sha256': sha(root / 'protocol.json'), 'policies_sha256': sha(policies)}
    if phase == 'evaluation':
        launch['selection_frozen_sha256'] = sha(root / 'selection-frozen.json')
    write(launch_path, launch)
    write(status_path, {'status': 'RUNNING', 'execution_enabled': False})
    start = time.monotonic()
    try:
        with log_path.open('x') as log:
            subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)
        for name, expected in code.items():
            if sha(name) != expected:
                raise ValueError('source changed during run: ' + name)
        if sha(policies) != launch['policies_sha256']:
            raise ValueError('policies changed during run')
        result = json.loads((target / 'aggregate.json').read_text())
        if result.get('execution_enabled') is not False:
            raise ValueError('missing offline execution marker')
        status = {'status': 'COMPLETED_PENDING_AUDIT', 'execution_enabled': False,
                  'elapsed_seconds': time.monotonic() - start,
                  'aggregate_sha256': sha(target / 'aggregate.json'),
                  'scenario_count': len(result['scenarios']),
                  'completed_utc': dt.datetime.now(dt.timezone.utc).isoformat()}
        write(status_path, status)
        print(json.dumps({'date': day, **status}), flush=True)
    except BaseException as error:
        write(status_path, {'status': 'FAILED', 'execution_enabled': False,
                            'error': str(error), 'elapsed_seconds': time.monotonic() - start})
        raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment', required=True)
    parser.add_argument('--day', required=True)
    parser.add_argument('--phase', choices=('search', 'evaluation'), required=True)
    parser.add_argument('--binary', required=True)
    args = parser.parse_args()
    run(args.experiment, args.day, args.phase, args.binary)

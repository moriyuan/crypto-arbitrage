"""One-shot read-only audits of the already authorized, frozen offline run."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

CODE=Path(__file__).resolve().parent
BASE=CODE.parent
RUN=BASE/'20261012-btw-exit-relative-tp5-sl20-h100-20261001'
STATUS=BASE/'20261012-btw-exit-optimization/final-audits-status.json'

def status(value):
    value['heartbeat_epoch']=time.time()
    tmp=STATUS.with_suffix('.tmp')
    tmp.write_text(json.dumps(value,indent=2))
    tmp.replace(STATUS)

def main():
    started=time.time()
    while True:
        current=json.loads((RUN/'status.json').read_text())['status']
        status({'status':'WAITING_FOR_REPLAY','replay_status':current})
        if current=='COMPLETED':break
        if current not in ('RUNNING','FINALIZING','EXPORTING') or time.time()-started>3600:
            status({'status':'NOT_RUN','replay_status':current,'reason':'replay not completed or wait budget exceeded'});return 1
        time.sleep(5)
    commands={
        'accounting':[str(CODE/'audit_accounting.py'),str(RUN)],
        'depth':[str(CODE/'depth_fill_audit.py'),'--run-dir',str(RUN),'--output',str(RUN/'depth-fill-audit.json')],
        'risk':[str(CODE/'audit_risk.py'),str(RUN)],
        'selected_equivalence':[str(CODE/'compare_selected.py'),'--screen',str(BASE/'20261012-btw-exit-optimization/screen'),'--python-run',str(RUN),'--output',str(RUN/'selected-equivalence.json')]}
    processes={};logs={}
    env=os.environ.copy();env['PYTHONPATH']='/Users/mori/.codex/worktrees/crypto-arbitrage-research/crypto/src'
    sources={k:hashlib.sha256(Path(v[0]).read_bytes()).hexdigest() for k,v in commands.items()}
    for name,cmd in commands.items():
        logs[name]=(RUN/(name+'-audit.log')).open('xb')
        processes[name]=subprocess.Popen([sys.executable,'-u',*cmd],stdout=logs[name],stderr=subprocess.STDOUT,env=env)
    while True:
        returns={k:p.poll() for k,p in processes.items()}
        done=all(v is not None for v in returns.values())
        status({'status':('PASS' if all(v==0 for v in returns.values()) else 'FAIL') if done else 'AUDITING',
                'returncodes':returns,'pids':{k:p.pid for k,p in processes.items()},'audit_sha256':sources,'execution_enabled':False})
        if done:
            for f in logs.values():f.close()
            return 0 if all(v==0 for v in returns.values()) else 1
        time.sleep(5)

if __name__=='__main__':raise SystemExit(main())

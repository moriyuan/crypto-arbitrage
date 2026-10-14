"""Wait on small status file only, then run the independent read-only auditor once."""
from pathlib import Path
import json,subprocess,sys,time
P=Path('/Users/mori/code/crypto/state/arb-runs/20261009-btw-tardis-replay-20261001')
started=time.monotonic();last=None
while time.monotonic()-started<2700:
 try:state=json.loads((P/'status.json').read_text())['status']
 except (FileNotFoundError,json.JSONDecodeError):time.sleep(10);continue
 if state in ('COMPLETED','UNRESOLVED','FAILED','FUNDING_UNVERIFIED'):
  print(json.dumps({'audit_phase':'STARTING_READ_ONLY_AUDIT','replay_status':state}),flush=True)
  raise SystemExit(subprocess.call([sys.executable,str(Path(__file__).with_name('audit_replay.py')),str(P)]))
 if state!=last:
  print(json.dumps({'audit_phase':'WAITING_NO_LEDGER_SCAN','replay_status':state}),flush=True);last=state
 time.sleep(30)
print(json.dumps({'audit_phase':'WAIT_EXPIRED_NO_LEDGER_SCAN'}),flush=True)
raise SystemExit(4)

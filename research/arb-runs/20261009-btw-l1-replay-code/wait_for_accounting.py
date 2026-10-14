"""Wait at most 35 minutes using status only, then audit one terminal replay."""
from pathlib import Path
import json
import subprocess
import sys
import time

RUN = Path('/Users/mori/code/crypto/state/arb-runs/20261009-btw-l1-replay-20261001')
LOG = Path(str(RUN) + '.accounting-audit.log')
TERMINAL = {'COMPLETED', 'UNRESOLVED', 'FUNDING_UNVERIFIED', 'FAILED'}


def main():
    started = time.monotonic()
    last = None
    # Exclusive creation makes accidental duplicate launches fail before an audit.
    with LOG.open('x', encoding='utf-8') as log:
        def emit(value):
            line = json.dumps(value, ensure_ascii=False)
            print(line, flush=True)
            log.write(line + '\n')
            log.flush()

        emit({'audit_phase': 'BOUNDED_WAIT_STARTED', 'max_wait_seconds': 2100,
              'poll_seconds': 30, 'run': str(RUN), 'log': str(LOG)})
        while time.monotonic() - started < 2100:
            try:
                status = json.loads((RUN / 'status.json').read_text())['status']
            except (FileNotFoundError, json.JSONDecodeError, KeyError) as error:
                status = 'UNREADABLE_STATUS:' + type(error).__name__
            if status in TERMINAL:
                emit({'audit_phase': 'STARTING_READ_ONLY_AUDIT', 'replay_status': status,
                      'waited_seconds': time.monotonic() - started})
                process = subprocess.Popen(
                    [sys.executable, str(Path(__file__).with_name('audit_accounting.py')), str(RUN)],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
                )
                for line in process.stdout:
                    print(line, end='', flush=True)
                    log.write(line)
                    log.flush()
                result = process.wait()
                emit({'audit_phase': 'AUDIT_EXITED', 'return_code': result,
                      'acceptance_exists': (RUN / 'acceptance.json').exists()})
                return result
            if status != last:
                emit({'audit_phase': 'WAITING_NO_LEDGER_SCAN', 'replay_status': status})
                last = status
            remaining = 2100 - (time.monotonic() - started)
            if remaining > 0:
                time.sleep(min(30, remaining))
        emit({'audit_phase': 'WAIT_EXPIRED_NO_LEDGER_SCAN', 'waited_seconds': time.monotonic() - started})
        return 4


if __name__ == '__main__':
    raise SystemExit(main())

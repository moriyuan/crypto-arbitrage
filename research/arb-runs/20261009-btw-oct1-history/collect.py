"""Bounded public-only warmup collector; never places or simulates orders."""
import asyncio
import gzip
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent
BASE = ROOT.parent
SOURCE = BASE / '20261009T032518Z-20d-history-backfill'
UNIVERSE = BASE / '20261009-full-crypto-universe-audit.json'
SOURCE_CODE = Path('/Users/mori/.codex/worktrees/crypto-arbitrage-research/crypto/src')
sys.path.insert(0, str(SOURCE_CODE))
from btc_cta.cross_arb.history_gate import _EXCLUDED_BASE

HOUR = 3600000
START = 1789081200000  # 2026-09-10 23:00 UTC: anchor for Sep11 daily RV.
END = 1790812800000  # 2026-10-01 00:00 UTC, exclusive.
OLD_END = 1789689600000  # Sep18 00:00; includes Sep17 23:00 overlap.

def utc(): return datetime.now(timezone.utc).isoformat()
def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()
def write(path, value):
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    temp.replace(path)

def eligible(row):
    """Same economic exclusions as the old pool; ST alone is no exclusion."""
    b, g, symbol = row['bybit'], row['bitget'], row['symbol']
    return (symbol == b['symbol'] == g['symbol'] and b['baseCoin'] == g['baseCoin'] == symbol[:-4]
            and symbol[:-4] not in _EXCLUDED_BASE
            and b['contractType'] == 'LinearPerpetual' and g['symbolType'] == 'perpetual'
            and b['quoteCoin'] == b['settleCoin'] == g['quoteCoin'] == 'USDT'
            and 'USDT' in g['supportMarginCoins'] and b['status'] == 'Trading'
            and g['symbolStatus'] == 'normal' and not b.get('isPreListing')
            and b.get('symbolType') in ('', 'innovation', None)
            and not b.get('underlyingTicker') and g.get('isRwa') == 'NO')

async def main():
    if (ROOT / 'config.json').exists():
        raise RuntimeError('Frozen run already exists; use preserved state, do not overwrite')
    (ROOT / 'raw').mkdir(exist_ok=True)
    universe = json.loads(UNIVERSE.read_text())
    old = set(json.loads((SOURCE / 'provenance.json').read_text())['symbols'])
    rows = [r for r in universe['eligible'] if eligible(r)]
    symbols = [r['symbol'] for r in rows]
    assert len(symbols) == 420 and old <= set(symbols)
    late = {'CTUSDT', 'STONKUSDT'}
    # Their observed listings are after the required Sep11 daily start.
    for symbol in late:
        row = next(r for r in rows if r['symbol'] == symbol)
        assert int(row['bybit']['launchTime']) > START + HOUR
    plan = [{'venue': v, 'symbol': s, 'start_ms': START,
             'end_exclusive_ms': OLD_END if s in old else END}
            for s in symbols if s not in late for v in ('bybit', 'bitget')]
    config = {'created_utc': utc(), 'replay_day_utc': '2026-10-01',
              'execution_enabled': False, 'source_worktree_modified': False,
              'public_get_only': True, 'proxy': 'http://127.0.0.1:7897', 'trust_env': False,
              'max_attempts': 3, 'request_rate_per_venue': 4, 'total_requests': len(plan),
              'symbols': symbols, 'pool_count': len(symbols), 'original_pool_count': len(old),
              'added_ST_symbols': sorted(set(symbols) - old),
              'excluded_USDC_reason': 'Crypto stablecoin; retained existing stablecoin exclusion, not classified as a stock.',
              'USDC_identity_source': 'https://www.circle.com/usdc',
              'universe_sha256': sha(UNIVERSE), 'source_directory': str(SOURCE),
              'source_manifest_sha256': sha(SOURCE / 'raw-manifest.json'),
              'collector_sha256': sha(ROOT / 'collect.py'),
              'builder_sha256': sha(ROOT / 'build_gate.py'),
              'history_gate_sha256': sha(SOURCE_CODE / 'btc_cta/cross_arb/history_gate.py'),
              'universe': rows, 'requests': plan, 'known_short_history_symbols': sorted(late),
              'window_days': 20, 'top_n': 10, 'min_turnover_usdt': '0', 'min_volatility_bps': '0',
              'publication_delay_ms': 60000,
              'first_60_seconds_policy': 'reject: prior Sep10 daily window not acquired',
              'identity_scope': 'Directory filters exclude TradFi/RWA; prior manually verified identities control admission only, never ranking. ST is a label and not an exclusion.',
              'caveats': ['Oct9 instrument directory is not point-in-time and has survivorship bias.',
                          'Completed candles assumed available after 60 seconds; actual historic publication and revisions unverified.',
                          'BTW selected after Oct8 research; Oct1 replay is retrospective, not out-of-sample validation.',
                          'Seven unverified cross-name multiplier pairs remain outside the exact-symbol pool.']}
    write(ROOT / 'config.json', config)
    status = {'status': 'RUNNING', 'started_utc': utc(), 'heartbeat_utc': utc(),
              'completed_requests': 0, 'total_requests': len(plan), 'errors': [],
              'execution_enabled': False}
    write(ROOT / 'status.json', status)
    manifest, attempts = {}, []
    locks = {v: asyncio.Lock() for v in ('bybit', 'bitget')}
    last = {v: 0. for v in locks}
    sem = asyncio.Semaphore(12)
    async with httpx.AsyncClient(proxy=config['proxy'], trust_env=False, timeout=20,
                                limits=httpx.Limits(max_connections=12)) as client:
        async def one(item):
            async with sem:
                v, s, start, end = item['venue'], item['symbol'], item['start_ms'], item['end_exclusive_ms']
                endpoint = '/v5/market/kline' if v == 'bybit' else '/api/v2/mix/market/candles'
                params = ({'category': 'linear', 'symbol': s, 'interval': '60', 'limit': 1000,
                           'start': start, 'end': end - 1} if v == 'bybit' else
                          {'productType': 'USDT-FUTURES', 'symbol': s, 'granularity': '1H',
                           'limit': 1000, 'startTime': start - HOUR, 'endTime': end, 'kLineType': 'market'})
                host = 'https://api.bybit.com' if v == 'bybit' else 'https://api.bitget.com'
                errors, success = [], False
                for attempt in range(1, 4):
                    async with locks[v]:
                        await asyncio.sleep(max(0, last[v] + .25 - time.monotonic()))
                        last[v] = time.monotonic()
                    name = f'raw/{v}_{s}_attempt{attempt}.json.gz'
                    try:
                        response = await client.get(host + endpoint, params=params)
                        body = response.json()
                        payload = {'venue': v, 'endpoint': endpoint, 'params': params,
                                   'fetched_utc': utc(), 'http_status': response.status_code,
                                   'response_text': response.text,
                                   'response_body_sha256': hashlib.sha256(response.content).hexdigest(),
                                   'response': body}
                        raw = gzip.compress(json.dumps(payload, allow_nan=False).encode())
                        (ROOT / name).write_bytes(raw)
                        digest = hashlib.sha256(raw).hexdigest()
                        attempts.append({'path': name, 'sha256': digest, 'venue': v, 'symbol': s})
                        response.raise_for_status()
                        code = body.get('retCode') if v == 'bybit' else body.get('code')
                        if code != (0 if v == 'bybit' else '00000'):
                            raise ValueError('API code ' + str(code))
                        manifest[v + '_' + s] = {'path': name, 'sha256': digest, **item}
                        success = True
                        break
                    except (httpx.HTTPError, ValueError) as exc:
                        errors.append(type(exc).__name__ + ': ' + str(exc)[:200])
                        if attempt < 3: await asyncio.sleep(attempt)
                if not success: status['errors'].append({'venue': v, 'symbol': s, 'errors': errors})
                status.update(completed_requests=status['completed_requests'] + 1, heartbeat_utc=utc())
                write(ROOT / 'status.json', status)
                if status['completed_requests'] % 50 == 0:
                    write(ROOT / 'raw-manifest.json', manifest)
                    write(ROOT / 'attempts-manifest.json', attempts)
                    print(json.dumps({k:status[k] for k in ('completed_requests','total_requests','heartbeat_utc','errors')}), flush=True)
        try:
            await asyncio.wait_for(asyncio.gather(*(one(x) for x in plan)), 600)
        except TimeoutError:
            status['errors'].append({'error': '600 second collection timeout'})
    write(ROOT / 'raw-manifest.json', manifest)
    write(ROOT / 'attempts-manifest.json', attempts)
    status.update(status='FAILED' if status['errors'] else 'COLLECTED', heartbeat_utc=utc())
    write(ROOT / 'status.json', status)
    if status['errors']: raise RuntimeError('Collection incomplete; no gate admission allowed')
    from build_gate import produce
    result = produce()
    status.update(status='COMPLETED', finished_utc=utc(), heartbeat_utc=utc(),
                  btw_rank=result['btw']['history_rank'], btw_allowed=result['btw']['history_filter_passed'])
    write(ROOT / 'status.json', status)
    print(json.dumps(status), flush=True)

if __name__ == '__main__': asyncio.run(main())

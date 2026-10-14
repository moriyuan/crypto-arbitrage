"""Immutable Oct1 historical gate builder; no network or order interfaces."""
import gzip
import hashlib
import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CODE = Path('/Users/mori/.codex/worktrees/crypto-arbitrage-research/crypto/src')
sys.path.insert(0, str(CODE))
from btc_cta.cross_arb.history_gate import HistoryGate, RollingHistoryConfig, _read_bars

START = 1789081200000
END = 1790812800000
HOUR = 3600000

def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()
def write(path, value): path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
def raw_values(path):
    payload = json.loads(gzip.decompress(path.read_bytes()))
    response = payload['response']
    rows = response['result']['list'] if payload['venue'] == 'bybit' else response['data']
    return {int(r[0]): tuple(Decimal(str(v)) for v in r[1:7]) for r in rows}

def build_gate():
    config = json.loads((ROOT/'config.json').read_text())
    if json.loads((ROOT/'status.json').read_text())['status'] not in ('COLLECTED','COMPLETED'):
        raise ValueError('collection not complete')
    source = Path(config['source_directory'])
    if sha(source/'raw-manifest.json') != config['source_manifest_sha256']:
        raise ValueError('source manifest hash mismatch')
    if sha(CODE/'btc_cta/cross_arb/history_gate.py') != config['history_gate_sha256']:
        raise ValueError('gate source changed')
    old_manifest = json.loads((source/'raw-manifest.json').read_text())
    new_manifest = json.loads((ROOT/'raw-manifest.json').read_text())
    old = set(json.loads((source/'provenance.json').read_text())['symbols'])
    identity = json.loads((source/'selection-crypto-verified.json').read_text())
    if identity['source_universe_sha256'] != sha(source/'universe.json'):
        raise ValueError('source identity not bound to universe')
    verified = {r['symbol'] for r in identity['top10']
                if r['asset_identity']['classification'] == 'crypto_project_token'
                and r['asset_identity']['perpetual_symbol'] == r['symbol']
                and r['asset_identity']['official_project_identity_source']}
    data, source_hashes, overlap_count, new_old_overlaps, normalized = {}, {}, 0, 0, []
    for symbol in config['symbols']:
        data[symbol] = {}
        for venue in ('bybit','bitget'):
            paths = []
            if symbol in old:
                for sub in ('source-raw','backfill-raw'):
                    name = f'{sub}/{venue}_{symbol}.json.gz'
                    paths.append((source/name, old_manifest[name], 'old'))
            key = venue+'_'+symbol
            if symbol not in config['known_short_history_symbols']:
                record = new_manifest[key]
                paths.append((ROOT/record['path'],record['sha256'],'new'))
            merged, full, origins = {}, {}, {}
            for path, digest, origin in paths:
                bars = _read_bars(path,venue,symbol,digest)
                values = raw_values(path)
                source_hashes[str(path)] = digest
                for bar in bars:
                    if bar.start_ms in merged:
                        if merged[bar.start_ms] != bar or full[bar.start_ms] != values[bar.start_ms]:
                            raise ValueError(f'overlap conflict: {symbol} {venue} {bar.start_ms}')
                        overlap_count += 1
                        if origins[bar.start_ms] == 'old' and origin == 'new':new_old_overlaps += 1
                    merged[bar.start_ms],full[bar.start_ms],origins[bar.start_ms] = bar,values[bar.start_ms],origin
            selected = tuple(merged[t] for t in sorted(merged) if START <= t < END)
            data[symbol][venue] = selected
            normalized.append({'symbol':symbol,'venue':venue,'bars':[{'start_ms':b.start_ms,'close':str(b.close),'turnover_usdt':str(b.turnover_usdt)} for b in selected],
                               'missing_hour_starts_ms':sorted(set(range(START,END,HOUR))-{b.start_ms for b in selected})})
    audit = {'source_paths_sha256':source_hashes,'archive_count':len(source_hashes),
             'overlap_bars_equal':overlap_count,'new_old_overlap_bars_equal':new_old_overlaps,
             'pool_count':len(data),'scope':'420 exact-symbol current-directory crypto candidates, including 35 ST; existing stablecoin/BTC/TradFi/RWA exclusions preserved.',
             'point_in_time_directory_verified':False,'publication_delay_verified':False,
             'caveats':config['caveats'],'normalized_window_start_ms':START,'normalized_window_end_exclusive_ms':END}
    gate = HistoryGate(RollingHistoryConfig(),data,identity_verified_symbols=verified,audit_metadata=audit)
    return gate,normalized

def produce():
    gate,normalized = build_gate()
    times = [END,END+59999,END+60000,END+3600000,END+86399999]
    checks = [{'decision_time_ms':t,'btw':gate.evaluate('BTWUSDT',t)} for t in times]
    assert all(not x['btw']['history_filter_passed'] for x in checks[:2])
    rows = [dict(symbol=s,**gate.evaluate(s,END+60000)) for s in gate.metadata()['symbols']]
    rows.sort(key=lambda r:(r['history_rank'] or 1000000,r['symbol']))
    result = {'status':'COMPLETED_WITH_POINT_IN_TIME_LIMITATIONS','execution_enabled':False,
              'evaluation_time_ms':END+60000,'daily_window_start_ms':END-20*86400000,
              'daily_window_end_exclusive_ms':END,'pool_count':len(rows),'ranked_count':sum(r['history_rank'] is not None for r in rows),
              'top10':[r for r in rows if r['history_rank'] and r['history_rank']<=10],
              'btw':next(r for r in rows if r['symbol']=='BTWUSDT'),'rows':rows,'checks':checks,
              'metadata':gate.metadata()}
    write(ROOT/'gate-input.json',{'schema_version':1,'execution_enabled':False,'config':gate.metadata()['config'],
                                 'available_from_ms':END+60000,'valid_until_exclusive_ms':END+86400000,
                                 'btw':result['btw'],'first_60_seconds':checks[0]['btw'],
                                 'pool_count':len(rows),'ranking_file':'ranking-20261001.json',
                                 'caveats':gate.metadata()['audit']['caveats']})
    write(ROOT/'normalized-hourly.json',{'window_start_ms':START,'window_end_exclusive_ms':END,'data':normalized})
    write(ROOT/'ranking-20261001.json',result)
    write(ROOT/'acceptance.json',{'status':'PASS_WITH_LIMITATIONS','execution_enabled':False,
                                 'created_utc':datetime.now(timezone.utc).isoformat(),
                                 'files_sha256':{n:sha(ROOT/n) for n in ('config.json','raw-manifest.json','gate-input.json','normalized-hourly.json','ranking-20261001.json')},
                                 'pool_count':len(rows),'ranked_count':result['ranked_count'],
                                 'btw':result['btw'],'checks':checks,
                                 'archive_count':gate.metadata()['audit']['archive_count'],
                                 'overlap_bars_equal':gate.metadata()['audit']['overlap_bars_equal'],
                                 'new_old_overlap_bars_equal':gate.metadata()['audit']['new_old_overlap_bars_equal'],
                                 'incomplete_symbols':[r['symbol'] for r in rows if r['history_filter_reason']=='history_incomplete'],
                                 'caveats':gate.metadata()['audit']['caveats']})
    return result

if __name__=='__main__': print(json.dumps(produce()['btw']))

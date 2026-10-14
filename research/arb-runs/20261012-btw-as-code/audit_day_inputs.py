"""Independent, offline semantic audit of the new historical gate/funding bundles."""
import argparse
import calendar
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal as D
import gzip
import hashlib
import json
import math
from pathlib import Path

HOUR = 3_600_000
DAY = 24 * HOUR

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def j(path):
    return json.loads(path.read_text())

def ns(text):
    whole, fraction = text[:-1].split('.')
    return calendar.timegm(datetime.strptime(whole, '%Y-%m-%dT%H:%M:%S').timetuple()) * 10**9 + int(fraction.ljust(9, '0'))

def history(root):
    config = j(root / 'config.json')
    end, start = config['end_ms'], config['start_ms']
    assert end % DAY == 0 and start == end - 21 * DAY - HOUR
    symbols = config['symbols']
    assert len(symbols) == len(set(symbols)) == 420
    manifest = j(root / 'raw-manifest.json')
    assert len(manifest) == config['request_count']
    expected = set()
    for symbol in set(symbols) - set(config['known_short_history_symbols']):
        expected.add(('bybit', symbol, start, end))
        for begin in range(start, end, 200 * HOUR):
            expected.add(('bitget', symbol, begin, min(end, begin + 200 * HOUR)))
    actual = {(r['venue'], r['symbol'], r['start'], r['end']) for r in manifest}
    assert actual == expected and len(actual) == len(manifest)
    merged = {(s, v): {} for s in symbols for v in ('bybit', 'bitget')}
    counts = Counter()
    for item in manifest:
        path = root / item['path']
        assert sha(path) == item['sha256']
        payload = json.loads(gzip.decompress(path.read_bytes()))
        assert payload['venue'] == item['venue'] and payload['http_status'] == 200
        assert hashlib.sha256(payload['response_text'].encode()).hexdigest() == payload['response_body_sha256']
        assert json.loads(payload['response_text']) == payload['response']
        params, response = payload['params'], payload['response']
        assert params['symbol'] == item['symbol']
        if item['venue'] == 'bybit':
            assert payload['url'] == 'https://api.bybit.com/v5/market/kline'
            assert params['category'] == 'linear' and params['interval'] == '60'
            assert (params['start'], params['end']) == (item['start'], item['end'] - 1)
            assert response['retCode'] == 0 and response['result']['category'] == 'linear'
            assert response['result']['symbol'] == item['symbol']
            rows = response['result']['list']
        else:
            assert payload['url'] == 'https://api.bitget.com/api/v2/mix/market/history-candles'
            assert params['productType'] == 'USDT-FUTURES' and params['granularity'] == '1H'
            assert params['kLineType'] == 'market' and params['limit'] == 200
            assert (params['startTime'], params['endTime']) == (item['start'] - 1, item['end'])
            assert response['code'] == '00000'
            rows = response['data']
        dest = merged[item['symbol'], item['venue']]
        for raw in rows:
            t = D(raw[0]); assert t == int(t) and int(t) % HOUR == 0
            t = int(t); values = tuple(D(x) for x in raw[1:7]); assert len(values) == 6
            o, h, lo, c, vol, turnover = values
            assert all(v.is_finite() for v in values)
            assert 0 < lo <= min(o, c) <= max(o, c) <= h and vol >= 0 and turnover >= 0
            counts['raw_candle_rows'] += 1
            counts['response_rows_outside_own_chunk'] += not item['start'] <= t < item['end']
            if not start <= t < end:
                counts['rows_outside_global_window_ignored'] += 1
                continue
            if t in dest:
                assert dest[t] == values
                counts['identical_overlap_rows'] += 1
            dest[t] = values
    normalized = j(root / 'normalized-hourly.json')
    assert (normalized['window_start_ms'], normalized['window_end_exclusive_ms']) == (start, end)
    assert len(normalized['data']) == 840
    for row in normalized['data']:
        raw = merged[row['symbol'], row['venue']]
        assert row['missing_hours'] == 505 - len(raw)
        assert row['bars'] == [{'start_ms': t, 'close': str(raw[t][3]), 'turnover_usdt': str(raw[t][5])} for t in sorted(raw)]
    published = {r['symbol']: r for r in j(root / ('ranking-' + config['date'].replace('-', '') + '.json'))['rows']}
    gate = j(root / 'gate-input.json')
    assert gate['available_from_ms'] == end + 60000 and gate['valid_until_exclusive_ms'] == end + DAY
    assert gate['config']['publication_delay_ms'] == 60000 and gate['config']['window_days'] == 20
    assert D(gate['config']['min_turnover_usdt']) == 0 and gate['config']['top_n'] == 10
    rankings = {}
    for cut in (end - DAY, end):
        score = {}; ranking = []
        for symbol in symbols:
            venue_scores = {}
            for venue in ('bybit', 'bitget'):
                bars = merged[symbol, venue]; daily = []
                for day in range(cut - 20 * DAY, cut, DAY):
                    times = list(range(day - HOUR, day + DAY, HOUR))
                    if not all(t in bars for t in times): break
                    returns = [math.log(float(bars[b][3] / bars[a][3])) for a, b in zip(times, times[1:])]
                    daily.append((D(str(10000 * math.sqrt(math.fsum(r*r for r in returns)))), sum((bars[t][5] for t in times[1:]), D(0))))
                if len(daily) == 20:
                    venue_scores[venue] = tuple(sum((v[i] for v in daily), D(0)) / 20 for i in (0, 1))
            score[symbol] = venue_scores
            if len(venue_scores) == 2:
                ranking.append((-min(v[0] for v in venue_scores.values()), -min(v[1] for v in venue_scores.values()), symbol))
        ranking.sort(); ranks = {row[2]: i + 1 for i, row in enumerate(ranking)}
        rankings[str(cut)] = {'ranked_count': len(ranking), 'btw_rank': ranks.get('BTWUSDT')}
        targets = published if cut == end else {'BTWUSDT': gate['first_60_seconds']}
        for symbol, record in targets.items():
            assert record['history_rank'] == ranks.get(symbol)
            assert (record['history_window_start_ms'], record['history_window_end_ms']) == (cut - 20 * DAY, cut)
            for venue in ('bybit', 'bitget'):
                for i, field in enumerate(('rv20_bps', 'adv20_usdt')):
                    wanted = str(score[symbol][venue][i]) if venue in score[symbol] else None
                    assert record[venue + '_' + field] == wanted
        counts['independent_rank_checks'] += len(targets)
    for name, digest in j(root / 'acceptance.json')['files_sha256'].items():
        assert sha(root / name) == digest
    return {'status': 'PASS', 'date': config['date'], 'checks': dict(counts), 'windows': rankings,
            'btw': gate['btw'], 'limitations': config['caveats'],
            'source_sha256': {name: sha(root / name) for name in ('config.json', 'raw-manifest.json', 'gate-input.json', 'acceptance.json')}}

def funding(root):
    manifest = j(root / 'manifest.json'); day = j(root / 'events.json')['target_day_utc']
    start, end = manifest['window_start_ms'], manifest['window_end_ms_exclusive']
    assert end - start == DAY
    rows = {'bybit': {}, 'bitget': {}}
    for request in manifest['requests']:
        path = root / request['file']; assert sha(path) == request['sha256']
        body = j(path); venue = request['venue']; params = request['params']
        assert params['symbol'] == 'BTWUSDT'
        if venue == 'bybit':
            assert request['url'] == 'https://api.bybit.com/v5/market/funding/history'
            assert params['category'] == 'linear' and body['retCode'] == 0
            actual = body['result']['list']
        else:
            assert request['url'] == 'https://api.bitget.com/api/v2/mix/market/history-fund-rate'
            assert params['productType'] == 'USDT-FUTURES' and body['code'] == '00000'
            actual = body['data']
        for row in actual:
            assert row['symbol'] == 'BTWUSDT'
            t = int(row['fundingRateTimestamp' if venue == 'bybit' else 'fundingTime'])
            rate = D(row['fundingRate']); assert rate.is_finite() and abs(rate) <= 1
            assert t not in rows[venue] or rows[venue][t] == rate
            rows[venue][t] = rate
    parsed = j(root / 'events.json')['venues']
    for venue in rows:
        times = sorted(rows[venue]); before = [t for t in times if t < start]; inside = [t for t in times if start <= t < end]; after = [t for t in times if t >= end]
        assert before and inside and after
        wanted = before[-1:] + inside + after[:1]
        assert [r['timestamp_ms'] for r in parsed[venue]['selected_events']] == wanted
        for record in parsed[venue]['selected_events']:
            assert D(record['funding_rate']) == rows[venue][record['timestamp_ms']]
            assert record['settlement_mark_price'] is None
    schedule_manifest = j(root / 'schedule-manifest.json')
    assert len(schedule_manifest['requests']) == 48
    assert {(r['venue'], r['params']['offset']) for r in schedule_manifest['requests']} == {(v, o) for v in rows for o in range(0, 1440, 60)}
    observations = []
    for request in schedule_manifest['requests']:
        raw = (root / request['file']).read_bytes(); assert hashlib.sha256(raw).hexdigest() == request['sha256']
        content = gzip.decompress(raw); assert hashlib.sha256(content).hexdigest() == request['body_sha256']
        venue = request['venue']; channel = 'tickers' if venue == 'bybit' else 'ticker'
        assert json.loads(request['params']['filters']) == [{'channel': channel, 'symbols': ['BTWUSDT']}]
        assert request['params']['from'] == day and request['params']['sliceSize'] == 1
        for line in content.decode().splitlines():
            if ' ' not in line: continue
            local, body = line.split(' ', 1)
            if not body.strip(): continue
            message = json.loads(body)
            topic = message.get('topic', message.get('arg', {}).get('topic'))
            if topic != ('tickers.BTWUSDT' if venue == 'bybit' else 'ticker'): continue
            if venue == 'bitget': assert message['arg']['symbol'] == 'BTWUSDT'
            data = message.get('data', []); data = [data] if isinstance(data, dict) else data
            for row in data:
                if 'nextFundingTime' not in row: continue
                known, nxt = ns(local), int(row['nextFundingTime'])
                assert start * 10**6 <= known < end * 10**6
                if known < nxt * 10**6:
                    assert nxt in rows[venue]
                    observations.append((known, venue, nxt, request['file']))
    observations.sort(); earliest = {}
    for known, venue, nxt, filename in observations:
        earliest.setdefault((venue, nxt), (known, filename))
    expected = sorted((known, venue, nxt, filename) for (venue, nxt), (known, filename) in earliest.items())
    actual = j(root / 'causal-schedule.json')['observations']
    assert [(r['known_at_ns'], r['venue'], r['next_funding_timestamp_ms'], r['source_file']) for r in actual] == expected
    for venue in rows:
        all_next = [r[2] for r in observations if r[1] == venue]
        assert all_next == sorted(all_next), 'Schedule revisions require preserving later repeated announcements'
    return {'status': 'PASS', 'date': day, 'raw_history_requests': len(manifest['requests']), 'ticker_files': 48,
            'actual_day_events': {v: parsed[v]['target_day_event_count'] for v in rows},
            'first_known_ns': {v: min(k for k, x, n, f in expected if x == v) for v in rows},
            'schedule_first_observation_only_reduction_valid_for_actual_data': True,
            'funding_amount_verified': False,
            'limitation': 'Hourly first-minute schedule observation, not continuous schedule monitoring. Missing mark prices prohibit settlement cash computation; require proven no crossing for net PnL.',
            'source_sha256': {name: sha(root / name) for name in ('manifest.json', 'events.json', 'schedule-manifest.json', 'causal-schedule.json', 'acceptance.json')}}

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--day-root', type=Path, required=True)
    parser.add_argument('--kind', choices=('history', 'funding'), required=True)
    args = parser.parse_args(); root = args.day_root.resolve()
    result = (history if args.kind == 'history' else funding)(root / args.kind)
    result.update(script_sha256=sha(Path(__file__)), execution_enabled=False)
    target = root / (args.kind + '-independent-audit.json')
    with target.open('x') as f: json.dump(result, f, indent=2, allow_nan=False); f.write('\n')
    print(json.dumps({'status': result['status'], 'kind': args.kind, 'path': str(target)}))

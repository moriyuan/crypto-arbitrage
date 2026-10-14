"""Read-only independent Decimal comparison of lifecycle CSV/summary exports."""
from collections import Counter
from decimal import Decimal as D
from pathlib import Path
import argparse
import csv
import hashlib
import json


def sha(path):
    with open(path, 'rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def jr(path):
    return json.loads(Path(path).read_text())


def jl(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines()]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('experiment')
    args = parser.parse_args()
    root = Path(args.experiment)
    stats = jr(root/'statistics.json')
    assert len(stats) == 3 and len({r['date_utc'] for r in stats}) == 3
    checks = 0
    days = []
    for stat in stats:
        folder = root/'validated-days'/stat['date_utc']
        summary, audit, funding = [jr(folder/n) for n in ('summary.json','independent-audit.json','funding-interval-audit.json')]
        rows, events = jl(folder/'cycles.jsonl'), jl(folder/'events.jsonl')
        assert audit['status'] == 'PASS'
        for n in ('summary','cycles','events'):
            ext = '.json' if n == 'summary' else '.jsonl'
            assert audit[n+'_sha256'] == sha(folder/(n+ext))
        filled = [r for r in rows if r['maker_open_qty']]
        closed = [r for r in filled if r['pnl_complete_before_funding']]
        residual = [r for r in rows if any(r['venue_positions'].values())]
        fills = [e for e in events if e['type']=='fill']
        expected = {'date_utc': summary['date_utc'], 'candidates':len(rows),
            'maker_filled_opportunities':len(filled),'closed_filled_opportunities':len(closed),
            'residual_opportunities':len(residual),
            'unresolved_flat_opportunities':sum(r['state']=='UNRESOLVED' and not any(r['venue_positions'].values()) for r in rows),
            'unhedged_residual_qty':sum(r['unhedged_open_qty'] for r in rows),
            'paired_residual_qty':sum(r['paired_open_qty'] for r in rows),
            'fill_records':len(fills),'fill_intents':dict(Counter(e['intent'] for e in fills)),
            'unhedged_timeout_request_count':sum(e['type']=='unhedged_exit_request' and e['reason']=='unhedged_timeout' for e in events),
            'paired_exit_reasons':dict(Counter(r['paired_exit_reason'] for r in rows if r['paired_exit_reason'])),
            'closed_opportunities_net_before_funding_sum':str(sum((D(r['net_before_funding']) for r in closed),D(0))),
            'funding_status':funding['status'],'independent_audit_status':audit['status'],
            'execution_enabled':False,'portfolio_aggregation_valid':False}
        assert set(stat) == set(expected)|{'scope'}
        assert 'not a continuous portfolio or final net return' in stat['scope']
        for k, value in expected.items():
            assert stat[k] == value, (summary['date_utc'],k)
            checks += 1
        with (folder/'cycles.csv').open() as stream:
            output = list(csv.DictReader(stream))
        assert len(output) == len(rows)
        for actual, source in zip(output, rows):
            for k, value in actual.items():
                assert value == ('' if source[k] is None else str(source[k])), (summary['date_utc'],'cycle field',k)
                checks += 1
        with (folder/'fills.csv').open() as stream:
            output = list(csv.DictReader(stream))
        assert len(output) == len(fills)
        ps, ms = summary['price_schema']['price_decimals'], summary['price_schema']['money_decimals']
        for actual, source in zip(output, fills):
            for k,value in actual.items():
                if k in ('price','fee','gross_realized'):
                    units, exponent = {'price':('price_units',ps),'fee':('fee_units',ms),'gross_realized':('gross_realized_units',ms)}[k]
                    assert D(value)*10**exponent == source[units], (summary['date_utc'],'fill numeric',k)
                elif k == 'venue':
                    assert value == ('bybit','bitget')[source[k]]
                else:
                    assert value == ('' if source.get(k) is None else str(source.get(k,''))), (summary['date_utc'],'fill field',k)
                checks += 1
        days.append({'date':summary['date_utc'],'cycle_rows':len(rows),'fill_rows':len(fills),
            'funding_status':funding['status'],'closed_partial_leg_crossings':len(funding['closed_partial_leg_crossings']),
            'file_sha256':{n:sha(folder/n) for n in ('cycles.jsonl','events.jsonl','cycles.csv','fills.csv','summary.json','independent-audit.json','funding-interval-audit.json')}})
    report = {'status':'PASS','method':'Independent JSON-to-CSV all-field comparison and Decimal scale verification; statistics recomputed from final validated-days JSONL, no production imports.',
        'checks':checks,'days':days,'statistics_sha256':sha(root/'statistics.json'),'audit_code_sha256':sha(__file__),
        'scope':'Conditional sums before funding remain before funding even for flat positions. Sep cycle2 closed legs crossed a settlement; no final strategy return is inferred.'}
    with (root/'export-audit.json').open('x') as stream:
        json.dump(report,stream,indent=2);stream.write('\n')
    print(json.dumps({'status':'PASS','checks':checks,'rows':[(d['date'],d['cycle_rows'],d['fill_rows']) for d in days]}))


if __name__ == '__main__':
    main()

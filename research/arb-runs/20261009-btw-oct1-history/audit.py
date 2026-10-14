"""Independent raw-to-rank recomputation; does not import the gate implementation."""
import gzip
import hashlib
import json
import math
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

ROOT=Path(__file__).resolve().parent
def read(name):return json.loads((ROOT/name).read_text())
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()

def main():
    config=read('config.json');ranking=read('ranking-20261001.json');meta=ranking['metadata']['audit']
    assert config['collector_sha256']==sha(ROOT/'collect.py')
    assert config['builder_sha256']==sha(ROOT/'build_gate.py')
    assert len(config['symbols'])==len(set(config['symbols']))==420
    raw=defaultdict(dict);dupes=0;body_verified=0
    start,end=1789081200000,1790812800000
    for name,digest in meta['source_paths_sha256'].items():
        path=Path(name);assert sha(path)==digest
        payload=json.loads(gzip.decompress(path.read_bytes()))
        venue,symbol=payload['venue'],payload['params']['symbol']
        if 'response_text' in payload:
            assert hashlib.sha256(payload['response_text'].encode()).hexdigest()==payload['response_body_sha256']
            assert json.loads(payload['response_text'])==payload['response']
            body_verified+=1
        rows=payload['response']['result']['list'] if venue=='bybit' else payload['response']['data']
        for row in rows:
            ts=int(row[0]);vals=tuple(Decimal(str(v)) for v in row[1:7])
            key=(symbol,venue)
            if ts in raw[key]:assert raw[key][ts]==vals;dupes+=1
            raw[key][ts]=vals
    assert body_verified==836
    attempts=read('attempts-manifest.json')
    for item in attempts:
        assert sha(ROOT/item['path'])==item['sha256']
        json.loads(gzip.decompress((ROOT/item['path']).read_bytes()))
    normalized=read('normalized-hourly.json')
    for row in normalized['data']:
        source=raw[(row['symbol'],row['venue'])]
        expect=[{'start_ms':t,'close':str(source[t][3]),'turnover_usdt':str(source[t][5])} for t in sorted(source) if start<=t<end]
        assert row['bars']==expect
        assert row['missing_hour_starts_ms']==sorted(set(range(start,end,3600000))-set(t for t in source if start<=t<end))
    recomputed=[];incomplete=[]
    expected=set(range(start,end,3600000));metrics={}
    for symbol in config['symbols']:
        pervenue={}
        for venue in ('bybit','bitget'):
            bars=raw[(symbol,venue)]
            if not expected<=set(bars):continue
            rv=[];turnover=Decimal(0)
            for day in range(start+3600000,end,86400000):
                returns=[]
                for t in range(day,day+86400000,3600000):
                    returns.append(math.log(float(bars[t][3]/bars[t-3600000][3])))
                    turnover+=bars[t][5]
                rv.append(Decimal(str(10000*math.sqrt(math.fsum(x*x for x in returns)))))
            assert len(rv)==20
            pervenue[venue]=(sum(rv,Decimal(0))/20,turnover/20)
        if len(pervenue)<2:incomplete.append(symbol);continue
        metrics[symbol]=pervenue
        recomputed.append((-min(x[0] for x in pervenue.values()),-min(x[1] for x in pervenue.values()),symbol))
    recomputed.sort();positions={s:i+1 for i,(_,_,s) in enumerate(recomputed)}
    cutoff=-recomputed[9][0]
    for row in ranking['rows']:
        symbol=row['symbol']
        assert row['history_rank']==positions.get(symbol)
        assert Decimal(row['history_cutoff_volatility_bps'])==cutoff
        if symbol in metrics:
            for venue,(rv,adv) in metrics[symbol].items():
                assert Decimal(row[venue+'_rv20_bps'])==rv
                assert Decimal(row[venue+'_adv20_usdt'])==adv
    assert positions['BTWUSDT']==3 and set(incomplete)=={'CTUSDT','STONKUSDT'}
    assert all(not row['btw']['history_filter_passed'] for row in ranking['checks'][:2])
    assert all(row['btw']['history_filter_passed'] for row in ranking['checks'][2:])
    assert all(row['btw']['history_window_end_ms']<=row['decision_time_ms']-60000 for row in ranking['checks'])
    old=set(json.loads((Path(config['source_directory'])/'provenance.json').read_text())['symbols'])
    old_ranked=[row for row in recomputed if row[2] in old]
    old_btw=next(i+1 for i,row in enumerate(old_ranked) if row[2]=='BTWUSDT')
    result={'status':'PASS','execution_enabled':False,'method':'Independent raw-to-RV20/ADV20/rank implementation, no HistoryGate calls.',
            'archives_sha_crc_verified':len(meta['source_paths_sha256']),'original_http_body_sha_verified':body_verified,
            'raw_attempt_archives_verified':len(attempts),'equal_duplicate_bars':dupes,
            'normalized_venue_series_verified':len(normalized['data']),'pool_count':420,'ranked_count':len(recomputed),
            'incomplete_symbols':incomplete,'btw_rank':positions['BTWUSDT'],'cutoff_bps':str(cutoff),
            'old_385_pool_sensitivity':{'btw_rank':old_btw,'cutoff_bps':str(-old_ranked[9][0]),'top10':[r[2] for r in old_ranked[:10]]},
            'new_420_pool_top10':[r[2] for r in recomputed[:10]],
            'boundary_checks':'First 60 seconds reject; from 00:01 prior 20 complete UTC days only.',
            'output_hashes':{n:sha(ROOT/n) for n in ('collect.py','build_gate.py','config.json','gate-input.json','ranking-20261001.json','normalized-hourly.json','acceptance.json','supplemental-crypto-review.json')},
            'not_verified':['Point-in-time instrument directory','Historical candle publication or revisions','Out-of-sample symbol selection']}
    (ROOT/'independent-recalculation.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(result,ensure_ascii=False))

if __name__=='__main__':main()

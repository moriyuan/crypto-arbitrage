"""Public hourly-history bundles; previous completed days only, no trading APIs."""
import argparse, asyncio, gzip, hashlib, importlib.util, json, math, sys, time
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path
import httpx

BASE=Path('/Users/mori/code/crypto/state/arb-runs')
SOURCE=BASE/'20261009T032518Z-20d-history-backfill'
sys.path.insert(0,'/Users/mori/.codex/worktrees/crypto-arbitrage-research/crypto/src')
from btc_cta.cross_arb.history_gate import HistoryGate, RollingHistoryConfig, HourlyBar
spec=importlib.util.spec_from_file_location('original_collector',BASE/'20261009-btw-oct1-history/collect.py')
original=importlib.util.module_from_spec(spec);spec.loader.exec_module(original)
HOUR=3600000;DAY=24*HOUR
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def utc():return datetime.now(timezone.utc).isoformat()
def write(p,v):
    p=Path(p);tmp=p.with_suffix(p.suffix+'.tmp');tmp.write_text(json.dumps(v,indent=2,allow_nan=False)+'\n');tmp.replace(p)
def start_ms(day):return int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp())*1000

def parse_rows(payload,venue,start,end):
    body=payload['response'];code=body.get('retCode') if venue=='bybit' else body.get('code')
    if payload['http_status']!=200 or code!=(0 if venue=='bybit' else '00000'):raise ValueError('unsuccessful raw response')
    rows=body['result']['list'] if venue=='bybit' else body['data'];out={}
    for row in rows:
        t=int(row[0]);vals=tuple(D(str(v)) for v in row[1:7])
        if len(vals)!=6 or any(not v.is_finite() for v in vals):raise ValueError('invalid OHLCV')
        if min(vals[:4])<=0 or min(vals[4:])<0 or t%HOUR:raise ValueError('invalid candle')
        if not start<=t<end:continue
        if t in out and out[t]!=vals:raise ValueError('conflicting duplicate candle')
        out[t]=vals
    return out

def independent(data,end):
    """Separate mathematical reduction, no HistoryGate internals."""
    output={};ranking=[]
    for symbol,venues in data.items():
        row={}
        for venue,bars in venues.items():
            bytime={b.start_ms:b for b in bars};days=[]
            for day in range(end-20*DAY,end,DAY):
                keys=list(range(day-HOUR,day+DAY,HOUR))
                if not all(k in bytime for k in keys):break
                returns=[math.log(float(bytime[b].close/bytime[a].close)) for a,b in zip(keys,keys[1:])]
                days.append((D(str(10000*math.sqrt(math.fsum(x*x for x in returns)))),sum((bytime[k].turnover_usdt for k in keys[1:]),D(0))))
            if len(days)==20:
                row[venue]=(sum((x[0] for x in days),D(0))/20,sum((x[1] for x in days),D(0))/20)
        output[symbol]=row
        if len(row)==2:ranking.append((-min(x[0] for x in row.values()),-min(x[1] for x in row.values()),symbol))
    ranking.sort();ranks={x[2]:i+1 for i,x in enumerate(ranking)}
    return output,ranks

def build(root):
    config=json.loads((root/'config.json').read_text());end=config['end_ms'];start=config['start_ms']
    manifest=json.loads((root/'raw-manifest.json').read_text());data={s:{v:[] for v in ('bybit','bitget')} for s in config['symbols']};merged={}
    for item in manifest:
        p=root/item['path']
        if sha(p)!=item['sha256']:raise ValueError('raw hash mismatch')
        payload=json.loads(gzip.decompress(p.read_bytes()))
        if hashlib.sha256(payload['response_text'].encode()).hexdigest()!=payload['response_body_sha256']:raise ValueError('response text mismatch')
        if json.loads(payload['response_text'])!=payload['response']:raise ValueError('response object mismatch')
        vals=parse_rows(payload,item['venue'],start,end);key=(item['symbol'],item['venue']);dest=merged.setdefault(key,{})
        for t,row in vals.items():
            if t in dest and dest[t]!=row:raise ValueError('cross-request duplicate mismatch')
            dest[t]=row
    normalized=[]
    for symbol,venues in data.items():
        for venue in venues:
            vals=merged.get((symbol,venue),{});venues[venue]=[HourlyBar(t,vals[t][3],vals[t][5]) for t in sorted(vals)]
            normalized.append({'symbol':symbol,'venue':venue,'bars':[{'start_ms':b.start_ms,'close':str(b.close),'turnover_usdt':str(b.turnover_usdt)} for b in venues[venue]],'missing_hours':(end-start)//HOUR-len(vals)})
    identity=json.loads((SOURCE/'selection-crypto-verified.json').read_text())
    if identity['source_universe_sha256']!=sha(SOURCE/'universe.json'):raise ValueError('identity hash mismatch')
    verified={r['symbol'] for r in identity['top10'] if r['asset_identity']['classification']=='crypto_project_token' and r['asset_identity']['perpetual_symbol']==r['symbol'] and r['asset_identity']['official_project_identity_source']}
    gate=HistoryGate(RollingHistoryConfig(),data,identity_verified_symbols=verified,audit_metadata={'caveats':config['caveats']})
    rows=[dict(symbol=s,**gate.evaluate(s,end+60000)) for s in data];rows.sort(key=lambda r:(r['history_rank'] or 99999,r['symbol']))
    checks=0
    for cut in (end-DAY,end):
        values,ranks=independent(data,cut)
        for s in data:
            actual=gate.evaluate(s,cut+60000)
            if actual['history_rank']!=ranks.get(s):raise ValueError('independent rank mismatch '+s)
            checks+=1
            for v in ('bybit','bitget'):
                for j,field in enumerate(('rv20_bps','adv20_usdt')):
                    expected=str(values[s][v][j]) if v in values[s] else None
                    if actual[v+'_'+field]!=expected:raise ValueError('independent metric mismatch '+s)
                    checks+=1
    rankname='ranking-'+config['date'].replace('-','')+'.json';btw=next(r for r in rows if r['symbol']=='BTWUSDT')
    write(root/rankname,{'date':config['date'],'rows':rows,'top10':[r for r in rows if r['history_rank'] and r['history_rank']<=10],'btw':btw,'pool_count':420,'ranked_count':sum(r['history_rank'] is not None for r in rows),'execution_enabled':False})
    write(root/'normalized-hourly.json',{'window_start_ms':start,'window_end_exclusive_ms':end,'data':normalized})
    write(root/'gate-input.json',{'schema_version':1,'execution_enabled':False,'config':gate.metadata()['config'],'available_from_ms':end+60000,'valid_until_exclusive_ms':end+DAY,'btw':btw,'first_60_seconds':gate.evaluate('BTWUSDT',end),'pool_count':420,'ranking_file':rankname,'caveats':config['caveats']})
    names=('config.json','raw-manifest.json','normalized-hourly.json','gate-input.json',rankname)
    hashes={n:sha(root/n) for n in names}
    write(root/'acceptance.json',{'status':'PASS_WITH_LIMITATIONS','date':config['date'],'btw':btw,'files_sha256':hashes,'caveats':config['caveats']})
    write(root/'independent-recalculation.json',{'status':'PASS','date':config['date'],'checks':checks,'output_hashes':hashes,'raw_response_count':len(manifest),'method':'Independent raw hash/CRC+full OHLCV overlap validation and completed-day RV/turnover reduction for both causal windows; all420 ranks compared.','execution_enabled':False})
    return btw

async def collect(client,locks,last,sem,day,out):
    root=out/'days'/day/'history';root.mkdir(parents=True,exist_ok=True)
    if (root/'status.json').exists():raise RuntimeError('refusing overwrite '+str(root))
    (root/'raw').mkdir();end=start_ms(day);start=end-21*DAY-HOUR
    rows=[r for r in json.loads((BASE/'20261009-full-crypto-universe-audit.json').read_text())['eligible'] if original.eligible(r)]
    assert len(rows)==420
    late=[r['symbol'] for r in rows if int(r['bybit']['launchTime'])>end-20*DAY]
    plan=[]
    for row in rows:
        if row['symbol'] in late:continue
        plan.append({'venue':'bybit','symbol':row['symbol'],'start':start,'end':end})
        for begin in range(start,end,200*HOUR):plan.append({'venue':'bitget','symbol':row['symbol'],'start':begin,'end':min(end,begin+200*HOUR)})
    config={'date':day,'start_ms':start,'end_ms':end,'symbols':[r['symbol'] for r in rows],'pool_count':420,'known_short_history_symbols':late,'request_count':len(plan),'collector_sha256':sha(__file__),'universe_sha256':sha(BASE/'20261009-full-crypto-universe-audit.json'),'request_rate_per_venue':4,'public_only':True,'execution_enabled':False,'caveats':['Oct9 directory is not a point-in-time universe; survivorship bias remains.','20 completed UTC days plus60s candle publication assumption; first60s uses previous window.','Known listings after required20day start excluded as incomplete, not removed from420pool.','BTW and dates selected retrospectively; no forward unseen-asset claim.']}
    write(root/'config.json',config);manifest=[];status={'status':'RUNNING','date':day,'total':len(plan),'completed':0,'errors':[],'heartbeat_utc':utc()};write(root/'status.json',status)
    async def one(item):
        async with sem:
            v=item['venue'];s=item['symbol'];a=item['start'];b=item['end']
            url='https://api.bybit.com/v5/market/kline' if v=='bybit' else 'https://api.bitget.com/api/v2/mix/market/history-candles'
            params={'category':'linear','symbol':s,'interval':'60','limit':1000,'start':a,'end':b-1} if v=='bybit' else {'productType':'USDT-FUTURES','symbol':s,'granularity':'1H','limit':200,'startTime':a-1,'endTime':b,'kLineType':'market'}
            error=None
            for attempt in range(1,4):
                async with locks[v]:
                    await asyncio.sleep(max(0,last[v]+.25-time.monotonic()));last[v]=time.monotonic()
                try:
                    response=await client.get(url,params=params);body=response.json();name=f'raw/{v}_{s}_{a}_attempt{attempt}.json.gz'
                    payload={'venue':v,'url':url,'params':params,'http_status':response.status_code,'response_text':response.text,'response_body_sha256':hashlib.sha256(response.content).hexdigest(),'response':body,'fetched_utc':utc()}
                    (root/name).write_bytes(gzip.compress(json.dumps(payload,allow_nan=False).encode()))
                    response.raise_for_status();parse_rows(payload,v,a,b)
                    manifest.append({**item,'path':name,'sha256':sha(root/name)});error=None;break
                except Exception as exc:
                    error=type(exc).__name__+': '+str(exc)[:200]
                    if isinstance(exc,httpx.HTTPStatusError) and exc.response.status_code in (401,403):break
                    if attempt<3:await asyncio.sleep(attempt*2)
            if error:status['errors'].append({**item,'error':error})
            status.update(completed=status['completed']+1,heartbeat_utc=utc());write(root/'status.json',status)
            if status['completed']%100==0:
                write(root/'raw-manifest.json',manifest);print(json.dumps({'date':day,'completed':status['completed'],'total':len(plan),'errors':len(status['errors'])}),flush=True)
    await asyncio.gather(*(one(x) for x in plan));write(root/'raw-manifest.json',manifest)
    if status['errors']:status['status']='FAILED'
    else:
        try:status.update(status='COMPLETED',btw=build(root))
        except Exception as exc:status.update(status='AUDIT_FAILED',audit_error=repr(exc))
    status['heartbeat_utc']=utc();write(root/'status.json',status);print(json.dumps(status),flush=True)

async def main():
    ap=argparse.ArgumentParser();ap.add_argument('--output',type=Path,required=True);ap.add_argument('--dates',nargs='+',required=True);args=ap.parse_args()
    locks={v:asyncio.Lock() for v in ('bybit','bitget')};last={v:0. for v in locks};sem=asyncio.Semaphore(12)
    async with httpx.AsyncClient(proxy='http://127.0.0.1:7897',trust_env=False,timeout=30,limits=httpx.Limits(max_connections=12)) as client:
        for day in args.dates:await collect(client,locks,last,sem,day,args.output)
if __name__=='__main__':asyncio.run(main())

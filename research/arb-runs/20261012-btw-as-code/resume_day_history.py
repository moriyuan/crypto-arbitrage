"""Recover missing public history responses only; preserve prior attempts and fail closed."""
import argparse,asyncio,gzip,hashlib,json,time
from pathlib import Path
import httpx
from day_history import HOUR,write,sha,utc,parse_rows,build

def recover_cached(root,plan):
    expected={(r['venue'],r['symbol'],r['start'],r['end']) for r in plan};cached={}
    for path in sorted((root/'raw').glob('*.json.gz')):
        payload=json.loads(gzip.decompress(path.read_bytes()));v=payload['venue'];p=payload['params'];s=p['symbol']
        start=int(p['start']) if v=='bybit' else int(p['startTime'])+1
        end=int(p['end'])+1 if v=='bybit' else int(p['endTime']);key=(v,s,start,end)
        if key not in expected:raise ValueError('cached request outside frozen plan')
        if hashlib.sha256(payload['response_text'].encode()).hexdigest()!=payload['response_body_sha256']:raise ValueError('cached body hash')
        if json.loads(payload['response_text'])!=payload['response']:raise ValueError('cached JSON changed')
        try:parse_rows(payload,v,start,end)
        except ValueError:continue
        cached[key]={'venue':v,'symbol':s,'start':start,'end':end,'path':str(path.relative_to(root)),'sha256':sha(path)}
    return cached

async def main():
    ap=argparse.ArgumentParser();ap.add_argument('--directory',type=Path,required=True);ap.add_argument('--route',choices=('proxy','direct'),required=True);a=ap.parse_args();root=a.directory
    config=json.loads((root/'config.json').read_text());plan=[]
    for s in config['symbols']:
        if s in config['known_short_history_symbols']:continue
        plan.append({'venue':'bybit','symbol':s,'start':config['start_ms'],'end':config['end_ms']})
        for start in range(config['start_ms'],config['end_ms'],200*HOUR):plan.append({'venue':'bitget','symbol':s,'start':start,'end':min(config['end_ms'],start+200*HOUR)})
    cached=recover_cached(root,plan);missing=[r for r in plan if (r['venue'],r['symbol'],r['start'],r['end']) not in cached]
    status={'status':'RECOVERING','route':a.route,'cached_requests':len(cached),'missing_requests':len(missing),'completed_recovery_requests':0,'errors':[],'heartbeat_utc':utc(),'execution_enabled':False,'recovery_code_sha256':sha(__file__)}
    write(root/'recovery-status.json',status);print(json.dumps(status),flush=True)
    sem=asyncio.Semaphore(4);locks={v:asyncio.Lock() for v in ('bybit','bitget')};last={v:0. for v in locks};stop=asyncio.Event()
    async with httpx.AsyncClient(proxy='http://127.0.0.1:7897' if a.route=='proxy' else None,trust_env=False,timeout=15,limits=httpx.Limits(max_connections=4,max_keepalive_connections=0)) as client:
        async def one(r):
            async with sem:
                if stop.is_set():return
                v=r['venue'];s=r['symbol'];start=r['start'];end=r['end']
                url='https://api.bybit.com/v5/market/kline' if v=='bybit' else 'https://api.bitget.com/api/v2/mix/market/history-candles'
                params={'category':'linear','symbol':s,'interval':'60','limit':1000,'start':start,'end':end-1} if v=='bybit' else {'productType':'USDT-FUTURES','symbol':s,'granularity':'1H','limit':200,'startTime':start-1,'endTime':end,'kLineType':'market'}
                error=None
                for attempt in range(1,3):
                    async with locks[v]:
                        await asyncio.sleep(max(0,last[v]+.25-time.monotonic()));last[v]=time.monotonic()
                    try:
                        response=await client.get(url,params=params);body=response.json();name=f'raw/{v}_{s}_{start}_recovery{attempt}.json.gz'
                        if (root/name).exists():raise FileExistsError('refuse overwriting recovery response')
                        payload={'venue':v,'url':url,'params':params,'http_status':response.status_code,'response_text':response.text,'response_body_sha256':hashlib.sha256(response.content).hexdigest(),'response':body,'fetched_utc':utc(),'route':a.route}
                        (root/name).write_bytes(gzip.compress(json.dumps(payload,allow_nan=False).encode()));response.raise_for_status();parse_rows(payload,v,start,end)
                        cached[(v,s,start,end)]={**r,'path':name,'sha256':sha(root/name)};error=None;break
                    except Exception as exc:
                        error=type(exc).__name__+': '+str(exc)[:160]
                        if isinstance(exc,httpx.HTTPStatusError) and exc.response.status_code in (401,403):stop.set();break
                        if attempt<2:await asyncio.sleep(2)
                if error:
                    status['errors'].append({**r,'error':error})
                    if len(status['errors'])>=3:stop.set()
                status.update(completed_recovery_requests=status['completed_recovery_requests']+1,heartbeat_utc=utc());write(root/'recovery-status.json',status)
                if status['completed_recovery_requests']%100==0:print(json.dumps(status),flush=True)
        await asyncio.gather(*(one(r) for r in missing))
    write(root/'raw-manifest.json',list(cached.values()))
    if len(cached)!=len(plan):status.update(status='RECOVERY_INCOMPLETE',remaining_requests=len(plan)-len(cached))
    else:
        try:status.update(status='COMPLETED',btw=build(root))
        except Exception as exc:status.update(status='AUDIT_FAILED',audit_error=repr(exc))
    status['heartbeat_utc']=utc();write(root/'recovery-status.json',status);write(root/'status.json',status);print(json.dumps(status),flush=True)
if __name__=='__main__':asyncio.run(main())

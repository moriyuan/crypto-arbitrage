"""Bounded public funding history + causal archived ticker schedule, no credentials."""
import argparse, asyncio, calendar, gzip, hashlib, json, shutil
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import httpx
from day_history import write, sha, utc, start_ms, DAY

def ns(ts):
    whole,_,fraction=ts.rstrip('Z').partition('.')
    dt=datetime.strptime(whole,'%Y-%m-%dT%H:%M:%S').replace(tzinfo=timezone.utc)
    if len(fraction)>9:raise ValueError('timestamp too precise')
    return calendar.timegm(dt.timetuple())*10**9+int(fraction.ljust(9,'0'))

def parse_events(body,venue):
    if body.get('retCode' if venue=='bybit' else 'code')!=(0 if venue=='bybit' else '00000'):raise ValueError('API error')
    rows=body['result']['list'] if venue=='bybit' else body['data'];events=[]
    for row in rows:
        if row['symbol']!='BTWUSDT':raise ValueError('funding symbol')
        ts=int(row['fundingRateTimestamp' if venue=='bybit' else 'fundingTime']);rate=Decimal(row['fundingRate'])
        if not rate.is_finite() or abs(rate)>1:raise ValueError('funding rate')
        events.append({'venue':venue,'symbol':'BTWUSDT','timestamp_ms':ts,'utc':datetime.fromtimestamp(ts/1000,timezone.utc).isoformat(),'funding_rate':str(rate),'funding_rate_bps':str(rate*10000),'settlement_mark_price':None,'mark_price_status':'NOT_PROVIDED_BY_FUNDING_HISTORY_ENDPOINT'})
    return events

def bracket(events,start,end):
    unique={}
    for e in events:
        t=e['timestamp_ms']
        if t in unique and unique[t]!=e:raise ValueError('conflicting funding event')
        unique[t]=e
    ordered=[unique[t] for t in sorted(unique)];before=[e for e in ordered if e['timestamp_ms']<start];inside=[e for e in ordered if start<=e['timestamp_ms']<end];after=[e for e in ordered if e['timestamp_ms']>=end]
    if not before or not inside or not after:raise ValueError('missing funding bracket')
    selected=before[-1:]+inside+after[:1]
    return {'status':'BRACKETED_HISTORY','raw_event_count':len(ordered),'previous_event':before[-1],'target_day_event_count':len(inside),'target_day_events':inside,'following_event':after[0],'selected_events':selected,'exact_funding_amount_status':'UNAVAILABLE_SETTLEMENT_MARK_PRICE_MISSING','intervals_ms_between_selected_events':[b['timestamp_ms']-a['timestamp_ms'] for a,b in zip(selected,selected[1:])]}

def schedule_rows(raw,venue,filename,day):
    records=[];counts={};start=start_ms(day)*10**6
    for line in gzip.decompress(raw).decode().splitlines():
        if not line.strip() or ' ' not in line:continue
        local,encoded=line.split(' ',1)
        if not encoded.strip():continue
        msg=json.loads(encoded);topic=msg.get('topic') or msg.get('arg',{}).get('topic')
        if topic!=('tickers.BTWUSDT' if venue=='bybit' else 'ticker'):continue
        if venue=='bitget' and msg.get('arg',{}).get('symbol')!='BTWUSDT':raise ValueError('ticker symbol')
        rows=msg.get('data',[]);rows=[rows] if isinstance(rows,dict) else rows
        for row in rows:
            if 'nextFundingTime' not in row:continue
            nxt=int(row['nextFundingTime']);known=ns(local)
            if not start<=known<start+DAY*10**6:raise ValueError('ticker date')
            counts[str(nxt)]=counts.get(str(nxt),0)+1
            if known>=nxt*10**6:continue
            records.append({'venue':venue,'known_at_local_timestamp':local,'known_at_ns':known,'known_at_us_ceil':(known+999)//1000,'next_funding_timestamp_ms':nxt,'next_funding_utc':datetime.fromtimestamp(nxt/1000,timezone.utc).isoformat(),'source_file':filename,'source_channel':'tickers' if venue=='bybit' else 'ticker','source_message_timestamp_ms':msg.get('ts'),'rate_ignored_for_entry':True})
    return records,counts

async def fetch_json(client,url,params,root,name,venue):
    if (root/name).exists():
        body=json.loads((root/name).read_text());parse_events(body,venue)
        return body,{'venue':venue,'url':url,'params':params,'file':name,'status_code':200,'sha256':sha(root/name),'bytes':(root/name).stat().st_size,'reused_frozen_response':True}
    r=await client.get(url,params=params);(root/name).write_bytes(r.content);r.raise_for_status();body=r.json();parse_events(body,venue)
    return body,{'venue':venue,'url':url,'params':params,'file':name,'status_code':r.status_code,'sha256':sha(root/name),'bytes':len(r.content),'received_utc':utc()}

async def main():
    ap=argparse.ArgumentParser();ap.add_argument('--output',type=Path,required=True);ap.add_argument('--dates',nargs='+',required=True);a=ap.parse_args()
    shared=a.output/'funding-shared';shared.mkdir(exist_ok=True)
    async with httpx.AsyncClient(proxy='http://127.0.0.1:7897',trust_env=False,timeout=50,follow_redirects=True,limits=httpx.Limits(max_connections=4)) as client:
        bg_events=[];bg_requests=[];fingerprints=set();oldest=min(start_ms(d) for d in a.dates)
        for page in range(1,31):
            params={'symbol':'BTWUSDT','productType':'USDT-FUTURES','pageSize':100,'pageNo':page};name=f'bitget-history-page{page}.json'
            body,record=await fetch_json(client,'https://api.bitget.com/api/v2/mix/market/history-fund-rate',params,shared,name,'bitget');events=parse_events(body,'bitget')
            fingerprint=tuple(e['timestamp_ms'] for e in events)
            if not events:
                bg_requests.append(record);break
            if fingerprint in fingerprints:raise ValueError('funding pagination repeated before required bracket')
            fingerprints.add(fingerprint);bg_events.extend(events);bg_requests.append(record)
            if min(fingerprint)<oldest:break
            await asyncio.sleep(.3)
        else:raise ValueError('funding page bound exhausted')
        write(shared/'manifest.json',{'requests':bg_requests,'execution_enabled':False})
        for day in a.dates:
            root=a.output/'days'/day/'funding';root.mkdir(parents=True,exist_ok=False);start=start_ms(day);end=start+DAY
            status={'status':'RUNNING','date':day,'heartbeat_utc':utc(),'execution_enabled':False};write(root/'status.json',status)
            try:
                for rec in bg_requests:shutil.copyfile(shared/rec['file'],root/rec['file'])
                params={'category':'linear','symbol':'BTWUSDT','startTime':start-DAY,'endTime':end+DAY-1,'limit':200}
                body,rec=await fetch_json(client,'https://api.bybit.com/v5/market/funding/history',params,root,'bybit-history.json','bybit')
                manifest={'symbol':'BTWUSDT','window_start_ms':start,'window_end_ms_exclusive':end,'requests':bg_requests+[rec],'public_only':True,'private_credentials_used':False,'collector_sha256':sha(__file__)}
                write(root/'manifest.json',manifest)
                parsed={'bybit':bracket(parse_events(body,'bybit'),start,end),'bitget':bracket(bg_events,start,end)}
                write(root/'events.json',{'symbol':'BTWUSDT','target_day_utc':day,'venues':parsed})
                requests=[];observations=[];sem=asyncio.Semaphore(4)
                async def ticker(ex,offset):
                    venue='bitget' if ex=='bitget-futures' else 'bybit';channel='ticker' if venue=='bitget' else 'tickers'
                    params={'from':day,'offset':offset,'sliceSize':1,'filters':json.dumps([{'channel':channel,'symbols':['BTWUSDT']}],separators=(',',':')),'compression':'gzip'}
                    name=f'{ex}-ticker-offset{offset:04d}.jsonl.gz'
                    async with sem:
                        async with client.stream('GET','https://api.tardis.dev/v1/data-feeds/'+ex,params=params) as r:
                            raw=bytearray()
                            async for chunk in r.aiter_raw():
                                raw.extend(chunk)
                                if len(raw)>8000000:raise ValueError('ticker size bound')
                            (root/name).write_bytes(raw);r.raise_for_status()
                            records,counts=schedule_rows(bytes(raw),venue,name,day);observations.extend(records)
                            requests.append({'venue':venue,'exchange':ex,'params':params,'status_code':r.status_code,'url':str(r.url),'file':name,'sha256':sha(root/name),'bytes':len(raw),'gzip_crc':'PASS','body_sha256':hashlib.sha256(gzip.decompress(raw)).hexdigest(),'announced_next_times':counts})
                            status.update(heartbeat_utc=utc(),schedule_requests=len(requests));write(root/'status.json',status)
                await asyncio.gather(*(ticker(ex,offset) for ex in ('bybit','bitget-futures') for offset in range(0,1440,60)))
                observations.sort(key=lambda r:(r['known_at_ns'],r['venue']));seen=set();filtered=[]
                for row in observations:
                    key=row['venue'],row['next_funding_timestamp_ms']
                    if key not in seen:seen.add(key);filtered.append(row)
                venues={v:{'observed_schedule_count':sum(r['venue']==v for r in filtered),'observations':[r for r in filtered if r['venue']==v],'first_known_at_ns':min((r['known_at_ns'] for r in filtered if r['venue']==v),default=None)} for v in ('bybit','bitget')}
                write(root/'schedule-manifest.json',{'date':day,'requests':sorted(requests,key=lambda r:r['file']),'offsets_minutes':list(range(0,1440,60)),'collector_sha256':sha(__file__),'public_only':True})
                write(root/'causal-schedule.json',{'date':day,'symbol':'BTWUSDT','mode':'CAUSAL_HISTORICAL_NEXT_FUNDING_TIME_OBSERVATIONS','observations':filtered,'venues':venues,'unknown_until_first_observation':True,'execution_enabled':False,'use_rule':'Causal next time only after known_at_ns. Expiration blocks entry. Historic realized rates never used to enter. Hourly first-minute sampling assumes unchanged next time until expiry; actual event history independently checked.'})
                # Re-read all raw bodies and reconstruct schedule for acceptance.
                for r in manifest['requests']:
                    if sha(root/r['file'])!=r['sha256']:raise ValueError('raw funding hash')
                    parse_events(json.loads((root/r['file']).read_text()),r['venue'])
                rebuilt=[]
                for r in requests:
                    raw=(root/r['file']).read_bytes()
                    if hashlib.sha256(raw).hexdigest()!=r['sha256']:raise ValueError('raw ticker hash')
                    rebuilt.extend(schedule_rows(raw,r['venue'],r['file'],day)[0])
                for v in ('bybit','bitget'):
                    actual={e['timestamp_ms'] for e in parsed[v]['selected_events']}
                    announced={r['next_funding_timestamp_ms'] for r in rebuilt if r['venue']==v}
                    if not announced or not announced<=actual:raise ValueError('announced/realized schedule mismatch '+v)
                write(root/'acceptance.json',{'status':'PUBLIC_FUNDING_HISTORY_AND_CAUSAL_SCHEDULE_VERIFIED','date':day,'symbol':'BTWUSDT','raw_sha':'PASS','ticker_gzip_crc':'PASS','announced_next_times_match_realized_history':True,'funding_events':{v:parsed[v]['target_day_event_count'] for v in parsed},'observed_next_times':{v:venues[v]['observed_schedule_count'] for v in venues},'settlement_mark_price':'UNKNOWN_NOT_REPLACED_WITH_ZERO','limitation':'Hourly first minute schedule observations; block while unknown or expired. No actual funding PnL unless absence of settlement crossing is proved.','execution_enabled':False})
                write(root/'contract-rules-evidence.json',{'status':'HISTORICAL_SCENARIO_ASSUMPTION','source':str(Path('/Users/mori/code/crypto/state/arb-runs/20261009-btw-oct1-funding/contract-rules-evidence.json')),'source_sha256':sha('/Users/mori/code/crypto/state/arb-runs/20261009-btw-oct1-funding/contract-rules-evidence.json'),'quantity_steps':{'bybit':10,'bitget':1},'price_ticks':{'bybit':'0.00001','bitget':'0.000001'},'note':'Quantity/min-notional rules are Oct9 assumptions; price grids preserve observed historical native precision and are not proven official historical rules.'})
                status.update(status='COMPLETED',heartbeat_utc=utc())
            except Exception as exc:status.update(status='FAILED',error=repr(exc),heartbeat_utc=utc())
            write(root/'status.json',status);print(json.dumps(status),flush=True)
if __name__=='__main__':asyncio.run(main())

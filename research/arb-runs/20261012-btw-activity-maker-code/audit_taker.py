"""Independent integer/Decimal audit of activity-gated offline BTW replay.

Does not import either replay engine, create orders, or access network/credentials.
Outputs only audit evidence. Reads the immutable decision tape once for all three
scenario-specific depth ledgers and checks every quote/order/risk trigger.
"""
from __future__ import annotations
import argparse
from collections import Counter,defaultdict
import csv
from datetime import datetime,timezone
from decimal import Decimal as D
import gzip
import hashlib
import json
import mmap
from pathlib import Path
import struct
import time

BASE=Path('/Users/mori/code/crypto/state/arb-runs')
EXP=BASE/'20261012-btw-activity-maker'
OLD_RUN=BASE/'20261012-btw-exit-relative-tp5-sl20-h100-20261001'
OLD_SCREEN=BASE/'20261012-btw-exit-optimization/screen/entry_deterioration_tp5_sl20'
VENUES=('bybit','bitget');HEADER=struct.Struct('<qBBBBI8q');UPDATE=struct.Struct('<B7xqq');FEATURE=struct.Struct('<qIIqqqqQ')
CHECKS=Counter()


def check(ok,kind,detail=''):
    CHECKS[kind]+=1
    if not ok: raise ValueError(kind+': '+str(detail))


def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        while x:=f.read(1048576):h.update(x)
    return h.hexdigest()


def read_json(path):return json.loads(Path(path).read_text())
def read_csv(path):
    with Path(path).open(newline='') as f:return list(csv.DictReader(f))
def norm_reason(x):
    if x.startswith('funding_guard'):return 'funding_guard'
    return 'replay_day_end' if x=='scheduled_day_end' else x

def timestamp_ns(stamp):
    base=datetime.strptime(stamp[:19],'%Y-%m-%dT%H:%M:%S').replace(tzinfo=timezone.utc)
    return int(base.timestamp())*10**9+int(stamp[20:-1].ljust(9,'0'))

def budget_after_display_change(debt,new_display):
    # Independent consumed-quantity representation: available quarters =
    # displayed coins - cumulative consumed quarters. A downward display change
    # clips consumed debt to remaining display; deleting a level forgets it.
    return min(debt,new_display)


def audit_funding(tape_meta):
    root=BASE/'20261009-btw-oct1-funding'
    for name,sha in tape_meta['funding_evidence'].items():check(digest(root/name)==sha,'funding metadata hash',name)
    acceptance=read_json(root/'acceptance.json')
    check(acceptance['status']=='PUBLIC_FUNDING_HISTORY_AND_CAUSAL_SCHEDULE_VERIFIED','funding evidence accepted')
    raw_count=0
    for name in ('manifest.json','schedule-manifest.json'):
        for row in read_json(root/name)['requests']:
            path=root/row['file'];check(row['status_code']==200 and digest(path)==row['sha256'],'funding raw SHA',str(path));raw_count+=1
            if path.suffix=='.gz':
                body=gzip.decompress(path.read_bytes())
                check(hashlib.sha256(body).hexdigest()==row['body_sha256'],'funding gzip CRC/body hash',path.name)
    events=read_json(root/'events.json')['venues'];observations=read_json(root/'causal-schedule.json')['observations']
    selected={}
    for venue in VENUES:
        native=read_json(root/(venue+'-history-response-1.json'))
        rows=native['result']['list'] if venue=='bybit' else native['data']
        known={int(r['fundingRateTimestamp'] if venue=='bybit' else r['fundingTime']):D(r['fundingRate']) for r in rows if r['symbol']=='BTWUSDT'}
        selected[venue]=[r['timestamp_ms']*1000000 for r in events[venue]['selected_events']]
        check(len(selected[venue])==8,'funding bracket count')
        for row in events[venue]['selected_events']:
            check(row['symbol']=='BTWUSDT' and known[row['timestamp_ms']]==D(row['funding_rate']),'funding raw realized event',row)
    for obs in observations:
        check(timestamp_ns(obs['known_at_local_timestamp'])==obs['known_at_ns'],'funding exact receive timestamp')
        matches=[]
        with gzip.open(root/obs['source_file'],'rt') as f:
            for line in f:
                stamp,sep,msg=line.strip().partition(' ')
                if not sep or stamp!=obs['known_at_local_timestamp']:continue
                native=json.loads(msg)
                if obs['venue']=='bybit':
                    check(native['topic']=='tickers.BTWUSDT','funding source symbol');data=native['data']
                else:
                    check(native['arg']['symbol']=='BTWUSDT' and native['arg']['instType']=='usdt-futures','funding source symbol');data=native['data'][0]
                if 'nextFundingTime' in data:matches.append(int(data['nextFundingTime']))
        check(obs['next_funding_timestamp_ms'] in matches,'funding announcement present in raw')
        check(obs['next_funding_timestamp_ms']*1000000 in selected[obs['venue']],'announced settlement realized')
    return observations,selected,{'status':'VERIFIED_PUBLIC_HISTORY_AND_CAUSAL_ANNOUNCEMENTS','raw_files_revalidated':raw_count,'settlement_mark':'unknown; unnecessary only because no position crosses any settlement','limitation':acceptance['limitation']}


def gateoff_parity(run,aggregate):
    for name in ('orders.csv','cycles.csv'):
        check((run/'gateoff'/name).read_bytes()==(OLD_SCREEN/name).read_bytes(),'old screen byte equality',name)
    cpp_orders=read_csv(run/'gateoff/orders.csv');py_orders=read_csv(OLD_RUN/'orders.csv')
    cpp_cycles=read_csv(run/'gateoff/cycles.csv');py_cycles=read_csv(OLD_RUN/'cycles.csv')
    check(len(cpp_orders)==len(py_orders)==716,'official order count')
    check(len(cpp_cycles)==len(py_cycles)==160,'official cycle count')
    mapping={}
    for index,(a,b) in enumerate(zip(cpp_orders,py_orders)):
        mapping.setdefault(a['cycle'],b['cycle_id']);check(mapping[a['cycle']]==b['cycle_id'],'official cycle id mapping')
        check(int(a['ns'])==int(b['decision_cursor_ns']),'official exact order ns',index)
        for k in ('timestamp_ms','venue','side','intent'):check(a[k]==b[k],'official order '+k,index)
        for k in ('qty','price','fee'):check(D(a[k])==D(b[k]),'official order '+k,index)
        check(b['symbol']=='BTWUSDT' and b['pair']=='BTW/USDT:USDT' and D(b['fee_rate'])==D('.0003'),'official order scope')
    for index,(a,b) in enumerate(zip(cpp_cycles,py_cycles)):
        check(mapping[a['cycle']]==b['cycle_id'],'official cycle mapping')
        for k in ('long_venue','short_venue'):check(a[k]==b[k],'official cycle '+k,index)
        for k in ('qty','entry_timestamp_ms','exit_timestamp_ms','holding_ms','long_entry_price','short_entry_price','gross_pnl','total_fees','net_before_funding','bybit_net_before_funding','bitget_net_before_funding','exit_parts'):
            check(D(a[k])==D(b[k]),'official cycle '+k,index)
        check(int(a['exit_ns'])==int(b['decision_cursor_ns']),'official final exit ns')
        check(norm_reason(a['exit_reason'])==norm_reason(b['exit_reason']),'official exit reason family')
    for name in ('depth-fill-audit.json','risk-audit.json','selected-equivalence.json'):
        check(read_json(OLD_RUN/name)['status']=='PASS','official prior audit',name)
    check(read_json(OLD_RUN/'funding-audit.json')['status']=='VERIFIED_NO_SETTLEMENT','official prior funding audit')
    return {'status':'PASS','official_decimal_orders':len(py_orders),'official_decimal_cycles':len(py_cycles),'old_screen_orders_and_cycles':'BYTE_IDENTICAL','id_mapping':'bijective by chronological economic events; no random-ID comparison','normalized_reason_families':['funding_guard','replay_day_end']}


def audit_scenario(run,config,observations,settlements):
    name=config['name'];path=run/name;orders=read_csv(path/'orders.csv');cycles=read_csv(path/'cycles.csv')
    snapshots=[json.loads(line) for line in (path/'entry_budgets.jsonl').read_text().splitlines()]
    check(len(orders)==config['orders'] and len(cycles)==len(snapshots)==config['cycles'],'scenario file counts',name)
    check(config['unresolved_qty']==0,'no unresolved position',name)
    cash=dict.fromkeys(VENUES,D(1000));positions={};bycycle=defaultdict(list);byns=defaultdict(list);previous=-1;last_final_ms=None
    for index in range(0,len(orders),2):
        pair=orders[index:index+2]
        check(len(pair)==2 and [r['venue'] for r in pair]==list(VENUES),'simultaneous paired venues',name)
        for k in ('ns','timestamp_ms','intent','qty','cycle','exit_reason'):check(pair[0][k]==pair[1][k],'paired '+k,name)
        check({r['side'] for r in pair}=={'buy','sell'},'opposite leg sides',name)
        ns=int(pair[0]['ns']);check(ns>=previous,'monotonic fills',name);previous=ns
        intent=pair[0]['intent'];cid=pair[0]['cycle'];q=D(pair[0]['qty']);check(q>0 and q%10==0,'quantity lot',name)
        check(ns//1000000==int(pair[0]['timestamp_ms']),'order millisecond floor',name)
        if intent=='entry':
            check(all(p['remaining']==0 for p in positions.values()),'one open pair',name)
            check(last_final_ms is None or ns//1000000>=last_final_ms+30000,'entry cooldown',name)
            check(sum(cash.values())-2000>D(-10),'closed loss cap before entry',name)
        for row in pair:
            venue=row['venue'];p=D(row['price']);f=D(row['fee']);check(p>0 and p%D('.00001')==0,'price tick',name)
            check(q*p>=5,'minimum order notional',name);check(f==q*p*D('.0003'),'exact fee',name)
            key=(cid,venue)
            if intent=='entry':
                check(key not in positions,'unique venue entry',name)
                check(q*p<=250 and q*p+f<=cash[venue],'position capital cap',name)
                sign=1 if row['side']=='buy' else -1
                positions[key]={'entry':p,'remaining':q,'sign':sign,'ns':ns}
                cash[venue]-=f
                obs=[x for x in observations if x['venue']==venue and x['known_at_ns']<=ns]
                check(bool(obs),'causal funding schedule known',name)
                check(obs[-1]['next_funding_timestamp_ms']*1000000-ns>120*10**9,'funding safety horizon at entry',name)
            elif intent=='exit':
                pos=positions[key];check(row['side']==('sell' if pos['sign']==1 else 'buy'),'exit side closes leg',name)
                check(q<=pos['remaining'],'no overclose',name)
                pos['remaining']-=q;cash[venue]+=q*(p-pos['entry'])*pos['sign']-f
                check(not any(pos['ns']<=stamp<=ns for stamp in settlements[venue]),'no funding settlement crossing',name)
            else:check(False,'known order intent',intent)
            check(cash[venue]==D(row['cash']),'cash after every order',name)
            bycycle[cid].append(row);byns[ns].append(row)
        if intent=='exit' and all(positions[(cid,v)]['remaining']==0 for v in VENUES):last_final_ms=ns//1000000
    check(all(p['remaining']==0 for p in positions.values()),'all reconstructed legs flat',name)
    gross=fees=net=D(0);wins=losses=0;reasons=Counter();triggers={};snapmap={}
    for c,snap in zip(cycles,snapshots):
        cid=c['cycle'];rows=bycycle[cid];entry=rows[:2];outs=rows[2:];q=D(c['qty']);start=int(c['entry_ns']);end=int(c['exit_ns'])
        check(start==int(entry[0]['ns']) and end==int(outs[-1]['ns']) and start<end,'cycle chronology',name)
        check(int(c['holding_ms'])==end//1000000-start//1000000,'cycle holding',name)
        check(len(rows)==2+2*int(c['exit_parts']),'partial order count',name)
        check({c['long_venue'],c['short_venue']}==set(VENUES),'cycle venues',name)
        c_gross=c_fees=D(0)
        for v in VENUES:
            er=next(r for r in entry if r['venue']==v);xr=[r for r in outs if r['venue']==v];sign=1 if v==c['long_venue'] else -1
            check(er['side']==('buy' if sign==1 else 'sell'),'cycle direction',name)
            check(D(er['qty'])==q==sum(D(r['qty']) for r in xr),'complete leg quantity',name)
            g=sum(D(r['qty'])*(D(r['price'])-D(er['price']))*sign for r in xr);f=sum(D(r['fee']) for r in [er]+xr)
            check(g-f==D(c[v+'_net_before_funding']),'cycle venue net',name);c_gross+=g;c_fees+=f
        check(c_gross==D(c['gross_pnl']) and c_fees==D(c['total_fees']) and c_gross-c_fees==D(c['net_before_funding']),'cycle economics',name)
        gross+=c_gross;fees+=c_fees;net+=c_gross-c_fees;wins+=c_gross>c_fees;losses+=c_gross<c_fees;reasons[c['exit_reason']]+=1
        check(int(snap['cycle_id'])==int(cid) and snap['entry_ns']==start and snap['ns']==start and snap['qty']==int(q)==snap['newqty'],'snapshot entry identity',name)
        check(snap['pre_entry_consumption'] is True and snap['after_entry_consumption'] is False,'snapshot pre-entry semantics',name)
        check(snap['long']==c['long_venue'] and snap['short']==c['short_venue'],'snapshot directions',name)
        check(start not in snapmap,'unique snapshot ns',name);snapmap[start]=snap
        trigger=int(c['first_trigger_ns']);check(start<trigger<=end,'risk trigger chronology',name)
        before=[r for r in rows if int(r['ns'])<trigger]
        remaining=q-sum(D(r['qty']) for r in before if r['venue']=='bybit' and r['intent']=='exit')
        realized_gross=sum(D(r['qty'])*(D(r['price'])-positions[(cid,r['venue'])]['entry'])*positions[(cid,r['venue'])]['sign'] for r in before if r['intent']=='exit')
        incurred_fees=sum(D(r['fee']) for r in before)
        remaining_entry_fee=remaining*sum(positions[(cid,v)]['entry'] for v in VENUES)*D('.0003')
        triggers[trigger]=(c,remaining,realized_gross-incurred_fees+remaining_entry_fee)
    check(gross==D(config['gross_pnl']) and fees==D(config['fees']) and net==D(config['net_before_funding']),'aggregate economics',name)
    check(wins==config['wins'] and losses==config['losses'] and dict(reasons)==config['exit_reasons'],'aggregate outcomes',name)
    for v in VENUES:check(cash[v]==D(config['cash'][v])==1000+sum(D(c[v+'_net_before_funding']) for c in cycles),'aggregate cash',name)
    check(sum(cash.values())==2000+net,'total cash PnL',name)
    return {'config':config,'orders_by_ns':byns,'snapshots':snapmap,'triggers':triggers,'cycles':cycles,'positions':positions,
            'result':{'status':'PASS','orders':len(orders),'paired_fills':len(orders)//2,'cycles':len(cycles),'partial_exit_cycles':sum(int(c['exit_parts'])>1 for c in cycles),'gross_usdt':str(gross),'fees_usdt':str(fees),'net_usdt':str(net),'cash':{v:str(cash[v]) for v in VENUES},'funding_status':'VERIFIED_NO_SETTLEMENT','funding_usdt':'0','open_positions':0}}


def audit_depth_and_gates(tape,activity,states,observations):
    displayed=[{} for _ in range(4)];debt={name:[{} for _ in range(4)] for name in states};last=None;rows=0
    pass_counts=dict.fromkeys(states,0);orders_checked=dict.fromkeys(states,0);entries_checked=dict.fromkeys(states,0);triggers_checked=dict.fromkeys(states,0)
    at=8
    with tape.open('rb') as f, mmap.mmap(f.fileno(),0,access=mmap.ACCESS_READ) as mm, activity.open('rb') as act:
        check(mm[:8]==b'BTWTP002' and act.read(8)==b'BTWACT01','binary formats')
        while at<len(mm):
            ns,valid,force,history,reserved,updates,*book=HEADER.unpack_from(mm,at);at+=80
            raw=act.read(56);check(len(raw)==56,'activity row complete')
            feat=FEATURE.unpack(raw);check(feat[0]==ns and (last is None or ns>last),'exact activity and book clock');last=ns;rows+=1
            check(reserved==0 and valid in (0,1) and force in (0,1,2) and history in (0,1),'book flags')
            px=book[:4];sizes=book[4:]
            if valid:
                for side,price,new_display in UPDATE.iter_unpack(mm[at:at+updates*24]):
                    old=displayed[side].get(price,0)
                    if new_display==0:
                        displayed[side].pop(price,None)
                        for debts in debt.values():debts[side].pop(price,None)
                    else:
                        displayed[side][price]=new_display
                        for debts in debt.values():
                            if price in debts[side]:debts[side][price]=budget_after_display_change(debts[side][price],new_display)
                for side in range(4):check(displayed[side].get(px[side]*1000)==sizes[side],'L1 header depth agreement')
            else:check(updates==0,'invalid book cannot alter budget')
            at+=updates*24
            for name,state in states.items():
                cfg=state['config'];gate=cfg['activity_gate'];d=debt[name]
                allowed=(not gate['enabled'] or (feat[7]==3 and feat[1]>=gate['countA'] and feat[2]>=gate['countB'] and feat[3]>=gate['turnoverAunits'] and feat[4]>=gate['turnoverBunits'] and 0<=feat[5]<=gate['maxAgeNs'] and 0<=feat[6]<=gate['maxAgeNs']))
                pass_counts[name]+=allowed
                if ns in state['triggers']:
                    c,remaining,realized=state['triggers'][ns];reason=c['exit_reason'];lng=VENUES.index(c['long_venue']);el=D(c['long_entry_price']);es=D(c['short_entry_price'])
                    if reason=='funding_guard':check(force==1,'funding trigger uses causal force',name)
                    elif reason=='replay_day_end':check(force==2,'day-end trigger',name)
                    elif reason=='holding_timeout':check(ns//1000000-int(c['entry_timestamp_ms'])>=60000,'timeout trigger',name)
                    else:
                        check(bool(valid),'risk trigger has valid book',name)
                        xl=D(px[2*lng+1])/100000;xs=D(px[2*(1-lng)])/100000
                        current=remaining*((xl-el)+(es-xs)-(el+es+xl+xs)*D('.0003'))
                        check(current==D(c['trigger_remaining_net_proxy']),'risk net from trigger quote',name)
                        if reason=='take_profit':check(current>=remaining*el*D('.0005'),'take-profit threshold',name)
                        elif reason=='stop_loss':check(current<=D(c['initial_quote_net_proxy'])*remaining/D(c['qty'])-remaining*el*D('.002'),'relative stop-loss threshold',name)
                        elif reason=='hard_stop_loss':check(realized+current<=-D(c['qty'])*el*D('.01'),'initial-notional realized-inclusive hard stop',name)
                        else:check(False,'known risk reason',reason)
                    triggers_checked[name]+=1
                if ns in state['snapshots']:
                    snap=state['snapshots'][ns]
                    check(bool(valid) and bool(history) and force==0,'entry valid/history/no funding force',name)
                    check(allowed,'all exact activity conditions at entry',name)
                    check(snap['prices']==px and snap['displayed']==sizes,'snapshot L1 agreement',name)
                    for side in range(4):
                        expected={price:(size,size-d[side].get(price,0)) for price,size in displayed[side].items()}
                        actual={r['price_units_1e8']:(r['displayed_qty'],r['remaining_quarter']) for r in snap['sides'][side]}
                        check(actual==expected,'complete pre-entry snapshot budget',name)
                    el=D(snap['entry_prices'][snap['long']])/100000;es=D(snap['entry_prices'][snap['short']])/100000
                    lng=VENUES.index(snap['long']);q=D(snap['qty']);baseline=q*((D(px[2*lng+1])/100000-el)+(es-D(px[2*(1-lng)])/100000)-(el+es+D(px[2*lng+1]+px[2*(1-lng)])/100000)*D('.0003'))
                    c=next(c for c in state['cycles'] if int(c['cycle'])==snap['cycle_id'])
                    check(baseline==D(c['initial_quote_net_proxy']),'reverse-entry baseline independent quote',name)
                    entries_checked[name]+=1
                orders=state['orders_by_ns'].get(ns,())
                for order in orders:
                    check(bool(valid),'fill has valid paired book',name)
                    venue=VENUES.index(order['venue']);side=2*venue+int(order['side']=='sell');price=px[side]*1000;q=int(order['qty'])
                    check(D(order['price'])==D(px[side])/100000,'fill equals opposing L1',name)
                    available=displayed[side][price]-d[side].get(price,0)
                    check(4*q<=available,'fill within unconsumed 25percent depth',name)
                    d[side][price]=d[side].get(price,0)+4*q;orders_checked[name]+=1
            if rows%500000==0:print(json.dumps({'audit_depth_rows':rows,'cursor_ns':ns}),flush=True)
        check(at==len(mm) and act.read(1)==b'','no trailing/missing aligned binary rows')
    check(rows==3160784,'full decision coverage')
    for name,state in states.items():
        cfg=state['config'];check(pass_counts[name]==cfg['activity_pass_decisions'] and rows-pass_counts[name]==cfg['activity_fail_decisions'],'all gate decision counts',name)
        check(orders_checked[name]==cfg['orders'] and entries_checked[name]==triggers_checked[name]==cfg['cycles'],'full orders/snapshot/trigger coverage',name)
        state['result'].update(activity_entry_checks=entries_checked[name],depth_fill_checks=orders_checked[name],risk_trigger_checks=triggers_checked[name],activity_pass_decisions=pass_counts[name])
    return {'status':'PASS','book_decisions':rows,'source_depth_changes':35675129,'budget_method':'independent consumed-debt ledger per scenario; display decrease clips debt and price deletion forgets debt; exact 25 percent participation in quarter-coins','risk_scope':'each recorded trigger checked against causal quote/force/timer and threshold; not an independent search for an earlier trigger'}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--run',default=str(EXP/'taker'));parser.add_argument('--output',default=str(EXP/'taker-independent-audit.json'));args=parser.parse_args()
    out=Path(args.output);check(not out.exists(),'refuse audit overwrite');started=time.time();report={'status':'FAIL','execution_enabled':False,'started_epoch':started}
    try:
        run=Path(args.run);aggregate=read_json(run/'aggregate.json');tape=BASE/'20261012-btw-exit-tape-v2/tape.bin';meta=read_json(tape.with_name('tape-metadata.json'));activity=EXP/'activity/features-decisions.bin';ameta=read_json(EXP/'activity/metadata.json')
        check(aggregate['execution_enabled'] is False and aggregate['activity_scope']=='entry_only','replay scope')
        check(digest(tape)==meta['tape_sha256']==ameta['book_tape_sha256'],'immutable original tape hash')
        check(digest(activity)==ameta['artifacts']['features-decisions.bin']['sha256'],'immutable activity feature hash')
        check(read_json(EXP/'activity/independent-audit.json')['status']=='PASS','independent activity audit')
        for name,sha in meta['source_evidence_sha256'].items():check(digest(Path(meta['source_archive'])/name)==sha,'raw source provenance hash',name)
        observations,settlements,funding=audit_funding(meta)
        report['gateoff_parity']=gateoff_parity(run,aggregate)
        states={c['name']:audit_scenario(run,c,observations,settlements) for c in aggregate['scenarios']}
        report['depth_and_gates']=audit_depth_and_gates(tape,activity,states,observations)
        report['scenarios']={name:state['result'] for name,state in states.items()};report['funding']=funding
        inputs=[run/'aggregate.json',tape.with_name('tape-metadata.json'),EXP/'activity/metadata.json',EXP/'activity/independent-audit.json',EXP/'thresholds.csv',EXP/'calibration.json',EXP/'protocol.json',Path(__file__)]
        for name in states:
            inputs.extend(run/name/f for f in ('orders.csv','cycles.csv','entry_budgets.jsonl'))
        report['provenance']={str(p.resolve()):{'sha256':digest(p),'bytes':p.stat().st_size} for p in inputs}
        report.update(status='PASS',elapsed_seconds=time.time()-started,checks=dict(CHECKS),total_checks=sum(CHECKS.values()),source_archive=meta['source_archive'],raw_archive_revalidation='source evidence hash-bound to original independent audit; activity extraction re-read all 288 gzip fragments and CRC/body-hash checked; this audit rechecks immutable derived-tape SHA',limitations=['Offline ideal synchronous L1 paired fills only; no latency or real order proof.','Existing retrospective day and historically selected universe; no new out-of-sample claim.','25 percent displayed-depth participation is a modelling assumption.','Risk threshold can be exceeded by quote gaps, partial fills, or missing executable depth.'])
    except BaseException as e:
        report.update(error=f'{type(e).__name__}: {e}',elapsed_seconds=time.time()-started,checks=dict(CHECKS),total_checks=sum(CHECKS.values()))
        out.write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n');raise
    out.write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n')
    lines=['# 活动筛选回放独立验收','',f'状态：{report["status"]}。共 {report["total_checks"]:,} 项检查，耗时 {report["elapsed_seconds"]:.2f} 秒。','',
           'gateoff 的订单及组合与原 C++ 结果逐字节一致，并与已验收的官方 Decimal Python 回放逐订单/组合经济值及纳秒时刻一致。三个情景的逐笔手续费、逐腿现金、等币数开平、25%可消耗盘口预算、开仓前快照、活动门槛及全部记录的退出触发条件均通过。','',
           '|情景|组合|订单|毛损益 USDT|手续费 USDT|净损益 USDT|Bybit余额|Bitget余额|','|---|---:|---:|---:|---:|---:|---:|---:|']
    for name,r in report['scenarios'].items():lines.append(f'|{name}|{r["cycles"]}|{r["orders"]}|{r["gross_usdt"]}|{r["fees_usdt"]}|{r["net_usdt"]}|{r["cash"]["bybit"]}|{r["cash"]["bitget"]}|')
    lines+=['','全部持仓已平，历史结算时刻与持仓区间无交集；资金费状态 VERIFIED_NO_SETTLEMENT，已核验资金费为0。全部开仓使用当时已公开的下一结算时间，并保留120秒保护余量。','',
            '本验收未独立重新搜索每个退出条件的最早触发时刻；已对每个记录的触发点计算阈值并核验。共同假设仍为同步、零延迟、对手一档成交；通过验收不等于已证实实盘套利。','',f'详细证据：[{out.name}]({out.resolve()})']
    out.with_suffix('.md').write_text('\n'.join(lines)+'\n')
    print(json.dumps({'status':report['status'],'total_checks':report['total_checks'],'elapsed_seconds':report['elapsed_seconds'],'scenarios':report['scenarios']},ensure_ascii=False))

if __name__=='__main__':main()

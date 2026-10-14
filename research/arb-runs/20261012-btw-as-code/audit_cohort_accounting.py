"""Independent Decimal accounting and noon-entry audit of a cohort CSV output."""
import argparse
from collections import defaultdict
import csv
from decimal import Decimal as D
import hashlib
import json
from pathlib import Path


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def rows(path):
    with Path(path).open() as f:return list(csv.DictReader(f))


def audit(directory):
    p=Path(directory);aggregate=json.loads((p/'aggregate.json').read_text())
    assert aggregate['status']=='COMPLETED_SCREEN_ONLY' and aggregate['execution_enabled'] is False
    assert aggregate['price_decimals'] in (5,6) and aggregate['money_decimals']==aggregate['price_decimals']+4
    results=[]
    for scenario in aggregate['scenarios']:
        name=scenario['name'];folder=p/name;orders=rows(folder/'orders.csv');cycles=rows(folder/'cycles.csv')
        snapshots=[json.loads(l) for l in (folder/'entry_budgets.jsonl').open()]
        assert not scenario['unresolved_qty'], 'Incomplete cycles cannot pass this closed-account audit'
        assert len(orders)==scenario['orders'] and len(cycles)==scenario['cycles']==len(snapshots)
        cash={v:D(1000) for v in ('bybit','bitget')};entries={};closed=defaultdict(lambda:D(0));economics=defaultdict(lambda:defaultdict(lambda:D(0)))
        bycycle={r['cycle']:r for r in cycles};assert len(bycycle)==len(cycles)
        last=-1
        for order in orders:
            ns=int(order['ns']);assert ns>=last and ns>=aggregate['entry_start_ns'];last=ns
            assert int(order['timestamp_ms'])==ns//10**6
            key=(order['cycle'],order['venue']);q,price,fee=map(D,(order['qty'],order['price'],order['fee']))
            assert q>0 and q%10==0 and price>0 and fee==q*price*D('.0003')
            row=bycycle[order['cycle']];long=order['venue']==row['long_venue']
            assert order['side']==('buy' if (long==(order['intent']=='entry')) else 'sell')
            g=D(0)
            if order['intent']=='entry':
                assert key not in entries;entries[key]=(q,price,ns)
                assert q==D(row['qty']) and ns==int(row['entry_ns'])
                assert price==D(row['long_entry_price'] if long else row['short_entry_price'])
            else:
                assert order['intent']=='exit' and key in entries
                eq,ep,ens=entries[key];assert ns>ens
                closed[key]+=q;assert closed[key]<=eq
                g=q*(price-ep)*(1 if long else -1)
            cash[order['venue']]+=g-fee
            assert cash[order['venue']]==D(order['cash']), 'Per-order cash mismatch'
            econ=economics[order['cycle']];econ['gross']+=g;econ['fees']+=fee;econ[order['venue']]+=g-fee
        for row,snapshot in zip(cycles,snapshots):
            cid=row['cycle'];econ=economics[cid]
            assert str(snapshot['cycle_id'])==cid and snapshot['qty']==int(row['qty'])
            assert snapshot['entry_ns']==int(row['entry_ns']) and snapshot['pre_entry_consumption'] and not snapshot['after_entry_consumption']
            assert snapshot['price_decimals']==aggregate['price_decimals'] and snapshot['money_decimals']==aggregate['money_decimals']
            assert snapshot['native_tick_units']==aggregate['native_tick_units']
            assert snapshot['long']==row['long_venue']
            for v in cash:
                assert entries[(cid,v)][0]==closed[(cid,v)]==D(row['qty'])
                assert econ[v]==D(row[v+'_net_before_funding'])
                assert D(snapshot['entry_prices'][v]).scaleb(-aggregate['price_decimals'])==entries[(cid,v)][1]
            assert econ['gross']==D(row['gross_pnl']) and econ['fees']==D(row['total_fees'])
            assert econ['gross']-econ['fees']==D(row['net_before_funding'])
        assert sum((e['gross'] for e in economics.values()),D(0))==D(scenario['gross_pnl'])
        assert sum((e['fees'] for e in economics.values()),D(0))==D(scenario['fees'])
        assert sum(cash.values())-D(2000)==D(scenario['net_before_funding'])
        assert all(cash[v]==D(scenario['cash'][v]) for v in cash)
        results.append({'name':name,'orders_verified':len(orders),'closed_cycles_verified':len(cycles),
                        'net_before_funding':scenario['net_before_funding'],'cash':{v:str(n) for v,n in cash.items()},
                        'artifact_sha256':{n:sha(folder/n) for n in ('orders.csv','cycles.csv','entry_budgets.jsonl')}})
    return {'status':'PASS','scope':'Cohort taker reference accounting only; does not certify maker fills or strategy profitability',
            'entry_start_ns':aggregate['entry_start_ns'],'price_decimals':aggregate['price_decimals'],'money_decimals':aggregate['money_decimals'],
            'aggregate_sha256':sha(p/'aggregate.json'),'audit_source_sha256':sha(__file__),'scenarios':results}

if __name__=='__main__':
    cli=argparse.ArgumentParser();cli.add_argument('cohort_dir');args=cli.parse_args()
    result=audit(args.cohort_dir)
    with (Path(args.cohort_dir)/'accounting-audit.json').open('x') as f:json.dump(result,f,indent=2)
    print(json.dumps(result))

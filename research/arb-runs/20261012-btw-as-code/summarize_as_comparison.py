"""Aggregate audited independent quote diagnostics; never label entry savings as PnL."""
import argparse,csv,json
from decimal import Decimal as D
from pathlib import Path
from day_history import sha,write

def summarize(replay):
    summary=json.loads((replay/'summary.json').read_text());date=summary['date_utc'];schema=summary['price_schema'];unit=D(schema['price_unit_usdt'])
    source=Path(summary['inputs']['entry_snapshots']['path'])
    if sha(source)!=summary['inputs']['entry_snapshots']['sha256']:raise ValueError('cohort hash mismatch')
    snapshots={int(s['entry_ns']):s for s in map(json.loads,source.read_text().splitlines())}
    if any(ns<summary['evaluation_start_ns'] for ns in snapshots):raise ValueError('final cohort contains pre-noon simulated entries')
    rows=[json.loads(line) for line in (replay/'quotes.jsonl').read_text().splitlines()]
    result=[]
    for name in ('fixed_ticks_1','fixed_ticks_5','as_inside'):
        selected=[r for r in rows if r['scenario_id']==name];metrics=summary['scenarios'][name]
        if len(selected)!=len(snapshots):raise ValueError('scenario candidates mismatch')
        advantages=D(0);ref_paired=D(0);ref_all=D(0);residual=D(0);resfee=D(0);saving=D(0);slippage=D(0);feedelta=D(0)
        for row in selected:
            s=snapshots[row['signal_ns']];longprice=D(s['prices'][0 if s['long']=='bybit' else 2])*unit
            if row['maker_qty']!=row['hedged_qty']+row['unhedged_qty']:raise ValueError('quantity conservation')
            ref_paired+=row['hedged_qty']*longprice;ref_all+=row['target_qty']*longprice
            quote=D(row['quote_price_units'])*unit;residual+=row['unhedged_qty']*quote;resfee+=row['unhedged_qty']*quote*D('.0003')
            advantages+=D(row['entry_cost_advantage_on_hedged_qty']);saving+=D(row['maker_price_saving_on_hedged_qty']);slippage+=D(row['hedge_slippage_on_hedged_qty']);feedelta+=D(row['entry_fee_delta_on_hedged_qty'])
        if advantages!=saving-slippage-feedelta:raise ValueError('cost decomposition')
        result.append({'date_utc':date,'scenario':name,'candidates':len(selected),'accepted_at_arrival':sum(r['arrival_ns'] is not None for r in selected),'maker_fill_quotes':metrics['quotes_with_maker_fill'],'fully_hedged_quotes':metrics['quotes_fully_hedged'],'paired_coins':metrics['hedged_qty'],'residual_coins':metrics['unhedged_qty'],'maker_fill_rate':str(D(metrics['quotes_with_maker_fill'])/len(selected)) if selected else None,'entry_advantage_usdt':str(advantages),'paired_reference_notional_usdt':str(ref_paired),'entry_advantage_bps_on_paired_notional':str(10000*advantages/ref_paired) if ref_paired else None,'all_candidate_reference_notional_usdt':str(ref_all),'entry_advantage_per_candidate_usdt':str(advantages/len(selected)) if selected else None,'maker_price_saving_usdt':str(saving),'hedge_price_worsening_usdt':str(slippage),'entry_fee_difference_usdt':str(feedelta),'residual_at_fill_notional_usdt':str(residual),'residual_entry_fee_usdt':str(resfee),'rejection_and_terminal_reasons':metrics['reasons'],'portfolio_pnl_computed':False,'source_summary_sha256':sha(replay/'summary.json'),'source_quotes_sha256':sha(replay/'quotes.jsonl')})
    return result

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--root',type=Path,required=True);ap.add_argument('--replays',type=Path,nargs='+',required=True);a=ap.parse_args();rows=[]
    for replay in a.replays:rows.extend(summarize(replay))
    write(a.root/'comparison.json',{'scope':'Independent entry-price quote diagnostics; not closing-strategy or portfolio return','rows':rows,'execution_enabled':False,'summarizer_sha256':sha(__file__)})
    with (a.root/'comparison.csv').open('w',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
    lines=['| 日期 UTC | 报价 | 机会数 | Maker 成交 / 完全对冲 | 配对 / 未对冲 BTW | 配对入场成本优势 bp |','|---|---|---:|---:|---:|---:|']
    for r in rows:
        b=r['entry_advantage_bps_on_paired_notional'];b='无配对成交' if b is None else f'{D(b):+.3f}'
        lines.append(f"| {r['date_utc']} | {r['scenario']} | {r['candidates']} | {r['maker_fill_quotes']} / {r['fully_hedged_quotes']} | {r['paired_coins']} / {r['residual_coins']} | {b} |")
    (a.root/'comparison-table.md').write_text('\n'.join(lines)+'\n')
    print(json.dumps({'dates':sorted({r['date_utc'] for r in rows}),'rows':len(rows)}))
if __name__=='__main__':main()

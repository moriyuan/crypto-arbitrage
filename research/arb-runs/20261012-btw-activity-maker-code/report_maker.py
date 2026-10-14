"""Describe immutable order-level diagnostic artifacts; no simulation or parameter selection."""
import json
from pathlib import Path
from decimal import Decimal as D
from collections import Counter
from statistics import median
import hashlib

EXP=Path('/Users/mori/code/crypto/state/arb-runs/20261012-btw-activity-maker')
RUN=EXP/'maker-q25'
rows=[json.loads(l) for l in (RUN/'quotes.jsonl').read_text().splitlines()]
events=[json.loads(l) for l in (RUN/'events.jsonl').read_text().splitlines()]
snapshots={int(s['cycle_id']):s for s in map(json.loads,(EXP/'taker/q25/entry_budgets.jsonl').read_text().splitlines())}
summary=json.loads((RUN/'summary.json').read_text())
scenarios={}
for alpha in (25,50,75):
    rr=[r for r in rows if r['alpha_percent']==alpha]
    ee=[e for e in events if e['alpha_percent']==alpha]
    paired=[r for r in rr if r['hedged_qty']]
    basis=sum(D(r['hedged_qty'])*snapshots[r['cycle']]['entry_prices'][r['long_venue']]/D(100000) for r in paired)
    decomposition={}
    for key in ('maker_price_saving_on_hedged_qty','hedge_slippage_on_hedged_qty','entry_fee_delta_on_hedged_qty','entry_cost_advantage_on_hedged_qty'):
        val=sum(D(r[key]) for r in paired)
        decomposition[key]={'amount_usdt':str(val),'bps':str(val/basis*10000)}
    unresolved=[r for r in rr if r['unhedged_qty']]
    dust=[r for r in unresolved if r['unhedged_qty']<10]
    depth=[r for r in unresolved if r['unhedged_qty']>=10]
    waits=[a['latency_ns']/1e6 for e in ee if e['type']=='hedge_fill' for a in e['allocations']]
    arrivals=[e['latency_ns']/1e6 for e in ee if e['type']=='quote_arrival']
    scenarios[str(alpha)]={'counts':summary['scenarios'][str(alpha)],'matched_reference_long_notional_usdt':str(basis),
      'decomposition':decomposition,'unhedged_dust_quotes':len(dust),'unhedged_dust_qty':sum(r['unhedged_qty'] for r in dust),
      'unhedged_depth_quotes':len(depth),'unhedged_depth_qty':sum(r['unhedged_qty'] for r in depth),
      'unhedged_snapshot_notional_at_maker_price':str(sum(D(r['unhedged_qty'])*r['quote_ticks']/D(100000) for r in unresolved)),
      'unhedged_qty_distribution':dict(Counter(r['unhedged_qty'] for r in unresolved)),
      'hedge_receipt_to_execution_latency_ms':{'fragments':len(waits),'median':median(waits),'max':max(waits)},
      'quote_request_to_arrival_latency_ms':{'count':len(arrivals),'median':median(arrivals),'max':max(arrivals)},
      'late_notification_reopened_quotes':len({e['cycle'] for e in ee if e['type']=='late_fill_notification_reopens_inventory'}),
      'maker_venue_counts':dict(Counter(r['maker_venue'] for r in rr))}
result={'status':'DESCRIPTIVE_ONLY','execution_enabled':False,'portfolio_pnl_computed':False,
 'source_summary_sha256':hashlib.sha256((RUN/'summary.json').read_bytes()).hexdigest(),
 'basis':'Each alpha uses only actually hedged hypothetical quantities, weighted by their original signal long-entry ask notional. Fee difference covers only both entry fills, not exit or funding.',
 'scenarios':scenarios}
(EXP/'maker-analysis.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
def fmt(v,n=2):return f'{D(v):.{n}f}'
lines=['# BTW 价差内单腿挂单诊断','',
'在同一批 130 个有效入场信号上，价差内挂单确实改善挂单腿的报价，但本轮成交后的另一所对冲价格恶化更大。三种挂单位置都没有显示优于原信号时立即双腿吃单的入场成本；这不是完整策略盈亏比较。','',
'固定使用 2026-10-01 UTC 全日、q25 活跃度门槛筛选后的实际 taker 入场时点、方向和原始数量。三种 alpha 对每个时点各做独立订单假设；不根据本轮结果选择 alpha。原始数据共 3,160,784 个盘口决策、768,203 笔实时成交（Bybit 609,033、Bitget 159,170），Bitget 300 笔首包历史成交已排除，39,030 笔 Bybit RPI 成交不作为普通挂单成交证据。','',
'买单报价为 `bid + alpha × (ask − bid)`，卖单为 `ask − alpha × (ask − bid)`；按最小报价单位取整并限制在买卖价差内部。alpha 越大越靠近对手价、越积极。选择自身买卖价差较宽的交易所挂单，另一所实际成交后吃单对冲；手续费均为每笔 0.03%。','',
'| alpha | 候选 | PostOnly 拒单 / 无内部价位 | 有 maker 成交 | 有任意对冲 / 完全对冲 | maker / 已对冲 / 残余币数 |',
'|---|---:|---:|---:|---:|---:|']
for alpha,x in scenarios.items():
 c=x['counts'];reasons=c['reasons']
 lines.append(f"| {alpha}% | {c['quotes']} | {reasons.get('post_only_would_cross',0)} / {reasons.get('no_legal_inside_tick',0)} | {c['quotes_with_maker_fill']} ({100*c['quotes_with_maker_fill']/c['quotes']:.2f}%) | {c['quotes_with_any_hedge']} / {c['quotes_fully_hedged']} | {c['maker_qty']} / {c['hedged_qty']} / {c['unhedged_qty']} |")
lines += ['',
'下表只比较完成对应数量对冲的片段，正的“挂单报价节省”有利，正的“对冲价格恶化”不利。统一以这些片段在原信号时的买入腿名义金额为分母：`bp = 成本差金额 / Σ(已对冲币数 × 原信号买入价) × 10,000`。不同 alpha 的实际成交样本不同，因此这些数值不能单独用于择优。','',
'| alpha | 匹配单腿名义金额 (USDT) | 挂单报价节省 | 对冲价格恶化 | 入场手续费增量 | 合并入场成本优势 |',
'|---|---:|---:|---:|---:|---:|']
for alpha,x in scenarios.items():
 d=x['decomposition']
 vals=[fmt(d[k]['bps'])+' bp' for k in ('maker_price_saving_on_hedged_qty','hedge_slippage_on_hedged_qty','entry_fee_delta_on_hedged_qty','entry_cost_advantage_on_hedged_qty')]
 lines.append(f"| {alpha}% | {fmt(x['matched_reference_long_notional_usdt'],4)} | "+' | '.join(vals)+' |')
lines += ['',
'这些恶化来自实际假设挂单等待及成交后至少 100ms 对冲期间的价格变化，相对于原信号价格衡量。没有同延迟、同成交样本的双腿 taker 对照，因此不能把全部差额归因为排队逆向选择。也不能把报价节省直接当作收益：尚未加入这批非同步持仓的完整退出、资金费和风险处置。','',
'未对冲部分均发生在 Bitget 挂单腿。Bybit 对冲采用公共最小币数 10、最低金额 5 USDT；Bitget 可以产生 1–9 币的小额部分成交，这些量没有被舍去。','',
'| alpha | 零头无法合法对冲 (报价数 / 币数) | 足量但缺深度 (报价数 / 币数) | 残余名义金额 (按 maker 成交价) |',
'|---|---:|---:|---:|']
for alpha,x in scenarios.items():
 lines.append(f"| {alpha}% | {x['unhedged_dust_quotes']} / {x['unhedged_dust_qty']} | {x['unhedged_depth_quotes']} / {x['unhedged_depth_qty']} | {fmt(x['unhedged_snapshot_notional_at_maker_price'],5)} USDT |")
lines += ['',
'各方案都包含第 83 个候选：Bitget 先成交 2 币再成交 8 币，已达到 10 币，但等待期间没有足够可用 Bybit 一档深度完成对冲，5 秒后保留为未解决。其余残余均少于 10 币。表中金额是独立假设样本的描述性总量，不是同时存在的账户敞口，更不是已实现损失。没有虚构应急平仓。','',
'| alpha | 挂单实际到达延迟中位数 / 最大 | 成交回报至对冲延迟中位数 / 最大 |',
'|---|---:|---:|']
for alpha,x in scenarios.items():
 a=x['quote_request_to_arrival_latency_ms'];h=x['hedge_receipt_to_execution_latency_ms']
 lines.append(f"| {alpha}% | {a['median']:.2f} / {a['max']:.2f} ms | {h['median']:.2f} / {h['max']:.2f} ms |")
lines += ['',
'模拟到达和对冲在不早于 100ms 的首个已确认盘口决策上执行；到达时会吃对手价的挂单按 PostOnly 拒绝。挂单最多保留 2 秒；首次成交、活跃度不满足、资金费保护或日终保护均请求撤余单，100ms 后生效。已成交库存继续对冲。每笔模拟 maker 成交要求正确主动买卖方向、成交源时间晚于挂单到达，且价格严格穿过挂单价；最多参与公开成交量的 25%，并遵循交易所数量增量。触价不算成交。','',
'撤单按成交源时间决定生效边界；撤单后的延迟回报仍会计入，只要其成交源时间在真实挂单窗口内。所有公开成交的接收延迟必须不超过 1 秒，撤单后的状态至少保留 1 秒以捕获迟到回报。本档案最大观测延迟 251.2902ms，三个方案没有出现需要重新打开已完全对冲状态的迟到成交；回归测试覆盖了这种情况。','',
'公共 L2 与逐笔成交不能证明反事实排队顺序、真实成交量或市场冲击。该模型没有真实订单，也没有账户净收益或完整资金费结算。全部结果来自已多次观察的一天，既不是新样本外验证，也不构成实盘收益证明。','',
'- [事件明细](maker-q25/events.jsonl)',
'- [每个报价结果](maker-q25/quotes.jsonl)',
'- [模拟参数、源哈希与汇总](maker-q25/summary.json)',
'- [描述性指标](maker-analysis.json)',
'- [冻结的活动门槛选择](selection-frozen.json)',
'- [启动证据](maker-q25.launch.json)',
'']
(EXP/'maker-report.md').write_text('\n'.join(lines))
print(json.dumps({'report':str(EXP/'maker-report.md'),'analysis':str(EXP/'maker-analysis.json')}))

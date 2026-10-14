"""Render verified replay outputs without changing the ledger or strategy."""
from pathlib import Path
from decimal import Decimal as D
from datetime import datetime,timezone
import csv,json,statistics

ROOT=Path('/Users/mori/code/crypto/state/arb-runs/20261009-btw-tardis-replay-20261001')
def num(value,places=6):return f'{D(str(value)):,.{places}f}'
def stamp(ns):return datetime.fromtimestamp(int(ns)//1_000_000_000,timezone.utc).strftime('%H:%M:%S')+f'.{int(ns)%1_000_000_000//1000000:03d}'
def link(name,label=None):return f'[{label or name}]({ROOT/name})'

def main():
    summary=json.loads((ROOT/'summary.json').read_text())
    audit=json.loads((ROOT/'acceptance.json').read_text())
    depth=json.loads((ROOT/'depth-fill-audit.json').read_text())
    if audit['audit_status']!='PASS':raise RuntimeError('accounting audit did not pass')
    # Depth auditor uses a separately documented schema; preserve its verdict verbatim.
    verdict=depth.get('status',depth.get('audit_status'))
    if verdict not in ('PASS','VERIFIED'):raise RuntimeError('depth fill audit did not pass: '+str(verdict))
    cycles=audit['cycles'];n=len(cycles);wins=sum(D(x['net_before_funding'])>0 for x in cycles)
    holdings=[D(x['holding_seconds']) for x in cycles]
    pnl=D(audit['cumulative_net_before_funding']);fees=sum((D(x['fees']) for x in cycles),D(0));gross=sum((D(x['gross_pnl']) for x in cycles),D(0))
    rows=list(csv.DictReader((ROOT/'cycles.csv').open()));directions={x['cycle_id']:x['long_venue'] for x in rows}
    raw=summary['raw_counts'];signals=audit['signal_statistics'];risk=audit['equity_metrics']
    text=[
      '# BTW 跨所永续逐笔回放：2026-10-01 UTC',
      '',
      f'回放状态：**{summary["status"]}**。覆盖 2026-10-01 00:00–24:00 UTC（北京时间 10 月 1 日 08:00 至 10 月 2 日 08:00）。账务和模拟成交深度独立验收均通过。',
      '',
      f'两所初始虚拟本金各 1,000 USDT，总计 2,000 USDT。本轮完成 **{n} 组、{summary["orders"]} 条模拟订单**，累计手续费后损益 **{num(pnl)} USDT**，按总本金计 **{num(audit["return_pct_on_total_2000"])}%**。资金费状态为 `{audit["funding_status"]}`；所有实际持仓均未跨结算，核验金额为 0。剩余仓位：{len(audit["residual_positions"])}。',
      '',
      '| 指标 | 结果 |','|---|---:|',
      f'| Bybit 最终虚拟现金 | {num(audit["final_cash"]["bybit"])} USDT |',
      f'| Bitget 最终虚拟现金 | {num(audit["final_cash"]["bitget"])} USDT |',
      f'| 价差毛损益 | {num(gross)} USDT |',
      f'| 累计手续费 | {num(fees)} USDT |',
      f'| 盈利组合 / 全部组合 | {wins} / {n} |',
      f'| 持仓时间中位数 | {num(statistics.median(holdings)) if holdings else "无交易"} 秒 |',
      f'| 最大持仓时间 | {num(audit["max_holding_seconds"])} 秒 |',
      f'| 持仓不足 1 秒的组合 | {sum(h < 1 for h in holdings)} / {n} |',
      f'| 可计算平仓净值点的最大回撤 | {num(risk["max_drawdown_usdt"])} USDT |',
      f'| 输入预检至 CSV 导出完成耗时（不含后续验收） | {num(summary["wall_elapsed_seconds"]/60,2)} 分钟 |',
      '',
      '行情与信号记录：',
      '',
      '| 项目 | 数量 |','|---|---:|',
      f'| Bybit 实时逐笔成交 | {raw.get("trade_records:bybit:trade",0):,} |',
      f'| Bitget 实时逐笔成交 | {raw.get("trade_records:bitget:trade",0):,} |',
      f'| Bitget 首包/重连历史成交（单列） | {raw.get("trade_records:bitget:historical_trade",0):,} |',
      f'| 原始行（含控制及断线标记） | {raw["frames:bybit"]+raw["frames:bitget"]:,} |',
      f'| 盘口决策次数 | {summary["ticks"]:,} |',
      f'| 两个方向的信号评估数 | {summary["signal_evaluations"]:,} |',
      f'| 估算净边际达到 5bp 的方向评估 | {signals["cost_threshold_passed"]:,} |',
      f'| 同时通过深度、费用、历史准入的方向信号 | {signals["eligible"]:,} |',
      f'| 最终开仓次数 | {signals["entered"]:,} |',
      '',
      '同一个持续机会可能产生多次方向信号；持仓中、冷却期、结算保护时段等会阻止再次开仓，所以信号计数不等于独立机会数或订单数。所有原始事件按供应商历史接收顺序消费；相同纳秒的消息先合批更新盘口，再决策。逐笔成交保留原序，成交价格不替代买卖盘报价。',
      '',
      '每组模拟损益（数量是每条腿的初始 BTW 币数；时间为 UTC）：',
      '',
      '| 组 | 开仓 | 平仓 | 买入所 / 卖出所 | BTW 数量 | 分批退出次数 | 毛损益 | 费用 | 净损益 USDT |',
      '|---:|---|---|---|---:|---:|---:|---:|---:|',
    ]
    for i,c in enumerate(cycles,1):
        long=directions[c['cycle_id']];short='bitget' if long=='bybit' else 'bybit'
        text.append(f'| {i} | {stamp(c["entry_ns"])} | {stamp(c["last_exit_ns"])} | {long} / {short} | {c["legs"][long]["entry_qty"]} | {c["exit_parts"]} | {num(c["gross_pnl"])} | {num(c["fees"])} | {num(c["net_before_funding"])} |')
    text.extend([
      '',
      '每笔手续费 0.03%（3bp）；开仓单腿名义额上限 250 USDT，1 倍，同币数对冲。开平仓只使用两所各前 50 档、距本侧最优价 10bp 内的可见深度，每档显示量的 25% 形成参与预算，成交消耗预算。已知档位只有显示量增加才补预算，部分平仓始终两腿同币数；每组订单数为 `2 + 2 × 分批退出次数`。',
      '',
      '入场使用保护价测算，扣除四笔往返约 12bp 手续费、2bp 预计退出价差和 2bp 资金费缓冲后，净边际至少 5bp；每腿另有 1bp 价格保护并按 tick 取整。止盈触发为剩余双腿手续费后预计损益达到剩余多头入场名义额的 1bp（250 USDT 对应约 0.025 USDT）；深度不足时使用可执行分片价格外推。单组亏损达到 5 USDT 触发退出，整组闭合后累计净损益达到 −10 USDT 则停止新增；它们是触发值，实际损失可能超出。最长持仓目标 60 秒，平仓后冷却 30 秒。止盈触发名称不代表最终盈利，后续分批退出可能使组合转亏。',
      '',
      '本次覆盖全天，组合数量上限为 10,000（运行结果如触发风险限制会记录）；23:58 UTC 起停止新增并尝试平仓。已知下次资金费结算前 120 秒阻止新增并促使平仓；下一结算时刻只能在该历史 ticker 已到达后使用，过期后等待新证据。这里采样了每 4 小时首分钟的历史 ticker，并假设其中公告的 nextFundingTime 在到期前有效；最终是否跨结算另以实际历史结算时刻核验。',
      '',
      '历史准入：以已结束的 20 个 UTC 日计算平均日波动率，420 个加密候选中 418 个历史完整。BTW 当天第 3，低的一所 RV20 为 1,822.275425bp，前十门槛 1,106.480873bp；00:01 起允许准入，首 60 秒因历史窗口不足拒绝。没有历史成交额硬性下限。',
      '',
      '档案中有 6 次日内重连及 Bitget 日末未恢复断线；重置时清空该所盘口，收到新快照再恢复。日末断线不会用旧盘口自动成交。接收时间保留原始 7 位小数，报价年龄不超过 2 秒、两所时间偏差不超过 500 毫秒。',
      '',
      '**结果边界：**这是可见深度下双腿同步成交的离线仿真，尚未建模网络延迟、单腿失败与订单队列；极短持仓的收益尤其依赖这些假设。数据为 L2 价档增量及逐笔成交，无法还原 L3 单笔挂单队列。使用 10 月 9 日目录回溯存在幸存者偏差，BTW 也是事后选择的案例；当前数量步长和最小名义额作为情景假设，未声称已证明 10 月 1 日历史规格。此单日结果不能证明实盘可获利。',
      '',
      f'回撤口径：按两所合计估计平仓净值计算；{risk["complete_liquidation_marks"]:,} 个时点可完整估值，{risk["incomplete_liquidation_marks"]:,} 个时点因深度不足等原因未纳入。缺失点不按零收益填补，因此这里的最大回撤可能低估完整风险。',
      '',
      '结果文件：'+ ' · '.join([link('orders.csv','订单'),link('cycles.csv','组合损益'),link('signals.csv.gz','全部方向信号'),link('equity.csv.gz','账户净值'),link('summary.json','汇总'),link('run_metadata.json','参数与来源'),link('acceptance.json','账务验收'),link('depth-fill-audit.json','成交深度验收')]),
      '',
      '历史准入：[逐日门槛](/Users/mori/code/crypto/state/arb-runs/20261009-btw-oct1-history/gate-input.json) · [独立复算](/Users/mori/code/crypto/state/arb-runs/20261009-btw-oct1-history/independent-recalculation.json)；资金费：[已知时刻时间表](/Users/mori/code/crypto/state/arb-runs/20261009-btw-oct1-funding/causal-schedule.json)。',
    ])
    (ROOT/'report.md').write_text('\n'.join(text)+'\n')
    print(json.dumps({'report':str(ROOT/'report.md'),'cycles':n,'net':str(pnl),'audit':'PASS'}))

if __name__=='__main__':main()

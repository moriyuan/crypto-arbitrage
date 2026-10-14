"""Render the audited exit study; refuse to present unaudited screen as final."""
import json
from decimal import Decimal as D
from pathlib import Path

BASE=Path(__file__).resolve().parent.parent
EXP=BASE/'20261012-btw-exit-optimization'
RUN=BASE/'20261012-btw-exit-relative-tp5-sl20-h100-20261001'
OLD=BASE/'20261012-btw-l1-tp2-net-sl2-net-20261001'
def read(p):return json.loads(p.read_text())
def link(label,p):return f'[{label}]({p})'

def main():
    acc=read(RUN/'acceptance.json');depth=read(RUN/'depth-fill-audit.json');risk=read(RUN/'risk-audit.json');eq=read(RUN/'selected-equivalence.json');fund=read(RUN/'funding-audit.json');summary=read(RUN/'summary.json')
    assert acc['audit_status']=='PASS' and depth['status']==risk['status']==eq['status']=='PASS'
    assert fund['status']=='VERIFIED_NO_SETTLEMENT' and summary['status']=='COMPLETED'
    m=read(EXP/'research_metrics.json');analysis=read(EXP/'selection/screen-analysis.json');choice=read(EXP/'selection/selection-frozen.json')
    baseline=m['policies']['baseline_net_tp2_sl2'];chosen=m['policies'][choice['selected_name']]
    assert D(chosen['net'])==D(acc['cumulative_net_before_funding'])
    initial=2000;cash=acc['final_cash'];assert sum(D(x) for x in cash.values())-initial==D(chosen['net'])
    lines=['# BTW 退出规则优化与错失机会核查','',
      '同一 2026-10-01 UTC 全天原生逐笔成交与增量盘口；公开数据、虚拟账户。实现并验证了相对入场平仓净值的止损。确实观察到过早止损后的回归机会，但新候选全天仍亏损，末 6 小时也仍全部亏损，不能据此称策略已稳定盈利。','',
      '## 止盈少是否错过机会','',
      '旧规则 203 组止损中，77 组在原 60 秒期限内的后续可见报价曾回到扣费后 +2bp；57 组同时有合法片段数量，55 组有足量双腿一档未消耗深度可平原整仓（27.09%）。两个窗口——首次止损触发后、原单最终平仓后——命中数相同。后一个窗口有 1 组无后续有效观察，不能判为绝无机会。','',
      '55 组足量机会在原平仓后等待的中位数为 4.3562 秒，95 分位为 51.9763 秒。到达前，39 组的可平仓报价净值曾低于 −20bp，5 组低于 −50bp，最差观察值约 −229.92bp。放宽止损可能保留部分回归，也需要承担继续恶化和深度不足的风险。','',
      '这些是分别冻结旧入场数量的独立影子诊断，观察至原最长持仓期限或资金费/日终保护，不是同一账户可同时实现的收益。不将后来最好价制造成订单；盘口命中仍假设零延迟双腿同时成交。142 组的观察窗口出现过无效决策，未观察到机会不等于缺失区间绝无机会。','',
      '两套策略保留相同入场规则，但持仓及冷却时刻改变会改变下一次可入场时刻，所以实际交易不是完全相同的一批。不能将新增止盈组数直接视为原55组影子机会中的已挽回数量。','',
      link('逐组错失机会证据',EXP/'missed-opportunities.json'),'','## 新退出机制','',
      '每笔手续费 0.03%=3bp。一次双腿开平约 12bp 手续费，加上两所各自买卖价差，开仓立即反向平仓的净值天然为负。把绝对扣费净亏损 −2bp 当成保护线，常会在价差尚未来得及回归时退出；原账本没有重复实际扣费，问题是阈值与往返成本不匹配。','',
      '冻结候选：扣费后止盈 +5bp；相对开仓时立即平仓的每币净值再恶化 20bp 触发普通止损；另检查累计已实现净损益加剩余平仓净值是否低于初始多头单腿名义金额的 −100bp。前两项分母为剩余多头单腿的原开仓名义金额，硬阈值分母固定为初始该单腿名义金额。','',
      '入场冻结反向一档报价，基准按剩余币数缩放而不重设。保留原入场、两所各 1000 USDT、每腿最多 250 USDT、同币数、对手一档和 25% 未消耗深度约束，以及 60 秒超时、30 秒冷却和资金费保护。入场越硬线仅作标记；下一可执行决策重评当前损益，已恢复则可继续持仓。硬线不是损失保证，也不是已经验证的生产入场风控。','',
      '## 同日逐笔回放结果','',
      '| 指标 | 原扣费后 ±2bp | 新候选 |','|---|---:|---:|']
    fields=[('完整组合数','cycles'),('模拟订单数','orders'),('最终盈利组数','wins'),('最终亏损组数','losses'),('胜率 %','win_rate_pct'),('毛损益 USDT','gross'),('手续费 USDT','fees'),('扣费及资金费后净损益 USDT','net'),('分批退出组合','partial_exit_cycles'),('实际持仓超过60秒组合','holding_over_60s')]
    for label,key in fields:
        a,b=baseline[key],chosen[key]
        if key=='win_rate_pct':a=f'{D(a):.2f}';b=f'{D(b):.2f}'
        lines.append(f'| {label} | {a} | {b} |')
    lines += ['',f"最终虚拟余额：Bybit **{cash['bybit']} USDT**，Bitget **{cash['bitget']} USDT**；两所合计 {sum(D(x) for x in cash.values())} USDT。所有组合闭合，资金费核验为 VERIFIED_NO_SETTLEMENT。",'',
      '| 退出原因 | 组数 | 最终盈利组数 | 净损益 USDT |','|---|---:|---:|---:|']
    for name,values in chosen['exit_reasons'].items():lines.append(f"| {name} | {values['cycles']} | {values['wins']} | {values['net']} |")
    lines += ['',
      '新候选止盈触发 49 组，其中 48 组最终盈利、1 组亏损；另有 2 组超时退出最终盈利，所以实际盈利总数为 50。亏损的“止盈退出”先以盈利报价平 10/40 币，之后按首次退出原因继续分四批清仓，最终净亏 −0.031133190 USDT。不能将退出原因标签直接当成盈利判定。','',
      '预注册粗筛中，相同20bp相对止损、把净止盈目标降到2bp时有56组止盈，全天净损益 −0.152778320 USDT；止盈次数虽多于5bp目标的49组，收益并未更好。这个额外方案只经过筛选引擎，未另外运行全量Decimal验收；不足0.019 USDT的单日差距也不能证明5bp跨日更优。冻结选择依据仍是预先指定的训练/验证流程，不是事后按全日结果改选。','',
      '原规则持仓中位数 0.6445 秒，新候选 60.574 秒，最长 208.626 秒；60 秒是开始超时退出的期限，等一档深度凑齐可能更久。新候选 101 组超时，合计 −2.895681800 USDT，是剩余亏损的主要来源。','',
      '完整剩余仓位可同时平仓时的估值最大回撤：原 4.282030410、新 2.701470820 USDT；但无法完整估值的决策数由 42,566/3,160,784（1.35%）增加至 274,161/3,160,784（8.67%）。回撤统计省略了这些深度不足时点，不能据此宣称整体风险一定下降。','',
      '## 选参和时间分段','',
      '先登记 12 个粗参数候选和基线，按前 12h 的“净损益 −0.5×已闭合回撤”从每种止损机制选一个候选，再用中间 6h 验证。验证要求净损益更高且已闭合回撤不劣于基线；参数冻结后才计算末 6h 指标。跨分段的组合剔除，实际回放状态持续运行，不在边界重启账户。','',
      '| 时段 UTC | 原组合数 | 原净损益 | 新组合数 | 新净损益 |','|---|---:|---:|---:|---:|']
    for fold,label in [('training','00–12 训练'),('validation','12–18 验证'),('internal_holdout','18–24 内部留出')]:
        a=analysis['scenarios']['baseline_net_tp2_sl2'][fold];b=analysis['scenarios'][choice['selected_name']][fold]
        lines.append(f"| {label} | {a['cycles']} | {a['net']} | {b['cycles']} | {b['net']} |")
    lines += ['',
      '**末 6h 新候选 24 组全部亏损，没有止盈触发。** 该日此前已反复看过，分段只是内部检验，不是真正外部未见样本。当前证据支持修正过紧止损的机制，尚不支持默认启用这一组参数或宣称可实盘盈利。','',
      '| 全部预注册方案 | 训练净损益 | 验证净损益 | 留出净损益 | 全日净损益 |','|---|---:|---:|---:|---:|']
    for name,x in analysis['scenarios'].items():lines.append(f"| {name} | {x['training']['net']} | {x['validation']['net']} | {x['internal_holdout']['net']} | {x['full_day']['net']} |")
    lines += ['', '## 验收与可复现文件','',
      f"加速引擎先与旧基线 978 单、220 组逐单对齐（64,647 检查通过）。冻结候选又通过独立 Decimal/Python 完整回放，并与加速引擎逐单一致（{eq['checks']} 检查）。会计验收 {acc['checks']['checks']} 检查通过；深度验收独立核对 {depth['orders_verified']} 单和 {depth['entry_baselines_verified']} 个入场反向报价基准；风险验收 {risk['checks']['checks']} 检查通过。",'',
      '风险审计重算所有已记录的触发和退出原因；逐笔引擎一致性与来源深度核验提供独立交叉证据，但不构成真实延迟、排队、单腿失败或市场冲击的验证。历史合约数量规则沿用 10月9日快照作场景假设，未证明与10月1日完全一致；币种及交易日选择也有回顾性局限。','',
      f"原始永续数据：Bybit 609,033 笔实时成交，Bitget 159,170 笔实时成交另有300笔首包历史成交；共有3,160,784次决策、{summary['signal_evaluations']:,}次方向信号评估，其中{summary['eligible_signals']:,}次达到成本门槛，实际还受持仓、冷却、资金费保护与深度限制。采集源记录 Bybit 1 次、Bitget 6 次断线，回放保守失效并等待快照恢复。回放及导出实际耗时 {summary['wall_elapsed_seconds']:.2f} 秒。",'',
      ' - '+link('新回放汇总',RUN/'summary.json'),
      ' - '+link('新订单',RUN/'orders.csv')+' / '+link('逐组合损益',RUN/'cycles.csv'),
      ' - '+link('全部方向信号',RUN/'signals.csv.gz')+' / '+link('估值序列',RUN/'equity.csv.gz'),
      ' - '+link('会计验收',RUN/'acceptance.json')+' / '+link('原生深度验收',RUN/'depth-fill-audit.json')+' / '+link('风险验收',RUN/'risk-audit.json')+' / '+link('逐单引擎一致性',RUN/'selected-equivalence.json'),
      ' - '+link('预注册方案',EXP/'experiment-protocol.json')+' / '+link('冻结选择',EXP/'selection/selection-frozen.json'),
      ' - '+link('实现与用法',BASE/'20261012-btw-exit-optimization-code/README.md'),
      ' - '+link('现货辅助定价研究',EXP/'spot-pricing-report.md'),'']
    text='\n'.join(lines)
    for p in (EXP/'report.md',RUN/'report.md'):
        with p.open('x') as f:f.write(text)
    print(json.dumps({'status':'REPORT_WRITTEN','report':str(EXP/'report.md'),'net_usdt':chosen['net']},ensure_ascii=False))

if __name__=='__main__':main()

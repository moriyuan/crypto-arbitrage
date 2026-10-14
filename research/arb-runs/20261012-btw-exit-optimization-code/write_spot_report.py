import json
from pathlib import Path

BASE=Path(__file__).resolve().parent.parent
EXP=BASE/'20261012-btw-exit-optimization'
DATA=BASE/'20261012-btw-spot-anchor-day'
STUDY=EXP/'spot-pricing-study'
def read(p):return json.loads(p.read_text())
def link(label,p):return f'[{label}]({p})'

def main():
    d=read(STUDY/'evaluation.json');audit=read(DATA/'export-audit.json');v=read(DATA/'validation.json');gate=read(EXP/'spot-input-acceptance.json')
    assert d['status']=='COMPLETED_PRICING_INFORMATION_STUDY' and audit['status']=='PASS_WITH_EXPLICIT_INVALIDATIONS' and not audit['errors']
    assert v['archive_integrity']=='PASS' and all(x['reason']=='source_disconnect' for x in v['failures'])
    assert d['source']['spot_bbo_sha256']==audit['input_sha256']['bbo.csv.gz']
    lines=['# BTW 现货辅助定价的初步量化研究','',
       '在这一个交易日，现货只带来微弱的额外预测信息，没有证明限价单交易收益会提升。末6小时的有现货模型相对合约模型 MAE 降幅为0.02%–0.68%，绝对差约0.00079–0.01157bp；两个1秒模型的 MAE 仍劣于“价格不变”。不据此自动改变报价或执行方式。','',
       '## 数据身份与质量','',
       'BTW 指 Bitway 虚拟币，其现货上市及 BEP20 合约地址可由 [Bitget 官方公告](https://www.bitget.com/support/articles/12560603859567)核对；不是股票同名代码。使用 2026-10-01 UTC 全天 Bitget BTWUSDT 现货原生 `books` 和 `publicTrade`，与同日 Bybit/Bitget 永续配对。数据服务商对当前频道的说明见 [Tardis Bitget Spot](https://docs.tardis.dev/historical-data-details/bitget)。','',
       '1440个分钟文件完整，压缩原档案110,333,490字节；1,590,719条盘口增量序列链接匹配。146,628笔实时成交、350笔首包历史成交分开记录。源中有6次日内重连边界及1次日末标记，因此原生验证文件的整体 status=FAIL 表示“全天没有中断”不成立；archive_integrity=PASS，独立导出验收=PASS_WITH_EXPLICIT_INVALIDATIONS，20,591,241项检查无错误。','',
       '序列有效状态覆盖86,397.6338594秒，剩余2.3661406秒不填充报价；这不是新鲜度覆盖率。BBO导出保留逐帧时钟、来源、序列和失效行，独立字典L2实现抽核15,914次BBO。新鲜度另在研究入口过滤。','',
       '## 方法与边界','',
       '固定500ms时点，按原始本地接收时间只使用截至该时点已收到的消息。现货使用盘口自己的更新时间，不用成交消息时间延长盘口寿命；本地/源时间年龄上限2秒，源时间容忍未来500ms。永续要求最新 tape 决策有效且距离网格点不超过500ms；沿用决策时原盘口检查，没有在网格点重新核验每所盘口年龄。','',
       '基线一是未来中价不变；基线二仅用两所永续过去1秒收益和跨所正常基差偏离；第三个模型加入现货过去1秒收益及现货相对该所永续的正常基差偏离。正常基差用仅过去数据的5分钟半衰期EMA，至少10分钟预热，间断超过60秒重置；EMA跨训练/验证边界沿用过去状态。','',
       '模型是带截距、固定0.001岭惩罚的线性回归，只在00–12h标准化和拟合，先保存全部系数才计算12–18h验证及18–24h内部留出。1秒历史收益窗口与未来标签都不能跨分段边界；每个比较用完全相同的有效样本。未来标签是1秒/5秒合约中价对数收益，单位bp，不是可成交价差或利润。没有按留出结果挑选模型或更改参数。','',
       f"共 {d['coverage']['samples']:,} 个网格点，两所永续有效 {d['coverage']['perp_valid_samples']:,} 个，现货新鲜有效 {d['coverage']['spot_valid_samples']:,} 个，三者同时有效 {d['coverage']['all_valid_samples']:,} 个。预热、历史窗口和未来窗口过滤后，样本进一步减少。",'',
       '## 内部留出结果（18–24h UTC）','',
       '| 目标 | 样本数 | 不变 MAE | 合约模型 MAE | 加现货 MAE | 相对合约 MAE 改善 | 合约/加现货 RMSE |',
       '|---|---:|---:|---:|---:|---:|---:|']
    for key,folds in d['results'].items():
        z,a,b=(folds['internal_holdout'][x] for x in ('no_change','perpetual_only','with_spot'))
        improvement=(a['MAE_bps']-b['MAE_bps'])/a['MAE_bps']*100
        lines.append(f"| {key} | {b['n']} | {z['MAE_bps']:.6f} | {a['MAE_bps']:.6f} | {b['MAE_bps']:.6f} | {improvement:.4f}% | {a['RMSE_bps']:.6f} / {b['RMSE_bps']:.6f} |")
    lines += ['', 'MAE、RMSE 均为bp。预测误差的微小改善不能换算成同等可交易收益，也不能直接与手续费做盈亏等式。重叠的1秒/5秒标签使样本相关，这些行数不是独立试验数；没有给出显著性或外日稳定性结论。500ms采样也不能评估亚毫秒抢单能力。','',
       '## 对限价报价的含义','',
       '同时从两所合约价格减去同一个现货价，并不创造新的跨所价差：`(P_B−S)−(P_A−S)=P_B−P_A`。现货的潜在作用是提前变化信息、每所正常基差或陈旧报价识别，而不是把永续强行拉到现货绝对价。','',
       '若继续研究，可把现货与过去正常基差得到的公平价作为报价参考，再用另一所实际可对冲的一档价格、共同币数深度、开平费用及目标净边际限制报价。被动买价需要同时低于公平买入上限和另一所卖出对冲可承受的上限；被动卖价需要同时高于对应下限，按交易所价格网格取整。公平价不能凌驾于可执行对冲约束。','',
       '使用 PostOnly 时，报价若到达时会立即成交可能被取消；它不是“挂限价就必然成交”。[Bybit 官方 Post-Only 说明](https://www.bybit.com/en/help-center/article/Post-Only-Order)给出了该行为。当前公开L2不能还原自己在每笔委托中的精确队列位置，因此不把触碰报价当作被动成交。','',
       '应将“相同 maker/taker 执行、有无现货信号”作为可比实验，再分别建模排队与撤单、成交后对冲延迟、部分成交和账户实际 maker 手续费。本轮没有这些成交模型，没有现货辅助限价收益结论，也没有改变本轮直接吃单的退出回放。','',
       '该日已被此前研究反复查看，内部时间分段不是外部未见数据。当前更明确的问题仍是退出深度、等待回归期间的风险，以及末6小时入场后未有效回归；现货信号的这点误差改善不足以证明解决了这些问题。','',
       '## 结果文件','',
       '- '+link('研究协议',EXP/'spot-study-protocol.json')+' / '+link('输入质量验收与口径说明',EXP/'spot-input-acceptance.json'),
       '- '+link('全部指标',STUDY/'evaluation.json')+' / '+link('冻结模型系数',STUDY/'models-frozen.json')+' / '+link('因果对齐中价',STUDY/'aligned-midpoints.npz'),
       '- '+link('现货档案报告',DATA/'report.md')+' / '+link('原生验证',DATA/'validation.json')+' / '+link('独立导出验收',DATA/'export-audit.json'),
       '- '+link('研究实现',BASE/'20261012-btw-exit-optimization-code/spot_pricing_study.py'),
       '- '+link('止盈止损完整回放报告',EXP/'report.md'),'']
    with (EXP/'spot-pricing-report.md').open('x') as f:f.write('\n'.join(lines))
    print(EXP/'spot-pricing-report.md')

if __name__=='__main__':main()

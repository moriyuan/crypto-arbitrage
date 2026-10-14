**活跃度筛选对部分日期有帮助，但本轮不支持直接把门槛从q25提高到q75。** q75三日样本少亏0.6229USDT，仍亏6.6707USDT；8月仍持续亏损，9月样本太少。按事先冻结的规则，保留q25作为主要验证条件。

本轮只调整两所成交活跃度门槛，固定开仓价差30bp、扣四笔费用后止盈20bp、相对入场立即反向净清算值恶化10bp止损、100bp绝对硬止损、最长15秒持仓。仍为0新增执行延迟、双腿对手一档全额吃单；每笔费用0.03%，每所初始1000USDT、每腿最高250USDT、30秒冷却、合计已实现亏损达到10USDT后停止新增。退出允许超过一档显示深度，历史盘口不扣除模拟冲击。

两所过去60秒成交笔数、成交额均须达到各自q25/q50/q75门槛，最近成交年龄均≤5秒且暖机有效。分位点由该日00:00≤UTC<12:00的等间隔1秒有效样本校准，12点以后开仓，23:58开始日终保护。活动条件下降只阻止新增仓位，不阻断平仓与风险控制。

8月、9月用于内部选择：每个日期至少10组交易、最差日及两日合计净损益均改善才允许提高门槛；选择先冻结，10月再运行三种预先声明的条件。q50/q75在9月只有9/4组，且8月实际净额更差，因此未提升。10月q50/q75属于诊断对照，没有据此回改冻结选择。这三个日期和BTW此前已研究过，不是全新未见样本。

| 日期 UTC | q25 净损益/组合数 | q50 净损益/组合数 | q75 净损益/组合数 |
|---|---:|---:|---:|
| 2026-08-01 | -10.0001U / 80组 | -10.2696U / 81组 | -10.4450U / 64组 |
| 2026-09-01 | +0.0723U / 17组 | +0.2514U / 9组 | +0.2790U / 4组 |
| 2026-10-01 | +2.6342U / 80组 | +2.6812U / 67组 | +3.4953U / 54组 |
| 三日样本合计 | -7.2936U / 177组 | -7.3369U / 157组 | -6.6707U / 122组 |

各日期账户独立重置，上述合计不是连续账户收益率。三种门槛在8月均触发10U日损失保护；最后一笔会越过上限，且停止时间不同，不能将末笔越界差额解读为完整交易日的精确门槛效果。

**盈利与亏损来源**

10月q75相对q25：中价基差有利变化收益从10.0144U降到9.5426U，保留95.29%；买卖价差成本从4.2744U降到3.7402U，手续费从3.1058U降到2.3071U。回归收益少0.4718U，但成本少1.3329U，净改善0.8611U。交易减少80→54组，净盈利34→32组、胜率42.5%→59.3%；最大清算回撤仍为1.2127U。

8月q75的中价基差变化收益却为−0.2050U，加上6.5498U买卖价差成本和3.6902U手续费，最终−10.4450U。最长连续净亏由q25的27组增至39组。成交更活跃没有保证价差朝有利方向变化；当前归因还不能证明是单边冲击、趋势或其他具体机制。

9月q75成本更低、净额从0.0723U提高至0.2790U，但只有4组交易、2组盈利，不能仅凭该结果证明改善稳健。

三日q75共122组、36组净盈利；81组中价基差向有利方向变化，其中45组仍在支付盘口成本和手续费后亏损。累计分解：中价基差变化+9.8040U − 买卖价差成本10.3891U − 手续费6.0856U = −6.6707U。相比q25，三日实际价差毛利反而下降，净少亏主要来自手续费节省，仍未解决成本覆盖问题。

**被过滤的机会与允许开仓时间**

10月q75活动条件挡住原q25的28个入场：其中3个原盈利、25个原亏损，原账本净额合计−0.1350U。但q75还产生3个新的入场，且其他交易的持仓/冷却路径可能变化。因此实际改善的0.8611U不能全部归因于“挡住亏损单”。8月q50挡住原来的12个亏损入场，实际交易反而从80增至81组且总净额更差，也体现了账户路径变化。

下面是12:00–23:58共43080个等间隔秒中，单独活动条件允许开仓的比例，不含盘口、资金费、现金、已有仓位及冷却限制，也不受当日已停损后是否实际交易的影响。它不是事件加权通过率。

| 日期 UTC | q25 时间覆盖 | q50 时间覆盖 | q75 时间覆盖 |
|---|---:|---:|---:|
| 2026-08-01 | 42.05% | 24.16% | 10.96% |
| 2026-09-01 | 17.35% | 7.78% | 2.40% |
| 2026-10-01 | 34.85% | 23.41% | 13.53% |

q75在9月仅允许2.40%的观察时间，解释了机会数量偏少。详细逐UTC小时覆盖保存在comparison.json；本轮没有据三个日期的盈亏事后排除固定钟点。

**参数方向与下一步判断**

1. 保留基础q25门槛，将q75作为待验证候选；增加未用于研究的交易日后再评估跨日稳定性，不放宽本轮最低交易样本条件。
2. 优先将活跃度与入场可计算的清算成本上限、反向盘口深度限制联合测试。活动强不等于价差成本低，q75仍有45组回归但净亏；应先限制不够覆盖成本的机会。
3. 针对8月高活跃状态仍连亏，单独检验开仓前短期基差是否持续扩张、净清算值是否恶化的限制，并保留连亏暂停候选。它们是下一轮假设，当前没有测定最优回看长度或证明具体亏损机制。
4. 本轮暂不叠加连续活跃10/30秒条件：提高活动门槛未取得两选参日共同改善，先修正成本与不利价差状态的筛选更有针对性。若后续检验持续时间，应单独预登记，保留本轮数据与结果。

**验收**

9份回放、456组组合、1824笔模拟订单全部独立对账和逐时点深审通过；同量双腿、费用、现金、最早允许开平时点一致，无残仓、资金费均VERIFIED_NO_SETTLEMENT。q25三天的订单/组合/权益CSV与上一轮逐字节一致。三日9208485条活动决策和259200个均匀秒，从1685475笔成交独立复算通过；本轮没有重下行情、读取私密凭据或发送真实订单。

聚焦测试：校准7项、选择8项、独立审计11项、归因7项通过。引擎没有修改，其原测试证据和冻结哈希继续适用；测试与理想撮合通过不等于实盘可执行或策略已盈利。

逐日逐门槛最终现金见comparison.csv，均满足各所1000USDT加该所已实现毛利减手续费。详细退出原因、最差交易、连续亏损、超深度成交依赖及精确拆分见归因报告。

[完整比较CSV](/Users/mori/code/crypto/state/arb-runs/20261013-btw-activity-gate-experiment-025636Z/comparison.csv) · [盈亏原因与机会筛选](/Users/mori/code/crypto/state/arb-runs/20261013-btw-activity-gate-experiment-025636Z/attribution.md) · [完整明细JSON](/Users/mori/code/crypto/state/arb-runs/20261013-btw-activity-gate-experiment-025636Z/comparison.json) · [冻结选择](/Users/mori/code/crypto/state/arb-runs/20261013-btw-activity-gate-experiment-025636Z/selection-frozen.json) · [独立验收](/Users/mori/code/crypto/state/arb-runs/20261013-btw-activity-gate-experiment-025636Z/independent-review.json) · [活跃度数据与校准验收](/Users/mori/code/crypto/state/arb-runs/20261013-btw-activity-gate-experiment-025636Z/activity-input-acceptance.json)

**逐笔订单与组合账本**

- 2026-08-01：[q25订单](/Users/mori/code/crypto/state/arb-runs/20261013-btw-activity-gate-experiment-025636Z/variants/q25/search/2026-08-01/e30_tp20_sl10_h15/orders.csv) / [q25组合](/Users/mori/code/crypto/state/arb-runs/20261013-btw-activity-gate-experiment-025636Z/variants/q25/search/2026-08-01/e30_tp20_sl10_h15/cycles.csv) · [q50订单](/Users/mori/code/crypto/state/arb-runs/20261013-btw-activity-gate-experiment-025636Z/variants/q50/search/2026-08-01/e30_tp20_sl10_h15/orders.csv) / [q50组合](/Users/mori/code/crypto/state/arb-runs/20261013-btw-activity-gate-experiment-025636Z/variants/q50/search/2026-08-01/e30_tp20_sl10_h15/cycles.csv) · [q75订单](/Users/mori/code/crypto/state/arb-runs/20261013-btw-activity-gate-experiment-025636Z/variants/q75/search/2026-08-01/e30_tp20_sl10_h15/orders.csv) / [q75组合](/Users/mori/code/crypto/state/arb-runs/20261013-btw-activity-gate-experiment-025636Z/variants/q75/search/2026-08-01/e30_tp20_sl10_h15/cycles.csv)
- 2026-09-01：[q25订单](/Users/mori/code/crypto/state/arb-runs/20261013-btw-activity-gate-experiment-025636Z/variants/q25/search/2026-09-01/e30_tp20_sl10_h15/orders.csv) / [q25组合](/Users/mori/code/crypto/state/arb-runs/20261013-btw-activity-gate-experiment-025636Z/variants/q25/search/2026-09-01/e30_tp20_sl10_h15/cycles.csv) · [q50订单](/Users/mori/code/crypto/state/arb-runs/20261013-btw-activity-gate-experiment-025636Z/variants/q50/search/2026-09-01/e30_tp20_sl10_h15/orders.csv) / [q50组合](/Users/mori/code/crypto/state/arb-runs/20261013-btw-activity-gate-experiment-025636Z/variants/q50/search/2026-09-01/e30_tp20_sl10_h15/cycles.csv) · [q75订单](/Users/mori/code/crypto/state/arb-runs/20261013-btw-activity-gate-experiment-025636Z/variants/q75/search/2026-09-01/e30_tp20_sl10_h15/orders.csv) / [q75组合](/Users/mori/code/crypto/state/arb-runs/20261013-btw-activity-gate-experiment-025636Z/variants/q75/search/2026-09-01/e30_tp20_sl10_h15/cycles.csv)
- 2026-10-01：[q25订单](/Users/mori/code/crypto/state/arb-runs/20261013-btw-activity-gate-experiment-025636Z/variants/q25/evaluation/2026-10-01/e30_tp20_sl10_h15/orders.csv) / [q25组合](/Users/mori/code/crypto/state/arb-runs/20261013-btw-activity-gate-experiment-025636Z/variants/q25/evaluation/2026-10-01/e30_tp20_sl10_h15/cycles.csv) · [q50订单](/Users/mori/code/crypto/state/arb-runs/20261013-btw-activity-gate-experiment-025636Z/variants/q50/evaluation/2026-10-01/e30_tp20_sl10_h15/orders.csv) / [q50组合](/Users/mori/code/crypto/state/arb-runs/20261013-btw-activity-gate-experiment-025636Z/variants/q50/evaluation/2026-10-01/e30_tp20_sl10_h15/cycles.csv) · [q75订单](/Users/mori/code/crypto/state/arb-runs/20261013-btw-activity-gate-experiment-025636Z/variants/q75/evaluation/2026-10-01/e30_tp20_sl10_h15/orders.csv) / [q75组合](/Users/mori/code/crypto/state/arb-runs/20261013-btw-activity-gate-experiment-025636Z/variants/q75/evaluation/2026-10-01/e30_tp20_sl10_h15/cycles.csv)

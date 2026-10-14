**已完成 BTWUSDT 的0执行延迟、双腿全额吃单回放与192组参数搜索，并加入每轮必做的盈亏归因。此次找到了验证日表现更好的参数组合，但没有找到跨日稳定盈利的方案。**

撮合按当时已接收的有效对手一档，双腿同时全额开平。开仓仍受当前一档深度100%、每腿250USDT、各所1000USDT虚拟本金、1倍和最小数量限制；平仓按理想假设全额成交，允许超过显示深度。每笔费用0.03%，30秒冷却及合计亏损10USDT停止新增保留。既有maker报价偏移和单腿1秒期限不参与双腿同步吃单模型。

采用完整逐笔盘口与成交活动特征重新产生信号，未复用此前322个固定机会。既有20日波动率前10和q25活跃度门槛保留；因上午00–12用于校准q25，交易从12:00UTC开始，23:58后日终退出。资金费护栏按各参数的持仓期限加60秒动态调整，只读已知公告，不用未来结算结果选择开仓。

| 参数 | 旧参数基线 | 本轮入选 |
|---|---:|---:|
| 原始跨所对手一档开仓价差 | 21bp | **30bp** |
| 扣四笔费用后止盈 | 5bp | **20bp** |
| 相对入场反向净清算值的恶化止损 | 20bp | **10bp** |
| 持仓期限 | 60秒 | **15秒** |
| 绝对净亏硬止损 | 100bp | 100bp |

止盈、普通止损、硬止损均以初始多头单腿名义金额为基数。“10bp止损”是相对入场净清算基线的恶化幅度，不能理解为总损失只到10bp；穿过价格间隙也可能越过触发值。

用8月、9月两日按“较差日期净收益优先、再比较总收益和回撤”选参。192组中144组满足两个日期各至少10组完整交易、无残仓、资金费与账务核验通过。冻结后10月只跑入选参数和旧参数；冻结时间早于10月执行启动，源引擎哈希相同。三个日期此前均已研究过，因此这是时间分开的内部验证，不是全新未见样本。

下表均为理想撮合下扣交易费后的模拟净损益，单位USDT。两套参数使用相同理想撮合模型，不能直接与上一轮maker独立机会结果比较。

| 日期 UTC | 用途 | 旧参数净额 | 入选净额 | 入选组合数 | 入选净胜率 | 入选最大清算回撤 |
|---|---|---:|---:|---:|---:|---:|
| 2026-08-01 | 选参 | -10.032615 | -10.000055 | 80 | 5.0000% | 10.307276 |
| 2026-09-01 | 选参 | -1.135498 | +0.072284 | 17 | 47.0588% | 0.262247 |
| 2026-10-01 | 冻结后验证 | -2.534685 | +2.634201 | 80 | 42.5000% | 1.212660 |

入选参数三日共177组、708笔模拟订单，全部平仓。三日账户各自重置，样本损益之和为 **−7.293570USDT**，旧参数为−13.702798USDT；这不是连续账户收益曲线。三日均核验无实际资金费结算穿越，资金费为0。每个有效盘口计算理想可平仓净值和回撤，无效盘口区间无法给出清算标记。

**盈亏原因**

完整账本恒等式为：`净损益 = 跨所中价基差收敛收益 − 四次吃单的买卖价差成本 − 四笔手续费`。中价仅用于记账分解，不用于撮合。

入选参数三日中价基差收敛收益 **+12.285945U**，买卖价差成本 **11.911535U**，手续费 **7.667980U**，最终 **-7.293570U**。177组中130组基差向有利方向变化，其中84组仍然净亏；支付买卖价差后87组盈利，再支付手续费后只剩46组。核心瓶颈是很多回归幅度不足以覆盖执行成本，即使0延迟、全额成交也存在。

10月旧参数价差毛利4.8914U被7.4261U手续费吞掉；入选参数毛利5.7400U、手续费3.1058U，交易由189组降至80组。入场、止盈止损与持仓期限同时变化，不能将改善单独归因于某个参数。8月入选参数中价基差收敛收益仍为正1.6092U，但支付买卖价差7.3291U和手续费4.2802U后触发10U停损；该日最长连续净亏27笔。

入选参数32次止盈合计+6.4137U；95次超时退出合计−4.5184U；49次普通止损和1次硬止损合计−9.1889U。另有14笔非止盈退出也盈利，止盈次数不等于盈利次数。仅凭实际开平记录不能判断超时后会不会回归或错失多少止盈。

77/177组（43.50%）的平仓使用了超过当时一档显示深度的理想假设，该组净额−5.8167U。这是分组描述，不是恢复真实深度后的损益估计。所有模拟订单均明确记录超深度标志。

**下一轮优先验证的方向**

1. **限制入场执行成本**：增加 `max_entry_liquidation_cost_bps`，可先预设20/30/40bp少量候选，按当时四个盘口和四笔费用计算。另检查相对过去基差中心的可捕获空间是否覆盖预计成本；只能使用开仓前数据。阈值尚未验证，不能把30bp分箱当作最优值。
2. **按正反向深度共同限制仓位**：增加 `min_exit_depth_coverage` 或按入场与反向平仓一档深度共同缩量，先比较50%/100%深度预算，再恢复延迟与部分成交检验。入场可见深度不能保证未来退出深度。
3. **增加连亏暂停条件**：预设连续3/5笔净亏后暂停60/180秒，并设置恢复条件；同时统计被暂停的后续盈利机会。保留10U上限，避免把“最后一笔少超出停损线”当成策略改进。
4. **研究按回归进展退出**：补充持有期间的有利/不利净清算值，比较在5/10秒检查是否出现预设改善，再决定继续持有或提前退出。先保持硬止损；当前数据不能证明延长或缩短时间单独更好。

这些是下一轮预先登记的研究方向，本轮没有据10月结果回改参数。8月候选均亏损，入选参数与基线都撞到10U保护线，排名微差受最后一笔越过上限的金额影响；三日样本还不足以确认参数稳定性。

入选参数各日最终虚拟现金如下，逐所均核对 `1000 + 该所已实现毛利 − 该所交易费`：

| 日期 UTC | Bybit USDT | Bitget USDT |
|---|---:|---:|
| 2026-08-01 | 995.6040742000 | 994.3958707380 |
| 2026-09-01 | 1000.1819714100 | 999.8903125830 |
| 2026-10-01 | 1004.020537250 | 998.613663790 |

**验收与文件**

386份候选日结果轻审通过；入选与基线三日517组、2068笔订单的原始一档来源、最早允许开平时点、资金费门槛、活动度、数量、费用、现金和逐笔清算回撤独立重算通过。原始行情沿用此前验收及哈希绑定，本轮没有下载新数据、读取私密凭据或发送真实订单。

[盈亏分组与最差交易](/Users/mori/code/crypto/state/arb-runs/20261013-btw-perfect-taker-optimization-021035Z/attribution.md) · [中价回归/买卖价差/手续费精确分解](/Users/mori/code/crypto/state/arb-runs/20261013-btw-perfect-taker-optimization-021035Z/microstructure-attribution.md) · [全部192组训练指标](/Users/mori/code/crypto/state/arb-runs/20261013-btw-perfect-taker-optimization-021035Z/grid-scores.json) · [符合条件的参数排名](/Users/mori/code/crypto/state/arb-runs/20261013-btw-perfect-taker-optimization-021035Z/ranked-fit.csv) · [冻结选择](/Users/mori/code/crypto/state/arb-runs/20261013-btw-perfect-taker-optimization-021035Z/selection-frozen.json) · [六组账户对比CSV](/Users/mori/code/crypto/state/arb-runs/20261013-btw-perfect-taker-optimization-021035Z/comparison.csv) · [输入验收](/Users/mori/code/crypto/state/arb-runs/20261013-btw-perfect-taker-optimization-021035Z/input-acceptance.json) · [复现与后续回放规范](/Users/mori/code/crypto/state/arb-runs/20261013-btw-perfect-taker-code/README.md)

- 2026-08-01：[入选参数成交](/Users/mori/code/crypto/state/arb-runs/20261013-btw-perfect-taker-optimization-021035Z/search/2026-08-01/e30_tp20_sl10_h15/orders.csv) · [入选参数组合](/Users/mori/code/crypto/state/arb-runs/20261013-btw-perfect-taker-optimization-021035Z/search/2026-08-01/e30_tp20_sl10_h15/cycles.csv) · [账户事件](/Users/mori/code/crypto/state/arb-runs/20261013-btw-perfect-taker-optimization-021035Z/search/2026-08-01/e30_tp20_sl10_h15/equity.csv) · [旧参数组合](/Users/mori/code/crypto/state/arb-runs/20261013-btw-perfect-taker-optimization-021035Z/search/2026-08-01/e21_tp5_sl20_h60/cycles.csv) · [逐笔深审](/Users/mori/code/crypto/state/arb-runs/20261013-btw-perfect-taker-optimization-021035Z/search/2026-08-01/perfect-deep-audit.json)
- 2026-09-01：[入选参数成交](/Users/mori/code/crypto/state/arb-runs/20261013-btw-perfect-taker-optimization-021035Z/search/2026-09-01/e30_tp20_sl10_h15/orders.csv) · [入选参数组合](/Users/mori/code/crypto/state/arb-runs/20261013-btw-perfect-taker-optimization-021035Z/search/2026-09-01/e30_tp20_sl10_h15/cycles.csv) · [账户事件](/Users/mori/code/crypto/state/arb-runs/20261013-btw-perfect-taker-optimization-021035Z/search/2026-09-01/e30_tp20_sl10_h15/equity.csv) · [旧参数组合](/Users/mori/code/crypto/state/arb-runs/20261013-btw-perfect-taker-optimization-021035Z/search/2026-09-01/e21_tp5_sl20_h60/cycles.csv) · [逐笔深审](/Users/mori/code/crypto/state/arb-runs/20261013-btw-perfect-taker-optimization-021035Z/search/2026-09-01/perfect-deep-audit.json)
- 2026-10-01：[入选参数成交](/Users/mori/code/crypto/state/arb-runs/20261013-btw-perfect-taker-optimization-021035Z/evaluation/2026-10-01/e30_tp20_sl10_h15/orders.csv) · [入选参数组合](/Users/mori/code/crypto/state/arb-runs/20261013-btw-perfect-taker-optimization-021035Z/evaluation/2026-10-01/e30_tp20_sl10_h15/cycles.csv) · [账户事件](/Users/mori/code/crypto/state/arb-runs/20261013-btw-perfect-taker-optimization-021035Z/evaluation/2026-10-01/e30_tp20_sl10_h15/equity.csv) · [旧参数组合](/Users/mori/code/crypto/state/arb-runs/20261013-btw-perfect-taker-optimization-021035Z/evaluation/2026-10-01/e21_tp5_sl20_h60/cycles.csv) · [逐笔深审](/Users/mori/code/crypto/state/arb-runs/20261013-btw-perfect-taker-optimization-021035Z/evaluation/2026-10-01/perfect-deep-audit.json)

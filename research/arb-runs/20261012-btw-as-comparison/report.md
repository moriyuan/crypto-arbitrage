**AS 报价已加入离线逐笔回放。当前配置覆盖率低，尚未证实能改善这条跨所策略。**

对比日期为 2026-08-01、09-01、10-01（UTC），共 322 个基线开仓机会、966 个独立情景。三天分别用 00:00–12:00 校准、12:00 后按原风险预算形成共同机会，比较固定 1 tick、固定 5 tick、AS 三种报价。三天行情包含 1,685,475 笔实时逐笔成交；Bitget 连接首包历史成交另计，未用作当时新增成交。

这里验证第一腿挂价、部分成交和另一所对冲执行，**不是完整 AS 开平仓收益回测**。所有机会以零初始未对冲库存独立运行；没有把遗留库存传入下一机会，不能声称已验证 AS 连续库存控制。

| 日期 UTC | 报价 | 机会数 | Maker 成交 / 完全对冲 | 配对 / 未对冲 BTW | 配对入场成本优势 bp |
|---|---|---:|---:|---:|---:|
| 2026-08-01 | 固定 1 tick | 191 | 48 / 25 | 4560 / 481 | -13.905 |
| 2026-08-01 | 固定 5 tick | 191 | 51 / 26 | 4580 / 508 | -13.701 |
| 2026-08-01 | AS 价差内报价 | 191 | 0 / 0 | 0 / 0 | 无配对成交 |
| 2026-09-01 | 固定 1 tick | 26 | 5 / 1 | 30 / 41 | -10.879 |
| 2026-09-01 | 固定 5 tick | 26 | 5 / 1 | 30 / 41 | -10.782 |
| 2026-09-01 | AS 价差内报价 | 26 | 0 / 0 | 0 / 0 | 无配对成交 |
| 2026-10-01 | 固定 1 tick | 105 | 25 / 19 | 290 / 18 | -24.477 |
| 2026-10-01 | 固定 5 tick | 105 | 29 / 21 | 310 / 30 | -23.084 |
| 2026-10-01 | AS 价差内报价 | 105 | 10 / 6 | 70 / 11 | -56.077 |

“完全对冲”指该机会已经成交的 maker 数量全部对冲，不代表原定目标仓位全额成交。“未对冲 BTW”是各独立机会残量之和，不是一个真实账户的合并持仓。

表中成本优势的比较基准，是信号时两所对手一档的双腿吃单报价，已经包括配对开仓部分的手续费差。分母为实际配对币数乘信号时买入腿价格，按**单腿名义金额**换算；负值表示比该报价基准更贵。它不含后续平仓、未对冲头寸盈亏和完整资金费，不是收益率。各方案的成交子集不同，10 月三者共同完成配对的机会仅 3 个；这些条件均值不能证明全策略优劣，也不是与延迟完全相同的双腿 taker 执行作因果比较。

8 月 AS 的 191 个机会中，121 个原始报价不在买卖一之间，70 个缺少连续有效的波动率样本，均在报价前拒绝。9 月 26 个机会全部因波动率窗口不满足而拒绝。这两天不能用于评估 AS 的实际成交价格质量，更不能把零成交视为零风险收益。

10 月 105 个机会中，AS 有 44 个波动率窗口拒绝、42 个价差内报价拒绝、1 个到达时 PostOnly 拒绝，18 个成功到达盘口。10 个机会发生 maker 成交，6 个将已成交部分完全对冲。已配对 70 BTW 的挂价节省 0.1603 USDT，但对冲价格多花 0.6914 USDT，费用差另为 0.00007527 USDT，因此入场成本增加 0.53117527 USDT。另有 11 BTW 未对冲，按各自成交价计名义金额 14.84185 USDT，其已付开仓费 0.004452555 USDT 不包含在配对成本优势中。

**本轮参数与交易规则**

AS 采用 [Avellaneda–Stoikov 原论文的一阶有限时域报价](https://math.nyu.edu/inmemoriam/avellaneda/HighFrequencyTrading.pdf)。报价单位为 10 BTW，库存为实际未对冲币数除以 10，参考价为本所中间价；`r=S−qγσ²τ`，半价差为 `γσ²τ/2+log(1+γ/k)/γ`。风险参数固定 γ=0.004 USDT⁻¹、τ=2 秒，未按回放收益搜参。A/k 每日前半天独立校准；三日两所共六组均通过既定拟合门槛。公开成交触达强度是排队未验证的代理，并不等于真实成交概率。

波动率来自最近 60 个连续一秒价格增量，要求 61 个有效盘口样本；失效、缺样本或连接分段变化即重置。买价向下、卖价向上按本所价格步长取整；原始或取整后价格不在买卖一之间即拒绝，不强制截入区间。固定报价为买入 ask1−d、卖出 bid1+d，d 使用 maker 本所的 1 或 5 tick。

历史筛选仍使用 420 个共同加密币候选的过去 20 个完整日平均波动率前 10，取消成交额硬门槛；BTW 在 8、9、10 月分别排第 9、6、3。活动度采用各日前半天整秒样本的 q25，最近成交不超过 5 秒。共同机会基线两所各 1000 USDT、单腿不超过 250 USDT，双腿相同币数；每笔手续费 0.03%。只使用可见一档和未消耗的 25% 深度，maker 成交需普通正确方向逐笔严格穿价且受 25% 成交参与率约束，对冲至少延迟 100ms；取消也有 100ms 时延，2 秒 TTL，超过 5 秒未对冲的残量明确保留。

**时间与样本限制**

8 月的共同机会基线在 16:51:30.283033400 UTC 触发原有累计亏损 10 USDT 上限，最后闭合使基线净损益达到 −10.0234711010 USDT，之后不再新增机会。这是用于固定机会池的 taker 基线，不是 AS 收益。因此 8 月不是完整后半天任意报价测试。9 月和 10 月没有触发该亏损上限。详见各日 `cohort-noon/cohort-truncation-evidence.json`。

7 月 1 日也下载并验收了完整原始行情，但 Bitget 公开已结算资金费接口仅返回到 7 月 14 日，缺少该日结算前后记录，未构造交易机会池。没有用假定资金费或虚构订单补足日期。

历史价格精度以原始盘口为准：8、9 月使用 1e-6 价格和 1e-10 USDT 金额单位；10 月保留旧 1e-5/1e-9 精度。Bybit/Bitget 的历史报价步长采用 1e-5/1e-6 情景假设（10 月均 1e-5），不是已核验的当日官方完整规则。8 月仅精确忽略首 snapshot 前一条已证明重复的 delta，随后重建并预热；旧严格失败记录保留。当前币种目录有幸存者偏差，BTW 也是回顾性选择；这三天不连续，不能当作向前样本外策略证明。

**验收与结果文件**

三日共 3,053,327 个训练窗口的命中、删失、曝光时长和 A/k 回归独立重算通过。41 项 AS 聚焦测试和流水线边界测试通过；独立核验 264 笔 maker 成交记录、152 笔 hedge 成交记录及 1,572 笔机会基线开平单的深度预算，涵盖费用、数量、来源、时序和两套整数精度。207 项汇总数值及哈希核对通过。测试通过不代表排队假设真实或策略盈利。

- [汇总 CSV](/Users/mori/code/crypto/state/arb-runs/20261012-btw-as-comparison/comparison.csv) · [机器可读结果](/Users/mori/code/crypto/state/arb-runs/20261012-btw-as-comparison/comparison.json) · [指标独立审计](/Users/mori/code/crypto/state/arb-runs/20261012-btw-as-comparison/comparison-independent-audit.json)
- [流水线验收](/Users/mori/code/crypto/state/arb-runs/20261012-btw-as-comparison/pipeline-acceptance.json) · [原始数据验收](/Users/mori/code/crypto/state/arb-runs/20261012-btw-as-comparison/multi-day-raw-acceptance.json) · [独立源码审查](/Users/mori/code/crypto/state/arb-runs/20261012-btw-as-comparison/as-independent-source-review.json)
- [冻结方案](/Users/mori/code/crypto/state/arb-runs/20261012-btw-as-comparison/protocol.json) · [中午开仓因果修正](/Users/mori/code/crypto/state/arb-runs/20261012-btw-as-comparison/protocol-causal-cohort-amendment.json) · [代码与用法](/Users/mori/code/crypto/state/arb-runs/20261012-btw-as-code/README.md)

2026-08-01：[逐机会报价](/Users/mori/code/crypto/state/arb-runs/20261012-btw-as-comparison/days/2026-08-01/replay-causal-noon/quotes.jsonl) · [逐事件记录](/Users/mori/code/crypto/state/arb-runs/20261012-btw-as-comparison/days/2026-08-01/replay-causal-noon/events.jsonl) · [独立成交审计](/Users/mori/code/crypto/state/arb-runs/20261012-btw-as-comparison/days/2026-08-01/replay-causal-noon-independent-audit.json) · [校准](/Users/mori/code/crypto/state/arb-runs/20261012-btw-as-comparison/days/2026-08-01/calibration/calibration.json) · [风险截断证据](/Users/mori/code/crypto/state/arb-runs/20261012-btw-as-comparison/days/2026-08-01/cohort-noon/cohort-truncation-evidence.json)

2026-09-01：[逐机会报价](/Users/mori/code/crypto/state/arb-runs/20261012-btw-as-comparison/days/2026-09-01/replay-causal-noon/quotes.jsonl) · [逐事件记录](/Users/mori/code/crypto/state/arb-runs/20261012-btw-as-comparison/days/2026-09-01/replay-causal-noon/events.jsonl) · [独立成交审计](/Users/mori/code/crypto/state/arb-runs/20261012-btw-as-comparison/days/2026-09-01/replay-causal-noon-independent-audit.json) · [校准](/Users/mori/code/crypto/state/arb-runs/20261012-btw-as-comparison/days/2026-09-01/calibration/calibration.json) · [风险截断证据](/Users/mori/code/crypto/state/arb-runs/20261012-btw-as-comparison/days/2026-09-01/cohort-noon/cohort-truncation-evidence.json)

2026-10-01：[逐机会报价](/Users/mori/code/crypto/state/arb-runs/20261012-btw-as-comparison/days/2026-10-01/replay-causal-noon/quotes.jsonl) · [逐事件记录](/Users/mori/code/crypto/state/arb-runs/20261012-btw-as-comparison/days/2026-10-01/replay-causal-noon/events.jsonl) · [独立成交审计](/Users/mori/code/crypto/state/arb-runs/20261012-btw-as-comparison/days/2026-10-01/replay-causal-noon-independent-audit.json) · [校准](/Users/mori/code/crypto/state/arb-runs/20261012-btw-as-comparison/days/2026-10-01/calibration/calibration.json) · [风险截断证据](/Users/mori/code/crypto/state/arb-runs/20261012-btw-as-comparison/days/2026-10-01/cohort-noon/cohort-truncation-evidence.json)

最终比较只使用 `replay-causal-noon`。旧 10 月 `replay` 保留为兼容诊断，未混入表格。没有使用私密 API、发送真实订单或计算虚构的最终 AS 账户余额。

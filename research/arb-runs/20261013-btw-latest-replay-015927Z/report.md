**已按最新参数重新完成 BTWUSDT 三个交易日的逐笔回放。** 新结果单独保存在本目录；不是复用旧结果文件。三个交易日的新组合与事件账本均与上一轮最终版本逐字节一致。

本次参数保持：未对冲单腿从每批成交接收时起计时 **1秒**；配对仓位从首次对冲成交起计时 **60秒**，后续补成交不延长。挂单买价为本所 ask1−1tick、卖价为 bid1+1tick，必须位于价差内部；成交后另一所按对手一档对冲。每笔手续费 **0.03%**，不开启 AS 报价。

配对提前退出保留：扣开平手续费后止盈 **5bp**、相对初始反向一档净值恶化 **20bp** 止损、**100bp** 硬止损，基数为实际配对买入腿名义金额。原有活动度筛选、报价2秒有效期、100ms执行/撤单延迟、25%可用深度、单腿最多250USDT及数量步长均未改动。到期触发退出请求，实际成交仍受盘口与本轮5USDT最小金额假设约束。

| 日期 UTC | 候选机会 | 有开仓成交 | 完全平仓 | 残仓机会 | 成交记录 | 完全平仓机会损益合计 USDT* |
|---|---:|---:|---:|---:|---:|---:|
| 2026-08-01 | 191 | 48 | 29 | 19 | 222 | -1.759832 |
| 2026-09-01 | 26 | 5 | 3 | 2 | 15 | -0.367673 |
| 2026-10-01 | 105 | 25 | 19 | 6 | 110 | +1.023275 |

合计 322 个独立机会，78 个发生开仓成交，51 个完全平仓，27 个仍有单腿残仓；共 347 笔模拟成交记录，不能等同于订单数。所有配对仓位最终平掉。配对退出触发原因：时间到期 34 个、普通止损 16 个、硬止损 2 个、止盈 3 个；这些机会还可能另有未配对余量。

\* 表中金额仅为**完全平仓机会扣交易手续费、未计资金费**的条件合计。残仓机会不计入该列，也没有把残仓按零收益处理。本实验按独立机会回放，各机会不共享现金、仓位和盘口容量，可能重叠，因此该列不是连续账户收益，无法由此给出最终账户余额或策略收益率。

三个日期分别仍有单腿余量 **316 / 13 / 18 BTW**，配对余量均为0；这是独立机会的数量合计，不是一个账户的净持仓。三日资金费核验均为 `UNRESOLVED_SETTLEMENT_MARK_PRICE_MISSING`：残仓有跨结算，9月还有已平单腿跨过结算，缺少历史结算 mark price，不能称最终净收益。

9月 cycle 2 的12 BTW原始部分成交金额低于当前5USDT假设，虽按1秒期限请求退出，实际直到约4小时11分钟后才平掉。因此，这两个期限不保证实际暴露只持续1秒或60秒；交易所真实 reduce-only 对小额平仓的规则尚不能由本仿真假设证明。配对平仓仍假设两所满足可见深度时同步同量成交，没有模拟两所独立拒单和真实排队。

行情仍为此前三个日期的公开历史档案；开仓只使用原先中午以后的冻结机会池，8月原池还受旧 taker 参考策略累计亏损上限截断，没有重新搜索更晚的新机会。因此本轮是固定机会池的执行与退出回放。

| 日期 UTC | 读取盘口事件 | 读取逐笔成交 | 回放计算耗时 秒 |
|---|---:|---:|---:|
| 2026-08-01 | 4,189,048 | 727,945 | 143.886 |
| 2026-09-01 | 1,858,653 | 189,327 | 5.632 |
| 2026-10-01 | 3,160,784 | 768,203 | 32.419 |

三日独立验收均通过：重新核对原始成交来源、费用、FIFO库存、退出计时、深度预算和分批退出数量；CSV与JSON的 12,687 个字段核对通过。代码及测试文件哈希与上次37项测试通过的版本一致，本次没有重复运行未变化的测试套件。所有回放进程正常结束，无真实订单。

[统计数据](/Users/mori/code/crypto/state/arb-runs/20261013-btw-latest-replay-015927Z/statistics.json) · [参数与启动记录](/Users/mori/code/crypto/state/arb-runs/20261013-btw-latest-replay-015927Z/launch.json) · [新旧账本一致性核验](/Users/mori/code/crypto/state/arb-runs/20261013-btw-latest-replay-015927Z/reproducibility.json) · [导出验收](/Users/mori/code/crypto/state/arb-runs/20261013-btw-latest-replay-015927Z/export-audit.json)

- 2026-08-01：[组合明细](/Users/mori/code/crypto/state/arb-runs/20261013-btw-latest-replay-015927Z/validated-days/2026-08-01/cycles.csv) · [逐笔模拟成交](/Users/mori/code/crypto/state/arb-runs/20261013-btw-latest-replay-015927Z/validated-days/2026-08-01/fills.csv) · [事件账本](/Users/mori/code/crypto/state/arb-runs/20261013-btw-latest-replay-015927Z/validated-days/2026-08-01/events.jsonl) · [独立验收](/Users/mori/code/crypto/state/arb-runs/20261013-btw-latest-replay-015927Z/validated-days/2026-08-01/independent-audit.json) · [资金费核验](/Users/mori/code/crypto/state/arb-runs/20261013-btw-latest-replay-015927Z/validated-days/2026-08-01/funding-interval-audit.json)
- 2026-09-01：[组合明细](/Users/mori/code/crypto/state/arb-runs/20261013-btw-latest-replay-015927Z/validated-days/2026-09-01/cycles.csv) · [逐笔模拟成交](/Users/mori/code/crypto/state/arb-runs/20261013-btw-latest-replay-015927Z/validated-days/2026-09-01/fills.csv) · [事件账本](/Users/mori/code/crypto/state/arb-runs/20261013-btw-latest-replay-015927Z/validated-days/2026-09-01/events.jsonl) · [独立验收](/Users/mori/code/crypto/state/arb-runs/20261013-btw-latest-replay-015927Z/validated-days/2026-09-01/independent-audit.json) · [资金费核验](/Users/mori/code/crypto/state/arb-runs/20261013-btw-latest-replay-015927Z/validated-days/2026-09-01/funding-interval-audit.json)
- 2026-10-01：[组合明细](/Users/mori/code/crypto/state/arb-runs/20261013-btw-latest-replay-015927Z/validated-days/2026-10-01/cycles.csv) · [逐笔模拟成交](/Users/mori/code/crypto/state/arb-runs/20261013-btw-latest-replay-015927Z/validated-days/2026-10-01/fills.csv) · [事件账本](/Users/mori/code/crypto/state/arb-runs/20261013-btw-latest-replay-015927Z/validated-days/2026-10-01/events.jsonl) · [独立验收](/Users/mori/code/crypto/state/arb-runs/20261013-btw-latest-replay-015927Z/validated-days/2026-10-01/independent-audit.json) · [资金费核验](/Users/mori/code/crypto/state/arb-runs/20261013-btw-latest-replay-015927Z/validated-days/2026-10-01/funding-interval-audit.json)

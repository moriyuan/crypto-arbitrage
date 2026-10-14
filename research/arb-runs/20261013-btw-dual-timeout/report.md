**已在本地回放模块实现两种独立、可调整的退出触发时间，不使用 AS 报价。**

| 持仓状态 | 默认期限 | 起点和动作 |
|---|---:|---|
| 单腿尚未对冲 | 1 秒 | 每批 maker 成交被本地接收后计时；到期撤剩余挂单、停止新增对冲，请求在原交易所按反向对手一档退出。 |
| 已形成双腿配对 | 60 秒 | 从首次 hedge 成交起计时，后续补成交不延长；到期请求同量关闭剩余双腿。 |

CLI 参数分别为 `--unhedged-timeout-ms 1000` 和 `--hedged-hold-ms 60000`。它们是初始固定研究参数，尚未优化，不是 AS 或价差回归模型估计出的最佳持仓周期。报价保持固定 1 个本所 tick 距离：买入 ask1−1tick、卖出 bid1+1tick，必须严格位于本所价差内。

配对部分保留既有提前退出条件：手续费后止盈 5bp、相对实际配对形成时反向一档净值恶化 20bp 止损、100bp 硬止损，基数为实际配对买入腿名义金额。资金费护栏和日终也可提前请求退出。每笔成交手续费保持 0.03%。

**期限控制的是请求时间，不保证成交时间。** 退出至少等待 100ms，再按有效盘口、尚未消耗的 25% 一档深度、原生数量步长与 5 USDT 最小名义金额执行。配对退出假设两所可见深度中的同量同步成交，可分批。活动度门槛只限制新挂单，不阻止风险退出。部分退出同时检查本笔及剩余数量合法性，避免主动制造小额尾仓；原始 maker 部分成交已经产生的小额仓位仍可能无法平掉。

**三日逐笔验证**

复用此前冻结的 2026-08-01、09-01、10-01（UTC）公开行情与共同开仓机会池，共 322 个独立机会、347 笔模拟成交。37 项聚焦测试通过，三日逐笔来源、深度预算、费用、库存、计时和分批退出独立验收均通过。各日只使用中午以后原机会池，8 月仍受原 taker 基线累计亏损上限截断，没有重新搜参或扩展机会池。

| 日期 UTC | 候选机会 | 发生 maker 成交 | 已完全平仓 | 仍有残仓的机会 | 单腿剩余 BTW | 配对剩余 BTW |
|---|---:|---:|---:|---:|---:|---:|
| 2026-08-01 | 191 | 48 | 29 | 19 | 316 | 0 |
| 2026-09-01 | 26 | 5 | 3 | 2 | 13 | 0 |
| 2026-10-01 | 105 | 25 | 19 | 6 | 18 | 0 |

全部已配对仓位最终退出；残仓来自原始小额单腿成交。表中剩余量为独立机会的合计，不是一个真实账户的净持仓。各机会没有共享现金或深度，不能将条件损益之和当作连续账户收益。

9 月 cycle 2 说明实际退出可能显著晚于时限：Bitget 分批成交合计 12 BTW，初始名义金额约 4.69 USDT；按时请求退出后，因当前最小金额假设，到 17:27:06 UTC 才成交，距离 13:16:25 UTC 开仓约 4 小时 11 分钟，跨过 16:00 的资金费结算。该规则通过不代表风险暴露被限制在 1 秒内。9 月 cycle 8 的配对小额尾仓则已通过分批数量修复消除。

三日剩余库存均有实际资金费跨期，9 月另有上述已平单腿跨期；历史结算 mark price 缺失，精确资金费未核验。最终资金费状态均为 `UNRESOLVED_SETTLEMENT_MARK_PRICE_MISSING`。`net_before_funding` 只反映已实现价格损益与已付交易费，不是最终净收益；未平部分没有按零收益处理。运行时 `summary.json` 中的待审计状态由独立资金费验收文件补充，原摘要不覆盖。

**结果与复现**

- [参数与运行说明](/Users/mori/code/crypto/state/arb-runs/20261013-btw-dual-timeout-code/README.md) · [实现](/Users/mori/code/crypto/state/arb-runs/20261013-btw-dual-timeout-code/lifecycle_engine.py:19) · [配置决策](/Users/mori/code/crypto/state/arb-runs/20261013-btw-dual-timeout/decision.json)
- [统计数据](/Users/mori/code/crypto/state/arb-runs/20261013-btw-dual-timeout/statistics.json) · [最终测试证据](/Users/mori/code/crypto/state/arb-runs/20261013-btw-dual-timeout/verification-final.json) · [三日独立验收](/Users/mori/code/crypto/state/arb-runs/20261013-btw-dual-timeout/independent-acceptance.json) · [12,687 项导出字段核对](/Users/mori/code/crypto/state/arb-runs/20261013-btw-dual-timeout/export-audit.json)
- 2026-08-01：[组合](/Users/mori/code/crypto/state/arb-runs/20261013-btw-dual-timeout/validated-days/2026-08-01/cycles.csv) · [模拟成交](/Users/mori/code/crypto/state/arb-runs/20261013-btw-dual-timeout/validated-days/2026-08-01/fills.csv) · [事件账本](/Users/mori/code/crypto/state/arb-runs/20261013-btw-dual-timeout/validated-days/2026-08-01/events.jsonl) · [独立验收](/Users/mori/code/crypto/state/arb-runs/20261013-btw-dual-timeout/validated-days/2026-08-01/independent-audit.json) · [资金费区间核验](/Users/mori/code/crypto/state/arb-runs/20261013-btw-dual-timeout/validated-days/2026-08-01/funding-interval-audit.json)
- 2026-09-01：[组合](/Users/mori/code/crypto/state/arb-runs/20261013-btw-dual-timeout/validated-days/2026-09-01/cycles.csv) · [模拟成交](/Users/mori/code/crypto/state/arb-runs/20261013-btw-dual-timeout/validated-days/2026-09-01/fills.csv) · [事件账本](/Users/mori/code/crypto/state/arb-runs/20261013-btw-dual-timeout/validated-days/2026-09-01/events.jsonl) · [独立验收](/Users/mori/code/crypto/state/arb-runs/20261013-btw-dual-timeout/validated-days/2026-09-01/independent-audit.json) · [资金费区间核验](/Users/mori/code/crypto/state/arb-runs/20261013-btw-dual-timeout/validated-days/2026-09-01/funding-interval-audit.json)
- 2026-10-01：[组合](/Users/mori/code/crypto/state/arb-runs/20261013-btw-dual-timeout/validated-days/2026-10-01/cycles.csv) · [模拟成交](/Users/mori/code/crypto/state/arb-runs/20261013-btw-dual-timeout/validated-days/2026-10-01/fills.csv) · [事件账本](/Users/mori/code/crypto/state/arb-runs/20261013-btw-dual-timeout/validated-days/2026-10-01/events.jsonl) · [独立验收](/Users/mori/code/crypto/state/arb-runs/20261013-btw-dual-timeout/validated-days/2026-10-01/independent-audit.json) · [资金费区间核验](/Users/mori/code/crypto/state/arb-runs/20261013-btw-dual-timeout/validated-days/2026-10-01/funding-interval-audit.json)

最终结果仅使用 `validated-days`。初版 `days`、`initial-code`、`verification.json` 和 [问题诊断](/Users/mori/code/crypto/state/arb-runs/20261013-btw-dual-timeout/initial-dust-diagnostic.json) 保留：初版 8 月 9 个、9 月 1 个配对退出组会主动制造低于最小金额的余量，修复后的 5 项回归测试由失败转为通过，三日重新回放。旧 AS 实验与其他项目代码均未修改，没有读取凭据或发送真实订单。

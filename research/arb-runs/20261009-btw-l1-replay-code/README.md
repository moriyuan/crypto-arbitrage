# BTW 对手一档逐笔回放

本次使用与上一版相同的 2026-10-01 UTC Bybit / Bitget BTW 原始逐笔与增量盘口、20 日历史 Top10 准入、资金费公告及结算证据。只做离线模拟，不读密钥、不连接交易接口。

买入报价及成交价均为卖一，卖出报价及成交价均为买一。开仓、分批平仓、止盈止损估计、账户平仓净值都只使用该一档；没有足够一档量就缩量或等待，不成交到第二档。每档显示量的 25% 构成可消耗预算，仍观察完整 top50 保留档位变化后的已耗预算。显示量没有增加不补预算。

相对旧版，除了限制一档外，将 `price_guard_bps` 从 1 改为 0，以保证开平限价严格等于对手一档。每笔手续费 3bp，净门槛 5bp，退出价差估计 2bp，资金费缓冲 2bp，因此买一/卖一价差需达到 21bp。继承的入场数量安全上界仍含 10bp 加一个 tick 的余量；该余量不会外扩报价或允许越档成交。两所各 1000 USDT，开仓每腿最多 250 USDT、1 倍、同 BTW 币数。其余退出及资金费保护规则保持不变。

`runner.py` 的 `FirstLevelPaper` 是原 `BatchPaper/PaperSession` 的小范围扩展，原执行源码及旧结果保持原样。启动时 `run_metadata.json` 固定所有输入和执行源码哈希；回放期间不修改它们。`verification.json` 和 `test-results.xml` 记录 32 项适配器、执行及边界测试。独立审阅未发现阻断问题。

结果目录为 `/Users/mori/code/crypto/state/arb-runs/20261009-btw-l1-replay-20261001`。完成后使用 `audit_accounting.py` 对账，`depth_fill_audit.py` 独立重建原始盘口核验每筆报价、成交和预算；两项通过后 `make_report.py` 生成报告与旧版比较。`signals.csv.gz` 和 `equity.csv.gz` 保存全量记录，`orders.csv` 与 `cycles.csv` 保存全部订单和组合。

复现入口（输出必须是新的不存在目录）：

```sh
PYTHONPATH=/Users/mori/.codex/worktrees/crypto-arbitrage-research/crypto/src \
  /Users/mori/code/crypto/.venv/bin/python runner.py --output <new-output-directory>
```

`--benchmark-batches` 仅用于运行入口/速度检查，不能作为交易结果。当前仍假设双腿同步成交，没有模拟真实网络延迟、单腿失败或基于标记价格的保证金强平；历史合约目录回溯、数量规则情景假设等限制与旧版相同。

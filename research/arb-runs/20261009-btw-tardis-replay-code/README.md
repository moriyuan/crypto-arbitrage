# BTW 原生 Tardis 流式适配器

`tardis_adapter.py` 只读输入，不建仓、不平仓，也不替代历史前十准入、资金费或盘口验证。原始审计的 `REVIEW` 保留；`preflight()` 检查原档完整性通过及已记录的断线边界，不把它改成无断线档案。

```python
from tardis_adapter import preflight, iter_batches, assert_not_future

archive = preflight("/Users/mori/code/crypto/state/arb-runs/20261009-btw-tardis-20261001")
for batch in iter_batches(archive):
    # batch.events 保留全部原帧，用于逐笔/历史首包/断线等统计。
    # 盘口只消费 batch.reconstruction_events，完成整批后才允许一次决策。
    for event in batch.reconstruction_events:
        # event.venue: bybit / bitget；event.exchange 保留原始数据源名称。
        # event.kind: book / trade / historical_trade / control / disconnect。
        # event.native 为未改写的交易所 JSON，raw_line 保留原始字节。
        # event.path/offset/line_number/source_sequence 提供来源。
        pass  # 调用方负责断线失效、原生 snapshot/delta 重建和交易逻辑。
```

`ordering_ns`、`local_ns` 使用整数纳秒，原始 7 位小数接收时间没有截成微秒或毫秒。空行断线的 `local_ns` / `local_timestamp` 为 `None`；只有排序游标使用前一条真实接收时间，并由 `ordering_basis` 标记。无先前时间的首行空断线会拒绝，避免制造接收时间。

同排序游标中，断线优先处理，并从 `reconstruction_events` 排除该所原本在断线之前的帧，防止旧 snapshot 将失效盘口重新激活。这些帧仍在 `events`。断线之后同接收时刻的新 snapshot 可以恢复。末尾断线后不可拿旧盘口强平。

`BookValidity` 提供可选的连接有效状态，不验证价格/数量/序列。调用方仍需执行原生盘口校验，以及行情年龄、跨所时间偏差、深度容量限制。`assert_not_future(quote_local_ns, decision_ns, source_timestamp_ms=...)` 可保护接收和交易所时间均不领先决策时钟。Tardis 的接收时间不代表用户下单延迟。

Bitget `publicTrade snapshot` 分类为 `historical_trade`；消息内部的源成交时间顺序不重排。盘口以外的逐笔也保留原始消费顺序。

验证命令：`PYTHONDONTWRITEBYTECODE=1 /Users/mori/code/crypto/.venv/bin/python -m unittest -v test_tardis_adapter.py`。13 项单元测试覆盖 100ns 顺序、同刻合批、断线/重连/日末失效、原始成交顺序、长间隔不补价、错误 SHA/缺分片、未来报价。`stream-acceptance.json` 记录完整 288 片的无交易流式验收；原始事件总数与独立审计一致。

## 2026-10-01 全天离线回放

`runner.py` 将该原生流接入研究分支已有的 PaperSession，使用已结束20日的前十波动率准入、3bp手续费和逐档深度参与预算。历史资金费公告只在记录的接收时刻之后可用。运行器不读取私钥，也不发送订单；两腿同步成交仍是仿真假设。

已完成结果及每组损益见 [report.md](/Users/mori/code/crypto/state/arb-runs/20261009-btw-tardis-replay-20261001/report.md)。参数、所有输入与实现源码哈希在同目录 `run_metadata.json`；`acceptance.json` 独立复算账务、导出和资金费，`depth-fill-audit.json` 从原始行情复核所有模拟成交。`verification.json` 保存针对适配器、运行器及已有执行逻辑的测试记录。

复现时使用项目 `.venv/bin/python`，将 `PYTHONPATH` 指向 `/Users/mori/.codex/worktrees/crypto-arbitrage-research/crypto/src`，调用 `runner.py --output <新的空目录>`。旧结果保留，不覆盖；`--benchmark-batches` 仅性能试跑，不作为策略结果。原始盘口及断线数据不插值，不足量时允许双腿等量分批退出。

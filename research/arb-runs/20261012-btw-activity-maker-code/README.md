# BTW 活跃度过滤与单腿挂单研究

此目录仅用于离线公开逐笔回放；没有交易所客户端、凭据读取或真实订单接口。保留旧结果不变。

## 实验顺序

1. `activity_features.py` 从原始接收顺序生成过去 60 秒成交笔数、成交额、最后成交年龄，与 BTWTP002 盘口决策逐纳秒对齐。断线清空状态，完整暖机后恢复准入；Bitget 历史首包不参与实时特征。
2. `calibrate.py` 按已保存的 `protocol.json`，仅用 UTC 00–12 点的均匀秒样本计算 q10/q25 阈值。`thresholds.csv` 中成交额单位为 1e-8 USDT，年龄单位为 ns。
3. `activity_replay.cpp` 同时运行 gateoff/q10/q25。活动条件仅用于开仓，原有止盈止损、对冲、资金费保护及超时退出保持工作。保存开仓前的真实模拟深度预算。
4. `select_activity.py` 先保存训练/验证选择，再计算晚间内部留出统计。这个历史日已被此前研究查看，不能称外部未见测试。
5. `maker_engine.py` 对冻结的同一批开仓机会比较 25%/50%/75% 价差内挂单。某一腿成交后，另一所按延迟后的对手一档与剩余深度吃单；取消延迟、部分成交、晚回报、无法对冲的零头均保留。输出为独立订单影子诊断，没有平仓、完整收益或组合资金曲线。

## 参数及执行

结果根目录：`/Users/mori/code/crypto/state/arb-runs/20261012-btw-activity-maker`。

活动门槛 CSV 参数：`name,countA,countB,turnoverAunits,turnoverBunits,maxAgeNs`，当前支持 `q10`、`q25` 两行，并内置不启用过滤的 `gateoff`。修改门槛时应复制实验到新的结果目录，重新冻结方案，不覆盖原结果。特征窗口可通过 `activity_features.py --window-seconds` 设置。

```sh
clang++ -std=c++17 -O2 activity_replay.cpp -o activity_replay
./activity_replay --tape /absolute/path/tape.bin \
  --activity /absolute/path/features-decisions.bin \
  --thresholds /absolute/path/thresholds.csv --output /new/output/taker

/Users/mori/code/crypto/.venv/bin/python maker_engine.py \
  --tape /absolute/path/tape.bin --trades /absolute/path/live-trades.bin \
  --activity /absolute/path/features-decisions.bin \
  --entry-snapshots /absolute/path/taker/q25/entry_budgets.jsonl \
  --thresholds /absolute/path/thresholds.csv --gate q25 --output /new/output/maker
```

报价分数固定比较三档；本轮到达/撤单/对冲延迟 100ms、TTL 2s、未对冲观察期限 5s 为代码内冻结假设，不是测得的交易所延迟。每次成交均按 3bp 收费，未假设 maker 费率优惠。历史 L2 无法确定反事实排队位置；严格穿价、方向一致且最多占对应成交 25% 仍是模拟成交假设。RPI/大宗成交不用于普通挂单的成交证据。

## 验证

```sh
/Users/mori/code/crypto/.venv/bin/python -m unittest discover -s . -p 'test_activity*.py'
/Users/mori/code/crypto/.venv/bin/python -m pytest -q test_maker_engine.py
```

全日验收另外对照原始逐笔、盘口预算、费用、现金、资金费证据；测试通过不代表策略有效。最终结论和审计证据见结果目录中的报告。

## 最新报价口径：距离对手一档一定距离

用户后续明确不要求固定中间价。`maker_offset_engine.py` 使用：卖单 `bid + d`，买单 `ask - d`。默认 `--pricing-specs ticks:1`；可改为 `ticks:2`、`ticks:5`，或例如 `fraction:0.10` 表示 `d = ceil(价差tick数 × 10%)`。必须满足 `1 <= d < 价差tick数`，否则跳过，不暗中缩小用户要求的距离。`fraction:0.50` 用于复现原中间价对照。

新结果目录：`/Users/mori/code/crypto/state/arb-runs/20261012-btw-counterparty-offset`。原 `maker_engine.py` 已冻结，新引擎继承其成交、取消和库存规则，执行时验证其源哈希。开仓腿成交后，另一所买入对冲用届时卖一、卖出对冲用届时买一，不使用信号时的旧报价。

新 CLI 沿用上面的盘口、成交、活动、快照、门槛路径，并增加：

```sh
--protocol /absolute/path/protocol.json \
--pricing-specs ticks:1,ticks:2,ticks:5,fraction:0.10,fraction:0.25,fraction:0.50 \
--compare-midpoint /absolute/path/original/maker-q25
```

`--compare-midpoint` 可选；比较网格含 50% 时使用，可强制逐事件核对原结果。报价在信号时确定，存续期间不自动追价；到达时会立即吃单则 PostOnly 拒绝。新参数测试为 `python -m pytest -q test_maker_offset_engine.py`。这些参数属于离线研究，未启用任何实时交易。

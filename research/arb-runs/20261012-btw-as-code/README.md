# BTW 跨所 AS 报价回放

仅运行公开历史行情的独立开仓报价实验，不提供真实订单接口。入口保留原有的 20 日波动率前 10、活动度和盘口容量约束，单腿 maker 成交后，另一所延迟至少 100ms 按对手一档对冲。每笔手续费为 0.03%。

`as_quote.py` 实现 Avellaneda–Stoikov 一阶有限时域报价：每个报价单位为 10 BTW，`r = S - q γ σ² τ`，半价差为 `γ σ² τ / 2 + log(1 + γ/k) / γ`。本次固定 `γ=0.004 USDT⁻¹`、`τ=2s`。买价向下、卖价向上对齐本所价格步长；报价不在原有买卖一之间时拒绝，不强行截到价差内。[原始论文](https://math.nyu.edu/inmemoriam/avellaneda/HighFrequencyTrading.pdf)

`rolling_sigma.py` 使用最近 60 个连续一秒价格增量计算方差。失效盘口、缺样本或连接分段变化都会重置。`as_calibrate.py` 仅使用当日 00:00–12:00 UTC 数据拟合 `λ(δ)=A exp(-kδ)`；该强度是公开逐笔成交触达报价的代理，无法证明真实排队成交概率。对全部日期使用同一拟合验收规则，拟合不通过就拒绝 AS 报价。

`cohort_replay.cpp` 的 `--entry-start-ns` 设置为当日 12:00，校准完成前只观察数据，不开模拟仓。`as_replay.py` 将相同的下午开仓机会分别交给 `fixed_ticks_1`、`fixed_ticks_5`、`as_inside`。每个独立机会的实际未对冲初始库存为 0，因此此实验验证报价宽度和成交后对冲成本，**没有验证连续持仓下的 AS 库存控制能力，也不计算完整开平仓策略收益**。

旧文件和旧结果不修改。`BTWTP002` 价格单位为 1e-5、金额为 1e-9 USDT；`BTWTP003` 价格单位为 1e-6、金额为 1e-10 USDT。原生深度和逐笔价格始终保持 1e-8。7–9 月 Bybit/Bitget 报价步长分别采用 1e-5/1e-6 的历史情景假设，匹配观测网格，但没有冒称已取得当日官方合约规则。

运行顺序：

1. 公开原始行情下载与独立 CRC、币种、增量序列验收；历史排名和资金费公告、结算记录单独验收。
2. `build_day_tape.py` 构建深度事件 tape 和连续整秒 mid grid；`activity_day_features.py` 构建活动特征和逐笔流。
3. 以前半天整秒样本计算活动度 q25，运行 `cohort_replay`，禁止 12:00 前开仓。
4. `as_calibrate.py --date DATE --tape TAPE --trades TRADES --grid GRID --protocol PROTOCOL --output CALIBRATION_DIR`。
5. `as_replay.py --date DATE --tape TAPE --trades TRADES --activity ACTIVITY --grid GRID --entry-snapshots NOON_COHORT --thresholds THRESHOLDS --calibration CALIBRATION_JSON --protocol PROTOCOL --output REPLAY_DIR`。
6. 独立复核填单来源、可用时间、数量、费用、对冲深度及成本分解；`summarize_as_comparison.py` 汇总。

所有输出目录拒绝覆盖。历史缺失、公告未知、排名不通过、拟合不通过分别保存原因，不能合并成“没有套利机会”。结果以 `20261012-btw-as-comparison` 下的协议、启动哈希、验收档案和最终报告为准；旧 October 全日 cohort 的诊断不作为新 noon-start 对比结果。

局限：当前 420 个币种的目录存在幸存者偏差，BTW 本身也是回顾性选择；真实排队位置和网络延迟未知。此处至少 100ms 延迟、25% 参与率与部分成交约束仍是仿真假设，无法据此证明实盘盈利。

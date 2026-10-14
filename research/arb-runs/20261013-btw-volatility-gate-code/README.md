# BTW 因果波动率开仓过滤实验

本目录只处理已有公开历史数据，`execution_enabled=false`。既有理想撮合、0.03% 每笔手续费、仓位、止盈止损、15 秒持仓上限、资金费回避和 q25 活跃度条件保持不变。新增门槛只约束开仓，不阻止已有仓位退出。

实验结果目录：`/Users/mori/code/crypto/state/arb-runs/20261013-btw-volatility-gate-experiment-090710Z`。完整指标、固定参数网格和选择规则见该目录 `protocol.json`；此目录的新文件不修改任何旧实验。

- `build_features.py`：从已验收的 1 秒中间价网格构造因果滚动指标，并对每条原生 tape 决策输出同时间戳 `BTWGATE1` bitmask。
- `perfect_taker.cpp`：在原引擎上增加可选 `--entry-gates`，严格校验文件时钟、长度和政策位；无文件时兼容原行为。
- `prepare.py`：冻结原始行情、资金费、活跃度、指标与 gate、引擎源代码和二进制哈希。
- `run_day.py`：对预先指定拟合日或评估日运行全部 15 组政策；拒绝覆盖已有运行。评估必须有提前冻结的选择结果。
- `audit_gate_features.py`、`audit_perfect.py`：独立重算特征/门槛，核对账本、源报价、首次触发和资金费。
- `selection.py`、`freeze_selection.py`：只用两拟合日选参数，候选每日至少 10 组，须同时改善最差日和总净收益，并胜过对应的数据覆盖对照。没有合格候选则保留 baseline；评估日其余政策仅作诊断。
- `summarize_experiment.py`：将每组结果、盈利/亏损原因、相对覆盖对照和被过滤原交易归因到报告；后者不是新策略收益变化的因果归因。

运行顺序：建立新的 protocol/policies → build_features → prepare → 独立特征验收 → 两拟合日 run_day/light audit → freeze_selection → 评估日 run_day/light audit → 三日 deep audit → summarize_experiment。每次使用新的结果目录；不要重启或覆盖本次结果。

指标说明：15 秒价差变化的波动尺度不等于预期可赚价差，也不保证回归。网格的有效覆盖率不足时拒绝新增开仓；用独立 coverage 对照识别这种拒绝，不能将其解释为策略优越。三日已经反复研究，只能作内部探索比较。零延迟、双腿全额一档吃单（理想退出可超过一档深度）的结果不代表真实执行收益。

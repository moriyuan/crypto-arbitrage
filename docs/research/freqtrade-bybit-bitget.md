# Bybit / Bitget Freqtrade 跨所永续协调器

> 历史研究记录，参数与状态可能已被后续实验替代。当前基准以[最新研究进度](../arbitrage-research.md)为准。
当前研究规则：**按回放时点的20日平均日波动率选前10名，并按当前盘口深度决定开平仓数量**（2026-10-09）。20日平均成交额仅记录，默认没有硬性下限；不做盘口预测。SKL旧仿真、BTC配置和预测实验保留为历史证据。

实现日期：2026-10-08；分支 `codex/crypto-arbitrage-research`。上游 Freqtrade 固定为 2026.9，commit `1f394eaebc2f46a83d26971388628707802f8602`。

本轮按用户指定使用两套 Freqtrade 执行实例，协调器控制同币数的 Bybit/Bitget BTC USDT 线性永续多空腿。**当前交付可运行的 dry-run 试验系统；不会下真实交易所订单。** 实盘尚未开放，所需资金规模、账户权限/风险档位与恢复流程须另行验收。

## 组件与责任

| 组件 | 责任 |
|---|---|
| `deploy/freqtrade-arb/bybit.json`、`bitget.json` | futures / isolated，独立虚拟钱包与数据库，仅 BTC/USDT:USDT；默认底稿的 API 关闭且无凭据 |
| `deploy/freqtrade-arb/strategies/CrossExchangeArb.py` | 不自主产生开仓/退出信号，仅接受 `ca1-…:venue` 标签的限价开仓；保留紧急止损；启动时拒绝 live |
| `deploy/freqtrade-arb/runtime.py` | 准备私有运行配置、生成本机 API 认证、启动固定版本；移除环境中的交易所凭据与 Freqtrade 配置覆盖变量 |
| `src/btc_cta/cross_arb/market.py` | 两所公开 REST 50档盘口与规格，并发读取、币数/价格步长、深度 VWAP、新鲜度基础字段 |
| `src/btc_cta/cross_arb/freqtrade.py` | loopback REST、状态/历史查询、限价开仓、市价平仓、撤单；错误脱敏且 mutation 不自动重试 |
| `src/btc_cta/cross_arb/engine.py` | 完整往返成本门槛、等币数双腿、持久化意图、部分成交补偿、重启对账、超时/止损/退出 |
| `src/btc_cta/cross_arb/__main__.py` | `check`、有界 `run`、`flatten` 与明确的 `--resume` |

Freqtrade 每实例只有一个交易所，Bybit 使用本机 `127.0.0.1:18081`，Bitget 使用 `127.0.0.1:18082`；协调器负责关联两所订单。该架构不提供跨所原子成交。官方依据：[多实例](https://www.freqtrade.io/en/stable/advanced-setup/#running-multiple-instances-of-freqtrade)、[交易所说明](https://www.freqtrade.io/en/stable/exchanges/)、[固定版本源码](https://github.com/freqtrade/freqtrade/tree/1f394eaebc2f46a83d26971388628707802f8602)。

## 运行

以下命令在本仓库根目录执行。先准备本仓库 Python 环境：

```bash
uv sync --locked --extra dev
uv venv --python 3.12 state/freqtrade-runtime
uv pip install --python state/freqtrade-runtime/bin/python \
  -r deploy/freqtrade-arb/requirements-macos-arm64.lock
python3 deploy/freqtrade-arb/runtime.py prepare
```

锁定清单记录本次通过验证的 macOS arm64 / CPython 3.12.13 依赖。其他平台可从 `deploy/freqtrade-arb/requirements.txt` 的固定源码 commit 解析环境，但必须重新验证依赖组合。两个模板沿用项目代理 `http://127.0.0.1:7897`，运行前确保其可用；调整代理后重新生成运行配置。

分别在两个终端启动：

```bash
python3 deploy/freqtrade-arb/runtime.py run bybit \
  --freqtrade state/freqtrade-runtime/bin/freqtrade
```

```bash
python3 deploy/freqtrade-arb/runtime.py run bitget \
  --freqtrade state/freqtrade-runtime/bin/freqtrade
```

等待两实例进入 RUNNING，再在第三个终端检查并进行 60 秒模拟观察：

```bash
PYTHONPATH=src .venv/bin/python -m btc_cta.cross_arb check
PYTHONPATH=src .venv/bin/python -m btc_cta.cross_arb run --seconds 60
```

Bybit/Bitget 初次启动可能拉取大量公开保证金档位，Bitget 本次约一分钟且出现过 429，后续使用真实返回的本地缓存。不能跳过风险档位或以伪造缓存缩短启动。REST 行情与 Freqtrade RPC 有实际延迟，当前实现用于执行研究，不声称毫秒级套利能力。

所有运行文件在 Git 忽略的 `state/freqtrade-arb/`：两所独立 SQLite、`api-auth.json`、生成的配置和 `coordinator.sqlite`。API 认证随机生成并以 0600 保存；不要打印、提交或上传这些文件。`prepare` 在启动前执行，模板变化后需要重新生成配置。

有界运行到期或 Ctrl-C 后会停止新开仓，并尝试最多约 30 秒恢复退出。接口超时会增加实际结束耗时。若输出 `RECONCILE_REQUIRED` / 退出码 2，表示仍有未解决意图，不得假定空仓；保留 journal 与两个 worker，运行：

```bash
PYTHONPATH=src .venv/bin/python -m btc_cta.cross_arb flatten --seconds 60
```

停止状态会持久化。确认空仓后明确恢复模拟观察：

```bash
PYTHONPATH=src .venv/bin/python -m btc_cta.cross_arb run --resume --seconds 60
```

`--resume` 只解除正常停止，不清除累计损失、轮次上限或异常暂停。未确认的开仓意图仍须对账；不要删除 SQLite、手工删 worker 交易或换一个 journal 来绕过。修改风险配置时，已有 journal 会拒绝不同 policy，需先用原配置完成对账。

## 默认研究参数

均为模拟情景，不是用户实盘额度：

- 单腿名义上限 250 USDT，杠杆固定 1，两所虚拟初始余额各 10,000 USDT。
- 费用各假设每次 6 bp，完整往返约 24 bp；再预留退出价差 2 bp、资金费 2 bp、最低净边际 5 bp。入场限价另外含每腿 1 bp 价格保护，不重复扣同一盘口冲击。
- 两边数量按公共币数网格取整；不能满足深度、最小数量或最小名义额就不入场。
- 入口 RPC 后最长等待完整对冲约 5 秒；持有上限 60 秒；可执行净收益目标 1 bp；组合估算亏损达 5 USDT 退出。
- 累计已实现净损失 10 USDT 或已完成 3 轮后停止新增；两次交易冷却 30 秒。
- 报价年龄最多 2 秒、两所源/接收时间差最多 500 ms、HTTP 请求最多 1.5 秒；容许交易所时钟领先本机最多 500 ms。状态查询和 preflight 消耗的时间也计入入场报价年龄。

可通过 `--policy /path/to/research-policy.json` 覆盖 `Policy` 中已知字段，例如 `{"max_notional":"500","max_cycles":2}`；未知字段和无效数值被拒绝。没有 live 参数。资金费字段使用 Freqtrade 的模拟估计，因此本系统的 dry-run PnL 不能代替真实资金费账单或真实滑点评估。

## 故障行为

1. 双腿发送前先写 SQLite；同一 journal 只允许一个协调进程。
2. RPC 超时/不明确响应不重发开仓，按唯一 entry tag 查开放与已关闭交易；未观察到记录不等于订单不存在。
3. 真正成交量从订单 fills 计算，不能使用尚未成交时的 `trade.amount` 请求量。任何部分成交不平衡都会在超时后撤余单、退出已知敞口。
4. 一所失联时该腿保持未知；另一所通过独立 preflight 后仍可退出。每次恢复撤单/平仓都重新确认 worker 的交易所和 dry-run 模式。
5. 已观察到的零成交首单取消后，Freqtrade 可能删除 trade，甚至随后返回 502；只有“已确认过、从未有正成交、有撤单意图、完整查询无记录”的情况才能判定这腿结束。
6. 遇到无关交易先停止新增，不操作无关仓位；自身仍有双腿记录时继续恢复自身敞口。
7. 只有两腿闭合并取得可用的已实现损益证据后才能正常继续；闭仓损益缺失则暂停。没有被观察到的未知意图可能需要人工核对，本系统不会为恢复运行而猜测其不存在。

独立止损、宕机、资金费扣款与订单延迟仍可能造成短暂裸腿。当前仅为模拟实现，尚未完成实盘权限、资金费逐笔核对、账户余额/订单外部事实校验和跨所保证金压力验收。

## 凭据

已确认以下变量在 `~/.zshrc` 中存在，未输出值：

- Bybit：`BYBIT_API_KEY`、`BYBIT_SECRET_KEY`。
- Bitget：`BITGET_API_KEY`、`BITGET_SECRET_KEY`、`BITGET_PASSPHRASE`。

可重复检查：

```bash
python3 deploy/freqtrade-arb/runtime.py credentials-check --zshrc ~/.zshrc
```

检查器仅解析指定变量的字面量赋值，不执行 `source`、变量展开或命令替换；重复/动态赋值被拒绝。真实交易所凭据不写入 dry-run 配置，也不注入这些模拟 worker。本地 API 密码是独立随机凭据。

## 已完成验证

- 两套真实 Freqtrade 2026.9 进程连接各自公开行情，API preflight 均报告 dry_run、RUNNING、初始空仓。
- 人为触发一次 **0.001 BTC 双腿模拟开平**，经过 OPEN → EXITING → FLAT；两所最终均空仓，协调记录闭合。该人为信号只验证执行，不验证价差收益；模拟净损益为 **-0.27867742 USDT**。原始脱敏结果见[执行验收证据](evidence/2026-10-08-freqtrade-dryrun-smoke.json)。
- 默认门槛下另进行 20 秒真实公共行情观察，未触发开仓，最终空仓；该短观察不证明市场长期没有机会。
- 133 项新增测试通过（含两项调用真实 Freqtrade 配置 schema 的验证）；Ruff 与新模块 strict mypy 通过。完整仓库回归 445 项通过：其中 Rust 集成测试初次受工作树写权限限制，设置临时 `CARGO_TARGET_DIR` 后单独重跑通过。
- 独立审阅复现的恢复、时间戳、停止持久化与 dry-run 检查问题已修复并增加回归测试。

验证命令：

```bash
PYTHONPATH=src FREQTRADE_TEST_PYTHON=state/freqtrade-runtime/bin/python \
  .venv/bin/python -m pytest tests/test_cross_arb*.py tests/test_freqtrade_arb_runtime.py
.venv/bin/python -m ruff check src/btc_cta/cross_arb deploy/freqtrade-arb \
  tests/test_cross_arb*.py tests/test_freqtrade_arb_runtime.py
.venv/bin/python -m mypy src/btc_cta/cross_arb
```

验收结束前再次确认两边 open_trades 均为 0，并关闭了本次启动的两个模拟进程。没有进行真实下单、账户模式修改、Git 提交或推送。

## WebSocket 实时信号仿真（2026-10-08）

按本轮“每账户 1,000 USDT、信号处成功成交”的要求，增加独立的理想成交实验入口。它复用上述协调器的 `Policy`、`candidate()` 与 `executable_pnl()`，直接用收到的盘口 VWAP 记双腿完整成交；本轮不启动 Freqtrade worker，也不发送 Freqtrade RPC 订单。因此结果用来检验信号与账务，实际下单延迟、排队、拒单、部分成交仍须用前述双实例执行系统另行验证。

```bash
PYTHONPATH=src .venv/bin/python -m btc_cta.cross_arb.live \
  --symbol BTCUSDT --fee-bps 6 --seconds 1800 --output state/arb-runs/unique-session-name
```

输出目录必须不存在，避免覆盖历史账本。代理仍默认 `http://127.0.0.1:7897`。`websockets` 最低版本 15，锁定环境为 16.1.1。程序在双所盘口订阅就绪后开始 30 分钟计时；SIGINT/SIGTERM 或时间结束时停止新增，使用新鲜行情退出。若行情断线导致无法估值/退出，保留未闭合状态，禁止用旧报价伪造空仓。

- Bybit：`orderbook.50.BTCUSDT`，50档 snapshot/delta，官方推送周期20ms；同步订阅 `publicTrade.BTCUSDT`。
- Bitget：v3 `books5`，5档完整快照，官方推送周期10ms；同步订阅 `publicTrade`。Bitget还提供1ms一档数据，此实现为验证可成交深度选用五档。
- 逐个有效盘口更新进入双方向评估。逐笔成交保持原始批次，用于后续回放，不能把最新成交价当作可执行买卖价。
- 原始公开 JSON 帧全部写入 `public-ws.jsonl.gz`，保存交易所原始时间、本机接收毫秒时间和单调时钟。真实断线后重连、重新取快照；不补造间隙数据。信号队列溢出或记录器失败会停止实验，错误在状态文件中显示。
- 两所各1,000 USDT，单腿最多250 USDT，杠杆1。手续费假设仍为每笔6bp，完整往返约24bp；保留原有净边际、退出价差与资金费缓冲、30秒冷却、60秒持有上限和10 USDT累计亏损限制。实验轮次上限设为10,000，由30分钟时长和其他风险门槛实际约束。
- 初始与每30秒刷新两所公开资金费时刻、费率和标记价格。缺失/过期或距离结算不足“最长持有时间+60秒”时不入场，并退出已有模拟仓位。`cycles.csv` 明确报告手续费后、资金费前损益；只有保存的时刻证据确认整个持有期未跨结算时，`status.json` 才认定资金费为0。未闭合或跨结算证据冲突时金额为unknown，不能据此宣称最终净收益。

输出文件：

| 文件 | 内容 |
|---|---|
| `ledger.sqlite` | 信号、订单、组合、权益与事件追加账本；每tick事务提交，保存最后状态 |
| `signals.csv` | 两方向所有评估，包括未达门槛、报价失效、余额不足与风控阻断 |
| `orders.csv` | 模拟成交ID、关联信号/组合、场所、方向、数量、VWAP、费用 |
| `cycles.csv` | 双腿完整往返、逐腿损益、总毛利、四笔手续费、资金费前净损益、退出原因 |
| `equity.csv` | 每所现金、已占保证金、可用余额与按当前盘口估算的权益 |
| `public-ws.jsonl.gz` | 所有收到的盘口和逐笔成交批次，附本机接收时间 |
| `funding-schedules.jsonl` | 公开资金费时刻查询证据 |
| `status.json` / `summary.json` | 运行进度/最终状态、消息与逐笔计数、最后模拟账本 |
| `report.md` | 中文汇总和文件说明 |

运行期间SQLite持续追加，`status.json`每10秒原子更新；CSV和报告在结束时导出。账本使用WAL/NORMAL，支持进程级事务恢复；操作系统崩溃可能损失最近尚未落盘记录，不宣称断电零丢失。原始gzip每秒flush。异常退出后保留原目录，不覆盖或自动重开一个假定空仓的新实验。

首轮12秒集成验收收到1,339个公开帧、452笔逐笔成交，完成2,266次方向评估，无断线、无队列丢弃、无合格信号、无模拟订单。正式30分钟实验单独运行，结果不得与这个连通性验收混合。本轮新增89项测试，跨所相关合计222项通过，Ruff和strict mypy通过。

协议依据：[Bybit盘口](https://bybit-exchange.github.io/docs/v5/websocket/public/orderbook)、[Bybit逐笔](https://bybit-exchange.github.io/docs/v5/websocket/public/trade)、[Bitget盘口](https://www.bitget.com/docs/uta/websocket/public/Order-Book-Channel)、[Bitget逐笔](https://www.bitget.com/docs/uta/websocket/public/New-Trades-Channel)、[Bitget推送频率](https://www.bitget.com/docs/uta/best-practices-guide)。

## 订单流驱动的盘口状态预测（后续升级）

当前订阅改为两所50档增量盘口和逐笔成交：Bybit `orderbook.50`、Bitget v3 `books50`，均按官方20ms档位处理。Bitget按首次快照与 `pseq/seq` 链重建；断链、断线会立即使该所状态失效，重新等待快照。上面的30分钟实验确实使用了当时的Bitget五档快照，其历史证据保留；新采集使用50档增量。

两所公开深度均为按价格聚合的L2，不能提供全市场每张委托的订单ID、精确新增/撤单归属或排队位置。这里的“订单流”是公开可见档位变化与逐笔成交，不称为完整MBO逐笔委托。

`nowcast.py` 的 `FlowState` 在每个委托档位更新或成交到达时更新状态：

- 用初始快照建立盘口，随后按增量维护；快照用于初始化/恢复。
- 从共同可见的前五档计算有符号深度变化，避免档位窗口移动被误认为新增/撤单；同时记录盘口失衡、微价格和盘口变动速度。
- 逐笔按成交ID去重，以成交源时间计算100/500ms主动买卖压力。Bitget初始历史批次和延迟成交不被当成刚发生的流量。
- 只把 `盘口源时间 < 成交源时间 <= 决策时间` 的可见成交按精确价位扣减；不把同毫秒或已经包含在盘口中的成交再扣一次。RPI/block与身份不明标记禁止用于可见深度扣减。
- 新盘口到达后重新对齐成交时间，避免重复扣量。投影保留原始盘口时间；单边耗尽或未覆盖历史被淘汰时投影失效。

`forecast.py` 用上述特征拟合当前盘口中间价修正，输出估计买卖一价、原确认盘口、成交补减数量及经验误差带。价格预测与已确认盘口分开保存。数量投影不是可成交数量证明，所有输出均为影子信号；不向原模拟账本或Freqtrade提交预测价作为成交。

复现与实时查看：

```bash
PYTHONPATH=src .venv/bin/python -m btc_cta.cross_arb.forecast replay \
  --symbol SKLUSDT --archive /path/to/public-ws.jsonl.gz \
  --output state/arb-runs/new-nowcast-evaluation
PYTHONPATH=src .venv/bin/python -m btc_cta.cross_arb.forecast live \
  --symbol SKLUSDT --model state/arb-runs/new-nowcast-evaluation/model.json \
  --output state/arb-runs/new-nowcast-shadow --seconds 60
```

输出目录必须是新目录。回放产物为 `model.json`、`evaluation.json`、`holdout-predictions.csv.gz`、`report.md`；实时产物为原始 `public-ws.jsonl.gz`、状态预测 `predictions.jsonl.gz`、两方向 `signals.jsonl`、`status.json`。接收每个事件，默认每50ms输出一次预测，实时频率沿用模型的训练采样间隔。

验证严格按物理接收记录顺序生成特征，不按交易所时间重排输入。目标为本地决策时刻之前25ms内、随后收到的最近已发布源盘口，并要求其后有另一个源盘口作为时间区间右端证据。标签只作事后验证，假设场所和本机时钟同步；它不是不可见的撮合引擎逐事件真值。训练/校准/测试按时间60%/20%/20%划分，边界隔离2秒，并核对标签实际收到时间不超过对应分段末端。模型系数只用训练段；误差带只用校准段。

本次旧30分钟记录留出结果（中间价误差）：

| 场所 | 留出样本 | 最后确认盘口MAE | 订单流MAE | 最后盘口P95误差 | 订单流P95误差 | 经验误差带实际覆盖率 |
|---|---:|---:|---:|---:|---:|---:|
| Bybit | 5,027 | 0.1884bp | 0.1955bp | 1.0126bp | 0.9081bp | 82.69% |
| Bitget | 4,216 | 0.2412bp | 0.2572bp | 1.0865bp | 0.9602bp | 82.99% |

两所均未通过校准段的平均误差改善门槛，因此影子候选的模型启用标记为false。尾部误差改善不能掩盖平均误差变差；校准段95%分位误差带在后段只有约83%覆盖，不能宣称95%保证。两所误差带相加也不构成组合95%置信保证。旧Bitget五档记录只验证该档案上的特征，新的50档采集还需积累独立样本。

影子信号以预测卖一价的保守下界减预测买一价的保守上界，再扣原有费用/退出/资金费/价格保护缓冲；它只叫预测BBO价差信号，未验证指定币数的真实可执行性。当前版本保留所有预测用于研究，不启用自动成交。

接口依据：[Bybit价格档位增量](https://bybit-exchange.github.io/docs/v5/websocket/public/orderbook)、[Bybit全深度仍为L2](https://bybit-exchange.github.io/docs/v5/websocket/public/full-ob)、[Bitget增量与序号规则](https://www.bitget.com/docs/uta/websocket/public/Order-Book-Channel)、[Bitget逐笔成交与RPI标记](https://www.bitget.com/docs/uta/websocket/public/New-Trades-Channel)。

本轮验收：相关271项测试通过（全套270项后补充无数据结束回归1项），Ruff和strict mypy通过。60秒实时影子运行处理5,623个事件、1,844次单所预测和1,842次方向信号评估，无断线或丢弃；校准门槛未通过，合格影子信号0、订单0。实际预测记录和回放评估分别保存在 `20261008-orderflow-shadow-60s` 与 `20261008-orderflow-nowcast-v1` 的运行目录。


## 当前标的：SKLUSDT 独立订单流采集（2026-10-09）

按用户要求停止以 BTC 作为新研究对象，选定两所共同正常交易的 SKL USDT 线性永续。选择证据位于原工作区 `state/arb-runs/20261008-altcoin-selection/selection.json`。这是相对 BTC 明显较低流动性的研究对象，并非全市场最薄尾部：选币时两所24小时成交额约356.01万/300.71万USDT；三次50档采样点差中位16.64/10.40bp。距中价25bp内买/卖深度中位，Bybit约1010.54/743.27USDT，Bitget约1899.68/1860.05USDT。每所每侧约250USDT的深度检查全部通过，但快照容量不是未来可成交保证。

共同数量步长1 SKL、最小金额5USDT、价格步长0.000001。公开规格中 Bybit资金费周期8小时、Bitget4小时；以后恢复模拟成交时必须使用各自公开结算时刻，不能沿用BTC时间表。本轮先独立采集/训练，不产生订单和损益。

实际公开WS探针发现 Bitget `books50` 在SKL持续返回snapshot/pseq=0；同连接的 `books` 则在15秒内返回1次初始snapshot及240次update，pseq连续。故当前采集选择 **Bybit orderbook.50 + Bitget books全深度增量**，并同时订阅两所 publicTrade。Bitget内部保存完整已知深度（上限每侧1000档），对特征提供前50档，深层档位能在前档删除后正确晋升。首快照实测430卖档/197买档，maxDepth可缺失。频道探针保存在 `state/arb-runs/20261009-skl-bitget-channel-probe.json`。公开数据仍是聚合价位L2，不是订单ID级逐笔委托。

```bash
PYTHONPATH=src .venv/bin/python -m btc_cta.cross_arb.capture \
  --symbol SKLUSDT --seconds 1800 --evaluate \
  --output state/arb-runs/unique-skl-orderflow-30m
```

`capture` 没有模型输入和成交入口；先保存完整原始档案，两所均有盘口、成交及真实增量且连续性完整才自动回放。采集中发生断线、状态失效或队列丢失立即停止并保留证据，不把重连前后记录拼接训练。正常关闭的状态失效计数另存，不误报为采集中断线。`status.json`每2秒更新，区分采集时长和含关闭耗时的总时长。结束后有 `report.md`，启用evaluate时另有 `evaluation/`；样本不足或回放异常明确标记EVALUATION_FAILED。

订阅、解析、特征、输出与模型均绑定symbol；CLI必须显式传币种。version2模型还保存实际深度频道，实时影子观察使用匹配频道。旧version1 BTC模型以及不同币种模型拒绝载入，含混币原始帧的档案拒绝训练；BTC旧档案只能显式指定BTC复核。每个新模型按原有时间切分独立训练和校准，不把BTC参数迁移到SKL。拟合完成不代表预测有效；校准改善、留出误差、样本覆盖与后续实时表现仍须验收，预测价不作为成交价。

本轮验收：相关365项测试、Ruff及strict mypy通过；独立复核未发现新增明显P1/P2。最终20秒SKL连通性检查收到770帧，Bybit378次delta及58笔成交、Bitget334次update及67笔成交，断线与队列丢弃为0，gzip完整性核验通过（成交包含Bitget启动历史首包，不全部代表20秒内发生）。正式采集目录为原工作区 `state/arb-runs/20261009-skl-orderflow-30m`，2026-10-09北京时间00:26:45启动，计划00:56:45结束后自动独立训练；采集和评估结果以该目录最终status为准。


## 当前仿真：SKL 直接吃单（2026-10-09）

用户已明确改为“不进行盘口预测，直接抢单”。当前 `live` 仿真不加载任何预测模型，按 Bybit `orderbook.50.SKLUSDT` 和 Bitget `books` 增量维护确认盘口，每次有效盘口更新检查两个方向的可见深度价差；publicTrade 逐笔同步保留。成交、退出和权益估值全部用实际收到的盘口VWAP。过去模型未通过校准的结果保留，不修改或重新命名为有效信号。

```bash
PYTHONPATH=src .venv/bin/python -m btc_cta.cross_arb.live \
  --symbol SKLUSDT --fee-bps 3 --seconds 1800 \
  --output state/arb-runs/unique-skl-taker-paper-30m
```

每所虚拟初始1000USDT、1倍杠杆、单腿最多250USDT，双腿为同一数量SKL，数量步长1、最小名义5USDT、价格步长0.000001。规则和资金费时刻从每所公开API分别获取并严格校验SKL身份；`PaperSession` 的所有SQL/CSV/summary记录正确symbol/pair。按用户纠正，完整往返假设每笔3bp（0.03%）共约12bp手续费，退出价差和资金费缓冲各2bp，要求扣除后至少5bp净边际；价位取整、价格保护与指定数量的深度冲击同时计入。最多持仓60秒，组合亏损5USDT退出，累计亏损10USDT停止新增，平仓后冷却30秒。资金费刷新及避开结算逻辑沿用已验收实现，不能复用BTC结算时刻。

这是原约定的理想成功吃单仿真：同一信号时刻双腿按可见深度同时全额成交，未建模网络延迟、排队、部分成交或交易所拒单。模拟限价保护不代表真实交易所承诺成交。盘口过期、不同步、深度不足、数量规则不满足或资金费证据不完整时不入场，绝不人为制造订单；到期退出若缺少新鲜盘口则保留未闭合组合并报告。

输出仍为完整 `ledger.sqlite`、`signals.csv`、`orders.csv`、`cycles.csv`、`equity.csv`、`summary.json`、`report.md`，以及公开原始消息和资金费证据。`run_metadata.json` 明确 `signal_model=confirmed_depth_taker`、`model_used=false`，`execution_enabled=false` 指真实下单关闭，本地模拟账本仍正常记单。结束计数在WS关闭后读取，另存 `stream_before_close` 以区分运行断线与正常关闭失效。


费用纠正：当前 `live` 入口默认 `--fee-bps 3`，显式同时传入两所Policy，信号门槛与逐笔订单使用同一个费率，报告从运行policy读取费用；旧实验账本不回写。此前6bp的SKL运行在约697秒时正常停止，44,568次方向评估、零订单、两所仍各1000USDT；原目录及 `fee-correction.json` 留存。后续3bp运行单独建账，仅继续原1800秒预算的剩余有效时长，两段数据不混报为统一费率的30分钟结果。按每腿250USDT估算，四笔往返手续费从约0.60降到0.30USDT；实际仍逐笔按成交名义额计费。


## 2026-10-09：参数化入场波动率与流动性筛选

当前 `btc_cta.cross_arb.live` 默认启用下面的筛选，仍使用确认盘口直接吃单，手续费每所每次成交3bp，不加载预测模型。两所各自的波动率都要达标，拟开仓多腿的卖盘与空腿的买盘带内深度也都要达标；之后仍受原有净价差、报价新鲜度、余额、资金费和仓位限制。筛选只决定新增仓位，平仓、止损、超时和强制退出不受这些入场阈值阻挡。

| CLI参数 / `EntryFilterConfig`字段 | 初值 | 定义 |
|---|---:|---|
| `--volatility-window-s` / `volatility_window_s` | 60 | 已完成采样桶覆盖的收益窗口秒数 |
| `--volatility-sample-ms` / `volatility_sample_ms` | 1000 | 固定时间桶宽度；各桶使用当时最后收到的有效中间价 |
| `--min-volatility-bps` / `min_volatility_bps` | 5 | 两所各自窗口实现波动率下限；0关闭该门槛 |
| `--liquidity-band-bps` / `liquidity_band_bps` | 10 | 买腿以ask1向上、卖腿以bid1向下的价格范围 |
| `--min-depth-usdt` / `min_depth_usdt` | 500 | 上述范围内每条入场腿的最低可见深度金额；0关闭深度门槛 |

这些是未优化的研究初值，不代表有盈利能力。深度按 `Σ(价格×币数)` 计算，不使用24小时成交额，也不把远离成交价的大额挂单算入。只累计所订阅盘口覆盖的档位，因此是可见深度；即使通过也不保证订单到达时深度仍然存在或退出时有相同流动性。

波动率定义为 `10000 × sqrt(Σ log(mid_t / mid_(t-1))²)`，`mid=(bid1+ask1)/2`，不年化、不减均值。60秒/1秒对应60个收益、61个已经完成的价格桶；启动后约需61秒暖机。当前未完成桶不进入指标，同桶多次更新不增加样本，避免消息频率改变指标权重。缺桶、间断超过一个采样间隔、失效报价、流失效/重连或时间回退会重置窗口；不跨空档把一次跳价当作可交易高波动。使用本机接收顺序，不用未来成交或未来盘口筛选当前信号。

配置是冻结的 dataclass，可用 `dataclasses.replace` 构造候选参数，CLI五项均可独立覆盖。窗口必须包含整数个采样间隔且至少2、最多10000个收益，阈值必须有限非负；非法配置在创建运行目录和联网前拒绝。对照实验可将两项最低阈值均设为0；Python旧调用 `PaperSession(..., entry_filter=None)` 保留未筛选行为，新的实时入口默认启用。示例（仅说明配置，本次未启动运行）：

```bash
PYTHONPATH=src /Users/mori/code/crypto/.venv/bin/python -m btc_cta.cross_arb.live \
  --symbol SKLUSDT --seconds 1800 --output state/arb-runs/new-filter-experiment \
  --fee-bps 3 --volatility-window-s 60 --volatility-sample-ms 1000 \
  --min-volatility-bps 5 --liquidity-band-bps 10 --min-depth-usdt 500
```

`run_metadata.json`、`status.json` 和账本 `latest_state` 保存完整 `entry_filter` 配置；`signals.csv`/SQLite每个方向保存两所波动率、两腿带内深度、已完成/所需收益数、`entry_filter_passed` 和 `entry_filter_reason`。原始成本不达标和筛选失败分别可见；仅从通过全部筛选的候选方向中选单。账本事务失败会一起回退采样状态，避免失败tick影响重试信号。

后续搜参应在固定、同时间段的两所数据上按时间划分训练/验证/留出段，候选窗口分别预热；不能把不同时间的多次在线采集当成同一回测样本比较。当前交易仿真仍是零下单延迟、双腿全额VWAP的理想假设；前一轮计时漂移、行情空档和持仓超时是未解决的独立运行问题。本次未执行新的采集、交易仿真或参数搜索，未改写旧账本。


### 两所历史行情回放来源（2026-10-09核对）

| 场所 | 已确认SKL逐笔成交 | 限制与验证程度 |
|---|---|---|
| Bybit | [官方SKLUSDT日目录](https://public.bybit.com/trading/SKLUSDT/) 的每日CSV.gz | 目录实测1661个文件，2022-03-23至2026-10-08；抽读最近文件前64KB成功，包含时间、价格、数量、方向、成交ID。未下载或校验整段完整性 |
| Bitget | 公共 `/api/v2/mix/market/fills-history`，`symbol=SKLUSDT&productType=USDT-FUTURES` | 实测2026-10-08查询返回成功；文档限制最近90天、每次查询跨度最多7天、每页最多1000条、`idLessThan`向前分页。仅验证样例，未全量回补 |

两条路径均可公开读取，无需账户密钥。Bybit的[recent-trade接口](https://bybit-exchange.github.io/docs/v5/market/recent-trade)只提供最近成交，合约最多1000条，无起止时间分页，长期历史使用官方文件。Bitget接口依据为[合约市场文档中的Get History Transactions](https://www.bitget.com/docs/catalog/classic-contract-market/classic-contract-market)，另有[历史下载中心](https://www.bitget.com/data-download)。分页必须按成交ID去重、检查时间/ID边界与缺口，保留原始精度；两所不可共用成交ID命名空间。

逐笔成交可用于成交价波动率、成交量、成交笔数研究，无法重建撤单、未成交挂单或中间价。本次实现的中间价波动率与带内深度筛选，需要两所同时间范围的L2历史。Bybit[历史入口](https://www.bybit.com/en/derivative-activity/history-data)列有orderbook；本次未核实SKL具体深度文件及频率。Bitget下载中心列有合约深度，其[深度下载说明](https://www.bitgetapp.com/docs/uta/data-download-center)描述按价格精度合并的买卖盘快照；未验证SKL文件或证明包含当前`books`频道的连续增量，不能称为完整逐笔委托/MBO。

现有自采 `public-ws.jsonl.gz` 含初始化快照、增量与成交，可按原接收顺序重建已观测L2；断线/序列缺口后必须重新快照与暖机。历史文件通常只有交易所时间，没有当时本机接收/下单延迟，因此未来回放必须明确假设延迟、跨所时钟和部分成交规则。前次有长空档的档案不能直接当作连续样本，不能跨缺口造平仓。此次仅完成来源核查与开仓筛选，没有新增历史交易回放器或批量搜参结果。


### 新筛选仿真启动前的计时修正

2026-10-09启动新30分钟采集前，修复上一轮单调时钟暂停导致墙钟超期继续开仓的问题：同时检查UTC墙钟和单调时钟，任一到期停止新增；等候行情返回后、产生信号前再次检查。两钟累计时间差超过5秒记为`clock_discontinuity`失败，保留账本。平仓等待也同时受两钟30秒限制，取盘口后在成交前检查；时间倒退至账本最后tick之前时保留未平仓，不捏造成交时间。启动使用仅绑定本次PID的`caffeinate -i`防空闲休眠，不保证合盖或强制休眠时继续接收行情。本轮仍使用理想全额VWAP成交，未加入真实延迟和部分成交模型。


## 2026-10-09：离线L2回放与参数搜索

`replay.py` 严格读取本项目采集的已完成公开档案：gzip/JSON与SHA、symbol、实际增量类型、交易所序列、配套运行状态及丢失计数均须通过；重连、第二快照、单所或全局超过5秒空档会拒绝整档。小幅接收时刻倒序保持物理记录顺序，以已看到的最大接收时刻推进决策，并保留盘口原接收时刻；不按交易所时间排序，原2秒新鲜度和500ms跨所时间差仍然生效。原始成交仅审计计数，入场与成交使用重建L2，不用成交首包历史构造过去信号。

`run_segment` 对每个分段建立独立PaperSession和两所各1000USDT资金；段前窗口仅预热筛选，不产生持仓。交易区间严格为`[start_ms,end_ms)`，提前最长持仓时间（当前60秒）停止新增并请求平仓，末尾仅用已见且仍新鲜的盘口退出，不借用下一段盘口。不能平仓则保留UNRESOLVED。每段输出ledger.sqlite、signals/orders/cycles/equity.csv、summary.json及segment.json。

首轮旧版实验的 `sweep.py` 固定60秒窗口、1秒采样和10bp深度带，搜索最低波动率`0,5,10,20bp` × 最低深度`250,500,1000U`，12组加一组0/0未筛选baseline；这是旧版记录，新默认见下节。波动率0组属于消融对照，不会被选作满足双阈值目标的最优配置。按接收时间60%训练/20%验证/20%留出切分，候选训练和验证各至少3组闭合交易且各自净收益为正，才按验证收益排序；同收益按候选ID稳定打破平局。无合格参数时不声称找到最优，只在留出评估预先指定默认5bp/500U和baseline。选参文件在留出运行前写入并冻结SHA，留出收益不参与选参。

首轮旧版命令（已运行；新CLI需要下节历史参数，不要复制到相同输出目录重启）：

```bash
PYTHONPATH=src /Users/mori/code/crypto/.venv/bin/python -m btc_cta.cross_arb.sweep \
  --run-dir /Users/mori/code/crypto/state/arb-runs/20261009-skl-orderflow-30m \
  --rules /Users/mori/code/crypto/state/arb-runs/20261009-skl-taker-paper-30m/instruments.json \
  --funding-audit /Users/mori/code/crypto/state/arb-runs/20261009-skl-replay-input-audit.json \
  --output /Users/mori/code/crypto/state/arb-runs/20261009T021700Z-skl-filter-sweep \
  --symbol SKLUSDT --fee-bps 3 --window-grid 60 --sample-ms 1000 \
  --volatility-grid 0,5,10,20 --depth-grid 250,500,1000 --band-grid 10 --min-trades 3
```

输出search-results.csv/json保存所有训练/验证候选，selection.json保存冻结选择，holdout/保存独立账本，summary.json/report.md汇总；status.json每1000个盘口点更新进度。运行只使用本地历史数据，不采集实时行情，不加载预测模型，不读取账户凭据和下单。窗口、带宽也支持逗号分隔参数网格，最大64个组合再加baseline，避免无界搜索。通过官方历史结算记录证明该档案完整区间无结算，证据必须绑定同一SHA；此证据仅用于事后核算，不作为历史时点可见特征。

原档只有约30分钟，是一次探索性搜索。理想全额VWAP和未建模延迟/部分成交的限制仍在；历史合约规则采用稍后同日公开快照，较早选币记录可作交叉核对，无法保证期间从未变更。搜参通过或单段盈利都不能替代更长独立样本与实际执行验证。

### 2026-10-09：回放中的动态20日排名与盘口容量

新回放/搜参默认必须提供 `HistoryGate`；缺失时在建立输出和账本之前拒绝。只有复现旧实验的显式 `allow_legacy_unfiltered=True` Python调用可沿用旧行为，CLI不提供此旁路。现有实时入口没有因本次修改自动启动或扩展币种。

每个回放时点取最近20个**已完成UTC日**：每日RV为24个小时收盘对数收益平方和开方，20日指标为日RV的算术均值，不年化。两所分别计算，以较低RV排序，稳定排序下只允许前10名开新仓。新日数据默认收盘60秒后可用，可调 `--history-publication-delay-ms`；不足20日或缺任一小时不填补、不借用当前未完成日，也不沿用更旧的合格名单。需要20×24+1个小时收盘。任一应有原始归档或manifest缺失时整池拒绝，防止缺文件使其他币错误升位；已归档但当时历史不足的币种不参与完整历史研究池排名。每个完整UTC日的排名缓存与秒级盘口筛选相互独立；盘口暂时失效不会重置20日历史。

用户已放宽历史流动性：`--history-min-turnover-usdt` 默认0，20日日均USDT成交额继续写入信号作为诊断，不是250U成交保证。短期波动率/固定盘口金额门槛在新CLI默认也为0；历史RV排名和实际深度容量承担筛选与控仓。研究者仍可显式启用额外门槛，其参数完整记录。

历史排名使用原始小时K线逐时点重算，不能读取今天的selection排名来倒填历史。`--identity-manifest`仅提供项目身份核验信息，不用于提前裁剪排名的参照币池：若历史前10里出现未完成项目身份核验的币种，拒绝开仓并记录原因，不用后面的已核验币替补。已知MARSCOIN股票代币关联项目排除。当前输入的合约目录是采集日目录，尚不是历史时点完整上市/退市目录；历史最终K线也不是逐版本行情档案。因此只能称固定研究币池内的因果指标回放，不能声称无幸存者偏差的全市场历史策略。

`DepthSizingConfig`默认使用对应买卖一档向外10bp的可见深度，最多占25%；开仓数量取双腿容量、单腿250U上限及账户可用资金允许数量的最小值，再按两所共同币数步长向下取整，最小数量/金额规则仍检查。`--execution-band-bps`、`--depth-participation`可调整这两个参数。原价差成本、手续费3bp、新鲜度和风控检查继续生效。

平仓不受历史排名或历史成交额阻挡，使用平仓方向的盘口容量；需要时进行双腿同币数分批退出。已消费的盘口不得重复生成可用流动性，只有同价档实际增加量补充额度；VWAP按剩余逐档额度计算，部分退出也避免主动留下低于最小订单量的余仓。每次部分退出分别记订单、手续费、已实现损益、剩余仓位及释放保证金；只有余额仓位全部退出才生成闭合组合。薄盘口或规则限制无法退出时保留UNRESOLVED，不制造满额成交。新验收应核对各腿累计开仓量=累计平仓量、两腿剩余量一致、现金=1000+逐笔已实现毛损益−所有费用；不能再强制每组合恰好四单。

搜参CLI新增必填 `--history-dir <公开小时线目录>`、`--identity-manifest <项目身份核验JSON>`。常用参数：

```text
--history-days 20 --history-top-n 10
--history-min-turnover-usdt 0 --history-min-volatility-bps 0
--history-publication-delay-ms 60000
--execution-band-bps 10 --depth-participation 0.25
--volatility-grid 0 --depth-grid 0 --fee-bps 3
```

训练、验证、留出和baseline共用相同历史门槛/盘口容量规则，baseline仅取消额外秒级固定阈值。历史指标可用该段开始前的已知历史预热，不使用未来价格或未来收益选择参数。每次允许数量仍假设双腿同时以可见深度VWAP成交，尚未建模真实网络延迟、排队、单腿失败或交易所真实部分回报，容量比例本身不是执行有效性证明。全仓深度不足时，止损会用可成交分片价格外推剩余仓风险并记录 `partial_depth_position_pnl_proxy`，它不是整仓可执行损益；实际订单数量仍限于当时可用预算。

现有选币小时线覆盖2026-09-19至10-08的20个完整UTC日；回放10-08的L2时至少还需09-18的历史作为暖机，不能使用10-08全天数据。当前只保存了SKL的连续L2档案，选币K线不等于新10币的逐笔订单簿；本次规则实现不代表已完成10币交易回放。

取消ADV硬门槛后，现有2026-10-09窗口的研究池前十新增LYN、STONK；其项目身份尚未完成核验，当前会拒绝开仓且不补位。

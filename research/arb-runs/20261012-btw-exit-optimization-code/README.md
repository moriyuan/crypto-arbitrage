# BTW 退出规则与现货辅助定价研究

这些脚本只处理已存档公开数据和虚拟账户，不连接账户或发送订单。2026-10-01 这一天此前已经看过，12h/6h/6h 切分只属于内部验证。

## 退出规则

`runner.py` 固定每笔手续费 3bp、两所各 1000 USDT、每腿最多 250 USDT、1 倍、相同币数；以对手一档报价和该档未消耗的 25% 数量模拟双腿同时成交。原开仓、历史选币、资金费保护、60 秒超时、30 秒冷却和逐档消耗记忆保留。

- `--risk-mode absolute_net`：止盈与止损都按剩余双腿扣费净损益，除以剩余多头腿原始开仓名义金额。复现原模式。
- `--risk-mode entry_deterioration`：在开仓时用实际成交价、当时反向一档报价及四笔费用，冻结“立即平仓净值”的每币基准。剩余仓位净值相对这个基准继续恶化至 `--stop-loss-bps` 时止损。基准不随行情重设。
- 止盈始终要求扣除开平费用后达到正的 `--take-profit-bps`，不是仅从负的入场基准改善若干 bp。
- 相对模式还检查“已经实现的组合净损益 + 剩余仓位可平仓净值”是否低于初始多头单腿名义金额的 `--hard-stop-loss-bps`。分批退出不会重置初始分母。

入场已经越过硬阈值时只记录标志，保留原入场用于隔离退出规则的影响；下一个可执行风险决策重新检查当前损益，若已恢复则不强行退出。硬阈值不是损失保证。资金费保护、超时、普通/硬止损、止盈有既定优先级；开始分批退出后沿用首次原因直到清仓。因而“止盈触发”不等于最终盈利。

冻结的本轮候选调用（`--output` 必须是不存在的新目录）：

```sh
PYTHONPATH=/Users/mori/.codex/worktrees/crypto-arbitrage-research/crypto/src \
  /Users/mori/code/crypto/.venv/bin/python runner.py \
  --output /absolute/path/to/new-offline-run \
  --risk-mode entry_deterioration \
  --take-profit-bps 5 --stop-loss-bps 20 --hard-stop-loss-bps 100
```

这不是新的默认实盘参数或盈利建议。选择依据、留出结果和验收见相邻 `20261012-btw-exit-optimization` 目录。

## 复现与验收

`build_tape.py` 从原生档案生成接收时间排序的决策流，`screen.cpp` 做整数精确的候选筛选。`verify_select.py` 先逐单对齐旧基线，再按预注册协议选参并先写冻结文件后计算留出指标。入选方案必须回到独立 Decimal/Python 引擎完整运行，并通过 `compare_selected.py`、`audit_accounting.py`、`depth_fill_audit.py` 和 `audit_risk.py`。深度审计独立重建原始盘口，核对入场冻结的反向报价。

`missed_opportunities.py` 为旧止损组合各自建立整仓影子，只观察原 60 秒期限内的净 +2bp 报价、合法片段与足量双腿盘口机会。各影子不组成一个真实资金账户，不能将后来最好价格加总成策略收益。

`spot_pricing_study.py` 按本地接收时刻合并现货与合约，固定 500ms 采样，过去 5 分钟半衰期正常基差与 1 秒历史收益，比较有无现货信息的 1 秒/5 秒合约中价预测。模型只在前 12h 拟合；它不模拟 maker 排队、成交概率或对冲损益。

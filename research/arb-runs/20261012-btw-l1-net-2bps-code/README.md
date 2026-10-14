# BTW 单腿2bp净止盈 / 净止损逐笔回放

本轮沿用2026-10-01 UTC Bybit / Bitget BTW逐笔与增量盘口，仅离线模拟。用户最终口径为计入手续费后判断止盈止损。

单腿基准 N = 剩余BTW数量 × 多头原始开仓价。双腿净损益 P = 双腿预计毛价格损益 − 对应剩余数量的两腿开仓手续费 − 两腿预计平仓手续费。P >= N × 2 / 10000 止盈；P <= −N × 2 / 10000 止损。两所每笔费用3bp；单腿250 USDT时净触发线为±0.05 USDT。风险估计的费用只参与比较，实际每条订单仍仅扣费一次。资金费另外核验，只有未跨结算时才能将手续费后净值称最终净损益。

报价和成交继续使用对手一档，每档显示量25%为可消耗预算。两所各1000 USDT、单腿最多250 USDT、1倍、双腿等币数；入场净门槛5bp及退出/资金费各2bp缓冲、60秒持仓退出、30秒全平后冷却、累计净亏10 USDT停新仓保持原规则。首次退出原因持续保留，可能分批退出；2bp是触发线而非保证成交或最终损失上限。风险估计以可执行片段价格外推剩余持仓，实际成交受共同预算限制。

每笔3bp、四笔往返约单腿12bp成本，因此报价不利或未迅速收敛时，下一次有有效可执行盘口就可能触发2bp净止损。禁止只按毛利判断、重复扣手续费、触价即虚构挂单成交。本轮不引入现货报价或maker模型。

runner.py默认两个bp参数均为2，入口 --take-profit-bps / --stop-loss-bps 可单独设置；本次验收固定检查用户指定2/2。通过 PYTHONPATH=/Users/mori/.codex/worktrees/crypto-arbitrage-research/crypto/src 使用 /Users/mori/code/crypto/.venv/bin/python runner.py --output <不存在的新目录> --take-profit-bps 2 --stop-loss-bps 2 复现。

paper_snapshot.py与risk_policy.py为独立执行版本，原gross版及旧回放不改；provenance.json、verification.json和正式run_metadata.json记录来源、测试和执行哈希。最终通过账务/导出、原始一档、风险触发三项独立验收后生成report.md和comparison.json。64项执行/适配测试包含非零费用精确±2bp边界、手续费导致净止损、毛利正但不能净止盈、分批退出、优先级和回滚。

仍为双腿同时理想成交，未模拟真实网络延迟、单腿失败、异步部分成交、队列或mark price强平；合约数量规则是10月9日快照情景。单日单币结果不证明实盘收益。

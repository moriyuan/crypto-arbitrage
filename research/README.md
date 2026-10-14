# 历史回放源码和结果汇总

`arb-runs/` 保留13组原研究源码目录，以及历史准入的 collect、build_gate、audit 三个辅助源码；另外保留若干 Markdown 报告和最新比较的少量汇总。没有上传原始行情、逐笔账本、压缩特征、编译二进制或 Freqtrade 安装目录。

最新入口说明见 `arb-runs/20261013-btw-microstructure-gate-code/README.md`，当前研究结论以[研究状态](../docs/arbitrage-research.md)及最新结果目录中的 `scope-amendment.json` 为准。原始 `protocol.json` 保留了后来已排除的持续时间组，不能直接重新作为当前候选运行。

三个 `perfect_taker.cpp` 的共享 reader include 已改为相邻目录的相对路径；例如可以在仓库根目录编译最新理想撮合内核：

```bash
c++ -std=c++20 -O2 \
  research/arb-runs/20261013-btw-microstructure-gate-code/perfect_taker.cpp \
  -o /tmp/crypto-arbitrage-perfect-taker
```

Python 档案中保留了一些历史绝对路径、已冻结参数与数据契约，因此不是开箱即用的端到端复现程序。需要分别准备有权使用的历史数据、编译当前平台二进制、配置输入路径、生成新的输入和代码哈希清单，并使用新的输出目录。不能复用原目录覆盖历史证据，不能跳过缺口、资金费或哈希校验。

已发布的历史 JSON 审计清单保留原文件路径和原始哈希，用于解释当时结果；它们不是对本次发布副本的哈希认证。发布副本的映射另见[发布清单](../publication-manifest.json)。C++ include 的路径适配不修改策略逻辑，但源码哈希与原审计记录不同。

逐组合 `attribution.json`、完整 `comparison.json`、订单/组合/权益 CSV 及引用的大文件仅留在本地研究档案。历史报告内提到这些文件时，不能据此假定它们已上传。

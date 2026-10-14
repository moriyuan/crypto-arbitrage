# Bybit 与 Bitget 跨所永续套利研究

公开行情采集、双腿模拟协调、逐事件回放与研究审计。当前研究标的是 BTWUSDT，研究进度和下一步方向见[研究状态](docs/arbitrage-research.md)。这是从原研究工作区提取的独立项目，不包含 BTC CTA 主项目。

当前冻结基准使用两所对手一档、零额外执行延迟的理想吃单回放；每笔手续费0.03%。新增波动率过滤关闭，价差持续时间不作为入场条件。盘口和成交速率候选尚未晋级。理想成交结果不能证明实盘可实现利润。

## 安装与测试

需要 Python 3.12。核心模块只依赖 httpx、numpy 和 websockets。

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest tests
.venv/bin/python -m btc_cta.cross_arb --help
```

`btc_cta` 仅保留历史包命名以兼容现有模块入口，包内只包含跨所套利功能。测试使用本地合成数据和模拟客户端；两个真实 Freqtrade schema 集成测试需要单独配置运行环境，否则跳过。

## 代码与证据

|目录|内容|
|---|---|
|`src/btc_cta/cross_arb/`|行情解析、盘口、费用、深度定仓、模拟账本、回放与 Freqtrade 协调器|
|`deploy/freqtrade-arb/`|两所 dry-run 模板、受限运行器和策略；不包含运行凭据或 Freqtrade 安装包|
|`tests/`|跨所模块和运行器测试|
|`docs/arbitrage-research.md`|当前参数、已停止条件、回放结论和下一步研究计划|
|`docs/research/`|此前研究及运行手册，明确标记为历史记录|
|`research/arb-runs/`|原研究代码与少量结果汇总；使用前阅读[源码档案说明](research/README.md)|
|`publication-manifest.json`|来源及发布副本哈希、排除范围和路径适配记录|

旧 Freqtrade 模板与命令保留早期 BTC 等参数，不能作为当前 BTW 基准直接启动。实时观察和旧挂单协调器，与最新离线理想吃单实验是不同入口。此发布没有自动启动任何行情订阅、仿真或真实交易。

## 数据与凭据

原始行情、第三方历史数据、逐笔交易账本、特征数组、二进制行情和私密运行配置不随仓库上传。复现历史结果需要自行取得相应数据使用权限、准备输入并重建路径和哈希清单；不能通过删除核验绕过缺失数据。

交易所 Key、Secret、Passphrase 和本地 API 认证文件不得提交。`.gitignore` 排除 `.env`、`state/`、数据库、私钥、原始压缩行情和构建产物。Freqtrade 运行器和策略只允许 dry-run；当前研究档案 `execution_enabled=false`。

仓库未指定开源许可证。Freqtrade 和其他依赖继续适用各自许可证。

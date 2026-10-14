# BTW Bitget现货一分钟原生数据探测

结果：**PASS**。只请求2026-10-01 00:00–00:01 UTC一次，HTTP 200，无API密钥、无付费权限、无模拟或真实订单。响应76,444字节，gzip CRC通过；SHA256：`f091745b1bec4b63a02275cb5a8da90da9caedbb14f86fcdea85b8b928269d39`。

真实频道身份均为`instType=spot`、`symbol=BTWUSDT`。共1038个原始消息行：2条订阅确认，`books`初始snapshot 1条、update 995条；`publicTrade`历史首包snapshot 1条、update 39条。

初始盘口有863档卖价、1000档买价，`maxdepth=1000`；买一1.32384、卖一1.324564。初始seq=64621671190、pseq=0；随后995条增量的pseq全部接上前序seq，结束seq=64621974486。按绝对数量增删和最大深度重建后，没有空盘口或交叉盘口；没有空行断线标记。

分钟内实时逐笔成交 **49笔**（唯一ID 49）；历史首包 **50笔**另计，其中46笔发生在分钟开始前。历史首包不可重复计入实时订单流。真实成交字段为`i`交易ID、`p`价格、`v`数量、`S`方向（buy/sell）、`T`交易所成交毫秒、`L`关联ID和`isRPI`。方向计数：`{'historical_snapshot:buy': 29, 'historical_snapshot:sell': 21, 'live:buy': 46, 'live:sell': 3}`。

原始接收时间范围：`2026-10-01T00:00:00.2076604Z` 至 `2026-10-01T00:00:59.9694215Z`，保留100ns文本精度，未用下载时间替代历史时间。所有帧/盘口/成交源时间均不晚于其接收时间。该分钟盘口接收间隔中位数52.3479ms、P95 115.6107ms、最大663.5903ms；这些是行情采集时钟，不能作为策略下单延迟。

币种由[Bitget官方现货首发公告](https://www.bitget.com/support/articles/12560603859567)核对为Bitway，BEP20合约`0x444045B0EE1ee319A660a5E3d604CA0ffA35ACaA`，2026-03-02 09:00 UTC上线。行情消息自身不带链上合约地址，不能仅凭BTW字符串把其它同名资产接入。

该样本证明此时段现货原生L2和成交可取得，**没有证明整日完整性、现货对永续的领先关系、maker成交率或收益**。后续需独立现货适配器（现有永续适配器锁定usdt-futures）、全日因果对齐和独立maker队列/部分成交/延迟模型；不得把一档容量预算直接改称挂单成交概率。未开始整日下载。

[原始响应](/Users/mori/code/crypto/state/arb-runs/20261012-btw-spot-anchor-probe/bitget-spot-BTWUSDT-20261001T0000-1m.jsonl.gz) · [请求URL、状态和哈希](/Users/mori/code/crypto/state/arb-runs/20261012-btw-spot-anchor-probe/request.json) · [结构与时钟验证](/Users/mori/code/crypto/state/arb-runs/20261012-btw-spot-anchor-probe/validation.json) · [探测脚本](/Users/mori/code/crypto/state/arb-runs/20261012-btw-spot-anchor-probe/probe.py) · [离线验证脚本](/Users/mori/code/crypto/state/arb-runs/20261012-btw-spot-anchor-probe/validate_sample.py)

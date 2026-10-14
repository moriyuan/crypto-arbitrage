# BTW Bitget 现货原生行情归档与 BBO 重建

归档与重建已完成；独立逐行导出验收为 **PASS_WITH_EXPLICIT_INVALIDATIONS**。下方 `FAIL` 指的是“整日从未失效”的连续性检查不成立，不是文件下载损坏：6次日内重连边界和1次日末空行结束标记都已保守输出失效状态。

结果：FAIL。档案完整性：PASS；已处理 1440/1440 分钟。全日连续性：NOT_CONTINUOUS。这两个检查区分下载损坏/缺失与交易所来源断线、失效；无效时输出空报价，真实 snapshot 后恢复，没有跨缺口补值。

原始消息 1,690,427 条，盘口 snapshot 7、update 1,590,719；实时逐笔成交 146,628 笔，历史首包 350 笔另计。来源空行断线 7 次，pseq 不匹配 0 次，已验证链接 1,590,719。缺分钟：[]。

首条接收时间整数 ns：1790812800207660400；最后：1790899199961452800。有效 BBO 区间 7 段，合计 86397.633859 秒；起点至首个有效 snapshot 的无盘口前缀 0.2475678 秒。有效只表示序列重建成立，新鲜度需要另行筛选。

`bbo.csv.gz` 按原始接收顺序逐帧输出。`local_recv_ns`、`exchange_ts_ms`/`exchange_ns` 是整数；`bid/ask/bid_size/ask_size` 保留十进制定点文本。`valid/reset/disconnect` 与 `invalid_reason` 标识状态。`book_local_recv_ns/book_exchange_ns` 是当前盘口自己的更新时间；成交或订阅帧不会刷新它。以 500ms 时点合并永续时，只能使用该时点之前已接收到的事件，并检查盘口自己的年龄。`source_offset/source_line/seq/pseq` 可追溯原档案。

`trades.csv.gz` 另保留逐笔原始方向、价格、数量和交易 ID；`is_historical=1` 的 snapshot 不属于新到实时成交。空行没有原生时间戳，使用保守的此前接收时间；缺分钟使用分钟边界；两者均在 `timestamp_basis` 明示，不能当作原生时间证据。

资源：压缩派生产物 48,208,106 字节，目录约 163,826,908 字节，低于 1GB 限额。重建耗时 50.581 秒。

币种依据 [Bitget Bitway 官方现货上币公告](https://www.bitget.com/support/articles/12560603859567) 核对；行情自身只有 `spot/BTWUSDT` 标识。本轮没有模拟或真实订单，没有验证现货锚定价能力、maker 成交率或收益。

[原档案目录](/Users/mori/code/crypto/state/arb-runs/20261012-btw-spot-anchor-day/raw) · [每分钟来源证据](/Users/mori/code/crypto/state/arb-runs/20261012-btw-spot-anchor-day/provenance) · [下载清单](/Users/mori/code/crypto/state/arb-runs/20261012-btw-spot-anchor-day/download-manifest.json) · [验证详情](/Users/mori/code/crypto/state/arb-runs/20261012-btw-spot-anchor-day/validation.json) · [BBO](/Users/mori/code/crypto/state/arb-runs/20261012-btw-spot-anchor-day/bbo.csv.gz) · [逐笔成交](/Users/mori/code/crypto/state/arb-runs/20261012-btw-spot-anchor-day/trades.csv.gz)

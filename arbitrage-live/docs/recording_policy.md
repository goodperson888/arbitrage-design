# 实盘记录与磁盘策略

`dataRecordingEnabled` 只控制历史回放数据是否落盘，不会停止后台 Market WebSocket、Binance WebSocket 或策略内存判断。

暂停高频记录时，runner 仍维护一个有界的 `runtime_state.json`（只覆盖最新状态，不是回测流），因此页面仍可显示最新行情；它最多保留最近 60 条紧凑摘要，盘口只保留前 10 档。

默认策略（`config/recording_policy.json`）：

- raw 原始 WS 消息：每个来源每 5 秒采样一条；市场切换、成交和结算类事件保留。
- events 盘口：每 5 秒写一份，保留前 10 档；每秒策略快照仍保留买一卖一、深度和 5 分钟 Binance 特征。
- signals：同一市场、同一拒绝原因每秒最多一条；接受信号全部保留。
- health/sessions：低频全部保留。
- orders/fills/settlements：关键账本同步写入，永不采样、永不因高频记录暂停而丢弃。

现有历史目录不会被自动删除。`retention_days` 只作为后续显式清理策略的配置，不会在后台静默删除数据。

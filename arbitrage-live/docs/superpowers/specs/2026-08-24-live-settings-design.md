# 实盘设置与 Polygon 账户识别设计

## 目标

让本地 `8766` 实盘控制台可视化填写非敏感配置、在 macOS Keychain 保存私钥、通过 Polygon RPC 判断账户类型，并在预检通过后生成可供 runner 使用的运行配置。

## 安全边界

- 控制台只绑定 `127.0.0.1`；设置接口只服务本机。
- 私钥永不写入仓库、监控目录、SQLite、NDJSON、日志或 `/api/state`。
- 私钥写入 Keychain 时使用 `security` CLI；读取失败时只显示未配置，不回显异常内容。
- 普通配置写入 `data/live_trading/ui_settings.json`，只保存策略和连接参数，不保存秘密。
- Polygon 账户识别只读取地址的 `eth_getCode`，不使用私钥签名或发交易。
- UI 的启动按钮只启动已通过预检的 runner，不提供下单按钮。

## 账户分类

- `EOA`: `eth_getCode(address)` 返回 `0x` 或空代码；建议 `signature_type=0`，资金账户为该地址。
- `CONTRACT_PROXY`: 返回非空合约代码；必须填写/确认 `funder`，默认建议 `signature_type=1`，最终以账户实际部署方式为准。
- `INVALID`/`UNKNOWN`: 地址格式、RPC 或网络检查失败，不允许启动实盘。

## 配置流程

1. GET `/api/settings` 返回非敏感设置与 `privateKeyConfigured` 布尔值。
2. POST `/api/settings` 校验并保存普通设置；私钥字段只写 Keychain。
3. POST `/api/account/detect` 对地址执行 Polygon RPC `eth_getCode`，返回分类、代码是否存在、建议签名参数。
4. GET `/api/preflight` 汇总 Keychain、账户分类、只读网络、订单开关、风险参数。
5. UI 只有预检全部通过后才启用“启动实盘”。


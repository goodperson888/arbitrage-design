# 第四阶段：可选 CLOB SDK transport（默认关闭）

实现依据 Polymarket 官方 `py-clob-client-v2` 接口：L1 使用 Polygon `chain_id=137` 与私钥初始化 `ClobClient`，L2 使用 `create_or_derive_api_key()` 或 `ApiCreds(api_key, api_secret, api_passphrase)`。订单使用模块导出的 `OrderArgs` 类型（不能从 `ClobClient` 实例查找，也不能传字典代替），先调用 `create_order` 签名，再单独调用 `post_order(..., OrderType.FAK)`。FAK 是有价格上限的立即成交订单，剩余未成交部分取消；无需使用隐藏了发送边界的 `create_and_post_*` 组合助手。查询使用 `get_order`，撤销使用 `cancel_order`。[官方客户端实现](https://github.com/Polymarket/py-clob-client-v2/blob/main/py_clob_client_v2/client.py)

CLOB V2 的抵押品是 pUSD，不再是旧版 USDC.e。每个市场的手续费由 `get_clob_market_info(condition_id).fd` 动态返回；runner 必须在构造 FAK 前取得 `rate/exponent`，按 `fee = quantity × rate × (price × (1-price))^exponent` 反推数量，并把本次含费成本作为 SDK `user_usdc_balance`。查询失败或字段不完整会立即停单，不能回退到研究固定费率。[官方 V2 迁移说明](https://docs.polymarket.com/v2-migration)

`py-clob-client-v2` 和 `websockets` 都是可选依赖，不安装时 observe/read-only 链路仍可运行。

## 双重/三重安全开关

真实 `PyClobClientTransport.submit()` 只有在以下条件同时满足时才会进入 SDK 调用：

1. 配置 `transport_enabled=true`；
2. 构造 transport 时显式传入 `preflight_passed=True`；
3. 环境变量 `LIVE_TRADING_ENABLE=1`。

当前仓库的配置仍是 `transport_enabled=false`，所以即使环境变量误设，也会被拒绝。默认命令不加载 SDK、不读取私钥、不发网络订单。

## 凭据

- L1：`POLYMARKET_PRIVATE_KEY`，由页面写入 macOS Keychain 后只在运行时读取；
- L2：`POLYMARKET_API_KEY`、`POLYMARKET_API_SECRET`、`POLYMARKET_API_PASSPHRASE`，兼容官方示例中的 `CLOB_API_KEY`、`CLOB_SECRET`、`CLOB_PASS_PHRASE`；页面“配置并派生身份”会通过官方 SDK 自动派生并加密保存到 Keychain；
- `funder` 与 `signature_type` 由页面按实际钱包身份选择。钱包登录的专用 EOA 通常为 `signature_type=0`，邮箱 Proxy/Safe/deposit wallet 分别使用 1/2/3；身份派生不依赖 Polygon RPC；
- 禁止文件路径和日志明文。

## 超时、幂等和对账

每个 SDK 调用都使用独立超时，不自动重试。初始化、参数构造、元数据读取或签名阶段失败时尚未进入 `post_order`，记录为 `REJECTED`，附带 `submission_error.phase=prepare/requestSent=false/reconcileRequired=false`。立即释放没有成交的风险占用，不消耗 canary 真实订单额度；停止新订单并在页面显示“订单未发送”，修复配置后重新启动即可。这类超时与提交超时必须区别处理。

进入 `post_order` 后发生超时、断网、HTTP 408/5xx 或无法识别的响应时，记录为 `UNKNOWN`、关闭新订单并要求官方对账；不能假定未发送。脱敏后的异常类型、阶段和原因持久化到 `submission_error`，监控 `ORDER_UNKNOWN` 同时记录 `clientOrderId/submissionError`。旧 UNKNOWN 缺少发送证据，修复代码不会自动清除它们。

FAK 请求沿用确定性 client order id；SQLite 以 `intent → submitting` 条件更新原子认领请求，只有首次认领成功才能进入 transport。重启、并发调用或重复调用处于 submitting/acknowledged/partial/unknown 的请求均不得重新提交。终态请求返回既有结果。同一市场多条记录只要仍有一条可能持仓，就不能因为另一条被拒而释放占用。已撤销订单若已部分成交，持仓仍保留至结算。

SDK 返回带 HTTP 4xx（不含 408）的权威拒单（例如没有可匹配卖盘、余额/授权不足、tick 或最小数量不合法）会记录为终态 `REJECTED`，保留状态码和脱敏后的 provider message；释放无成交的风险占用，但已发送的拒单仍消耗 canary 的一次额度。因此页面可以区分“未发送”“确实被拒”与“需要对账”。

## 盘口深度与滑点

构造请求时将卖盘按价格升序消耗，允许跨多档，但只纳入 `price ≤ max_entry_price`（默认 0.85）的档位。请求预期成本必须达到最低有效金额（默认 2.5U），且含手续费不超过单笔预算（默认 5U），否则在本地拒绝，不发送订单。不会因盘口不足自动追到 0.86 或更高，也不会自动重试。

签名始终携带明确的 `price=limit_price` 和 `user_usdc_balance=expected_cost`；V2 SDK 必要时缩减 BUY 份数，将限价成交的费用纳入该预算。0.82 的卖盘在提交前变到 0.84 时仍可能成交；变到 0.86 时不会越过 0.85 限价追价。提交后 FAK 允许部分成交；实际成交不足 2.5U 仍可靠记账，但不进入正式收益统计。

点击停止时，dashboard 先关闭新订单开关，再向 runner 发送中断信号，让其执行正常的收尾和对账；只有超时未退出才强制终止。这样不会因为停止动作本身制造新的提交中间态。

页面选择 canary/live 后仍必须通过认证预检：SDK、L1/L2 身份、USDC 余额、allowance、远端未结订单、本地恢复状态、CLOB closed-only 状态以及两路行情新鲜度任一失败都会阻止启动。canary 每次进程会话最多一笔订单。

FAK 返回 `matched` 不等于假定完整成交；订单先进入已确认状态，再按精确 trade ID 去重并汇总实际数量、价格、手续费和成本。结算只使用实际 fill 汇总，低于 `2.5U` 的零碎成交保留事实记录，但不进入正式亏损熔断。

测试使用 mock SDK 验证状态边界、超时、失败注入、并发认领和重启恢复。`tests/test_clob_sdk_contract.py` 额外使用安装的真实 SDK 类型、签名器和 HMAC，通过 `httpx.MockTransport` 拦截全部 HTTP 请求，检查最终 FAK 的限价和含费预算；只使用合成凭据，不发真实订单。未安装 live 依赖时该用例明确跳过。

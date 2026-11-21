# S4：Maker 成交后跨平台对冲

## 1. 策略目标

在价差较大或流动性较薄的平台挂被动单，降低手续费并获取更好价格；只有第一腿实际成交后，才在流动性更好的平台执行反方向对冲。

这是 S1 和 S3 的执行增强层，不改变结算覆盖逻辑。

## 2. 适用场景

- 第一平台支持限价 maker；
- 第二平台具备可靠 API、实时订单簿和足够深度；
- 两市场已通过 `EXACT` 或 `COVERED` 验证；
- 第一腿订单可快速撤销；
- 第二腿允许 IOC/FOK 或可模拟等价保护。

## 3. Maker 报价计算

假设准备在平台 A 买 Up，平台 B 当前 Down 对冲成本为 `h(q)`：

```text
max_maker_price(q)
= worst_case_payout_per_share
- h(q)
- fees(q)
- gas(q)
- required_profit(q)
- hedge_delay_buffer(q)
```

maker 买价必须小于等于 `max_maker_price`。

示例：

```text
B Down 对冲 ask = 0.30
全部费用及延迟缓冲 = 0.015
要求利润 = 0.02
```

```text
A Up 最大 maker 买价
= 1 - 0.30 - 0.015 - 0.02
= 0.665
```

可挂 `0.66`，不可挂 `0.68`。

## 4. 动态撤单

maker 单存续期间，系统持续更新第二腿的可执行成本。

若：

```text
maker_order_price + current_hedge_cost + buffers >= 1
```

必须撤单。

需要处理“撤单与成交竞态”：撤单请求成功返回前，订单仍可能成交。撤单后必须查询最终成交数量，再决定是否对冲。

## 5. 状态机

```text
IDLE
  -> QUOTING
  -> MAKER_WORKING
  -> MAKER_PARTIAL -> HEDGING_PARTIAL
  -> MAKER_FILLED -> HEDGING
  -> HEDGED
  -> SETTLED

MAKER_WORKING -> CANCEL_PENDING -> CANCELLED
CANCEL_PENDING -> LATE_FILL -> HEDGING
HEDGING -> HEDGE_PARTIAL -> RECOVERY
HEDGING -> HEDGE_REJECTED -> RECOVERY
```

每次状态变化必须持久化后才能进行不可逆的下一步。

## 6. 部分成交处理

示例：maker 委托 100 张，成交 37.5 张。

系统必须：

1. 立即为 37.5 张生成对冲单；
2. 剩余 62.5 张继续挂单或撤单，取决于当前价差；
3. 不得一次对冲 100 张；
4. 若随后再成交 10 张，再独立对冲 10 张；
5. 允许按短时间窗口聚合微小成交，但聚合期间的最大裸露数量必须受限。

## 7. 对冲路由

对每个候选对冲平台计算：

```text
hedge_score
= executable_cost
+ fee
+ expected_slippage
+ latency_penalty
+ reject_risk_penalty
+ venue_exposure_penalty
```

选择分数最低且通过规则匹配的平台。

系统允许把一次第一腿成交拆到多个对冲平台，但必须避免重复对冲。

## 8. 恢复策略

第二腿失败后，按配置顺序评估：

1. 在备选平台完成相反结果对冲；
2. 在第一平台平掉第一腿；
3. 使用已有结果份额库存对冲；
4. 若以上都不可行，标记裸方向仓位并触发人工告警；
5. 不得用现货/永续伪装成完整的二元结果对冲。

恢复动作选择最小化：

```text
worst_case_loss + execution_cost + operational_risk
```

## 9. Kill Switch

以下情况停止新挂 maker，但继续处理已有成交：

- 对冲行情断流；
- 用户成交流断流；
- 订单簿序列号缺口；
- 第二平台延迟超过上限；
- 拒单率超过上限；
- 第一平台撤单超时；
- 余额或 allowance 不足；
- 距到期或停止接单时间过近；
- 已有裸露量超过限额；
- 结算规则或费用变更。

## 10. 指标

- maker 挂单时长；
- maker 成交率；
- maker 撤单率；
- 撤单后迟到成交数；
- 成交到对冲提交延迟；
- 成交到完全对冲延迟；
- 对冲滑点分布；
- 部分成交率；
- 单腿最大数量和持续时间；
- 恢复成本；
- maker 费用节省与返利；
- 最终净利润。

## 11. 回测要求

需要事件驱动回放，至少包含：

- 订单簿更新；
- 自己订单排队位置；
- 成交事件；
- 撤单确认；
- 网络延迟分布；
- 第二腿盘口在延迟期间的变化。

不能假设只要市场触及 maker 价格就全额成交。

## 12. 验收标准

1. 上述 `0.665` 最大 maker 价格计算正确；
2. 对冲价格恶化时自动撤单；
3. 撤单与迟到成交竞态不会漏对冲；
4. 部分成交只按实际成交量对冲；
5. 重启后能识别所有未完成 HedgeGroup；
6. Kill Switch 不会阻止已有仓位的风险处置；
7. 故障注入后不存在无记录的裸方向仓位。


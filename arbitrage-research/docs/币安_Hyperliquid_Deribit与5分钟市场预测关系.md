# 币安、Hyperliquid、Deribit 与 BTC 5分钟涨跌市场的预测关系

更新时间：2026-08-22  
文档状态：研究设计已完成，跨市场预测效果尚未完成正式回测  
适用市场：Polymarket BTC 5分钟 `UP / DOWN` 市场  
用途：数据采集、特征设计、回测、实时纸上监控和后续 AI 交接  

## 一、最重要的结论

币安、Hyperliquid 和 Deribit 都可以为 BTC 5分钟涨跌市场提供信息，但它们的作用不同：

| 数据源 | 最适合提供的信息 | 对5分钟方向的作用 | 建议定位 |
|---|---|---|---|
| 币安现货 | 实际成交价格、短周期K线、成交量和主动买卖方向 | 最直接的短线价格动量参考 | 主要方向信号 |
| Hyperliquid BTC永续 | 永续合约中间价、主动成交、盘口、持仓量和标记价/预言机价偏离 | 杠杆资金的方向、追涨杀跌和强平压力 | 主要确认信号 |
| Deribit BTC期权 | 看涨/看跌波动率偏斜、隐含波动率、期权盘口、BTC指数和永续价格 | 方向偏好较慢，但能判断尾部风险和市场是否容易发生反转 | 辅助方向与风险信号 |
| Chainlink BTC/USD | Polymarket指定的结算价格源 | 决定最终 `UP / DOWN` 标签 | 唯一结算基准 |
| Polymarket盘口 | 市场参与者已经给出的概率和真实买入成本 | 决定是否存在交易价值 | 概率基准与执行价格 |

推荐的信息层级是：

1. 先判断 BTC 当前相对本轮5分钟起点的位置；
2. 再用币安和 Hyperliquid 判断短线方向是否一致；
3. 用 Deribit 判断期权市场是否支持该方向，以及当前是否处于高反转风险环境；
4. 最后和 Polymarket 的实际买入价格、手续费、盘口深度比较；
5. 只有预测概率高于实际成本，才可能存在正期望。

这些关系目前是需要回测验证的研究假设，不是已经验证可直接下单的策略。

## 二、Polymarket 5分钟市场到底预测什么

设每个市场的开始时间为 `S`，结束时间为 `E = S + 300秒`。

Polymarket BTC 5分钟市场的正式标签是：

- 如果 Chainlink BTC/USD 在结束时的价格大于或等于开始时的价格，结果为 `UP`；
- 如果结束价格低于开始价格，结果为 `DOWN`。

写成公式：

```text
UP   ：ChainlinkPrice(E) >= ChainlinkPrice(S)
DOWN ：ChainlinkPrice(E) <  ChainlinkPrice(S)
```

因此真正要预测的不是“未来5分钟收益率有多大”，而是：

> 到窗口结束时，BTC 是否仍在本轮起点价格之上。

在任意观察时间 `t`，最重要的状态量是：

```text
当前距离 = 当前参考价格 - 本轮起点参考价格
剩余时间 = E - t
```

同样是上涨 20 美元：

- 剩余 30 秒时，守住起点的概率通常较高；
- 剩余 240 秒时，仍有较长时间发生反转；
- 当前波动率越高，20 美元的领先越不可靠；
- 当前波动率越低，20 美元的领先越有意义。

币安、Hyperliquid 和 Deribit 的所有特征，最终都应当围绕“当前距离、剩余时间、短线方向、反转风险”解释。

## 三、外部价格不能替代官方结算价格

必须严格区分：

- `预测输入`：币安、Hyperliquid、Deribit 的价格、成交、盘口和期权指标；
- `正式标签`：Polymarket 官方结算结果，或与市场规则一致的 Chainlink 起止价格；
- `交易成本`：Polymarket 对应方向的实际卖一价、盘口深度和手续费。

不能使用以下错误口径：

```text
错误：币安5分钟K线收涨，因此把该市场标记为UP。
正确：市场仍以Chainlink起止价格决定UP/DOWN；币安收涨只能作为预测特征。
```

原因是不同平台存在价格偏差、更新时间差、指数构成差和短暂脱锚。大多数时候方向相同，但边界附近的几美元差异就可能改变5分钟市场结算。

## 四、统一时间对齐方法

所有数据源必须先对齐到同一个市场窗口，才能讨论预测能力。

### 4.1 市场主键

建议以 Polymarket 市场开始时间作为主键：

```text
market_id          = btc-updown-5m-{start_unix_seconds}
market_start_us    = S
market_end_us      = S + 300秒
```

不要通过页面显示的时区文字对齐，必须使用 Unix 时间戳。

### 4.2 固定观察点

每个市场建议至少保存以下观察点：

| 距离结算 | 市场已经运行 | 主要用途 |
|---:|---:|---|
| 240秒 | 60秒 | 早期方向形成 |
| 180秒 | 120秒 | 中期动量 |
| 120秒 | 180秒 | 与历史两分钟研究衔接 |
| 100秒 | 200秒 | 已完成历史补测的时间点 |
| 90秒 | 210秒 | 当前纸上候选窗口起点 |
| 60秒 | 240秒 | 主候选窗口中段 |
| 30秒 | 270秒 | 当前候选窗口终点 |

当前研究明确排除最后30秒作为主要入场依据，但仍可以保存数据用于诊断。

### 4.3 严禁未来数据泄漏

在观察时间 `t` 生成特征时，只能使用：

```text
source_time <= t
received_time <= 当时本机决策时间
```

特别注意：

- 不能在剩余90秒时使用整根已经结束的当前5分钟K线收盘价；
- 不能用市场结算后的期权、盘口或成交数据回填入场特征；
- 不能用同一个市场后续30秒的数据判断90秒时是否应该入场；
- 训练集和测试集必须按时间切分，不能随机把相邻市场混在一起；
- 同一市场的多个观察点必须全部归入同一个训练或测试分组。

## 五、币安K线与5分钟方向的关系

### 5.1 币安提供什么

币安 BTCUSDT 现货K线包含：

- 开盘价、最高价、最低价、当前/收盘价；
- 基础资产成交量和计价资产成交额；
- 成交笔数；
- 主动买入成交量和主动买入成交额；
- K线是否已经结束；
- K线开始和结束时间。

对于当前5分钟预测，推荐同时记录：

```text
btcusdt@kline_1m
btcusdt@kline_5m
btcusdt@aggTrade
btcusdt@bookTicker 或深度流
```

仅使用5分钟K线的最终收盘值不够，因为它在 Polymarket 同一窗口结束后才确定。实时预测需要保存尚未结束的K线更新，或者使用已完成的1分钟K线和实时成交。

### 5.2 建议计算的币安特征

#### 当前相对起点收益

```text
binance_return_from_start(t)
  = ln(BinancePrice(t) / BinancePrice(S))
```

它通常是最直接的方向信号：

- 大于0，倾向支持 `UP`；
- 小于0，倾向支持 `DOWN`；
- 非常接近0，结果对微小价格变化和跨平台价差敏感。

#### 1分钟和短周期动量

```text
return_15s
return_30s
return_60s
return_180s
```

用途：区分“本轮虽然仍在起点上方，但最近已经快速下跌”和“价格领先且仍在继续上涨”。

例如：

| 当前状态 | 解释 |
|---|---|
| 距离起点为正，最近60秒也上涨 | UP趋势较一致 |
| 距离起点为正，最近30秒快速下跌 | UP仍领先，但反转风险增加 |
| 距离起点为负，最近60秒继续下跌 | DOWN趋势较一致 |
| 距离起点为负，最近30秒快速反弹 | DOWN仍领先，但反转风险增加 |

#### K线实体强度

```text
candle_body = close - open
body_ratio  = (close - open) / max(high - low, 最小价格单位)
```

- `body_ratio` 接近 `+1`：价格接近区间高位，买方占优；
- `body_ratio` 接近 `-1`：价格接近区间低位，卖方占优；
- 接近0：来回震荡，方向不明确。

#### 收盘位置

```text
close_location = (close - low) / max(high - low, 最小价格单位)
```

- 接近1：当前价格靠近短周期最高位；
- 接近0：当前价格靠近短周期最低位。

#### 上下影线

```text
upper_wick = high - max(open, close)
lower_wick = min(open, close) - low
wick_bias  = (lower_wick - upper_wick) / max(high - low, 最小价格单位)
```

- 长上影可能表示上涨受到抛压；
- 长下影可能表示下跌受到承接；
- 影线只用于判断反转风险，不能单独作为方向信号。

#### 主动买入比例

```text
taker_buy_ratio = taker_buy_quote_volume / total_quote_volume
```

- 高于近期正常水平，支持买方主动；
- 低于近期正常水平，支持卖方主动；
- 不建议固定用0.50作为唯一阈值，应使用最近1小时或最近1天的分位数标准化。

#### 成交活跃度

```text
volume_zscore
trade_count_zscore
```

同样幅度的价格移动，如果成交量和成交笔数明显放大，通常比低成交量移动更可信。但放量也可能来自强平或短暂新闻冲击，因此需要和 Hyperliquid、Deribit 一起判断。

### 5.3 币安特征的推荐解释

| 币安状态 | 对UP的影响 | 对DOWN的影响 |
|---|---|---|
| 当前价明显高于本轮起点 | 支持 | 反对 |
| 当前价明显低于本轮起点 | 反对 | 支持 |
| 最近15–60秒持续上涨 | 支持 | 反对 |
| 最近15–60秒持续下跌 | 反对 | 支持 |
| 主动买入比例显著升高 | 支持 | 反对 |
| 主动卖出占优 | 反对 | 支持 |
| 距离起点很小且波动放大 | 降低置信度 | 降低置信度 |
| 价格和成交量背离 | 增加反转风险 | 增加反转风险 |

### 5.4 当前项目的币安代码缺口

项目已经有币安回填连接器和实时录制器，但当前实时录制器只在K线关闭后写入K线事件。对于同一轮5分钟市场的实时预测，这会丢失尚未收盘K线内部的变化。

后续应补充：

- 未闭合1分钟/5分钟K线的增量记录；
- 实时成交和主动买卖方向；
- 最佳买卖价或深度；
- 每条数据的源时间和本机接收时间；
- 断线重连后的缺口记录。

## 六、Hyperliquid 涨跌与5分钟方向的关系

### 6.1 Hyperliquid 提供什么

Hyperliquid 的 BTC 主要是永续合约市场。官方 WebSocket 可订阅：

- `candle`：1分钟、3分钟、5分钟等K线；
- `trades`：逐笔成交，包含方向、价格和数量；
- `bbo`：最佳买卖价；
- `l2Book`：盘口档位和数量；
- `allMids`：中间价；
- `activeAssetCtx`：标记价、中间价、预言机价、资金费率、持仓量等。

Hyperliquid 不是 Polymarket 的结算源。它的价值在于观察杠杆交易者是否正在用真实仓位追涨、追跌或被强平。

### 6.2 建议计算的Hyperliquid特征

#### 永续中间价相对起点收益

```text
hl_mid_return_from_start(t)
  = ln(HyperliquidMid(t) / HyperliquidMid(S))
```

解释与币安相同，但它反映的是永续市场而不是现货市场。

#### 标记价与预言机价偏离

```text
mark_oracle_basis
  = (markPx - oraclePx) / oraclePx
```

- 正值扩大：永续买方更激进，短线偏向支持UP；
- 负值扩大：永续卖方更激进，短线偏向支持DOWN；
- 极端偏离也可能意味着拥挤，随后出现均值回归或强平反转。

#### Hyperliquid 与币安现货价差

```text
hl_binance_basis
  = (hl_mid - binance_spot_mid) / binance_spot_mid
```

- Hyperliquid 明显领先上涨：杠杆市场可能先行做多；
- Hyperliquid 明显领先下跌：杠杆市场可能先行做空；
- 价差快速扩大后又回落：可能是短暂强平或流动性冲击，不应直接追单。

#### 主动成交方向

在固定滚动窗口内计算：

```text
hl_trade_imbalance
  = (主动买入金额 - 主动卖出金额)
    / (主动买入金额 + 主动卖出金额)
```

- 接近 `+1`：主动买入占优；
- 接近 `-1`：主动卖出占优；
- 需要分别测试15秒、30秒和60秒窗口。

#### 盘口不平衡

```text
hl_book_imbalance_N
  = (前N档买量 - 前N档卖量)
    / (前N档买量 + 前N档卖量)
```

推荐同时保存第1档和前5档结果：

- 第1档反应快但容易被撤单影响；
- 前5档更稳定，但反应稍慢；
- 盘口不平衡必须结合后续真实成交验证，不能假设挂单一定可靠。

#### 持仓量变化

```text
open_interest_change_1m
open_interest_change_5m
```

价格和持仓量组合可作以下解释：

| 价格 | 持仓量 | 常见解释 |
|---|---|---|
| 上涨 | 增加 | 新多仓或双边新增仓位，趋势可能更有持续性 |
| 上涨 | 减少 | 空头回补，可能是快速但短暂的上涨 |
| 下跌 | 增加 | 新空仓或双边新增仓位，跌势可能更有持续性 |
| 下跌 | 减少 | 多头平仓或强平，可能出现快速反弹 |

这只是解释框架，必须通过历史数据验证，不能把每种组合机械地当作买卖规则。

#### 资金费率

资金费率变化较慢，对单个5分钟方向通常不是主要信号。它更适合表示市场拥挤程度：

- 长期高正资金费率：多头拥挤，急跌风险可能更高；
- 长期高负资金费率：空头拥挤，急涨风险可能更高。

因此资金费率应作为背景状态，不应单独预测下一轮5分钟涨跌。

### 6.3 Hyperliquid特征的推荐解释

| Hyperliquid状态 | 方向含义 |
|---|---|
| 中间价、标记价和币安现货同步上涨 | 支持UP |
| 中间价、标记价和币安现货同步下跌 | 支持DOWN |
| 标记价高于预言机价且主动买入占优 | 支持UP，但极端偏离时注意拥挤反转 |
| 标记价低于预言机价且主动卖出占优 | 支持DOWN，但极端偏离时注意拥挤反转 |
| 币安上涨而Hyperliquid不跟 | UP信号减弱 |
| 币安下跌而Hyperliquid不跟 | DOWN信号减弱 |
| 两个平台方向相反 | 降低置信度，记录为跨市场分歧 |

## 七、Deribit期权与5分钟方向的关系

### 7.1 期权不能简单理解成“看涨期权涨就是BTC涨”

期权价格同时受到以下因素影响：

- BTC现货或指数价格；
- 隐含波动率；
- 剩余到期时间；
- 执行价与当前价格的距离；
- 利率、流动性和买卖价差；
- Delta、Gamma、Vega、Theta。

所以：

```text
看涨期权价格上涨
不一定等于
市场预测BTC下一轮5分钟会上涨
```

它也可能只是隐含波动率整体上升。Deribit 应当拆成“方向信息”和“波动/反转风险信息”。

### 7.2 建议采集的Deribit数据

#### BTC指数和永续合约

- `deribit_price_index.btc_usd`；
- `BTC-PERPETUAL` 的最新价、最佳买卖价、标记价、指数价、持仓量和成交量；
- 指数与永续的价差。

这部分与币安、Hyperliquid类似，可提供短线价格确认。

#### BTC期权

对每个选定合约保存：

- 到期时间、执行价、看涨/看跌类型；
- 最佳买卖价、中间价、标记价格；
- `bid_iv`、`ask_iv`、`mark_iv`；
- Delta、Gamma、Vega、Theta；
- 标的参考价和指数价；
- 成交量、持仓量和逐笔成交；
- 源时间、本机接收时间和盘口新鲜度。

#### DVOL或波动率指数

Deribit 波动率指数用于判断市场预期波动强弱。它主要回答：

> 接下来发生大幅波动的风险是否升高？

它不直接回答：

> 下一轮5分钟一定上涨还是下跌？

### 7.3 期权合约选择

建议同时构建三组：

1. 最接近当前BTC价格的平值期权；
2. Delta约为 `+0.25` 的看涨期权；
3. Delta约为 `-0.25` 的看跌期权。

到期选择不能永久写死。应当记录最近到期和次近到期，并回测排除“距离到期少于6小时、12小时、24小时”等不同门槛。

原因是临近到期时 Gamma 和 Theta 变化非常快，盘口也可能变差，容易把到期效应误认为5分钟方向信号。

### 7.4 建议计算的Deribit方向特征

#### Deribit指数相对本轮起点收益

```text
deribit_index_return_from_start(t)
  = ln(DeribitIndex(t) / DeribitIndex(S))
```

它是Deribit数据中最直接、最容易解释的方向特征。

#### 永续基差

```text
deribit_perp_basis
  = (perpetual_mid - index_price) / index_price
```

- 基差向上扩大：永续买方更激进；
- 基差向下扩大：永续卖方更激进；
- 极端基差同样可能代表拥挤，而不是可持续方向。

#### 25 Delta风险逆转

```text
RR25 = 25Delta看涨期权隐含波动率
       - 25Delta看跌期权隐含波动率
```

一般解释：

- `RR25` 上升：看涨期权相对更贵，市场上行保护或看涨需求增强；
- `RR25` 下降：看跌期权相对更贵，下行保护需求增强；
- 绝对值和变化量都要保存；
- 对5分钟市场，`RR25` 更适合作为慢速背景或反转风险信号，不应当单独作为入场方向。

#### 平值隐含波动率

```text
atm_iv
atm_iv_change_1m
atm_iv_change_5m
```

- IV上升：市场预期波动放大，当前领先方向更容易被反转；
- IV下降：市场预期波动收敛，当前领先方向可能更容易守到结算；
- IV本身不决定UP或DOWN。

#### 波动率微笑倾斜

```text
downside_skew = put_iv - atm_iv
upside_skew   = call_iv - atm_iv
```

- 下行偏斜快速扩大：下跌尾部保护需求增强；
- 上行偏斜快速扩大：上涨尾部需求增强；
- 必须控制到期时间和Delta，否则不同合约不可直接比较。

#### 看涨/看跌成交方向

不能只比较看涨成交量和看跌成交量。至少要进行：

- 买方主动或卖方主动识别；
- Delta标准化；
- Vega标准化；
- 到期时间和执行价分组；
- 名义金额统一。

例如主动买入看跌期权通常比“看跌期权总成交量增加”更能表达下行保护需求。

#### 特定行权价 OI 与主动资金流

OI 增加本身不能判断多空，因为每一张新增合约都同时有买方和卖方。当前实现因此不把
`OI上涨` 单独翻译成 UP 或 DOWN，而是联合检查：

```text
偏多确认 = Call主动买入资金占优 + 对应25D Call OI上升 + Call IV上升
偏空确认 = Put主动买入资金占优  + 对应25D Put OI上升  + Put IV上升
```

主动资金流使用 Deribit 公共期权成交的 `direction` 作为主动方方向，并按下面方式统一为美元权利金：

```text
premium_usd = option_price_btc × amount_btc × btc_index_price
bullish_flow = Call主动买入 + Put主动卖出
bearish_flow = Put主动买入 + Call主动卖出
flow_imbalance = (bullish_flow - bearish_flow) / (bullish_flow + bearish_flow)
```

这是“主动成交方向代理”，不是账户级净入金；做市商对冲、组合单和不同到期结构仍可能影响解释。
项目同时保存全 BTC 期权资金流，以及本次选中的近25D Call/Put资金流，避免把远月大单和近月信号混为一谈。

#### 期权隐含远期价格

可以通过同到期、同执行价的看涨与看跌价格关系估计隐含远期方向。但 Deribit 不同产品的报价货币、合约单位和结算方式必须先统一，不能直接套用未经换算的现货期权公式。

建议把隐含远期相对 Deribit 指数的偏离作为研究特征，不直接作为结算价格。

### 7.5 Deribit特征的推荐解释

| Deribit状态 | 对5分钟市场的意义 |
|---|---|
| BTC指数和永续同步上涨 | 支持UP |
| BTC指数和永续同步下跌 | 支持DOWN |
| RR25上升且指数上涨 | 增强UP确认 |
| RR25下降且指数下跌 | 增强DOWN确认 |
| IV/DVOL快速上升 | 增加双向反转风险，降低确定性 |
| IV下降且价格远离起点 | 当前领先方向更可能保持，但仍需回测 |
| 期权信号和现货/永续方向相反 | 记录为分歧，不强行追随任何一方 |
| 期权盘口陈旧或价差很大 | 该期权特征应标记缺失，不填0 |

## 八、三个外部市场如何组合

### 8.1 不建议一开始手工写死权重

不要未经回测直接规定：

```text
币安50% + Hyperliquid30% + Deribit20%
```

正确做法是先保存各自的原始特征，再通过时间顺序回测确定它们是否真的提供增量信息。

建议第一版使用可解释的逻辑回归：

```text
logit(P_UP) = β0
            + β1 × 币安相对起点收益
            + β2 × 币安短线动量
            + β3 × 币安主动成交不平衡
            + β4 × Hyperliquid相对起点收益
            + β5 × Hyperliquid成交不平衡
            + β6 × Hyperliquid盘口不平衡
            + β7 × Hyperliquid标记价/预言机价偏离
            + β8 × Deribit指数相对起点收益
            + β9 × RR25变化
            + β10 × ATM IV或DVOL变化
            + β11 × 剩余时间
            + β12 × 当前距离/近期波动率
```

然后：

```text
P_DOWN = 1 - P_UP
```

模型必须做概率校准。预测 `0.85` 应当在独立样本中大约有85%的事件为真，否则不能直接和 Polymarket 价格比较。

### 8.2 简单的一致性标签

在模型建立前，可以先做不下单的研究标签：

| 标签 | 条件解释 |
|---|---|
| `ALL_SUPPORT_UP` | 币安、Hyperliquid、Deribit方向特征均支持UP |
| `ALL_SUPPORT_DOWN` | 三者均支持DOWN |
| `SPOT_PERP_UP_OPTIONS_NEUTRAL` | 币安和Hyperliquid支持UP，期权没有明显方向 |
| `SPOT_PERP_DOWN_OPTIONS_NEUTRAL` | 币安和Hyperliquid支持DOWN，期权没有明显方向 |
| `OPTIONS_DIVERGENCE` | 期权方向与现货/永续相反 |
| `BINANCE_HL_DIVERGENCE` | 币安和Hyperliquid方向相反 |
| `HIGH_VOLATILITY_RISK` | Deribit IV/DVOL快速升高，反转风险增加 |
| `NEAR_START_PRICE` | 当前价格距离起点太近，结算方向脆弱 |
| `STALE_EXTERNAL_DATA` | 任一关键数据源过期或断线 |

阈值必须通过历史分位数或训练样本确定，不应凭感觉固定。

## 九、与当前0.80–0.85策略的关系

当前历史研究最支持：

```text
方向：DOWN
Polymarket价格：0.80–0.85
时间：距离结算90–30秒
确认：观察到单笔历史成交量≥10
```

外部市场数据第一阶段不应修改这个条件，只添加旁路标签：

```text
binance_direction
hyperliquid_direction
deribit_direction
external_consensus
external_divergence
distance_from_start_bps
realized_volatility
implied_volatility_regime
```

之后分别统计：

| 分组 | 要回答的问题 |
|---|---|
| 原条件全部信号 | 不加外部过滤的基准结果 |
| 原条件 + 三市场都支持DOWN | 一致性是否提高胜率和净收益 |
| 原条件 + 币安/Hyperliquid支持DOWN | 期权数据是否有额外价值 |
| 原条件 + Deribit下行偏斜增强 | 期权是否能减少反转失败 |
| 原条件 + 外部市场分歧 | 是否应该放弃或降低仓位 |
| 原条件 + 高IV | 高波动时失败率是否显著升高 |

在独立样本验证前，外部标签不得覆盖原策略、不得自动放行真实订单，也不得因为历史上某个标签表现好就删除原始未过滤样本。

## 十、预测概率如何与Polymarket价格比较

假设模型预测 `P_UP`，Polymarket 当前可买卖一价为 `Ask_UP`。

持有到结算、每份赢得1、输得0时，未计费用的期望值为：

```text
EV_UP_before_fee = P_UP - Ask_UP
EV_DOWN_before_fee = (1 - P_UP) - Ask_DOWN
```

加入手续费和真实成交均价后：

```text
EV_UP = P_UP - 实际UP成交均价 - 每份手续费
EV_DOWN = (1 - P_UP) - 实际DOWN成交均价 - 每份手续费
```

因此：

- 模型预测UP概率85%，但UP实际买入成本也是0.85，再加手续费，仍然不是好交易；
- 必须要求预测概率高于“实际成交均价 + 手续费 + 安全边际”；
- Polymarket盘口没有足够数量时，即使理论期望为正也不能假设成交；
- UP、DOWN 必须分别计算，不能把两边胜率合并。

## 十一、建议保存的统一数据字段

### 市场与标签

```text
market_id
market_start_us
market_end_us
observation_us
received_time_us
remaining_seconds
official_winner
chainlink_start_price
chainlink_end_price
```

### Polymarket

```text
poly_up_best_bid / poly_up_best_ask
poly_down_best_bid / poly_down_best_ask
poly_up_depth_to_085 / poly_down_depth_to_085
poly_last_trade_outcome
poly_last_trade_price
poly_last_trade_size
poly_book_age_ms
```

### 币安

```text
binance_price
binance_return_from_start
binance_return_15s / 30s / 60s / 180s
binance_candle_body_ratio
binance_close_location
binance_taker_buy_ratio
binance_volume_zscore
binance_trade_count_zscore
binance_book_imbalance_1 / 5
binance_age_ms
```

### Hyperliquid

```text
hl_mid_price
hl_mark_price
hl_oracle_price
hl_return_from_start
hl_mark_oracle_basis
hl_binance_basis
hl_trade_imbalance_15s / 30s / 60s
hl_book_imbalance_1 / 5
hl_open_interest
hl_open_interest_change_1m / 5m
hl_funding_rate
hl_age_ms
```

### Deribit

```text
deribit_index_price
deribit_perp_mid
deribit_index_return_from_start
deribit_perp_basis
deribit_atm_iv
deribit_atm_iv_change_1m / 5m
deribit_rr25
deribit_rr25_change_1m / 5m
deribit_dvol
deribit_dvol_change_1m / 5m
deribit_call_flow_delta_adjusted
deribit_put_flow_delta_adjusted
deribit_option_expiry_seconds
deribit_option_spread_ratio
deribit_age_ms
```

### 数据健康度

```text
binance_connected
hyperliquid_connected
deribit_connected
source_gap_seconds
clock_offset_ms
feature_missing_mask
external_consensus_label
```

缺失值必须保留为缺失，不能把缺失的收益率、成交量或盘口不平衡填成0。

## 十二、数据保存建议

建议继续使用长连接 WebSocket，不要每秒反复请求网站页面。

```text
data/cross_market/
├── raw/
│   ├── venue=binance/date=YYYY-MM-DD/hour=HH/*.ndjson.gz
│   ├── venue=hyperliquid/date=YYYY-MM-DD/hour=HH/*.ndjson.gz
│   └── venue=deribit/date=YYYY-MM-DD/hour=HH/*.ndjson.gz
├── normalized/
│   ├── venue=binance/...
│   ├── venue=hyperliquid/...
│   └── venue=deribit/...
├── features/
│   └── market_date=YYYY-MM-DD/*.parquet
├── health/
└── manifests/
```

保存原则：

- 原始消息追加写入，不覆盖；
- 按UTC日期和小时轮转；
- 原始高频流压缩；
- 标准事件和特征表分开；
- 每个文件记录行数、时间范围和SHA-256；
- 断线、重连、丢包和时钟偏差单独记录；
- 页面状态缓存不能作为回测事实源。

## 十三、回测方案

### 13.1 必须对比的模型

| 模型 | 目的 |
|---|---|
| Polymarket价格单独 | 市场自身概率基准 |
| 币安单独 | 现货短线信息价值 |
| Hyperliquid单独 | 永续和杠杆信息价值 |
| Deribit单独 | 期权方向和波动信息价值 |
| 币安 + Hyperliquid | 现货与永续组合 |
| 币安 + Deribit | 现货与期权组合 |
| Hyperliquid + Deribit | 永续与期权组合 |
| 三个外部市场 | 完整外部模型 |
| Polymarket + 三个外部市场 | 最终概率与交易模型 |

只有当新增数据源在独立测试集上改善结果，才说明它有额外预测价值。

### 13.2 评价指标

方向预测指标：

- 准确率；
- UP、DOWN 分方向召回率；
- Brier Score；
- Log Loss；
- 概率校准表；
- AUC作为辅助指标。

交易指标：

- 信号数、真实可成交数、未成交率；
- 胜率与95%置信区间；
- 平均实际成交价；
- 扣费后净收益/份；
- 总净收益；
- 最大连续亏损；
- 最大回撤；
- 按剩余90–60秒和60–30秒分组；
- UP、DOWN分开；
- 高IV、低IV和跨市场分歧分组。

### 13.3 时间切分

推荐：

```text
训练：最早60%
验证：中间20%
测试：最新20%
```

同时做滚动前推验证：

```text
用过去N天训练 -> 测下一天 -> 窗口向前滚动
```

禁止随机打乱全部5分钟样本后再切分，因为相邻市场高度相关，随机切分会夸大模型效果。

### 13.4 消融实验

每次去掉一个特征组：

- 去掉币安成交量；
- 去掉Hyperliquid盘口；
- 去掉持仓量；
- 去掉Deribit偏斜；
- 去掉IV/DVOL；
- 去掉Polymarket自身价格。

如果移除某组特征后独立测试结果几乎不变，它就不值得增加实时系统复杂度。

## 十四、最容易出现的错误

1. 用币安收盘方向代替Chainlink正式结算方向。
2. 在市场结束前使用整根5分钟K线最终值。
3. 用Deribit IV上涨直接预测UP。
4. 不控制期权到期时间和Delta，直接比较不同合约价格。
5. 把Hyperliquid资金费率当成下一轮5分钟方向。
6. 把跨市场价差极端扩大当成趋势，却忽略强平后的快速反转。
7. 不记录源时间与本机接收时间，导致无法判断真实延迟。
8. 断线期间用旧盘口继续计算信号。
9. 把缺失特征填成0，让模型误认为“中性”。
10. 用同一市场的多个时间点同时出现在训练集和测试集。
11. 只看胜率，不比较入场价、手续费和盘口深度。
12. 把历史上别人的成交量当成自己的可成交深度。
13. UP和DOWN合并统计，掩盖其中一方的负收益。
14. 在测试集上反复调整阈值，再把测试集结果当作真实样本外表现。

## 十五、推荐实施顺序

### 第一阶段：只记录，不产生交易信号

- 增加币安未闭合K线、逐笔成交和盘口记录；
- 新增Hyperliquid WebSocket记录器；
- 新增Deribit指数、永续、期权和DVOL记录器；
- 统一源时间、接收时间和断线健康记录；
- 同步保存当前 Polymarket 市场和 Chainlink 起止基准。

### 第二阶段：建立市场级特征表

- 按每个5分钟市场对齐；
- 在剩余240、180、120、100、90、60、30秒生成快照；
- 标记数据是否新鲜、是否缺失；
- 只在市场结算后追加正式标签。

### 第三阶段：离线回测

- 先做简单统计和逻辑回归；
- 进行时间切分、滚动前推和消融实验；
- 评估三个数据源是否提供增量信息；
- 确定阈值前先写好评价门槛。

### 第四阶段：纸上交叉验证

- 原有纸上策略保持不变；
- 外部预测只添加标签；
- 同时记录“支持、反对、分歧、数据过期”；
- 至少累计300–500个完整市场；
- 比较加标签前后的成交率、胜率和净收益。

### 第五阶段：重新评审

只有满足以下条件，才考虑把外部数据升级为策略过滤条件：

- 最新独立样本仍有正收益；
- 概率校准有效；
- 考虑延迟、真实盘口和手续费后仍为正；
- 不是只在单一日期或单一波动环境有效；
- 加入该数据源确实优于Polymarket价格和币安价格基准；
- 代码能处理断线、重连、旧数据和时钟偏差；
- 不影响原实盘执行链路。

## 十六、当前项目现状

截至2026-08-22已经存在：

- Polymarket BTC 5分钟历史成交、连续卖一和部分盘口数据；
- 币安历史K线回填连接器；
- 币安实时记录器基础代码；
- Polymarket实时纸上监控；
- UP/DOWN分方向记录；
- 90–30秒、100秒、120秒历史回测；
- 统一 `MarketEvent` 数据契约。
- Binance、Hyperliquid、Deribit、Polymarket RTDS/Chainlink 的实时连接器；
- Deribit BTC指数、永续、DVOL、近月期权 ticker 和全 BTC 期权主动成交订阅；
- 最近到期且距离到期超过12小时的平值与近25D期权选择，每边记录12个邻近行权价；
- `RR25`、RR25/ATM IV/DVOL的15–120秒变化、特定行权价OI变化和期权资金流；
- 每秒市场快照、100毫秒领先滞后事件、固定观察时点和官方结算标签；
- 实时预测关系报告与原纸上成交入场前快照联表。

仍然需要积累或完善：

- 足够多的实时已结算样本；公共历史接口无法完整还原过去的逐笔期权资金流与逐秒OI；
- 次近到期与更多 Delta 的对照组；
- 按时间滚动前推的样本外回测和概率校准；
- 跨市场数据覆盖与延迟质量报告。

因此这些新字段已经进入前瞻采集与验证代码，但尚不能仅凭少量实时样本认定能提高策略收益。

## 十七、官方接口参考

- Polymarket BTC 5分钟市场规则：<https://polymarket.com/event/btc-updown-5m-1771273500>
- Chainlink BTC/USD Data Stream：<https://data.chain.link/streams/btc-usd>
- Binance WebSocket K线说明：<https://developers.binance.com/docs/binance-spot-api-docs/web-socket-streams>
- Hyperliquid WebSocket：<https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket>
- Hyperliquid订阅类型与字段：<https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions>
- Hyperliquid Info接口与历史K线：<https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint>
- Deribit公共订阅：<https://docs.deribit.com/api-reference/subscription-management/public-subscribe>
- Deribit按品种聚合期权成交：<https://docs.deribit.com/subscriptions/trades/tradeskindcurrencyinterval>
- Deribit实时波动率指数：<https://docs.deribit.com/subscriptions/market-data/deribit_volatility_indexindex_name>
- Deribit行情Ticker与期权IV/Greeks：<https://docs.deribit.com/api-reference/market-data/public-ticker>
- Deribit BTC指数价格：<https://docs.deribit.com/api-reference/market-data/public-get_index_price>
- Deribit期权合约信息：<https://docs.deribit.com/api-reference/market-data/public-get_instruments>
- Deribit波动率指数历史：<https://docs.deribit.com/api-reference/market-data/public-get_volatility_index_data>

## 十八、一句话总结

币安最适合判断现货短线方向，Hyperliquid最适合确认杠杆资金和盘口方向，Deribit最适合判断期权偏斜与反转风险；三者必须按同一5分钟窗口和同一观察时间对齐，再以Chainlink正式结算为标签、以Polymarket实际成交成本为交易判断依据。

import assert from 'node:assert/strict'
import test from 'node:test'
import {
  GateNormalizer, parseGateDomText, parseGateMarketObject, parseGateWebSocketTrades
} from '../../scripts/monitor/run_gate_low_frequency_monitor.mjs'
import {
  gateSubscriptionRows, marketDefinitionFromResearchState
} from '../../scripts/monitor/run_gate_direct_monitor.mjs'

test('parses the observed Gate contract-events list shape', () => {
  const market = parseGateMarketObject({
    id: '881275', event_name: 'BTC 5分钟涨或跌', crypto: 'btc', period: '5m',
    start_date: 1_787_328_600, end_date: 1_787_328_900,
    bullish: '0.44', bearish: '0.57', clob_token_ids: ['up-token', 'down-token']
  }, 1_787_328_840_000)
  assert.equal(market.marketId, '881275')
  assert.equal(market.startTime, 1_787_328_600_000)
  assert.equal(market.outcomes.UP.bestAsk, '0.44')
  assert.equal(market.outcomes.DOWN.bestAsk, '0.57')
})

test('merges later token order-book frames without inventing depth', () => {
  const normalizer = new GateNormalizer()
  normalizer.ingest({ data: { list: [{
    id: '881275', event_name: 'BTC 5分钟涨或跌', crypto: 'btc', period: '5m',
    start_date: 1_787_328_600, end_date: 1_787_328_900,
    bullish: '0.44', bearish: '0.57', clob_token_ids: ['up-token', 'down-token']
  }] } }, 1_787_328_840_000)
  normalizer.ingest({ token_id: 'up-token', asks: [[0.45, 12], [0.46, 8]] }, 1_787_328_841_000)
  const market = normalizer.windows(1_787_328_841_000)[0]
  assert.deepEqual(market.outcomes.UP.levels, [['0.45', '12'], ['0.46', '8']])
  assert.equal(market.outcomes.UP.askSize, '12')
})

test('accepts only an explicit settlement field as winner', () => {
  const market = parseGateMarketObject({
    id: '881275', event_name: 'BTC 5m', start_date: 1_787_328_600,
    end_date: 1_787_328_900, bullish: '0.01', bearish: '0.99', result: 'DOWN'
  })
  assert.equal(market.winner, 'DOWN')
})

test('parses displayed two-way prices and selected-side depth', () => {
  const parsed = parseGateDomText(
    '交易规则 看涨 0.78 成本 (USDT) 看跌 0.23 Target 78421 订单薄 看涨 看跌 委托价 数量( 张 ) 累计( 张 ) 0.78 11.48 11.48 0.79 33 44.48 0.80 10 54.48 0.01 200 200',
    'https://www.gate.com/zh/trade-events/btc-updown-5m?eventId=883676&outcome=Up', 3
  )
  assert.deepEqual(parsed, {
    marketId: '883676', selectedOutcome: 'UP', up: '0.78', down: '0.23',
    levels: [['0.78', '11.48'], ['0.79', '33'], ['0.8', '10']]
  })
})

test('parses a new round while the target price is still unavailable', () => {
  const parsed = parseGateDomText(
    '交易规则 看涨 0.59 成本 (USDT) 看跌 0.42 已结束 11:20 订单薄 看涨 看跌 委托价 数量( 张 ) 累计( 张 ) 0.59 7.63 7.63',
    'https://www.gate.com/zh/trade-events/btc-updown-5m?eventId=883686&outcome=Up', 3
  )
  assert.equal(parsed.up, '0.59')
  assert.equal(parsed.down, '0.42')
})

test('settles a Gate market from its official target and final price', () => {
  const normalizer = new GateNormalizer()
  normalizer.ingest({ data: { list: [{
    id: '883676', event_name: 'BTC 5分钟涨或跌', crypto: 'btc', period: '5m',
    start_date: 1_787_368_500, end_date: 1_787_368_800,
    bullish: '0.94', bearish: '0.07', clob_token_ids: ['up-token', 'down-token']
  }] } }, 1_787_368_700_000)
  normalizer.ingest({
    slug: 'btc-updown-5m-1787368500', price_to_beat: '78421.06', final_price: '78425.10'
  }, 1_787_368_801_000)
  assert.equal(normalizer.contexts.get('883676').winner, 'UP')
})

test('uses Gate compact websocket orderbook fields for both direct and complement asks', () => {
  const normalizer = new GateNormalizer()
  normalizer.ingest({ id: '883900', event_name: 'BTC 5m', start_date: 1_787_370_000,
    end_date: 1_787_370_300, markets: [{ condition_id: 'condition', best_ask: 0.65,
      best_ask_token1: 0.36, clob_token_ids: ['up-token', 'down-token'] }] }, 1_787_370_010_000)
  normalizer.ingest({ channel: 'predict.poly.orderbook', event: 'update', result: {
    aid: 'up-token', mk: 'condition', a: [['0.61', '9']], b: [['0.59', '7']]
  } }, 1_787_370_011_000)
  const market = normalizer.contexts.get('883900')
  assert.equal(market.outcomes.UP.bestAsk, '0.61')
  assert.equal(market.outcomes.DOWN.bestAsk, '0.41')
  assert.equal(market.outcomes.DOWN.inferredFromComplementBook, true)
})

test('normalizes Gate websocket trades into direct and inferred aggressive outcomes', () => {
  const normalizer = new GateNormalizer()
  normalizer.ingest({ id: '883900', event_name: 'BTC 5m', start_date: 1_787_370_000,
    end_date: 1_787_370_300, markets: [{ condition_id: 'condition', best_ask: 0.65,
      best_ask_token1: 0.36, clob_token_ids: ['up-token', 'down-token'] }] }, 1_787_370_010_000)
  const trades = parseGateWebSocketTrades({ channel: 'predict.poly.trade', event: 'update', result: [
    { mk: 'condition', p: '0.66', s: '1.77', sd: 'SELL', t: 1_787_370_011_000, tid: 'one' },
    { mk: 'condition', p: '0.67', s: '2', sd: 'BUY', t: 1_787_370_012_000, tid: 'two' }
  ] }, normalizer, 'UP', 1_787_370_012_100)
  assert.deepEqual(trades.map(({ aggressiveOutcome, aggressivePrice }) => [aggressiveOutcome, aggressivePrice]), [
    ['DOWN', '0.34'], ['UP', '0.67']
  ])
  assert.ok(trades.every((trade) => trade.outcomeClassification === 'nearest_current_outcome_price_inference'))
})

test('uses Gate trade tid as the outcome token ID and builds a composite dedupe key', () => {
  const normalizer = new GateNormalizer()
  normalizer.ingest({ id: '883901', event_name: 'BTC 5m', start_date: 1_787_370_000,
    end_date: 1_787_370_300, markets: [{ condition_id: 'condition-direct', best_ask: 0.65,
      best_ask_token1: 0.36, clob_token_ids: ['up-token', 'down-token'] }] }, 1_787_370_010_000)
  const [trade] = parseGateWebSocketTrades({ channel: 'predict.poly.trade', event: 'update', result: {
    mk: 'condition-direct', p: '0.36', s: '4', sd: 'BUY', t: 1_787_370_011_000, tid: 'down-token'
  } }, normalizer, 'UP', 1_787_370_011_100)
  assert.equal(trade.tradedOutcome, 'DOWN')
  assert.equal(trade.aggressiveOutcome, 'DOWN')
  assert.equal(trade.outcomeClassification, 'token_id_direct')
  assert.match(trade.tradeId, /down-token:1787370011000:0\.36:4:BUY$/)
})

test('builds a direct Gate market and public subscriptions from the Polymarket state', () => {
  const market = marketDefinitionFromResearchState({ market: {
    slug: 'btc-updown-5m-1787373900', conditionId: 'condition',
    startUs: '1787373900000000', endUs: '1787374200000000',
    tokenByOutcome: { UP: 'up-token', DOWN: 'down-token' },
    outcomes: {
      UP: { bestAsk: '0.61', bestAskSize: '9', asks: [['0.61', '9']] },
      DOWN: { bestAsk: '0.40', bestAskSize: '7', asks: [['0.40', '7']] }
    }
  } }, 1_787_373_901_000)
  assert.equal(market.marketId, 'btc-updown-5m-1787373900')
  assert.equal(market.outcomes.UP.bestAsk, '0.61')
  assert.deepEqual(gateSubscriptionRows(market, 'subscribe', 1_787_373_902_000)
    .map(({ channel, payload }) => [channel, payload[0]]), [
    ['predict.poly.trade', 'condition'],
    ['predict.poly.orderbook', 'down-token'],
    ['predict.poly.price_to_beat_final_price', 'btc-updown-5m-1787373900']
  ])
})

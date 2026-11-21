#!/usr/bin/env node
import { appendFileSync, existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import { gzipSync } from 'node:zlib'

const GATE_URL = 'https://www.gate.com/trade-events'
const DEFAULT_CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
const MAX_WALK_OBJECTS = 12_000

const record = (value) => value && typeof value === 'object' && !Array.isArray(value) ? value : undefined
const firstValue = (source, keys) => {
  for (const key of keys) if (source[key] !== undefined && source[key] !== null) return source[key]
}
const stringValue = (value) => typeof value === 'string' && value.trim()
  ? value.trim()
  : typeof value === 'number' && Number.isFinite(value) ? String(value) : undefined

function timestamp(value) {
  if (typeof value === 'string' && !/^\d+(?:\.\d+)?$/.test(value)) {
    const parsed = Date.parse(value)
    return Number.isFinite(parsed) ? parsed : undefined
  }
  const parsed = Number(value)
  if (!Number.isFinite(parsed) || parsed <= 0) return undefined
  if (parsed < 10_000_000_000) return Math.round(parsed * 1_000)
  if (parsed > 10_000_000_000_000) return Math.round(parsed / 1_000)
  return Math.round(parsed)
}

function direction(value) {
  const raw = String(value ?? '').toUpperCase()
  if (/\b(?:UP|CALL|RISE|HIGHER|BULL|BULLISH|LONG)\b/.test(raw)) return 'UP'
  if (/\b(?:DOWN|PUT|FALL|LOWER|BEAR|BEARISH|SHORT)\b/.test(raw)) return 'DOWN'
}

function decimal(value) {
  const parsed = Number(value)
  return Number.isFinite(parsed) && parsed > 0 && parsed < 1 ? parsed.toString() : undefined
}

function level(value) {
  if (Array.isArray(value)) {
    const price = decimal(value[0])
    const size = Number(value[1])
    return price && Number.isFinite(size) && size > 0 ? [price, String(size)] : undefined
  }
  const item = record(value)
  if (!item) return
  const price = decimal(firstValue(item, ['price', 'p', 'rate']))
  const size = Number(firstValue(item, ['size', 'quantity', 'qty', 'amount', 'volume', 'q']))
  return price && Number.isFinite(size) && size > 0 ? [price, String(size)] : undefined
}

function askLevels(source) {
  const raw = firstValue(source, ['asks', 'ask', 'a', 'sell', 'sells', 'sellOrders', 'sell_orders'])
  return (Array.isArray(raw) ? raw : []).map(level).filter(Boolean)
    .sort((left, right) => Number(left[0]) - Number(right[0]))
}

function bidLevels(source) {
  const raw = firstValue(source, ['bids', 'bid', 'b', 'buy', 'buys', 'buyOrders', 'buy_orders'])
  return (Array.isArray(raw) ? raw : []).map(level).filter(Boolean)
    .sort((left, right) => Number(right[0]) - Number(left[0]))
}

function complementAsks(levels) {
  return levels.map(([price, size]) => [(1 - Number(price)).toFixed(8).replace(/0+$/, '').replace(/\.$/, ''), size])
    .filter(([price]) => Number(price) > 0 && Number(price) < 1)
    .sort((left, right) => Number(left[0]) - Number(right[0]))
}

function outcomeQuote(source, fallback, receivedAt) {
  const parsedDirection = direction(firstValue(source, ['direction', 'outcome', 'side', 'type', 'name', 'label', 'title']) ?? fallback)
  if (!parsedDirection) return
  const levels = askLevels(source)
  const bestAsk = levels[0]?.[0] ?? decimal(firstValue(source, ['bestAsk', 'best_ask', 'askPrice', 'ask_price', 'sellPrice', 'sell_price', 'price']))
  const askSize = levels[0]?.[1] ?? stringValue(firstValue(source, ['askSize', 'ask_size', 'availableQuantity', 'available_quantity', 'quantity', 'qty', 'size'])) ?? '0'
  if (!bestAsk) return
  const outcomeId = stringValue(firstValue(source, ['outcomeId', 'outcome_id', 'contractId', 'contract_id', 'symbolId', 'symbol_id', 'id'])) ?? parsedDirection
  return { direction: parsedDirection, outcomeId, bestAsk, askSize, levels, receivedAt }
}

function durationMinutes(source, startTime, endTime) {
  const raw = firstValue(source, ['durationMinutes', 'duration_min', 'duration', 'period', 'interval', 'cycle', 'timeframe', 'slug', 'event_name', 'question'])
  const match = String(raw ?? '').toLowerCase().match(/(?:^|\D)(5|15)\s*(?:m|min|minute|分)/)
  const direct = Number(raw)
  const inferred = startTime && endTime ? Math.round((endTime - startTime) / 60_000) : undefined
  const value = match ? Number(match[1]) : direct === 5 || direct === 15 ? direct : inferred
  return value === 5 || value === 15 ? value : undefined
}

function isBtc(source) {
  const raw = firstValue(source, ['asset', 'underlying', 'underlyingAsset', 'symbol', 'crypto', 'coin', 'base', 'event_name', 'question', 'slug', 'title', 'name'])
  return /BTC(?:\/|_|-)?(?:USD|USDT)?/i.test(String(raw ?? ''))
}

function marketId(source) {
  return stringValue(firstValue(source, ['marketId', 'market_id', 'eventId', 'event_id', 'contractGroupId', 'contract_group_id', 'id']))
}

function timeRange(source) {
  const startTime = timestamp(firstValue(source, ['startTime', 'start_time', 'game_start_time', 'beginTime', 'begin_time', 'openTime', 'open_time', 'startedAt', 'startAt', 'start_date']))
  const endTime = timestamp(firstValue(source, ['endTime', 'end_time', 'expireTime', 'expire_time', 'expirationTime', 'expiration_time', 'settleTime', 'settle_time', 'endsAt', 'endAt', 'end_date']))
  return startTime && endTime && endTime > startTime ? { startTime, endTime } : undefined
}

function winnerDirection(source) {
  const explicit = firstValue(source, [
    'winningOutcome', 'winning_outcome', 'winner', 'winnerOutcome', 'winner_outcome',
    'settlementResult', 'settlement_result', 'settleResult', 'settle_result', 'result'
  ])
  return direction(explicit)
}

function childOutcomeSources(source) {
  const result = []
  for (const key of ['outcomes', 'contracts', 'selections', 'symbols', 'options']) {
    if (Array.isArray(source[key])) for (const value of source[key]) if (record(value)) result.push([value])
  }
  for (const key of ['up', 'down', 'call', 'put', 'UP', 'DOWN']) {
    if (record(source[key])) result.push([source[key], key])
  }
  return result
}

export function parseGateMarketObject(source, receivedAt = Date.now()) {
  const id = marketId(source)
  let range = timeRange(source)
  if (!id || !range || !isBtc(source)) return
  const duration = durationMinutes(source, range.startTime, range.endTime)
  if (!duration) return
  if (Math.abs(range.endTime - range.startTime - duration * 60_000) > 1_000) {
    range = { startTime: range.endTime - duration * 60_000, endTime: range.endTime }
  }
  const outcomes = {}
  const tokenIds = Array.isArray(source.clob_token_ids) ? source.clob_token_ids.map(stringValue) : []
  const upPrice = decimal(firstValue(source, ['bullish', 'outcome_price0']))
  const downPrice = decimal(firstValue(source, ['bearish', 'best_ask_token1', 'outcome_price1']))
  if (upPrice) outcomes.UP = { direction: 'UP', outcomeId: tokenIds[0] ?? 'UP', bestAsk: upPrice, askSize: '0', levels: [], receivedAt }
  if (downPrice) outcomes.DOWN = { direction: 'DOWN', outcomeId: tokenIds[1] ?? 'DOWN', bestAsk: downPrice, askSize: '0', levels: [], receivedAt }
  const nestedMarket = Array.isArray(source.markets) ? record(source.markets[0]) : undefined
  if (nestedMarket) {
    const nestedTokens = Array.isArray(nestedMarket.clob_token_ids) ? nestedMarket.clob_token_ids.map(stringValue) : []
    const nestedUp = decimal(firstValue(nestedMarket, ['best_ask', 'outcome_price0']))
    const nestedDown = decimal(firstValue(nestedMarket, ['best_ask_token1', 'outcome_price1']))
    if (nestedUp) outcomes.UP = { direction: 'UP', outcomeId: nestedTokens[0] ?? 'UP', bestAsk: nestedUp, askSize: '0', levels: [], receivedAt }
    if (nestedDown) outcomes.DOWN = { direction: 'DOWN', outcomeId: nestedTokens[1] ?? 'DOWN', bestAsk: nestedDown, askSize: '0', levels: [], receivedAt }
  }
  for (const [candidate, fallback] of childOutcomeSources(source)) {
    const quote = outcomeQuote(candidate, fallback, receivedAt)
    if (quote) outcomes[quote.direction] = quote
  }
  const direct = outcomeQuote(source, undefined, receivedAt)
  if (direct) outcomes[direct.direction] = direct
  const slug = stringValue(firstValue(source, ['slug', 'event_slug'])) ?? `btc-updown-5m-${Math.floor(range.startTime / 1_000)}`
  const conditionId = stringValue(firstValue(source, ['condition_id', 'conditionId']))
    ?? stringValue(nestedMarket ? firstValue(nestedMarket, ['condition_id', 'conditionId']) : undefined)
  return { marketId: id, conditionId, slug, asset: 'BTC/USD', durationMinutes: duration, ...range, outcomes, winner: winnerDirection(source) }
}

function walk(value, visitor) {
  const queue = [value]
  let visited = 0
  while (queue.length && visited < MAX_WALK_OBJECTS) {
    const current = queue.shift()
    if (typeof current === 'string') {
      const start = Math.min(...['{', '['].map((token) => {
        const index = current.indexOf(token)
        return index < 0 ? Number.POSITIVE_INFINITY : index
      }))
      if (Number.isFinite(start)) {
        try { queue.push(JSON.parse(current.slice(start))) } catch { /* not embedded JSON */ }
      }
      continue
    }
    if (!current || typeof current !== 'object') continue
    visited += 1
    if (Array.isArray(current)) queue.push(...current)
    else {
      visitor(current)
      queue.push(...Object.values(current))
    }
  }
}

function describePayload(payload) {
  try {
    const parsed = JSON.parse(payload)
    const shapes = []
    walk(parsed, (item) => {
      const keys = Object.keys(item).sort()
      if (keys.length && !shapes.some((value) => value.join('|') === keys.join('|'))) shapes.push(keys)
    })
    return shapes.slice(0, 12)
  } catch {
    return [['non-json-response']]
  }
}

function payloadHints(payload) {
  try {
    const parsed = JSON.parse(payload)
    const hints = new Set()
    walk(parsed, (item) => {
      for (const key of ['channel', 'topic', 'event', 'method', 'type', 'action', 'operation', 'op']) {
        const value = item[key]
        if (typeof value === 'string' && value.length <= 160) hints.add(`${key}=${value}`)
      }
    })
    return [...hints].slice(0, 16)
  } catch { return [] }
}

function publicPayloadSample(payload) {
  try {
    const parsed = JSON.parse(payload)
    const root = record(parsed)
    const channel = stringValue(root?.channel)
    const result = record(root?.result)
    if (!result || !/^predict\.poly\.(?:trade|orderbook)$/.test(channel ?? '')) return undefined
    if (channel === 'predict.poly.trade') {
      return Object.fromEntries(['fr', 'mk', 'p', 's', 'sd', 't', 'tid'].map((key) => [key, result[key]]))
    }
    return {
      aid: result.aid, mk: result.mk, h: result.h,
      a: Array.isArray(result.a) ? result.a.slice(0, 2) : result.a,
      b: Array.isArray(result.b) ? result.b.slice(0, 2) : result.b
    }
  } catch { return undefined }
}

export class GateNormalizer {
  constructor() {
    this.contexts = new Map()
    this.outcomeToMarket = new Map()
    this.conditionToMarket = new Map()
  }

  ingest(payload, receivedAt = Date.now()) {
    let parsed
    try { parsed = typeof payload === 'string' ? JSON.parse(payload) : payload } catch { return false }
    let changed = false
    walk(parsed, (item) => {
      const slug = stringValue(firstValue(item, ['slug', 'event_slug']))
      const slugStart = slug?.match(/btc-updown-5m-(\d{10})$/)?.[1]
      const priceToBeat = Number(firstValue(item, ['price_to_beat', 'priceToBeat', 'target_price']))
      const finalPrice = Number(firstValue(item, ['final_price', 'finalPrice', 'settlement_price']))
      if (slugStart && Number.isFinite(priceToBeat) && priceToBeat > 0 && Number.isFinite(finalPrice) && finalPrice > 0) {
        const startTime = Number(slugStart) * 1_000
        const context = [...this.contexts.values()].find((value) => value.startTime === startTime)
        if (context && receivedAt >= context.endTime) {
          context.winner = finalPrice >= priceToBeat ? 'UP' : 'DOWN'
          changed = true
        }
      }
      const market = parseGateMarketObject(item, receivedAt)
      if (market) {
        const previous = this.contexts.get(market.marketId)
        const merged = previous
          ? { ...previous, ...market, outcomes: { ...previous.outcomes, ...market.outcomes }, winner: market.winner ?? previous.winner }
          : market
        this.contexts.set(market.marketId, merged)
        if (merged.conditionId) this.conditionToMarket.set(merged.conditionId, merged.marketId)
        for (const quote of Object.values(merged.outcomes)) {
          if (quote) this.outcomeToMarket.set(quote.outcomeId, { marketId: merged.marketId, direction: quote.direction })
        }
        changed = true
      }
      const tokenId = stringValue(firstValue(item, ['tokenId', 'token_id', 'clobTokenId', 'clob_token_id', 'asset_id', 'aid']))
      const tokenContext = tokenId ? this.outcomeToMarket.get(tokenId) : undefined
      const conditionId = stringValue(firstValue(item, ['condition_id', 'conditionId', 'mk']))
      const conditionMarketId = conditionId ? this.conditionToMarket.get(conditionId) : undefined
      const activeConditionContext = conditionId ? [...this.contexts.values()].find((value) =>
        value.conditionId === conditionId && value.durationMinutes === 5
        && value.startTime <= receivedAt && value.endTime >= receivedAt) : undefined
      const context = tokenContext
        ? this.contexts.get(tokenContext.marketId)
        : activeConditionContext ?? (conditionMarketId ? this.contexts.get(conditionMarketId) : undefined)
      const levels = context ? askLevels(item) : []
      const parsedDirection = tokenContext?.direction
      if (context && parsedDirection && levels.length) {
        context.outcomes[parsedDirection] = {
          direction: parsedDirection, outcomeId: tokenId,
          bestAsk: levels[0][0], askSize: levels[0][1], levels, receivedAt,
          transport: 'WEBSOCKET_ORDERBOOK'
        }
        changed = true
      }
      const bids = context ? bidLevels(item) : []
      if (context && parsedDirection && bids.length) {
        const opposite = parsedDirection === 'UP' ? 'DOWN' : 'UP'
        const impliedLevels = complementAsks(bids)
        if (impliedLevels.length) {
          const previousOutcome = context.outcomes[opposite]
          context.outcomes[opposite] = {
            direction: opposite, outcomeId: previousOutcome?.outcomeId ?? opposite,
            bestAsk: impliedLevels[0][0], askSize: impliedLevels[0][1], levels: impliedLevels,
            receivedAt, inferredFromComplementBook: true, transport: 'WEBSOCKET_COMPLEMENT_BOOK'
          }
          changed = true
        }
      }
      const id = marketId(item)
      const directContext = id ? this.contexts.get(id) : undefined
      if (directContext) {
        const quote = outcomeQuote(item, undefined, receivedAt)
        if (quote) { directContext.outcomes[quote.direction] = quote; changed = true }
        const winner = winnerDirection(item)
        if (winner) { directContext.winner = winner; changed = true }
      }
    })
    return changed
  }

  windows(now = Date.now()) {
    return [...this.contexts.values()]
      .filter((market) => market.durationMinutes === 5 && market.endTime > now - 15 * 60_000)
      .sort((a, b) => a.startTime - b.startTime)
  }

  prune(now = Date.now(), retainMs = 30 * 60_000) {
    let changed = false
    for (const [id, market] of this.contexts) {
      if (market.endTime >= now - retainMs) continue
      this.contexts.delete(id)
      changed = true
    }
    if (!changed) return
    this.outcomeToMarket.clear()
    this.conditionToMarket.clear()
    for (const market of this.contexts.values()) {
      if (market.conditionId) this.conditionToMarket.set(market.conditionId, market.marketId)
      for (const quote of Object.values(market.outcomes)) {
        if (quote) this.outcomeToMarket.set(quote.outcomeId, { marketId: market.marketId, direction: quote.direction })
      }
    }
  }
}

export function parseGateWebSocketTrades(payload, normalizer, selectedOutcome = 'UP', receivedAt = Date.now()) {
  let parsed
  try { parsed = typeof payload === 'string' ? JSON.parse(payload) : payload } catch { return [] }
  const root = record(parsed)
  if (root?.channel !== 'predict.poly.trade' || root?.event !== 'update') return []
  const rows = []
  walk(root.result, (item) => {
    const tokenId = stringValue(firstValue(item, ['tid', 'token_id', 'tokenId']))
    const conditionId = stringValue(firstValue(item, ['mk', 'condition_id', 'conditionId']))
    const price = Number(firstValue(item, ['p', 'price']))
    const size = Number(firstValue(item, ['s', 'size', 'quantity', 'qty']))
    const side = String(firstValue(item, ['sd', 'side', 'taker_side']) ?? '').toUpperCase()
    if (!tokenId || !conditionId || !(price > 0 && price < 1) || !(size > 0) || !['BUY', 'SELL'].includes(side)) return
    const sourceTime = timestamp(firstValue(item, ['t', 'time', 'timestamp'])) ?? receivedAt
    const mappedMarketId = normalizer.conditionToMarket.get(conditionId)
    const mappedMarket = mappedMarketId ? normalizer.contexts.get(mappedMarketId) : undefined
    const market = normalizer.windows(sourceTime).find((value) => value.conditionId === conditionId
      && value.startTime <= sourceTime && value.endTime >= sourceTime) ?? mappedMarket
    if (!market) return
    const tokenContext = normalizer.outcomeToMarket.get(tokenId)
    const upReference = Number(market.outcomes.UP?.bestAsk)
    const downReference = Number(market.outcomes.DOWN?.bestAsk)
    const tradedOutcome = tokenContext?.marketId === market.marketId
      ? tokenContext.direction
      : Number.isFinite(upReference) && Number.isFinite(downReference)
        ? Math.abs(price - upReference) <= Math.abs(price - downReference) ? 'UP' : 'DOWN'
        : selectedOutcome
    const outcomeClassification = tokenContext?.marketId === market.marketId
      ? 'token_id_direct'
      : 'nearest_current_outcome_price_inference'
    const aggressiveOutcome = side === 'BUY' ? tradedOutcome : tradedOutcome === 'UP' ? 'DOWN' : 'UP'
    const aggressivePrice = side === 'BUY' ? price : 1 - price
    const tradeId = [conditionId, tokenId, sourceTime, price, size, side].join(':')
    rows.push({
      tradeId, tokenId, marketId: market.marketId, conditionId, pageSelectedOutcome: selectedOutcome,
      tradedOutcome, outcomeClassification, rawSide: side,
      rawTradePrice: price.toString(), aggressiveOutcome,
      aggressivePrice: aggressivePrice.toFixed(8).replace(/0+$/, '').replace(/\.$/, ''),
      size: size.toString(), sourceTime, receivedAt,
      interpretation: side === 'BUY'
        ? `taker_buy_${outcomeClassification}`
        : `opposite_buy_from_sell_${outcomeClassification}`
    })
  })
  return rows
}

export function parseGateDomText(text, rawUrl, maxLevels = 3) {
  const clean = String(text ?? '').replace(/\s+/g, ' ')
  let id
  let selectedOutcome = 'UP'
  try {
    const url = new URL(rawUrl)
    id = url.searchParams.get('eventId') ?? undefined
    selectedOutcome = String(url.searchParams.get('outcome') ?? 'Up').toUpperCase() === 'DOWN' ? 'DOWN' : 'UP'
  } catch { /* keep defaults */ }
  const quoteMatch = clean.match(/交易规则\s+看涨\s+([0-9]+(?:\.[0-9]+)?)\s+成本.*?看跌\s+([0-9]+(?:\.[0-9]+)?|--)\s+(?:Target|已结束|订单薄)/)
  const up = quoteMatch ? decimal(quoteMatch[1]) : undefined
  const down = quoteMatch && quoteMatch[2] !== '--' ? decimal(quoteMatch[2]) : undefined
  const levels = []
  const book = clean.split('订单薄')[1] ?? ''
  const body = book.replace(/^.*?累计\( 张 \)\s*/, '')
  const levelPattern = /([0-9]+(?:\.[0-9]+)?)\s+([0-9][0-9,.]*(?:\s*万)?)\s+([0-9][0-9,.]*(?:\s*万)?)/g
  let previousPrice = -1
  for (const match of body.matchAll(levelPattern)) {
    const price = Number(match[1])
    if (!(price > 0 && price < 1) || price < previousPrice) break
    const sizeText = match[2].replace(/,/g, '')
    const size = Number.parseFloat(sizeText) * (/万/.test(match[2]) ? 10_000 : 1)
    if (Number.isFinite(size) && size > 0) levels.push([String(price), String(size)])
    previousPrice = price
    if (levels.length >= maxLevels) break
  }
  return { marketId: id, selectedOutcome, up, down, levels }
}

class RotatingGzipWriter {
  constructor(root, stream) { this.root = root; this.stream = stream }
  async write(row, at = new Date()) {
    const hour = at.toISOString().slice(0, 13)
    const [date, utcHour] = hour.split('T')
    const path = join(this.root, this.stream, `date=${date}`, `hour=${utcHour}`, 'part-001.ndjson.gz')
    mkdirSync(dirname(path), { recursive: true })
    appendFileSync(path, gzipSync(Buffer.from(`${JSON.stringify(row)}\n`), { level: 6 }))
  }
  async close() {}
}

function parseArgs(argv) {
  const args = {
    dataDir: resolve('data/gate_monitor'), snapshotSeconds: 10, maxLevels: 3,
    proxy: undefined, show: false, chrome: DEFAULT_CHROME, url: GATE_URL
  }
  for (let index = 0; index < argv.length; index += 1) {
    const key = argv[index]
    if (key === '--show') args.show = true
    else if (key === '--data-dir') args.dataDir = resolve(argv[++index])
    else if (key === '--snapshot-seconds') args.snapshotSeconds = Math.max(5, Number(argv[++index]))
    else if (key === '--max-levels') args.maxLevels = Math.max(0, Number(argv[++index]))
    else if (key === '--proxy') args.proxy = argv[++index]
    else if (key === '--chrome') args.chrome = argv[++index]
    else if (key === '--url') args.url = argv[++index]
    else throw new Error(`Unknown argument: ${key}`)
  }
  return args
}

function loadRuntimeState(path) {
  if (!existsSync(path)) return { signals: {}, settled: {} }
  try { return JSON.parse(readFileSync(path, 'utf8')) } catch { return { signals: {}, settled: {} } }
}

function compactOutcome(outcome, maxLevels, aggregate) {
  if (!outcome) return null
  return {
    bestAsk: outcome.bestAsk, askSize: outcome.askSize,
    levels: outcome.levels.slice(0, maxLevels), receivedAtUs: String(outcome.receivedAt * 1_000),
    transport: outcome.transport ?? null,
    inferredFromComplementBook: Boolean(outcome.inferredFromComplementBook),
    interval: aggregate ? {
      startUs: String(aggregate.startMs * 1_000),
      endUs: String(aggregate.endMs * 1_000),
      openAsk: aggregate.openAsk,
      highAsk: aggregate.highAsk,
      lowAsk: aggregate.lowAsk,
      closeAsk: aggregate.closeAsk,
      observationCount: aggregate.observationCount
    } : null
  }
}

function compactTradeAggregate(aggregate) {
  if (!aggregate) return null
  return {
    startUs: String(aggregate.startMs * 1_000),
    endUs: String(aggregate.endMs * 1_000),
    tradeCount: aggregate.tradeCount,
    volume: aggregate.volume.toFixed(8).replace(/0+$/, '').replace(/\.$/, ''),
    vwap: (aggregate.notional / aggregate.volume).toFixed(8).replace(/0+$/, '').replace(/\.$/, ''),
    minPrice: aggregate.minPrice.toFixed(8).replace(/0+$/, '').replace(/\.$/, ''),
    maxPrice: aggregate.maxPrice.toFixed(8).replace(/0+$/, '').replace(/\.$/, ''),
    lastPrice: aggregate.lastPrice.toFixed(8).replace(/0+$/, '').replace(/\.$/, ''),
    directCount: aggregate.directCount,
    inferredCount: aggregate.inferredCount
  }
}

function paperStats(runtime) {
  const rows = Object.values(runtime.settled)
  const wins = rows.filter((row) => row.won).length
  const pnl = rows.reduce((sum, row) => sum + Number(row.grossPnlPerShare), 0)
  return { closedTrades: rows.length, wins, losses: rows.length - wins, winRate: rows.length ? wins / rows.length : null, grossPnlPerShareTotal: pnl }
}

async function run() {
  // The direct collector imports only the normalizer/parser from this file.
  // Load browser automation only when the browser collector is actually run.
  const { chromium } = await import('playwright-core')
  const args = parseArgs(process.argv.slice(2))
  mkdirSync(args.dataDir, { recursive: true })
  const statePath = join(args.dataDir, 'state', 'latest.json')
  const runtimePath = join(args.dataDir, 'state', 'runtime.json')
  mkdirSync(dirname(statePath), { recursive: true })
  const runtime = loadRuntimeState(runtimePath)
  const normalizer = new GateNormalizer()
  const aggregates = new Map()
  const tradeAggregates = new Map()
  const recentTrades = []
  const seenTradeIds = new Set()
  const pendingSignals = []
  const snapshots = new RotatingGzipWriter(args.dataDir, 'snapshots')
  const paper = new RotatingGzipWriter(args.dataDir, 'paper')
  const startedAt = Date.now()
  const counters = {
    responses: 0, webSocketFrames: 0, webSocketTextFramesSeen: 0, webSocketBinaryFramesSeen: 0,
    parsedUpdates: 0, tradesReceived: 0, snapshotsWritten: 0, stateWrites: 0,
    signals: 0, settlements: 0
  }
  const responseDiagnostics = new Map()
  const webSocketDiagnostics = new Map()
  const webSocketSentDiagnostics = new Map()
  let lastCaptureAt
  let lastRelevantUrl = null
  let lastPayloadShape = []
  let lastSettlementPoll = null
  let pageDiagnostic = { title: '', text: '' }
  let lastSnapshotAt = 0
  let lastError = null
  let closing = false

  const browser = await chromium.launch({
    executablePath: args.chrome,
    headless: !args.show,
    proxy: args.proxy ? { server: args.proxy } : undefined,
    args: ['--disable-background-networking', '--disable-component-update', '--no-first-run']
  })
  const context = await browser.newContext({ locale: 'zh-CN', viewport: { width: 1280, height: 800 } })
  const page = await context.newPage()

  const selectedOutcome = () => {
    try { return String(new URL(page.url()).searchParams.get('outcome') ?? 'Up').toUpperCase() === 'DOWN' ? 'DOWN' : 'UP' }
    catch { return 'UP' }
  }

  const ingestTrade = (trade) => {
    if (seenTradeIds.has(trade.tradeId)) return
    seenTradeIds.add(trade.tradeId)
    if (seenTradeIds.size > 10_000) seenTradeIds.delete(seenTradeIds.values().next().value)
    const price = Number(trade.aggressivePrice)
    const size = Number(trade.size)
    const key = `${trade.marketId}:${trade.aggressiveOutcome}`
    const previous = tradeAggregates.get(key)
    tradeAggregates.set(key, previous ? {
      ...previous,
      endMs: trade.receivedAt,
      tradeCount: previous.tradeCount + 1,
      volume: previous.volume + size,
      notional: previous.notional + price * size,
      minPrice: Math.min(previous.minPrice, price),
      maxPrice: Math.max(previous.maxPrice, price),
      lastPrice: price,
      directCount: previous.directCount + (trade.rawSide === 'BUY' ? 1 : 0),
      inferredCount: previous.inferredCount + (trade.rawSide === 'SELL' ? 1 : 0)
    } : {
      startMs: trade.receivedAt, endMs: trade.receivedAt, tradeCount: 1,
      volume: size, notional: price * size, minPrice: price, maxPrice: price, lastPrice: price,
      directCount: trade.rawSide === 'BUY' ? 1 : 0,
      inferredCount: trade.rawSide === 'SELL' ? 1 : 0
    })
    recentTrades.push({
      ...trade,
      sourceTimeUs: String(trade.sourceTime * 1_000),
      receivedTimeUs: String(trade.receivedAt * 1_000)
    })
    if (recentTrades.length > 20) recentTrades.splice(0, recentTrades.length - 20)
    counters.tradesReceived += 1
    lastCaptureAt = trade.receivedAt
  }

  const observeMarkets = (receivedAt) => {
    for (const market of normalizer.windows(receivedAt)) {
      const remaining = (market.endTime - receivedAt) / 1_000
      for (const outcomeName of ['UP', 'DOWN']) {
        const quote = market.outcomes[outcomeName]
        const price = Number(quote?.bestAsk)
        if (!Number.isFinite(price)) continue
        const key = `${market.marketId}:${outcomeName}`
        const existing = aggregates.get(key)
        aggregates.set(key, existing ? {
          ...existing,
          endMs: receivedAt,
          highAsk: String(Math.max(Number(existing.highAsk), price)),
          lowAsk: String(Math.min(Number(existing.lowAsk), price)),
          closeAsk: quote.bestAsk,
          observationCount: existing.observationCount + 1
        } : {
          startMs: receivedAt, endMs: receivedAt, openAsk: quote.bestAsk,
          highAsk: quote.bestAsk, lowAsk: quote.bestAsk, closeAsk: quote.bestAsk,
          observationCount: 1
        })
        if (!runtime.signals[key] && remaining >= 30 && remaining <= 90 && price >= 0.80 && price <= 0.85) {
          const signal = {
            schemaVersion: 1, eventType: 'GATE_PAPER_SIGNAL', marketId: market.marketId,
            marketStartUs: String(market.startTime * 1_000), marketEndUs: String(market.endTime * 1_000),
            outcome: outcomeName, entryPrice: quote.bestAsk, remainingSeconds: remaining,
            receivedTimeUs: String(receivedAt * 1_000), quantity: '1', feeVerified: false,
            triggerSource: quote.transport ?? 'OBSERVED_BEST_ASK',
            executionQuality: 'rough_observed_best_ask_no_fill_guarantee'
          }
          runtime.signals[key] = signal
          pendingSignals.push(signal)
          counters.signals += 1
        }
      }
    }
  }
  const ingest = (payload) => {
    lastCaptureAt = Date.now()
    if (normalizer.ingest(payload, lastCaptureAt)) {
      counters.parsedUpdates += 1
      observeMarkets(lastCaptureAt)
    }
  }
  page.on('response', (response) => {
    const url = response.url()
    if (!/(?:gate\.com|gate\.io|gateio\.(?:ws|live))/i.test(url) || !/event|predict|contract|clob|order.?book|depth/i.test(url)) return
    counters.responses += 1
    void response.text().then((payload) => {
      lastRelevantUrl = url
      lastPayloadShape = describePayload(payload)
      const diagnosticKey = (() => { try { return new URL(url).pathname } catch { return url.slice(0, 160) } })()
      const previous = responseDiagnostics.get(diagnosticKey)
      responseDiagnostics.set(diagnosticKey, {
        path: diagnosticKey, count: (previous?.count ?? 0) + 1,
        shape: lastPayloadShape, hints: payloadHints(payload), lastAtUs: String(Date.now() * 1_000)
      })
      if (responseDiagnostics.size > 24) responseDiagnostics.delete(responseDiagnostics.keys().next().value)
      ingest(payload)
    }).catch(() => undefined)
  })
  page.on('websocket', (socket) => {
    if (!/(?:gate\.com|gate\.io|gateio\.(?:ws|live))/i.test(socket.url())) return
    socket.on('framesent', ({ payload }) => {
      if (typeof payload !== 'string' || !/predict\.poly|pred\.poly/i.test(payload)) return
      try {
        const parsed = JSON.parse(payload)
        const key = `${parsed.channel ?? 'unknown'}:${parsed.event ?? parsed.type ?? 'unknown'}`
        webSocketSentDiagnostics.set(key, { url: socket.url(), payload: parsed, lastAtUs: String(Date.now() * 1_000) })
        if (webSocketSentDiagnostics.size > 24) webSocketSentDiagnostics.delete(webSocketSentDiagnostics.keys().next().value)
      } catch { /* ignore non-JSON client frames */ }
    })
    socket.on('framereceived', ({ payload }) => {
      if (typeof payload !== 'string') {
        counters.webSocketBinaryFramesSeen += 1
        return
      }
      counters.webSocketTextFramesSeen += 1
      const hints = payloadHints(payload)
      const shape = describePayload(payload)
      const diagnosticKey = `${socket.url()}|${hints.join('|') || shape[0]?.join(',') || 'unknown'}`.slice(0, 500)
      const previous = webSocketDiagnostics.get(diagnosticKey)
      webSocketDiagnostics.set(diagnosticKey, {
        url: socket.url(), count: (previous?.count ?? 0) + 1, payloadBytes: Buffer.byteLength(payload),
        shape, hints, publicSample: publicPayloadSample(payload), lastAtUs: String(Date.now() * 1_000)
      })
      if (webSocketDiagnostics.size > 24) webSocketDiagnostics.delete(webSocketDiagnostics.keys().next().value)
      if (!/event|predict|contract|clob|market|depth|trade|fill|deal|match/i.test(payload)) return
      counters.webSocketFrames += 1
      for (const trade of parseGateWebSocketTrades(payload, normalizer, selectedOutcome(), Date.now())) ingestTrade(trade)
      ingest(payload)
    })
  })
  page.on('pageerror', (error) => { lastError = error.message })
  await page.goto(args.url, { waitUntil: 'domcontentloaded', timeout: 60_000 })

  const tick = async () => {
    const now = Date.now()
    normalizer.prune(now)
    pageDiagnostic = await page.evaluate(() => ({
      title: document.title,
      text: document.body?.innerText?.replace(/\s+/g, ' ').slice(0, 12_000) ?? ''
    })).catch(() => ({ title: '', text: '' }))
    const domQuote = parseGateDomText(pageDiagnostic.text, page.url(), args.maxLevels)
    const domContext = domQuote.marketId ? normalizer.contexts.get(domQuote.marketId) : undefined
    if (domContext && (domQuote.up || domQuote.down)) {
      if (domQuote.up) domContext.outcomes.UP = {
        ...(domContext.outcomes.UP ?? { direction: 'UP', outcomeId: 'UP', askSize: '0', levels: [] }),
        bestAsk: domQuote.up, receivedAt: now,
        levels: domQuote.selectedOutcome === 'UP' && domQuote.levels.length ? domQuote.levels : (domContext.outcomes.UP?.levels ?? []),
        askSize: domQuote.selectedOutcome === 'UP' && domQuote.levels[0] ? domQuote.levels[0][1] : (domContext.outcomes.UP?.askSize ?? '0'),
        transport: 'DOM_1S_FALLBACK'
      }
      if (domQuote.down) domContext.outcomes.DOWN = {
        ...(domContext.outcomes.DOWN ?? { direction: 'DOWN', outcomeId: 'DOWN', askSize: '0', levels: [] }),
        bestAsk: domQuote.down, receivedAt: now,
        levels: domQuote.selectedOutcome === 'DOWN' && domQuote.levels.length ? domQuote.levels : (domContext.outcomes.DOWN?.levels ?? []),
        askSize: domQuote.selectedOutcome === 'DOWN' && domQuote.levels[0] ? domQuote.levels[0][1] : (domContext.outcomes.DOWN?.askSize ?? '0'),
        transport: 'DOM_1S_FALLBACK'
      }
      observeMarkets(now)
    }
    const settlementTargets = normalizer.windows(now).filter((market) => {
      const age = now - market.endTime
      const due = !market.settlementPollAt || now - market.settlementPollAt >= 30_000
      return !market.winner && age >= 0 && age <= 3 * 60_000 && due && (market.settlementPollCount ?? 0) < 3
    })
    if (settlementTargets.length) {
      for (const market of settlementTargets) {
        market.settlementPollAt = now
        market.settlementPollCount = (market.settlementPollCount ?? 0) + 1
      }
      const settlementPayloads = await page.evaluate(async (targets) => Promise.all(targets.map(async ({ slug }) => {
        try {
          const response = await fetch(`/apiw/v2/event-contract/events/price-to-beat-final-price/${encodeURIComponent(slug)}?sub_website_id=0`, {
            credentials: 'same-origin', cache: 'no-store'
          })
          return { slug, status: response.status, payload: response.ok ? await response.text() : null }
        } catch (error) { return { slug, status: 0, error: String(error) } }
      })), settlementTargets.map(({ slug }) => ({ slug }))).catch(() => [])
      for (const result of settlementPayloads) {
        lastSettlementPoll = {
          slug: result.slug, status: result.status,
          payloadShape: result.payload ? describePayload(result.payload) : [],
          error: result.error ?? null, receivedAtUs: String(Date.now() * 1_000)
        }
        if (result.payload) ingest(result.payload)
      }
    }
    const active = normalizer.windows(now)
    const compactMarkets = active.map((market) => ({
      marketId: market.marketId,
      startUs: String(market.startTime * 1_000), endUs: String(market.endTime * 1_000),
      remainingSeconds: (market.endTime - now) / 1_000,
      outcomes: {
        UP: compactOutcome(market.outcomes.UP, args.maxLevels, aggregates.get(`${market.marketId}:UP`)),
        DOWN: compactOutcome(market.outcomes.DOWN, args.maxLevels, aggregates.get(`${market.marketId}:DOWN`))
      },
      tradeWindow: {
        seconds: args.snapshotSeconds,
        UP: compactTradeAggregate(tradeAggregates.get(`${market.marketId}:UP`)),
        DOWN: compactTradeAggregate(tradeAggregates.get(`${market.marketId}:DOWN`))
      },
      winner: market.winner ?? null
    }))
    const snapshotDue = now - lastSnapshotAt >= args.snapshotSeconds * 1_000
    if (snapshotDue && compactMarkets.length) {
      await snapshots.write({
        schemaVersion: 2, eventType: 'GATE_10S_AGGREGATE_SNAPSHOT',
        receivedTimeUs: String(now * 1_000), markets: compactMarkets
      })
      counters.snapshotsWritten += 1
      lastSnapshotAt = now
    }

    while (pendingSignals.length) await paper.write(pendingSignals.shift())

    for (const market of active) {
      for (const outcomeName of ['UP', 'DOWN']) {
        const key = `${market.marketId}:${outcomeName}`
        if (market.winner && runtime.signals[key] && !runtime.settled[key]) {
          const signal = runtime.signals[key]
          const won = outcomeName === market.winner
          const grossPnlPerShare = (won ? 1 - Number(signal.entryPrice) : -Number(signal.entryPrice)).toFixed(8)
          const settlement = {
            schemaVersion: 1, eventType: 'GATE_PAPER_SETTLEMENT', marketId: market.marketId,
            outcome: outcomeName, winner: market.winner, won, entryPrice: signal.entryPrice,
            grossPnlPerShare, feeVerified: false, receivedTimeUs: String(now * 1_000)
          }
          runtime.settled[key] = settlement
          counters.settlements += 1
          await paper.write(settlement)
        }
      }
    }
    writeFileSync(runtimePath, JSON.stringify(runtime, null, 2))
    writeFileSync(statePath, JSON.stringify({
      schemaVersion: 1, generatedAtUs: String(now * 1_000),
      service: {
        status: 'live', startedAtUs: String(startedAt * 1_000),
        lastCaptureAtUs: lastCaptureAt ? String(lastCaptureAt * 1_000) : null,
        lastError, pageUrl: page.url(), pageTitle: pageDiagnostic.title,
        pageTextPreview: pageDiagnostic.text.slice(0, 300), lastRelevantUrl, lastPayloadShape,
        lastSettlementPoll,
        responseDiagnostics: [...responseDiagnostics.values()],
        webSocketDiagnostics: [...webSocketDiagnostics.values()],
        webSocketSentDiagnostics: [...webSocketSentDiagnostics.values()]
      },
      storage: {
        aggregationSeconds: args.snapshotSeconds,
        realtimeDisplayIntervalMs: 1_000,
        aggregationFields: ['openAsk', 'highAsk', 'lowAsk', 'closeAsk', 'observationCount', 'tradeCount', 'volume', 'vwap', 'minPrice', 'maxPrice', 'lastPrice'],
        gzip: true, rawFramesStored: false, recentTradesAppendOnlyStored: false,
        recentTradesStoredInOverwriteState: true, maxRecentTradesInState: 20,
        maxDepthLevels: args.maxLevels, accountDataStored: false
      },
      counters, activeMarkets: compactMarkets, recentTrades, paperStats: paperStats(runtime)
    }, null, 2))
    counters.stateWrites += 1
    if (snapshotDue) {
      aggregates.clear()
      tradeAggregates.clear()
    }
  }

  let ticking = false
  const runTick = async () => {
    if (ticking) return
    ticking = true
    try { await tick() } catch (error) { lastError = error.message }
    finally { ticking = false }
  }
  const timer = setInterval(() => void runTick(), 1_000)
  await runTick()
  console.log('Gate realtime monitor started')
  console.log(`Page: ${args.url} (${args.show ? 'visible' : 'headless'})`)
  console.log(`Data: ${args.dataDir}`)
  console.log(`Realtime: WebSocket orderbook/trades with 1s DOM fallback and immediate paper signals`)
  console.log(`Storage: ${args.snapshotSeconds}s ask/trade aggregates, gzip, raw frames disabled`)
  console.log('Paper: rough best-ask signals only; no order placement, no credentials, fees unverified')

  const close = async () => {
    if (closing) return
    closing = true
    clearInterval(timer)
    await snapshots.close()
    await paper.close()
    await browser.close()
    process.exit(0)
  }
  process.on('SIGINT', () => void close())
  process.on('SIGTERM', () => void close())
  await new Promise(() => undefined)
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  run().catch((error) => { console.error(error); process.exit(1) })
}

#!/usr/bin/env node
import { appendFileSync, existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import { gzipSync } from 'node:zlib'

import { GateNormalizer, parseGateWebSocketTrades } from './run_gate_low_frequency_monitor.mjs'

const DEFAULT_WS_URL = 'wss://prediction-ws.gateio.ws/v1/ws/prediction/event-contract/web'

function parseArgs(argv) {
  const args = {
    dataDir: resolve('data/gate_monitor'),
    liveStatePath: resolve('data/live_monitor/state/latest.json'),
    snapshotSeconds: 10,
    maxLevels: 3,
    websocketUrl: DEFAULT_WS_URL
  }
  for (let index = 0; index < argv.length; index += 1) {
    const key = argv[index]
    if (key === '--data-dir') args.dataDir = resolve(argv[++index])
    else if (key === '--live-state') args.liveStatePath = resolve(argv[++index])
    else if (key === '--snapshot-seconds') args.snapshotSeconds = Math.max(5, Number(argv[++index]))
    else if (key === '--max-levels') args.maxLevels = Math.max(0, Number(argv[++index]))
    else if (key === '--websocket-url') args.websocketUrl = argv[++index]
    else if (key === '--stop-file') args.stopFile = resolve(argv[++index])
    else throw new Error(`Unknown argument: ${key}`)
  }
  return args
}

function loadJson(path, fallback) {
  if (!existsSync(path)) return fallback
  try { return JSON.parse(readFileSync(path, 'utf8')) } catch { return fallback }
}

class RotatingGzipWriter {
  constructor(root, stream) { this.root = root; this.stream = stream }
  async write(row, at = new Date()) {
    const [date, hour] = at.toISOString().slice(0, 13).split('T')
    const path = join(this.root, this.stream, `date=${date}`, `hour=${hour}`, 'part-001.ndjson.gz')
    mkdirSync(dirname(path), { recursive: true })
    appendFileSync(path, gzipSync(Buffer.from(`${JSON.stringify(row)}\n`), { level: 6 }))
  }
  async close() {}
}

function cleanDecimal(value) {
  return Number(value).toFixed(8).replace(/0+$/, '').replace(/\.$/, '')
}

export function marketDefinitionFromResearchState(state, receivedAt = Date.now()) {
  const market = state?.market
  const tokens = market?.tokenByOutcome
  const startMs = Number(market?.startUs) / 1_000
  const endMs = Number(market?.endUs) / 1_000
  if (!market?.slug || !market?.conditionId || !tokens?.UP || !tokens?.DOWN
    || !Number.isFinite(startMs) || !Number.isFinite(endMs) || endMs <= startMs) return null
  const outcomes = {}
  for (const outcome of ['UP', 'DOWN']) {
    const source = market.outcomes?.[outcome]
    const bestAsk = Number(source?.bestAsk)
    if (!(bestAsk > 0 && bestAsk < 1)) continue
    outcomes[outcome] = {
      direction: outcome,
      outcomeId: String(tokens[outcome]),
      bestAsk: cleanDecimal(bestAsk),
      askSize: String(source.bestAskSize ?? '0'),
      levels: Array.isArray(source.asks) ? source.asks.slice(0, 10) : [],
      receivedAt,
      transport: 'POLYMARKET_STATE_BOOTSTRAP'
    }
  }
  return {
    marketId: String(market.slug),
    conditionId: String(market.conditionId),
    slug: String(market.slug),
    asset: 'BTC/USD',
    durationMinutes: 5,
    startTime: startMs,
    endTime: endMs,
    outcomes,
    winner: null,
    tokenByOutcome: { UP: String(tokens.UP), DOWN: String(tokens.DOWN) }
  }
}

export function gateSubscriptionRows(market, event = 'subscribe', now = Date.now()) {
  if (!market) return []
  const time = Math.floor(now / 1_000)
  return [
    ['predict.poly.trade', market.conditionId],
    ['predict.poly.orderbook', market.tokenByOutcome.DOWN],
    ['predict.poly.price_to_beat_final_price', market.slug]
  ].map(([channel, value], index) => ({ time, channel, event, payload: [value], id: index + 1 }))
}

function compactOutcome(outcome, maxLevels, aggregate) {
  if (!outcome) return null
  return {
    bestAsk: outcome.bestAsk,
    askSize: outcome.askSize,
    levels: (outcome.levels ?? []).slice(0, maxLevels),
    receivedAtUs: String(outcome.receivedAt * 1_000),
    transport: outcome.transport ?? 'DIRECT_WEBSOCKET',
    inferredFromComplementBook: Boolean(outcome.inferredFromComplementBook),
    interval: aggregate ? {
      startUs: String(aggregate.startMs * 1_000), endUs: String(aggregate.endMs * 1_000),
      openAsk: aggregate.openAsk, highAsk: aggregate.highAsk, lowAsk: aggregate.lowAsk,
      closeAsk: aggregate.closeAsk, observationCount: aggregate.observationCount
    } : null
  }
}

function compactTradeAggregate(aggregate) {
  if (!aggregate) return null
  return {
    startUs: String(aggregate.startMs * 1_000), endUs: String(aggregate.endMs * 1_000),
    tradeCount: aggregate.tradeCount, volume: cleanDecimal(aggregate.volume),
    vwap: cleanDecimal(aggregate.notional / aggregate.volume),
    minPrice: cleanDecimal(aggregate.minPrice), maxPrice: cleanDecimal(aggregate.maxPrice),
    lastPrice: cleanDecimal(aggregate.lastPrice), directCount: aggregate.directCount,
    inferredCount: aggregate.inferredCount
  }
}

function paperStats(runtime) {
  const rows = Object.values(runtime.settled ?? {})
  const wins = rows.filter((row) => row.won).length
  return {
    closedTrades: rows.length,
    wins,
    losses: rows.length - wins,
    winRate: rows.length ? wins / rows.length : null,
    grossPnlPerShareTotal: rows.reduce((sum, row) => sum + Number(row.grossPnlPerShare), 0)
  }
}

async function run() {
  const args = parseArgs(process.argv.slice(2))
  mkdirSync(args.dataDir, { recursive: true })
  const statePath = join(args.dataDir, 'state', 'latest.json')
  const runtimePath = join(args.dataDir, 'state', 'runtime.json')
  mkdirSync(dirname(statePath), { recursive: true })
  const runtime = loadJson(runtimePath, { signals: {}, settled: {} })
  runtime.signals ??= {}
  runtime.settled ??= {}

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
    webSocketFrames: 0, parsedUpdates: 0, tradesReceived: 0, subscriptionsSent: 0,
    reconnects: 0, snapshotsWritten: 0, stateWrites: 0, signals: 0, settlements: 0
  }
  let socket = null
  let socketStatus = 'connecting'
  let subscribedMarket = null
  let currentMarket = null
  const settlementMarkets = new Map()
  let lastCaptureAt = null
  let lastResearchStateAt = null
  let lastSnapshotAt = 0
  let lastError = null
  let reconnectTimer = null
  let reconnectDelayMs = 1_000
  let closing = false

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
          ...existing, endMs: receivedAt,
          highAsk: String(Math.max(Number(existing.highAsk), price)),
          lowAsk: String(Math.min(Number(existing.lowAsk), price)),
          closeAsk: quote.bestAsk, observationCount: existing.observationCount + 1
        } : {
          startMs: receivedAt, endMs: receivedAt, openAsk: quote.bestAsk,
          highAsk: quote.bestAsk, lowAsk: quote.bestAsk, closeAsk: quote.bestAsk,
          observationCount: 1
        })
        if (!runtime.signals[key] && remaining >= 30 && remaining <= 90 && price >= 0.80 && price <= 0.85) {
          const signal = {
            schemaVersion: 2, eventType: 'GATE_PAPER_SIGNAL', marketId: market.marketId,
            marketStartUs: String(market.startTime * 1_000), marketEndUs: String(market.endTime * 1_000),
            outcome: outcomeName, entryPrice: quote.bestAsk, remainingSeconds: remaining,
            receivedTimeUs: String(receivedAt * 1_000), quantity: '1', feeVerified: false,
            triggerSource: quote.transport ?? 'DIRECT_WEBSOCKET',
            executionQuality: 'rough_observed_best_ask_no_fill_guarantee'
          }
          runtime.signals[key] = signal
          pendingSignals.push(signal)
          counters.signals += 1
        }
      }
    }
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
      ...previous, endMs: trade.receivedAt, tradeCount: previous.tradeCount + 1,
      volume: previous.volume + size, notional: previous.notional + price * size,
      minPrice: Math.min(previous.minPrice, price), maxPrice: Math.max(previous.maxPrice, price),
      lastPrice: price, directCount: previous.directCount + (trade.rawSide === 'BUY' ? 1 : 0),
      inferredCount: previous.inferredCount + (trade.rawSide === 'SELL' ? 1 : 0)
    } : {
      startMs: trade.receivedAt, endMs: trade.receivedAt, tradeCount: 1,
      volume: size, notional: price * size, minPrice: price, maxPrice: price, lastPrice: price,
      directCount: trade.rawSide === 'BUY' ? 1 : 0, inferredCount: trade.rawSide === 'SELL' ? 1 : 0
    })
    recentTrades.push({ ...trade, sourceTimeUs: String(trade.sourceTime * 1_000), receivedTimeUs: String(trade.receivedAt * 1_000) })
    if (recentTrades.length > 20) recentTrades.splice(0, recentTrades.length - 20)
    counters.tradesReceived += 1
  }

  const sendRows = (market, event, includeFinal = true) => {
    if (!socket || socket.readyState !== WebSocket.OPEN || !market) return
    const rows = gateSubscriptionRows(market, event).filter((row) =>
      includeFinal || row.channel !== 'predict.poly.price_to_beat_final_price')
    for (const row of rows) {
      socket.send(JSON.stringify(row))
      counters.subscriptionsSent += 1
    }
  }

  const switchSubscription = (market) => {
    if (!market || subscribedMarket?.slug === market.slug) return
    if (subscribedMarket) sendRows(subscribedMarket, 'unsubscribe', false)
    subscribedMarket = market
    sendRows(market, 'subscribe')
  }

  const scheduleReconnect = () => {
    if (closing || reconnectTimer) return
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null
      connect()
    }, reconnectDelayMs)
    reconnectDelayMs = Math.min(reconnectDelayMs * 2, 30_000)
  }

  const connect = () => {
    socketStatus = 'connecting'
    socket = new WebSocket(args.websocketUrl)
    socket.addEventListener('open', () => {
      socketStatus = 'live'
      reconnectDelayMs = 1_000
      lastError = null
      subscribedMarket = null
      switchSubscription(currentMarket)
      for (const market of settlementMarkets.values()) {
        if (market.slug === currentMarket?.slug) continue
        const row = gateSubscriptionRows(market, 'subscribe').find((item) =>
          item.channel === 'predict.poly.price_to_beat_final_price')
        if (row) { socket.send(JSON.stringify(row)); counters.subscriptionsSent += 1 }
      }
    })
    socket.addEventListener('message', (event) => {
      const receivedAt = Date.now()
      lastCaptureAt = receivedAt
      counters.webSocketFrames += 1
      let payload
      try { payload = JSON.parse(String(event.data)) } catch { return }
      for (const trade of parseGateWebSocketTrades(payload, normalizer, 'UP', receivedAt)) ingestTrade(trade)
      if (normalizer.ingest(payload, receivedAt)) {
        counters.parsedUpdates += 1
        observeMarkets(receivedAt)
      }
    })
    socket.addEventListener('error', () => { lastError = 'Gate WebSocket connection error' })
    socket.addEventListener('close', () => {
      socketStatus = 'reconnecting'
      counters.reconnects += 1
      scheduleReconnect()
    })
  }

  const refreshMarketDefinition = (now) => {
    const researchState = loadJson(args.liveStatePath, null)
    const market = marketDefinitionFromResearchState(researchState, now)
    if (!market) return
    lastResearchStateAt = Number(researchState.generatedAtUs) / 1_000
    const previous = normalizer.contexts.get(market.marketId)
    normalizer.contexts.set(market.marketId, previous ? {
      ...previous, ...market, outcomes: { ...market.outcomes, ...previous.outcomes },
      winner: previous.winner ?? market.winner
    } : market)
    normalizer.conditionToMarket.set(market.conditionId, market.marketId)
    for (const quote of Object.values(market.outcomes)) {
      normalizer.outcomeToMarket.set(quote.outcomeId, { marketId: market.marketId, direction: quote.direction })
    }
    currentMarket = market
    settlementMarkets.set(market.slug, market)
    for (const [slug, candidate] of settlementMarkets) {
      if (candidate.endTime < now - 10 * 60_000) settlementMarkets.delete(slug)
    }
    switchSubscription(market)
    observeMarkets(now)
  }

  const tick = async () => {
    const now = Date.now()
    refreshMarketDefinition(now)
    normalizer.prune(now)
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
      await snapshots.write({ schemaVersion: 2, eventType: 'GATE_10S_AGGREGATE_SNAPSHOT', receivedTimeUs: String(now * 1_000), markets: compactMarkets })
      counters.snapshotsWritten += 1
      lastSnapshotAt = now
    }
    while (pendingSignals.length) await paper.write(pendingSignals.shift())
    for (const market of active) {
      for (const outcome of ['UP', 'DOWN']) {
        const key = `${market.marketId}:${outcome}`
        if (!market.winner || !runtime.signals[key] || runtime.settled[key]) continue
        const signal = runtime.signals[key]
        const won = outcome === market.winner
        const settlement = {
          schemaVersion: 2, eventType: 'GATE_PAPER_SETTLEMENT', marketId: market.marketId,
          outcome, winner: market.winner, won, entryPrice: signal.entryPrice,
          grossPnlPerShare: (won ? 1 - Number(signal.entryPrice) : -Number(signal.entryPrice)).toFixed(8),
          feeVerified: false, receivedTimeUs: String(now * 1_000)
        }
        runtime.settled[key] = settlement
        counters.settlements += 1
        await paper.write(settlement)
      }
    }
    writeFileSync(runtimePath, JSON.stringify(runtime, null, 2))
    writeFileSync(statePath, JSON.stringify({
      schemaVersion: 2, generatedAtUs: String(now * 1_000),
      service: {
        status: socketStatus === 'live' ? 'live' : socketStatus,
        transport: 'DIRECT_WEBSOCKET', browserRequired: false,
        startedAtUs: String(startedAt * 1_000),
        lastCaptureAtUs: lastCaptureAt ? String(lastCaptureAt * 1_000) : null,
        lastResearchStateAtUs: lastResearchStateAt ? String(lastResearchStateAt * 1_000) : null,
        lastError, websocketUrl: args.websocketUrl, researchStatePath: args.liveStatePath
      },
      storage: {
        aggregationSeconds: args.snapshotSeconds, realtimeDisplayIntervalMs: 1_000,
        aggregationFields: ['openAsk', 'highAsk', 'lowAsk', 'closeAsk', 'observationCount', 'tradeCount', 'volume', 'vwap', 'minPrice', 'maxPrice', 'lastPrice'],
        gzip: true, rawFramesStored: false, recentTradesAppendOnlyStored: false,
        recentTradesStoredInOverwriteState: true, maxRecentTradesInState: 20,
        maxDepthLevels: args.maxLevels, accountDataStored: false
      },
      counters, activeMarkets: compactMarkets, recentTrades, paperStats: paperStats(runtime)
    }, null, 2))
    counters.stateWrites += 1
    if (snapshotDue) { aggregates.clear(); tradeAggregates.clear() }
  }

  connect()
  let ticking = false
  const runTick = async () => {
    if (ticking) return
    ticking = true
    try { await tick() } catch (error) { lastError = error.message }
    finally { ticking = false }
  }
  const timer = setInterval(() => void runTick(), 1_000)
  await runTick()
  console.log('Gate direct WebSocket monitor started')
  console.log(`WebSocket: ${args.websocketUrl}`)
  console.log(`Market source: ${args.liveStatePath}`)
  console.log(`Data: ${args.dataDir}`)
  console.log(`Storage: ${args.snapshotSeconds}s ask/trade aggregates; no browser and no raw frames`)

  const close = async () => {
    if (closing) return
    closing = true
    clearInterval(timer)
    clearInterval(stopTimer)
    if (reconnectTimer) clearTimeout(reconnectTimer)
    if (socket?.readyState === WebSocket.OPEN) socket.close()
    await snapshots.close()
    await paper.close()
    process.exit(0)
  }
  process.on('SIGINT', () => void close())
  process.on('SIGTERM', () => void close())
  const stopTimer = setInterval(() => {
    if (args.stopFile && existsSync(args.stopFile)) void close()
  }, 200)
  await new Promise(() => undefined)
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  run().catch((error) => { console.error(error); process.exit(1) })
}

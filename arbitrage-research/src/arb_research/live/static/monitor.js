"use strict";

const $ = (id) => document.getElementById(id);
const escapeHtml = (value) => String(value ?? "")
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;")
  .replaceAll("'", "&#039;");

const number = (value, digits = 2) => {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed.toLocaleString("zh-CN", { maximumFractionDigits: digits }) : "--";
};
const price = (value) => value == null ? "--" : Number(value).toFixed(3);
const clock = (microseconds) => {
  if (!microseconds) return "--";
  return new Date(Number(microseconds) / 1000).toLocaleTimeString("zh-CN", { hour12: false });
};
const countdown = (value) => {
  const seconds = Math.max(0, Math.floor(Number(value) || 0));
  return `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
};
const age = (value) => value == null ? "盘口 --" : `盘口 ${Math.round(Number(value))}ms`;

const statusLabels = {
  live: ["实时连接", "live"],
  starting: ["正在启动", "waiting"],
  waiting_for_market: ["等待市场", "waiting"],
  reconnecting: ["正在重连", "error"],
  stopped: ["监控已停止", "error"],
};
const reasonLabels = {
  buySide: "非主动买入",
  priceBand: "价格不在区间",
  remainingBand: "时间不在区间",
  tradeSize: "成交量不足",
  freshOrderBook: "盘口陈旧",
  stableOrderBook: "盘口未稳定500ms",
  fullDepthAtPriceCap: "0.85内深度不足",
  entryVwapBand: "实际成交均价不在0.80–0.85",
  sameDirectionMomentum: "入场前5秒同方向动量不足0.01",
  crossMarketAligned: "Spot/Futures/Hyperliquid未同向",
  oneTradePerMarket: "本市场已有持仓",
  missingOrderBook: "缺少盘口",
  staleOrderBook: "盘口陈旧",
  insufficientDepthAtPriceCap: "0.85内深度不足",
  entryVwapOutOfBand: "实际成交均价不在0.80–0.85",
};
const resultLabels = {
  REJECTED: "未触发",
  QUALIFIED: "有效信号",
  PAPER_FILLED: "纸上成交",
  NO_FILL: "未成交",
  ALREADY_FILLED: "已有持仓",
};

function rowsForBook(levels) {
  if (!levels?.length) return '<tr><td colspan="2">暂无数据</td></tr>';
  return levels.map(([p, size]) => `<tr><td>${escapeHtml(price(p))}</td><td>${escapeHtml(number(size, 2))}</td></tr>`).join("");
}

function renderSignalSummary(signal) {
  const box = $("latest-signal");
  if (!signal) {
    box.dataset.tone = "neutral";
    box.innerHTML = '<span class="signal-kicker">最近信号</span><strong>尚无 UP / DOWN 成交事件</strong><small>所有未触发和未成交原因都会保存</small>';
    return;
  }
  const payload = signal.payload || {};
  const tone = payload.status === "PAPER_FILLED" ? "filled" : payload.qualified ? "qualified" : "rejected";
  const reasons = (payload.reasons || []).map((item) => reasonLabels[item] || item).join("、") || "全部条件通过";
  box.dataset.tone = tone;
  box.innerHTML = `<span class="signal-kicker">最近信号 · ${escapeHtml(signal.outcome || payload.outcome || "--")} · ${escapeHtml(clock(signal.receivedTimeUs))}</span>
    <strong>${escapeHtml(resultLabels[payload.status] || payload.status)} · ${escapeHtml(price(payload.price))} / ${escapeHtml(number(payload.size, 2))}份</strong>
    <small>${escapeHtml(reasons)}</small>`;
}

function renderSignals(signals) {
  const rows = [...(signals || [])].reverse();
  if (!rows.length) {
    $("signal-table").innerHTML = '<tr><td colspan="7">等待 UP / DOWN 成交事件</td></tr>';
    return;
  }
  $("signal-table").innerHTML = rows.map((signal) => {
    const p = signal.payload || {};
    const reasons = (p.reasons || []).map((item) => reasonLabels[item] || item).join("、") || "全部条件通过";
    const tone = p.status === "PAPER_FILLED" ? "filled" : p.status === "NO_FILL" ? "no-fill" : "rejected";
    return `<tr>
      <td>${escapeHtml(clock(signal.receivedTimeUs))}</td>
      <td><span class="outcome-chip" data-outcome="${escapeHtml(signal.outcome)}">${escapeHtml(signal.outcome)}</span></td>
      <td>${escapeHtml(number(p.remainingSeconds, 1))}s</td>
      <td>${escapeHtml(price(p.price))}</td>
      <td>${escapeHtml(number(p.size, 2))}</td>
      <td><span class="event-status" data-tone="${tone}">${escapeHtml(resultLabels[p.status] || p.status)}</span></td>
      <td>${escapeHtml(reasons)}</td>
    </tr>`;
  }).join("");
}

function renderPaper(events) {
  const rows = [...(events || [])].reverse();
  if (!rows.length) {
    $("paper-table").innerHTML = '<tr><td colspan="7">尚无纸上交易</td></tr>';
    return;
  }
  $("paper-table").innerHTML = rows.map((event) => {
    const p = event.payload || {};
    const isFill = event.eventType === "PAPER_FILL";
    const isExit = event.eventType === "PAPER_EXIT";
    const value = isFill
      ? `VWAP ${price(p.entryVwap)}`
      : isExit
        ? `0.99提前卖出 · VWAP ${price(p.exitVwap)}`
        : `${p.won ? "胜" : "负"} · ${escapeHtml(p.winnerOutcome || "--")}`;
    const pnl = isFill ? "--" : `$${number(p.netPnl, 2)}`;
    return `<tr>
      <td>${escapeHtml(clock(event.receivedTimeUs))}</td>
      <td>${escapeHtml(event.marketId)}</td>
      <td><span class="outcome-chip" data-outcome="${escapeHtml(event.outcome)}">${escapeHtml(event.outcome)}</span></td>
      <td>${isFill ? "纸上成交" : isExit ? "提前退出" : "结算"}</td>
      <td>${escapeHtml(number(p.quantity, 2))}</td>
      <td>${value}</td>
      <td>${escapeHtml(pnl)}</td>
    </tr>`;
  }).join("");
}

function renderOutcomeStats(state, outcome) {
  const key = outcome.toLowerCase();
  const counters = state.counters || {};
  const stats = state.paperStatsByOutcome?.[outcome] || {};
  $(`${key}-evaluations`).textContent = number(counters.signalEvaluationsByOutcome?.[outcome], 0);
  $(`${key}-qualified`).textContent = number(counters.qualifiedSignalsByOutcome?.[outcome], 0);
  $(`${key}-fills`).textContent = number(counters.paperFillsByOutcome?.[outcome], 0);
  $(`${key}-no-fills`).textContent = number(counters.paperNoFillsByOutcome?.[outcome], 0);
  $(`${key}-win-loss`).textContent = `${number(stats.wins, 0)}–${number(stats.losses, 0)}`;
  $(`${key}-win-rate`).textContent = `${(Number(stats.winRate || 0) * 100).toFixed(1)}%`;
  const pnl = Number(stats.totalPnl || 0);
  $(`${key}-pnl`).textContent = `${pnl >= 0 ? "+" : "-"}$${Math.abs(pnl).toFixed(2)}`;
}

function renderGate(state) {
  const gate = state.gateMonitor;
  const service = gate?.service || {};
  const generatedAgeMs = gate?.generatedAtUs ? Date.now() - Number(gate.generatedAtUs) / 1000 : Infinity;
  const isLive = service.status === "live" && generatedAgeMs < 45_000;
  const isDirect = service.transport === "DIRECT_WEBSOCKET";
  const status = $("gate-status");
  status.textContent = isLive
    ? `${isDirect ? "直连WS" : "采集中"} · ${clock(gate.generatedAtUs)}`
    : gate ? "数据已停止或陈旧" : "Gate 采集未连接";
  status.dataset.tone = isLive ? "live" : "waiting";

  const markets = [...(gate?.activeMarkets || [])];
  const market = markets.filter((item) => Number(item.remainingSeconds) > 0)
    .sort((left, right) => Number(left.remainingSeconds) - Number(right.remainingSeconds))[0]
    || markets.sort((left, right) => Number(right.endUs) - Number(left.endUs))[0];
  $("gate-market-id").textContent = market ? `市场 ${market.marketId}` : "尚无当前市场";
  $("gate-countdown").textContent = market ? countdown(market.remainingSeconds) : "--:--";
  $("gate-up-ask").textContent = price(market?.outcomes?.UP?.bestAsk);
  $("gate-down-ask").textContent = price(market?.outcomes?.DOWN?.bestAsk);

  const recentTrades = [...(gate?.recentTrades || [])]
    .filter((trade) => !market || trade.marketId === market.marketId);
  const recentCutoff = Date.now() - 10_000;
  for (const outcome of ["UP", "DOWN"]) {
    const key = outcome.toLowerCase();
    const sample = recentTrades.filter((trade) => trade.aggressiveOutcome === outcome
      && Number(trade.receivedTimeUs) / 1000 >= recentCutoff);
    const volume = sample.reduce((sum, trade) => sum + Number(trade.size || 0), 0);
    const notional = sample.reduce((sum, trade) => sum + Number(trade.size || 0) * Number(trade.aggressivePrice || 0), 0);
    $(`gate-${key}-trades`).textContent = number(sample.length, 0);
    $(`gate-${key}-volume`).textContent = number(volume, 2);
    $(`gate-${key}-vwap`).textContent = volume > 0 ? price(notional / volume) : "--";
  }
  const tradeRows = recentTrades.slice(-20).reverse();
  $("gate-trade-table").innerHTML = tradeRows.length ? tradeRows.map((trade) => {
    const inferred = trade.rawSide === "SELL";
    const directOutcome = trade.outcomeClassification === "token_id_direct";
    const directionLabel = trade.aggressiveOutcome === "UP" ? "主动买涨" : "主动买跌";
    return `<tr>
      <td>${escapeHtml(clock(trade.sourceTimeUs || trade.receivedTimeUs))}</td>
      <td><span class="outcome-chip" data-outcome="${escapeHtml(trade.aggressiveOutcome)}">${directionLabel} · ${escapeHtml(trade.aggressiveOutcome)}</span></td>
      <td>${price(trade.aggressivePrice)}</td>
      <td>${escapeHtml(number(trade.size, 2))}</td>
      <td>${escapeHtml(trade.rawSide || "--")}</td>
      <td>${directOutcome
        ? inferred ? "SELL 互补换算 · token ID直接识别" : "BUY · token ID直接识别"
        : inferred ? "SELL 互补换算；原方向按价格匹配" : "BUY；方向按当前两边价格匹配"}</td>
    </tr>`;
  }).join("") : '<tr><td colspan="6">等待 Gate WebSocket 成交</td></tr>';

  const stats = gate?.paperStats || {};
  const counters = gate?.counters || {};
  $("gate-signal-count").textContent = number(counters.signals, 0);
  $("gate-open-count").textContent = number(stats.openPositions, 0);
  $("gate-closed-count").textContent = number(stats.closedTrades, 0);
  $("gate-win-loss").textContent = `${number(stats.wins, 0)}–${number(stats.losses, 0)}`;
  $("gate-win-rate").textContent = `${(Number(stats.winRate || 0) * 100).toFixed(1)}%`;
  const pnl = Number(stats.grossPnlPerShareTotal || 0);
  $("gate-pnl").textContent = `${pnl >= 0 ? "+" : "-"}$${Math.abs(pnl).toFixed(2)}`;
  for (const outcome of ["UP", "DOWN"]) {
    const key = outcome.toLowerCase();
    const directionStats = gate?.paperStatsByOutcome?.[outcome] || {};
    $(`gate-${key}-signals`).textContent = number(directionStats.signals, 0);
    $(`gate-${key}-open`).textContent = number(directionStats.openPositions, 0);
    $(`gate-${key}-win-loss`).textContent = `${number(directionStats.wins, 0)}–${number(directionStats.losses, 0)}`;
    $(`gate-${key}-win-rate`).textContent = `${(Number(directionStats.winRate || 0) * 100).toFixed(1)}%`;
    const directionPnl = Number(directionStats.grossPnlPerShareTotal || 0);
    $(`gate-${key}-pnl`).textContent = `${directionPnl >= 0 ? "+" : "-"}$${Math.abs(directionPnl).toFixed(2)}`;
  }

  const rows = [...(gate?.recentPaperEvents || [])].reverse();
  if (!rows.length) {
    $("gate-paper-table").innerHTML = '<tr><td colspan="7">尚无 Gate 纸上交易；等待价格在剩余90–30秒进入0.80–0.85</td></tr>';
    return;
  }
  $("gate-paper-table").innerHTML = rows.map((event) => {
    const isSignal = event.eventType === "GATE_PAPER_SIGNAL";
    const result = isSignal
      ? `入场 ${price(event.entryPrice)}`
      : `${event.won ? "胜" : "负"} · 结果 ${escapeHtml(event.winner || "--")}`;
    const pnlText = isSignal ? "--" : `${Number(event.grossPnlPerShare) >= 0 ? "+" : ""}$${number(event.grossPnlPerShare, 4)}`;
    return `<tr>
      <td>${escapeHtml(clock(event.receivedTimeUs))}</td>
      <td>${escapeHtml(event.marketId)}</td>
      <td><span class="outcome-chip" data-outcome="${escapeHtml(event.outcome)}">${escapeHtml(event.outcome)}</span></td>
      <td>${isSignal ? "纸上入场" : "官方结算"}</td>
      <td>${isSignal ? `${escapeHtml(number(event.remainingSeconds, 1))}s` : "--"}</td>
      <td>${result}</td>
      <td>${escapeHtml(pnlText)}</td>
    </tr>`;
  }).join("");
}

function render(state) {
  const service = state.service || {};
  const [label, tone] = statusLabels[service.status] || [service.status || "未知", "waiting"];
  $("status-label").textContent = label;
  $("status-dot").dataset.tone = tone;
  $("last-message").textContent = service.lastMessageAtUs ? `最后消息 ${clock(service.lastMessageAtUs)}` : "等待官方数据";
  $("data-dir").textContent = service.dataDir || "data/live_monitor";

  const market = state.market;
  $("market-heading").textContent = market?.title || "等待 BTC 5分钟市场";
  $("market-slug").textContent = market?.slug || "尚未发现市场";
  $("countdown").textContent = market ? countdown(market.remainingSeconds) : "--:--";
  const up = market?.outcomes?.UP || {};
  const down = market?.outcomes?.DOWN || {};
  $("up-ask").textContent = price(up.bestAsk);
  $("up-size").textContent = number(up.bestAskSize, 2);
  $("up-age").textContent = age(up.bookAgeMs);
  $("down-ask").textContent = price(down.bestAsk);
  $("down-size").textContent = number(down.bestAskSize, 2);
  $("down-age").textContent = age(down.bookAgeMs);
  $("down-asks").innerHTML = rowsForBook(down.asks);
  $("down-bids").innerHTML = rowsForBook(down.bids);
  $("book-health").textContent = down.bookAgeMs == null ? "等待盘口" : Number(down.bookAgeMs) <= 2000 ? "盘口正常" : "盘口陈旧";

  const stats = state.paperStats || {};
  const counters = state.counters || {};
  $("closed-count").textContent = number(stats.closedTrades, 0);
  $("win-rate").textContent = `${(Number(stats.winRate || 0) * 100).toFixed(1)}%`;
  $("win-loss").textContent = `${number(stats.wins, 0)} 胜 / ${number(stats.losses, 0)} 负`;
  const pnl = Number(stats.totalPnl || 0);
  $("total-pnl").textContent = `${pnl >= 0 ? "+" : "-"}$${Math.abs(pnl).toFixed(2)}`;
  $("qualified-count").textContent = number(counters.qualifiedSignals, 0);
  $("no-fill-count").textContent = `${number(counters.paperNoFills, 0)} 次未成交`;
  $("raw-count").textContent = number(counters.rawMessages, 0);
  $("event-count").textContent = number(counters.normalizedEvents, 0);
  $("received-count").textContent = number(counters.receivedMessages, 0);
  $("evaluation-count").textContent = number(counters.signalEvaluations, 0);
  $("fill-count").textContent = number(counters.paperFills, 0);
  $("error-count").textContent = number(counters.connectionErrors, 0);
  $("open-position-badge").textContent = `${number(stats.openPositions, 0)} 个持仓`;
  renderOutcomeStats(state, "UP");
  renderOutcomeStats(state, "DOWN");
  renderGate(state);
  if (window.renderCandidates) window.renderCandidates(state);

  const storage = state.storage || {};
  $("storage-status").textContent = storage.freeBytes == null
    ? "磁盘余量暂不可用"
    : `磁盘剩余 ${(storage.freeBytes / 1024 ** 3).toFixed(1)} GiB · 行情/盘口与信号日志无损压缩 · 每60秒检查余量`;
  const storageWarning = $("storage-warning");
  const lowStorage = storage.level && storage.level !== "ok";
  storageWarning.hidden = !lowStorage;
  storageWarning.dataset.level = storage.level || "unknown";
  const storageMessage = lowStorage ? storage.message || "请检查数据盘余量。" : "";
  if (storageWarning.textContent !== storageMessage) storageWarning.textContent = storageMessage;

  const signals = state.recentSignals || [];
  renderSignalSummary(signals.at(-1));
  renderSignals(signals);
  renderPaper(state.recentPaperEvents || []);
  const banner = $("error-banner");
  if (service.lastError) {
    banner.hidden = false;
    banner.textContent = `连接提示：${service.lastError}。监控器会自动重试，错误已写入健康日志。`;
  } else {
    banner.hidden = true;
  }
}

async function refresh() {
  try {
    const response = await fetch("/api/state", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    render(await response.json());
  } catch (error) {
    $("status-label").textContent = "页面连接中断";
    $("status-dot").dataset.tone = "error";
    $("last-message").textContent = String(error);
    if (window.candidateConnectionLost) window.candidateConnectionLost();
  }
}

refresh();
setInterval(refresh, 1000);

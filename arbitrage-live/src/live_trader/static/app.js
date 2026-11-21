const $ = (id) => document.getElementById(id);
let runnerActionPending = false;
const fmt = (n) => new Intl.NumberFormat('zh-CN').format(Number(n || 0));
const bytes = (n) => { const value = Number(n || 0); if (value < 1024) return `${value} B`; if (value < 1024**2) return `${(value/1024).toFixed(1)} KB`; if (value < 1024**3) return `${(value/1024**2).toFixed(1)} MB`; return `${(value/1024**3).toFixed(2)} GB`; };
const pnl = (value) => { if (value === null || value === undefined || value === '') return '—'; const number = Number(value); if (!Number.isFinite(number)) return '—'; const formatted = new Intl.NumberFormat('zh-CN',{minimumFractionDigits:0,maximumFractionDigits:8}).format(Math.abs(number)); return `${number > 0 ? '+' : number < 0 ? '-' : ''}${formatted}U`; };
const pnlClass = (node, value) => { const number = Number(value); node.classList.remove('positive','negative','neutral'); node.classList.add(number > 0 ? 'positive' : number < 0 ? 'negative' : 'neutral'); };
const time = (us) => us ? new Date(Number(us)/1000).toLocaleTimeString('zh-CN', {hour12:false}) : '—';
const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const setText = (id, value) => { const node = $(id); if (node && node.textContent !== String(value ?? '')) node.textContent = String(value ?? ''); };
const setHtml = (id, value) => { const node = $(id); if (node && node.innerHTML !== value) node.innerHTML = value; };
const eventSummary = (event) => {
  const payload = event.payload || {};
  const reason = String(payload.reason || '').replaceAll('_', ' ');
  switch (event.eventType) {
    case 'MARKET_DEFINITION': return ['发现新市场', payload.question || 'BTC 5 分钟市场'];
    case 'ORDERBOOK_SNAPSHOT': return ['盘口更新', `DOWN 卖一 ${payload.down_price_now || payload.bestAsk || '—'}`];
    case 'MARKET_SNAPSHOT_1S': return ['策略快照', `剩余 ${payload.remainingSeconds || '—'}s · 卖一 ${payload.bestAsk || '—'}`];
    case 'TRADE': return ['主动成交', `${payload.price || '—'} · ${payload.quantity || '—'} 份 · ${payload.aggressor || '—'}`];
    case 'SIGNAL_ACCEPTED': return ['策略条件满足', `DOWN ${payload.price || '—'}，进入执行检查`];
    case 'SIGNAL_REJECTED': return ['策略未通过', reason || '未满足入场条件'];
    case 'SIGNAL_NOT_SUBMITTED': return ['信号未下单', reason || '当前模式或安全开关阻止提交'];
    case 'ORDER_REQUEST': return ['订单请求', `FAK ${payload.quantity || '—'} 份 · 限价 ${payload.limitPrice || '—'} · 等待官方回执`];
    case 'ORDER_RESPONSE': return ['CLOB 响应', `${payload.state || payload.status || '已返回'}${payload.submitLatencyMs != null ? ` · ${payload.submitLatencyMs}ms` : ''}`];
    case 'ORDER_REJECTED': {
      if (payload.requestSent === false || payload.phase === 'prepare') {
        return ['订单未发送', `${payload.reason || '参数、签名或准备失败'} · 无需对账，修复后重新启动`];
      }
      const depth = payload.depth || {};
      const depthDetail = depth.eligibleCostWithFees ? `可用深度约 ${depth.eligibleQuantity || '—'} 份 / ${depth.eligibleCostWithFees}U，最低 ${depth.minimumValidCost || '—'}U` : '';
      return ['订单拒绝', `${reason || 'CLOB 或风控拒绝'}${depthDetail ? ` · ${depthDetail}` : ''}`];
    }
    case 'ORDER_UNKNOWN': return ['订单状态未知', `无法确认官方是否接单 · 必须先对账${payload.submissionError?.message ? ` · ${payload.submissionError.message}` : ''}`];
    case 'ORDER_PREPARATION_FAILED': return ['订单准备失败', `${payload.message || '订单未发送'} · 修复后重新启动`];
    case 'ORDER_RECONCILED_NO_REMOTE': return ['订单已对账', '官方无远端记录 · 未成交'];
    case 'REFERENCE_PRICE': return ['参考价格更新', `${event.venue || '参考源'} ${payload.value || payload.close || '—'}`];
    case 'HEALTH_STATUS': return ['服务启动', payload.status || '后台运行中'];
    case 'HEALTH_STOPPED': return ['服务停止', payload.status || '已停止'];
    case 'POLYMARKET_FEED_ERROR': return ['Market WS 异常', payload.detail || '正在重连'];
    case 'BINANCE_FEED_ERROR': return ['Binance 连接异常', payload.detail || '正在重连'];
    default: return ['运行状态更新', payload.detail || payload.status || '后台已记录'];
  }
};
const executionKind = (event) => {
  const type = String(event.eventType || '').toUpperCase();
  if (['FILL','PAPER_FILL','SETTLEMENT'].includes(type) || type.startsWith('ORDER_') || type.startsWith('SIGNAL_')) {
    return event.paperOnly ? '纸上' : '实盘';
  }
  return '';
};
const signalReason = (reason) => ({
  remaining_above_window:'尚未进入剩余 90–30 秒窗口',
  remaining_below_window:'已进入最后 30 秒，禁止入场',
  stale_market_data:'Polymarket 盘口超过 2 秒，行情陈旧',
  stale_binance_data:'Binance 参考行情陈旧',
  active_trade_not_down_buy:'不是官方主动 DOWN BUY',
  active_trade_not_up_buy:'不是官方主动 UP BUY',
  active_trade_below_threshold:'官方主动成交数量不足',
  active_trade_below_dynamic_threshold:'主动成交不足以覆盖本次计划买入量',
  stale_binance_data:'历史记录：旧版 Binance 新鲜度门禁（当前不阻断）',
  binance_not_down:'历史记录：旧版 Binance 下跌门槛（当前已移除）',
  entry_vwap_below_min:'预计 VWAP 低于 0.80',
  price_above_max:'价格超过 0.85',
  same_direction_momentum_below_min:'DOWN 价格五秒同向涨幅不足 0.01',
  same_direction_up_momentum_below_min:'UP 价格五秒同向涨幅不足 0.01',
  down_price_reversed:'DOWN 价格在入场前发生逆向回落',
  up_price_reversed:'UP 价格在入场前发生逆向回落',
  strategy_side_mismatch:'成交方向不在当前策略选择内',
  snapshot_from_future:'行情时间戳异常',
  one_order_per_market:'该市场已有纸上或实盘记录',
  order_already_processed:'同一订单请求已有结果，不会重复提交',
  canary_session_limit:'canary 本次会话额度已用完',
  trading_paused:'新订单提交已暂停',
  preflight_blocked:'启动前检查未通过',
  'insufficient depth for minimum valid FAK cost':'0.85 价格上限内盘口深度不足 2.5U',
  'CLOB V2 market fee lookup failed':'CLOB 手续费查询失败，订单未发送',
  'CLOB submit failed after request; reconcile official status before retry':'CLOB 请求后状态未知，需先对账',
  runner_stopped:'runner 已停止',
  heartbeat_stale:'最后心跳超时，runner 可能异常退出'
}[reason] || (String(reason || '').startsWith('CLOB 拒单') ? String(reason) : String(reason || '').replaceAll('_', ' ')) || '未提供原因');
function render(state) {
  const runnerStatus = String(state.runner?.status || '');
  const stopped = runnerStatus === 'stopped' || runnerStatus === 'exited';
  const mode = stopped ? 'STOPPED' : String(state.runner?.mode || state.mode || 'observe').toUpperCase();
  const ordersDisabled = state.safety?.ordersDisabled !== false;
  const orderBlockReason = String(state.safety?.orderBlockReason || '');
  $('modeBadge').textContent = mode === 'STOPPED' ? 'STOPPED · 已停止' : mode === 'OBSERVE' ? 'OBSERVE · 只读' : ordersDisabled ? `${mode} · 行情 / 订单关闭` : `${mode} · 订单开启`;
  $('modeBadge').className = `badge ${mode === 'OBSERVE' || mode === 'STOPPED' ? 'badge-safe' : ''}`;
  const safetyBanner = $('orderSafetyBanner');
  const safetyTitle = $('orderSafetyTitle');
  const safetyDetail = $('orderSafetyDetail');
  if (safetyBanner && safetyTitle && safetyDetail) {
    safetyBanner.className = `order-safety ${ordersDisabled ? 'closed' : 'open'}`;
    if (mode === 'OBSERVE' || mode === 'STOPPED') {
      safetyTitle.textContent = '真实下单：关闭';
      safetyDetail.textContent = mode === 'STOPPED' ? '程序已停止；不会接收行情，也不会提交订单。' : 'observe 只读采集，只记录纸上结果，不会提交真实订单。';
    } else if (ordersDisabled) {
      safetyTitle.textContent = orderBlockReason === 'submission_paused' ? `${mode} 模式：新订单已暂停` : `${mode} 模式：订单闸门关闭`;
      safetyDetail.textContent = orderBlockReason === 'submission_paused'
        ? '行情和预检已经通过；下方“新订单提交”仍为暂停。点击“允许新订单”并确认后才会提交真实订单。'
        : orderBlockReason === 'waiting_for_freshness'
          ? '正在等待 Polymarket 行情新鲜度验证，验证完成前不会提交订单。'
          : 'CLOB transport、预检或安全开关尚未全部满足；当前不会下单。';
    } else {
      safetyTitle.textContent = `${mode} 模式：真实订单已允许`;
      safetyDetail.textContent = '当前可能提交真实订单；请确认预算、方向和风险限额。';
    }
  }
  const serviceStatus = $('serviceStatus');
  const healthText = $('healthText');
  const statusDot = $('statusDot');
  if (serviceStatus) serviceStatus.textContent = stopped ? 'STOPPED' : String(state.service?.status || 'UNKNOWN').replaceAll('_',' ').toUpperCase();
  if (healthText) healthText.textContent = state.service?.health || '等待健康事件';
  $('lastUpdate').textContent = state.service?.lastEventAtUs ? `更新 ${time(state.service.lastEventAtUs)}` : '暂无事件';
  if (statusDot) statusDot.style.background = state.safety?.ordersDisabled ? 'var(--green)' : 'var(--red)';
  const overview = state.overview || {};
  $('overviewMode').textContent = overview.modeLabel || mode;
  $('overviewService').textContent = overview.serviceLabel || '等待启动';
  $('overviewOrderGate').textContent = overview.orderGate || '只读关闭';
  $('overviewMarketFeed').textContent = overview.marketFeed || '等待';
  $('overviewBinanceFeed').textContent = state.market?.binanceSpotReturn5m ? '参考已接收（可选）' : '可选参考 · 未更新';
  $('overviewReferenceFeed').textContent = overview.referenceFeed || '默认关闭';
  const s = state.strategy || {}; const items = [['时间窗口',s.window],['预计 VWAP',s.vwapBand],['最坏成交价',s.priceCap],['单笔预算',s.budget],['最低有效',s.minFill],['触发',s.trigger],['5秒同向动量',s.momentum],['方向',s.name ? s.name.replace(/^BTC 5m\s*/, '') : 'DOWN']];
  setHtml('strategyGrid', items.map(([k,v])=>`<div class="strategy-item"><span>${k}</span><strong>${v || '—'}</strong></div>`).join(''));
  const market = state.market || {}; const marketValue = (value, fallback = '—') => value === undefined || value === null || value === '' ? fallback : String(value);
  const marketAge = Number(market.ageSeconds);
  const marketAgeText = Number.isFinite(marketAge) ? (marketAge < 60 ? `${marketAge.toFixed(1)} 秒前` : `${Math.floor(marketAge / 60)} 分钟前`) : '时间未知';
  // Never present persisted quotes as if they were actionable.  The backend
  // intentionally retains historical snapshots for audit/replay, but the
  // live card must go blank whenever the runner is stopped or freshness fails.
  const liveQuotes = !stopped && market.realtime === true;
  $('marketStatus').textContent = stopped && market.updatedAtUs ? '历史快照 · 非实时' : market.realtime ? 'LIVE · 只读' : market.connected ? '行情陈旧' : '等待行情';
  $('marketFeed').textContent = stopped && market.updatedAtUs ? `runner 已停止 · 最后行情来自 ${marketAgeText} · 价格已隐藏` : !liveQuotes && market.connected ? `Market WS 行情陈旧（${marketAgeText}）· 价格已隐藏` : (market.feed || '公开 Market WebSocket · 被动推送 · 只读');
  $('marketPageRefresh').textContent = stopped ? 'runner 已停止 · 当前数值不会更新' : `约每 ${market.pageRefreshSeconds || 2.5} 秒刷新`;
  $('marketId').textContent = marketValue(market.marketId);
  const identity = [market.question || market.slug, market.slug ? `slug ${market.slug}` : '', market.conditionId ? `condition ${market.conditionId}` : ''].filter(Boolean).join(' · ');
  $('marketQuestion').textContent = marketValue(identity, '尚未发现 BTC 5m 市场');
  $('marketLiveQuestion').textContent = marketValue(market.question || market.slug, '尚未发现 BTC 5m 市场');
  $('marketLiveSlug').textContent = marketValue(market.slug);
  const remaining = Number(market.remainingSeconds);
  $('marketLiveCountdown').textContent = liveQuotes && Number.isFinite(remaining) ? `${Math.max(0, Math.floor(remaining / 60)).toString().padStart(2, '0')}:${Math.max(0, Math.floor(remaining % 60)).toString().padStart(2, '0')}` : '—';
  const tokenIds = market.tokenIds || {};
  $('marketIdentityMeta').textContent = `UP token ${marketValue(tokenIds.UP)} · DOWN token ${marketValue(tokenIds.DOWN)} · 数据源 Polymarket Market WS`;
  const quotes = market.quotes || {};
  const upQuote = liveQuotes ? (quotes.UP || {}) : {};
  const downQuote = liveQuotes ? (quotes.DOWN || {}) : {};
  $('marketUpBid').textContent = marketValue(upQuote.bestBid);
  $('marketUpAsk').textContent = marketValue(upQuote.bestAsk);
  $('marketUpAskSize').textContent = marketValue(upQuote.bestAskSize);
  $('marketUpVwap').textContent = marketValue(upQuote.vwap10, '深度不足');
  $('marketUpLast').textContent = marketValue(upQuote.lastTradePrice);
  $('marketDownBid').textContent = liveQuotes ? marketValue(downQuote.bestBid || market.bestBid) : '—';
  $('marketDownAsk').textContent = liveQuotes ? marketValue(downQuote.bestAsk || market.bestAsk) : '—';
  $('marketDownAskSize').textContent = liveQuotes ? marketValue(downQuote.bestAskSize) : '—';
  $('marketDownVwap').textContent = marketValue(downQuote.vwap10, '深度不足');
  $('marketDownLast').textContent = marketValue(downQuote.lastTradePrice);
  const resultPrice = (quote) => {
    const value = quote.bestAsk || quote.lastTradePrice;
    const number = Number(value);
    return Number.isFinite(number) && number > 0 ? number : null;
  };
  const upResult = resultPrice(upQuote);
  const downResult = resultPrice(downQuote);
  const resultLabel = (value) => value === null ? '—' : value.toFixed(4);
  const resultPercent = (value, quote) => value === null ? '—' : `${(value * 100).toFixed(1)}% · ${quote.bestAsk ? '盘口卖一' : '最新成交'}`;
  $('resultUpPrice').textContent = resultLabel(upResult);
  $('resultDownPrice').textContent = resultLabel(downResult);
  $('resultUpPercent').textContent = resultPercent(upResult, upQuote);
  $('resultDownPercent').textContent = resultPercent(downResult, downQuote);
  if (upResult !== null && downResult !== null) {
    const sum = upResult + downResult;
    $('resultSum').textContent = sum.toFixed(4);
    $('resultSumNote').textContent = `${(sum * 100).toFixed(1)}% · ${Math.abs(sum - 1) <= 0.03 ? '接近 100%' : '受价差/更新时间影响'}`;
  } else {
    $('resultSum').textContent = '—';
    $('resultSumNote').textContent = '等待两边行情';
  }
  $('marketBestBid').textContent = liveQuotes ? marketValue(market.bestBid) : '—';
  $('marketBestAsk').textContent = liveQuotes ? marketValue(market.bestAsk) : '—';
  $('marketLastTrade').textContent = liveQuotes ? marketValue(market.lastTradePrice) : '—';
  $('marketTradeMeta').textContent = liveQuotes && market.lastTradePrice ? `${marketValue(market.lastTradeOutcome, '—')} · ${marketValue(market.lastTradeSide, '—')} · 数量 ${marketValue(market.lastTradeQuantity)}` : '仅实时连接时显示';
  $('marketRemaining').textContent = liveQuotes && market.remainingSeconds ? `${market.remainingSeconds}s` : '—';
  $('marketUpdated').textContent = market.updatedAtUs ? `最后更新 ${time(market.updatedAtUs)} · ${marketAgeText}` : '更新时间 —';
  $('marketBinanceReturn').textContent = liveQuotes ? marketValue(market.binanceSpotReturn5m) : '—';
  $('marketClobStatus').textContent = market.clobTransport === 'disabled' ? 'CLOB 已关闭' : marketValue(market.clobTransport);
  const decision = state.strategyDecision || {};
  const decisionHistorical = stopped && decision.receivedTimeUs;
  const decisionType = String(decision.eventType || '');
  $('lastSignalStatus').textContent = !decisionType ? '尚无判断' : decisionType === 'SIGNAL_ACCEPTED' || decisionType === 'SIGNAL_PAPER_EXECUTED' ? `${decisionHistorical ? '历史 · ' : ''}条件满足` : decisionType === 'SIGNAL_NOT_SUBMITTED' ? `${decisionHistorical ? '历史 · ' : ''}满足但未提交` : `${decisionHistorical ? '历史 · ' : ''}未满足`;
  const decisionDetail = ['SIGNAL_ACCEPTED','SIGNAL_PAPER_EXECUTED'].includes(decisionType) ? '全部策略硬条件已通过' : signalReason(decision.reason);
  $('lastSignalReason').textContent = !decisionType ? '启动 observe 后，满足或拒绝原因会显示在这里。' : `${decision.outcome || '—'} · ${decisionDetail} · ${time(decision.receivedTimeUs)}`;
  const refs = state.referencePrices || {};
  const ref = (name) => refs[name] || {};
  const renderRef = (valueId, metaId, item, fallback = '—') => {
    $(valueId).textContent = marketValue(item.value, fallback);
    const windowLabel = item.windowSeconds ? ` · ${item.windowSeconds}s` : '';
    $(metaId).textContent = item.updatedAtUs ? `更新 ${time(item.updatedAtUs)}${windowLabel}` : (item.value ? `已接收${windowLabel}` : '等待 RTDS');
  };
  renderRef('referenceChainlink', 'referenceChainlinkMeta', ref('chainlink'));
  renderRef('referenceTwap30', 'referenceTwap30Meta', ref('chainlink_twap'));
  renderRef('referenceBinance', 'referenceBinanceMeta', ref('binance'));
  const c = state.counts || {}; const byKind = state.countsByKind || {}; $('signalsCount').textContent=fmt(c.signals); $('ordersCount').textContent=fmt(c.orders); $('paperFillsCount').textContent=fmt(byKind.paper?.fills); $('liveFillsCount').textContent=fmt(byKind.live?.fills); $('paperSettlementsCount').textContent=fmt(byKind.paper?.settlements); $('liveSettlementsCount').textContent=fmt(byKind.live?.settlements);
  const renderPerformance = (kind, stats = {}) => { const cap = kind === 'paper' ? 'paper' : 'real'; const realized = $(`${cap}RealizedPnl`); realized.textContent = pnl(stats.realizedPnl ?? 0); pnlClass(realized, stats.realizedPnl ?? 0); $(`${cap}WinRate`).textContent = stats.winRate === null || stats.winRate === undefined ? '—' : `${stats.winRate}%`; $(`${cap}ReturnRate`).textContent = stats.returnRate === null || stats.returnRate === undefined ? '—' : `${stats.returnRate}%`; $(`${cap}WinsCount`).textContent = fmt(stats.wins); $(`${cap}LossesCount`).textContent = fmt(stats.losses); $(`${cap}SettledCount`).textContent = fmt(stats.settledCount); $(`${cap}OpenPositions`).textContent = `${fmt(stats.openPositions)} 笔`; const estimated = $(`${cap}UnrealizedPnl`); estimated.textContent = stats.valuationStatus === 'priced' ? pnl(stats.unrealizedPnl) : '—'; pnlClass(estimated, stats.valuationStatus === 'priced' ? stats.unrealizedPnl : 0); $(`${cap}ValuationNote`).textContent = stats.valuationStatus === 'priced' ? '按当前买一估值 · 未实现' : stats.valuationStatus === 'awaiting_market' ? '等待对应市场行情或官方结算' : '没有未结算仓位'; };
  const renderDirection = (kind, stats = {}) => {
    const cap = kind === 'paper' ? 'paper' : 'real';
    const bySide = stats.bySide || {};
    for (const side of ['UP', 'DOWN']) {
      const key = side[0] + side.slice(1).toLowerCase();
      const item = bySide[side] || {};
      const pnlNode = $(`${cap}${key}Pnl`);
      pnlNode.textContent = item.realizedPnl === undefined ? '—' : pnl(item.realizedPnl);
      pnlClass(pnlNode, item.realizedPnl ?? 0);
      $(`${cap}${key}WinRate`).textContent = item.winRate == null ? '—' : `${item.winRate}%`;
      $(`${cap}${key}ReturnRate`).textContent = item.returnRate == null ? '—' : `${item.returnRate}%`;
      $(`${cap}${key}SettledCount`).textContent = fmt(item.settledCount);
    }
  };
  renderPerformance('paper', state.performance?.paper); renderPerformance('real', state.performance?.live); renderDirection('paper', state.performance?.paper); renderDirection('real', state.performance?.live);
  const diagnostics = state.executionDiagnostics || {}; const diagRunner = diagnostics.runner || {};
  setText('diagnosticsWindow', diagnostics.window || '最近窗口');
  setText('diagEvaluations', fmt(diagnostics.signalEvaluations)); setText('diagAccepted', fmt(diagnostics.acceptedSignals)); setText('diagRejected', fmt(diagnostics.rejectedSignals + diagnostics.orderRejected)); setText('diagLiveFills', fmt(diagnostics.liveFills));
  const latestExecution = diagnostics.latestExecution || {};
  const latestLabel = latestExecution.eventType === 'ORDER_REJECTED' || latestExecution.eventType === 'SIGNAL_REJECTED' || latestExecution.eventType === 'SIGNAL_NOT_SUBMITTED'
    ? signalReason(latestExecution.reason)
    : latestExecution.eventType ? eventSummary({eventType: latestExecution.eventType, payload:{reason:latestExecution.reason}})[0] : '暂无执行记录';
  setText('diagLatest', latestExecution.eventType ? `最近：${latestLabel}${latestExecution.marketId ? ` · 市场 ${latestExecution.marketId}` : ''} · ${time(latestExecution.receivedTimeUs)}` : '暂无执行记录');
  const reasons = diagnostics.rejectReasons || [];
  setText('diagRejectCount', `${fmt(reasons.reduce((sum, item) => sum + Number(item.count || 0), 0))} 条`);
  setHtml('diagReasons', reasons.length ? reasons.map(item => `<div class="diagnostic-reason-row"><span>${escapeHtml(signalReason(item.reason))}</span><b>${fmt(item.count)}</b></div>`).join('') : '<div class="empty-state compact">暂无拒绝记录</div>');
  const runnerDiagStatus = diagRunner.status || (stopped ? 'stopped' : 'running');
  setText('diagRunnerStatus', runnerDiagStatus === 'running' ? '运行中' : '已停止');
  const heartbeatAge = Number(diagRunner.heartbeatAgeSeconds); const heartbeatText = Number.isFinite(heartbeatAge) ? (heartbeatAge < 60 ? `${heartbeatAge.toFixed(1)} 秒前` : `${Math.floor(heartbeatAge / 60)} 分钟前`) : '—';
  setText('diagHeartbeat', heartbeatText); const uptime = Number(diagRunner.uptimeSeconds); setText('diagUptime', Number.isFinite(uptime) ? (uptime < 60 ? `${uptime.toFixed(0)} 秒` : `${Math.floor(uptime / 60)} 分钟`) : '—');
  setText('diagStopReason', diagRunner.stopReason ? signalReason(diagRunner.stopReason) : runnerDiagStatus === 'running' ? '无' : 'runner 已停止');
  setText('diagStopDetail', diagRunner.stopDetail || (runnerDiagStatus === 'running' ? 'runner 正在运行；若成交为 0，请查看未成交主要原因。' : 'runner 未运行；不会接收新行情或提交订单。'));
  const u = state.usage || {}; $('storageSize').textContent=bytes(u.directoryBytes); $('queueText').textContent=u.rawQueueCapacity ? `${u.rawQueueDepth || 0} / ${u.rawQueueCapacity}` : '运行进程内'; $('droppedText').textContent=fmt(u.rawDropped); $('storageBar').style.width=`${Math.min(100, Math.max(3, Number(u.directoryBytes||0)/1024/1024))}%`;
  const events = (state.recentEvents || []).slice().reverse().slice(0, 10); setText('eventCount', `${events.length} 条`); setHtml('events', events.length ? events.map(e=>{ const [title, detail] = eventSummary(e); const kind = executionKind(e); const side = ['UP','DOWN'].includes(String(e.outcome || '').toUpperCase()) ? String(e.outcome).toUpperCase() : ''; const context = side || (String(e.venue || '').toUpperCase() === 'BINANCE' ? 'BTC 参考行情' : ''); return `<div class="event-row"><div class="event-type">${kind ? `<span class="event-kind">${escapeHtml(kind)}</span> ` : ''}${escapeHtml(title)}</div><div class="event-main">${context ? `<strong>${escapeHtml(context)}</strong>` : ''}<small>${escapeHtml(detail)}</small></div><div class="event-time">${time(e.receivedTimeUs)}</div></div>`; }).join('') : '<div class="empty-state">暂无动态，启动 observe 后会在这里显示。</div>');
}
async function refresh(){ try { const response = await fetch('/api/state', {cache:'no-store'}); if (!response.ok) throw new Error('state'); render(await response.json()); } catch (_) { if ($('serviceStatus')) $('serviceStatus').textContent='DASHBOARD OFFLINE'; if ($('healthText')) $('healthText').textContent='无法读取本地状态'; if ($('statusDot')) $('statusDot').style.background='var(--red)'; } }
const settingFields = ['walletAddress','rpcUrl','proxy','liveMode','strategySide','funder','signatureType','maxBudgetUsd','activeTradeMinQuantity','activeTradeMultiplier','maxDailyLossCount','maxDailyLossTotalUsd'];
const settingValue = (id) => { const node = $(id); return node.type === 'checkbox' ? node.checked : node.value; };
const setSetting = (id, value) => { const node = $(id); if (!node) return; if (node.type === 'checkbox') node.checked = Boolean(value); else node.value = value || ''; };
function settingsPayload(includeSecret = true){ const payload = {}; settingFields.filter(id => id !== 'accountType').forEach(id => { payload[id] = settingValue(id); }); const secret = $('privateKey').value; if (includeSecret && secret) payload.privateKey = secret; return payload; }
function settingsStatus(text, kind = '') { $('settingsStatus').textContent = text; $('settingsStatus').className = `tiny-label ${kind}`; }
function privateKeyFormatMessage(){
  const value = $('privateKey').value.trim();
  if (!value) return '';
  return /^0x[0-9a-fA-F]{64}$/.test(value) ? '' : '私钥格式不符合：请输入 0x 开头、后接 64 个十六进制字符（共 32 字节）。不要把私钥发到聊天。';
}
function updateModeHelp(){
  const mode = $('liveMode').value;
  const text = mode === 'observe'
    ? 'observe：只采集行情、信号和纸上结果，不会提交真实订单。'
    : mode === 'canary'
      ? 'canary：受限实盘行情；当前 CLOB transport 默认关闭，预检和显式开关全部通过后才可能提交订单。'
      : 'live：实盘行情；当前 CLOB transport 默认关闭，预检、凭据和显式开关全部通过后才可能提交订单。';
  $('modeHelp').textContent = `${text} Polymarket / Binance 行情连接由模式自动管理；CLOB 是后台的 Polymarket 订单接口，不需要单独操作。`;
}
async function api(path, options = {}) { const response = await fetch(path, {cache:'no-store', ...options, headers:{'Content-Type':'application/json', ...(options.headers || {})}}); const body = await response.json(); if (!response.ok) throw new Error(body.error || '请求失败'); return body; }
async function loadSettings(){ try { const values = await api('/api/settings'); settingFields.forEach(id => setSetting(id, values[id])); $('accountType').value = values.accountType || ''; const signer = values.clobIdentitySummary?.signerAddress || ''; $('signerAddress').value = signer; if (signer && Number(values.signatureType) === 3) $('walletAddress').value = signer; $('privateKey').value = ''; settingsStatus(values.clobIdentityConfigured ? 'CLOB 身份已配置' : (values.privateKeyConfigured ? '私钥已配置，待派生 CLOB 身份' : '私钥未配置'), values.clobIdentityConfigured || values.privateKeyConfigured ? 'ok' : ''); } catch (error) { settingsStatus(error.message, 'error'); } }
async function saveSettings(){ try { const formatError = privateKeyFormatMessage(); if (formatError) { settingsStatus(formatError, 'error'); return; } const saved = await api('/api/settings', {method:'POST', body:JSON.stringify(settingsPayload(true))}); $('privateKey').value = ''; settingFields.forEach(id => setSetting(id, saved[id])); $('accountType').value = saved.accountType || ''; settingsStatus(saved.privateKeyConfigured ? '已保存 · Keychain' : '已保存', 'ok'); } catch (error) { settingsStatus(error.message, 'error'); } }
async function detectAccount(){ try { const formatError = privateKeyFormatMessage(); if (formatError) { $('accountResult').textContent = formatError; $('accountResult').className = 'inline-status error'; return; } $('accountResult').textContent = '正在通过 Polymarket CLOB 派生身份…'; $('accountResult').className = 'inline-status'; const result = await api('/api/account/configure', {method:'POST', body:JSON.stringify({walletAddress:$('walletAddress').value, funder:$('funder').value, signatureType:Number($('signatureType').value), privateKey:$('privateKey').value, proxy:$('proxy').value})}); $('privateKey').value = ''; $('accountType').value = result.accountType || ''; $('signerAddress').value = result.signerAddress || ''; if (result.signerAddress && Number(result.signatureType) === 3) $('walletAddress').value = result.signerAddress; if (result.funderAddress) $('funder').value = result.funderAddress; if (result.signatureType !== null && result.signatureType !== undefined) $('signatureType').value = String(result.signatureType); $('accountResult').textContent = result.status === 'OK' ? `${result.accountType} · signer ${result.signerAddress || '—'} · 请与 MetaMask 当前地址核对` : (result.error || '身份配置失败'); $('accountResult').className = `inline-status ${result.status === 'OK' ? 'ok' : 'error'}`; settingsStatus(result.status === 'OK' ? '身份已配置 · Keychain' : (result.error || '身份配置失败'), result.status === 'OK' ? 'ok' : 'error'); } catch (error) { $('accountResult').textContent = error.message; $('accountResult').className = 'inline-status error'; } }
async function validateIdentity(){ try { $('accountResult').textContent = '正在验证 CLOB signer、Funder、余额和账户状态（不下单）…'; $('accountResult').className = 'inline-status'; const result = await api('/api/account/validate', {method:'POST', body:JSON.stringify({proxy:$('proxy').value})}); $('signerAddress').value = result.signerAddress || $('signerAddress').value; $('accountResult').textContent = result.ok ? `${result.message} · pUSD ${result.collateralBalance}` : (result.message || '身份验证失败'); $('accountResult').className = `inline-status ${result.ok ? 'ok' : 'error'}`; } catch (error) { $('accountResult').textContent = error.message; $('accountResult').className = 'inline-status error'; } }
async function runPreflight(){ try { const result = await api('/api/preflight'); const entries = Object.values(result.checks || {}); $('preflightList').innerHTML = entries.map(item => `<div class="preflight-item ${item.passed ? 'pass' : 'fail'}"><strong>${item.passed ? '✓' : '!' } ${item.label}</strong><small>${item.detail}</small></div>`).join(''); $('accountResult').textContent = result.safe ? '预检通过，可以按当前模式启动' : '预检未通过，修正红色项目后再启动'; $('accountResult').className = `inline-status ${result.safe ? 'ok' : 'error'}`; } catch (error) { $('accountResult').textContent = error.message; $('accountResult').className = 'inline-status error'; } }
function setRunnerActionPending(action, pending) {
  const startButton = $('startRunner');
  const stopButton = $('stopRunner');
  runnerActionPending = pending;
  startButton.disabled = pending;
  stopButton.disabled = pending;
  startButton.textContent = pending && action === 'start' ? '正在启动…' : '启动当前模式';
  stopButton.textContent = pending && action === 'stop' ? '正在停止…' : '全部停止';
  [startButton, stopButton].forEach(button => {
    if (pending) button.setAttribute?.('aria-busy', 'true');
    else button.removeAttribute?.('aria-busy');
  });
}
async function runnerAction(path){
  if (runnerActionPending) return;
  const starting = path.endsWith('/start');
  const action = starting ? 'start' : 'stop';
  if (starting && $('liveMode').value !== 'observe') {
    const label = $('liveMode').value === 'canary' ? '受限实盘' : '实盘';
    if (!window.confirm(`即将启动${label}监控。只有“允许新订单”已开启且全部安全检查通过时，系统才会提交真实订单。确认继续？`)) return;
  }
  setRunnerActionPending(action, true);
  settingsStatus(starting
    ? '正在连接 Polymarket / Binance 行情并执行完整预检，通常需要 30–120 秒；请勿重复点击，检查通过前订单保持关闭…'
    : '正在安全停止后台行情与交易管线，请稍候…');
  try {
  // The mode selector is authoritative. Persist it at start time so an
  // operator who selected observe and immediately clicked start cannot
  // accidentally launch the previously saved canary/live mode.
  if (starting) {
    await api('/api/settings', {method:'POST', body:JSON.stringify({liveMode:$('liveMode').value})});
  }
  const result = await api(path, {method:'POST', body:'{}'}); const status = result.status || 'unknown'; const mode = String(result.mode || '').toUpperCase(); settingsStatus(status === 'running' ? `${mode || '当前模式'}运行中 · PID ${result.pid || '—'}` : '当前运行已停止；行情停止后变旧属于正常现象', status === 'running' ? 'ok' : ''); await refreshControl(); await refresh();
  } catch (error) {
    settingsStatus(`${starting ? '启动' : '停止'}失败：${error.message}。请检查下方预检与连接状态后重试。`, 'error');
  } finally {
    setRunnerActionPending(action, false);
  }
}
async function refreshRunner(){ try { const result = await api('/api/runner'); if (result.status === 'running') settingsStatus(`当前模式运行中 · PID ${result.pid || '—'}`, 'ok'); await refreshControl(); } catch (_) { /* dashboard refresh reports the primary status */ } }
async function refreshControl(){ try {
  const state = await api('/api/control');
  const running = state.runner?.status === 'running';
  $('collectionStatus').textContent = !running ? 'runner 未运行' : state.dataRecordingEnabled ? '高频记录中' : '高频记录已暂停';
  $('toggleCollection').textContent = state.dataRecordingEnabled ? '暂停高频记录' : '开启高频记录';
  const canTrade = state.mode !== 'observe';
  const tradeEnabled = Boolean(state.effectiveOrderSubmissionEnabled);
  $('toggleTrading').disabled = !canTrade;
  $('toggleTrading').textContent = tradeEnabled ? '暂停新订单' : '允许新订单';
  $('tradingStatus').textContent = !canTrade ? 'observe 模式不提交真实订单' : !running ? 'runner 未运行；请先启动当前模式' : tradeEnabled ? '允许新订单提交' : '新订单已暂停';
} catch (_) { $('collectionStatus').textContent = '控制状态不可用'; } }
async function toggleCollection(){
  const current = await api('/api/control');
  const next = !current.dataRecordingEnabled;
  if (!next && !window.confirm('暂停后只停止高频原始行情、特征和信号记录；网络监控仍会继续，订单、成交、结算等关键账本仍会可靠写入。确认暂停高频记录？')) return;
  try { await api('/api/control', {method:'POST', body:JSON.stringify({dataRecordingEnabled: next})}); await refreshControl(); } catch (error) { settingsStatus(error.message, 'error'); }
}
async function toggleTrading(){
  const current = await api('/api/control');
  const next = !current.effectiveOrderSubmissionEnabled;
  if (next && $('liveMode').value === 'observe') return;
  if (next && current.runner?.status !== 'running') {
    settingsStatus('当前 runner 未运行；请先点击“启动当前模式”，等行情新鲜度和预检通过后再允许新订单。', 'error');
    return;
  }
  if (next && !window.confirm('开启后，当前运行模式允许提交新的真实订单；运行模式本身仍是权限上限，observe 永远不会下单。确认允许新订单？')) return;
  try { await api('/api/control', {method:'POST', body:JSON.stringify({orderSubmissionEnabled: next})}); await refreshControl(); } catch (error) { settingsStatus(error.message, 'error'); }
}
const orderStateLabel = (state) => ({intent:'准备提交',submitting:'提交中断 · 待确认',unknown:'官方状态未知',acknowledged:'官方已接收',partial:'部分成交',filled:'已成交待结算',canceled:'已关闭',rejected:'已拒绝',disabled:'未发送'}[state] || state || '未知');
async function loadRecoveryOrders(){
  try {
    const result = await api('/api/orders/recovery');
    const orders = result.orders || [];
    $('recoveryBadge').textContent = result.unresolvedCount ? `${result.unresolvedCount} 笔需处理` : '无待处理订单';
    $('recoveryBadge').className = `tiny-label ${result.unresolvedCount ? 'error' : 'ok'}`;
    const rows = orders.slice(0, 12).map(order => {
      const id = escapeHtml(order.clientOrderId);
      const remote = order.remoteOrderId ? escapeHtml(order.remoteOrderId) : '无官方订单号';
      const action = order.canConfirmNoRemote
        ? `<button class="button danger reconcile-button" type="button" data-reconcile-id="${id}">我已确认官方无订单</button>`
        : `<span class="order-state">${escapeHtml(orderStateLabel(order.state))}</span>`;
      return `<div class="recovery-order ${order.needsAttention ? 'attention' : ''}">
        <div class="order-id"><strong title="${id}">${id}</strong><small>${remote}</small></div>
        <div class="recovery-cell"><small>状态</small><b>${escapeHtml(orderStateLabel(order.state))}</b></div>
        <div class="recovery-cell"><small>市场 / 方向</small><b>${escapeHtml(order.marketId)} · ${escapeHtml(order.side || '—')}</b></div>
        <div class="recovery-cell"><small>数量</small><b>${escapeHtml(order.quantity || '—')} 份</b></div>
        <div class="recovery-cell"><small>预计成本</small><b>${escapeHtml(order.expectedCost || '—')}U</b></div>${action}</div>`;
    }).join('');
    setHtml('recoveryOrders', rows || '<div class="empty-state compact">没有本地订单记录。</div>');
    document.querySelectorAll('[data-reconcile-id]').forEach(button => button.addEventListener('click', () => openRecoveryDialog(button.dataset.reconcileId)));
  } catch (error) {
    $('recoveryBadge').textContent = '读取失败';
    $('recoveryBadge').className = 'tiny-label error';
    $('recoveryStatus').textContent = error.message;
    $('recoveryStatus').className = 'inline-status error';
  }
}
function openRecoveryDialog(clientOrderId){
  $('recoveryTargetId').value = clientOrderId;
  $('recoveryConfirmation').value = '';
  $('recoveryDialogStatus').textContent = '请先核对 Polymarket 官方订单和成交记录；确认无记录后输入完整 ID。';
  $('recoveryDialogStatus').className = 'inline-status';
  $('confirmRecovery').disabled = true;
  $('recoveryDialog').showModal();
  $('recoveryConfirmation').focus();
}
async function confirmNoRemoteOrder(){
  const clientOrderId = $('recoveryTargetId').value;
  const button = $('confirmRecovery');
  button.disabled = true;
  button.textContent = '正在保存对账…';
  try {
    const result = await api('/api/orders/reconcile-no-remote', {method:'POST', body:JSON.stringify({clientOrderId, confirmation:$('recoveryConfirmation').value.trim(), officialNoRecordConfirmed:true})});
    $('recoveryDialog').close();
    $('recoveryStatus').textContent = result.message || '对账完成';
    $('recoveryStatus').className = 'inline-status ok';
    await loadRecoveryOrders();
    await runPreflight();
  } catch (error) {
    $('recoveryDialogStatus').textContent = error.message;
    $('recoveryDialogStatus').className = 'inline-status error';
    $('recoveryStatus').textContent = error.message;
    $('recoveryStatus').className = 'inline-status error';
  } finally {
    button.textContent = '确认无记录并关闭';
    button.disabled = $('recoveryConfirmation').value.trim() !== $('recoveryTargetId').value;
  }
}
$('saveSettings').addEventListener('click', saveSettings); $('detectAccount').addEventListener('click', detectAccount); $('runPreflight').addEventListener('click', runPreflight); $('startRunner').addEventListener('click', () => runnerAction('/api/runner/start')); $('stopRunner').addEventListener('click', () => runnerAction('/api/runner/stop'));
$('validateIdentity').addEventListener('click', validateIdentity);
$('toggleCollection').addEventListener('click', toggleCollection); $('toggleTrading').addEventListener('click', toggleTrading);
$('cancelRecovery').addEventListener('click', () => $('recoveryDialog').close());
$('recoveryConfirmation').addEventListener('input', () => { $('confirmRecovery').disabled = $('recoveryConfirmation').value.trim() !== $('recoveryTargetId').value; });
$('confirmRecovery').addEventListener('click', confirmNoRemoteOrder);
$('liveMode').addEventListener('change', updateModeHelp);
refresh(); loadRecoveryOrders(); loadSettings().then(() => { updateModeHelp(); refreshControl(); if ($('walletAddress').value) detectAccount(); }); refreshRunner(); setInterval(refresh, 2500); setInterval(refreshRunner, 2500); setInterval(loadRecoveryOrders, 5000);

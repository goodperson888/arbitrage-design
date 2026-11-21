"use strict";

(() => {
  const get = (id) => document.getElementById(id);
  const esc = (v) => String(v ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
  const num = (v, digits = 2) => v == null ? "—" : Number(v).toLocaleString("zh-CN", { maximumFractionDigits: digits });
  const money = (v) => v == null ? "—" : `${Number(v) >= 0 ? "+" : ""}${Number(v).toFixed(3)}U`;
  const pct = (v) => v == null ? "—" : `${(Number(v) * 100).toFixed(1)}%`;
  const stamp = (us) => us ? new Date(Number(us) / 1000).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false }) : "—";
  let latest = null;
  let sizing = "fixed";
  const tabs = [get("tab-original"), get("tab-candidates")];
  function selectTab(index, focus = false) {
    tabs.forEach((tab, i) => {
      tab.setAttribute("aria-selected", String(i === index));
      tab.tabIndex = i === index ? 0 : -1;
      get(tab.getAttribute("aria-controls")).hidden = i !== index;
    });
    if (focus) tabs[index].focus();
    if (index === 0) hideHelp();
    history.replaceState(null, "", index === 1 ? "#candidates" : "#original");
  }
  tabs.forEach((tab, index) => {
    tab.addEventListener("click", () => selectTab(index));
    tab.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      selectTab(event.key === "Home" ? 0 : event.key === "End" ? 1 : 1 - index, true);
    });
  });
  if (location.hash === "#candidates") selectTab(1);
  window.addEventListener("hashchange", () => selectTab(location.hash === "#candidates" ? 1 : 0));
  document.querySelectorAll('input[name="candidate-sizing"]').forEach((input) => {
    input.addEventListener("change", () => { sizing = input.value; if (latest) render(latest); });
  });
  const descriptions = {
    S1_net: "1分钟 ATR10×3 翻转 · 含费单位成本≤0.50",
    S1_gross: "1分钟 ATR10×3 翻转 · 名义价格≤0.50",
    D1: "距离时间分数≥1 · ATR14 · 双向",
    S3: "D1 ＋ 1分钟 ATR14×3 趋势一致",
    A5: "ATR归一化锚定距离≥1.0 · 不按剩余时间缩放",
    A6: "ATR归一化锚定距离≥1.5 · 不按剩余时间缩放",
  };
  const entryLogic = {
    S1_net: ["方向触发", "使用已收盘的Binance Spot 1分钟Supertrend（Wilder ATR10×3）。只有真实从空翻多才买UP、从多翻空才买DOWN；一直保持同方向不会重复产生翻转信号。", "价格限制", "决策卖一0.30–0.50；逐档成交时每份价格＋模拟手续费≤0.50，因此名义成交上限略低于0.50。"],
    S1_gross: ["方向触发", "使用已收盘的Binance Spot 1分钟Supertrend（Wilder ATR10×3）真实翻转：从空翻多买UP、从多翻空买DOWN。它与保守版的方向信号完全相同。", "价格限制", "决策卖一0.30–0.50；每一档成交价格≤0.50，手续费另从本笔含费预算中扣除。"],
    D1: ["方向与公式", "分数＝|当前Binance Spot－本场精确Binance开盘价| ÷ [1分钟Wilder ATR14 × √(剩余秒数÷60)]。分数≥1才参与；Spot高于开盘价买UP，低于开盘价买DOWN。", "附加条件", "Spot来源时间和本地接收时间均≤2秒。开盘价只取本场5分钟起点的1分钟K线open；不要求Supertrend方向一致。"],
    S3: ["方向与公式", "先满足D1：|Spot－本场Binance开盘价| ÷ [1分钟Wilder ATR14 × √(剩余秒数÷60)] ≥1，高于开盘价选UP、低于选DOWN。", "趋势确认", "再要求已收盘1分钟Supertrend ATR14×3的已确认方向与所选方向一致。初始趋势种子不算确认，必须已观察到真实翻转；Spot来源与接收时间均≤2秒。"],
    A5: ["方向与公式", "有符号分数 z＝ln(当前Binance Spot÷本场精确Binance开盘价) ÷ (1分钟Wilder ATR14÷当前Spot)。z≥1.0买UP，z≤−1.0买DOWN。", "与D1的区别", "不除以剩余时间的平方根，不要求Supertrend确认或翻转。仍限制剩余270–30秒；Spot来源与接收时间均≤2秒，必须有本场精确Binance开盘价。"],
    A6: ["方向与公式", "有符号分数 z＝ln(当前Binance Spot÷本场精确Binance开盘价) ÷ (1分钟Wilder ATR14÷当前Spot)。z≥1.5买UP，z≤−1.5买DOWN。", "与A5的区别", "仅把ATR距离阈值从1.0提高到1.5，其余执行条件相同；不按剩余时间缩放，不要求Supertrend确认。Spot来源与接收时间均≤2秒，必须有本场精确Binance开盘价。"],
  };
  const help = get("candidate-strategy-help");
  let helpTrigger = null;
  let helpPinned = false;
  let helpSizing = null;
  let hideTimer;
  function hideHelp() {
    clearTimeout(hideTimer);
    if (helpTrigger) {
      helpTrigger.setAttribute("aria-expanded", "false");
      helpTrigger.removeAttribute("aria-describedby");
    }
    help.hidden = true;
    helpTrigger = null;
    helpPinned = false;
  }
  function positionHelp() {
    if (!helpTrigger || help.hidden) return;
    if (!helpTrigger.isConnected) { hideHelp(); return; }
    const anchor = helpTrigger.getBoundingClientRect();
    if (anchor.bottom < 0 || anchor.top > window.innerHeight) { hideHelp(); return; }
    const bounds = help.getBoundingClientRect();
    const left = Math.max(12, Math.min(anchor.left, window.innerWidth - bounds.width - 12));
    const below = anchor.bottom + 8;
    const top = below + bounds.height <= window.innerHeight - 12 ? below : Math.max(12, anchor.top - bounds.height - 8);
    help.style.left = `${left}px`;
    help.style.top = `${top}px`;
  }
  function showHelp(trigger, pinned = false) {
    clearTimeout(hideTimer);
    if (helpTrigger && helpTrigger !== trigger) hideHelp();
    helpTrigger = trigger;
    helpPinned = pinned || helpPinned;
    helpSizing = sizing;
    const rule = trigger.dataset.rule;
    const logic = entryLogic[rule];
    if (!logic) return;
    const html = `<h3>${esc(trigger.dataset.name)} · 详细开仓逻辑</h3>
      <p><strong>${esc(logic[0])}：</strong>${esc(logic[1])}</p>
      <p><strong>${esc(logic[2])}：</strong>${esc(logic[3])}</p>
      <p><strong>共同入场限制：</strong>剩余270–30秒，决策卖一0.30–0.50；完整盘口的来源和本地接收时间均≤2秒。限价内必须足够本笔完整含费预算，份额向下取整到0.01。</p>
      <p><strong>实时与成交：</strong>每秒判断；只用已收到的收盘1分钟指标，最早收盘后1秒生效，原收盘边界后60秒失效。提交后至少等待300ms，再检查当时盘口新鲜度与深度；执行晚于计划超过1秒记未成交。已提交的同场同方向不补单。</p>
      <p><strong>资金与退出：</strong>${sizing === "fixed" ? "固定账户初始100U，每笔1U含费预算。" : "倍投账户初始2047U，亏后按1、2、4…1024U加注，最多11档；赢一笔或第11次亏损后回到1U。"}单账户最多一笔待执行单或持仓，UP/DOWN共享资金${sizing === "martin11" ? "与档位" : ""}。持有到官方结算；收到结果且上一场结束至少60秒后才允许下一笔，余额不足暂停。</p>
      <p class="candidate-note">模拟每份费用＝0.07×价格×(1−价格)，沿用历史研究口径；它不是当前平台费率。Binance开盘价是研究锚点，胜负以Polymarket官方结果为准。仅纸上模拟。</p>
      <p class="candidate-note">点击标题可固定说明；再次点击、点击外部或按Esc关闭。</p>`;
    if (help.innerHTML !== html) help.innerHTML = html;
    help.hidden = false;
    trigger.setAttribute("aria-expanded", "true");
    trigger.setAttribute("aria-describedby", help.id);
    positionHelp();
  }
  function scheduleHide() {
    clearTimeout(hideTimer);
    if (!helpPinned && document.activeElement !== helpTrigger) hideTimer = setTimeout(hideHelp, 180);
  }
  const cards = get("candidate-cards");
  cards.addEventListener("mouseover", (event) => {
    const trigger = event.target.closest(".candidate-title-trigger");
    if (trigger && !trigger.contains(event.relatedTarget) && !helpPinned) showHelp(trigger);
  });
  cards.addEventListener("mouseout", (event) => {
    const trigger = event.target.closest(".candidate-title-trigger");
    if (trigger && !trigger.contains(event.relatedTarget) && document.activeElement !== trigger) scheduleHide();
  });
  cards.addEventListener("focusin", (event) => {
    if (event.target.matches(".candidate-title-trigger")) showHelp(event.target);
  });
  cards.addEventListener("focusout", (event) => {
    if (event.target.matches(".candidate-title-trigger")) scheduleHide();
  });
  cards.addEventListener("click", (event) => {
    const trigger = event.target.closest(".candidate-title-trigger");
    if (!trigger) return;
    if (helpTrigger === trigger && helpPinned) hideHelp(); else showHelp(trigger, true);
  });
  help.addEventListener("mouseenter", () => clearTimeout(hideTimer));
  help.addEventListener("mouseleave", scheduleHide);
  document.addEventListener("click", (event) => {
    if (!event.target.closest(".candidate-title-trigger") && !help.contains(event.target)) hideHelp();
  });
  document.addEventListener("keydown", (event) => { if (event.key === "Escape") hideHelp(); });
  window.addEventListener("resize", positionHelp);
  window.addEventListener("scroll", positionHelp, { passive: true });
  const types = { ACCOUNT_STARTED: "账户开始记录", ATTEMPT: "提交模拟单", FILLED: "模拟成交", NO_FILL: "未成交", SETTLED: "官方结算" };

  function render(state) {
    latest = state;
    const c = state.candidates || {};
    const feed = c.feed || {};
    const generated = Number(state.generatedAtUs || 0);
    const spot = feed.spot;
    const spotAge = spot ? Math.max(0, (generated - Number(spot.sourceUs)) / 1000) : null;
    const receiveAge = spot ? Math.max(0, (generated - Number(spot.receivedUs)) / 1000) : null;
    const market = state.market;
    get("candidate-market").textContent = market ? `${market.slug} · 剩余${num(market.remainingSeconds, 1)}秒 · Binance Spot ${num(spot?.price)} · 校时后来源延迟${num(spotAge, 0)}ms · 接收后${num(receiveAge, 0)}ms` : "等待Polymarket市场";
    const statuses = { starting: "正在连接", warming: "正在补齐已收盘K线", live: "已连接", reconnecting: "正在重连" };
    const ruleCount = new Set((c.accounts || []).map((a) => a.rule)).size;
    get("candidate-heading").textContent = `${ruleCount || "六"}个候选，同时观察 UP / DOWN`;
    get("candidate-ledgers").textContent = `查看账本（${(c.accounts || []).length || 12}本账户同时运行）`;
    get("candidate-health").textContent = !c.enabled ? "候选模块尚未启用" : `Binance：${statuses[feed.status] || "等待启动"} · ${feed.error || `${ruleCount}个策略独立运行，页面切换不影响采集`}`;
    const clock = feed.timeSync;
    if (c.enabled && clock) {
      const clockText = clock.status === "synced"
        ? `服务器校时正常 · 本机${clock.localAheadMs >= 0 ? "快" : "慢"}${num(Math.abs(clock.localAheadMs), 0)}ms · 校时往返${num(clock.roundTripMs, 0)}ms`
        : `服务器校时${clock.status === "stale" ? "已过期" : "未完成"}，暂停新开仓${clock.error ? ` · ${clock.error}` : ""}`;
      get("candidate-health").textContent += ` · ${clockText}`;
    }
    const error = c.runtimeError || c.failure;
    get("candidate-error").hidden = !error;
    get("candidate-error").textContent = error || "";
    const v = c.coverage || {};
    get("candidate-coverage").textContent = `开始记录：${stamp(c.firstRecordedUs)} · 入场窗口观察${num(v.eligibleSeconds, 0)}秒 · 双边新鲜盘口${num(v.freshBothBookSeconds, 0)}秒 · 新鲜Spot${num(v.freshSpotSeconds, 0)}秒 · 指标就绪${num(v.indicatorReadySeconds, 0)}秒 · 锚点就绪${num(v.anchorReadySeconds, 0)}秒 · 调度漏秒${num(v.missedDecisionSeconds, 0)}`;
    get("candidate-capital").textContent = sizing === "fixed" ? "各100U虚拟本金 · 每笔含费预算1U" : "各2047U虚拟本金 · 1U起，最多11档 · 各账户独立倍投";
    const accounts = (c.accounts || []).filter((a) => a.sizing === sizing);
    if (accounts.length) {
      cards.querySelectorAll(":scope > :not(.candidate-card)").forEach((node) => node.remove());
      cards.querySelectorAll(".candidate-card").forEach((node) => {
        if (!accounts.some((a) => a.rule === node.dataset.rule)) node.remove();
      });
    }
    accounts.forEach((a) => {
      const position = a.position;
      const pending = a.pending;
      const up = a.byOutcome.UP, down = a.byOutcome.DOWN;
      const n = a.wins + a.losses;
      let card = get(`candidate-card-${a.rule}`);
      if (!card) {
        card = document.createElement("article");
        card.id = `candidate-card-${a.rule}`;
        card.dataset.rule = a.rule;
        card.className = "panel candidate-card";
        // Keep the title node stable across live refreshes so hover and
        // keyboard focus survive the once-per-second metrics update.
        card.innerHTML = `<div class="panel-heading"><h2><button class="candidate-title-trigger" type="button" data-rule="${esc(a.rule)}" data-name="${esc(a.name)}" aria-expanded="false" aria-controls="candidate-strategy-help" aria-label="${esc(a.name)}：查看详细开仓逻辑">${esc(a.name)}<span>开仓逻辑</span></button></h2><span class="quiet-badge">${esc(a.rule)}</span></div>
          <p class="candidate-note">${esc(descriptions[a.rule])}</p><div class="candidate-live"></div>`;
        cards.appendChild(card);
      }
      card.querySelector(".candidate-live").innerHTML = `<p class="candidate-note">本账户开始记录：${stamp(a.startedUs || c.firstRecordedUs)}</p><dl class="candidate-metrics">
          <div><dt>已结算 / 成交</dt><dd>${n} / ${a.fills}</dd></div>
          <div><dt>胜率</dt><dd>${pct(a.winRate)}</dd></div>
          <div><dt>胜 / 负</dt><dd>${a.wins} / ${a.losses}</dd></div>
          <div><dt>累计净收益</dt><dd>${money(a.pnl)}</dd></div>
          <div><dt>收益 / 累计投入</dt><dd>${pct(a.roi)}</dd></div>
          <div><dt>最大连续亏损</dt><dd>${a.maxLossStreak}</dd></div>
          <div><dt>已结算最大回撤</dt><dd>${num(a.maxDrawdown, 3)}U</dd></div>
          <div><dt>可用虚拟余额</dt><dd>${num(a.cash, 3)}U</dd></div>
          <div><dt>下一档预算</dt><dd>${num(a.nextBudget)}U${sizing === "martin11" ? ` · 第${a.step + 1}档` : ""}</dd></div>
          <div><dt>未成交 / 最大预算</dt><dd>${a.noFills} / ${num(a.maxBudget)}U</dd></div>
        </dl>
        <p class="candidate-directions">UP ${up.wins}胜${up.losses}负 ${money(up.pnl)}<br>DOWN ${down.wins}胜${down.losses}负 ${money(down.pnl)}</p>
        <p class="candidate-position">${position ? `待结算：${esc(position.side)} ${num(position.quantity)}份 · 均价${num(position.vwap, 4)} · 延迟${num(position.actualDelayMs, 0)}ms` : pending ? `待执行：${esc(pending.side)} · ${num(pending.budget)}U` : "无持仓或待执行订单"}</p>
        <p class="candidate-reason">${esc(error ? "账户已暂停，请查看上方错误" : a.reason)}</p>
        ${sizing === "martin11" ? `<p class="candidate-note">11连亏周期 ${a.capLossCycles}次 · 赢单后周期仍亏 ${a.negativeWinCycles}次</p>` : ""}
      `;
    });
    if (!accounts.length) { hideHelp(); cards.innerHTML = '<article class="panel">尚无候选账户。启用模块后会显示六个策略。</article>'; }
    if (helpTrigger && !help.hidden) {
      if (helpSizing !== sizing) showHelp(helpTrigger); else positionHelp();
    }
    const weeks = [...new Set(accounts.flatMap((a) => Object.keys(a.weekly)))].sort().reverse();
    get("candidate-week-head").innerHTML = `<th>周一日期</th>${accounts.map((a) => `<th>${esc(a.name)}</th>`).join("")}`;
    get("candidate-weeks").innerHTML = weeks.length ? weeks.map((w) => `<tr><th>${esc(w)}</th>${accounts.map((a) => `<td>${a.weekly[w] || 0}</td>`).join("")}</tr>`).join("") : `<tr><td colspan="${accounts.length + 1}">尚无模拟成交，不代表没有行情</td></tr>`;
    const names = Object.fromEntries((c.accounts || []).map((a) => [a.id, a.name]));
    const events = [...(c.recentEvents || [])].filter((e) => e.accountId?.endsWith(`:${sizing}`)).reverse();
    get("candidate-events").innerHTML = events.length ? events.map((e) => `<tr><td>${esc(stamp(e.atUs))}</td><td>${esc(names[e.accountId])}</td><td>${esc(types[e.type] || e.type)}${e.type === "SETTLED" ? (e.won ? " · 胜" : " · 负") : ""}</td><td>${esc(e.marketId)}<br>${esc(e.side)}</td><td>${e.budget ? `${num(e.budget)}U` : "—"} / ${num(e.vwap, 4)}</td><td>${e.pnl != null ? money(e.pnl) : esc(e.reason || "—")}</td></tr>`).join("") : '<tr><td colspan="6">尚无候选订单</td></tr>';
    get("candidate-version").textContent = c.model ? `规则版本：${c.model.version} · 指纹：${c.fingerprint}` : "";
  }
  window.renderCandidates = render;
  window.candidateConnectionLost = () => {
    get("candidate-health").textContent = "页面连接中断，以上是上次收到的状态，请勿视为当前行情。";
  };
})();

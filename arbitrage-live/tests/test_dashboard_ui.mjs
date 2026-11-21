import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

test('execution summaries distinguish pre-send failure from unknown submission', () => {
  const appPath = new URL('../src/live_trader/static/app.js', import.meta.url);
  const helpers = readFileSync(appPath, 'utf8').split('function render(state)')[0];
  const context = vm.createContext({ document: {} });
  vm.runInContext(helpers, context);
  context.event = { eventType: 'ORDER_REJECTED', payload: { requestSent: false, phase: 'prepare', reason: '订单准备失败，未发送：参数异常' } };
  let result = vm.runInContext('eventSummary(event)', context);
  assert.equal(result[0], '订单未发送');
  assert.match(result[1], /无需对账/);
  context.event = { eventType: 'ORDER_UNKNOWN', payload: { submissionError: { message: 'TLS connection closed' } } };
  result = vm.runInContext('eventSummary(event)', context);
  assert.equal(result[0], '订单状态未知');
  assert.match(result[1], /必须先对账/);
  assert.match(result[1], /TLS connection closed/);
});

test('stopping the runner does not invoke startup preflight', async () => {
  const listeners = new Map();
  const nodes = new Map();
  const requests = [];

  function nodeFor(id) {
    if (!nodes.has(id)) {
      nodes.set(id, {
        id,
        type: 'text',
        value: id === 'liveMode' ? 'canary' : '',
        checked: false,
        disabled: false,
        textContent: '',
        innerHTML: '',
        style: {},
        className: '',
        classList: { add() {}, remove() {} },
        addEventListener(type, handler) { listeners.set(`${id}:${type}`, handler); },
      });
    }
    return nodes.get(id);
  }

  globalThis.document = { getElementById: nodeFor };
  globalThis.window = { confirm: () => true };
  globalThis.setInterval = () => 0;
  globalThis.fetch = async (path, options = {}) => {
    requests.push({ path, method: options.method || 'GET' });
    const bodies = {
      '/api/state': { mode: 'observe', service: {}, safety: { ordersDisabled: true }, market: {}, overview: {}, connections: {}, counts: {}, performance: {}, usage: {} },
      '/api/settings': { liveMode: 'canary', accountType: '', privateKeyConfigured: false, clobIdentityConfigured: false, clobIdentitySummary: {} },
      '/api/runner': { status: 'running', pid: 123, mode: 'observe' },
      '/api/control': { mode: 'observe', desiredMode: 'canary', dataRecordingEnabled: true, effectiveOrderSubmissionEnabled: false, runner: { status: 'running', pid: 123, mode: 'observe' } },
      '/api/runner/stop': { status: 'stopped', pid: null, mode: null },
      '/api/preflight': { safe: false, checks: { marketFreshness: { passed: false, label: 'Polymarket 行情新鲜度', detail: '行情已陈旧' } } },
    };
    return { ok: true, async json() { return bodies[path] || {}; } };
  };

  const appPath = new URL('../src/live_trader/static/app.js', import.meta.url);
  vm.runInThisContext(readFileSync(appPath, 'utf8'), { filename: appPath.pathname });
  await new Promise(resolve => setImmediate(resolve));
  requests.length = 0;

  await listeners.get('stopRunner:click')();
  await new Promise(resolve => setImmediate(resolve));

  assert.ok(requests.some(request => request.path === '/api/runner/stop'));
  assert.ok(!requests.some(request => request.path === '/api/preflight'));
});

test('the mode badge shows the actual stopped runner instead of the previous session mode', async () => {
  const nodes = new Map();
  function nodeFor(id) {
    if (!nodes.has(id)) {
      nodes.set(id, {
        id,
        type: 'text',
        value: id === 'liveMode' ? 'canary' : '',
        checked: false,
        disabled: false,
        textContent: '',
        innerHTML: '',
        style: {},
        className: '',
        classList: { add() {}, remove() {} },
        addEventListener() {},
      });
    }
    return nodes.get(id);
  }
  const state = {
    mode: 'observe',
    runner: { status: 'stopped', pid: null, mode: null },
    service: {}, safety: { ordersDisabled: true },
    market: { connected: true, realtime: false, historical: true, updatedAtUs: 1700000000000000, ageSeconds: 120,
      marketId: 'old-market', quotes: { UP: { bestAsk: '0.42' }, DOWN: { bestAsk: '0.58' } },
      lastTradePrice: '0.50', remainingSeconds: '120', binanceSpotReturn5m: '0.01' },
    strategyDecision: { eventType: 'SIGNAL_REJECTED', outcome: 'UP', reason: 'remaining_above_window', receivedTimeUs: 1700000000000000 },
    overview: {}, connections: {}, counts: {}, performance: {}, usage: {},
  };
  const context = vm.createContext({
    console,
    document: { getElementById: nodeFor },
    window: { confirm: () => true },
    setInterval: () => 0,
    fetch: async path => ({
      ok: true,
      async json() {
        if (path === '/api/state') return state;
        if (path === '/api/settings') return { liveMode: 'canary', accountType: '', privateKeyConfigured: false, clobIdentityConfigured: false, clobIdentitySummary: {} };
        if (path === '/api/runner') return state.runner;
        if (path === '/api/control') return { mode: 'canary', desiredMode: 'canary', dataRecordingEnabled: true, effectiveOrderSubmissionEnabled: false, runner: state.runner };
        return {};
      },
    }),
  });
  const appPath = new URL('../src/live_trader/static/app.js', import.meta.url);
  vm.runInContext(readFileSync(appPath, 'utf8'), context, { filename: appPath.pathname });
  await new Promise(resolve => setImmediate(resolve));

  assert.equal(nodeFor('modeBadge').textContent, 'STOPPED · 已停止');
  assert.equal(nodeFor('marketStatus').textContent, '历史快照 · 非实时');
  assert.match(nodeFor('marketPageRefresh').textContent, /不会更新/);
  assert.equal(nodeFor('marketUpAsk').textContent, '—');
  assert.equal(nodeFor('marketDownAsk').textContent, '—');
  assert.equal(nodeFor('marketLastTrade').textContent, '—');
  assert.equal(nodeFor('marketRemaining').textContent, '—');
  assert.match(nodeFor('lastSignalReason').textContent, /尚未进入剩余 90–30 秒窗口/);
});

test('starting canary immediately explains that feeds are connecting with orders closed', async () => {
  const listeners = new Map();
  const nodes = new Map();
  function nodeFor(id) {
    if (!nodes.has(id)) {
      nodes.set(id, {
        id,
        type: 'text',
        value: id === 'liveMode' ? 'canary' : '',
        checked: false,
        disabled: false,
        textContent: '',
        innerHTML: '',
        style: {},
        className: '',
        classList: { add() {}, remove() {} },
        addEventListener(type, handler) { listeners.set(`${id}:${type}`, handler); },
      });
    }
    return nodes.get(id);
  }
  const stopped = { status: 'stopped', pid: null, mode: null };
  const neverResolvingStart = new Promise(() => {});
  const context = vm.createContext({
    console,
    document: { getElementById: nodeFor },
    window: { confirm: () => true },
    setInterval: () => 0,
    fetch: async (path, options = {}) => {
      if (path === '/api/runner/start' && options.method === 'POST') return neverResolvingStart;
      const body = path === '/api/state'
        ? { mode: 'observe', runner: stopped, service: {}, safety: { ordersDisabled: true }, market: {}, overview: {}, connections: {}, counts: {}, performance: {}, usage: {} }
        : path === '/api/settings'
          ? { liveMode: 'canary', accountType: '', privateKeyConfigured: false, clobIdentityConfigured: false, clobIdentitySummary: {} }
          : path === '/api/runner'
            ? stopped
            : { mode: 'canary', desiredMode: 'canary', dataRecordingEnabled: true, effectiveOrderSubmissionEnabled: false, runner: stopped };
      return { ok: true, async json() { return body; } };
    },
  });
  const appPath = new URL('../src/live_trader/static/app.js', import.meta.url);
  vm.runInContext(readFileSync(appPath, 'utf8'), context, { filename: appPath.pathname });
  await new Promise(resolve => setImmediate(resolve));

  listeners.get('startRunner:click')();
  await new Promise(resolve => setImmediate(resolve));

  assert.match(nodeFor('settingsStatus').textContent, /正在连接.*行情/);
  assert.match(nodeFor('settingsStatus').textContent, /30–120 秒/);
  assert.match(nodeFor('settingsStatus').textContent, /订单保持关闭/);
  assert.equal(nodeFor('startRunner').disabled, true);
  assert.equal(nodeFor('stopRunner').disabled, true);
  assert.equal(nodeFor('startRunner').textContent, '正在启动…');
});

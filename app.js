/*
 * 스톡맵 (StockMap) — 개인용 실시간 포트폴리오 히트맵
 *
 * 보안 원칙 (자세한 내용은 SECURITY.md)
 *  1. 외부에서 들어온 문자열은 절대 innerHTML로 넣지 않는다 → textContent만 사용
 *  2. 사용자 입력·저장 데이터·API 응답은 모두 형식 검사 후 사용한다
 *  3. API 키는 이 브라우저 localStorage에만 저장하고, finnhub.io 외에는 보내지 않는다
 *  4. eval / new Function / 인라인 스크립트를 쓰지 않는다 (CSP로도 차단)
 */
(function () {
  'use strict';

  // 다른 사이트가 이 앱을 iframe으로 감싸는 것(클릭재킹)을 막는다.
  if (window.top !== window.self) {
    document.documentElement.textContent = '';
    return;
  }

  // ---------------------------------------------------------------------------
  // 상수 · 검증 규칙
  // ---------------------------------------------------------------------------
  const STORE_KEY = 'stockmap.v1';
  const API_KEY_KEY = 'stockmap.apikey';
  const MAX_HOLDINGS = 50;              // Finnhub 무료 웹소켓 구독 한도
  const MAX_IMPORT_BYTES = 100 * 1024;  // 가져오기 파일 최대 100KB
  const MAX_WS_MESSAGE = 512 * 1024;    // 비정상적으로 큰 메시지는 무시
  const SYMBOL_RE = /^[A-Z0-9][A-Z0-9.\-]{0,14}$/;
  const API_KEY_RE = /^[A-Za-z0-9]{10,64}$/;
  const CONTROL_CHARS_RE = /[\u0000-\u001F\u007F-\u009F​-‏‪-‮⁦-⁩]/g;
  const QUOTE_URL = 'https://finnhub.io/api/v1/quote';
  const WS_URL = 'wss://ws.finnhub.io';
  const RENDER_INTERVAL = 1000;   // 화면 갱신은 최대 1초에 한 번
  const RELAYOUT_INTERVAL = 15000; // 박스 크기 재배치는 15초에 한 번
  const QUOTE_REFRESH = 10 * 60 * 1000;
  // 기간별 등락 (과거 가격은 Twelve Data 무료 요금제: 분당 8건, 하루 800건)
  const TD_KEY_KEY = 'stockmap.tdkey';
  const HIST_KEY = 'stockmap.hist.v1';
  const TD_URL = 'https://api.twelvedata.com/time_series';
  const TD_FX_URL = 'https://api.twelvedata.com/exchange_rate'; // 환율 (1건 = 무료 한도 1건, 같은 대기열 사용)
  const FX_PAIR = 'USD/KRW';
  const TD_SPACING = 8000;                 // 8초에 1건 → 분당 7.5건
  const DAILY_MAX_AGE = 6 * 60 * 60 * 1000; // 일봉은 6시간마다 새로
  const PERIODS = { '1h': '1시간', day: '오늘', '1w': '1주', '1m': '1달', '3m': '3달', '6m': '6달', '1y': '1년', '3y': '3년' };
  const PERIOD_DAYS = { '1w': 7, '1m': 30, '3m': 91, '6m': 182, '1y': 365, '3y': 1095 };
  const DAILY_BARS = 800; // 약 3년치 거래일 (요청 1건 = 무료 한도 1건, 길이와 무관)
  const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
  const DATETIME_RE = /^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):\d{2}$/;

  const $ = (id) => document.getElementById(id);
  const ui = {
    map: $('map'), total: $('total'), today: $('today'), market: $('market'), conn: $('conn'),
    updated: $('updated'), fxInfo: $('fxInfo'), banner: $('banner'), tooltip: $('tooltip'), maskBtn: $('maskBtn'),
    manageDlg: $('manageDlg'), settingsDlg: $('settingsDlg'), addForm: $('addForm'),
    formMsg: $('formMsg'), holdingsBody: $('holdingsBody'), importFile: $('importFile'),
    keyForm: $('keyForm'), fKey: $('fKey'), keyStatus: $('keyStatus'), settingsMsg: $('settingsMsg'),
  };

  // ---------------------------------------------------------------------------
  // 안전한 저장소 접근
  // ---------------------------------------------------------------------------
  const memoryStore = new Map();
  let storageOk = true;
  function storeGet(key) {
    try { return window.localStorage.getItem(key); } catch (e) { storageOk = false; return memoryStore.get(key) ?? null; }
  }
  function storeSet(key, value) {
    try { window.localStorage.setItem(key, value); } catch (e) { storageOk = false; memoryStore.set(key, value); }
  }
  function storeRemove(key) {
    try { window.localStorage.removeItem(key); } catch (e) { /* ignore */ }
    memoryStore.delete(key);
  }

  // ---------------------------------------------------------------------------
  // 입력 검증
  // ---------------------------------------------------------------------------
  function cleanText(value, maxLen) {
    return String(value ?? '').replace(CONTROL_CHARS_RE, '').trim().slice(0, maxLen);
  }
  function toNumber(value, { min, max, allowEmpty = false }) {
    if (allowEmpty && (value === '' || value === null || value === undefined)) return null;
    const n = typeof value === 'number' ? value : Number(String(value).trim());
    if (!Number.isFinite(n) || n < min || n > max) throw new Error('숫자 범위가 올바르지 않아요.');
    return n;
  }
  function validateHolding(raw) {
    if (!raw || typeof raw !== 'object') throw new Error('잘못된 종목 데이터예요.');
    const symbol = cleanText(raw.symbol, 15).toUpperCase();
    if (!SYMBOL_RE.test(symbol)) throw new Error('티커는 영문 대문자·숫자·점(.)·하이픈(-)만 쓸 수 있어요.');
    if (raw.currency !== 'USD' && raw.currency !== 'KRW') throw new Error('통화는 USD 또는 KRW만 가능해요.');
    const shares = toNumber(raw.shares, { min: 0.000001, max: 1e9 });
    const avgPrice = toNumber(raw.avgPrice, { min: 0, max: 1e10 });
    const manualPrice = toNumber(raw.manualPrice, { min: 0, max: 1e10, allowEmpty: true });
    const group = cleanText(raw.group, 20) || '기타';
    const name = cleanText(raw.name, 20) || null; // 선택: 종목명 (국내 종목은 코드 대신 이 이름을 표시)
    return { symbol, currency: raw.currency, shares, avgPrice, group, manualPrice, name };
  }
  function validateSettings(raw) {
    const s = { fxRate: 1400, fxAuto: true, fxLast: null, range: 3, masked: false, demo: false, period: 'day', rememberKeys: true, rememberHoldings: false };
    if (!raw || typeof raw !== 'object') return s;
    try { s.fxRate = toNumber(raw.fxRate, { min: 1, max: 100000 }); } catch (e) { /* 기본값 */ }
    s.fxAuto = raw.fxAuto !== false; // 기본값: 자동
    // 마지막으로 자동으로 받은 환율 (자동 받기에 실패했을 때 대신 써요)
    if (raw.fxLast && typeof raw.fxLast === 'object') {
      try {
        s.fxLast = {
          rate: toNumber(raw.fxLast.rate, { min: 1, max: 100000 }),
          at: toNumber(raw.fxLast.at, { min: 0, max: Date.now() + 86400000 }),
        };
      } catch (e) { s.fxLast = null; }
    }
    try { s.range = toNumber(raw.range, { min: 0.5, max: 20 }); } catch (e) { /* 기본값 */ }
    s.masked = raw.masked === true;
    s.demo = raw.demo === true;
    s.rememberKeys = raw.rememberKeys !== false;
    s.rememberHoldings = raw.rememberHoldings === true; // 기본값: 기억하지 않음
    if (Object.prototype.hasOwnProperty.call(PERIODS, raw.period)) s.period = raw.period;
    return s;
  }

  // ---------------------------------------------------------------------------
  // 상태
  // ---------------------------------------------------------------------------
  const state = {
    holdings: [],
    settings: validateSettings(null),
    quotes: new Map(),       // symbol -> { price, prevClose, supported, lastTrade }
    mode: 'demo',            // 'demo' | 'live'
    ws: null, wsWanted: false, wsRetry: 0, wsTimer: null,
    timers: [], runId: 0,
    lastRender: 0, renderPending: false, lastLayout: 0, layoutDirty: true,
    leafEls: new Map(), lastPrice: new Map(),
    daily: new Map(),     // symbol -> { fetchedAt, bars: [[YYYY-MM-DD, close], ...] (최신순) }
    intraday: new Map(),  // symbol -> { fetchedAt, bars: [[분 단위 시각, close, 'MM/DD HH:MM'], ...] (최신순) }
    histUnsupported: new Set(),
    demoBases: {},
    fx: { status: 'idle', rate: null, at: null, reason: '' }, // 이번에 연 앱에서 자동으로 받은 환율
  };

  function loadState() {
    const raw = storeGet(STORE_KEY);
    if (!raw) return;
    try {
      const data = JSON.parse(raw);
      state.settings = validateSettings(data.settings);
      const list = Array.isArray(data.holdings) ? data.holdings.slice(0, MAX_HOLDINGS) : [];
      const seen = new Set();
      for (const item of list) {
        try {
          const h = validateHolding(item);
          if (!seen.has(h.symbol)) { seen.add(h.symbol); state.holdings.push(h); }
        } catch (e) { /* 잘못된 항목은 버린다 */ }
      }
      // 기억하기가 꺼져 있으면 이번 탭에서만 쓰고, 저장돼 있던 종목은 지운다.
      if (!state.settings.rememberHoldings && Object.prototype.hasOwnProperty.call(data, 'holdings')) saveState();
    } catch (e) {
      showBanner('저장된 데이터를 읽지 못해 초기화했어요.', true);
    }
  }
  function saveState() {
    // 보유 종목(수량·평단·그룹)은 '기억하기'를 켠 때만 저장한다. 끄면 메모리에만 둔다.
    const data = { version: 1, settings: state.settings };
    if (state.settings.rememberHoldings) data.holdings = state.holdings;
    storeSet(STORE_KEY, JSON.stringify(data));
  }
  // API 키: "기억하기"를 끄면 sessionStorage(탭을 닫으면 삭제)에 둔다.
  function sessGet(key) { try { return window.sessionStorage.getItem(key); } catch (e) { return memoryStore.get('s:' + key) ?? null; } }
  function sessSet(key, v) { try { window.sessionStorage.setItem(key, v); } catch (e) { memoryStore.set('s:' + key, v); } }
  function sessRemove(key) { try { window.sessionStorage.removeItem(key); } catch (e) { /* ignore */ } memoryStore.delete('s:' + key); }
  function readKey(name) {
    const k = storeGet(name) || sessGet(name);
    return k && API_KEY_RE.test(k) ? k : null;
  }
  function writeKey(name, value) {
    if (state.settings.rememberKeys) { storeSet(name, value); sessRemove(name); }
    else { sessSet(name, value); storeRemove(name); }
  }
  function removeKey(name) { storeRemove(name); sessRemove(name); }
  function getApiKey() { return readKey(API_KEY_KEY); }
  function getTdKey() { return readKey(TD_KEY_KEY); }
  // 일봉 캐시 (가격 숫자만 저장, 형식 검사 후 사용)
  function loadHistCache() {
    const raw = storeGet(HIST_KEY);
    // 보유 종목 기억하기가 꺼져 있으면 이번 탭에서만 쓰고, 저장돼 있던 캐시는 지운다 (종목 목록이 남지 않게).
    if (raw !== null && !state.settings.rememberHoldings) storeRemove(HIST_KEY);
    try {
      const data = JSON.parse(raw || '{}');
      if (!data || typeof data !== 'object') return;
      for (const [sym, v] of Object.entries(data).slice(0, MAX_HOLDINGS)) {
        if (!SYMBOL_RE.test(sym) || !v || !Array.isArray(v.bars) || !Number.isFinite(v.fetchedAt)) continue;
        const bars = v.bars.slice(0, DAILY_BARS).filter((b) => Array.isArray(b) && DATE_RE.test(b[0]) && Number.isFinite(b[1]) && b[1] > 0);
        if (bars.length) state.daily.set(sym, { fetchedAt: v.fetchedAt, bars, full: v.full === true });
      }
    } catch (e) { /* 캐시가 깨졌으면 무시 */ }
  }
  function saveHistCache() {
    if (!state.settings.rememberHoldings) { storeRemove(HIST_KEY); return; } // 꺼져 있으면 메모리에만
    const keep = new Set(state.holdings.map((h) => h.symbol));
    const out = {};
    for (const [sym, v] of state.daily) if (keep.has(sym)) out[sym] = v;
    storeSet(HIST_KEY, JSON.stringify(out));
  }

  // ---------------------------------------------------------------------------
  // 표시 도우미 (항상 textContent 사용)
  // ---------------------------------------------------------------------------
  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  const nfKRW = new Intl.NumberFormat('ko-KR', { maximumFractionDigits: 0 });
  const nfNum = new Intl.NumberFormat('ko-KR', { maximumFractionDigits: 4 });
  function fmtKRW(n) { return state.settings.masked ? '••••••' : nfKRW.format(Math.round(n)) + '원'; }
  function fmtSignedKRW(n) { return state.settings.masked ? '••••' : (n >= 0 ? '+' : '') + nfKRW.format(Math.round(n)) + '원'; }
  function fmtPct(p) { return Number.isFinite(p) ? (p >= 0 ? '+' : '') + p.toFixed(2) + '%' : '—'; }
  // 국내(KRW) 종목은 숫자 코드 대신 종목명, 이름이 없으면 코드. 미국 종목은 티커.
  function displayName(h) { return h.currency === 'KRW' ? (h.name || h.symbol) : h.symbol; }
  const nfFx = new Intl.NumberFormat('ko-KR', { maximumFractionDigits: 2 });
  const fxTimeFmt = new Intl.DateTimeFormat('ko-KR', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
  function fxLabel() {
    const f = currentFx();
    const rate = nfFx.format(f.rate) + '원';
    if (f.source === 'auto') return `환율: 자동 · ${rate} · ${fxTimeFmt.format(f.at)} 기준`;
    const auto = state.settings.fxAuto;
    const head = auto && state.fx.status === 'loading' ? '환율: 받는 중' : '환율: 수동';
    const why = auto && state.fx.status === 'fail' && state.fx.reason ? ` (자동 실패: ${state.fx.reason})` : '';
    if (f.source === 'last') return `${head} · ${rate} · 지난 자동값 ${fxTimeFmt.format(f.at)} 기준${why}`;
    return `${head} · ${rate} · 직접 입력${why}`;
  }
  function fmtPrice(p, cur) {
    if (!Number.isFinite(p)) return '—';
    return cur === 'USD' ? '$' + p.toFixed(2) : nfKRW.format(p) + '원';
  }
  function showBanner(text, isError) {
    ui.banner.textContent = text;
    ui.banner.classList.toggle('error', !!isError);
    ui.banner.hidden = !text;
  }
  function setConn(text, kind) {
    ui.conn.textContent = text;
    ui.conn.className = 'badge' + (kind ? ' ' + kind : '');
  }

  // ---------------------------------------------------------------------------
  // 미국 장 상태 (뉴욕 시간 기준, 공휴일은 반영하지 않음)
  // ---------------------------------------------------------------------------
  function marketSession(now = new Date()) {
    const parts = new Intl.DateTimeFormat('en-US', {
      timeZone: 'America/New_York', weekday: 'short', hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
    }).formatToParts(now);
    const get = (t) => parts.find((p) => p.type === t)?.value;
    const day = get('weekday');
    const mins = Number(get('hour')) * 60 + Number(get('minute'));
    if (day === 'Sat' || day === 'Sun') return { text: '미국 휴장(주말)', kind: '' };
    if (mins >= 240 && mins < 570) return { text: '프리마켓', kind: 'warn' };
    if (mins >= 570 && mins < 960) return { text: '정규장', kind: 'live' };
    if (mins >= 960 && mins < 1200) return { text: '애프터마켓', kind: 'warn' };
    return { text: '미국 장 마감', kind: '' };
  }
  function updateMarketBadge() {
    const s = marketSession();
    ui.market.textContent = s.text;
    ui.market.className = 'badge' + (s.kind ? ' ' + s.kind : '');
  }

  // ---------------------------------------------------------------------------
  // 계산
  // ---------------------------------------------------------------------------
  function activeHoldings() { return state.mode === 'demo' ? DEMO.holdings : state.holdings; }
  // 지금 쓰는 환율: 자동(이번에 받은 값) → 지난 자동값 → 직접 입력값 순서
  function currentFx() {
    const s = state.settings;
    if (s.fxAuto && state.fx.status === 'ok') return { rate: state.fx.rate, at: state.fx.at, source: 'auto' };
    if (s.fxAuto && s.fxLast) return { rate: s.fxLast.rate, at: s.fxLast.at, source: 'last' };
    return { rate: s.fxRate, at: null, source: 'manual' };
  }
  function fx(h) { return h.currency === 'USD' ? currentFx().rate : 1; }

  // 뉴욕 기준 날짜 문자열 (YYYY-MM-DD)
  const nyDateFmt = new Intl.DateTimeFormat('en-CA', { timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit' });
  function nyDate(ms) { return nyDateFmt.format(new Date(ms)); }

  // 선택한 기간의 "기준가"를 찾는다. { price, label } 또는 null
  function baseFor(symbol, q) {
    const period = state.settings.period;
    if (state.mode === 'demo') {
      const b = state.demoBases[period] && state.demoBases[period][symbol];
      return b ? { price: b, label: PERIODS[period] + ' 전 (예시)' } : null;
    }
    if (period === 'day') return q && q.prevClose > 0 ? { price: q.prevClose, label: '전일 종가' } : null;
    if (PERIOD_DAYS[period]) {
      const d = state.daily.get(symbol);
      if (!d) return null;
      const target = nyDate(Date.now() - PERIOD_DAYS[period] * 86400000);
      const bar = d.bars.find((b) => b[0] <= target);
      if (!bar) return d.full ? { missing: true } : null; // 상장한 지 얼마 안 된 종목 등
      const label = `${bar[0].replace(/-/g, '.')} 종가` + (PERIOD_DAYS[period] >= 91 ? ' (배당 제외)' : '');
      return { price: bar[1], label };
    }
    if (period === '1h') {
      const d = state.intraday.get(symbol);
      if (!d || !d.bars.length) return null;
      const target = d.bars[0][0] - 60;
      const bar = d.bars.find((b) => b[0] <= target);
      return bar ? { price: bar[1], label: `${bar[2]} 가격` } : null;
    }
    return null;
  }

  function computeRow(h) {
    const q = state.quotes.get(h.symbol);
    let price = null; let live = false;
    if (q && q.supported && q.price > 0) { price = q.price; live = true; }
    else if (h.manualPrice != null && h.manualPrice > 0) { price = h.manualPrice; }
    const shownPrice = price ?? (h.avgPrice > 0 ? h.avgPrice : null);
    const value = (shownPrice ?? 0) * h.shares * fx(h);
    const base = live ? baseFor(h.symbol, q) : null;
    const ok = price != null && base && !base.missing && base.price > 0;
    const dp = ok ? ((price - base.price) / base.price) * 100 : NaN;
    const dayChange = ok ? (price - base.price) * h.shares * fx(h) : 0;
    const dayBase = ok ? base.price * h.shares * fx(h) : 0;
    const pnl = shownPrice != null && h.avgPrice > 0 ? (shownPrice - h.avgPrice) * h.shares * fx(h) : NaN;
    return { h, price: shownPrice, base, live, value, dp, dayChange, dayBase, pnl };
  }
  function colorFor(dp) {
    if (!Number.isFinite(dp)) return null;
    const t = Math.min(Math.abs(dp) / state.settings.range, 1);
    const from = [52, 58, 66];
    const to = dp >= 0 ? [40, 178, 82] : [214, 62, 68];
    const c = from.map((f, i) => Math.round(f + (to[i] - f) * Math.pow(t, 0.8)));
    return `rgb(${c[0]}, ${c[1]}, ${c[2]})`;
  }

  // ---------------------------------------------------------------------------
  // 렌더링
  // ---------------------------------------------------------------------------
  function relayout() {
    state.lastLayout = Date.now();
    state.layoutDirty = false;
    state.leafEls.clear();
    const rows = activeHoldings().map(computeRow);
    const rect = ui.map.getBoundingClientRect();
    if (!rows.length) {
      ui.map.replaceChildren(el('div', 'empty', '종목 관리에서 보유 종목을 추가해 주세요.'));
      return;
    }
    if (!window.d3 || typeof window.d3.treemap !== 'function') {
      ui.map.replaceChildren(el('div', 'empty', '라이브러리를 불러오지 못했어요. 새로고침해 주세요.'));
      return;
    }
    const allZero = rows.every((r) => !(r.value > 0));
    const groups = new Map();
    for (const r of rows) {
      if (!groups.has(r.h.group)) groups.set(r.h.group, []);
      groups.get(r.h.group).push(r);
    }
    const data = { children: [...groups.entries()].map(([name, list]) => ({ name, children: list })) };
    const root = window.d3.hierarchy(data)
      .sum((d) => (d.h ? (allZero ? 1 : Math.max(d.value, 0)) : 0))
      .sort((a, b) => b.value - a.value);
    window.d3.treemap()
      .tile(window.d3.treemapSquarify)
      .size([Math.max(rect.width, 50), Math.max(rect.height, 50)])
      .paddingOuter(3)
      .paddingTop((n) => (n.depth === 1 ? 20 : 3))
      .paddingInner(2)
      .round(true)(root);

    const frag = document.createDocumentFragment();
    for (const g of root.children || []) {
      const gEl = el('div', 'group');
      placeBox(gEl, g);
      gEl.appendChild(el('div', 'group-label', g.data.name));
      frag.appendChild(gEl);
    }
    for (const leaf of root.leaves()) {
      if (!leaf.data.h || leaf.value <= 0) continue;
      const box = el('div', 'leaf');
      placeBox(box, leaf);
      const w = leaf.x1 - leaf.x0; const h = leaf.y1 - leaf.y0;
      box.dataset.symbol = leaf.data.h.symbol;
      box.dataset.size = w < 44 || h < 26 ? 'xs' : (w < 90 || h < 52 ? 's' : (w * h > 40000 ? 'l' : 'm'));
      // 좁은 박스에서 글자가 잘리지 않도록 너비에 맞춰 글자 크기를 줄인다 (한글 종목명은 글자 폭을 1em으로 계산)
      const label = displayName(leaf.data.h);
      const ems = [...label].reduce((n, c) => n + (/[\u1100-\u11FF\u3130-\u318F\uAC00-\uD7AF\u4E00-\u9FFF]/.test(c) ? 1 : 0.62), 0);
      const fontBase = Math.max(9, Math.min(28, Math.sqrt(w * h) / 6, w / 4, (w * 0.85) / Math.max(ems, 1)));
      const sym = el('div', 'sym'); sym.style.fontSize = fontBase + 'px';
      const pctSize = Math.max(8, Math.min(fontBase * 0.7, w / 5.5));
      const pct = el('div', 'pct'); pct.style.fontSize = pctSize + 'px';
      // 이름과 등락이 두 줄로 안 들어가는 낮은 박스는 등락을 숨겨요 (툴팁에서 볼 수 있어요)
      if (h < fontBase * 1.15 + pctSize * 1.2 + 6) box.dataset.tight = '1';
      const val = el('div', 'val');
      box.append(sym, pct, val);
      attachTooltip(box);
      frag.appendChild(box);
      state.leafEls.set(leaf.data.h.symbol, { box, sym, pct, val });
    }
    ui.map.replaceChildren(frag);
    updateLeaves(rows);
  }
  function placeBox(node, d) {
    node.style.left = d.x0 + 'px';
    node.style.top = d.y0 + 'px';
    node.style.width = Math.max(0, d.x1 - d.x0) + 'px';
    node.style.height = Math.max(0, d.y1 - d.y0) + 'px';
  }
  function leafStatus(r) {
    if (!r.live) return r.h.manualPrice ? '수동 가격' : '시세 없음';
    if (r.base && r.base.missing) return '기간 부족';
    if (state.settings.period !== 'day' && state.histUnsupported.has(r.h.symbol)) return '데이터 없음';
    if (state.settings.period !== 'day' && !getTdKey()) return '키 필요';
    return '받는 중';
  }
  function updateLeaves(rows) {
    for (const r of rows) {
      const ref = state.leafEls.get(r.h.symbol);
      if (!ref) continue;
      const size = ref.box.dataset.size;
      const color = colorFor(r.dp);
      ref.box.classList.toggle('nodata', !color);
      ref.box.style.backgroundColor = color || '';
      ref.sym.textContent = size === 'xs' ? '' : displayName(r.h);
      ref.pct.textContent = size === 'xs' ? '' : (Number.isFinite(r.dp) ? fmtPct(r.dp) : leafStatus(r));
      ref.val.textContent = size === 'l' ? fmtKRW(r.value) : '';
      const prev = state.lastPrice.get(r.h.symbol);
      if (prev != null && r.price != null && prev !== r.price) {
        const cls = r.price > prev ? 'flash-up' : 'flash-down';
        ref.box.classList.remove('flash-up', 'flash-down');
        void ref.box.offsetWidth; // 애니메이션 재시작
        ref.box.classList.add(cls);
        setTimeout(() => ref.box.classList.remove(cls), 700);
      }
      if (r.price != null) state.lastPrice.set(r.h.symbol, r.price);
    }
  }
  function updateTopBar(rows) {
    let total = 0; let change = 0; let base = 0;
    for (const r of rows) { total += r.value; change += r.dayChange; base += r.dayBase; }
    ui.total.textContent = fmtKRW(total);
    ui.maskBtn.textContent = state.settings.masked ? '금액 보기' : '금액 숨기기';
    $('periodLabel').textContent = PERIODS[state.settings.period];
    document.querySelectorAll('.period-btn').forEach((b) => b.classList.toggle('active', b.dataset.period === state.settings.period));
    const pct = base > 0 ? (change / base) * 100 : NaN;
    ui.today.textContent = Number.isFinite(pct) ? `${fmtSignedKRW(change)} (${fmtPct(pct)})` : '—';
    ui.today.classList.toggle('up', pct > 0);
    ui.today.classList.toggle('down', pct < 0);
    ui.updated.textContent = '업데이트 ' + new Date().toLocaleTimeString('ko-KR');
    ui.fxInfo.textContent = fxLabel();
    ui.fxInfo.classList.toggle('fx-manual', currentFx().source !== 'auto');
  }
  function renderNow() {
    state.lastRender = Date.now();
    state.renderPending = false;
    const rows = activeHoldings().map(computeRow);
    updateTopBar(rows);
    if (state.layoutDirty && Date.now() - state.lastLayout > RELAYOUT_INTERVAL) relayout();
    else updateLeaves(rows);
  }
  function scheduleRender(forceLayout) {
    if (forceLayout) { state.lastLayout = 0; state.layoutDirty = true; }
    if (state.renderPending) return;
    state.renderPending = true;
    const wait = Math.max(0, RENDER_INTERVAL - (Date.now() - state.lastRender));
    setTimeout(renderNow, forceLayout ? 0 : wait);
  }

  // ---------------------------------------------------------------------------
  // 툴팁 (textContent로만 구성)
  // ---------------------------------------------------------------------------
  // 마우스: 올리면 뜨고 벗어나면 닫혀요. 터치(핸드폰): 박스를 탭하면 뜨고, 다른 곳을 탭하면 닫혀요.
  let tipBox = null; // 지금 툴팁을 띄운 박스
  function hideTip() { ui.tooltip.hidden = true; tipBox = null; }
  // 툴팁이 화면 밖으로 나가지 않게: 기본은 손가락/커서 오른쪽 아래, 넘치면 반대쪽, 그래도 넘치면 화면 안쪽으로 붙여요.
  function placeTip(cx, cy) {
    const pad = 14; const edge = 6;
    const vw = document.documentElement.clientWidth || window.innerWidth;
    const vh = document.documentElement.clientHeight || window.innerHeight;
    const tw = ui.tooltip.offsetWidth; const th = ui.tooltip.offsetHeight;
    let x = cx + pad; let y = cy + pad;
    if (x + tw > vw - edge) x = cx - tw - pad;
    if (y + th > vh - edge) y = cy - th - pad;
    x = Math.min(Math.max(edge, x), Math.max(edge, vw - tw - edge));
    y = Math.min(Math.max(edge, y), Math.max(edge, vh - th - edge));
    ui.tooltip.style.left = x + 'px';
    ui.tooltip.style.top = y + 'px';
  }
  function attachTooltip(box) {
    const show = (ev) => {
      const sym = box.dataset.symbol;
      const h = activeHoldings().find((x) => x.symbol === sym);
      if (!h) return;
      const r = computeRow(h);
      const masked = state.settings.masked;
      const rowsData = [
        ['현재가', masked ? '••••' : fmtPrice(r.price, h.currency) + (r.live ? '' : ' (직접입력/평단)')],
        [PERIODS[state.settings.period] + ' 등락', fmtPct(r.dp)],
        ['기준가', r.base && !r.base.missing ? `${fmtPrice(r.base.price, h.currency)} · ${r.base.label}` : '—'],
        ['보유 수량', masked ? '••••' : nfNum.format(h.shares)],
        ['평균 단가', masked ? '••••' : fmtPrice(h.avgPrice, h.currency)],
        ['평가액', fmtKRW(r.value)],
        ['평가손익', Number.isFinite(r.pnl) ? fmtSignedKRW(r.pnl) : '—'],
      ];
      if (!r.live && h.manualPrice) rowsData.push(['수동 가격', '실시간 시세 대신 직접 입력한 가격이라 등락(%)은 표시되지 않아요.']);
      const nodes = [el('div', 'tt-title', `${displayName(h)} · ${h.group}`)];
      for (const [k, v] of rowsData) {
        const row = el('div', 'tt-row');
        row.append(el('span', '', k), el('span', '', v));
        nodes.push(row);
      }
      ui.tooltip.replaceChildren(...nodes);
      ui.tooltip.hidden = false;
      tipBox = box;
      placeTip(ev.clientX, ev.clientY);
    };
    const isMouse = (ev) => ev.pointerType === 'mouse';
    box.addEventListener('pointerenter', (ev) => { if (isMouse(ev)) show(ev); });
    box.addEventListener('pointermove', (ev) => { if (isMouse(ev) && tipBox === box) placeTip(ev.clientX, ev.clientY); });
    box.addEventListener('pointerleave', (ev) => { if (isMouse(ev)) hideTip(); });
    box.addEventListener('click', show); // 탭(터치)·클릭·키보드
  }
  // 박스가 아닌 곳을 누르면 닫아요 (핸드폰에서 탭으로 연 툴팁 닫기).
  document.addEventListener('pointerdown', (ev) => {
    if (ui.tooltip.hidden) return;
    const t = ev.target;
    if (t && t.closest && t.closest('.leaf')) return; // 다른 박스를 탭하면 click에서 그 박스로 바뀌어요
    hideTip();
  }, true);
  document.addEventListener('keydown', (ev) => { if (ev.key === 'Escape') hideTip(); });
  window.addEventListener('resize', hideTip);

  // ---------------------------------------------------------------------------
  // 실시간 시세 (Finnhub)
  // ---------------------------------------------------------------------------
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  async function fetchQuote(symbol, key) {
    const url = new URL(QUOTE_URL);
    url.searchParams.set('symbol', symbol);
    url.searchParams.set('token', key);
    const res = await fetch(url.toString(), {
      method: 'GET', mode: 'cors', credentials: 'omit', referrerPolicy: 'no-referrer', cache: 'no-store', redirect: 'error',
    });
    if (res.status === 401) throw Object.assign(new Error('API 키가 올바르지 않아요.'), { fatal: true });
    if (res.status === 429) throw Object.assign(new Error('요청 한도 초과'), { retry: true });
    if (!res.ok) return { supported: false };
    const text = await res.text();
    if (text.length > 10000) return { supported: false };
    let j;
    try { j = JSON.parse(text); } catch (e) { return { supported: false }; }
    const c = Number(j && j.c); const pc = Number(j && j.pc);
    if (!Number.isFinite(c) || !Number.isFinite(pc) || (c <= 0 && pc <= 0)) return { supported: false };
    return { supported: true, price: c > 0 ? c : pc, prevClose: pc > 0 ? pc : null };
  }

  async function loadQuotes(runId, key) {
    const usd = state.holdings.filter((h) => h.currency === 'USD');
    for (const h of usd) {
      if (runId !== state.runId) return;
      for (let attempt = 0; attempt < 3; attempt++) {
        try {
          const q = await fetchQuote(h.symbol, key);
          const prev = state.quotes.get(h.symbol);
          state.quotes.set(h.symbol, { ...q, lastTrade: prev ? prev.lastTrade : null });
          break;
        } catch (e) {
          if (e.fatal) { showBanner(e.message + ' 설정에서 키를 확인해 주세요.', true); setConn('키 오류', 'bad'); return; }
          if (e.retry) { await sleep(5000); continue; }
          await sleep(1500);
        }
      }
      scheduleRender(true);
      await sleep(300); // 무료 한도(분당 60회) 안쪽으로 천천히
    }
  }

  function connectWS(runId, key) {
    if (runId !== state.runId) return;
    closeWS();
    const symbols = state.holdings
      .filter((h) => h.currency === 'USD' && state.quotes.get(h.symbol)?.supported)
      .map((h) => h.symbol);
    if (!symbols.length) { setConn('실시간 대상 없음', 'warn'); return; }
    const subscribed = new Set(symbols);
    state.wsWanted = true;
    setConn('연결 중…', 'warn');
    let ws;
    try { ws = new WebSocket(WS_URL + '?token=' + encodeURIComponent(key)); } catch (e) { scheduleReconnect(runId, key); return; }
    state.ws = ws;
    ws.addEventListener('open', () => {
      state.wsRetry = 0;
      setConn('실시간', 'live');
      for (const s of symbols) ws.send(JSON.stringify({ type: 'subscribe', symbol: s }));
    });
    ws.addEventListener('message', (ev) => {
      if (typeof ev.data !== 'string' || ev.data.length > MAX_WS_MESSAGE) return;
      let msg;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      if (!msg || typeof msg !== 'object') return;
      if (msg.type === 'trade' && Array.isArray(msg.data)) {
        let changed = false;
        for (const t of msg.data) {
          if (!t || typeof t.s !== 'string' || !subscribed.has(t.s)) continue;
          const p = Number(t.p);
          if (!Number.isFinite(p) || p <= 0 || p > 1e7) continue;
          const q = state.quotes.get(t.s);
          if (!q) continue;
          q.price = p; q.lastTrade = Date.now(); changed = true;
        }
        if (changed) { state.layoutDirty = true; scheduleRender(false); }
      } else if (msg.type === 'error') {
        setConn('연결 오류', 'bad');
      }
    });
    ws.addEventListener('close', () => {
      if (state.ws === ws) state.ws = null;
      if (state.wsWanted && runId === state.runId) scheduleReconnect(runId, key);
    });
    ws.addEventListener('error', () => { /* close 이벤트에서 재연결 처리 */ });
  }
  function scheduleReconnect(runId, key) {
    clearTimeout(state.wsTimer);
    const delay = Math.min(30000, 1000 * Math.pow(2, state.wsRetry++));
    setConn(`재연결 대기 (${Math.round(delay / 1000)}초)`, 'warn');
    state.wsTimer = setTimeout(() => connectWS(runId, key), delay);
  }
  function closeWS() {
    state.wsWanted = false;
    clearTimeout(state.wsTimer);
    if (state.ws) { try { state.ws.close(); } catch (e) { /* ignore */ } state.ws = null; }
  }

  // ---------------------------------------------------------------------------
  // 과거 가격 (Twelve Data) — 한도를 지키는 대기열
  // ---------------------------------------------------------------------------
  const td = { queue: [], busy: false, last: 0, intradayTimer: null };

  async function fetchSeries(symbol, interval, outputsize, key) {
    const url = new URL(TD_URL);
    url.searchParams.set('symbol', symbol);
    url.searchParams.set('interval', interval);
    url.searchParams.set('outputsize', String(outputsize));
    url.searchParams.set('timezone', 'America/New_York');
    url.searchParams.set('apikey', key);
    const res = await fetch(url.toString(), {
      method: 'GET', mode: 'cors', credentials: 'omit', referrerPolicy: 'no-referrer', cache: 'no-store', redirect: 'error',
    });
    const text = await res.text();
    if (text.length > 400000) throw new Error('응답이 너무 커요.');
    let j;
    try { j = JSON.parse(text); } catch (e) { throw new Error('응답 형식 오류'); }
    const code = Number(j && j.code) || res.status;
    if (code === 401 || code === 403) throw Object.assign(new Error('Twelve Data 키가 올바르지 않거나 권한이 없어요.'), { fatal: true });
    if (code === 429) throw Object.assign(new Error('한도 초과'), { retry: true });
    if (!j || j.status !== 'ok' || !Array.isArray(j.values)) throw Object.assign(new Error('지원하지 않는 종목'), { unsupported: true });
    return j.values.slice(0, DAILY_BARS);
  }

  // USD/KRW 환율: 보내는 것은 통화쌍과 키뿐이에요.
  async function fetchFxRate(key) {
    const url = new URL(TD_FX_URL);
    url.searchParams.set('symbol', FX_PAIR);
    url.searchParams.set('apikey', key);
    const res = await fetch(url.toString(), {
      method: 'GET', mode: 'cors', credentials: 'omit', referrerPolicy: 'no-referrer', cache: 'no-store', redirect: 'error',
    });
    const text = await res.text();
    if (text.length > 10000) throw new Error('응답이 너무 커요.');
    let j;
    try { j = JSON.parse(text); } catch (e) { throw new Error('응답 형식 오류'); }
    const code = Number(j && j.code) || res.status;
    if (code === 401 || code === 403) throw Object.assign(new Error('Twelve Data 키가 올바르지 않거나 권한이 없어요.'), { fatal: true });
    if (code === 429) throw Object.assign(new Error('한도 초과'), { retry: true });
    const rate = Number(j && j.rate);
    if (!j || j.symbol !== FX_PAIR || !Number.isFinite(rate) || rate < 1 || rate > 100000) throw new Error('환율 응답 오류');
    const ts = Number(j.timestamp) * 1000; // 기준 시각 (초 → 밀리초). 이상하면 받은 시각을 써요.
    const now = Date.now();
    const at = Number.isFinite(ts) && ts > now - 30 * 86400000 && ts < now + 86400000 ? ts : now;
    return { rate, at };
  }
  function fxFail(reason) {
    if (state.fx.status === 'ok') return;
    state.fx = { status: 'fail', rate: null, at: null, reason };
    scheduleRender(true);
  }
  // 앱을 열 때 한 번. 과거 가격과 같은 대기열(8초에 1건)을 쓰고, 맨 앞에 넣어요.
  function requestFx() {
    if (!state.settings.fxAuto || state.fx.status === 'ok' || state.fx.status === 'loading') return;
    const key = getTdKey();
    if (!key) { fxFail('Twelve Data 키 없음'); return; }
    state.fx = { status: 'loading', rate: null, at: null, reason: '' };
    if (!td.queue.some((j) => j.kind === 'fx')) td.queue.unshift({ id: 'fx', runId: null, symbol: FX_PAIR, kind: 'fx', key, tries: 0 });
    scheduleRender(false);
    tdPump();
  }
  // 실패한 뒤 키를 넣거나 자동을 다시 켰을 때만 다시 받아요 (이미 받았으면 요청 없음).
  function retryFx() {
    if (state.fx.status === 'fail') state.fx.status = 'idle';
    requestFx();
  }

  function parseDaily(values) {
    const bars = [];
    for (const v of values) {
      const c = Number(v && v.close);
      if (v && typeof v.datetime === 'string' && DATE_RE.test(v.datetime) && Number.isFinite(c) && c > 0) bars.push([v.datetime, c]);
    }
    return bars.sort((a, b) => (a[0] < b[0] ? 1 : -1));
  }
  function parseIntraday(values) {
    const bars = [];
    for (const v of values) {
      const m = v && typeof v.datetime === 'string' ? DATETIME_RE.exec(v.datetime) : null;
      const c = Number(v && v.close);
      if (!m || !Number.isFinite(c) || c <= 0) continue;
      const mins = Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5]) / 60000;
      bars.push([mins, c, `${m[2]}/${m[3]} ${m[4]}:${m[5]}`]);
    }
    return bars.sort((a, b) => b[0] - a[0]);
  }

  function tdEnqueue(runId, symbol, kind, key) {
    const id = kind + ':' + symbol;
    if (td.queue.some((j) => j.id === id)) return;
    td.queue.push({ id, runId, symbol, kind, key, tries: 0 });
    tdPump();
  }
  // 환율 작업(runId: null)은 재시작과 상관없이 남겨요.
  const jobStale = (job) => job.runId !== null && job.runId !== state.runId;
  function tdDone(job) {
    const i = td.queue.indexOf(job);
    if (i >= 0) td.queue.splice(i, 1);
  }
  async function tdPump() {
    if (td.busy) return;
    td.busy = true;
    while (td.queue.length) {
      const job = td.queue[0];
      if (jobStale(job)) { tdDone(job); continue; }
      const wait = TD_SPACING - (Date.now() - td.last);
      if (wait > 0) await sleep(wait);
      if (jobStale(job) || !td.queue.includes(job)) { tdDone(job); continue; }
      td.last = Date.now();
      try {
        if (job.kind === 'fx') {
          const fxKey = getTdKey(); // 기다리는 동안 키가 지워졌으면 보내지 않아요
          if (!fxKey) { fxFail('Twelve Data 키 없음'); tdDone(job); continue; }
          const r = await fetchFxRate(fxKey);
          state.fx = { status: 'ok', rate: r.rate, at: r.at, reason: '' };
          state.settings.fxLast = { rate: r.rate, at: r.at };
          saveState();
          scheduleRender(true);
        } else if (job.kind === 'daily') {
          const bars = parseDaily(await fetchSeries(job.symbol, '1day', DAILY_BARS, job.key));
          if (bars.length) { state.daily.set(job.symbol, { fetchedAt: Date.now(), bars, full: true }); saveHistCache(); }
        } else {
          const bars = parseIntraday(await fetchSeries(job.symbol, '5min', 24, job.key));
          if (bars.length) state.intraday.set(job.symbol, { fetchedAt: Date.now(), bars });
        }
        tdDone(job);
      } catch (e) {
        if (e.fatal) {
          if (td.queue.some((j) => j.kind === 'fx')) fxFail('키 오류');
          td.queue.length = 0;
          showBanner(e.message + ' 설정에서 Twelve Data 키를 확인해 주세요.', true);
          break;
        }
        if (e.retry && job.tries < 1) { job.tries++; td.last = Date.now() + 52000; continue; } // 1분 쉬고 한 번만 재시도
        if (job.kind === 'fx') fxFail('받기 실패');
        else if (e.unsupported) state.histUnsupported.add(job.symbol);
        tdDone(job);
      }
      updateHistBanner();
      scheduleRender(false);
    }
    td.busy = false;
    updateHistBanner();
  }
  function updateHistBanner() {
    if (state.mode !== 'live' || state.settings.period === 'day') return;
    const n = td.queue.length;
    if (n > 0) showBanner(`${PERIODS[state.settings.period]} 전 가격을 받는 중 · ${n}종목 남음 (종목당 약 8초)`);
    else if (!ui.banner.classList.contains('error')) showBanner('');
  }

  // 지금 선택된 기간에 필요한 과거 데이터를 요청한다.
  function requestHistory(runId) {
    if (state.mode !== 'live') return;
    const key = getTdKey();
    const period = state.settings.period;
    clearInterval(td.intradayTimer);
    if (period === 'day') { updateHistBanner(); return; }
    if (!key) {
      showBanner(`${PERIODS[period]} 등락을 보려면 설정에서 Twelve Data 키를 넣어 주세요.`);
      return;
    }
    const usd = state.holdings.filter((h) => h.currency === 'USD' && !state.histUnsupported.has(h.symbol));
    if (PERIOD_DAYS[period]) {
      for (const h of usd) {
        const d = state.daily.get(h.symbol);
        if (!d || !d.full || Date.now() - d.fetchedAt > DAILY_MAX_AGE) tdEnqueue(runId, h.symbol, 'daily', key);
      }
    } else if (period === '1h') {
      // 하루 800건 한도: 종목이 많을수록 새로고침 간격을 늘린다 (최소 10분)
      const everyMin = Math.max(10, Math.ceil(usd.length * 0.7));
      const refresh = (force) => {
        for (const h of usd) {
          const d = state.intraday.get(h.symbol);
          if (force || !d || Date.now() - d.fetchedAt > everyMin * 60000) tdEnqueue(runId, h.symbol, 'intraday', key);
        }
      };
      refresh(false);
      td.intradayTimer = setInterval(() => {
        if (runId === state.runId && marketSession().text === '정규장') refresh(false);
      }, 60000);
    }
    updateHistBanner();
  }

  function setPeriod(period) {
    if (!Object.prototype.hasOwnProperty.call(PERIODS, period) || period === state.settings.period) return;
    state.settings.period = period;
    saveState();
    showBanner('');
    if (state.mode === 'demo') showBanner(DEMO_BANNER);
    requestHistory(state.runId);
    renderNow();
  }

  // ---------------------------------------------------------------------------
  // 데모 (가상 데이터 — 실제 시세가 아님)
  // ---------------------------------------------------------------------------
  const DEMO = {
    holdings: [
      { symbol: 'VOO', currency: 'USD', shares: 6, avgPrice: 480, group: '미국 지수', manualPrice: null },
      { symbol: 'QQQ', currency: 'USD', shares: 4, avgPrice: 450, group: '미국 지수', manualPrice: null },
      { symbol: 'VTI', currency: 'USD', shares: 3, avgPrice: 260, group: '미국 지수', manualPrice: null },
      { symbol: 'SCHD', currency: 'USD', shares: 30, avgPrice: 26, group: '배당', manualPrice: null },
      { symbol: 'JEPI', currency: 'USD', shares: 10, avgPrice: 55, group: '배당', manualPrice: null },
      { symbol: 'SOXX', currency: 'USD', shares: 5, avgPrice: 210, group: '반도체', manualPrice: null },
      { symbol: 'SMH', currency: 'USD', shares: 2, avgPrice: 240, group: '반도체', manualPrice: null },
      { symbol: 'TLT', currency: 'USD', shares: 6, avgPrice: 90, group: '채권', manualPrice: null },
      { symbol: '000660', currency: 'KRW', shares: 3, avgPrice: 180000, group: '한국', manualPrice: 205000, name: 'SK하이닉스' },
    ],
    base: { VOO: 500, QQQ: 470, VTI: 270, SCHD: 27, JEPI: 56, SOXX: 225, SMH: 255, TLT: 88 },
  };
  const DEMO_BANNER = '예시 화면이에요. [종목 관리]에서 보유 종목을 넣으면 내 포트폴리오로 바뀌어요.';
  function startDemo(runId) {
    state.quotes.clear();
    const spread = { '1h': 0.012, day: 0.05, '1w': 0.1, '1m': 0.2, '3m': 0.3, '6m': 0.4, '1y': 0.6, '3y': 1.0 };
    state.demoBases = {};
    for (const p of Object.keys(PERIODS)) state.demoBases[p] = {};
    for (const [s, pc] of Object.entries(DEMO.base)) {
      const start = pc * (1 + (Math.random() - 0.45) * 0.05);
      state.quotes.set(s, { supported: true, price: start, prevClose: pc, lastTrade: null });
      for (const p of Object.keys(PERIODS)) {
        state.demoBases[p][s] = p === 'day' ? pc : start * (1 + (Math.random() - 0.5) * spread[p]);
      }
    }
    setConn('데모', 'warn');
    showBanner(DEMO_BANNER);
    const t = setInterval(() => {
      if (runId !== state.runId) return;
      const syms = Object.keys(DEMO.base);
      for (let i = 0; i < 2; i++) {
        const s = syms[Math.floor(Math.random() * syms.length)];
        const q = state.quotes.get(s);
        q.price = q.price * (1 + (Math.random() - 0.5) * 0.004);
      }
      state.layoutDirty = true;
      scheduleRender(false);
    }, 700);
    state.timers.push(t);
    scheduleRender(true);
  }

  // ---------------------------------------------------------------------------
  // 시작 / 재시작
  // ---------------------------------------------------------------------------
  function restart() {
    state.runId++;
    const runId = state.runId;
    closeWS();
    state.timers.forEach(clearInterval);
    state.timers = [];
    state.quotes.clear();
    state.lastPrice.clear();
    clearInterval(td.intradayTimer);
    showBanner('');
    state.mode = state.settings.demo || state.holdings.length === 0 ? 'demo' : 'live';
    if (state.mode === 'demo') { startDemo(runId); return; }

    const key = getApiKey();
    scheduleRender(true);
    if (!key) {
      setConn('API 키 없음', 'bad');
      showBanner('설정에서 Finnhub 키를 넣으면 등락이 표시돼요.');
      return;
    }
    if (!storageOk) showBanner('이 브라우저는 저장소를 쓸 수 없어 새로고침하면 데이터가 사라져요.', true);
    setConn('시세 불러오는 중…', 'warn');
    td.queue = td.queue.filter((j) => j.kind === 'fx'); // 받는 중인 환율은 남겨요
    state.histUnsupported.clear();
    requestHistory(runId);
    loadQuotes(runId, key).then(() => connectWS(runId, key));
    state.timers.push(setInterval(() => { if (runId === state.runId) loadQuotes(runId, key); }, QUOTE_REFRESH));
  }

  // ---------------------------------------------------------------------------
  // 종목 관리 화면
  // ---------------------------------------------------------------------------
  function renderHoldingsList() {
    const rows = state.holdings.map((h, i) => {
      const tr = document.createElement('tr');
      tr.append(
        el('td', '', h.symbol), el('td', '', h.name || ''), el('td', '', h.currency), el('td', '', nfNum.format(h.shares)),
        el('td', '', fmtPrice(h.avgPrice, h.currency)), el('td', '', h.group),
      );
      const td = el('td');
      const del = el('button', 'btn danger', '삭제');
      del.type = 'button';
      del.addEventListener('click', () => {
        state.holdings.splice(i, 1);
        saveState(); renderHoldingsList(); restart();
      });
      td.appendChild(del);
      tr.appendChild(td);
      return tr;
    });
    if (!rows.length) {
      const tr = document.createElement('tr');
      const td = el('td', 'muted', '아직 종목이 없어요.'); td.colSpan = 7;
      tr.appendChild(td); rows.push(tr);
    }
    ui.holdingsBody.replaceChildren(...rows);
  }
  ui.addForm.addEventListener('submit', (ev) => {
    ev.preventDefault();
    try {
      if (state.holdings.length >= MAX_HOLDINGS) throw new Error(`종목은 최대 ${MAX_HOLDINGS}개까지예요.`);
      const h = validateHolding({
        symbol: $('fSymbol').value, currency: $('fCurrency').value, shares: $('fShares').value,
        avgPrice: $('fAvg').value, group: $('fGroup').value, manualPrice: $('fManual').value,
        name: $('fName').value,
      });
      if (state.holdings.some((x) => x.symbol === h.symbol)) throw new Error('이미 추가된 티커예요.');
      state.holdings.push(h);
      if (state.settings.demo) state.settings.demo = false;
      saveState();
      ui.addForm.reset();
      ui.formMsg.textContent = `${displayName(h)} 추가 완료`;
      renderHoldingsList();
      restart();
    } catch (e) {
      ui.formMsg.textContent = e.message;
    }
  });
  $('exportBtn').addEventListener('click', () => {
    // API 키는 절대 내보내지 않는다.
    const blob = new Blob([JSON.stringify({ version: 1, holdings: state.holdings }, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = 'stockmap-holdings.json';
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  ui.importFile.addEventListener('change', async () => {
    const file = ui.importFile.files && ui.importFile.files[0];
    ui.importFile.value = '';
    if (!file) return;
    try {
      if (file.size > MAX_IMPORT_BYTES) throw new Error('파일이 너무 커요 (최대 100KB).');
      const data = JSON.parse(await file.text());
      if (!data || !Array.isArray(data.holdings)) throw new Error('형식이 올바르지 않아요.');
      if (data.holdings.length > MAX_HOLDINGS) throw new Error(`종목은 최대 ${MAX_HOLDINGS}개까지예요.`);
      const list = []; const seen = new Set();
      for (const item of data.holdings) {
        const h = validateHolding(item);
        if (seen.has(h.symbol)) throw new Error(`중복된 티커: ${h.symbol}`);
        seen.add(h.symbol); list.push(h);
      }
      if (!window.confirm(`${list.length}개 종목으로 바꿀까요? 지금 목록은 지워져요.`)) return;
      state.holdings = list;
      state.settings.demo = false;
      saveState(); renderHoldingsList(); restart();
      ui.formMsg.textContent = '가져오기 완료';
    } catch (e) {
      ui.formMsg.textContent = '가져오기 실패: ' + (e instanceof SyntaxError ? 'JSON 형식 오류' : e.message);
    }
  });

  // ---------------------------------------------------------------------------
  // 설정 화면
  // ---------------------------------------------------------------------------
  function renderKeyStatus() {
    const k = getApiKey();
    ui.keyStatus.textContent = k ? '저장됨' : '저장된 키 없음';
    const t = getTdKey();
    $('tdKeyStatus').textContent = t ? '저장됨' : '저장된 키 없음';
  }
  $('tdKeyForm').addEventListener('submit', (ev) => {
    ev.preventDefault();
    const input = $('fTdKey');
    const k = input.value.trim();
    input.value = '';
    if (!API_KEY_RE.test(k)) { ui.settingsMsg.textContent = 'Twelve Data 키 형식이 올바르지 않아요 (영문·숫자만).'; return; }
    writeKey(TD_KEY_KEY, k);
    renderKeyStatus();
    ui.settingsMsg.textContent = 'Twelve Data 키를 저장했어요.';
    restart();
    retryFx();
  });
  $('deleteTdKeyBtn').addEventListener('click', () => {
    removeKey(TD_KEY_KEY);
    renderKeyStatus();
    ui.settingsMsg.textContent = 'Twelve Data 키를 삭제했어요.';
    restart();
  });
  function openSettings() {
    $('fFx').value = String(state.settings.fxRate);
    $('fFxAuto').checked = state.settings.fxAuto;
    $('fRange').value = String(state.settings.range);
    $('fDemo').checked = state.settings.demo;
    $('fRemember').checked = state.settings.rememberKeys;
    $('fRememberHoldings').checked = state.settings.rememberHoldings;
    ui.fKey.value = '';
    ui.settingsMsg.textContent = '';
    renderKeyStatus();
    ui.settingsDlg.showModal();
  }
  ui.keyForm.addEventListener('submit', (ev) => {
    ev.preventDefault();
    const k = ui.fKey.value.trim();
    ui.fKey.value = '';
    if (!API_KEY_RE.test(k)) { ui.settingsMsg.textContent = '키 형식이 올바르지 않아요 (영문·숫자만).'; return; }
    writeKey(API_KEY_KEY, k);
    renderKeyStatus();
    ui.settingsMsg.textContent = '키를 저장했어요.';
    restart();
  });
  $('deleteKeyBtn').addEventListener('click', () => {
    removeKey(API_KEY_KEY);
    renderKeyStatus();
    ui.settingsMsg.textContent = '키를 삭제했어요.';
    restart();
  });
  $('wipeBtn').addEventListener('click', () => {
    if (!window.confirm('키, 보유 종목, 저장된 가격을 이 기기에서 모두 지울까요? 되돌릴 수 없어요.')) return;
    for (const k of [STORE_KEY, API_KEY_KEY, TD_KEY_KEY, HIST_KEY]) { storeRemove(k); sessRemove(k); }
    state.holdings = []; state.settings = validateSettings(null);
    state.daily.clear(); state.intraday.clear();
    state.fx = { status: 'idle', rate: null, at: null, reason: '' };
    ui.settingsDlg.close();
    restart();
  });
  $('saveSettingsBtn').addEventListener('click', () => {
    try {
      state.settings.fxRate = toNumber($('fFx').value, { min: 1, max: 100000 });
      state.settings.fxAuto = $('fFxAuto').checked; // 끄면 직접 입력값만 써요
      state.settings.range = toNumber($('fRange').value, { min: 0.5, max: 20 });
      state.settings.demo = $('fDemo').checked;
      const remember = $('fRemember').checked;
      if (remember !== state.settings.rememberKeys) {
        const a = getApiKey(); const t = getTdKey();
        state.settings.rememberKeys = remember;
        if (a) writeKey(API_KEY_KEY, a);
        if (t) writeKey(TD_KEY_KEY, t);
      }
      state.settings.rememberHoldings = $('fRememberHoldings').checked;
      saveState(); // 끄면 저장돼 있던 보유 종목이 여기서 지워져요
      saveHistCache(); // 과거 가격 캐시도 같은 스위치를 따라요
      ui.settingsMsg.textContent = '적용했어요.';
      restart();
      retryFx(); // 자동을 켰는데 아직 못 받았으면 한 번 받아요 (이미 받았으면 요청 없음)
    } catch (e) {
      ui.settingsMsg.textContent = e.message;
    }
  });

  // ---------------------------------------------------------------------------
  // 공통 이벤트
  // ---------------------------------------------------------------------------
  document.querySelectorAll('[data-close]').forEach((b) => {
    b.addEventListener('click', () => $(b.dataset.close).close());
  });
  $('manageBtn').addEventListener('click', () => { ui.formMsg.textContent = ''; renderHoldingsList(); ui.manageDlg.showModal(); });
  $('settingsBtn').addEventListener('click', openSettings);
  ui.maskBtn.addEventListener('click', () => {
    state.settings.masked = !state.settings.masked;
    saveState(); scheduleRender(true);
  });
  ui.map.addEventListener('pointerleave', (ev) => { if (ev.pointerType === 'mouse') hideTip(); });
  if ('ResizeObserver' in window) {
    let rt = null;
    new ResizeObserver(() => { clearTimeout(rt); rt = setTimeout(() => scheduleRender(true), 150); }).observe(ui.map);
  }
  setInterval(updateMarketBadge, 30000);
  document.querySelectorAll('.period-btn').forEach((b) => {
    b.addEventListener('click', () => setPeriod(b.dataset.period));
  });

  // 시작
  loadState();
  loadHistCache();
  updateMarketBadge();
  restart();
  requestFx(); // 앱을 열 때 환율 한 번
})();

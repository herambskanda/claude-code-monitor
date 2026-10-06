/* CCM dashboard core: state, filters, API client, chart helpers, import panel, router. Views live in views.js / views2.js. */
(function () {
  'use strict';
  var U = window.CCMUtil, esc = U.esc;
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };

  var TABS = [['overview', 'Overview'], ['limits', 'Limits'], ['when', 'When & where'], ['convs', 'Conversations'],
    ['tools', 'Tools & models'], ['context', 'Context'], ['anomalies', 'Anomalies'], ['settings', 'Settings']];
  var FILTER_KEYS = ['devices', 'accounts', 'projects', 'models', 'entrypoints'];
  var EST_TIP = 'Estimate. Anthropic does not publish plan limits. Capacity is calibrated from the utilization % Claude Code reported ' +
    '(one sample per upload) against the API-equivalent $ we computed for that window. It is a lower bound until every device that ' +
    'uses the account has uploaded, and claude.ai chat usage is invisible here. Set a manual capacity in Limits to override.';

  var S = {
    tab: 'overview',
    f: { from: '', to: '', devices: [], accounts: [], projects: [], models: [], entrypoints: [], q: '' },
    tzKey: 'local', theme: 'auto', groupBy: 'account', sens: 5, meta: null, preset: '30'
  };
  var cache = new Map();
  var charts = [];
  var CCM = window.CCM = { U: U, S: S, esc: esc, $: $, $$: $$, views: {}, EST_TIP: EST_TIP, TABS: TABS };

  /* ------------------------------------------------------------------ storage (always guarded) */
  function lsGet(k) { try { return window.localStorage.getItem(k); } catch (e) { return null; } }
  function lsSet(k, v) { try { window.localStorage.setItem(k, v); } catch (e) { /* private mode */ } }

  /* ------------------------------------------------------------------ timezone */
  CCM.tz = function () {
    if (S.tzKey === 'local' || S.tzKey === 'utc') return S.tzKey;
    var d = S.meta && S.meta.devices.filter(function (x) { return 'dev:' + x.id === S.tzKey; })[0];
    return d ? { offset: d.tz || 0 } : 'local';
  };
  var hourCache = { key: null, map: new Map() };
  /** calendar fields of an hour bucket (hours since epoch) in the selected timezone, memoised */
  CCM.hourFields = function (h) {
    if (hourCache.key !== S.tzKey) { hourCache = { key: S.tzKey, map: new Map() }; }
    var r = hourCache.map.get(h);
    if (!r) {
      var f = U.fields(CCM.tz(), h * 3600);
      r = { day: U.ymd(f), hour: f.h, dow: f.dow };
      hourCache.map.set(h, r);
    }
    return r;
  };
  CCM.fmtTime = function (e, withDate) { return U.fmtTime(CCM.tz(), e, withDate); };
  CCM.fmtDay = function (e) { return U.fmtDay(CCM.tz(), e); };
  CCM.dayStart = function (s) { return U.dayStart(CCM.tz(), s); };
  CCM.tzOffsetNow = function () { return U.tzOffsetMin(CCM.tz()); };

  /* ------------------------------------------------------------------ API */
  var pending = 0;
  function progress(delta) {
    pending += delta;
    var bar = $('#loadbar');
    if (pending > 0) { bar.style.opacity = 1; bar.style.width = '70%'; }
    else { bar.style.width = '100%'; setTimeout(function () { if (!pending) { bar.style.opacity = 0; bar.style.width = '0'; } }, 250); }
  }
  /** Query string for the current global filters. opts.noTime / opts.noFilters drop parts; extra adds params. */
  CCM.qs = function (extra, opts) {
    opts = opts || {};
    var p = new URLSearchParams(), f = S.f;
    if (!opts.noTime) {
      if (f.from) p.set('from', CCM.dayStart(f.from));
      if (f.to) p.set('to', CCM.dayStart(U.addDays(f.to, 1)));
    }
    if (!opts.noFilters) {
      FILTER_KEYS.forEach(function (k) { f[k].forEach(function (v) { p.append(k, v); }); });
      if (f.q) p.set('q', f.q);
    }
    Object.keys(extra || {}).forEach(function (k) { if (extra[k] != null) p.set(k, extra[k]); });
    return p.toString();
  };
  CCM.api = function (path, extra, opts) {
    var url = path + '?' + CCM.qs(extra, opts);
    if (cache.has(url)) return cache.get(url);
    progress(1);
    var pr = fetch(url).then(function (r) {
      return r.json().then(function (j) { if (!r.ok) throw new Error(j.error || r.statusText); return j; });
    }).then(function (j) { progress(-1); return j; }, function (e) { progress(-1); cache.delete(url); throw e; });
    cache.set(url, pr);
    return pr;
  };
  CCM.post = function (path, body) {
    return fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
      .then(function (r) { return r.json().then(function (j) { if (!r.ok) throw new Error(j.error || r.statusText); return j; }); })
      .then(function (j) { cache.clear(); return j; });
  };
  CCM.cube = function () { return CCM.api('/api/cube'); };
  CCM.anomalies = function (extra) {
    return CCM.api('/api/anomalies', Object.assign({ sens: S.sens, tz: CCM.tzOffsetNow() }, extra || {}));
  };

  /* ------------------------------------------------------------------ small UI helpers */
  CCM.info = function (text) { return '<i class="info" tabindex="0" data-tip="' + esc(text) + '">i</i>'; };
  CCM.badge = function (conf) {
    var tip = { observed: 'Account read from the config dir when the conversation was first seen', assumed: 'History from before the agent was installed: assumed to belong to the account currently logged in for that config dir',
      ambiguous: 'The login changed since the previous run, so this attribution is uncertain', override: 'Set by an override rule' }[conf] || '';
    return '<span class="badge ' + esc(conf) + '" title="' + esc(tip) + '">' + esc(conf || '?') + '</span>';
  };
  var toastTimer;
  CCM.toast = function (msg, bad) {
    var t = $('#toast');
    t.textContent = msg; t.style.display = 'block'; t.style.background = bad ? 'var(--bad)' : 'var(--text)';
    t.style.color = bad ? '#fff' : 'var(--bg)';
    clearTimeout(toastTimer); toastTimer = setTimeout(function () { t.style.display = 'none'; }, bad ? 6000 : 2600);
  };
  CCM.openModal = function (html, wide) {
    var box = $('#modal-box'); box.className = 'box' + (wide ? ' wide' : ''); box.innerHTML = html;
    $('#modal').classList.add('on'); return box;
  };
  CCM.closeModal = function () { $('#modal').classList.remove('on'); $('#modal-box').innerHTML = ''; };
  CCM.nameOf = function (kind, id) {
    var m = S.meta; if (!m) return id;
    var list = kind === 'dev' ? m.devices : m.accounts;
    var x = list.filter(function (d) { return d.id === id; })[0];
    return x ? x.name : id;
  };
  CCM.acctIndex = function (id) { return S.meta.accounts.findIndex(function (a) { return a.id === id; }); };
  CCM.devIndex = function (id) { return S.meta.devices.findIndex(function (a) { return a.id === id; }); };

  /* ------------------------------------------------------------------ colours + chart helpers */
  var T = {};
  function readTheme() {
    var cs = getComputedStyle(document.documentElement);
    var v = function (n) { return cs.getPropertyValue(n).trim(); };
    T = { text: v('--text'), text2: v('--text-2'), muted: v('--muted'), grid: v('--grid'), surface: v('--surface'), border: v('--border-strong'),
      accent: v('--accent'), good: v('--good'), warn: v('--warn'), bad: v('--bad'), other: v('--c-other'), surface2: v('--surface-2'),
      series: [1, 2, 3, 4, 5, 6, 7, 8].map(function (i) { return v('--c' + i); }) };
    CCM.T = T;
  }
  CCM.color = function (i) { return i >= 0 && i < 8 ? T.series[i] : T.other; };
  /** Mix of a CSS colour with the surface (for a heatmap ramp) */
  CCM.seq = function () { return [T.surface2, T.accent]; };

  function merge(a, b) {
    if (Array.isArray(b) || b === null || typeof b !== 'object') return b;
    var out = Object.assign({}, a || {});
    Object.keys(b).forEach(function (k) { out[k] = (a && typeof a[k] === 'object' && !Array.isArray(a[k]) && typeof b[k] === 'object' && !Array.isArray(b[k])) ? merge(a[k], b[k]) : b[k]; });
    return out;
  }
  CCM.merge = merge;
  /** common ECharts option skeleton */
  CCM.base = function (extra) {
    var ax = { axisLine: { lineStyle: { color: T.border } }, axisTick: { show: false }, axisLabel: { color: T.text2 }, splitLine: { lineStyle: { color: T.grid } }, nameTextStyle: { color: T.muted } };
    var o = {
      animationDuration: 350, color: T.series, textStyle: { color: T.text2, fontFamily: 'inherit' },
      grid: { left: 8, right: 16, top: 34, bottom: 8, containLabel: true },
      legend: { type: 'scroll', top: 0, textStyle: { color: T.text2 }, icon: 'roundRect', itemWidth: 10, itemHeight: 10, pageIconColor: T.text2, pageTextStyle: { color: T.text2 } },
      tooltip: { trigger: 'axis', backgroundColor: T.surface, borderColor: T.border, textStyle: { color: T.text, fontSize: 12 }, extraCssText: 'box-shadow:0 6px 24px rgba(0,0,0,.22);max-width:420px;white-space:normal;', confine: true, axisPointer: { type: 'shadow', shadowStyle: { color: 'rgba(128,128,128,.12)' }, lineStyle: { color: T.muted } } },
      xAxis: Object.assign({}, ax), yAxis: Object.assign({}, ax)
    };
    return extra ? merge(o, extra) : o;
  };
  CCM.axisY = function (extra) { return merge({ type: 'value', axisLine: { show: false }, axisTick: { show: false }, axisLabel: { color: T.text2 }, splitLine: { lineStyle: { color: T.grid } } }, extra || {}); };
  CCM.axisX = function (extra) { return merge({ type: 'category', axisLine: { lineStyle: { color: T.border } }, axisTick: { show: false }, axisLabel: { color: T.text2 }, splitLine: { show: false } }, extra || {}); };
  var keep = [];
  /** persistent=true: the chart belongs to the drawer and survives view re-renders (see CCM.disposeKept) */
  CCM.chart = function (el, option, persistent) {
    if (typeof el === 'string') el = $(el);
    if (!el) return null;
    var c = echarts.init(el, null, { renderer: 'canvas' });
    c.setOption(option);
    var ro = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(function () { c.resize(); }) : null;
    if (ro) ro.observe(el);
    (persistent ? keep : charts).push({ c: c, ro: ro });
    return c;
  };
  CCM.disposeKept = function () {
    keep.forEach(function (x) { if (x.ro) x.ro.disconnect(); try { x.c.dispose(); } catch (e) { /* gone */ } });
    keep = [];
  };
  function disposeCharts() {
    charts.forEach(function (x) { if (x.ro) x.ro.disconnect(); try { x.c.dispose(); } catch (e) { /* gone */ } });
    charts = [];
  }
  CCM.disposeCharts = disposeCharts;
  /** HTML for tooltip rows; every dynamic string is escaped */
  CCM.tipRow = function (color, name, value) {
    return '<div style="display:flex;gap:8px;justify-content:space-between;align-items:center"><span>' +
      (color ? '<span style="display:inline-block;width:9px;height:9px;border-radius:2px;background:' + esc(color) + ';margin-right:6px"></span>' : '') +
      esc(name) + '</span><b style="font-variant-numeric:tabular-nums">' + esc(value) + '</b></div>';
  };

  /* ------------------------------------------------------------------ cube helpers (time series built in the browser, in the chosen timezone) */
  CCM.C = { HOUR: 0, DEV: 1, ACCT: 2, MODEL: 3, CALLS: 4, USD: 5, INP: 6, OUT: 7, CR: 8, CW: 9, THINK: 10, ACT: 11 };
  /** daily series: returns {days, groups:[idx], vals: {idx: Float64Array}} grouping by 'dev' | 'acct' | 'model' (null = total) */
  CCM.daily = function (rows, by, col, range) {
    var C = CCM.C, gi = by === 'dev' ? C.DEV : by === 'acct' ? C.ACCT : by === 'model' ? C.MODEL : -1;
    var perDay = new Map(), groups = new Set();
    rows.forEach(function (r) {
      var day = CCM.hourFields(r[0]).day, g = gi < 0 ? 0 : r[gi];
      var m = perDay.get(day); if (!m) { m = new Map(); perDay.set(day, m); }
      m.set(g, (m.get(g) || 0) + (typeof col === 'function' ? col(r) : r[col]));
      groups.add(g);
    });
    var keys = Array.from(perDay.keys()).sort();
    var first = (range && range.from) || keys[0], last = (range && range.to) || keys[keys.length - 1];
    var days = [];
    if (first && last) { for (var d = first, n = 0; d <= last && n < 4000; d = U.addDays(d, 1), n++) days.push(d); }
    var gl = Array.from(groups).sort(function (a, b) { return a - b; }), vals = {};
    gl.forEach(function (g) { vals[g] = days.map(function (d) { var m = perDay.get(d); return m && m.get(g) || 0; }); });
    return { days: days, groups: gl, vals: vals };
  };
  CCM.range = function () {
    var f = S.f, m = S.meta && S.meta.range;
    return { from: f.from || (m && m.min ? CCM.fmtDay(m.min) : ''), to: f.to || (m && m.max ? CCM.fmtDay(m.max) : '') };
  };

  /* ------------------------------------------------------------------ filters */
  function setHash() {
    var p = new URLSearchParams(), f = S.f;
    if (f.from) p.set('from', f.from);
    if (f.to) p.set('to', f.to);
    if (S.preset) p.set('p', S.preset);
    FILTER_KEYS.forEach(function (k) { if (f[k].length) p.set(k, f[k].join('\u0001')); });
    if (f.q) p.set('q', f.q);
    var h = '#' + S.tab + (p.toString() ? '?' + p.toString() : '');
    if (location.hash !== h) history.replaceState(null, '', h);
  }
  function readHash() {
    var h = location.hash.replace(/^#/, ''), i = h.indexOf('?'), tab = i < 0 ? h : h.slice(0, i);
    if (TABS.some(function (t) { return t[0] === tab; })) S.tab = tab;
    if (i >= 0) {
      var p = new URLSearchParams(h.slice(i + 1));
      ['from', 'to', 'q'].forEach(function (k) { if (p.has(k)) S.f[k] = p.get(k); });
      if (p.has('p')) S.preset = p.get('p');
      FILTER_KEYS.forEach(function (k) { if (p.has(k)) S.f[k] = p.get(k).split('\u0001').filter(Boolean); });
      return p.has('from') || p.has('to') || p.has('p');
    }
    return false;
  }
  function anchorDay() {
    var now = Date.now() / 1000, m = S.meta.range.max;
    var e = (m && m < now - 30 * 86400) ? m : now;
    return CCM.fmtDay(e);
  }
  function applyPreset(p) {
    S.preset = p;
    if (p === 'all') { S.f.from = ''; S.f.to = ''; return; }
    var a = anchorDay();
    S.f.to = a; S.f.from = U.addDays(a, -(parseInt(p, 10) - 1));
  }
  var renderTimer;
  CCM.changed = function (immediate) {
    setHash(); renderFilters(true);
    clearTimeout(renderTimer);
    renderTimer = setTimeout(render, immediate ? 0 : 250);
  };
  /** patch filters from outside (anomaly jump, drill-down) */
  CCM.setFilters = function (patch, tab) {
    if ('from' in patch || 'to' in patch) S.preset = '';
    Object.assign(S.f, patch);
    if (tab) S.tab = tab;
    renderTabs();
    CCM.changed(true);
  };
  CCM.resetFilters = function () {
    S.f = { from: '', to: '', devices: [], accounts: [], projects: [], models: [], entrypoints: [], q: '' };
    applyPreset('30'); CCM.changed(true);
  };
  CCM.rerender = function () { render(); };
  CCM.go = function (tab) { S.tab = tab; renderTabs(); setHash(); render(); window.scrollTo(0, 0); };

  var DD = [['devices', 'Devices'], ['accounts', 'Accounts'], ['projects', 'Projects'], ['models', 'Models'], ['entrypoints', 'Entrypoints']];
  function ddOptions(key) {
    var m = S.meta;
    if (key === 'devices') return m.devices.map(function (d) { return { v: d.id, label: d.name, n: d.os || '' }; });
    if (key === 'accounts') return m.account_info.map(function (a) { return { v: a.id, label: a.name, n: a.plan ? a.plan.split(' / ').pop() : '' }; });
    if (key === 'models') return m.models.map(function (x) { return { v: x, label: x, n: '' }; });
    if (key === 'projects') return m.projects.map(function (p) { return { v: p[0], label: p[0], n: p[1] }; });
    return m.entrypoints.map(function (p) { return { v: p[0], label: p[0], n: p[1] }; });
  }
  function labelFor(key, v) {
    if (key === 'devices') return CCM.nameOf('dev', v);
    if (key === 'accounts') return CCM.nameOf('acct', v);
    return v;
  }
  function renderFilters(light) {
    var box = $('#filters'), f = S.f;
    if (!S.meta) return;
    if (!light || !$('#f-from')) {
      box.innerHTML = '<div class="seg" id="presets">' + [['7', '7d'], ['30', '30d'], ['90', '90d'], ['all', 'All']].map(function (p) {
        return '<button data-p="' + p[0] + '" aria-pressed="false">' + p[1] + '</button>'; }).join('') + '</div>' +
        '<input type="date" id="f-from" aria-label="From date"><span class="muted">to</span><input type="date" id="f-to" aria-label="To date">' +
        '<span class="sep"></span>' + DD.map(function (d) {
          return '<div class="dd" data-k="' + d[0] + '"><button class="btn" aria-haspopup="true">' + d[1] + ' <span class="count hidden"></span></button></div>'; }).join('') +
        '<input type="search" id="f-q" placeholder="Search titles, projects" aria-label="Search titles and projects">' +
        '<button class="btn" id="f-reset">Reset</button><div class="chips" id="chips"></div>';
    }
    $('#f-from').value = f.from; $('#f-to').value = f.to; $('#f-q').value = f.q === undefined ? '' : f.q;
    $$('#presets button').forEach(function (b) { b.setAttribute('aria-pressed', String(b.getAttribute('data-p') === S.preset)); });
    $$('.dd').forEach(function (d) {
      var k = d.getAttribute('data-k'), c = $('.count', d);
      c.textContent = f[k].length; c.classList.toggle('hidden', !f[k].length);
    });
    var chips = [];
    DD.forEach(function (d) { f[d[0]].forEach(function (v) {
      chips.push('<span class="chip">' + esc(d[1].replace(/s$/, '')) + ': ' + esc(labelFor(d[0], v)) + '<button data-k="' + d[0] + '" data-v="' + esc(v) + '" aria-label="Remove filter">&times;</button></span>'); }); });
    $('#chips').innerHTML = chips.join('');
    $('#chips').style.display = chips.length ? 'flex' : 'none';
  }
  function openDD(dd) {
    closeDDs();
    var k = dd.getAttribute('data-k'), opts = ddOptions(k), sel = new Set(S.f[k]);
    var p = document.createElement('div'); p.className = 'panel';
    p.innerHTML = (opts.length > 8 ? '<input type="search" placeholder="Filter list" aria-label="Filter list">' : '') + '<div class="opts"></div>' +
      '<div class="row"><button class="btn sm" data-a="clear">Clear</button><button class="btn sm" data-a="close">Done</button></div>';
    dd.appendChild(p);
    var list = $('.opts', p);
    function fill(q) {
      var shown = opts.filter(function (o) { return !q || o.label.toLowerCase().indexOf(q) >= 0; }).slice(0, 300);
      list.innerHTML = shown.length ? shown.map(function (o) {
        return '<label><input type="checkbox" value="' + esc(o.v) + '"' + (sel.has(o.v) ? ' checked' : '') + '><span>' + esc(o.label) + '</span><span class="n">' + esc(o.n) + '</span></label>';
      }).join('') : '<div class="muted" style="padding:8px">Nothing to select</div>';
    }
    fill('');
    var inp = $('input[type=search]', p);
    if (inp) inp.addEventListener('input', function () { fill(inp.value.toLowerCase()); });
    p.addEventListener('change', function (e) {
      if (e.target.type !== 'checkbox') return;
      if (e.target.checked) sel.add(e.target.value); else sel.delete(e.target.value);
      S.f[k] = Array.from(sel); CCM.changed();
    });
    p.addEventListener('click', function (e) {
      var a = e.target.getAttribute && e.target.getAttribute('data-a');
      if (a === 'clear') { sel.clear(); S.f[k] = []; fill(inp ? inp.value.toLowerCase() : ''); CCM.changed(); }
      if (a === 'close') closeDDs();
    });
    if (inp) inp.focus();
  }
  function closeDDs() { $$('.dd .panel').forEach(function (p) { p.remove(); }); }

  function bindFilterEvents() {
    var box = $('#filters');
    box.addEventListener('click', function (e) {
      var t = e.target;
      if (t.closest('.panel')) return;
      var pb = t.closest('#presets button');
      if (pb) { applyPreset(pb.getAttribute('data-p')); CCM.changed(true); return; }
      var ddb = t.closest('.dd > button');
      if (ddb) { var dd = ddb.parentNode; if ($('.panel', dd)) closeDDs(); else openDD(dd); return; }
      if (t.id === 'f-reset') { CCM.resetFilters(); return; }
      var cb = t.closest('.chip button');
      if (cb) { var k = cb.getAttribute('data-k'), v = cb.getAttribute('data-v'); S.f[k] = S.f[k].filter(function (x) { return x !== v; }); CCM.changed(); }
    });
    box.addEventListener('change', function (e) {
      if (e.target.id === 'f-from' || e.target.id === 'f-to') {
        S.f[e.target.id === 'f-from' ? 'from' : 'to'] = e.target.value; S.preset = ''; CCM.changed();
      }
    });
    var qt;
    box.addEventListener('input', function (e) {
      if (e.target.id === 'f-q') { clearTimeout(qt); qt = setTimeout(function () { S.f.q = e.target.value; CCM.changed(); }, 300); }
    });
    document.addEventListener('click', function (e) { if (!e.target.closest('.dd')) closeDDs(); });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') { closeDDs(); if ($('#modal').classList.contains('on')) CCM.closeModal(); else if (CCM.closeDrawer) CCM.closeDrawer(); }
    });
  }

  /* ------------------------------------------------------------------ header controls */
  function renderTabs() {
    $('#tabs').innerHTML = TABS.map(function (t) {
      return '<button role="tab" data-t="' + t[0] + '" aria-selected="' + (t[0] === S.tab) + '">' + esc(t[1]) + '</button>'; }).join('');
  }
  function renderTz() {
    var sel = $('#tz'), opts = [['local', 'Local time (' + U.tzLabel(-new Date().getTimezoneOffset()) + ')'], ['utc', 'UTC']];
    S.meta.devices.forEach(function (d) { if (d.tz != null) opts.push(['dev:' + d.id, d.name + ' (' + U.tzLabel(d.tz) + ')']); });
    sel.innerHTML = opts.map(function (o) { return '<option value="' + esc(o[0]) + '">' + esc(o[1]) + '</option>'; }).join('');
    if (!opts.some(function (o) { return o[0] === S.tzKey; })) S.tzKey = 'local';
    sel.value = S.tzKey;
  }
  function applyTheme() {
    var r = document.documentElement;
    if (S.theme === 'auto') r.removeAttribute('data-theme'); else r.setAttribute('data-theme', S.theme);
    readTheme();
  }
  function isDark() {
    return S.theme === 'dark' || (S.theme === 'auto' && window.matchMedia && matchMedia('(prefers-color-scheme: dark)').matches);
  }
  CCM.isDark = isDark;

  /* ------------------------------------------------------------------ import panel (drop zone + results) */
  CCM.importPanel = function (host, drop) {
    host.innerHTML = '<div class="dropzone" tabindex="0"><div><b>Drop export files here</b> or <a data-pick>choose files</a></div>' +
      '<div class="muted" style="margin-top:4px">ccm-&lt;device&gt;-&lt;date&gt;.ndjson.gz, as many as you like. Re-importing a newer file from the same device just updates it.</div>' +
      '<input type="file" multiple accept=".gz,.ndjson,.ndjson.gz" class="hidden"></div>' +
      '<div class="muted" style="margin-top:8px">Or copy the files into <span class="mono">' + esc(drop && drop.path || '') + '</span> ' +
      '(checked every ' + esc(drop ? drop.poll_seconds : 5) + ' s; imported files move to <span class="mono">done/</span>).</div><div class="results"></div>';
    var zone = $('.dropzone', host), inp = $('input[type=file]', host), res = $('.results', host);
    function run(files) { files = Array.prototype.slice.call(files || []); if (files.length) CCM.importFiles(files, res); }
    $('[data-pick]', host).addEventListener('click', function () { inp.click(); });
    zone.addEventListener('keydown', function (e) { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); inp.click(); } });
    inp.addEventListener('change', function () { run(inp.files); inp.value = ''; });
    ['dragenter', 'dragover'].forEach(function (ev) { zone.addEventListener(ev, function (e) { e.preventDefault(); e.stopPropagation(); zone.classList.add('over'); }); });
    ['dragleave', 'drop'].forEach(function (ev) { zone.addEventListener(ev, function (e) { e.preventDefault(); e.stopPropagation(); zone.classList.remove('over'); }); });
    zone.addEventListener('drop', function (e) { run(e.dataTransfer && e.dataTransfer.files); });
    host._run = run;
    return host;
  };
  CCM.importFiles = function (files, resEl) {
    var done = 0, ok = 0;
    function row(f, cls, html) {
      var d = document.createElement('div'); d.className = 'r ' + cls; d.innerHTML = html; resEl.appendChild(d); return d;
    }
    return files.reduce(function (chain, file) {
      return chain.then(function () {
        var r = row(file, 'duplicate', '<span class="mono">' + esc(file.name) + '</span><span class="muted">importing...</span>');
        return fetch('/api/import', { method: 'POST', headers: { 'Content-Type': 'application/octet-stream', 'X-CCM-Filename': encodeURIComponent(file.name) }, body: file })
          .then(function (resp) { return resp.json().catch(function () { return { status: 'error', error: resp.statusText }; }); })
          .then(function (j) {
            var st = j.status || 'error';
            if (st === 'ok') ok++;
            r.className = 'r ' + (st === 'ok' ? 'ok' : st === 'duplicate' ? 'duplicate' : 'error');
            r.innerHTML = '<b>' + (st === 'ok' ? 'Imported' : st === 'duplicate' ? 'Already imported' : 'Error') + '</b><span class="mono">' + esc(file.name) + '</span>' +
              (j.device ? '<span>device <b>' + esc(j.device) + '</b></span>' : '') +
              (st === 'ok' ? '<span class="muted">' + U.fmtInt(j.sessions) + ' conversations, ' + U.fmtInt(j.calls) + ' calls</span>' : '') +
              (st === 'error' ? '<span style="color:var(--bad)">' + esc(j.error || 'failed') + '</span>' : '');
          }, function (e) { r.className = 'r error'; r.innerHTML = '<b>Error</b><span class="mono">' + esc(file.name) + '</span><span>' + esc(e.message) + '</span>'; })
          .then(function () { done++; });
      });
    }, Promise.resolve()).then(function () { if (ok) { CCM.reload(); CCM.toast('Imported ' + ok + ' file' + (ok > 1 ? 's' : '')); } });
  };
  CCM.openImport = function () {
    var box = CCM.openModal('<div style="display:flex;align-items:center;margin-bottom:12px"><h2 style="flex:1">Import export files</h2><button class="btn sm" data-close>Close</button></div><div id="imp-host"></div>');
    $('[data-close]', box).addEventListener('click', CCM.closeModal);
    CCM.importPanel($('#imp-host', box), S.meta.drop);
    return $('#imp-host', box);
  };
  CCM.emptyState = function (el) {
    var d = S.meta.drop;
    el.innerHTML = '<div class="hero"><h1>No data yet</h1><p>Drop the files you received here, or copy them into <span class="mono">' + esc(d.path) + '</span>.</p><div id="empty-imp" style="text-align:left;margin-top:16px"></div></div>';
    CCM.importPanel($('#empty-imp', el), d);
  };

  /* ------------------------------------------------------------------ render loop */
  var renderSeq = 0;
  async function render() {
    var seq = ++renderSeq, el = $('#view');
    disposeCharts();
    if (!S.meta) return;
    if (S.meta.empty && S.tab !== 'settings') { CCM.emptyState(el); return; }
    var v = CCM.views[S.tab];
    el.innerHTML = '<div class="empty">Loading...</div>';
    var host = document.createElement('div');
    try {
      await v.render(host, seq, function () { return seq === renderSeq; });
    } catch (e) {
      if (seq !== renderSeq) return;
      console.error(e);
      el.innerHTML = '<div class="callout bad">Could not load this view: ' + esc(e.message || e) + '</div>';
    }
  }
  /** Views call mount(html, init) once data is ready: it swaps the markup in and then lets init build charts. */
  CCM.mount = function (host, html, init) {
    var old = $('#view');
    disposeCharts();
    var el = old.cloneNode(false);      // fresh element: views attach their own click handlers, old ones must not pile up
    old.parentNode.replaceChild(el, old);
    el.innerHTML = html;
    if (init) init(el);
  };

  CCM.reload = async function () {
    cache.clear();
    try {
      S.meta = await fetch('/api/meta').then(function (r) { return r.json(); });
    } catch (e) { CCM.toast('Cannot reach the dashboard server', true); return; }
    renderTz();
    renderFilters(false);
    render();
  };

  CCM.start = async function () {
    S.theme = lsGet('ccm.theme') || 'auto';
    S.tzKey = lsGet('ccm.tz') || 'local';
    S.groupBy = lsGet('ccm.groupBy') || 'account';
    S.sens = parseFloat(lsGet('ccm.sens')) || 5;
    applyTheme();
    renderTabs();
    $('#tabs').addEventListener('click', function (e) { var b = e.target.closest('button'); if (b) CCM.go(b.getAttribute('data-t')); });
    $('#tz').addEventListener('change', function (e) { S.tzKey = e.target.value; lsSet('ccm.tz', S.tzKey); S.preset = S.preset; if (S.preset) applyPreset(S.preset); CCM.changed(true); });
    $('#btn-theme').addEventListener('click', function () {
      S.theme = isDark() ? 'light' : 'dark'; lsSet('ccm.theme', S.theme); applyTheme(); render();
    });
    $('#btn-refresh').addEventListener('click', function () { CCM.reload(); });
    $('#btn-import').addEventListener('click', CCM.openImport);
    $('#modal').addEventListener('mousedown', function (e) { if (e.target.id === 'modal') CCM.closeModal(); });
    if (window.matchMedia) matchMedia('(prefers-color-scheme: dark)').addEventListener && matchMedia('(prefers-color-scheme: dark)').addEventListener('change', function () { if (S.theme === 'auto') { applyTheme(); render(); } });
    // dropping files anywhere opens the import dialog and imports them
    ['dragover', 'drop'].forEach(function (ev) { window.addEventListener(ev, function (e) {
      if (!e.dataTransfer || !Array.prototype.some.call(e.dataTransfer.types || [], function (t) { return t === 'Files'; })) return;
      e.preventDefault();
      if (ev === 'drop' && !e.target.closest('.dropzone')) { var h = CCM.openImport(); if (h._run) h._run(e.dataTransfer.files); }
    }); });
    window.addEventListener('hashchange', function () { var before = JSON.stringify(S.f) + S.tab; readHash(); if (JSON.stringify(S.f) + S.tab !== before) { renderTabs(); renderFilters(true); render(); } });
    document.addEventListener('submit', function (e) { e.preventDefault(); });   // forms are handled by their buttons (no inline handlers: CSP)
    bindFilterEvents();
    try {
      S.meta = await fetch('/api/meta').then(function (r) { if (!r.ok) throw new Error(r.statusText); return r.json(); });
    } catch (e) { $('#view').innerHTML = '<div class="callout bad">Cannot load data: ' + esc(e.message) + '</div>'; return; }
    var hadRange = readHash();
    if (!hadRange) applyPreset('30');
    renderTz();
    renderTabs();
    renderFilters(false);
    setHash();
    render();
    setInterval(function () { if (!document.hidden) pollMeta(); }, 20000);
  };

  /** pick up imports that arrive while the page is open (drop folder, ingest server) */
  async function pollMeta() {
    try {
      var m = await fetch('/api/meta').then(function (r) { return r.json(); });
      if (S.meta && (m.calls !== S.meta.calls || m.sessions !== S.meta.sessions)) {
        CCM.toast('New data imported, refreshing');
        CCM.reload();
      } else if (S.meta) { S.meta.drop = m.drop; }
    } catch (e) { /* offline */ }
  }
})();

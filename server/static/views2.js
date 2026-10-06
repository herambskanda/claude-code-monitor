/* Views: Conversations (+drawer), Tools & models, Context, Anomalies, Settings */
(function () {
  'use strict';
  var CCM = window.CCM, U = CCM.U, esc = CCM.esc, S = CCM.S, $ = CCM.$, $$ = CCM.$$;
  var V = CCM.views, card = CCM.card, chartDiv = CCM.chartDiv, none = CCM.none, tx = CCM.tx;
  var EST = function () { return CCM.info(CCM.EST_TIP); };

  function pctCell(v) {
    if (v == null) return '<span class="muted">-</span>';
    var col = v >= 100 ? 'var(--bad)' : v >= 80 ? 'var(--warn)' : 'var(--accent)';
    return '<div class="pctbar"><i style="width:' + Math.min(100, v) + '%;background:' + col + '"></i><span>' + v.toFixed(v < 10 ? 1 : 0) + '%</span></div>';
  }
  function ago(iso) {
    if (!iso) return '-';
    var t = Date.parse(iso.length <= 19 && iso.indexOf('Z') < 0 ? iso + 'Z' : iso);
    if (isNaN(t)) return iso;
    var s = (Date.now() - t) / 1000;
    if (s < 90) return 'just now';
    if (s < 5400) return Math.round(s / 60) + ' min ago';
    if (s < 172800) return Math.round(s / 3600) + ' h ago';
    return Math.round(s / 86400) + ' days ago';
  }
  function put(path, body, ok) {
    return CCM.post(path, body).then(function (r) { CCM.toast(ok || 'Saved'); return r; }, function (e) { CCM.toast(e.message, true); throw e; });
  }

  /* ------------------------------------------------------------------ Conversations table */
  var CV = { sort: 'start', dir: 'desc', limit: 300 };
  var COLS = [
    ['title', 'Title', ''], ['project', 'Project', ''], ['device', 'Device', ''], ['account', 'Account', ''], ['entry', 'Entry', ''],
    ['start', 'Start', ''], ['active', 'Active', 'num'], ['prompts', 'Prompts', 'num'], ['calls', 'Calls', 'num'], ['tokens', 'Tokens', 'num'],
    ['usd', 'API $', 'num'], ['p5', 'Peak 5h est.', 'num'], ['p7', 'Weekly est.', 'num'], ['ctx', 'Ctx peak', 'num'], ['compact', 'Compact.', 'num'], ['subs', 'Subagents', 'num']
  ];
  V.convs = {
    render: async function (host, seq, alive) {
      var r = await CCM.api('/api/sessions', { sort: CV.sort, dir: CV.dir, limit: CV.limit });
      if (!alive()) return;
      if (!r.total) { CCM.mount(host, none('No conversations match these filters.')); return; }
      var head = COLS.map(function (c) {
        var arrow = CV.sort === c[0] ? (CV.dir === 'asc' ? ' &#9650;' : ' &#9660;') : '';
        var tip = (c[0] === 'p5' || c[0] === 'p7') ? EST() : '';
        return '<th class="s ' + c[2] + '" data-sort="' + c[0] + '">' + c[1] + arrow + tip + '</th>';
      }).join('');
      var body = r.rows.map(function (x) {
        return '<tr class="click" data-conv="' + esc(x.d) + '|' + esc(x.sid) + '"><td class="title" title="' + esc(x.title) + '">' + esc(x.title || '(untitled)') + '</td><td class="proj" title="' + esc(x.project) + '">' + esc(x.project || '-') + '</td><td>' + esc(x.dev) + '</td>' +
          '<td class="nowrap">' + esc(x.acct) + ' ' + CCM.badge(x.conf) + '</td><td>' + esc(x.entry || '-') + '</td><td class="nowrap">' + esc(CCM.fmtTime(x.first)) + '</td>' +
          '<td class="num">' + U.fmtDur(x.active) + '</td><td class="num">' + U.fmtInt(x.prompts) + '</td><td class="num">' + U.fmtInt(x.calls) + '</td><td class="num">' + U.fmtN(x.tokens) + '</td>' +
          '<td class="num">' + U.fmtUSD(x.usd) + '</td><td class="num">' + pctCell(x.p5) + '</td><td class="num">' + pctCell(x.p7) + '</td><td class="num">' + U.fmtN(x.ctx) + '</td>' +
          '<td class="num">' + (x.compact || '-') + '</td><td class="num">' + (x.subs || '-') + '</td></tr>';
      }).join('');
      var html = card('Conversations', U.fmtInt(r.total) + ' match; showing ' + r.rows.length + '. Figures cover the selected date range and models. Percentages are estimates ' + EST(),
        '<div class="tw maxh"><table class="conv"><thead><tr>' + head + '</tr></thead><tbody>' + body + '</tbody></table></div>' +
        (r.rows.length < r.total ? '<div style="text-align:center;margin-top:10px"><button class="btn" id="more">Show 300 more</button></div>' : ''), 'span-12',
        '<span class="muted">use the search box above to filter by title or project</span>');
      CCM.mount(host, '<div class="grid">' + html + '</div>', function (el) {
        el.addEventListener('click', function (e) {
          var th = e.target.closest('th[data-sort]');
          if (th) { var k = th.getAttribute('data-sort'); if (CV.sort === k) CV.dir = CV.dir === 'asc' ? 'desc' : 'asc'; else { CV.sort = k; CV.dir = ['title', 'project', 'device', 'account', 'entry'].indexOf(k) >= 0 ? 'asc' : 'desc'; } CCM.rerender(); return; }
          if (e.target.id === 'more') { CV.limit += 300; CCM.rerender(); return; }
          var tr = e.target.closest('tr[data-conv]');
          if (tr) { var p = tr.getAttribute('data-conv').split('|'); CCM.openConv(p[0], p.slice(1).join('|')); }
        });
      });
    }
  };

  /* ------------------------------------------------------------------ Conversation drawer */
  var drawer = $('#drawer'), scrim = $('#scrim'), openKey = null;
  CCM.closeDrawer = function () {
    drawer.classList.remove('open'); scrim.classList.remove('on'); drawer.setAttribute('aria-hidden', 'true'); openKey = null;
    CCM.disposeKept();
  };
  scrim.addEventListener('click', CCM.closeDrawer);
  CCM.openConv = function (device, sid) {
    openKey = device + '|' + sid;
    CCM.disposeKept();
    drawer.innerHTML = '<div class="dh"><div style="flex:1"><h2>Loading...</h2></div><button class="btn sm" data-close>Close</button></div><div class="db"><div class="empty">Loading conversation...</div></div>';
    drawer.classList.add('open'); scrim.classList.add('on'); drawer.setAttribute('aria-hidden', 'false');
    $('[data-close]', drawer).addEventListener('click', CCM.closeDrawer);
    CCM.api('/api/conversation', { device: device, sid: sid }, { noTime: true, noFilters: true }).then(function (c) {
      if (openKey !== device + '|' + sid) return;
      renderConv(c);
    }, function (e) {
      drawer.innerHTML = '<div class="dh"><div style="flex:1"><h2>Conversation unavailable</h2></div><button class="btn sm" data-close>Close</button></div><div class="db"><div class="callout bad">' + esc(e.message) + '</div></div>';
      $('[data-close]', drawer).addEventListener('click', CCM.closeDrawer);
    });
  };

  function renderConv(c) {
    var r = c.row, models = c.models, ncalls = c.ncalls;
    var tok = models.reduce(function (a, m) { return { inp: a.inp + m.inp, out: a.out + m.out, cr: a.cr + m.cr, cw: a.cw + m.cw, think: a.think + m.think }; }, { inp: 0, out: 0, cr: 0, cw: 0, think: 0 });
    var hit = tok.inp + tok.cr + tok.cw ? 100 * tok.cr / (tok.inp + tok.cr + tok.cw) : 0;
    var diff = c.cc_cost ? (c.est_usd - c.cc_cost) / c.cc_cost * 100 : null;
    var kp = [['Calls', U.fmtInt(ncalls)], ['Tokens', U.fmtN(r.tokens)], ['API $ (ours)', U.fmtUSD(c.est_usd)],
      ['Claude Code cost', c.cc_cost == null ? 'n/a' : U.fmtUSD(c.cc_cost) + (diff == null ? '' : ' (' + (diff > 0 ? '+' : '') + diff.toFixed(0) + '%)')],
      ['Active', U.fmtDur(r.active)], ['Prompts', U.fmtInt(r.prompts)], ['Peak context', U.fmtN(r.ctx)], ['Cache hit', hit.toFixed(0) + '%'],
      ['Peak 5h est.', r.p5 == null ? 'n/a' : r.p5 + '%'], ['5h sum est.', r.p5s == null ? 'n/a' : r.p5s + '%'], ['Weekly est.', r.p7 == null ? 'n/a' : r.p7 + '%'], ['Compactions', String(c.compactions.length)]];
    var tools = c.tools.slice(0, 14);
    var mcp = Object.keys(c.mcp || {}).map(function (s) { return [s, c.mcp[s]]; });
    var html = '<div class="dh"><div style="flex:1;min-width:0"><h2 style="font-size:17px;overflow-wrap:anywhere">' + esc(r.title || '(untitled)') + '</h2>' +
      '<div class="t2" style="margin-top:4px">' + esc(r.project || 'no project') + (c.branch ? ' &middot; ' + esc(c.branch) : '') + ' &middot; ' + esc(r.dev) + ' &middot; ' + esc(r.acct) + ' ' + CCM.badge(r.conf) + ' &middot; ' + esc(r.entry || '-') + ' &middot; ' + esc(CCM.fmtTime(r.first)) + ' &rarr; ' + esc(CCM.fmtTime(r.last, false)) + '</div></div>' +
      '<button class="btn sm" id="dr-override">Change account</button><button class="btn sm" data-close>Close</button></div><div class="db">' +
      '<div class="mini-kpis">' + kp.map(function (k) { return '<div><b>' + esc(k[1]) + '</b><span>' + esc(k[0]) + (k[0].indexOf('est') >= 0 ? ' (estimate)' : '') + '</span></div>'; }).join('') + '</div>' +
      (c.stride > 1 ? '<div class="callout" style="margin-bottom:12px">Timeline thinned to every ' + c.stride + 'th call (' + U.fmtInt(ncalls) + ' calls).</div>' : '') +
      '<div class="grid"><section class="card span-12"><header><h2>Context size per call</h2><span class="sub">dashed lines are compactions; grey dots are subagent calls</span></header>' + chartDiv('dr-ctx', 'short') + '</section>' +
      '<section class="card span-12"><header><h2>Tokens per call</h2><span class="sub">stacked</span></header>' + chartDiv('dr-tok', 'short') + '</section>' +
      '<section class="card span-6"><header><h2>Cumulative API-equivalent $</h2></header>' + chartDiv('dr-cum', 'xs') + '</section>' +
      '<section class="card span-6"><header><h2>Tools</h2></header>' + (tools.length ? chartDiv('dr-tools', 'xs') : none('No tool calls')) + '</section>' +
      '<section class="card span-12"><header><h2>Model split</h2><span class="sub">our estimate vs the cost Claude Code recorded</span></header><div class="tw"><table><thead><tr><th>Model</th><th class="num">Calls</th><th class="num">API $ (ours)</th><th class="num">Claude Code $</th><th class="num">Input</th><th class="num">Output</th><th class="num">Cache read</th><th class="num">Cache write</th><th class="num">Thinking</th></tr></thead><tbody>' +
      models.map(function (m) { return '<tr><td>' + esc(m.model) + '</td><td class="num">' + U.fmtInt(m.calls) + '</td><td class="num">' + U.fmtUSD(m.usd) + '</td><td class="num">' + (m.cc_usd == null ? '-' : U.fmtUSD(m.cc_usd)) + '</td><td class="num">' + U.fmtN(m.inp) + '</td><td class="num">' + U.fmtN(m.out) + '</td><td class="num">' + U.fmtN(m.cr) + '</td><td class="num">' + U.fmtN(m.cw) + '</td><td class="num">' + U.fmtN(m.think) + '</td></tr>'; }).join('') + '</tbody></table></div></section>' +
      (mcp.length ? '<section class="card span-6"><header><h2>MCP</h2></header><div class="tw"><table><tbody>' + mcp.map(function (m) { return '<tr><td><b>' + esc(m[0]) + '</b></td><td>' + Object.keys(m[1]).map(function (t) { return esc(t) + ' &times;' + m[1][t]; }).join(', ') + '</td></tr>'; }).join('') + '</tbody></table></div></section>' : '') +
      (c.subagents.length ? '<section class="card span-6"><header><h2>Subagents</h2><span class="sub">' + c.subagents.length + '</span></header><div class="tw maxh" style="max-height:280px"><table><thead><tr><th>Id</th><th class="num">Calls</th><th class="num">API $</th><th class="num">Tokens</th><th class="num">Peak ctx</th><th class="num">Duration</th></tr></thead><tbody>' +
        c.subagents.slice(0, 60).map(function (s) { return '<tr><td class="mono">' + esc(s.id.slice(0, 10)) + '</td><td class="num">' + s.calls + '</td><td class="num">' + U.fmtUSD(s.usd) + '</td><td class="num">' + U.fmtN(s.tokens) + '</td><td class="num">' + U.fmtN(s.ctx_peak) + '</td><td class="num">' + U.fmtDur(s.last - s.first) + '</td></tr>'; }).join('') + '</tbody></table></div></section>' : '') +
      '<section class="card span-6"><header><h2>Share of the 5h limit ' + EST() + '</h2></header>' + (c.windows5.length ? '<div class="tw"><table><thead><tr><th>Window</th><th class="num">API $</th><th class="num">Est. %</th></tr></thead><tbody>' + c.windows5.map(function (w) { return '<tr><td>' + esc(CCM.fmtTime(w.start)) + '</td><td class="num">' + U.fmtUSD(w.usd) + '</td><td class="num">' + pctCell(w.pct) + '</td></tr>'; }).join('') + '</tbody></table></div>' : none('No estimate (no capacity yet)')) + '</section>' +
      '<section class="card span-6"><header><h2>Details</h2></header><dl class="kv"><dt>Directory</dt><dd class="mono">' + esc(c.cwd || '-') + '</dd><dt>Config dir</dt><dd class="mono">' + esc(c.cfg || '-') + '</dd><dt>Version</dt><dd>' + esc(c.version || '-') + '</dd><dt>Conversation id</dt><dd class="mono">' + esc(r.sid) + '</dd><dt>Account source</dt><dd>' + esc(c.overridden ? 'override rule' : (c.conf || '-')) + '</dd><dt>Web requests</dt><dd>' + U.fmtInt(c.web_requests || 0) + '</dd></dl></section>' +
      '</div></div>';
    drawer.innerHTML = html;
    $$('[data-close]', drawer).forEach(function (b) { b.addEventListener('click', CCM.closeDrawer); });
    $('#dr-override', drawer).addEventListener('click', function () { overrideModal(c); });
    var main = c.calls.filter(function (x) { return x[9] < 0; }), subs = c.calls.filter(function (x) { return x[9] >= 0; });
    CCM.chart('#dr-ctx', CCM.base({
      useUTC: true, legend: { show: false }, tooltip: { trigger: 'axis', formatter: function (ps) { return '<b>' + esc(U.fmtTime('utc', ps[0].value[0] / 1000)) + '</b>' + ps.map(function (p) { return CCM.tipRow(p.color, p.seriesName, U.fmtN(p.value[1])); }).join(''); } },
      xAxis: { type: 'time', axisLabel: { color: CCM.T.text2, hideOverlap: true }, axisLine: { lineStyle: { color: CCM.T.border } }, splitLine: { show: false } }, yAxis: CCM.axisY({ axisLabel: { formatter: function (v) { return U.fmtN(v, 0); } } }),
      dataZoom: [{ type: 'inside' }],
      series: [{ name: 'Context (main)', type: 'line', showSymbol: false, lineStyle: { width: 1.6 }, areaStyle: { opacity: .12 }, itemStyle: { color: CCM.T.series[0] }, data: main.map(function (x) { return [tx(x[0]), x[1]]; }),
        markLine: { silent: true, symbol: 'none', label: { show: false }, lineStyle: { type: 'dashed', color: CCM.T.warn }, data: c.compactions.map(function (k) { return { xAxis: tx(Date.parse(k.ts) / 1000) }; }) } },
        { name: 'Context (subagent)', type: 'scatter', symbolSize: 3, itemStyle: { color: CCM.T.muted, opacity: .6 }, data: subs.map(function (x) { return [tx(x[0]), x[1]]; }) }]
    }), true);
    var stack = function (name, i, ci) { return { name: name, type: 'line', stack: 't', showSymbol: false, lineStyle: { width: 0 }, areaStyle: { opacity: .85 }, itemStyle: { color: CCM.T.series[ci] }, data: c.calls.map(function (x) { return [tx(x[0]), x[i]]; }) }; };
    CCM.chart('#dr-tok', CCM.base({
      useUTC: true, legend: { data: ['Cache read', 'Cache write', 'Input', 'Output'] },
      tooltip: { trigger: 'axis', formatter: function (ps) { return '<b>' + esc(U.fmtTime('utc', ps[0].value[0] / 1000)) + '</b>' + ps.map(function (p) { return CCM.tipRow(p.color, p.seriesName, U.fmtN(p.value[1])); }).join(''); } },
      xAxis: { type: 'time', axisLabel: { color: CCM.T.text2, hideOverlap: true }, axisLine: { lineStyle: { color: CCM.T.border } }, splitLine: { show: false } }, yAxis: CCM.axisY({ axisLabel: { formatter: function (v) { return U.fmtN(v, 0); } } }),
      dataZoom: [{ type: 'inside' }], series: [stack('Cache read', 4, 0), stack('Cache write', 5, 1), stack('Input', 2, 2), stack('Output', 3, 3)]
    }), true);
    var run = 0;
    CCM.chart('#dr-cum', CCM.base({
      useUTC: true, legend: { show: false }, tooltip: { trigger: 'axis', formatter: function (ps) { return '<b>' + esc(U.fmtTime('utc', ps[0].value[0] / 1000)) + '</b>' + CCM.tipRow(ps[0].color, 'Cumulative', U.fmtUSD(ps[0].value[1])); } },
      xAxis: { type: 'time', axisLabel: { color: CCM.T.text2, hideOverlap: true }, axisLine: { lineStyle: { color: CCM.T.border } }, splitLine: { show: false } }, yAxis: CCM.axisY({ axisLabel: { formatter: function (v) { return U.fmtUSD(v); } } }),
      series: [{ type: 'line', showSymbol: false, step: 'end', itemStyle: { color: CCM.T.series[1] }, areaStyle: { opacity: .1 }, data: c.calls.map(function (x) { run += x[7] * c.stride; return [tx(x[0]), +run.toFixed(3)]; }) }]
    }), true);
    if (tools.length) {
      var tl = tools.slice().reverse();
      CCM.chart('#dr-tools', CCM.base({ legend: { show: false }, grid: { top: 4, left: 4 }, tooltip: { trigger: 'item', formatter: function (p) { return CCM.tipRow(p.color, p.name, U.fmtInt(p.value) + ' calls'); } },
        xAxis: CCM.axisY(), yAxis: CCM.axisX({ data: tl.map(function (t) { return t[0].replace(/^mcp__/, 'mcp:'); }), axisLabel: { interval: 0, width: 120, overflow: 'truncate' } }),
        series: [{ type: 'bar', barMaxWidth: 12, itemStyle: { color: CCM.T.series[2], borderRadius: [0, 3, 3, 0] }, data: tl.map(function (t) { return t[1]; }) }] }), true);
    }
  }

  function overrideModal(c) {
    var r = c.row, accts = S.meta.account_info;
    var box = CCM.openModal('<h2 style="margin-bottom:6px">Change the account for matching conversations</h2><p class="t2" style="margin:0 0 12px">Creates an override rule. It applies to <b>every</b> conversation matching all the conditions below (leave a field empty to ignore it), and all limit estimates are recomputed.</p>' +
      '<form class="form"><label class="fld full">Account<select name="acct">' + accts.map(function (a) { return '<option value="' + esc(a.id) + '"' + (a.id !== r.acct_id ? '' : ' disabled') + '>' + esc(a.name) + (a.id === r.acct_id ? ' (current)' : '') + '</option>'; }).join('') + '</select></label>' +
      '<label class="fld">Device<select name="dev"><option value="">any device</option>' + S.meta.devices.map(function (d) { return '<option value="' + esc(d.id) + '"' + (d.id === r.d ? ' selected' : '') + '>' + esc(d.name) + '</option>'; }).join('') + '</select></label>' +
      '<label class="fld">Project contains<input type="text" name="proj" value="' + esc(r.project || '') + '"></label>' +
      '<label class="fld">From (conversation ends after)<input type="date" name="from" value="' + esc(CCM.fmtDay(r.first)) + '"></label>' +
      '<label class="fld">To (conversation starts before)<input type="date" name="to" value="' + esc(CCM.fmtDay(r.last + 86400)) + '"></label>' +
      '<label class="fld full">Note<input type="text" name="note" maxlength="300" placeholder="why"></label>' +
      '<div class="full"><button class="btn primary" data-save>Create rule</button> <button class="btn" data-cancel>Cancel</button></div></form>');
    $('[data-cancel]', box).addEventListener('click', CCM.closeModal);
    $('[data-save]', box).addEventListener('click', function () {
      var f = $('form', box);
      put('/api/override', { account_uuid: f.acct.value, device_id: f.dev.value, project_like: f.proj.value, from_ts: f.from.value, to_ts: f.to.value, note: f.note.value }, 'Override rule created').then(function () {
        CCM.closeModal(); CCM.closeDrawer(); CCM.reload();
      }, function () { /* toast shown */ });
    });
  }

  /* ------------------------------------------------------------------ Tools & models */
  var TM = { metric: 'usd' };
  V.tools = {
    render: async function (host, seq, alive) {
      var out = await Promise.all([CCM.api('/api/tools'), CCM.cube()]);
      if (!alive()) return;
      var t = out[0], cube = out[1], C = CCM.C;
      if (!t.models.length) { CCM.mount(host, none()); return; }
      var mrows = t.models.map(function (m) {
        var hit = m.inp + m.cr + m.cw ? 100 * m.cr / (m.inp + m.cr + m.cw) : 0, th = m.out ? 100 * m.think / m.out : 0;
        return '<tr><td>' + esc(m.model) + '</td><td class="num">' + U.fmtInt(m.calls) + '</td><td class="num">' + U.fmtUSD(m.usd) + '</td><td class="num">' + hit.toFixed(1) + '%</td><td class="num">' + th.toFixed(0) + '%</td><td class="num">' + U.fmtN(m.out) + '</td></tr>';
      }).join('');
      var html = '<div class="grid">' +
        card('Tool usage', 'top 25 by calls', t.tools.length ? chartDiv('t-tools', 'tall') : none('No tool calls'), 'span-6') +
        card('MCP servers and tools', 'by calls', t.mcp.length ? chartDiv('t-mcp', 'tall') : none('No MCP tool calls in this selection'), 'span-6') +
        card('Model mix over time', '', chartDiv('t-mix', 'short'), 'span-8', '<div class="seg" id="tm-metric"><button data-m="usd" aria-pressed="' + (TM.metric === 'usd') + '">API $</button><button data-m="calls" aria-pressed="' + (TM.metric === 'calls') + '">Calls</button><button data-m="tokens" aria-pressed="' + (TM.metric === 'tokens') + '">Tokens</button></div>') +
        card('Speed and effort', 'share of calls', chartDiv('t-se', 'short'), 'span-4') +
        card('Cache hit ratio over time', 'cache_read / (input + cache_read + cache_write)', chartDiv('t-cache', 'short'), 'span-6') +
        card('Thinking-token share', 'thinking / output tokens', chartDiv('t-think', 'short'), 'span-6') +
        card('Models', 'cache hit and thinking share per model', '<div class="tw"><table><thead><tr><th>Model</th><th class="num">Calls</th><th class="num">API $</th><th class="num">Cache hit</th><th class="num">Thinking</th><th class="num">Output tok.</th></tr></thead><tbody>' + mrows + '</tbody></table></div>', 'span-12') + '</div>';
      CCM.mount(host, html, function (el) {
        if (t.tools.length) {
          var tl = t.tools.slice(0, 25).reverse();
          CCM.chart('#t-tools', CCM.base({ legend: { show: false }, grid: { top: 4, left: 4, right: 30 }, tooltip: { trigger: 'item', formatter: function (p) { return CCM.tipRow(p.color, p.name, U.fmtInt(p.value) + ' calls'); } },
            xAxis: CCM.axisY({ axisLabel: { formatter: function (v) { return U.fmtN(v, 0); } } }), yAxis: CCM.axisX({ data: tl.map(function (x) { return x[0]; }), axisLabel: { interval: 0, width: 110, overflow: 'truncate' } }),
            series: [{ type: 'bar', barMaxWidth: 14, itemStyle: { color: CCM.T.series[0], borderRadius: [0, 4, 4, 0] }, data: tl.map(function (x) { return x[1]; }) }] }));
        }
        if (t.mcp.length) {
          var flat = [];
          t.mcp.forEach(function (s, si) { s.tools.forEach(function (x) { flat.push({ server: s.server, tool: x[0], n: x[1], si: si }); }); });
          flat.sort(function (a, b) { return b.n - a.n; }); flat = flat.slice(0, 25).reverse();
          CCM.chart('#t-mcp', CCM.base({ legend: { show: false }, grid: { top: 4, left: 4, right: 30 },
            tooltip: { trigger: 'item', formatter: function (p) { var d = flat[p.dataIndex]; return '<b>' + esc(d.server) + '</b>' + CCM.tipRow(p.color, d.tool, U.fmtInt(d.n) + ' calls') + '<div class="muted">server total ' + U.fmtInt(t.mcp[d.si].calls) + '</div>'; } },
            xAxis: CCM.axisY(), yAxis: CCM.axisX({ data: flat.map(function (d) { return d.server + ' / ' + d.tool; }), axisLabel: { interval: 0, width: 170, overflow: 'truncate' } }),
            series: [{ type: 'bar', barMaxWidth: 14, data: flat.map(function (d) { return { value: d.n, itemStyle: { color: CCM.color(d.si), borderRadius: [0, 4, 4, 0] } }; }) }] }));
        }
        var col = TM.metric === 'usd' ? C.USD : TM.metric === 'calls' ? C.CALLS : function (r) { return r[C.INP] + r[C.OUT] + r[C.CR] + r[C.CW]; };
        var d = CCM.daily(cube.rows, 'model', col, CCM.range());
        var f = TM.metric === 'usd' ? U.fmtUSD : U.fmtN;
        CCM.chart('#t-mix', CCM.base({ legend: { data: d.groups.map(function (g) { return cube.models[g]; }) }, grid: { bottom: d.days.length > 45 ? 50 : 8 },
          tooltip: { formatter: function (ps) { var tt = 0, rs = ps.map(function (p) { tt += p.value || 0; return p.value ? CCM.tipRow(p.color, p.seriesName, f(p.value)) : ''; }).join(''); return '<b>' + esc(ps[0].axisValue) + '</b>' + CCM.tipRow('', 'Total', f(tt)) + rs; } },
          xAxis: CCM.axisX({ data: d.days, axisLabel: { hideOverlap: true, color: CCM.T.text2 } }), yAxis: CCM.axisY({ axisLabel: { formatter: function (v) { return f(v); } } }),
          dataZoom: d.days.length > 45 ? [{ type: 'slider', height: 16, bottom: 4, borderColor: CCM.T.grid }, { type: 'inside' }] : [],
          series: d.groups.map(function (g) { return { name: cube.models[g], type: 'bar', stack: 'm', barMaxWidth: 24, itemStyle: { color: CCM.color(g), borderColor: CCM.T.surface, borderWidth: 1 }, data: d.vals[g].map(function (v) { return +v.toFixed(2); }) }; }) }));
        // speed / effort: two 100% stacked bars
        var totS = t.speeds.reduce(function (s, x) { return s + x[1]; }, 0) || 1, totE = t.efforts.reduce(function (s, x) { return s + x[1]; }, 0) || 1;
        var seSeries = [], ci = 0;
        t.speeds.forEach(function (x) { seSeries.push({ name: 'speed: ' + x[0], v: [100 * x[1] / totS, 0], n: [x[1], 0], c: ci++ }); });
        t.efforts.forEach(function (x) { seSeries.push({ name: 'effort: ' + x[0], v: [0, 100 * x[1] / totE], n: [0, x[1]], c: ci++ }); });
        CCM.chart('#t-se', CCM.base({ legend: { type: 'scroll', bottom: 0, top: 'auto' }, grid: { top: 10, bottom: 56, left: 4, right: 12 },
          tooltip: { trigger: 'item', formatter: function (p) { return CCM.tipRow(p.color, p.seriesName, U.fmtInt(seSeries[p.seriesIndex].n[p.dataIndex]) + ' calls (' + p.value.toFixed(1) + '%)'); } },
          xAxis: CCM.axisY({ max: 100, axisLabel: { formatter: '{value}%' } }), yAxis: CCM.axisX({ data: ['Speed', 'Effort'], inverse: true }),
          series: seSeries.map(function (s) { return { name: s.name, type: 'bar', stack: 'se', barMaxWidth: 34, itemStyle: { color: CCM.color(s.c), borderColor: CCM.T.surface, borderWidth: 1 }, data: s.v.map(function (x) { return x || null; }) }; }) }));
        // cache + thinking per day
        var num = CCM.daily(cube.rows, null, function (r) { return r[C.CR]; }, CCM.range()), den = CCM.daily(cube.rows, null, function (r) { return r[C.INP] + r[C.CR] + r[C.CW]; }, CCM.range());
        var th = CCM.daily(cube.rows, null, function (r) { return r[C.THINK]; }, CCM.range()), ou = CCM.daily(cube.rows, null, function (r) { return r[C.OUT]; }, CCM.range());
        var line = function (id, days, vals, name, color) {
          CCM.chart('#' + id, CCM.base({ legend: { show: false }, tooltip: { trigger: 'axis', formatter: function (ps) { return '<b>' + esc(ps[0].axisValue) + '</b>' + (ps[0].value == null ? '<div class="muted">no usage</div>' : CCM.tipRow(ps[0].color, name, ps[0].value + '%')); } },
            xAxis: CCM.axisX({ data: days, axisLabel: { hideOverlap: true, color: CCM.T.text2 } }), yAxis: CCM.axisY({ min: 0, max: 100, axisLabel: { formatter: '{value}%' } }),
            series: [{ type: 'line', connectNulls: false, showSymbol: days.length < 50, symbolSize: 5, itemStyle: { color: color }, lineStyle: { width: 2 }, areaStyle: { opacity: .08 }, data: vals }] }));
        };
        var key0 = function (o) { return o.vals[o.groups[0]] || []; };
        line('t-cache', den.days, den.days.map(function (_, i) { var a = key0(den)[i]; return a ? +(100 * key0(num)[i] / a).toFixed(1) : null; }), 'Cache hit ratio', CCM.T.series[2]);
        line('t-think', ou.days, ou.days.map(function (_, i) { var a = key0(ou)[i]; return a ? +(100 * key0(th)[i] / a).toFixed(1) : null; }), 'Thinking share', CCM.T.series[6]);
        el.addEventListener('click', function (e) { var m = e.target.closest('#tm-metric button'); if (m) { TM.metric = m.getAttribute('data-m'); CCM.rerender(); } });
      });
    }
  };

  /* ------------------------------------------------------------------ Context */
  V.context = {
    render: async function (host, seq, alive) {
      var c = await CCM.api('/api/context');
      if (!alive()) return;
      if (!c.n_calls) { CCM.mount(host, none()); return; }
      var labels = c.edges.map(function (e, i) { return i === c.edges.length - 1 ? U.fmtN(e, 0) + '+' : U.fmtN(e, 0) + '-' + U.fmtN(c.edges[i + 1], 0); });
      var kp = ['p50', 'p90', 'p99', 'max'].map(function (k) { return '<div><b>' + U.fmtN(c.pct[k]) + '</b><span>' + (k === 'max' ? 'largest context' : k === 'p50' ? 'median call' : k + ' of calls') + '</span></div>'; }).join('') +
        Object.keys(c.big).map(function (k) { return '<div><b>' + c.big[k] + '</b><span>conversations over ' + U.fmtN(+k, 0) + '</span></div>'; }).join('');
      var topRows = c.top.slice(0, 15).map(function (p) { return '<tr class="click" data-conv="' + esc(p.d) + '|' + esc(p.sid) + '"><td class="title" title="' + esc(p.title) + '">' + esc(p.title || '(untitled)') + '</td><td>' + esc(S.meta.devices[p.dev].name) + '</td><td class="num">' + U.fmtN(p.ctx) + '</td><td class="num">' + (p.nc || '-') + '</td><td class="num">' + U.fmtUSD(p.usd) + '</td></tr>'; }).join('');
      var html = '<div class="mini-kpis">' + kp + '</div><div class="grid">' +
        card('Context size per call', U.fmtInt(c.n_calls) + ' calls', chartDiv('x-hist', 'short'), 'span-6') +
        card('Compactions per conversation', c.compactions + ' compactions' + (c.pre_median ? ', median size before compaction ' + U.fmtN(c.pre_median) : '') + (Object.keys(c.triggers).length ? ' (' + Object.keys(c.triggers).map(function (k) { return esc(k) + ' ' + c.triggers[k]; }).join(', ') + ')' : ''), chartDiv('x-comp', 'short'), 'span-6') +
        card('Peak context per conversation', 'x: start time, y: peak context, size: API $. Click a point to open it', chartDiv('x-scatter', 'tall'), 'span-12') +
        card('Conversations that hit the biggest contexts', '', '<div class="tw"><table><thead><tr><th>Title</th><th>Device</th><th class="num">Peak ctx</th><th class="num">Compactions</th><th class="num">API $</th></tr></thead><tbody>' + topRows + '</tbody></table></div>', 'span-12') + '</div>';
      CCM.mount(host, html, function (el) {
        CCM.chart('#x-hist', CCM.base({ legend: { show: false }, tooltip: { formatter: function (ps) { return '<b>' + esc(labels[ps[0].dataIndex]) + ' tokens</b>' + CCM.tipRow(ps[0].color, 'calls', U.fmtInt(ps[0].value)); } },
          xAxis: CCM.axisX({ data: labels, axisLabel: { interval: 0, rotate: 35, color: CCM.T.text2 } }), yAxis: CCM.axisY({ axisLabel: { formatter: function (v) { return U.fmtN(v, 0); } } }),
          series: [{ type: 'bar', barMaxWidth: 34, itemStyle: { color: CCM.T.series[0], borderRadius: [4, 4, 0, 0] }, data: c.hist }] }));
        var ch = c.comp_hist.map(function (x) { return x; });
        CCM.chart('#x-comp', CCM.base({ legend: { show: false }, tooltip: { formatter: function (ps) { return CCM.tipRow(ps[0].color, ps[0].axisValue + ' compactions', U.fmtInt(ps[0].value) + ' conversations'); } },
          xAxis: CCM.axisX({ data: ch.map(function (x) { return x[0] >= 6 ? '6+' : String(x[0]); }) }), yAxis: CCM.axisY(),
          series: [{ type: 'bar', barMaxWidth: 40, itemStyle: { color: CCM.T.series[1], borderRadius: [4, 4, 0, 0] }, data: ch.map(function (x) { return x[1]; }) }] }));
        var usdMax = Math.max.apply(null, c.points.map(function (p) { return p.usd || 0; }).concat([1]));
        var byAcct = {};
        c.points.forEach(function (p) { (byAcct[p.acct] = byAcct[p.acct] || []).push(p); });
        var sc = CCM.chart('#x-scatter', CCM.base({ useUTC: true, legend: { data: Object.keys(byAcct).map(function (a) { return S.meta.accounts[a].name; }) },
          tooltip: { trigger: 'item', formatter: function (p) { var d = p.data.p; return '<b style="white-space:normal">' + esc((d.title || '(untitled)').slice(0, 70)) + '</b>' + CCM.tipRow('', 'Started', CCM.fmtTime(d.t)) + CCM.tipRow('', 'Peak context', U.fmtN(d.ctx)) + CCM.tipRow('', 'API-equivalent', U.fmtUSD(d.usd)) + CCM.tipRow('', 'Compactions', String(d.nc)); } },
          xAxis: { type: 'time', axisLabel: { color: CCM.T.text2, hideOverlap: true }, axisLine: { lineStyle: { color: CCM.T.border } }, splitLine: { show: false } },
          yAxis: CCM.axisY({ axisLabel: { formatter: function (v) { return U.fmtN(v, 0); } }, name: 'peak ctx', nameTextStyle: { color: CCM.T.muted } }),
          dataZoom: [{ type: 'inside' }],
          series: Object.keys(byAcct).map(function (a) { return { name: S.meta.accounts[a].name, type: 'scatter', itemStyle: { color: CCM.color(+a), opacity: .75, borderColor: CCM.T.surface, borderWidth: 1 },
            symbolSize: function (v) { return 6 + 26 * Math.sqrt((v[2] || 0) / usdMax); },
            data: byAcct[a].filter(function (p) { return p.t; }).map(function (p) { return { value: [tx(p.t), p.ctx, p.usd], p: p }; }) }; }) }));
        sc.on('click', function (ev) { var p = ev.data && ev.data.p; if (p) CCM.openConv(p.d, p.sid); });
        el.addEventListener('click', function (e) { var tr = e.target.closest('tr[data-conv]'); if (tr) { var q = tr.getAttribute('data-conv').split('|'); CCM.openConv(q[0], q.slice(1).join('|')); } });
      });
    }
  };

  /* ------------------------------------------------------------------ Anomalies */
  var hiddenKinds = {};
  CCM.jump = function (a) {
    if (!a) return;
    var d1 = CCM.fmtDay(a.ts), d2 = CCM.fmtDay(Math.max(a.ts, (a.ts_end || a.ts) - 1));
    var devs = a.device_id ? [a.device_id] : [], accts = a.account ? [a.account] : [];
    var base = { from: d1, to: d2, devices: devs, accounts: accts, projects: [], models: [], entrypoints: [], q: '' };
    if (a.sid && a.kind !== 'window_spend') { CCM.setFilters(base, 'convs'); CCM.openConv(a.device_id, a.sid); }
    else if (a.kind === 'window_spend') { base.devices = []; CCM.setFilters(base, 'convs'); }
    else if (a.kind === 'daily_spend') { CCM.setFilters(base, 'overview'); }
    else { CCM.setFilters(base, 'when'); }
  };
  V.anomalies = {
    render: async function (host, seq, alive) {
      var out = await Promise.all([CCM.anomalies({ limit: 500 }), CCM.cube()]);
      if (!alive()) return;
      var an = out[0], cube = out[1];
      var kinds = Object.keys(an.kinds);
      var items = an.items.filter(function (a) { return !hiddenKinds[a.kind]; });
      var pills = kinds.map(function (k) { return '<button class="pill" data-kind="' + k + '" aria-pressed="' + !hiddenKinds[k] + '">' + esc(an.kinds[k]) + ' ' + (an.counts[k] || 0) + '</button>'; }).join('');
      var rows = items.map(function (a, i) {
        return '<tr class="click" data-an="' + i + '"><td class="nowrap"><span class="sevbar"><i style="width:' + a.severity + '%;background:' + (a.severity >= 80 ? 'var(--bad)' : a.severity >= 65 ? 'var(--warn)' : 'var(--accent)') + '"></i></span><b>' + a.severity + '</b></td><td class="nowrap">' + esc(a.label) + '</td><td class="nowrap">' + esc(CCM.fmtTime(a.ts)) + '</td><td>' + esc(a.device || '-') + '</td><td>' + esc(a.account_name || '-') + '</td><td class="reason">' + esc(a.reason) + '</td></tr>';
      }).join('');
      var html = '<div class="grid">' +
        card('Daily API-equivalent $ with anomalies', 'triangles mark days with anomalies', chartDiv('a-daily', 'short'), 'span-12', '<div class="seg" id="grp"><button data-g="account" aria-pressed="' + (S.groupBy === 'account') + '">By account</button><button data-g="device" aria-pressed="' + (S.groupBy === 'device') + '">By device</button></div>') +
        card('Ranked anomalies', an.total + ' found in this selection', '<div class="slider" style="margin-bottom:10px"><label for="sens" class="t2">Sensitivity</label><span class="muted">strict</span><input type="range" id="sens" min="1" max="10" step="0.5" value="' + S.sens + '" style="width:220px"><span class="muted">sensitive</span><b id="sens-v">' + S.sens + '</b>' +
          CCM.info('Robust z-score (median and MAD) against the same device, account, project or recent windows, with minimum-sample and minimum-size guards. Higher sensitivity lowers the thresholds (currently z >= ' + an.params.z + ', context growth >= ' + an.params.ctx_ratio.toFixed(2) + 'x, tool loop >= ' + an.params.run + ' calls in a row). Severity 50 = just over the threshold, 100 = about 47x over.') + '</div>' +
          '<div class="pills" style="margin-bottom:10px">' + pills + '</div>' +
          (items.length ? '<div class="tw maxh"><table><thead><tr><th>Severity</th><th>Kind</th><th>When</th><th>Device</th><th>Account</th><th>Why</th></tr></thead><tbody>' + rows + '</tbody></table></div>' : '<div class="empty">No anomalies at this sensitivity for these filters.</div>'), 'span-12') + '</div>';
      CCM.mount(host, html, function (el) {
        CCM.dailyChart('a-daily', cube, an.items, S.groupBy);
        el.addEventListener('click', function (e) {
          var g = e.target.closest('#grp button'); if (g) { S.groupBy = g.getAttribute('data-g'); CCM.rerender(); return; }
          var k = e.target.closest('[data-kind]'); if (k) { var kk = k.getAttribute('data-kind'); hiddenKinds[kk] = !hiddenKinds[kk]; CCM.rerender(); return; }
          var tr = e.target.closest('tr[data-an]'); if (tr) CCM.jump(items[+tr.getAttribute('data-an')]);
        });
        var sl = $('#sens', el);
        if (sl) {
          sl.addEventListener('input', function () { $('#sens-v', el).textContent = sl.value; });
          sl.addEventListener('change', function () { S.sens = parseFloat(sl.value); try { localStorage.setItem('ccm.sens', S.sens); } catch (x) { /* ignore */ } CCM.rerender(); });
        }
      });
    }
  };

  /* ------------------------------------------------------------------ Settings / admin */
  var ghTimer = null, ghAnswerDeadline = 0;
  function stopGhPoll() { if (ghTimer) { clearInterval(ghTimer); ghTimer = null; } }

  function ghPanelHtml(st) {
    if (!st) return '<div class="empty">Loading...</div>';
    if (!st.configured) {
      return '<div class="callout warn"><b>Not configured.</b> The GitHub transport lets devices upload encrypted snapshots to a private GitHub repo that this PC pulls. Set it up once on this PC:<br><span class="mono">python3 server/ccm_server.py github-init --repo OWNER/NAME</span><br>Importing files by hand (above) works without it.</div>';
    }
    var lr = st.last_request || null, rid = lr && lr.id ? lr.id : 0, targets = lr && lr.devices;
    var devs = st.devices || [];
    var rows = devs.map(function (d) {
      var targeted = !(targets && targets !== '*' && !(Array.isArray(targets) ? targets : [targets]).some(function (t) { return t === d.label || t === d.device_id || (d.device_id && t === d.device_id.slice(0, 8)); }));
      var ans = rid && d.handled_request >= rid;
      var state = !rid ? '<span class="muted">no request yet</span>' : !targeted ? '<span class="muted">not asked</span>' : ans ? '<span class="badge observed">answered</span>' : '<span class="badge assumed">waiting</span>';
      return '<tr><td><b>' + esc(d.label || d.branch) + '</b></td><td>' + esc(d.os || '-') + '</td><td>' + state + '</td><td class="nowrap">' + esc(d.uploaded_at ? ago(d.uploaded_at) : '-') + '</td><td class="nowrap">' + esc(ago(d.pulled_at)) + '</td><td class="num">' + (d.bytes ? U.fmtN(d.bytes) + 'B' : '-') + '</td></tr>';
    }).join('');
    var pull = st.pull || {};
    var err = st.error || pull.error;
    var res = (pull.results || []).filter(function (r) { return r.status !== 'unchanged'; });
    return '<div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:10px"><span>Repo <b class="mono">' + esc(st.repo) + '</b></span>' +
      (lr && lr.requested_at ? '<span class="muted">last request ' + esc(ago(lr.requested_at)) + (lr.auto_hours ? ' &middot; auto upload every ' + esc(lr.auto_hours) + ' h' : '') + '</span>' : '<span class="muted">no request sent yet</span>') + '</div>' +
      (err ? '<div class="callout bad" style="margin-bottom:10px">' + esc(err) + '</div>' : '') +
      '<div style="display:flex;gap:8px;flex-wrap:wrap;align-items:flex-end;margin-bottom:12px">' +
      '<button class="btn primary" id="gh-req" style="padding:9px 18px;font-weight:600">Request data now</button>' +
      '<details id="gh-which"><summary class="btn sm" style="display:inline-block">Choose devices</summary><div class="card" style="position:absolute;z-index:20;margin-top:4px;min-width:220px">' +
      (devs.length ? devs.map(function (d) { return '<label style="display:flex;gap:6px;padding:2px 0"><input type="checkbox" name="gh-dev" value="' + esc(d.label || d.branch) + '"> ' + esc(d.label || d.branch) + '</label>'; }).join('') : '<span class="muted">no devices yet</span>') + '<div class="muted" style="margin-top:4px">none ticked = all devices</div></div></details>' +
      '<button class="btn" id="gh-pull">' + (pull.running ? 'Pulling...' : 'Pull now') + '</button>' +
      '<label class="fld" style="margin-left:12px">Auto upload every (hours, 0 = off)<span style="display:flex;gap:6px"><input type="number" id="gh-auto" min="0" step="0.5" style="width:90px" value="' + esc(lr && lr.auto_hours || 0) + '"><button class="btn" id="gh-auto-set">Set</button></span></label></div>' +
      '<div class="tw"><table><thead><tr><th>Device</th><th>OS</th><th>Latest request</th><th>Last upload</th><th>Pulled here</th><th class="num">Size</th></tr></thead><tbody>' + (rows || '<tr><td colspan="6" class="muted">No device has uploaded yet.</td></tr>') + '</tbody></table></div>' +
      (res.length ? '<div class="results">' + res.map(function (r) { return '<div class="r ' + (r.status === 'ok' ? 'ok' : r.status === 'error' ? 'error' : 'duplicate') + '"><b>' + esc(r.status) + '</b><span class="mono">' + esc(r.branch) + '</span>' + (r.status === 'ok' ? '<span class="muted">' + U.fmtInt(r.sessions) + ' conversations, ' + U.fmtInt(r.calls) + ' calls</span>' : '') + (r.error ? '<span style="color:var(--bad)">' + esc(r.error) + '</span>' : '') + '</div>'; }).join('') + '</div>' : '') +
      (pull.at ? '<div class="muted" style="margin-top:6px">Last pull ' + esc(pull.at) + '. This PC also pulls automatically every ' + esc(st.pull_interval || 60) + ' s.</div>' : '');
  }

  function loadGh(host, fresh) {
    return fetch('/api/github/status' + (fresh ? '?fresh=1' : '')).then(function (r) { return r.json(); }).then(function (st) {
      if (S.tab !== 'settings' || !host.isConnected) { stopGhPoll(); return st; }
      host.innerHTML = ghPanelHtml(st);
      var allAnswered = st.configured && st.last_request && st.last_request.id && (st.devices || []).length && st.devices.every(function (d) { return d.handled_request >= st.last_request.id; });
      if (ghTimer && (allAnswered || Date.now() > ghAnswerDeadline)) stopGhPoll();
      return st;
    }, function (e) { host.innerHTML = '<div class="callout bad">Cannot read GitHub status: ' + esc(e.message) + '</div>'; });
  }
  function ghPull(host) {
    return fetch('/api/github/pull', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }).then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); }).then(function (x) {
      if (!x.ok) CCM.toast(x.j.error || 'pull failed', true);
      else if ((x.j.results || []).some(function (r) { return r.status === 'ok'; })) { CCM.toast('Imported new data from GitHub'); CCM.reload(); }
      return loadGh(host, true);
    });
  }
  function startGhPoll(host) {
    stopGhPoll(); ghAnswerDeadline = Date.now() + 30 * 60 * 1000;
    ghTimer = setInterval(function () {
      if (S.tab !== 'settings' || !host.isConnected) { stopGhPoll(); return; }
      // devices answer on their next poll; pull what has arrived so the table updates live
      fetch('/api/github/pull', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }).then(function (r) { return r.json(); }).then(function (j) {
        if ((j.results || []).some(function (r) { return r.status === 'ok'; })) CCM.reload();
      }).catch(function () { /* offline */ }).then(function () { return loadGh(host, true); });
    }, 15000);
  }

  function table(head, rows) { return '<div class="tw"><table><thead><tr>' + head.map(function (h) { return '<th' + (h[1] ? ' class="' + h[1] + '"' : '') + '>' + h[0] + '</th>'; }).join('') + '</tr></thead><tbody>' + rows + '</tbody></table></div>'; }

  V.settings = {
    render: async function (host, seq, alive) {
      stopGhPoll();
      var a = await fetch('/api/admin').then(function (r) { return r.json(); });
      if (!alive()) return;
      var m = S.meta, drop = a.drop;
      var devRows = a.devices.map(function (d) {
        return '<tr><td class="nowrap"><input type="text" data-dev="' + esc(d.device_id) + '" value="' + esc(d.label || '') + '" maxlength="60" placeholder="' + esc(d.hostname || '') + '" style="width:150px"> <button class="btn sm" data-save-dev="' + esc(d.device_id) + '">Save</button></td><td>' + esc(d.os || '-') + ' <span class="muted">' + esc(d.os_release || '') + '</span></td><td>' + (d.tz_offset_min == null ? '-' : esc(U.tzLabel(d.tz_offset_min))) + '</td><td>' + esc(d.agent_version || '-') + '</td><td class="nowrap">' + esc(ago(d.last_seen)) + '</td><td class="num">' + U.fmtInt(d.sessions) + '</td></tr>';
      }).join('');
      var acctRows = a.accounts.map(function (x) {
        return '<tr><td class="nowrap"><input type="text" data-acct="' + esc(x.account_uuid) + '" value="' + esc(x.nickname || '') + '" maxlength="60" placeholder="' + esc(x.email || '') + '" style="width:150px"> <button class="btn sm" data-save-acct="' + esc(x.account_uuid) + '">Save</button></td><td>' + esc(x.email || '-') + '<div class="muted" style="font-size:12px">' + esc(x.plan || '-') + '</div></td><td class="mono">' + esc(x.cfgs.map(function (c) { return c.device + ' ' + c.cfg; }).join('; ') || '-') + '</td><td class="num">' + U.fmtInt(x.sessions) + '</td></tr>';
      }).join('');
      var ovRows = a.overrides.map(function (o) {
        return '<tr><td>' + esc(CCM.nameOf('acct', o.account_uuid)) + '</td><td>' + esc(o.device_id ? (a.dev_names[o.device_id] || o.device_id) : 'any') + '</td><td>' + esc(o.project_like || 'any') + '</td><td>' + esc(o.from_ts || '-') + '</td><td>' + esc(o.to_ts || '-') + '</td><td>' + esc(o.note || '') + '</td><td><button class="btn sm danger" data-del-ov="' + o.id + '">Delete</button></td></tr>';
      }).join('');
      var priceRows = a.prices.map(function (p) {
        var inp = function (f) { return '<td><input type="number" step="any" min="0" data-f="' + f + '" value="' + esc(p[f]) + '" style="width:76px"></td>'; };
        return '<tr data-price="' + esc(p.pattern) + '"><td class="mono">' + esc(p.pattern) + '</td>' + inp('in_p') + inp('out_p') + inp('cr_p') + inp('cw5_p') + inp('cw1_p') + '<td><input type="text" data-f="note" value="' + esc(p.note || '') + '" style="width:150px"></td><td class="nowrap"><button class="btn sm" data-save-price>Save</button> ' + (p.pattern === '*' ? '' : '<button class="btn sm danger" data-del-price>Delete</button>') + '</td></tr>';
      }).join('');
      var impRows = a.imports.map(function (i) { return '<tr><td class="mono">' + esc(i.filename) + '</td><td>' + esc(a.dev_names[i.device_id] || i.device_id || '-') + '</td><td class="nowrap">' + esc(i.received_at) + '</td><td class="num">' + U.fmtInt(i.records) + '</td></tr>'; }).join('');
      var dropLog = (drop.recent || []).map(function (r) { return '<div class="r ' + (r.status === 'ok' ? 'ok' : r.status === 'duplicate' ? 'duplicate' : 'error') + '"><b>' + esc(r.status) + '</b><span class="mono">' + esc(r.file) + '</span>' + (r.device ? '<span>' + esc(r.device) + '</span>' : '') + (r.error ? '<span style="color:var(--bad)">' + esc(r.error) + '</span>' : '') + '<span class="muted">' + esc(r.at || '') + '</span></div>'; }).join('');
      var html = '<div class="grid">' +
        card('Import', 'drop the export files you received', '<div id="imp"></div>' + (dropLog ? '<h4 style="margin-top:12px">Drop folder activity</h4><div class="results">' + dropLog + '</div>' : '') + '<div class="muted" style="margin-top:8px">Data directory: <span class="mono">' + esc(a.data_dir) + '</span></div>', 'span-12') +
        card('Devices and data requests (GitHub)', 'ask devices to upload fresh data and pull it here', '<div id="gh"><div class="empty">Checking GitHub...</div></div>', 'span-12') +
        card('Devices', 'rename labels; unique labels keep charts readable', table([['Label'], ['OS'], ['Timezone'], ['Agent'], ['Last seen'], ['Conv.', 'num']], devRows), 'span-6') +
        card('Accounts', 'nickname replaces the email everywhere', table([['Nickname'], ['Email / plan'], ['Config dirs'], ['Conv.', 'num']], acctRows), 'span-6') +
        card('Account override rules', 'newest matching rule wins; transcripts do not record the account', (ovRows ? table([['Account'], ['Device'], ['Project contains'], ['From'], ['To'], ['Note'], ['']], ovRows) : '<div class="muted">No rules yet. Use "Change account" in a conversation, or add one here.</div>') +
          '<form class="form" id="ov-form" style="margin-top:12px"><label class="fld">Account<select name="acct">' + m.account_info.map(function (x) { return '<option value="' + esc(x.id) + '">' + esc(x.name) + '</option>'; }).join('') + '</select></label>' +
          '<label class="fld">Device<select name="dev"><option value="">any</option>' + m.devices.map(function (d) { return '<option value="' + esc(d.id) + '">' + esc(d.name) + '</option>'; }).join('') + '</select></label>' +
          '<label class="fld">Project contains<input type="text" name="proj"></label><label class="fld">From<input type="date" name="from"></label><label class="fld">To<input type="date" name="to"></label><label class="fld">Note<input type="text" name="note" maxlength="300"></label>' +
          '<div class="full"><button class="btn primary" id="ov-add">Add rule</button></div></form>', 'span-12') +
        card('Price table', 'USD per million tokens. Longest matching prefix wins, then *family, then *. Changes recompute every cost.', table([['Pattern'], ['Input'], ['Output'], ['Cache read'], ['Cache write 5m'], ['Cache write 1h'], ['Note'], ['']], priceRows) +
          '<form class="form" id="pr-form" style="margin-top:12px"><label class="fld">New pattern<input type="text" name="pattern" placeholder="claude-opus-6"></label>' + ['in_p:Input', 'out_p:Output', 'cr_p:Cache read', 'cw5_p:Cache write 5m', 'cw1_p:Cache write 1h'].map(function (x) { var p = x.split(':'); return '<label class="fld">' + p[1] + '<input type="number" step="any" min="0" name="' + p[0] + '"></label>'; }).join('') + '<div class="full"><button class="btn primary" id="pr-add">Add price</button></div></form>', 'span-12') +
        card('Import log', 'last ' + a.imports.length + ' files', '<div class="tw maxh">' + table([['File'], ['Device'], ['Received'], ['Records', 'num']], impRows) + '</div>', 'span-12') + '</div>';
      CCM.mount(host, html, function (el) {
        CCM.importPanel($('#imp', el), drop);
        var gh = $('#gh', el);
        loadGh(gh, false);
        el.addEventListener('click', function (e) {
          var t = e.target, b;
          if ((b = t.closest('[data-save-dev]'))) { var id = b.getAttribute('data-save-dev'); put('/api/device', { device_id: id, label: $('[data-dev="' + id + '"]', el).value }).then(CCM.reload); return; }
          if ((b = t.closest('[data-save-acct]'))) { var ac = b.getAttribute('data-save-acct'); put('/api/account', { account_uuid: ac, nickname: $('[data-acct="' + ac + '"]', el).value }).then(CCM.reload); return; }
          if ((b = t.closest('[data-del-ov]'))) { put('/api/override/delete', { id: +b.getAttribute('data-del-ov') }, 'Rule deleted').then(CCM.reload); return; }
          if ((b = t.closest('[data-save-price]'))) { var tr = b.closest('tr'), body = { pattern: tr.getAttribute('data-price') }; $$('[data-f]', tr).forEach(function (i) { body[i.getAttribute('data-f')] = i.value; }); put('/api/price', body, 'Price saved').then(CCM.reload); return; }
          if ((b = t.closest('[data-del-price]'))) { put('/api/price/delete', { pattern: b.closest('tr').getAttribute('data-price') }, 'Price deleted').then(CCM.reload); return; }
          if (t.id === 'ov-add') { var f = $('#ov-form', el); put('/api/override', { account_uuid: f.acct.value, device_id: f.dev.value, project_like: f.proj.value, from_ts: f.from.value, to_ts: f.to.value, note: f.note.value }, 'Rule added').then(CCM.reload, function () {}); return; }
          if (t.id === 'pr-add') { var p = $('#pr-form', el); put('/api/price', { pattern: p.pattern.value, in_p: p.in_p.value, out_p: p.out_p.value, cr_p: p.cr_p.value, cw5_p: p.cw5_p.value, cw1_p: p.cw1_p.value }, 'Price added').then(CCM.reload, function () {}); return; }
          // GitHub panel
          if (t.id === 'gh-req') {
            var devs = $$('input[name=gh-dev]:checked', gh).map(function (i) { return i.value; });
            t.disabled = true;
            fetch('/api/github/request', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ devices: devs.length ? devs : null }) }).then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); }).then(function (x) {
              if (!x.ok) CCM.toast(x.j.error || 'request failed', true); else { CCM.toast('Request sent. Devices answer on their next poll (about 10 minutes at most).'); startGhPoll(gh); }
              return loadGh(gh, true);
            });
            return;
          }
          if (t.id === 'gh-pull') { t.disabled = true; t.textContent = 'Pulling...'; ghPull(gh); return; }
          if (t.id === 'gh-auto-set') {
            var h = parseFloat($('#gh-auto', gh).value || '0');
            fetch('/api/github/request', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ auto_hours: isNaN(h) ? 0 : h, trigger: false }) }).then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); }).then(function (x) {
              CCM.toast(x.ok ? (h ? 'Devices will upload every ' + h + ' h' : 'Automatic upload turned off') : (x.j.error || 'failed'), !x.ok); return loadGh(gh, true);
            });
          }
        });
      });
    }
  };
  window.addEventListener('hashchange', function () { if (S.tab !== 'settings') stopGhPoll(); });
  document.addEventListener('click', function (e) { if (e.target.closest('#tabs')) stopGhPoll(); });

  // start the app once every view is registered
  CCM.start();
})();

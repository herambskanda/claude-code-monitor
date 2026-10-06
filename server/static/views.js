/* Views: Overview, Limits, When & where */
(function () {
  'use strict';
  var CCM = window.CCM, U = CCM.U, esc = CCM.esc, S = CCM.S, $ = CCM.$, $$ = CCM.$$;
  var V = CCM.views;
  var DOW = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  var EST = function () { return CCM.info(CCM.EST_TIP); };

  function card(title, sub, body, cls, right) {
    return '<section class="card ' + (cls || 'span-12') + '"><header><h2>' + title + '</h2>' + (sub ? '<span class="sub">' + sub + '</span>' : '') +
      '<span class="grow"></span>' + (right || '') + '</header>' + body + '</section>';
  }
  CCM.card = card;
  function chartDiv(id, size) { return '<div class="chart ' + (size || '') + '" id="' + id + '"></div>'; }
  CCM.chartDiv = chartDiv;
  /** ms value for an ECharts time axis rendered with useUTC, so the axis shows the selected timezone */
  function tx(epoch) { return (epoch + U.tzOffsetMin(CCM.tz(), epoch) * 60) * 1000; }
  CCM.tx = tx;
  function none(msg) { return '<div class="empty">' + esc(msg || 'No data for these filters') + '</div>'; }
  CCM.none = none;
  function sum(a, f) { var t = 0; for (var i = 0; i < a.length; i++) t += f(a[i]); return t; }
  function groupToggle() {
    return '<div class="seg" id="grp"><button data-g="account" aria-pressed="' + (S.groupBy === 'account') + '">By account</button><button data-g="device" aria-pressed="' + (S.groupBy === 'device') + '">By device</button></div>';
  }

  /* ------------------------------------------------------------------ Overview */
  function delta(cur, prev) {
    if (prev == null || prev === 0 || cur == null) return '<span class="delta flat">n/a</span>';
    var p = (cur - prev) / prev * 100, big = Math.abs(p) > 999, cls = Math.abs(p) < 0.5 ? 'flat' : p > 0 ? 'up' : 'down';
    return '<span class="delta ' + cls + '">' + (p > 0.5 ? '&#9650; +' : p < -0.5 ? '&#9660; ' : '&#9644; ') + (big ? (p > 0 ? '>999' : '<-999') : p.toFixed(Math.abs(p) < 10 ? 1 : 0)) + '%</span>';
  }
  function kpiCard(label, val, fmt, prev, sub, tip) {
    return '<div class="kpi"><div class="l">' + esc(label) + (tip ? CCM.info(tip) : '') + '</div><div class="v">' + esc(fmt(val)) + '</div><div class="d">' +
      (prev === undefined ? '' : delta(val, prev) + '<span>vs previous period</span>') + (sub ? '<span>' + esc(sub) + '</span>' : '') + '</div></div>';
  }
  CCM.kpiCard = kpiCard;

  function anomalyDayMap(items) {
    var m = new Map();
    items.forEach(function (a) {
      var d = CCM.fmtDay(a.ts); var x = m.get(d); if (!x) { x = []; m.set(d, x); } x.push(a);
    });
    return m;
  }
  /** Stacked daily $ chart with anomaly triangles. by = 'account' | 'device' */
  CCM.dailyChart = function (elId, cube, anomalies, by) {
    var C = CCM.C, key = by === 'device' ? 'dev' : 'acct', dims = by === 'device' ? cube.devices : cube.accounts;
    var d = CCM.daily(cube.rows, key, C.USD, CCM.range());
    var tot = d.days.map(function (_, i) { return sum(d.groups, function (g) { return d.vals[g][i]; }); });
    var am = anomalyDayMap(anomalies);
    var series = d.groups.map(function (g) {
      return { name: dims[g].name, type: 'bar', stack: 'usd', barMaxWidth: 26, emphasis: { focus: 'series' }, itemStyle: { color: CCM.color(g), borderColor: CCM.T.surface, borderWidth: 1 },
        data: d.vals[g].map(function (v) { return Math.round(v * 100) / 100; }) };
    });
    var pts = [];
    d.days.forEach(function (day, i) { if (am.has(day)) pts.push({ value: [day, tot[i]], anoms: am.get(day) }); });
    series.push({ name: 'Anomalies', type: 'scatter', symbol: 'triangle', symbolSize: 12, symbolOffset: [0, -10], z: 10, itemStyle: { color: CCM.T.bad, borderColor: CCM.T.surface, borderWidth: 1.5 }, data: pts });
    var c = CCM.chart('#' + elId, CCM.base({
      legend: { data: d.groups.map(function (g) { return dims[g].name; }).concat(['Anomalies']) },
      grid: { top: 40, bottom: d.days.length > 45 ? 52 : 12 },
      tooltip: { formatter: function (ps) {
        var day = ps[0].axisValue, rows = '', total = 0;
        ps.forEach(function (p) { if (p.seriesType === 'bar' && p.value) { total += p.value; rows += CCM.tipRow(p.color, p.seriesName, U.fmtUSD(p.value)); } });
        var an = am.get(day), extra = '';
        if (an) {
          extra = '<div style="margin-top:6px;border-top:1px solid ' + CCM.T.grid + ';padding-top:4px"><b style="color:' + esc(CCM.T.bad) + '">' + an.length + ' anomal' + (an.length > 1 ? 'ies' : 'y') + '</b>' +
            an.slice(0, 3).map(function (a) { return '<div style="max-width:380px;white-space:normal">&bull; ' + esc(a.label) + ' (' + a.severity + '): ' + esc(a.reason.slice(0, 110)) + '</div>'; }).join('') + '</div>';
        }
        return '<b>' + esc(day) + '</b>' + (rows ? CCM.tipRow('', 'Total', U.fmtUSD(total)) + rows : '<div class="muted">no usage</div>') + extra +
          '<div style="color:' + esc(CCM.T.muted) + ';margin-top:4px">click a bar to filter to this day</div>';
      } },
      xAxis: CCM.axisX({ data: d.days, axisLabel: { color: CCM.T.text2, hideOverlap: true } }),
      yAxis: CCM.axisY({ axisLabel: { color: CCM.T.text2, formatter: function (v) { return U.fmtUSD(v); } } }),
      dataZoom: d.days.length > 45 ? [{ type: 'slider', height: 18, bottom: 8, borderColor: CCM.T.grid, textStyle: { color: CCM.T.muted } }, { type: 'inside' }] : [],
      series: series
    }));
    c.on('click', function (p) {
      if (p.seriesType === 'scatter') { CCM.setFilters({ from: p.value[0], to: p.value[0] }, 'anomalies'); }
      else if (p.seriesType === 'bar') { CCM.setFilters({ from: p.name, to: p.name }); }
    });
    return c;
  };

  CCM.anomalyList = function (items, n) {
    if (!items.length) return '<div class="empty">No anomalies at this sensitivity. Nothing looks unusual.</div>';
    return '<div class="tw"><table><tbody>' + items.slice(0, n).map(function (a, i) {
      return '<tr class="click" data-an="' + i + '"><td style="width:62px"><span class="sevbar" title="severity ' + a.severity + '"><i style="width:' + a.severity + '%;background:' + (a.severity >= 80 ? 'var(--bad)' : a.severity >= 65 ? 'var(--warn)' : 'var(--accent)') + '"></i></span><b>' + a.severity + '</b></td>' +
        '<td><b>' + esc(a.label) + '</b> <span class="muted">' + esc(CCM.fmtTime(a.ts)) + (a.device ? ' &middot; ' + esc(a.device) : '') + '</span><div class="t2" style="font-size:12px">' + esc(a.reason) + '</div></td></tr>';
    }).join('') + '</tbody></table></div>';
  };

  V.overview = {
    render: async function (host, seq, alive) {
      var out = await Promise.all([CCM.api('/api/overview'), CCM.cube(), CCM.anomalies({ limit: 300 }),
        CCM.api('/api/sessions', { sort: 'usd', dir: 'desc', limit: 8 })]);
      if (!alive()) return;
      var ov = out[0], cube = out[1], an = out[2], top = out[3], cur = ov.cur, prev = ov.prev, C = CCM.C;
      if (!cur.calls) {
        CCM.mount(host, '<div class="empty">No usage matches these filters. Try a wider date range or press Reset.</div>');
        return;
      }
      var cachePct = cur.inp + cur.cr + cur.cw ? 100 * cur.cr / (cur.inp + cur.cr + cur.cw) : 0;
      var rg = CCM.range(), days = rg.from && rg.to ? Math.max(1, Math.round((CCM.dayStart(U.addDays(rg.to, 1)) - CCM.dayStart(rg.from)) / 86400)) : 1;
      var kpis = [
        kpiCard('Conversations', cur.conversations, U.fmtInt, prev && prev.conversations),
        kpiCard('API calls', cur.calls, U.fmtN, prev && prev.calls),
        kpiCard('Tokens', cur.tokens, U.fmtN, prev && prev.tokens, 'cache reads ' + cachePct.toFixed(0) + '%', 'All tokens in and out, including cache reads. Fresh in+out+cache-write: ' + U.fmtN(cur.inp + cur.out + cur.cw)),
        kpiCard('API-equivalent $', cur.usd, U.fmtUSD, prev && prev.usd, days > 1 ? U.fmtUSD(cur.usd / days) + '/day' : '', 'What these calls would cost at API list prices (editable in Settings). Not what you pay on a subscription.'),
        kpiCard('Active hours', cur.active_s / 3600, function (v) { return U.fmtHours(v * 3600); }, prev && prev.active_s / 3600, '', 'Time between consecutive API calls of a conversation when the gap is under 5 minutes.'),
        kpiCard('Devices', cur.devices, U.fmtInt, prev && prev.devices),
        kpiCard('Accounts', cur.accounts, U.fmtInt, prev && prev.accounts)
      ].join('');
      var topRows = top.rows.map(function (r) {
        return '<tr class="click" data-conv="' + esc(r.d) + '|' + esc(r.sid) + '"><td class="title" style="max-width:190px" title="' + esc(r.title + ' (' + r.dev + ')') + '">' + esc(r.title || '(untitled)') + '</td><td class="num">' + U.fmtUSD(r.usd) + '</td><td class="num">' + (r.p5 == null ? '-' : r.p5 + '%') + '</td></tr>';
      }).join('');
      var html = '<div class="kpis">' + kpis + '</div><div class="grid">' +
        card('Daily API-equivalent $', 'click a bar to filter to a day; triangles are anomalies', chartDiv('c-daily', 'tall'), 'span-8', groupToggle()) +
        card('Top anomalies', '<a data-go="anomalies">all ' + an.total + '</a>', '<div id="an-list">' + CCM.anomalyList(an.items, 7) + '</div>', 'span-4') +
        card('Spend by model', '', chartDiv('c-model', 'short'), 'span-4') +
        card('Spend by project', 'top 12', chartDiv('c-proj', 'short'), 'span-4') +
        card('Most expensive conversations', 'est. peak 5h share ' + EST(), '<div class="tw"><table><thead><tr><th>Title</th><th class="num">$</th><th class="num">5h est.</th></tr></thead><tbody>' + topRows + '</tbody></table></div>', 'span-4') +
        '</div>';
      CCM.mount(host, html, function (el) {
        var daily = CCM.dailyChart('c-daily', cube, an.items, S.groupBy);
        el.addEventListener('click', function (e) {
          var g = e.target.closest('#grp button');
          if (g) { S.groupBy = g.getAttribute('data-g'); try { localStorage.setItem('ccm.groupBy', S.groupBy); } catch (x) { /* ignore */ } CCM.rerender(); return; }
          var a = e.target.closest('[data-an]'); if (a) CCM.jump(an.items[+a.getAttribute('data-an')]);
          var c = e.target.closest('[data-conv]'); if (c) { var p = c.getAttribute('data-conv').split('|'); CCM.openConv(p[0], p.slice(1).join('|')); }
          var go = e.target.closest('[data-go]'); if (go) CCM.go(go.getAttribute('data-go'));
        });
        // by model
        var byModel = {};
        cube.rows.forEach(function (r) { byModel[r[C.MODEL]] = (byModel[r[C.MODEL]] || 0) + r[C.USD]; });
        var ml = Object.keys(byModel).map(Number).sort(function (a, b) { return byModel[a] - byModel[b]; });
        CCM.chart('#c-model', CCM.base({
          tooltip: { trigger: 'item', formatter: function (p) { return CCM.tipRow(p.color, p.name, U.fmtUSD(p.value)); } }, grid: { top: 6, left: 4, right: 24 },
          legend: { show: false }, xAxis: CCM.axisY({ axisLabel: { formatter: function (v) { return U.fmtUSD(v); } } }), yAxis: CCM.axisX({ data: ml.map(function (i) { return cube.models[i]; }), axisLabel: { width: 130, overflow: 'truncate' } }),
          series: [{ type: 'bar', barMaxWidth: 16, data: ml.map(function (i) { return { value: +byModel[i].toFixed(2), itemStyle: { color: CCM.color(i), borderRadius: [0, 4, 4, 0] } }; }) }]
        }));
        var pr = ov.projects.slice().reverse();
        CCM.chart('#c-proj', CCM.base({
          tooltip: { trigger: 'item', formatter: function (p) { var r = pr[p.dataIndex]; return '<b>' + esc(r.project) + '</b>' + CCM.tipRow('', 'API-equivalent', U.fmtUSD(r.usd)) + CCM.tipRow('', 'Conversations', U.fmtInt(r.conversations)) + CCM.tipRow('', 'Calls', U.fmtN(r.calls)); } },
          grid: { top: 6, left: 4, right: 24 }, legend: { show: false },
          xAxis: CCM.axisY({ axisLabel: { formatter: function (v) { return U.fmtUSD(v); } } }), yAxis: CCM.axisX({ data: pr.map(function (r) { return r.project; }), axisLabel: { width: 110, overflow: 'truncate' } }),
          series: [{ type: 'bar', barMaxWidth: 16, itemStyle: { color: CCM.T.series[0], borderRadius: [0, 4, 4, 0] }, data: pr.map(function (r) { return r.usd; }) }]
        })).on('click', function (p) { var r = pr[p.dataIndex]; CCM.setFilters({ projects: [r.project] }, 'convs'); });
        void daily;
      });
    }
  };

  /* ------------------------------------------------------------------ Limits */
  function pctColor(p) { return p >= 100 ? CCM.T.bad : p >= 80 ? CCM.T.warn : CCM.T.accent; }
  function winLabel(w) { return CCM.fmtTime(w.start); }

  function winChart(id, wins, acct, title, unitCap) {
    var hasCap = unitCap != null;
    var vals = wins.map(function (w) { return hasCap ? w.pct : w.usd; });
    var c = CCM.chart('#' + id, CCM.base({
      legend: { show: false }, grid: { top: 22, bottom: wins.length > 40 ? 50 : 8 },
      tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' }, formatter: function (ps) {
        var w = wins[ps[0].dataIndex];
        return '<b>' + esc(winLabel(w)) + ' &rarr; ' + esc(CCM.fmtTime(w.end, false)) + '</b>' + CCM.tipRow('', 'API-equivalent', U.fmtUSD(w.usd)) +
          (w.pct != null ? CCM.tipRow('', 'Est. of limit', w.pct.toFixed(0) + '%') : '') + CCM.tipRow('', 'Calls / conversations', U.fmtInt(w.calls) + ' / ' + w.n_sess) +
          w.top.map(function (t) { return '<div style="max-width:340px;white-space:normal" class="muted">&bull; ' + esc((t.title || '(untitled)').slice(0, 56)) + ' ' + esc(U.fmtUSD(t.usd)) + '</div>'; }).join('') +
          '<div class="muted" style="margin-top:3px">click to open its conversations</div>';
      } },
      xAxis: CCM.axisX({ data: wins.map(winLabel), axisLabel: { hideOverlap: true, color: CCM.T.text2 } }),
      yAxis: CCM.axisY({ axisLabel: { formatter: function (v) { return hasCap ? v + '%' : U.fmtUSD(v); } } }),
      dataZoom: wins.length > 40 ? [{ type: 'slider', height: 16, bottom: 6, borderColor: CCM.T.grid }, { type: 'inside' }] : [],
      series: [{ type: 'bar', barMaxWidth: 18, data: vals.map(function (v, i) { return { value: v, itemStyle: { color: hasCap ? pctColor(v || 0) : CCM.T.accent, borderRadius: [3, 3, 0, 0] } }; }),
        markLine: hasCap ? { silent: true, symbol: 'none', label: { color: CCM.T.muted, formatter: '{c}%', position: 'insideEndTop' }, lineStyle: { type: 'dashed', color: CCM.T.muted }, data: [{ yAxis: 80 }, { yAxis: 100, lineStyle: { color: CCM.T.bad } }] } : undefined }]
    }));
    c.on('click', function (p) {
      var w = wins[p.dataIndex];
      CCM.setFilters({ from: CCM.fmtDay(w.start), to: CCM.fmtDay(w.end - 1), accounts: [acct.id] }, 'convs');
    });
    return c;
  }

  function snapChart(id, snaps, realIdx, estIdx, endIdx, label, cap) {
    var real = snaps.filter(function (s) { return s[realIdx] != null; }).map(function (s) { return [tx(s[0]), s[realIdx]]; });
    var est = snaps.filter(function (s) { return s[estIdx] != null; }).map(function (s) { return [tx(s[0]), +s[estIdx]]; });
    if (!real.length) return null;
    return CCM.chart('#' + id, CCM.base({
      useUTC: true, grid: { top: 38 },
      legend: { data: ['Reported by Claude Code', 'Estimated by CCM'] },
      tooltip: { trigger: 'axis', formatter: function (ps) {
        return '<b>' + esc(U.fmtTime('utc', ps[0].value[0] / 1000)) + '</b>' + ps.map(function (p) { return CCM.tipRow(p.color, p.seriesName, p.value[1].toFixed(0) + '%'); }).join('');
      } },
      xAxis: { type: 'time', axisLabel: { color: CCM.T.text2, hideOverlap: true }, axisLine: { lineStyle: { color: CCM.T.border } }, splitLine: { show: false } },
      yAxis: CCM.axisY({ min: 0, max: function (v) { return Math.max(100, Math.ceil(v.max / 10) * 10); }, axisLabel: { formatter: '{value}%' } }),
      dataZoom: [{ type: 'inside' }],
      series: [
        { name: 'Reported by Claude Code', type: 'line', showSymbol: real.length < 120, symbolSize: 5, lineStyle: { width: 2 }, itemStyle: { color: CCM.T.series[0] }, data: real,
          markLine: { silent: true, symbol: 'none', label: { show: false }, lineStyle: { type: 'dashed', color: CCM.T.muted }, data: [{ yAxis: 100 }] } },
        { name: 'Estimated by CCM', type: 'line', showSymbol: false, lineStyle: { width: 2, type: 'dashed' }, itemStyle: { color: CCM.T.series[1] }, data: est }
      ]
    }));
  }

  V.limits = {
    render: async function (host, seq, alive) {
      var lim = await CCM.api('/api/limits', {}, { noFilters: false });
      if (!alive()) return;
      if (!lim.accounts.length) { CCM.mount(host, none('No accounts with usage match these filters.')); return; }
      var html = '<div class="callout" style="margin-bottom:16px">Percent figures on this page are <b>estimates</b> ' + EST() + '. The reported lines are what Claude Code itself showed (rounded to whole %); the estimated lines are our API-equivalent $ in the same window divided by the calibrated capacity.</div>';
      lim.accounts.forEach(function (a, i) {
        var c = a.caps, last = a.snaps[a.snaps.length - 1];
        var uploaded = a.devices.length, seen = a.devices_total_seen;
        html += '<section class="card" style="margin-bottom:16px" id="acc-' + i + '"><header><h2><span class="dot" style="background:' + CCM.color(CCM.acctIndex(a.id)) + '"></span>' + esc(a.name) + '</h2><span class="sub">' + (a.email && a.email !== a.name ? esc(a.email) + ' &middot; ' : '') + esc(a.plan || 'unknown plan') + '</span><span class="grow"></span>' +
          (last ? '<span class="sub">last report ' + esc(CCM.fmtTime(last[0])) + ': 5h <b>' + (last[1] == null ? '-' : last[1] + '%') + '</b>, weekly <b>' + (last[2] == null ? '-' : last[2] + '%') + '</b></span>' : '') + '</header>' +
          '<div class="grid"><div class="span-6"><h3>5-hour window utilization</h3>' + (a.snaps.length ? chartDiv('sn5-' + i, 'short') : none('No utilization reports yet')) + '</div>' +
          '<div class="span-6"><h3>Weekly utilization</h3>' + (a.snaps.length ? chartDiv('sn7-' + i, 'short') : none('No utilization reports yet')) + '</div>' +
          '<div class="span-6"><h3>5-hour windows, est. share of limit ' + EST() + '</h3>' + (a.windows5.length ? chartDiv('w5-' + i, 'short') : none('No windows in range')) + '</div>' +
          '<div class="span-6"><h3>Weekly windows, est. share of limit ' + EST() + '</h3>' + (a.windows7.length ? chartDiv('w7-' + i, 'short') : none('No weekly windows in range (needs a weekly utilization report)')) + '</div>' +
          '<div class="span-7"><h3>Heaviest 5-hour windows</h3>' + heavyTable(a) + '</div>' +
          '<div class="span-5"><h3>Capacity calibration ' + EST() + '</h3>' + calibration(a) + '</div></div></section>';
        void uploaded; void seen;
      });
      CCM.mount(host, html, function (el) {
        lim.accounts.forEach(function (a, i) {
          if (a.snaps.length) { snapChart('sn5-' + i, a.snaps, 1, 3, 6); snapChart('sn7-' + i, a.snaps, 2, 4, 7); }
          if (a.windows5.length) winChart('w5-' + i, a.windows5, a, '5h', a.caps.cap_5h);
          if (a.windows7.length) winChart('w7-' + i, a.windows7, a, 'weekly', a.caps.cap_7d);
        });
        el.addEventListener('click', function (e) {
          var cv = e.target.closest('[data-conv]');
          if (cv) { var p = cv.getAttribute('data-conv').split('|'); CCM.openConv(p[0], p.slice(1).join('|')); return; }
          var w = e.target.closest('[data-win]');
          if (w) { var q = w.getAttribute('data-win').split('|'); CCM.setFilters({ from: CCM.fmtDay(+q[1]), to: CCM.fmtDay(+q[2] - 1), accounts: [q[0]] }, 'convs'); return; }
          var sv = e.target.closest('[data-cap-save]');
          if (sv) saveCap(sv.closest('form'), sv.getAttribute('data-cap-save'));
          var cl = e.target.closest('[data-cap-clear]');
          if (cl) { var f = cl.closest('form'); f.cap5.value = ''; f.cap7.value = ''; saveCap(f, cl.getAttribute('data-cap-clear')); }
        });
      });
    }
  };

  function heavyTable(a) {
    var ws = a.windows5.slice().sort(function (x, y) { return y.usd - x.usd; }).slice(0, 8);
    if (!ws.length) return none('No windows');
    return '<div class="tw"><table><thead><tr><th>Window start</th><th class="num">API-equiv.</th><th class="num">Est. %</th><th>Top conversation</th></tr></thead><tbody>' + ws.map(function (w) {
      var t = w.top[0];
      return '<tr class="click" data-win="' + esc(a.id) + '|' + w.start + '|' + w.end + '"><td class="nowrap">' + esc(CCM.fmtTime(w.start)) + '</td><td class="num">' + U.fmtUSD(w.usd) + '</td><td class="num" style="color:' + (w.pct == null ? '' : pctColor(w.pct)) + '"><b>' + (w.pct == null ? '-' : w.pct.toFixed(0) + '%') + '</b></td>' +
        '<td class="title">' + (t ? '<a data-conv="' + esc(t.d) + '|' + esc(t.sid) + '">' + esc((t.title || '(untitled)').slice(0, 60)) + '</a> <span class="muted">' + U.fmtUSD(t.usd) + '</span>' : '-') + '</td></tr>';
    }).join('') + '</tbody></table></div>';
  }

  function calibration(a) {
    var c = a.caps;
    var rows = [['5-hour', c.cap_5h, c.n_5h, c.manual_5h], ['Weekly', c.cap_7d, c.n_7d, c.manual_7d]].map(function (r) {
      var src = r[3] ? 'manual' : (r[2] ? 'calibrated' : '-');
      return '<tr><td>' + r[0] + '</td><td class="num">' + (r[1] ? U.fmtUSD(r[1]) : 'n/a') + '</td><td class="num">' + (r[2] || 0) + '</td><td><span class="badge ' + (src === 'manual' ? 'override' : src === 'calibrated' ? 'observed' : '') + '">' + src + '</span></td></tr>';
    }).join('');
    return '<div class="tw"><table><thead><tr><th>Window</th><th class="num">Capacity (API-equiv.)</th><th class="num">Samples n</th><th>Source</th></tr></thead><tbody>' + rows + '</tbody></table></div>' +
      '<div class="callout warn" style="margin:10px 0">Calibration is a <b>lower bound</b> until every device that uses this account has uploaded: usage on other devices inflates the real meter, and claude.ai chat usage is invisible here. ' +
      'Devices known for this account: <b>' + esc(a.devices.join(', ') || 'none') + '</b>' + (a.devices_total_seen > a.devices.length ? ' (+ others seen via overrides)' : '') + '.</div>' +
      '<form class="form"><label class="fld">Manual 5h capacity ($)<input type="number" min="0" step="any" name="cap5" value="' + esc(c.manual_5h || '') + '" placeholder="auto"></label>' +
      '<label class="fld">Manual weekly capacity ($)<input type="number" min="0" step="any" name="cap7" value="' + esc(c.manual_7d || '') + '" placeholder="auto"></label>' +
      '<label class="fld full">Note<input type="text" name="note" maxlength="200" value="' + esc(c.note || '') + '"></label>' +
      '<div class="full"><button class="btn primary" data-cap-save="' + esc(a.id) + '">Save manual capacity</button> <button class="btn" data-cap-clear="' + esc(a.id) + '">Back to calibrated</button></div></form>';
  }
  function saveCap(form, acct) {
    CCM.post('/api/capacity', { account_uuid: acct, cap_5h: form.cap5.value, cap_7d: form.cap7.value, note: form.note.value }).then(function () {
      CCM.toast('Capacity saved'); CCM.reload();
    }, function (e) { CCM.toast(e.message, true); });
  }

  /* ------------------------------------------------------------------ When & where */
  var W = { metric: 'calls', dev: '', acct: '' };
  V.when = {
    render: async function (host, seq, alive) {
      var cube = await CCM.cube();
      if (!alive()) return;
      if (!cube.rows.length) { CCM.mount(host, none()); return; }
      var C = CCM.C;
      var devSet = Array.from(new Set(cube.rows.map(function (r) { return r[C.DEV]; }))).sort();
      var acctSet = Array.from(new Set(cube.rows.map(function (r) { return r[C.ACCT]; }))).sort();
      var val = function (r) { return W.metric === 'usd' ? r[C.USD] : W.metric === 'active' ? r[C.ACT] / 3600 : r[C.CALLS]; };
      var fmt = function (v) { return W.metric === 'usd' ? U.fmtUSD(v) : W.metric === 'active' ? U.fmtHours(v * 3600) : U.fmtN(v); };
      var rows = cube.rows.filter(function (r) { return (!W.dev || String(r[C.DEV]) === W.dev) && (!W.acct || String(r[C.ACCT]) === W.acct); });
      var opt = function (list, dims, cur, all) { return '<option value="">' + all + '</option>' + list.map(function (i) { return '<option value="' + i + '"' + (String(i) === cur ? ' selected' : '') + '>' + esc(dims[i].name) + '</option>'; }).join(''); };
      var tzl = (S.tzKey === 'local' ? 'local time' : S.tzKey === 'utc' ? 'UTC' : cube.devices.filter(function (d) { return 'dev:' + d.id === S.tzKey; }).map(function (d) { return d.name + ' time'; })[0] || 'local time');
      var controls = '<div class="seg" id="w-metric"><button data-m="calls" aria-pressed="' + (W.metric === 'calls') + '">Calls</button><button data-m="usd" aria-pressed="' + (W.metric === 'usd') + '">API $</button><button data-m="active" aria-pressed="' + (W.metric === 'active') + '">Active time</button></div> ' +
        '<select id="w-dev" aria-label="Device">' + opt(devSet, cube.devices, W.dev, 'All devices') + '</select> <select id="w-acct" aria-label="Account">' + opt(acctSet, cube.accounts, W.acct, 'All accounts') + '</select>';
      var html = '<div class="grid">' + card('Hour of day x day of week', 'shown in ' + esc(tzl) + ' (change in the header)', chartDiv('c-heat', 'tall'), 'span-8', controls) +
        card('Active hours per device', 'stacked by account', chartDiv('c-act', 'tall'), 'span-4') + '</div>' +
        '<div class="grid">' + devSet.map(function (d) {
          return card('<span class="dot" style="background:' + CCM.color(d) + '"></span>' + esc(cube.devices[d].name), 'day x hour activity (' + esc(W.metric === 'usd' ? 'API $' : W.metric === 'active' ? 'active time' : 'calls') + ')', chartDiv('c-tl-' + d, 'short'), 'span-12');
        }).join('') + '</div>';
      CCM.mount(host, html, function (el) {
        // heatmap
        var grid = {}, max = 0;
        rows.forEach(function (r) {
          var f = CCM.hourFields(r[0]), k = ((f.dow + 6) % 7) + '|' + f.hour;
          grid[k] = (grid[k] || 0) + val(r);
        });
        var cells = [];
        for (var dw = 0; dw < 7; dw++) for (var h = 0; h < 24; h++) { var v = grid[dw + '|' + h] || 0; if (v > max) max = v; cells.push([h, dw, v]); }
        var hours = []; for (var i = 0; i < 24; i++) hours.push(U.pad(i));
        CCM.chart('#c-heat', CCM.base({
          legend: { show: false }, grid: { top: 10, left: 8, right: 16, bottom: 44 },
          tooltip: { trigger: 'item', formatter: function (p) { return '<b>' + DOW[p.value[1]] + ' ' + hours[p.value[0]] + ':00</b>' + CCM.tipRow('', W.metric === 'usd' ? 'API-equivalent' : W.metric === 'active' ? 'Active time' : 'API calls', fmt(p.value[2])); } },
          xAxis: CCM.axisX({ data: hours, splitArea: { show: false } }), yAxis: CCM.axisX({ data: DOW, inverse: true }),
          visualMap: { min: 0, max: Math.max(max, 1e-9), calculable: false, orient: 'horizontal', left: 'center', bottom: 0, itemWidth: 12, itemHeight: 160, text: ['more', 'less'], inRange: { color: CCM.seq() }, textStyle: { color: CCM.T.text2 }, formatter: function (v) { return fmt(v); } },
          series: [{ type: 'heatmap', data: cells, itemStyle: { borderColor: CCM.T.surface, borderWidth: 2, borderRadius: 3 }, emphasis: { itemStyle: { borderColor: CCM.T.text } } }]
        }));
        // active hours per device
        var act = {};
        cube.rows.forEach(function (r) { var k = r[C.DEV] + '|' + r[C.ACCT]; act[k] = (act[k] || 0) + r[C.ACT] / 3600; });
        CCM.chart('#c-act', CCM.base({
          tooltip: { trigger: 'axis', formatter: function (ps) { var t = 0; var rs = ps.map(function (p) { t += p.value || 0; return p.value ? CCM.tipRow(p.color, p.seriesName, U.fmtHours((p.value || 0) * 3600)) : ''; }).join(''); return '<b>' + esc(ps[0].axisValue) + '</b>' + CCM.tipRow('', 'Total', U.fmtHours(t * 3600)) + rs; } },
          grid: { top: 30, left: 8, bottom: 8 }, legend: { data: acctSet.map(function (a) { return cube.accounts[a].name; }) },
          xAxis: CCM.axisX({ data: devSet.map(function (d) { return cube.devices[d].name; }), axisLabel: { interval: 0, width: 80, overflow: 'truncate' } }),
          yAxis: CCM.axisY({ axisLabel: { formatter: function (v) { return v + 'h'; } } }),
          series: acctSet.map(function (a) { return { name: cube.accounts[a].name, type: 'bar', stack: 'a', barMaxWidth: 34, itemStyle: { color: CCM.color(a), borderColor: CCM.T.surface, borderWidth: 1 }, data: devSet.map(function (d) { return +(act[d + '|' + a] || 0).toFixed(2); }) }; })
        }));
        // timelines
        var range = CCM.range();
        devSet.forEach(function (d) {
          var m = {}, mx = 0;
          rows.forEach(function (r) { if (r[C.DEV] !== d) return; var f = CCM.hourFields(r[0]), k = f.day + '|' + f.hour; m[k] = (m[k] || 0) + val(r); });
          var days = CCM.daily(cube.rows.filter(function (r) { return r[C.DEV] === d; }), null, C.CALLS, range).days;
          var data = [];
          days.forEach(function (day, xi) { for (var h = 0; h < 24; h++) { var v = m[day + '|' + h]; if (v) { data.push([xi, h, v]); if (v > mx) mx = v; } } });
          var dayLabels = days;
          CCM.chart('#c-tl-' + d, CCM.base({
            legend: { show: false }, grid: { top: 6, left: 8, right: 12, bottom: days.length > 60 ? 40 : 22 },
            tooltip: { trigger: 'item', formatter: function (p) { return '<b>' + esc(dayLabels[p.value[0]]) + ' ' + U.pad(p.value[1]) + ':00</b>' + CCM.tipRow('', W.metric === 'usd' ? 'API-equivalent' : W.metric === 'active' ? 'Active time' : 'API calls', fmt(p.value[2])); } },
            xAxis: CCM.axisX({ data: dayLabels, axisLabel: { hideOverlap: true, color: CCM.T.text2 }, splitArea: { show: false } }),
            yAxis: CCM.axisX({ type: 'category', data: hours, inverse: true, axisLabel: { interval: 3, color: CCM.T.text2 } }),
            dataZoom: days.length > 60 ? [{ type: 'slider', height: 14, bottom: 4, borderColor: CCM.T.grid }, { type: 'inside' }] : [],
            visualMap: { show: false, min: 0, max: Math.max(mx, 1e-9), inRange: { color: [CCM.seq()[0], CCM.color(d)] } },
            series: [{ type: 'heatmap', data: data, itemStyle: { borderColor: CCM.T.surface, borderWidth: 1 } }]
          }));
        });
        el.addEventListener('click', function (e) {
          var m = e.target.closest('#w-metric button'); if (m) { W.metric = m.getAttribute('data-m'); CCM.rerender(); }
        });
        el.addEventListener('change', function (e) {
          if (e.target.id === 'w-dev') { W.dev = e.target.value; CCM.rerender(); }
          if (e.target.id === 'w-acct') { W.acct = e.target.value; CCM.rerender(); }
        });
      });
    }
  };
})();

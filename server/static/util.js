/* Pure helpers: escaping, number formatting, timezone maths. Works in the browser (window.CCMUtil) and in node. */
(function (root) {
  'use strict';

  /** Escape a value for safe insertion into HTML text or a quoted attribute. Every user-derived string goes through this. */
  function esc(v) {
    return String(v == null ? '' : v)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;').replace(/`/g, '&#96;');
  }

  function fmtN(n, d) {
    if (n == null || isNaN(n)) return '-';
    var a = Math.abs(n);
    if (a >= 1e12) return (n / 1e12).toFixed(d == null ? 1 : d) + 'T';
    if (a >= 1e9) return (n / 1e9).toFixed(d == null ? 1 : d) + 'B';
    if (a >= 1e6) return (n / 1e6).toFixed(d == null ? 1 : d) + 'M';
    if (a >= 1e4) return (n / 1e3).toFixed(d == null ? 1 : d) + 'k';
    return Math.round(n).toLocaleString('en-US');
  }
  function fmtInt(n) { return n == null || isNaN(n) ? '-' : Math.round(n).toLocaleString('en-US'); }
  function fmtUSD(n) {
    if (n == null || isNaN(n)) return '-';
    var a = Math.abs(n);
    if (a >= 1e6) return '$' + (n / 1e6).toFixed(2) + 'M';
    if (a >= 10000) return '$' + (n / 1e3).toFixed(1) + 'k';
    if (a >= 100) return '$' + Math.round(n).toLocaleString('en-US');
    if (a > 0 && a < 0.01) return '<$0.01';
    return '$' + n.toFixed(2);
  }
  function fmtPct(n, d) { return n == null || isNaN(n) ? '-' : n.toFixed(d == null ? 0 : d) + '%'; }
  function fmtDur(s) {
    if (s == null || isNaN(s)) return '-';
    s = Math.round(s);
    if (s < 60) return s + 's';
    var m = Math.round(s / 60);
    if (m < 60) return m + 'm';
    var h = Math.floor(m / 60), mm = m % 60;
    if (h < 100) return h + 'h' + (mm ? ' ' + mm + 'm' : '');
    return fmtInt(h) + 'h';
  }
  function fmtHours(s) { return s == null ? '-' : (s / 3600 < 10 ? (s / 3600).toFixed(1) : fmtInt(s / 3600)) + 'h'; }
  function pad(n) { return (n < 10 ? '0' : '') + n; }

  /* Timezone modes: 'local' | 'utc' | {offset: minutes east of UTC}. Returns calendar fields for an epoch (seconds). */
  function fields(tz, epoch) {
    var d;
    if (tz === 'local') {
      d = new Date(epoch * 1000);
      return { y: d.getFullYear(), m: d.getMonth() + 1, d: d.getDate(), h: d.getHours(), mi: d.getMinutes(), dow: d.getDay() };
    }
    var off = tz === 'utc' ? 0 : tz.offset;
    d = new Date((epoch + off * 60) * 1000);
    return { y: d.getUTCFullYear(), m: d.getUTCMonth() + 1, d: d.getUTCDate(), h: d.getUTCHours(), mi: d.getUTCMinutes(), dow: d.getUTCDay() };
  }
  function ymd(f) { return f.y + '-' + pad(f.m) + '-' + pad(f.d); }
  function dayKey(tz, epoch) { return ymd(fields(tz, epoch)); }
  /** Epoch seconds of 00:00 on the given YYYY-MM-DD in tz. */
  function dayStart(tz, s) {
    var p = s.split('-').map(Number);
    if (tz === 'local') return new Date(p[0], p[1] - 1, p[2]).getTime() / 1000;
    var off = tz === 'utc' ? 0 : tz.offset;
    return Date.UTC(p[0], p[1] - 1, p[2]) / 1000 - off * 60;
  }
  function addDays(s, n) {
    var p = s.split('-').map(Number);
    var d = new Date(Date.UTC(p[0], p[1] - 1, p[2] + n));
    return d.getUTCFullYear() + '-' + pad(d.getUTCMonth() + 1) + '-' + pad(d.getUTCDate());
  }
  function fmtTime(tz, epoch, withDate) {
    if (epoch == null) return '-';
    var f = fields(tz, epoch);
    return (withDate === false ? '' : ymd(f) + ' ') + pad(f.h) + ':' + pad(f.mi);
  }
  function fmtDay(tz, epoch) { return ymd(fields(tz, epoch)); }
  function tzOffsetMin(tz, epoch) {
    if (tz === 'local') return -new Date((epoch || Date.now() / 1000) * 1000).getTimezoneOffset();
    return tz === 'utc' ? 0 : tz.offset;
  }
  function tzLabel(off) {
    var s = off < 0 ? '-' : '+', a = Math.abs(off);
    return 'UTC' + s + pad(Math.floor(a / 60)) + (a % 60 ? ':' + pad(a % 60) : '');
  }

  var api = { esc: esc, fmtN: fmtN, fmtInt: fmtInt, fmtUSD: fmtUSD, fmtPct: fmtPct, fmtDur: fmtDur, fmtHours: fmtHours,
    fields: fields, ymd: ymd, dayKey: dayKey, dayStart: dayStart, addDays: addDays, fmtTime: fmtTime, fmtDay: fmtDay,
    tzOffsetMin: tzOffsetMin, tzLabel: tzLabel, pad: pad };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  root.CCMUtil = api;
})(typeof window !== 'undefined' ? window : globalThis);

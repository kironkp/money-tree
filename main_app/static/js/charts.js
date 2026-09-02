// Lightweight Charts helpers. Times are unix seconds (UTC); we shift them by
// the ET offset so the axis reads market time.
(function () {
  function etOffsetSeconds(unix) {
    var d = new Date(unix * 1000);
    var et = new Date(d.toLocaleString('en-US', { timeZone: 'America/New_York' }));
    var utc = new Date(d.toLocaleString('en-US', { timeZone: 'UTC' }));
    return Math.round((et - utc) / 1000);
  }
  function shift(t) { return t + etOffsetSeconds(t); }
  function colors() {
    var s = getComputedStyle(document.documentElement);
    return {
      text: s.getPropertyValue('--muted').trim() || '#888',
      grid: s.getPropertyValue('--border').trim() || '#333',
      up: s.getPropertyValue('--up').trim() || '#3fb950',
      down: s.getPropertyValue('--down').trim() || '#f85149',
      accent: s.getPropertyValue('--accent').trim() || '#3fb950',
      info: s.getPropertyValue('--info').trim() || '#58a6ff',
    };
  }
  function baseOptions(el) {
    var c = colors();
    return {
      width: el.clientWidth, height: el.clientHeight || 260,
      layout: { background: { type: 'solid', color: 'transparent' }, textColor: c.text, fontFamily: 'IBM Plex Mono, Menlo, monospace', fontSize: 11 },
      grid: { vertLines: { color: c.grid }, horzLines: { color: c.grid } },
      rightPriceScale: { borderColor: c.grid },
      timeScale: { borderColor: c.grid, timeVisible: true, secondsVisible: false },
      crosshair: { mode: 1 },
    };
  }
  function autosize(chart, el) {
    var ro = new ResizeObserver(function () { chart.applyOptions({ width: el.clientWidth, height: el.clientHeight || 260 }); });
    ro.observe(el);
  }
  function lineChart(el, series, opts) {
    if (!window.LightweightCharts || !el) return null;
    var c = colors();
    var chart = LightweightCharts.createChart(el, baseOptions(el));
    var palette = [c.accent, c.info, '#d29922', '#a371f7'];
    series.forEach(function (s, i) {
      var line = chart.addLineSeries({ color: s.color || palette[i % palette.length], lineWidth: 2, priceLineVisible: false, title: s.label || '' });
      line.setData(s.data.map(function (p) { return { time: shift(p[0]), value: p[1] }; }));
    });
    if (opts && opts.baseline != null) {
      var b = chart.addLineSeries({ color: c.text, lineWidth: 1, lineStyle: 2, priceLineVisible: false });
      var first = series[0] && series[0].data[0], last = series[0] && series[0].data[series[0].data.length - 1];
      if (first && last) b.setData([{ time: shift(first[0]), value: opts.baseline }, { time: shift(last[0]), value: opts.baseline }]);
    }
    chart.timeScale().fitContent();
    autosize(chart, el);
    return chart;
  }
  function drawdownChart(el, data) {
    if (!window.LightweightCharts || !el || !data.length) return null;
    var c = colors();
    var chart = LightweightCharts.createChart(el, baseOptions(el));
    var area = chart.addAreaSeries({ lineColor: c.down, topColor: 'rgba(248,81,73,0.05)', bottomColor: 'rgba(248,81,73,0.35)', lineWidth: 1, priceLineVisible: false });
    var peak = -Infinity;
    area.setData(data.map(function (p) { peak = Math.max(peak, p[1]); return { time: shift(p[0]), value: peak > 0 ? (p[1] / peak - 1) * 100 : 0 }; }));
    chart.timeScale().fitContent();
    autosize(chart, el);
    return chart;
  }
  function candleChart(el, bars, markers) {
    if (!window.LightweightCharts || !el || !bars.length) return null;
    var c = colors();
    var chart = LightweightCharts.createChart(el, baseOptions(el));
    var s = chart.addCandlestickSeries({ upColor: c.up, downColor: c.down, borderVisible: false, wickUpColor: c.up, wickDownColor: c.down });
    s.setData(bars.map(function (b) { return { time: shift(b.time), open: b.open, high: b.high, low: b.low, close: b.close }; }));
    var v = chart.addHistogramSeries({ priceFormat: { type: 'volume' }, priceScaleId: '' });
    v.priceScale().applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } });
    v.setData(bars.map(function (b) { return { time: shift(b.time), value: b.volume, color: b.close >= b.open ? 'rgba(63,185,80,.35)' : 'rgba(248,81,73,.35)' }; }));
    if (markers && markers.length) s.setMarkers(markers.map(function (m) { return Object.assign({}, m, { time: shift(m.time) }); }));
    chart.timeScale().fitContent();
    autosize(chart, el);
    return chart;
  }
  function fetchJSON(url) { return fetch(url, { credentials: 'same-origin' }).then(function (r) { return r.json(); }); }
  window.MTCharts = { lineChart: lineChart, drawdownChart: drawdownChart, candleChart: candleChart, fetchJSON: fetchJSON };
})();

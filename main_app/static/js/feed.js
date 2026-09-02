// Decision journal: polls /api/feed/ and appends readable lines; each line
// expands to its technical audit (rules with values/thresholds, ids). A
// failed poll is shown, never swallowed.
(function () {
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }
  var PHASE = { observe: 'observe', evaluate: 'evaluate', decide: 'decide', size: 'size', submit: 'submit', fill: 'fill', manage: 'manage', close: 'close', alert: 'alert', system: 'system' };
  function details(ev) {
    var rows = [];
    (ev.rules || []).forEach(function (r) {
      rows.push('<div class="rule ' + (r.ok ? 'ok' : 'no') + '">' + (r.ok ? '✔' : '✘') + ' <b>' + esc(r.strategy) + ' · ' + esc(r.rule) + '</b> ' + esc(r.text) +
        (r.value != null ? ' <span class="muted">(value ' + esc(r.value) + (r.threshold != null ? ', threshold ' + esc(r.threshold) : '') + ')</span>' : '') + '</div>');
    });
    var d = ev.details || {};
    var kv = Object.keys(d).filter(function (k) { return d[k] != null && typeof d[k] !== 'object'; }).map(function (k) { return esc(k) + '=' + esc(d[k]); });
    if (ev.card) kv.push('card #' + esc(ev.card));
    if (ev.strategy) kv.push('strategy=' + esc(ev.strategy));
    if (kv.length) rows.push('<div class="muted mono">' + kv.join(' · ') + '</div>');
    return rows.length ? rows.join('') : '';
  }
  function mount(el) {
    var url = el.dataset.feedUrl;
    var box = el.querySelector('.feed');
    var stateEl = el.querySelector('[data-feed-state]');
    var lastId = 0, paused = false, levels = '', stuck = true, failures = 0;
    function setState(text, cls) { if (stateEl) { stateEl.textContent = text; stateEl.className = 'feed-state ' + (cls || ''); } }
    function render(ev) {
      var line = document.createElement('div');
      line.className = 'fl fl-' + ev.level + ' ph-' + (PHASE[ev.phase] || 'system');
      var html = '<span class="ft">' + esc(ev.t) + '</span>';
      if (ev.bar) html += '<span class="fb">bar ' + esc(ev.bar) + '</span>';
      if (ev.symbol) html += '<span class="fs">' + esc(ev.symbol) + '</span>';
      if (ev.strategy) html += '<span class="fk">' + esc(ev.strategy) + '</span>';
      html += '<span class="fx">' + esc(ev.text) + '</span>';
      var det = details(ev);
      if (det) { html += '<span class="fmore">▸</span><div class="fdet" hidden>' + det + '</div>'; line.classList.add('has-details'); }
      line.innerHTML = html;
      if (det) line.addEventListener('click', function () { var d = line.querySelector('.fdet'); d.hidden = !d.hidden; line.classList.toggle('open', !d.hidden); });
      box.appendChild(line);
    }
    function trim() { while (box.children.length > 600) box.removeChild(box.firstChild); }
    function tick() {
      if (paused || document.hidden) return;
      var q = url + (url.indexOf('?') >= 0 ? '&' : '?') + 'after=' + lastId + (levels ? '&levels=' + levels : '');
      fetch(q, { credentials: 'same-origin' }).then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); }).then(function (d) {
        failures = 0;
        setState('live', 'ok');
        if (!d.events.length) return;
        d.events.forEach(render);
        lastId = d.last_id || lastId;
        trim();
        if (stuck) box.scrollTop = box.scrollHeight;
      }).catch(function (err) {
        failures += 1;
        setState('DISCONNECTED — ' + (err && err.message ? err.message : 'no response') + ' (' + failures + ')', 'bad');
      });
    }
    function reset() { box.innerHTML = ''; lastId = 0; stuck = true; tick(); }
    box.addEventListener('scroll', function () { stuck = box.scrollHeight - box.scrollTop - box.clientHeight < 40; });
    el.querySelectorAll('[data-levels]').forEach(function (chip) {
      chip.addEventListener('click', function () {
        el.querySelectorAll('[data-levels]').forEach(function (c) { c.classList.remove('active'); });
        chip.classList.add('active');
        levels = chip.dataset.levels;
        reset();
      });
    });
    var pauseBtn = el.querySelector('[data-pause]');
    if (pauseBtn) pauseBtn.addEventListener('click', function () {
      paused = !paused; pauseBtn.textContent = paused ? '▶ resume' : '❚❚ pause';
      setState(paused ? 'paused' : 'live', paused ? 'warn' : 'ok');
      if (!paused) { stuck = true; tick(); }
    });
    var jump = el.querySelector('[data-jump]');
    if (jump) jump.addEventListener('click', function () { stuck = true; box.scrollTop = box.scrollHeight; });
    tick();
    setInterval(tick, parseInt(el.dataset.feedInterval || '2000', 10));
    document.addEventListener('visibilitychange', function () { if (!document.hidden) tick(); });
  }
  document.addEventListener('DOMContentLoaded', function () { document.querySelectorAll('[data-feed-url]').forEach(mount); });
  window.MTFeed = { mount: mount };
})();

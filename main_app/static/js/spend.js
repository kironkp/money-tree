/* The spend panel: period toggle, stepper, axis-locked swipe, arrow keys.
   Swipe is the primary gesture on the phone, so it must coexist with vertical
   page scrolling — the handler locks to an axis on the first meaningful
   movement and never fights a scroll. */
(function () {
  var panel = document.getElementById('spend-panel');
  if (!panel) return;

  var usd = function (v) {
    var n = Number(v) || 0;
    if (n > 0 && n < 0.01) return '<$0.01';
    return '$' + n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  };
  var k = function (n) { return Math.round((Number(n) || 0) / 1000) + 'k'; };
  var esc = function (s) { var d = document.createElement('div'); d.textContent = s == null ? '' : s; return d.innerHTML; };

  var labels = {};
  try { labels = JSON.parse(panel.dataset.labels || '{}'); } catch (e) { labels = {}; }
  var period = 'day', offset = 0, seq = 0, drag = 0;
  var body = panel.querySelector('[data-swipe]');

  function rows(el, buckets, total) {
    if (!buckets.length) { el.innerHTML = '<li class="muted small">Nothing in this period.</li>'; return; }
    el.innerHTML = buckets.map(function (b) {
      var frac = total ? (b.usd / total) * 100 : 0;
      var name = labels[b.key] || b.key;
      return '<li>' +
        '<div class="spend-row-top"><span class="spend-row-name">' + esc(name) +
        (b.estimated ? '<span class="spend-est">est</span>' : '') + '</span>' +
        '<span class="mono spend-row-usd">' + usd(b.usd) + '</span></div>' +
        '<span class="spend-meter"><span style="width:' + Math.max(1.5, Math.min(100, frac)) + '%"></span></span>' +
        '<div class="spend-row-sub mono">' + b.calls + ' call' + (b.calls === 1 ? '' : 's') +
        ' · ' + k(b.input_tokens) + ' in · ' + k(b.output_tokens) + ' out</div></li>';
    }).join('');
  }

  function render(rep) {
    var w = rep.window;
    panel.querySelector('[data-label]').textContent = w.label;
    panel.querySelector('[data-total]').textContent = usd(rep.total_usd);
    var sub = rep.calls + ' call' + (rep.calls === 1 ? '' : 's');
    if (rep.days > 1) sub += ' · ' + usd(rep.per_day_usd) + '/day';
    panel.querySelector('[data-sub]').textContent = sub;
    panel.querySelector('[data-step="newer"]').disabled = !w.has_next;

    var bars = panel.querySelector('[data-bars]');
    if (rep.daily && rep.daily.length > 1) {
      var peak = Math.max.apply(null, rep.daily.map(function (d) { return d.usd; }).concat([0.0001]));
      bars.innerHTML = rep.daily.map(function (d) {
        return '<span title="' + esc(d.day) + ': ' + usd(d.usd) + '" class="' + (d.usd > 0 ? 'on' : '') +
          '" style="height:' + Math.max(3, (d.usd / peak) * 100) + '%"></span>';
      }).join('');
      bars.hidden = false;
    } else { bars.hidden = true; }

    rows(panel.querySelector('[data-rows="kind"]'), rep.by_kind, rep.total_usd);
    rows(panel.querySelector('[data-rows="model"]'), rep.by_model, rep.total_usd);

    var wrap = panel.querySelector('[data-biggest-wrap]');
    if (rep.biggest && rep.biggest.length) {
      panel.querySelector('[data-biggest]').innerHTML = rep.biggest.map(function (b) {
        return '<li><span class="spend-row-name">' + esc(labels[b.kind] || b.kind) +
          '<span class="spend-dim">' + esc(b.model || 'unknown') + '</span></span>' +
          '<span class="mono spend-dim">' + k(b.input_tokens) + ' in</span>' +
          '<span class="mono spend-row-usd">' + usd(b.usd) + '</span></li>';
      }).join('');
      wrap.hidden = false;
    } else { wrap.hidden = true; }

    var unit = period === 'day' ? 'day' : period === 'week' ? 'week' : 'month';
    var foot = 'Swipe or use ← → to move between ' + unit + 's. All time: ' +
      usd(panel.dataset.alltimeUsd) + ' across ' + panel.dataset.alltimeCalls + ' calls.';
    if (rep.any_estimated) {
      foot += ' Rows marked est are upper bounds — a model with no published rate is priced at the ' +
              'most expensive one we know.';
    }
    foot += ' Claude Code and other subscription work is not billed here.';
    panel.querySelector('[data-foot]').textContent = foot;
  }

  function go(nextPeriod, nextOffset) {
    if (nextOffset > 0) return;                       // no spend in the future
    period = nextPeriod; offset = nextOffset;
    panel.querySelectorAll('.spend-periods button').forEach(function (b) {
      b.classList.toggle('active', b.dataset.period === period);
      b.setAttribute('aria-selected', b.dataset.period === period ? 'true' : 'false');
    });
    var mine = ++seq;
    body.classList.add('loading');
    fetch('/api/spend/report/?period=' + period + '&offset=' + offset, { headers: { 'X-Requested-With': 'fetch' } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d || mine !== seq) return;               // a slower earlier request must not win
        if (d.kind_labels) labels = d.kind_labels;
        render(d.report);
      })
      .catch(function () { /* leave the last good report on screen */ })
      .finally(function () { if (mine === seq) body.classList.remove('loading'); });
  }

  var older = function () { go(period, offset - 1); };
  var newer = function () { if (offset < 0) go(period, offset + 1); };

  panel.querySelectorAll('.spend-periods button').forEach(function (b) {
    // Switching unit returns to the current period: "7 days" means this week.
    b.addEventListener('click', function () { go(b.dataset.period, 0); });
  });
  panel.querySelector('[data-step="older"]').addEventListener('click', older);
  panel.querySelector('[data-step="newer"]').addEventListener('click', newer);

  var touch = null;
  body.addEventListener('touchstart', function (e) {
    var t = e.touches[0]; touch = { x: t.clientX, y: t.clientY, axis: '' };
  }, { passive: true });
  body.addEventListener('touchmove', function (e) {
    if (!touch) return;
    var t = e.touches[0], dx = t.clientX - touch.x, dy = t.clientY - touch.y;
    if (!touch.axis) {
      if (Math.abs(dx) < 8 && Math.abs(dy) < 8) return;
      touch.axis = Math.abs(dx) > Math.abs(dy) ? 'x' : 'y';
    }
    if (touch.axis !== 'x') return;                   // release a vertical gesture back to the page
    var limited = (dx < 0 && offset >= 0) ? dx * 0.25 : dx;   // rubber-band at the edge
    drag = Math.max(-120, Math.min(120, limited));
    body.style.transition = 'none';
    body.style.transform = 'translateX(' + drag + 'px)';
  }, { passive: true });
  function endTouch() {
    var axis = touch && touch.axis, dx = drag;
    touch = null; drag = 0;
    body.style.transition = 'transform 200ms cubic-bezier(0.22,0.9,0.32,1)';
    body.style.transform = '';
    if (axis !== 'x' || Math.abs(dx) < 55) return;
    if (dx > 0) older(); else newer();                // swipe right reaches back in time
  }
  body.addEventListener('touchend', endTouch);
  body.addEventListener('touchcancel', endTouch);

  document.addEventListener('keydown', function (e) {
    var el = document.activeElement;
    if (el && ['INPUT', 'TEXTAREA', 'SELECT'].indexOf(el.tagName) >= 0) return;
    if (e.key === 'ArrowLeft') older();
    if (e.key === 'ArrowRight') newer();
  });

  try { render(JSON.parse(panel.dataset.report)); } catch (e) { go('day', 0); }
})();

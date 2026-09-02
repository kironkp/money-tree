// Live feed: polls /api/feed/ and appends new lines. One mount per element.
(function () {
  function esc(s) { return String(s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }
  function mount(el) {
    var url = el.dataset.feedUrl;
    var box = el.querySelector('.feed');
    var lastId = 0, paused = false, levels = '', timer = null, stuck = true;
    function render(ev) {
      var line = document.createElement('div');
      line.className = 'fl fl-' + ev.level;
      line.innerHTML = '<span class="ft">' + esc(ev.t) + '</span>' +
        (ev.symbol ? '<span class="fs">' + esc(ev.symbol) + '</span>' : '') +
        '<span class="fx">' + esc(ev.text) + '</span>';
      box.appendChild(line);
    }
    function trim() { while (box.children.length > 600) box.removeChild(box.firstChild); }
    function tick() {
      if (paused || document.hidden) return;
      var q = url + (url.indexOf('?') >= 0 ? '&' : '?') + 'after=' + lastId + (levels ? '&levels=' + levels : '');
      fetch(q, { credentials: 'same-origin' }).then(function (r) { return r.json(); }).then(function (d) {
        if (!d.events.length) return;
        d.events.forEach(render);
        lastId = d.last_id || lastId;
        trim();
        if (stuck) box.scrollTop = box.scrollHeight;
      }).catch(function () {});
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
      if (!paused) { stuck = true; tick(); }
    });
    var jump = el.querySelector('[data-jump]');
    if (jump) jump.addEventListener('click', function () { stuck = true; box.scrollTop = box.scrollHeight; });
    tick();
    timer = setInterval(tick, parseInt(el.dataset.feedInterval || '2000', 10));
    document.addEventListener('visibilitychange', function () { if (!document.hidden) tick(); });
    return { stop: function () { clearInterval(timer); } };
  }
  document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('[data-feed-url]').forEach(mount);
  });
  window.MTFeed = { mount: mount };
})();

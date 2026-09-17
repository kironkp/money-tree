/* THE FLOOR — MoneyTree's machine room.
 *
 * Nodes are absolutely-positioned divs moved only by `transform`. Cables are
 * <path>s in one world-coordinate SVG. Pan and zoom are one transform on a
 * shared wrapper, so panning costs a single style write and no physics.
 *
 * CABLE SHAPE is a closed form, not a solved catenary. The true catenary of
 * fixed length L between two points needs sinh(u)/u = R solved by Newton, and it
 * is numerically fragile at both ends — the parameter diverges as the cable goes
 * taut and cosh overflows as the endpoints meet. The form below is within 1.3%
 * of a real catenary across the whole useful slack range, costs one sqrt, and
 * degrades correctly to a cable folded in half when both ends coincide.
 *
 * PHYSICS is verlet with projected distance constraints, not springs. A cable is
 * inextensible, so the stiffness a spring would need is exactly the stiffness
 * that makes explicit integration explode. Verlet applies the constraint as a
 * position correction, which is stable at any stiffness and about four times
 * cheaper per point.
 *
 * EVERYTHING SLEEPS. In a settled graph the correct number of cables being
 * simulated is zero and the animation loop should not be running at all.
 */
(function () {
  'use strict';

  var stage = document.getElementById('f-stage');
  if (!stage) return;
  var view = document.getElementById('f-view');
  var svg = document.getElementById('f-cables');
  var layer = document.getElementById('f-nodes');
  var panel = document.getElementById('f-panel');
  var hint = document.getElementById('f-hint');
  var data = JSON.parse(document.getElementById('graph-data').textContent);
  var live = JSON.parse(document.getElementById('graph-state').textContent);
  var reduced = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  var SAG_A = 0.6124;   /* sqrt(3/8): the exact shallow-sag limit */
  var SAG_B = 0.1124;   /* makes a folded cable hang exactly L/2 */
  var MAX_SAG = 210, POINTS = 7, ITER = 3;
  var GRAVITY = 1500, DAMP = 0.985, DT = 1 / 60, MAX_STEPS = 3;
  var SLEEP_E = 0.02, SLEEP_FRAMES = 12;
  var NODE_W = 212, NODE_H = 88, COL_W = 272, ROW_H = 134;
  var SLOP_MOUSE = 4, SLOP_TOUCH = 8;

  var nodes = {}, order = [], cables = [], byNode = {};
  var awake = 0, raf = 0, acc = 0, last = 0;
  var pan = { x: 80, y: 40, k: 0.78 };
  var selected = null, hidden = {};
  var laneCss = {};

  /* --------------------------------------------------------------- shape */
  function sagFor(d, L) {
    if (L <= 0) return 0;
    var k = 1 - d / L;
    if (k <= 0) return 0;
    if (k > 1) k = 1;
    var s = L * Math.sqrt(k) * (SAG_A - SAG_B * k);
    return s < MAX_SAG ? s : MAX_SAG;
  }

  /* A cubic whose controls drop by 4s/3 has its midpoint exactly s below the
   * chord. Two thirds is the classic wrong answer and never hangs enough. */
  function staticPath(ax, ay, bx, by, L) {
    var g = sagFor(Math.hypot(bx - ax, by - ay), L) * 4 / 3;
    return 'M' + ax.toFixed(1) + ',' + ay.toFixed(1) +
      'C' + (ax + (bx - ax) / 3).toFixed(1) + ',' + (ay + (by - ay) / 3 + g).toFixed(1) +
      ' ' + (ax + 2 * (bx - ax) / 3).toFixed(1) + ',' + (ay + 2 * (by - ay) / 3 + g).toFixed(1) +
      ' ' + bx.toFixed(1) + ',' + by.toFixed(1);
  }

  function ropePath(p) {
    var d = 'M' + p[0].toFixed(1) + ',' + p[1].toFixed(1), i;
    for (i = 1; i < POINTS - 1; i++) {
      d += 'Q' + p[i * 2].toFixed(1) + ',' + p[i * 2 + 1].toFixed(1) + ' ' +
        ((p[i * 2] + p[i * 2 + 2]) / 2).toFixed(1) + ',' +
        ((p[i * 2 + 1] + p[i * 2 + 3]) / 2).toFixed(1);
    }
    return d + 'L' + p[(POINTS - 1) * 2].toFixed(1) + ',' + p[(POINTS - 1) * 2 + 1].toFixed(1);
  }

  function outAnchor(n) { return { x: n.x + NODE_W, y: n.y + NODE_H / 2 }; }
  function inAnchor(n) { return { x: n.x, y: n.y + NODE_H / 2 }; }

  /* ---------------------------------------------------------- simulation */
  function seed(c) {
    var a = outAnchor(nodes[c.from]), b = inAnchor(nodes[c.to]);
    c.p = new Float64Array(POINTS * 2);
    c.q = new Float64Array(POINTS * 2);
    var s = sagFor(Math.hypot(b.x - a.x, b.y - a.y), c.L), i, t;
    for (i = 0; i < POINTS; i++) {
      t = i / (POINTS - 1);
      c.p[i * 2] = a.x + (b.x - a.x) * t;
      c.p[i * 2 + 1] = a.y + (b.y - a.y) * t + 4 * s * t * (1 - t);
      c.q[i * 2] = c.p[i * 2];
      c.q[i * 2 + 1] = c.p[i * 2 + 1];
    }
    c.seg = c.L / (POINTS - 1);
    c.sleeping = true;
    c.still = SLEEP_FRAMES + 1;
  }

  function step(c) {
    var p = c.p, q = c.q, i, it, dx, dy, dist, diff, px, py, vx, vy;
    var a = outAnchor(nodes[c.from]), b = inAnchor(nodes[c.to]);
    var e = (POINTS - 1) * 2;

    for (i = 1; i < POINTS - 1; i++) {
      px = p[i * 2]; py = p[i * 2 + 1];
      vx = (px - q[i * 2]) * DAMP; vy = (py - q[i * 2 + 1]) * DAMP;
      q[i * 2] = px; q[i * 2 + 1] = py;
      p[i * 2] = px + vx;
      p[i * 2 + 1] = py + vy + GRAVITY * DT * DT;
    }
    /* endpoints are pinned to the sockets, every frame */
    p[0] = a.x; p[1] = a.y; p[e] = b.x; p[e + 1] = b.y;

    for (it = 0; it < ITER; it++) {
      for (i = 0; i < POINTS - 1; i++) {
        var j = i + 1;
        dx = p[j * 2] - p[i * 2];
        dy = p[j * 2 + 1] - p[i * 2 + 1];
        dist = Math.sqrt(dx * dx + dy * dy) || 1e-6;
        diff = (dist - c.seg) / dist;
        var freeI = i > 0 ? 1 : 0, freeJ = j < POINTS - 1 ? 1 : 0;
        if (!freeI && !freeJ) continue;
        var share = 1 / (freeI + freeJ);
        if (freeI) { p[i * 2] += dx * diff * share; p[i * 2 + 1] += dy * diff * share; }
        if (freeJ) { p[j * 2] -= dx * diff * share; p[j * 2 + 1] -= dy * diff * share; }
      }
      p[0] = a.x; p[1] = a.y; p[e] = b.x; p[e + 1] = b.y;
    }

    var energy = 0;
    for (i = 1; i < POINTS - 1; i++) {
      vx = p[i * 2] - q[i * 2]; vy = p[i * 2 + 1] - q[i * 2 + 1];
      energy += vx * vx + vy * vy;
    }
    return energy;
  }

  function frame(now) {
    raf = 0;
    if (!last) last = now;
    acc += Math.min(0.1, (now - last) / 1000);
    last = now;
    var steps = 0;
    while (acc >= DT && steps < MAX_STEPS) { acc -= DT; steps++; }
    if (!steps) steps = 1;

    for (var i = 0; i < cables.length; i++) {
      var c = cables[i];
      if (c.sleeping) continue;
      var e = 0;
      for (var s = 0; s < steps; s++) e = step(c);
      c.el.setAttribute('d', ropePath(c.p));
      if (e < SLEEP_E) {
        if (++c.still > SLEEP_FRAMES) { c.sleeping = true; awake--; rest(c); }
      } else c.still = 0;
    }
    if (awake > 0) start(); else last = 0;
  }

  function start() { if (!raf) raf = requestAnimationFrame(frame); }
  function wake(c) { if (c.sleeping) { c.sleeping = false; awake++; } c.still = 0; start(); }
  function rest(c) {
    var a = outAnchor(nodes[c.from]), b = inAnchor(nodes[c.to]);
    c.el.setAttribute('d', staticPath(a.x, a.y, b.x, b.y, c.L));
  }
  function wakeFor(id) {
    var list = byNode[id] || [];
    for (var i = 0; i < list.length; i++) wake(list[i]);
  }

  /* -------------------------------------------------------------- render */
  function place(n) { n.el.style.transform = 'translate3d(' + n.x + 'px,' + n.y + 'px,0)'; }
  function applyView() {
    view.style.transform = 'translate3d(' + pan.x + 'px,' + pan.y + 'px,0) scale(' + pan.k + ')';
  }
  function laneColor(lane) {
    if (!laneCss[lane]) {
      laneCss[lane] = getComputedStyle(document.querySelector('.floor'))
        .getPropertyValue('--lane-' + lane).trim() || '#7C8AA6';
    }
    return laneCss[lane];
  }

  /* ---------------------------------------------------------------- icons */
  var ICONS = {
    'bot-stocks': 'M3 17l5-6 4 4 7-9M17 6h4v4',
    'bot-crypto': 'M12 3v18M8 7h6a3 3 0 010 6H8m0 0h7a3 3 0 010 6H8m0-12v12',
    'bot-degen': 'M12 2l3 7h7l-5.5 4.5L18 21l-6-4-6 4 1.5-7.5L2 9h7z',
    'bot-forex': 'M12 3a9 9 0 100 18 9 9 0 000-18zM3 12h18M12 3c3 4 3 14 0 18M12 3c-3 4-3 14 0 18',
    'bot-news': 'M4 5h16v14H4zM7 9h6M7 12h10M7 15h10M16 8h1',
    'bot-research': 'M9 3v6l-5 9a2 2 0 002 3h12a2 2 0 002-3l-5-9V3M8 3h8M9 14h6',
    strategy: 'M4 18l5-5 3 3 8-9M4 4v16h16',
    antenna: 'M12 21V10M12 10a3 3 0 100-6 3 3 0 000 6M6 15a8 8 0 010-8M18 15a8 8 0 000-8',
    archive: 'M3 5h18v4H3zM5 9v11h14V9M9 13h6',
    brain: 'M9 4a3 3 0 00-3 3 3 3 0 00-1 5 3 3 0 002 5 3 3 0 005 1V4zM15 4a3 3 0 013 3 3 3 0 011 5 3 3 0 01-2 5 3 3 0 01-5 1',
    database: 'M4 6c0-1.7 3.6-3 8-3s8 1.3 8 3-3.6 3-8 3-8-1.3-8-3zM4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3',
    calendar: 'M4 6h16v15H4zM4 10h16M8 3v4M16 3v4M9 15h2M14 15h2',
    cpu: 'M7 7h10v10H7zM9 3v4M15 3v4M9 17v4M15 17v4M3 9h4M3 15h4M17 9h4M17 15h4',
    shield: 'M12 3l8 3v6c0 5-3.4 8.4-8 9-4.6-.6-8-4-8-9V6zM9 12l2 2 4-4',
    quote: 'M7 7h4v4c0 3-2 5-4 5V7zM15 7h4v4c0 3-2 5-4 5V7z',
    chip: 'M6 6h12v12H6zM10 10h4v4h-4zM12 2v4M12 18v4M2 12h4M18 12h4',
    bank: 'M3 10l9-6 9 6M5 10v9M9 10v9M15 10v9M19 10v9M3 20h18',
    ledger: 'M5 4h12l2 2v14H5zM8 9h8M8 13h8M8 17h5',
    newspaper: 'M4 4h13v16H4zM17 8h3v10a2 2 0 01-3 2M7 8h7M7 12h7M7 16h4',
    inbox: 'M4 13l2-8h12l2 8v6H4zM4 13h5l1 3h4l1-3h5',
    tag: 'M3 12l9-9h8v8l-9 9zM16 8h.01',
    globe: 'M12 3a9 9 0 100 18 9 9 0 000-18zM3 12h18M12 3c2.5 3 2.5 15 0 18M12 3c-2.5 3-2.5 15 0 18',
    gavel: 'M3 21h9M7 17l6-6M9 3l8 8-3 3-8-8zM14 12l6 6',
    lock: 'M6 11h12v9H6zM9 11V8a3 3 0 016 0v3M12 15v2',
    document: 'M6 3h8l4 4v14H6zM14 3v4h4M9 12h6M9 16h6',
    folder: 'M3 6h6l2 2h10v11H3zM3 10h18',
    ghost: 'M6 21v-9a6 6 0 1112 0v9l-3-2-3 2-3-2zM9 10h.01M15 10h.01',
    seal: 'M12 3l2.5 2 3.5-.5.5 3.5L21 12l-2.5 4 .5 3.5-3.5-.5L12 21l-2.5-2-3.5.5.5-3.5L3 12l2.5-4L5 4.5 8.5 5z',
    balance: 'M12 3v18M5 7h14M7 7l-3 6h6zM17 7l-3 6h6zM8 21h8',
    chart: 'M4 20V6M4 20h16M8 17v-5M12 17V8M16 17v-8',
    flask: 'M9 3v7l-5 8a2 2 0 002 3h12a2 2 0 002-3l-5-8V3M8 3h8M6 16h12',
    ladder: 'M7 3v18M17 3v18M7 8h10M7 13h10M7 18h10',
    lightbulb: 'M9 18h6M10 21h4M12 3a6 6 0 00-4 10.5V16h8v-2.5A6 6 0 0012 3z',
    mail: 'M3 6h18v12H3zM3 7l9 6 9-6',
    receipt: 'M5 3h14v18l-2-2-2 2-2-2-2 2-2-2-2 2zM9 8h6M9 12h6M9 16h4',
    eye: 'M2 12s4-6 10-6 10 6 10 6-4 6-10 6-10-6-10-6zM12 15a3 3 0 100-6 3 3 0 000 6',
    'archive-box': 'M3 5h18v4H3zM5 9v11h14V9M10 13h4',
    ruler: 'M3 9h18v6H3zM7 9v3M11 9v4M15 9v3M19 9v4',
    toll: 'M4 20V9l8-5 8 5v11M9 20v-6h6v6M4 20h16',
    scale: 'M12 4v16M6 8h12M6 8l-3 6h6zM18 8l-3 6h6zM8 20h8',
    flag: 'M5 21V4M5 5h12l-2 4 2 4H5',
    dice: 'M4 4h16v16H4zM8 8h.01M16 8h.01M12 12h.01M8 16h.01M16 16h.01',
    coins: 'M8 8a5 3 0 1010 0 5 3 0 10-10 0M8 8v5c0 1.7 2.2 3 5 3s5-1.3 5-3V8M3 13a5 3 0 1010 0 5 3 0 10-10 0M3 13v5c0 1.7 2.2 3 5 3s5-1.3 5-3v-2',
    hourglass: 'M7 3h10v4l-5 5 5 5v4H7v-4l5-5-5-5zM7 3h10M7 21h10',
    stopwatch: 'M12 22a8 8 0 100-16 8 8 0 000 16zM12 10v4l3 2M9 2h6M19 6l-2-2',
    waves: 'M2 8c3-3 5 3 8 0s5 3 8 0M2 14c3-3 5 3 8 0s5 3 8 0M2 20c3-3 5 3 8 0s5 3 8 0',
    'minus-circle': 'M12 21a9 9 0 100-18 9 9 0 000 18zM8 12h8'
  };
  function icon(name) {
    var d = ICONS[name] || ICONS.cpu;
    return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" ' +
      'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="' + d + '"/></svg>';
  }

  /* ---------------------------------------------------------------- nodes */
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, function (m) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[m];
    });
  }

  function nodeHTML(n, st) {
    var tag = n.kind === 'equation' ? 'formula'
      : n.kind === 'paper' ? 'input'
        : n.kind === 'gate' ? 'gate'
          : n.kind === 'source' ? 'outside'
            : n.kind === 'store' ? 'store'
              : n.kind === 'venue' ? 'venue' : '';
    var h = '<span class="fn-tag">' + esc(tag) + '</span>' +
      '<div class="fn-top"><span class="fn-icon">' + icon(n.icon) + '</span>' +
      '<span class="fn-name">' + esc(n.name) + '</span>' +
      '<span class="fn-led ' + esc(st ? st.status : '') + '"></span></div>' +
      '<div class="fn-one">' + esc(n.one_liner) + '</div>';
    if (st && st.headline) {
      h += '<div class="fn-live"><span class="fn-big">' + esc(st.headline) + '</span>' +
        '<span class="fn-sub">' + esc(st.sub || '') + '</span></div>';
    }
    return h;
  }

  function build() {
    data.nodes.forEach(function (n) {
      n.x = n.col * COL_W; n.y = n.row * ROW_H;
      var el = document.createElement('div');
      el.className = 'fnode k-' + n.kind + (n.note ? ' offline' : '');
      el.dataset.id = n.id;
      el.style.setProperty('--lane', laneColor(n.lane));
      el.innerHTML = nodeHTML(n, live[n.id]);
      el.style.transform = 'translate3d(' + n.x + 'px,' + n.y + 'px,0)';
      layer.appendChild(el);
      n.el = el;
      nodes[n.id] = n;
      order.push(n);
      byNode[n.id] = [];
    });
    data.edges.forEach(function (e) {
      if (!nodes[e.from] || !nodes[e.to]) return;
      var spec = data.payloads[e.payload] || data.payloads.number;
      var a = outAnchor(nodes[e.from]), b = inAnchor(nodes[e.to]);
      var c = {
        from: e.from, to: e.to, payload: e.payload, label: e.label, spec: spec,
        L: Math.max(80, Math.hypot(b.x - a.x, b.y - a.y)) * spec.slack
      };
      var path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      path.setAttribute('class', 'cable');
      path.setAttribute('stroke', 'currentColor');
      path.setAttribute('stroke-width', spec.width);
      path.setAttribute('opacity', '0.46');
      if (spec.dash) path.setAttribute('stroke-dasharray', spec.dash);
      path.style.color = laneColor(nodes[e.from].lane);
      svg.appendChild(path);
      c.el = path;
      seed(c);
      rest(c);
      cables.push(c);
      byNode[e.from].push(c);
      byNode[e.to].push(c);
    });
  }

  /* --------------------------------------------------------------- drag */
  var drag = null, panning = null;

  function toWorld(cx, cy) {
    var r = stage.getBoundingClientRect();
    return { x: (cx - r.left - pan.x) / pan.k, y: (cy - r.top - pan.y) / pan.k };
  }

  layer.addEventListener('pointerdown', function (ev) {
    var el = ev.target.closest('.fnode');
    if (!el) return;
    if (ev.pointerType === 'mouse' && ev.button !== 0) return;
    var n = nodes[el.dataset.id];
    ev.stopPropagation();
    el.setPointerCapture(ev.pointerId);
    var w = toWorld(ev.clientX, ev.clientY);
    drag = {
      id: ev.pointerId, n: n, dx: w.x - n.x, dy: w.y - n.y,
      sx: ev.clientX, sy: ev.clientY, moved: false,
      slop: ev.pointerType === 'touch' ? SLOP_TOUCH : SLOP_MOUSE
    };
    el.style.willChange = 'transform';
    el.style.zIndex = '20';
    if (hint) hint.classList.add('gone');
  });

  layer.addEventListener('pointermove', function (ev) {
    if (!drag || ev.pointerId !== drag.id) return;
    if (!drag.moved &&
      Math.abs(ev.clientX - drag.sx) + Math.abs(ev.clientY - drag.sy) < drag.slop) return;
    drag.moved = true;
    var w = toWorld(ev.clientX, ev.clientY);
    drag.n.x = w.x - drag.dx;
    drag.n.y = w.y - drag.dy;
    place(drag.n);
    wakeFor(drag.n.id);
  });

  function endDrag(ev) {
    if (!drag || ev.pointerId !== drag.id) return;
    var el = drag.n.el;
    el.style.willChange = '';
    el.style.zIndex = '';
    if (!drag.moved) select(drag.n.id);
    else wakeFor(drag.n.id);
    drag = null;
  }
  layer.addEventListener('pointerup', endDrag);
  layer.addEventListener('pointercancel', endDrag);

  /* --------------------------------------------------------- pan & zoom */
  stage.addEventListener('pointerdown', function (ev) {
    if (ev.target.closest('.fnode') || ev.target.closest('.f-bar') ||
      ev.target.closest('.f-panel')) return;
    panning = { id: ev.pointerId, sx: ev.clientX, sy: ev.clientY, px: pan.x, py: pan.y };
    stage.setPointerCapture(ev.pointerId);
    stage.classList.add('dragging');
  });
  stage.addEventListener('pointermove', function (ev) {
    if (!panning || ev.pointerId !== panning.id) return;
    pan.x = panning.px + (ev.clientX - panning.sx);
    pan.y = panning.py + (ev.clientY - panning.sy);
    applyView();
  });
  function endPan(ev) {
    if (!panning || ev.pointerId !== panning.id) return;
    panning = null;
    stage.classList.remove('dragging');
  }
  stage.addEventListener('pointerup', endPan);
  stage.addEventListener('pointercancel', endPan);

  stage.addEventListener('wheel', function (ev) {
    ev.preventDefault();
    var r = stage.getBoundingClientRect();
    var mx = ev.clientX - r.left, my = ev.clientY - r.top;
    var k = Math.min(2.2, Math.max(0.28, pan.k * (ev.deltaY < 0 ? 1.1 : 1 / 1.1)));
    pan.x = mx - (mx - pan.x) * (k / pan.k);
    pan.y = my - (my - pan.y) * (k / pan.k);
    pan.k = k;
    applyView();
  }, { passive: false });

  /* ------------------------------------------------------------ arrange */
  function arrange() {
    /* Layered layout: longest-path layering on the DAG, then order within each
     * layer by the mean position of whatever feeds the node — a cheap barycentre
     * pass, which is enough to stop cables crossing gratuitously. */
    var depth = {}, indeg = {}, i;
    order.forEach(function (n) { depth[n.id] = 0; indeg[n.id] = 0; });
    cables.forEach(function (c) { indeg[c.to]++; });
    var queue = order.filter(function (n) { return !indeg[n.id]; }).map(function (n) { return n.id; });
    var guard = 0;
    while (queue.length && guard++ < 4000) {
      var id = queue.shift();
      (byNode[id] || []).forEach(function (c) {
        if (c.from !== id) return;
        if (depth[c.to] < depth[id] + 1) depth[c.to] = depth[id] + 1;
        if (--indeg[c.to] === 0) queue.push(c.to);
      });
    }
    var layers = {};
    order.forEach(function (n) { (layers[depth[n.id]] = layers[depth[n.id]] || []).push(n); });
    var targets = {};
    Object.keys(layers).forEach(function (d) {
      var rows = layers[d];
      rows.sort(function (a, b) {
        return (bary(a) - bary(b)) || a.name.localeCompare(b.name);
      });
      rows.forEach(function (n, idx) {
        targets[n.id] = { x: d * COL_W, y: idx * ROW_H - (rows.length - 1) * ROW_H / 2 + 460 };
      });
    });
    animateTo(targets);
  }
  function bary(n) {
    var list = byNode[n.id] || [], sum = 0, k = 0;
    for (var i = 0; i < list.length; i++) {
      if (list[i].to === n.id) { sum += nodes[list[i].from].y; k++; }
    }
    return k ? sum / k : n.y;
  }

  function animateTo(targets) {
    var from = {}, t0 = performance.now(), MS = reduced ? 0 : 720;
    order.forEach(function (n) { from[n.id] = { x: n.x, y: n.y }; });
    cables.forEach(wake);
    function tick(now) {
      var t = MS ? Math.min(1, (now - t0) / MS) : 1;
      var e = 1 - Math.pow(1 - t, 3);
      order.forEach(function (n) {
        var f = from[n.id], g = targets[n.id];
        if (!g) return;
        n.x = f.x + (g.x - f.x) * e;
        n.y = f.y + (g.y - f.y) * e;
        place(n);
      });
      if (t < 1) requestAnimationFrame(tick);
      else {
        /* Slack is recomputed from the RESTING distance, once, here. During a
         * drag only the chord length changes, never L — which is what makes a
         * cable go taut when you pull its node away and pile up slack when you
         * push it closer, with no extra state. */
        cables.forEach(function (c) {
          var a = outAnchor(nodes[c.from]), b = inAnchor(nodes[c.to]);
          c.L = Math.max(80, Math.hypot(b.x - a.x, b.y - a.y)) * c.spec.slack;
          c.seg = c.L / (POINTS - 1);
          wake(c);
        });
      }
      start();
    }
    requestAnimationFrame(tick);
  }

  /* ------------------------------------------------------------- fit */
  function fit(pad) {
    pad = pad || 90;
    var minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    order.forEach(function (n) {
      if (hidden[n.lane]) return;
      if (n.x < minX) minX = n.x;
      if (n.y < minY) minY = n.y;
      if (n.x + NODE_W > maxX) maxX = n.x + NODE_W;
      if (n.y + NODE_H > maxY) maxY = n.y + NODE_H;
    });
    if (!isFinite(minX)) return;
    var r = stage.getBoundingClientRect();
    var k = Math.min((r.width - pad * 2) / (maxX - minX),
                     (r.height - pad * 2) / (maxY - minY), 1.1);
    pan.k = Math.max(0.24, k);
    pan.x = (r.width - (maxX - minX) * pan.k) / 2 - minX * pan.k;
    pan.y = (r.height - (maxY - minY) * pan.k) / 2 - minY * pan.k;
    applyView();
  }

  /* ---------------------------------------------------------- inspector */
  function select(id) {
    selected = id;
    order.forEach(function (n) { n.el.classList.toggle('sel', n.id === id); });
    var touching = {};
    (byNode[id] || []).forEach(function (c) { touching[c.from] = touching[c.to] = 1; });
    order.forEach(function (n) {
      n.el.classList.toggle('dim', !!id && n.id !== id && !touching[n.id]);
    });
    cables.forEach(function (c) {
      var on = c.from === id || c.to === id;
      c.el.classList.toggle('dim', !!id && !on);
      c.el.classList.toggle('lit', on);
      c.el.setAttribute('opacity', on ? '0.95' : '0.46');
    });
    if (!id) { panel.classList.remove('open'); return; }
    panel.innerHTML = panelHTML(nodes[id]);
    panel.classList.add('open');
    panel.querySelector('.f-close').addEventListener('click', function () { select(null); });
    Array.prototype.forEach.call(panel.querySelectorAll('.f-wire'), function (w) {
      w.addEventListener('click', function () { select(w.dataset.go); });
    });
  }

  function panelHTML(n) {
    var st = live[n.id] || {};
    var ins = [], outs = [];
    (byNode[n.id] || []).forEach(function (c) {
      var other = c.from === n.id ? c.to : c.from;
      var row = '<div class="f-wire" data-go="' + esc(other) + '" style="--c:' +
        laneColor(nodes[other].lane) + '"><span class="f-wire-dot"></span>' +
        '<span class="f-wire-name">' + esc(nodes[other].name) + '</span>' +
        '<span class="f-wire-what">' + esc(c.label || c.spec.label) + '</span></div>';
      (c.to === n.id ? ins : outs).push(row);
    });
    var h = '<button class="f-close" aria-label="Close">&times;</button>' +
      '<div class="f-panel-in" style="--lane:' + laneColor(n.lane) + '">' +
      '<div class="f-panel-top"><span class="f-panel-icon">' + icon(n.icon) + '</span>' +
      '<div><h2>' + esc(n.name) + '</h2>' +
      '<p class="f-kicker">' + esc(data.lanes[n.lane].name) + ' · ' + esc(n.kind) + '</p></div></div>' +
      '<p class="f-lede">' + esc(n.one_liner) + '</p>' +
      '<p class="f-body">' + esc(n.what) + '</p>';

    if (n.eq) {
      h += '<div class="eq-block"><span class="eq">' + n.eq.html + '</span></div>';
      if (n.eq.subs) h += '<p class="eq-subs">' + esc(n.eq.subs) + '</p>';
      if (n.eq.note) h += '<p class="f-note">' + esc(n.eq.note) + '</p>';
    }
    if (n.note) h += '<p class="f-note">' + esc(n.note) + '</p>';
    if (st.detail) h += '<p class="f-note">' + esc(st.detail) + '</p>';

    var facts = '';
    if (st.headline) facts += '<dt>Now</dt><dd>' + esc(st.headline) + ' ' + esc(st.sub || '') + '</dd>';
    if (n.cadence) facts += '<dt>Runs</dt><dd>' + esc(n.cadence) + '</dd>';
    if (n.model) facts += '<dt>Model</dt><dd>' + esc(n.model) + '</dd>';
    if (n.file) facts += '<dt>Code</dt><dd>' + esc(n.file) + '</dd>';
    if (facts) h += '<dl class="f-facts">' + facts + '</dl>';

    h += '<div class="f-wires">';
    if (ins.length) h += '<h3>Fed by</h3>' + ins.join('');
    if (outs.length) h += '<h3>Feeds</h3>' + outs.join('');
    return h + '</div></div>';
  }

  stage.addEventListener('click', function (ev) {
    if (!ev.target.closest('.fnode') && !ev.target.closest('.f-panel') &&
      !ev.target.closest('.f-bar')) select(null);
  });
  document.addEventListener('keydown', function (ev) {
    if (ev.key === 'Escape') select(null);
  });

  /* ------------------------------------------------------------- filters */
  function filterLane(lane, btn) {
    hidden[lane] = !hidden[lane];
    btn.classList.toggle('off', hidden[lane]);
    order.forEach(function (n) {
      var off = hidden[n.lane];
      n.el.style.display = off ? 'none' : '';
    });
    cables.forEach(function (c) {
      var off = hidden[nodes[c.from].lane] || hidden[nodes[c.to].lane];
      c.el.style.display = off ? 'none' : '';
    });
  }

  /* --------------------------------------------------------------- boot */
  build();
  arrange();
  setTimeout(function () { fit(); }, reduced ? 0 : 760);

  document.getElementById('f-arrange').addEventListener('click', function () {
    arrange();
    setTimeout(fit, reduced ? 0 : 760);
  });
  document.getElementById('f-reset').addEventListener('click', function () {
    select(null);
    fit();
  });
  document.getElementById('f-shake').addEventListener('click', function () {
    order.forEach(function (n) {
      n.y += (Math.random() - 0.5) * 90;
      n.x += (Math.random() - 0.5) * 40;
      place(n);
    });
    cables.forEach(wake);
  });
  Array.prototype.forEach.call(document.querySelectorAll('.f-lane'), function (b) {
    b.addEventListener('click', function () { filterLane(b.dataset.lane, b); fit(); });
  });

  var fitTimer;
  window.addEventListener('resize', function () {
    clearTimeout(fitTimer);
    fitTimer = setTimeout(fit, 220);
  });

  /* live values, merged into existing DOM — the graph is never rebuilt */
  setInterval(function () {
    fetch('/api/map/state/', { headers: { 'X-Requested-With': 'fetch' } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (j) {
        if (!j) return;
        live = j.state;
        order.forEach(function (n) {
          var st = live[n.id];
          if (!st) return;
          var led = n.el.querySelector('.fn-led');
          if (led) led.className = 'fn-led ' + (st.status || '');
          var big = n.el.querySelector('.fn-big'), sub = n.el.querySelector('.fn-sub');
          if (big && st.headline) big.textContent = st.headline;
          if (sub) sub.textContent = st.sub || '';
        });
      }).catch(function () { });
  }, 20000);
})();

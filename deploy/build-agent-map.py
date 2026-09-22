#!/usr/bin/env python
"""Render the agent map to a standalone HTML file.

Reads graph.json / state.json next to the output directory, or regenerates them
from graph_model.graph() and graph_state.state() when run through manage.py.
The drawing is generated from the model, so a node added in graph_model.py
appears here without anyone redrawing anything.

    pipenv run python deploy/build-agent-map.py
"""
import json, io, os, sys, datetime
SP = os.environ.get('MT_MAP_OUT', os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'run'))
os.makedirs(SP, exist_ok=True)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'moneytree.settings')
import django; django.setup()
from main_app.services.graph_model import graph
from main_app.services.graph_state import state
g, st = graph(), state()
stamp = datetime.datetime.now().strftime('%-d %b %Y, %-I:%M %p')
blob = json.dumps({'graph': g, 'state': st, 'stamp': stamp}).replace('</', r'<\/')

HTML = r'''<title>MoneyTree Patchbay</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wght@500;600;700&family=JetBrains+Mono:wght@400;500&family=Source+Serif+4:opsz,wght@8..60,400;8..60,600&display=swap">
<style>
:root{
  --ground:#e9edf1; --panel:#fff; --panel-2:#f4f7f9; --ink:#0f1a21; --muted:#5d6e7b;
  --hair:#c6d1d9; --hair-soft:#dbe3e9; --accent:#0f6e80; --accent-soft:#d8ecef;
  --shadow:0 1px 2px rgba(16,32,44,.07),0 8px 24px -12px rgba(16,32,44,.22);
  --shadow-lift:0 2px 6px rgba(16,32,44,.12),0 20px 42px -16px rgba(16,32,44,.34);
  --stocks:#1a6f80; --crypto:#8d6413; --degen:#a4382e; --forex:#4a6626; --all:#4f5c6a; --infra:#63508a;
  --ok:#2f7d4f; --warn:#9a6b18; --idle:#7b8794;
  --sans:'Archivo',system-ui,-apple-system,'Segoe UI',sans-serif;
  --serif:'Source Serif 4',Georgia,'Times New Roman',serif;
  --mono:'JetBrains Mono',ui-monospace,'SF Mono',Menlo,monospace;
  --rail:56px;
}
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"]){
    --ground:#0c1116; --panel:#141b22; --panel-2:#1a232b; --ink:#dde6ed; --muted:#8397a6;
    --hair:#26323c; --hair-soft:#1d262e; --accent:#3aa9bd; --accent-soft:#12333a;
    --shadow:0 1px 2px rgba(0,0,0,.5),0 8px 24px -12px rgba(0,0,0,.7);
    --shadow-lift:0 2px 6px rgba(0,0,0,.55),0 20px 42px -16px rgba(0,0,0,.8);
    --stocks:#46b3c8; --crypto:#d9a441; --degen:#e07568; --forex:#9dc165; --all:#97a6b5; --infra:#a894d4;
    --ok:#5cc184; --warn:#d9a441; --idle:#8b98a6;
  }
}
:root[data-theme="dark"]{
  --ground:#0c1116; --panel:#141b22; --panel-2:#1a232b; --ink:#dde6ed; --muted:#8397a6;
  --hair:#26323c; --hair-soft:#1d262e; --accent:#3aa9bd; --accent-soft:#12333a;
  --shadow:0 1px 2px rgba(0,0,0,.5),0 8px 24px -12px rgba(0,0,0,.7);
  --shadow-lift:0 2px 6px rgba(0,0,0,.55),0 20px 42px -16px rgba(0,0,0,.8);
  --stocks:#46b3c8; --crypto:#d9a441; --degen:#e07568; --forex:#9dc165; --all:#97a6b5; --infra:#a894d4;
  --ok:#5cc184; --warn:#d9a441; --idle:#8b98a6;
}
*{box-sizing:border-box}
body{margin:0;background:var(--ground);color:var(--ink);font-family:var(--sans);
     -webkit-font-smoothing:antialiased;overflow:hidden}
button{font:inherit;color:inherit;background:none;border:0;cursor:pointer}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:3px}

/* ---------------------------------------------------------------- top rail */
.rail{position:fixed;inset:0 0 auto 0;height:var(--rail);z-index:40;display:flex;align-items:center;
      gap:18px;padding:0 16px;background:var(--panel);border-bottom:1px solid var(--hair);
      box-shadow:0 1px 0 rgba(0,0,0,.02)}
.brand{display:flex;align-items:baseline;gap:10px;flex:none}
.brand h1{margin:0;font-size:15px;font-weight:700;letter-spacing:-.01em;white-space:nowrap}
.brand .count{font-family:var(--mono);font-size:10.5px;color:var(--muted);white-space:nowrap}
.rail .sep{width:1px;height:26px;background:var(--hair);flex:none}
.chips{display:flex;gap:5px;overflow-x:auto;scrollbar-width:none;flex:1;min-width:0;padding:2px 0}
.chips::-webkit-scrollbar{display:none}
.chip{display:inline-flex;align-items:center;gap:6px;padding:4px 9px;border:1px solid var(--hair);
      border-radius:2px;font-size:11.5px;font-weight:600;white-space:nowrap;color:var(--muted);
      background:var(--panel);transition:background .12s,color .12s,border-color .12s}
.chip .dot{width:7px;height:7px;border-radius:50%;background:currentColor;flex:none}
.chip[aria-pressed="true"]{color:var(--panel);background:var(--lane);border-color:var(--lane)}
.chip:not([aria-pressed="true"]):hover{border-color:var(--lane);color:var(--lane)}
.tools{display:flex;gap:4px;flex:none;align-items:center}
.tool{padding:5px 10px;border:1px solid var(--hair);border-radius:2px;font-size:11.5px;font-weight:600;
      color:var(--muted);background:var(--panel)}
.tool:hover{border-color:var(--accent);color:var(--accent)}
.tool.on{background:var(--accent-soft);border-color:var(--accent);color:var(--accent)}

/* ------------------------------------------------------------------- stage */
.stage{position:fixed;inset:var(--rail) 0 0 0;overflow:auto;cursor:grab;
       background-image:radial-gradient(circle at 1px 1px,var(--hair-soft) 1px,transparent 0);
       background-size:26px 26px}
.stage.panning{cursor:grabbing}
.world{position:relative;transform-origin:0 0}
svg.cables{position:absolute;inset:0;overflow:visible;pointer-events:none}
svg.cables path{fill:none;stroke-linecap:round;transition:opacity .16s,stroke-width .16s}
svg.cables path.hot{stroke-width:3.4;opacity:1!important}
.plug{fill:var(--panel);stroke-width:1.5}

/* ------------------------------------------------------------------- nodes */
.node{position:absolute;width:182px;background:var(--panel);border:1px solid var(--hair);
      border-left:3px solid var(--lane);border-radius:2px;padding:8px 10px 9px;box-shadow:var(--shadow);
      cursor:grab;user-select:none;transition:box-shadow .14s,opacity .16s,border-color .14s}
.node:hover{box-shadow:var(--shadow-lift);border-color:var(--lane)}
.node.drag{cursor:grabbing;box-shadow:var(--shadow-lift);z-index:20}
.node.sel{border-color:var(--lane);box-shadow:var(--shadow-lift),0 0 0 2px var(--lane)}
.node.dim{opacity:.17}
.node .top{display:flex;align-items:center;gap:6px;margin-bottom:3px}
.node .glyph{width:13px;height:13px;flex:none;color:var(--lane)}
.node .nm{font-size:12px;font-weight:600;line-height:1.2;letter-spacing:-.005em;
          overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.node .meta{font-family:var(--mono);font-size:9.5px;color:var(--muted);line-height:1.35;
            overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.node .pill{margin-top:5px;display:inline-flex;align-items:center;gap:5px;font-family:var(--mono);
            font-size:9.5px;font-weight:500;padding:2px 5px;border-radius:2px;background:var(--panel-2);
            max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.node .pill i{width:6px;height:6px;border-radius:50%;flex:none;font-style:normal}
.s-ok i{background:var(--ok)} .s-warn i{background:var(--warn)} .s-idle i{background:var(--idle)}
.s-ok{color:var(--ok)} .s-warn{color:var(--warn)} .s-idle{color:var(--idle)}

/* ------------------------------------------------------------ column heads */
.colhead{position:absolute;font-family:var(--mono);font-size:9.5px;font-weight:500;letter-spacing:.14em;
         text-transform:uppercase;color:var(--muted);opacity:.62;white-space:nowrap}

/* ------------------------------------------------------------------ legend */
.legend{position:fixed;left:14px;bottom:14px;z-index:30;background:var(--panel);border:1px solid var(--hair);
        border-radius:2px;padding:9px 11px;box-shadow:var(--shadow);max-width:210px}
.legend h2{margin:0 0 6px;padding-right:18px;font-family:var(--mono);font-size:9.5px;font-weight:500;
           letter-spacing:.14em;text-transform:uppercase;color:var(--muted)}
.legend ul{list-style:none;margin:0;padding:0;display:grid;gap:3px}
.legend li{display:flex;align-items:center;gap:7px;font-size:11px;color:var(--muted)}
.legend svg{flex:none}
.legend .close{position:absolute;top:5px;right:6px;font-size:14px;line-height:1;color:var(--muted);padding:2px}

/* ------------------------------------------------------------------ drawer */
.drawer{position:fixed;top:var(--rail);right:0;bottom:0;width:min(440px,100vw);z-index:50;
        background:var(--panel);border-left:1px solid var(--hair);box-shadow:var(--shadow-lift);
        transform:translateX(101%);transition:transform .24s cubic-bezier(.4,0,.2,1);
        display:flex;flex-direction:column}
.drawer.open{transform:none}
@media (prefers-reduced-motion:reduce){.drawer{transition:none}}
.dhead{padding:16px 18px 13px;border-bottom:1px solid var(--hair);border-top:3px solid var(--lane);flex:none}
.dhead .kicker{display:flex;align-items:center;gap:7px;margin-bottom:7px}
.dhead .kicker span{font-family:var(--mono);font-size:9.5px;font-weight:500;letter-spacing:.12em;
                    text-transform:uppercase;color:var(--lane)}
.dhead h2{margin:0;font-size:20px;font-weight:700;letter-spacing:-.018em;line-height:1.15;text-wrap:balance}
.dhead p{margin:7px 0 0;font-family:var(--serif);font-size:14px;line-height:1.45;color:var(--muted)}
.dclose{position:absolute;top:12px;right:14px;font-size:20px;line-height:1;color:var(--muted);padding:3px 6px}
.dclose:hover{color:var(--ink)}
.dbody{padding:16px 18px 40px;overflow-y:auto;flex:1}
.dbody .what{font-family:var(--serif);font-size:15px;line-height:1.58;margin:0 0 18px}
.dl{display:grid;grid-template-columns:auto 1fr;gap:5px 14px;font-size:12px;margin:0 0 16px;
    padding-top:14px;border-top:1px solid var(--hair-soft)}
.dl dt{font-family:var(--mono);font-size:9.5px;letter-spacing:.1em;text-transform:uppercase;
       color:var(--muted);padding-top:2px}
.dl dd{margin:0;font-family:var(--mono);font-size:11.5px;line-height:1.5;word-break:break-word}
.io{display:grid;gap:4px;margin:0 0 16px}
.io .row{display:flex;gap:8px;align-items:baseline;font-size:12px}
.io .k{font-family:var(--mono);font-size:9.5px;letter-spacing:.1em;text-transform:uppercase;
       color:var(--muted);flex:none;width:52px;padding-top:2px}
.tag{display:inline-block;font-family:var(--mono);font-size:10.5px;padding:1.5px 6px;margin:0 3px 3px 0;
     border:1px solid var(--hair);border-radius:2px;color:var(--muted)}
.wires{margin:0;padding-top:14px;border-top:1px solid var(--hair-soft)}
.wires h3{margin:0 0 8px;font-family:var(--mono);font-size:9.5px;font-weight:500;letter-spacing:.12em;
          text-transform:uppercase;color:var(--muted)}
.wire{display:flex;align-items:baseline;gap:8px;padding:4px 0;font-size:12px;border-bottom:1px solid var(--hair-soft);
      width:100%;text-align:left}
.wire:last-child{border-bottom:0}
.wire:hover .wn{color:var(--accent)}
.wire .dir{font-family:var(--mono);font-size:10px;color:var(--muted);flex:none;width:22px}
.wire .wn{font-weight:600;font-size:12px}
.wire .wl{font-family:var(--mono);font-size:10px;color:var(--muted);margin-left:auto;flex:none;padding-left:8px}
.eqbox{margin:0 0 16px;padding:13px 14px;background:var(--panel-2);border:1px solid var(--hair-soft);
       border-radius:2px;overflow-x:auto}
.eqbox .expr{font-family:var(--mono);font-size:14px;line-height:1.7;white-space:nowrap}
.eqbox .v{font-style:italic;font-family:var(--serif);font-size:15px}
.eqbox .op{margin:0 5px;color:var(--muted)}
.eqbox .fn{color:var(--accent);margin-right:2px}
.eqbox .paren::before{content:"(";color:var(--muted)} .eqbox .paren::after{content:")";color:var(--muted)}
.eqbox .abs::before{content:"|";color:var(--muted)} .eqbox .abs::after{content:"|";color:var(--muted)}
.eqbox .frac{display:inline-flex;flex-direction:column;vertical-align:middle;text-align:center;margin:0 3px}
.eqbox .frac .num{border-bottom:1px solid currentColor;padding:0 5px 1px}
.eqbox .frac .den{padding:1px 5px 0}
.eqbox .subs{margin:9px 0 0;font-family:var(--serif);font-size:13px;line-height:1.5;color:var(--muted)}
.note{margin:0 0 16px;padding:10px 12px;border-left:3px solid var(--lane);background:var(--panel-2);
      font-family:var(--serif);font-size:13.5px;line-height:1.5}

.foot{position:fixed;right:14px;bottom:14px;z-index:30;font-family:var(--mono);font-size:9.5px;
      color:var(--muted);text-align:right;line-height:1.5;pointer-events:none;opacity:.8}
@media (max-width:760px){
  .legend{display:none}
  .drawer{width:100vw;top:auto;height:74vh;border-left:0;border-top:1px solid var(--hair);
          transform:translateY(101%)}
  .drawer.open{transform:none}
  .brand .count{display:none}
}
</style>

<header class="rail">
  <div class="brand">
    <h1>MoneyTree Patchbay</h1>
    <span class="count" id="count"></span>
  </div>
  <div class="sep"></div>
  <div class="chips" id="chips"></div>
  <div class="sep"></div>
  <div class="tools">
    <button class="tool" id="arrange">Arrange</button>
    <button class="tool" id="fit">Fit</button>
    <button class="tool" id="legendBtn">Key</button>
  </div>
</header>

<div class="stage" id="stage">
  <div class="world" id="world">
    <svg class="cables" id="cables"></svg>
  </div>
</div>

<aside class="legend" id="legend">
  <button class="close" id="legendClose" aria-label="Hide the key">&times;</button>
  <h2>What travels the wire</h2>
  <ul id="legendList"></ul>
</aside>

<aside class="drawer" id="drawer" aria-live="polite">
  <div class="dhead" id="dhead">
    <button class="dclose" id="dclose" aria-label="Close">&times;</button>
    <div class="kicker"><span id="dkick"></span></div>
    <h2 id="dname"></h2>
    <p id="done"></p>
  </div>
  <div class="dbody" id="dbody"></div>
</aside>

<div class="foot" id="foot"></div>

<script id="data" type="application/json">__DATA__</script>
<script>
(function(){
"use strict";
var D = JSON.parse(document.getElementById('data').textContent);
var G = D.graph, STATE = D.state;
var NODES = G.nodes, EDGES = G.edges, LANES = G.lanes, PAYLOADS = G.payloads;
var byId = {}; NODES.forEach(function(n){ byId[n.id] = n; });

var COLW = 236, ROWH = 84, PADX = 108, PADY = 50, NW = 182;
var laneVar = function(l){ return 'var(--' + (LANES[l] ? l : 'all') + ')'; };

/* ---- grid layout. col/row come from the model, so the drawing cannot drift
   from the machine: a node added in graph_model.py lands here automatically. */
var maxCol = 0, maxRow = 0;
NODES.forEach(function(n){ maxCol = Math.max(maxCol, n.col); maxRow = Math.max(maxRow, n.row); });
var cols = {};
NODES.forEach(function(n){ (cols[n.col] = cols[n.col] || []).push(n); });
Object.keys(cols).forEach(function(c){
  /* Pack down the column. The model's rows set the order and the rough height;
     this guarantees two nodes can never land on top of each other, however the
     rows are numbered upstream. */
  var floorY = -1e9;
  cols[c].sort(function(a, b){ return a.row - b.row || a.id.localeCompare(b.id); })
    .forEach(function(n){
      var y = Math.max(PADY + n.row * ROWH, floorY);
      n.hx = PADX + Number(c) * COLW; n.hy = y;
      n.x = n.hx; n.y = n.hy; n.h = 62;
      floorY = y + 62 + 26;
    });
});
var W = PADX * 2 + maxCol * COLW + NW, H = PADY * 2 + (maxRow + 1) * ROWH;

var world = document.getElementById('world'), svg = document.getElementById('cables');
world.style.width = W + 'px'; world.style.height = H + 'px';
world.style.willChange = 'transform';
svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
svg.style.width = W + 'px'; svg.style.height = H + 'px';

/* ---- column captions, derived from what actually sits in each column ------ */
var CAPTION = ['Outside world','Raw store','Strategies','The four lanes','Engine & maths',
               'Risk & judgement','Venues','The books','Where it shows up'];
CAPTION.forEach(function(t, c){
  var e = document.createElement('div');
  e.className = 'colhead'; e.textContent = t;
  e.style.left = (PADX + c * COLW) + 'px'; e.style.top = '18px';
  world.appendChild(e);
});

/* ---- glyphs by kind ------------------------------------------------------ */
var GLYPH = {
  source:'<circle cx="8" cy="8" r="2.4"/><path d="M8 5.6V1M3.8 12.2a6 6 0 0 1 8.4 0" fill="none" stroke="currentColor" stroke-width="1.5"/>',
  store:'<rect x="2" y="3" width="12" height="10" rx="1.4" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M2 6.6h12" stroke="currentColor" stroke-width="1.5"/>',
  bot:'<rect x="2.5" y="5" width="11" height="8.5" rx="2" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M8 5V2.5" stroke="currentColor" stroke-width="1.5"/><circle cx="8" cy="2" r="1.3"/><circle cx="5.8" cy="9" r="1"/><circle cx="10.2" cy="9" r="1"/>',
  gate:'<path d="M8 1.6 2.6 4.4v4c0 3.1 2.3 5.4 5.4 6.1 3.1-.7 5.4-3 5.4-6.1v-4z" fill="none" stroke="currentColor" stroke-width="1.5"/>',
  venue:'<path d="M2.4 13.4h11.2M3.8 13.4V6.6m8.4 6.8V6.6M2 6.2 8 2.4l6 3.8z" fill="none" stroke="currentColor" stroke-width="1.5"/>',
  paper:'<path d="M4 2h5.6L12.5 5v9H4z" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M6 8h4.6M6 10.4h4.6" stroke="currentColor" stroke-width="1.3"/>',
  equation:'<path d="M3 5.6h10M3 10.4h10" stroke="currentColor" stroke-width="1.6"/><path d="M11 2.6 6 13.4" stroke="currentColor" stroke-width="1.6"/>'
};

/* ---- node cards ---------------------------------------------------------- */
NODES.forEach(function(n){
  var s = STATE[n.id];
  var el = document.createElement('div');
  el.className = 'node'; el.dataset.id = n.id; el.tabIndex = 0;
  el.style.setProperty('--lane', laneVar(n.lane));
  el.style.left = n.x + 'px'; el.style.top = n.y + 'px';
  var meta = n.cadence || n.model || (LANES[n.lane] ? LANES[n.lane].name : '');
  el.innerHTML =
    '<div class="top"><svg class="glyph" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">' +
      (GLYPH[n.kind] || GLYPH.bot) + '</svg><div class="nm">' + esc(n.name) + '</div></div>' +
    '<div class="meta">' + esc(meta) + '</div>' +
    (s ? '<div class="pill s-' + esc(s.status) + '"><i></i>' + esc(s.headline) +
         (s.sub ? ' · ' + esc(s.sub) : '') + '</div>' : '');
  world.appendChild(el);
  n.el = el;
  n.h = el.offsetHeight;
});

/* ---- cables. A cubic with both controls dropped, so a wire hangs under its
   own weight; heavier payloads (money, orders) hang slacker than a number. */
var paths = EDGES.map(function(e){
  var p = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  var meta = PAYLOADS[e.payload] || {slack:1.1, width:1.6, dash:''};
  p.setAttribute('stroke', laneVar(pickLane(e)));
  p.setAttribute('stroke-width', meta.width);
  if (meta.dash) p.setAttribute('stroke-dasharray', meta.dash);
  p.setAttribute('opacity', '.4');
  svg.appendChild(p);
  return {el:p, e:e, meta:meta};
});
function pickLane(e){
  var a = byId[e.from], b = byId[e.to];
  if (a && a.lane !== 'all') return a.lane;
  if (b && b.lane !== 'all') return b.lane;
  return 'all';
}
function anchors(a, b){
  var ax = a.x + NW, ay = a.y + a.h / 2, bx = b.x, by = b.y + b.h / 2;
  if (b.x + NW / 2 < a.x + NW / 2) { ax = a.x; bx = b.x + NW; }   /* backwards wire */
  return [ax, ay, bx, by];
}
function draw(){
  paths.forEach(function(p){
    var a = byId[p.e.from], b = byId[p.e.to];
    if (!a || !b) return;
    var q = anchors(a, b), ax = q[0], ay = q[1], bx = q[2], by = q[3];
    var dx = bx - ax, dy = by - ay, dist = Math.sqrt(dx * dx + dy * dy);
    var back = (bx < ax) ? 1 : 0;
    var h = Math.max(46, Math.min(190, Math.abs(dx) * 0.46)) * (back ? -1.5 : 1);
    var sag = (p.meta.slack - 1) * dist * 1.45 + 8;
    p.el.setAttribute('d', 'M' + ax + ',' + ay +
      'C' + (ax + h) + ',' + (ay + sag) + ' ' + (bx - h) + ',' + (by + sag) + ' ' + bx + ',' + by);
  });
}
draw();

/* ---- lane filter --------------------------------------------------------- */
var active = {}, chips = document.getElementById('chips');
Object.keys(LANES).forEach(function(k){
  var b = document.createElement('button');
  b.className = 'chip'; b.type = 'button'; b.setAttribute('aria-pressed', 'false');
  b.style.setProperty('--lane', laneVar(k));
  b.style.color = laneVar(k);
  b.innerHTML = '<span class="dot"></span>' + esc(LANES[k].name);
  b.title = LANES[k].note;
  b.onclick = function(){
    if (active[k]) delete active[k]; else active[k] = 1;
    b.setAttribute('aria-pressed', active[k] ? 'true' : 'false');
    applyFilter();
  };
  chips.appendChild(b);
});
function applyFilter(){
  var keys = Object.keys(active), on = keys.length > 0;
  NODES.forEach(function(n){ n.el.classList.toggle('dim', on && !active[n.lane]); });
  paths.forEach(function(p){
    var a = byId[p.e.from], b = byId[p.e.to];
    var vis = !on || (active[a.lane] && active[b.lane]);
    p.el.setAttribute('opacity', vis ? '.4' : '.05');
  });
}

/* ---- hover highlights the wires a node is actually on -------------------- */
function hot(id, isOn){
  paths.forEach(function(p){
    var t = p.e.from === id || p.e.to === id;
    if (!t) return;
    p.el.classList.toggle('hot', isOn);
    p.el.setAttribute('opacity', isOn ? '1' : '.4');
  });
  NODES.forEach(function(n){
    if (n.id === id) return;
    var linked = EDGES.some(function(e){
      return (e.from === id && e.to === n.id) || (e.to === id && e.from === n.id); });
    n.el.style.opacity = (isOn && !linked) ? '.4' : '';
  });
}

/* ---- drag ---------------------------------------------------------------- */
var drag = null;
world.addEventListener('pointerdown', function(ev){
  var el = ev.target.closest('.node'); if (!el) return;
  var n = byId[el.dataset.id];
  drag = {n:n, dx:ev.clientX / zoom - n.x, dy:ev.clientY / zoom - n.y, moved:false};
  el.classList.add('drag'); el.setPointerCapture(ev.pointerId);
  ev.preventDefault();
});
world.addEventListener('pointermove', function(ev){
  if (!drag) return;
  drag.moved = true;
  drag.n.x = ev.clientX / zoom - drag.dx;
  drag.n.y = ev.clientY / zoom - drag.dy;
  drag.n.el.style.left = drag.n.x + 'px';
  drag.n.el.style.top = drag.n.y + 'px';
  draw();
});
world.addEventListener('pointerup', function(ev){
  if (!drag) return;
  drag.n.el.classList.remove('drag');
  if (!drag.moved) open(drag.n);
  drag = null;
});
world.addEventListener('pointerover', function(ev){
  var el = ev.target.closest('.node'); if (el && !drag) hot(el.dataset.id, true); });
world.addEventListener('pointerout', function(ev){
  var el = ev.target.closest('.node'); if (el && !drag) hot(el.dataset.id, false); });
world.addEventListener('keydown', function(ev){
  var el = ev.target.closest('.node');
  if (el && (ev.key === 'Enter' || ev.key === ' ')) { ev.preventDefault(); open(byId[el.dataset.id]); }
});

/* ---- arrange: ease every node home -------------------------------------- */
document.getElementById('arrange').onclick = function(){
  var t0 = null, from = NODES.map(function(n){ return [n.x, n.y]; });
  var reduce = matchMedia('(prefers-reduced-motion:reduce)').matches;
  if (reduce){ NODES.forEach(function(n){ n.x = n.hx; n.y = n.hy; place(n); }); draw(); return; }
  function step(t){
    if (t0 === null) t0 = t;
    var k = Math.min(1, (t - t0) / 620), e = 1 - Math.pow(1 - k, 3);
    NODES.forEach(function(n, i){
      n.x = from[i][0] + (n.hx - from[i][0]) * e;
      n.y = from[i][1] + (n.hy - from[i][1]) * e;
      place(n);
    });
    draw();
    if (k < 1) requestAnimationFrame(step);
  }
  requestAnimationFrame(step);
};
function place(n){ n.el.style.left = n.x + 'px'; n.el.style.top = n.y + 'px'; }

/* ---- zoom to fit --------------------------------------------------------- */
var stage = document.getElementById('stage'), zoom = 1;
var fitBtn = document.getElementById('fit');
function fitZoom(){
  return Math.max(.3, Math.min(1, (stage.clientWidth - 24) / W, (stage.clientHeight - 24) / H));
}
function setZoom(z){
  zoom = z;
  world.style.transform = 'scale(' + z + ')';
  world.style.width = (W * z) + 'px'; world.style.height = (H * z) + 'px';
  fitBtn.classList.toggle('on', Math.abs(z - 1) > .001);
}
fitBtn.onclick = function(){
  setZoom(Math.abs(zoom - 1) < .001 ? fitZoom() : 1);
  stage.scrollTo({left:0, top:0, behavior:'smooth'});
};
/* Open showing the whole machine — a graph this wide reads as broken if it
   opens cropped to one corner. But below about half size the labels stop being
   words, so a narrow screen opens at 1:1 instead, where the sources and the
   four lanes already are, and pans from there. */
setZoom(fitZoom() >= .5 ? fitZoom() : 1);

/* ---- drag the background to pan ----------------------------------------- */
var pan = null;
stage.addEventListener('pointerdown', function(ev){
  if (ev.target.closest('.node')) return;
  pan = {x:ev.clientX, y:ev.clientY, l:stage.scrollLeft, t:stage.scrollTop};
  stage.classList.add('panning');
});
addEventListener('pointermove', function(ev){
  if (!pan) return;
  stage.scrollLeft = pan.l - (ev.clientX - pan.x);
  stage.scrollTop = pan.t - (ev.clientY - pan.y);
});
addEventListener('pointerup', function(){ pan = null; stage.classList.remove('panning'); });

/* ---- the key ------------------------------------------------------------- */
var list = document.getElementById('legendList');
Object.keys(PAYLOADS).forEach(function(k){
  var p = PAYLOADS[k], li = document.createElement('li');
  li.innerHTML = '<svg width="34" height="10" aria-hidden="true"><path d="M1 3 Q17 ' +
    (3 + (p.slack - 1) * 22) + ' 33 3" fill="none" stroke="var(--muted)" stroke-width="' + p.width +
    '"' + (p.dash ? ' stroke-dasharray="' + p.dash + '"' : '') + '/></svg>' + esc(p.label);
  list.appendChild(li);
});
var legend = document.getElementById('legend'), legendBtn = document.getElementById('legendBtn');
function showLegend(on){ legend.hidden = !on; legendBtn.classList.toggle('on', on); }
legendBtn.onclick = function(){ showLegend(legend.hidden); };
document.getElementById('legendClose').onclick = function(){ showLegend(false); };
showLegend(innerWidth > 900);

/* ---- drawer -------------------------------------------------------------- */
var drawer = document.getElementById('drawer'), dbody = document.getElementById('dbody');
var selected = null;
function open(n){
  if (selected) selected.el.classList.remove('sel');
  selected = n; n.el.classList.add('sel');
  var s = STATE[n.id], lane = LANES[n.lane] || LANES.all;
  document.getElementById('dhead').style.setProperty('--lane', laneVar(n.lane));
  document.getElementById('dkick').textContent = lane.name + ' · ' + n.kind;
  document.getElementById('dname').textContent = n.name;
  document.getElementById('done').textContent = n.one_liner;

  var h = '';
  if (s) h += '<div class="note" style="--lane:' + laneVar(n.lane) + '"><strong>' + esc(s.headline) +
              '</strong>' + (s.sub ? ' — ' + esc(s.sub) : '') +
              (s.detail ? '<br><span style="color:var(--muted)">' + esc(s.detail) + '</span>' : '') + '</div>';
  h += '<p class="what">' + esc(n.what) + '</p>';
  if (n.note) h += '<div class="note" style="--lane:' + laneVar(n.lane) + '">' + esc(n.note) + '</div>';
  if (n.eq) h += '<div class="eqbox"><div class="expr">' + n.eq.html + '</div>' +
                 (n.eq.subs ? '<p class="subs">' + esc(n.eq.subs) + '</p>' : '') +
                 (n.eq.note ? '<p class="subs">' + esc(n.eq.note) + '</p>' : '') + '</div>';

  var dl = '';
  if (n.cadence) dl += '<dt>Runs</dt><dd>' + esc(n.cadence) + '</dd>';
  if (n.model)   dl += '<dt>Model</dt><dd>' + esc(n.model) + '</dd>';
  if (n.file)    dl += '<dt>Code</dt><dd>' + esc(n.file) + '</dd>';
  if (dl) h += '<dl class="dl">' + dl + '</dl>';

  if ((n.reads || []).length || (n.writes || []).length){
    h += '<div class="io">';
    if ((n.reads || []).length)
      h += '<div class="row"><span class="k">Reads</span><span>' +
           n.reads.map(function(t){ return '<span class="tag">' + esc(t) + '</span>'; }).join('') + '</span></div>';
    if ((n.writes || []).length)
      h += '<div class="row"><span class="k">Writes</span><span>' +
           n.writes.map(function(t){ return '<span class="tag">' + esc(t) + '</span>'; }).join('') + '</span></div>';
    h += '</div>';
  }

  var wires = EDGES.filter(function(e){ return e.from === n.id || e.to === n.id; });
  if (wires.length){
    h += '<div class="wires"><h3>' + wires.length + ' wire' + (wires.length > 1 ? 's' : '') + '</h3>';
    wires.forEach(function(e){
      var out = e.from === n.id, other = byId[out ? e.to : e.from];
      if (!other) return;
      h += '<button class="wire" data-go="' + esc(other.id) + '">' +
           '<span class="dir">' + (out ? '→' : '←') + '</span>' +
           '<span class="wn">' + esc(other.name) + '</span>' +
           '<span class="wl">' + esc(e.label || (PAYLOADS[e.payload] || {}).label || '') + '</span></button>';
    });
    h += '</div>';
  }
  dbody.innerHTML = h;
  dbody.scrollTop = 0;
  dbody.querySelectorAll('[data-go]').forEach(function(b){
    b.onclick = function(){
      var t = byId[b.dataset.go];
      open(t);
      t.el.scrollIntoView({block:'center', inline:'center', behavior:'smooth'});
    };
  });
  drawer.classList.add('open');
}
function close(){
  drawer.classList.remove('open');
  if (selected) selected.el.classList.remove('sel');
  selected = null;
}
document.getElementById('dclose').onclick = close;
addEventListener('keydown', function(ev){ if (ev.key === 'Escape') close(); });

/* ---- chrome -------------------------------------------------------------- */
document.getElementById('count').textContent =
  NODES.length + ' parts · ' + EDGES.length + ' wires';
document.getElementById('foot').innerHTML =
  'drag a part · click for detail<br>state snapshot ' + esc(D.stamp);

function esc(s){
  return String(s == null ? '' : s).replace(/[&<>"]/g, function(c){
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]; });
}
})();
</script>'''
out = os.path.join(SP, 'moneytree-patchbay.html')
io.open(out, 'w', encoding='utf-8').write(HTML.replace('__DATA__', blob))
print('wrote', out, os.path.getsize(out), 'bytes')

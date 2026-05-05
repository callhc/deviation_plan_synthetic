/**
 * app.js — state management, SVG rendering, interactions, export.
 *
 * Depends on network.js (parseNet) being loaded first.
 *
 * Edge CSS classes (colour) + stroke-width (set as presentation attribute):
 *   e-base      grey   — normal, unselected edge
 *   e-closed    red    — the closed road
 *   e-detour    blue   — edges added to the current detour
 *   e-available amber  — edges reachable as the next detour step
 *   e-dimmed    light  — edges that cannot be selected right now
 *   e-done      sky    — detour edges when destination has been reached
 *
 * Stroke widths are defined in style.css using vector-effect:non-scaling-stroke
 * so they stay crisp in screen pixels at every zoom level.
 */

'use strict';

/* ── application state ───────────────────────────────────────────────────── */
let mode        = 'close';   // 'close' | 'detour'
let closedEdge  = null;      // id of the closed edge, or null
let detourEdges = [];        // ordered list of detour edge ids

let edgeInfo  = {};          // id → { from, to }    — populated after file load
let outgoing  = {};          // nodeId → [edgeId, …] — for neighbour lookup
const edgeElems = {};        // id → visible SVG <polyline>

/* ── SVG viewBox / pan / zoom ────────────────────────────────────────────── */
const svg = document.getElementById('canvas');
let vb     = { x: 0, y: 0, w: 1, h: 1 };
let drag   = false;
let dragPt = null;

svg.addEventListener('mousedown', e => {
  if (e.target.classList.contains('e-hit')) return;
  drag = true;
  dragPt = { x: e.clientX, y: e.clientY };
  svg.classList.add('drag');
});

window.addEventListener('mousemove', e => {
  if (!drag) return;
  const r = svg.getBoundingClientRect();
  vb.x -= (e.clientX - dragPt.x) / r.width  * vb.w;
  vb.y -= (e.clientY - dragPt.y) / r.height * vb.h;
  dragPt = { x: e.clientX, y: e.clientY };
  applyVB();
});

window.addEventListener('mouseup', () => {
  drag = false;
  svg.classList.remove('drag');
});

svg.addEventListener('wheel', e => {
  e.preventDefault();
  const r  = svg.getBoundingClientRect();
  const f  = e.deltaY > 0 ? 1.12 : 0.89;
  const mx = (e.clientX - r.left) / r.width  * vb.w + vb.x;
  const my = (e.clientY - r.top)  / r.height * vb.h + vb.y;
  vb.w *= f;
  vb.h *= f;
  vb.x  = mx - (e.clientX - r.left) / r.width  * vb.w;
  vb.y  = my - (e.clientY - r.top)  / r.height * vb.h;
  applyVB();
}, { passive: false });

function applyVB() {
  svg.setAttribute('viewBox', `${vb.x} ${vb.y} ${vb.w} ${vb.h}`);
}

/* ── file input ──────────────────────────────────────────────────────────── */
document.getElementById('file-input').addEventListener('change', function () {
  const file = this.files[0];
  if (!file) return;
  setStatus('Parsing…');
  const reader = new FileReader();
  reader.onload = ev => {
    try {
      const net = parseNet(ev.target.result);
      edgeInfo = net.edgeInfo;
      outgoing = net.outgoing;
      resetState();
      renderNet(net.edges);
      setStatus(`${net.edges.length} edges loaded.`);
    } catch (err) {
      setStatus('Error: ' + err.message);
    }
  };
  reader.readAsText(file);
});

function setStatus(msg) {
  document.getElementById('status').textContent = msg;
}

/* ── SVG rendering ───────────────────────────────────────────────────────── */
function renderNet(edges) {
  // compute bounding box over all shape points
  let [x0, y0, x1, y1] = [Infinity, Infinity, -Infinity, -Infinity];
  for (const { shape } of edges)
    for (const [x, y] of shape) {
      if (x < x0) x0 = x;  if (x > x1) x1 = x;
      if (y < y0) y0 = y;  if (y > y1) y1 = y;
    }
  const pad = Math.max(x1 - x0, y1 - y0) * 0.025;
  x0 -= pad; x1 += pad; y0 -= pad; y1 += pad;

  Object.assign(vb, { x: x0, y: y0, w: x1 - x0, h: y1 - y0 });
  applyVB();

  const NS = 'http://www.w3.org/2000/svg';

  // ── direction arrow marker ────────────────────────────────────────────────
  // markerUnits="strokeWidth" keeps the arrow proportional to the screen-pixel
  // stroke width of each edge class.  fill:context-stroke inherits the edge
  // colour automatically, so no per-class marker variants are needed.
  let defs = svg.querySelector('defs');
  if (!defs) {
    defs = document.createElementNS(NS, 'defs');
    svg.insertBefore(defs, svg.firstChild);
  }
  defs.innerHTML = `
    <marker id="arr" markerWidth="5" markerHeight="5"
            refX="5" refY="2.5" orient="auto" markerUnits="strokeWidth">
      <path d="M0,0.5 L5,2.5 L0,4.5 L1.2,2.5 Z"
            fill="context-stroke" stroke="none"/>
    </marker>`;

  const world = document.getElementById('world');
  world.innerHTML = '';

  for (const e of edges) {
    // flip Y: SUMO is north-up, SVG is top-down → reflect around mid-Y
    const pts = e.shape
      .map(([x, y]) => `${x},${y0 + y1 - y}`)
      .join(' ');

    // visible polyline — class drives all visual properties via stylesheet
    const vis = document.createElementNS(NS, 'polyline');
    vis.setAttribute('points', pts);
    vis.setAttribute('class', 'e-base');
    vis.setAttribute('marker-end', 'url(#arr)');
    edgeElems[e.id] = vis;

    // wide transparent hit target — non-scaling so it stays easy to click
    const hit = document.createElementNS(NS, 'polyline');
    hit.setAttribute('points', pts);
    hit.setAttribute('class', 'e-hit');
    hit.addEventListener('click', ev => { ev.stopPropagation(); onEdgeClick(e.id); });

    // SVG tooltip shows edge id on hover
    const ttip = document.createElementNS(NS, 'title');
    ttip.textContent = e.id;
    hit.appendChild(ttip);

    world.appendChild(vis);
    world.appendChild(hit);
  }
}

/* ── destination helpers ─────────────────────────────────────────────────── */
/**
 * True when the last detour edge ends at the target node of the closed edge.
 * Once true, no further edges should be added.
 */
function isDestinationReached() {
  return (
    closedEdge !== null &&
    detourEdges.length > 0 &&
    edgeInfo[detourEdges.at(-1)]?.to === edgeInfo[closedEdge]?.to
  );
}

/* ── available-edges logic ───────────────────────────────────────────────── */
/**
 * Return the set of edge ids that are valid as the next detour step.
 *
 * Returns an empty set once the destination node has been reached, so no
 * further edges can be appended.
 *
 * First step:  edges leaving the `from` node of the closed edge.
 * Subsequent:  edges leaving the `to` node of the last selected detour edge.
 * Already-used edges and the closed edge itself are excluded.
 */
function getAvailableEdges() {
  if (!closedEdge) return new Set();
  if (isDestinationReached()) return new Set();   // ← stop here

  const startNode = detourEdges.length === 0
    ? edgeInfo[closedEdge].from
    : edgeInfo[detourEdges.at(-1)].to;

  const detourSet = new Set(detourEdges);
  const available = new Set();
  for (const id of (outgoing[startNode] || [])) {
    if (id !== closedEdge && !detourSet.has(id))
      available.add(id);
  }
  return available;
}

/* ── highlight update ────────────────────────────────────────────────────── */
/**
 * Recompute and apply class + stroke-width to every visible edge element.
 *
 * Priority (highest first):
 *   e-closed    the closed road
 *   e-done      detour edges when destination reached (signals completion)
 *   e-detour    detour edges still in progress
 *   e-available edges reachable as the next step (detour mode only)
 *   e-dimmed    everything else in detour mode
 *   e-base      everything else in close mode
 */
function updateHighlights() {
  const inDetour   = new Set(detourEdges);
  const destDone   = isDestinationReached();
  const available  = (mode === 'detour' && closedEdge && !destDone)
    ? getAvailableEdges()
    : new Set();
  const applyDim   = mode === 'detour' && closedEdge !== null;

  for (const [id, el] of Object.entries(edgeElems)) {
    if (id === closedEdge)         el.setAttribute('class', 'e-closed');
    else if (inDetour.has(id))     el.setAttribute('class', destDone ? 'e-done' : 'e-detour');
    else if (available.has(id))    el.setAttribute('class', 'e-available');
    else if (applyDim)             el.setAttribute('class', 'e-dimmed');
    else                           el.setAttribute('class', 'e-base');
  }
}

/* ── edge click handler ──────────────────────────────────────────────────── */
function onEdgeClick(id) {
  if (mode === 'close') {
    // selecting a new closed edge resets the detour
    detourEdges = [];
    closedEdge  = id;
    updateHighlights();
    refreshClosedDisplay();
    refreshDetourDisplay();
  } else {
    // only accept edges that are highlighted as available
    if (!getAvailableEdges().has(id)) return;
    detourEdges.push(id);
    updateHighlights();
    refreshDetourDisplay();
  }
  refreshExportBtn();
}

/* ── mode buttons ────────────────────────────────────────────────────────── */
document.getElementById('btn-close').addEventListener('click', () => {
  mode = 'close';
  document.getElementById('btn-close').classList.add('on');
  document.getElementById('btn-detour').classList.remove('on');
  updateHighlights();
});

document.getElementById('btn-detour').addEventListener('click', () => {
  mode = 'detour';
  document.getElementById('btn-detour').classList.add('on');
  document.getElementById('btn-close').classList.remove('on');
  updateHighlights();
});

/* ── sidebar display helpers ─────────────────────────────────────────────── */
function refreshClosedDisplay() {
  document.getElementById('closed-display').innerHTML = closedEdge
    ? `<span class="eid c">${closedEdge}</span>`
    : '<span class="tip">None selected.</span>';
}

function refreshDetourDisplay() {
  document.getElementById('detour-list').innerHTML = detourEdges
    .map(id => `<li><span class="eid d">${id}</span></li>`)
    .join('');

  const dest = document.getElementById('dest-status');
  if (!closedEdge || detourEdges.length === 0) {
    dest.textContent = '';
  } else if (isDestinationReached()) {
    dest.style.color = '#2e7d32';
    dest.textContent = '✓ Destination reached — ready to export.';
  } else {
    dest.style.color = '#999';
    dest.textContent = `Target node: ${edgeInfo[closedEdge].to}`;
  }
}

function refreshExportBtn() {
  document.getElementById('export-btn').disabled =
    !(closedEdge && detourEdges.length > 0);
}

/* ── state reset ─────────────────────────────────────────────────────────── */
function resetState() {
  mode        = 'close';
  closedEdge  = null;
  detourEdges = [];

  for (const k of Object.keys(edgeElems)) delete edgeElems[k];

  document.getElementById('btn-close').classList.add('on');
  document.getElementById('btn-detour').classList.remove('on');
  refreshClosedDisplay();
  refreshDetourDisplay();
  refreshExportBtn();
}

/* ── remove / clear buttons ──────────────────────────────────────────────── */
document.getElementById('btn-remove').addEventListener('click', () => {
  if (!detourEdges.length) return;
  detourEdges.pop();
  updateHighlights();
  refreshDetourDisplay();
  refreshExportBtn();
});

document.getElementById('btn-clear').addEventListener('click', () => {
  detourEdges = [];
  updateHighlights();
  refreshDetourDisplay();
  refreshExportBtn();
});

/* ── export ──────────────────────────────────────────────────────────────── */
document.getElementById('export-btn').addEventListener('click', () => {
  const planId = document.getElementById('plan-id').value.trim() || 'manual_01';

  const payload = {
    closed_edge: closedEdge,
    plans: [{
      plan_id:      planId,
      detour_edges: [...detourEdges],
      source_node:  edgeInfo[detourEdges[0]]?.from   ?? null,
      target_node:  edgeInfo[detourEdges.at(-1)]?.to ?? null,
    }],
  };

  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
  const a    = document.createElement('a');
  a.href     = URL.createObjectURL(blob);
  a.download = `${planId}.json`;
  a.click();
  URL.revokeObjectURL(a.href);
});

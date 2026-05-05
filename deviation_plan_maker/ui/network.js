/**
 * network.js — SUMO .net.xml parser (no DOM dependencies beyond DOMParser).
 *
 * Exported function: parseNet(xmlText) → { edges, edgeInfo, outgoing }
 *
 *   edges    — Array<{ id, from, to, shape: [[x,y], …] }>
 *   edgeInfo — Map  id → { from, to }
 *   outgoing — Map  nodeId → [edgeId, …]   (for fast neighbour lookup)
 */

'use strict';

function parseNet(xmlText) {
  const doc = new DOMParser().parseFromString(xmlText, 'text/xml');
  if (doc.querySelector('parsererror'))
    throw new Error('Invalid XML — check that the file is a well-formed .net.xml.');

  /* ── 1. junction coordinates (fallback geometry) ─────────────────────── */
  const jct = {};
  for (const j of doc.querySelectorAll('junction')) {
    const id = j.getAttribute('id');
    if (id && !id.startsWith(':'))
      jct[id] = { x: +j.getAttribute('x'), y: +j.getAttribute('y') };
  }

  /* ── 2. edges ─────────────────────────────────────────────────────────── */
  const edges    = [];
  const edgeInfo = {};   // id → { from, to }
  const outgoing = {};   // nodeId → [edgeId, …]

  for (const e of doc.querySelectorAll('edge')) {
    const id = e.getAttribute('id');

    // skip internal junction connectors
    if (!id || id.startsWith(':') || e.getAttribute('function') === 'internal') continue;

    const from = e.getAttribute('from');
    const to   = e.getAttribute('to');
    if (!from || !to) continue;

    // prefer lane shape attribute; fall back to straight junction-to-junction line
    let shape = null;
    const lane = e.querySelector('lane');
    if (lane) {
      const raw = lane.getAttribute('shape');
      if (raw) {
        shape = raw.trim().split(' ').map(pt => pt.split(',').map(Number));
      }
    }
    if (!shape && jct[from] && jct[to]) {
      shape = [[jct[from].x, jct[from].y], [jct[to].x, jct[to].y]];
    }
    if (!shape) continue;

    edges.push({ id, from, to, shape });
    edgeInfo[id] = { from, to };

    if (!outgoing[from]) outgoing[from] = [];
    outgoing[from].push(id);
  }

  if (!edges.length)
    throw new Error('No drivable edges found — is this a valid SUMO .net.xml?');

  return { edges, edgeInfo, outgoing };
}

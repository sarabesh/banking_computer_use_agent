// Runs inside every frame (with describe.js prepended). Builds the list of things a human
// operator would perceive: controls (with their visible caption) and text (with its table
// column/row context). Elements are kept in window.__cua_elems so Python can get a handle.
const H = cuaHelpers();
const out = [];
const elems = [];
if (document.body) {
  // The body itself too: legacy pages often put text straight into <body>.
  for (const el of [document.body, ...document.body.querySelectorAll('*')]) {
    if (!H.isCandidate(el)) continue;
    out.push({ i: elems.length, ...H.describe(el) });
    elems.push(el);
  }
}
window.__cua_elems = elems;
return out;

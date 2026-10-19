// How an element is described the way a human operator would describe it. Shared by
// perception (observe.js) and the human-action recorder (recorder.js), so an agent step
// and a human step on the same control produce the same fingerprint.
function cuaHelpers() {
  const clean = t => (t || '').replace(/\s+/g, ' ').trim();
  const CONTROL_SEL = 'a[href], button, input:not([type=hidden]), select, textarea, [onclick], ' +
                      '[role=button], [role=link], [role=tab], [role=menuitem], [role=checkbox]';
  const SKIP_TAGS = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'OPTION', 'IFRAME', 'FRAME', 'HEAD', 'TITLE']);

  const visible = el => {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) return false;
    const s = getComputedStyle(el);
    return s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0';
  };

  const inputType = el => (el.getAttribute('type') || 'text').toLowerCase();
  const isButtonInput = el => el.tagName === 'INPUT' && ['submit', 'button', 'reset', 'image'].includes(inputType(el));

  const roleOf = el => {
    const explicit = el.getAttribute('role');
    if (explicit) return explicit;
    const tag = el.tagName;
    if (tag === 'A') return 'link';
    if (tag === 'BUTTON' || isButtonInput(el)) return 'button';
    if (tag === 'SELECT') return 'combobox';
    if (tag === 'TEXTAREA') return 'textbox';
    if (tag === 'INPUT') {
      const t = inputType(el);
      if (t === 'checkbox' || t === 'radio') return t;
      return 'textbox';
    }
    if (el.hasAttribute('onclick')) return 'button';
    if (tag === 'TD' || tag === 'TH') return 'cell';
    return 'text';
  };

  const labelFor = el => {
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l) return clean(l.innerText);
    }
    const wrap = el.closest('label');
    return wrap ? clean(wrap.innerText) : null;
  };

  const nameOf = el => {
    const aria = el.getAttribute('aria-label');
    if (aria) return clean(aria);
    const lb = el.getAttribute('aria-labelledby');
    if (lb) {
      const t = lb.split(/\s+/).map(id => document.getElementById(id)).filter(Boolean).map(e => e.innerText).join(' ');
      if (clean(t)) return clean(t);
    }
    if (isButtonInput(el)) return clean(el.value || el.alt);
    if (['INPUT', 'SELECT', 'TEXTAREA'].includes(el.tagName)) return labelFor(el) || clean(el.title) || '';
    return clean(el.innerText || el.textContent).slice(0, 120);
  };

  // The visible text a human reads as a field's label when the markup has no <label>:
  // the nearest non-empty cell to the left in the same row, else the preceding text.
  const captionOf = el => {
    const cell = el.closest('td,th');
    if (cell) {
      for (let prev = cell.previousElementSibling; prev; prev = prev.previousElementSibling) {
        const t = clean(prev.innerText);
        if (t) return t.slice(0, 60);
      }
    }
    for (let node = el.previousSibling; node; node = node.previousSibling) {
      const t = clean(node.textContent);
      if (t) return t.slice(-60);
    }
    return clean(el.getAttribute('placeholder')) || null;
  };

  // Semantic table position: header of this cell's column + first cell of its row.
  // Survives column reordering and relabelled layouts better than a positional path.
  const looksBold = c => c.tagName === 'TH' || parseInt(getComputedStyle(c).fontWeight, 10) >= 600 ||
    Array.from(c.children).some(k => ['B', 'STRONG'].includes(k.tagName) && clean(k.innerText) === clean(c.innerText));
  // A first row is a header only if every non-empty cell looks like one. Key/value layouts
  // ("Name: | Avery Quinn") have a data cell in row 0 and so get no column headers.
  const isHeaderRow = row => {
    const cells = Array.from(row.cells).filter(c => clean(c.innerText));
    return cells.length > 1 && cells.every(looksBold);
  };

  // For a value cell in a key/value layout: the label cell to its left, if it reads as a label.
  const valueLabelOf = el => {
    const cell = el.closest('td,th');
    if (!cell || looksBold(cell)) return null;
    for (let prev = cell.previousElementSibling; prev; prev = prev.previousElementSibling) {
      const t = clean(prev.innerText);
      if (!t) continue;
      return (looksBold(prev) || t.endsWith(':')) ? t.slice(0, 60) : null;
    }
    return null;
  };

  const tableContext = el => {
    const cell = el.closest('td,th');
    if (!cell) return {};
    const row = cell.parentElement;
    const table = row && row.closest('table');
    if (!table) return {};
    const header = table.rows[0];
    let col = null;
    if (header && header !== row && isHeaderRow(header) && header.cells[cell.cellIndex]) {
      col = clean(header.cells[cell.cellIndex].innerText) || null;
    }
    const key = row.cells[0] ? clean(row.cells[0].innerText) : null;
    return { col_header: col, row_key: key ? key.slice(0, 60) : null, row_text: clean(row.innerText).slice(0, 200) };
  };

  const cssPath = el => {
    const parts = [];
    for (let node = el; node && node.nodeType === 1 && node !== document.body; node = node.parentElement) {
      let i = 1;
      for (let s = node.previousElementSibling; s; s = s.previousElementSibling) if (s.tagName === node.tagName) i++;
      parts.unshift(`${node.tagName.toLowerCase()}:nth-of-type(${i})`);
    }
    return ['body', ...parts].join(' > ');
  };

  const occluded = el => {
    const r = el.getBoundingClientRect();
    const x = r.left + r.width / 2, y = r.top + r.height / 2;
    if (x < 0 || y < 0 || x > window.innerWidth || y > window.innerHeight) return false;
    const hit = document.elementFromPoint(x, y);
    return !!hit && hit !== el && !el.contains(hit) && !hit.contains(el);
  };

  const ownText = el => clean(Array.from(el.childNodes).filter(n => n.nodeType === 3).map(n => n.textContent).join(' '));

  // Worth listing: a visible control, or visible text not inside a control.
  const isCandidate = el => {
    if (SKIP_TAGS.has(el.tagName)) return false;
    if (!el.matches(CONTROL_SEL)) {
      if (!ownText(el)) return false;
      if (el.closest(CONTROL_SEL)) return false;   // text inside a link/button belongs to that control
    }
    return visible(el);
  };

  // Relational facts (label-independent): which form an element belongs to, and whether it
  // is that form's submit control. Lets a locator say "the button that submits this form".
  const formOf = el => (el.form || el.closest('form'));
  const submitOf = form => form ? form.querySelector(
    'input[type=submit], input[type=image], button:not([type=button]):not([type=reset])') : null;

  const describe = el => {
    const control = el.matches(CONTROL_SEL);
    const form = control ? formOf(el) : null;
    const tag = el.tagName.toLowerCase();
    const role = roleOf(el);
    const full = clean(el.innerText);
    const r = el.getBoundingClientRect();
    return {
      kind: control ? 'control' : 'text',
      role,
      // <body> only contributes its own loose text, never the whole page.
      name: control ? nameOf(el) : (full.length <= 160 && el !== document.body ? full : ownText(el).slice(0, 160)),
      // Fields and value cells both get the label a human reads beside them ("SSN:").
      caption: control ? (['textbox', 'combobox', 'checkbox', 'radio'].includes(role) ? captionOf(el) : null)
                       : valueLabelOf(el),
      tag,
      field_name: el.getAttribute('name'),
      dom_id: el.id || null,
      input_type: tag === 'input' ? inputType(el) : null,
      href: tag === 'a' ? el.getAttribute('href') : null,
      synthetic: control && !['a', 'button', 'input', 'select', 'textarea'].includes(tag),
      has_value: ['INPUT', 'TEXTAREA'].includes(el.tagName) && !isButtonInput(el) ? !!el.value : false,
      options: tag === 'select' ? Array.from(el.options).map(o => clean(o.text)) : [],
      selected: tag === 'select' && el.selectedIndex >= 0 ? clean(el.options[el.selectedIndex].text) : null,
      css: cssPath(el),
      bbox: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
      occluded: occluded(el),
      form: form ? cssPath(form) : null,
      is_submit: !!form && submitOf(form) === el,
      ...tableContext(el),
    };
  };

  return { CONTROL_SEL, isButtonInput, isCandidate, describe };
}

// Installed in every frame as an init script (with describe.js prepended), so it survives
// navigations. Reports what a human does - clicks, field changes, Enter - with the same
// element description perception uses. Python only keeps these while a human holds control.
// Password values are never sent.
if (!window.__cuaRecorderInstalled) {
  window.__cuaRecorderInstalled = true;
  const H = cuaHelpers();
  const isField = el => ['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName) && !H.isButtonInput(el);
  const send = (kind, el, extra) => {
    try { window.__cua_record({ kind, element: H.describe(el), ...extra }); } catch (e) { /* binding gone */ }
  };
  const sendFill = el => {
    if (el.__cuaSent === el.value) return;          // already reported (Enter before change)
    el.__cuaSent = el.value;
    const secret = el.type === 'password';
    send('fill', el, { value: secret ? null : el.value, secret });
  };
  document.addEventListener('click', e => {
    const el = e.target.closest(H.CONTROL_SEL) || e.target;
    if (!isField(el)) send('click', el, {});
  }, true);
  document.addEventListener('change', e => {
    const el = e.target;
    if (el.tagName === 'SELECT') send('select', el, { value: el.options[el.selectedIndex] ? el.options[el.selectedIndex].text.trim() : null });
    else if (isField(el)) sendFill(el);
  }, true);
  document.addEventListener('keydown', e => {
    if (e.key !== 'Enter') return;
    if (isField(e.target) && e.target.tagName !== 'SELECT') sendFill(e.target);
    send('press', e.target, { key: 'Enter' });
  }, true);
}

// Shared behaviour for every page: theme, toasts, dialogs, forms, time fields, camera check, model download.
(() => {
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
  const urls = document.body.dataset;

  // Theme: follows the system unless the person picked one (kept in this browser).
  const root = document.documentElement;
  $('#theme-toggle')?.addEventListener('click', () => {
    const t = root.dataset.theme === 'dark' ? 'light' : 'dark';
    root.dataset.theme = t;
    try { localStorage.setItem('theme', t); } catch (e) {}
  });

  // Toasts: success messages leave by themselves; warnings and errors stay until closed.
  const dismiss = t => { t.classList.add('leaving'); t.addEventListener('animationend', () => t.remove(), { once: true }); };
  $$('.toast').forEach(t => {
    $('.close', t)?.addEventListener('click', () => dismiss(t));
    if (t.dataset.flash === 'ok') setTimeout(() => dismiss(t), 6000);
  });

  // Dialogs: click outside closes; [data-close] closes the nearest dialog.
  $$('dialog').forEach(d => {
    d.addEventListener('click', e => { if (e.target === d) d.close(); });
    $$('[data-close]', d).forEach(b => b.addEventListener('click', () => d.close()));
  });

  // Forms: a confirmation when asked for, then a spinner on the button so nothing is sent twice.
  document.addEventListener('submit', e => {
    const form = e.target;
    if (form.dataset.confirm && !confirm(form.dataset.confirm)) { e.preventDefault(); return; }
    const btn = e.submitter || $('button:not([type="button"])', form);
    if (btn && !btn.hasAttribute('formnovalidate')) {
      btn.classList.add('is-loading');
      setTimeout(() => { btn.disabled = true; }, 0);  // after the form data is collected
      setTimeout(() => { btn.disabled = false; btn.classList.remove('is-loading'); }, 15000);  // in case the page stays
    }
  });
  $$('[data-autosubmit]').forEach(el => el.addEventListener('change', () => el.form?.requestSubmit()));

  // Keyboard: "/" focuses the page's search box.
  document.addEventListener('keydown', e => {
    if (e.key === '/' && !e.target.closest('input, textarea, select') && $('#search')) { e.preventDefault(); $('#search').focus(); }
  });

  // 24-hour time fields: "830", "8:30" and "0830" all become "08:30"; the arrow keys move by 5 minutes.
  function normTime(v) {
    if (!v.replace(/\D/g, '')) return '';
    let h, m;
    if (v.includes(':')) [h, m] = v.split(':').map(x => parseInt(x.replace(/\D/g, '') || '0', 10));
    else {
      const d = v.replace(/\D/g, '');
      [h, m] = d.length <= 2 ? [+d, 0] : d.length === 3 ? [+d[0], +d.slice(1)] : [+d.slice(0, 2), +d.slice(2, 4)];
    }
    if (h > 23 || m > 59) return v;
    return String(h).padStart(2, '0') + ':' + String(m).padStart(2, '0');
  }
  function stepTime(el, by) {
    const [h, m] = (normTime(el.value) || '00:00').split(':').map(Number);
    const t = ((h * 60 + m + by) % 1440 + 1440) % 1440;
    el.value = String(Math.floor(t / 60)).padStart(2, '0') + ':' + String(t % 60).padStart(2, '0');
    el.dispatchEvent(new Event('input', { bubbles: true }));
  }
  document.addEventListener('focusout', e => { if (e.target.matches?.('input.time24')) e.target.value = normTime(e.target.value); });
  document.addEventListener('keydown', e => {
    if (!e.target.matches?.('input.time24')) return;
    if (e.key === 'ArrowUp' || e.key === 'ArrowDown') { e.preventDefault(); stepTime(e.target, e.key === 'ArrowUp' ? 5 : -5); }
    if (e.key === 'Enter') e.target.value = normTime(e.target.value);  // before the browser checks the format
  });

  // Camera address fields: test the address as soon as it is entered (and on load for data-camera-check="auto").
  $$('input[data-camera-check]').forEach(input => {
    const box = document.createElement('div');
    box.className = 'cam-status'; box.hidden = true;
    input.after(box);
    let seq = 0;
    async function run() {
      const url = input.value.trim(), mine = ++seq;
      if (!url) { box.hidden = true; return; }
      box.hidden = false; box.className = 'cam-status';
      box.innerHTML = '<span class="spin"></span><span>Checking the camera… (≤ 10 s)</span>';
      let r;
      try {
        r = await (await fetch(urls.cameraCheckUrl, { method: 'POST', body: new URLSearchParams({ url }) })).json();
      } catch (e) { r = { level: 'error', message: 'Could not reach the program.' }; }
      if (mine !== seq) return;  // a newer address was typed meanwhile
      box.className = 'cam-status ' + r.level;
      box.replaceChildren();
      if (r.image) { const img = document.createElement('img'); img.src = r.image; img.alt = ''; box.append(img); }
      const text = document.createElement('span');
      text.textContent = (r.level === 'ok' ? '✓ ' : r.level === 'warn' ? '⚠ ' : '✕ ') + r.message;
      const again = document.createElement('a'); again.className = 'again'; again.textContent = 'Check again';
      again.addEventListener('click', e => { e.preventDefault(); run(); });
      text.append(' ', again);
      box.append(text);
    }
    input.addEventListener('change', run);
    input.addEventListener('paste', () => setTimeout(run, 0));
    if (input.dataset.cameraCheck === 'auto' && input.value.trim()) run();
  });

  // First start: follow the model download until it is done.
  const modelsBox = $('#models-status');
  if (modelsBox && urls.modelsUrl) {
    const poll = setInterval(async () => {
      try {
        const s = (await (await fetch(urls.modelsUrl)).json()).status;
        if (s) { modelsBox.lastElementChild.textContent = s + ' · faces are recognised once done; everything else already works.'; return; }
        clearInterval(poll); modelsBox.className = 'alert ok'; modelsBox.textContent = 'Face recognition is ready.';
      } catch (e) {}
    }, 2000);
  }
})();

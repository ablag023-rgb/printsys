'use strict';

// ============== TABS ==============
function switchTab(name){
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
  document.querySelectorAll('.panel').forEach(p => p.classList.toggle('active', p.dataset.panel === name));
}
document.addEventListener('click', e => {
  const t = e.target.closest('.tab');
  if (t) switchTab(t.dataset.tab);
});

// ============== SELECTION ==============
const selected = new Set();
function toggleRow(cb){
  const ksr = cb.dataset.ksr;
  if (cb.checked) selected.add(ksr); else selected.delete(ksr);
  cb.closest('tr').classList.toggle('selected', cb.checked);
  updateBulk();
}
function toggleAll(cb){
  document.querySelectorAll('.row-cb').forEach(x => {
    x.checked = cb.checked;
    const ksr = x.dataset.ksr;
    if (cb.checked) selected.add(ksr); else selected.delete(ksr);
    x.closest('tr').classList.toggle('selected', cb.checked);
  });
  updateBulk();
}
function clearSelection(){
  selected.clear();
  document.querySelectorAll('.row-cb').forEach(x => { x.checked = false; x.closest('tr').classList.remove('selected'); });
  const all = document.getElementById('cb-all'); if (all) all.checked = false;
  updateBulk();
}
function updateBulk(){
  const bar = document.getElementById('bulkbar-form');
  document.getElementById('bulkbar-n').textContent = selected.size;
  bar.classList.toggle('show', selected.size > 0);
  // Sync hidden inputs for ksrs[]
  bar.querySelectorAll('input[name="ksrs"]').forEach(x => x.remove());
  selected.forEach(k => {
    const inp = document.createElement('input');
    inp.type = 'hidden'; inp.name = 'ksrs'; inp.value = k;
    bar.appendChild(inp);
  });
}
function setBulkAction(a){ document.getElementById('bulk-action').value = a; }
function confirmBulk(a, msg){
  if (!confirm(msg + '\nВыбрано: ' + selected.size)) return false;
  setBulkAction(a);
  return true;
}

// После bulk-запроса — снимаем выделение и обновляем список
document.body.addEventListener('htmx:afterRequest', (e) => {
  if (e.detail.xhr.status === 200 && e.detail.requestConfig.path === '/cases/bulk'){
    clearSelection();
    htmx.trigger('body', 'cases-changed');
  }
});

// ============== SORT ==============
function sortBy(key){
  const cur = document.getElementById('current-sort-key')?.value;
  const dir = Number(document.getElementById('current-sort-dir')?.value || 1);
  const newDir = (cur === key) ? -dir : 1;
  const params = new URLSearchParams(window.location.search);
  const form = document.querySelector('.filterbar');
  const fd = new FormData(form);
  const q = new URLSearchParams();
  fd.forEach((v,k) => q.set(k,v));
  q.set('sort_key', key);
  q.set('sort_dir', newDir);
  htmx.ajax('GET', '/cases?' + q.toString(), {target:'#cases-panel', swap:'innerHTML'});
}

function resetFilters(e){
  const form = document.querySelector('.filterbar');
  form.querySelectorAll('input,select').forEach(x => x.value = '');
  htmx.trigger(form, 'change');
}

// ============== DRAWER ==============
function closeDrawer(){
  const slot = document.getElementById('drawer-slot');
  if (slot) slot.innerHTML = '';
}
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeDrawer(); });

// ============== PRINT ==============
async function printSelected(){
  if (selected.size === 0) return;
  const ksrs = [...selected];
  const total = ksrs.length;
  if (!confirm(`Открыть ${total} PDF-дел для печати?\nБраузер поочерёдно откроет вкладки — для каждой нажмите Ctrl+P.`)) return;
  for (const ksr of ksrs){
    const w = window.open(`/cases/${ksr}/pdf`, `print-${ksr}`);
    if (!w){
      alert('Браузер заблокировал попап. Разрешите всплывающие окна.');
      break;
    }
    // Небольшая пауза между вкладками
    await new Promise(r => setTimeout(r, 700));
  }
  setTimeout(() => htmx.trigger('body','cases-changed'), 1500);
}

// ============== SLOTS EDITOR ==============
function initSlotsSortable(){
  const el = document.getElementById('slots-editor');
  if (!el || el.dataset.sorted) return;
  if (typeof Sortable === 'undefined') return;
  Sortable.create(el, {handle:'.drag-handle', animation:150});
  el.dataset.sorted = '1';
}

function addSlot(){
  const el = document.getElementById('slots-editor');
  const idx = el.children.length + 1;
  const id = 'slot_' + Math.random().toString(36).slice(2,8);
  const html = `
    <div class="slot-editor" data-id="${id}">
      <span class="drag-handle">≡</span>
      <span class="slot-num">${String(idx).padStart(2,'0')}</span>
      <input class="input name-input" value="Новый слот" data-field="name">
      <input class="input mask-input" value="" data-field="mask" placeholder="подстрока или /regex/">
      <label class="switch"><input type="checkbox" data-field="required"><span class="slider"></span></label>
      <span style="font-size:12px;color:var(--text-muted);width:90px">Опционально</span>
      <button type="button" class="icon-btn sm" title="Удалить" onclick="removeSlot(this)">🗑</button>
    </div>`;
  el.insertAdjacentHTML('beforeend', html);
}
function removeSlot(btn){
  btn.closest('.slot-editor').remove();
}

// Собираем slots_json перед отправкой формы
document.body.addEventListener('htmx:configRequest', (e) => {
  if (e.detail.path === '/settings/slots'){
    const el = document.getElementById('slots-editor');
    const slots = [];
    el.querySelectorAll('.slot-editor').forEach(row => {
      const id = row.dataset.id;
      const name = row.querySelector('[data-field="name"]').value.trim();
      const mask = row.querySelector('[data-field="mask"]').value.trim();
      const required = row.querySelector('[data-field="required"]').checked;
      const isCatchAll = mask === '*';
      slots.push({id, name, mask, required, is_catch_all: isCatchAll});
    });
    e.detail.parameters['slots_json'] = JSON.stringify(slots);
  }
});

// Init sortable on load and after any settings-panel swap
document.body.addEventListener('htmx:afterSwap', () => { initSlotsSortable(); });
document.addEventListener('DOMContentLoaded', initSlotsSortable);

// Toast-подобное уведомление при сохранении
document.body.addEventListener('settings-saved', () => {
  const t = document.createElement('div');
  t.textContent = '✓ Сохранено';
  t.style.cssText = 'position:fixed;bottom:24px;left:50%;transform:translateX(-50%);background:var(--ok);color:#fff;padding:10px 18px;border-radius:10px;box-shadow:var(--shadow-lg);z-index:70;font-size:13px;font-weight:500';
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 1800);
});

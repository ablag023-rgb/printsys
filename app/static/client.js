/* Печать: работает только внутри клиента печати.
 *
 * В обычном браузере этого кода как будто нет: `window.pywebview` отсутствует,
 * элементы с классом .client-only остаются скрытыми. Так один и тот же шаблон
 * обслуживает и веб, и клиент — второго набора страниц не заводится.
 *
 * Браузер физически не может печатать пакет: ему недоступны принтер оператора,
 * установленный Excel и очередь печати на его машине. Всё это делает Python
 * на стороне клиента, страница лишь вызывает его через window.pywebview.api.
 */
(function () {
  'use strict';

  let api = null;          // window.pywebview.api, когда доступен
  let pollTimer = null;

  function el(id) { return document.getElementById(id); }

  // ============== обнаружение клиента ==============

  function onClientReady() {
    api = window.pywebview.api;
    api.hello().then((info) => {
      document.body.classList.add('in-client');
      window.__printsysClient = info;
      fillPrinters(info);
      refreshQueueBadge();
    }).catch((e) => console.warn('клиент не ответил:', e));
  }

  if (window.pywebview && window.pywebview.api) {
    onClientReady();
  } else {
    window.addEventListener('pywebviewready', onClientReady);
  }

  // ============== печать выбранных ==============

  function selectedKsrs() {
    return Array.from(document.querySelectorAll('.row-cb:checked'))
      .map((x) => x.dataset.ksr);
  }

  window.printSelected = function () {
    if (!api) return;
    const ksrs = selectedKsrs();
    if (!ksrs.length) return;
    const info = window.__printsysClient || {};
    const printer = (info.settings || {}).printer || '(по умолчанию)';
    if (!confirm('Отправить на печать: ' + ksrs.length + '\nПринтер: ' + printer)) return;

    openProgress(ksrs.length);
    api.print_cases(ksrs).then((r) => {
      if (!r.ok) { addLine('Не удалось начать печать: ' + r.error); finishProgress(); return; }
      addLine('Принтер: ' + r.printer);
      startPolling();
    });
  };

  window.resumeBatch = function () {
    if (!api) return;
    openProgress(0);
    api.resume_batch().then((r) => {
      if (!r.ok) { addLine(r.error); finishProgress(); return; }
      el('pp-total').textContent = r.total;
      startPolling();
    });
  };

  // ============== окно хода печати ==============

  function openProgress(total) {
    el('print-progress').classList.add('show');
    el('pp-total').textContent = total || '?';
    el('pp-done').textContent = '0';
    el('pp-log').textContent = '';
    el('pp-stop').disabled = false;
    el('pp-close').disabled = true;
  }

  function addLine(text) {
    const log = el('pp-log');
    log.textContent += text + '\n';
    log.scrollTop = log.scrollHeight;
  }

  let shownLines = 0;

  function startPolling() {
    shownLines = 0;
    clearInterval(pollTimer);
    pollTimer = setInterval(() => {
      api.print_status().then((st) => {
        for (let i = shownLines; i < st.lines.length; i++) addLine(st.lines[i]);
        shownLines = st.lines.length;
        el('pp-done').textContent = st.done;
        if (st.total) el('pp-total').textContent = st.total;
        if (!st.running) { reportSummary(st.summary); finishProgress(); }
      });
    }, 600);
  }

  function reportSummary(s) {
    if (!s) return;
    addLine('');
    addLine('Передано на принтер: ' + (s.done || 0));
    (s.failed || []).forEach((f) => addLine('  ! ' + f.ksr + ': ' + f.message));
    if ((s.ambiguous || []).length) {
      addLine('Требуют решения оператора: ' + s.ambiguous.join(', ') +
              ' — откройте «Очередь печати»');
    }
    if (s.paused) {
      addLine('ПАКЕТ ОСТАНОВЛЕН: ' + s.pause_reason);
      addLine('Устраните причину и нажмите «Продолжить пакет» в очереди печати.');
    }
  }

  function finishProgress() {
    clearInterval(pollTimer);
    pollTimer = null;
    el('pp-stop').disabled = true;
    el('pp-close').disabled = false;
    refreshQueueBadge();
    if (window.htmx) htmx.trigger('body', 'cases-changed');
  }

  window.stopPrint = function () {
    if (api) api.stop_print();
    el('pp-stop').disabled = true;
  };

  window.closeProgress = function () {
    el('print-progress').classList.remove('show');
  };

  // ============== состав дела ==============

  window.previewCase = function (ksr) {
    if (!api) return;
    const box = el('preview-box');
    el('preview-modal').classList.add('show');
    box.innerHTML = '<p class="muted">Скачиваем и конвертируем документы…</p>';
    api.preview(ksr).then((r) => {
      if (!r.ok) { box.innerHTML = '<p class="err">' + r.error + '</p>'; return; }
      let html = '<table class="mini"><thead><tr><th>#</th><th>Слот</th>' +
                 '<th>Листов</th><th>Документ</th></tr></thead><tbody>';
      r.docs.forEach((d, i) => {
        html += '<tr><td>' + (i + 1) + '</td><td>' + esc(d.slot) + '</td><td>' +
                d.pages + '</td><td>' + esc(d.name) +
                (d.stub ? ' <span class="err">(заглушка)</span>' : '') + '</td></tr>';
      });
      html += '</tbody></table><p><b>Всего листов: ' + r.total_pages + '</b></p>';
      if (r.skipped && r.skipped.length) {
        html += '<p class="err">Не приложено: ' + esc(r.skipped.join('; ')) + '</p>';
      }
      box.innerHTML = html;
    });
  };

  // ============== очередь печати ==============

  window.openQueue = function () {
    if (!api) return;
    el('queue-modal').classList.add('show');
    loadQueue();
  };

  function loadQueue() {
    api.queue_list().then((r) => {
      const box = el('queue-box');
      el('q-resume').disabled = !(r.batches && r.batches.length);
      if (!r.jobs.length) {
        box.innerHTML = '<p class="muted">Очередь пуста, незавершённых пакетов нет.</p>';
        return;
      }
      let html = '';
      if (r.recovered) {
        html += '<p class="warn">После сбоя разобрано заданий: ' + r.recovered + '</p>';
      }
      html += '<table class="mini"><thead><tr><th>КСР</th><th>Состояние</th>' +
              '<th>Листов</th><th>Отчёт</th><th>Пояснение</th><th></th>' +
              '</tr></thead><tbody>';
      r.jobs.forEach((j) => {
        const amb = j.state === 'AMBIGUOUS';
        html += '<tr class="' + stateClass(j.state) + '"><td>' + esc(j.ksr) + '</td><td>' +
                esc(stateLabel(j.state)) + '</td><td>' + j.pages + '</td><td>' +
                (j.reported ? 'да' : 'нет') + '</td><td>' + esc(j.message || '') + '</td><td>';
        if (amb) {
          html += '<button class="btn tiny" onclick="resolveJob(\'' + esc(j.ksr) +
                  '\',\'reprint\')">Печатать заново</button> ' +
                  '<button class="btn tiny" onclick="resolveJob(\'' + esc(j.ksr) +
                  '\',\'skip\')">Считать напечатанным</button>';
        }
        html += '</td></tr>';
      });
      box.innerHTML = html + '</tbody></table>';
    });
  }

  const STATES = {
    QUEUED: 'в очереди', SENDING: 'отправляется', SPOOLED: 'в очереди принтера',
    SENT: 'передано на принтер', BLOCKED: 'принтер сообщил об ошибке',
    FAILED: 'ошибка', AMBIGUOUS: 'требует решения',
    SKIPPED: 'помечено напечатанным', CANCELLED: 'отменено',
  };
  function stateLabel(s) { return STATES[s] || s; }
  function stateClass(s) {
    if (s === 'AMBIGUOUS' || s === 'BLOCKED' || s === 'FAILED') return 'row-bad';
    if (s === 'SENT' || s === 'SKIPPED') return 'row-done';
    return '';
  }

  window.resolveJob = function (ksr, action) {
    const msg = action === 'reprint'
      ? 'Дело ' + ksr + ' будет напечатано заново.\nВ лотке уже может лежать копия. Продолжить?'
      : 'Дело ' + ksr + ' будет помечено напечатанным без печати.\nУбедитесь, что бумага вышла. Продолжить?';
    if (!confirm(msg)) return;
    api.queue_resolve(ksr, action).then(loadQueue);
  };

  window.purgeQueue = function () {
    if (!confirm('Удалить завершённые записи старше 30 дней?\nНезавершённые пакеты не тронем.')) return;
    api.queue_purge().then(loadQueue);
  };

  window.closeQueue = function () { el('queue-modal').classList.remove('show'); };
  window.closePreview = function () { el('preview-modal').classList.remove('show'); };

  function refreshQueueBadge() {
    if (!api) return;
    api.queue_list().then((r) => {
      const amb = r.jobs.filter((j) => j.state === 'AMBIGUOUS').length;
      const badge = el('queue-badge');
      if (!badge) return;
      badge.textContent = amb ? String(amb) : '';
      badge.classList.toggle('show', amb > 0);
    });
  }

  // ============== настройки рабочего места ==============

  function fillPrinters(info) {
    const sel = el('client-printer');
    if (!sel) return;
    sel.innerHTML = '';
    (info.printers || []).forEach((name) => {
      const o = document.createElement('option');
      o.value = name; o.textContent = name;
      if (name === (info.settings || {}).printer) o.selected = true;
      sel.appendChild(o);
    });
    const s = info.settings || {};
    if (el('client-copies')) el('client-copies').value = s.copies || 1;
    if (el('client-duplex')) el('client-duplex').value = s.duplex || 1;
    if (el('client-window')) el('client-window').value = s.print_window || 3;
  }

  window.saveClientSettings = function () {
    if (!api) return;
    api.save_settings({
      printer: el('client-printer').value,
      copies: parseInt(el('client-copies').value, 10),
      duplex: parseInt(el('client-duplex').value, 10),
      print_window: parseInt(el('client-window').value, 10),
      slot_trays: (window.__printsysClient.settings || {}).slot_trays || {},
    }).then(() => {
      window.__printsysClient.settings.printer = el('client-printer').value;
      const n = el('client-saved');
      if (n) { n.classList.add('show'); setTimeout(() => n.classList.remove('show'), 2000); }
    });
  };

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }
})();

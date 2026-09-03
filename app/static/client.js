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
    // Признак клиента ставим сразу: если мост ответит ошибкой, кнопки печати
    // должны остаться видимыми, а оператор — увидеть причину, а не пустой экран
    document.body.classList.add('in-client');
    api.hello().then((info) => {
      window.__printsysClient = info;
      fillPrinters(info);
      refreshQueueBadge();
    }).catch((e) => {
      console.warn('клиент не ответил:', e);
      alert('Не удалось получить сведения о рабочем месте:\n' + e +
            '\nПечать может быть недоступна.');
    });
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
    var qb = el('pp-queue');
    if (qb) qb.style.display = 'none';
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
      // Курсор, а не абсолютный индекс: Python отдаёт строки начиная с него,
      // иначе журнал замолкал после первых двух сотен строк
      api.print_status(shownLines).then((st) => {
        (st.lines || []).forEach(addLine);
        shownLines = st.next != null ? st.next : shownLines + (st.lines || []).length;
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
    var needQueue = (s.ambiguous || []).length > 0;
    (s.already_queued || []).forEach((a) => {
      addLine('  = ' + a.ksr + ': ' + (a.reason || 'пропущено'));
      if (a.state === 'AMBIGUOUS') needQueue = true;
    });
    // Кнопка появляется ровно тогда, когда без очереди дальше не пройти
    var qb = el('pp-queue');
    if (qb) qb.style.display = needQueue ? '' : 'none';
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

  // ============== отдельный документ ==============

  window.openDocument = function (storageId, key, name) {
    if (!api) return;
    api.open_document(storageId, key, name).then((r) => {
      if (!r.ok) alert('Не удалось открыть документ: ' + r.error);
    });
  };

  window.printDocument = function (storageId, key, name, etag) {
    if (!api) return;
    const info = window.__printsysClient || {};
    const printer = (info.settings || {}).printer || '(по умолчанию)';
    if (!confirm('Напечатать отдельный документ?\n' + name +
                 '\nПринтер: ' + printer +
                 '\n\nСквозная нумерация КСР/NN не наносится — это допечатка ' +
                 'вне сшитого пакета, и статус дела не изменится.')) return;
    api.print_document(storageId, key, name, etag || '').then((r) => {
      alert(r.ok ? ('Отправлено на принтер: ' + r.pages + ' л.')
                 : ('Не удалось напечатать: ' + r.error));
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
        // Кнопки нужны не только «требует решения»: задание могло остаться
        // «отправляется»/«в очереди принтера» после сбоя внутри печати. Такие
        // строки восстановление не разбирает (владелец — живой процесс), и без
        // ручного выхода дело блокировало повторную печать до перезапуска.
        // Окно очереди открывается только когда печать не идёт, так что живое
        // задание сюда не попадёт.
        const amb = ['AMBIGUOUS', 'SENDING', 'SPOOLED'].indexOf(j.state) >= 0;
        html += '<tr class="' + stateClass(j.state) + '"><td>' + esc(j.ksr) + '</td><td>' +
                esc(stateLabel(j.state)) + '</td><td>' + j.pages + '</td><td>' +
                (j.reported ? 'да' : 'нет') + '</td><td>' + esc(j.message || '') + '</td><td>';
        if (amb) {
          // Адресуемся по id ЗАДАНИЯ: одно дело может иметь строки в разных
          // пакетах, и решение по одной не должно поднимать чужую
          html += '<button class="btn tiny" onclick="resolveJob(' + j.id +
                  ',\'' + esc(j.ksr) + '\',\'reprint\')">Печатать заново</button> ' +
                  '<button class="btn tiny" onclick="resolveJob(' + j.id +
                  ',\'' + esc(j.ksr) + '\',\'skip\')">Считать напечатанным</button>';
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

  window.resolveJob = function (jobId, ksr, action) {
    const msg = action === 'reprint'
      ? 'Дело ' + ksr + ' будет напечатано заново.\nВ лотке уже может лежать копия. Продолжить?'
      : 'Дело ' + ksr + ' будет помечено напечатанным без печати.\nУбедитесь, что бумага вышла. Продолжить?';
    if (!confirm(msg)) return;
    api.queue_resolve(jobId, action).then(loadQueue);
  };

  window.cancelQueue = function () {
    if (!api) return;
    if (!confirm('Снять с печати все незавершённые дела?\n\n' +
                 'Снимутся и те, что ждут решения: если такое дело всё же ' +
                 'напечаталось, оно останется помеченным как ненапечатанное ' +
                 'и его можно будет напечатать снова.\n\n' +
                 'Уже отправленные в принтер листы не отменятся — они у него ' +
                 'в очереди.')) return;
    // Снимаем и «требует решения»: иначе очередь не очищалась — такие строки
    // не терминальные, и пакет продолжал числиться незавершённым
    api.queue_cancel('', true).then((r) => {
      if (!r.ok) { alert(r.error); return; }
      if (r.left) {
        alert('Снято дел: ' + r.cancelled + '.\nОсталось в очереди: ' + r.left +
              ' — это уже отправленные в принтер, отменить их отсюда нельзя.');
      }
      loadQueue();
    });
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

  // Вкладка настроек приезжает по HTMX уже после запуска client.js, поэтому
  // заполняем поля ещё и после каждой подстановки разметки
  document.body.addEventListener('htmx:afterSwap', () => {
    // Заполняем ТОЛЬКО пустой список: обновление таблицы дел приходит тем же
    // событием и раньше сбрасывало несохранённый выбор принтера и лотков
    const sel = el('client-printer');
    if (api && window.__printsysClient && sel && !sel.options.length) {
      fillPrinters(window.__printsysClient);
    }
  });

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
    if (el('client-quality')) el('client-quality').value = s.print_quality || 'normal';
    showCache(info.cache);
    const trays = s.slot_trays || {};
    document.querySelectorAll('.client-tray').forEach((inp) => {
      inp.value = trays[inp.dataset.slot] != null ? trays[inp.dataset.slot] : '';
    });
  }

  function showCache(c) {
    const n = el('client-cache');
    if (n && c) n.textContent = c.files + ' док., ' + c.mb + ' МБ';
  }

  window.clearPdfCache = function () {
    if (!api) return;
    if (!confirm('Удалить готовые PDF?\nСледующая печать снова потратит время ' +
                 'на конвертацию через Excel.')) return;
    api.cache_clear().then(() => api.cache_info()).then((c) => {
      if (window.__printsysClient) window.__printsysClient.cache = c;
      showCache(c);
    });
  };

  window.saveClientSettings = function () {
    if (!api) return;
    const trays = {};
    let bad = null;
    document.querySelectorAll('.client-tray').forEach((inp) => {
      const v = inp.value.trim();
      if (!v) return;
      if (!/^\d+$/.test(v)) { bad = v; return; }
      trays[inp.dataset.slot] = parseInt(v, 10);
    });
    if (bad !== null) { alert('Номер лотка должен быть числом: «' + bad + '»'); return; }
    const data = {
      printer: el('client-printer').value,
      copies: parseInt(el('client-copies').value, 10),
      duplex: parseInt(el('client-duplex').value, 10),
      print_window: parseInt(el('client-window').value, 10),
      print_quality: el('client-quality') ? el('client-quality').value : 'normal',
      slot_trays: trays,
    };
    api.save_settings(data).then((r) => {
      if (!r.ok) { alert('Не удалось сохранить: ' + (r.error || '')); return; }
      window.__printsysClient.settings = Object.assign(
        window.__printsysClient.settings || {}, data);
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

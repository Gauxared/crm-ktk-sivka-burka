(function () {
  'use strict';

  const app = document.querySelector('#app');
  const session = { csrfToken: null, ownerId: null };
  const s = {
    f: { status: '', has_visit: '', channel: '' },
    cursor: null,
    next: null,
    calendar: { year: null, month: null, timezone: null }
  };
  const rub = new Intl.NumberFormat('ru-RU', {
    style: 'currency', currency: 'RUB', maximumFractionDigits: 0
  });
  const zoneFormatters = new Map();
  let viewVersion = 0;
  let inquiryRequest = 0;

  const esc = value => String(value == null ? '—' : value).replace(
    /[&<>"']/g,
    character => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[character])
  );
  const msg = (text, kind) => '<p class="message ' + (kind || '') + '" role="alert">' + text + '</p>';
  const read = response => response.json().catch(() => ({}));
  const apiData = payload => payload && payload.data && typeof payload.data === 'object'
    ? payload.data : payload;

  function sessionRequest(method, body, csrf) {
    const headers = Object.assign(
      { 'Content-Type': 'application/json', 'X-Requested-With': 'crm' },
      csrf ? { 'X-CSRF-Token': csrf } : {}
    );
    return fetch('/api/v1/admin/session', {
      method,
      credentials: 'include',
      headers,
      body: body === undefined ? undefined : JSON.stringify(body)
    });
  }

  function login(notice) {
    viewVersion += 1;
    app.innerHTML = '<section class="card login">' +
      '<p class="eyebrow">КТК «Сивка-Бурка»</p><h1>Вход в CRM</h1>' + (notice || '') +
      '<form id="login-form">' +
      '<label for="login">Логин</label><input id="login" name="login" autocomplete="username" required>' +
      '<label for="password">Пароль</label><input id="password" name="password" type="password" autocomplete="current-password" required>' +
      '<button>Войти</button></form><div id="login-status" aria-live="polite"></div></section>';
    document.querySelector('#login-form').onsubmit = signIn;
  }

  async function signIn(event) {
    event.preventDefault();
    const form = event.target;
    try {
      const response = await sessionRequest('POST', {
        login: form.login.value,
        password: form.password.value
      });
      if (!response.ok) {
        login(msg(response.status === 401
          ? 'Не удалось войти. Проверьте логин и пароль.'
          : 'Сервис временно недоступен.', 'error'));
        return;
      }
      const data = apiData(await read(response));
      session.csrfToken = data.csrf_token;
      session.ownerId = data.owner_id;
      shell();
    } catch (_) {
      login(msg('Сервис временно недоступен. Проверьте соединение.', 'error'));
    }
  }

  async function boot() {
    try {
      const response = await sessionRequest('GET');
      if (response.status === 401) {
        login();
        return;
      }
      if (!response.ok) {
        login(msg('Не удалось проверить сессию. Повторите попытку.', 'error'));
        return;
      }
      const data = apiData(await read(response));
      session.csrfToken = data.csrf_token;
      session.ownerId = data.owner_id;
      shell();
    } catch (_) {
      login(msg('Сервис временно недоступен. Проверьте соединение.', 'error'));
    }
  }

  function shell() {
    app.innerHTML = '<div class="shell"><header><div>' +
      '<p class="eyebrow">CRM владельца</p><h1>Сивка-Бурка</h1></div>' +
      '<button id="logout" class="secondary">Выйти</button></header>' +
      '<nav aria-label="Основная навигация">' +
      '<a href="#inquiries" data-route="inquiries">Заявки</a>' +
      '<a href="#calendar" data-route="calendar">Календарь</a></nav>' +
      '<main id="content" aria-live="polite"></main></div>';
    document.querySelector('#logout').onclick = signOut;
    window.onhashchange = route;
    if (location.hash !== '#calendar' && location.hash !== '#inquiries') {
      history.replaceState(null, '', '#inquiries');
    }
    route();
  }

  function route() {
    const name = location.hash === '#calendar' ? 'calendar' : 'inquiries';
    document.querySelectorAll('[data-route]').forEach(link => {
      if (link.dataset.route === name) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    });
    if (name === 'calendar') showCalendar();
    else showInquiryList();
  }

  function inquiryParams() {
    const params = new URLSearchParams();
    Object.keys(s.f).forEach(key => { if (s.f[key]) params.set(key, s.f[key]); });
    if (s.cursor) params.set('cursor', s.cursor);
    params.set('limit', '50');
    return params;
  }

  function showInquiryList() {
    viewVersion += 1;
    document.querySelector('#content').innerHTML = '<section class="card"><h2>Заявки</h2>' +
      '<div class="filters">' +
      '<label>Статус<select id="status"><option value="">Все</option><option>NEW</option>' +
      '<option>NEGOTIATING</option><option>CONFIRMED</option><option>COMPLETED</option><option>CANCELLED</option></select></label>' +
      '<label>Визит<select id="has_visit"><option value="">Все</option><option value="true">Есть</option><option value="false">Нет</option></select></label>' +
      '<label>Канал<select id="channel"><option value="">Все</option><option>TELEGRAM</option><option>VK</option>' +
      '<option>PHONE</option><option>WHATSAPP</option><option>OTHER</option></select></label></div>' +
      '<div id="state">Загрузка…</div><div id="pager"></div></section>';
    Object.keys(s.f).forEach(key => {
      const field = document.querySelector('#' + key);
      field.value = s.f[key];
      field.onchange = () => {
        s.f[key] = field.value;
        s.cursor=null;
        loadInquiries();
      };
    });
    loadInquiries();
  }

  function responseError(response, subject) {
    if (response.status === 401) {
      login(msg('Сессия истекла. Войдите снова.', 'error'));
      return '';
    }
    if (response.status === 403) return msg('Недостаточно прав для просмотра ' + subject + '.', 'error');
    if (response.status === 404) return msg(subject === 'заявок' ? 'Заявка не найдена.' : 'Прогулка не найдена.', 'error');
    if (response.status === 422) return msg('Сервер отклонил диапазон календаря. Выберите другой месяц.', 'error');
    return msg('Не удалось загрузить данные. Попробуйте ещё раз.', 'error');
  }

  async function loadInquiries() {
    const request = ++inquiryRequest;
    const state = document.querySelector('#state');
    if (!state) return;
    state.textContent = 'Загрузка…';
    try {
      const response = await fetch('/api/v1/admin/inquiries?' + inquiryParams(), { credentials:'include' });
      if (request !== inquiryRequest || location.hash === '#calendar') return;
      if (!response.ok) {
        state.innerHTML = responseError(response, 'заявок');
        return;
      }
      const data = apiData(await read(response)) || {};
      const items = Array.isArray(data.items) ? data.items : [];
      s.next = data.next_cursor || null;
      state.innerHTML = items.length
        ? '<div class="inquiry-grid">' + items.map(inquiryCard).join('') + '</div>'
        : '<p class="muted">Заявок нет.</p>';
      document.querySelectorAll('.open-inquiry').forEach(button => {
        button.onclick = () => showInquiryDetail(button.dataset.id);
      });
      const pager = document.querySelector('#pager');
      pager.innerHTML = s.next ? '<button id="next-inquiries">Следующая страница</button>' : '';
      if (s.next) document.querySelector('#next-inquiries').onclick = () => {
        s.cursor = s.next;
        loadInquiries();
      };
    } catch (_) {
      if (request === inquiryRequest && state.isConnected) {
        state.innerHTML = msg('Не удалось загрузить заявки. Проверьте соединение.', 'error');
      }
    }
  }

  function inquiryCard(item) {
    const requested = item.requested_time && (item.requested_time.time_text || item.requested_time.date);
    return '<article class="inquiry"><button class="open open-inquiry" data-id="' + esc(item.id) + '">' +
      '<strong>' + esc(item.requester_name) + '</strong></button><dl>' +
      '<dt>Статус</dt><dd>' + esc(item.status) + '</dd>' +
      '<dt>Услуга</dt><dd>' + esc(item.service_title) + '</dd>' +
      '<dt>Время</dt><dd>' + esc(requested) + '</dd>' +
      '<dt>Канал</dt><dd>' + esc(item.channel) + '</dd></dl></article>';
  }

  async function showInquiryDetail(id) {
    viewVersion += 1;
    const content = document.querySelector('#content');
    content.innerHTML = '<section class="card"><button id="back" class="secondary">← К списку</button><p>Загрузка…</p></section>';
    document.querySelector('#back').onclick = showInquiryList;
    try {
      const response = await fetch('/api/v1/admin/inquiries/' + encodeURIComponent(id), { credentials:'include' });
      if (!response.ok) {
        content.querySelector('.card').insertAdjacentHTML('beforeend', responseError(response, 'заявок'));
        return;
      }
      const item = apiData(await read(response));
      content.innerHTML = '<section class="card detail"><button id="back" class="secondary">← К списку</button>' +
        '<h2>Заявка ' + esc(item.id) + '</h2><dl>' +
        '<dt>Контакт</dt><dd>' + esc(item.current_contact && item.current_contact.value) + '</dd>' +
        '<dt>Пожелание времени</dt><dd>' + esc(item.requested_time && item.requested_time.date) + '</dd>' +
        '<dt>Услуга и условия</dt><dd>' + esc(item.service_title) + ' · ' + esc(item.agreed_terms && item.agreed_terms.note) + '</dd>' +
        '<dt>Участие / визит</dt><dd>' + esc(item.participation && item.participation.visit_id) + '</dd>' +
        '<dt>Деньги</dt><dd>' + formatMinor(item.cash_summary && item.cash_summary.net_received_minor) + '</dd>' +
        '<dt>Заметки</dt><dd>' + esc(item.owner_note || item.comment) + '</dd>' +
        '<dt>Уведомления</dt><dd>' + esc(item.notification_summary && item.notification_summary.status) + '</dd>' +
        '</dl></section>';
      document.querySelector('#back').onclick = showInquiryList;
    } catch (_) {
      content.innerHTML = '<section class="card">' +
        '<button id="back" class="secondary">← К списку</button>' +
        msg('Не удалось загрузить заявку. Проверьте соединение.', 'error') + '</section>';
      document.querySelector('#back').onclick = showInquiryList;
    }
  }

  function formatMinor(value) {
    return Number.isInteger(value) ? esc(rub.format(value / 100)) : '—';
  }

  async function clubTimezone() {
    if (s.calendar.timezone) return s.calendar.timezone;
    const response = await fetch('/api/v1/public/catalog');
    if (!response.ok) throw new Error('CATALOG_UNAVAILABLE');
    const data = apiData(await read(response)) || {};
    const timezone = data.club_timezone;
    if (typeof timezone !== 'string' || !timezone) throw new Error('TIMEZONE_UNAVAILABLE');
    try {
      new Intl.DateTimeFormat('ru-RU', { timeZone: timezone }).format(new Date());
    } catch (_) {
      throw new Error('TIMEZONE_INVALID');
    }
    s.calendar.timezone = timezone;
    return timezone;
  }

  function zoneFormatter(timezone) {
    if (!zoneFormatters.has(timezone)) {
      zoneFormatters.set(timezone, new Intl.DateTimeFormat('en-CA', {
        timeZone: timezone,
        year: 'numeric', month: '2-digit', day: '2-digit',
        hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23'
      }));
    }
    return zoneFormatters.get(timezone);
  }

  function zoneParts(value, timezone) {
    const parts = {};
    zoneFormatter(timezone).formatToParts(new Date(value)).forEach(part => {
      if (part.type !== 'literal') parts[part.type] = Number(part.value);
    });
    return parts;
  }

  function dateKey(parts) {
    return parts.year * 10000 + parts.month * 100 + parts.day;
  }

  function startOfClubDate(year, month, day, timezone) {
    const target = year * 10000 + month * 100 + day;
    const anchor = Date.UTC(year, month - 1, day);
    let low = anchor - 36 * 60 * 60 * 1000;
    let high = anchor + 36 * 60 * 60 * 1000;
    if (dateKey(zoneParts(low, timezone)) >= target || dateKey(zoneParts(high, timezone)) < target) {
      throw new Error('TIMEZONE_BOUNDARY_UNAVAILABLE');
    }
    while (low < high) {
      const middle = Math.floor((low + high) / 2);
      if (dateKey(zoneParts(middle, timezone)) >= target) high = middle;
      else low = middle + 1;
    }
    if (dateKey(zoneParts(low, timezone)) !== target) throw new Error('LOCAL_DATE_UNAVAILABLE');
    return new Date(low).toISOString();
  }

  function shiftedMonth(year, month, delta) {
    const value = new Date(Date.UTC(year, month - 1 + delta, 1));
    return { year: value.getUTCFullYear(), month: value.getUTCMonth() + 1 };
  }

  function currentClubMonth(timezone) {
    const parts = zoneParts(Date.now(), timezone);
    return { year: parts.year, month: parts.month };
  }

  function monthBounds(year, month, timezone) {
    const next = shiftedMonth(year, month, 1);
    return {
      from: startOfClubDate(year, month, 1, timezone),
      to: startOfClubDate(next.year, next.month, 1, timezone)
    };
  }

  async function showCalendar() {
    const token = ++viewVersion;
    const content = document.querySelector('#content');
    content.innerHTML = '<section class="card"><h2>Календарь</h2><p>Загрузка часового пояса клуба…</p></section>';
    try {
      const timezone = await clubTimezone();
      if (token !== viewVersion || location.hash !== '#calendar') return;
      if (s.calendar.year == null || s.calendar.month == null) {
        Object.assign(s.calendar, currentClubMonth(timezone));
      }
      renderCalendarShell(timezone);
      await loadCalendar(timezone, token);
    } catch (_) {
      if (token === viewVersion && content.isConnected) {
        content.innerHTML = '<section class="card"><h2>Календарь</h2>' +
          msg('Не удалось получить настроенный часовой пояс клуба. Календарь временно недоступен.', 'error') +
          '</section>';
      }
    }
  }

  function renderCalendarShell(timezone) {
    const label = new Intl.DateTimeFormat('ru-RU', {
      month: 'long', year: 'numeric', timeZone: 'UTC'
    }).format(new Date(Date.UTC(s.calendar.year, s.calendar.month - 1, 1)));
    const content = document.querySelector('#content');
    content.innerHTML = '<section class="card calendar-card"><div class="calendar-heading">' +
      '<div><p class="eyebrow">Часовой пояс: ' + esc(timezone) + '</p><h2>' + esc(label) + '</h2></div>' +
      '<div class="calendar-controls" aria-label="Навигация по месяцам">' +
      '<button id="previous-month" class="secondary" aria-label="Предыдущий месяц">←</button>' +
      '<button id="current-month" class="secondary">Текущий месяц</button>' +
      '<button id="next-month" class="secondary" aria-label="Следующий месяц">→</button></div></div>' +
      '<div id="calendar-state" aria-live="polite">Загрузка прогулок…</div></section>';
    document.querySelector('#previous-month').onclick = () => changeMonth(-1);
    document.querySelector('#next-month').onclick = () => changeMonth(1);
    document.querySelector('#current-month').onclick = () => {
      Object.assign(s.calendar, currentClubMonth(timezone));
      showCalendar();
    };
  }

  function changeMonth(delta) {
    Object.assign(s.calendar, shiftedMonth(s.calendar.year, s.calendar.month, delta));
    showCalendar();
  }

  async function visitPages(bounds) {
    const items = [];
    const seen = new Set();
    let cursor = null;
    do {
      const params = new URLSearchParams();
      params.set('from', bounds.from);
      params.set('to', bounds.to);
      params.set('limit', '100');
      if (cursor) params.set('cursor', cursor);
      const response = await fetch('/api/v1/admin/visits?' + params, { credentials:'include' });
      if (!response.ok) {
        const error = new Error('VISITS_UNAVAILABLE');
        error.response = response;
        throw error;
      }
      const data = apiData(await read(response)) || {};
      if (!Array.isArray(data.items)) throw new Error('INVALID_VISIT_LIST');
      items.push(...data.items);
      const next = data.next_cursor || null;
      if (next && seen.has(next)) throw new Error('CURSOR_LOOP');
      if (next) seen.add(next);
      cursor = next;
    } while(cursor);
    return items;
  }

  async function loadCalendar(timezone, token) {
    const state = document.querySelector('#calendar-state');
    try {
      const bounds = monthBounds(s.calendar.year, s.calendar.month, timezone);
      const visits = await visitPages(bounds);
      if (token !== viewVersion || location.hash !== '#calendar') return;
      renderCalendar(visits, timezone);
    } catch (error) {
      if (token !== viewVersion || !state || !state.isConnected) return;
      if (error.response) state.innerHTML = responseError(error.response, 'календаря');
      else state.innerHTML = msg('Не удалось загрузить календарь. Проверьте соединение.', 'error');
    }
  }

  function localDateKey(value, timezone) {
    const parts = zoneParts(value, timezone);
    return parts.year + '-' + String(parts.month).padStart(2, '0') + '-' + String(parts.day).padStart(2, '0');
  }

  function visitTime(value, timezone) {
    try {
      return new Intl.DateTimeFormat('ru-RU', {
        timeZone: timezone, hour: '2-digit', minute: '2-digit'
      }).format(new Date(value));
    } catch (_) {
      return 'Время не указано';
    }
  }

  function visitDateTime(value, timezone) {
    try {
      return new Intl.DateTimeFormat('ru-RU', {
        timeZone: timezone, dateStyle: 'long', timeStyle: 'short'
      }).format(new Date(value));
    } catch (_) {
      return 'Дата и время не указаны';
    }
  }

  function visitCard(visit, timezone, includeDate) {
    const unresolved = visit.has_unresolved_inquiries
      ? '<span class="visit-warning">Есть несогласованные заявки</span>' : '';
    const duration = visit.duration_minutes == null
      ? 'Длительность не указана' : esc(visit.duration_minutes) + ' мин';
    return '<button class="visit-card" data-visit-id="' + esc(visit.id) + '">' +
      '<span class="visit-time">' + esc(includeDate
        ? visitDateTime(visit.start_at, timezone) : visitTime(visit.start_at, timezone)) + '</span>' +
      '<strong>' + esc(visit.service_title) + '</strong>' +
      '<span>' + esc(visit.participants_count) + ' чел. · ' + duration + '</span>' +
      '<span>Статус: ' + esc(visit.status) + '</span>' + unresolved + '</button>';
  }

  function renderCalendar(visits, timezone) {
    const first = new Date(Date.UTC(s.calendar.year, s.calendar.month - 1, 1));
    const days = new Date(Date.UTC(s.calendar.year, s.calendar.month, 0)).getUTCDate();
    const leading = (first.getUTCDay() + 6) % 7;
    const byDay = new Map();
    const outside = [];
    const prefix = s.calendar.year + '-' + String(s.calendar.month).padStart(2, '0') + '-';
    visits.forEach(visit => {
      try {
        const key = localDateKey(visit.start_at, timezone);
        if (!key.startsWith(prefix)) outside.push(visit);
        else {
          if (!byDay.has(key)) byDay.set(key, []);
          byDay.get(key).push(visit);
        }
      } catch (_) {
        outside.push(visit);
      }
    });
    const weekday = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс'];
    let html = '<div class="weekday-row" aria-hidden="true">' +
      weekday.map(day => '<span>' + day + '</span>').join('') + '</div><div class="calendar-grid">';
    for (let blank = 0; blank < leading; blank += 1) html += '<div class="calendar-blank" aria-hidden="true"></div>';
    for (let day = 1; day <= days; day += 1) {
      const key = prefix + String(day).padStart(2, '0');
      const dayVisits = byDay.get(key) || [];
      html += '<section class="calendar-day" aria-label="' + day + ' число, записей: ' + dayVisits.length + '">' +
        '<h3><span>' + day + '</span><small>' + dayVisits.length + '</small></h3>' +
        '<div class="day-visits">' + dayVisits.map(visit => visitCard(visit, timezone)).join('') + '</div></section>';
    }
    html += '</div>';
    if (!visits.length) html += '<p class="muted calendar-empty">В этом месяце прогулок нет.</p>';
    if (outside.length) {
      html += '<section class="cross-month"><h3>Прогулки, пересекающие границу месяца</h3>' +
        outside.map(visit => visitCard(visit, timezone, true)).join('') + '</section>';
    }
    const state = document.querySelector('#calendar-state');
    state.innerHTML = html;
    state.querySelectorAll('[data-visit-id]').forEach(button => {
      button.onclick = () => showVisitDetail(button.dataset.visitId, timezone);
    });
  }

  async function showVisitDetail(id, timezone) {
    const token = ++viewVersion;
    const content = document.querySelector('#content');
    content.innerHTML = '<section class="card"><button id="back-calendar" class="secondary">← К календарю</button>' +
      '<p>Загрузка прогулки…</p></section>';
    document.querySelector('#back-calendar').onclick = showCalendar;
    try {
      const response = await fetch('/api/v1/admin/visits/' + encodeURIComponent(id), { credentials:'include' });
      if (token !== viewVersion) return;
      if (!response.ok) {
        content.querySelector('.card').insertAdjacentHTML('beforeend', responseError(response, 'прогулки'));
        return;
      }
      const visit = apiData(await read(response));
      const participations = Array.isArray(visit.participations) ? visit.participations : [];
      content.innerHTML = '<section class="card detail visit-detail">' +
        '<button id="back-calendar" class="secondary">← К календарю</button>' +
        '<p class="eyebrow">' + esc(visitTime(visit.start_at, timezone)) + ' · ' + esc(timezone) + '</p>' +
        '<h2>' + esc(visit.service_title) + '</h2><dl>' +
        '<dt>Статус</dt><dd>' + esc(visit.status) + '</dd>' +
        '<dt>Начало</dt><dd>' + esc(visitDateTime(visit.start_at, timezone)) + '</dd>' +
        '<dt>Длительность</dt><dd>' + (visit.duration_minutes == null ? 'Не указана' : esc(visit.duration_minutes) + ' мин') + '</dd>' +
        '<dt>Участников</dt><dd>' + esc(visit.participants_count) + '</dd></dl>' +
        '<h3>Заявки в общей прогулке</h3>' +
        (participations.length ? '<div class="participation-list">' + participations.map(participationCard).join('') + '</div>'
          : '<p class="muted">Актуальных участников нет.</p>') + '</section>';
      document.querySelector('#back-calendar').onclick = showCalendar;
    } catch (_) {
      if (token !== viewVersion) return;
      content.innerHTML = '<section class="card"><button id="back-calendar" class="secondary">← К календарю</button>' +
        msg('Не удалось загрузить прогулку. Проверьте соединение.', 'error') + '</section>';
      document.querySelector('#back-calendar').onclick = showCalendar;
    }
  }

  function participationCard(item) {
    const cash = item.cash_summary || {};
    return '<article class="participation"><h4>' + esc(item.requester_name) + '</h4><dl>' +
      '<dt>Статус заявки</dt><dd>' + esc(item.status) + '</dd>' +
      '<dt>Участников</dt><dd>' + esc(item.participants_count) + '</dd>' +
      '<dt>Получено</dt><dd>' + formatMinor(cash.net_received_minor) + '</dd>' +
      '<dt>Остаток</dt><dd>' + formatMinor(cash.balance_minor) + '</dd></dl></article>';
  }

  async function signOut() {
    try {
      const response = await sessionRequest('DELETE', undefined, session.csrfToken);
      if (!response.ok && response.status >= 500) return;
      session.csrfToken = null;
      session.ownerId = null;
      s.calendar.timezone = null;
      login(msg('Вы вышли из CRM.', 'success'));
    } catch (_) {
      // Keep the current authenticated view when the outcome is unknown.
    }
  }

  boot();
}());

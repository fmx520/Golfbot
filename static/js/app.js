const S = {
  step: 1,
  connected: false,
  baseUrl: '',
  scheduleId: '',
  courseId: '',
  username: '',
  jobId: null,
  pollTimer: null,
  slots: [],
  selectedSlot: null,
  jobStatus: null,
  jobEvents: [],
  sniperMode: false,
  catalog: [],
  preflight: null,
  courseKey: '',
  overrideBadLink: false,
};

const CUSTOM_COURSE = '__custom__';

function $(id) { return document.getElementById(id); }

function raceStrategy() {
  return document.querySelector('input[name="raceStrategy"]:checked')?.value || 'off';
}

function show(id) { $(id)?.classList.remove('hidden'); }
function hide(id) { $(id)?.classList.add('hidden'); }

function esc(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

function fmt12(t) {
  if (!t) return 'N/A';
  const [h, m] = t.split(':').map(Number);
  const ampm = h >= 12 ? 'PM' : 'AM';
  const hr = h % 12 || 12;
  return `${hr}:${String(m).padStart(2, '0')} ${ampm}`;
}

function showAlert(id, msg) {
  const el = $(id);
  if (!el) return;
  el.textContent = msg;
  el.classList.add('show');
  setTimeout(() => el.classList.remove('show'), 8000);
}

function hideAlert(id) {
  $(id)?.classList.remove('show');
}

async function api(path, opts) {
  const r = await fetch('/api' + path, opts);
  const j = await r.json();
  if (!r.ok) throw new Error(j.detail || j.error || `HTTP ${r.status}`);
  return j;
}

function goToStep(n) {
  S.step = n;
  document.querySelectorAll('.step-content').forEach(el => el.classList.remove('active'));
  const target = $('step' + n);
  if (target) target.classList.add('active');

  document.querySelectorAll('.step-circle').forEach(el => {
    const step = parseInt(el.dataset.step);
    el.classList.remove('active', 'completed');
    if (step === n) el.classList.add('active');
    else if (step < n) { el.classList.add('completed'); el.textContent = '✓'; }
    else el.textContent = step;
  });

  document.querySelectorAll('.step-label').forEach(el => {
    const step = parseInt(el.dataset.step);
    el.classList.remove('active', 'completed');
    if (step === n) el.classList.add('active');
    else if (step < n) el.classList.add('completed');
  });

  document.querySelectorAll('.step-line').forEach(el => {
    const after = parseInt(el.dataset.after);
    el.classList.toggle('completed', after < n);
  });
}

// Step 1: Parse booking URL
function parseBookingUrl(url) {
  try {
    const parsed = new URL(url);
    const parts = parsed.pathname.split('/').filter(Boolean);
    const bix = parts.indexOf('booking');
    if (bix >= 0) {
      if (parts[bix + 1]) $('scheduleId').value = parts[bix + 1];
      if (parts[bix + 2]) $('courseId').value = parts[bix + 2];
    }
  } catch {}
}

// Step 1: Course catalog + link preflight
//
// The booking URL used to be typed by hand, which meant a stale or mistyped
// link was indistinguishable from a good one until the bot searched the wrong
// tee sheet. Now the links ship with the app and are checked against ForeUp
// before anyone connects.

const PREFLIGHT_STATE = {
  ok:          { cls: 'ok',      icon: '✓' },
  mismatch:    { cls: 'bad',     icon: '✕' },
  malformed:   { cls: 'bad',     icon: '✕' },
  unverified:  { cls: 'warn',    icon: '!' },
  unreachable: { cls: 'warn',    icon: '!' },
};

function checkFor(key) {
  return (S.preflight?.courses || []).find(c => c.key === key) || null;
}

function relativeTime(iso) {
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return '';
  const secs = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (secs < 60) return 'just now';
  if (secs < 3600) return `${Math.round(secs / 60)} min ago`;
  return `${Math.round(secs / 3600)} hr ago`;
}

async function loadCourseCatalog() {
  try {
    const data = await api('/course-catalog');
    S.catalog = data.courses || [];
    if (data.preflight) S.preflight = data.preflight;
  } catch (e) {
    S.catalog = [];
  }
  renderCourseOptions();
  renderPreflight();
}

function renderCourseOptions() {
  const sel = $('courseSelect');
  const previous = S.courseKey;
  sel.innerHTML = '';

  if (!S.catalog.length) {
    // Catalog unavailable — fall back to the manual URL flow rather than
    // stranding the user with an empty dropdown.
    const opt = document.createElement('option');
    opt.value = CUSTOM_COURSE;
    opt.textContent = 'Paste a ForeUp URL';
    sel.appendChild(opt);
    sel.value = CUSTOM_COURSE;
    renderCoursePicker();
    onCourseChange();
    return;
  }

  const placeholder = document.createElement('option');
  placeholder.value = '';
  placeholder.disabled = true;
  placeholder.textContent = 'Select a course';
  sel.appendChild(placeholder);

  S.catalog.forEach(c => {
    const chk = checkFor(c.key);
    const mark = chk ? (PREFLIGHT_STATE[chk.status]?.icon || '') : '';
    const opt = document.createElement('option');
    opt.value = c.key;
    opt.textContent = `${c.emoji ? c.emoji + '  ' : ''}${c.name}${mark ? '  ' + mark : ''}`;
    sel.appendChild(opt);
  });

  const custom = document.createElement('option');
  custom.value = CUSTOM_COURSE;
  custom.textContent = 'Other — paste a ForeUp URL';
  sel.appendChild(custom);

  sel.value = previous || '';
  if (!sel.value) sel.selectedIndex = 0;
  renderCoursePicker();
  onCourseChange();
}

// The picker mirrors the <select>: a row writes the key and dispatches `change`,
// so onCourseChange and applyLinkGate run exactly as they do for a native pick.
function renderCoursePicker() {
  const box = $('coursePicker');
  if (!box) return;
  box.innerHTML = '';

  const entries = S.catalog.map(c => ({
    key: c.key,
    name: c.name,
    sub: c.note || '',
    check: checkFor(c.key),
  }));
  entries.push({ key: CUSTOM_COURSE, name: 'Other course', sub: 'Paste a ForeUp booking URL', check: null });

  entries.forEach(e => {
    const row = document.createElement('button');
    row.type = 'button';
    row.className = 'course-row';
    row.dataset.key = e.key;

    let badge = '';
    if (e.check) {
      const st = PREFLIGHT_STATE[e.check.status] || PREFLIGHT_STATE.unverified;
      const label = e.check.status === 'ok' ? 'Verified' : (st.cls === 'bad' ? 'Wrong link' : 'Unconfirmed');
      const kind = e.check.status === 'ok' ? 'success' : (st.cls === 'bad' ? 'error' : 'warning');
      badge = `<span class="badge badge-${kind} course-row-badge">${st.icon} ${esc(label)}</span>`;
    }

    row.innerHTML = crestFor(e.key)
      + `<span class="course-row-text">`
      + `<span class="course-row-head"><span class="course-row-name">${esc(e.name)}</span>${badge}</span>`
      + (e.sub ? `<span class="course-row-sub">${esc(e.sub)}</span>` : '')
      + `</span>`;

    row.addEventListener('click', () => {
      const sel = $('courseSelect');
      sel.value = e.key;
      sel.dispatchEvent(new Event('change'));
    });
    box.appendChild(row);
  });

  syncPickerSelection();
}

function syncPickerSelection() {
  const current = $('courseSelect')?.value || '';
  document.querySelectorAll('#coursePicker .course-row').forEach(row => {
    const on = row.dataset.key === current;
    row.classList.toggle('selected', on);
    row.setAttribute('aria-pressed', on ? 'true' : 'false');
  });
}

function onCourseChange() {
  const key = $('courseSelect').value;
  S.courseKey = key;
  S.overrideBadLink = false;
  syncPickerSelection();

  if (key === CUSTOM_COURSE) {
    show('customUrlCard');
    hide('courseDetail');
    $('connectBtn').disabled = false;
    $('connectBtn').textContent = 'Connect';
    return;
  }

  hide('customUrlCard');
  const course = S.catalog.find(c => c.key === key);
  if (!course) {
    hide('courseDetail');
    $('connectBtn').disabled = false;
    return;
  }

  // The rest of the app reads these three fields, so the picker writes to them
  // exactly as a paste would. Nothing downstream needs to know about courses.
  $('bookingUrl').value = course.booking_url;
  $('scheduleId').value = course.schedule_id;
  $('courseId').value = course.course_id;

  renderCourseDetail(course);
  applyLinkGate(course);
}

// Course crests. Inline SVG keyed by catalog key, so they cost no request and
// stay sharp at any pixel density. A native <option> can only hold text, which
// is why the dropdown keeps the catalog's emoji and the crest appears here, on
// the detail panel, the moment a course is picked. Unknown keys (and the
// paste-a-URL flow) fall back to the flag.
const COURSE_CRESTS = {
  bethpage: `<circle cx="16" cy="16" r="14.5" fill="#0c1611" stroke="#2fbf6d" stroke-width="1.2"/>
    <circle cx="16" cy="16" r="11.5" fill="none" stroke="#f0b429" stroke-width="0.8" opacity="0.55"/>
    <text x="16" y="21.5" text-anchor="middle" font-family="Georgia, 'Times New Roman', serif"
          font-size="15" font-weight="700" fill="#edf2ec">B</text>`,

  sunken_meadow: `<circle cx="16" cy="16" r="14.5" fill="#0c1611" stroke="#2fbf6d" stroke-width="1.2"/>
    <circle cx="16" cy="11.5" r="3.4" fill="#f0b429"/>
    <path d="M7.5 19q3-2.6 6-0 3 2.6 6 0 3-2.6 6 0" fill="none" stroke="#58b6d8"
          stroke-width="1.5" stroke-linecap="round"/>
    <path d="M7.5 23.2q3-2.6 6-0 3 2.6 6 0 3-2.6 6 0" fill="none" stroke="#2fbf6d"
          stroke-width="1.5" stroke-linecap="round" opacity="0.85"/>`,

  montauk_downs: `<circle cx="16" cy="16" r="14.5" fill="#0c1611" stroke="#2fbf6d" stroke-width="1.2"/>
    <path d="M9.5 10.5h3M19.5 10.5h3" stroke="#f0b429" stroke-width="1.3" stroke-linecap="round"/>
    <path d="M16 6.5l2.4 3h-4.8z" fill="#edf2ec"/>
    <rect x="14.1" y="9.4" width="3.8" height="2.8" rx="0.5" fill="#f0b429"/>
    <path d="M14.4 12.4h3.2l1.5 9.6h-6.2z" fill="#edf2ec"/>
    <path d="M13.6 17h4.8" stroke="#0c1611" stroke-width="1.4"/>
    <path d="M8.5 22.4q3.6-1.8 7.5-1.8t7.5 1.8" fill="none" stroke="#2fbf6d"
          stroke-width="1.5" stroke-linecap="round"/>`,

  crab_meadow: `<circle cx="16" cy="16" r="14.5" fill="#0c1611" stroke="#2fbf6d" stroke-width="1.2"/>
    <circle cx="10.2" cy="15.4" r="1.9" fill="#e5484d"/>
    <circle cx="21.8" cy="15.4" r="1.9" fill="#e5484d"/>
    <ellipse cx="16" cy="18.8" rx="5.2" ry="3.5" fill="#e5484d"/>
    <path d="M11.2 21.6l-2 2.1M13.2 22.5l-1.2 2.3M20.8 21.6l2 2.1M18.8 22.5l1.2 2.3"
          stroke="#e5484d" stroke-width="1.2" stroke-linecap="round"/>
    <circle cx="14.3" cy="17.9" r="0.85" fill="#0c1611"/>
    <circle cx="17.7" cy="17.9" r="0.85" fill="#0c1611"/>`,
};

const FALLBACK_CREST = `<circle cx="16" cy="16" r="14.5" fill="#0c1611" stroke="#2fbf6d" stroke-width="1.2"/>
  <path d="M12.5 24V8.5" stroke="#edf2ec" stroke-width="1.6" stroke-linecap="round"/>
  <path d="M12.5 9.2l8 2.9-8 2.9z" fill="#f0b429"/>
  <circle cx="20" cy="22.5" r="1.9" fill="#edf2ec"/>`;

function crestFor(key) {
  return `<svg class="course-crest" viewBox="0 0 32 32" aria-hidden="true" focusable="false">`
       + (COURSE_CRESTS[key] || FALLBACK_CREST) + `</svg>`;
}

function renderCourseDetail(course) {
  const chk = checkFor(course.key);
  const state = chk ? (PREFLIGHT_STATE[chk.status] || PREFLIGHT_STATE.unverified) : null;
  const rows = [];

  rows.push(`<div class="course-detail-row"><span class="course-detail-label">Booking link</span>
    <a href="${esc(course.booking_url)}" target="_blank" rel="noopener">${esc(course.booking_url)}</a></div>`);
  rows.push(`<div class="course-detail-row"><span class="course-detail-label">IDs</span>
    <code>schedule ${esc(course.schedule_id)}</code> <code>course ${esc(course.course_id)}</code></div>`);

  if (chk) {
    const label = chk.status === 'ok' ? 'Verified' : (state.cls === 'bad' ? 'Wrong link' : 'Unconfirmed');
    rows.push(`<div class="course-detail-row"><span class="course-detail-label">Link check</span>
      <span class="badge badge-${chk.status === 'ok' ? 'success' : (state.cls === 'bad' ? 'error' : 'warning')}">${state.icon} ${esc(label)}</span>
      <span class="course-detail-note">${esc(chk.detail)}</span></div>`);
  } else {
    rows.push(`<div class="course-detail-row"><span class="course-detail-label">Link check</span>
      <span class="course-detail-note">Not checked yet.</span></div>`);
  }

  if (course.note) {
    rows.push(`<div class="course-detail-row"><span class="course-detail-label">Note</span>
      <span class="course-detail-note">${esc(course.note)}</span></div>`);
  }

  const rejected = chk && (chk.status === 'mismatch' || chk.status === 'malformed');
  if (rejected && !S.overrideBadLink) {
    rows.push(`<div class="course-detail-row"><span class="course-detail-label"></span>
      <button type="button" class="link-btn" id="overrideLinkBtn">Use this link anyway</button></div>`);
  }

  $('courseDetail').innerHTML = rows.join('');
  show('courseDetail');

  $('overrideLinkBtn')?.addEventListener('click', () => {
    S.overrideBadLink = true;
    hideAlert('connectError');
    renderCourseDetail(course);
    applyLinkGate(course);
  });
}

// A link ForeUp actively rejects is the only case worth blocking on. "Could not
// reach ForeUp" and "a bot check got in the way" are not evidence the link is
// wrong, so those warn and let the user through.
function applyLinkGate(course) {
  const chk = checkFor(course.key);
  const btn = $('connectBtn');
  const blocked = chk && (chk.status === 'mismatch' || chk.status === 'malformed') && !S.overrideBadLink;
  btn.disabled = !!blocked;
  btn.textContent = 'Connect';
  if (blocked) {
    showAlert('connectError',
      `${course.name}: ${chk.detail} Fix the link, or choose "Other — paste a ForeUp URL".`);
  }
}

function renderPreflight() {
  const banner = $('preflightBanner');
  const text = $('preflightText');
  banner.classList.remove('preflight-ok', 'preflight-warn', 'preflight-bad', 'preflight-pending');

  if (!S.preflight) {
    banner.classList.add('preflight-pending');
    text.textContent = 'Checking ForeUp booking links…';
    return;
  }

  const p = S.preflight;
  const when = relativeTime(p.checked_at);
  const cls = p.all_ok ? 'preflight-ok' : (p.any_bad ? 'preflight-bad' : 'preflight-warn');
  banner.classList.add(cls);
  const icon = p.all_ok ? '✓' : (p.any_bad ? '✕' : '!');
  text.textContent = `${icon} ${p.summary}${when ? ' Checked ' + when + '.' : ''}`;
}

async function runPreflight(refresh) {
  const btn = $('preflightRecheck');
  if (btn) btn.disabled = true;
  if (refresh || !S.preflight) {
    S.preflight = null;
    renderPreflight();
  }
  try {
    S.preflight = await api('/course-catalog/preflight' + (refresh ? '?refresh=true' : ''));
  } catch (e) {
    // Report the failure as "could not confirm" rather than leaving the banner
    // spinning forever — the links may well be fine.
    S.preflight = {
      checked_at: new Date().toISOString(),
      ok_count: 0,
      total: S.catalog.length,
      all_ok: false,
      any_bad: false,
      summary: `Could not run the link check (${e.message}). The built-in links are unchanged — you can still connect.`,
      courses: [],
    };
  } finally {
    if (btn) btn.disabled = false;
  }
  renderPreflight();
  renderCourseOptions();
}

// Step 1: Connect
async function connect() {
  hideAlert('connectError');

  const key = $('courseSelect').value;
  if (!key) {
    showAlert('connectError', 'Pick a course first.');
    return;
  }
  if (key !== CUSTOM_COURSE) {
    const chk = checkFor(key);
    if (chk && (chk.status === 'mismatch' || chk.status === 'malformed') && !S.overrideBadLink) {
      showAlert('connectError', `ForeUp rejected this course's link: ${chk.detail}`);
      return;
    }
  } else if (!$('bookingUrl').value.trim() && !$('scheduleId').value.trim()) {
    showAlert('connectError', 'Paste a ForeUp booking URL, or pick a built-in course.');
    return;
  }

  const btn = $('connectBtn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-inline"></span> Connecting...';

  const username = $('username').value.trim();
  const password = $('password').value.trim();
  const bookingUrl = $('bookingUrl').value.trim();
  const scheduleId = $('scheduleId').value.trim();
  const courseId = $('courseId').value.trim();

  try {
    const data = await api('/connect', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        username,
        password,
        booking_url: bookingUrl,
        schedule_id: scheduleId,
        course_id: courseId,
      }),
    });

    S.connected = true;
    S.baseUrl = data.base_url || '';
    S.scheduleId = data.schedule_id || scheduleId;
    S.courseId = data.course_id || courseId;
    S.username = username;

    $('connectionBadge').textContent = 'Connected';
    $('connectionBadge').classList.add('connected');

    if (data.schedule_id) $('scheduleId').value = data.schedule_id;
    if (data.course_id) $('courseId').value = data.course_id;

    const courses = data.courses || [];
    S.courses = courses;

    const bcSelect = $('bookingClass');
    bcSelect.innerHTML = '<option value="" disabled selected>Select a booking class</option>';

    // Build the booking-class option list from the union of names across all
    // courses so picks are course-agnostic; backend resolves per course.
    const nameSet = new Map();
    courses.forEach(c => (c.classes || []).forEach(cls => {
      if (cls.name && !nameSet.has(cls.name)) nameSet.set(cls.name, cls);
    }));
    const classOptions = Array.from(nameSet.values());
    if (classOptions.length === 0 && data.booking_classes) {
      // Single-course fallback (legacy path)
      data.booking_classes.forEach(bc => classOptions.push({ id: bc.id, name: bc.name }));
    }
    classOptions.forEach(cls => {
      const opt = document.createElement('option');
      opt.value = cls.name || cls.id;  // send the name; backend resolves per course
      opt.textContent = cls.name;
      bcSelect.appendChild(opt);
    });

    const coursesGroup = $('coursesGroup');
    const rankingGroup = $('rankingGroup');
    const courseBox = $('courseCheckboxes');
    courseBox.innerHTML = '';
    if (courses.length > 1) {
      courses.forEach((c, i) => {
        const checked = (c.teesheet_id === S.courseId) || (i === 0 && !S.courseId);
        const row = document.createElement('label');
        row.className = 'checkbox-row';
        row.innerHTML = `<input type="checkbox" value="${c.teesheet_id}" ${checked ? 'checked' : ''}> ${c.name}`;
        courseBox.appendChild(row);
      });
      coursesGroup.classList.remove('hidden');
      rankingGroup.classList.remove('hidden');
    } else {
      coursesGroup.classList.add('hidden');
      rankingGroup.classList.add('hidden');
    }

    updateDateRange();
    goToStep(2);
  } catch (e) {
    showAlert('connectError', e.message);
  } finally {
    btn.disabled = false;
    btn.innerHTML = 'Connect';
  }
}

// Step 2: Search tee times
async function searchTeeTimes(opts = {}) {
  const preselected = opts.preselected || null;
  hideAlert('searchError');
  const btn = $('searchBtn');
  const strategy = preselected ? 'off' : raceStrategy();
  const isSniper = strategy !== 'off';
  btn.disabled = true;
  btn.innerHTML = preselected
    ? '<span class="spinner-inline"></span> Booking...'
    : isSniper
      ? '<span class="spinner-inline"></span> Sniper Armed...'
      : '<span class="spinner-inline"></span> Searching...';

  if (isSniper) {
    $('eventLogContainer')?.classList.add('sniper-armed');
    requestNotificationPermission();
  }

  show('eventLogContainer');
  $('eventLog').innerHTML = '';
  hide('slotsContainer');
  S.slots = [];
  if (!preselected) S.selectedSlot = null;
  S.jobEvents = [];
  S.sniperMode = isSniper;

  const scheduleId = S.scheduleId || $('scheduleId').value.trim();
  const courseId = S.courseId || $('courseId').value.trim();
  const bookingUrl = $('bookingUrl').value.trim();

  if (!scheduleId && !bookingUrl) {
    showAlert('searchError', 'No schedule ID or booking URL. Go back to Step 1 and connect first.');
    btn.disabled = false;
    btn.innerHTML = isSniper ? (raceStrategy() === 'future_sight' ? 'Arm Future Sight' : 'Arm Sniper') : 'Search Tee Times';
    return;
  }

  if (!$('bookingClass').value) {
    showAlert('searchError', 'Please select a booking class before searching.');
    btn.disabled = false;
    btn.innerHTML = isSniper ? (raceStrategy() === 'future_sight' ? 'Arm Future Sight' : 'Arm Sniper') : 'Search Tee Times';
    $('eventLogContainer')?.classList.remove('sniper-armed');
    return;
  }

  // Future Sight has nothing to aim at without a release moment. The backend
  // downgrades it to plain Sniper rather than firing blind, but say so here so
  // an armed job is never quietly not the mode that was picked.
  if (strategy === 'future_sight' && !$('releaseTime').value) {
    showAlert('searchError', 'Future Sight needs a release time — it pre-stages holds for the instant ' +
                             'the sheet opens. For a mid-day cancellation, choose Sniper instead.');
    btn.disabled = false;
    btn.innerHTML = 'Arm Future Sight';
    $('eventLogContainer')?.classList.remove('sniper-armed');
    return;
  }

  const payload = {
    username: $('username').value.trim(),
    password: $('password').value.trim(),
    foreup_booking_url: bookingUrl,
    schedule_id: scheduleId,
    course_id: courseId,
    play_date: $('playDate').value,
    players: parseInt($('players').value),
    holes: parseInt($('holes').value),
    time_start: $('timeStart').value,
    time_end: $('timeEnd').value,
    booking_class: $('bookingClass').value,
    course_ids: Array.from(document.querySelectorAll('#courseCheckboxes input[type=checkbox]:checked')).map(el => el.value),
    ranking: ($('ranking') && $('ranking').value) || 'earliest',
    preselected_slot: preselected,
    dry_run: $('dryRun').checked,
    sniper_mode: isSniper,
    release_time: (isSniper && !$('snipeNow').checked) ? $('releaseTime').value : '',
    auto_book: isSniper ? $('autoBook').checked : false,
    future_sight: strategy === 'future_sight',
    anticaptcha_api_key: '',
  };

  try {
    const { job_id } = await api('/jobs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });

    S.jobId = job_id;
    addEvent('system', 'job', 'Job started: ' + job_id, null);
    const stopBtn = $('stopBtn');
    stopBtn.disabled = false;
    stopBtn.textContent = 'Stop';
    show('stopBtn');
    startPolling();
  } catch (e) {
    showAlert('searchError', e.message);
    btn.disabled = false;
    btn.innerHTML = isSniper ? (raceStrategy() === 'future_sight' ? 'Arm Future Sight' : 'Arm Sniper') : 'Search Tee Times';
    $('eventLogContainer')?.classList.remove('sniper-armed');
  }
}

function startPolling() {
  if (S.pollTimer) clearInterval(S.pollTimer);
  S.pollTimer = setInterval(async () => {
    try {
      const data = await api('/jobs/' + S.jobId);
      S.jobStatus = data.status;

      const newEvents = data.events.slice(S.jobEvents.length);
      for (const ev of newEvents) {
        S.jobEvents.push(ev);
        addEvent(ev.phase || 'system', ev.stage, ev.message, ev.latency_ms);
      }

      if (data.status === 'awaiting_input') {
        showVerificationForm();
        fireNotification({ events: [{ slot: null }] });
      }

      if (data.status === 'succeeded' || data.status === 'failed' || data.status === 'cancelled') {
        clearInterval(S.pollTimer);
        S.pollTimer = null;
        hideVerificationForm();
        hide('stopBtn');

        const btn = $('searchBtn');
        btn.disabled = false;
        btn.innerHTML = S.sniperMode ? (raceStrategy() === 'future_sight' ? 'Arm Future Sight' : 'Arm Sniper') : 'Search Tee Times';
        $('eventLogContainer')?.classList.remove('sniper-armed');

        if (data.status === 'succeeded') {
          fireNotification(data);
          extractSlotsFromEvents(data.events);
          if (S.slots.length > 0) {
            renderSlots();
            show('slotsContainer');
          }
          goToStep(3);
          renderResults(data);
        } else {
          showAlert('searchError', 'Job failed. Check event log for details.');
        }
      }
    } catch {
      // polling errors are non-fatal
    }
  }, 500);
}

function extractSlotsFromEvents(events) {
  for (const ev of events) {
    if (ev.slot) {
      S.slots = [ev.slot];
      S.selectedSlot = ev.slot;
    }
    if (ev.candidates && typeof ev.candidates === 'number' && ev.candidates > 0) {
      // if we got candidates info, note it
    }
  }
}

function addEvent(phase, stage, message, latencyMs) {
  const log = $('eventLog');
  const line = document.createElement('div');
  line.className = 'event-line';

  const now = new Date().toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });

  let phaseClass = 'badge-neutral';
  if (phase === 'auth') phaseClass = 'badge-info';
  else if (phase === 'discovery') phaseClass = 'badge-warning';
  else if (phase === 'checkout' || phase === 'confirm') phaseClass = 'badge-success';
  else if (phase === 'sniper') phaseClass = 'badge-sniper';
  else if (phase === 'captcha') phaseClass = 'badge-captcha';
  else if (phase === 'verify') phaseClass = 'badge-verify';

  line.innerHTML =
    `<span class="event-time">${esc(now)}</span>` +
    `<span class="badge ${phaseClass} event-phase">${esc(phase)}</span>` +
    `<span class="event-message">${esc(message)}</span>` +
    (latencyMs ? `<span class="event-latency">${latencyMs}ms</span>` : '');

  log.appendChild(line);
  log.scrollTop = log.scrollHeight;
}

function renderSlots() {
  const tbody = $('slotsBody');
  tbody.innerHTML = '';

  if (S.slots.length === 0) {
    tbody.innerHTML = '<tr><td colspan="4" class="text-center text-muted">No tee times found</td></tr>';
    return;
  }

  S.slots.forEach((slot, i) => {
    const tr = document.createElement('tr');
    tr.className = 'slot-select' + (S.selectedSlot === slot ? ' selected' : '');
    tr.onclick = () => { S.selectedSlot = slot; renderSlots(); };
    tr.innerHTML =
      `<td>${esc(fmt12(slot.time))}</td>` +
      `<td>${slot.available_spots || '?'}</td>` +
      `<td>${slot.green_fee ? '$' + slot.green_fee : 'N/A'}</td>` +
      `<td>${S.selectedSlot === slot ? '<span class="badge badge-success">Selected</span>' : '<span class="badge badge-neutral">Select</span>'}</td>`;
    tbody.appendChild(tr);
  });
}

// Step 3: Results
function renderResults(jobData) {
  const container = $('resultsContent');
  const isDryRun = S.jobEvents.some(ev => ev.terminal === 'dry_run_stopped');
  const isSniperAlert = S.jobEvents.some(ev => ev.terminal === 'sniper_alert');
  const isSuccess = S.jobEvents.some(ev => ev.terminal === 'success');
  const isFailed = S.jobEvents.some(ev => ev.terminal === 'failed');

  const sniperEvent = S.jobEvents.find(ev => ev.terminal === 'sniper_alert' || ev.terminal === 'dry_run_stopped');
  const bookingUrl = sniperEvent?.booking_url || '';
  const sniperStats = sniperEvent?.sniper_stats;

  let statusHtml = '';
  if ((isDryRun || isSniperAlert) && S.selectedSlot) {
    const statusBadge = isSniperAlert
      ? '<span class="badge badge-sniper">Sniper Alert</span>'
      : '<span class="badge badge-warning">Dry Run</span>';
    const alertMsg = isSniperAlert
      ? 'SLOT FOUND — Book it now before it\'s gone!'
      : 'Dry run complete — tee time found and available for booking.';
    const slotRaw = S.selectedSlot.raw || {};
    const courseLabel = slotRaw.schedule_name || slotRaw.course_name || S.selectedSlot.schedule_name || '';
    statusHtml = `
      <div class="alert alert-success show">${esc(alertMsg)}</div>
      <div class="summary-card">
        ${courseLabel ? `<div class="summary-row"><span class="summary-key">Course</span><span class="summary-value">${esc(courseLabel)}</span></div>` : ''}
        <div class="summary-row"><span class="summary-key">Tee Time</span><span class="summary-value">${esc(fmt12(S.selectedSlot.time))}</span></div>
        <div class="summary-row"><span class="summary-key">Available Spots</span><span class="summary-value">${S.selectedSlot.available_spots || '?'}</span></div>
        <div class="summary-row"><span class="summary-key">Green Fee</span><span class="summary-value">${S.selectedSlot.green_fee ? '$' + S.selectedSlot.green_fee : 'N/A'}</span></div>
        <div class="summary-row"><span class="summary-key">Date</span><span class="summary-value">${esc($('playDate').value)}</span></div>
        <div class="summary-row"><span class="summary-key">Players</span><span class="summary-value">${$('players').value}</span></div>
        <div class="summary-row"><span class="summary-key">Status</span><span class="summary-value">${statusBadge}</span></div>
        ${sniperStats ? `<div class="summary-row"><span class="summary-key">Sniper Stats</span><span class="summary-value">${sniperStats.polls} polls in ${Math.round(sniperStats.total_ms)}ms${sniperStats.attempts && sniperStats.attempts > 1 ? ` · ${sniperStats.attempts} booking attempts` : ''}</span></div>` : ''}
        ${sniperStats && sniperStats.tried_slots && sniperStats.tried_slots.length > 0 ? `<div class="summary-row"><span class="summary-key">Slots Taken</span><span class="summary-value">${sniperStats.tried_slots.map(esc).join(', ')}</span></div>` : ''}
      </div>
      ${isDryRun ? '<button class="btn btn-primary btn-block mt-1" id="bookNowBtn">Book this tee time now</button>' : ''}
      ${bookingUrl ? `<a href="${esc(bookingUrl)}" target="_blank" class="btn btn-secondary btn-block mt-1">Open ForeUp Booking Page</a>` : ''}
    `;
  } else if (isSuccess) {
    statusHtml = `
      <div class="alert alert-success show">Booking confirmed!</div>
      <div class="summary-card">
        <div class="summary-row"><span class="summary-key">Status</span><span class="summary-value"><span class="badge badge-success">Confirmed</span></span></div>
      </div>
    `;
  } else if (isFailed) {
    const failMsg = S.jobEvents.find(ev => ev.terminal === 'failed')?.message || 'Unknown error';
    statusHtml = `
      <div class="alert alert-error show">${esc(failMsg)}</div>
    `;
  } else {
    statusHtml = '<div class="empty-state">No results yet.</div>';
  }

  container.innerHTML = statusHtml;

  // Mirror the run's timeline onto the results page so the log there isn't an
  // empty box after every job.
  const resultsLog = $('resultsEventLog');
  if (resultsLog) {
    resultsLog.innerHTML = $('eventLog')?.innerHTML || '';
    resultsLog.scrollTop = resultsLog.scrollHeight;
  }

  const bookNow = $('bookNowBtn');
  if (bookNow) {
    bookNow.onclick = () => {
      if (!confirm('Book this tee time now? This will submit a real reservation for the exact slot shown above.')) return;
      $('dryRun').checked = false;
      goToStep(2);
      searchTeeTimes({ preselected: S.selectedSlot });
    };
  }
}

// Navigation helpers
function backToStep(n) {
  if (S.pollTimer) {
    clearInterval(S.pollTimer);
    S.pollTimer = null;
  }
  goToStep(n);
}

// Notifications
function requestNotificationPermission() {
  if ('Notification' in window && Notification.permission === 'default') {
    Notification.requestPermission();
  }
}

function fireNotification(data) {
  if (!('Notification' in window) || Notification.permission !== 'granted') return;

  const slotEv = (data.events || []).find(ev => ev.slot);
  const slot = slotEv?.slot || S.selectedSlot;
  const title = S.sniperMode ? 'Sniper: Tee Time Found!' : 'Tee Time Found!';
  const body = slot
    ? `${fmt12(slot.time)} — ${slot.available_spots} spots — $${slot.green_fee || '?'}`
    : 'A matching tee time is available.';

  try {
    const n = new Notification(title, { body, icon: '⛳', tag: 'golfbot-slot' });
    n.onclick = () => { window.focus(); n.close(); };
  } catch {}
}

// Date picker restrictions based on booking class
function updateDateRange() {
  const bcSelect = $('bookingClass');
  const dateSelect = $('playDate');
  const selectedText = bcSelect.options[bcSelect.selectedIndex]?.textContent || '';
  const prevValue = dateSelect.value;

  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const hour = now.getHours();

  const isNonResident = /non.?resident/i.test(selectedText);
  const advanceDays = isNonResident ? 6 : 8;
  const maxDays = hour >= 19 ? advanceDays : advanceDays - 1;

  const fmt = d => `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
  const dayNames = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
  const monthNames = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];

  dateSelect.innerHTML = '';
  for (let i = 0; i <= maxDays; i++) {
    const d = new Date(today);
    d.setDate(d.getDate() + i);
    const opt = document.createElement('option');
    opt.value = fmt(d);
    const label = i === 0 ? 'Today' : i === 1 ? 'Tomorrow' : `${dayNames[d.getDay()]}, ${monthNames[d.getMonth()]} ${d.getDate()}`;
    opt.textContent = label;
    dateSelect.appendChild(opt);
  }

  // Restore previous selection if still in range, otherwise default to tomorrow
  if (prevValue && [...dateSelect.options].some(o => o.value === prevValue)) {
    dateSelect.value = prevValue;
  } else if (dateSelect.options.length > 1) {
    dateSelect.value = dateSelect.options[1].value;
  }
}

// Verification code form
// --- Pre-stage probe (experimental) -------------------------------------
// Shared inputs for both probe steps, read from the main form.
function probeBase() {
  return {
    username: $('username').value.trim(),
    password: $('password').value.trim(),
    foreup_booking_url: $('bookingUrl').value.trim(),
    schedule_id: S.scheduleId || $('scheduleId').value.trim(),
    course_id: S.courseId || $('courseId').value.trim(),
    play_date: $('playDate').value,
    players: parseInt($('players').value),
    holes: parseInt($('holes').value),
    booking_class: $('bookingClass').value,
    // Multi-course facilities: without these we would only read the course in
    // the URL, which on Bethpage is Black and is usually full.
    course_ids: Array.from(
      document.querySelectorAll('#courseCheckboxes input[type=checkbox]:checked')
    ).map(el => el.value),
  };
}

function probeLog(html) {
  const out = $('probeOut');
  out.classList.remove('hidden');
  out.innerHTML = html;
}

async function showTeeGrid() {
  const btn = $('gridBtn');
  btn.disabled = true;
  btn.textContent = 'Reading teesheet...';
  try {
    const g = await api('/teesheet-grid', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(probeBase()),
    });

    // Offer every real tee time, not just the currently-open ones: at a
    // release everything is open, so booked-now times are valid targets.
    const sel = $('probeTime');
    const open = new Set(g.open_times.map(t => (t.includes(' ') ? t.split(' ')[1] : t).slice(0, 5)));
    sel.innerHTML = g.grid_times.map(t =>
      `<option value="${esc(t)}">${esc(t)}${open.has(t) ? ' (open now)' : ' (booked now)'}</option>`
    ).join('') || '<option value="">no grid found</option>';
    $('probeBtn').disabled = g.grid_times.length === 0;
    $('validateBtn').disabled = g.grid_times.length === 0;

    // Always show what was actually read, so an empty result explains itself
    // rather than looking like a broken lookup.
    const breakdown = (g.courses || []).map(c =>
      `&bull; ${esc(c.name || c.course_id)}: ${c.open_count} open`
      + (c.interval_minutes ? `, ${c.interval_minutes}min grid` : '')
      + (c.error ? ` — ${esc(c.error)}` : '')
    ).join('<br>');

    if (!g.observed_count) {
      probeLog(
        `<strong>No tee times found on ${esc(g.play_date)}.</strong><br><br>`
        + (breakdown ? breakdown + '<br><br>' : '')
        + 'Either that date has not been released yet, or the selected course(s) '
        + 'are full. Tick a course that has availability (check the ForeUp site), '
        + 'or pick a nearer date — the grid is a property of the course, so any '
        + 'bookable date teaches us the same thing.'
      );
      return;
    }

    let html = `<strong>Course:</strong> ${esc(g.course_name || g.course_id)}<br>`
             + `<strong>Interval:</strong> ${g.interval_minutes}min &nbsp; `
             + `<strong>Range:</strong> ${esc(g.first_time)}–${esc(g.last_time)}<br>`
             + `<strong>Confidence:</strong> ${esc(g.confidence)} `
             + `(${g.observed_count} open now, ${g.grid_count} real tee times)<br>`
             + `<strong>Usable for pre-staging:</strong> ${g.usable_for_prestage ? 'yes' : 'NO'}`;
    if (g.learned_courses && g.learned_courses.length) {
      html += `<br><strong>Remembered:</strong> Golfbot now knows this grid for `
            + `${esc(g.learned_courses.join(', '))} on this weekday, so Future Sight can `
            + `aim here even when the reference sheet is full.`;
    }
    if (breakdown) html += '<br><br>' + breakdown;
    if (g.notes && g.notes.length) {
      html += '<br><br>' + g.notes.map(n => '&bull; ' + esc(n)).join('<br>');
    }
    probeLog(html);
  } catch (e) {
    probeLog('<strong>Failed:</strong> ' + esc(e.message));
  } finally {
    btn.disabled = false;
    btn.textContent = "Show this course's tee grid";
  }
}

async function scanCheckoutFields() {
  const btn = $('fieldsBtn');
  btn.disabled = true;
  btn.textContent = 'Reading ForeUp\u2019s checkout...';
  try {
    const r = await api('/probe/checkout-fields', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(probeBase()),
    });
    let html = `<strong>${esc(r.verdict)}</strong><br><br>`
             + `Scanned ${r.scripts_scanned} source(s); we send ${r.fields_we_send} fields.`;
    if ((r.scripts_with_payload_code || []).length) {
      html += '<br><br><strong>Payload code found in:</strong><br>'
            + r.scripts_with_payload_code.map(x => '&bull; ' + esc(x)).join('<br>');
    }
    if ((r.api_calls || []).length) {
      // The endpoints ForeUp's own checkout calls. Any we don't call is a
      // step we skip, which is the likeliest home of the fee.
      html += '<br><br><strong>ForeUp checkout API calls found:</strong>';
      for (const a of r.api_calls) {
        html += `<br>&bull; <strong>${esc(a.name)}</strong> &rarr; <code>`
              + esc((a.url_fragments || []).join('  ')) + '</code>'
              + ((a.url_bases || []).length ? ' <em>(' + esc(a.url_bases.join(', ')) + ')</em>' : '');
      }
    }
    if ((r.candidate_fields || []).length) {
      html += '<br><br><strong>Fields near it that we do NOT send:</strong><br>'
            + '<code>' + esc(r.candidate_fields.join(', ')) + '</code>';
    }
    for (const res of r.results || []) {
      for (const h of res.anchor_hits || []) {
        html += `<br><br><strong>${esc(h.anchor)}</strong> in ${esc(res.script)}:<br>`
              + '<code>' + esc(h.excerpt) + '</code>';
      }
      for (const pm of (res.payment_mentions || []).slice(0, 12)) {
        html += `<br><br><strong>payment hint &ldquo;${esc(pm.hint)}&rdquo;:</strong><br>`
              + '<code>' + esc(pm.excerpt) + '</code>';
      }
    }
    probeLog(html);
  } catch (e) {
    probeLog('<strong>Failed:</strong> ' + esc(e.message));
  } finally {
    btn.disabled = false;
    btn.textContent = "Scan ForeUp's checkout fields";
  }
}

async function fireValidateProbe() {
  const target = $('probeTime').value;
  if (!target) return;
  if (!confirm(
    `Ask ForeUp whether it would accept a booking at ${target}?\n\n`
    + 'This holds the time, then sends the real booking request with '
    + "validate_only set — ForeUp should answer without creating anything.\n\n"
    + 'CAUTION: we have never sent that flag before. If ForeUp ignores it, this '
    + 'BOOKS FOR REAL and charges any booking fee. Only aim it at a time you '
    + 'would be happy to have.'
  )) return;

  const btn = $('validateBtn');
  btn.disabled = true;
  btn.textContent = 'Asking ForeUp...';
  try {
    const r = await api('/probe/cold-hold', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...probeBase(), target_time: target, validate_after_hold: true }),
    });
    let v = r.validate || {};

    // ForeUp gates on the emailed code before it looks at the fee, so a first
    // pass that stops there has not answered the question we asked. Push
    // through it against the SAME hold — the code is issued for that one.
    if (v.needs_code && v.code_requested && v.hold_id) {
      const code = prompt(
        'ForeUp wants the emailed booking code before it will look at the fee.\n\n'
        + 'Check your email and paste the code here to finish the check. '
        + 'The hold is still open, so do this promptly.'
      );
      if (code && code.trim()) {
        btn.textContent = 'Submitting code...';
        const r2 = await api('/probe/cold-hold', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            ...probeBase(),
            target_time: target,
            validate_after_hold: true,
            existing_hold_id: v.hold_id,
            booking_code: code.trim(),
            want_payment_url: true,
          }),
        });
        v = r2.validate || v;
        r.payment = r2.payment;
        r.steps = (r.steps || []).concat(r2.steps || []);
      }
    }
    let html = r.steps.map(x => '&bull; ' + esc(x)).join('<br>')
             + `<br><br><strong>Hold:</strong> ${r.accepted ? 'ACCEPTED' : 'REJECTED'} in ${r.latency_ms}ms`;
    if (!r.accepted && r.error) html += '<br>' + esc(r.error);

    if (v.skipped) {
      html += '<br><br>' + esc(v.verdict);
    } else if (v.verdict) {
      // The fee answer is the entire point of this probe, so lead with it.
      html += `<br><br><strong>Validation:</strong> ${v.ok ? 'PASSED' : 'REJECTED'}`
            + (v.latency_ms ? ` in ${v.latency_ms}ms` : '')
            + `<br>${esc(v.verdict)}`;
      if (v.needs_code && !v.code_sent) {
        html += '<br><br><strong>Not an answer yet.</strong> ForeUp stopped at the code '
              + 'gate, which sits ahead of the fee check, so the fee question is still '
              + 'open. Re-run and enter the emailed code to get past it.';
      }
      if (v.code_request_error) {
        html += '<br><br><strong>Could not request a code:</strong> ' + esc(v.code_request_error);
      }
      if (v.fee_blocked) {
        html += '<br><br><strong>This is the decisive answer:</strong> the booking-fee '
              + 'check runs during validation, so reordering our requests to match the '
              + "browser cannot get past it. The fee has to be paid.";
      }
      if (v.error) html += '<br><br><strong>ForeUp said:</strong><br>' + esc(v.error);
      if (v.response) html += '<br><br><strong>Response:</strong><br>'
                            + esc(JSON.stringify(v.response).slice(0, 800));
      if (r.payment) {
        const pm = r.payment;
        html += `<br><br><strong>Payment page:</strong> ${pm.ok ? 'created' : 'NOT created'}`
              + (pm.latency_ms ? ` in ${Math.round(pm.latency_ms)}ms` : '');
        if (pm.payment_url) {
          // The whole point of the test: can this URL be opened elsewhere?
          html += '<br><a href="' + esc(pm.payment_url) + '" target="_blank" rel="noopener">'
                + esc(pm.payment_url) + '</a>'
                + '<br><em>Open this on your phone. If a card form loads, the hand-off works.</em>';
        }
        html += '<br>' + esc(pm.verdict || '');
        if (pm.error) html += '<br><strong>ForeUp said:</strong><br>' + esc(pm.error);
        if (!pm.payment_url && pm.raw_html) {
          html += '<br><strong>Response:</strong><br><code>' + esc(pm.raw_html.slice(0, 500)) + '</code>';
        }
      }
      if (v.ok && !r.payment) {
        html += '<br><br><strong>Check your ForeUp account now.</strong> If a reservation '
              + 'is there, validate_only was ignored and this booked for real.';
      }
    }
    probeLog(html);
  } catch (e) {
    probeLog('<strong>Refused:</strong> ' + esc(e.message));
  } finally {
    btn.disabled = false;
    btn.textContent = 'Ask ForeUp: would this book?';
  }
}

async function fireColdHoldProbe() {
  const target = $('probeTime').value;
  if (!target) return;
  if (!confirm(
    `Fire a real hold on ${target}?\n\n`
    + 'Nothing gets booked, but if ForeUp accepts, that tee time is locked to '
    + 'your account for about 2 minutes before it releases itself.'
  )) return;

  const btn = $('probeBtn');
  btn.disabled = true;
  btn.textContent = 'Firing...';
  try {
    const r = await api('/probe/cold-hold', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...probeBase(), target_time: target }),
    });
    let html = r.steps.map(x => '&bull; ' + esc(x)).join('<br>')
             + `<br><br><strong>${r.accepted ? 'ACCEPTED' : 'REJECTED'}</strong> `
             + `in ${r.latency_ms}ms<br>${esc(r.verdict)}`;
    if (r.error) html += '<br><br><strong>ForeUp said:</strong><br>' + esc(r.error);
    probeLog(html);
  } catch (e) {
    probeLog('<strong>Refused:</strong> ' + esc(e.message));
  } finally {
    btn.disabled = false;
    btn.textContent = 'Fire cold-hold probe';
  }
}

async function stopJob() {
  if (!S.jobId) return;
  const btn = $('stopBtn');
  btn.disabled = true;
  btn.textContent = 'Stopping...';
  addEvent('system', 'job', 'Stop requested — waiting for the bot to finish its current step.', null);
  try {
    await api('/jobs/' + S.jobId + '/cancel', { method: 'POST' });
  } catch (e) {
    // Most likely the job finished on its own between render and click.
    addEvent('system', 'job', 'Could not stop: ' + e.message, null);
    btn.disabled = false;
    btn.textContent = 'Stop';
  }
  // The poller handles the rest — it hides the button when the job reports
  // 'cancelled', so the UI only says stopped once the worker actually has.
}

function showVerificationForm() {
  show('verificationContainer');
  $('verificationCode').focus();
  $('searchBtn').innerHTML = 'Awaiting Verification Code...';
}

function hideVerificationForm() {
  hide('verificationContainer');
  $('verificationCode').value = '';
}

async function submitVerificationCode() {
  const code = $('verificationCode').value.trim();
  if (!code) return;

  const btn = $('submitCodeBtn');
  btn.disabled = true;
  btn.textContent = 'Submitting...';

  try {
    await api('/jobs/' + S.jobId + '/verify', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code }),
    });
    hideVerificationForm();
    $('searchBtn').innerHTML = '<span class="spinner-inline"></span> Verifying...';
  } catch (e) {
    showAlert('searchError', 'Failed to submit code: ' + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Verify';
  }
}

// Release-racing strategy
function onStrategyChange() {
  const strategy = raceStrategy();
  const fields = $('sniperFields');
  const btn = $('searchBtn');
  const autoBook = $('autoBook');
  if (strategy === 'off') {
    fields.classList.add('hidden');
    btn.textContent = 'Search Tee Times';
  } else {
    fields.classList.remove('hidden');
    updateSniperButtonLabel();
  }
  // Future Sight fires real holds and completes them itself — auto-book is
  // not a choice there, so show it as locked on rather than trusting the
  // user to know the dependency.
  if (strategy === 'future_sight') {
    autoBook.checked = true;
    autoBook.disabled = true;
  } else {
    autoBook.disabled = false;
  }
  // "Start now" is meaningless for Future Sight: with no release moment there
  // is nothing to pre-stage against, and the holds would fire at slots that
  // are simply already booked. Sniper is the right tool for that.
  const now = $('snipeNow');
  const nowHelp = $('snipeNowHelp');
  if (strategy === 'future_sight') {
    now.checked = false;
    now.disabled = true;
    if (nowHelp) nowHelp.textContent =
      'Not available with Future Sight — pre-staged holds need a known release moment to aim at. ' +
      'For a mid-day cancellation, choose Sniper instead.';
  } else {
    now.disabled = false;
    if (nowHelp) nowHelp.textContent =
      'For mid-day cancellations or when a course unexpectedly drops new times. Skips the countdown.';
  }
  toggleSnipeNow();
  toggleCaptchaKey();
}

function toggleSnipeNow() {
  const now = $('snipeNow').checked;
  const row = $('releaseTimeRow');
  if (row) row.style.display = now ? 'none' : '';
  updateSniperButtonLabel();
}

function updateSniperButtonLabel() {
  const strategy = raceStrategy();
  if (strategy === 'off') return;
  const arm = strategy === 'future_sight' ? 'Arm Future Sight' : 'Arm Sniper';
  $('searchBtn').textContent = $('snipeNow').checked ? 'Start Now' : arm;
}

function toggleCaptchaKey() {}

// Init
document.addEventListener('DOMContentLoaded', () => {
  goToStep(1);

  $('bookingUrl').addEventListener('input', (e) => parseBookingUrl(e.target.value));
  $('courseSelect').addEventListener('change', onCourseChange);
  $('preflightRecheck').addEventListener('click', () => runPreflight(true));
  $('connectBtn').addEventListener('click', connect);
  $('searchBtn').addEventListener('click', searchTeeTimes);
  $('stopBtn').addEventListener('click', stopJob);
  // The cold-hold probe places a real hold on real inventory, so it stays out
  // of the normal booking flow. ?debug=1 brings it back for diagnosing a course.
  if (new URLSearchParams(location.search).get('debug') === '1') {
    $('probeTools').classList.remove('hidden');
  }
  $('gridBtn').addEventListener('click', showTeeGrid);
  $('probeBtn').addEventListener('click', fireColdHoldProbe);
  $('validateBtn').addEventListener('click', fireValidateProbe);
  $('fieldsBtn').addEventListener('click', scanCheckoutFields);
  document.querySelectorAll('input[name="raceStrategy"]').forEach(el =>
    el.addEventListener('change', onStrategyChange));
  $('snipeNow').addEventListener('change', toggleSnipeNow);
  $('autoBook').addEventListener('change', toggleCaptchaKey);
  $('bookingClass').addEventListener('change', updateDateRange);

  loadCourseCatalog().then(() => runPreflight(false));
});

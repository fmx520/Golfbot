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
};

function $(id) { return document.getElementById(id); }

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

// Step 1: Connect
async function connect() {
  hideAlert('connectError');
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
  const isSniper = !preselected && $('sniperMode').checked;
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
    btn.innerHTML = isSniper ? 'Arm Sniper' : 'Search Tee Times';
    return;
  }

  if (!$('bookingClass').value) {
    showAlert('searchError', 'Please select a booking class before searching.');
    btn.disabled = false;
    btn.innerHTML = isSniper ? 'Arm Sniper' : 'Search Tee Times';
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
    btn.innerHTML = isSniper ? 'Arm Sniper' : 'Search Tee Times';
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
        btn.innerHTML = S.sniperMode ? 'Arm Sniper' : 'Search Tee Times';
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

    let html = `<strong>Interval:</strong> ${g.interval_minutes}min &nbsp; `
             + `<strong>Range:</strong> ${esc(g.first_time)}–${esc(g.last_time)}<br>`
             + `<strong>Confidence:</strong> ${esc(g.confidence)} `
             + `(${g.observed_count} open now, ${g.grid_count} real tee times)<br>`
             + `<strong>Usable for pre-staging:</strong> ${g.usable_for_prestage ? 'yes' : 'NO'}`;
    if (g.notes && g.notes.length) {
      html += '<br><br>' + g.notes.map(n => '&bull; ' + esc(n)).join('<br>');
    }
    probeLog(html);
  } catch (e) {
    probeLog('<strong>Failed:</strong> ' + esc(e.message));
  } finally {
    btn.disabled = false;
    btn.textContent = "1. Show this course's tee grid";
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
    btn.textContent = '2. Fire cold-hold probe';
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

// Sniper toggle
function toggleSniperMode() {
  const checked = $('sniperMode').checked;
  const fields = $('sniperFields');
  const btn = $('searchBtn');
  if (checked) {
    fields.classList.remove('hidden');
    updateSniperButtonLabel();
  } else {
    fields.classList.add('hidden');
    btn.textContent = 'Search Tee Times';
  }
  toggleCaptchaKey();
}

function toggleSnipeNow() {
  const now = $('snipeNow').checked;
  const row = $('releaseTimeRow');
  if (row) row.style.display = now ? 'none' : '';
  updateSniperButtonLabel();
}

function updateSniperButtonLabel() {
  if (!$('sniperMode').checked) return;
  $('searchBtn').textContent = $('snipeNow').checked ? 'Snipe Now' : 'Arm Sniper';
}

function toggleCaptchaKey() {}

// Init
document.addEventListener('DOMContentLoaded', () => {
  goToStep(1);

  $('bookingUrl').addEventListener('input', (e) => parseBookingUrl(e.target.value));
  $('connectBtn').addEventListener('click', connect);
  $('searchBtn').addEventListener('click', searchTeeTimes);
  $('stopBtn').addEventListener('click', stopJob);
  $('gridBtn').addEventListener('click', showTeeGrid);
  $('probeBtn').addEventListener('click', fireColdHoldProbe);
  $('sniperMode').addEventListener('change', toggleSniperMode);
  $('snipeNow').addEventListener('change', toggleSnipeNow);
  $('autoBook').addEventListener('change', toggleCaptchaKey);
  $('bookingClass').addEventListener('change', updateDateRange);
});

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
};

function $(id) { return document.getElementById(id); }

function show(id) { $(id)?.classList.remove('hidden'); }
function hide(id) { $(id)?.classList.add('hidden'); }

function esc(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
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

    goToStep(2);
  } catch (e) {
    showAlert('connectError', e.message);
  } finally {
    btn.disabled = false;
    btn.innerHTML = 'Connect';
  }
}

// Step 2: Search tee times
async function searchTeeTimes() {
  hideAlert('searchError');
  const btn = $('searchBtn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-inline"></span> Searching...';

  show('eventLogContainer');
  $('eventLog').innerHTML = '';
  hide('slotsContainer');
  S.slots = [];
  S.selectedSlot = null;
  S.jobEvents = [];

  const payload = {
    username: $('username').value.trim(),
    password: $('password').value.trim(),
    foreup_booking_url: $('bookingUrl').value.trim(),
    schedule_id: S.scheduleId || $('scheduleId').value.trim(),
    course_id: S.courseId || $('courseId').value.trim(),
    play_date: $('playDate').value,
    players: parseInt($('players').value),
    holes: parseInt($('holes').value),
    time_start: $('timeStart').value,
    time_end: $('timeEnd').value,
    dry_run: true,
  };

  try {
    const { job_id } = await api('/jobs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });

    S.jobId = job_id;
    addEvent('system', 'job', 'Job started: ' + job_id, null);
    startPolling();
  } catch (e) {
    showAlert('searchError', e.message);
    btn.disabled = false;
    btn.innerHTML = 'Search Tee Times';
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

      if (data.status === 'succeeded' || data.status === 'failed') {
        clearInterval(S.pollTimer);
        S.pollTimer = null;

        const btn = $('searchBtn');
        btn.disabled = false;
        btn.innerHTML = 'Search Tee Times';

        if (data.status === 'succeeded') {
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
      `<td>${esc(slot.time || 'N/A')}</td>` +
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
  const isSuccess = S.jobEvents.some(ev => ev.terminal === 'success');
  const isFailed = S.jobEvents.some(ev => ev.terminal === 'failed');

  let statusHtml = '';
  if (isDryRun && S.selectedSlot) {
    statusHtml = `
      <div class="alert alert-success show">Dry run complete — tee time found and available for booking.</div>
      <div class="summary-card">
        <div class="summary-row"><span class="summary-key">Tee Time</span><span class="summary-value">${esc(S.selectedSlot.time || 'N/A')}</span></div>
        <div class="summary-row"><span class="summary-key">Available Spots</span><span class="summary-value">${S.selectedSlot.available_spots || '?'}</span></div>
        <div class="summary-row"><span class="summary-key">Green Fee</span><span class="summary-value">${S.selectedSlot.green_fee ? '$' + S.selectedSlot.green_fee : 'N/A'}</span></div>
        <div class="summary-row"><span class="summary-key">Date</span><span class="summary-value">${esc($('playDate').value)}</span></div>
        <div class="summary-row"><span class="summary-key">Players</span><span class="summary-value">${$('players').value}</span></div>
        <div class="summary-row"><span class="summary-key">Status</span><span class="summary-value"><span class="badge badge-warning">Dry Run</span></span></div>
      </div>
      <p class="text-sm text-muted mt-1">To book this tee time, uncheck "Dry Run" in Step 2 and search again.</p>
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
}

// Navigation helpers
function backToStep(n) {
  if (S.pollTimer) {
    clearInterval(S.pollTimer);
    S.pollTimer = null;
  }
  goToStep(n);
}

// Init
document.addEventListener('DOMContentLoaded', () => {
  goToStep(1);

  $('bookingUrl').addEventListener('input', (e) => parseBookingUrl(e.target.value));
  $('connectBtn').addEventListener('click', connect);
  $('searchBtn').addEventListener('click', searchTeeTimes);

  // Set default date to tomorrow
  const tomorrow = new Date();
  tomorrow.setDate(tomorrow.getDate() + 1);
  $('playDate').value = tomorrow.toISOString().split('T')[0];
});

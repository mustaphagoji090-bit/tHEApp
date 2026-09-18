'use strict';

const $ = (id) => document.getElementById(id);
const STAGES = [
  ['probing', 'Reading audio'],
  ['transcribing', 'Transcribing'],
  ['planning', 'Timing beats'],
  ['directing', 'Writing prompts'],
  ['generating', 'Generating images'],
  ['rendering', 'Rendering video'],
  ['done', 'Done'],
];

const EFFECT_LABELS = {
  none: 'None (clean)',
  film_grain: 'Film grain',
  vignette: 'Vignette',
  archival: 'Archival sepia + grain',
  bw_found_footage: 'B&W found footage',
};

let CONFIG = null;
let currentJob = null;
let pollTimer = null;
let openBeat = null;
const files = { voiceover: null, music: null, script: null };

// ---------------------------------------------------------------- utilities
async function api(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let detail = response.statusText;
    try { detail = (await response.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return response.status === 204 ? null : response.json();
}

function esc(value) {
  return String(value == null ? '' : value).replace(/[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function timecode(seconds) {
  const s = Math.max(0, seconds || 0);
  const h = String(Math.floor(s / 3600)).padStart(2, '0');
  const m = String(Math.floor((s % 3600) / 60)).padStart(2, '0');
  const sec = String(Math.floor(s % 60)).padStart(2, '0');
  return `${h}:${m}:${sec}`;
}

function humanDuration(seconds) {
  if (!seconds) return '--';
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  return m ? `${m}m ${s}s` : `${s}s`;
}

// ---------------------------------------------------------------- bootstrap
async function boot() {
  CONFIG = await api('/api/config');

  const services = CONFIG.services || {};
  $('pills').innerHTML = [
    ['OpenAI', services.openai],
    ['Anthropic', services.anthropic],
    ['fal.ai', services.fal],
    ['Replicate', services.replicate],
  ].map(([name, on]) =>
    `<span class="pill ${on ? 'ok' : 'off'}">${name}: ${on ? 'ready' : 'no key'}</span>`
  ).join('');

  $('aspect').innerHTML = CONFIG.aspects
    .map((a) => `<option value="${a}">${a}${a === '16:9' ? ' (YouTube)' : a === '9:16' ? ' (Shorts)' : ''}</option>`)
    .join('');

  $('style').innerHTML = CONFIG.style_presets
    .map((s) => `<option value="${s}">${s.replace(/_/g, ' ')}</option>`).join('');

  $('effect').innerHTML = (CONFIG.effects || ['none'])
    .map((e) => `<option value="${e}">${EFFECT_LABELS[e] || e.replace(/_/g, ' ')}</option>`).join('');

  const writers = CONFIG.writers || [];
  $('writer').innerHTML = writers.length
    ? writers.map((w) => `<option value="${w}"${w === CONFIG.default_writer ? ' selected' : ''}>${
        w === 'openai' ? `OpenAI (${CONFIG.openai_text_model})` : 'Anthropic (Claude)'}</option>`).join('')
    : '<option value="">no text model configured</option>';

  const providers = CONFIG.providers || [];
  $('provider').innerHTML = providers.length
    ? providers.map((p) => `<option value="${p}"${p === CONFIG.default_provider ? ' selected' : ''}>${p}</option>`).join('')
    : '<option value="">no provider configured</option>';
  if (!providers.length) {
    showNewError('No image provider has an API key yet. Add FAL_KEY (or REPLICATE_API_TOKEN / OPENAI_API_KEY) to your .env and restart.');
  } else if (!writers.length) {
    showNewError('No text model configured. Add OPENAI_API_KEY (or ANTHROPIC_API_KEY) to .env — it writes the image prompts, not your script.');
  }

  wireDrop('drop-vo', 'file-vo', 'voiceover', (f) => `${f.name} — ${humanDuration(files.voiceoverDuration)}`);
  wireDrop('drop-music', 'file-music', 'music', (f) => f.name);
  wireDrop('drop-script', 'file-script', 'script', (f) => f.name);

  ['ipm', 'provider'].forEach((id) => $(id).addEventListener('input', updateEstimate));
  $('script').addEventListener('input', describeScript);
  $('btn-start').addEventListener('click', startJob);
  $('btn-back').addEventListener('click', () => { stopPolling(); showNew(); loadJobs(); });
  $('btn-cancel').addEventListener('click', cancelJob);
  $('btn-render').addEventListener('click', () => triggerRender());
  $('btn-rerender').addEventListener('click', () => triggerRender());
  $('btn-download').addEventListener('click', () => location.assign(`/api/jobs/${currentJob.id}/download`));
  $('btn-zip').addEventListener('click', () => location.assign(`/api/jobs/${currentJob.id}/images.zip`));
  $('btn-zip2').addEventListener('click', () => location.assign(`/api/jobs/${currentJob.id}/images.zip`));
  $('btn-csv').addEventListener('click', () => location.assign(`/api/jobs/${currentJob.id}/timeline.csv`));
  $('btn-retry-failed').addEventListener('click', retryFailed);
  $('m-close').addEventListener('click', closeModal);
  $('m-regen').addEventListener('click', regenerateOpen);
  $('modal').addEventListener('click', (e) => { if (e.target.id === 'modal') closeModal(); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeModal(); });

  loadJobs();
  const fromHash = location.hash.replace('#', '');
  if (fromHash) openJob(fromHash);
}

function wireDrop(dropId, inputId, key, label) {
  const drop = $(dropId);
  const input = $(inputId);
  drop.addEventListener('click', () => input.click());
  drop.addEventListener('dragover', (e) => { e.preventDefault(); drop.classList.add('over'); });
  drop.addEventListener('dragleave', () => drop.classList.remove('over'));
  drop.addEventListener('drop', (e) => {
    e.preventDefault();
    drop.classList.remove('over');
    if (e.dataTransfer.files.length) accept(e.dataTransfer.files[0]);
  });
  input.addEventListener('change', () => { if (input.files.length) accept(input.files[0]); });

  async function accept(file) {
    files[key] = file;
    if (key === 'voiceover') {
      files.voiceoverDuration = await readDuration(file).catch(() => 0);
      updateEstimate();
    }
    if (key === 'script') {
      $('script').value = await file.text().catch(() => '');
      describeScript();
    }
    drop.classList.add('filled');
    drop.innerHTML = `<strong>${esc(file.name)}</strong>${key === 'voiceover' ? humanDuration(files.voiceoverDuration) : 'ready'}`;
  }
}

function describeScript() {
  const text = $('script').value;
  const words = (text.match(/\S+/g) || []).length;
  const timed = /\d{1,2}:\d{2}[.,]\d{1,3}\s*-->/.test(text);
  $('script-stat').innerHTML = words
    ? `${words.toLocaleString()} words${timed ? ' &middot; <strong>timed subtitle file &mdash; transcription will be skipped</strong>' : ''}`
    : '';
}

function readDuration(file) {
  return new Promise((resolve, reject) => {
    const audio = document.createElement('audio');
    audio.preload = 'metadata';
    audio.onloadedmetadata = () => { URL.revokeObjectURL(audio.src); resolve(audio.duration); };
    audio.onerror = reject;
    audio.src = URL.createObjectURL(file);
  });
}

function updateEstimate() {
  const duration = files.voiceoverDuration || 0;
  if (!duration) { $('estimate').innerHTML = '&nbsp;'; return; }
  const ipm = parseFloat($('ipm').value) || 10;
  const count = Math.round((duration / 60) * ipm);
  const rate = (CONFIG.provider_cost || {})[$('provider').value];
  const cost = rate ? ` &middot; roughly $${(count * rate).toFixed(2)} in image credits` : '';
  $('estimate').innerHTML =
    `${humanDuration(duration)} of audio &rarr; <strong>${count} images</strong>${cost}. Rough estimate.`;
}

function showNewError(message) {
  const box = $('new-error');
  box.textContent = message;
  box.classList.remove('hidden');
}

// ---------------------------------------------------------------- job start
async function startJob() {
  $('new-error').classList.add('hidden');
  if (!files.voiceover) return showNewError('Pick a voiceover file first.');
  if (!$('script').value.trim()) return showNewError('Paste or upload your script — it is required.');
  if (!$('provider').value) return showNewError('No image provider configured.');
  if (!$('writer').value) return showNewError('No text model configured to write the image prompts.');

  const form = new FormData();
  form.append('voiceover', files.voiceover);
  if (files.music) form.append('music', files.music);
  form.append('title', $('title').value);
  form.append('script', $('script').value);
  form.append('notes', $('notes').value);
  form.append('images_per_minute', $('ipm').value);
  form.append('aspect', $('aspect').value);
  form.append('style_preset', $('style').value);
  form.append('provider', $('provider').value);
  form.append('writer', $('writer').value);
  form.append('effect', $('effect').value);
  form.append('transition', $('transition').value);
  form.append('transition_seconds', $('fade').value);
  form.append('auto_render', $('autorender').checked ? 'true' : 'false');

  $('btn-start').disabled = true;
  $('btn-start').textContent = 'Uploading...';
  try {
    const { id } = await api('/api/jobs', { method: 'POST', body: form });
    openJob(id);
  } catch (err) {
    showNewError(err.message);
  } finally {
    $('btn-start').disabled = false;
    $('btn-start').textContent = 'Start';
  }
}

// ---------------------------------------------------------------- job view
function showNew() {
  $('view-new').classList.remove('hidden');
  $('view-job').classList.add('hidden');
  location.hash = '';
}

function openJob(id) {
  $('view-new').classList.add('hidden');
  $('view-job').classList.remove('hidden');
  location.hash = id;
  stopPolling();
  refreshJob(id);
  pollTimer = setInterval(() => refreshJob(id), 1800);
}

function stopPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = null;
}

async function refreshJob(id) {
  let job;
  try {
    job = await api(`/api/jobs/${id}`);
  } catch (err) {
    stopPolling();
    return;
  }
  const firstLoad = !currentJob || currentJob.id !== job.id;
  currentJob = job;
  renderJob(job, firstLoad);
  if (['done', 'error', 'cancelled', 'awaiting_review'].includes(job.status)) stopPolling();
}

function renderJob(job, firstLoad) {
  $('job-title').textContent = job.title || '(untitled)';
  const tag = $('job-tag');
  tag.textContent = job.status;
  tag.className = 'tag ' + (job.status === 'done' ? 'done' : job.status === 'error' ? 'error' : 'running');

  const reached = STAGES.findIndex(([key]) => key === job.stage);
  $('stages').innerHTML = STAGES.map(([key, label], i) => {
    let cls = 'stage';
    if (job.status === 'error' && i === reached) cls += ' failed';
    else if (key === job.stage) cls += ' active';
    else if (reached > -1 && i < reached) cls += ' done';
    return `<span class="${cls}">${label}</span>`;
  }).join('');

  const { done = 0, total = 0 } = job.progress || {};
  const pct = total ? Math.round((done / total) * 100) : (job.status === 'done' ? 100 : 0);
  $('bar').querySelector('i').style.width = pct + '%';
  $('bar').classList.toggle('done', job.status === 'done');
  $('job-msg').textContent = total ? `${job.message} — ${done} / ${total}` : (job.message || '');

  const align = job.alignment;
  const alignLabel = !align ? '—'
    : align.source === 'script-timed' ? 'from subtitles'
    : align.source === 'transcript' ? 'no match — used transcript'
    : `${Math.round(align.ratio * 100)}% matched`;
  $('stats').innerHTML = [
    ['Length', humanDuration(job.audio && job.audio.duration)],
    ['Images', `${job.image_count} / ${job.beat_count}`],
    ['Failed', job.failed_count],
    ['Per minute', job.settings.images_per_minute],
    ['Script sync', alignLabel],
  ].map(([k, v]) => `<div class="stat"><div class="k">${k}</div><div class="v">${v}</div></div>`).join('');

  const errorBox = $('job-error');
  errorBox.classList.toggle('hidden', !job.error);
  if (job.error) errorBox.textContent = job.error;

  $('btn-cancel').classList.toggle('hidden', !['running', 'rendering', 'queued'].includes(job.status));
  $('review-panel').classList.toggle('hidden', job.status !== 'awaiting_review');

  const hasVideo = job.status === 'done' && job.video;
  $('video-panel').classList.toggle('hidden', !hasVideo);
  if (hasVideo) {
    const src = `/api/jobs/${job.id}/download?t=${job.updated_at}`;
    if ($('player').getAttribute('src') !== src) $('player').setAttribute('src', src);
    $('video-hint').textContent =
      `${job.image_count} shots · ${humanDuration(job.audio && job.audio.duration)} · ${job.settings.aspect} · ${job.settings.transition}`;
  }

  renderGallery(job, firstLoad);
}

function renderGallery(job, force) {
  const gallery = $('gallery');
  const beats = job.beats || [];
  $('gallery-count').textContent = beats.length ? `${beats.length} shots` : '';
  $('btn-retry-failed').classList.toggle('hidden', !job.failed_count);

  const shapeClass = job.settings.aspect === '9:16' ? 'portrait' : job.settings.aspect === '1:1' ? 'square' : '';

  // Rebuild only when the number of tiles changes; otherwise patch in place so
  // the grid does not flicker on every poll.
  if (force || gallery.children.length !== beats.length) {
    gallery.innerHTML = beats.map((b) => tileHTML(job, b, shapeClass)).join('');
    gallery.querySelectorAll('.shot').forEach((el) => {
      el.addEventListener('click', () => openModal(parseInt(el.dataset.index, 10)));
    });
  } else {
    beats.forEach((b, i) => {
      const el = gallery.children[i];
      if (!el) return;
      const signature = `${b.file || ''}|${b.error || ''}|${b.version || ''}`;
      if (el.dataset.signature !== signature) {
        el.outerHTML = tileHTML(job, b, shapeClass);
        const fresh = gallery.children[i];
        fresh.addEventListener('click', () => openModal(parseInt(fresh.dataset.index, 10)));
      }
    });
  }
}

function tileHTML(job, beat, shapeClass) {
  const signature = `${beat.file || ''}|${beat.error || ''}|${beat.version || ''}`;
  const base = `class="shot ${shapeClass}" data-index="${beat.index}" data-signature="${signature}"`;
  if (beat.file) {
    const src = `/api/jobs/${job.id}/image/${beat.index}?v=${beat.version || 0}`;
    return `<div ${base}>
      <img src="${src}" loading="lazy" alt="Shot ${beat.index + 1}">
      <span class="n">${beat.index + 1}</span>
      <span class="tc">${timecode(beat.start)}</span>
    </div>`;
  }
  if (beat.error) {
    return `<div ${base.replace('class="shot', 'class="shot failed')}>failed — click to retry</div>`;
  }
  return `<div ${base.replace('class="shot', 'class="shot pending')}>${beat.prompt ? 'queued' : 'waiting'}</div>`;
}

// ---------------------------------------------------------------- modal
function openModal(index) {
  const beat = (currentJob.beats || [])[index];
  if (!beat) return;
  openBeat = index;
  $('m-img').src = beat.file
    ? `/api/jobs/${currentJob.id}/image/${index}?v=${beat.version || 0}`
    : '';
  $('m-img').classList.toggle('hidden', !beat.file);
  $('m-meta').textContent =
    `Shot ${index + 1} of ${currentJob.beats.length} · ${timecode(beat.start)} → ${timecode(beat.end)} · ${(beat.end - beat.start).toFixed(1)}s · ${beat.shot || '—'} · ${beat.motion || '—'}`;
  $('m-narration').textContent = beat.text || '(no narration in this beat)';
  $('m-prompt').value = beat.prompt || '';
  $('m-status').textContent = beat.error ? beat.error : '';
  $('modal').classList.add('open');
}

function closeModal() {
  $('modal').classList.remove('open');
  openBeat = null;
}

async function regenerateOpen() {
  if (openBeat === null) return;
  $('m-regen').disabled = true;
  $('m-status').textContent = 'Generating...';
  try {
    const beat = await api(`/api/jobs/${currentJob.id}/regenerate`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ index: openBeat, prompt: $('m-prompt').value }),
    });
    currentJob.beats[openBeat] = beat;
    $('m-status').textContent = beat.error ? beat.error : 'Updated.';
    if (beat.file) {
      $('m-img').classList.remove('hidden');
      $('m-img').src = `/api/jobs/${currentJob.id}/image/${openBeat}?v=${beat.version || Date.now()}`;
    }
    renderGallery(currentJob, false);
  } catch (err) {
    $('m-status').textContent = err.message;
  } finally {
    $('m-regen').disabled = false;
  }
}

async function retryFailed() {
  const failed = (currentJob.beats || []).filter((b) => b.error).map((b) => b.index);
  if (!failed.length) return;
  $('btn-retry-failed').disabled = true;
  for (const index of failed) {
    try {
      currentJob.beats[index] = await api(`/api/jobs/${currentJob.id}/regenerate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ index }),
      });
      renderGallery(currentJob, false);
    } catch (_) { /* keep going through the rest */ }
  }
  $('btn-retry-failed').disabled = false;
  refreshJob(currentJob.id);
}

async function triggerRender() {
  try {
    await api(`/api/jobs/${currentJob.id}/render`, { method: 'POST' });
    openJob(currentJob.id);
  } catch (err) {
    alert(err.message);
  }
}

async function cancelJob() {
  await api(`/api/jobs/${currentJob.id}/cancel`, { method: 'POST' }).catch(() => {});
}

// ---------------------------------------------------------------- job list
async function loadJobs() {
  const { jobs } = await api('/api/jobs').catch(() => ({ jobs: [] }));
  $('recent-panel').classList.toggle('hidden', !jobs.length);
  $('joblist').innerHTML = jobs.slice(0, 12).map((job) => `
    <div class="jobrow" data-id="${job.id}">
      <div>
        <div class="t">${esc(job.title) || '(untitled)'}</div>
        <div class="s">${humanDuration(job.audio && job.audio.duration)} · ${job.image_count}/${job.beat_count} images</div>
      </div>
      <div class="spacer"></div>
      <span class="tag ${job.status === 'done' ? 'done' : job.status === 'error' ? 'error' : 'running'}">${job.status}</span>
    </div>`).join('');
  $('joblist').querySelectorAll('.jobrow').forEach((row) =>
    row.addEventListener('click', () => openJob(row.dataset.id)));
}

boot();

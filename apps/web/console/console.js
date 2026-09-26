// Developer platform console: ingestion, agent commands, documents, intelligence.
// Identity is owned by auth.js; every call carries the server-verified headers.
(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const state = { entities: [], lastApproval: null, jobs: new Map(), shownJob: null, reviewJob: null };

  function headers(json) {
    return window.SaffronAuth.headers(json);
  }

  // `options.raw` returns the Response untouched (used for file downloads);
  // otherwise JSON bodies are parsed and anything else is returned as-is.
  async function api(path, options = {}) {
    const { json, raw, ...rest } = options;
    const init = { ...rest, headers: { ...headers(Boolean(json)), ...(options.headers || {}) } };
    if (json) init.body = JSON.stringify(json);
    const response = await fetch(path, init);
    const type = response.headers.get('content-type') || '';
    if (!response.ok) {
      let message = `Request failed (${response.status})`;
      try {
        const data = await response.json();
        message = data.detail || data.error?.message || message;
      } catch { /* non-JSON error */ }
      const error = new Error(message);
      error.status = response.status;
      throw error;
    }
    if (raw) return response;
    return type.includes('application/json') ? response.json() : response;
  }

  // Report downloads must carry the same verified identity as every other
  // call, and a plain anchor navigation cannot send the Authorization header.
  // So the file is fetched with the api() helper and handed to the browser as
  // an object URL behind a temporary anchor with the `download` attribute.
  function fileNameFromDisposition(header, fallback) {
    const match = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(header || '');
    return match ? decodeURIComponent(match[1]) : fallback;
  }

  async function downloadReport(path, fallbackName) {
    const response = await api(path, { raw: true });
    const blob = await response.blob();
    const name = fileNameFromDisposition(response.headers.get('content-disposition'), fallbackName || 'report');
    const url = URL.createObjectURL(blob);
    try {
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = name;
      anchor.rel = 'noopener';
      document.body.append(anchor);
      anchor.click();
      anchor.remove();
    } finally {
      // Give the click a tick to start before the URL is revoked.
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    }
  }

  // Every element rendered with data-download="/v1/reports/<id>/download"
  // becomes an authenticated download; data-file-name is the fallback name.
  function bindDownloads(root) {
    root.querySelectorAll('[data-download]').forEach((button) => button.addEventListener('click', async () => {
      button.disabled = true;
      try {
        await downloadReport(button.dataset.download, button.dataset.fileName);
      } catch (error) {
        toast(error.message);
      } finally {
        button.disabled = false;
      }
    }));
  }

  async function upload(path, file, fields) {
    const form = new FormData();
    form.append('file', file, file.name);
    Object.entries(fields).forEach(([key, value]) => { if (value !== '' && value != null) form.append(key, value); });
    const response = await fetch(path, { method: 'POST', headers: headers(false), body: form });
    if (!response.ok) {
      let message = `Upload failed (${response.status})`;
      try { const data = await response.json(); message = data.detail || data.error?.message || message; } catch { /* ignore */ }
      throw new Error(message);
    }
    return response.json();
  }

  function toast(message) {
    const node = $('toast');
    node.textContent = message;
    node.classList.add('show');
    clearTimeout(state.toastTimer);
    state.toastTimer = setTimeout(() => node.classList.remove('show'), 4000);
  }

  function setStatus(label, kind) {
    $('api-status').className = `status ${kind}`;
    $('api-status-label').textContent = label;
  }

  function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
  }

  function pill(status, kind) {
    kind = kind ?? (['imported', 'complete', 'success', 'succeeded', 'indexed', 'sent', 'approved'].includes(status) ? 'ok'
      : ['needs_review', 'partial', 'needs_input', 'approval_required', 'queued', 'processing', 'accepted', 'pending', 'review'].includes(status) ? 'warn'
        : ['failed', 'refused', 'denied', 'rejected'].includes(status) ? 'bad' : '');
    return `<span class="pill ${kind}">${escapeHtml(status)}</span>`;
  }

  function table(columns, rows, render) {
    if (!rows.length) return '<p class="muted">Nothing yet.</p>';
    const head = columns.map((column) => `<th>${escapeHtml(column)}</th>`).join('');
    const body = rows.map((row) => `<tr>${render(row).map((cell) => `<td>${cell}</td>`).join('')}</tr>`).join('');
    return `<table class="table"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
  }

  // ---------------------------------------------------------------- navigation
  document.querySelectorAll('.platform-nav button').forEach((button) => {
    button.addEventListener('click', () => {
      document.querySelectorAll('.platform-nav button').forEach((item) => item.classList.toggle('active', item === button));
      document.querySelectorAll('.platform-panel').forEach((panel) => panel.classList.toggle('active', panel.id === `panel-${button.dataset.panel}`));
      if (button.dataset.panel === 'ingestion') loadJobs();
      if (button.dataset.panel === 'documents') loadDocuments();
      if (button.dataset.panel === 'intelligence') loadProfile();
      if (button.dataset.panel === 'activity') { loadReports(); loadNotifications(); }
    });
  });

  // ------------------------------------------------------------------ commands
  // `command` is the text that was sent; confirming its approval re-sends exactly it.
  function renderCommandResult(data, command) {
    const box = $('command-result');
    box.hidden = false;
    const sources = (data.sources || []).map((source) => source.url
      ? `<li><a href="${escapeHtml(source.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(source.title)}</a>${source.published_at ? ` · ${escapeHtml(source.published_at.slice(0, 10))}` : ''}</li>`
      : `<li>${escapeHtml(source.title)} · ${escapeHtml(source.locator)}</li>`).join('');
    const artifacts = (data.artifacts || []).filter((item) => item.download_path).map((item) => `<li><button class="link-button" type="button" data-download="${escapeHtml(item.download_path)}" data-file-name="${escapeHtml(item.file_name || item.title || 'download')}">${escapeHtml(item.title || item.file_name || 'download')}</button> (${escapeHtml(item.format)}, ${item.row_count} rows)</li>`).join('');
    const steps = (data.steps || []).map((step) => `${escapeHtml(step.tool)} ${pill(step.status)}`).join(' → ');
    const warnings = (data.warnings || []).map((warning) => `<li>${escapeHtml(warning.message)}</li>`).join('');
    box.innerHTML = `${pill(data.status)} <strong>${escapeHtml(data.intent || '')}</strong>\n${escapeHtml(data.answer)}`
      + (steps ? `<div class="result-meta">Plan: ${steps}</div>` : '')
      + (sources ? `<ul class="source-list">${sources}</ul>` : '')
      + (artifacts ? `<ul class="source-list">${artifacts}</ul>` : '')
      + (warnings ? `<ul class="source-list muted">${warnings}</ul>` : '')
      + (data.job_id ? `<div class="result-meta">Background job ${escapeHtml(data.job_id)}; check Reports &amp; notifications when it finishes.</div>` : '');
    bindDownloads(box);
    const approvalBox = $('command-approval');
    if (data.status === 'approval_required' && data.approval) {
      state.lastApproval = { id: data.approval.approval_id, command };
      approvalBox.hidden = false;
      approvalBox.innerHTML = `<div class="result-box">This action changes institutional records: <strong>${escapeHtml(data.approval.tool_name)}</strong> ${escapeHtml(JSON.stringify(data.approval.arguments))}
<button id="approval-confirm" class="btn" type="button">Confirm and run</button> <button id="approval-reject" class="btn danger" type="button">Cancel</button></div>`;
      $('approval-confirm').addEventListener('click', () => decideApproval(true));
      $('approval-reject').addEventListener('click', () => decideApproval(false));
    } else {
      approvalBox.hidden = true;
      approvalBox.innerHTML = '';
      state.lastApproval = null;
    }
  }

  // Returns the error when the command could not be run or answered.
  async function runCommand(approvalId) {
    const text = $('command-input').value.trim();
    if (!text) return null;
    if (!approvalId) {
      // A new command: a confirmation still showing belongs to an earlier one
      // and must not be confirmed while this one is pending.
      $('command-approval').hidden = true;
      $('command-approval').innerHTML = '';
      state.lastApproval = null;
    }
    $('command-run').disabled = true;
    try {
      const data = await api('/v1/agent/commands', {
        method: 'POST',
        json: { command: text, channel: 'text', run_in_background: $('command-background').value === 'true', approval_id: approvalId || null, include_data: false },
      });
      renderCommandResult(data, text);
      return null;
    } catch (error) {
      toast(error.message);
      return error;
    } finally {
      $('command-run').disabled = false;
    }
  }

  // The id and its command are taken before any await: the re-sent command may
  // ask for a new confirmation, whose id and buttons must survive this call returning.
  async function decideApproval(approve) {
    const approval = state.lastApproval;
    if (!approval) return;
    state.lastApproval = null;
    setBusy($('command-approval'), true);
    try {
      await api(`/v1/agent/approvals/${encodeURIComponent(approval.id)}`, { method: 'POST', json: { approve } });
    } catch (error) {
      toast(error.message);
      // An expired or already decided confirmation cannot be used again: the command must be sent anew.
      $('command-approval').hidden = true;
      return;
    }
    if (!approve) {
      toast('Action cancelled.');
      $('command-approval').hidden = true;
      return;
    }
    // Re-send the command this confirmation was issued for, not the last one typed.
    $('command-input').value = approval.command;
    const failed = await runCommand(approval.id);
    if (failed) {
      // The answer was lost, not necessarily the change (a timeout can come after it was made).
      $('command-approval').hidden = true;
      toast(`The confirmed change may or may not have been made (${failed.message}). Check the record before sending the command again.`);
    }
  }

  async function loadTools() {
    try {
      const data = await api('/v1/agent/tools');
      const groups = {};
      data.tools.forEach((tool) => { (groups[tool.group] = groups[tool.group] || []).push(tool); });
      $('tool-list').innerHTML = Object.entries(groups).map(([group, tools]) => `<div><strong>${escapeHtml(group)}</strong>: ${tools.map((tool) => `<span title="${escapeHtml(tool.description)}">${escapeHtml(tool.name)}</span>`).join(', ')}</div>`).join('') || '<p class="muted">No tools are available to your role.</p>';
    } catch (error) {
      $('tool-list').textContent = error.message;
    }
  }

  $('command-run').addEventListener('click', () => runCommand(null));
  $('command-input').addEventListener('keydown', (event) => { if (event.key === 'Enter') runCommand(null); });

  // ---------------------------------------------------------------- ingestion
  // Processing, and with the thread or SQS queue even the start of it, runs on
  // the server's job queue: a job is followed until it settles, so its review
  // opens and its result shows without pressing Refresh.
  const RUNNING = ['queued', 'processing'];
  const FOLLOW_MS = 10 * 60 * 1000;
  // A job still "processing" after this long lost its run (a restart or a
  // crash); the server restarts it on Retry and leaves a live run alone.
  const STALLED_MS = 15 * 60 * 1000;
  const following = new Set();

  async function loadEntities() {
    try {
      const data = await api('/v1/ingestion/entities');
      state.entities = data.entities;
      const select = $('upload-entity');
      data.entities.forEach((entity) => {
        const option = document.createElement('option');
        option.value = entity.name;
        option.textContent = `${entity.name} — ${entity.description}`;
        select.append(option);
      });
    } catch { /* role may not ingest */ }
  }

  function importCounts(result) {
    const reasons = Object.entries(result.skipped_reasons || {}).map(([reason, count]) => `${reason.replace(/_/g, ' ')} ${count}`);
    return `+${result.inserted} / ~${result.updated} / =${result.unchanged} / skip ${result.skipped}${reasons.length ? ` (${reasons.join(', ')})` : ''}`;
  }

  // An import that brought in nothing is not a success, whatever its status says.
  function jobPill(job) {
    const result = job.report?.import;
    const empty = job.status === 'imported' && result && !(result.inserted + result.updated + result.unchanged);
    return pill(job.status, empty ? 'warn' : undefined);
  }

  // Where a job stands and what the person does next.
  function jobSummary(job) {
    const result = job.report?.import;
    if (job.status === 'imported') {
      if (!result) return 'Imported.';
      return `Imported: ${result.inserted} new, ${result.updated} updated, ${result.unchanged} unchanged, ${result.skipped} skipped${result.message ? ` (${result.message})` : ''}.`;
    }
    if (job.status === 'ready') return `Ready to import: press Commit.${job.error ? ` ${job.error}` : ''}`;
    if (job.status === 'needs_review') return job.stage === 'duplicate_review' ? 'Possible duplicates need a decision before the import.' : 'The column mapping needs your review.';
    if (job.status === 'failed') return `Failed: ${job.error || 'no reason was recorded'}. Press Retry once the cause is fixed.`;
    return `Working on it (${job.stage})…`;
  }

  function stalled(job) {
    return job.status === 'processing' && Date.now() - Date.parse(job.updated_at) > STALLED_MS;
  }

  function jobAction(job) {
    const id = escapeHtml(job.job_id);
    if (job.status === 'needs_review') return `<button class="btn secondary" data-review="${id}" type="button">Review</button>`;
    if (job.status === 'ready') return `<button class="btn" data-commit="${id}" type="button">Commit</button>`;
    if (job.status === 'failed' || stalled(job)) return `<button class="btn secondary" data-retry="${id}" type="button">Retry</button>`;
    return '';
  }

  function renderJobs(jobs) {
    $('jobs-table').innerHTML = table(['Job', 'Entity', 'Status', 'Stage', 'Rows', 'Import', ''], jobs, (job) => [
      `<code>${escapeHtml(job.job_id.slice(0, 12))}</code><br><span class="muted">${escapeHtml(job.created_at.slice(0, 19))}</span>`,
      escapeHtml(job.entity || '—'),
      jobPill(job),
      escapeHtml(job.stage),
      String(job.row_count ?? ''),
      job.report?.import ? escapeHtml(importCounts(job.report.import)) + (job.report.import.message ? `<br><span class="muted">${escapeHtml(job.report.import.message)}</span>` : '')
        : (job.error ? `<span class="muted">${escapeHtml(job.error)}</span>` : ''),
      jobAction(job),
    ]);
    $('jobs-table').querySelectorAll('[data-review]').forEach((button) => button.addEventListener('click', () => openReview(button.dataset.review)));
    $('jobs-table').querySelectorAll('[data-commit]').forEach((button) => button.addEventListener('click', () => commitJob(button.dataset.commit, button)));
    $('jobs-table').querySelectorAll('[data-retry]').forEach((button) => button.addEventListener('click', () => retryJob(button.dataset.retry, button)));
  }

  async function loadJobs() {
    try {
      const data = await api('/v1/ingestion/jobs?limit=25');
      state.jobs = new Map(data.jobs.map((job) => [job.job_id, job]));
      renderJobs(data.jobs);
      // The result box shows the same state as its row, not an older poll.
      if (state.jobs.has(state.shownJob)) showJobResult(state.jobs.get(state.shownJob));
    } catch (error) {
      $('jobs-table').innerHTML = `<p class="muted">${escapeHtml(error.message)}</p>`;
    }
  }

  function showJobResult(job) {
    if (job.job_id !== state.shownJob) return;
    const box = $('upload-result');
    box.hidden = false;
    const more = (job.sibling_job_ids || []).length;
    box.innerHTML = `${jobPill(job)} Job ${escapeHtml(job.job_id)} · stage ${escapeHtml(job.stage)} · entity ${escapeHtml(job.entity || 'detecting')}\n${escapeHtml(jobSummary(job))}`
      + (more ? `\n${more} more sheet${more === 1 ? ' was' : 's were'} queued as separate jobs (see Recent jobs).` : '');
  }

  // After every step: show the job, then open what needs the person next, or
  // follow the job while the server is still working on it. A job followed in
  // the background never takes over a review card that shows another job.
  function settle(job, { background = false } = {}) {
    showJobResult(job);
    loadJobs();
    if (job.status === 'needs_review') {
      if (!background || $('review-card').hidden || state.reviewJob === job.job_id) openReview(job.job_id);
      else toast(`Job ${job.job_id.slice(0, 12)} needs review: press Review in Recent jobs.`);
      return;
    }
    if (state.reviewJob === job.job_id) {
      $('review-card').hidden = true;
      state.reviewJob = null;
    }
    if (RUNNING.includes(job.status) && !stalled(job)) followJob(job.job_id);
  }

  // ``unchangedSince`` keeps following a job the queue has not picked up yet
  // (a retried job still reads "failed" until a worker starts it).
  async function followJob(jobId, { unchangedSince = null } = {}) {
    if (following.has(jobId)) return;
    following.add(jobId);
    try {
      const deadline = Date.now() + FOLLOW_MS;
      let delay = 800;
      while (Date.now() < deadline) {
        await new Promise((resolve) => { setTimeout(resolve, delay); });
        delay = Math.min(delay * 1.5, 8000);
        let job;
        try {
          job = (await api(`/v1/ingestion/jobs/${encodeURIComponent(jobId)}`)).job;
        } catch (error) {
          if (error.status === 403 || error.status === 404) return;
          continue;
        }
        if (RUNNING.includes(job.status) || (unchangedSince && job.updated_at === unchangedSince)) {
          showJobResult(job);
          continue;
        }
        following.delete(jobId);
        settle(job, { background: true });
        return;
      }
      loadJobs();
    } finally {
      following.delete(jobId);
    }
  }

  // Where the job really stands after a step failed: an approval that timed out
  // may still finish, and one that failed may have moved the job on. A mapping
  // still under review keeps its card, so the reviewer can fix the selection.
  async function refreshJob(jobId, { keepMappingReview = false } = {}) {
    try {
      const { job } = await api(`/v1/ingestion/jobs/${encodeURIComponent(jobId)}`);
      if (keepMappingReview && job.status === 'needs_review' && job.stage === 'mapping_review') {
        showJobResult(job);
        return;
      }
      settle(job);
    } catch {
      loadJobs();
    }
  }

  async function commitJob(jobId, button) {
    if (button) button.disabled = true;
    try {
      const { job } = await api(`/v1/ingestion/jobs/${encodeURIComponent(jobId)}/commit`, { method: 'POST', json: {} });
      state.shownJob = jobId;
      toast(jobSummary(job));
      settle(job);
    } catch (error) {
      toast(error.message);
      refreshJob(jobId);
    }
  }

  async function retryJob(jobId, button) {
    if (button) button.disabled = true;
    const before = state.jobs.get(jobId)?.updated_at;
    try {
      const { job } = await api(`/v1/ingestion/jobs/${encodeURIComponent(jobId)}/retry`, { method: 'POST', json: {} });
      state.shownJob = jobId;
      toast('Retrying the job.');
      if (job.updated_at === before) {
        showJobResult(job);
        followJob(jobId, { unchangedSince: before });
      } else {
        settle(job);
      }
    } catch (error) {
      toast(error.message);
      loadJobs();
    }
  }

  function setBusy(root, busy) {
    root.querySelectorAll('button, select').forEach((element) => { element.disabled = busy; });
  }

  function entityFields(name) {
    return (state.entities.find((item) => item.name === name) || { fields: [] }).fields;
  }

  function fieldOptions(entityName, selected) {
    const fields = entityFields(entityName);
    return `<option value="">— keep as extra attribute —</option>${fields.map((field) => `<option value="${escapeHtml(field.name)}"${field.name === selected ? ' selected' : ''}>${escapeHtml(field.name)}${field.required ? ' *' : ''}</option>`).join('')}`;
  }

  // The detected entity and its runners-up first, then every other entity.
  function entityChoices(payload) {
    const names = [payload.entity, ...(payload.entity_alternatives || []).map((item) => item.entity), ...state.entities.map((item) => item.name)];
    return [...new Set(names.filter(Boolean))];
  }

  function missingRequired(entityName, mapped) {
    const chosen = new Set(mapped);
    // First and last name together stand in for the name.
    if (chosen.has('first_name') || chosen.has('last_name')) chosen.add('name');
    return entityFields(entityName).filter((field) => field.required && !chosen.has(field.name)).map((field) => field.name);
  }

  function mappingReview(jobId, review) {
    const payload = review.payload;
    const block = document.createElement('div');
    block.className = 'result-box';
    const reasons = (payload.review_required || []).map((item) => `${item.source_header} (${item.reason || 'uncertain'})`).join('; ') || 'none';
    const doubt = (payload.previous_error ? `The last attempt failed: ${payload.previous_error}. ` : '')
      + (payload.entity_uncertain ? `The records were detected as ${payload.entity}, but not with certainty: check the kind of records. ` : '');
    const entities = entityChoices(payload).map((name) => `<option value="${escapeHtml(name)}"${name === payload.entity ? ' selected' : ''}>${escapeHtml(name)}</option>`).join('');
    const rows = payload.headers.map((header) => `<div>${escapeHtml(header)}</div><div><select data-header="${escapeHtml(header)}">${fieldOptions(payload.entity, payload.proposed_mapping[header])}</select></div><div class="muted">${escapeHtml((payload.samples[header] || []).join(' | ').slice(0, 40))}</div>`).join('');
    block.innerHTML = `<strong>Column mapping</strong><div class="field-row stack-sm"><label>Records in this file <select data-entity>${entities}</select></label></div><div class="muted stack-sm">${escapeHtml(doubt)}Review required: ${escapeHtml(reasons)}. Missing required: <span data-missing></span>.</div><div class="mapping-grid stack-sm"><div class="muted">Source header</div><div class="muted">Canonical field</div><div class="muted">Sample</div>${rows}</div><div class="stack"><button class="btn" data-approve type="button">Approve mapping</button></div>`;
    const entitySelect = block.querySelector('select[data-entity]');
    const selects = [...block.querySelectorAll('select[data-header]')];
    const showMissing = () => {
      // Without the entity's field list the choices cannot be checked (or even offered): say so rather than "none".
      const known = entityFields(entitySelect.value).length > 0;
      const missing = known ? missingRequired(entitySelect.value, selects.map((select) => select.value).filter(Boolean)) : [];
      block.querySelector('[data-missing]').textContent = known ? (missing.join(', ') || 'none') : 'unknown (the field list could not be loaded; reload the page)';
      block.querySelector('[data-approve]').disabled = !known;
    };
    entitySelect.addEventListener('change', () => {
      // Keep each choice the new entity also has; the rest start unmapped.
      selects.forEach((select) => { select.innerHTML = fieldOptions(entitySelect.value, select.value); });
      showMissing();
    });
    selects.forEach((select) => select.addEventListener('change', () => { select.classList.remove('invalid'); showMissing(); }));
    showMissing();
    block.querySelector('[data-approve]').addEventListener('click', async () => {
      const mapping = {};
      const byField = new Map();
      selects.forEach((select) => {
        mapping[select.dataset.header] = select.value || null;
        select.classList.remove('invalid');
        if (select.value) byField.set(select.value, [...(byField.get(select.value) || []), select]);
      });
      // One canonical field takes one column; say which ones clash instead of sending a request the server refuses.
      const clash = [...byField.entries()].find(([, list]) => list.length > 1);
      if (clash) {
        clash[1].forEach((select) => select.classList.add('invalid'));
        toast(`${clash[1].map((select) => select.dataset.header).join(' and ')} are both mapped to ${clash[0]}: keep it on one and set the other to “keep as extra attribute”.`);
        return;
      }
      setBusy(block, true);
      try {
        const { job } = await api(`/v1/ingestion/jobs/${encodeURIComponent(jobId)}/mapping`, { method: 'POST', json: { mapping, entity: entitySelect.value, remember: true } });
        state.shownJob = jobId;
        toast(`Mapping approved. ${jobSummary(job)}`);
        settle(job);
      } catch (error) {
        setBusy(block, false);
        showMissing();
        toast(error.message);
        refreshJob(jobId, { keepMappingReview: true });
      }
    });
    return block;
  }

  function duplicateReview(jobId, review) {
    const payload = review.payload;
    // Two copies of one identifier can only become one record: the choice is which copy.
    const conflict = payload.kind === 'conflicting_key';
    const [title, keep, other] = conflict
      ? ['Same identifier, different details', 'Keep the earlier row (skip this one)', 'Use this row instead']
      : ['Possible duplicate', 'Same person (skip the new row)', 'Different people (import)'];
    const differing = conflict && payload.evidence?.differing_fields?.length ? ` · differs in ${payload.evidence.differing_fields.join(', ')}` : '';
    const block = document.createElement('div');
    block.className = 'result-box';
    block.innerHTML = `<strong>${title}</strong> (${escapeHtml(payload.kind)}, score ${escapeHtml(payload.score)})<div class="muted">${escapeHtml(payload.left_locator)} vs ${escapeHtml(payload.right_locator || payload.record_key || 'existing record')}${escapeHtml(differing)} · ${escapeHtml(JSON.stringify(payload.evidence || {}))}</div><div class="stack-sm"><button class="btn" data-decision="approved" type="button">${keep}</button> <button class="btn secondary" data-decision="rejected" type="button">${other}</button></div>`;
    block.querySelectorAll('button[data-decision]').forEach((button) => button.addEventListener('click', async () => {
      // One decision at a time: the last one may start the import.
      setBusy($('review-body'), true);
      try {
        const { job } = await api(`/v1/ingestion/reviews/${encodeURIComponent(review.review_id)}`, { method: 'POST', json: { decision: button.dataset.decision } });
        state.shownJob = jobId;
        toast(job.status === 'needs_review' ? 'Decision recorded.' : `Decision recorded. ${jobSummary(job)}`);
        settle(job);
      } catch (error) {
        toast(error.message);
        refreshJob(jobId);
      }
    }));
    return block;
  }

  async function openReview(jobId) {
    const card = $('review-card');
    card.hidden = false;
    state.reviewJob = jobId;
    const body = $('review-body');
    body.innerHTML = '<p class="muted">Loading…</p>';
    try {
      const data = await api(`/v1/ingestion/jobs/${encodeURIComponent(jobId)}`);
      const reviews = data.pending_reviews || [];
      if (reviews.some((review) => review.kind === 'mapping') && !state.entities.length) await loadEntities();
      // A later openReview owns the card; this answer is stale.
      if (state.reviewJob !== jobId) return;
      if (!reviews.length) {
        body.innerHTML = `<p class="muted">Nothing to review. ${escapeHtml(jobSummary(data.job))}</p>`;
        return;
      }
      body.replaceChildren(...reviews.map((review) => (review.kind === 'mapping' ? mappingReview(jobId, review) : duplicateReview(jobId, review))));
    } catch (error) {
      if (state.reviewJob === jobId) body.innerHTML = `<p class="muted">${escapeHtml(error.message)}</p>`;
    }
  }

  $('upload-run').addEventListener('click', async () => {
    const file = $('upload-file').files[0];
    if (!file) { toast('Choose a file first.'); return; }
    $('upload-run').disabled = true;
    try {
      const data = await upload('/v1/ingestion/uploads', file, { entity: $('upload-entity').value, sheet: $('upload-sheet').value, auto_commit: $('upload-autocommit').value });
      state.shownJob = data.job.job_id;
      settle(data.job);
    } catch (error) {
      toast(error.message);
    } finally {
      $('upload-run').disabled = false;
    }
  });
  $('jobs-refresh').addEventListener('click', loadJobs);

  // ---------------------------------------------------------------- documents
  async function loadDocuments() {
    try {
      const data = await api('/v1/documents?limit=50');
      $('docs-table').innerHTML = table(['Title', 'Category', 'Classification', 'Pages', 'Chunks', 'Uploaded'], data.documents, (doc) => [escapeHtml(doc.title), escapeHtml(doc.category), escapeHtml(doc.classification), String(doc.page_count), String(doc.chunk_count), escapeHtml(doc.uploaded_at.slice(0, 19))]);
    } catch (error) {
      $('docs-table').innerHTML = `<p class="muted">${escapeHtml(error.message)}</p>`;
    }
  }
  $('doc-upload').addEventListener('click', async () => {
    const file = $('doc-file').files[0];
    if (!file) { toast('Choose a file first.'); return; }
    try {
      const data = await upload('/v1/documents', file, { title: $('doc-title').value, classification: $('doc-class').value, category: $('doc-category').value });
      const box = $('doc-upload-result');
      box.hidden = false;
      box.textContent = `Indexed "${data.title}": ${data.chunks} passage(s) from ${data.pages} page(s).${data.warnings.length ? ` Warnings: ${data.warnings.join(', ')}` : ''}`;
      loadDocuments();
    } catch (error) { toast(error.message); }
  });
  $('doc-search').addEventListener('click', async () => {
    const question = $('doc-question').value.trim();
    if (!question) return;
    try {
      const data = await api('/v1/documents/search', { method: 'POST', json: { question, top_k: 5 } });
      const box = $('doc-search-result');
      box.hidden = false;
      box.innerHTML = `${escapeHtml(data.answer)}<ul class="source-list">${data.sources.map((source) => `<li>${escapeHtml(source.title)} · ${escapeHtml(source.locator)} (score ${source.score})</li>`).join('')}</ul>`;
    } catch (error) { toast(error.message); }
  });
  $('docs-refresh').addEventListener('click', loadDocuments);

  // ------------------------------------------------------------- intelligence
  const list = (value) => value.split(',').map((item) => item.trim()).filter(Boolean);
  async function loadProfile() {
    try {
      const data = await api('/v1/intelligence/profile');
      const profile = data.profile;
      $('profile-result').textContent = data.configured ? '' : 'Search provider not configured on the server; profiles can be saved but investigations are unavailable.';
      if (!profile) return;
      $('profile-name').value = profile.name || '';
      $('profile-location').value = profile.location || '';
      $('profile-aliases').value = (profile.aliases || []).join(', ');
      $('profile-domains').value = (profile.official_domains || []).join(', ');
      $('profile-programs').value = (profile.programs || []).join(', ');
      $('profile-exclusions').value = (profile.exclusions || []).join(', ');
      $('profile-monitoring').value = String(Boolean(profile.monitoring_enabled));
      $('profile-alerts').value = (profile.alert_recipients || []).join(', ');
      $('profile-security').value = (profile.security_contacts || []).join(', ');
    } catch (error) {
      $('profile-result').textContent = error.message;
    }
  }
  $('profile-save').addEventListener('click', async () => {
    try {
      await api('/v1/intelligence/profile', { method: 'PUT', json: {
        name: $('profile-name').value, location: $('profile-location').value, aliases: list($('profile-aliases').value), official_domains: list($('profile-domains').value),
        programs: list($('profile-programs').value), exclusions: list($('profile-exclusions').value), monitoring_enabled: $('profile-monitoring').value === 'true', alert_recipients: list($('profile-alerts').value),
        security_contacts: list($('profile-security').value),
      } });
      toast('Profile saved.');
    } catch (error) { toast(error.message); }
  });
  function renderFindings(data) {
    const box = $('intel-result');
    box.hidden = false;
    const findings = (data.findings || data.items || []).map((item) => `<li><a href="${escapeHtml(item.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(item.title)}</a> — ${escapeHtml(item.source_label || item.source_type)}${item.published_at ? ` · ${escapeHtml(item.published_at.slice(0, 10))}` : ' · date not stated'}${item.match_level ? ` · match ${escapeHtml(item.match_level)}` : ''}</li>`).join('');
    box.innerHTML = `${escapeHtml(data.summary)}<ul class="source-list">${findings}</ul>` + (data.excluded ? `<div class="result-meta">Excluded: ${escapeHtml(JSON.stringify(data.excluded))} · queries: ${(data.queries || []).length}</div>` : '') + `<div class="result-meta">Public-web content is untrusted data; every finding links to its source.</div>`;
  }
  $('intel-run').addEventListener('click', async () => {
    try {
      renderFindings(await api('/v1/intelligence/investigate', { method: 'POST', json: { question: $('intel-question').value || null, window_days: Number($('intel-days').value) || 7, max_results: 15 } }));
    } catch (error) { toast(error.message); }
  });
  $('intel-digest').addEventListener('click', async () => {
    try { renderFindings(await api('/v1/intelligence/digest?days=1')); } catch (error) { toast(error.message); }
  });

  // ---------------------------------------------------------------- internet map
  const GRADE_KIND = { O: 'ok', A: 'ok', 'A-arch': 'warn', B: 'ok', C: 'warn', D: 'bad' };
  function gradePill(grade) {
    return grade ? `<span class="pill ${GRADE_KIND[grade] || ''}">${escapeHtml(grade)}</span>` : '<span class="muted">—</span>';
  }
  const percent = (value) => (value == null ? '—' : `${Math.round(value * 1000) / 10}%`);
  const points = (value) => (value == null ? '—' : `${value > 0 ? '+' : ''}${Math.round(value * 1000) / 10} pts`);
  const when = (stamp) => escapeHtml((stamp || '').slice(0, 16).replace('T', ' '));
  // The plan's one number: did the latest tick re-find more of the hidden list without letting a known look-alike through?
  function runSeries(series, cost) {
    const latest = series[0];
    const verdict = latest
      ? `Latest run (${when(latest.started_at)}): holdout recall ${percent(latest.holdout_recall)} (${points(latest.holdout_recall_change)} on the run before), ${latest.canary_leaks_caught ?? '—'} known look-alike(s) caught and set back, precision ${percent(latest.precision)}.`
      : 'The engine has not run yet.';
    const rows = table(['Started', 'Status', 'Sources', 'New', 'Raised', 'Leads', 'Verified', 'Holdout recall', 'Change', 'Look-alikes caught', 'Spend', 'Per verified'], series, (run) => [
      when(run.started_at), `${pill(run.status)}${run.gate ? ` <span class="pill ${run.gate === 'passed' ? 'ok' : 'bad'}">${escapeHtml(run.gate.replace(/_/g, ' '))}</span>` : ''}`,
      String(run.sources ?? '—'), String(run.new_assets ?? '—'), String(run.raised ?? '—'), String(run.leads ?? '—'),
      `${run.verified ?? '—'}${run.verified_change ? ` (${run.verified_change > 0 ? '+' : ''}${run.verified_change})` : ''}`, percent(run.holdout_recall), points(run.holdout_recall_change),
      String(run.canary_leaks_caught ?? '—'), String(run.spend ?? '—'), String(run.cost_per_verified ?? '—'),
    ]);
    const spent = cost ? `Spend so far: ${cost.cost_total} budget units, ${cost.cost_per_verified ?? '—'} per verified item. Units are requests (or a provider's quota units), not money.` : '';
    return `<h4>Runs</h4><p>${verdict}</p>${rows}<p class="result-meta">${spent}</p>`;
  }
  async function loadMap() {
    const box = $('map-result');
    box.hidden = false;
    box.innerHTML = '<p class="muted">Loading…</p>';
    try {
      const data = await api('/v1/intelligence/map/summary');
      const grades = Object.entries(data.by_grade || {}).filter(([, count]) => count).map(([grade, count]) => `${gradePill(grade)} ${count}`).join(' ');
      const coverage = data.coverage || {};
      const estimate = coverage.estimated_total ? `about ${Math.round(coverage.estimated_total)} accounts estimated to exist (${Math.round((coverage.coverage || 0) * 100)}% found by the site or the indexes${coverage.other_known ? `; ${coverage.other_known} more known otherwise, such as imported claims` : ''}; ${escapeHtml(coverage.assumption)})` : 'not enough independent sightings yet to estimate how many accounts exist';
      const platforms = (data.grid && data.grid.platforms) || [];
      const grid = table(['Entity', ...platforms], (data.grid && data.grid.rows) || [], (row) => [escapeHtml(row.entity), ...platforms.map((platform) => gradePill(row.cells[platform].grade))]);
      const shown = data.grid && data.grid.truncated ? ` Showing the first ${data.grid.rows.length} of ${data.grid.entities_total} entities, the institution's own first.` : '';
      const fresh = data.freshness || {};
      const freshness = fresh.verified
        ? `Freshness: of ${fresh.verified} verified, ${percent(fresh.within_7_days)} checked within 7 days, ${percent(fresh.within_30_days)} within 30 and ${percent(fresh.within_90_days)} within 90; median age ${fresh.median_age_days ?? '—'} days${fresh.never_verified ? ` (${fresh.never_verified} never checked live)` : ''}`
        : 'Freshness: nothing verified yet';
      const overdue = fresh.sources_overdue != null ? `; ${fresh.sources_overdue} sources more than a day overdue` : '';
      let manager = '';
      if (data.incidents_open) {
        const incidents = table(['Severity', 'Incident', 'Seen', 'Last seen'], data.incidents_open, (item) => [`<span class="pill ${item.severity === 'high' ? 'bad' : 'warn'}">${escapeHtml(item.severity)}</span>`, escapeHtml(`${item.kind.replace(/_/g, ' ')}: ${item.target}`), String(item.times_seen), escapeHtml((item.last_seen_at || '').slice(0, 10))]);
        const waiting = Object.entries(data.review_waiting || {}).map(([kind, count]) => `${count} ${escapeHtml(kind.replace(/_/g, ' '))}`).join(', ') || 'nothing';
        const truth = data.ground_truth || {};
        manager = `<h4>Open incidents</h4>${incidents}<p class="result-meta">Waiting for review: ${waiting}. Ground truth: holdout recall ${percent(truth.holdout_recall)}, precision ${percent(truth.precision)}, seed verification ${percent(truth.seed_verification_rate)}, look-alike leaks ${(truth.canary_leaks || []).length}.</p>${runSeries(data.series || [], data.cost)}`;
      }
      // Rows the OpenStreetMap connector found carry its ODbL attribution wherever they are shown.
      const osm = (data.connectors || []).some((row) => row.connector === 'openstreetmap' && row.runs) ? '<p class="result-meta">Includes place data © OpenStreetMap contributors (ODbL).</p>' : '';
      box.innerHTML = `<p>${data.verified} verified of ${data.assets} mapped. ${grades}</p><p class="result-meta">Coverage: ${data.grid ? `${data.grid.covered} of ${data.grid.cells} entity–platform cells have a verified account` : ''}; ${estimate}.${shown}</p><p class="result-meta">${freshness}${overdue}.</p>${grid}${manager}${osm}`;
    } catch (error) {
      box.innerHTML = `<p class="muted">${escapeHtml(error.message)}</p>`;
    }
  }
  $('map-load').addEventListener('click', loadMap);

  // ------------------------------------------------------------------ activity
  async function loadReports() {
    try {
      const data = await api('/v1/reports?limit=50');
      $('reports-table').innerHTML = table(['Title', 'Format', 'Rows', 'Created', ''], data.reports, (report) => [escapeHtml(report.title), escapeHtml(report.format), String(report.row_count), escapeHtml(report.created_at.slice(0, 19)), `<button class="btn secondary" type="button" data-download="${escapeHtml(report.download_path)}" data-file-name="${escapeHtml(`${report.title || 'report'}.${report.format || 'csv'}`)}">Download</button>`]);
      bindDownloads($('reports-table'));
    } catch (error) { $('reports-table').innerHTML = `<p class="muted">${escapeHtml(error.message)}</p>`; }
  }
  async function loadNotifications() {
    try {
      const data = await api('/v1/notifications?limit=50');
      $('notifications-table').innerHTML = table(['Title', 'Body', 'Status', 'When'], data.notifications, (item) => [escapeHtml(item.title), escapeHtml(item.body), pill(item.status), escapeHtml(item.created_at.slice(0, 19))]);
    } catch (error) { $('notifications-table').innerHTML = `<p class="muted">${escapeHtml(error.message)}</p>`; }
  }
  $('reports-refresh').addEventListener('click', loadReports);
  $('notifications-refresh').addEventListener('click', loadNotifications);

  // ------------------------------------------------------------------- start
  const DEMO_ROLE_KEY = 'saffron.platform.demoRole';

  function showGate(message) {
    $('sign-in-gate').hidden = false;
    $('platform-body').hidden = true;
    if (message) $('sign-in-message').textContent = message;
  }

  function installDemoRolePicker() {
    // Local development only: the server accepts X-Demo-Role, so the console
    // can be exercised as each institutional role. Production uses OIDC claims.
    const session = window.SaffronAuth.demoSession;
    let saved = null;
    try { saved = window.sessionStorage.getItem(DEMO_ROLE_KEY); } catch { /* storage may be blocked */ }
    session.role = saved || 'principal';
    session.capabilities = [];
    const picker = document.createElement('select');
    picker.className = 'account-button';
    picker.title = 'Demo role (development only)';
    ['student', 'faculty', 'staff', 'hod', 'principal', 'institution_admin'].forEach((role) => {
      const option = document.createElement('option');
      option.value = role;
      option.textContent = `Demo role: ${role}`;
      option.selected = role === session.role;
      picker.append(option);
    });
    picker.addEventListener('change', () => {
      session.role = picker.value;
      try { window.sessionStorage.setItem(DEMO_ROLE_KEY, picker.value); } catch { /* ignore */ }
      window.location.reload();
    });
    $('account').hidden = false;
    $('account-name').textContent = `${session.principal} (${session.college})`;
    $('sign-out').replaceWith(picker);
  }

  async function start() {
    let mode;
    try {
      mode = await window.SaffronAuth.init();
    } catch (error) {
      showGate(error.message);
      setStatus('Signed out', 'bad');
      return;
    }
    if (mode === 'unavailable') {
      showGate('This console is not accepting sign-ins yet. Its identity provider is not configured.');
      setStatus('Sign-in unavailable', 'bad');
      return;
    }
    if (!window.SaffronAuth.isAuthenticated()) {
      showGate();
      setStatus('Signed out', 'neutral');
      return;
    }
    if (mode === 'oidc') {
      $('account').hidden = false;
      $('account-name').textContent = window.SaffronAuth.currentUser();
    } else {
      installDemoRolePicker();
    }
    try {
      const ready = await api('/v1/health/ready');
      setStatus(ready.platform?.enabled ? `Ready · ${ready.platform.institution_database} · ${ready.platform.platform_tools} tools` : 'Platform disabled', ready.status === 'ready' ? 'ok' : 'warn');
    } catch {
      setStatus('API unavailable', 'bad');
    }
    await loadTools();
    await loadEntities();
    loadJobs();
  }

  $('sign-in').addEventListener('click', () => window.SaffronAuth.signIn().catch((error) => toast(error.message)));
  $('sign-out').addEventListener('click', () => window.SaffronAuth.signOut());
  document.addEventListener('DOMContentLoaded', start);
})();

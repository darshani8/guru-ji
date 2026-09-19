const state = {
  voiceSessionId: null,
  voiceTransport: null,
  requestInFlight: false,
};

const session = {
  principal: 'demo-user',
  role: 'student',
  college: 'college_a',
  capabilities: ['ask:read_only', 'voice:start'],
};

const $ = (id) => document.getElementById(id);

function headers(json = false) {
  const result = {
    Authorization: 'Bearer dev-token',
    'X-Demo-Principal': session.principal,
    'X-Demo-Role': session.role,
    'X-Demo-College': session.college,
    'X-Demo-Capabilities': session.capabilities.join(','),
  };
  if (json) result['Content-Type'] = 'application/json';
  return result;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { ...headers(Boolean(options.body)), ...(options.headers || {}) },
  });
  let data = {};
  try {
    data = await response.json();
  } catch {
    // Some health or proxy failures do not return JSON.
  }
  if (!response.ok) {
    throw new Error(data.detail || data.error?.message || `Request failed (${response.status})`);
  }
  return data;
}

function setApiStatus(label, kind = 'neutral') {
  const node = $('api-status');
  node.className = `status ${kind}`;
  $('api-status-label').textContent = label;
}

function showToast(message) {
  const node = $('toast');
  node.textContent = message;
  node.classList.add('show');
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => node.classList.remove('show'), 3600);
}

function scrollHistory() {
  const history = $('chat-history');
  history.scrollTop = history.scrollHeight;
}

function addMessage(kind, text, options = {}) {
  const article = document.createElement('article');
  article.className = `message ${kind}-message${options.voice ? ' voice-reply' : ''}`;

  const avatar = document.createElement('div');
  avatar.className = 'avatar';
  avatar.textContent = kind === 'user' ? 'You' : 'GJ';

  const body = document.createElement('div');
  body.className = 'message-body';

  const label = document.createElement('span');
  label.className = 'message-label';
  label.textContent = kind === 'user' ? 'You' : 'Guru Ji';

  const bubble = document.createElement('div');
  bubble.className = 'bubble';

  if (options.answer) {
    const status = document.createElement('span');
    status.className = `answer-status ${options.answer.status === 'refused' ? 'refused' : options.answer.status === 'partial' ? 'partial' : ''}`;
    status.textContent = options.answer.status || 'complete';
    bubble.append(status);
  }

  if (options.voice && kind === 'assistant') {
    const tag = document.createElement('span');
    tag.className = 'voice-tag';
    tag.textContent = '◉ Voice response';
    bubble.append(tag);
  }

  const copy = document.createElement('div');
  copy.textContent = text;
  bubble.append(copy);

  if (options.answer) {
    const citations = options.answer.citations || [];
    const warnings = options.answer.warnings || [];
    const evidence = document.createElement('div');
    evidence.className = 'evidence-block';
    evidence.textContent = `${citations.length} citation(s)${warnings.length ? ` · ${warnings.length} warning(s)` : ''} · ${options.answer.generation_mode || 'deterministic'}`;
    bubble.append(evidence);
  }

  body.append(label, bubble);
  article.append(avatar, body);
  $('chat-history').append(article);
  scrollHistory();
  return article;
}

function setComposerBusy(busy) {
  state.requestInFlight = busy;
  $('text-input').disabled = busy;
  $('send-button').disabled = busy;
  $('send-button').textContent = busy ? 'Sending…' : 'Send ↗';
}

async function ask(prompt) {
  const text = prompt.trim();
  if (!text || state.requestInFlight) return;

  addMessage('user', text);
  $('text-input').value = '';
  setComposerBusy(true);
  const loading = addMessage('assistant', 'Thinking within approved scope…');
  loading.classList.add('loading-message');

  try {
    const data = await api('/v1/chat', {
      method: 'POST',
      body: JSON.stringify({
        prompt: text,
        institution_scope: { college_id: session.college },
        channel: 'text',
      }),
    });
    loading.remove();
    const answer = data.answer || data;
    addMessage('assistant', answer.answer || answer.refusal_reason || 'No answer returned.', { answer });
  } catch (error) {
    loading.remove();
    addMessage('assistant', error.message);
    showToast(error.message);
  } finally {
    setComposerBusy(false);
    $('text-input').focus();
  }
}

function renderVoiceState(message = '') {
  const active = Boolean(state.voiceSessionId);
  const button = $('voice-button');
  button.classList.toggle('active', active);
  button.setAttribute('aria-pressed', String(active));
  $('voice-button-label').textContent = active ? 'End voice session' : 'Voice Assistant';
  $('voice-status-line').textContent = message;
}

async function startVoiceSession() {
  if (state.voiceSessionId) return;
  $('voice-button').disabled = true;
  renderVoiceState('Starting a bounded voice session…');
  try {
    const data = await api('/v1/voice/sessions', { method: 'POST' });
    state.voiceSessionId = data.session_id;
    state.voiceTransport = data.transport || 'unknown';
    if (state.voiceTransport === 'not_configured') {
      renderVoiceState('Voice session ready. Live audio transport is not configured yet.');
    } else {
      renderVoiceState(`Voice session ready via ${state.voiceTransport}.`);
    }
  } catch (error) {
    renderVoiceState('');
    showToast(error.message);
  } finally {
    $('voice-button').disabled = false;
  }
}

async function closeVoiceSession() {
  if (!state.voiceSessionId) return;
  const sessionId = state.voiceSessionId;
  state.voiceSessionId = null;
  state.voiceTransport = null;
  $('voice-button').disabled = true;
  renderVoiceState('Closing voice session…');
  try {
    await api(`/v1/voice/sessions/${encodeURIComponent(sessionId)}`, { method: 'DELETE' });
    renderVoiceState('Voice session closed.');
  } catch (error) {
    renderVoiceState('Voice session ended locally; server cleanup may retry on expiry.');
    showToast(error.message);
  } finally {
    $('voice-button').disabled = false;
  }
}

async function toggleVoiceSession() {
  if (state.voiceSessionId) await closeVoiceSession();
  else await startVoiceSession();
}

async function loadApiStatus() {
  try {
    const data = await api('/v1/health/live');
    setApiStatus(`API ${data.version || 'ready'}`, 'ok');
  } catch (error) {
    setApiStatus('API unavailable', 'error');
  }
}

$('text-form').addEventListener('submit', (event) => {
  event.preventDefault();
  ask($('text-input').value);
});

$('text-input').addEventListener('keydown', (event) => {
  if (event.key === 'Enter') {
    event.preventDefault();
    ask(event.target.value);
  }
});

$('voice-button').addEventListener('click', toggleVoiceSession);
window.addEventListener('beforeunload', () => {
  if (!state.voiceSessionId) return;
  fetch(`/v1/voice/sessions/${encodeURIComponent(state.voiceSessionId)}`, {
    method: 'DELETE',
    headers: headers(),
    keepalive: true,
  }).catch(() => {});
});

loadApiStatus();

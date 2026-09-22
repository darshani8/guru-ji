const state = {
  voiceSessionId: null,
  voiceSocket: null,
  transportTicket: null,
  recognition: null,
  recognitionSupported: false,
  shouldListen: false,
  waitingForAnswer: false,
  speaking: false,
  voiceTransportReady: false,
  intentionalClose: false,
  requestInFlight: false,
  finalResultKeys: new Set(),
  pingTimer: null,
};

const $ = (id) => document.getElementById(id);
const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
state.recognitionSupported = Boolean(SpeechRecognition);

// Identity is owned by auth.js: it sends a verified OIDC ID token when the
// server asks for one, and only falls back to the fixed demo headers in local
// development, where the server is the side that decides to accept them.
function headers(json = false) {
  return window.GuruAuth.headers(json);
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

  if (options.voice) {
    const tag = document.createElement('span');
    tag.className = 'voice-tag';
    tag.textContent = kind === 'user' ? '◉ Voice input' : '◉ Voice response';
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
        institution_scope: { college_id: window.GuruAuth.collegeId() },
        channel: 'text',
      }),
    });
    loading.remove();
    const answer = data.answer && typeof data.answer === 'object' ? data.answer : data;
    const answerText = typeof data.answer === 'string'
      ? data.answer
      : answer.answer || answer.refusal_reason || 'No answer returned.';
    addMessage('assistant', answerText, { answer });
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
  $('voice-button-label').textContent = active ? 'Stop voice assistant' : 'Voice Assistant';
  $('voice-status-line').textContent = message;
}

function setVoiceButtonDisabled(disabled) {
  $('voice-button').disabled = disabled;
}

function stopRecognition() {
  if (!state.recognition) return;
  try {
    state.recognition.stop();
  } catch {
    // Recognition may already be stopped by the browser.
  }
}

function startRecognition() {
  if (!state.recognition || !state.shouldListen || state.waitingForAnswer || state.speaking) return;
  try {
    state.recognition.start();
  } catch (error) {
    if (error.name !== 'InvalidStateError') showToast('Voice input could not start.');
  }
}

function speakAnswer(text) {
  if (!window.speechSynthesis || !text) {
    renderVoiceState('Answer received. Speech output is unavailable in this browser.');
    state.waitingForAnswer = false;
    startRecognition();
    return;
  }
  state.speaking = true;
  stopRecognition();
  window.speechSynthesis.cancel();
  const utterance = new SpeechSynthesisUtterance(text);
  utterance.lang = 'en-IN';
  utterance.onend = () => {
    state.speaking = false;
    if (state.voiceSessionId) {
      state.waitingForAnswer = false;
      renderVoiceState('Listening…');
      startRecognition();
    }
  };
  utterance.onerror = () => {
    state.speaking = false;
    if (state.voiceSessionId) {
      state.waitingForAnswer = false;
      renderVoiceState('Listening…');
      startRecognition();
    }
  };
  window.speechSynthesis.speak(utterance);
}

function sendUtterance(text) {
  const value = text.trim();
  if (!value || !state.voiceTransportReady || state.waitingForAnswer || !state.voiceSocket || state.voiceSocket.readyState !== WebSocket.OPEN) return;
  const clientMessageId = `voice-${crypto.randomUUID ? crypto.randomUUID() : Date.now()}`;
  state.waitingForAnswer = true;
  stopRecognition();
  state.voiceSocket.send(JSON.stringify({
    type: 'utterance',
    client_message_id: clientMessageId,
    text: value,
  }));
  addMessage('user', value, { voice: true });
  renderVoiceState('Guru Ji is preparing a cited answer…');
}

function configureRecognition() {
  if (!SpeechRecognition) return null;
  const recognition = new SpeechRecognition();
  recognition.continuous = true;
  recognition.interimResults = true;
  recognition.lang = 'en-IN';

  recognition.onstart = () => {
    renderVoiceState('Listening…');
  };
  recognition.onresult = (event) => {
    let interim = '';
    for (let index = event.resultIndex; index < event.results.length; index += 1) {
      const result = event.results[index];
      const transcript = result[0]?.transcript?.trim() || '';
      if (!result.isFinal) {
        interim += transcript;
        continue;
      }
      const resultKey = `${index}:${transcript}`;
      if (transcript && !state.finalResultKeys.has(resultKey)) {
        state.finalResultKeys.add(resultKey);
        if (state.finalResultKeys.size > 100) state.finalResultKeys.clear();
        sendUtterance(transcript);
      }
    }
    if (interim && !state.waitingForAnswer) renderVoiceState(`Hearing: “${interim}”`);
  };
  recognition.onerror = (event) => {
    if (!state.voiceSessionId) return;
    if (event.error === 'not-allowed' || event.error === 'service-not-allowed') {
      handleVoiceFailure('Microphone permission was denied.');
      return;
    }
    if (event.error !== 'aborted' && event.error !== 'no-speech') {
      renderVoiceState(`Voice input error: ${event.error}.`);
    }
  };
  recognition.onend = () => {
    if (state.shouldListen && state.voiceSessionId && !state.waitingForAnswer && !state.speaking) {
      window.setTimeout(startRecognition, 120);
    }
  };
  return recognition;
}

function handleVoiceMessage(event) {
  let message;
  try {
    message = JSON.parse(event.data);
  } catch {
    handleVoiceFailure('The voice transport returned invalid data.');
    return;
  }

  if (message.type === 'ready') {
    state.voiceTransportReady = true;
    renderVoiceState('Listening…');
    startRecognition();
    return;
  }
  if (message.type === 'answer') {
    const answer = message.answer || {};
    const text = answer.answer || answer.refusal_reason || 'No answer returned.';
    addMessage('assistant', text, { answer, voice: true });
    state.waitingForAnswer = false;
    speakAnswer(text);
    return;
  }
  if (message.type === 'pong') return;
  if (message.type === 'expired') {
    handleVoiceFailure('The voice session expired. Start Voice Assistant again.');
    return;
  }
  if (message.type === 'error') {
    state.waitingForAnswer = false;
    renderVoiceState(message.message || 'Voice transport error.');
    if (!state.speaking) startRecognition();
  }
}

function startTransport(data) {
  return new Promise((resolve, reject) => {
    let settled = false;
    const socket = new WebSocket(data.websocket_url);
    state.voiceSocket = socket;
    socket.onopen = () => {
      socket.send(JSON.stringify({ type: 'auth', ticket: data.transport_ticket }));
    };
    socket.onmessage = (event) => {
      handleVoiceMessage(event);
      if (!settled) {
        try {
          const message = JSON.parse(event.data);
          if (message.type === 'ready') {
            settled = true;
            resolve();
          } else if (message.type === 'error' || message.type === 'expired') {
            settled = true;
            reject(new Error(message.message || 'Voice transport authentication failed.'));
          }
        } catch {
          // The regular message handler reports malformed payloads.
        }
      }
    };
    socket.onerror = () => {
      if (!settled) {
        settled = true;
        reject(new Error('Live voice transport could not connect.'));
      }
      if (state.voiceSessionId && !state.intentionalClose) handleVoiceFailure('Live voice transport disconnected.');
    };
    socket.onclose = () => {
      clearInterval(state.pingTimer);
      state.pingTimer = null;
      if (!settled) {
        settled = true;
        reject(new Error('Live voice transport closed before it was ready.'));
      }
      if (state.voiceSessionId && !state.intentionalClose) handleVoiceFailure('Live voice transport closed.');
    };
  });
}

async function requestMicrophonePermission() {
  if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
    throw new Error('Voice input requires HTTPS or localhost and microphone access.');
  }
  const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  stream.getTracks().forEach((track) => track.stop());
}

async function startVoiceSession() {
  if (state.voiceSessionId) return;
  if (!state.recognitionSupported) {
    renderVoiceState('Voice input is not supported in this browser.');
    showToast('Use a browser with SpeechRecognition support or type your question.');
    return;
  }

  setVoiceButtonDisabled(true);
  renderVoiceState('Requesting microphone permission…');
  try {
    await requestMicrophonePermission();
    const data = await api('/v1/voice/sessions', {
      method: 'POST',
      body: JSON.stringify({ college_id: window.GuruAuth.collegeId() }),
    });
    if (data.transport !== 'browser_web_speech_ws' || !data.transport_ticket || !data.websocket_url) {
      throw new Error('The server did not provide a supported live voice transport.');
    }
    state.voiceSessionId = data.session_id;
    state.transportTicket = data.transport_ticket;
    state.intentionalClose = false;
    state.shouldListen = true;
    state.voiceTransportReady = false;
    state.finalResultKeys.clear();
    state.recognition = configureRecognition();
    renderVoiceState('Connecting live voice transport…');
    await startTransport(data);
    state.pingTimer = window.setInterval(() => {
      if (state.voiceSocket?.readyState === WebSocket.OPEN) state.voiceSocket.send(JSON.stringify({ type: 'ping' }));
    }, 20_000);
  } catch (error) {
    await cleanupVoiceSession(true);
    renderVoiceState('');
    showToast(error.message || 'Voice Assistant could not start.');
  } finally {
    setVoiceButtonDisabled(false);
  }
}

async function cleanupVoiceSession(closeServerSession = true) {
  const sessionId = state.voiceSessionId;
  state.shouldListen = false;
  state.waitingForAnswer = false;
  state.speaking = false;
  state.intentionalClose = true;
  clearInterval(state.pingTimer);
  state.pingTimer = null;
  stopRecognition();
  if (window.speechSynthesis) window.speechSynthesis.cancel();
  if (state.voiceSocket) {
    try {
      if (state.voiceSocket.readyState === WebSocket.OPEN) {
        state.voiceSocket.send(JSON.stringify({ type: 'close' }));
      }
      state.voiceSocket.close(1000, 'client_closed');
    } catch {
      // The socket may already be closed.
    }
  }
  state.voiceSocket = null;
  state.transportTicket = null;
  state.voiceTransportReady = false;
  state.recognition = null;
  state.voiceSessionId = null;
  if (closeServerSession && sessionId) {
    try {
      await api(`/v1/voice/sessions/${encodeURIComponent(sessionId)}`, { method: 'DELETE' });
    } catch {
      // The session manager will expire abandoned sessions safely.
    }
  }
}

async function closeVoiceSession() {
  setVoiceButtonDisabled(true);
  renderVoiceState('Closing voice session…');
  await cleanupVoiceSession(true);
  renderVoiceState('Voice session closed.');
  setVoiceButtonDisabled(false);
}

async function handleVoiceFailure(message) {
  if (!state.voiceSessionId) return;
  await cleanupVoiceSession(true);
  renderVoiceState(message);
  showToast(message);
}

async function toggleVoiceSession() {
  if (state.voiceSessionId) await closeVoiceSession();
  else await startVoiceSession();
}

async function loadApiStatus() {
  try {
    const data = await api('/v1/health/live');
    setApiStatus(`API ${data.version || 'ready'}`, 'ok');
  } catch {
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
  state.shouldListen = false;
  stopRecognition();
  if (window.speechSynthesis) window.speechSynthesis.cancel();
  if (state.voiceSocket) state.voiceSocket.close(1000, 'page_unload');
  if (state.voiceSessionId) {
    fetch(`/v1/voice/sessions/${encodeURIComponent(state.voiceSessionId)}`, {
      method: 'DELETE',
      headers: headers(),
      keepalive: true,
    }).catch(() => {});
  }
});

function showSignInGate(message) {
  $('sign-in-gate').hidden = false;
  $('chat-history').hidden = true;
  $('composer-wrap').hidden = true;
  $('account').hidden = true;
  if (message) $('sign-in-message').textContent = message;
}

function showApp() {
  $('sign-in-gate').hidden = true;
  $('chat-history').hidden = false;
  $('composer-wrap').hidden = false;
  if (window.GuruAuth.mode() === 'oidc') {
    $('account').hidden = false;
    $('account-name').textContent = window.GuruAuth.currentUser();
  }
}

$('sign-in').addEventListener('click', () => {
  window.GuruAuth.signIn().catch((error) => showToast(error.message));
});

$('sign-out').addEventListener('click', () => window.GuruAuth.signOut());

async function start() {
  let mode;
  try {
    mode = await window.GuruAuth.init();
  } catch (error) {
    // A failed or refused redirect must not leave the app looking signed in.
    showSignInGate(error.message);
    setApiStatus('Signed out', 'error');
    return;
  }

  if (mode === 'unavailable') {
    showSignInGate('This portal is not accepting sign-ins yet. Its identity provider is not configured.');
    setApiStatus('Sign-in unavailable', 'error');
    return;
  }

  if (!window.GuruAuth.isAuthenticated()) {
    showSignInGate();
    setApiStatus('Signed out', 'neutral');
    return;
  }

  showApp();
  await loadApiStatus();
}

start();

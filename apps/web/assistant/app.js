const LANGUAGES = ['en-IN', 'hi-IN', 'kn-IN'];
const HISTORY_TURNS = 12;
const HISTORY_TURN_CHARS = 1000;
// A spoken turn ends after this much quiet, so "search the internet for" and
// "the latest ISRO launch" said with a pause become one request.
const TURN_QUIET_MS = 700;
// Speech the microphone picks up this soon after Guru Ji stops is checked as echo.
const ECHO_TAIL_MS = 1500;
const STOP_WORDS = /^(stop|stop it|stop talking|please stop|wait|ok stop|ruko|ruk jao|bas|bas karo|chup|enough|रुको|रुक जाओ|बस|बस करो|चुप|ನಿಲ್ಲಿಸು|ನಿಲ್ಲಿಸಿ|ಸಾಕು)$/i;
// iOS routes audio away from the speaker while the microphone is open and
// restarts recognition after every phrase, so it listens between replies and
// offers "Tap to interrupt" instead of talking over them.
const HALF_DUPLEX = /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);

const state = {
  voiceSessionId: null,
  voiceSocket: null,
  transportTicket: null,
  recognition: null,
  recognitionSupported: false,
  shouldListen: false,
  waitingForAnswer: false,
  speaking: false,
  utterance: null,
  voiceTransportReady: false,
  intentionalClose: false,
  requestInFlight: false,
  finalResultKeys: new Set(),
  voiceCommands: new Map(),
  pingTimer: null,
  // Full-duplex conversation
  conversationId: newId('conv'),
  history: [],
  language: loadLanguage(),
  pendingTurn: '',
  turnTimer: null,
  activeReplyId: null,
  echoText: '',
  echoTimer: null,
  thinking: new Map(),
  liveText: new Map(),
  audioContext: null,
  playback: { queue: [], source: null, generation: 0, busy: false },
  tts: null,
  reconnectAttempts: 0,
  reconnecting: false,
  warnedNoVoice: new Set(),
};

const $ = (id) => document.getElementById(id);
const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
state.recognitionSupported = Boolean(SpeechRecognition);

function newId(prefix) {
  return `${prefix}-${crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`}`;
}

// Per-browser conveniences only; the page works the same when storage is blocked.
function loadLanguage() {
  try {
    const saved = window.localStorage.getItem('guruji.language');
    if (LANGUAGES.includes(saved)) return saved;
  } catch {
    // Storage may be unavailable in a private window.
  }
  const preferred = (navigator.language || '').toLowerCase();
  if (preferred.startsWith('hi')) return 'hi-IN';
  if (preferred.startsWith('kn')) return 'kn-IN';
  return 'en-IN';
}

function saveLanguage(language) {
  try {
    window.localStorage.setItem('guruji.language', language);
  } catch {
    // Not remembered; the choice still applies to this visit.
  }
}

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
    const error = new Error(data.detail || data.error?.message || `Request failed (${response.status})`);
    error.status = response.status;
    throw error;
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

// ------------------------------------------------------------------ history
// The conversation is kept here only; each request carries its recent turns.
function remember(role, text) {
  const value = (text || '').replace(/\s+/g, ' ').trim().slice(0, HISTORY_TURN_CHARS);
  if (!value) return;
  state.history.push({ role, text: value });
  if (state.history.length > HISTORY_TURNS) state.history.splice(0, state.history.length - HISTORY_TURNS);
}

function recentHistory() {
  return state.history.slice(-HISTORY_TURNS);
}

// ------------------------------------------------------------------ messages
function safeLink(url) {
  try {
    const parsed = new URL(url);
    return parsed.protocol === 'https:' || parsed.protocol === 'http:' ? parsed.href : null;
  } catch {
    return null;
  }
}

function addSources(bubble, sources) {
  const links = (sources || []).map((source) => ({ ...source, href: safeLink(source.url) })).filter((source) => source.href);
  if (!links.length) return;
  const list = document.createElement('ol');
  list.className = 'source-list';
  for (const source of links.slice(0, 6)) {
    const item = document.createElement('li');
    const anchor = document.createElement('a');
    anchor.href = source.href;
    anchor.target = '_blank';
    anchor.rel = 'noopener noreferrer';
    anchor.textContent = source.title || new URL(source.href).hostname;
    item.append(anchor);
    list.append(item);
  }
  bubble.append(list);
}

function addMessage(kind, text, options = {}) {
  const article = document.createElement('article');
  article.className = `message ${kind}-message${options.voice ? ' voice-reply' : ''}`;
  if (options.language) article.lang = options.language;

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
  copy.className = 'message-text';
  copy.textContent = text;
  bubble.append(copy);

  if (options.answer) {
    addSources(bubble, (options.answer.sources || []).filter((source) => source.url));
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

// Questions go to the institutional agent, which answers from the records the
// college has imported. Where the data platform is off (503) or the account may
// not run agent commands (403), the read-only assistant answers instead.
function askAgent(text, approvalId = null) {
  return api('/v1/agent/commands', {
    method: 'POST',
    body: JSON.stringify({
      command: text,
      channel: 'text',
      include_data: false,
      approval_id: approvalId,
      conversation_id: state.conversationId,
      history: approvalId ? [] : recentHistory(),
      language: state.language,
    }),
  });
}

function askReadOnlyAssistant(text) {
  return api('/v1/chat', {
    method: 'POST',
    body: JSON.stringify({
      prompt: text,
      institution_scope: { college_id: window.GuruAuth.collegeId() },
      channel: 'text',
      conversation_id: state.conversationId,
      conversational: true,
      history: recentHistory(),
      language: state.language,
    }),
  });
}

function answerText(data) {
  const answer = data.answer && typeof data.answer === 'object' ? data.answer : data;
  return typeof data.answer === 'string' ? data.answer : answer.answer || answer.refusal_reason || 'No answer returned.';
}

function showAnswer(data, options = {}) {
  const answer = data.answer && typeof data.answer === 'object' ? data.answer : data;
  const text = answerText(data);
  const article = addMessage('assistant', text, { ...options, answer, language: answer.language });
  if (answer.status === 'approval_required' && answer.approval) {
    addApprovalControls(article, answer.approval, options.command);
  }
  return article;
}

// An action that changes institutional records waits for the person to
// confirm it; confirming records the decision and runs the same command again.
function addApprovalControls(article, approval, command) {
  const controls = document.createElement('div');
  controls.className = 'approval-actions';
  const confirm = document.createElement('button');
  confirm.type = 'button';
  confirm.className = 'approval-button';
  confirm.textContent = 'Confirm and run';
  const cancel = document.createElement('button');
  cancel.type = 'button';
  cancel.className = 'approval-button secondary';
  cancel.textContent = 'Cancel';
  controls.append(confirm, cancel);
  article.querySelector('.bubble').append(controls);

  async function decide(approve) {
    confirm.disabled = true;
    cancel.disabled = true;
    try {
      await api(`/v1/agent/approvals/${encodeURIComponent(approval.approval_id)}`, {
        method: 'POST',
        body: JSON.stringify({ approve }),
      });
      controls.remove();
      if (!approve || !command) {
        addMessage('assistant', approve ? 'Confirmed.' : 'Cancelled. Nothing was changed.');
        return;
      }
      showAnswer(await askAgent(command, approval.approval_id), { command });
    } catch (error) {
      confirm.disabled = false;
      cancel.disabled = false;
      showToast(error.message);
    }
  }
  confirm.addEventListener('click', () => decide(true));
  cancel.addEventListener('click', () => decide(false));
}

async function ask(prompt) {
  const text = prompt.trim();
  if (!text || state.requestInFlight) return;

  addMessage('user', text);
  $('text-input').value = '';
  setComposerBusy(true);
  const loading = addMessage('assistant', 'Thinking…');
  loading.classList.add('loading-message');

  try {
    let data;
    try {
      data = await askAgent(text);
    } catch (error) {
      if (error.status !== 503 && error.status !== 403) throw error;
      data = await askReadOnlyAssistant(text);
    }
    loading.remove();
    showAnswer(data, { command: text });
    remember('user', text);
    remember('assistant', answerText(data));
  } catch (error) {
    loading.remove();
    addMessage('assistant', error.message);
    showToast(error.message);
  } finally {
    setComposerBusy(false);
    $('text-input').focus();
  }
}

// ------------------------------------------------------------------ voice state
function renderVoiceState(message = '') {
  const active = Boolean(state.voiceSessionId);
  const button = $('voice-button');
  button.classList.toggle('active', active);
  button.setAttribute('aria-pressed', String(active));
  $('voice-button-label').textContent = active ? 'Stop voice assistant' : 'Voice Assistant';
  $('voice-status-line').textContent = message;
  $('interrupt-button').hidden = !(active && state.speaking);
}

function listeningHint() {
  return HALF_DUPLEX ? 'Listening…' : 'Listening… talk any time, even while I am speaking.';
}

function setVoiceButtonDisabled(disabled) {
  $('voice-button').disabled = disabled;
}

// ------------------------------------------------------------------ speech output
// The AudioContext must be created or resumed inside the click that starts
// voice, before anything is awaited, or the browser keeps it muted.
function unlockAudio() {
  const Context = window.AudioContext || window.webkitAudioContext;
  if (!state.audioContext && Context) {
    try {
      state.audioContext = new Context();
    } catch {
      state.audioContext = null;
    }
  }
  if (state.audioContext && state.audioContext.state === 'suspended') state.audioContext.resume().catch(() => {});
  try {
    if (window.speechSynthesis) {
      const primer = new SpeechSynthesisUtterance('');
      primer.volume = 0;
      window.speechSynthesis.speak(primer);
    }
  } catch {
    // Unlocking is best effort; the Polly audio path does not need it.
  }
}

const VOICE_PREFERENCES = {
  'en-IN': [/kajal/i, /neerja/i, /heera/i, /aditi/i, /prabhat/i, /ravi/i, /rishi/i, /india/i],
  'hi-IN': [/kajal/i, /swara/i, /madhur/i, /kalpana/i, /lekha/i, /hemant/i, /हिन्दी/, /hindi/i],
  'kn-IN': [/sapna/i, /gagan/i, /kannada/i, /ಕನ್ನಡ/],
};

function pickVoice(language) {
  const voices = window.speechSynthesis ? window.speechSynthesis.getVoices() : [];
  const wanted = language.toLowerCase();
  const matching = voices.filter((voice) => (voice.lang || '').replace('_', '-').toLowerCase() === wanted);
  for (const pattern of VOICE_PREFERENCES[language] || []) {
    const hit = matching.find((voice) => pattern.test(voice.name));
    if (hit) return hit;
  }
  return matching[0] || voices.find((voice) => (voice.lang || '').toLowerCase().startsWith(wanted.slice(0, 2))) || null;
}

function speechEcho(text) {
  state.echoText = `${state.echoText} ${text}`.slice(-2000);
  clearTimeout(state.echoTimer);
}

function endSpeaking() {
  state.speaking = false;
  state.utterance = null;
  clearTimeout(state.echoTimer);
  state.echoTimer = setTimeout(() => { state.echoText = ''; }, ECHO_TAIL_MS);
  if (state.voiceSessionId) {
    renderVoiceState(state.waitingForAnswer ? 'Thinking…' : listeningHint());
    startRecognition();
  }
}

// Stop whatever Guru Ji is saying, at once.
function stopSpeaking() {
  const playback = state.playback;
  playback.generation += 1;
  playback.queue = [];
  playback.busy = false;
  if (playback.source) {
    try {
      playback.source.onended = null;
      playback.source.stop();
    } catch {
      // Already finished.
    }
    playback.source = null;
  }
  if (window.speechSynthesis) window.speechSynthesis.cancel();
  if (state.speaking) endSpeaking();
}

function base64ToBuffer(value) {
  const binary = atob(value);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
  return bytes.buffer;
}

function enqueueSpeech(item) {
  const playback = state.playback;
  if (item.audioBase64 && state.audioContext) {
    // Decoding starts now so the sentence is ready when its turn comes.
    item.decoded = state.audioContext.decodeAudioData(base64ToBuffer(item.audioBase64)).catch(() => null);
  }
  playback.queue.push(item);
  if (!playback.busy) playNext(playback.generation);
}

function speakWithBrowser(item, generation) {
  return new Promise((resolve) => {
    const language = item.hinglish ? 'en-IN' : item.language;
    const voice = pickVoice(language);
    if (!window.speechSynthesis || (!voice && language === 'kn-IN')) {
      if (language === 'kn-IN' && !state.warnedNoVoice.has(language)) {
        state.warnedNoVoice.add(language);
        showToast('This device has no Kannada voice, so Kannada replies are shown on screen.');
      }
      resolve();
      return;
    }
    const utterance = new SpeechSynthesisUtterance(item.text);
    // Chrome can garbage-collect an unreferenced utterance mid-speech and never
    // fire onend, which would leave the conversation stuck after one answer.
    state.utterance = utterance;
    utterance.lang = language;
    if (voice) utterance.voice = voice;
    utterance.rate = 1;
    // Some engines never report the end at all: move on after a generous
    // estimate of the sentence's length instead of waiting forever.
    const guard = setTimeout(() => resolve(), 4000 + item.text.length * 120);
    utterance.onend = () => { clearTimeout(guard); resolve(); };
    utterance.onerror = () => { clearTimeout(guard); resolve(); };
    if (generation !== state.playback.generation) {
      resolve();
      return;
    }
    window.speechSynthesis.speak(utterance);
  });
}

async function speakWithAudio(item, generation) {
  const buffer = await item.decoded;
  if (!buffer || generation !== state.playback.generation) return false;
  return new Promise((resolve) => {
    const source = state.audioContext.createBufferSource();
    source.buffer = buffer;
    source.connect(state.audioContext.destination);
    source.onended = () => resolve(true);
    state.playback.source = source;
    source.start();
  });
}

async function playNext(generation) {
  const playback = state.playback;
  if (generation !== playback.generation) return;
  const item = playback.queue.shift();
  if (!item) {
    playback.busy = false;
    playback.source = null;
    if (state.speaking) endSpeaking();
    return;
  }
  playback.busy = true;
  if (!state.speaking) {
    state.speaking = true;
    renderVoiceState(HALF_DUPLEX ? 'Speaking… tap “Stop” to interrupt.' : 'Speaking… just talk to interrupt me.');
    if (HALF_DUPLEX) stopRecognition();
  }
  speechEcho(item.text);
  let played = false;
  if (item.decoded) played = await speakWithAudio(item, generation);
  if (!played && generation === playback.generation) await speakWithBrowser(item, generation);
  playback.source = null;
  playNext(generation);
}

// Text-only speech for a reply the server sent without speech events.
function speakAnswer(text, language = state.language) {
  if (!text) {
    state.waitingForAnswer = false;
    startRecognition();
    return;
  }
  enqueueSpeech({ text, language, hinglish: false, audioBase64: null });
}

// ------------------------------------------------------------------ speech input
function stopRecognition() {
  if (!state.recognition) return;
  try {
    state.recognition.stop();
  } catch {
    // Recognition may already be stopped by the browser.
  }
}

function startRecognition() {
  if (!state.recognition || !state.shouldListen) return;
  if (HALF_DUPLEX && state.speaking) return;
  try {
    state.recognition.start();
  } catch (error) {
    if (error.name !== 'InvalidStateError') showToast('Voice input could not start.');
  }
}

function words(text) {
  return (text || '').toLowerCase().normalize('NFC').replace(/[^\p{L}\p{M}\p{N}\s]/gu, ' ').split(/\s+/).filter(Boolean);
}

// The microphone also hears the speaker: what matches Guru Ji's own words is echo.
function isEcho(transcript) {
  const heard = words(transcript);
  if (!heard.length) return true;
  const spoken = new Set(words(state.echoText));
  if (!spoken.size) return false;
  const overlap = heard.filter((word) => spoken.has(word)).length;
  return overlap / heard.length >= 0.6;
}

function interruptReply(reason = 'interrupted') {
  const replyId = state.activeReplyId;
  stopSpeaking();
  if (state.voiceSocket && state.voiceSocket.readyState === WebSocket.OPEN) {
    state.voiceSocket.send(JSON.stringify(replyId ? { type: 'interrupt', client_message_id: replyId } : { type: 'interrupt' }));
  }
  if (reason === 'stop') renderVoiceState(listeningHint());
}

function queueTurn(transcript) {
  const text = transcript.trim();
  if (!text) return;
  if (STOP_WORDS.test(text.replace(/[.!?।,]+$/u, ''))) {
    state.pendingTurn = '';
    clearTimeout(state.turnTimer);
    interruptReply('stop');
    return;
  }
  if ((state.speaking || state.echoText) && isEcho(text)) return;
  state.pendingTurn = `${state.pendingTurn} ${text}`.trim();
  clearTimeout(state.turnTimer);
  state.turnTimer = setTimeout(flushTurn, TURN_QUIET_MS);
}

function flushTurn() {
  clearTimeout(state.turnTimer);
  const text = state.pendingTurn;
  state.pendingTurn = '';
  if (text) sendUtterance(text);
}

function sendUtterance(text) {
  const value = text.trim();
  if (!value || !state.voiceTransportReady || !state.voiceSocket || state.voiceSocket.readyState !== WebSocket.OPEN) return;
  const clientMessageId = newId('voice');
  // Whatever was still being said belongs to the previous turn.
  stopSpeaking();
  state.waitingForAnswer = true;
  state.activeReplyId = clientMessageId;
  state.voiceCommands.set(clientMessageId, value);
  state.voiceSocket.send(JSON.stringify({
    type: 'utterance',
    client_message_id: clientMessageId,
    text: value,
    // The server uses the agent only where the platform is on and the account
    // may run commands, and answers read-only otherwise.
    mode: 'agent',
    language: state.language,
    conversation_id: state.conversationId,
    history: recentHistory(),
  }));
  addMessage('user', value, { voice: true, language: state.language });
  remember('user', value);
  renderVoiceState('Thinking…');
}

function configureRecognition() {
  if (!SpeechRecognition) return null;
  const recognition = new SpeechRecognition();
  recognition.continuous = true;
  recognition.interimResults = true;
  recognition.lang = state.language;

  recognition.onstart = () => {
    // Result indexes restart at 0 on every start, so keys from an earlier run
    // would swallow a phrase the user repeats (for example "yes" twice).
    state.finalResultKeys.clear();
    if (!state.speaking && !state.waitingForAnswer) renderVoiceState(listeningHint());
  };
  recognition.onresult = (event) => {
    let interim = '';
    for (let index = event.resultIndex; index < event.results.length; index += 1) {
      const result = event.results[index];
      const transcript = result[0]?.transcript?.trim() || '';
      if (!result.isFinal) {
        interim += `${transcript} `;
        continue;
      }
      const resultKey = `${index}:${transcript}`;
      if (transcript && !state.finalResultKeys.has(resultKey)) {
        state.finalResultKeys.add(resultKey);
        if (state.finalResultKeys.size > 100) state.finalResultKeys.clear();
        queueTurn(transcript);
      }
    }
    interim = interim.trim();
    if (!interim) return;
    // Still talking: the turn is not over yet.
    if (state.pendingTurn) {
      clearTimeout(state.turnTimer);
      state.turnTimer = setTimeout(flushTurn, TURN_QUIET_MS * 2);
    }
    // Barge-in: two real words over Guru Ji's voice stop it at once.
    if (state.speaking && !isEcho(interim) && (words(interim).length >= 2 || STOP_WORDS.test(interim))) {
      interruptReply();
    }
    if (!state.speaking) renderVoiceState(`Hearing: “${interim}”`);
  };
  recognition.onerror = (event) => {
    if (!state.voiceSessionId) return;
    if (event.error === 'not-allowed' || event.error === 'service-not-allowed') {
      handleVoiceFailure('Microphone permission was denied.');
      return;
    }
    if (event.error === 'language-not-supported') {
      showToast('This browser cannot recognise that language; switching to English (India).');
      setLanguage('en-IN');
      return;
    }
    if (event.error !== 'aborted' && event.error !== 'no-speech') {
      renderVoiceState(`Voice input error: ${event.error}.`);
    }
  };
  recognition.onend = () => {
    // Browsers end continuous recognition after silence or a network blip;
    // the conversation keeps listening until the person stops it.
    if (state.shouldListen && state.voiceSessionId && !(HALF_DUPLEX && state.speaking)) {
      window.setTimeout(startRecognition, 120);
    }
  };
  return recognition;
}

function setLanguage(language) {
  if (!LANGUAGES.includes(language)) return;
  state.language = language;
  saveLanguage(language);
  $('language-select').value = language;
  if (state.recognition) {
    state.recognition.lang = language;
    // A running recognizer keeps its language until restarted.
    stopRecognition();
  }
}

// ------------------------------------------------------------------ transport
function removeThinking(clientMessageId) {
  const bubble = state.thinking.get(clientMessageId);
  if (bubble) bubble.remove();
  state.thinking.delete(clientMessageId);
  state.liveText.delete(clientMessageId);
}

// A streamed reply is shown as it is spoken; the full answer replaces it.
function showLiveText(clientMessageId, sentence) {
  const bubble = state.thinking.get(clientMessageId);
  if (!bubble) return;
  const text = `${state.liveText.get(clientMessageId) || ''} ${sentence}`.trim();
  state.liveText.set(clientMessageId, text);
  bubble.classList.remove('loading-message');
  bubble.querySelector('.message-text').textContent = text;
  scrollHistory();
}

function resetLiveText(clientMessageId) {
  const bubble = state.thinking.get(clientMessageId);
  state.liveText.delete(clientMessageId);
  if (!bubble) return;
  bubble.classList.add('loading-message');
  bubble.querySelector('.message-text').textContent = 'Thinking…';
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
    state.tts = message.tts || null;
    state.reconnectAttempts = 0;
    renderVoiceState(listeningHint());
    startRecognition();
    return;
  }
  if (message.type === 'thinking') {
    const bubble = addMessage('assistant', 'Thinking…', { voice: true });
    bubble.classList.add('loading-message');
    state.thinking.set(message.client_message_id, bubble);
    return;
  }
  if (message.type === 'answer') {
    const answer = message.answer || {};
    const text = answer.answer || answer.refusal_reason || 'No answer returned.';
    const command = state.voiceCommands.get(message.client_message_id);
    state.voiceCommands.delete(message.client_message_id);
    removeThinking(message.client_message_id);
    showAnswer(answer, { voice: true, command });
    remember('assistant', text);
    if (message.client_message_id === state.activeReplyId) state.waitingForAnswer = false;
    if (!state.speaking) renderVoiceState(listeningHint());
    // Older servers send no speech events: speak the whole answer here.
    if (answer.spoken !== false && answer.speech_text === undefined && message.client_message_id === state.activeReplyId) speakAnswer(text);
    return;
  }
  if (message.type === 'speech') {
    // Sentences of a reply the person has already talked over are dropped.
    if (message.client_message_id !== state.activeReplyId) return;
    if (!message.filler) showLiveText(message.client_message_id, message.text);
    enqueueSpeech({
      text: message.text,
      language: message.language || state.language,
      hinglish: Boolean(message.hinglish),
      audioBase64: message.audio_base64,
    });
    return;
  }
  if (message.type === 'cancelled') {
    if (message.reason === 'retracted') {
      // What was said came from a reply that then failed: stop it; the
      // replacement answer and its speech follow.
      if (message.client_message_id === state.activeReplyId) stopSpeaking();
      resetLiveText(message.client_message_id);
      return;
    }
    removeThinking(message.client_message_id);
    return;
  }
  // Marks the end of a reply's speech; playback ends when its queue drains.
  if (message.type === 'speech_end') return;
  if (message.type === 'pong') return;
  if (message.type === 'expired') {
    const reason = message.reason === 'idle'
      ? 'Voice paused after a quiet spell. Tap Voice Assistant to talk again.'
      : message.reason === 'closed'
        ? 'Voice moved to your newer window or tab.'
        : 'The voice session ended. Tap Voice Assistant to talk again.';
    handleVoiceFailure(reason);
    return;
  }
  if (message.type === 'error') {
    if (message.client_message_id) {
      removeThinking(message.client_message_id);
      if (message.client_message_id === state.activeReplyId) state.waitingForAnswer = false;
    }
    if (message.code === 'turn_failed') addMessage('assistant', message.message, { voice: true });
    renderVoiceState(message.message || 'Voice transport error.');
    startRecognition();
  }
}

// The page and the API share an origin. Behind a TLS-terminating load balancer
// the server sees plain HTTP and advertises ws://, which an HTTPS page is not
// allowed to open, so the socket always takes this page's own scheme and host.
function voiceSocketUrl(advertised) {
  const { pathname, search } = new URL(advertised, window.location.href);
  const url = new URL(pathname + search, window.location.href);
  url.protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return url.toString();
}

function startTransport(data) {
  return new Promise((resolve, reject) => {
    let settled = false;
    const socket = new WebSocket(voiceSocketUrl(data.websocket_url));
    state.voiceSocket = socket;
    socket.onopen = () => {
      socket.send(JSON.stringify({ type: 'auth', ticket: data.transport_ticket, features: ['thinking', 'speech', 'interrupt'] }));
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
    };
    socket.onclose = () => {
      clearInterval(state.pingTimer);
      state.pingTimer = null;
      if (!settled) {
        settled = true;
        reject(new Error('Live voice transport closed before it was ready.'));
      }
      if (state.voiceSessionId && !state.intentionalClose && socket === state.voiceSocket && !state.reconnecting) reconnectVoice();
    };
  });
}

async function requestMicrophonePermission() {
  if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
    throw new Error('Voice input requires HTTPS or localhost and microphone access.');
  }
  const stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
  stream.getTracks().forEach((track) => track.stop());
}

async function openVoiceTransport() {
  const collegeId = window.GuruAuth.collegeId();
  const data = await api('/v1/voice/sessions', {
    method: 'POST',
    // With no college on hand, let the server use the verified token's own
    // scope; an empty college_id is rejected as a contract violation.
    body: JSON.stringify(collegeId ? { college_id: collegeId } : {}),
  });
  if (data.transport !== 'browser_web_speech_ws' || !data.transport_ticket || !data.websocket_url) {
    throw new Error('The server did not provide a supported live voice transport.');
  }
  state.voiceSessionId = data.session_id;
  state.transportTicket = data.transport_ticket;
  state.intentionalClose = false;
  state.voiceTransportReady = false;
  await startTransport(data);
  clearInterval(state.pingTimer);
  state.pingTimer = window.setInterval(() => {
    if (state.voiceSocket?.readyState === WebSocket.OPEN) state.voiceSocket.send(JSON.stringify({ type: 'ping' }));
  }, 20_000);
}

async function startVoiceSession() {
  if (state.voiceSessionId) return;
  if (!state.recognitionSupported) {
    renderVoiceState('Voice input is not supported in this browser.');
    showToast('Use Chrome, Edge or Safari for voice, or type your question.');
    return;
  }
  // Inside the click, before anything is awaited.
  unlockAudio();
  setVoiceButtonDisabled(true);
  renderVoiceState('Requesting microphone permission…');
  try {
    await requestMicrophonePermission();
    state.shouldListen = true;
    state.finalResultKeys.clear();
    state.recognition = configureRecognition();
    renderVoiceState('Connecting live voice transport…');
    await openVoiceTransport();
  } catch (error) {
    await cleanupVoiceSession(true);
    renderVoiceState('');
    showToast(error.message || 'Voice Assistant could not start.');
  } finally {
    setVoiceButtonDisabled(false);
  }
}

// A dropped connection (network change, server deploy) reconnects by itself
// and keeps the conversation, a few times, before giving up.
async function reconnectVoice() {
  if (state.reconnecting) return;
  state.reconnecting = true;
  const previous = state.voiceSessionId;
  try {
    while (state.shouldListen && state.reconnectAttempts < 3) {
      state.reconnectAttempts += 1;
      state.voiceTransportReady = false;
      renderVoiceState('Reconnecting…');
      await new Promise((resolve) => setTimeout(resolve, 600 * 2 ** (state.reconnectAttempts - 1)));
      if (!state.shouldListen) return;
      try {
        await openVoiceTransport();
        if (previous && previous !== state.voiceSessionId) {
          api(`/v1/voice/sessions/${encodeURIComponent(previous)}`, { method: 'DELETE' }).catch(() => {});
        }
        return;
      } catch {
        // Try again after a longer pause.
      }
    }
  } finally {
    state.reconnecting = false;
  }
  if (state.shouldListen) handleVoiceFailure('Live voice transport closed.');
}

async function cleanupVoiceSession(closeServerSession = true) {
  const sessionId = state.voiceSessionId;
  state.shouldListen = false;
  state.waitingForAnswer = false;
  state.intentionalClose = true;
  state.pendingTurn = '';
  state.activeReplyId = null;
  clearTimeout(state.turnTimer);
  clearInterval(state.pingTimer);
  state.pingTimer = null;
  stopRecognition();
  stopSpeaking();
  for (const key of [...state.thinking.keys()]) removeThinking(key);
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
  renderVoiceState($('voice-status-line').textContent);
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
$('interrupt-button').addEventListener('click', () => interruptReply('stop'));
$('language-select').value = state.language;
$('language-select').addEventListener('change', (event) => setLanguage(event.target.value));
if (window.speechSynthesis) window.speechSynthesis.addEventListener?.('voiceschanged', () => pickVoice(state.language));
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

/*
 * Agentic Saffron client assistant.
 *
 * Text and two-way voice conversation with the institutional agent. Chats are
 * kept in this browser (IndexedDB), per signed-in account; the server keeps
 * no transcript, so each request carries the recent turns of its chat. Answers
 * show their sources and the files the agent made (Excel, Word, PowerPoint,
 * PDF, CSV), which download through the authenticated client.
 */
const LANGUAGES = ['en-IN', 'hi-IN', 'kn-IN'];
const HISTORY_TURNS = 12;
const HISTORY_TURN_CHARS = 1000;
// What the server keeps of the history (MAX_HISTORY_CHARS); more would only
// make a voice message too large for the socket in Hindi or Kannada.
const HISTORY_TOTAL_CHARS = 6000;
// A spoken turn ends after this much quiet, so "search the internet for" and
// "the latest ISRO launch" said with a pause become one request.
const TURN_QUIET_MS = 700;
// Background noise keeps producing interim results; a heard phrase is sent at
// most this long after it was first recognised, however long the noise goes on.
const TURN_MAX_HOLD_MS = 2500;
// Chrome can stop listening without telling the page; with nothing heard for
// this long, recognition is restarted.
const RECOGNITION_STALL_MS = 8000;
// How often the browser reports what its recognition did (counts, never words).
const CLIENT_LOG_MS = 5000;
// Speech the microphone picks up this soon after the assistant stops is checked as echo.
const ECHO_TAIL_MS = 1500;
const STOP_WORDS = /^(stop|stop it|stop talking|please stop|wait|ok stop|ruko|ruk jao|bas|bas karo|chup|enough|रुको|रुक जाओ|बस|बस करो|चुप|ನಿಲ್ಲಿಸು|ನಿಲ್ಲಿಸಿ|ಸಾಕು)$/i;
// iOS routes audio away from the speaker while the microphone is open and
// restarts recognition after every phrase, so it listens between replies and
// offers "Tap to interrupt" instead of talking over them.
const HALF_DUPLEX = /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);

// Chat history kept in this browser.
const HISTORY_DB = 'agentic-saffron';
const HISTORY_STORE = 'conversations';
const MAX_CONVERSATIONS = 300;
const MAX_MESSAGES = 400;
const MAX_MESSAGE_CHARS = 20000;
const TITLE_CHARS = 60;
// A background task (the open-task agent on a queue) is followed until done.
const JOB_POLL_MS = 4000;
const JOB_FOLLOW_MS = 20 * 60 * 1000;
// Only this server's own report downloads are ever fetched with the identity.
const REPORT_PATH = /^\/v1\/reports\/[\w-]+\/download$/;
// Blockquotes nest no deeper than this; beyond it the text shows as it is.
const MAX_QUOTE_DEPTH = 4;
// Warnings worth showing under an answer; the rest are for operators.
const NOTE_CODES = new Set(['list_truncated', 'empty_report', 'email_failed', 'email_no_recipients', 'unresolved_recipients', 'email_delivery_error', 'step_not_executed']);
const STATUS_LABELS = {
  approval_required: ['Needs confirmation', 'pending'],
  accepted: ['Working in the background', 'pending'],
  partial: ['Partial answer', 'partial'],
  refused: ['Not allowed', 'refused'],
  failed: ['Could not finish', 'failed'],
};
const FILE_KINDS = { xlsx: 'Excel workbook', docx: 'Word document', pptx: 'PowerPoint deck', pdf: 'PDF', csv: 'CSV table', png: 'Image', jpg: 'Image', jpeg: 'Image', txt: 'Text', md: 'Text', json: 'JSON' };
const SUGGESTIONS = [
  { icon: 'i-table', label: 'Excel of low attendance', prompt: 'Create an Excel report of students below 75% attendance' },
  { icon: 'i-doc', label: 'Word file of pending fees', prompt: 'Make a Word document of students with pending fees' },
  { icon: 'i-slides', label: 'Slides for a review', prompt: 'Prepare slides of students below 75% attendance' },
  { icon: 'i-spark', label: 'College overview', prompt: 'Give me an overview of our institution' },
  { icon: 'i-globe', label: 'Search the internet', prompt: 'Search the internet for the latest UGC guidelines' },
];

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
  // Chats with a typed question still waiting for its answer, and the
  // "Thinking…" shown for each while it is open.
  inFlight: new Set(),
  textThinking: new Map(),
  finalResultKeys: new Set(),
  voiceCommands: new Map(),
  // Which chat (and which question in it) each spoken turn belongs to, so the
  // answer lands there even if the person has opened another chat meanwhile.
  voiceTurns: new Map(),
  pingTimer: null,
  // Chats
  owner: '',
  conversation: null,
  conversations: [],
  historyQuery: '',
  renaming: null,
  followedJobs: new Set(),
  language: loadLanguage(),
  pendingTurn: '',
  turnTimer: null,
  turnStartedAt: 0,
  recognitionActiveAt: 0,
  watchdogTimer: null,
  clientLogTimer: null,
  recognitionStats: null,
  // The microphone stays open while voice is on: its level shows on screen,
  // and a microphone the person picked is handed to speech recognition.
  micStream: null,
  micDeviceId: loadMicrophone(),
  micMeter: null,
  micTrackFailed: false,
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
  readingAloud: null,
};

const $ = (id) => document.getElementById(id);
const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
state.recognitionSupported = Boolean(SpeechRecognition);
const narrowScreen = window.matchMedia('(max-width: 860px)');

function newId(prefix) {
  return `${prefix}-${crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`}`;
}

// Per-browser conveniences only; the page works the same when storage is blocked.
function readSetting(key, legacyKey) {
  try {
    const value = window.localStorage.getItem(key);
    if (value !== null || !legacyKey) return value;
    return window.localStorage.getItem(legacyKey);
  } catch {
    // Storage may be unavailable in a private window.
    return null;
  }
}

function writeSetting(key, value) {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    // Not remembered; the choice still applies to this visit.
  }
}

function loadLanguage() {
  const saved = readSetting('saffron.language', 'guruji.language');
  if (LANGUAGES.includes(saved)) return saved;
  const preferred = (navigator.language || '').toLowerCase();
  if (preferred.startsWith('hi')) return 'hi-IN';
  if (preferred.startsWith('kn')) return 'kn-IN';
  return 'en-IN';
}

function loadMicrophone() {
  return readSetting('saffron.microphone', 'guruji.microphone') || '';
}

function saveMicrophone(deviceId) {
  writeSetting('saffron.microphone', deviceId || '');
}

function saveLanguage(language) {
  writeSetting('saffron.language', language);
}

// Identity is owned by auth.js: it sends a verified OIDC ID token when the
// server asks for one, and only falls back to the fixed demo headers in local
// development, where the server is the side that decides to accept them.
function headers(json = false) {
  return window.SaffronAuth.headers(json);
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

function setApiStatus(label, kind = 'neutral', title = '') {
  const node = $('api-status');
  node.className = `status ${kind}`;
  node.title = title;
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

// ------------------------------------------------------------------ DOM helpers
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function icon(name, className = 'icon') {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('class', className);
  svg.setAttribute('aria-hidden', 'true');
  const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
  use.setAttribute('href', `#${name}`);
  svg.append(use);
  return svg;
}

function iconButton(name, label, onClick) {
  const button = el('button', 'icon-button');
  button.type = 'button';
  button.title = label;
  button.setAttribute('aria-label', label);
  button.append(icon(name));
  button.addEventListener('click', onClick);
  return button;
}

function safeLink(url) {
  try {
    const parsed = new URL(url);
    return parsed.protocol === 'https:' || parsed.protocol === 'http:' ? parsed.href : null;
  } catch {
    return null;
  }
}

function externalLink(href, text) {
  const anchor = document.createElement('a');
  anchor.href = href;
  anchor.target = '_blank';
  anchor.rel = 'noopener noreferrer';
  anchor.textContent = text;
  return anchor;
}

// ------------------------------------------------------------------ Markdown
// Answers may carry light Markdown. It is built node by node, never parsed as
// HTML, so nothing in an answer can become markup or script.
const INLINE = /\*\*([^*\n]+)\*\*|`([^`\n]+)`|\[([^\]\n]+)\]\(([^)\s]+)\)|(https?:\/\/[^\s<>"'()]+[^\s<>"'().,;:!?])|\*([^*\s\n](?:[^*\n]*[^*\s\n])?)\*/g;
const BULLET = /^\s*[-*•]\s+/;
const ORDERED = /^\s*(\d{1,3})[.)]\s+/;
const TABLE_RULE = /^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?$/;

function appendInline(parent, text) {
  let last = 0;
  for (const match of text.matchAll(INLINE)) {
    if (match.index > last) parent.append(text.slice(last, match.index));
    const [whole, bold, code, linkText, linkUrl, bare, italic] = match;
    if (bold) {
      const strong = el('strong');
      appendInline(strong, bold);
      parent.append(strong);
    } else if (code) {
      parent.append(el('code', '', code));
    } else if (linkText) {
      const href = safeLink(linkUrl);
      parent.append(href ? externalLink(href, linkText) : linkText);
    } else if (bare) {
      const href = safeLink(bare);
      parent.append(href ? externalLink(href, bare) : bare);
    } else if (italic) {
      const em = el('em');
      appendInline(em, italic);
      parent.append(em);
    }
    last = match.index + whole.length;
  }
  if (last < text.length) parent.append(text.slice(last));
}

function markdownTable(rows) {
  const cells = (row) => row.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((cell) => cell.trim());
  const wrap = el('div', 'table-wrap');
  const table = el('table');
  const head = el('thead');
  const headRow = el('tr');
  for (const cell of cells(rows[0])) {
    const th = el('th');
    appendInline(th, cell);
    headRow.append(th);
  }
  head.append(headRow);
  const body = el('tbody');
  for (const row of rows.slice(1)) {
    const tr = el('tr');
    for (const cell of cells(row)) {
      const td = el('td');
      appendInline(td, cell);
      tr.append(td);
    }
    body.append(tr);
  }
  table.append(head, body);
  wrap.append(table);
  return wrap;
}

// "## Title ##" -> "Title", in one pass: a regex here is slow on long runs of spaces.
function stripClosingHashes(text) {
  let end = text.length;
  while (end > 0 && text[end - 1] === '#') end -= 1;
  if (end === text.length || (end > 0 && text[end - 1] !== ' ' && text[end - 1] !== '\t')) return text;
  return text.slice(0, end).trimEnd();
}

function renderMarkdown(container, source, depth = 0) {
  container.replaceChildren();
  const lines = String(source || '').replace(/\r\n?/g, '\n').split('\n');
  let paragraph = [];
  const flush = () => {
    if (!paragraph.length) return;
    const p = el('p');
    paragraph.forEach((line, index) => {
      if (index) p.append(el('br'));
      appendInline(p, line);
    });
    container.append(p);
    paragraph = [];
  };
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    const trimmed = line.trim();
    if (!trimmed) {
      flush();
      index += 1;
      continue;
    }
    const fence = trimmed.match(/^(```|~~~)/);
    if (fence) {
      flush();
      const code = [];
      index += 1;
      while (index < lines.length && !lines[index].trim().startsWith(fence[1])) {
        code.push(lines[index]);
        index += 1;
      }
      index += 1;
      const pre = el('pre');
      pre.append(el('code', '', code.join('\n')));
      container.append(pre);
      continue;
    }
    const heading = trimmed.match(/^(#{1,6})\s+(.*)$/);
    if (heading) {
      flush();
      const node = el(heading[1].length <= 2 ? 'h3' : 'h4');
      appendInline(node, stripClosingHashes(heading[2]));
      container.append(node);
      index += 1;
      continue;
    }
    if (/^(-{3,}|\*{3,}|_{3,})$/.test(trimmed)) {
      flush();
      container.append(el('hr'));
      index += 1;
      continue;
    }
    if (trimmed.startsWith('|') && index + 1 < lines.length && TABLE_RULE.test(lines[index + 1].trim())) {
      flush();
      const rows = [line];
      index += 2;
      while (index < lines.length && lines[index].trim().startsWith('|')) {
        rows.push(lines[index]);
        index += 1;
      }
      container.append(markdownTable(rows));
      continue;
    }
    if (/^>\s?/.test(trimmed) && depth < MAX_QUOTE_DEPTH) {
      flush();
      const quoted = [];
      while (index < lines.length && /^>\s?/.test(lines[index].trim())) {
        quoted.push(lines[index].trim().replace(/^>\s?/, ''));
        index += 1;
      }
      const quote = el('blockquote');
      renderMarkdown(quote, quoted.join('\n'), depth + 1);
      container.append(quote);
      continue;
    }
    if (BULLET.test(line) || ORDERED.test(line)) {
      flush();
      const ordered = !BULLET.test(line);
      const marker = ordered ? ORDERED : BULLET;
      const list = el(ordered ? 'ol' : 'ul');
      if (ordered) {
        const start = Number(line.match(ORDERED)[1]);
        if (start > 1) list.start = start;
      }
      let item = null;
      while (index < lines.length) {
        const current = lines[index];
        if (!current.trim()) {
          const next = lines[index + 1];
          if (next && marker.test(next)) {
            index += 1;
            continue;
          }
          break;
        }
        if (marker.test(current)) {
          item = el('li');
          appendInline(item, current.replace(marker, ''));
          list.append(item);
        } else if (item && /^\s+\S/.test(current)) {
          // An indented line (or a nested point) continues the item above.
          item.append(el('br'));
          appendInline(item, current.trim().replace(BULLET, '• '));
        } else {
          break;
        }
        index += 1;
      }
      container.append(list);
      continue;
    }
    paragraph.push(trimmed);
    index += 1;
  }
  flush();
}

// ------------------------------------------------------------------ chat store
// IndexedDB, one record per chat. Where the browser refuses storage (a private
// window, blocked site data) chats live in memory for this visit only.
const HistoryStore = (() => {
  const memory = new Map();
  let opening = null;

  function open() {
    if (opening) return opening;
    opening = new Promise((resolve) => {
      let request;
      try {
        request = window.indexedDB.open(HISTORY_DB, 1);
      } catch {
        resolve(null);
        return;
      }
      request.onupgradeneeded = () => {
        const db = request.result;
        if (!db.objectStoreNames.contains(HISTORY_STORE)) {
          db.createObjectStore(HISTORY_STORE, { keyPath: 'id' }).createIndex('owner', 'owner');
        }
      };
      request.onsuccess = () => {
        const db = request.result;
        // Safari drops the connection when an installed app sits in the
        // background, and clearing site data closes it: open a new one then.
        db.onclose = () => { opening = null; };
        db.onversionchange = () => {
          db.close();
          opening = null;
        };
        resolve(db);
      };
      request.onerror = () => resolve(null);
      request.onblocked = () => resolve(null);
    });
    return opening;
  }

  async function run(mode, work, retried = false) {
    const db = await open();
    if (!db) return { ok: false };
    const outcome = await new Promise((resolve) => {
      let value;
      try {
        const tx = db.transaction(HISTORY_STORE, mode);
        const request = work(tx.objectStore(HISTORY_STORE));
        if (request) request.onsuccess = () => { value = request.result; };
        tx.oncomplete = () => resolve({ ok: true, value });
        tx.onerror = () => resolve({ ok: false });
        tx.onabort = () => resolve({ ok: false });
      } catch {
        // The connection is gone (closing, or closed under us).
        resolve({ ok: false, lost: true });
      }
    });
    if (outcome.lost && !retried) {
      opening = null;
      return run(mode, work, true);
    }
    return outcome;
  }

  async function list(owner) {
    const outcome = await run('readonly', (store) => store.index('owner').getAll(owner));
    const rows = outcome.ok ? outcome.value || [] : [...memory.values()].filter((row) => row.owner === owner);
    return rows.sort((a, b) => b.updatedAt - a.updatedAt);
  }

  async function get(id) {
    const outcome = await run('readonly', (store) => store.get(id));
    return outcome.ok ? outcome.value || null : memory.get(id) || null;
  }

  async function put(record) {
    const outcome = await run('readwrite', (store) => store.put(record));
    if (!outcome.ok) memory.set(record.id, record);
  }

  async function remove(id) {
    memory.delete(id);
    await run('readwrite', (store) => store.delete(id));
  }

  // Read, change and write one chat in a single transaction, so two writers
  // (an answer for a chat that is not open, a finished job, a rename) never
  // undo each other and a chat deleted meanwhile is not brought back.
  // Returns the changed chat, or null when there was none.
  async function update(id, change) {
    let changed = null;
    const outcome = await run('readwrite', (store) => {
      const request = store.get(id);
      request.onsuccess = () => {
        const record = request.result;
        if (!record || change(record) === false) return;
        changed = record;
        store.put(record);
      };
      return null;
    });
    if (outcome.ok) return changed;
    const record = memory.get(id);
    if (!record || change(record) === false) return null;
    return record;
  }

  return { list, get, put, remove, update };
})();

function clip(text, limit = MAX_MESSAGE_CHARS) {
  const value = String(text || '');
  return value.length > limit ? `${value.slice(0, limit)}…` : value;
}

function titleFrom(text) {
  const value = String(text || '').replace(/\s+/g, ' ').trim();
  return value.length > TITLE_CHARS ? `${value.slice(0, TITLE_CHARS - 1).trimEnd()}…` : value || 'New chat';
}

function freshConversation() {
  return { id: newId('conv'), owner: state.owner, title: 'New chat', titleEdited: false, createdAt: Date.now(), updatedAt: Date.now(), messages: [] };
}

function makeMessage(role, text, extra = {}) {
  return { id: newId('msg'), role, text: clip(text), at: Date.now(), ...extra };
}

function touch(conversation) {
  conversation.updatedAt = Date.now();
  if (!conversation.titleEdited) {
    const first = conversation.messages.find((message) => message.role === 'user');
    if (first) conversation.title = titleFrom(first.text);
  }
  if (conversation.messages.length > MAX_MESSAGES) conversation.messages.splice(0, conversation.messages.length - MAX_MESSAGES);
}

async function saveConversation(conversation) {
  if (!conversation.messages.length || !conversation.owner) return;
  await HistoryStore.put(conversation);
  await refreshHistory();
}

async function refreshHistory() {
  if (!state.owner) return;
  const rows = await HistoryStore.list(state.owner);
  // The oldest chats beyond the limit are let go.
  for (const stale of rows.slice(MAX_CONVERSATIONS)) HistoryStore.remove(stale.id);
  state.conversations = rows.slice(0, MAX_CONVERSATIONS);
  renderHistoryList();
}

// The conversation so far, for the model: answered turns only, as the server
// keeps no transcript of its own.
function recentHistory() {
  const turns = (state.conversation?.messages || [])
    .filter((message) => !message.awaiting && (message.role === 'user' || message.role === 'assistant') && message.text)
    .map((message) => ({ role: message.role, text: message.text.replace(/\s+/g, ' ').trim().slice(0, HISTORY_TURN_CHARS) }))
    .filter((turn) => turn.text);
  const kept = [];
  let total = 0;
  for (const turn of turns.slice(-HISTORY_TURNS).reverse()) {
    if (total + turn.text.length > HISTORY_TOTAL_CHARS) break;
    total += turn.text.length;
    kept.unshift(turn);
  }
  return kept;
}

function chatContext() {
  return { conversationId: state.conversation.id, history: recentHistory() };
}

// Put a message into its chat: shown at once when that chat is open, stored
// either way.
function addToConversation(conversationId, message, answered = null) {
  const current = state.conversation;
  if (current && current.id === conversationId) {
    if (answered) {
      const question = current.messages.find((item) => item.id === answered);
      if (question) delete question.awaiting;
    }
    current.messages.push(message);
    touch(current);
    const article = renderMessage(message);
    $('thread').append(article);
    markLatest();
    setEmpty(false);
    $('chat-title').textContent = current.title;
    scrollHistory();
    saveConversation(current).catch(() => {});
    return article;
  }
  const apply = (conversation) => {
    if (conversation.messages.some((item) => item.id === message.id)) return false;
    if (answered) {
      const question = conversation.messages.find((item) => item.id === answered);
      if (question) delete question.awaiting;
    }
    conversation.messages.push(message);
    touch(conversation);
    return true;
  };
  HistoryStore.update(conversationId, apply).then((stored) => {
    if (!stored) return null;
    // Opened while this was being written: show it there as well.
    const opened = state.conversation;
    if (opened?.id === conversationId && apply(opened)) {
      $('thread').append(renderMessage(message));
      markLatest();
      setEmpty(false);
      scrollHistory();
    }
    return refreshHistory();
  }).catch(() => {});
  return null;
}

function postUserMessage(text, options = {}) {
  const message = makeMessage('user', text, { awaiting: true, ...options });
  addToConversation(state.conversation.id, message);
  return message;
}

// ------------------------------------------------------------------ rendering
function setEmpty(empty) {
  $('main').classList.toggle('is-empty', empty);
}

function markLatest() {
  const articles = $('thread').querySelectorAll('.assistant-message:not(.loading-message)');
  articles.forEach((article, index) => article.classList.toggle('latest', index === articles.length - 1));
}

function voiceTag() {
  const tag = el('span', 'voice-tag');
  tag.append(icon('i-mic'), 'Voice');
  return tag;
}

function displayText(message) {
  const files = (message.answer?.artifacts || []).some((item) => item.type === 'report');
  // The file cards below replace the plain download paths in the text.
  if (!files) return message.text;
  return message.text
    .replace(/Download: *\/v1\/reports\/[\w-]+\/download\.?/g, '')
    .replace(/\n\nFiles:\n(?:- [^\n]*\/v1\/reports\/[\w-]+\/download\n?)+/g, '\n')
    .trim();
}

function assistantShell(options = {}) {
  const article = el('article', 'message assistant-message');
  if (options.language) article.lang = options.language;
  const avatar = el('div', 'avatar');
  avatar.append(icon('i-logo'));
  const body = el('div', 'message-body');
  const bubble = el('div', 'bubble');
  const meta = el('div', 'message-meta');
  if (options.voice) meta.append(voiceTag());
  if (options.status && STATUS_LABELS[options.status]) {
    const [label, kind] = STATUS_LABELS[options.status];
    meta.append(el('span', `answer-status ${kind}`, label));
  }
  if (meta.childElementCount) bubble.append(meta);
  const text = el('div', 'message-text');
  bubble.append(text);
  body.append(bubble);
  article.append(avatar, body);
  return { article, body, bubble, text };
}

function renderMessage(message) {
  if (message.role === 'user') {
    const article = el('article', 'message user-message');
    article.dataset.id = message.id;
    if (message.language) article.lang = message.language;
    const body = el('div', 'message-body');
    if (message.voice) body.append(voiceTag());
    body.append(el('div', 'bubble', message.text));
    article.append(body);
    return article;
  }
  const answer = message.answer || {};
  const parts = assistantShell({ voice: message.voice, language: message.language, status: answer.decided ? null : answer.status });
  parts.article.dataset.id = message.id;
  renderMarkdown(parts.text, displayText(message));
  const files = (answer.artifacts || []).filter((item) => item.type === 'report');
  if (files.length) {
    const list = el('div', 'file-list');
    files.forEach((file) => list.append(fileCard(file)));
    parts.bubble.append(list);
  }
  for (const email of (answer.artifacts || []).filter((item) => item.type === 'email')) {
    parts.bubble.append(el('span', 'email-chip', `Email ${email.status || 'recorded'}`));
  }
  if (answer.status === 'accepted' && answer.job_id && !answer.job_done) {
    const card = el('div', 'job-card');
    card.append(el('span', 'spinner'), el('span', '', 'Working on it in the background. The result and any files will appear here.'));
    parts.bubble.append(card);
  }
  if (answer.notes?.length) {
    const notes = el('div', 'answer-notes');
    answer.notes.forEach((note) => notes.append(el('p', '', note)));
    parts.bubble.append(notes);
  }
  addSources(parts.bubble, answer.sources);
  if (answer.status === 'approval_required' && answer.approval && !answer.decided) addApprovalControls(parts.article, message);
  parts.body.append(messageActions(message));
  return parts.article;
}

function addSources(bubble, sources) {
  const items = (sources || []).filter((source) => source.title || source.url);
  if (!items.length) return;
  const details = el('details', 'sources');
  const summary = el('summary');
  summary.append(`${items.length} source${items.length === 1 ? '' : 's'}`, icon('i-chevron'));
  const list = el('ol', 'source-list');
  for (const source of items) {
    const item = el('li');
    const href = source.url ? safeLink(source.url) : null;
    if (href) item.append(externalLink(href, source.title || new URL(href).hostname));
    else item.append(source.title);
    if (source.locator && !href) item.append(' ', el('span', 'source-locator', `· ${source.locator}`));
    list.append(item);
  }
  details.append(summary, list);
  bubble.append(details);
}

function messageActions(message) {
  const actions = el('div', 'message-actions');
  const copy = iconButton('i-copy', 'Copy', async () => {
    await copyText(displayText(message));
    copy.classList.add('done');
    copy.replaceChildren(icon('i-check'));
    setTimeout(() => {
      copy.classList.remove('done');
      copy.replaceChildren(icon('i-copy'));
    }, 1500);
  });
  actions.append(copy);
  if (window.speechSynthesis) {
    actions.append(iconButton('i-speaker', 'Read aloud', () => readAloud(displayText(message), message.language || state.language)));
  }
  const again = iconButton('i-retry', 'Retry', () => retry(message));
  again.classList.add('retry-action');
  actions.append(again);
  return actions;
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return;
  } catch {
    // Older browsers, or no clipboard permission: copy through a selection.
  }
  const area = el('textarea');
  area.value = text;
  area.setAttribute('readonly', '');
  area.className = 'sr-only';
  document.body.append(area);
  area.select();
  try {
    document.execCommand('copy');
  } finally {
    area.remove();
  }
}

// Reads an answer with the device's own voice; a second tap stops it.
function readAloud(text, language) {
  const synth = window.speechSynthesis;
  if (!synth) return;
  if (state.readingAloud === text && synth.speaking) {
    synth.cancel();
    state.readingAloud = null;
    return;
  }
  synth.cancel();
  state.readingAloud = text;
  const voice = pickVoice(language);
  // Chrome cuts long utterances off; a sentence at a time plays through.
  const sentences = text.replace(/\s+/g, ' ').match(/[^.!?।]+[.!?।]*/g) || [text];
  for (const sentence of sentences) {
    const utterance = new SpeechSynthesisUtterance(sentence.trim());
    utterance.lang = language;
    if (voice) utterance.voice = voice;
    synth.speak(utterance);
  }
}

function showThinking(options = {}) {
  const parts = assistantShell(options);
  parts.article.classList.add('loading-message');
  parts.text.append(el('span', 'thinking-text', 'Thinking…'));
  $('thread').append(parts.article);
  setEmpty(false);
  scrollHistory();
  return parts.article;
}

// A failed request is shown but not kept: the question stays in the chat and
// can be asked again.
function showError(text, questionId = null, options = {}) {
  const parts = assistantShell(options);
  parts.text.append(el('p', 'error-text', text));
  if (questionId) {
    const actions = el('div', 'message-actions');
    actions.append(iconButton('i-retry', 'Try again', () => {
      if (state.inFlight.has(state.conversation.id)) {
        showToast('Wait for the answer that is on its way.');
        return;
      }
      const question = state.conversation.messages.find((message) => message.id === questionId);
      parts.article.remove();
      if (question) ask(question.text, { questionId, keepInput: true });
    }));
    parts.body.append(actions);
  }
  $('thread').append(parts.article);
  setEmpty(false);
  scrollHistory();
  return parts.article;
}

function renderConversation() {
  const conversation = state.conversation;
  const thread = $('thread');
  thread.replaceChildren(...conversation.messages.map(renderMessage));
  markLatest();
  setEmpty(!conversation.messages.length);
  $('chat-title').textContent = conversation.messages.length ? conversation.title : 'New chat';
  document.title = conversation.messages.length ? `${conversation.title} — Agentic Saffron` : 'Agentic Saffron — Assistant';
  renderHistoryList();
  if (state.inFlight.has(conversation.id)) state.textThinking.set(conversation.id, showThinking());
  updateSendButton();
  requestAnimationFrame(scrollHistory);
  for (const message of conversation.messages) {
    if (message.answer?.status === 'accepted' && message.answer.job_id && !message.answer.job_done) followJob(conversation.id, message.id, message.answer.job_id);
  }
}

// ------------------------------------------------------------------ files
function fileExtension(file) {
  const format = String(file.format || '').toLowerCase();
  if (format) return format === 'excel' ? 'xlsx' : format;
  const name = String(file.file_name || '');
  return name.includes('.') ? name.split('.').pop().toLowerCase() : 'file';
}

function formatBytes(bytes) {
  const value = Number(bytes) || 0;
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(value < 10 * 1024 ? 1 : 0)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

function fileCard(file) {
  const extension = fileExtension(file);
  const name = String(file.title || file.file_name || 'File');
  const card = el('button', 'file-card');
  card.type = 'button';
  card.title = `Download ${name}`;
  card.setAttribute('aria-label', `Download ${name} (${FILE_KINDS[extension] || extension.toUpperCase()})`);
  const badge = el('span', `file-icon ${extension}`, extension.toUpperCase().slice(0, 4));
  const info = el('span', 'file-info');
  const meta = [FILE_KINDS[extension] || extension.toUpperCase()];
  if (Number(file.row_count) > 0) meta.push(`${Number(file.row_count).toLocaleString()} row${Number(file.row_count) === 1 ? '' : 's'}`);
  if (file.size_bytes) meta.push(formatBytes(file.size_bytes));
  info.append(el('span', 'file-name', name), el('span', 'file-meta', meta.join(' · ')));
  const download = el('span', 'file-download');
  download.append(icon('i-download'));
  card.append(badge, info, download);
  card.addEventListener('click', () => downloadFile(file, card));
  return card;
}

function fileNameFromDisposition(disposition, fallback) {
  const match = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(disposition || '');
  if (!match) return fallback;
  try {
    return decodeURIComponent(match[1]);
  } catch {
    return match[1];
  }
}

// Files are fetched with the signed-in identity and handed to the browser as a
// blob; a plain link would reach the server without it. Only report paths on
// this server are ever requested.
async function downloadFile(file, card) {
  const path = String(file.download_path || '');
  if (!REPORT_PATH.test(path)) {
    showToast('This file cannot be downloaded here.');
    return;
  }
  card.classList.add('busy');
  card.disabled = true;
  try {
    const response = await fetch(path, { headers: headers() });
    if (!response.ok) {
      let detail = '';
      try {
        detail = (await response.json()).detail || '';
      } catch {
        // Not JSON.
      }
      throw new Error(detail || (response.status === 410 ? 'This file is no longer available. Ask again to make it again.' : `Download failed (${response.status})`));
    }
    const blob = await response.blob();
    const fallback = file.file_name || `${String(file.title || 'report').replace(/[^\w.-]+/g, '_')}.${fileExtension(file)}`;
    const url = URL.createObjectURL(blob);
    try {
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = fileNameFromDisposition(response.headers.get('content-disposition'), fallback);
      anchor.rel = 'noopener';
      document.body.append(anchor);
      anchor.click();
      anchor.remove();
    } finally {
      // Give the click a tick to start before the URL is revoked.
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    }
  } catch (error) {
    showToast(error.message);
  } finally {
    card.classList.remove('busy');
    card.disabled = false;
  }
}

async function openFiles() {
  const list = $('files-list');
  list.replaceChildren(el('p', 'files-empty', 'Loading…'));
  closeDrawer();
  $('files-dialog').showModal();
  try {
    const data = await api('/v1/reports?limit=100');
    const reports = data.reports || [];
    list.replaceChildren(...(reports.length ? reports.map(fileCard) : [el('p', 'files-empty', 'No files yet. Ask for an Excel, Word or PowerPoint file and it will appear here.')]));
  } catch (error) {
    const message = error.status === 503 ? 'Files are not available on this server yet.' : error.status === 403 ? 'Your account cannot make files.' : error.message;
    list.replaceChildren(el('p', 'files-empty', message));
  }
}

// ------------------------------------------------------------------ answers
function compactAnswer(answer) {
  const artifacts = Array.isArray(answer.artifacts) ? answer.artifacts : [];
  const files = artifacts.filter((item) => item && item.type === 'report' && typeof item.download_path === 'string').slice(0, 12).map((item) => ({
    type: 'report', report_id: item.report_id, title: String(item.title || item.file_name || 'File').slice(0, 200), format: item.format,
    file_name: item.file_name, row_count: item.row_count, size_bytes: item.size_bytes, download_path: item.download_path,
  }));
  const emails = artifacts.filter((item) => item && item.type === 'email').slice(0, 5).map((item) => ({ type: 'email', status: item.status }));
  const seen = new Set();
  const sources = [];
  for (const source of answer.sources || answer.citations || []) {
    const entry = { title: String(source.title || source.source_id || '').slice(0, 300), url: source.url || null, locator: source.locator ? String(source.locator).slice(0, 120) : '' };
    const key = `${entry.title}|${entry.url}`;
    if (!seen.has(key) && sources.length < 10) {
      seen.add(key);
      sources.push(entry);
    }
  }
  const notes = (answer.warnings || []).filter((warning) => warning && NOTE_CODES.has(warning.code)).slice(0, 3).map((warning) => String(warning.message).slice(0, 300));
  const approval = answer.status === 'approval_required' && answer.approval?.approval_id ? { approval_id: String(answer.approval.approval_id) } : null;
  return { status: answer.status || 'complete', artifacts: [...files, ...emails], sources, notes, approval, job_id: answer.job_id || null, generation_mode: answer.generation_mode || null };
}

// Questions go to the institutional agent, which answers from the records the
// college has imported. Where the data platform is off (503) or the account may
// not run agent commands (403), the read-only assistant answers instead.
function askAgent(text, approvalId = null, context = chatContext()) {
  return api('/v1/agent/commands', {
    method: 'POST',
    body: JSON.stringify({
      command: text,
      channel: 'text',
      include_data: false,
      approval_id: approvalId,
      conversation_id: context.conversationId,
      history: approvalId ? [] : context.history,
      language: state.language,
    }),
  });
}

function askReadOnlyAssistant(text, context = chatContext()) {
  return api('/v1/chat', {
    method: 'POST',
    body: JSON.stringify({
      prompt: text,
      institution_scope: { college_id: window.SaffronAuth.collegeId() },
      channel: 'text',
      conversation_id: context.conversationId,
      conversational: true,
      history: context.history,
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
  const message = makeMessage('assistant', answerText(data), {
    voice: Boolean(options.voice), language: answer.language || undefined, command: options.command, answer: compactAnswer(answer),
  });
  const conversationId = options.conversationId || state.conversation.id;
  const article = addToConversation(conversationId, message, options.answers || null);
  if (message.answer.status === 'accepted' && message.answer.job_id) followJob(conversationId, message.id, message.answer.job_id);
  return article;
}

// An action that changes institutional records waits for the person to
// confirm it; confirming records the decision and runs the same command again.
function addApprovalControls(article, message) {
  const conversationId = state.conversation.id;
  const { approval } = message.answer;
  const { command } = message;
  const controls = el('div', 'approval-actions');
  const confirm = el('button', 'approval-button', 'Confirm and run');
  confirm.type = 'button';
  const cancel = el('button', 'approval-button secondary', 'Cancel');
  cancel.type = 'button';
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
      await updateMessage(conversationId, message.id, (stored) => {
        stored.answer = { ...stored.answer, decided: approve ? 'confirmed' : 'cancelled' };
      });
      if (!approve || !command) {
        addToConversation(conversationId, makeMessage('assistant', approve ? 'Confirmed.' : 'Cancelled. Nothing was changed.'));
        return;
      }
      const loading = state.conversation.id === conversationId ? showThinking() : null;
      try {
        showAnswer(await askAgent(command, approval.approval_id, { conversationId, history: [] }), { command, conversationId });
      } finally {
        loading?.remove();
      }
    } catch (error) {
      confirm.disabled = false;
      cancel.disabled = false;
      showToast(error.message);
    }
  }
  confirm.addEventListener('click', () => decide(true));
  cancel.addEventListener('click', () => decide(false));
}

// A task the agent runs in the background is checked on until it finishes;
// its answer and files then replace the "working on it" note in the chat.
async function followJob(conversationId, messageId, jobId) {
  if (state.followedJobs.has(jobId)) return;
  state.followedJobs.add(jobId);
  const deadline = Date.now() + JOB_FOLLOW_MS;
  try {
    while (Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, JOB_POLL_MS));
      let job;
      try {
        job = (await api(`/v1/agent/jobs/${encodeURIComponent(jobId)}`)).job;
      } catch (error) {
        if (error.status === 404 || error.status === 403) {
          await updateMessage(conversationId, messageId, (message) => {
            message.text = clip(`${message.text}\n\nThis background task can no longer be followed from here; its files, if any, are in “Your files”.`);
            message.answer = { ...message.answer, job_done: true };
          });
          return;
        }
        continue;
      }
      if (!job || (job.status !== 'succeeded' && job.status !== 'failed')) continue;
      const result = job.result || {};
      await updateMessage(conversationId, messageId, (message) => {
        const done = job.status === 'succeeded';
        message.text = clip(done ? result.answer || message.text : `The background task did not finish: ${job.error || 'unknown error'}.`);
        const update = compactAnswer({ ...result, status: done ? result.status || 'complete' : 'failed' });
        message.answer = { ...message.answer, ...update, job_id: jobId, job_done: true };
      });
      return;
    }
  } finally {
    state.followedJobs.delete(jobId);
  }
}

async function updateMessage(conversationId, messageId, change) {
  const apply = (conversation) => {
    const message = conversation.messages.find((item) => item.id === messageId);
    if (!message) return false;
    change(message);
    touch(conversation);
    return true;
  };
  if (state.conversation?.id === conversationId) {
    if (!apply(state.conversation)) return;
    await saveConversation(state.conversation);
  } else {
    if (!(await HistoryStore.update(conversationId, apply))) return;
    await refreshHistory();
  }
  const message = state.conversation?.id === conversationId ? state.conversation.messages.find((item) => item.id === messageId) : null;
  if (message) {
    const old = $('thread').querySelector(`[data-id="${CSS.escape(messageId)}"]`);
    if (old) old.replaceWith(renderMessage(message));
    markLatest();
  }
}

function updateSendButton() {
  const busy = Boolean(state.conversation && state.inFlight.has(state.conversation.id));
  const send = $('send-button');
  send.classList.toggle('busy', busy);
  send.disabled = busy || !$('text-input').value.trim();
}

function clearTextThinking(conversationId) {
  state.textThinking.get(conversationId)?.remove();
  state.textThinking.delete(conversationId);
}

function resizeInput() {
  const input = $('text-input');
  input.style.height = 'auto';
  input.style.height = `${Math.min(input.scrollHeight, 240)}px`;
}

async function ask(prompt, options = {}) {
  const text = prompt.trim();
  const conversationId = state.conversation.id;
  if (!text || state.inFlight.has(conversationId)) return;
  // The chat and its history are fixed now: the answer, and the read-only
  // fallback's request, stay with this chat even if another is opened.
  const context = { conversationId, history: recentHistory() };
  const question = (options.questionId && state.conversation.messages.find((message) => message.id === options.questionId)) || postUserMessage(text);
  if (!options.keepInput) {
    $('text-input').value = '';
    resizeInput();
  }
  state.inFlight.add(conversationId);
  updateSendButton();
  state.textThinking.set(conversationId, showThinking());

  try {
    let data;
    try {
      data = await askAgent(text, null, context);
    } catch (error) {
      if (error.status !== 503 && error.status !== 403) throw error;
      data = await askReadOnlyAssistant(text, context);
    }
    clearTextThinking(conversationId);
    showAnswer(data, { command: text, conversationId, answers: question.id });
  } catch (error) {
    clearTextThinking(conversationId);
    if (state.conversation.id === conversationId) showError(error.message, question.id);
    showToast(error.message);
  } finally {
    state.inFlight.delete(conversationId);
    updateSendButton();
    if (!narrowScreen.matches && state.conversation.id === conversationId) $('text-input').focus();
  }
}

// Asks the question behind an answer again, in place of that answer.
// Only the latest answer is asked again, so the chat keeps its order.
function retry(message) {
  const conversation = state.conversation;
  if (state.inFlight.has(conversation.id)) return;
  const messages = conversation.messages;
  const index = messages.findIndex((item) => item.id === message.id);
  if (index < 0) return;
  if (index !== messages.length - 1) {
    showToast('Only the latest answer can be asked again.');
    return;
  }
  const question = [...messages.slice(0, index)].reverse().find((item) => item.role === 'user');
  if (!question) return;
  messages.splice(index, 1);
  question.awaiting = true;
  $('thread').querySelector(`[data-id="${CSS.escape(message.id)}"]`)?.remove();
  markLatest();
  saveConversation(conversation).catch(() => {});
  ask(question.text, { questionId: question.id, keepInput: true });
}

// ------------------------------------------------------------------ chats and sidebar
function startNewChat() {
  state.conversation = freshConversation();
  renderConversation();
  history.replaceState(null, '', window.location.pathname);
  closeDrawer();
  if (!narrowScreen.matches) $('text-input').focus();
}

async function openConversation(id) {
  if (state.conversation?.id === id) {
    closeDrawer();
    return;
  }
  const stored = await HistoryStore.get(id);
  if (!stored || stored.owner !== state.owner) {
    showToast('That chat is not on this device.');
    renderConversation();
    return;
  }
  window.speechSynthesis?.cancel();
  state.conversation = stored;
  renderConversation();
  history.replaceState(null, '', `#chat/${encodeURIComponent(id)}`);
  closeDrawer();
}

function groupLabel(time) {
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const day = 86_400_000;
  if (time >= today) return 'Today';
  if (time >= today - day) return 'Yesterday';
  if (time >= today - 7 * day) return 'Previous 7 days';
  if (time >= today - 30 * day) return 'Previous 30 days';
  return new Date(time).toLocaleDateString(undefined, { month: 'long', year: 'numeric' });
}

function matchesQuery(conversation, query) {
  if (conversation.title.toLowerCase().includes(query)) return true;
  return conversation.messages.some((message) => message.text.toLowerCase().includes(query));
}

function renderHistoryList() {
  // Re-rendering would throw away the name being typed; it runs when that ends.
  if (state.renaming) return;
  const list = $('history-list');
  const query = state.historyQuery.trim().toLowerCase();
  const rows = state.conversations.filter((conversation) => !query || matchesQuery(conversation, query));
  const nodes = [];
  if (!rows.length) {
    nodes.push(el('p', 'history-empty', query ? 'No chats match.' : 'Your chats will appear here.'));
  }
  let group = '';
  for (const conversation of rows) {
    const label = groupLabel(conversation.updatedAt);
    if (label !== group) {
      group = label;
      nodes.push(el('h2', 'history-group', label));
    }
    const item = el('div', `history-item${conversation.id === state.conversation?.id ? ' active' : ''}`);
    item.dataset.id = conversation.id;
    const link = el('button', 'history-link', conversation.title);
    link.type = 'button';
    link.title = conversation.title;
    if (conversation.id === state.conversation?.id) link.setAttribute('aria-current', 'page');
    link.addEventListener('click', () => openConversation(conversation.id));
    const more = iconButton('i-more', `Options for ${conversation.title}`, (event) => {
      event.stopPropagation();
      openMenu(more, [
        { label: 'Rename', icon: 'i-pencil', action: () => renameConversation(conversation.id) },
        { label: 'Delete', icon: 'i-trash', danger: true, action: () => deleteConversation(conversation.id) },
      ]);
    });
    more.classList.add('history-more');
    more.setAttribute('aria-haspopup', 'menu');
    more.setAttribute('aria-expanded', 'false');
    item.append(link, more);
    nodes.push(item);
  }
  list.replaceChildren(...nodes);
}

function renameConversation(id) {
  const item = $('history-list').querySelector(`.history-item[data-id="${CSS.escape(id)}"]`);
  const conversation = state.conversations.find((row) => row.id === id);
  if (!item || !conversation) return;
  const input = el('input', 'history-rename');
  input.value = conversation.title;
  input.maxLength = 120;
  input.setAttribute('aria-label', 'Chat name');
  item.replaceChildren(input);
  state.renaming = id;
  input.focus();
  input.select();
  let finished = false;
  const finish = async (save) => {
    if (finished) return;
    finished = true;
    state.renaming = null;
    const title = input.value.replace(/\s+/g, ' ').trim().slice(0, 120);
    if (save && title && title !== conversation.title) {
      const rename = (record) => {
        record.title = title;
        record.titleEdited = true;
      };
      if (state.conversation?.id === id) {
        rename(state.conversation);
        await HistoryStore.put(state.conversation);
        $('chat-title').textContent = title;
      } else {
        await HistoryStore.update(id, rename);
      }
    }
    await refreshHistory();
  };
  input.addEventListener('keydown', (event) => {
    if (event.isComposing || event.keyCode === 229) return;
    if (event.key === 'Enter') finish(true);
    if (event.key === 'Escape') finish(false);
  });
  input.addEventListener('blur', () => finish(true));
}

function confirmDialog(title, text, okLabel, options = {}) {
  const dialog = $('confirm-dialog');
  $('confirm-title').textContent = title;
  $('confirm-text').textContent = text;
  $('confirm-ok').textContent = okLabel;
  $('confirm-ok').className = `button${options.danger === false ? '' : ' danger'}`;
  $('confirm-option').hidden = !options.check;
  $('confirm-check').checked = false;
  $('confirm-check-label').textContent = options.check || '';
  dialog.returnValue = '';
  dialog.showModal();
  return new Promise((resolve) => {
    dialog.addEventListener('close', () => resolve(dialog.returnValue === 'ok'), { once: true });
  });
}

async function deleteConversation(id) {
  const conversation = state.conversations.find((row) => row.id === id);
  if (!conversation) return;
  if (!(await confirmDialog('Delete chat?', `“${conversation.title}” will be deleted from this device.`, 'Delete'))) return;
  await HistoryStore.remove(id);
  if (state.conversation?.id === id) startNewChat();
  await refreshHistory();
}

async function deleteAllConversations() {
  if (!state.conversations.length) {
    showToast('There are no chats to delete.');
    return;
  }
  if (!(await confirmDialog('Delete all chats?', 'Every chat kept on this device for your account will be deleted. Files already made stay in “Your files”.', 'Delete all'))) return;
  await Promise.all(state.conversations.map((conversation) => HistoryStore.remove(conversation.id)));
  startNewChat();
  await refreshHistory();
}

// ------------------------------------------------------------------ menus
let menuAnchor = null;

function closeMenu() {
  $('menu').hidden = true;
  if (menuAnchor) menuAnchor.setAttribute('aria-expanded', 'false');
  menuAnchor = null;
}

function openMenu(anchor, items) {
  const menu = $('menu');
  if (menuAnchor === anchor) {
    closeMenu();
    return;
  }
  closeMenu();
  const nodes = items.map((item) => {
    if (item.separator) return el('div', 'menu-sep');
    if (item.heading) return el('div', 'menu-label', item.heading);
    const button = el('button', `menu-item${item.danger ? ' danger' : ''}`);
    button.type = 'button';
    button.setAttribute('role', item.checked === undefined ? 'menuitem' : 'menuitemradio');
    if (item.checked !== undefined) button.setAttribute('aria-checked', String(item.checked));
    if (item.icon) button.append(icon(item.icon));
    button.append(item.label);
    button.addEventListener('click', () => {
      closeMenu();
      item.action();
    });
    return button;
  });
  menu.replaceChildren(...nodes);
  menu.hidden = false;
  menuAnchor = anchor;
  anchor.setAttribute('aria-expanded', 'true');
  const box = anchor.getBoundingClientRect();
  const width = menu.offsetWidth;
  const height = menu.offsetHeight;
  const left = Math.min(Math.max(8, box.right - width), window.innerWidth - width - 8);
  const below = box.bottom + 6;
  const top = below + height > window.innerHeight - 8 ? Math.max(8, box.top - height - 6) : below;
  menu.style.left = `${left}px`;
  menu.style.top = `${top}px`;
  menu.querySelector('.menu-item')?.focus();
}

function themeSetting() {
  const saved = readSetting('saffron.theme');
  return saved === 'light' || saved === 'dark' ? saved : 'system';
}

function applyTheme(theme) {
  if (theme === 'light' || theme === 'dark') document.documentElement.dataset.theme = theme;
  else delete document.documentElement.dataset.theme;
}

function setTheme(theme) {
  writeSetting('saffron.theme', theme);
  applyTheme(theme);
}

function openAccountMenu() {
  const theme = themeSetting();
  const items = [
    { heading: window.SaffronAuth.currentUser() || 'Signed in' },
    { label: 'Your files', icon: 'i-folder', action: openFiles },
    { separator: true },
    { heading: 'Appearance' },
    { label: 'Match this device', checked: theme === 'system', action: () => setTheme('system') },
    { label: 'Light', checked: theme === 'light', action: () => setTheme('light') },
    { label: 'Dark', checked: theme === 'dark', action: () => setTheme('dark') },
    { separator: true },
    { label: 'Delete all chats on this device', icon: 'i-trash', danger: true, action: deleteAllConversations },
  ];
  if (window.SaffronAuth.mode() === 'oidc') items.push({ label: 'Sign out', icon: 'i-signout', action: signOut });
  openMenu($('account-menu-button'), items);
}

// Chats stay on this device for the next sign-in; on a shared computer the
// person can take them away as they sign out.
async function signOut() {
  const ok = await confirmDialog('Sign out?', 'Your chats stay on this device for your next sign-in unless you delete them now.', 'Sign out', {
    danger: false, check: 'Also delete my chats from this device',
  });
  if (!ok) return;
  if ($('confirm-check').checked) await Promise.all(state.conversations.map((conversation) => HistoryStore.remove(conversation.id)));
  window.SaffronAuth.signOut();
}

// ------------------------------------------------------------------ sidebar layout
function closeDrawer() {
  $('shell').classList.remove('drawer-open');
  $('scrim').hidden = true;
}

function openSidebar() {
  if (narrowScreen.matches) {
    $('shell').classList.add('drawer-open');
    $('scrim').hidden = false;
    return;
  }
  $('shell').classList.remove('sidebar-collapsed');
  writeSetting('saffron.sidebar', 'open');
}

function closeSidebar() {
  if (narrowScreen.matches) {
    closeDrawer();
    return;
  }
  $('shell').classList.add('sidebar-collapsed');
  writeSetting('saffron.sidebar', 'collapsed');
}

function greeting() {
  const hour = new Date().getHours();
  const part = hour < 12 ? 'Good morning' : hour < 17 ? 'Good afternoon' : 'Good evening';
  const name = window.SaffronAuth.displayName().split(/\s+/)[0];
  return name ? `${part}, ${name}` : part;
}

function renderSuggestions() {
  $('suggestions').replaceChildren(...SUGGESTIONS.map((suggestion) => {
    const button = el('button', 'suggestion');
    button.type = 'button';
    button.append(icon(suggestion.icon), suggestion.label);
    button.addEventListener('click', () => ask(suggestion.prompt));
    return button;
  }));
}

// ------------------------------------------------------------------ voice state
function renderVoiceState(message = '') {
  const active = Boolean(state.voiceSessionId);
  const button = $('voice-button');
  button.classList.toggle('active', active);
  button.setAttribute('aria-pressed', String(active));
  $('voice-button-label').textContent = active ? 'Stop voice' : 'Voice';
  button.title = active ? 'Stop the voice conversation' : 'Talk to Agentic Saffron';
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

// Stop whatever the assistant is saying, at once.
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
  const context = state.audioContext;
  // A context the browser still holds muted would never report the end of the
  // sentence: try to wake it, and let the device's own voice speak otherwise.
  if (context.state !== 'running') {
    try {
      await Promise.race([context.resume(), new Promise((resolve) => setTimeout(resolve, 300))]);
    } catch {
      // Resuming is best effort.
    }
    if (context.state !== 'running') return false;
  }
  return new Promise((resolve) => {
    const source = context.createBufferSource();
    source.buffer = buffer;
    source.connect(context.destination);
    // Move on after the clip's length even if 'ended' never fires.
    const guard = setTimeout(() => resolve(true), buffer.duration * 1000 + 1500);
    source.onended = () => { clearTimeout(guard); resolve(true); };
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
  // A microphone the person picked is given to recognition directly (newer
  // Chrome accepts an audio track); otherwise it uses the browser's default.
  const track = state.micDeviceId && !state.micTrackFailed ? state.micStream?.getAudioTracks()[0] : null;
  try {
    if (track && track.readyState === 'live') state.recognition.start(track);
    else state.recognition.start();
  } catch (error) {
    if (error.name === 'InvalidStateError') return;
    if (track) {
      state.micTrackFailed = true;
      try {
        state.recognition.start();
        return;
      } catch {
        // Reported below.
      }
    }
    showToast('Voice input could not start.');
  }
}

function words(text) {
  return (text || '').toLowerCase().normalize('NFC').replace(/[^\p{L}\p{M}\p{N}\s]/gu, ' ').split(/\s+/).filter(Boolean);
}

// The microphone also hears the speaker: what matches the assistant's own words is echo.
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
  if ((state.speaking || state.echoText) && isEcho(text)) {
    countRecognition('dropped_echo');
    return;
  }
  if (!state.pendingTurn) state.turnStartedAt = Date.now();
  state.pendingTurn = `${state.pendingTurn} ${text}`.trim();
  if (!state.speaking) renderVoiceState(`Heard: “${state.pendingTurn}”`);
  holdTurn(TURN_QUIET_MS);
}

// Wait a little for the rest of the sentence, but never past TURN_MAX_HOLD_MS
// from its first words: noise that keeps Chrome's interim results coming must
// not keep what was said from being sent.
function holdTurn(delay) {
  clearTimeout(state.turnTimer);
  const left = TURN_MAX_HOLD_MS - (Date.now() - state.turnStartedAt);
  state.turnTimer = setTimeout(flushTurn, Math.max(0, Math.min(delay, left)));
}

function flushTurn() {
  clearTimeout(state.turnTimer);
  const text = state.pendingTurn;
  state.pendingTurn = '';
  state.turnStartedAt = 0;
  if (text) sendUtterance(text);
}

function sendUtterance(text) {
  const value = text.trim();
  if (!value || !state.voiceTransportReady || !state.voiceSocket || state.voiceSocket.readyState !== WebSocket.OPEN) return;
  const clientMessageId = newId('voice');
  countRecognition('sent');
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
    conversation_id: state.conversation.id,
    history: recentHistory(),
  }));
  const question = postUserMessage(value, { voice: true, language: state.language });
  state.voiceTurns.set(clientMessageId, { conversationId: state.conversation.id, questionId: question.id });
  renderVoiceState('Thinking…');
}

function configureRecognition() {
  if (!SpeechRecognition) return null;
  const recognition = new SpeechRecognition();
  recognition.continuous = true;
  recognition.interimResults = true;
  recognition.lang = state.language;

  recognition.onstart = () => {
    state.recognitionActiveAt = Date.now();
    countRecognition('starts');
    // Result indexes restart at 0 on every start, so keys from an earlier run
    // would swallow a phrase the user repeats (for example "yes" twice).
    state.finalResultKeys.clear();
    if (!state.speaking && !state.waitingForAnswer) renderVoiceState(listeningHint());
  };
  recognition.onresult = (event) => {
    state.recognitionActiveAt = Date.now();
    let interim = '';
    for (let index = event.resultIndex; index < event.results.length; index += 1) {
      const result = event.results[index];
      const transcript = result[0]?.transcript?.trim() || '';
      if (!result.isFinal) {
        interim += `${transcript} `;
        continue;
      }
      countRecognition('finals');
      const resultKey = `${index}:${transcript}`;
      if (transcript && !state.finalResultKeys.has(resultKey)) {
        state.finalResultKeys.add(resultKey);
        if (state.finalResultKeys.size > 100) state.finalResultKeys.clear();
        queueTurn(transcript);
      }
    }
    interim = interim.trim();
    if (!interim) return;
    countRecognition('interim');
    // Still talking: the turn is not over yet (holdTurn caps the wait).
    if (state.pendingTurn) holdTurn(TURN_QUIET_MS * 2);
    // Barge-in: two real words over the assistant's voice stop it at once.
    if (state.speaking && !isEcho(interim) && (words(interim).length >= 2 || STOP_WORDS.test(interim))) {
      interruptReply();
    }
    if (!state.speaking) renderVoiceState(`Hearing: “${interim}”`);
  };
  recognition.onerror = (event) => {
    countRecognition('errors', event.error);
    if (!state.voiceSessionId) return;
    // This browser would not listen to the picked microphone directly: go back
    // to its default microphone (onend restarts recognition).
    if (state.micDeviceId && !state.micTrackFailed && ['audio-capture', 'not-allowed', 'service-not-allowed', 'bad-grammar'].includes(event.error)) {
      state.micTrackFailed = true;
      showToast('This browser listens to its default microphone. Pick it in Chrome settings (chrome://settings/content/microphone).');
      return;
    }
    if (event.error === 'not-allowed' || event.error === 'service-not-allowed') {
      handleVoiceFailure('Microphone permission was denied.');
      return;
    }
    if (event.error === 'language-not-supported') {
      showToast('This browser cannot recognise that language; switching to English (India).');
      setLanguage('en-IN');
      return;
    }
    if (event.error === 'audio-capture') {
      renderVoiceState('No microphone is available. Check that one is connected and not used by another app.');
      return;
    }
    if (event.error === 'network') {
      renderVoiceState('Speech recognition lost its connection; trying again…');
    }
    // onend follows every error and restarts listening.
  };
  recognition.onend = () => {
    countRecognition('ends');
    // Browsers end continuous recognition after silence or a network blip;
    // the conversation keeps listening until the person stops it.
    if (state.shouldListen && state.voiceSessionId && !(HALF_DUPLEX && state.speaking)) {
      window.setTimeout(startRecognition, 120);
    }
  };
  return recognition;
}

// ------------------------------------------------------------------ listening health
function freshRecognitionStats() {
  return { starts: 0, ends: 0, interim: 0, finals: 0, dropped_echo: 0, sent: 0, restarts: 0, errors: [], level_peak: 0 };
}

function countRecognition(name, error) {
  const stats = state.recognitionStats;
  if (!stats) return;
  if (name === 'errors') {
    if (stats.errors.length < 20) stats.errors.push(RECOGNITION_ERRORS.has(error) ? error : 'other');
    return;
  }
  stats[name] += 1;
}

const RECOGNITION_ERRORS = new Set(['no-speech', 'aborted', 'audio-capture', 'network', 'not-allowed', 'service-not-allowed', 'bad-grammar', 'language-not-supported']);

function browserLabel() {
  const match = navigator.userAgent.match(/(Edg|OPR|Chrome|Firefox|Version)\/(\d+)/);
  if (!match) return 'unknown';
  const name = { Edg: 'Edge', OPR: 'Opera', Version: 'Safari' }[match[1]] || match[1];
  return `${name} ${match[2]}`;
}

// Tells the server, every few seconds, what speech recognition did: counts and
// error codes only, never the words. When voice "hears nothing", the server log
// then shows whether the browser heard anything at all.
function sendClientLog() {
  const stats = state.recognitionStats;
  const socket = state.voiceSocket;
  if (!stats || !socket || socket.readyState !== WebSocket.OPEN) return;
  const changed = Object.entries(stats).some(([key, value]) => key !== 'level_peak' && (key === 'errors' ? value.length : value) > 0);
  if (!changed) return;
  socket.send(JSON.stringify({ type: 'client_log', ...stats, browser: browserLabel() }));
  state.recognitionStats = freshRecognitionStats();
}

// Chrome can stop listening without an end event (or after a network error):
// restart recognition when nothing has been heard for a while.
function checkRecognition() {
  if (!state.shouldListen || !state.voiceSessionId || !state.recognition) return;
  if (HALF_DUPLEX && state.speaking) return;
  if (Date.now() - state.recognitionActiveAt < RECOGNITION_STALL_MS) return;
  state.recognitionActiveAt = Date.now();
  countRecognition('restarts');
  try {
    state.recognition.abort();
  } catch {
    // Not running; start it below.
  }
  window.setTimeout(startRecognition, 400);
}

function startListeningHealth() {
  stopListeningHealth();
  state.recognitionStats = freshRecognitionStats();
  state.recognitionActiveAt = Date.now();
  state.watchdogTimer = window.setInterval(checkRecognition, 2000);
  state.clientLogTimer = window.setInterval(sendClientLog, CLIENT_LOG_MS);
}

function stopListeningHealth() {
  sendClientLog();
  clearInterval(state.watchdogTimer);
  clearInterval(state.clientLogTimer);
  state.watchdogTimer = null;
  state.clientLogTimer = null;
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
    startListeningHealth();
    startRecognition();
    listMicrophones();
    return;
  }
  if (message.type === 'thinking') {
    const turn = state.voiceTurns.get(message.client_message_id);
    // Shown only in the chat the question was asked in.
    if (turn && turn.conversationId !== state.conversation.id) return;
    state.thinking.set(message.client_message_id, showThinking({ voice: true }));
    return;
  }
  if (message.type === 'answer') {
    const answer = message.answer || {};
    const text = answer.answer || answer.refusal_reason || 'No answer returned.';
    const command = state.voiceCommands.get(message.client_message_id);
    const turn = state.voiceTurns.get(message.client_message_id) || {};
    state.voiceCommands.delete(message.client_message_id);
    state.voiceTurns.delete(message.client_message_id);
    removeThinking(message.client_message_id);
    showAnswer(answer, { voice: true, command, conversationId: turn.conversationId, answers: turn.questionId });
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
    state.voiceTurns.delete(message.client_message_id);
    state.voiceCommands.delete(message.client_message_id);
    return;
  }
  // Marks the end of a reply's speech; playback ends when its queue drains.
  if (message.type === 'speech_end') return;
  if (message.type === 'pong') return;
  if (message.type === 'expired') {
    const reason = message.reason === 'idle'
      ? 'Voice paused after a quiet spell. Tap Voice to talk again.'
      : message.reason === 'closed'
        ? 'Voice moved to your newer window or tab.'
        : 'The voice session ended. Tap Voice to talk again.';
    handleVoiceFailure(reason);
    return;
  }
  if (message.type === 'error') {
    const turn = message.client_message_id ? state.voiceTurns.get(message.client_message_id) : null;
    if (message.client_message_id) {
      removeThinking(message.client_message_id);
      state.voiceTurns.delete(message.client_message_id);
      state.voiceCommands.delete(message.client_message_id);
      if (message.client_message_id === state.activeReplyId) state.waitingForAnswer = false;
    }
    if (message.code === 'turn_failed' && (!turn || turn.conversationId === state.conversation.id)) showError(message.message, null, { voice: true });
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

async function openMicrophone(deviceId = state.micDeviceId) {
  if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
    throw new Error('Voice input requires HTTPS or localhost and microphone access.');
  }
  const audio = { echoCancellation: true, noiseSuppression: true, autoGainControl: true };
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: deviceId ? { ...audio, deviceId: { exact: deviceId } } : audio });
  } catch (error) {
    // A remembered microphone that is no longer plugged in: use the default.
    if (!deviceId || !['OverconstrainedError', 'NotFoundError', 'NotReadableError'].includes(error.name)) throw error;
    state.micDeviceId = '';
    saveMicrophone('');
    stream = await navigator.mediaDevices.getUserMedia({ audio });
  }
  closeMicrophone();
  state.micStream = stream;
  startMicMeter(stream);
  await listMicrophones();
  return stream;
}

function closeMicrophone() {
  if (state.micMeter) {
    clearInterval(state.micMeter.timer);
    try {
      state.micMeter.source.disconnect();
      state.micMeter.context.close();
    } catch {
      // Already disconnected or closed.
    }
    state.micMeter = null;
  }
  if (state.micStream) state.micStream.getTracks().forEach((track) => track.stop());
  state.micStream = null;
  $('mic-meter').hidden = true;
  $('mic-meter-fill').style.width = '0';
}

// A bar that moves with the sound the microphone picks up, so the person can
// see at once whether they are being heard. Nothing is recorded or sent.
function startMicMeter(stream) {
  const Context = window.AudioContext || window.webkitAudioContext;
  if (!Context) return;
  try {
    // Its own context: a microphone source on the context that plays the
    // assistant's voice can read as silence in Chrome.
    const context = new Context();
    if (context.state === 'suspended') context.resume().catch(() => {});
    const source = context.createMediaStreamSource(stream);
    const analyser = context.createAnalyser();
    analyser.fftSize = 1024;
    source.connect(analyser);
    const samples = new Float32Array(analyser.fftSize);
    const timer = window.setInterval(() => {
      analyser.getFloatTimeDomainData(samples);
      let sum = 0;
      for (const sample of samples) sum += sample * sample;
      const level = Math.min(1, Math.sqrt(sum / samples.length) * 6);
      $('mic-meter-fill').style.width = `${Math.round(level * 100)}%`;
      if (state.recognitionStats) state.recognitionStats.level_peak = Math.max(state.recognitionStats.level_peak, Math.round(level * 100));
    }, 100);
    state.micMeter = { context, source, analyser, timer };
    $('mic-meter').hidden = false;
  } catch {
    // The meter is a convenience; voice works without it.
  }
}

async function listMicrophones() {
  const select = $('mic-select');
  let devices = [];
  try {
    devices = (await navigator.mediaDevices.enumerateDevices()).filter((device) => device.kind === 'audioinput');
  } catch {
    // Listing devices is best effort.
  }
  const current = state.micStream?.getAudioTracks()[0]?.getSettings?.().deviceId || state.micDeviceId;
  select.replaceChildren(...devices.map((device, index) => {
    const option = document.createElement('option');
    option.value = device.deviceId;
    option.textContent = `🎙 ${device.label || `Microphone ${index + 1}`}`;
    return option;
  }));
  if (current && devices.some((device) => device.deviceId === current)) select.value = current;
  select.hidden = devices.length < 2 || !state.voiceSessionId;
}

async function changeMicrophone(deviceId) {
  state.micDeviceId = deviceId;
  state.micTrackFailed = false;
  saveMicrophone(deviceId);
  if (!state.voiceSessionId) return;
  try {
    await openMicrophone(deviceId);
    showToast('Microphone changed. Speak and watch the bar move.');
  } catch (error) {
    showToast(error.message || 'That microphone could not be opened.');
  }
  // Restart recognition so it listens to the chosen microphone.
  try {
    state.recognition?.abort();
  } catch {
    // Not running; it starts again below.
  }
  window.setTimeout(startRecognition, 300);
}

async function openVoiceTransport() {
  const collegeId = window.SaffronAuth.collegeId();
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
    await openMicrophone();
    state.shouldListen = true;
    state.finalResultKeys.clear();
    state.recognition = configureRecognition();
    renderVoiceState('Connecting live voice transport…');
    await openVoiceTransport();
  } catch (error) {
    await cleanupVoiceSession(true);
    renderVoiceState('');
    showToast(error.message || 'Voice could not start.');
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
  state.turnStartedAt = 0;
  state.activeReplyId = null;
  clearTimeout(state.turnTimer);
  clearInterval(state.pingTimer);
  state.pingTimer = null;
  stopListeningHealth();
  stopRecognition();
  closeMicrophone();
  $('mic-select').hidden = true;
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
  renderVoiceState('');
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
    setApiStatus('Online', 'ok', `API ${data.version || 'ready'}`);
  } catch {
    setApiStatus('Offline', 'error', 'The assistant cannot reach its server.');
  }
}

// ------------------------------------------------------------------ events
applyTheme(themeSetting());
if (readSetting('saffron.sidebar') === 'collapsed') $('shell').classList.add('sidebar-collapsed');
state.conversation = freshConversation();
renderSuggestions();

$('text-form').addEventListener('submit', (event) => {
  event.preventDefault();
  ask($('text-input').value);
});

$('text-input').addEventListener('keydown', (event) => {
  // Enter sends; Shift+Enter starts a new line. Enter that confirms an input
  // method's composition (Hindi, Kannada keyboards) is left to it.
  if (event.isComposing || event.keyCode === 229) return;
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault();
    ask(event.target.value);
  }
});
$('text-input').addEventListener('input', () => {
  resizeInput();
  updateSendButton();
});

$('voice-button').addEventListener('click', toggleVoiceSession);
$('interrupt-button').addEventListener('click', () => interruptReply('stop'));
$('language-select').value = state.language;
$('language-select').addEventListener('change', (event) => setLanguage(event.target.value));
$('mic-select').addEventListener('change', (event) => changeMicrophone(event.target.value));
if (window.speechSynthesis) window.speechSynthesis.addEventListener?.('voiceschanged', () => pickVoice(state.language));

$('new-chat').addEventListener('click', startNewChat);
$('head-new-chat').addEventListener('click', startNewChat);
$('sidebar-toggle').addEventListener('click', closeSidebar);
$('sidebar-open').addEventListener('click', openSidebar);
$('scrim').addEventListener('click', closeDrawer);
$('history-search').addEventListener('input', (event) => {
  state.historyQuery = event.target.value;
  renderHistoryList();
});
$('files-open').addEventListener('click', openFiles);
$('files-close').addEventListener('click', () => $('files-dialog').close());
$('account-menu-button').addEventListener('click', (event) => {
  event.stopPropagation();
  openAccountMenu();
});
document.addEventListener('click', (event) => {
  if (!$('menu').hidden && !$('menu').contains(event.target)) closeMenu();
});
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') {
    closeMenu();
    closeDrawer();
  }
  if (!$('menu').hidden && (event.key === 'ArrowDown' || event.key === 'ArrowUp')) {
    const items = [...$('menu').querySelectorAll('.menu-item')];
    const index = items.indexOf(document.activeElement);
    items[(index + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length]?.focus();
    event.preventDefault();
  }
  // Ctrl/Cmd+Shift+O starts a new chat.
  if ((event.ctrlKey || event.metaKey) && event.shiftKey && event.key.toLowerCase() === 'o') {
    event.preventDefault();
    startNewChat();
  }
});
window.addEventListener('resize', closeMenu);
narrowScreen.addEventListener?.('change', closeDrawer);

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
  $('shell').hidden = true;
  $('chat-history').hidden = true;
  $('composer-wrap').hidden = true;
  if (message) $('sign-in-message').textContent = message;
}

function showApp() {
  $('sign-in-gate').hidden = true;
  $('shell').hidden = false;
  $('chat-history').hidden = false;
  $('composer-wrap').hidden = false;
  const auth = window.SaffronAuth;
  const name = auth.displayName() || auth.currentUser() || 'Signed in';
  $('account-name').textContent = auth.mode() === 'demo' ? 'Local development' : name;
  $('account-avatar').textContent = (auth.mode() === 'demo' ? 'D' : name.trim().charAt(0) || '·').toUpperCase();
  $('welcome-title').textContent = greeting();
}

$('sign-in').addEventListener('click', () => {
  window.SaffronAuth.signIn().catch((error) => showToast(error.message));
});

async function start() {
  let mode;
  try {
    mode = await window.SaffronAuth.init();
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

  if (!window.SaffronAuth.isAuthenticated()) {
    showSignInGate();
    setApiStatus('Signed out', 'neutral');
    return;
  }

  showApp();
  state.owner = window.SaffronAuth.userKey();
  state.conversation = freshConversation();
  await refreshHistory();
  const linked = window.location.hash.match(/^#chat\/(.+)$/);
  let linkedId = null;
  try {
    linkedId = linked ? decodeURIComponent(linked[1]) : null;
  } catch {
    // A mangled link: start a new chat instead.
  }
  if (linkedId) await openConversation(linkedId);
  else renderConversation();
  await loadApiStatus();
}

start();

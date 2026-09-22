/*
 * Browser-side identity for Guru Ji.
 *
 * The server decides which scheme it will accept and publishes that at
 * /v1/auth/config. In `demo` mode (local development only) the old fixed
 * bearer token and X-Demo-* headers are used. In `oidc` mode this runs an
 * Authorization Code flow with PKCE and sends the resulting ID token, so the
 * caller's role, capabilities and institution scope come from claims the
 * server verifies rather than from headers the page chose for itself.
 *
 * The ID token is used, not the access token: the server verifies `aud`
 * against its configured client id, and a Cognito access token carries no
 * `aud` claim.
 *
 * Tokens live in sessionStorage so they do not outlive the tab. No refresh
 * token is stored; when the ID token expires the user signs in again.
 */
(function (global) {
  'use strict';

  const TOKEN_KEY = 'guru.auth.token';
  const VERIFIER_KEY = 'guru.auth.verifier';
  const STATE_KEY = 'guru.auth.state';

  // Re-authenticate slightly early so a request cannot be sent with a token
  // that expires while it is in flight.
  const EXPIRY_SKEW_SECONDS = 60;

  const demoSession = {
    principal: 'demo-user',
    role: 'student',
    college: 'college_a',
    capabilities: ['ask:read_only', 'voice:start'],
  };

  // Fail closed: until the server has said which scheme it accepts, nothing is
  // sent and nobody counts as signed in. Demo headers are only ever used when
  // the server explicitly answers `mode: "demo"`.
  const KNOWN_MODES = ['demo', 'oidc', 'unavailable'];
  const RETRY_HINT = 'Reload the page to try again.';

  let config = { mode: 'unavailable' };
  let discovery = null;
  let token = null;

  function store() {
    try {
      return global.sessionStorage;
    } catch {
      return null; // Storage can be blocked entirely; treat as signed out.
    }
  }

  function readStored(key) {
    const s = store();
    if (!s) return null;
    try {
      return s.getItem(key);
    } catch {
      return null;
    }
  }

  function writeStored(key, value) {
    const s = store();
    if (!s) return;
    try {
      if (value === null) s.removeItem(key);
      else s.setItem(key, value);
    } catch {
      // A full or disabled store is not fatal; the user signs in again.
    }
  }

  function base64UrlEncode(bytes) {
    let binary = '';
    for (const b of new Uint8Array(bytes)) binary += String.fromCharCode(b);
    return global.btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  }

  function randomUrlSafe(byteLength) {
    const bytes = new Uint8Array(byteLength);
    global.crypto.getRandomValues(bytes);
    return base64UrlEncode(bytes);
  }

  async function pkceChallenge(verifier) {
    const digest = await global.crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier));
    return base64UrlEncode(digest);
  }

  function decodeJwtPayload(jwt) {
    const parts = jwt.split('.');
    if (parts.length !== 3) throw new Error('malformed token');
    const padded = parts[1].replace(/-/g, '+').replace(/_/g, '/');
    return JSON.parse(global.atob(padded + '='.repeat((4 - (padded.length % 4)) % 4)));
  }

  function tokenIsUsable(candidate) {
    if (!candidate || !candidate.idToken || !candidate.expiresAt) return false;
    return candidate.expiresAt - EXPIRY_SKEW_SECONDS > Math.floor(Date.now() / 1000);
  }

  function loadToken() {
    const raw = readStored(TOKEN_KEY);
    if (!raw) return null;
    try {
      const parsed = JSON.parse(raw);
      return tokenIsUsable(parsed) ? parsed : null;
    } catch {
      return null;
    }
  }

  function rememberToken(idToken) {
    // `exp` is only read to decide when to re-authenticate. Authority still
    // comes from the server verifying the signature.
    const claims = decodeJwtPayload(idToken);
    const scopes = Array.isArray(claims.guru_scopes) ? claims.guru_scopes : [];
    const value = {
      idToken,
      expiresAt: Number(claims.exp) || 0,
      subject: claims.sub || '',
      email: claims.email || '',
      // Mirrors the server's own precedence in app.auth.oidc._scopes. The
      // server re-derives this from the verified token; the copy here only
      // populates request bodies so the page does not have to guess.
      collegeId: claims.college_id || claims.college || (scopes[0] && scopes[0].college_id) || '',
    };
    writeStored(TOKEN_KEY, JSON.stringify(value));
    return value;
  }

  function redirectUri() {
    return global.location.origin + '/';
  }

  async function loadDiscovery() {
    if (discovery) return discovery;
    const url = config.issuer.replace(/\/+$/, '') + '/.well-known/openid-configuration';
    const response = await fetch(url);
    if (!response.ok) throw new Error('could not load identity provider metadata');
    discovery = await response.json();
    return discovery;
  }

  async function beginLogin() {
    if (config.mode !== 'oidc') {
      // Reached when the config could not be loaded (or sign-in is disabled):
      // the gate's button then doubles as "retry", which re-fetches the config.
      global.location.reload();
      return;
    }
    const meta = await loadDiscovery();
    const verifier = randomUrlSafe(48);
    const stateValue = randomUrlSafe(16);
    writeStored(VERIFIER_KEY, verifier);
    writeStored(STATE_KEY, stateValue);

    const params = new URLSearchParams({
      response_type: 'code',
      client_id: config.client_id,
      redirect_uri: redirectUri(),
      scope: (config.scopes || ['openid']).join(' '),
      state: stateValue,
      code_challenge: await pkceChallenge(verifier),
      code_challenge_method: 'S256',
    });
    global.location.assign(meta.authorization_endpoint + '?' + params.toString());
  }

  async function completeLogin(code, returnedState) {
    const expectedState = readStored(STATE_KEY);
    const verifier = readStored(VERIFIER_KEY);
    writeStored(STATE_KEY, null);
    writeStored(VERIFIER_KEY, null);

    if (!expectedState || returnedState !== expectedState) {
      throw new Error('sign-in could not be verified; please try again');
    }
    if (!verifier) throw new Error('sign-in state was lost; please try again');

    const meta = await loadDiscovery();
    const response = await fetch(meta.token_endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({
        grant_type: 'authorization_code',
        client_id: config.client_id,
        code,
        redirect_uri: redirectUri(),
        code_verifier: verifier,
      }).toString(),
    });
    if (!response.ok) throw new Error('sign-in failed while exchanging the code');
    const payload = await response.json();
    if (!payload.id_token) throw new Error('identity provider returned no ID token');
    return rememberToken(payload.id_token);
  }

  function clearQueryParams() {
    global.history.replaceState({}, document.title, global.location.pathname);
  }

  /**
   * Ask the server which scheme it accepts. Anything other than a well-formed
   * answer is an error the page shows on the sign-in gate; it never becomes a
   * silent fallback to demo headers.
   */
  async function loadConfig() {
    let response;
    try {
      response = await fetch('/v1/auth/config', { cache: 'no-store' });
    } catch {
      throw new Error('The server could not be reached to find out how to sign in. ' + RETRY_HINT);
    }
    if (!response.ok) {
      throw new Error('The server did not say how to sign in (HTTP ' + response.status + '). ' + RETRY_HINT);
    }
    let payload;
    try {
      payload = await response.json();
    } catch {
      throw new Error('The sign-in configuration could not be read. ' + RETRY_HINT);
    }
    if (!payload || KNOWN_MODES.indexOf(payload.mode) === -1) {
      throw new Error('The server answered with an unknown sign-in mode. ' + RETRY_HINT);
    }
    if (payload.mode === 'oidc' && (!payload.issuer || !payload.client_id)) {
      throw new Error('The sign-in configuration is incomplete. ' + RETRY_HINT);
    }
    return payload;
  }

  /**
   * Resolve how this page should authenticate, and finish a redirect if one is
   * in progress. Returns the active mode so the caller can render accordingly.
   * Throws when the configuration could not be loaded; `config` then stays
   * `unavailable`, so no request carries credentials and nobody is signed in.
   */
  async function init() {
    config = { mode: 'unavailable' };
    config = await loadConfig();

    if (config.mode !== 'oidc') return config.mode;

    const params = new URLSearchParams(global.location.search);
    const error = params.get('error');
    if (error) {
      clearQueryParams();
      throw new Error(params.get('error_description') || 'sign-in was refused');
    }

    const code = params.get('code');
    if (code) {
      try {
        token = await completeLogin(code, params.get('state'));
      } finally {
        clearQueryParams();
      }
      return config.mode;
    }

    token = loadToken();
    return config.mode;
  }

  function isAuthenticated() {
    if (config.mode === 'demo') return true;
    if (config.mode === 'oidc') return tokenIsUsable(token);
    return false;
  }

  function currentUser() {
    if (config.mode === 'demo') return demoSession.principal;
    if (config.mode === 'oidc' && token) return token.email || token.subject;
    return '';
  }

  function collegeId() {
    if (config.mode === 'demo') return demoSession.college;
    if (config.mode === 'oidc' && token) return token.collegeId;
    return '';
  }

  function headers(json) {
    const result = {};
    if (config.mode === 'oidc') {
      if (tokenIsUsable(token)) result.Authorization = 'Bearer ' + token.idToken;
    } else if (config.mode === 'demo') {
      result.Authorization = 'Bearer dev-token';
      result['X-Demo-Principal'] = demoSession.principal;
      result['X-Demo-Role'] = demoSession.role;
      result['X-Demo-College'] = demoSession.college;
      result['X-Demo-Capabilities'] = demoSession.capabilities.join(',');
    }
    if (json) result['Content-Type'] = 'application/json';
    return result;
  }

  function signOut() {
    token = null;
    writeStored(TOKEN_KEY, null);
    if (config.mode === 'oidc' && discovery && discovery.end_session_endpoint) {
      const params = new URLSearchParams({
        client_id: config.client_id,
        logout_uri: redirectUri(),
      });
      global.location.assign(discovery.end_session_endpoint + '?' + params.toString());
      return;
    }
    global.location.reload();
  }

  global.GuruAuth = {
    init,
    headers,
    isAuthenticated,
    currentUser,
    collegeId,
    signIn: beginLogin,
    signOut,
    mode: () => config.mode,
    demoSession,
  };
})(window);

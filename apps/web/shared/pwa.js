/*
 * Installable-app support shared by the client assistant and the developer
 * console: registers the service worker, offers "Install" where the browser
 * allows it (Chrome and Edge on desktop and Android), explains "Add to Home
 * Screen" on iPhone and iPad Safari, and keeps the layout sized to the part of
 * the screen the on-screen keyboard leaves visible.
 */
(function () {
  'use strict';

  const DISMISS_KEY = 'saffron.install.dismissed';
  const isIos = /iPad|iPhone|iPod/.test(navigator.userAgent)
    || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  const isStandalone = window.matchMedia('(display-mode: standalone)').matches
    || window.navigator.standalone === true;

  if (isStandalone) document.documentElement.classList.add('standalone');

  if ('serviceWorker' in navigator && window.isSecureContext) {
    window.addEventListener('load', () => {
      navigator.serviceWorker.register('/sw.js').catch(() => {
        // The app works without it; it just cannot be installed or open offline.
      });
    });
  }

  function dismissed() {
    try {
      return window.localStorage.getItem(DISMISS_KEY) === '1';
    } catch {
      return false;
    }
  }

  function remember() {
    try {
      window.localStorage.setItem(DISMISS_KEY, '1');
    } catch {
      // Storage can be blocked; the hint just shows again next time.
    }
  }

  function showBanner(message, actionLabel, onAction) {
    if (document.getElementById('install-banner')) return;
    const banner = document.createElement('div');
    banner.id = 'install-banner';
    banner.className = 'install-banner';
    banner.setAttribute('role', 'region');
    banner.setAttribute('aria-label', 'Install Agentic Saffron');

    const icon = document.createElement('img');
    icon.className = 'install-icon';
    icon.src = '/icons/icon-192.png';
    icon.alt = '';

    const text = document.createElement('p');
    text.className = 'install-text';
    text.textContent = message;

    const actions = document.createElement('div');
    actions.className = 'install-actions';
    if (actionLabel) {
      const action = document.createElement('button');
      action.type = 'button';
      action.className = 'install-button';
      action.textContent = actionLabel;
      action.addEventListener('click', () => {
        banner.remove();
        onAction();
      });
      actions.appendChild(action);
    }
    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'install-close';
    close.setAttribute('aria-label', 'Dismiss');
    close.textContent = '×';
    close.addEventListener('click', () => {
      remember();
      banner.remove();
    });
    actions.appendChild(close);

    banner.append(icon, text, actions);
    // Under the top bar, in the page flow, so it never covers the composer.
    const topbar = document.querySelector('.topbar, .main-head');
    if (topbar) {
      topbar.after(banner);
    } else {
      banner.classList.add('floating');
      document.body.appendChild(banner);
    }
  }

  // Chrome and Edge: keep the browser's install prompt for our own button.
  let deferredPrompt = null;
  window.addEventListener('beforeinstallprompt', (event) => {
    event.preventDefault();
    deferredPrompt = event;
    if (dismissed()) return;
    showBanner('Install Agentic Saffron as an app for quicker access.', 'Install', async () => {
      const prompt = deferredPrompt;
      deferredPrompt = null;
      if (!prompt) return;
      prompt.prompt();
      try {
        const choice = await prompt.userChoice;
        if (choice && choice.outcome === 'dismissed') remember();
      } catch {
        // Nothing to do; the browser closed its prompt.
      }
    });
  });
  window.addEventListener('appinstalled', () => {
    deferredPrompt = null;
    document.getElementById('install-banner')?.remove();
  });

  // Safari on iPhone/iPad has no install prompt; say where the option is.
  const isSafari = /Safari/.test(navigator.userAgent) && !/CriOS|FxiOS|EdgiOS|OPiOS/.test(navigator.userAgent);
  if (isIos && isSafari && !isStandalone && !dismissed()) {
    window.addEventListener('load', () => {
      showBanner('Install Agentic Saffron: tap Share, then “Add to Home Screen”.', null, null);
    });
  }

  // iOS does not shrink the layout when the keyboard opens; it scrolls the
  // page instead, pushing the top bar away and leaving the composer floating.
  // Sizing the app to the visible area keeps both on screen.
  const viewport = window.visualViewport;
  const touch = window.matchMedia('(pointer: coarse)').matches;
  if (viewport && touch) {
    const root = document.documentElement;
    let frame = 0;
    const fit = () => {
      frame = 0;
      if (viewport.scale > 1.01) return; // Pinch-zoomed: leave the layout alone.
      root.style.setProperty('--app-height', `${Math.round(viewport.height)}px`);
      if (window.scrollY !== 0 && document.body.dataset.scrolls !== 'page') window.scrollTo(0, 0);
    };
    const schedule = () => {
      if (!frame) frame = window.requestAnimationFrame(fit);
    };
    viewport.addEventListener('resize', schedule);
    viewport.addEventListener('scroll', schedule);
    window.addEventListener('orientationchange', schedule);
    fit();
  }
})();

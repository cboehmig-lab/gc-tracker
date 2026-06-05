// ── Google Analytics init ────────────────────────────────────────────────────
// Reads GA measurement ID from <meta name="ga-id"> injected by the server.
// This runs at parse time (script at bottom of <body>), so dataLayer is
// queued and processed when gtag.js finishes loading asynchronously.
(function() {
  var meta = document.querySelector('meta[name="ga-id"]');
  var gaId = meta && meta.getAttribute('content');
  if (!gaId) return;
  window.dataLayer = window.dataLayer || [];
  function gtag(){window.dataLayer.push(arguments);}
  window.gtag = gtag;
  gtag('js', new Date());
  gtag('config', gaId);
})();

let allStores = [], favorites = [], running = false;

// ── Mobile sidebar / store sheet ─────────────────────────────────────────────
function _isMobile() { return window.innerWidth <= 820; }

// Keep sticky table headers just below the sticky filter bar.
// #results-top-bar is inside the same scroll container (.results), also sticky at top:0.
// Table headers must use top = filter-bar height so they don't hide behind it.
function _applyFrozenHeaderOffset() {
  if (_isMobile()) return;
  const topBar = document.getElementById('results-top-bar');
  const h = topBar ? topBar.offsetHeight : 88;
  document.documentElement.style.setProperty('--tbl-hdr-top', h + 'px');
}
window.addEventListener('resize', _applyFrozenHeaderOffset);

function toggleMobileSidebar(which) {
  if (_isMobile()) {
    const panel = document.getElementById(which === 'gc' ? 'gc-left' : 'cl-left');
    const isOpen = panel.classList.toggle('sheet-open');
    const backdrop = document.getElementById('store-sheet-backdrop');
    if (backdrop) {
      backdrop.classList.toggle('visible', isOpen);
    }
    return;
  }
  // Desktop: collapse/expand in layout
  const panel = document.getElementById(which === 'gc' ? 'gc-left' : 'cl-left');
  const arrow = document.getElementById(which + '-toggle-arrow');
  const isCollapsed = panel.classList.toggle('collapsed');
  arrow.classList.toggle('open', !isCollapsed);
}

function _closeAllSheets() {
  ['gc-left', 'cl-left'].forEach(id => {
    document.getElementById(id)?.classList.remove('sheet-open');
  });
  document.getElementById('gc-filter-collapsible')?.classList.remove('sheet-open');
  const backdrop = document.getElementById('store-sheet-backdrop');
  if (backdrop) backdrop.classList.remove('visible');
}
// Keep old name as alias so any other callers still work
const _closeStoreSheet = _closeAllSheets;

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') _closeAllSheets();
});

// ── Swipe-to-dismiss for mobile bottom sheets ─────────────────────────────────
function _initSwipeDismiss(sheetEl, closeFn, scrollBodySel) {
  let startY = 0, startScrollTop = 0, dragging = false;
  const getScrollBody = () => (scrollBodySel ? sheetEl.querySelector(scrollBodySel) : null);

  sheetEl.addEventListener('touchstart', e => {
    const sb = getScrollBody();
    startScrollTop = sb ? sb.scrollTop : 0;
    startY = e.touches[0].clientY;
    dragging = false;
  }, {passive: true});

  sheetEl.addEventListener('touchmove', e => {
    const dy = e.touches[0].clientY - startY;
    if (!dragging) {
      // Start drag only when swiping downward and scroll body is at the top
      if (dy > 10 && startScrollTop <= 0) {
        dragging = true;
        sheetEl.style.transition = 'none';
      } else {
        return; // let normal scrolling happen
      }
    }
    if (dragging) {
      const offset = Math.max(0, dy);
      sheetEl.style.transform = 'translateY(' + offset + 'px)';
      e.preventDefault();
    }
  }, {passive: false});

  sheetEl.addEventListener('touchend', e => {
    if (!dragging) return;
    dragging = false;
    const dy = e.changedTouches[0].clientY - startY;
    sheetEl.style.transition = '';
    if (dy > 90) {
      sheetEl.style.transform = '';  // let CSS class removal animate it out
      closeFn();
    } else {
      sheetEl.style.transform = 'translateY(0)';  // snap back
    }
  }, {passive: true});
}

// Wire up swipe-dismiss once DOM is ready (only matters on mobile)
document.addEventListener('DOMContentLoaded', () => {
  const storeSheet  = document.getElementById('gc-left');
  const filterSheet = document.getElementById('gc-filter-collapsible');
  if (storeSheet)  _initSwipeDismiss(storeSheet,  _closeAllSheets, '#store-list');
  if (filterSheet) _initSwipeDismiss(filterSheet, _closeAllSheets, '.filter-scroll-body');
});

// ── Search info popover ───────────────────────────────────────────────────────
function _toggleSearchInfo(e) {
  e.stopPropagation();
  const pop = document.getElementById('search-info-popover');
  if (pop) pop.classList.toggle('open');
}
document.addEventListener('click', function(e) {
  const pop = document.getElementById('search-info-popover');
  const btn = document.getElementById('search-info-btn');
  if (pop && btn && pop.classList.contains('open') && e.target !== btn && !btn.contains(e.target))
    pop.classList.remove('open');
});

// Close saved-searches dropdown on outside click
document.addEventListener('click', function(e) {
  const dd  = document.getElementById('ss-dropdown');
  const btn = document.getElementById('saved-searches-btn');
  if (!dd || dd.style.display !== 'block') return;
  const insideBtn = btn && (e.target === btn || btn.contains(e.target));
  if (!insideBtn && !dd.contains(e.target)) _closeSavedSearchesDropdown();
});
// Persistent delegated listener for saved-searches dropdown actions
// (single listener — avoids {once:true} accumulation across re-renders)
(function() {
  const dd = document.getElementById('ss-dropdown');
  if (!dd) return;
  dd.addEventListener('click', function(e) {
    if (e.target.closest('[data-ss-restore]')) { _closeSavedSearchesDropdown(); _restoreFilterState(); return; }
    if (e.target.closest('[data-ss-clear]')) { _closeSavedSearchesDropdown(); clearFilters(); return; }
    const delBtn = e.target.closest('[data-ss-del]');
    if (delBtn) { e.stopPropagation(); _deleteSavedSearch(delBtn.dataset.ssDel); return; }
    const item = e.target.closest('[data-ss-id]');
    if (item) _applySavedSearch(item.dataset.ssId);
  });
})();

// Prevent pinch-zoom on iOS (Safari ignores user-scalable=no since iOS 10)
document.addEventListener('gesturestart', function(e) { e.preventDefault(); }, { passive: false });
document.addEventListener('touchmove', function(e) { if (e.touches && e.touches.length > 1) e.preventDefault(); }, { passive: false });

function _updateMobileToggleCounts() {
  const gcCount = document.getElementById('gc-toggle-count');
  if (gcCount) {
    const n = document.querySelectorAll('.store-row input:checked').length;
    gcCount.textContent = n > 0 ? n + ' selected' : '';
  }
  const clCount = document.getElementById('cl-toggle-count');
  if (clCount) {
    const n = document.querySelectorAll('.cl-city-row input:checked').length;
    clCount.textContent = n > 0 ? n + ' selected' : '';
  }
  _updateMobileBottomBar();
}

// ── Mobile filter toggle ─────────────────────────────────────────────────────
function toggleMobileFilters(which) {
  const body = document.getElementById(which + '-filter-collapsible');
  if (!body) return;
  if (_isMobile()) {
    const isOpen = body.classList.toggle('sheet-open');
    const backdrop = document.getElementById('store-sheet-backdrop');
    if (backdrop) backdrop.classList.toggle('visible', isOpen);
    return;
  }
  // Desktop: collapse/expand inline
  const arrow = document.getElementById(which + '-filter-arrow');
  const isCollapsed = body.classList.toggle('collapsed');
  if (arrow) arrow.classList.toggle('open', !isCollapsed);
}

function _updateFilterDot() {
  // Show a red dot on the Filters toggle when any filter is active
  const dot = document.getElementById('gc-filter-dot');
  if (!dot) return;
  const hasFilters = (window._selectedBrands && window._selectedBrands.length) ||
    (window._selectedConds && window._selectedConds.length) ||
    (window._selectedCats && window._selectedCats.length) ||
    (window._selectedSubs && window._selectedSubs.length) ||
    _watchFilterActive ||
    _priceDropFilterActive ||
    window._priceMin !== null ||
    window._priceMax !== null ||
    (document.getElementById('res-search').value.trim().length > 0);
  dot.classList.toggle('visible', !!hasFilters);
  _updateSaveSearchBtn();
  _updateMobileBottomBar();
}

// ── Mobile bottom action bar ─────────────────────────────────────────────────
function _mbbStores() {
  const which = document.querySelector('.app-tab.active')?.classList.contains('cl-tab') ? 'cl' : 'gc';
  const panel = document.getElementById(which === 'gc' ? 'gc-left' : 'cl-left');
  const willOpen = !panel.classList.contains('sheet-open');
  // Close filter sheet first if we're opening stores
  if (willOpen) {
    document.getElementById('gc-filter-collapsible')?.classList.remove('sheet-open');
  }
  toggleMobileSidebar(which);
}

function _mbbFilters() {
  const filters = document.getElementById('gc-filter-collapsible');
  const willOpen = filters && !filters.classList.contains('sheet-open');
  // Close store sheet first if we're opening filters
  if (willOpen) {
    ['gc-left', 'cl-left'].forEach(id => {
      document.getElementById(id)?.classList.remove('sheet-open');
    });
  }
  toggleMobileFilters('gc');
}

function _mbbCheck() {
  if (running) return;
  runTracker();
}

function _updateMobileBottomBar() {
  if (!_isMobile()) return;

  // Filters active dot
  const hasFilters = (window._selectedBrands && window._selectedBrands.length) ||
    (window._selectedConds && window._selectedConds.length) ||
    (window._selectedCats && window._selectedCats.length) ||
    (window._selectedSubs && window._selectedSubs.length) ||
    _watchFilterActive || _priceDropFilterActive ||
    (document.getElementById('res-search')?.value.trim().length > 0);
  const dot = document.getElementById('mbb-filter-dot');
  if (dot) dot.classList.toggle('visible', !!hasFilters);

  // Check Now button state
  const btn = document.getElementById('mbb-check');
  const icon = document.getElementById('mbb-check-icon');
  const label = document.getElementById('mbb-check-label');
  if (btn && icon && label) {
    if (running) {
      btn.classList.add('scanning');
      icon.textContent = '⏳';
      label.textContent = 'Scanning…';
    } else {
      btn.classList.remove('scanning');
      icon.textContent = '▶';
      label.textContent = 'Scan For New';
    }
  }
}

// Init sidebar/filter state on page load
document.addEventListener('DOMContentLoaded', () => {
  if (_isMobile()) {
    // Store panels and filter sheet start closed — CSS transform handles it, no class needed
    _updateMobileBottomBar();
    _updateViewToggleBtn();
  } else {
    // Desktop: ensure sidebars are expanded
    document.getElementById('gc-left').classList.remove('collapsed');
    document.getElementById('cl-left').classList.remove('collapsed');
  }
});
// Handle orientation change / resize
window.addEventListener('resize', () => {
  const gcFilters = document.getElementById('gc-filter-collapsible');
  if (!_isMobile()) {
    // Switching to desktop: reset sheet state, show sidebars
    _closeStoreSheet();
    document.getElementById('gc-left').classList.remove('collapsed');
    document.getElementById('cl-left').classList.remove('collapsed');
    if (gcFilters) gcFilters.classList.remove('collapsed');
  }
});

// ── localStorage helpers ─────────────────────────────────────────────────────
function _lsGet(key, fallback) {
  try { const v = localStorage.getItem('gt_' + key); return v ? JSON.parse(v) : fallback; }
  catch(e) { return fallback; }
}
function _lsSet(key, val) {
  try {
    localStorage.setItem('gt_' + key, JSON.stringify(val));
  } catch(e) {
    // localStorage full — clear legacy non-critical keys and retry
    console.warn('localStorage full for gt_' + key + ', attempting cleanup…');
    try {
      ['prev_snapshot', 'prev_fp_set'].forEach(k => {
        try { localStorage.removeItem('gt_' + k); } catch(_) {}
      });
      localStorage.setItem('gt_' + key, JSON.stringify(val));
    } catch(e2) {
      console.error('localStorage write failed for gt_' + key + ': ' + e2.message);
    }
  }
}
function _lsSetVerified(key, val) {
  // Write AND verify — critical for large data
  // where a silent failure could cause data loss
  _lsSet(key, val);
  try {
    const readback = localStorage.getItem('gt_' + key);
    if (!readback) {
      console.error('CRITICAL: gt_' + key + ' failed to persist — localStorage may be full');
      return false;
    }
    return true;
  } catch(e) {
    console.error('CRITICAL: gt_' + key + ' readback failed: ' + e.message);
    return false;
  }
}


// ── Auth & server sync ────────────────────────────────────────────────────────
window._authUser = null;  // null = not logged in, {email} = logged in
let _syncTimer = null;

// Show Google sign-in buttons if the server has Google OAuth configured
(async function _initGoogleOAuth() {
  try {
    const r = await fetch('/api/auth/config');
    const d = await r.json();
    window._googleOauthEnabled = !!d.google_oauth;
    if (d.google_oauth) {
      ['auth-google-wrap','auth-google-wrap-reg','welcome-google-wrap','welcome-google-wrap-reg'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.style.display = '';
      });
    }
    const params = new URLSearchParams(window.location.search);
    // Show error if redirected back with ?google_error=1
    if (params.get('google_error') === '1') {
      const url = new URL(window.location.href);
      url.searchParams.delete('google_error');
      history.replaceState(null, '', url.toString());
      _openAuthModal('login');
      const el = document.getElementById('auth-login-err');
      if (el) { el.textContent = 'Google sign-in failed. Please try again or use your username and password.'; }
    }
    // Show welcome modal for new Google users
    if (params.get('google_new') === '1') {
      const url = new URL(window.location.href);
      url.searchParams.delete('google_new');
      history.replaceState(null, '', url.toString());
      _gwOpen();
    }
  } catch(e) {}
})();

function _googleSignIn(next) {
  window.location.href = '/api/auth/google' + (next ? '?next=' + encodeURIComponent(next) : '');
}

// ── Google welcome modal (new Google users) ──────────────────────────────────
function _gwOpen() {
  const modal = document.getElementById('google-welcome-modal');
  if (!modal) return;
  // Pre-fill with current username
  const input = document.getElementById('gw-username');
  if (input && window._authUser) input.value = window._authUser.username || '';
  modal.classList.add('open');
}
function _gwSkip() {
  document.getElementById('google-welcome-modal').classList.remove('open');
}
function _gwClearImport() {
  document.getElementById('gw-msg').textContent = '';
  document.getElementById('gw-msg').className = 'gw-msg';
}
function _gwToggleImport() {
  const sec = document.getElementById('gw-import-section');
  const btn = document.getElementById('gw-import-toggle');
  const open = sec.classList.toggle('open');
  btn.textContent = open ? '− Hide import' : '+ Import existing account';
}
async function _gwSubmit() {
  const username = (document.getElementById('gw-username').value || '').trim();
  const importPw = (document.getElementById('gw-import-pw').value || '').trim();
  const msg      = document.getElementById('gw-msg');
  const btn      = document.getElementById('gw-submit');
  msg.textContent = ''; msg.className = 'gw-msg';
  if (!username || username.length < 3) {
    msg.textContent = 'Username must be at least 3 characters.'; msg.className = 'gw-msg error'; return;
  }
  btn.disabled = true; btn.textContent = 'Saving…';
  try {
    const body = {username};
    if (importPw) body.import_password = importPw;
    const r = await fetch('/api/setup-google-account', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const d = await r.json();
    if (d.error === 'taken') {
      // Username belongs to existing account — show import section
      msg.textContent = 'That username belongs to an existing account. Enter its password above to import your saved data.';
      msg.className = 'gw-msg error';
      const sec = document.getElementById('gw-import-section');
      if (!sec.classList.contains('open')) _gwToggleImport();
      document.getElementById('gw-import-pw').focus();
    } else if (d.error === 'wrong_password') {
      msg.textContent = 'Incorrect password for that account.';
      msg.className = 'gw-msg error';
    } else if (d.error) {
      msg.textContent = d.error; msg.className = 'gw-msg error';
    } else {
      // Success — update auth UI and close
      if (d.status === 'imported') {
        msg.textContent = '✓ Data imported successfully!'; msg.className = 'gw-msg success';
        if (d.data) _loadAndMergeServerData(d.data);
      }
      _setAuthUI(d.username, '');
      window._authUser = {username: d.username};
      setTimeout(() => _gwSkip(), d.status === 'imported' ? 1200 : 0);
    }
  } catch(e) {
    msg.textContent = 'Network error. Please try again.'; msg.className = 'gw-msg error';
  }
  btn.disabled = false; btn.textContent = 'Save & Continue';
}

// ── Google link banner (existing password users without Google linked) ────────
function _glinkStart() {
  window.location.href = '/api/auth/google?next=' + encodeURIComponent(window.location.pathname);
}
function _glinkDismiss() {
  try { localStorage.setItem('gt_google_link_dismissed', '1'); } catch(e) {}
  document.getElementById('google-link-banner').classList.remove('show');
}
function _maybeShowLinkBanner(googleLinked, hasEmail, googleOauthEnabled) {
  if (!googleOauthEnabled || googleLinked) return;
  try { if (localStorage.getItem('gt_google_link_dismissed') === '1') return; } catch(e) {}
  const banner = document.getElementById('google-link-banner');
  if (banner) banner.classList.add('show');
}

function _openAuthModal(tab) {
  tab = tab || 'login';
  _switchAuthTab(tab);
  document.getElementById('auth-modal').classList.add('open');
  setTimeout(() => {
    const el = document.getElementById(tab === 'login' ? 'auth-login-email' : 'auth-reg-email');
    if (el) el.focus();
  }, 80);
}
function _closeAuthModal() {
  document.getElementById('auth-modal').classList.remove('open');
}
function _switchAuthTab(tab) {
  document.getElementById('auth-form-login').style.display     = tab === 'login'    ? '' : 'none';
  document.getElementById('auth-form-register').style.display  = tab === 'register' ? '' : 'none';
  document.getElementById('auth-tab-login').classList.toggle('active',    tab === 'login');
  document.getElementById('auth-tab-register').classList.toggle('active', tab === 'register');
  document.getElementById('auth-login-err').textContent = '';
  document.getElementById('auth-reg-err').textContent   = '';
}

// Close modal on backdrop click
document.addEventListener('click', e => {
  const modal = document.getElementById('auth-modal');
  if (modal && modal.classList.contains('open') && e.target === modal) _closeAuthModal();
});
// Close modal on Escape
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') _closeAuthModal();
});
// Enter key submits forms
document.addEventListener('keydown', e => {
  if (e.key !== 'Enter') return;
  // Header auth modal
  const modal = document.getElementById('auth-modal');
  if (modal && modal.classList.contains('open')) {
    if (document.getElementById('auth-form-login').style.display !== 'none')         _authLogin();
    else if (document.getElementById('auth-form-register').style.display !== 'none') _authRegister();
    return;
  }
  // Welcome modal
  const welcome = document.getElementById('first-run-modal');
  if (welcome && welcome.style.display === 'flex') {
    if (document.getElementById('welcome-form-login').style.display !== 'none')         _welcomeLogin();
    else if (document.getElementById('welcome-form-register').style.display !== 'none') _welcomeRegister();
  }
});

function _setAuthUI(username, email) {
  const loginBtn  = document.getElementById('auth-login-btn');
  const userInfo  = document.getElementById('auth-user-info');
  const emailSpan = document.getElementById('auth-email');
  const syncDot   = document.getElementById('auth-sync-dot');
  if (username || email) {
    loginBtn.style.display  = 'none';
    userInfo.style.display  = 'flex';
    emailSpan.textContent   = username || email;
    emailSpan.title         = email || '';
    syncDot.style.display   = 'block';
  } else {
    loginBtn.style.display  = '';
    userInfo.style.display  = 'none';
    syncDot.style.display   = 'none';
  }
  // Saved searches chip — only meaningful when logged in
  const ssWrap = document.getElementById('ss-wrap');
  if (ssWrap) ssWrap.style.display = (username || email) ? '' : 'none';
  if (!(username || email)) _closeSavedSearchesDropdown();
  // Mobile bottom bar auth button
  const mIcon  = document.getElementById('mbb-auth-icon');
  const mLabel = document.getElementById('mbb-auth-label');
  if (mIcon && mLabel) {
    if (username || email) {
      mIcon.textContent  = '🚪';
      mLabel.textContent = 'Sign Out';
    } else {
      mIcon.textContent  = '👤';
      mLabel.textContent = 'Sign In';
    }
  }
}

function _mobileAuthToggle() {
  if (window._authUser) {
    _authLogout();
  } else {
    _openAuthModal('login');
  }
}

async function _loadAndMergeServerData(serverData) {
  // Merge server data with whatever's already in localStorage.
  // Strategy: server-wins for watchlist, keywords, saved_searches (so deletions
  // on one device propagate to all others). Fall back to local only if server
  // record is empty (first-ever sync for this account).
  // most-recent-wins for last_run / new_ids.
  const sWl  = serverData.watchlist      || {};
  const sKw  = serverData.keywords       || [];
  const sFav = serverData.favorites      || [];
  const sLr  = serverData.last_run       || '';
  const sNid = serverData.new_ids        || [];
  const sSS  = serverData.saved_searches || [];
  const sLa  = serverData.last_anchor    || '';

  // Watchlist: server is authoritative when logged in so that deletions on one
  // device propagate to all others. Only fall back to local if server record is
  // empty (first-ever sync for this account).
  const mergedWl  = Object.keys(sWl).length > 0 ? Object.assign({}, sWl) : Object.assign({}, window._watchlist);
  // Keywords: server is authoritative when logged in so that deletions on one
  // device propagate to all others. Only fall back to local if server record is
  // empty (first-ever sync for this account).
  const mergedKw  = sKw.length > 0 ? [...sKw].sort() : [...window._keywords].sort();
  const mergedFav = [...new Set([...sFav, ...favorites])];
  const localLr   = window._lastRunISO    || '';
  const localLa   = window._lastAnchorISO || '';
  let mergedLr, mergedNid;
  if (sLr && localLr) {
    if (sLr >= localLr) { mergedLr = sLr;  mergedNid = sNid; }
    else                { mergedLr = localLr; mergedNid = [...(window._newIds || [])]; }
  } else {
    mergedLr  = sLr || localLr;
    mergedNid = sLr ? sNid : [...(window._newIds || [])];
  }
  // Anchor: take the newer of server vs local (string ISO compare works for UTC Z).
  // Server-wins when set because it's authoritative across devices.
  const mergedLa = (sLa && localLa) ? (sLa >= localLa ? sLa : localLa) : (sLa || localLa);

  // Saved searches: server is authoritative (like keywords) so deletions propagate
  const mergedSS = sSS.length > 0 ? sSS : (window._savedSearches || []);

  window._watchlist      = mergedWl;
  window._keywords       = mergedKw;
  favorites              = mergedFav;
  window._lastRunISO     = mergedLr || null;
  window._lastAnchorISO  = mergedLa || null;
  window._newIds         = new Set(mergedNid);
  window._savedSearches  = mergedSS;

  _lsSet('watchlist', window._watchlist);
  _lsSet('keywords',  window._keywords);
  _lsSet('favorites', favorites);
  if (window._lastRunISO)    _lsSet('last_run',    window._lastRunISO);
  if (window._lastAnchorISO) _lsSet('last_anchor', window._lastAnchorISO);
  _lsSet('new_ids', [...window._newIds]);

  // Push merged state back to server
  await _syncToServer(true);
  _updateSavedSearchesUI();
}

async function _syncToServer(immediate) {
  if (!window._authUser) return;   // no-op when not logged in
  if (!immediate) {
    // Debounce rapid changes (e.g. adding many keywords)
    clearTimeout(_syncTimer);
    _syncTimer = setTimeout(() => _syncToServer(true), 600);
    return;
  }
  try {
    await fetch('/api/sync', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        watchlist:      window._watchlist     || {},
        keywords:       window._keywords      || [],
        favorites:      favorites              || [],
        last_run:       window._lastRunISO    || '',
        last_anchor:    window._lastAnchorISO || '',
        new_ids:        window._newIds instanceof Set ? [...window._newIds] : (window._newIds || []),
        saved_searches: window._savedSearches || [],
      }),
    });
  } catch(e) { /* sync failure is non-fatal — data is still in localStorage */ }
}

// ── Shared auth helper — called after any successful login or register ─────────
async function _onAuthSuccess(d, isNew) {
  window._authUser = {username: d.username, googleLinked: !!d.google_linked};
  _lsSet('guest_dismissed', false);  // clear guest flag now that they have an account
  _setAuthUI(d.username, '');
  _closeAuthModal();
  document.getElementById('first-run-modal').style.display = 'none';
  await _loadAndMergeServerData(d.data || {});
  _updateRelativeTime();
  _maybeShowLinkBanner(!!d.google_linked, !!d.has_email, window._googleOauthEnabled);
  // Auto-trigger baseline scan on first-ever login (no prior scan history)
  if (!window._lastRunISO) {
    appendLog('🎸 Welcome! Building the inventory database for the first time — this takes a few minutes…', 'log-dim');
    setTimeout(() => startRun({stores: [], baseline: true}, true), 400);
  } else if (_browseMode === 'server') {
    _fetchBrowsePage(1);
  }
}

async function _authLogin() {
  const username = document.getElementById('auth-login-user').value.trim();
  const pw       = document.getElementById('auth-login-pw').value;
  const errEl    = document.getElementById('auth-login-err');
  errEl.textContent = '';
  if (!username || !pw) { errEl.textContent = 'Please fill in both fields.'; return; }
  const btn = document.querySelector('#auth-form-login .auth-submit');
  btn.disabled = true; btn.textContent = 'Signing in…';
  try {
    const r = await fetch('/api/login', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({username, password: pw}),
    });
    const d = await r.json();
    if (!r.ok) { errEl.textContent = d.error || 'Login failed.'; return; }
    await _onAuthSuccess(d, false);
  } catch(e) {
    errEl.textContent = 'Network error — please try again.';
  } finally {
    btn.disabled = false; btn.textContent = 'Sign In';
  }
}

async function _authRegister() {
  const username = document.getElementById('auth-reg-username').value.trim();
  const pw       = document.getElementById('auth-reg-pw').value;
  const pw2      = document.getElementById('auth-reg-pw2').value;
  const errEl    = document.getElementById('auth-reg-err');
  errEl.textContent = '';
  if (!username || !pw) { errEl.textContent = 'Please fill in all fields.'; return; }
  if (username.length < 3) { errEl.textContent = 'Username must be at least 3 characters.'; return; }
  if (pw.length < 8)       { errEl.textContent = 'Password must be at least 8 characters.'; return; }
  if (pw !== pw2)          { errEl.textContent = 'Passwords do not match.'; return; }
  const btn = document.querySelector('#auth-form-register .auth-submit');
  btn.disabled = true; btn.textContent = 'Creating account…';
  try {
    const email = (document.getElementById('auth-reg-email').value || '').trim();
    const r = await fetch('/api/register', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({username, password: pw, email: email || ''}),
    });
    const d = await r.json();
    if (!r.ok) { errEl.textContent = d.error || 'Registration failed.'; return; }
    await _onAuthSuccess(d, true);
  } catch(e) {
    errEl.textContent = 'Network error — please try again.';
  } finally {
    btn.disabled = false; btn.textContent = 'Create Account';
  }
}

async function _authLogout() {
  await fetch('/api/logout', {method: 'POST'});
  window._authUser = null;
  // Clear all per-user state from memory and localStorage
  window._watchlist      = {};
  window._keywords       = [];
  window._newIds         = new Set();
  window._lastRunISO     = null;
  window._savedSearches  = [];
  favorites              = [];
  ['watchlist','keywords','new_ids','last_run','favorites'].forEach(k => _lsSet(k, k === 'watchlist' ? {} : []));
  _setAuthUI(null, null);
  // Re-render to reflect cleared state
  updateCount();
  renderKeywordList?.();
  if (typeof renderTable === 'function') renderTable();
}

// ── Welcome modal tab switching & form submission ─────────────────────────────
function _welcomeTab(tab) {
  const loginForm = document.getElementById('welcome-form-login');
  const regForm   = document.getElementById('welcome-form-register');
  const loginTab  = document.getElementById('welcome-tab-login');
  const regTab    = document.getElementById('welcome-tab-register');
  loginForm.style.display = tab === 'login' ? '' : 'none';
  regForm.style.display   = tab === 'register' ? '' : 'none';
  loginTab.style.color    = tab === 'login' ? '#ff5555' : '#666';
  loginTab.style.borderBottomColor = tab === 'login' ? '#c00' : 'transparent';
  regTab.style.color      = tab === 'register' ? '#ff5555' : '#666';
  regTab.style.borderBottomColor   = tab === 'register' ? '#c00' : 'transparent';
  document.getElementById('welcome-login-err').textContent = '';
  document.getElementById('welcome-reg-err').textContent   = '';
}

async function _welcomeLogin() {
  const username = document.getElementById('welcome-login-user').value.trim();
  const pw       = document.getElementById('welcome-login-pw').value;
  const errEl    = document.getElementById('welcome-login-err');
  errEl.textContent = '';
  if (!username || !pw) { errEl.textContent = 'Please fill in both fields.'; return; }
  const btn = document.querySelector('#welcome-form-login .auth-submit');
  btn.disabled = true; btn.textContent = 'Signing in…';
  try {
    const r = await fetch('/api/login', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({username, password: pw}),
    });
    const d = await r.json();
    if (!r.ok) { errEl.textContent = d.error || 'Login failed.'; return; }
    await _onAuthSuccess(d, false);
  } catch(e) {
    errEl.textContent = 'Network error — please try again.';
  } finally {
    btn.disabled = false; btn.textContent = 'Sign In';
  }
}

async function _welcomeRegister() {
  const username = document.getElementById('welcome-reg-user').value.trim();
  const pw       = document.getElementById('welcome-reg-pw').value;
  const pw2      = document.getElementById('welcome-reg-pw2').value;
  const errEl    = document.getElementById('welcome-reg-err');
  errEl.textContent = '';
  if (!username || !pw) { errEl.textContent = 'Please fill in all fields.'; return; }
  if (username.length < 3) { errEl.textContent = 'Username must be at least 3 characters.'; return; }
  if (pw.length < 8)       { errEl.textContent = 'Password must be at least 8 characters.'; return; }
  if (pw !== pw2)          { errEl.textContent = 'Passwords do not match.'; return; }
  const btn = document.querySelector('#welcome-form-register .auth-submit');
  btn.disabled = true; btn.textContent = 'Creating account…';
  try {
    const email = (document.getElementById('welcome-reg-email').value || '').trim();
    const r = await fetch('/api/register', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({username, password: pw, email: email || ''}),
    });
    const d = await r.json();
    if (!r.ok) { errEl.textContent = d.error || 'Registration failed.'; return; }
    await _onAuthSuccess(d, true);
  } catch(e) {
    errEl.textContent = 'Network error — please try again.';
  } finally {
    btn.disabled = false; btn.textContent = 'Create Account &amp; Start Scanning';
  }
}

// ── Init ─────────────────────────────────────────────────────────────────────
function toggleDesktopSidebar() {
  const left = document.getElementById('gc-left');
  const btn  = document.getElementById('sidebar-collapse-btn');
  if (!left || !btn) return;
  const collapsed = left.classList.toggle('sidebar-collapsed');
  btn.textContent = collapsed ? '»' : '«';
  btn.title = collapsed ? 'Expand store panel' : 'Collapse store panel';
  try { localStorage.setItem('gt_sidebar_collapsed', collapsed ? '1' : ''); } catch(e) {}
}

document.addEventListener('DOMContentLoaded', async () => {
  document.getElementById('search').addEventListener('input', filterList);
  // Desktop sidebar: restore collapsed state
  if (localStorage.getItem('gt_sidebar_collapsed')) {
    const left = document.getElementById('gc-left');
    const btn  = document.getElementById('sidebar-collapse-btn');
    if (left) { left.classList.add('sidebar-collapsed'); }
    if (btn)  { btn.textContent = '»'; btn.title = 'Expand store panel'; }
  }
  // Desktop thumbnail view: apply saved preference on load
  _applyDesktopThumbMode();
  // Mobile sort row
  document.getElementById('mobile-sort-row')?.addEventListener('click', e => {
    const btn = e.target.closest('.mobile-sort-btn');
    if (!btn) return;
    _srvSortField = btn.dataset.sortField;
    _srvSortDir   = btn.dataset.sortDir;
    window._sortCol = _srvSortField === 'price' ? 3 : 7;
    window._sortDir = _srvSortDir === 'asc' ? 1 : -1;
    _updateMobileSortBtns();
    _srvPage = 1;
    _fetchBrowsePage(1);
  });
  // Load personal data from localStorage
  favorites = _lsGet('favorites', []);
  window._watchlist = _lsGet('watchlist', {});
  window._clWatchlist = _lsGet('cl_watchlist', {});
  // Migrate any cl: prefixed items from shared watchlist to separate CL watchlist
  Object.keys(window._watchlist).forEach(k => {
    if (k.startsWith('cl:')) {
      if (!window._clWatchlist[k]) window._clWatchlist[k] = window._watchlist[k];
      delete window._watchlist[k];
    }
  });
  _lsSet('watchlist', window._watchlist);
  _lsSet('cl_watchlist', window._clWatchlist);
  window._keywords = _lsGet('keywords', []);
  window._newIds = new Set(_lsGet('new_ids', []));               // Items flagged NEW from last Check for New
  // Clean up legacy localStorage keys from fingerprint-based detection (no longer used)
  try { localStorage.removeItem('gt_prev_snapshot'); localStorage.removeItem('gt_prev_fp_set'); } catch(e) {}
  clRenderCities(true);  // Select all cities on initial load
  // Check if already logged in (session cookie persists across page loads)
  let alreadyLoggedIn = false;
  try {
    const meR = await fetch('/api/me');
    const meD = await meR.json();
    if (meD.logged_in) {
      alreadyLoggedIn = true;
      window._authUser = {username: meD.username, googleLinked: !!meD.google_linked};
      _setAuthUI(meD.username, '');
      await _loadAndMergeServerData(meD.data || {});
      _maybeShowLinkBanner(!!meD.google_linked, !!meD.has_email, window._googleOauthEnabled);
      if (meD.is_admin) {
        const al = document.getElementById('admin-footer-link');
        const as = document.getElementById('admin-footer-sep');
        if (al) al.style.display = '';
        if (as) as.style.display = '';
      }
    }
  } catch(e) { /* not logged in or network error — continue with localStorage */ }
  await loadData();
  await loadState(alreadyLoggedIn);
});

async function loadData() {
  const r = await fetch('/api/stores');
  const d = await r.json();
  allStores = d.stores;
  _selectedStores = new Set(d.stores);  // Select all stores on initial load
  renderList();
  const info = d.info || {};
  const storeLabel = info.count ? info.count : allStores.length;
  _baseStoreCount = parseInt(storeLabel) || allStores.length;
  document.getElementById('hdr-status').textContent = storeLabel + ' stores available';
  document.getElementById('s-stores').textContent = storeLabel;
  if (allStores.length === 0) {
    appendLog('💡 No stores loaded yet — a scan will populate them automatically.', 'log-dim');
  }
  // Load store coords and apply ZIP sort if a saved ZIP exists
  _loadStoreCoords();
}

window._lastRunISO = null;
window._lastAnchorISO = null;
let _relTimeTimer = null;

function _timeAgo(iso) {
  if (!iso) return 'never';
  const diff = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  if (diff < 60)   return 'just now';
  if (diff < 120)  return '1 minute ago';
  if (diff < 3600) return Math.floor(diff / 60) + ' minutes ago';
  if (diff < 7200) return '1 hour ago';
  if (diff < 86400) return Math.floor(diff / 3600) + ' hours ago';
  if (diff < 172800) return '1 day ago';
  return Math.floor(diff / 86400) + ' days ago';
}

function _fmtDropDate(iso) {
  if (!iso) return '';
  const diff = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  if (diff < 86400)   return 'today';
  if (diff < 172800)  return 'yesterday';
  if (diff < 604800)  return Math.floor(diff / 86400) + ' days ago';
  // Older than a week: show short date
  return new Date(iso).toLocaleDateString(undefined, {month:'short', day:'numeric'});
}

function _updateRelativeTime() {
  document.getElementById('s-last').textContent = _timeAgo(window._lastRunISO);
  const btn = document.getElementById('check-now-btn');
  if (btn) btn.textContent = 'Scan For New';
  clearInterval(_relTimeTimer);
  _relTimeTimer = setInterval(() => {
    document.getElementById('s-last').textContent = _timeAgo(window._lastRunISO);
  }, 30000); // Update every 30s
}

async function loadState(alreadyLoggedIn) {
  // Per-user timing from localStorage (may already be set from server merge)
  if (!window._lastRunISO) window._lastRunISO = _lsGet('last_run', null);
  // Per-user anchor (v2.10.18): max date_listed this user has been exposed to.
  // Used by the server's NEW detection threshold so other users' scan activity
  // can't push our threshold forward and silently hide genuinely-new items.
  if (!window._lastAnchorISO) window._lastAnchorISO = _lsGet('last_anchor', null);

  // Shared state from server
  const r = await fetch('/api/state');
  const s = await r.json();
  _baseItemCount = s.total_items || 0;
  document.getElementById('s-known').textContent = _baseItemCount.toLocaleString();

  _updateRelativeTime();
  document.getElementById('check-now-btn').style.display = 'inline';

  // Show welcome/auth modal if not logged in — only once, persisted in localStorage
  if (!alreadyLoggedIn && !window._authUser && !window._firstRunShown && !_lsGet('guest_dismissed', false)) {
    window._firstRunShown = true;
    document.getElementById('first-run-modal').style.display = 'flex';
  }
}

// ── Refresh store list ────────────────────────────────────────────────────────

// ── ZIP sort ──────────────────────────────────────────────────────────────────
window._storeCoords  = {};   // {storeName: {lat, lng}}
window._zipSortMode  = false;
window._userLat      = null;
window._userLng      = null;

function _haversine(lat1, lng1, lat2, lng2) {
  const R    = 3958.8;
  const dLat = (lat2 - lat1) * Math.PI / 180;
  const dLng = (lng2 - lng1) * Math.PI / 180;
  const a    = Math.sin(dLat/2)**2 +
               Math.cos(lat1*Math.PI/180) * Math.cos(lat2*Math.PI/180) * Math.sin(dLng/2)**2;
  return Math.round(R * 2 * Math.asin(Math.sqrt(a)));
}

function _storeDistance(name) {
  if (!window._userLat || !window._userLng) return Infinity;
  const c = window._storeCoords[name];
  if (!c) return Infinity;
  return _haversine(window._userLat, window._userLng, c.lat, c.lng);
}

function _setZipStatus(msg, active) {
  const inp = document.getElementById('zip-input');
  const btn = document.getElementById('zip-sort-btn');
  if (msg) {
    inp.placeholder = msg;
    inp.disabled = active;
    btn.disabled = active;
  } else {
    inp.placeholder = 'ZIP code…';
    inp.disabled = false;
    btn.disabled = false;
  }
}

async function _loadStoreCoords() {
  // Load server-side coords (shared for all users, built by admin)
  try {
    const r = await fetch('/api/store-coords');
    window._storeCoords = await r.json();
  } catch(e) {}
  // No auto-restore of ZIP or sort — user must type their ZIP each session
}

async function _geocodeZip(zip, silent=false) {
  if (!zip || zip.length < 5) return false;
  try {
    const r = await fetch(`https://api.zippopotam.us/us/${zip}`);
    if (!r.ok) { if (!silent) appendLog('❌ ZIP not found: ' + zip, 'log-err'); return false; }
    const d = await r.json();
    const place = d.places && d.places[0];
    if (!place) { if (!silent) appendLog('❌ No location for ZIP ' + zip, 'log-err'); return false; }
    window._userLat = parseFloat(place.latitude);
    window._userLng = parseFloat(place.longitude);
    // Don't persist ZIP — user types it fresh each session
    return true;
  } catch(e) {
    if (!silent) appendLog('❌ ZIP lookup failed — check connection.', 'log-err');
    return false;
  }
}

async function applyZipSort() {
  const zip = document.getElementById('zip-input').value.trim();
  if (!zip || zip.length < 5) return;
  const ok = await _geocodeZip(zip);
  if (!ok) return;
  window._zipSortMode = true;
  // (ZIP sort state is not persisted — cleared on page load)
  document.getElementById('zip-sort-btn').classList.add('active');
  document.getElementById('zip-sort-btn').textContent = '↕ A-Z Sort';
  renderList();
}

function toggleZipSort() {
  if (window._zipSortMode) {
    // Turn off — go back to A-Z
    window._zipSortMode = false;
    // (not persisted)
    document.getElementById('zip-sort-btn').classList.remove('active');
    document.getElementById('zip-sort-btn').textContent = '📍 ZIP Sort';
    renderList();
  } else {
    applyZipSort();
  }
}

// buildStoreCoords / validateStores are admin-only — use /admin/build-coords and /admin/validate-stores
function buildStoreCoords() {}
function validateStores() {}

// ── Mode switching ────────────────────────────────────────────────────────────
let favsOnly = false;
// In-memory selection set — persists across store filter text changes
let _selectedStores = new Set();
// Snapshot of all-stores selection taken just before entering favorites mode
let _preFavsSelection = null;

function _getCheckedStores() {
  return new Set(_selectedStores);
}

function toggleFavsFilter() {
  favsOnly = !favsOnly;
  const btn = document.getElementById('favs-btn');
  btn.classList.toggle('active', favsOnly);
  btn.textContent = favsOnly ? 'All Stores' : '★ Favorites';
  document.getElementById('search').value = '';
  if (favsOnly) {
    // Switching TO favorites: snapshot current all-stores selection, then
    // select ONLY favorites (not merged with current selection)
    _preFavsSelection = new Set(_selectedStores);
    _selectedStores = new Set(favorites);
    renderList();
  } else {
    // Switching back to All Stores: restore the pre-favorites selection exactly
    _selectedStores = _preFavsSelection || new Set(allStores);
    _preFavsSelection = null;
    renderList();
  }
}

function selectAll() {
  document.querySelectorAll('.store-row:not(.hidden) input[type=checkbox]').forEach(cb => {
    cb.checked = true;
    _selectedStores.add(cb.value);
  });
  updateCount();
}
function clearAll() {
  _selectedStores.clear();
  document.querySelectorAll('.store-row input[type=checkbox]').forEach(cb => cb.checked = false);
  updateCount();
}
function toggleSelectAll() {
  const visible = [...document.querySelectorAll('.store-row:not(.hidden) input[type=checkbox]')];
  const allChecked = visible.length > 0 && visible.every(cb => cb.checked);
  allChecked ? clearAll() : selectAll();
}

// ── Render store list ─────────────────────────────────────────────────────────
function renderList() {
  const el = document.getElementById('store-list');
  const q  = document.getElementById('search').value.toLowerCase();
  // In favorites mode with a search query, show ALL matching stores so users can find and add new favorites
  let stores = (favsOnly && !q) ? favorites : allStores;

  if (favsOnly && !q && !favorites.length) {
    el.innerHTML = '<div class="empty-msg">No favorites yet.<br>Click ★ next to any store to add it,<br>or type in the search box to find stores.</div>';
    updateCount(); return;
  }

  let filtered = q ? stores.filter(s => s.toLowerCase().includes(q)) : stores;

  // Sort: ZIP mode → nearest first; favorites mode with search → favs first; else A-Z (allStores already sorted)
  if (window._zipSortMode && window._userLat) {
    filtered = [...filtered].sort((a, b) => _storeDistance(a) - _storeDistance(b));
  } else if (favsOnly && q) {
    const favSet = new Set(favorites);
    filtered.sort((a, b) => (favSet.has(b) ? 1 : 0) - (favSet.has(a) ? 1 : 0));
  }

  el.innerHTML = '';
  el.scrollTop = 0;
  filtered.forEach(name => {
    const isFav = favorites.includes(name);
    const dist  = (window._zipSortMode && window._userLat) ? _storeDistance(name) : null;
    // Distance suffix embedded in label: "Austin (7 mi)" — only in ZIP sort mode
    const distSuffix = dist !== null && dist !== Infinity
      ? ` <span class="store-dist-inline">(${dist.toLocaleString()} mi)</span>`
      : (window._zipSortMode ? ' <span class="store-dist-inline">(?)</span>' : '');
    const div   = document.createElement('div');
    div.className = 'store-row';
    div.dataset.name = name;
    const id = 'cb_' + name.replace(/[^a-zA-Z0-9]/g,'_');
    const isChecked = _selectedStores.has(name);
    div.innerHTML =
      `<button class="fav-btn ${isFav?'active':''}" title="${isFav?'Remove from':'Add to'} favorites">★</button>` +
      `<input type="checkbox" id="${id}" value="${name}" ${isChecked ? 'checked' : ''}>` +
      `<label for="${id}">${name}${distSuffix}</label>`;
    div.querySelector('.fav-btn').addEventListener('click', e => { e.stopPropagation(); toggleFav(e, name, e.currentTarget); });
    div.querySelector('input').addEventListener('change', e => {
      if (e.target.checked) _selectedStores.add(name);
      else _selectedStores.delete(name);
      updateCount();
    });
    el.appendChild(div);
  });
  updateCount();
}

function filterList() {
  renderList();  // preserves current selections via _getCheckedStores
}

// ── Favorites ─────────────────────────────────────────────────────────────────
function toggleFav(e, name, btn) {
  e.stopPropagation();
  const adding = !favorites.includes(name);
  if (adding) {
    favorites.push(name);
  } else {
    favorites = favorites.filter(f => f !== name);
  }
  favorites.sort();
  _lsSet('favorites', favorites);
  _syncToServer();
  btn.classList.toggle('active', adding);
  btn.title = (adding ? 'Remove from' : 'Add to') + ' favorites';
  if (favsOnly) renderList();
}

// ── Selection ─────────────────────────────────────────────────────────────────
function updateCount() {
  const n = _selectedStores.size;
  document.getElementById('sel-count').textContent = n + ' store' + (n===1?'':'s') + ' selected';
  const visible = [...document.querySelectorAll('.store-row:not(.hidden) input[type=checkbox]')];
  const allChecked = visible.length > 0 && visible.every(cb => cb.checked);
  const selBtn = document.getElementById('sel-all-btn');
  if (selBtn) selBtn.textContent = allChecked ? 'Clear All' : 'Select All';
  _updateMobileToggleCounts();
  // Auto-browse cached inventory when stores are selected
  if (n > 0 && !running && !_globalSearchActive) browseCache();
  else if (n === 0 && !_globalSearchActive) {
    document.getElementById('res-panel').style.display = 'none';
  }
}

// ── Browse cached inventory (server-side pagination) ──────────────────────
let _browseTimer = null;
let _skipBrowse = false;  // Set after a scan to prevent browseCache from overwriting results
let _watchFilterActive = false;
let _priceDropFilterActive = false;
window._priceMin = null;   // null = no filter; number = active min price
window._priceMax = null;   // null = no filter; number = active max price
let _priceTimer = null;
let _globalSearchActive = false;
let _globalSearchQuery = '';
let _wantListSearchActive = false;

// ── Special-view state save / restore ─────────────────────────────────────────
// Holds a snapshot of filter+store state taken before entering Watch List,
// Want List, or Saved Searches view so we can restore on toggle-off / "← Back".
let _preSpecialViewState = null;

function _captureFilterState() {
  return {
    srvStores:      _srvStores.slice(),
    selectedStores: new Set(_selectedStores),
    brands:         (window._selectedBrands || []).slice(),
    conds:          (window._selectedConds  || []).slice(),
    cats:           (window._selectedCats   || []).slice(),
    subs:           (window._selectedSubs   || []).slice(),
    priceMin:       window._priceMin,
    priceMax:       window._priceMax,
    searchQ:        (document.getElementById('res-search') || {}).value || '',
    strictSearch:   window._strictSearch || false,
    watchActive:    _watchFilterActive,
    priceDropActive:_priceDropFilterActive,
    wantListActive: _wantListSearchActive,
    globalActive:   _globalSearchActive,
    globalQuery:    _globalSearchQuery,
    sortField:      _srvSortField,
    sortDir:        _srvSortDir,
  };
}

function _restoreFilterState() {
  const state = _preSpecialViewState;
  if (!state) return;
  _preSpecialViewState = null;
  // Restore stores
  _selectedStores = new Set(state.selectedStores);
  _srvStores = state.srvStores;
  renderList();
  updateCount && updateCount();
  // Restore filter chips
  _watchFilterActive = state.watchActive;
  const wtBtn = document.getElementById('watchlist-toggle');
  if (wtBtn) wtBtn.classList.toggle('wl-active', _watchFilterActive);
  _priceDropFilterActive = state.priceDropActive;
  const pdBtn = document.getElementById('price-drop-toggle');
  if (pdBtn) pdBtn.classList.toggle('wl-active', _priceDropFilterActive);
  // Restore want list state
  _wantListSearchActive = state.wantListActive;
  _globalSearchActive   = state.globalActive;
  _globalSearchQuery    = state.globalQuery;
  const wlBtn = document.getElementById('want-list-toggle');
  if (wlBtn) wlBtn.classList.toggle('wl-active', _wantListSearchActive);
  _updateWantListCount && _updateWantListCount();
  // Restore filter dropdowns
  window._selectedBrands = state.brands;
  window._selectedConds  = state.conds;
  window._selectedCats   = state.cats;
  window._selectedSubs   = state.subs;
  _updateBrandBtn(); _updateCondBtn(); _updateCatBtn(); _updateSubcatBtn();
  _accRenderBrand && _accRenderBrand();
  _accRenderCond  && _accRenderCond();
  _accRenderCat   && _accRenderCat();
  _accRenderSub   && _accRenderSub();
  _accUpdateSummaries && _accUpdateSummaries();
  // Restore price
  window._priceMin = state.priceMin;
  window._priceMax = state.priceMax;
  var pMinStr = window._priceMin !== null ? String(window._priceMin) : '';
  var pMaxStr = window._priceMax !== null ? String(window._priceMax) : '';
  ['price-min','price-min-dd'].forEach(function(id){var el=document.getElementById(id);if(el)el.value=pMinStr;});
  ['price-max','price-max-dd'].forEach(function(id){var el=document.getElementById(id);if(el)el.value=pMaxStr;});
  _updatePriceBtn && _updatePriceBtn();
  // Restore search box + strict toggle
  const rsEl = document.getElementById('res-search');
  if (rsEl) { rsEl.value = state.searchQ; }
  _updateResSearchClear && _updateResSearchClear();
  window._strictSearch = state.strictSearch;
  const strictBtn = document.getElementById('strict-search-btn');
  if (strictBtn) strictBtn.classList.toggle('active', window._strictSearch);
  _updateFilterDot && _updateFilterDot();
  _srvLoading = false;
  _srvPage = 1;
  _fetchBrowsePage(1);
}

// Mode: 'server' = browse with server-side pagination, 'local' = scan/watchlist with client data
let _browseMode = 'server';
// Server-side pagination state
let _srvStores = [];
let _srvPage = 1;
let _srvSortField = 'date';
let _srvSortDir = 'desc';
window._sortCol = null;   // null (not undefined) so user_sorted=false on fresh load
window._sortDir = 1;

function _updateMobileSortBtns() {
  document.querySelectorAll('.mobile-sort-btn').forEach(btn => {
    btn.classList.toggle('active',
      btn.dataset.sortField === _srvSortField && btn.dataset.sortDir === _srvSortDir);
  });
}

// ── Desktop thumbnail view ────────────────────────────────────────────────────
window._desktopThumbView = localStorage.getItem('gt_desktop_thumb_view') === 'true';

function _applyDesktopThumbMode() {
  const rb = document.getElementById('res-body');
  if (rb) rb.classList.toggle('thumb-mode', !!window._desktopThumbView);
  const btn = document.getElementById('desktop-thumb-toggle');
  if (btn) {
    // ⊞ shown when in list view (click to switch to grid)
    // ☰ shown when in grid/thumb view (click to switch back to list)
    btn.textContent = window._desktopThumbView ? '☰' : '⊞';
    btn.title = window._desktopThumbView ? 'Switch to list view' : 'Switch to thumbnail grid view';
    btn.classList.toggle('wl-active', !!window._desktopThumbView);
  }
}

function toggleDesktopThumbView() {
  window._desktopThumbView = !window._desktopThumbView;
  localStorage.setItem('gt_desktop_thumb_view', window._desktopThumbView);
  _applyDesktopThumbMode();
}
let _srvTotalCount = 0;
let _srvTotalUnfiltered = 0;
let _srvTotalPages = 1;
let _srvLoading = false;
let _baseItemCount = 0;   // full catalog count (set on load, reset target when filters clear)
let _baseStoreCount = 0;  // full store count (set on load)
window._strictSearch = false;  // Strict/whole-word search mode for the main search bar

function _getBrowseFilters() {
  return {
    filter_q:              document.getElementById('res-search').value.trim(),
    filter_brands:         window._selectedBrands || [],
    filter_conditions:     window._selectedConds || [],
    filter_categories:     window._selectedCats || [],
    filter_subcategories:  window._selectedSubs || [],
    filter_watched:         _watchFilterActive,
    filter_price_drop_only: _priceDropFilterActive,
    filter_strict:          window._strictSearch || false,
    filter_price_min:       window._priceMin,
    filter_price_max:       window._priceMax,
  };
}

async function _fetchBrowsePage(page) {
  if (_srvLoading) return;
  _srvLoading = true;
  const filters = _getBrowseFilters();
  // In global search mode, override filter_q with the global query and search all stores
  const body = {
    page:       page,
    per_page:   50,
    sort_field: _srvSortField,
    sort_dir:   _srvSortDir,
    user_sorted: window._sortCol !== null,
    fav_stores: favorites,
    keywords:   window._keywords || [],
    watchlist_ids: Object.keys(window._watchlist || {}),
    new_ids:    window._newIds instanceof Set ? [...window._newIds] : (window._newIds || []),
    user_last_scan: window._lastRunISO || '',
    ...filters,
  };
  if (_globalSearchActive || _watchFilterActive) {
    body.all_stores = true;
    if (_globalSearchActive) {
      body.filter_q = _globalSearchQuery;
    }
    if (_wantListSearchActive) {
      body.filter_want_list_only = true;
    }
  } else {
    body.stores = _srvStores;
  }
  try {
    const r = await fetch('/api/browse', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify(body)
    });
    const d = await r.json();
    if (d.no_store_data) {
      document.getElementById('res-panel').style.display = 'block';
      document.getElementById('res-title').textContent = 'No Browse Data Yet';
      document.getElementById('res-badge').textContent = '';
      document.getElementById('res-body').innerHTML =
        '<div class="no-res">Select stores on the left, then click <b>Scan For New</b> to scan for inventory.</div>';
      ['cond-dropdown','cat-dropdown','subcat-dropdown'].forEach(id => document.getElementById(id).style.display = 'none');
      return;
    }
    if (!d.items || (!d.items.length && page === 1)) {
      document.getElementById('res-panel').style.display = 'block';
      document.getElementById('res-title').textContent = 'No Items Found';
      document.getElementById('res-badge').textContent = '';
      document.getElementById('res-body').innerHTML = '<div class="no-res">No cached inventory for selected store(s). Run Check for New Items to scan.</div>';
      return;
    }

    _srvPage           = d.page;
    _srvTotalCount     = d.total_count;
    _srvTotalUnfiltered = d.total_unfiltered;
    _srvTotalPages     = d.total_pages;

    // Update header
    const hasFilters = filters.filter_q ||
      (filters.filter_brands && filters.filter_brands.length) ||
      (filters.filter_conditions && filters.filter_conditions.length) ||
      (filters.filter_categories && filters.filter_categories.length) ||
      (filters.filter_subcategories && filters.filter_subcategories.length) ||
      filters.filter_watched ||
      filters.filter_price_drop_only ||
      filters.filter_price_min != null ||
      filters.filter_price_max != null ||
      _wantListSearchActive;

    // Item count near filter buttons — only show when a filter is active
    const countEl2 = document.getElementById('filter-item-count');
    if (countEl2) countEl2.textContent = hasFilters ? _srvTotalCount.toLocaleString() + ' items' : '';
    const newCount = d.new_count || 0;
    if (_wantListSearchActive) {
      document.getElementById('res-title').textContent = _srvTotalCount > 0
        ? `${_srvTotalCount.toLocaleString()} Want List matches nationwide`
        : 'No Want List matches found';
    } else if (_priceDropFilterActive) {
      document.getElementById('res-title').textContent = _srvTotalCount > 0
        ? `↓ Price Drops — ${_srvTotalCount.toLocaleString()} item${_srvTotalCount !== 1 ? 's' : ''}`
        : 'No price drops found in selected stores';
    } else if (_watchFilterActive) {
      document.getElementById('res-title').textContent = _srvTotalCount > 0
        ? `Watch List — ${_srvTotalCount.toLocaleString()} item${_srvTotalCount !== 1 ? 's' : ''} nationwide`
        : 'Watch List — none of your watched items are currently available';
      document.getElementById('res-badge').textContent = '';
    } else if (_globalSearchActive) {
      const label = _srvTotalCount > 0
        ? `${_srvTotalCount.toLocaleString()} results for "${_globalSearchQuery}"`
        : `No results for "${_globalSearchQuery}"`;
      document.getElementById('res-title').textContent = hasFilters
        ? `${_srvTotalCount.toLocaleString()} of ${_srvTotalUnfiltered.toLocaleString()} results for "${_globalSearchQuery}"`
        : label;
    } else if (newCount > 0 && !hasFilters) {
      document.getElementById('res-title').textContent = `${_srvTotalUnfiltered.toLocaleString()} Items`;
      document.getElementById('res-badge').textContent = newCount + ' NEW';
    } else if (hasFilters) {
      document.getElementById('res-title').textContent = `${_srvTotalCount.toLocaleString()} of ${_srvTotalUnfiltered.toLocaleString()} Items`;
      document.getElementById('res-badge').textContent = '';
    } else {
      document.getElementById('res-title').textContent = _srvTotalCount > 0
        ? `${_srvTotalCount.toLocaleString()} Items` : 'No Items Found';
      document.getElementById('res-badge').textContent = '';
    }
    document.getElementById('res-panel').style.display = 'block';

    // Update filter count
    const countEl = document.getElementById('res-search-count');
    if (hasFilters) {
      countEl.textContent = `${_srvTotalCount.toLocaleString()} of ${_srvTotalUnfiltered.toLocaleString()}`;
    } else {
      countEl.textContent = '';
    }
    const clearBtn = document.getElementById('clear-filters-btn');
    if (clearBtn) clearBtn.style.display = (filters.filter_q || (filters.filter_brands && filters.filter_brands.length) || (filters.filter_conditions && filters.filter_conditions.length) || (filters.filter_categories && filters.filter_categories.length) || (filters.filter_subcategories && filters.filter_subcategories.length) || filters.filter_strict || filters.filter_price_drop_only || filters.filter_watched) ? '' : 'none';

    // Populate filter dropdowns from server-provided options
    _populateFiltersFromServer(d.brands || [], d.conditions || [], d.categories || [], d.subcategories || [], filters);

    // Cache items for mobile view toggle re-render
    window._lastBrowseItems = d.items;

    // Advance the per-user anchor to the newest item currently visible on page 1.
    // This ensures "Scan For New" only flags items genuinely newer than what you've
    // already seen at the top of the table — not stuff that was already there.
    // Only advance the anchor when viewing the fully unfiltered table — any active
    // filter (want list, watch list, price drop, brand, category, search, price
    // range) means we're seeing a subset, so its dates must not push the anchor
    // forward and silently hide genuinely-new items on the next scan.
    if (page === 1 && !hasFilters && !_globalSearchActive) {
      var topDate = d.items.reduce(function(best, item) {
        var dr = item.date_raw || '';
        return dr > best ? dr : best;
      }, window._lastAnchorISO || '');
      if (topDate && topDate !== window._lastAnchorISO) {
        window._lastAnchorISO = topDate;
        _lsSet('last_anchor', topDate);
        _syncToServer();
      }
    }

    // Render table + paginator
    _renderServerTable(d.items);

    // Scroll results to top on page change (use #res-body on mobile where .results is overflow:hidden)
    (document.getElementById('res-body') || document.querySelector('.results'))?.scrollTo(0, 0);

    // Update want list count badge in toolbar (only when not already filtering by want list)
    if (page === 1 && !_watchFilterActive && !_wantListSearchActive) {
      _updateWantListCount();
    }

    _updateSaveSearchBtn();

  } finally {
    _srvLoading = false;
  }
}

function _populateFiltersFromServer(brands, conditions, categories, subcategories, currentFilters) {
  _setBrandList(brands);
  // Use window._selected* (current user state) rather than currentFilters (stale snapshot
  // from the request that just returned). This prevents a race condition where a slow
  // response overwrites selections the user made while the request was in flight.
  window._selectedBrands = (window._selectedBrands || []).filter(b => brands.some(br => br.name === b));
  _updateBrandBtn();

  _setCondList(conditions);
  window._selectedConds = (window._selectedConds || []).filter(c => conditions.some(x => (x.name !== undefined ? x.name : x) === c));
  _updateCondBtn();

  _setCatList(categories);
  window._selectedCats = (window._selectedCats || []).filter(c => categories.some(x => (x.name !== undefined ? x.name : x) === c));
  _updateCatBtn();

  if (subcategories.length && window._selectedCats.length) {
    _setSubList(subcategories);
    window._selectedSubs = (window._selectedSubs || []).filter(s => subcategories.some(x => (x.name !== undefined ? x.name : x) === s));
  } else {
    _setSubList([]);
    window._selectedSubs = [];
  }
  _updateSubcatBtn();
}

function _buildRowHtml(item) {
  const priceNum = parseFloat((item.price||'').replace(/[^0-9.]/g,'')) || 0;
  const esc = s => (s||'').replace(/"/g,'&quot;').replace(/</g,'&lt;');
  const nameLink = item.url
    ? `<a href="${esc(_safeHttpUrl(item.url))}" target="_blank">${esc(item.name)}</a>`
    : esc(item.name);
  const thumbSrc = item.image_id
    ? `https://media.guitarcenter.com/is/image/MMGS7/${esc(item.image_id)}-00-200x200.jpg`
    : '';
  const thumbHtml = thumbSrc
    ? `<img class="row-thumb" src="${thumbSrc}" alt="" loading="lazy" onerror="this.style.display='none'">`
    : '<span class="row-thumb"></span>';
  // nameCell wraps thumb + link; soldBadge stays outside the flex div
  const nameCell = `<div class="thumb-name-cell">${thumbHtml}${nameLink}</div>`;
  const isSold = item.sold || false;
  const isWatched = (window._watchlist || {})[item.id || ''];
  const watchStar = item.id
    ? `<button class="watch-btn ${isWatched ? 'active' : ''}" data-action="toggleWatch" data-id="${(item.id||'').replace(/"/g,'&quot;')}" title="${isWatched ? 'Remove from' : 'Add to'} watch list">${isWatched ? '★' : '☆'}</button>`
    : '';
  const soldBadge = isSold ? ' <span class="tag-sold">Sold</span>' : '';
  const isNew = item.isNew || (item.id && window._newIds && window._newIds.has(item.id));
  const rowClass = [isSold ? 'sold-row' : '', item.isFav ? 'fav-row' : ''].filter(Boolean).join(' ');
  const brandCell = `<td>${item.brand ? esc(item.brand) : ''}</td>`;
  const hasDrop = item.price_drop > 0;
  const dropSinceLabel = hasDrop && item.price_drop_since
    ? ` · dropped ${_fmtDropDate(item.price_drop_since)}`
    : '';
  const priceCell = hasDrop
    ? `<td><span class="price-drop-val" title="Price drop! Down $${item.price_drop.toFixed(2)}${dropSinceLabel}">` +
      `↓ ${item.price||''}` +
      (item.list_price_raw > item.price_raw ? ` <span class="price-orig">$${item.list_price_raw.toFixed(2)}</span>` : '') +
      `</span></td>`
    : `<td>${item.price||''}</td>`;
  return `<tr class="${rowClass}" data-name="${esc(item.name)}" data-brand="${esc(item.brand)}" data-price="${priceNum}" data-store="${esc(item.store)}" data-location="${esc(item.location)}" data-condition="${esc(item.condition)}" data-category="${esc(item.category)}" data-subcategory="${esc(item.subcategory)}" data-image-id="${esc(item.image_id)}">` +
    `<td>${item.kwMatch ? '<span class="tag-kw">WANT</span>' : ''}</td>` +
    `<td>${watchStar}</td>` +
    `<td>${isNew ? '<span class="tag">NEW</span>' : ''}</td>` +
    `<td>${nameCell}${soldBadge}</td>` +
    (_isMobile() ? priceCell + brandCell : brandCell + priceCell) +
    `<td>${esc(item.condition)}</td>` +
    `<td>${esc(item.category)}</td>` +
    `<td>${esc(item.subcategory)}</td>` +
    `<td>${esc(item.date||'')}</td>` +
    `<td>${esc(item.store||item.location)}</td>` +
    `</tr>`;
}

// ── Paginator builder ────────────────────────────────────────────────────────
function _buildPaginatorHtml(currentPage, totalPages, totalCount, perPage) {
  if (totalPages <= 1) {
    // Single page — still render the paginator so its sticky #111 background seals the bottom
    const info = totalCount > 0 ? `<span class="pg-info">${totalCount.toLocaleString()} item${totalCount !== 1 ? 's' : ''}</span>` : '';
    return `<div class="paginator">${info}<button class="pg-active" disabled>1</button></div>`;
  }
  const startItem = (currentPage - 1) * perPage + 1;
  const endItem   = Math.min(currentPage * perPage, totalCount);

  let html = '<div class="paginator">';
  html += `<span class="pg-info">${startItem.toLocaleString()}–${endItem.toLocaleString()} of ${totalCount.toLocaleString()}</span>`;

  // First / Prev
  html += `<button class="pg-nav" data-action="goToPage" data-page="1" ${currentPage === 1 ? 'disabled' : ''} title="First page">&#x276E;&#x276E;</button>`;
  html += `<button class="pg-nav" data-action="goToPage" data-page="${currentPage - 1}" ${currentPage === 1 ? 'disabled' : ''} title="Previous page">&#x276E;</button>`;

  // Page numbers with smart ellipsis
  const pages = _getPaginatorRange(currentPage, totalPages);
  pages.forEach(p => {
    if (p === '...') {
      html += '<span class="pg-ellipsis">…</span>';
    } else {
      html += `<button class="${p === currentPage ? 'pg-active' : ''}" data-action="goToPage" data-page="${p}">${p}</button>`;
    }
  });

  // Next / Last
  html += `<button class="pg-nav" data-action="goToPage" data-page="${currentPage + 1}" ${currentPage === totalPages ? 'disabled' : ''} title="Next page">&#x276F;</button>`;
  html += `<button class="pg-nav" data-action="goToPage" data-page="${totalPages}" ${currentPage === totalPages ? 'disabled' : ''} title="Last page">&#x276F;&#x276F;</button>`;

  html += '</div>';
  return html;
}

function _getPaginatorRange(current, total) {
  // Always show first 2, last 2, and 2 around current. Fill gaps with ellipsis.
  if (total <= 9) return Array.from({length: total}, (_, i) => i + 1);

  const pages = new Set();
  // First two
  pages.add(1); pages.add(2);
  // Last two
  pages.add(total - 1); pages.add(total);
  // Around current
  for (let i = current - 2; i <= current + 2; i++) {
    if (i >= 1 && i <= total) pages.add(i);
  }

  const sorted = [...pages].sort((a, b) => a - b);
  const result = [];
  for (let i = 0; i < sorted.length; i++) {
    if (i > 0 && sorted[i] - sorted[i - 1] > 1) {
      result.push('...');
    }
    result.push(sorted[i]);
  }
  return result;
}

function _renderServerTable(items) {
  const mob = _isMobile();

  // On mobile, dispatch to card or compact-list renderer
  if (mob) {
    _updateViewToggleBtn();
    const view = localStorage.getItem('gt_mobile_view') || 'cards';
    if (view === 'list') {
      _renderMobileList(items);
    } else {
      _renderMobileCards(items);
    }
    return;
  }

  const hasNew  = items.some(i => i.isNew);
  const hasWant = items.some(i => i.kwMatch);
  const tblCls  = [!hasNew ? 'no-new' : '', !hasWant ? 'no-want' : ''].filter(Boolean).join(' ');
  let html = `<table id="res-table"${tblCls ? ` class="${tblCls}"` : ''}><thead><tr>
    <th data-col="kw"></th>
    <th data-col="watch"></th>
    <th data-col="0"></th>
    <th data-col="1">Item</th>
    <th data-col="2">Brand</th>
    <th data-col="3">Price</th>
    <th data-col="4">Condition</th>
    <th data-col="5">Category</th>
    <th data-col="6">Subcategory</th>
    <th data-col="7">Date Listed</th>
    <th data-col="8">Location</th>
  </tr></thead><tbody>`;
  items.forEach(item => { html += _buildRowHtml(item); });
  html += '</tbody></table>';
  html += _buildPaginatorHtml(_srvPage, _srvTotalPages, _srvTotalCount, 50);
  document.getElementById('res-body').innerHTML = html;
  _applyFrozenHeaderOffset();  // update sticky-top offset to match current filter-bar height
  // Scroll to top after content renders — desktop scrolls .results, mobile scrolls #res-body
  document.getElementById('res-panel')?.scrollTo(0, 0);
  document.getElementById('res-body')?.scrollTo(0, 0);

  // Attach sort headers
  if (window._sortCol !== null) {
    const th = document.querySelector(`#res-table th[data-col="${window._sortCol}"]`);
    if (th) th.classList.add(window._sortDir === 1 ? 'sort-asc' : 'sort-desc');
  }
  document.querySelectorAll('#res-table thead th[data-col]').forEach(th => {
    const colIdx = parseInt(th.dataset.col);
    if (!_SORT_COLS[colIdx]) return;
    th.addEventListener('click', () => sortTable(colIdx));
  });
}

// ── Mobile view toggle ────────────────────────────────────────────────────────
function _updateViewToggleBtn() {
  const view = localStorage.getItem('gt_mobile_view') || 'cards';
  const btn  = document.getElementById('view-toggle-btn');
  const icon = document.getElementById('view-toggle-icon');
  if (btn && icon) {
    if (view === 'list') {
      icon.textContent = '⊞';
      btn.title = 'Switch to card view';
      btn.classList.add('active');
    } else {
      icon.textContent = '☰';
      btn.title = 'Switch to compact list view';
      btn.classList.remove('active');
    }
  }
  // Also sync the chip bar button
  const chip = document.getElementById('view-toggle-chip');
  if (chip) {
    chip.textContent = view === 'list' ? '⊞' : '☰';
    chip.title = view === 'list' ? 'Switch to card view' : 'Switch to compact list view';
    chip.classList.toggle('wl-active', view === 'list');
  }
}

function toggleMobileView() {
  const cur  = localStorage.getItem('gt_mobile_view') || 'cards';
  const next = cur === 'cards' ? 'list' : 'cards';
  localStorage.setItem('gt_mobile_view', next);
  // Re-render with same items already fetched
  if (window._lastBrowseItems) {
    _updateViewToggleBtn();
    if (next === 'list') {
      _renderMobileList(window._lastBrowseItems);
    } else {
      _renderMobileCards(window._lastBrowseItems);
    }
  }
}

// ── Mobile card renderer ──────────────────────────────────────────────────────
function _renderMobileCards(items) {
  const esc = s => (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  let html = '<div class="card-grid">';
  items.forEach(item => {
    const isNew    = item.isNew  ? ' is-new'  : '';
    const isWant   = item.kwMatch ? ' is-want' : '';
    const isSold   = item.sold   ? ' is-sold'  : '';
    const watched  = window._watchlist && window._watchlist[item.id];
    const watchCls = watched ? ' wl-on' : '';

    const imgId = item.image_id || '';
    const imgUrl = imgId
      ? `https://media.guitarcenter.com/is/image/MMGS7/${imgId}-00-200x200.jpg`
      : '';

    const newBadge  = item.isNew   ? '<span class="tag">NEW</span>'   : '';
    const wantBadge = item.kwMatch  ? '<span class="tag-kw">WANT</span>' : '';
    const soldBadge = item.sold     ? '<span class="tag-sold">SOLD</span>' : '';

    const store   = esc(item.store_name || item.store || '');
    const loc     = esc(item.location   || '');
    const cond    = esc(item.condition  || '');
    const name    = esc(item.name       || '');
    const url     = item.url || '#';

    const hasDrop = item.price_drop > 0;
    const dropLabel = hasDrop && item.price_drop_since ? ` · dropped ${_fmtDropDate(item.price_drop_since)}` : '';
    const priceHtml = hasDrop
      ? `<span class="price-drop-val" title="Price drop! Down $${item.price_drop.toFixed(2)}${dropLabel}">` +
        (item.list_price_raw > item.price_raw ? `<span class="price-orig">$${item.list_price_raw.toFixed(2)}</span> ` : '') +
        `↓ ${item.price || '—'}</span>`
      : (item.price || '—');

    html += `<div class="item-card${isNew}${isWant}${isSold}">`;

    // Thumbnail
    html += '<div class="card-thumb-wrap">';
    if (imgUrl) {
      html += `<a href="${url}" target="_blank" rel="noopener"><img class="card-thumb" src="${imgUrl}" alt="" loading="lazy" onerror="this.style.display='none'"></a>`;
    } else {
      html += '<div class="card-thumb" style="background:#1a1a1a;display:flex;align-items:center;justify-content:center;color:#444;font-size:.7rem">No img</div>';
    }
    html += '</div>';

    // Body
    html += '<div class="card-body">';
    html += `<div class="card-badges">${newBadge}${wantBadge}${soldBadge}</div>`;
    html += `<div class="card-name"><a href="${url}" target="_blank" rel="noopener">${name}</a></div>`;
    html += `<div class="card-price">${priceHtml}</div>`;
    html += `<div class="card-meta">${cond ? cond + ' · ' : ''}${store}${item.date ? ' · ' + esc(item.date) : ''}</div>`;
    html += `<div class="card-actions">`;
    html += `<button class="card-watch-btn${watchCls}" data-action="toggleWatch" data-id="${item.id}">${watched ? '★' : '☆'}</button>`;
    html += `</div>`;
    html += '</div>'; // card-body

    html += '</div>'; // item-card
  });
  html += '</div>';
  html += _buildPaginatorHtml(_srvPage, _srvTotalPages, _srvTotalCount, 50);
  document.getElementById('res-body').innerHTML = html;
}

// ── Mobile compact list renderer ─────────────────────────────────────────────
function _renderMobileList(items) {
  const esc = s => (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  let html = '<div class="compact-list">';
  items.forEach(item => {
    const isNew    = item.isNew   ? ' is-new'  : '';
    const isWant   = item.kwMatch  ? ' is-want' : '';
    const watched  = window._watchlist && window._watchlist[item.id];
    const watchCls = watched ? ' wl-on' : '';
    const newBadge = item.isNew ? '<span class="tag">NEW</span>' : '';
    const url      = item.url || '#';
    const name     = esc(item.name || '');

    const hasDrop2 = item.price_drop > 0;
    const dropLabel2 = hasDrop2 && item.price_drop_since ? ` · dropped ${_fmtDropDate(item.price_drop_since)}` : '';
    const priceHtml2 = hasDrop2
      ? `<span class="price-drop-val" title="Price drop! Down $${item.price_drop.toFixed(2)}${dropLabel2}">` +
        (item.list_price_raw > item.price_raw ? `<span class="price-orig">$${item.list_price_raw.toFixed(2)}</span> ` : '') +
        `↓ ${item.price || '—'}</span>`
      : (item.price || '—');

    const cond2  = esc(item.condition  || '');
    const store2 = esc(item.store_name || item.store || '');
    const subParts = [cond2, store2, esc(item.date || '')].filter(Boolean);
    const subLine  = subParts.join(' · ');

    html += `<div class="compact-row${isNew}${isWant}">`;
    html += `<div class="compact-row-left">`;
    html += `<span class="compact-row-name">${newBadge}<a href="${url}" target="_blank" rel="noopener">${name}</a></span>`;
    if (subLine) html += `<span class="compact-row-sub">${subLine}</span>`;
    html += `</div>`;
    html += `<span class="compact-row-price">${priceHtml2}</span>`;
    html += `<button class="compact-row-watch${watchCls}" data-action="toggleWatch" data-id="${item.id}">${watched ? '★' : '☆'}</button>`;
    html += `</div>`;
  });
  html += '</div>';
  html += _buildPaginatorHtml(_srvPage, _srvTotalPages, _srvTotalCount, 50);
  document.getElementById('res-body').innerHTML = html;
}

async function browseCache() {
  if (_skipBrowse) { _skipBrowse = false; return; }
  clearTimeout(_browseTimer);
  _browseTimer = setTimeout(async () => {
    const stores = getSelected();
    if (!stores.length) return;
    _browseMode = 'server';
    _globalSearchActive = false;
    _globalSearchQuery = '';
    _resetWantListLink();
    _srvStores = stores;
    _srvPage = 1;
    // Preserve current sort and all active filters (brand/condition/category/subcategory/
    // search text/watch/price-drop/want-list) — contextual facet counts will automatically
    // update to reflect what's available in the new store set, and zero-count options
    // will be hidden. Don't reset _srvSortField/_srvSortDir/window._sortCol either.
    _srvLoading = false;  // Cancel any in-flight request so store changes always land
    await _fetchBrowsePage(1);
  }, 300);
}

// ── Watch list ────────────────────────────────────────────────────────────
window._watchlist = {};
window._clWatchlist = {};

// loadWatchlist no longer needed — loaded from localStorage in init

function toggleWatch(id, btn) {
  const isWatched = !!(window._watchlist[id]);
  if (isWatched) {
    delete window._watchlist[id];
  } else {
    // Try table row first, fall back to cached browse items (mobile card/list view)
    const row = btn.closest('tr');
    let name = '', store = '', location = '';
    if (row) {
      name     = row.dataset.name     || '';
      store    = row.dataset.store    || '';
      location = row.dataset.location || '';
    } else {
      const item = (window._lastBrowseItems || []).find(i => i.id === id);
      if (item) {
        name     = item.name          || '';
        store    = item.store_name    || item.store || '';
        location = item.location      || '';
      }
    }
    window._watchlist[id] = { name, store, location, date_added: new Date().toISOString().slice(0,10) };
  }
  _lsSet('watchlist', window._watchlist);
  btn.classList.toggle('active', !isWatched);
  btn.classList.toggle('wl-on',  !isWatched);
  btn.textContent = isWatched ? '☆' : '★';
  btn.title = isWatched ? 'Add to watch list' : 'Remove from watch list';
  // If currently in Watch List view and user just unwatched, remove the row immediately
  if (isWatched && _watchFilterActive) {
    const row = btn.closest('tr') || btn.closest('.item-card') || btn.closest('.compact-row');
    if (row) row.remove();
  }
  _syncToServer();
}

function toggleWatchFilter() {
  const btn = document.getElementById('watchlist-toggle');
  if (!_watchFilterActive) {
    // Activating — save current state, then clear all other filters for a clean view
    _preSpecialViewState = _captureFilterState();
    // Clear all filter state
    window._selectedBrands = []; _updateBrandBtn();
    window._selectedConds  = []; _updateCondBtn();
    window._selectedCats   = []; _updateCatBtn();
    window._selectedSubs   = []; _updateSubcatBtn(); _setSubList([]);
    window._priceMin = null; window._priceMax = null;
    ['price-min','price-max','price-min-dd','price-max-dd'].forEach(function(id){var el=document.getElementById(id);if(el)el.value='';});
    _updatePriceBtn && _updatePriceBtn();
    const rsEl = document.getElementById('res-search'); if (rsEl) rsEl.value = '';
    _updateResSearchClear && _updateResSearchClear();
    // Deactivate Want List / Price Drop if active
    if (_wantListSearchActive) {
      _wantListSearchActive = false;
      _globalSearchActive = false;
      _globalSearchQuery = '';
      _resetWantListLink();
    }
    if (_priceDropFilterActive) {
      _priceDropFilterActive = false;
      const pdBtn = document.getElementById('price-drop-toggle');
      if (pdBtn) pdBtn.classList.remove('wl-active');
    }
    _watchFilterActive = true;
    btn.classList.add('wl-active');
  } else {
    // Deactivating — restore pre-watch state
    _watchFilterActive = false;
    btn.classList.remove('wl-active');
    if (_preSpecialViewState) { _restoreFilterState(); return; }
  }
  _updateFilterDot();
  _browseMode = 'server';
  _srvPage = 1;
  _srvLoading = false;
  _fetchBrowsePage(1);
}

// Legacy showWatchList — now just activates the toggle
async function showWatchList() {
  if (!_watchFilterActive) toggleWatchFilter();
}

function togglePriceDropFilter() {
  _priceDropFilterActive = !_priceDropFilterActive;
  const btn = document.getElementById('price-drop-toggle');
  btn.classList.toggle('wl-active', _priceDropFilterActive);
  _updateFilterDot();
  _srvPage = 1;
  _srvLoading = false;  // cancel any in-flight request so the toggle always lands
  _fetchBrowsePage(1);
}



function getSelected() {
  return [..._selectedStores];
}


function dismissFirstRun() {
  document.getElementById('first-run-modal').style.display = 'none';
  window._firstRunShown = true;
  _lsSet('guest_dismissed', true);  // persist across reloads for guests
}

function _openAboutModal() {
  document.getElementById('about-modal').classList.add('open');
}
function _closeAboutModal() {
  document.getElementById('about-modal').classList.remove('open');
}

// ── Saved Searches ────────────────────────────────────────────────────────────
window._savedSearches = [];

function _updateSaveSearchBtn() {
  const f = _getBrowseFilters();
  const hasAny = f.filter_q ||
    (f.filter_brands        && f.filter_brands.length)        ||
    (f.filter_conditions    && f.filter_conditions.length)    ||
    (f.filter_categories    && f.filter_categories.length)    ||
    (f.filter_subcategories && f.filter_subcategories.length) ||
    f.filter_price_drop_only ||
    f.filter_price_min !== null ||
    f.filter_price_max !== null;
  const showSave  = !!(window._authUser && hasAny);
  const showClear = !!hasAny;
  const btn   = document.getElementById('save-search-btn');
  const clrBtn = document.getElementById('clear-filters-btn');
  const wrap  = document.getElementById('filter-action-btns');
  if (btn)    btn.style.display    = showSave  ? '' : 'none';
  if (clrBtn) clrBtn.style.display = showClear ? '' : 'none';
  // On mobile the wrapper needs display:flex; on desktop CSS keeps it as display:contents
  if (wrap) wrap.style.display = (showSave || showClear) ? (_isMobile() ? 'flex' : '') : 'none';
}

function _updateSavedSearchesUI() {
  const wrap = document.getElementById('ss-wrap');
  if (!wrap) return;
  wrap.style.display = window._authUser ? '' : 'none';
}

function _ssEsc(s) {
  return String(s == null ? '' : s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
// Only allow http(s) hrefs — blocks javascript:/data: URIs in scraped/listed URLs.
function _safeHttpUrl(u) {
  u = String(u == null ? '' : u);
  return /^https?:\/\//i.test(u) ? u : '';
}

function _ssDescription(ss) {
  const parts = [];
  const f = ss.filters || {};
  if (f.filter_q) parts.push('"' + f.filter_q + '"');
  const brands = f.filter_brands || [];
  if (brands.length) parts.push(brands.slice(0,2).join(', ') + (brands.length > 2 ? ' +' + (brands.length-2) : ''));
  const conds = f.filter_conditions || [];
  if (conds.length) parts.push(conds.join(', '));
  const cats = f.filter_categories || [];
  if (cats.length) parts.push(cats[0]);
  const sc = (ss.stores || []).length;
  if (sc) parts.push(sc + ' store' + (sc !== 1 ? 's' : ''));
  return parts.join(' · ') || 'All items';
}

function _renderSavedSearchesDropdown() {
  const dd = document.getElementById('ss-dropdown');
  if (!dd) return;
  const searches = window._savedSearches || [];
  // "← Back" button — only shown when there's a saved pre-state to restore
  const backBtn = _preSpecialViewState
    ? '<button class="ss-back-btn" data-ss-restore="1">← Back</button>'
    : '';
  if (!searches.length) {
    dd.innerHTML =
      '<div class="ss-dropdown-hdr"><span>Saved Searches</span>' + backBtn + '</div>' +
      '<div class="ss-empty">No saved searches yet.<br>Set filters then click <b>💾 Save Search</b>.</div>';
    return;
  }
  let html = '<div class="ss-dropdown-hdr"><span>Saved Searches</span>' + backBtn + '<button class="ss-clear-all-btn" data-ss-clear="1">Clear</button></div><div class="ss-list">';
  searches.forEach(function(ss) {
    html +=
      '<div class="ss-item" data-ss-id="' + _ssEsc(ss.id) + '">' +
        '<div class="ss-item-main">' +
          '<div class="ss-item-name">' + _ssEsc(ss.name) + '</div>' +
          '<div class="ss-item-desc">' + _ssEsc(_ssDescription(ss)) + '</div>' +
        '</div>' +
        '<span class="ss-count-badge" id="ss-cnt-' + _ssEsc(ss.id) + '">…</span>' +
        '<button class="ss-delete-btn" data-ss-del="' + _ssEsc(ss.id) + '" title="Delete this search">&#10005;</button>' +
      '</div>';
  });
  html += '</div>';
  dd.innerHTML = html;
}

function _toggleSavedSearchesDropdown() {
  const dd = document.getElementById('ss-dropdown');
  if (!dd) return;
  if (dd.style.display === 'block') { _closeSavedSearchesDropdown(); return; }
  const btn = document.getElementById('saved-searches-btn');
  if (!btn) return;
  const rect = btn.getBoundingClientRect();
  dd.style.display = 'block';
  dd.style.top  = (rect.bottom + 4) + 'px';
  dd.style.left = rect.left + 'px';
  requestAnimationFrame(function() {
    const ddRect = dd.getBoundingClientRect();
    if (ddRect.right > window.innerWidth - 8) {
      dd.style.left = Math.max(8, window.innerWidth - ddRect.width - 8) + 'px';
    }
  });
  _renderSavedSearchesDropdown();
  _fetchSavedSearchCounts();
}

function _closeSavedSearchesDropdown() {
  const dd = document.getElementById('ss-dropdown');
  if (dd) dd.style.display = 'none';
}

async function _fetchSavedSearchCounts() {
  const searches = window._savedSearches || [];
  if (!searches.length) return;
  try {
    const r = await fetch('/api/saved-search-counts', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({searches: searches.map(function(ss) {
        return {filters: ss.filters, stores: ss.stores};
      })})
    });
    const d = await r.json();
    if (!d.counts) return;
    d.counts.forEach(function(n, i) {
      const ss  = searches[i];
      const el  = document.getElementById('ss-cnt-' + ss.id);
      if (!el) return;
      el.textContent = n.toLocaleString();
      el.classList.toggle('loaded', true);
    });
  } catch(e) { /* non-fatal */ }
}

function _applySavedSearch(id) {
  const ss = (window._savedSearches || []).find(function(s) { return s.id === id; });
  if (!ss) return;
  // Save current state before applying so the "← Back" button can restore it
  _preSpecialViewState = _captureFilterState();
  _closeSavedSearchesDropdown();
  const f = ss.filters || {};
  // Restore filter state
  window._selectedBrands = f.filter_brands      || [];
  window._selectedConds  = f.filter_conditions  || [];
  window._selectedCats   = f.filter_categories  || [];
  window._selectedSubs   = f.filter_subcategories || [];
  window._strictSearch   = !!f.filter_strict;
  const rsEl = document.getElementById('res-search');
  if (rsEl) rsEl.value = f.filter_q || '';
  _updateResSearchClear && _updateResSearchClear();
  // Update filter button labels
  _updateBrandBtn(); _updateCondBtn(); _updateCatBtn(); _updateSubcatBtn();
  // Update accordion summaries
  _accRenderBrand && _accRenderBrand();
  _accRenderCond  && _accRenderCond();
  _accRenderCat   && _accRenderCat();
  _accRenderSub   && _accRenderSub();
  _accUpdateSummaries && _accUpdateSummaries();
  // Update strict button
  const strictBtn = document.getElementById('strict-search-btn');
  if (strictBtn) {
    strictBtn.textContent = '≈';
    strictBtn.classList.toggle('active', window._strictSearch);
    strictBtn.title = window._strictSearch
      ? '≈ Fuzzy (contains) mode on — click to restore whole-word default'
      : 'Whole-word search (default) — click for ≈ fuzzy (contains) mode';
  }
  // Restore watch / price-drop chip state
  _watchFilterActive = !!f.filter_watched;
  const wtBtn = document.getElementById('watchlist-toggle');
  if (wtBtn) wtBtn.classList.toggle('wl-active', _watchFilterActive);
  _priceDropFilterActive = !!f.filter_price_drop_only;
  const pdBtn = document.getElementById('price-drop-toggle');
  if (pdBtn) pdBtn.classList.toggle('wl-active', _priceDropFilterActive);
  // Restore price range
  window._priceMin = (f.filter_price_min !== undefined && f.filter_price_min !== null) ? f.filter_price_min : null;
  window._priceMax = (f.filter_price_max !== undefined && f.filter_price_max !== null) ? f.filter_price_max : null;
  var _pMinStr = window._priceMin !== null ? String(window._priceMin) : '';
  var _pMaxStr = window._priceMax !== null ? String(window._priceMax) : '';
  ['price-min', 'price-min-dd'].forEach(function(id) { var el = document.getElementById(id); if (el) el.value = _pMinStr; });
  ['price-max', 'price-max-dd'].forEach(function(id) { var el = document.getElementById(id); if (el) el.value = _pMaxStr; });
  _updatePriceBtn();
  // Restore store selection
  const savedStores = new Set(ss.stores || []);
  _selectedStores = savedStores;
  renderList();
  _srvStores = ss.stores || [];
  updateCount && updateCount();
  _updateFilterDot();
  _srvLoading = false;
  _srvPage = 1;
  _fetchBrowsePage(1);
}

function _saveCurrentSearch() {
  if (!window._authUser) return;
  const name = prompt('Name this search:');
  if (name === null) return;
  const trimmed = name.trim();
  if (!trimmed) return;
  const id = 'ss_' + Date.now();
  window._savedSearches = window._savedSearches || [];
  window._savedSearches.push({
    id:         id,
    name:       trimmed,
    filters:    _getBrowseFilters(),
    stores:     [...(_srvStores || [])],
    created_at: new Date().toISOString(),
  });
  _syncToServer();
  _updateSavedSearchesUI();
}

function _deleteSavedSearch(id) {
  const ss = (window._savedSearches || []).find(function(s) { return s.id === id; });
  const name = ss ? ss.name : 'this search';
  if (!confirm('Delete "' + name + '"? This cannot be undone.')) return;
  window._savedSearches = (window._savedSearches || []).filter(function(s) { return s.id !== id; });
  _syncToServer();
  _updateSavedSearchesUI();
  _renderSavedSearchesDropdown();
}

function _clearAllSavedSearches() {
  const n = (window._savedSearches || []).length;
  if (!n) return;
  if (!confirm('Clear all ' + n + ' saved search' + (n !== 1 ? 'es' : '') + '? This cannot be undone.')) return;
  window._savedSearches = [];
  _syncToServer();
  _updateSavedSearchesUI();
  _renderSavedSearchesDropdown();
}

// ── Want List ─────────────────────────────────────────────────────────────────
window._keywords = [];

// loadKeywords no longer needed — loaded from localStorage in init

function openKeywords() {
  _closeAllSheets();
  document.getElementById('kw-modal').style.display = 'flex';
  document.getElementById('kw-input').value = '';
  renderKeywordList();
  setTimeout(() => document.getElementById('kw-input').focus(), 50);
}

function closeKeywords() {
  document.getElementById('kw-modal').style.display = 'none';
  // Refresh whichever tab is active
  const clActive = document.querySelector('.cl-tab.active');
  if (clActive && _clData.length) {
    clRenderResults();
    if (_clWantListFilterActive) clFilterResults();
  } else if (_browseMode === 'server') {
    _fetchBrowsePage(_srvPage);
  } else {
    renderTable();
  }
}

function renderKeywordList() {
  const el = document.getElementById('kw-list');
  if (!window._keywords.length) {
    el.innerHTML = '<div style="color:#555;font-size:.82rem;padding:8px 0">Your want list is empty. Add an item above.</div>';
    return;
  }
  // Sort alphabetically (case-insensitive), preserving original indices for safe removal
  const sorted = window._keywords
    .map((kw, i) => ({kw, i}))
    .sort((a, b) => a.kw.toLowerCase().localeCompare(b.kw.toLowerCase()));
  el.innerHTML = `<div style="display:flex;flex-wrap:wrap;gap:7px;padding:10px 0">` +
    sorted.map(({kw, i}) => {
      const isStrict = kw.startsWith('=');
      const display = isStrict ? kw.slice(1) : kw;
      const safe = display.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
      const chipBg = isStrict ? '#0a3c6e' : '#0a5c2a';
      const chipColor = isStrict ? '#93c5fd' : '#4ade80';
      const chipBorder = isStrict ? '#1e40af' : '#2d6a2d';
      const badge = isStrict ? `<span style="font-size:.65rem;font-weight:700;color:#fbbf24;padding-right:2px" title="Strict whole-word match">=</span>` : '';
      return `<span style="display:inline-flex;align-items:center;gap:3px;background:${chipBg};color:${chipColor};border:1px solid ${chipBorder};border-radius:14px;padding:4px 7px 4px 9px;font-size:.78rem;font-weight:600;white-space:nowrap">` +
        `${badge}${safe}` +
        `<button data-action="removeKeyword" data-idx="${i}" style="background:none;border:none;color:${chipColor};opacity:.6;font-size:.75rem;cursor:pointer;padding:0 0 0 4px;line-height:1" title="Remove">&#10005;</button>` +
        `</span>`;
    }).join('') +
  `</div>`;
}

function addKeyword() {
  const input = document.getElementById('kw-input');
  const word = input.value.trim();
  if (!word) return;
  if (!window._keywords.some(k => k.toLowerCase() === word.toLowerCase())) {
    window._keywords.push(word);
    window._keywords.sort();
    _lsSet('keywords', window._keywords);
    _syncToServer();
  }
  input.value = '';
  renderKeywordList();
  input.focus();
}

function removeKeyword(word) {
  window._keywords = window._keywords.filter(k => k.toLowerCase() !== word.toLowerCase());
  _lsSet('keywords', window._keywords);
  _syncToServer();
  renderKeywordList();
}

function removeKeywordAt(i) {
  // Index-based removal — safe for keywords containing any characters (quotes, etc.)
  window._keywords.splice(i, 1);
  _lsSet('keywords', window._keywords);
  _syncToServer();
  renderKeywordList();
}

function clearAllKeywords() {
  if (!window._keywords.length) return;
  if (!confirm(`Clear all ${window._keywords.length} want list item${window._keywords.length !== 1 ? 's' : ''}? This cannot be undone.`)) return;
  window._keywords = [];
  _lsSet('keywords', window._keywords);
  _syncToServer();
  renderKeywordList();
}

function _toggleKeywordStrict(i) {
  const kw = window._keywords[i];
  if (kw === undefined) return;
  window._keywords[i] = kw.startsWith('=') ? kw.slice(1) : '=' + kw;
  _lsSet('keywords', window._keywords);
  _syncToServer();
  renderKeywordList();
}

function _toggleStrictSearch() {
  window._strictSearch = !window._strictSearch;
  const btn = document.getElementById('strict-search-btn');
  if (btn) {
    btn.textContent = '≈';
    btn.classList.toggle('active', window._strictSearch);
    btn.title = window._strictSearch
      ? '≈ Fuzzy (contains) mode on — click to restore whole-word default'
      : 'Whole-word search (default) — click for ≈ fuzzy (contains) mode';
  }
  _srvLoading = false;
  _srvPage = 1;
  _fetchBrowsePage(1);
}

function _escapeRegex(s) {
  return s.replace(/[\\^$.*+?()[\]{}|]/g, '\\$&');
}
function _parseQueryTerms(queryStr, fuzzy) {
  /* Mirror of Python _compile_query — same syntax rules. */
  var wb = '\\b';  // word boundary for new RegExp()
  var terms = [];
  queryStr.split(',').forEach(function(part) {
    part = part.trim();
    if (!part) return;
    if (part.startsWith('"') && part.endsWith('"') && part.length > 2) {
      terms.push({mode:'exact', val: part.slice(1,-1).toLowerCase()});
    } else if (part.indexOf('*') >= 0) {
      var pieces = part.split('*').map(function(p) { return _escapeRegex(p); });
      terms.push({mode:'regex', val: new RegExp(pieces.join('.*'), 'i')});
    } else if (fuzzy) {
      terms.push({mode:'contains', val: part.toLowerCase()});
    } else {
      var escaped = _escapeRegex(part);
      terms.push({mode:'word', val: new RegExp(wb + escaped + wb, 'i')});
    }
  });
  return terms;
}
function _matchesAllTerms(textLower, terms) {
  return terms.length > 0 && terms.every(function(t) {
    if (t.mode === 'exact' || t.mode === 'contains') return textLower.includes(t.val);
    return t.val.test(textLower);
  });
}
function _itemMatchesKeyword(item) {
  if (!window._keywords || !window._keywords.length) return false;
  const text = ((item.name || '') + ' ' + (item.brand || '')).toLowerCase();
  return window._keywords.some(function(kw) {
    // Strip legacy '=' strict prefix — whole-word is the default now
    var k = kw.replace(/^=/, '').trim();
    return k && _matchesAllTerms(text, _parseQueryTerms(k));
  });
}

// ── Global search (all stores) — now driven by #res-search in filter sheet ───
function globalSearch() {
  // Legacy entry point — now search is handled by _globalKeywordSearch() via #res-search
  const el = document.getElementById('res-search');
  const q = el ? el.value.trim() : '';
  if (!q) return;
  _globalKeywordSearch();
}

function clearGlobalSearch() {
  _globalSearchActive = false; _wantListSearchActive = false;
  _globalSearchQuery = '';
  const el = document.getElementById('res-search');
  if (el) { el.value = ''; _updateResSearchClear(); }
  _resetWantListLink();
  // Go back to whatever stores are selected — bypass browseCache debounce
  // and force-clear any stuck loading flag so the fetch always fires
  const stores = getSelected();
  if (!stores.length) {
    document.getElementById('res-panel').style.display = 'none';
    return;
  }
  _srvStores = stores;
  _srvPage = 1;
  _srvLoading = false;
  _fetchBrowsePage(1);
}

function searchWantList() {
  // Toggle: if already active, restore pre-want-list state
  if (_wantListSearchActive) {
    _wantListSearchActive = false;
    _globalSearchActive = false;
    _globalSearchQuery = '';
    _resetWantListLink();
    if (_preSpecialViewState) { _restoreFilterState(); return; }
    _srvStores = getSelected();
    _srvPage = 1;
    _srvLoading = false;
    _fetchBrowsePage(1);
    return;
  }
  if (!window._keywords || !window._keywords.length) {
    openKeywords();
    return;
  }
  // Activating — save current state before clearing
  _preSpecialViewState = _captureFilterState();
  _globalSearchActive = true;
  _wantListSearchActive = true;
  _globalSearchQuery = '';
  _browseMode = 'server';
  _srvPage = 1;
  _srvSortField = 'date';
  _srvSortDir = 'desc';
  window._sortCol = null; window._sortDir = 1;
  document.getElementById('res-search').value = '';
  document.getElementById('res-search-count').textContent = '';
  window._selectedBrands = []; _updateBrandBtn();
  window._selectedConds = []; _updateCondBtn();
  window._selectedCats = []; _updateCatBtn();
  window._selectedSubs = []; _updateSubcatBtn(); _setSubList([]);
  // Clear price filter too
  window._priceMin = null; window._priceMax = null;
  ['price-min','price-max','price-min-dd','price-max-dd'].forEach(function(id){var el=document.getElementById(id);if(el)el.value='';});
  _updatePriceBtn && _updatePriceBtn();
  _watchFilterActive = false;
  document.getElementById('watchlist-toggle').classList.remove('wl-active');
  _priceDropFilterActive = false;
  document.getElementById('price-drop-toggle').classList.remove('wl-active');
  document.getElementById('want-list-toggle').classList.add('wl-active');
  _updateWantListCount();
  _srvLoading = false;  // cancel any in-flight request so the toggle always lands
  _fetchBrowsePage(1);
}

function _resetWantListLink() {
  const btn = document.getElementById('want-list-toggle');
  if (btn) btn.classList.remove('wl-active');
  _updateWantListCount();
}

// Show/hide Edit Want List link — visible only when want list filter is active
let _wlCountTimer = null;
function _updateWantListCount() {
  const editLink = document.getElementById('search-wl-link');
  if (!editLink) return;
  // Desktop: always visible. Mobile: only when want list filter is active.
  editLink.style.display = (!_isMobile() || _wantListSearchActive) ? 'inline' : 'none';
}

// ── Run ───────────────────────────────────────────────────────────────────────
async function runTracker() {
  // Always scan nationwide so snapshot comparison is accurate
  await startRun({stores:[], baseline:false}, false);
}

async function stopRun() {
  const btn = document.getElementById('stop-btn');
  btn.textContent = '⏹ Stopping…';
  btn.disabled = true;
  await fetch('/api/stop', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({run_id: window._activeRunId || ''})
  });
}

async function startRun(payload, isBaseline) {
  running = true; updateCount(); _updateMobileBottomBar();
  const stopBtn = document.getElementById('stop-btn');
  stopBtn.style.display = 'inline-block';
  stopBtn.disabled = false;
  stopBtn.textContent = '⏹ Stop Running';

  document.getElementById('res-panel').style.display = 'none';
  document.getElementById('log').innerHTML = '';

  // Include this device's last-run time so the server gives per-device NEW results.
  // Also include the per-user anchor (v2.10.18) — for guests this is the only way the
  // server learns it; for logged-in users the server reads its own DB and ignores this.
  const runPayload = Object.assign({}, payload, {
    device_last_run:    window._lastRunISO    || '',
    device_last_anchor: window._lastAnchorISO || ''
  });
  const resp = await fetch('/api/run', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(runPayload)
  });
  if (!resp.ok) {
    const e = await resp.json();
    running = false; stopBtn.style.display = 'none'; updateCount(); _updateMobileBottomBar();
    appendLog('Error: ' + (e.error || resp.statusText), 'log-err');
    return;
  }
  // Get run_id and run_time from start response.
  // status "joined" means another user's scan is already running — we subscribe to it.
  const runData = await resp.json();
  const runId = runData.run_id || '';
  window._activeRunId = runId;  // used by stopRun() to authenticate the stop request
  const scanRunTime = runData.run_time || '';
  if (runData.status === 'joined') {
    appendLog('⏳ Scan already in progress — joining…', 'log-dim');
  }

  const es = new EventSource('/api/progress' + (runId ? '?run_id=' + encodeURIComponent(runId) : ''));
  es.onmessage = e => {
    let msg;
    try { msg = JSON.parse(e.data); } catch(err) {
      appendLog('Warning: could not parse progress message', 'log-err');
      return;
    }
    if (msg.type === 'ping') return;
    if (msg.type === 'progress') { appendLog(msg.msg); return; }
    if (msg.type === 'done') {
      es.close(); running = false;
      window._activeRunId = '';  // clear so a stale runId can't be replayed
      stopBtn.style.display = 'none';
      _skipBrowse = true;  // Prevent browseCache from overwriting scan results
      updateCount(); _updateMobileBottomBar(); loadState(); showResults(msg, isBaseline);
    }
  };
  es.onerror = () => {
    // SSE connection dropped — recover gracefully
    es.close();
    if (running) {
      running = false;
      stopBtn.style.display = 'none';
      updateCount(); loadState();
      appendLog('Connection to server lost. Refreshing results…', 'log-dim');
      // The scan likely completed on the server even if our SSE stream dropped
      // (common on mobile when screen locks or network blips). Use the scan's
      // actual start time (returned by /api/run) so browse gating doesn't hide
      // items the scan found, AND so the next scan's new-detection window starts
      // from the right place rather than an arbitrary "now".
      window._lastRunISO = scanRunTime || new Date().toISOString();
      _lsSet('last_run', window._lastRunISO);
      _syncToServer(true);
      _updateRelativeTime();
      // Fall back to browse mode to show whatever data was saved
      setTimeout(() => {
        const stores = getSelected();
        if (stores.length) browseCache();
      }, 1000);
    }
  };
}

// ── Results ───────────────────────────────────────────────────────────────────
function showResults(msg, isBaseline) {
  const panel = document.getElementById('res-panel');
  panel.style.display = 'block';

  if (msg.error) {
    document.getElementById('res-title').textContent = 'Error';
    document.getElementById('res-badge').textContent = '';
    document.getElementById('res-body').innerHTML = `<div class="no-res" style="color:#f88">${msg.error}</div>`;
    return;
  }

  const stoppedNote = msg.stopped ? ' (stopped early)' : '';

  // New-item detection is date-based, computed server-side.
  // The server compares each item's date_listed (Algolia startDate) against this device's
  // previous scan time. Items listed after that scan are "new".
  const newIdSet = new Set(msg.new_ids || []);
  const isFirstRun = msg.baseline;
  const freshNewCount = newIdSet.size;

  // Always replace the NEW set with exactly what this scan found.
  // 0 new = all NEW tags clear. Each scan is the source of truth.
  if (!isFirstRun) {
    window._newIds = newIdSet;
    _lsSet('new_ids', [...newIdSet]);
    // Immediately remove stale NEW badges from the DOM — don't wait for async browse re-render
    if (freshNewCount === 0) {
      document.querySelectorAll('.tag').forEach(el => { if (el.textContent.trim() === 'NEW') el.remove(); });
      document.querySelectorAll('.is-new').forEach(el => el.classList.remove('is-new'));
    }
  }

  appendLog(`\\n✓ ${isFirstRun ? 'Initial scan complete' : 'Done' + stoppedNote + ' — ' + freshNewCount.toLocaleString() + ' new this scan'}.`, 'log-dim');

  window._lastRunISO = msg.scan_time || new Date().toISOString();
  _lsSet('last_run', window._lastRunISO);
  // Per-user anchor (v2.10.18): server sends the max date_listed in the
  // post-scan cache. Store locally so the next scan's threshold isn't
  // contaminated by other users' activity.
  if (msg.scan_anchor) {
    window._lastAnchorISO = msg.scan_anchor;
    _lsSet('last_anchor', msg.scan_anchor);
  }
  // Sync scan results to server so they follow the user to other devices
  _syncToServer(true);
  _updateRelativeTime();
  document.getElementById('check-now-btn').style.display = 'inline';

  // Check if any new items match the want list and show notification
  const wantMatchEl = document.getElementById('s-want-match');
  if (freshNewCount > 0 && window._keywords && window._keywords.length) {
    // We need item details to check want list — fetch from server cache
    fetch('/api/browse', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({page:1, per_page:1000, all_stores:true, new_ids:[...newIdSet], keywords:window._keywords, filter_want_list_only:true})
    }).then(r => r.json()).then(d => {
      const wantNewCount = d.new_want_count ?? d.total_count ?? 0;
      if (wantNewCount > 0) {
        wantMatchEl.textContent = '🎯 ' + wantNewCount + ' new want list match' + (wantNewCount > 1 ? 'es' : '') + '!';
        wantMatchEl.style.display = '';
      } else {
        wantMatchEl.style.display = 'none';
      }
    }).catch(() => { wantMatchEl.style.display = 'none'; });
  } else {
    wantMatchEl.style.display = 'none';
  }

  // Refresh shared item count from server
  fetch('/api/state').then(r => r.json()).then(s => {
    document.getElementById('s-known').textContent = s.total_items.toLocaleString();
  }).catch(() => {});

  if (isBaseline) {
    document.getElementById('res-title').textContent = 'Baseline Complete';
    document.getElementById('res-badge').textContent = '';
    document.getElementById('res-body').innerHTML =
      `<div class="no-res">Inventory database built (${msg.scanned.toLocaleString()} items)${stoppedNote}. Check back any time to see what's new!</div>`;
    ['cond-dropdown','cat-dropdown','subcat-dropdown'].forEach(id => document.getElementById(id).style.display = 'none');
    return;
  }

  document.getElementById('res-search').value = '';
  document.getElementById('res-search-count').textContent = '';
  document.getElementById('res-title').textContent = `${msg.scanned.toLocaleString()} Items`;
  document.getElementById('res-badge').textContent = freshNewCount > 0 ? freshNewCount + ' NEW' : '';

  if (msg.scanned === 0) {
    document.getElementById('res-body').innerHTML = '<div class="no-res">Nothing found for selected stores.</div>';
    ['cond-dropdown','cat-dropdown','subcat-dropdown'].forEach(id => document.getElementById(id).style.display = 'none');
    return;
  }

  // For large scans, switch to server-side browse
  if (msg.use_browse) {
    _browseMode = 'server';
    _srvStores = getSelected();
    if (!_srvStores.length) _srvStores = [];
    _srvPage = 1;
    _srvSortField = 'date';
    _srvSortDir = 'desc';
    window._sortCol = null; window._sortDir = 1;
    _watchFilterActive = false;
    document.getElementById('watchlist-toggle').classList.remove('wl-active');
    _priceDropFilterActive = false;
    document.getElementById('price-drop-toggle').classList.remove('wl-active');
    _wantListSearchActive = false;
    document.getElementById('want-list-toggle').classList.remove('wl-active');
    if (!_srvStores.length) {
      _globalSearchActive = false; _wantListSearchActive = false;
      _globalSearchQuery = '';
    }
    _srvLoading = false;  // Reset guard — same defensive pattern as browseCache/clearGlobalSearch
    _fetchBrowsePage(1);
    return;
  }

  // Small scan: render items client-side, marking isNew per-user
  // Use the accumulated window._newIds (which may carry over from prior scan if this one found 0)
  _browseMode = 'local';
  const effectiveNewIds = window._newIds instanceof Set ? window._newIds : new Set();
  window._tableData = (msg.items || []).map(item => ({
    ...item,
    isNew: effectiveNewIds.has(item.id),
    kwMatch: _itemMatchesKeyword(item),
  }));
  window._tableData.sort((a, b) => {
    // Only NEW items float to top; everything else by date desc
    const aNew = a.isNew ? 0 : 1;
    const bNew = b.isNew ? 0 : 1;
    if (aNew !== bNew) return aNew - bNew;
    return (b.date_raw || '').localeCompare(a.date_raw || '');
  });
  window._sortCol = null; window._sortDir = 1; window._localPage = 1;
  populateCategoryFilter();
  renderTable();
}

// ── Category filters ──────────────────────────────────────────────────────────
function populateCategoryFilter() {
  // In server mode, filters are populated by _populateFiltersFromServer — this is for local mode only
  if (_browseMode === 'server') return;
  const data = window._tableData || [];
  // Brand filter — count occurrences and sort by count desc
  const brandMap = {};
  data.forEach(i => { if (i.brand) brandMap[i.brand] = (brandMap[i.brand] || 0) + 1; });
  const brandList = Object.entries(brandMap).sort((a,b) => b[1] - a[1]).map(([name, count]) => ({name, count}));
  _setBrandList(brandList);
  window._selectedBrands = [];
  _updateBrandBtn();
  // Condition filter (multi-select) — ordered best to worst
  const _condOrder = {Excellent:0,Great:1,Good:2,Fair:3,Poor:4};
  const conds = [...new Set(data.map(i => i.condition).filter(Boolean))].sort((a,b) => (_condOrder[a]??9) - (_condOrder[b]??9));
  _setCondList(conds);
  window._selectedConds = [];
  _updateCondBtn();
  // Category filter (multi-select)
  const cats = [...new Set(data.map(i => i.category).filter(Boolean))].sort();
  _setCatList(cats);
  window._selectedCats = [];
  _updateCatBtn();
  // Subcategory filter (multi-select) — start hidden
  window._selectedSubs = [];
  _updateSubcatBtn();
  _setSubList([]);
}

function onCatFilterChange() {
  if (_browseMode === 'server') {
    // In server mode, changing category resets subcategory and fetches page 1
    window._selectedSubs = []; _updateSubcatBtn();
    _srvPage = 1;
    _srvLoading = false;  // cancel any in-flight request so filter always lands
    _fetchBrowsePage(1);
    return;
  }
  const catArr = window._selectedCats || [];
  const data   = window._tableData || [];
  const subcats = [...new Set(
    data.filter(i => !catArr.length || catArr.includes(i.category || '')).map(i => i.subcategory).filter(Boolean)
  )].sort();
  if (subcats.length && catArr.length) {
    _setSubList(subcats);
  } else {
    _setSubList([]);
  }
  window._selectedSubs = [];
  _updateSubcatBtn();
  filterResults();
}

// ── Table rendering & sorting ─────────────────────────────────────────────────
// col indices: 0=status, 1=name, 2=brand, 3=price, 4=condition, 5=category, 6=subcategory, 7=date, 8=location
const _SORT_COLS = [null, 'name', 'brand', 'price', 'condition', 'category', 'subcategory', 'date', 'location'];
const PAGE_SIZE = 50;
window._localPage = 1;

function renderTable() {
  // In server mode, rendering is handled by _renderServerTable
  if (_browseMode === 'server') return;

  const allData = window._tableData || [];

  // Apply filters to get the filtered set
  const q        = document.getElementById('res-search').value.toLowerCase().trim();
  const brandArr = window._selectedBrands || [];
  const condArr  = window._selectedConds || [];
  const catArr   = window._selectedCats || [];
  const subArr   = window._selectedSubs || [];

  const filtered = allData.filter(item => {
    if (_watchFilterActive && !(window._watchlist || {})[item.id || '']) return false;
    // Text filter: all words must match (AND), or exact phrase if quoted
    if (q) {
      const text = ((item.name||'')+' '+(item.brand||'')+' '+(item.store||'')+' '+(item.location||'')+' '+(item.category||'')+' '+(item.subcategory||'')).toLowerCase();
      if (q.startsWith('"') && q.endsWith('"') && q.length > 2) {
        if (!text.includes(q.slice(1,-1))) return false;
      } else {
        const words = q.split(/\s+/).filter(Boolean);
        if (!words.every(w => text.includes(w))) return false;
      }
    }
    return (!brandArr.length || brandArr.includes(item.brand || '')) &&
           (!condArr.length  || condArr.includes(item.condition || '')) &&
           (!catArr.length   || catArr.includes(item.category || '')) &&
           (!subArr.length   || subArr.includes(item.subcategory || ''));
  });

  const totalPages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  window._localPage = Math.min(window._localPage, totalPages);
  const start = (window._localPage - 1) * PAGE_SIZE;
  const pageItems = filtered.slice(start, start + PAGE_SIZE);

  let html = `<table id="res-table"><thead><tr>
    <th data-col="0"></th>
    <th data-col="kw"></th>
    <th data-col="watch"></th>
    <th data-col="1">Item</th>
    <th data-col="2">Brand</th>
    <th data-col="3">Price</th>
    <th data-col="drop"></th>
    <th data-col="4">Condition</th>
    <th data-col="5">Category</th>
    <th data-col="6">Subcategory</th>
    <th data-col="7">Date Listed</th>
    <th data-col="8">Location</th>
  </tr></thead><tbody>`;
  pageItems.forEach(item => { html += _buildRowHtml(item); });
  html += '</tbody></table>';
  html += _buildPaginatorHtml(window._localPage, totalPages, filtered.length, PAGE_SIZE);
  document.getElementById('res-body').innerHTML = html;

  // Update filter count display
  const countEl = document.getElementById('res-search-count');
  if (q || brandArr.length || condArr.length || catArr.length || subArr.length) {
    countEl.textContent = `${filtered.length} of ${allData.length}`;
  } else {
    countEl.textContent = '';
  }
  const clearBtn = document.getElementById('clear-filters-btn');
  if (clearBtn) clearBtn.style.display = (q || brandArr.length || condArr.length || catArr.length || subArr.length) ? '' : 'none';

  if (window._sortCol !== null) {
    const th = document.querySelector(`#res-table th[data-col="${window._sortCol}"]`);
    if (th) th.classList.add(window._sortDir === 1 ? 'sort-asc' : 'sort-desc');
  }

  document.querySelectorAll('#res-table thead th[data-col]').forEach(th => {
    const colIdx = parseInt(th.dataset.col);
    if (!_SORT_COLS[colIdx]) return;
    th.addEventListener('click', () => sortTable(colIdx));
  });
}

function goToPage(page) {
  if (_browseMode === 'server') {
    if (page < 1 || page > _srvTotalPages || page === _srvPage) return;
    _fetchBrowsePage(page);  // scroll handled inside _fetchBrowsePage after innerHTML
    return;
  }
  // Local mode
  window._localPage = page;
  renderTable();
  document.getElementById('res-panel')?.scrollTo(0, 0);
  document.getElementById('res-body')?.scrollTo(0, 0);
}

function sortTable(colIdx) {
  const field = _SORT_COLS[colIdx];
  if (!field) return;

  if (_browseMode === 'server') {
    // Determine new direction
    const newDir = (window._sortCol === colIdx)
      ? (window._sortDir === 1 ? -1 : 1)
      : (field === 'date' ? -1 : 1);
    window._sortCol = colIdx;
    window._sortDir = newDir;
    _srvSortField = field;
    _srvSortDir = (newDir === -1) ? 'desc' : 'asc';
    _srvPage = 1;
    _fetchBrowsePage(1);
    return;
  }

  window._sortDir = (window._sortCol === colIdx) ? window._sortDir * -1 : (field === 'date' ? -1 : 1);
  window._sortCol = colIdx;
  window._localPage = 1;  // Reset pagination on sort
  const dir = window._sortDir;
  // Quality ranking for condition column (v2.10.18) — best to worst, not alphabetical.
  // Unknown conditions go to the end regardless of direction.
  const _condRank = {Excellent:0, Great:1, Good:2, Fair:3, Poor:4};
  window._tableData.sort((a, b) => {
    let av = a[field] || '', bv = b[field] || '';
    if (field === 'price') {
      av = parseFloat((av+'').replace(/[^0-9.]/g,'')) || 0;
      bv = parseFloat((bv+'').replace(/[^0-9.]/g,'')) || 0;
      return (av - bv) * dir;
    }
    if (field === 'date') {
      av = a['date_raw'] || '';
      bv = b['date_raw'] || '';
      return av.toString().localeCompare(bv.toString()) * dir;
    }
    if (field === 'condition') {
      const ra = (_condRank[av] !== undefined) ? _condRank[av] : (dir === 1 ? 99 : -1);
      const rb = (_condRank[bv] !== undefined) ? _condRank[bv] : (dir === 1 ? 99 : -1);
      return (ra - rb) * dir;
    }
    return av.toString().localeCompare(bv.toString()) * dir;
  });
  renderTable();
}

function autoSizeItemColumn() {
  // Measure the longest visible item name using a hidden canvas for accuracy
  const canvas = autoSizeItemColumn._canvas || (autoSizeItemColumn._canvas = document.createElement('canvas'));
  const ctx = canvas.getContext('2d');
  ctx.font = '13.3px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif'; // matches td font ~.83rem
  let maxW = 80;
  // In server mode, read names from DOM rows; in local mode, use _tableData
  if (_browseMode === 'server') {
    document.querySelectorAll('#res-table tbody tr').forEach(tr => {
      const name = tr.dataset.name || '';
      const w = ctx.measureText(name).width;
      if (w > maxW) maxW = w;
    });
  } else {
    const data = window._tableData || [];
    if (!data.length) return;
    data.forEach(item => {
      const w = ctx.measureText(item.name || '').width;
      if (w > maxW) maxW = w;
    });
  }
  // Add padding; cap so data columns get room to breathe
  const colW = Math.min(Math.ceil(maxW) + 32, 420); // cap at 420px
  const th = document.querySelector('#res-table th[data-col="1"]');
  if (th) th.style.maxWidth = colW + 'px';
  document.querySelectorAll('#res-table tbody tr td:nth-child(4)').forEach(td => {
    td.style.maxWidth = colW + 'px';
  });
}

// ── Image thumbnail hover ────────────────────────────────────────────────────
(function() {
  const tooltip = document.getElementById('img-tooltip');
  const tooltipImg = tooltip.querySelector('img');
  let hoverTimer = null;
  const HOVER_DELAY = 400; // ms before showing thumbnail

  document.addEventListener('mouseenter', function(e) {
    if (_isMobile()) return;  // No hover thumbnails on mobile
    // GC results
    const gcLink = e.target.closest('#res-body a');
    // CL results
    const clLink = e.target.closest('#cl-body a');
    const link = gcLink || clLink;
    if (!link) return;
    const row = link.closest('tr');
    if (!row) return;

    let imgUrl = '';
    if (gcLink) {
      const imageId = row.dataset.imageId;
      if (imageId) imgUrl = 'https://media.guitarcenter.com/is/image/MMGS7/' + imageId + '-00-600x600.jpg';
    } else if (clLink) {
      imgUrl = row.dataset.clImage || '';
    }
    if (!imgUrl) return;

    clearTimeout(hoverTimer);
    hoverTimer = setTimeout(function() {
      tooltipImg.src = imgUrl;
      const rect = link.getBoundingClientRect();
      let left = rect.right + 12;
      let top = rect.top - 40;
      if (left + 220 > window.innerWidth) left = rect.left - 220;
      if (top + 220 > window.innerHeight) top = window.innerHeight - 225;
      if (top < 5) top = 5;
      tooltip.style.left = left + 'px';
      tooltip.style.top = top + 'px';
      tooltip.style.display = 'block';
    }, HOVER_DELAY);
  }, true);

  document.addEventListener('mouseleave', function(e) {
    const link = e.target.closest('#res-body a') || e.target.closest('#cl-body a');
    if (!link) return;
    clearTimeout(hoverTimer);
    tooltip.style.display = 'none';
    tooltipImg.src = '';
  }, true);

  // Also hide on scroll (both panels)
  document.querySelector('.results')?.addEventListener('scroll', function() {
    clearTimeout(hoverTimer);
    tooltip.style.display = 'none';
  });
  document.getElementById('cl-body')?.addEventListener('scroll', function() {
    clearTimeout(hoverTimer);
    tooltip.style.display = 'none';
  });
})();

// ── Brand multi-select dropdown (with search) ───────────────────────────────
window._selectedBrands = [];
window._brandList = [];

// ── Mobile filter accordion ───────────────────────────────────────────────────
let _accOpenSection = null;

function _accToggle(section) {
  const body = document.getElementById('acc-' + section + '-body');
  const arrow = document.getElementById('acc-' + section + '-arrow');
  if (!body) return;
  if (_accOpenSection === section) {
    body.style.maxHeight = '0';
    body.classList.remove('open');
    arrow.classList.remove('open');
    _accOpenSection = null;
  } else {
    if (_accOpenSection) {
      const prev = document.getElementById('acc-' + _accOpenSection + '-body');
      const prevArrow = document.getElementById('acc-' + _accOpenSection + '-arrow');
      if (prev) { prev.style.maxHeight = '0'; prev.classList.remove('open'); }
      if (prevArrow) prevArrow.classList.remove('open');
    }
    _accOpenSection = section;
    if (section === 'brand') _accRenderBrand();
    else if (section === 'cond') _accRenderCond();
    else if (section === 'cat') _accRenderCat();
    else if (section === 'sub') _accRenderSub();
    body.classList.add('open');
    arrow.classList.add('open');
    body.style.maxHeight = body.scrollHeight + 'px';
  }
}

function _accExpandHeight(section) {
  const body = document.getElementById('acc-' + section + '-body');
  if (body && _accOpenSection === section) body.style.maxHeight = body.scrollHeight + 'px';
}

function _accBuildItems(dataList, selectedArr) {
  let html = '';
  dataList.forEach(item => {
    const name = (item && item.name !== undefined) ? item.name : item;
    const count = (item && item.count !== undefined) ? item.count : '';
    const isActive = selectedArr.includes(name);
    if (count === 0 && !isActive) return;
    const esc = name.replace(/&/g,'&amp;').replace(/"/g,'&quot;');
    html += '<div class="acc-item' + (isActive ? ' acc-active' : '') + '" data-val="' + esc + '">'
      + '<span class="acc-check">' + (isActive ? '✓' : '') + '</span>'
      + '<span class="acc-label">' + name + '</span>'
      + (count !== '' ? '<span class="acc-count">' + Number(count).toLocaleString() + '</span>' : '')
      + '</div>';
  });
  return html;
}

function _accRenderBrand(query) {
  const list = document.getElementById('acc-brand-list');
  if (!list) return;
  const q = (query !== undefined ? query : (document.getElementById('acc-brand-search') ? document.getElementById('acc-brand-search').value : '') || '').toLowerCase();
  const data = (window._brandList || []).filter(b => {
    const name = (b && b.name !== undefined) ? b.name : b;
    const count = (b && b.count !== undefined) ? b.count : '';
    if (count === 0 && !(window._selectedBrands || []).includes(name)) return false;
    return !q || name.toLowerCase().includes(q);
  });
  const _bClear = (window._selectedBrands || []).length > 0 ? '<div class="acc-clear-row" data-action="clearBrand">✕ Clear Brand</div>' : '';
  list.innerHTML = _bClear + (_accBuildItems(data, window._selectedBrands || []) || '<div class="acc-empty">No brands found</div>');
  list.onclick = function(e) { const el = e.target.closest('.acc-item'); if (el) _toggleBrand(el.dataset.val); };
  _accExpandHeight('brand');
}

function _accRenderCond() {
  const list = document.getElementById('acc-cond-list');
  if (!list) return;
  const _cClear = (window._selectedConds || []).length > 0 ? '<div class="acc-clear-row" data-action="clearCond">✕ Clear Condition</div>' : '';
  list.innerHTML = _cClear + (_accBuildItems(window._condList || [], window._selectedConds || []) || '<div class="acc-empty">No conditions</div>');
  list.onclick = function(e) { const el = e.target.closest('.acc-item'); if (el) _toggleCond(el.dataset.val); };
  _accExpandHeight('cond');
}

function _accRenderCat() {
  const list = document.getElementById('acc-cat-list');
  if (!list) return;
  const _catClear = (window._selectedCats || []).length > 0 ? '<div class="acc-clear-row" data-action="clearCat">✕ Clear Category</div>' : '';
  list.innerHTML = _catClear + (_accBuildItems(window._catList || [], window._selectedCats || []) || '<div class="acc-empty">No categories</div>');
  list.onclick = function(e) { const el = e.target.closest('.acc-item'); if (el) _toggleCat(el.dataset.val); };
  _accExpandHeight('cat');
}

function _accRenderSub() {
  const list = document.getElementById('acc-sub-list');
  if (!list) return;
  const _sClear = (window._selectedSubs || []).length > 0 ? '<div class="acc-clear-row" data-action="clearSub">✕ Clear Subcategory</div>' : '';
  list.innerHTML = _sClear + (_accBuildItems(window._subList || [], window._selectedSubs || []) || '<div class="acc-empty">No subcategories</div>');
  list.onclick = function(e) { const el = e.target.closest('.acc-item'); if (el) _toggleSub(el.dataset.val); };
  _accExpandHeight('sub');
}

function _accUpdateSummaries() {
  const fmt = (arr) => !arr.length ? '' : arr.length === 1 ? arr[0] : arr[0] + ' +' + (arr.length - 1);
  const set = (id, arr) => { const el = document.getElementById(id); if (el) el.textContent = fmt(arr); };
  set('acc-brand-summary', window._selectedBrands || []);
  set('acc-cond-summary',  window._selectedConds  || []);
  set('acc-cat-summary',   window._selectedCats   || []);
  set('acc-sub-summary',   window._selectedSubs   || []);
}

function _accCloseAll() {
  ['brand','cond','cat','sub'].forEach(s => {
    const body = document.getElementById('acc-' + s + '-body');
    const arrow = document.getElementById('acc-' + s + '-arrow');
    if (body) { body.style.maxHeight = '0'; body.classList.remove('open'); }
    if (arrow) arrow.classList.remove('open');
  });
  _accOpenSection = null;
}

function _accUpdateVisibility() {
  const catAcc = document.getElementById('acc-cat');
  const subAcc = document.getElementById('acc-sub');
  if (catAcc) catAcc.style.display = (window._catList && window._catList.length) ? '' : 'none';
  if (subAcc) subAcc.style.display = (window._subList && window._subList.length) ? '' : 'none';
}

function _closeAllDropdowns() {
  _closeBrandDropdown();
  _closeCondDropdown();
  _closeCatDropdown();
  _closeSubDropdown();
  _closePriceDropdown();
}

function toggleBrandDropdown() {
  const panel = document.getElementById('brand-dd-panel');
  if (panel.style.display === 'none') {
    _closeAllDropdowns();
    panel.style.display = '';
    document.getElementById('brand-dd-search').value = '';
    _renderBrandList();
    document.getElementById('brand-dd-search').focus();
    setTimeout(() => document.addEventListener('click', _closeBrandOnOutside, true), 0);
  } else {
    _closeBrandDropdown();
  }
}

function _closeBrandDropdown() {
  document.getElementById('brand-dd-panel').style.display = 'none';
  document.removeEventListener('click', _closeBrandOnOutside, true);
}

function _closeBrandOnOutside(e) {
  if (!e.target.closest('#brand-dropdown')) _closeBrandDropdown();
}

function filterBrandDropdown() { _renderBrandList(); }

function _renderBrandList() {
  const q = (document.getElementById('brand-dd-search').value || '').toLowerCase();
  const list = document.getElementById('brand-dd-list');
  let html = window._selectedBrands.length > 0
    ? '<div class="dd-clear-row" data-action="clearBrand">✕ Clear Brand</div>' : '';
  window._brandList.forEach(b => {
    if (q && !b.name.toLowerCase().includes(q)) return;
    const isActive = window._selectedBrands.includes(b.name);
    if (b.count === 0 && !isActive) return;
    const esc = b.name.replace(/"/g,'&quot;');
    html += '<div class="brand-dd-item' + (isActive ? ' active' : '') + '" data-brand="' + esc + '">'
         + '<span class="cond-dd-check">' + (isActive ? '✓' : '') + '</span>'
         + esc + '<span class="bcount">' + b.count.toLocaleString() + '</span></div>';
  });
  list.innerHTML = html;
  list.onclick = function(e) {
    const item = e.target.closest('.brand-dd-item');
    if (!item) return;
    _toggleBrand(item.dataset.brand);
  };
}

function _toggleBrand(brand) {
  const idx = window._selectedBrands.indexOf(brand);
  if (idx >= 0) window._selectedBrands.splice(idx, 1);
  else window._selectedBrands.push(brand);
  _updateBrandBtn();
  _renderBrandList();
  if (_isMobile()) { _accRenderBrand(); _accUpdateSummaries(); }
  filterResults();
}

function selectBrand(brand) {
  // Called from table brand-link clicks — toggle behavior
  const idx = window._selectedBrands.indexOf(brand);
  if (idx >= 0) {
    window._selectedBrands.splice(idx, 1);
  } else {
    window._selectedBrands = [brand];  // Set to just this brand
  }
  _updateBrandBtn();
  filterResults();
}

function _updateBrandBtn() {
  const btn = document.getElementById('brand-dd-btn');
  if (window._selectedBrands.length === 0) btn.textContent = 'All Brands ▾';
  else if (window._selectedBrands.length === 1) btn.textContent = window._selectedBrands[0] + ' ▾';
  else btn.textContent = window._selectedBrands.length + ' Brands ▾';
}

function _setBrandList(brands) {
  window._brandList = brands || [];
  const hasData = !!(brands && brands.length);
  document.getElementById('brand-dropdown').style.display = hasData ? '' : 'none';
  const pdd = document.getElementById('price-dropdown');
  if (pdd) pdd.style.display = hasData ? '' : 'none';
}

// ── Condition multi-select dropdown ──────────────────────────────────────────
window._selectedConds = [];
window._condList = [];

function toggleCondDropdown() {
  const panel = document.getElementById('cond-dd-panel');
  if (panel.style.display === 'none') {
    _closeAllDropdowns();
    panel.style.display = '';
    _renderCondList();
    setTimeout(() => document.addEventListener('click', _closeCondOnOutside, true), 0);
  } else {
    _closeCondDropdown();
  }
}

function _closeCondDropdown() {
  document.getElementById('cond-dd-panel').style.display = 'none';
  document.removeEventListener('click', _closeCondOnOutside, true);
}

function _closeCondOnOutside(e) {
  if (!e.target.closest('#cond-dropdown')) _closeCondDropdown();
}

function _renderCondList() {
  const inner = document.getElementById('cond-dd-inner') || document.getElementById('cond-dd-panel');
  let html = window._selectedConds.length > 0
    ? '<div class="dd-clear-row" data-action="clearCond">✕ Clear Condition</div>' : '';
  window._condList.forEach(c => {
    const name = (c && c.name !== undefined) ? c.name : c;
    const count = (c && c.count !== undefined) ? c.count : '';
    const isActive = window._selectedConds.includes(name);
    if (count === 0 && !isActive) return;
    const esc = name.replace(/"/g,'&quot;');
    html += '<div class="cond-dd-item' + (isActive ? ' active' : '') + '" data-cond="' + esc + '">'
         + '<span class="cond-dd-check">' + (isActive ? '✓' : '') + '</span>'
         + esc + (count !== '' ? '<span class="bcount">' + Number(count).toLocaleString() + '</span>' : '') + '</div>';
  });
  inner.innerHTML = html;
  inner.onclick = function(e) {
    const item = e.target.closest('.cond-dd-item');
    if (!item) return;
    _toggleCond(item.dataset.cond);
  };
}

function _toggleCond(cond) {
  const idx = window._selectedConds.indexOf(cond);
  if (idx >= 0) {
    window._selectedConds.splice(idx, 1);
  } else {
    window._selectedConds.push(cond);
  }
  _updateCondBtn();
  _renderCondList();
  if (_isMobile()) { _accRenderCond(); _accUpdateSummaries(); }
  filterResults();
}

function _updateCondBtn() {
  const btn = document.getElementById('cond-dd-btn');
  if (window._selectedConds.length === 0) {
    btn.textContent = 'All Conditions ▾';
  } else if (window._selectedConds.length === 1) {
    btn.textContent = window._selectedConds[0] + ' ▾';
  } else {
    btn.textContent = window._selectedConds.length + ' Conditions ▾';
  }
}

// ── Price range filter ────────────────────────────────────────────────────────

function _updatePriceBtn() {
  const btn = document.getElementById('price-dd-btn');
  const clr = document.getElementById('price-dd-clear');
  if (!btn) return;
  const hasMin = window._priceMin !== null && window._priceMin !== undefined;
  const hasMax = window._priceMax !== null && window._priceMax !== undefined;
  if (!hasMin && !hasMax) {
    btn.textContent = 'Price ▾';
  } else {
    const fmt = function(v) { return '$' + (Number(v) % 1 === 0 ? Number(v) : Number(v).toFixed(2)); };
    if (hasMin && hasMax) btn.textContent = fmt(window._priceMin) + '–' + fmt(window._priceMax) + ' ▾';
    else if (hasMin)      btn.textContent = fmt(window._priceMin) + '+ ▾';
    else                  btn.textContent = 'Up to ' + fmt(window._priceMax) + ' ▾';
  }
  if (clr) clr.style.display = (hasMin || hasMax) ? '' : 'none';
}

function _clearPriceFilter() {
  window._priceMin = null;
  window._priceMax = null;
  ['price-min', 'price-max', 'price-min-dd', 'price-max-dd'].forEach(function(id) {
    var el = document.getElementById(id); if (el) el.value = '';
  });
  _updatePriceBtn();
  _updateFilterDot();
  _srvPage = 1;
  _fetchBrowsePage(1);
}

function _closePriceDropdown() {
  var panel = document.getElementById('price-dd-panel');
  if (panel) panel.style.display = 'none';
  document.removeEventListener('click', _closePriceOnOutside, true);
}

function _closePriceOnOutside(e) {
  if (!e.target.closest('#price-dropdown')) _closePriceDropdown();
}

function togglePriceDropdown() {
  var panel = document.getElementById('price-dd-panel');
  if (!panel) return;
  var isOpen = panel.style.display !== 'none';
  _closeAllDropdowns();   // closes price panel too, so re-open if it wasn't already open
  if (!isOpen) {
    var btn = document.getElementById('price-dd-btn');
    if (!btn) return;
    var rect = btn.getBoundingClientRect();
    panel.style.top  = (rect.bottom + 4) + 'px';
    panel.style.left = rect.left + 'px';
    panel.style.display = 'block';
    requestAnimationFrame(function() {
      var pRect = panel.getBoundingClientRect();
      if (pRect.right > window.innerWidth - 8) {
        panel.style.left = Math.max(8, window.innerWidth - pRect.width - 8) + 'px';
      }
    });
    setTimeout(function() { document.addEventListener('click', _closePriceOnOutside, true); }, 0);
  }
}

function _setCondList(conditions) {
  window._condList = conditions || [];
  const dd = document.getElementById('cond-dropdown');
  dd.style.display = conditions && conditions.length ? '' : 'none';
}

// ── Category multi-select dropdown ───────────────────────────────────────────
window._selectedCats = [];
window._catList = [];

function toggleCatDropdown() {
  const panel = document.getElementById('cat-dd-panel');
  if (panel.style.display === 'none') {
    _closeAllDropdowns();
    panel.style.display = '';
    _renderCatList();
    setTimeout(() => document.addEventListener('click', _closeCatOnOutside, true), 0);
  } else { _closeCatDropdown(); }
}
function _closeCatDropdown() {
  document.getElementById('cat-dd-panel').style.display = 'none';
  document.removeEventListener('click', _closeCatOnOutside, true);
}
function _closeCatOnOutside(e) { if (!e.target.closest('#cat-dropdown')) _closeCatDropdown(); }

function _renderCatList() {
  const inner = document.getElementById('cat-dd-inner') || document.getElementById('cat-dd-panel');
  let html = window._selectedCats.length > 0
    ? '<div class="dd-clear-row" data-action="clearCat">✕ Clear Category</div>' : '';
  window._catList.forEach(c => {
    const name = (c && c.name !== undefined) ? c.name : c;
    const count = (c && c.count !== undefined) ? c.count : '';
    const isActive = window._selectedCats.includes(name);
    if (count === 0 && !isActive) return;
    const esc = name.replace(/"/g,'&quot;');
    html += '<div class="cond-dd-item' + (isActive ? ' active' : '') + '" data-val="' + esc + '">'
         + '<span class="cond-dd-check">' + (isActive ? '✓' : '') + '</span>' + esc
         + (count !== '' ? '<span class="bcount">' + Number(count).toLocaleString() + '</span>' : '') + '</div>';
  });
  inner.innerHTML = html;
  inner.onclick = function(e) {
    const item = e.target.closest('.cond-dd-item');
    if (!item) return;
    _toggleCat(item.dataset.val);
  };
}
function _toggleCat(cat) {
  const idx = window._selectedCats.indexOf(cat);
  if (idx >= 0) window._selectedCats.splice(idx, 1);
  else window._selectedCats.push(cat);
  _updateCatBtn();
  _renderCatList();
  // When categories change, reset subcategories
  window._selectedSubs = [];
  _updateSubcatBtn();
  if (_isMobile()) { _accRenderCat(); _accRenderSub(); _accUpdateSummaries(); }
  filterResults();
}
function _updateCatBtn() {
  const btn = document.getElementById('cat-dd-btn');
  if (window._selectedCats.length === 0) btn.textContent = 'All Categories ▾';
  else if (window._selectedCats.length === 1) btn.textContent = window._selectedCats[0] + ' ▾';
  else btn.textContent = window._selectedCats.length + ' Categories ▾';
}
function _setCatList(categories) {
  window._catList = categories || [];
  document.getElementById('cat-dropdown').style.display = categories && categories.length ? '' : 'none';
  _accUpdateVisibility();
}

// ── Subcategory multi-select dropdown ────────────────────────────────────────
window._selectedSubs = [];
window._subList = [];

function toggleSubcatDropdown() {
  const panel = document.getElementById('subcat-dd-panel');
  if (panel.style.display === 'none') {
    _closeAllDropdowns();
    panel.style.display = '';
    _renderSubList();
    setTimeout(() => document.addEventListener('click', _closeSubOnOutside, true), 0);
  } else { _closeSubDropdown(); }
}
function _closeSubDropdown() {
  document.getElementById('subcat-dd-panel').style.display = 'none';
  document.removeEventListener('click', _closeSubOnOutside, true);
}
function _closeSubOnOutside(e) { if (!e.target.closest('#subcat-dropdown')) _closeSubDropdown(); }

function _renderSubList() {
  const inner = document.getElementById('subcat-dd-inner') || document.getElementById('subcat-dd-panel');
  let html = window._selectedSubs.length > 0
    ? '<div class="dd-clear-row" data-action="clearSub">✕ Clear Subcategory</div>' : '';
  window._subList.forEach(s => {
    const name = (s && s.name !== undefined) ? s.name : s;
    const count = (s && s.count !== undefined) ? s.count : '';
    const isActive = window._selectedSubs.includes(name);
    if (count === 0 && !isActive) return;
    const esc = name.replace(/"/g,'&quot;');
    html += '<div class="cond-dd-item' + (isActive ? ' active' : '') + '" data-val="' + esc + '">'
         + '<span class="cond-dd-check">' + (isActive ? '✓' : '') + '</span>' + name
         + (count !== '' ? '<span class="bcount">' + Number(count).toLocaleString() + '</span>' : '') + '</div>';
  });
  inner.innerHTML = html;
  inner.onclick = function(e) {
    const item = e.target.closest('.cond-dd-item');
    if (!item) return;
    _toggleSub(item.dataset.val);
  };
}
function _toggleSub(sub) {
  const idx = window._selectedSubs.indexOf(sub);
  if (idx >= 0) window._selectedSubs.splice(idx, 1);
  else window._selectedSubs.push(sub);
  _updateSubcatBtn();
  _renderSubList();
  if (_isMobile()) { _accRenderSub(); _accUpdateSummaries(); }
  filterResults();
}
function _updateSubcatBtn() {
  const btn = document.getElementById('subcat-dd-btn');
  if (window._selectedSubs.length === 0) btn.textContent = 'All Subcategories ▾';
  else if (window._selectedSubs.length === 1) btn.textContent = window._selectedSubs[0] + ' ▾';
  else btn.textContent = window._selectedSubs.length + ' Subcategories ▾';
}
function _setSubList(subcategories) {
  window._subList = subcategories || [];
  document.getElementById('subcat-dropdown').style.display = subcategories && subcategories.length ? '' : 'none';
  _accUpdateVisibility();
}

// ── Per-filter clear helpers ──────────────────────────────────────────────────
function _clearBrandFilter() {
  window._selectedBrands = []; _updateBrandBtn(); _renderBrandList();
  if (_isMobile()) { _accRenderBrand(); _accUpdateSummaries(); }
  _srvLoading = false; _srvPage = 1; _fetchBrowsePage(1);
}
function _clearCondFilter() {
  window._selectedConds = []; _updateCondBtn(); _renderCondList();
  if (_isMobile()) { _accRenderCond(); _accUpdateSummaries(); }
  _srvLoading = false; _srvPage = 1; _fetchBrowsePage(1);
}
function _clearCatFilter() {
  window._selectedCats = []; _updateCatBtn(); _renderCatList();
  window._selectedSubs = []; _updateSubcatBtn(); _renderSubList();
  if (_isMobile()) { _accRenderCat(); _accRenderSub(); _accUpdateSummaries(); }
  _srvLoading = false; _srvPage = 1; _fetchBrowsePage(1);
}
function _clearSubFilter() {
  window._selectedSubs = []; _updateSubcatBtn(); _renderSubList();
  if (_isMobile()) { _accRenderSub(); _accUpdateSummaries(); }
  _srvLoading = false; _srvPage = 1; _fetchBrowsePage(1);
}

// ── Results filter ────────────────────────────────────────────────────────────
let _filterTimer = null;
let _kwSearchTimer = null;

function _globalKeywordSearch() {
  // Debounce — wait 400ms after user stops typing
  clearTimeout(_kwSearchTimer);
  // Typing in the search box exits want-list / global-search mode.
  // Without this, _globalSearchActive=true causes _fetchBrowsePage to override
  // filter_q with _globalSearchQuery='' and the typed text is silently ignored.
  if (_globalSearchActive || _wantListSearchActive) {
    _globalSearchActive = false;
    _wantListSearchActive = false;
    _globalSearchQuery = '';
    _resetWantListLink();
  }
  _kwSearchTimer = setTimeout(function() {
    _browseMode = 'server';
    _srvPage = 1;
    // Force-clear the loading guard so a user-initiated search is never silently
    // dropped by an in-flight background browse (page load, filter change, etc.).
    // Every other deliberate action (clearFilters, clearGlobalSearch, scan done)
    // already does this — _globalKeywordSearch was the only exception.
    _srvLoading = false;
    _srvStores = getSelected();
    _fetchBrowsePage(1);
  }, 400);
}

function clearFilters() {
  window._selectedBrands = []; _updateBrandBtn();
  window._selectedConds = []; _updateCondBtn();
  window._selectedCats = []; _updateCatBtn();
  window._selectedSubs = []; _updateSubcatBtn(); _setSubList([]);
  document.getElementById('clear-filters-btn').style.display = 'none';
  _accCloseAll(); _accUpdateSummaries();
  // Clear keyword search box and reset strict mode
  const resSearch = document.getElementById('res-search');
  if (resSearch) { resSearch.value = ''; }
  document.getElementById('res-search-count').textContent = '';
  _updateResSearchClear();
  window._strictSearch = false;
  const _strictBtn = document.getElementById('strict-search-btn');
  if (_strictBtn) { _strictBtn.textContent = '≈'; _strictBtn.classList.remove('active'); _strictBtn.title = 'Whole-word search (default) — click for ≈ fuzzy (contains) mode'; }
  // Also turn off watch/price-drop filters if active
  if (_watchFilterActive) {
    _watchFilterActive = false;
    document.getElementById('watchlist-toggle').classList.remove('wl-active');
  }
  if (_priceDropFilterActive) {
    _priceDropFilterActive = false;
    document.getElementById('price-drop-toggle').classList.remove('wl-active');
  }
  if (_wantListSearchActive) {
    _wantListSearchActive = false;
    document.getElementById('want-list-toggle').classList.remove('wl-active');
  }
  // Clear price range
  window._priceMin = null;
  window._priceMax = null;
  ['price-min', 'price-max', 'price-min-dd', 'price-max-dd'].forEach(function(id) {
    var el = document.getElementById(id); if (el) el.value = '';
  });
  _updatePriceBtn();
  // Hide action button row immediately (will be confirmed by _updateSaveSearchBtn after browse)
  const _fab = document.getElementById('filter-action-btns');
  if (_fab) _fab.style.display = 'none';
  // Bypass debounce — force-clear loading flag and re-fetch immediately
  _srvLoading = false;
  _srvPage = 1;
  _fetchBrowsePage(1);
}

function _updateResSearchClear() {
  const btn = document.getElementById('res-search-clear');
  if (!btn) return;
  const val = (document.getElementById('res-search').value || '').trim();
  btn.style.display = val ? '' : 'none';
}

function clearResSearch() {
  const el = document.getElementById('res-search');
  if (el) el.value = '';
  document.getElementById('res-search-count').textContent = '';
  _updateResSearchClear();
  window._strictSearch = false;
  const _strictBtn = document.getElementById('strict-search-btn');
  if (_strictBtn) { _strictBtn.textContent = '≈'; _strictBtn.classList.remove('active'); _strictBtn.title = 'Whole-word search (default) — click for ≈ fuzzy (contains) mode'; }
  // Clear want-list / global search mode so the ✕ button fully exits those states
  // (without this, _globalSearchActive stays true and re-fetches keep returning
  // want-list-filtered results even though the chip looks inactive)
  _globalSearchActive = false;
  _wantListSearchActive = false;
  _globalSearchQuery = '';
  _resetWantListLink();
  _srvLoading = false;
  _srvPage = 1;
  _fetchBrowsePage(1);
}

function filterResults() {
  _updateFilterDot();
  if (_browseMode === 'server') {
    // Debounce text input, fire immediately for dropdowns
    clearTimeout(_filterTimer);
    _filterTimer = setTimeout(() => {
      _srvPage = 1;
      _srvLoading = false;  // cancel any in-flight request so filter always lands
      _fetchBrowsePage(1);
    }, 250);
    return;
  }
  window._localPage = 1;  // Reset pagination on filter change
  renderTable();
}

// ── Populate store data (one-time migration) ──────────────────────────────────
// populateStoreData is admin-only
function populateStoreData() {}

// validateStores / startValidate are admin-only — use /admin/validate-stores
function cancelValidate() {}
function startValidate() {}


// ── Reset ─────────────────────────────────────────────────────────────────────
async function resetData() {
  if (running) { appendLog('Stop the current run before resetting.', 'log-err'); return; }
  if (!confirm('Reset all inventory data? This preserves your watchlist, want list, and favorites.')) return;
  const r = await fetch('/api/reset', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({})
  });
  const d = await r.json();
  if (!r.ok) {
    appendLog('Reset failed: ' + (d.error || 'unknown error'), 'log-err');
    return;
  }
  appendLog('✓ ' + d.status + (d.deleted.length ? ' Deleted: ' + d.deleted.join(', ') : ''), 'log-dim');
  // Clear per-user inventory tracking state (preserves favorites, watchlist, want list)
  window._newIds = new Set();
  _lsSet('new_ids', []);
  window._lastRunISO = null;
  _lsSet('last_run', null);
  window._lastAnchorISO = null;
  _lsSet('last_anchor', null);
  // Clean up any legacy keys
  try { localStorage.removeItem('gt_prev_snapshot'); localStorage.removeItem('gt_prev_fp_set'); } catch(e) {}
  _updateRelativeTime();
  document.getElementById('check-now-btn').style.display = 'inline'; // Show so user can kick off a new scan
  document.getElementById('s-known').textContent = '0';
}

// ── Log helper ────────────────────────────────────────────────────────────────
function appendLog(text, cls) {
  const log  = document.getElementById('log');
  const line = document.createElement('div');
  if (cls) line.className = cls;
  line.textContent = text;
  log.appendChild(line);
  log.scrollTop = log.scrollHeight;
}

// ── Tab switching ─────────────────────────────────────────────────────────────
function switchTab(tab) {
  document.querySelectorAll('.app-tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.app-panel').forEach(p => p.classList.remove('active'));
  document.querySelector('.' + tab + '-tab').classList.add('active');
  document.getElementById(tab + '-panel').classList.add('active');
}

// ── CL City list ──────────────────────────────────────────────────────────────
const CL_CITIES = [
  {id:'albuquerque',label:'Albuquerque'}, {id:'atlanta',label:'Atlanta'},
  {id:'austin',label:'Austin'},           {id:'baltimore',label:'Baltimore'},
  {id:'boise',label:'Boise'},             {id:'boston',label:'Boston'},
  {id:'buffalo',label:'Buffalo'},         {id:'charlotte',label:'Charlotte'},
  {id:'chicago',label:'Chicago'},         {id:'cincinnati',label:'Cincinnati'},
  {id:'cleveland',label:'Cleveland'},     {id:'columbus',label:'Columbus'},
  {id:'dallas',label:'Dallas'},           {id:'denver',label:'Denver'},
  {id:'desmoines',label:'Des Moines'},    {id:'detroit',label:'Detroit'},
  {id:'elpaso',label:'El Paso'},          {id:'fortworth',label:'Fort Worth'},
  {id:'fresno',label:'Fresno'},           {id:'grandrapids',label:'Grand Rapids'},
  {id:'greensboro',label:'Greensboro'},   {id:'hartford',label:'Hartford'},
  {id:'honolulu',label:'Honolulu'},       {id:'houston',label:'Houston'},
  {id:'indianapolis',label:'Indianapolis'},{id:'jacksonville',label:'Jacksonville'},
  {id:'kansascity',label:'Kansas City'},  {id:'knoxville',label:'Knoxville'},
  {id:'lasvegas',label:'Las Vegas'},      {id:'losangeles',label:'Los Angeles'},
  {id:'louisville',label:'Louisville'},   {id:'madison',label:'Madison'},
  {id:'memphis',label:'Memphis'},         {id:'miami',label:'Miami'},
  {id:'milwaukee',label:'Milwaukee'},     {id:'minneapolis',label:'Minneapolis'},
  {id:'nashville',label:'Nashville'},     {id:'neworleans',label:'New Orleans'},
  {id:'newyork',label:'New York'},        {id:'norfolk',label:'Norfolk'},
  {id:'oklahomacity',label:'Oklahoma City'},{id:'omaha',label:'Omaha'},
  {id:'orlando',label:'Orlando'},         {id:'philadelphia',label:'Philadelphia'},
  {id:'phoenix',label:'Phoenix'},         {id:'pittsburgh',label:'Pittsburgh'},
  {id:'portland',label:'Portland'},       {id:'providence',label:'Providence'},
  {id:'raleigh',label:'Raleigh'},         {id:'richmond',label:'Richmond'},
  {id:'riverside',label:'Riverside'},     {id:'rochester',label:'Rochester'},
  {id:'sacramento',label:'Sacramento'},   {id:'saltlakecity',label:'Salt Lake City'},
  {id:'sanantonio',label:'San Antonio'},  {id:'sandiego',label:'San Diego'},
  {id:'sfbay',label:'SF Bay Area'},       {id:'seattle',label:'Seattle'},
  {id:'spokane',label:'Spokane'},         {id:'stlouis',label:'St. Louis'},
  {id:'syracuse',label:'Syracuse'},       {id:'tampabay',label:'Tampa Bay'},
  {id:'toledo',label:'Toledo'},           {id:'tucson',label:'Tucson'},
  {id:'tulsa',label:'Tulsa'},             {id:'virginiabeach',label:'Virginia Beach'},
  {id:'washingtondc',label:'Washington DC'},{id:'wichita',label:'Wichita'},
];

let _clFavs = [];
try { _clFavs = JSON.parse(localStorage.getItem('cl_favs') || '[]'); } catch(e) {}
let _clFavsOnly = false;
let _clData = [];
let _clSortCol = null, _clSortDir = 1;

function clSaveFavs() {
  try { localStorage.setItem('cl_favs', JSON.stringify(_clFavs)); } catch(e) {}
}

function clRenderCities(selectAll) {
  const q   = (document.getElementById('cl-city-search').value || '').toLowerCase();
  const list = document.getElementById('cl-city-list');
  const cities = _clFavsOnly
    ? CL_CITIES.filter(c => _clFavs.includes(c.id))
    : (q ? CL_CITIES.filter(c => c.label.toLowerCase().includes(q)) : CL_CITIES);

  list.innerHTML = '';
  cities.forEach(c => {
    const isFav = _clFavs.includes(c.id);
    const div = document.createElement('div');
    div.className = 'cl-city-row';
    const cbId = 'cl_cb_' + c.id;
    const cb  = document.createElement('input');
    cb.type = 'checkbox'; cb.id = cbId; cb.value = c.id;
    if (selectAll) cb.checked = true;
    cb.addEventListener('change', function() { _updateMobileToggleCounts(); clFilterResults(); });
    const lbl = document.createElement('label');
    lbl.htmlFor = cbId; lbl.textContent = c.label;
    const btn = document.createElement('button');
    btn.className = 'cl-fav-btn' + (isFav ? ' active' : '');
    btn.title = (isFav ? 'Remove from' : 'Add to') + ' favorites';
    btn.textContent = '★';
    btn.dataset.cityId = c.id;
    btn.addEventListener('click', function(e) {
      e.stopPropagation();
      clToggleFav(c.id, this);
    });
    div.appendChild(cb);
    div.appendChild(lbl);
    div.appendChild(btn);
    list.appendChild(div);
  });
}

function clFilterCities() { clRenderCities(); }

function clToggleFavs() {
  _clFavsOnly = !_clFavsOnly;
  document.getElementById('cl-favs-btn').classList.toggle('active', _clFavsOnly);
  document.getElementById('cl-city-search').value = '';
  clRenderCities();
  clFilterResults();  // Also filter results to show only favorites
}

function clToggleFav(id, btn) {
  if (_clFavs.includes(id)) {
    _clFavs = _clFavs.filter(f => f !== id);
    btn.classList.remove('active');
  } else {
    _clFavs.push(id);
    btn.classList.add('active');
  }
  clSaveFavs();
  if (_clFavsOnly) clRenderCities();
}

function clSelectAll() {
  document.querySelectorAll('#cl-city-list input[type=checkbox]').forEach(cb => cb.checked = true);
  _updateMobileToggleCounts();
  clFilterResults();
}
function clClearAll() {
  document.querySelectorAll('#cl-city-list input[type=checkbox]').forEach(cb => cb.checked = false);
  _updateMobileToggleCounts();
  clFilterResults();
}

function clGetSelected() {
  return [...document.querySelectorAll('#cl-city-list input[type=checkbox]:checked')].map(cb => cb.value);
}

// ── CL Search ─────────────────────────────────────────────────────────────────
async function clSearch() {
  const q = document.getElementById('cl-query').value.trim();
  if (!q) return;
  const selected = clGetSelected();
  const btn = document.getElementById('cl-search-btn');
  const status = document.getElementById('cl-status');
  btn.disabled = true;
  btn.textContent = 'Searching…';
  const cityCount = selected.length || CL_CITIES.length;
  status.textContent = 'Searching ' + cityCount + ' markets…';
  document.getElementById('cl-results-hdr').style.display = 'none';
  document.getElementById('cl-body').innerHTML = '<div class="cl-empty">Searching…</div>';
  try {
    const cities = selected.length ? selected.join(',') : '';
    const r = await fetch('/api/cl-search?q=' + encodeURIComponent(q) + (cities ? '&cities=' + encodeURIComponent(cities) : ''));
    if (!r.ok) {
      const text = await r.text();
      document.getElementById('cl-body').innerHTML = '<div class="cl-empty" style="color:#f88">Search failed (HTTP ' + r.status + '). Try selecting fewer cities.</div>';
      return;
    }
    let d;
    try {
      d = await r.json();
    } catch(parseErr) {
      document.getElementById('cl-body').innerHTML = '<div class="cl-empty" style="color:#f88">Search failed — server returned an invalid response. This can happen if the request timed out. Try selecting fewer cities.</div>';
      return;
    }
    if (d.error) {
      document.getElementById('cl-body').innerHTML = '<div class="cl-empty" style="color:#f88">' + d.error + '</div>';
      return;
    }
    _clData = d.results || [];
    // Filter results: all words must match (AND), or exact phrase if quoted
    const rawQ = q.trim();
    if (rawQ) {
      let matchFn;
      if (rawQ.startsWith('"') && rawQ.endsWith('"') && rawQ.length > 2) {
        // Exact phrase match
        const phrase = rawQ.slice(1, -1).toLowerCase();
        matchFn = item => (item.title || '').toLowerCase().includes(phrase);
      } else {
        // All words must be present (AND)
        const words = rawQ.toLowerCase().split(/\s+/).filter(Boolean);
        matchFn = item => {
          const t = (item.title || '').toLowerCase();
          return words.every(w => t.includes(w));
        };
      }
      _clData = _clData.filter(matchFn);
    }
    status.textContent = '';
    clRenderResults();
  } catch(e) {
    document.getElementById('cl-body').innerHTML = '<div class="cl-empty" style="color:#f88">Search failed: ' + e.message + '</div>';
  } finally {
    btn.disabled = false;
    btn.textContent = 'Search';
  }
}

function clFilterResults() {
  const q = (document.getElementById('cl-res-search').value || '').toLowerCase();
  const selectedCities = new Set(clGetSelected());
  const rows = document.querySelectorAll('#cl-body tbody tr');
  let visible = 0;
  rows.forEach(row => {
    const textMatch = !q || row.textContent.toLowerCase().includes(q);
    const favMatch = !_clFavsOnly || _clFavs.includes(row.dataset.city || '');
    const cityMatch = selectedCities.size === 0 || selectedCities.has(row.dataset.city || '');
    const watchMatch = !_clWatchFilterActive || !!(window._clWatchlist || {})[row.dataset.clId || ''];
    const wantMatch = !_clWantListFilterActive || _clMatchesWantList(row.querySelector('td:nth-child(3)') ? row.querySelector('td:nth-child(3)').textContent : '');
    const show = textMatch && favMatch && cityMatch && watchMatch && wantMatch;
    row.style.display = show ? '' : 'none';
    if (show) visible++;
  });
  document.getElementById('cl-count').textContent =
    (q || _clFavsOnly || _clWatchFilterActive || _clWantListFilterActive || selectedCities.size < _clData.length) ? (visible + ' of ' + _clData.length + ' listings') : (_clData.length + ' listings');
}

function _clMatchesWantList(title) {
  if (!window._keywords || !window._keywords.length) return false;
  const text = (title || '').toLowerCase();
  return window._keywords.some(kw => {
    kw = kw.trim();
    if (kw.startsWith('"') && kw.endsWith('"') && kw.length > 2) {
      return text.includes(kw.slice(1, -1).toLowerCase());
    } else if (kw.includes(',')) {
      return kw.split(',').map(t => t.trim().toLowerCase()).filter(Boolean).every(t => text.includes(t));
    } else {
      return text.includes(kw.toLowerCase());
    }
  });
}

function clRenderResults() {
  const hdr  = document.getElementById('cl-results-hdr');
  const body = document.getElementById('cl-body');
  if (!_clData.length) {
    body.innerHTML = '<div class="cl-empty">No listings found. Try a different search term or select more cities.</div>';
    hdr.style.display = 'none';
    return;
  }
  document.getElementById('cl-count').textContent = _clData.length + ' listings';
  document.getElementById('cl-res-search').value = '';
  hdr.style.display = 'flex';

  const cols = _clCols;
  const labels = ['','Want','Item','Price','Location','Date'];
  let html = '<table><thead><tr>';
  labels.forEach((l, i) => {
    if (i === 0) { html += '<th style="width:30px"></th>'; return; }
    if (i === 1) { html += '<th style="width:62px;text-align:center">Want</th>'; return; }
    const sortIdx = i - 2;
    const cls = _clSortCol === sortIdx ? (_clSortDir === 1 ? 'sort-asc' : 'sort-desc') : '';
    html += '<th class="' + cls + '" data-action="clSort" data-idx="' + sortIdx + '">' + l + '</th>';
  });
  html += '</tr></thead><tbody>';

  // Favorites first, then rest — within each group, sort by selected col
  const isFavResult = r => _clFavs.includes(r.cityId);

  // Relevance scoring based on current search query
  const rawQuery = (document.getElementById('cl-query').value || '').trim().toLowerCase();
  const queryWords = rawQuery.split(/[ \t]+/).filter(Boolean);
  function relevanceScore(title) {
    const t = (title || '').toLowerCase();
    if (!rawQuery) return 0;
    if (t.includes(rawQuery)) return 3;          // exact phrase
    if (queryWords.every(w => t.includes(w))) return 2;  // all words
    if (queryWords.some(w => t.includes(w))) return 1;   // some words
    return 0;
  }

  let sorted = [..._clData];
  if (_clSortCol !== null) {
    const key = cols[_clSortCol];
    sorted.sort((a, b) => {
      if (key === 'relevance') {
        // For relevance, desc = most relevant first (flip _clSortDir meaning)
        return _clSortDir * (relevanceScore(b.title) - relevanceScore(a.title));
      }
      const av = a[key] || '', bv = b[key] || '';
      if (key === 'price') {
        return _clSortDir * ((parseFloat(String(av).replace(/[^0-9.]/g,'')) || 0) -
                             (parseFloat(String(bv).replace(/[^0-9.]/g,'')) || 0));
      }
      return _clSortDir * String(av).localeCompare(String(bv));
    });
  }

  // Favorites float to top only when no sort is active
  let final;
  if (_clSortCol === null) {
    // Sort by relevance within each tier
    const score = r => relevanceScore(r.title);
    const favResults  = sorted.filter(r =>  isFavResult(r)).sort((a,b) => score(b)-score(a));
    const restResults = sorted.filter(r => !isFavResult(r)).sort((a,b) => score(b)-score(a));
    final = [...favResults, ...restResults];
  } else {
    final = sorted;
  }

  final.forEach(r => {
    const isFav = isFavResult(r);
    const star  = isFav ? '<span class="cl-fav-star">★</span>' : '';
    const clId  = 'cl:' + (r.url || r.title || '');
    const isWatched = (window._clWatchlist || {})[clId];
    const watchStar = `<button class="watch-btn ${isWatched ? 'active' : ''}" data-action="clToggleWatch" data-id="${_ssEsc(clId)}" data-title="${_ssEsc(r.title||'')}" data-url="${_ssEsc(_safeHttpUrl(r.url))}" data-price="${_ssEsc(r.price||'')}" data-location="${_ssEsc(r.location||'')}" title="${isWatched ? 'Remove from' : 'Add to'} watch list">${isWatched ? '★' : '☆'}</button>`;
    const wantMatch = _clMatchesWantList(r.title || '');
    const safeClUrl = _safeHttpUrl(r.url);
    const title = safeClUrl
      ? star + '<a href="' + _ssEsc(safeClUrl) + '" target="_blank" rel="noopener">' + _ssEsc(r.title || '(no title)') + '</a>'
      : star + _ssEsc(r.title || '(no title)');
    html += '<tr class="' + (isFav ? 'cl-fav-result' : '') + '" data-city="' + _ssEsc(r.cityId||'') + '" data-cl-id="' + _ssEsc(clId) + '" data-cl-image="' + _ssEsc(_safeHttpUrl(r.image)) + '">' +
            '<td style="text-align:center">' + watchStar + '</td>' +
            '<td style="text-align:center">' + (wantMatch ? '<span class="tag-kw">WANT</span>' : '') + '</td>' +
            '<td title="' + _ssEsc(r.title||'') + '">' + title + '</td>' +
            '<td>' + _ssEsc(r.price||'') + '</td>' +
            '<td>' + _ssEsc(r.location||'') + '</td>' +
            '<td>' + _ssEsc(r.date||'') + '</td></tr>';
  });
  html += '</tbody></table>';
  body.innerHTML = html;
}

const _clCols = ['title','price','location','date','relevance'];
function clSort(col) {
  const isRelevance = _clCols[col] === 'relevance';
  if (isRelevance && _clSortCol === col) {
    _clSortCol = null; _clSortDir = 1;
  } else if (_clSortCol === col) {
    _clSortDir *= -1;
  } else {
    _clSortCol = col; _clSortDir = 1;
  }
  clRenderResults();
}

let _clWatchFilterActive = false;
let _clWantListFilterActive = false;

async function clSearchWantList() {
  if (_clWantListFilterActive) {
    _clWantListFilterActive = false;
    document.getElementById('cl-search-wl-link').textContent = 'Search Want List';
    document.getElementById('cl-search-wl-link').style.color = '#4ade80';
    clFilterResults();
    return;
  }
  if (!window._keywords || !window._keywords.length) {
    openKeywords();
    return;
  }
  // Actually search CL for each want list keyword across all cities
  const btn = document.getElementById('cl-search-wl-link');
  const status = document.getElementById('cl-status');
  btn.textContent = 'Searching…';
  btn.style.color = '#ffbb33';
  status.textContent = 'Searching want list across all markets…';
  document.getElementById('cl-results-hdr').style.display = 'none';
  document.getElementById('cl-body').innerHTML = '<div class="cl-empty">Searching want list…</div>';
  try {
    const allResults = [];
    const seenKeys = new Set();
    for (const kw of window._keywords) {
      // Strip quotes from keyword for search
      let q = kw.trim();
      if (q.startsWith('"') && q.endsWith('"') && q.length > 2) q = q.slice(1, -1);
      if (!q) continue;
      try {
        const r = await fetch('/api/cl-search?q=' + encodeURIComponent(q) + '&title_only=1');
        if (r.ok) {
          const d = await r.json();
          const results = d.results || [];
          for (const item of results) {
            const key = (item.title || '').toLowerCase().trim() + '|' + (item.price || '') + '|' + (item.cityId || '');
            if (!seenKeys.has(key)) {
              seenKeys.add(key);
              allResults.push(item);
            }
          }
        }
      } catch(e) { /* skip failed keyword */ }
      status.textContent = 'Searched "' + q + '"… (' + allResults.length + ' results so far)';
    }
    _clData = allResults;
    _clData.sort((a, b) => (b.date || '').localeCompare(a.date || ''));
    _clWantListFilterActive = true;
    btn.textContent = 'Clear Want List Search';
    btn.style.color = '#f88';
    status.textContent = '';
    clRenderResults();
  } catch(e) {
    document.getElementById('cl-body').innerHTML = '<div class="cl-empty" style="color:#f88">Want list search failed: ' + e.message + '</div>';
    btn.textContent = 'Search Want List';
    btn.style.color = '#4ade80';
    status.textContent = '';
  }
}

function clToggleWatchFilter() {
  _clWatchFilterActive = !_clWatchFilterActive;
  const btn = document.getElementById('cl-watchlist-toggle');
  btn.classList.toggle('wl-active', _clWatchFilterActive);
  clFilterResults();
}

function clToggleWatch(id, name, url, price, location, btn) {
  const isWatched = !!(window._clWatchlist[id]);
  if (isWatched) {
    delete window._clWatchlist[id];
  } else {
    window._clWatchlist[id] = {
      name: name, url: url, store: location, price: price,
      date_added: new Date().toISOString().slice(0,10),
    };
  }
  _lsSet('cl_watchlist', window._clWatchlist);
  btn.classList.toggle('active', !isWatched);
  btn.textContent = isWatched ? '☆' : '★';
  btn.title = isWatched ? 'Add to watch list' : 'Remove from watch list';
}

// ── Phase 3: inline event handler wiring ─────────────────────────────────────
// All formerly-inline onclick/oninput/onkeydown attributes have been removed
// from the HTML templates and are wired here via addEventListener.
// ── Global delegated click handler for dynamically-rendered elements ─────────
// Handles onclick that can't be inline due to CSP script-src without 'unsafe-inline'
document.addEventListener('click', function(e) {
  const el = e.target.closest('[data-action]');
  if (!el) return;
  const action = el.dataset.action;
  if (action === 'toggleWatch') {
    toggleWatch(el.dataset.id, el);
  } else if (action === 'goToPage') {
    goToPage(parseInt(el.dataset.page, 10));
  } else if (action === 'removeKeyword') {
    removeKeywordAt(parseInt(el.dataset.idx, 10));
  } else if (action === 'clearBrand') {
    _clearBrandFilter();
  } else if (action === 'clearCond') {
    _clearCondFilter();
  } else if (action === 'clearCat') {
    _clearCatFilter();
  } else if (action === 'clearSub') {
    _clearSubFilter();
  } else if (action === 'clSort') {
    clSort(parseInt(el.dataset.idx, 10));
  } else if (action === 'clToggleWatch') {
    clToggleWatch(el.dataset.id, el.dataset.title, el.dataset.url, el.dataset.price, el.dataset.location, el);
  }
});

document.addEventListener('DOMContentLoaded', function() {

  // Validate stores modal
  document.getElementById('vs-backdrop')?.addEventListener('click', cancelValidate);
  document.getElementById('vs-cancel-btn')?.addEventListener('click', cancelValidate);
  document.getElementById('vs-no-btn')?.addEventListener('click', function() { startValidate(false); });
  document.getElementById('vs-yes-btn')?.addEventListener('click', function() { startValidate(true); });

  // First-run / welcome modal
  document.getElementById('first-run-backdrop')?.addEventListener('click', dismissFirstRun);
  document.getElementById('welcome-tab-login')?.addEventListener('click', function() { _welcomeTab('login'); });
  document.getElementById('welcome-tab-register')?.addEventListener('click', function() { _welcomeTab('register'); });
  document.querySelectorAll('.auth-google-btn').forEach(function(btn) {
    btn.addEventListener('click', function() { _googleSignIn('/'); });
  });
  document.getElementById('welcome-login-submit')?.addEventListener('click', _welcomeLogin);
  document.getElementById('welcome-register-submit')?.addEventListener('click', _welcomeRegister);
  document.getElementById('first-run-guest-btn')?.addEventListener('click', dismissFirstRun);

  // Keywords / want-list modal
  document.getElementById('kw-modal-backdrop')?.addEventListener('click', closeKeywords);
  document.getElementById('kw-input')?.addEventListener('keydown', function(e) { if (e.key === 'Enter') addKeyword(); });
  document.getElementById('kw-add-btn')?.addEventListener('click', addKeyword);
  document.getElementById('kw-clear-btn')?.addEventListener('click', clearAllKeywords);
  document.getElementById('kw-done-btn')?.addEventListener('click', closeKeywords);

  // Auth modal
  document.querySelector('.auth-close')?.addEventListener('click', _closeAuthModal);
  document.getElementById('auth-tab-login')?.addEventListener('click', function() { _switchAuthTab('login'); });
  document.getElementById('auth-tab-register')?.addEventListener('click', function() { _switchAuthTab('register'); });
  document.getElementById('auth-login-submit')?.addEventListener('click', _authLogin);
  document.getElementById('auth-register-submit')?.addEventListener('click', _authRegister);

  // Auth header
  document.getElementById('stop-btn')?.addEventListener('click', stopRun);
  document.getElementById('auth-logout-btn')?.addEventListener('click', _authLogout);
  document.getElementById('auth-login-btn')?.addEventListener('click', function() { _openAuthModal('login'); });

  // Google welcome modal
  document.querySelector('.gw-backdrop')?.addEventListener('click', _gwSkip);
  document.getElementById('gw-username')?.addEventListener('input', _gwClearImport);
  document.getElementById('gw-import-toggle')?.addEventListener('click', _gwToggleImport);
  document.getElementById('gw-submit')?.addEventListener('click', _gwSubmit);
  document.getElementById('gw-skip-btn')?.addEventListener('click', _gwSkip);

  // Google link banner
  document.querySelector('.glib-link')?.addEventListener('click', _glinkStart);
  document.querySelector('.glib-dismiss')?.addEventListener('click', _glinkDismiss);

  // Mobile title bar about button
  document.querySelector('.mtb-about')?.addEventListener('click', _openAboutModal);

  // Sidebar / layout
  document.getElementById('sidebar-collapse-btn')?.addEventListener('click', toggleDesktopSidebar);
  document.getElementById('gc-sidebar-toggle')?.addEventListener('click', function() { toggleMobileSidebar('gc'); });

  // Store panel
  document.getElementById('favs-btn')?.addEventListener('click', toggleFavsFilter);
  document.getElementById('sel-all-btn')?.addEventListener('click', toggleSelectAll);
  document.getElementById('zip-sort-btn')?.addEventListener('click', toggleZipSort);
  const zipInput = document.getElementById('zip-input');
  if (zipInput) {
    zipInput.addEventListener('input', function() { this.value = this.value.replace(/\D/g, ''); });
    zipInput.addEventListener('keydown', function(e) { if (e.key === 'Enter') applyZipSort(); });
  }

  // Status bar
  document.getElementById('check-now-btn')?.addEventListener('click', runTracker);
  document.getElementById('view-toggle-btn')?.addEventListener('click', toggleMobileView);
  document.getElementById('s-want-match')?.addEventListener('click', searchWantList);

  // Quick filter chips
  document.getElementById('price-drop-toggle')?.addEventListener('click', togglePriceDropFilter);
  document.getElementById('saved-searches-btn')?.addEventListener('click', _toggleSavedSearchesDropdown);
  document.getElementById('watchlist-toggle')?.addEventListener('click', toggleWatchFilter);
  document.getElementById('want-list-toggle')?.addEventListener('click', searchWantList);
  document.getElementById('search-wl-link')?.addEventListener('click', openKeywords);
  document.getElementById('view-toggle-chip')?.addEventListener('click', toggleMobileView);
  document.getElementById('desktop-thumb-toggle')?.addEventListener('click', toggleDesktopThumbView);

  // Filter sheet
  document.getElementById('gc-filter-toggle')?.addEventListener('click', function() { toggleMobileFilters('gc'); });
  document.getElementById('filter-clear-all-btn')?.addEventListener('click', clearFilters);
  const resSearch = document.getElementById('res-search');
  if (resSearch) resSearch.addEventListener('input', function() { _globalKeywordSearch(); _updateResSearchClear(); });
  document.getElementById('res-search-clear')?.addEventListener('click', clearResSearch);
  document.getElementById('search-info-btn')?.addEventListener('click', function(e) { _toggleSearchInfo(e); });

  // Accordion headers via data-acc attribute
  document.querySelectorAll('.acc-header[data-acc]').forEach(function(btn) {
    btn.addEventListener('click', function() { _accToggle(this.dataset.acc); });
  });
  document.getElementById('acc-brand-search')?.addEventListener('input', function() { _accRenderBrand(this.value); });

  // Filter dropdowns
  document.getElementById('brand-dd-btn')?.addEventListener('click', toggleBrandDropdown);
  document.getElementById('brand-dd-search')?.addEventListener('input', filterBrandDropdown);
  document.getElementById('cond-dd-btn')?.addEventListener('click', toggleCondDropdown);
  document.getElementById('cat-dd-btn')?.addEventListener('click', toggleCatDropdown);
  document.getElementById('subcat-dd-btn')?.addEventListener('click', toggleSubcatDropdown);

  // Price range — desktop dropdown
  document.getElementById('price-dd-btn')?.addEventListener('click', togglePriceDropdown);
  document.getElementById('price-dd-clear')?.addEventListener('click', _clearPriceFilter);

  // Price inputs — debounced, syncs mobile ↔ desktop, triggers browse
  function _onPriceInput(isDesktop) {
    clearTimeout(_priceTimer);
    _priceTimer = setTimeout(function() {
      var minEl = document.getElementById(isDesktop ? 'price-min-dd' : 'price-min');
      var maxEl = document.getElementById(isDesktop ? 'price-max-dd' : 'price-max');
      var minVal = minEl ? minEl.value : '';
      var maxVal = maxEl ? maxEl.value : '';
      window._priceMin = minVal !== '' ? parseFloat(minVal) : null;
      window._priceMax = maxVal !== '' ? parseFloat(maxVal) : null;
      // Sync the other set of inputs
      var oMinEl = document.getElementById(isDesktop ? 'price-min' : 'price-min-dd');
      var oMaxEl = document.getElementById(isDesktop ? 'price-max' : 'price-max-dd');
      if (oMinEl) oMinEl.value = minVal;
      if (oMaxEl) oMaxEl.value = maxVal;
      _updatePriceBtn();
      _updateFilterDot();
      _srvPage = 1;
      _fetchBrowsePage(1);
    }, 400);
  }
  document.getElementById('price-min-dd')?.addEventListener('input', function() { _onPriceInput(true); });
  document.getElementById('price-max-dd')?.addEventListener('input', function() { _onPriceInput(true); });
  document.getElementById('price-min')?.addEventListener('input', function() { _onPriceInput(false); });
  document.getElementById('price-max')?.addEventListener('input', function() { _onPriceInput(false); });

  document.getElementById('save-search-btn')?.addEventListener('click', _saveCurrentSearch);
  document.getElementById('clear-filters-btn')?.addEventListener('click', clearFilters);
  document.querySelector('.filter-done-btn')?.addEventListener('click', _closeAllSheets);

  // CL stub elements in main page
  document.getElementById('cl-city-search')?.addEventListener('input', clFilterCities);
  document.getElementById('cl-favs-btn')?.addEventListener('click', clToggleFavs);
  document.getElementById('cl-select-all-btn')?.addEventListener('click', clSelectAll);
  document.getElementById('cl-clear-all-btn')?.addEventListener('click', clClearAll);
  document.getElementById('cl-query')?.addEventListener('keydown', function(e) { if (e.key === 'Enter') clSearch(); });
  document.getElementById('cl-search-btn')?.addEventListener('click', clSearch);
  document.getElementById('cl-watchlist-toggle')?.addEventListener('click', clToggleWatchFilter);
  document.getElementById('cl-stub-open-kw-btn')?.addEventListener('click', openKeywords);
  const clWlLink = document.getElementById('cl-search-wl-link');
  if (clWlLink) {
    clWlLink.addEventListener('click', clSearchWantList);
    clWlLink.addEventListener('mouseover', function() { this.style.textDecoration = 'underline'; });
    clWlLink.addEventListener('mouseout',  function() { this.style.textDecoration = 'none'; });
  }
  document.getElementById('cl-res-search')?.addEventListener('input', clFilterResults);

  // Store sheet backdrop + mobile bottom bar
  document.getElementById('store-sheet-backdrop')?.addEventListener('click', _closeStoreSheet);
  document.getElementById('mbb-check')?.addEventListener('click', _mbbCheck);
  document.getElementById('mbb-filters')?.addEventListener('click', _mbbFilters);
  document.getElementById('mbb-stores')?.addEventListener('click', _mbbStores);
  document.getElementById('mbb-auth')?.addEventListener('click', _mobileAuthToggle);

  // About modal
  document.getElementById('about-modal')?.addEventListener('click', function(e) {
    if (e.target === this) _closeAboutModal();
  });
  document.querySelector('.about-close-btn')?.addEventListener('click', _closeAboutModal);
  document.querySelector('[data-action="open-about"]')?.addEventListener('click', function(e) {
    e.preventDefault();
    _openAboutModal();
  });
});

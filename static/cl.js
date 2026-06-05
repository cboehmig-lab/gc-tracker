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

// ── HTML-escape helper ────────────────────────────────────────────────────────
// Craigslist titles/prices/locations are scraped from listings that ANYONE on the
// internet can post, so every such value must be escaped before it touches innerHTML.
function _clEsc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
// Only allow http(s) links — blocks javascript:/data: URIs in scraped post URLs.
function _clSafeUrl(u) {
  u = String(u == null ? '' : u);
  return /^https?:\/\//i.test(u) ? u : '';
}

// ── localStorage helpers ──────────────────────────────────────────────────────
function _lsGet(key, fallback) {
  try { const v = localStorage.getItem('gt_' + key); return v ? JSON.parse(v) : fallback; }
  catch(e) { return fallback; }
}
function _lsSet(key, val) {
  try { localStorage.setItem('gt_' + key, JSON.stringify(val)); } catch(e) {}
}

// ── Auth ──────────────────────────────────────────────────────────────────────
async function clDoLogin() {
  const user = document.getElementById('auth-user').value.trim();
  const pw   = document.getElementById('auth-pw').value;
  const err  = document.getElementById('auth-err');
  err.style.display = 'none';
  if (!user || !pw) { err.textContent = 'Please fill in both fields.'; err.style.display = 'block'; return; }
  const btn = document.querySelector('#auth-modal .auth-submit');
  btn.disabled = true; btn.textContent = 'Signing in…';
  try {
    const r = await fetch('/api/login', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:user,password:pw})});
    const d = await r.json();
    if (d.success) {
      document.getElementById('auth-modal').classList.remove('open');
      document.getElementById('hdr-user').textContent = user;
      document.getElementById('hdr-signout').style.display = '';
      window._keywords = _lsGet('keywords', []);
      window._clWatchlist = _lsGet('cl_watchlist', {});
    } else {
      err.textContent = d.error || 'Login failed.'; err.style.display = 'block';
    }
  } catch(e) { err.textContent = 'Network error.'; err.style.display = 'block'; }
  btn.disabled = false; btn.textContent = 'Sign In';
}

async function clSignOut() {
  await fetch('/api/logout', {method:'POST'});
  document.getElementById('hdr-user').textContent = '';
  document.getElementById('hdr-signout').style.display = 'none';
  document.getElementById('auth-modal').classList.add('open');
}

// ── Init ──────────────────────────────────────────────────────────────────────
window._keywords    = [];
window._clWatchlist = {};

document.addEventListener('DOMContentLoaded', async () => {
  window._keywords    = _lsGet('keywords', []);
  window._clWatchlist = _lsGet('cl_watchlist', {});
  let _clFavsStore = [];
  try { _clFavsStore = JSON.parse(localStorage.getItem('cl_favs') || '[]'); } catch(e) {}
  _clFavs = _clFavsStore;

  clRenderCities(true);

  try {
    const r = await fetch('/api/me');
    const d = await r.json();
    if (d.logged_in) {
      document.getElementById('auth-modal').classList.remove('open');
      document.getElementById('hdr-user').textContent = d.username;
      document.getElementById('hdr-signout').style.display = '';
    }
  } catch(e) {}

  // Show Google sign-in button if configured
  try {
    const cr = await fetch('/api/auth/config');
    const cd = await cr.json();
    if (cd.google_oauth) {
      const el = document.getElementById('cl-google-wrap');
      if (el) el.style.display = '';
    }
  } catch(e) {}
});

// ── City list ─────────────────────────────────────────────────────────────────
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
let _clFavsOnly = false;
let _clData = [];
let _clSortCol = null, _clSortDir = 1;
const _clCols = ['title','price','location','date','relevance'];

function clSaveFavs() {
  try { localStorage.setItem('cl_favs', JSON.stringify(_clFavs)); } catch(e) {}
}

function clRenderCities(selectAll) {
  const q = (document.getElementById('cl-city-search').value || '').toLowerCase();
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
    const cb = document.createElement('input');
    cb.type = 'checkbox'; cb.id = cbId; cb.value = c.id;
    if (selectAll) cb.checked = true;
    const lbl = document.createElement('label');
    lbl.htmlFor = cbId; lbl.textContent = c.label;
    const btn = document.createElement('button');
    btn.className = 'cl-fav-btn' + (isFav ? ' active' : '');
    btn.title = (isFav ? 'Remove from' : 'Add to') + ' favorites';
    btn.textContent = '★';
    btn.addEventListener('click', e => { e.stopPropagation(); clToggleFav(c.id, btn); });
    div.appendChild(cb); div.appendChild(lbl); div.appendChild(btn);
    list.appendChild(div);
  });
}

function clFilterCities() { clRenderCities(); }
function clSelectAll() { document.querySelectorAll('#cl-city-list input[type=checkbox]').forEach(cb => cb.checked = true); }
function clClearAll()  { document.querySelectorAll('#cl-city-list input[type=checkbox]').forEach(cb => cb.checked = false); }
function clGetSelected() { return [...document.querySelectorAll('#cl-city-list input[type=checkbox]:checked')].map(cb => cb.value); }

function clToggleFavs() {
  _clFavsOnly = !_clFavsOnly;
  document.getElementById('cl-favs-btn').classList.toggle('active', _clFavsOnly);
  document.getElementById('cl-city-search').value = '';
  clRenderCities();
}

function clToggleFav(id, btn) {
  if (_clFavs.includes(id)) { _clFavs = _clFavs.filter(f => f !== id); btn.classList.remove('active'); }
  else { _clFavs.push(id); btn.classList.add('active'); }
  clSaveFavs();
  if (_clFavsOnly) clRenderCities();
}

// ── Search ────────────────────────────────────────────────────────────────────
async function clSearch() {
  const q = document.getElementById('cl-query').value.trim();
  if (!q) return;
  const selected = clGetSelected();
  const btn = document.getElementById('cl-search-btn');
  const status = document.getElementById('cl-status');
  btn.disabled = true; btn.textContent = 'Searching…';
  const cityCount = selected.length || CL_CITIES.length;
  status.textContent = 'Searching ' + cityCount + ' markets…';
  document.getElementById('cl-results-hdr').style.display = 'none';
  document.getElementById('cl-body').innerHTML = '<div class="cl-empty">Searching…</div>';
  try {
    const cities = selected.length ? selected.join(',') : '';
    const r = await fetch('/api/cl-search?q=' + encodeURIComponent(q) + (cities ? '&cities=' + encodeURIComponent(cities) : ''));
    if (!r.ok) { document.getElementById('cl-body').innerHTML = '<div class="cl-empty" style="color:#f88">Search failed (HTTP ' + r.status + '). Try selecting fewer cities.</div>'; return; }
    let d;
    try { d = await r.json(); }
    catch(e) { document.getElementById('cl-body').innerHTML = '<div class="cl-empty" style="color:#f88">Server returned an invalid response — the request may have timed out. Try fewer cities.</div>'; return; }
    if (d.error) { document.getElementById('cl-body').innerHTML = '<div class="cl-empty" style="color:#f88">' + d.error + '</div>'; return; }
    _clData = d.results || [];
    const rawQ = q.trim();
    if (rawQ) {
      let matchFn;
      if (rawQ.startsWith('"') && rawQ.endsWith('"') && rawQ.length > 2) {
        const phrase = rawQ.slice(1,-1).toLowerCase();
        matchFn = item => (item.title||'').toLowerCase().includes(phrase);
      } else {
        const words = rawQ.toLowerCase().split(/\\s+/).filter(Boolean);
        matchFn = item => { const t=(item.title||'').toLowerCase(); return words.every(w=>t.includes(w)); };
      }
      _clData = _clData.filter(matchFn);
    }
    status.textContent = '';
    clRenderResults();
  } catch(e) {
    document.getElementById('cl-body').innerHTML = '<div class="cl-empty" style="color:#f88">Search failed: ' + e.message + '</div>';
  } finally { btn.disabled = false; btn.textContent = 'Search'; }
}

function clFilterResults() {
  const q = (document.getElementById('cl-res-search').value || '').toLowerCase();
  const selectedCities = new Set(clGetSelected());
  const rows = document.querySelectorAll('#cl-body tbody tr');
  let visible = 0;
  rows.forEach(row => {
    const textMatch  = !q || row.textContent.toLowerCase().includes(q);
    const favMatch   = !_clFavsOnly || _clFavs.includes(row.dataset.city || '');
    const watchMatch = !_clWatchFilterActive || !!(window._clWatchlist||{})[row.dataset.clId||''];
    const wantMatch  = !_clWantListFilterActive || _clMatchesWantList(row.querySelector('td:nth-child(3)')?.textContent || '');
    const show = textMatch && favMatch && watchMatch && wantMatch;
    row.style.display = show ? '' : 'none';
    if (show) visible++;
  });
  document.getElementById('cl-count').textContent =
    (q||_clFavsOnly||_clWatchFilterActive||_clWantListFilterActive) ? (visible+' of '+_clData.length+' listings') : (_clData.length+' listings');
}

function _clMatchesWantList(title) {
  if (!window._keywords || !window._keywords.length) return false;
  const text = (title||'').toLowerCase();
  return window._keywords.some(kw => {
    kw = kw.trim();
    if (kw.startsWith('"') && kw.endsWith('"') && kw.length>2) return text.includes(kw.slice(1,-1).toLowerCase());
    if (kw.includes(',')) return kw.split(',').map(t=>t.trim().toLowerCase()).filter(Boolean).every(t=>text.includes(t));
    return text.includes(kw.toLowerCase());
  });
}

function clRenderResults() {
  const hdr  = document.getElementById('cl-results-hdr');
  const body = document.getElementById('cl-body');
  if (!_clData.length) {
    body.innerHTML = '<div class="cl-empty">No listings found. Try a different search or select more cities.</div>';
    hdr.style.display = 'none'; return;
  }
  document.getElementById('cl-count').textContent = _clData.length + ' listings';
  document.getElementById('cl-res-search').value = '';
  hdr.style.display = 'flex';
  const labels = ['','Want','Item','Price','Location','Date'];
  let html = '<table><thead><tr>';
  labels.forEach((l,i) => {
    if (i===0) { html += '<th style="width:30px"></th>'; return; }
    if (i===1) { html += '<th style="width:62px;text-align:center">Want</th>'; return; }
    const si = i-2;
    const cls = _clSortCol===si ? (_clSortDir===1?'sort-asc':'sort-desc') : '';
    html += '<th class="'+cls+'" onclick="clSort('+si+')">' + l + '</th>';
  });
  html += '</tr></thead><tbody>';
  const rawQ = (document.getElementById('cl-query').value||'').trim().toLowerCase();
  const qWords = rawQ.split(/\\s+/).filter(Boolean);
  function relevance(title) {
    const t=(title||'').toLowerCase();
    if (!rawQ) return 0;
    if (t.includes(rawQ)) return 3;
    if (qWords.every(w=>t.includes(w))) return 2;
    if (qWords.some(w=>t.includes(w))) return 1;
    return 0;
  }
  let sorted = [..._clData];
  if (_clSortCol !== null) {
    const key = _clCols[_clSortCol];
    sorted.sort((a,b) => {
      if (key==='relevance') return _clSortDir*(relevance(b.title)-relevance(a.title));
      const av=a[key]||'', bv=b[key]||'';
      if (key==='price') return _clSortDir*((parseFloat(String(av).replace(/[^0-9.]/g,''))||0)-(parseFloat(String(bv).replace(/[^0-9.]/g,''))||0));
      return _clSortDir*String(av).localeCompare(String(bv));
    });
  }
  const isFavR = r => _clFavs.includes(r.cityId);
  let final;
  if (_clSortCol===null) {
    const sc = r=>relevance(r.title);
    final = [...sorted.filter(r=>isFavR(r)).sort((a,b)=>sc(b)-sc(a)), ...sorted.filter(r=>!isFavR(r)).sort((a,b)=>sc(b)-sc(a))];
  } else { final = sorted; }
  final.forEach(r => {
    const isFav = isFavR(r);
    const clId  = 'cl:' + (r.url||r.title||'');
    const isWatched = !!(window._clWatchlist||{})[clId];
    const safeTitle = _clEsc(r.title||'(no title)');
    const safeUrl   = _clSafeUrl(r.url);
    // The whole onclick is escaped as an HTML attribute value. It's CSP-inert
    // anyway, but escaping prevents a crafted title/url from breaking out of the
    // attribute (e.g. injecting an inline style overlay).
    const onclickJs = 'clToggleWatch('+JSON.stringify(clId)+','+JSON.stringify(r.title||'')+','+JSON.stringify(r.url||'')+','+JSON.stringify(r.price||'')+','+JSON.stringify(r.location||'')+',this)';
    const watchBtn = '<button class="watch-btn'+(isWatched?' active':'')+'" onclick="'+_clEsc(onclickJs)+'" title="'+(isWatched?'Remove from':'Add to')+' watch list">'+(isWatched?'★':'☆')+'</button>';
    const wantMatch = _clMatchesWantList(r.title||'');
    const star = isFav ? '<span class="cl-fav-star">★</span>' : '';
    const titleCell = safeUrl ? star+' <a href="'+_clEsc(safeUrl)+'" target="_blank" rel="noopener">'+safeTitle+'</a>'
                              : star+' '+safeTitle;
    html += '<tr class="'+(isFav?'cl-fav-result':'')+'" data-city="'+_clEsc(r.cityId||'')+'" data-cl-id="'+_clEsc(clId)+'">'
      + '<td>'+watchBtn+'</td>'
      + '<td style="text-align:center">'+(wantMatch?'<span class="tag-kw">WANT</span>':'')+'</td>'
      + '<td title="'+_clEsc(r.title||'')+'">'+titleCell+'</td>'
      + '<td>'+_clEsc(r.price||'')+'</td>'
      + '<td>'+_clEsc(r.location||'')+'</td>'
      + '<td>'+_clEsc(r.date||'')+'</td></tr>';
  });
  html += '</tbody></table>';
  body.innerHTML = html;
}

function clSort(col) {
  if (_clSortCol===col) { _clSortDir*=-1; } else { _clSortCol=col; _clSortDir=1; }
  clRenderResults();
}

let _clWatchFilterActive = false;
let _clWantListFilterActive = false;

function clToggleWatchFilter() {
  _clWatchFilterActive = !_clWatchFilterActive;
  document.getElementById('cl-watchlist-toggle').classList.toggle('wl-active', _clWatchFilterActive);
  clFilterResults();
}

async function clSearchWantList() {
  if (_clWantListFilterActive) {
    _clWantListFilterActive = false;
    document.getElementById('cl-wl-link').style.display = 'none';
    document.getElementById('cl-wantlist-btn').classList.remove('wl-active');
    clFilterResults(); return;
  }
  if (!window._keywords || !window._keywords.length) {
    alert('No want list keywords saved. Add keywords on the main GC Tracker page first.');
    return;
  }
  const wlBtn = document.getElementById('cl-wantlist-btn');
  const status = document.getElementById('cl-status');
  wlBtn.classList.add('wl-active');
  status.textContent = 'Searching want list…';
  document.getElementById('cl-results-hdr').style.display = 'none';
  document.getElementById('cl-body').innerHTML = '<div class="cl-empty">Searching want list across all markets…</div>';
  try {
    const allResults = [], seenKeys = new Set();
    for (const kw of window._keywords) {
      let q = kw.trim();
      if (q.startsWith('"') && q.endsWith('"') && q.length>2) q = q.slice(1,-1);
      if (!q) continue;
      try {
        const r = await fetch('/api/cl-search?q='+encodeURIComponent(q)+'&title_only=1');
        if (r.ok) {
          const d = await r.json();
          for (const item of (d.results||[])) {
            const key = (item.title||'').toLowerCase().trim()+'|'+(item.price||'')+'|'+(item.cityId||'');
            if (!seenKeys.has(key)) { seenKeys.add(key); allResults.push(item); }
          }
        }
      } catch(e) {}
      status.textContent = 'Searched "'+q+'"… ('+allResults.length+' results so far)';
    }
    _clData = allResults.sort((a,b)=>(b.date||'').localeCompare(a.date||''));
    _clWantListFilterActive = true;
    document.getElementById('cl-wl-link').style.display = 'inline';
    status.textContent = '';
    clRenderResults();
  } catch(e) {
    document.getElementById('cl-body').innerHTML = '<div class="cl-empty" style="color:#f88">Want list search failed: '+e.message+'</div>';
    wlBtn.classList.remove('wl-active');
    status.textContent = '';
  }
}

function clToggleWatch(id, name, url, price, location, btn) {
  const isWatched = !!(window._clWatchlist[id]);
  if (isWatched) { delete window._clWatchlist[id]; }
  else { window._clWatchlist[id] = {name,url,store:location,price,date_added:new Date().toISOString().slice(0,10)}; }
  _lsSet('cl_watchlist', window._clWatchlist);
  btn.classList.toggle('active', !isWatched);
  btn.textContent = isWatched ? '☆' : '★';
  btn.title = isWatched ? 'Add to watch list' : 'Remove from watch list';
}

// ── Phase 3: inline event handler wiring ─────────────────────────────────────
document.addEventListener('DOMContentLoaded', function() {

  // Header
  document.getElementById('hdr-signout')?.addEventListener('click', clSignOut);

  // City sidebar
  document.getElementById('cl-city-search')?.addEventListener('input', clFilterCities);
  document.getElementById('cl-favs-btn')?.addEventListener('click', clToggleFavs);
  document.getElementById('cl-select-all-btn')?.addEventListener('click', clSelectAll);
  document.getElementById('cl-clear-all-btn')?.addEventListener('click', clClearAll);

  // Search bar
  document.getElementById('cl-query')?.addEventListener('keydown', function(e) {
    if (e.key === 'Enter') clSearch();
  });
  document.getElementById('cl-search-btn')?.addEventListener('click', clSearch);

  // Toolbar chips
  document.getElementById('cl-watchlist-toggle')?.addEventListener('click', clToggleWatchFilter);
  document.getElementById('cl-wantlist-btn')?.addEventListener('click', clSearchWantList);
  document.getElementById('cl-wl-link')?.addEventListener('click', clSearchWantList);

  // Results filter
  document.getElementById('cl-res-search')?.addEventListener('input', clFilterResults);

  // Auth modal — Google button
  document.getElementById('cl-auth-google-btn')?.addEventListener('click', function() {
    window.location.href = '/api/auth/google?next=/cl';
  });

  // Auth modal — password login
  document.getElementById('auth-pw')?.addEventListener('keydown', function(e) {
    if (e.key === 'Enter') clDoLogin();
  });
  document.getElementById('cl-login-submit')?.addEventListener('click', clDoLogin);

  // cl-search-wl-link hover effect
  const clWlLink = document.getElementById('cl-search-wl-link');
  if (clWlLink) {
    clWlLink.addEventListener('click', clSearchWantList);
    clWlLink.addEventListener('mouseover', function() { this.style.textDecoration = 'underline'; });
    clWlLink.addEventListener('mouseout',  function() { this.style.textDecoration = 'none'; });
  }
});

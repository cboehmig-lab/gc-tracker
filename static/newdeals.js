// GC New Deals — /newdeals (admin only)

var _ndPage      = 1;
var _ndSort      = 'pct_off';
var _ndDir       = 'desc';
var _ndLoading   = false;
var _ndKeywords  = [];
var _ndWlActive  = false;
var _ndBrands    = [];
var _ndCats      = [];
var _ndHdrHeight = 0;

document.addEventListener('DOMContentLoaded', function() {

  // ── Event wiring ────────────────────────────────────────────────────────────
  document.getElementById('nd-refresh-btn').addEventListener('click', ndRefresh);
  document.getElementById('nd-clear-btn').addEventListener('click', ndClearFilters);
  document.getElementById('nd-wl-btn').addEventListener('click', ndToggleWantList);
  document.getElementById('nd-include-sw').addEventListener('change', function() { _ndPage = 1; ndFetch(); });
  document.getElementById('nd-search').addEventListener('input', _ndDebounce(function() { _ndPage = 1; ndFetch(); }, 400));
  document.getElementById('nd-brand-sel').addEventListener('change', function() { _ndPage = 1; ndFetch(); });
  document.getElementById('nd-cat-sel').addEventListener('change', function() { _ndPage = 1; ndFetch(); });
  document.getElementById('nd-pct-sel').addEventListener('change', function() { _ndPage = 1; ndFetch(); });
  document.getElementById('nd-price-min').addEventListener('input', _ndDebounce(function() { _ndPage = 1; ndFetch(); }, 600));
  document.getElementById('nd-price-max').addEventListener('input', _ndDebounce(function() { _ndPage = 1; ndFetch(); }, 600));

  // Column sort headers
  document.querySelectorAll('.nd-th[data-sort]').forEach(function(th) {
    th.addEventListener('click', function() {
      var field = th.dataset.sort;
      if (_ndSort === field) {
        _ndDir = _ndDir === 'desc' ? 'asc' : 'desc';
      } else {
        _ndSort = field;
        _ndDir  = (field === 'name' || field === 'brand' || field === 'category') ? 'asc' : 'desc';
      }
      _ndPage = 1;
      _ndUpdateSortHeaders();
      ndFetch();
    });
  });
  _ndUpdateSortHeaders();

  // Sticky header offset for table headers
  function _ndApplyHdrOffset() {
    var bar = document.getElementById('nd-top-bar');
    var h = bar ? bar.offsetHeight : 80;
    document.documentElement.style.setProperty('--nd-hdr-top', h + 'px');
  }
  window.addEventListener('resize', _ndApplyHdrOffset);
  _ndApplyHdrOffset();

  // Load want list keywords from /api/me
  fetch('/api/me').then(function(r) { return r.json(); }).then(function(d) {
    _ndKeywords = d.keywords || [];
    _ndUpdateWlBtn();
  }).catch(function() {});

  // Initial fetch
  ndFetch();
});

// ── Helpers ──────────────────────────────────────────────────────────────────

function _ndDebounce(fn, ms) {
  var t;
  return function() { clearTimeout(t); t = setTimeout(fn, ms); };
}

function _ndEsc(s) {
  return String(s || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function _ndFmtDate(iso) {
  try { return new Date(iso).toLocaleString(); } catch(e) { return iso; }
}

function _ndUpdateSortHeaders() {
  document.querySelectorAll('.nd-th[data-sort]').forEach(function(th) {
    var field = th.dataset.sort;
    var arrow = '';
    if (field === _ndSort) arrow = _ndDir === 'desc' ? ' ↓' : ' ↑';
    // Strip any previous arrow before adding
    th.textContent = th.textContent.replace(/ [↓↑]$/, '') + arrow;
  });
}

function _ndUpdateWlBtn() {
  var btn = document.getElementById('nd-wl-btn');
  if (!_ndKeywords.length) {
    btn.style.opacity = '0.45';
    btn.title = 'No want list keywords — add them in the main tracker first';
  } else {
    btn.style.opacity = '';
    btn.title = 'Filter to want list keywords (' + _ndKeywords.length + ')';
  }
}

function _ndUpdateDropdown(id, values, placeholder) {
  var sel    = document.getElementById(id);
  var current = sel.value;
  sel.innerHTML = '<option value="">' + placeholder + '</option>' +
    values.map(function(v) {
      return '<option value="' + _ndEsc(v) + '"' + (v === current ? ' selected' : '') + '>' + _ndEsc(v) + '</option>';
    }).join('');
}

// ── Refresh (re-scan Algolia) ─────────────────────────────────────────────────

async function ndRefresh() {
  var btn = document.getElementById('nd-refresh-btn');
  btn.disabled = true;
  btn.textContent = '⟳ Refreshing…';
  document.getElementById('nd-empty-msg').textContent = 'Fetching new inventory from GC — this may take 30–60 seconds…';
  document.getElementById('nd-empty-msg').style.display = 'block';
  document.getElementById('nd-results-wrap').style.display = 'none';
  document.getElementById('nd-paginator').innerHTML = '';
  document.getElementById('nd-result-count').textContent = '';
  try {
    var r = await fetch('/api/new-scan', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
    });
    var d = await r.json();
    if (d.ok) {
      document.getElementById('nd-refresh-time').textContent =
        'Last refresh: ' + _ndFmtDate(d.last_updated) + ' (' + d.count.toLocaleString() + ' SKUs)';
      _ndPage = 1;
      ndFetch();
    } else {
      document.getElementById('nd-empty-msg').textContent = 'Refresh failed: ' + (d.error || 'unknown error');
    }
  } catch(e) {
    document.getElementById('nd-empty-msg').textContent = 'Refresh failed: ' + e.message;
  } finally {
    btn.disabled = false;
    btn.textContent = '↻ Refresh Data';
  }
}

// ── Browse / fetch results ────────────────────────────────────────────────────

async function ndFetch() {
  if (_ndLoading) return;
  _ndLoading = true;

  var body = {
    page:               _ndPage,
    sort:               _ndSort,
    dir:                _ndDir,
    filter_q:           document.getElementById('nd-search').value.trim(),
    filter_brands:      Array.from(document.getElementById('nd-brand-sel').selectedOptions)
                          .map(function(o) { return o.value; }).filter(Boolean),
    filter_categories:  Array.from(document.getElementById('nd-cat-sel').selectedOptions)
                          .map(function(o) { return o.value; }).filter(Boolean),
    filter_min_pct_off: parseInt(document.getElementById('nd-pct-sel').value) || 0,
    filter_price_min:   document.getElementById('nd-price-min').value || null,
    filter_price_max:   document.getElementById('nd-price-max').value || null,
    include_software:   document.getElementById('nd-include-sw').checked,
    filter_want_list:   _ndWlActive,
    keywords:           _ndKeywords,
  };

  try {
    var r = await fetch('/api/new-browse', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify(body),
    });
    var d = await r.json();

    if (d.no_cache) {
      document.getElementById('nd-empty-msg').textContent =
        'No data yet — click ↻ Refresh Data to load inventory.';
      document.getElementById('nd-empty-msg').style.display = 'block';
      document.getElementById('nd-results-wrap').style.display = 'none';
      document.getElementById('nd-paginator').innerHTML = '';
      document.getElementById('nd-result-count').textContent = '';
      return;
    }

    // Update filter dropdowns preserving current selection
    if (d.brands)     { _ndBrands = d.brands; _ndUpdateDropdown('nd-brand-sel', _ndBrands, 'All Brands'); }
    if (d.categories) { _ndCats   = d.categories; _ndUpdateDropdown('nd-cat-sel', _ndCats, 'All Categories'); }

    // Status
    if (d.last_updated) {
      document.getElementById('nd-refresh-time').textContent =
        'Last refresh: ' + _ndFmtDate(d.last_updated);
    }
    var total = d.total || 0;
    document.getElementById('nd-result-count').textContent =
      total.toLocaleString() + ' item' + (total !== 1 ? 's' : '');

    // Render
    if (!d.items || !d.items.length) {
      document.getElementById('nd-empty-msg').textContent = 'No items match your filters.';
      document.getElementById('nd-empty-msg').style.display = 'block';
      document.getElementById('nd-results-wrap').style.display = 'none';
    } else {
      document.getElementById('nd-empty-msg').style.display = 'none';
      document.getElementById('nd-results-wrap').style.display = '';
      _ndRenderTable(d.items);
      // Re-apply header offset after table render
      var bar = document.getElementById('nd-top-bar');
      if (bar) document.documentElement.style.setProperty('--nd-hdr-top', bar.offsetHeight + 'px');
    }

    _ndRenderPaginator(d.page, d.total_pages);

  } catch(e) {
    console.error('ndFetch error:', e);
  } finally {
    _ndLoading = false;
  }
}

// ── Render table rows ─────────────────────────────────────────────────────────

function _ndRenderTable(items) {
  var tbody = document.getElementById('nd-tbody');
  tbody.innerHTML = items.map(function(item) {
    var pctOff  = item.pct_off > 0 ? item.pct_off + '%' : '—';
    var pctClass = item.pct_off >= 60 ? 'nd-pct-hot'
                 : item.pct_off >= 40 ? 'nd-pct-warm'
                 : 'nd-pct-ok';
    var price   = item.price      > 0 ? '$' + item.price.toFixed(2)      : '—';
    var msrp    = item.list_price > 0 ? '$' + item.list_price.toFixed(2) : '—';
    var nameHtml = item.url
      ? '<a href="' + _ndEsc(item.url) + '" target="_blank" rel="noopener">' + _ndEsc(item.name) + '</a>'
      : _ndEsc(item.name);
    return '<tr>' +
      '<td class="nd-pct ' + pctClass + '">' + pctOff + '</td>' +
      '<td class="nd-price">' + price + '</td>' +
      '<td class="nd-msrp">' + msrp + '</td>' +
      '<td class="nd-name">' + nameHtml + '</td>' +
      '<td class="nd-brand">' + _ndEsc(item.brand || '') + '</td>' +
      '<td class="nd-cat">'  + _ndEsc(item.category || '') + '</td>' +
      '</tr>';
  }).join('');
}

// ── Paginator ─────────────────────────────────────────────────────────────────

function _ndRenderPaginator(page, totalPages) {
  var el = document.getElementById('nd-paginator');
  if (!totalPages || totalPages <= 1) { el.innerHTML = ''; return; }
  var html = '';
  if (page > 1)
    html += '<button class="nd-pg-btn" data-page="' + (page - 1) + '">&#8249; Prev</button>';
  html += '<span class="nd-pg-info">Page ' + page + ' / ' + totalPages + '</span>';
  if (page < totalPages)
    html += '<button class="nd-pg-btn" data-page="' + (page + 1) + '">Next &#8250;</button>';
  el.innerHTML = html;
  el.querySelectorAll('[data-page]').forEach(function(btn) {
    btn.addEventListener('click', function() {
      _ndPage = parseInt(btn.dataset.page);
      window.scrollTo(0, 0);
      ndFetch();
    });
  });
}

// ── Toggle actions ────────────────────────────────────────────────────────────

function ndToggleWantList() {
  if (!_ndKeywords.length) {
    alert('No want list keywords found. Add them in the main tracker first.');
    return;
  }
  _ndWlActive = !_ndWlActive;
  document.getElementById('nd-wl-btn').classList.toggle('wl-active', _ndWlActive);
  _ndPage = 1;
  ndFetch();
}

function ndClearFilters() {
  document.getElementById('nd-search').value     = '';
  document.getElementById('nd-brand-sel').value  = '';
  document.getElementById('nd-cat-sel').value    = '';
  document.getElementById('nd-pct-sel').value    = '40';
  document.getElementById('nd-price-min').value  = '';
  document.getElementById('nd-price-max').value  = '';
  _ndWlActive = false;
  document.getElementById('nd-wl-btn').classList.remove('wl-active');
  _ndPage = 1;
  ndFetch();
}

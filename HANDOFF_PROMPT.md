# GC Gear Tracker — Session Handoff Prompt
*Generated: 2026-06-29 · Version: v2.14.3 · Live at: gcgeartracker.com*

Use this at the start of a new session to bring Claude up to speed instantly.

---

## The Project

**GC Used Inventory Tracker** (`gcgeartracker.com`) is a Flask web app that tracks Guitar Center used inventory. Users create accounts (username/password or Google Sign-In) and get items flagged NEW since their last scan. Watch list, want list, favorites, and saved searches all sync across devices via server-side user accounts. A separate `/cl` page does Craigslist used gear search. A private `/newdeals` admin page browses new GC inventory deals (items discounted from MSRP).

- **Repo**: `cboehmig-lab/gc-tracker` on GitHub → auto-deploys to Railway on every push to `main`
- **Python entry**: `gc_tracker_app.py` (single file, ~5600+ lines)
- **Static assets**: `static/gc.js`, `static/gc.css`, `static/newdeals.js`, `static/newdeals.css`, `static/admin.js`, `static/og-image.svg`
- **Local workspace**: `~/Desktop/gc_tracker/`
- **Full technical reference**: read `~/Desktop/gc_tracker/HANDOFF.md` before making any changes

---

## Critical Rules (read before touching anything)

1. **Never write inline JS** — all JS lives in `static/gc.js` (main app) or `static/newdeals.js` (/newdeals). CSP blocks inline scripts.
2. **No inline onclick attributes** — use `data-*` attributes + `addEventListener`.
3. **Git pushes must come from the Mac terminal** — the Cowork sandbox gets a proxy 403 on GitHub pushes. As of v2.13.3, `origin` points at the SSH URL (`git@github.com:cboehmig-lab/gc-tracker.git`), so the normal `git push origin main` works AND keeps the ahead/behind count accurate. (Previously pushes went to the raw SSH URL while `origin` was HTTPS — pushes landed but `origin/main` tracking never updated, causing a phantom "ahead N" forever.)
4. **Sandbox git lock files**: if `git commit` fails with "cannot lock ref HEAD", the Mac owns the lock. Tell the user: `rm ~/Desktop/gc_tracker/.git/HEAD.lock && rm ~/Desktop/gc_tracker/.git/refs/heads/main.lock` then re-run.
5. **Version bump**: only change `APP_VERSION` in `gc_tracker_app.py` — the `<!-- __VER__ -->` placeholder in `HTML_TEMPLATE` auto-propagates it everywhere.
6. **`_require_admin()` / `_require_admin_api()` are NOT decorators** — they return None or a response. Call inline: `denied = _require_admin(); if denied: return denied`. Never use as `@_require_admin`.
7. **Template replacements at startup**: `HTML_TEMPLATE`, `CL_TEMPLATE`, and `NEWDEALS_TEMPLATE` all get `<!-- __GA__ -->` replaced at module load. `HTML_TEMPLATE` also gets `<!-- __VER__ -->`. The `<!-- __STORES_NOSCRIPT__ -->` placeholder stays in `HTML_TEMPLATE` and is replaced at *request time* in `index()` — do not bake it in at startup.

---

## Architecture in 60 Seconds

**Desktop filter bar**: two-row layout inside `#results-top-bar` (`flex-direction:column`). Row 1 = `.quick-filter-bar` (chips). Row 2 = `.results-hdr` (dropdowns + search + Save/Clear buttons). Mobile: `#results-top-bar{display:contents}` — wrapper is invisible, children flow directly in `#res-panel`.

**Dropdowns that escape overflow clipping**: use `position:fixed` + JS `getBoundingClientRect()`. `.right` has `overflow:hidden` which clips absolute children. `#ss-dropdown` and `#price-dd-panel` both use this pattern.

**Sticky table headers**: `border-collapse:separate; border-spacing:0` on `table`. `th{position:sticky; top:var(--tbl-hdr-top,88px)}`. CSS variable set by `_applyFrozenHeaderOffset()` in JS after each render and on resize.

**Server-side browse**: `POST /api/browse` — reads `gc_category_cache.json`, applies all filters in `_apply_base()`, returns 50 items/page. Space-separated search terms are AND'd.

**NEW detection**: anchor-based per-user. `threshold = _norm_anchor` (user's "top of table" high-water mark). Anchor only advances when browsing fully unfiltered (`!hasFilters && !_globalSearchActive`). Persisted server-side in `user_data.last_anchor`.

**Special view state save/restore**: `_captureFilterState()` / `_restoreFilterState()` in `gc.js`. `_preSpecialViewState` is set when Watch List, Want List, or a Saved Search is activated — stores all filter state. Toggling any of these off calls `_restoreFilterState()` to return to the exact prior state. Watch and Want List also clear all filters (brand, cond, cat, price, search) and go nationwide (`all_stores: true` in browse body) on activation.

**Sync merge strategy**: server-wins for watchlist, keywords, saved_searches (so deletions propagate cross-device). Falls back to local only if server record is empty.

**Mobile**: `_isMobile()` = `window.innerWidth <= 820px`. Bottom sheet pattern for store panel + filter panel. Swipe-to-dismiss on both.

---

## /newdeals Admin Page

Private page (`_require_admin()` gate). New GC inventory (not used) discounted from MSRP.

- **`NEW_DEALS_CACHE_FILE`**: `gc_new_deals_cache.json` — stores all new inventory items
- **`/api/new-scan` (POST)**: fetches ALL new GC inventory via Algolia (`condition.lvl0:New`), dedupes by SKU, saves cache. Uses `ThreadPoolExecutor(max_workers=12)` for parallel page fetches.
- **`/api/new-browse` (POST)**: filters/sorts/paginates the cached items. Filters: `include_software`, `filter_q`, `filter_brands`, `filter_categories`, `filter_min_pct_off`, `filter_price_min/max`, `filter_want_list` + `keywords`.
- **Software detection**: `_is_software_item(name, category)` checks both fields against `_SOFTWARE_KEYWORDS`. `is_software` boolean stored on each item at scan time — browsing filters on `item.get("is_software", False)`.
- **Category extraction**: uses `hit.get("categories")[0].get("lvl0")` (same as used gear). Falls back to `categoryPageIds` (skips bare "New"/"Used" values).
- **Static files**: `static/newdeals.js`, `static/newdeals.css` — self-contained, no shared state with main app JS.
- **Want list**: loaded from `/api/me` on page load; "Want List" chip filters by whole-word keyword match (OR logic across keywords).
- **⚠️ After any deploy that changes scan logic**: admin must click "↻ Refresh Data" on `/newdeals` to rebuild cache with updated fields.

**`gc_new_deals.py`** — standalone terminal script (not part of the web app). Same Algolia credentials. Run: `python3 gc_new_deals.py [--threshold 0.5] [--category Guitars]`

---

## Algolia Details

- **Index**: `cD-guitarcenter`
- **Used inventory**: `facetFilters: ["condition.lvl0:Used"]` + `stores: [<store_name>]`
- **New inventory**: `facetFilters: ["condition.lvl0:New"]` — nationwide, no store filter
- **Price fields**: `hit.get("price")` = sale price, `hit.get("listPrice")` = MSRP
- **Category fields**: `hit.get("categories")[0].get("lvl0")` = top-level category (e.g. "Guitars")
- **Store fields**: `hit.get("stores")` array; `hit.get("storeName")` = "City, ST" format
- **`all_stores: true`** in `/api/browse` body bypasses store filter server-side

---

## SEO (v2.12.25–27)

- **Title**: "Guitar Center Used Gear Tracker — Browse Inventory by Store Location"
- **Meta description**: "Browse used gear at any Guitar Center location. Search guitars, amps, pedals, drums, and more by store, city, condition, and price — updated in real time. Free watch list and want list."
- **JSON-LD**: `WebSite` schema with `SearchAction` (`potentialAction`) — injected as `<script type="application/ld+json">` (not blocked by CSP `script-src 'self'`)
- **Noscript store list**: `_build_stores_noscript()` called in `index()` — reads `STORES_CACHE` fresh, generates `<noscript>` block listing all ~240+ store names. Invisible to JS users, crawlable by Google. Updates automatically when store list is refreshed.
- **Footer**: `.seo-footer` — visible "Privacy Policy · Not affiliated with Guitar Center, Inc." in `#555` gray. No hidden text.

---

## Current State: v2.14.3 (results-table horizontal scroll + brandless "(none)" label — pending push; v2.14.1 + v2.14.2 deployed)

### Recent changes (this session)

- **v2.14.3** — **Results-table horizontal scroll** (follow-up to v2.14.2 #3). Truncation alone still left the table wider than the panel on some rows, and `.results` was `overflow-x:hidden`, so Date Added + Location/Store were clipped with no scrollbar. `gc.css` (2 props): `.results` → `overflow-x:auto`; `#results-top-bar` → add `left:0` to its `position:sticky;top:0` so the filter bar stays pinned while the table scrolls. Sticky header untouched (vertical scroll still on `.results`); mobile unaffected (its media query overrides `.results` to `overflow:hidden` and scrolls `#res-body`). Truncation + `title=` tooltips from v2.14.2 kept. **Also folded in**: brandless rows now show a muted **"(none)"** in the Brand column (`_buildRowHtml` + `.brand-none`) instead of a blank cell, matching the filter label; `data-brand` stays `""` so sorting is unchanged. Detail in HANDOFF.md "v2.14.2 → v2.14.3".
- **v2.14.2** — **Three user-reported fixes** (Discord); no cache rebuild, no new endpoints; `gc_tracker_app.py` + `static/gc.js` + `static/gc.css`. (1) **Filter dropdowns no longer hidden by the paginator**: Brand/Cond/Cat/Subcat panels were `position:absolute;z-index:50` trapped in `#results-top-bar`'s stacking context (`z-index:2`), so the sticky opaque `.paginator` (`z-index:5`) painted over them when the result set was short (≲5 items). Moved the four panels to `position:fixed;z-index:500` + a new `_positionFixedPanel()` helper (the Price/Saved-Search pattern), which escapes the top-bar context. Desktop-only (mobile uses the accordion). (2) **"(none)" brand**: ~108 brandless items (`brand==""`) are now filterable — `NO_BRAND_LABEL` + `_brand_ok()` in `/api/browse` (facet count via `empty_label` + both filter passes) and matching logic in `/api/saved-search-counts`. Flows through the existing generic brand dropdown UI — no frontend change; plain equality, no regex/DoS surface. (3) **Long Category/Subcategory truncation**: capped col 8/9 `max-width` (160/200px) in `gc.css` so they truncate with `…` instead of pushing Date Added + Store off-screen (`.results` is `overflow-x:hidden`); full text shown via `title=` hover tooltip on both cells in `_buildRowHtml`. Detail in HANDOFF.md "v2.14.1 → v2.14.2".
- **v2.14.1** — **Algolia key health endpoint** (`GET /api/health/algolia`) + daily Cowork monitor (`gc-algolia-key-health`, 08:00 local). The GC Algolia search key is the single point of failure for scans (rotation → 401/403 → silent scan death). The endpoint runs the scanner's used query at `hitsPerPage:0`, returns `{ok, nbHits, http_status}`, cached ~15 min (≤ ~96 probes/day — no quota abuse, public, no secret leaked). Monitor alerts on key death / 0 results / unreachable. Pending push.
- **v2.14.0** — **Vintage filter** (new feature). "🎸 Vintage" quick-filter chip (right of Price Drops) showing only gear GC classifies as vintage. Uses GC's own authoritative signal — the raw Algolia hit's **`premiumGear == "Vintage"`** field (~2,127 used items) — captured at scan time as a per-item `is_vintage` flag (the `is_software` pattern). `/api/browse` takes a `vintage_only` boolean → `_apply_base()` keeps `is_vintage` items (plain boolean, no regex/DoS surface). The chip is a **composable** content filter like Price Drops: respects store selection, folded into `_captureFilterState`/`_restoreFilterState` + saved searches; it is NOT a Watch/Want-style nationwide takeover. Verified cleaner than a title heuristic — `premiumGear` excludes the modern "Fender American Vintage"/"American Vintage II" reissues, Vintage Reissue amps, and the modern "Vintage" brand (overlap 0–3 items) while keeping genuine vintage; the `name.startswith("Vintage")` heuristic carried ~128 false positives. ⚠️ The cache has no `is_vintage` until the **first scan after deploy** — the chip is empty until then (same caveat as the v2.12.24 software-flag rollout). Algolia findings documented by `probe_vintage*.py` (gitignored).
- **v2.13.3** — Mobile ZIP apply fix: iOS's `inputmode="numeric"` keypad has no Return/Go key, so the ZIP input now auto-applies ZIP Sort when 5 digits are present (covers AutoFill too), with `blur()` on mobile to dismiss the keypad; `enterkeyhint="go"` added for Android. Also fixed the phantom "ahead N" git state (see Critical Rule 3).
- **v2.13.2** — **ZIP distance filter** (the planned feature — now done). "Within [Any/5/10/25/50/100 mi]" select under the ZIP input, shown only in ZIP Sort mode; filters the store list AND narrows `_selectedStores` (snapshot/restore via `_preRadiusSelection`, same pattern as `_preFavsSelection`) so browse results actually filter. Un-geocoded "(?)" stores excluded by any finite radius; Watch/Want List unaffected (`all_stores:true`); Favorites toggle and saved-search apply reset radius to Any. Not persisted, like the ZIP. Full detail in HANDOFF.md "Recent Changes (v2.13.1 → v2.13.2)".
- **v2.13.1** — Full-site audit fixes: per-type want-list keyword caps (preserves large lists, bounds wildcard DoS); atomic 53MB cat-cache write (temp + `os.replace`, no more truncation-wipe). See HANDOFF.md + `AUDIT_REPORT_2026-06-12.md`.
- **v2.13.0** — Want-list fix + `/api/browse` performance overhaul (minor bump; `gc_tracker_app.py` only, no JS, no cache rebuild). (1) **Want lists >50 terms no longer drop matches** — the v2.12.31 `keywords[:50]` DoS cap was silently breaking power users (real cases: 220 and 73 terms). The matcher was rewritten (plain words → set membership; phrases/wildcards → one alternation regex), verified behavior-identical (32K fuzz cases, 0 mismatches), and the cap raised to **750 logged-in / 250 guest** after dedupe. (2) **`_load_cat_cache()` no longer re-parses the 53MB cache on every browse** — memoized by mtime (**~400ms → ~1µs/call**), which also un-serializes concurrent request threads (GIL was held during the parse). (3) Defense-in-depth: `filter_q` token cap (12) + `/api/saved-search-counts` clamp. Full writeup: HANDOFF.md "Recent Changes (v2.12.36 → v2.13.0)".
- **v2.12.36** — Security posture (not a vuln): added `Cross-Origin-Opener-Policy: same-origin-allow-popups` to every response (scanner credit + cross-window isolation; safe with redirect-based OAuth) and an RFC 9116 `/.well-known/security.txt` (private report channel). Deliberately did NOT add CORP (would break OG-image social previews). Documented the one remaining CSP weakness (`style-src 'unsafe-inline'`) as a future refactor — no longer an active hole after the v2.12.35 escaping.
- **v2.12.35** — Security: fixed a stored XSS in the Craigslist render path. CL listing fields (title/price/location/url/image) are scraped from a world-writable source and were concatenated into `innerHTML` unescaped in `static/cl.js` and `static/gc.js` (`clRenderResults`). Now HTML-escaped + URL-allowlisted. (CSP `script-src 'self'` blocked script execution, but inline-style overlay phishing was live — Medium.) `newdeals.js` was already escaped.
- **v2.12.34** — Security: `/api/cl-search` now requires login (was an unauthenticated outbound-amplification vector — one call fans out to ~75 Craigslist markets); stopped leaking raw exception text; added the `_CL_CITIES` allowlist to `/api/cl-parse-test` (admin SSRF primitive). No UX impact (CL is sign-in-only by design).
- **v2.12.33** — Security: fixed an open-redirect bypass in the `?next=` param on `/api/auth/google` and `/admin/login`. `/\evil.com` passed the old `startswith("/")` check but browsers normalize it to `//evil.com`. New `_safe_next()` helper rejects backslashes, `//`, and control chars.
- **v2.12.32** — Security: closed three unauthenticated "write to a global file" endpoints that the v2.12.28 favorites fix missed. `/api/stores/refresh` → admin-only (it scrapes GC and overwrites the shared store cache — anyone could wipe the store list). `/api/watchlist` + `/api/keywords` (GET & POST) → require login. All are dead code (not called by any frontend).
- **v2.12.31** — Security: `/api/browse` unauthenticated CPU-DoS fix — capped the client `keywords` array (≤50, ≤100 chars each) and `filter_q` (≤200 chars). Each keyword compiles to a regex run over the ~92K-item cache; the array was previously unbounded. Same class as the v2.12.30 saved-search-counts cap.
- **v2.12.30** — Security: `/api/saved-search-counts` DoS fix — added login check and 50-search hard cap. No UX impact.
- **v2.12.29** — Security: fixed admin privilege escalation. `_is_admin()` now requires `google_id` to be set before trusting the email match — blocks password-account users from claiming admin by self-reporting the admin email at registration.
- **v2.12.28** — Security audit: hardened three unprotected endpoints. `/api/stop` now requires `run_id` echo. `/api/populate-store-data` and `/api/fill-gaps` now require admin session. `/api/favorites` now requires logged-in session. Full audit log in HANDOFF.md.
- **v2.12.27** — SEO: `_build_stores_noscript()` moved to request-time (was startup-time) so it always reflects the live store cache. Clean footer with only Privacy Policy + affiliation notice.
- **v2.12.26** — SEO: visible footer simplified; store location content moved to `<noscript>`.
- **v2.12.25** — SEO: updated title/description/OG/Twitter tags to match location-inventory search intent; added JSON-LD `WebSite` schema with `SearchAction`.
- **v2.12.24** — Fixed software/plugin filtering on `/newdeals`. Name-based detection via `_is_software_item(name, category)`. `is_software` flag stored at scan time. Category extraction now uses `categories[0].lvl0` (same as used gear). **Admin must Refresh Data after this deploy.**
- **v2.12.23** — Built `/newdeals` admin page end-to-end: backend routes (`/newdeals`, `/api/new-scan`, `/api/new-browse`), `NEWDEALS_TEMPLATE`, `static/newdeals.js`, `static/newdeals.css`.
- **v2.12.22** — Watch List, Want List, and Saved Searches now bypass all current filters and search nationwide on activation. Toggle-off restores exact prior filter/store state. Added "← Back" button to Saved Searches dropdown. Implemented `_captureFilterState()` / `_restoreFilterState()` helper pattern in `gc.js`.

### Traffic / scale
- ~854 unique visitors/week (Google Analytics)
- 152 registered accounts
- Organic + Reddit-driven
- Google Search Console active — showing page 1 positions (avg 6–10) for location-specific queries like "guitar center [city] inventory" with zero clicks (CTR problem, not ranking problem — addressed in v2.12.25–27)

---

## Nothing Currently Broken

v2.13.0–v2.13.3 all pushed and deployed. No known issues. Still-open recommendations from the 2026-06-12 audit (not bugs): memoize the browse base list by cache mtime, single gunicorn worker instead of Flask dev server, SQLite WAL, back up `gc_users.db` — see `AUDIT_REPORT_2026-06-12.md`. Run `/admin/build-coords` if store coords coverage looks thin (un-geocoded stores are excluded by any ZIP radius).

---

## ✅ Security Audit: TWO ROUNDS DONE (v2.12.28 + v2.12.31–34)

**Round 1 (v2.12.28–30)** — see HANDOFF.md:
- v2.12.28: `/api/stop` (run_id validation), `/api/populate-store-data` + `/api/fill-gaps` (admin guard), `/api/favorites` (require login).
- v2.12.29: `_is_admin()` privilege escalation via self-reported email — now requires `google_id`.
- v2.12.30: `/api/saved-search-counts` CPU DoS — login + 50-search cap.

**Round 2 (v2.12.31–36, this session)** — full adversarial re-review. Round 1's "all other surface clean" was overconfident; round 2 found a related family of bugs and fixed them. See the "Security Audit Round 2" section in HANDOFF.md for the complete log (attack vector + severity + fix for each):
- **v2.12.31 (High)**: `/api/browse` unauthenticated CPU-DoS — capped `keywords` (≤50) and `filter_q` (≤200). The single biggest remaining public-abuse surface.
- **v2.12.32 (High+Med)**: `/api/stores/refresh` → admin (unauth scrape + store-list wipe); `/api/watchlist` + `/api/keywords` → login (the favorites fix's two missed siblings).
- **v2.12.33 (Med)**: open-redirect via `/\evil.com` backslash bypass in `?next=` — new `_safe_next()`.
- **v2.12.34 (Med+Low)**: `/api/cl-search` → login (outbound amplification) + no more leaked exception text; `/api/cl-parse-test` city allowlist.
- **v2.12.35 (Med)**: stored XSS in the Craigslist render path — scraped (world-writable) listing fields went into `innerHTML` unescaped in `cl.js` + `gc.js`. Now escaped + URL-allowlisted. Script exec was already blocked by CSP, but inline-style overlay phishing was live. (`newdeals.js` was already escaped; admin pages escape server-side.)
- **v2.12.36 (posture)**: added COOP header + RFC 9116 `/.well-known/security.txt`; documented `style-src 'unsafe-inline'` as the one known CSP weakness (future refactor, not an active hole).
- **Confirmed clean (re-verified)**: SSRF (cl-search allowlist + quoting), SQLi (parameterized), ReDoS (`re.escape` + new caps), SSTI, CSRF "no-Origin" path (SameSite=Lax + JSON content-type + admin tokens make it non-exploitable), admin escalation, OAuth state/email_verified, SECRET_KEY/CSP/HSTS/cookies, client-side *manipulation* of server behavior (the render-path XSS was the one client-side gap — now fixed in v2.12.35).
- **Documented Low (deferred)**: L1 dead `/login` + GET `/logout` CSRF (recommend deleting both routes — `session["logged_in"]` is confirmed dead code); L3 SSE exception strings; L4 malformed-input 500s; L5 unbounded `/api/run` stores array. Full detail in HANDOFF.md.

**Reddit comment ("still isn't secure")**: round 2 closed the most likely candidates — an unauthenticated endpoint that wipes shared state (`/api/stores/refresh`), trivial unauthenticated CPU-DoS (`/api/browse`), an OAuth open-redirect, and a **stored XSS in the CL search results** (post a Craigslist listing with HTML in the title, search for it — it rendered). That XSS is probably the single most likely thing a security-minded redditor actually poked at. We still can't know for sure what they meant, but these are the things a casual prober finds first.

App is ready for Reddit posts and Product Hunt once v2.12.36 is deployed.

---

## Next Steps (after security)

- **Product Hunt listing** — hold until after security audit. Good fit for indie tool launch.
- **Reddit posts** — proven to convert. Targets: `r/guitarpedals`, `r/WeAreTheMusicMakers`, `r/Bass`, `r/drums`. Hold until security is buttoned up.
- **SEO — watch Search Console** — give it 4–6 weeks after v2.12.25–27. Expect CTR improvement on existing impressions first, then possible ranking lift on location queries. URL Inspection → Request Indexing already done.
- **Android app** — WebView wrapper is the fastest path to Play Store. Needs 14-day closed test with 12+ opted-in testers before production.
- **Monetization** — Reverb affiliate (ShareASale) is the cleanest fit — inline sponsored rows contextual to nearby items. Freemium (email/SMS alerts, more watch list slots) avoids ads entirely.

---

## Where to Go for More

- Full architecture, all routes, auth flow, security hardening history, mobile layout details: **`~/Desktop/gc_tracker/HANDOFF.md`**
- Version history back to v2.8.0 is in HANDOFF.md under "Recent Changes" sections

# Want List Email Alerts — Design (gcgeartracker.com)

Status: **step 1 (v2.18.0) LIVE 2026-10-07; v2.18.1 privacy policy LIVE + Postmark APPROVED 2026-10-08; step 2 (v2.19.0, daily alert engine) BUILT + locally verified 2026-10-08, not yet pushed.** Design agreed in claude.ai chat 2026-10-02 → 10-07; code investigation
done in Cowork 2026-10-07 (app at v2.17.9). Build happens in the 5 steps at the bottom, each step its own version(s),
each waiting on Chuck's go-ahead.

---

## 1. What we're building (decided)

**Opt-in**
- Per Want List pill, via a bell icon on the pill (filled = alerts on). Registered users only.
- First bell click opens a setup box: enter email → choose frequency → type the 6-digit code we email, in-page.
- **Existing addresses are never used.** `users.email` (optional at registration / from Google sign-in) is NOT an
  alert address. Everyone opts in and confirms fresh, into separate encrypted storage.
- Alerts always cover **all stores** (no favorites-only option).

**Engine**
- Server runs a scan at the top of every hour. If any scan finished within ~10 min, it skips its own and uses that one.
- A Postgres **advisory lock** makes sure only one instance runs the hour (Railway deploy overlap = two containers).
- Frequency (one per user):
  - **Hourly**: an email after an hourly run only if there are new matches.
  - **Daily summary**: 10:00 America/New_York (zoneinfo, so DST-safe), with catch-up on boot if today's run was missed.
  - **DECISION 2026-10-08 (Chuck): "daily alert" only — no hourly option.** At most one email a day, at 10:00 ET: the
    server runs its own scan at 10:00 ET (GC tends to post new listings ~8-9 AM Central, so this catches the
    morning upload), compiles that user's Want List matches since the last daily alert, and sends ONLY if there is at least one
    match — no matches = no email (Chuck, 2026-10-08: "daily alert", not a digest). Body carries a note
    along the lines of: "Items listed as available at scan time may have sold since. Matching items that were
    listed and then sold or removed between scans may have been missed." Supersedes the hourly/daily choice
    above (drop `frequency` from alert_settings, the hourly scheduler and `last_hourly_run`; update the popup /
    privacy copy that mentions hourly).
  - **DECIDED 2026-10-08 (Chuck agreed): which pills alert.** Chuck suggested an "alert on my
    entire Want List" option plus per-pill selection, shown as a vertical list with a bell beside each pill (instead
    of the cloud), so huge Want Lists don't flood people. Proposal: one alert mode per user, `all` (default; pills
    added later are included automatically) or `selected` (only belled pills). Since it's at most one email a day,
    a big list makes a longer email, not more emails, so also cap items per email (e.g. top ~25, newest first, then
    "+N more on gcgeartracker.com"). Chuck agreed: all by default, choose pills optionally, ~25 per email.
  - **Email "view all" link (Chuck, 2026-10-08)**: the "+N more" / "View all N matches" link opens the site on the
    Want List view showing exactly that email's matches. Proposed mechanics: link `gcgeartracker.com/?alert=<signed
    token>` → the page loads that alert's SKUs from the ledger (still-available ones, sold ones dropped) and shows them
    as a filtered Want List view. Do NOT auto-run a scan or rely on the NEW tag here — NEW is relative to the user's
    own last scan, so if they scanned on the site earlier that morning the emailed items would no longer be NEW, and
    an auto-scan would move their anchor. Requires login (signed token is per-user; logged-out → sign in, then land).
- **"New" = exactly the site's NEW-tag rule**: `date_listed` newer than an anchor, anchor only advanced on a scan
  with complete coverage. Late-arriving older-dated listings do not count (Chuck, 2026-10-01).
- **Matching** = the existing Postgres Want List matcher (`_tsquery_want_list_entry`, same one `/api/browse` uses),
  per pill, limited to the scan's new SKUs and `available`.
- **Sent ledger** per user, unique `(user_id, sku)`: an item is never emailed twice. Only still-available items are sent
  (daily summary re-checks availability at send time).

**Emails**
- Grouped by pill, ~20 items per pill, then "see all N" link (opens the site's Want List view for that pill).
- Subjects:
  - one item: `Want List Item Found: <item name>`
  - several: `Want List Items Found: N matches for <pills>`
  - daily: `Want List Daily: N new matches`
- From `GC Gear Tracker <alerts@gcgeartracker.com>`.
- Item links: `https://gcgeartracker.com/go/<sku>` → 302 to the GC listing. Logs nothing. Lets affiliate links be
  swapped in later in one place.
- Footer on every email: manage alerts · stop alerts for this pill · pause all. The last two are **one-click**, via
  HMAC-signed links (no login). Headers: `List-Unsubscribe` (URL) + `List-Unsubscribe-Post: List-Unsubscribe=One-Click`
  (RFC 8058). Plain-text part always included. Postmark open/click tracking **OFF**.

**Settings panel**
- master pause · frequency · masked address (`c•••@gmail.com`, computed at read time) with change (re-confirm by code)
  · send test email · separate **"Remove my email"**.
- Turning alerts off **keeps** the email. It is deleted only by "Remove my email" or account deletion.

**Account deletion** (doesn't exist for users today — see §2.1 — so it's part of this project)
- Confirm, then hard-delete ALL per-user data immediately: encrypted alert email, want list, watch list, favorites,
  saved searches, alert settings, pill subscriptions, codes, ledger, user row.

**Security (top priority)**
- Alert email encrypted at the app level; key in a Railway env var, never in the DB. The SQL box, `gc_users.db`, its
  daily backups and any export show only ciphertext.
- No route, admin page, export or log line ever outputs an address. (The only place an address is decrypted: building
  a Postmark request, and the masked hint for its owner.)
- Confirmation codes stored hashed, short expiry, attempt-limited and rate-limited. An unconfirmed address gets
  nothing beyond the one code email.
- **Spend circuit breaker**: hard global daily send ceiling. Hitting it pauses all alert sends for the day and warns
  the admin (Postmark bills overage automatically, so this is the only brake).

**Rollout**
- Beta: per-user flag on the admin users page + a global switch. Chuck only, then a few users. Everything invisible to
  unflagged users (no bell, no popup, routes 404/403).
- Announcement popup for logged-in users: at most once/day, count stored on the account, stops after 3 showings,
  "don't show again", or once alerts are set up. Includes the privacy promise + policy link. A logged-out
  "register to get alerts" variant is optional — **not decided**.
- Privacy policy promise: email stored encrypted, used only for alerts you turned on, never sold/rented/shared/used for
  marketing, deleted when you remove it or delete your account. Postmark processes it only to deliver messages and keeps
  a 45-day sending log. Encrypted backups rotate out (7 days — see §2.4).

**Postmark**
- Basic: $15/mo for 10K emails; maybe start on the free 100/mo developer plan while it's only Chuck.
- Domain `gcgeartracker.com`: DKIM TXT + Return-Path CNAME from Postmark, plus `_dmarc` TXT `v=DMARC1; p=none;` at the
  registrar.
- Approval request wording: opt-in Want List alerts for registered users, every address confirmed by code, one-click
  unsubscribe, no marketing.
- **OPEN**: ask Postmark whether hourly/daily alerts belong on the transactional or broadcast stream (confirmation
  codes are clearly transactional).
- `POSTMARK_SERVER_TOKEN` goes straight into Railway (serene-determination → web → Variables). Never in chat, logs, files.

---

## 2. What the code looks like today (investigation 2026-10-07, v2.17.9)

### 2.1 Account deletion — **no self-service deletion exists**
- Users can't delete their own account. The privacy policy says "request deletion … by contacting us".
- Admin only: `/admin/users` → `POST /admin/delete-user` with `action` = `schedule` (sets `users.deleted_at` = now+10
  days), `cancel`, or `now` (immediate). Scheduled deletions are purged **lazily, only when an admin opens
  `/admin/users`** (the purge loop is at the top of `admin_users()`), not on a timer.
- All three delete paths (admin `now`, the lazy purge, and the Google "import existing account" merge in
  `api_setup_google_account`) run the same two statements: `DELETE FROM user_data …` + `DELETE FROM users …`.
  Any new per-user table must be added to all three → plan: one `_purge_user(conn, user_id)` helper used everywhere.
- **Google-only accounts have no password** (`password_hash = ''`). "Confirm with password" can't work for them —
  needs another confirmation (see §4 open decisions).
- No per-user data lives in Postgres (`pg_schema.sql` has only `items`), so deletion is SQLite-only today.

### 2.2 Registration and email — **yes, an optional email is already collected (plaintext)**
- `POST /api/register` accepts an optional `email` (validated loosely, `UNIQUE COLLATE NOCASE`), stored plaintext in
  `users.email`.
- Google sign-in stores the Google account email plaintext in `users.email` too, and uses it to auto-link an existing
  account by **verified** email.
- `_is_admin()` relies on `users.email == ADMIN_EMAIL` (plus `google_id` set).
- `/api/me` returns only `has_email` (boolean), never the address. The header shows the username.
- Per the design these addresses are **not used for alerts**. Side note (not in this project's scope unless Chuck
  wants it): these plaintext addresses are in `gc_users.db` and its backups today, and the privacy policy says they're
  "used only for account recovery" — but there is no account recovery feature. Worth a later decision (stop collecting
  at registration? blind-index + encrypt?). Logged as an open item.

### 2.3 Privacy policy
- `GET /privacy` → `PRIVACY_TEMPLATE`, an inline HTML string in `gc_tracker_app.py` (~line 7990), "Last updated: May
  2026". Sections: Information We Collect, How We Use, Cookies, Third-Party Services (GA, Google Sign-In, Railway),
  Data Retention, Children, Changes, Contact.
- Needs (step 4): an "Email alerts" paragraph with the promise, Postmark under Third-Party Services, Data Retention
  updated for self-service deletion + 7-day backup rotation, the account-recovery sentence fixed, new date.

### 2.4 `gc_users.db` on Railway, backups, exports
- `USER_DB = DATA_DIR / "gc_users.db"`; on Railway `DATA_DIR=/data` (persistent volume on the web service). SQLite,
  WAL mode. The copy in the repo folder is a stale dev snapshot.
- **Backup**: `_maybe_backup_users_db()` (from the `after_request` hook) — first request of each UTC day does
  `VACUUM INTO /data/backups/gc_users_YYYYMMDD.db`, keeps the last **7**. So a deleted email survives ≤7 days in
  backups — as ciphertext only, once encrypted. No route serves or downloads these files.
- **Export**: `GET /api/export-data` (admin) — streams `gc_state.json`, stores cache, legacy global
  favorites/watchlist/keywords JSON files and the Postgres catalog. **No user rows.** `/api/import-data` — catalog only.
- **Admin users page** `SELECT`s `u.email` but never renders it. Step 1 removes the column from that query anyway.
- **Logs**: no `print` outputs an address today (checked every `email` reference). Device log
  (`gc_device_log.jsonl`) holds device id, UA, IP — no user/email.
- `send_file` is only used for the Excel download.

### 2.5 Admin users page (for the per-user beta flag)
- `admin_users()` builds a server-rendered HTML table: Username · Joined · Last scan · Last login · Watch · Want ·
  Favs · action (delete forms). Sortable via `/static/admin.js` (CSP: no inline JS).
- Actions are plain HTML `<form method="POST">` with a hidden `_csrf` from `_admin_page_csrf()`, validated with
  `hmac.compare_digest` against `session["_admin_csrf"]`, then redirect back. A beta toggle is the same pattern:
  `POST /admin/alerts-beta` {id, on/off, _csrf} → `users.alerts_beta`. Plus a column showing alerts state
  (beta flag, confirmed yes/no, # pills on, frequency) — never the address.
- Admin = `_is_admin()`; API routes use `_require_admin_api()`.

### 2.6 Other things that shape the build
- **CSRF**: global `before_request` Origin/Referer check blocks cross-origin POSTs but allows POSTs with neither header
  — mail providers' RFC 8058 one-click POSTs come server-side with no Origin, so they pass. The unsubscribe route
  must not require a session or CSRF token (the HMAC signature is the auth).
- **gunicorn**: 1 worker, 8 threads, `--timeout=0`. Background work = daemon threads (pattern: `_malloc_trim_loop`).
  Scheduler = a daemon thread that sleeps to the next :00, takes the Postgres advisory lock, runs, releases.
- **Scans**: `_run(...)` with a two-phase design since v2.17.4 (quick pass for NEW, then a silent background sweep);
  NEW uses each user's own `last_anchor`. Alerts need their **own global anchor** (`alerts_anchor`, max `date_listed`
  of the last complete-coverage run), same normalization (`_norm_item_date`) and the same "don't advance on a gap" rule.
  New SKUs for a run = `items` with `date_listed > alerts_anchor` and available — computed from Postgres, which also
  makes "reuse a scan that finished <10 min ago" trivial.
- **Want List** = `user_data.keywords` (JSON list of strings). A pill is its keyword text, so a subscription is keyed by
  `(user_id, keyword text)`; a subscription whose text no longer appears in the user's keywords is ignored (and
  cleaned up). `_tsquery_want_list_entry` can raise `_TsqueryUnsupported` → per-pill try/except, skip + count.
- **Deps**: `requirements.txt` = flask, flask-compress, requests, openpyxl, authlib, gunicorn, psycopg2-binary.
  `cryptography` arrives via authlib but will be listed explicitly. Postmark is called with `requests` (no SDK).
- **Boot safety** (v2.16.16 incident): missing alert env vars must disable alerts, never crash the app.

---

## 3. Data model (proposed — SQLite `gc_users.db`, next to users/user_data)

Per-user data stays in one database so account deletion is one transaction and the existing backup covers it.

```
users.alerts_beta        INTEGER DEFAULT 0            -- admin beta flag
users.alerts_popup_count INTEGER DEFAULT 0            -- step 5
users.alerts_popup_last  TEXT                          -- step 5 (UTC date)
users.alerts_popup_off   INTEGER DEFAULT 0            -- step 5 ("don't show again")

alert_email    (user_id PK → users.id,
                email_enc   TEXT NOT NULL,     -- Fernet ciphertext
                email_bidx  TEXT NOT NULL,     -- HMAC-SHA256(normalized email) for per-address rate limits / bounces
                confirmed_at TEXT, created_at TEXT, updated_at TEXT)
alert_settings (user_id PK, frequency 'hourly'|'daily', paused INTEGER, suppressed TEXT /* bounce/complaint reason */)
alert_pills    (user_id, keyword TEXT, created_at, PRIMARY KEY (user_id, keyword))
alert_codes    (user_id PK, email_enc, email_bidx, code_hash, expires_at, attempts, sent_at)
alert_sent     (user_id, sku, sent_at, PRIMARY KEY (user_id, sku))      -- step 2
alert_pending  (user_id, sku, keyword, found_at, PRIMARY KEY (user_id, sku))  -- step 2, daily queue
alert_sends    (day TEXT PK, count INTEGER)                              -- global daily ceiling counter
alert_meta     (k TEXT PK, v TEXT)   -- alerts_anchor, last_hourly_run, last_daily_run, global_switch, ceiling_hit
```
Keys (Railway env vars): `ALERTS_EMAIL_KEY` (Fernet; `MultiFernet` with `ALERTS_EMAIL_KEY_OLD` for rotation),
`ALERTS_HMAC_KEY` (code hashing, blind index, unsubscribe/pause link signatures), `POSTMARK_SERVER_TOKEN`.

---

## 4. Open decisions
1. ~~Account deletion confirmation for Google-only accounts~~ **DECIDED 2026-10-07**: password accounts confirm with
   their password; Google-only accounts re-authenticate with Google (OAuth round trip, delete on return within ~5 min).
2. ~~Self-service deletion timing~~ **DECIDED 2026-10-07**: immediate hard delete; only the ≤7-day encrypted backups remain.
3. Postmark stream for alerts: transactional vs broadcast — ask Postmark during approval.
4. ~~Plan~~ **DECIDED 2026-10-07**: start on Postmark's free developer plan (100/mo); upgrade to Basic before beta.
5. Logged-out "register to get alerts" popup variant — undecided.
6. Existing plaintext `users.email` (registration + Google) — separate later decision (§2.2).
7. Daily send ceiling value (suggest 500/day to start: Basic's 10K/mo ≈ 330/day).

---

**Built in v2.18.0 (2026-10-07):** step 1 below, as planned, plus `alert_code_sends` (48 h code-mailer rate-limit log;
kept on "Remove my email" so remove → re-add can't reset limits, purged on account deletion) and a default daily
ceiling of 50 (`ALERTS_DAILY_CEILING`) for the free plan. See HANDOFF.md v2.18.0.

## 5. Build order
1. **Plumbing, no UI** — Postmark + DNS, `send_email()` wrapper, encrypted storage, confirmation flow, admin-only test
   send. (Concrete plan: NEXT_SESSION_PROMPT.md / below.)
2. **Engine** — ledger, hourly scheduler + scan, daily summary, unsubscribe tokens/headers, bounce/complaint handling
   (Postmark webhook → `alert_settings.suppressed`), send ceiling.
3. **UI + email templates**, live for Chuck only (bell, setup box, settings panel, account deletion UI).
4. **Beta** — a few users, privacy policy updated, watch deliverability.
5. **Launch** — popup, global switch.

### Step 1 plan (v2.18.0) — Chuck approved 2026-10-07
- `requirements.txt`: add `cryptography`.
- Config: read `POSTMARK_SERVER_TOKEN`, `ALERTS_EMAIL_KEY`, `ALERTS_HMAC_KEY` at boot; if any missing/invalid →
  `_ALERTS_READY = False`, one log line naming *which variable* is missing (never values), app boots normally.
- Schema: create `alert_email`, `alert_settings`, `alert_codes`, `alert_sends`, `alert_meta`; add `users.alerts_beta`.
- Crypto helpers: `_enc_email()/_dec_email()` (Fernet/MultiFernet), `_email_bidx()`, `_mask_email()`,
  `_hash_code(user_id, code)` (HMAC-SHA256), constant-time compare.
- `send_email(to, subject, text, html=None, *, stream, tag, headers=None)` — the only path to Postmark:
  `POST https://api.postmarkapp.com/email`, `TrackOpens:false`, `TrackLinks:"None"`, 10 s timeout, checks + bumps the
  global daily counter, refuses when the ceiling is reached, logs only `tag`, Postmark `MessageID`/`ErrorCode`, never
  the address or subject. Local testing uses Postmark's `POSTMARK_API_TEST` token (validates, sends nothing).
- Confirmation flow (admin-only in step 1, beta-flag-gated later), JSON routes:
  - `POST /api/alerts/email/start {email}` → validate, generate 6-digit code (`secrets`), store hashed + encrypted
    pending address, 15-min expiry, send the code email (transactional). Limits: 1 send/60 s, 5/hour per user,
    5/day per address (blind index), plus a global hourly cap.
  - `POST /api/alerts/email/confirm {code}` → max 5 attempts per code; on success move into `alert_email`, delete the
    code row.
  - `GET /api/alerts/status` → `{ready, confirmed, masked, frequency, paused}` (never the address).
  - `POST /api/alerts/test-send` → sends a test email to the confirmed address.
  - `POST /api/alerts/email/remove` → deletes `alert_email` + `alert_codes` (+ settings).
- `_purge_user(conn, user_id)` helper used by all three existing deletion paths, covering the new tables.
- Admin users page: drop `u.email` from the SELECT.
- Test page: `/admin/alerts` — plain HTML forms with `_csrf` (no JS needed): enter address → enter code → test send →
  remove. Shows `_ALERTS_READY`, which env vars are present (yes/no), today's send count vs ceiling.
- Verify: py_compile, node --check, local run with a scratch `DATA_DIR` + `POSTMARK_API_TEST`: ciphertext only in the
  DB file (`strings gc_users.db | grep @` finds nothing), codes hashed, limits fire, missing-env boot works, logs
  contain no address. Then live: Chuck confirms his own address and receives a test send.

### Step 2 plan (v2.19.0, daily alert engine) — APPROVED by Chuck 2026-10-08, BUILT (see HANDOFF.md v2.19.0)
Supersedes the hourly parts of §1/§3. Still Chuck-only: a user gets alerts only if confirmed address AND
(admin or `users.alerts_beta`) AND the global switch (`alert_meta.global_switch`, default on for admin only).

**What we found in the code that shapes this**
- The background **sweep** (`_start_sweep()` → `_run(mode="sweep")`, v2.17.4) already does a full nationwide fetch
  (~20 s), writes Postgres, and reports `result.complete` (the nbHits coverage guard). A click during a sweep queues
  one follow-up sweep. So the 10 AM "scan" = call `_start_sweep()` and wait for a `complete` result — no new scan code.
- Every listing any scan saw stays in `items` (sold ones flip `available = false`), so "what's new since yesterday's
  alert" is a Postgres query on `date_listed`. Correction to my 2026-10-08 chat idea of "collecting matches from every
  scan during the day": not needed — those items are already in `items`, and the ones that sold before 10 AM shouldn't
  be emailed anyway. The disclaimer covers that gap.
- Matcher = `_tsquery_want_list_entry(kw)` per pill (same SQL the site's Want List uses), after `_kw_accept_capped`.
  `_TsqueryUnsupported` → skip that pill, count it.

**Schedule**
- Daemon thread (pattern: `_malloc_trim_loop`), wakes every 60 s. Fires once per ET calendar day at/after 10:00
  America/New_York (zoneinfo, DST-safe), every day including weekends. Catch-up after a deploy/restart: if today's run
  hasn't happened, run any time until 13:00 ET; after that skip the day (no evening surprise emails).
- `pg_try_advisory_lock(<const>)` on a pooled Postgres connection so only one container runs it during a deploy overlap;
  `alert_meta.last_daily_run = <ET date>` written when done → never twice a day.
- Run: `_start_sweep()`, wait for it to finish (max ~5 min). If `complete` is false: retry at +20 and +40 min. If still
  incomplete: send anyway from what's in Postgres, but don't advance anyone's anchor (ledger stops duplicates; the next
  day picks up anything missed).

**Who gets what (per user)**
- `alert_settings` gets `mode` ('all' default | 'selected') and `anchor` (TEXT, same normalized `date_listed` form as
  the NEW rule, `_norm_item_date`). `frequency` column left in place, ignored.
- `anchor` is set when the address is confirmed = current max available `date_listed` → a new subscriber's first alert
  covers only listings after they signed up, never the whole back catalog.
- `alert_pills (user_id, keyword, on INTEGER, updated_at, PK(user_id, keyword))`. Mode `all`: every Want List pill
  except rows with on=0. Mode `selected`: only rows with on=1. Pills whose text is no longer in the Want List are
  ignored (and pruned).
- Window for a user: `available AND date_listed > user.anchor AND date_listed <= run_max` (run_max = max date_listed
  after the sweep). One query per pill limited to the window's SKUs (a day's window is a few hundred to ~2,000 rows).
- Drop SKUs already in the ledger `alert_sent (user_id, sku, sent_at, PK(user_id, sku))`. Nothing left → no email.
- After a successful send, or no matches: write ledger rows and `anchor = run_max`. Paused/suppressed users: skip and
  advance the anchor (no backlog dump when they un-pause). Send failure or ceiling hit: anchor NOT advanced → tomorrow.
- Ledger pruned after 180 days.

**The email**
- Subject: one item → `Want List Item Found: <item name>`; several → `Want List Items Found: N matches for <pill>,
  <pill>` (+ "and N more" if long). From `GC Gear Tracker <alerts@gcgeartracker.com>`, transactional stream.
- Body (HTML + plain text): grouped by pill, newest first, **max 25 items total**; each: name, price (and price drop
  if any), condition, store, "View at Guitar Center" → `https://gcgeartracker.com/go/<sku>`.
- "View all N matches on GC Gear Tracker" → `/?alert=<id>` (the Want List view filtered to exactly this email's still-
  available SKUs, read from the ledger; login required; no scan, no NEW tag). The site-side view is step 3 UI; in step
  2 the link lands on the Want List page.
- Disclaimer (Chuck's wording, tidied): "Listings were available when we checked at 10 AM ET and may have sold since.
  Items that were listed and sold between checks may not appear."
- Footer: Manage alerts · Stop alerts for "<pill>" (one per pill shown) · Pause all alerts. The last two are one-click
  HMAC-signed links (no login): GET shows a small confirm page with a button, POST does it (link scanners in mail apps
  issue GETs, so GET never changes anything). Headers `List-Unsubscribe: <https://gcgeartracker.com/alerts/u/<token>>`
  + `List-Unsubscribe-Post: List-Unsubscribe=One-Click` (RFC 8058 POST → pause all, no confirm page).
- Tokens: HMAC(ALERTS_HMAC_KEY, "u:<user_id>:<action>:<pill>") — no expiry, invalid once the address is removed.

**New routes**
- `GET /go/<sku>` → 302 to that item's GC URL (only if it's a guitarcenter.com URL, else home). Logs nothing per user.
- `GET|POST /alerts/u/<token>` (pause all), `GET|POST /alerts/p/<token>` (stop one pill). CSRF-exempt by design (the
  signature is the auth; the global Origin check already lets header-less POSTs through).
- `POST /api/alerts/postmark-webhook` — Postmark Bounce + Spam Complaint + Subscription Change webhooks, protected by
  HTTP basic auth (new env var `ALERTS_WEBHOOK_SECRET`, set in Postmark's webhook URL). HardBounce / SpamComplaint /
  SuppressSending → `alert_settings.suppressed = <type>`; matched by blind index of the address, never logged.

**Admin (/admin/alerts additions)**
- Last run: time, sweep complete y/n, users considered / emailed / no matches / skipped (counts only, no addresses).
- Ceiling-hit warning. Global switch on/off.
- "Preview my alert" (dry run for Chuck's own account: shows the email that WOULD go out, sends nothing) and "Send my
  alert now" (real send to Chuck only, doesn't wait for 10 AM; uses the same window/ledger so it's a true test).

**Not in step 2** (step 3): the bell / vertical pill list UI, the settings panel, the `?alert=` view, account deletion UI.

**Verify locally** (cloud sandbox, throwaway Postgres + scratch DATA_DIR, Postmark mocked, fake clock): no email when
nothing matches; one email with grouped items when matches; never the same SKU twice; 25-item cap; anchor not
advanced on failure/ceiling/incomplete sweep; paused + suppressed skip; catch-up before 13:00 ET, skip after; DST
boundary; advisory lock blocks a second runner; one-click POST pauses, GET doesn't; bad token 404; webhook without
auth 401; no address in logs or DB plaintext. Then live: Chuck's own 10 AM alert the next morning.

---

## 6. Setup log (2026-10-07)

**Cloudflare (DNS host for gcgeartracker.com)** — before: `v=spf1 -all`, `_dmarc` = `p=reject; sp=reject; adkim=s; aspf=s`
(Cloudflare "domain sends no mail" lockdown), no MX. Now:
- Email Routing on: `chuck@gcgeartracker.com` → Chuck's Gmail (receive-only; replies go out from Gmail — fine).
  Added Cloudflare's 3 MX + `cf2024-1._domainkey` TXT; SPF replaced with `v=spf1 include:_spf.mx.cloudflare.net ~all`.
- Postmark: `pm-bounces` CNAME → `pm.mtasv.net` (DNS only); `20261007201050pm._domainkey` TXT (Postmark DKIM).
- `_dmarc` → `v=DMARC1; p=none;` (tighten to quarantine/reject once alert mail is passing; DKIM d=gcgeartracker.com
  aligns even under strict adkim).
- Leftover `_domainkey` TXT `v=DKIM1; p=` (old lockdown) is harmless — different name from the selectors in use.

**Postmark** — account owner `chuck@gcgeartracker.com` (Postmark rejects Gmail sign-ups). Server renamed
"GC Gear Tracker" (ID 21088536); open/link tracking off at server level too. Domain gcgeartracker.com: DKIM
Verified, Return-Path Verified. Account is in **test mode** (100 emails total, only to verified domains → test with
`chuck@gcgeartracker.com`, which forwards to Gmail). Approval form needs a real send first ("Send or receive an email").

**Approval request draft** (submit after the first live test send):
- Volume: 1 – 1,000 emails/mo (to start).
- Why Postmark: first provider for a small hobby site; chose Postmark for transactional deliverability and its
  no-marketing focus.
- Message types: (1) 6-digit confirmation codes when a registered user turns on alerts; (2) opt-in "Want List"
  alerts — when a used-gear listing matching a search the user saved appears at Guitar Center, an email with links
  to those listings, at most one email a day (~10 AM ET), sent only when there are new matches — no match, no email. No newsletters or marketing.
- Acquisition: only registered gcgeartracker.com users who click a bell on one of their saved searches, enter an
  address and confirm it with the emailed code (confirmed opt-in). Never imported or purchased lists; existing
  account emails are not used. Every alert has one-click unsubscribe (List-Unsubscribe + RFC 8058) and per-search
  stop links; bounces and spam complaints suppress the address via webhook; a hard daily send cap.
- Stream: transactional (resolved 2026-10-08 — Postmark's own docs list "individual alert emails the user has
  opted-in to receive" and per-user digest emails as transactional). Mention: in development, Chuck only → a few beta users;
  privacy policy (v2.18.1) at gcgeartracker.com/privacy describes the feature.

**Postmark APPROVED 2026-10-08** — submitted the form above (adjusted to the daily-alert wording, stream =
transactional; fields: volume 1-1,000/mo, why Postmark, message types, recipient acquisition) and Postmark showed
"approved" right away. Test mode is lifted: can send to any address. Still on the FREE plan (100 emails/mo) — keep
ALERTS_DAILY_CEILING at 50 until upgrading to Basic before beta. The form promised: one-click unsubscribe
(List-Unsubscribe + RFC 8058), pause/stop links, bounce/complaint suppression via webhook, daily cap — step 2 MUST
ship all of these before anyone but Chuck gets alerts.

**Railway + live test (2026-10-07)**: env vars POSTMARK_SERVER_TOKEN, ALERTS_EMAIL_KEY (backed up in Chuck's password
manager — losing it makes stored addresses unreadable), ALERTS_HMAC_KEY added to web. v2.18.0 deployed, `[alerts] ready`;
code → confirm → test email to chuck@gcgeartracker.com all worked.

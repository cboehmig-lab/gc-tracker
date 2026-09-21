# GC Gear Tracker — Phase F Design: Retire the JSON Catalog
*Drafted 2026-09-21. Discussion/planning document — no code shipped this session.
Current version at time of writing: v2.16.28. Follows from POSTGRES_MIGRATION_PLAN.md
(the master 6-phase plan; Phases A-E there are complete/deployed).*

## 0. Where this picks up

Phases A-E moved the catalog's **read** path to Postgres for the dominant traffic shapes
(Tier 1: no keywords/filter_q, v2.16.22; Tier 2 narrowed: keywords/filter_q with a store
subset or per-device scan anchor, v2.16.26) and built a proven, live-verified **write**
mirror (`_pg_sync_scan`, dual-writing since v2.16.15/Phase B). What's left: `gc_category_cache.json`
is still the thing `_run()` treats as the actual source of truth, `_cat_cache` (the ~92K-item,
~50MB+ dict) still loads at boot and stays resident in memory for the whole process lifetime
regardless of traffic, and several request paths outside `/api/browse` still read it directly.

Phase F = stop treating JSON/`_cat_cache` as the source of truth; Postgres becomes sole
source of truth for the catalog. Only after this does the original memory-growth mechanism
(materializing large temporary Python structures under concurrent thread load) become
**structurally impossible** rather than just avoided on the hot path — the actual point of
this whole migration, versus just keeping the `malloc_trim` mitigation (v2.16.13) forever.

## 1. User data survives untouched — and the one real risk to check

**Where want lists / watch lists / favorite stores / saved searches actually live**, confirmed
by reading the schema directly (`gc_users.db`, SQLite):

```sql
CREATE TABLE user_data (
    user_id     INTEGER PRIMARY KEY REFERENCES users(id),
    watchlist   TEXT DEFAULT '{}',   -- {sku: {name, price, store, url, condition, ...}}
    keywords    TEXT DEFAULT '[]',   -- want-list search terms
    favorites   TEXT DEFAULT '[]',   -- favorite STORE names (drives the store filter)
    last_run    TEXT DEFAULT '',
    new_ids     TEXT DEFAULT '[]',   -- SKUs currently flagged NEW for this user
    saved_searches TEXT DEFAULT '[]',
    last_anchor TEXT DEFAULT ''
)
```

This is a completely separate SQLite database file from the catalog. `POSTGRES_MIGRATION_PLAN.md`
§0 scoped `gc_users.db` **out of this migration from day one** ("recommend leaving as-is... isn't
the thing causing memory growth"). Phase F's scope — everything in this document — is entirely
about `gc_category_cache.json` / `_cat_cache` / the Postgres `items` table. Nothing in Phase F
reads, writes, or touches `user_data` or `gc_users.db` in any way. So every want list, watch list
entry, favorite store, saved search, and keyword survives Phase F automatically — not because
Phase F does anything new to protect them, but because they were never in scope to begin with.

**The only coupling is by reference.** `watchlist` keys and `new_ids` are SKU strings.
`api_browse()` determines "watched"/"isNew" by simple set membership against whatever the catalog
serves for that request (`sku in wl_ids`, `sku in new_ids`) — it doesn't store or duplicate catalog
data itself. The admin `/admin/users` page's per-user `wl_count` does the same kind of check
directly against `_cat_cache` today (line 2937-2943). As long as Postgres has the same SKUs the
JSON catalog has, every one of these references keeps resolving exactly as it does today.

**This is not a hypothetical risk** — it already happened once in this exact migration: Phase A's
original backfill (from a stale local `gc_category_cache.json` snapshot) undercounted, silently.
It was only caught and corrected by a full re-backfill from the *live* in-process `_cat_cache`,
confirmed via a parity check (436,343 == 436,343, zero missing/extra/mismatched) on 2026-09-02.
Every parity check since has been an aggregate row-count/field comparison — never specifically
cross-referenced against real users' watchlists.

**Design requirement for Phase F's cutover gate**: a targeted check, not just another aggregate
parity pass. Collect every SKU referenced in ANY user's `watchlist` or `new_ids` (trivial — it's
a local SQLite query against `gc_users.db`), then confirm each one resolves in Postgres `items`.
Zero tolerance: any gap gets backfilled before cutover, not after. This is specifically about
protecting real people's lists, not the catalog's aggregate stats.

One more thing worth stating plainly: `available=false` items are kept forever in both `_cat_cache`
and Postgres on purpose (Chuck's call, for a future price-history feature) — the 436K-vs-~111K-
active split is intentional, not a leak. So a watched item that sold months ago still resolves
(as sold) in Postgres exactly like it does in JSON today. Nothing changes there.

**Secondary finding, related but not identical**: a second, separate flat-file mechanism exists —
`gc_watchlist.json` via `load_watchlist()`/`save_watchlist()` (lines 1166-1177) — distinct from the
per-user `user_data.watchlist` SQLite column described above. It's touched only inside `_run()`'s
scan (a sold-marking check and a price/condition/location refresh loop, roughly lines 6488-6524)
and referenced by admin backup/reset (lines 5318-5349, alongside `gc_favorites.json`). An
exhaustive grep across the whole file for every reference to `WATCHLIST_FILE` / `gc_watchlist` /
`FAVORITES_FILE` / `gc_favorites` turned up **no GET/POST route that reads `load_watchlist()` to
serve a live request** — the actual per-user Watch/Want List UI appears to run entirely off
`user_data.watchlist` plus live matching inside `api_browse()`. This looks like a holdover from
before multi-user accounts existed (the code comment right above it notes sibling functions
`load_favorites`/`load_keywords` were already removed as dead code in the 2026-07 audit, E3).
It accounts for 3 of the 96 `_cat_cache` usage sites that would otherwise need converting to
Postgres lookups for Phase F. Worth a short, direct confirmation before Phase F ships: if it's
genuinely dead, drop it along with the rest of the cleanup (fewer things to migrate, one fewer
file on disk); if something reads it that this grep missed, it needs the same Postgres
redirection as everything else in §3.

## 2. Write path: replacing `_cat_cache` as the merge source

Today, `_run()`'s per-product loop reads `cached = _cat_cache.get(sku, {})` (line 6425) to build
the merge-source dict for each incoming Algolia record, writes the merged result back into
`_cat_cache[sku]` (line 6444), then a separate full-`_cat_cache` iteration does sold-marking
(line 6490 onward), then `_save_cat_cache()` persists JSON, and only *after* all of that does
`_pg_sync_scan()` mirror both effects into Postgres — best-effort, in a background thread, never
allowed to affect what the user sees.

The good news: `_pg_sync_scan`'s actual SQL is already built and has been live and correct since
v2.16.15 — the upsert (`_PG_UPSERT_SQL`/`_pg_row_for`/`execute_values`) and the anti-join
sold-marking UPDATE (temp table + `NOT EXISTS`) both run inside **one Postgres transaction**
(single connection, one `conn.commit()` at the end covering both effects) — so atomicity is
already solid; nothing new needed there.

What actually changes:

1. Before the per-item merge loop starts, one batched query —
   `SELECT sku, has_price_drop, price_drop_since, first_seen, is_vintage, condition_note,
   category, subcategory, condition, brand, location FROM items WHERE sku = ANY(:skus)` for
   every SKU in this run's `all_products` — replaces the per-item `_cat_cache.get(sku, {})` reads.
   One round trip, not one query per item, mirroring the batched-fetch pattern Tier 2's own
   `_pg_tier2_narrow_items` already uses.
2. The merge logic itself (the price-drop tracking, first_seen preservation, etc.) stays
   byte-identical Python — it doesn't care whether `cached` came from a dict lookup or a
   pre-fetched batch dict keyed the same way.
3. `_pg_sync_scan`'s upsert + sold-marking becomes the **primary, synchronous** write, not a
   best-effort mirror running after the "real" write already succeeded. This is the one change
   with real teeth: today, `_pg_sync_scan` failing just logs a line and changes nothing a user
   sees, because JSON already has the correct, complete data. After Phase F, that same failure
   means this scan's results never get persisted anywhere — there's no second copy. The scan
   needs to detect that failure (retry once, then mark the run failed/incomplete rather than
   reporting success) instead of the current fire-and-forget tolerance.
4. `_load_cat_cache()`, `_save_cat_cache()`, the `_cat_cache`/`_cat_cache_mtime` globals, and the
   memoized `_build_base_item_list()`/`_base_item_list_mtime` machinery are removed once nothing
   reads them (§3 covers what has to move first).
5. The watchlist refresh loop (~lines 6505-6521) is contingent on §1's `gc_watchlist.json`
   finding: drop it if confirmed dead, otherwise give it the same batched-SELECT treatment.

## 3. Read path: the genuinely hard part

Beyond the write path, every remaining `_cat_cache` read falls into one of these clusters:

- **Tier 1** — already 100% Postgres when eligible (v2.16.22). Its fallback to
  `_build_base_item_list()` only fires today when Postgres itself is unreachable — after Phase F
  that fallback has nothing left to fall to (§4).
- **Tier 2, narrowed case** (`_pg_tier2_would_narrow` true) — already 100% Postgres (v2.16.26).
  Same "fallback has nothing to fall to" issue as Tier 1.
- **Tier 2, RESIDUAL case** (`_pg_tier2_would_narrow` false — a nationwide search with no
  per-device last-scan anchor, e.g. a first-time scan) — still calls `_build_base_item_list()`
  today, i.e. still the exact full in-memory materialization Phase F exists to eliminate. This is
  the open question flagged in v2.16.23's own code comments since Phase E began, and it's the
  crux of this document. See below.
- **`/api/saved-search-counts`** (~line 4049) — independently calls `_load_cat_cache()` and builds
  its own `all_items` list, completely bypassing `_build_base_item_list()`. A second, separate
  full-materialization path Phase E never touched, because it isn't `/api/browse`.
- **`/api/state`'s `total_items`** (~line 5223) — `sum(1 for v in _cat_cache.values() if
  v.get("available"))` becomes a trivial `SELECT COUNT(*) FROM items WHERE available`.
- **`has_store_data`** (~line 4869) — `any(v.get("store") for v in _cat_cache.values())` becomes
  `SELECT EXISTS(SELECT 1 FROM items WHERE store != '')`.
- **Admin tooling** — the `wl_count` cross-check (~line 2937-2943), the catalog dump (~line
  3115-3119), and the Postgres parity/backfill tools themselves (`_pg_parity_check`,
  `_pg_full_backfill`, `migrate_cat_cache_to_pg.py`) exist specifically to reconcile Postgres
  *against* JSON — once JSON is gone, their entire purpose disappears. Retire rather than convert
  (see open questions).
- **Two reset-adjacent functions** (~lines 3223-3293, ~5356-5358) that blank/rebuild `_cat_cache`
  — need a Postgres equivalent (`TRUNCATE items` or a scoped DELETE). Not fully characterized in
  this pass; these look like destructive admin "wipe and rescan" tooling and deserve a careful
  read of their own before anyone touches them.

**The hard question, spelled out**: if Phase F simply points the Tier 2 residual case's
`SELECT * FROM items WHERE available` into Python on every such request instead of reading a
resident dict, that does *not* kill the memory-growth mechanism for that specific request shape
— it just moves where the temporary Python structure gets built from, and adds a full-table
network fetch on top. It *does* still achieve Phase F's primary, stated goal: the
always-resident 50MB+ dict that sits in memory 24/7 regardless of traffic goes away. The original
memory investigation (`perf_railway_memory_growth_2026-08-31`) was about that permanent baseline,
not this one occasional shape.

**Three options**:

1. **(Recommended for this phase) Accept the residual cost as a per-request cost, not a resident
   one.** Source the residual case's materialization from a fresh, uncached
   `SELECT * FROM items WHERE available` per request. Ships with essentially zero new logic — the
   existing `_kw_match`/`_apply_base`/sort/tiering code already accepts "a list of dicts in the
   exact shape `_build_base_item_list()` produces" from either source, per
   `_pg_tier2_narrow_items`'s own docstring. Kills the resident-memory baseline (the actual
   stated goal) even though this one narrow shape (nationwide + keyword + no scan anchor) still
   does a real per-request fetch. Matches the plan's own established discipline of not building
   machinery speculatively — ship the safe version, measure on Railway's memory graph, revisit
   only if this residual shape's cost turns out to matter in practice (the same evidence-gated
   process that decided Phase E itself).
2. **Push simple keyword terms into SQL (ILIKE/pg_trgm), keep the Python matcher for complex
   syntax.** Real payoff (kills the residual case's cost too, not just the resident baseline) but
   real risk — reimplementing any slice of `_kw_match` in SQL is exactly what
   `POSTGRES_MIGRATION_PLAN.md` §3 explicitly declined to rush, citing that matcher's own
   multi-session bug history (v2.13.1 DoS caps, v2.14.4 cap-starvation, v2.16.0 operators,
   v2.16.3 colon-prefix). Worth scoping later, as its own phase, only if Option 1's residual cost
   turns out to matter — not now.
3. **Server-side cursor / batched streaming instead of one big fetch.** Closest to "structurally
   impossible" without touching matching logic at all, but breaks cleanly against today's
   offset-based pagination and multi-pass contextual facet counting, both of which assume the
   full candidate list is available for reordering. Would need cursor-based pagination (a real
   API/UX change) to actually pay off — buffering every batch just to sort defeats the point.
   Parking this, not designing it further here.

## 4. Rollback story — the safety net changes shape

Every phase so far (D, E) had automatic, zero-thought rollback: a Postgres hiccup silently falls
back to the still-fresh, still-correct JSON path via the existing try/except, protecting real
traffic without anyone noticing. Phase F removes what that fallback falls *to*.

**Relevant evidence**: across every phase from B onward, Railway deploy logs have shown **zero**
fallback-path firings — the "falling back to JSON path" / "falling back to full cache" log lines
have never once fired in production. Postgres has been fully reliable through this entire
migration so far. That's real evidence for moving to fully-Postgres-with-confidence — not a
reason to skip deciding the fallback question deliberately.

**Recommendation**: stage this the same way D and E staged their own cutovers — don't stop
reading AND writing JSON in the same version bump. First cut reads over to Postgres-only (§3)
while still writing a JSON snapshot that nothing reads anymore (pure backup) for a burn-in
window; only stop generating the snapshot once that's run clean for a while. A periodic
write-only snapshot is not the old dual-write — no resident dict, no per-request read dependency
— so it doesn't reintroduce the mechanism Phase F exists to kill. It's cheap insurance, and it
mirrors the exact staged discipline that's kept every phase of this migration free of a real
production incident so far.

## 5. Verification plan

- Standing checks every phase has used: `python3 -m py_compile gc_tracker_app.py`,
  `node --check static/gc.js`, disposable-venv route-table import.
- **New this phase** — a write-path offline diff harness: synthetic scan data (reusing the
  sparse/malformed-row injection pattern from the Phase C/D harness, since that exact class of
  bug has already hit this migration twice), comparing the OLD (`_cat_cache`-sourced merge + JSON
  sold-marking) output against the NEW (Postgres-batched-SELECT-sourced merge) output byte-for-
  byte, before touching real data.
- The targeted user-data SKU parity check from §1 — every real watchlist/`new_ids` SKU across
  every real user account, cross-referenced against live Postgres, zero tolerance.
- Live spot-checks extended to the endpoints Phase F newly converts that D/E never touched:
  `/api/saved-search-counts`, `/api/state`, `has_store_data`, admin `wl_count`/dump.
- A forced-exception test proving whatever the agreed rollback story (§4) turns out to be
  behaves as designed under a real, simulated Postgres failure — same pattern every phase has
  used for its own cutover.

## 6. Open questions for Chuck

1. Tier 2 residual case (§3): ship Option 1 (accept per-request cost, no resident cache) for this
   phase, and only revisit SQL-keyword-narrowing if the memory graph shows it still matters?
   Recommended, but your call.
2. Rollback safety net (§4): keep a periodic Postgres→JSON archival snapshot (write-only, never
   read by the app) during and after cutover, or go fully Postgres-only with zero local fallback?
3. `gc_watchlist.json`/`load_watchlist()` (§1): looks dead based on this session's grep — want me
   to confirm fully and retire it as part of this phase's cleanup, or leave it out of scope?
4. Burn-in cadence: the same question asked before every prior phase started — how long do you
   want the "Postgres-only reads/writes, JSON snapshot still generated" stage to run before
   actually deleting the snapshot generation too?
5. `_pg_parity_check` / `_pg_full_backfill` / `migrate_cat_cache_to_pg.py` exist only to
   reconcile Postgres against JSON — retire them with this phase, or keep them around dormant?

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

**Resolved with real production data, 2026-09-21.** Rather than guess how often the syntax
features that don't map cleanly onto Postgres full-text search (`tsquery`) actually get used,
shipped a temporary admin-only diagnostic (`GET /api/search-syntax-stats`, v2.16.29 — see that
version's `HANDOFF.md` entry) and pulled real numbers from all 123 real accounts with saved
data:

| | |
|---|---|
| Total want-list keyword entries | 833 |
| — contain `*` at all | 8 (~1%) |
| — flagged "non-suffix" by the diagnostic's string-level check | 2 (~0.2%) |
| — quoted phrases | 27 |
| — `;` (OR) | 16 |
| — `-` (NOT) | 12 |
| — `,` (AND) | 149 |
| Total saved searches | 343 |
| — with `filter_strict` (fuzzy/contains-anywhere mode) set | **0** |
| — with non-empty `filter_q` | 302 |

Two things fall out of this immediately. First, `filter_strict` — the one syntax feature that
has no clean SQL translation at all, because it's arbitrary substring matching rather than
token matching — has **zero** measured real usage across every saved search in the app. (Caveat:
this only counts *saved* preferences, not an ephemeral toggle typed into a live, never-saved
search, so it's a proxy, not a certainty — but it's an unambiguous proxy at this magnitude, and
it's the best signal actually available.) Second, the two entries the diagnostic flagged as
"non-suffix" wildcards turned out, on inspection, to be false positives of a blunt check: the
sample entries are `"Ampeg, -pedal: AMG*; AMB*"` and `"Warwick, -rock*: Streamer; Fortress"` —
both use ordinary suffix wildcards (`AMG*`, `AMB*`, `rock*`), which map fine onto `tsquery`'s
`:*` prefix operator. They only got flagged because the diagnostic tested whether the *entire*
keyword string was literal-text-then-trailing-stars, and these are complex multi-clause entries
(colon-prefix expansion, comma-AND, dash-NOT, semicolon-OR) with an ordinary wildcard embedded
inside one branch — not an actual mid-word wildcard like `*caster`. Real prefix-breaking
wildcards appear to be effectively absent from the current corpus.

**Decision: build the full `tsquery` translation as the sole search mechanism**, not a hybrid.
Plain word, comma-AND, `;`-OR, `-`-NOT, quoted phrase, and suffix-wildcard together account for
essentially all real, measured usage (comma-AND alone is 18% of all entries; every other feature
maps directly onto a native `tsquery` operator — see `POSTGRES_MIGRATION_PLAN.md` §3's original
concern about reimplementing `_kw_match` in SQL, which this data addresses directly). The
`pg_trgm`/fuzzy-substring fallback that Option 2 (below, superseded) would have required is not
a build requirement for this phase — real usage of the one feature it exists for is zero. It
stays documented as a future escape hatch, not something Phase F needs to ship.

This resolves the Tier 2 residual case completely, not just narrows it: once keyword matching
itself runs in SQL, there's no request shape left where the full catalog needs to land in Python
— a nationwide, no-anchor, keyword-active request now gets exactly the matching rows back from
Postgres, the same as every other shape. That also means Tier 1 and Tier 2 can collapse into one
Postgres query path (`WHERE available [AND store = ANY(...)] [AND search_vector @@ tsquery(...)]
ORDER BY ... LIMIT ... OFFSET ...`) instead of today's two-tier split with a Python matcher stuck
on the end of one of them — genuinely simpler code, not just faster.

The existing Python `_kw_match`/`_compile_query`/`_wl_bool_compile` machinery (real engineering,
several versions of DoS-cap tuning) is not deleted — it stays in the codebase, unused on the hot
path, as a manual escape hatch if a genuine need for arbitrary substring matching ever shows up.
And "zero in today's 833 entries" isn't "impossible forever" — someone can type `*caster`
tomorrow. The honest residual risk is a compound rare case (a truly mid-word wildcard, in a
nationwide search, with no scan-history anchor, all at once) that the `tsquery` translator can't
express exactly; the plan is to degrade that one term gracefully (translate it to the nearest
expressible form, e.g. its longest literal prefix) rather than build fallback machinery for a
shape that's shown zero occurrences across every real want list in the app — the same
evidence-gated discipline every phase of this migration has used, applied one more time.

**What replaces the old three-option list above**: Option 1 (accept per-request cost) and Option
3 (streaming cursor) are moot — there's no residual full-materialization case left to accept a
cost for. Option 2 (hybrid ILIKE/ pg_trgm) is superseded by the cleaner, now-validated full
`tsquery` translation described above.

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
- **New given the tsquery decision (§3)**: diff-test the `tsquery` translator against ALL 833
  real want-list keyword entries and all 343 real saved-search `filter_q` values (via
  `/api/search-syntax-stats`'s underlying query, or a proper export) pulled from production —
  not a sample, the actual corpus, since ~1,176 real strings is small enough to test in full.
  Compare old-Python-matcher output vs. new-tsquery output for every one, byte-for-byte, plus
  synthetic edge cases the real data happens not to contain (genuine mid-word wildcards like
  `*caster`, since "zero seen so far" isn't "will never occur").
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

1. ~~Tier 2 residual case~~ **RESOLVED 2026-09-21** (§3): real production data (123 accounts,
   833 keyword entries, 343 saved searches) shows the syntax that doesn't map cleanly onto
   Postgres full-text search (`filter_strict` fuzzy mode) has zero measured real usage, and the
   handful of flagged "non-suffix" wildcards turned out to be ordinary suffix wildcards inside
   complex expressions. Decision: build the full `tsquery` translation as the sole search
   mechanism — no hybrid, no accepted residual cost, Tier 1/Tier 2 collapse into one Postgres
   query path. See §3 for the full writeup.
2. Rollback safety net (§4): keep a periodic Postgres→JSON archival snapshot (write-only, never
   read by the app) during and after cutover, or go fully Postgres-only with zero local fallback?
3. `gc_watchlist.json`/`load_watchlist()` (§1): looks dead based on this session's grep — want me
   to confirm fully and retire it as part of this phase's cleanup, or leave it out of scope?
4. Burn-in cadence: the same question asked before every prior phase started — how long do you
   want the "Postgres-only reads/writes, JSON snapshot still generated" stage to run before
   actually deleting the snapshot generation too?
5. `_pg_parity_check` / `_pg_full_backfill` / `migrate_cat_cache_to_pg.py` exist only to
   reconcile Postgres against JSON — retire them with this phase, or keep them around dormant?

## 7. Recommended next step — the search-engine build, sequenced

With the dead syntax gone (v2.16.30) and the real-data verdict locked in (§3), this is the
concrete build order for the `tsquery` engine — the piece that actually eliminates the Tier 2
residual case and collapses Tier 1/Tier 2 into one query path.

1. **Schema**: add a generated `search_vector tsvector` column to `items`
   (`to_tsvector('simple', coalesce(name,'') || ' ' || coalesce(brand,''))`) plus a GIN index,
   built `CONCURRENTLY` so it doesn't lock production reads/writes while it builds. `'simple'`
   config on purpose — no stemming, no stopword removal — so matching stays close to today's
   literal whole-word semantics rather than introducing new fuzziness nobody asked for. Folded
   into `pg_schema.sql` (idempotent `IF NOT EXISTS`, same pattern every other column there
   already uses) so `_init_pg_schema()` applies it on its own.

2. **Translator**: a new function turning the existing PARSED structure — `_compile_query`'s
   AND/phrase/suffix-wildcard token classification and `_wl_bool_compile`'s OR-of-AND-with-NOT
   clause shape, both already built and already used for `_expand_colon_prefix` preprocessing —
   into a Postgres query, not into Python regexes. Safety detail worth locking in now: build
   this via multiple parameterized `to_tsquery('simple', %s)` calls combined with SQL's own
   `&&`/`||`/`!!` tsquery operators in the query itself, rather than string-concatenating one raw
   tsquery expression in Python — avoids ever having to hand-escape tsquery's own operator
   syntax (`&`, `|`, `!`, `<->`, `:`) against a user-typed term.

3. **Verification**: extend `/api/search-syntax-stats`'s underlying query into a real diff
   harness — run every one of the 833 real keyword entries and 343 real saved-search `filter_q`
   values through the OLD Python matcher and the NEW translator over the same catalog snapshot,
   diff the resulting SKU sets byte-for-byte. Add synthetic edge cases the real data doesn't
   happen to contain (a genuine mid-word wildcard, empty/punctuation-only strings, apostrophes in
   brand names, very long input) — same "don't trust a clean sample, inject the ugly cases"
   discipline as the Phase C/D harnesses.

4. **Unify Tier 1 + Tier 2**: one `_pg_browse()` replacing `_pg_tier1_browse`,
   `_pg_tier2_narrow_items`, and the Python `_kw_match`/`_apply_base` text-filtering step —
   availability + store + scan-gate + facet filters + `search_vector @@ (...)` all in one WHERE
   clause, sort/paginate in SQL. The genuinely hard sub-piece: today's "contextual" facet counts
   (each facet's count reflects every OTHER active facet, never itself) are computed by a Python
   single-pass loop — replicating that in SQL (separate `FILTER (WHERE ...)` aggregates, mirroring
   what Tier 1's own Q1 query already does for its counts) is real work and deserves its own
   careful pass, not a rushed afterthought.
5. Ship it the same way every phase has: shadow-mode first (`?pg_shadow=1`, admin-only, diffed
   against live production), then cutover — never a direct hot-swap.

Once search itself is proven this way, it plugs directly into the write-path and rollback design
already written in §2 and §4 — the search engine was the one genuinely open sub-problem in this
document; the rest of the plan doesn't change.

---

### Stage 1 — SHIPPED (v2.16.31, 2026-09-22)

The generated `search_vector tsvector` column + GIN index from step 1 above are in
`pg_schema.sql`, but NOT quite as step 1 originally described ("folded into `pg_schema.sql`...
so `_init_pg_schema()` applies it on its own" — true in spirit, but the mechanism needed a real
fix, not a drop-in append).

**The bug this surfaced**: `_init_pg_schema()` and `migrate_cat_cache_to_pg.py` both apply
`pg_schema.sql` as ONE multi-statement string via a single `cur.execute()` call on a connection
with `autocommit=False` — i.e. inside an explicit transaction. Postgres rejects `CREATE INDEX
CONCURRENTLY` outright when it runs inside a transaction block, full stop, regardless of
`IF NOT EXISTS`. Appending the GIN index as literally described in step 1 would have made
`_init_pg_schema()` throw on every app startup from the moment this file first deployed —
caught by the function's own try/except (so it wouldn't have blocked startup), but the index
would never have been built, silently, forever, with no signal beyond a Railway log line.

**Fix**: `pg_schema.sql` now has a `-- ==CONCURRENT-INDEXES==` marker. Both callers
(`_init_pg_schema()`, `migrate_cat_cache_to_pg.py`) split the file on that marker: everything
above runs as before (one transaction, one commit); the single `CREATE INDEX CONCURRENTLY IF NOT
EXISTS` statement below it runs afterward on its own `autocommit=True` connection, its own
statement, its own try/except (a build failure/timeout there doesn't block startup or roll back
the schema changes that already committed). `pg_schema.sql` also documents the CONCURRENTLY
leftover-invalid-index operational gotcha (a build interrupted by a redeploy leaves an index
Postgres considers "exists" but won't use — `IF NOT EXISTS` won't rebuild it) with the
`pg_index.indisvalid` check to diagnose it and the `DROP INDEX CONCURRENTLY` to clear it.

**Verified**: `python3 -m py_compile` on both `gc_tracker_app.py` and
`migrate_cat_cache_to_pg.py`, `node --check static/gc.js` (unchanged, checked anyway), a
disposable-venv import + route-table build (61 routes, unchanged — this is a schema-only change,
no routes touched — `/api/browse` present, `APP_VERSION` confirmed `2.16.31`), and a standalone
Python check that `pg_schema.sql`'s own marker-partition produces exactly the expected split
(`main_sql` ends cleanly at the `ALTER TABLE` statement, `concurrent_sql` contains exactly the
one `CREATE INDEX CONCURRENTLY` statement, ignoring comments).

**NOT verified**: no live Postgres was reachable from the device shell this session (no root, no
Docker/Homebrew, apt blocked — same constraint noted in the 2026-09-04/08 sessions), so none of
this DDL has actually run against a real server yet. The `ALTER TABLE ... ADD COLUMN ...
GENERATED ALWAYS AS (...) STORED` is a full-table rewrite under an `ACCESS EXCLUSIVE` lock — for
~450K rows (including historical sold/delisted items `_cat_cache` keeps forever) expect it to
take real seconds-to-tens-of-seconds during the FIRST startup after this deploys, not
instantaneous. Since this app runs `--workers=1` (deliberately, for in-process scan/SSE state —
see HANDOFF.md's v2.16.10 entry) and Railway is not known to run >1 replica of this service, the
lock should only be visible as slightly extended startup time on that one deploy, not as
contention with a second live instance — worth Chuck double-checking Railway's replica count
before/after this deploys, since that assumption was not verified against the Railway dashboard
this session. After deploy, check Railway's logs for `[pg] items table ready` followed by either
`[pg] concurrent indexes ready` or a `[pg] concurrent index build skipped: ...` line with the
actual exception — that line is the only signal this new code path worked.

**Next**: stage 2, the `tsquery` translator (step 2 above) — not started.

---

### Stage 2 — SHIPPED (v2.16.32, 2026-09-22): the tsquery translator

Five new module-level functions, placed right after `_wl_bool_compile` (the "Shared
query-matching helpers" section): `_tsquery_compile_term`, `_tsquery_compile_query`,
`_tsquery_bool_clauses`, `_tsquery_want_list_entry`, `_tsquery_filter_q`, plus a
`_TsqueryUnsupported` exception. **Not wired into any live route yet** — nothing calls these,
`_compile_query`/`_wl_bool_compile`/`_compile_fq_clauses`/`_kw_match` are untouched and still
what every real request uses. That wiring is step 4 (unify Tier 1/Tier 2), which needs step 3
(the live diff harness, see below) to pass first.

**Approach**: each function mirrors its Python-matcher counterpart's OWN parsing/routing
decisions one for one (same comma=AND, `;`=OR, leading `-`=NOT, same gate for when the OR/NOT
path even engages — e.g. `_tsquery_bool_clauses` reimplements `_wl_bool_compile`'s exact
`has_neg`/`;` check so it returns `(None, [])` in precisely the same cases, meaning "OD-1"
correctly falls through to the plain path instead of being misparsed as NOT-syntax) — rather than
re-deriving the rules independently. This means auditing it is a side-by-side read against
`_compile_query`/`_wl_bool_compile`/`_compile_fq_clauses`, not a fresh review from scratch.
One real behavioral divergence caught doing this line-by-line: `_compile_fq_clauses` (filter_q)
KEEPS an all-negative clause (`if pos or neg:`) — `"-electric"` alone means "everything except
electric" — while `_wl_bool_compile` (want-list) REQUIRES at least one positive term per clause
and drops an all-negative entry entirely. `_tsquery_filter_q` and `_tsquery_bool_clauses` preserve
this exact asymmetry; getting it wrong either direction would have been a silent, hard-to-notice
divergence.

Builds each entry as multiple parameterized `to_tsquery()`/`phraseto_tsquery()` calls joined by
SQL's own tsquery operators (`&&`/`||`/`!!`) in the query text — never string-concatenating raw
tsquery syntax — exactly per step 2's safety note (a user-typed term can never be interpreted as
tsquery operator syntax this way). `phraseto_tsquery('simple', %s)` turns out to be the right
native primitive for BOTH plain single words and un-quoted multi-word phrases (Postgres handles
the single-lexeme case fine too), which is cleaner than hand-building `<->` chains term by term.

**Two deliberate, documented semantic narrowings** (both directions are "tsquery is stricter /
more correct", never looser — see the long code comment above the functions for the full
reasoning):
1. Suffix wildcards (`OD*`) → `to_tsquery('simple','od:*')`, a true lexeme-PREFIX match. The
   Python regex is unanchored substring matching over the whole "name brand" text (no boundary at
   the wildcard end), so `OD*` can incidentally match inside "Wood" today; tsquery's prefix match
   won't.
2. Quoted `"exact phrase"` → `phraseto_tsquery` (word-boundary-respecting), vs. the Python path's
   raw substring containment (`_matches_all`'s `val in text_lower`), which technically allows
   mid-word matches (`"amp"` matching "trampoline"). This is genuinely the closest available
   tsquery primitive — there's no cheap way to express "phrase as a raw substring, mid-word
   matches allowed" in tsquery without pg_trgm, which §3 already ruled out for this phase (real
   usage of the one feature that needed it was zero).

Anything not expressible in pure tsquery (a non-suffix wildcard — leading, mid-word, multiple
stars, or a wildcard inside a multi-word term) raises `_TsqueryUnsupported` rather than being
silently mistranslated. §3's real-data pass found zero non-suffix-wildcard usage in production, so
this is expected to never actually fire — but step 4's job is to catch it per-entry and fall back
to the dormant `_kw_match` path for just that one entry, never guess or drop it silently.

**Verified this session** (structural only — see the real gap below): `python3 -m py_compile`,
`node --check` (untouched), a disposable-venv import + 61-route table build (unchanged — nothing
wired to a route yet), and a 22-case self-test
(`.phase_working/test_tsquery_translator.py`, not committed — scratch, kept locally) exercising
every branch: plain word, quoted phrase, suffix wildcard, three flavors of unsupported wildcard
(leading/mid-word/multi-word), comma-AND, `;`-OR, leading-`-`-NOT, the internal-hyphen
non-NOT-syntax case (`"OD-1"`), colon-prefix expansion, the want-list-vs-filter_q all-negative-
clause asymmetry, and the token/clause budget caps. All 22 pass. **This checks STRUCTURE only —
does each input route correctly and produce the right fragment shape/params — not that the
generated SQL actually matches the same SKUs as the Python matcher when run against real
Postgres**, because this sandbox has no live Postgres access (same constraint noted for stage 1).

**Step 3 (the real diff harness) — designed, not built, and NOT a Chuck's-Mac-terminal script
like `migrate_cat_cache_to_pg.py`.** Investigated where the 833+343 real strings actually live:
`gc_users.db` (`user_data.keywords`/`user_data.saved_searches`) is on the Railway volume, not on
Chuck's Mac — `migrate_cat_cache_to_pg.py` only works locally because `gc_category_cache.json` is
a file Chuck happens to have a local copy of; there's no local equivalent for `gc_users.db`.
`/api/search-syntax-stats` (v2.16.29) establishes the right precedent instead: an admin-only,
read-only endpoint that runs SERVER-SIDE (where both `_cat_cache`/`gc_users.db` and
`PG_DATABASE_URL`/`_PG_POOL` already live in the same process) and returns AGGREGATE counts plus a
small capped sample — deliberately never a user's full keyword/saved-search list. The diff harness
should follow the same shape: a new admin endpoint (e.g. `/api/tsquery-diff-check`) that, for each
real keyword/filter_q string, (a) runs it through the existing single-entry `_compile_query`/
`_wl_bool_compile` path against the in-memory `_cat_cache` to get the OLD matching SKU set
(compare against the FULL catalog, not just `available` items, so it's apples-to-apples with
Postgres's `items` table, which also keeps full sold/delisted history), (b) runs it through the
new translator and executes `SELECT sku FROM items WHERE search_vector @@ (...)` via `_pg_conn()`
to get the NEW set, (c) diffs the two sets, and (d) returns aggregate mismatch counts plus a
capped sample of just the MISMATCHING entries' raw text (same privacy posture as
`/api/search-syntax-stats`'s existing sample fields) — never SKUs or item names. This is real,
scoped follow-up work, not done this session; building AND shipping a new live-Postgres-querying
admin endpoint in the same pass as the translator itself risked rushing something
privacy-sensitive, and Chuck's ask this session was specifically the translator (stage 2).

**Next**: implement and ship the `/api/tsquery-diff-check` endpoint described above, run it, and
only proceed to step 4 (unifying Tier 1/Tier 2 into `_pg_browse()`) once it comes back clean on
all 833+343 real strings.

### Stage 3 — SHIPPED (v2.16.33, 2026-09-22): the tsquery diff-check admin endpoint

Built the endpoint designed above almost exactly as specified, with one deliberate addition: the
naive per-entry brute force described in the stage-2 addendum (`_compile_query`/`_wl_bool_compile`
run directly against every item in `_cat_cache` for every one of the ~1176 real strings) turns out
to be on the order of ~500M Python-level regex/substring operations — unacceptable GIL-held time
on this app's shared `--workers=1` gunicorn config, even from a background thread (the GIL is
still shared with every real customer request). So the actual ground-truth matcher
(`_tsquery_diff_old_want_list_matches`/`_tsquery_diff_old_filter_q_matches`) narrows candidates
first via a one-time inverted token index (`_tsquery_diff_build_catalog_index`) before running the
real matcher primitives, rather than scanning the full catalog per entry.

**Why the narrowing is sound, not an approximation**: a term's REQUIRED tokens (the words of its
plain, non-wildcard, non-quoted subterms — `_plain_req_tokens`, factored out from the exact rule
`_wl_bool_compile`/`_kw_and` already use in production) are a NECESSARY condition for a match, so
intersecting the token index's postings lists for those required tokens is a SUPERSET of the true
match set — it can only narrow candidates, never drop a true one. A bare wildcard or quoted-exact
term gets NO pre-filter here, same as production gives it none: both do unanchored substring
matching that isn't aligned to token boundaries (`'OD*'` matches inside "Wood" — a single token
that doesn't START with "od"; `'"amp"'` matches inside "Trampoline" — a single token that doesn't
CONTAIN "amp" as a separate word), so a token-boundary index would silently exclude true
candidates for exactly those two term types. They fall back to a full-catalog scan, unchanged —
same as production's own "exotic" bucket, which also can't pre-filter them. Once narrowed, the
REAL unmodified matcher primitives (`_matches_all`/`_matches_any`/`_wl_bool_compile`/
`_compile_query`/`_compile_fq_clauses`'s own per-token calls) run as ground truth — this module
never re-derives matching *rules*, only which items are worth checking against them.

**Endpoint shape**: `POST /api/tsquery-diff-check` (admin-gated, 409 if already running) starts a
background thread with its own dedicated `_TSQUERY_DIFF_LOCK`/`_TSQUERY_DIFF_STATE` — deliberately
NOT the scan `_lock`/`_stop_event`/`_q` (that's the inventory-scan SSE stream; sharing it would
mean this diagnostic blocks, or is blocked by, a real scan). `GET /api/tsquery-diff-check` polls
status/result. The Postgres side of each comparison runs the stage-2 translator's own SQL fragment
unmodified via a dedicated ad-hoc `psycopg2.connect()` (autocommit, not the pooled `_pg_conn()`) —
matching the existing precedent set by `_pg_parity_check`/`_pg_full_backfill`: a rare,
background-thread-triggered diagnostic that can run for a while (one query per distinct entry, up
to ~1176) shouldn't hold a slot out of the connection pool's 2-12 connections, which are sized for
concurrent request threads. A per-entry Postgres query failure is caught and counted separately
(`errors`) rather than aborting the whole run.

**Universe consistency**: the catalog index and the ground-truth matcher cover `_cat_cache` in
full — available or not. Postgres's `items` table mirrors `_cat_cache` 1:1 regardless of
availability (Phase B dual-write), and this endpoint is testing TEXT-matching correctness, not
availability filtering, so a mismatch here should never just be an availability-filter difference
in disguise. The Postgres query has no `available` filter either, for the same reason.

**Privacy**: identical posture to `/api/search-syntax-stats` — per group (`keywords`,
`saved_searches`), aggregate counts (`total`/`matched`/`mismatched`/`unsupported`/`errors`) plus a
capped sample (20) of MISMATCHING/UNSUPPORTED/ERRORED entry TEXT only. Never SKUs, item names, or
any other per-user data.

**Verified this session** (structural only — the real gap is below): `python3 -m py_compile`,
`node --check` (untouched), a disposable venv (see the build-environment note — had to build it in
`/tmp`, not the mounted connected folder, which fails `ensurepip`) confirming the app imports
cleanly and registers all 63 routes including both new `/api/tsquery-diff-check` routes. Then a
21-case self-test (`.phase_working/test_diff_harness.py`, not committed — scratch) against a
hand-built synthetic catalog specifically constructed to hit the two unsound-narrowing traps
(`"Wood"` for `'OD*'`, `"Trampoline"` for `'"amp"'`) plus colon-prefix expansion, comma-AND, bool
OR/NOT via `;`/`-`, the internal-hyphen-isn't-NOT-syntax case, legacy `=` strip, and an
all-negative `filter_q` clause. Compares the new candidate-narrowed ground truth against a naive
brute-force reimplementation of the exact same production routing with NO indexing at all — all 21
cases (14 want-list + 7 filter_q) match exactly.

**The real gap, same shape as stage 1 and 2**: this only proves the NEW ground-truth code (the
thing that has to stay correct for the diff check to mean anything) agrees with the OLD production
logic it mirrors, on synthetic data. It does **not** yet prove the stage-2 translator's tsquery
output matches the same SKUs as the Python matcher on REAL production want-list/filter_q strings,
because this sandbox still has no live Postgres access. That comparison only happens once
v2.16.33 is deployed and an admin actually calls `POST /api/tsquery-diff-check` against the real
Railway database.

**A build-environment note for future sessions**: `python3 -m venv` fails with an `ensurepip`
error when the venv is created inside the FUSE-mounted connected folder (`~/mnt/gc_tracker/...`).
Build disposable venvs in `/tmp` instead — a real local filesystem on the device, separate from
both the mounted folder and the device VM's own `$HOME` (which stays persistently near-full; keep
using the mounted folder, e.g. `.phase_working/`, for scratch files, never bare `$HOME` — see the
disk-space note from earlier this session in project memory). `/tmp` had 4.2G free this session.

**Next**: deploy v2.16.33, run `POST /api/tsquery-diff-check` against real production data, and
read the results. Clean (zero real mismatches, `unsupported` accounted for by the known
zero-real-usage non-suffix-wildcard case) means stage 2's translator is proven and step 4 (unify
Tier 1/Tier 2 into one `_pg_browse()`, with a per-entry fallback to the dormant `_kw_match` path
for any `_TsqueryUnsupported` case) can start. Any real mismatch needs to be reproduced by hand for
that one entry and diagnosed as either a stage-2 translator bug or a stage-3 ground-truth-mirror
bug before touching any code — the stage-3 module's own docstring in `gc_tracker_app.py` lists the
two known, deliberate, already-documented semantic narrowings (suffix-wildcard prefix matching,
quoted-phrase word-boundary matching), so a mismatch explained by one of those is expected, not a
bug.

### Stage 3 RUN against production, v2.16.34 (2026-09-22): real translator bug found + fixed; two more findings need a decision

Ran `POST /api/tsquery-diff-check` against real production data for the first time (via an admin
browser session). Not clean: 48/810 want-list keywords, 23/294 saved searches mismatched. Root-
caused every distinct pattern using a local throwaway Postgres cluster in the sandbox (`postgresql-
16` was already installed; `pg_ctlcluster 16 main start`, a scratch `gc_test` database,
`pg_schema.sql` applied verbatim) with synthetic catalogs designed to isolate each hypothesis —
this closes the "never verified against live Postgres" gap flagged in every addendum since stage 1,
without touching a single row of real production data.

**Bug found and fixed**: `_tsquery_bool_clauses` (want-list bool/OR/NOT syntax) and
`_tsquery_filter_q` (search box / saved searches) both built `pos_frags`/`neg_frags` from the
input terms, but appended each term's SQL params to a single flat `params` list in ORIGINAL
left-to-right encounter order — while the SQL fragment string itself is built as
`pos_frags + [negated frags]`, reordering positives before negatives. Whenever a clause had a
negative term appear BEFORE a later positive term (e.g. `'Ampeg, -pedal, AMG*'` — `-pedal` sits
between the two positives), the flat params list no longer lined up with the SQL string's `%s`
placeholders (psycopg2 substitutes strictly left-to-right through the SQL text) — so params landed
in the WRONG placeholders. Depending on term types this could silently search for entirely the
wrong words, or invert a NOT into a positive requirement.

Confirmed mechanism directly:
```
_tsquery_bool_clauses('Ampeg, -pedal, AMG*')
  -> frag:   "(phraseto_tsquery('simple', %s) && to_tsquery('simple', %s) && !!(phraseto_tsquery('simple', %s)))"
  -> params: ['Ampeg', 'pedal', 'AMG:*']     # WRONG — pedal/AMG:* swapped vs. their placeholders
```
Then reproduced the real-world impact against a synthetic catalog on real Postgres: for
`'Ampeg, -pedal: AMG*; AMB*'`, the buggy translator matched `SKU2` ("Ampeg Pedal Tuner" — the ONE
item that IS a pedal, meant to be excluded) and missed `SKU1`/`SKU5` (the two genuine Ampeg
AMG/AMB matches) — the exact inversion the params-mismatch mechanism predicts. This is almost
certainly the root cause of the strongest signal in the production diff-check output: entries like
`Ampeg, -pedal: AMG*; AMB*` and `Carvin, -amp, -cabinet, -acoustic: guitar` showing COMPLETELY
DISJOINT result sets (zero overlap) between old and pg — a structural bug, not a semantic nuance.
A clause with the negative term already LAST (`'Ampeg, AMG*, -pedal'`) happened to work by
coincidence (params order matched placeholder order by luck of the ordering), which is why stage
2's original structural self-test — whose bool-syntax cases all happened to put `-` last — never
caught this.

**Fix**: track `pos_params`/`neg_params` in parallel with `pos_frags`/`neg_frags` per clause, and
append them to the shared `params` list in the SAME order the fragments are joined into the SQL
string (positives, then negatives) rather than original encounter order. Identical fix applied to
both functions, since both had the identical bug shape.

**Verified**: (1) the exact synthetic repro above, confirmed fixed against real Postgres — same
catalog, same query, now matches `{SKU1, SKU5}` correctly with params
`['Ampeg', 'AMG:*', 'pedal']` correctly aligned. (2) `py_compile`/`node --check` clean. (3) Ran the
FULL stage-2 translator (not just Python-side structural comparison) against real Postgres over
the stage-3 self-test's existing 18-item catalog plus new negative-term-ordering cases (`-pedal`
first/middle/last, both want-list and filter_q) — 22/26 cases pass exactly. The 4 "mismatches" are
the two ALREADY-DOCUMENTED deliberate semantic narrowings from stage 2's own module docstring
(wildcard suffix → true lexeme-prefix match, not unanchored substring; quoted phrase →
word-boundary phrase match, not raw substring containment) — confirmed working exactly as
designed on live Postgres, not bugs.

**Two more real findings from the same production run — confirmed, but NOT fixed, and need a
decision rather than a quick patch:**

1. **Hyphen-adjacent numeric tokens silently don't match.** Confirmed directly against Postgres:
   `to_tsvector('simple', 'Gibson ES-335 Memphis')` → `'-335':3 'es':2 'gibson':1 'memphis':4`.
   Postgres's `simple`-config parser treats a hyphen immediately followed by digits as a
   NEGATIVE-NUMBER sign, producing lexeme `-335`, not `335`. A plain search for `335`
   (`phraseto_tsquery('simple', '335')` → `'335'`, no leading minus) never matches it — confirmed:
   `to_tsvector('simple', 'Gibson ES-335 Memphis') @@ phraseto_tsquery('simple', '335')` is
   `false`. The Python `\W+`-split tokenizer has no such quirk — it cleanly splits `ES-335` into
   `es`/`335`. This is likely the single largest remaining production mismatch cluster by item
   count (`335` 299→103, `339` 48→18, `heritage 535` 15→2, `gibson es 335` 97→25, likely
   explaining part of `59`/`69*`/`beyer* M*` too — all only-in-old / pg-undercounting). A real fix
   changes what the generated `search_vector` column indexes (e.g. preprocess `-` followed by a
   digit into a space, applied identically to both the stored generated column and every
   query-side `to_tsquery`/`phraseto_tsquery` call, so both sides tokenize consistently) — that's
   another schema change and another full-table `ALTER TABLE` rewrite like stage 1's, not a
   same-session code patch. Needs Chuck's go-ahead on the approach before building.
2. **Quoted-phrase narrowing (stage 2's own deliberate choice #2) is NOT invisible on real data**,
   contradicting the original expectation written into stage 2's docstring. `'"jam pedal"'`
   (singular, quoted) went from old=90 matches to pg=3 in production. Cause: `'simple'` config
   does zero stemming, so `pedal`/`pedals` are different lexemes; `phraseto_tsquery` requires an
   exact adjacent-lexeme phrase match, while the OLD matcher's raw-substring check happened to
   tolerate the trailing "s" for free (`"jam pedal"` is a literal substring of `"jam pedals ..."`)
   — an accidental leniency that quoted-phrase users may be relying on. Separately, also newly
   observed: punctuation is now insensitive on the pg side in a way the old matcher wasn't
   (`"Mr. Black"` and `"mr black"` converge to the identical result, since `to_tsvector`/
   `phraseto_tsquery` strip periods entirely) — arguably a usability improvement, but a real,
   previously-undocumented behavior change worth Chuck knowing explicitly. No code change
   proposed for either sub-finding here — this is a product-level call about acceptable search
   semantics, not a bug fix.

**Also confirmed NOT a bug**: roughly a dozen production mismatches were off by only 1-8 items on
counts ranging from the hundreds to hundreds of thousands (`Boss` 19853→19851, `Gibson`
10673→10671, `-LTD` 483920→483921, etc.) — consistent with ordinary catalog churn during the
diff-check's ~21.5s run (the ground-truth side snapshots `_cat_cache` once at the start; Postgres
is queried live per entry across the run), not a correctness issue. Not investigated further.

**Status**: v2.16.34 built, verified locally (structural + live-Postgres synthetic-catalog tests),
NOT yet pushed. **Next**: Chuck pushes from his Mac terminal, confirm deploy healthy, re-run the
diff-check against real production data to confirm the params-bug fix actually closes those
specific mismatches live and to size what's left (expected: the two open findings above, still
showing as mismatches until separately decided), then bring the new numbers + the two open
questions back to Chuck.


---

### Addendum 11 (2026-09-22, v2.16.35) — both open findings fixed and verified: hyphen tokenization + quoted-phrase punctuation/plural parity

Chuck's explicit instruction: "change those two to get as close to parity as possible with old" —
referring to Addendum 9/10's two open findings. Both fixed this session, not just documented. Full
writeup in HANDOFF.md's 2026-09-22 v2.16.35 entry — summary here:

- **Hyphen-adjacent digits**: `search_vector`'s generated expression and every query-side
  `to_tsquery`/`phraseto_tsquery` call now normalize `-` to a space before tokenizing
  (`regexp_replace(..., '-', ' ', 'g')` / `part.replace('-', ' ')`), so `ES-335` and a plain `335`
  query tokenize consistently on both sides. Required a self-migrating schema
  (`_pg_migrate_search_vector_if_stale()`, since a `GENERATED ALWAYS AS (...) STORED` column's
  expression can't be altered in place — confirmed needs `DROP COLUMN` + re-`ADD COLUMN`, another
  full-table rewrite). The wildcard fast path now excludes hyphenated words (falls back to
  `_TsqueryUnsupported`) rather than risk a wrong match — `to_tsquery`'s query-string parser
  treats an internal hyphen as a phrase separator, unlike `to_tsvector`'s document parser, so
  normalizing it the same way doesn't work there; confirmed via direct Postgres testing.
- **Quoted-phrase punctuation/plural narrowing**: quoted terms now compile to a `pg_trgm`-backed
  `ILIKE '%...%'` match against `name || ' ' || brand` instead of `phraseto_tsquery`, restoring
  the old Python matcher's raw-substring semantics (plural tolerance, punctuation sensitivity,
  mid-word substring matching) exactly. New `idx_items_name_brand_trgm` GIN trigram index backs
  it. New `_like_escape()` helper for safe `%`/`_`/`\` embedding in the pattern.
- **Forced architectural change**: `_tsquery_compile_term` now returns a COMPLETE boolean SQL
  predicate per term (ILIKE or `search_vector @@ (...)`), not a bare tsquery fragment — an ILIKE
  predicate and a tsquery value don't compose under tsquery's `&&`/`||`/`!!` operators (different
  type systems). All four translator functions now compose with plain SQL `AND`/`OR`/`NOT`
  instead. The stage-3 diff-check's execute call site dropped its `search_vector @@ (...)` wrapper
  accordingly.
- **Mechanical fix to ship 2 `CREATE INDEX CONCURRENTLY` statements instead of 1**: Postgres
  implicitly wraps every multi-statement `cur.execute()` call in one transaction even under
  `autocommit=True`, and `CONCURRENTLY` is rejected inside any transaction block regardless of
  autocommit. Fixed by splitting the CONCURRENT-INDEXES section into individual `cur.execute()`
  calls, one per statement, with full-line SQL comments stripped first (the file's own comments
  contain example commands ending in `;`, which a naive split-on-`;` would otherwise mistake for a
  statement boundary).

**Verified**: full schema+migration flow simulated end to end locally against all three real
states (fresh deploy, upgrade from old schema, idempotent re-run on already-migrated schema).
Rewritten translator run against real Postgres over the existing self-test catalog plus new
hyphen/quoted-phrase/mixed-composition cases — 39/42 pass exactly; the 3 differences are the
pre-existing documented wildcard mid-word-substring tradeoff (2 cases, unrelated to this session's
fixes) and one new, intentional, desirable side effect of the hyphen fix (`"ES 335"` now also
matches hyphenated `ES-335` text — hyphens and spaces are now equivalent at the token level on
both sides, which is the point of the fix). `py_compile` clean.

**Status**: built and verified locally, NOT yet pushed. Next: Chuck pushes, confirm deploy
healthy (this is a real schema change — full-table rewrite + two concurrent index builds), then
re-run the production diff-check to confirm both fixes close their respective mismatch clusters
live.


---

### Addendum 12 (2026-09-22, v2.16.36) — Mesa/Boogie slash-tokenization gap fixed and verified

Found via the v2.16.35 production diff-check re-run (see Addendum 11's own "Status" note about
re-running the diff-check after v2.16.35 shipped). Chuck's explicit instruction: "why not get
those two things cleaned up now" — referring to this fix and the diagnostic-endpoint cleanup.
Full writeup in HANDOFF.md's 2026-09-22 v2.16.36 entry — summary here:

- **Root cause**: Postgres's `'simple'` parser fuses a bare `word/word` pattern into ONE compound
  lexeme instead of splitting it (`to_tsvector('simple', 'Mesa/Boogie Rectifier')` → single lexeme
  `'mesa/boogie'`), so a plain `mesa` search never matched Mesa/Boogie-branded items. Different
  parser quirk than the v2.16.35 hyphen bug (that one misread hyphen-before-digit as a negative
  sign); slash just doesn't split at all.
- **Fix**: same shape as the hyphen fix — `/` normalized to a space alongside `-`, both in
  `search_vector`'s generated expression and in `_tsquery_compile_term`'s plain-word path
  (`part.replace('-', ' ').replace('/', ' ')`).
- **Wildcard fast path needed no code change**: `_TSQUERY_LEXEME_RE`'s charset never allowed `/`,
  so slash-containing wildcard words already fall back to `_TsqueryUnsupported` automatically —
  confirmed this is correct (an un-normalized slash wildcard wouldn't match normalized text anyway,
  and a normalized one hits the same "bare space" `to_tsquery` syntax error hyphen does).
- **Staleness-check bug caught before shipping**: the v2.16.35 check (`'regexp_replace' not in
  expr`) would have wrongly treated an already-migrated v2.16.35 database as current, since that
  expression DOES contain `regexp_replace` (just not slash handling). Fixed by switching to
  `"'/'" not in expr` — the literal `/` only appears in `pg_get_expr()`'s reflected output once the
  combined hyphen+slash expression is actually live, confirmed by direct inspection. Mirrored in
  `migrate_cat_cache_to_pg.py`.

**Verified**: full migration flow simulated against FOUR local Postgres states this time (fresh
deploy, pre-v2.16.35, and critically, upgrade-from-live-v2.16.35 — the case the old staleness check
would have missed — plus idempotent re-run). Real translator module run against real Postgres with
4 new slash-pattern catalog items and 9 new test cases (mesa, boogie, Mesa/Boogie, mesa/boogie,
3/4, "3/4", 3, rectifier, Mesa -Rectifier) — all 9 pass exactly, no regressions to the existing
42-case suite (52/55 total, 3 pre-existing documented tradeoffs, 0 new failures). `py_compile` and
`node --check` both clean.

**Status**: built and verified locally, NOT yet pushed. Next: Chuck pushes, confirm deploy healthy,
re-run the production diff-check to confirm the Mesa/Boogie cluster closes live, THEN (as a
separate follow-up commit, not bundled here) delete the two temporary diagnostic endpoints
(`/api/search-syntax-stats`, `/api/tsquery-diff-check`) and their supporting code.

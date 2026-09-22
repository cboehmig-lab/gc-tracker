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

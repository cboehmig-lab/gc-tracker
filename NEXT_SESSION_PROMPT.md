# Next Session Prompt — NEW-item anchor/tagging bug (narrowly scoped)

Copy and paste this to start the next Cowork session.

---

We're working on **GC Gear Tracker** (`gcgeartracker.com`), a Flask app on Railway tracking
Guitar Center used inventory. Read `HANDOFF.md` first for architecture/version history, then
read project memory file `bug_new_item_anchor_scope.md` in full before doing anything else — it
has the complete investigation history and the narrow scope for this session.

**All repo work happens via the device bridge in `~/Desktop/gc_tracker`, never the cloud sandbox
filesystem — check `device_bash` works before starting anything.**

## Context in brief

Chuck has reported, across multiple sessions, that genuinely-new items aren't getting tagged NEW
— most recently (2026-09-02) with a much better repro than before: items with TODAY's date
(`date_listed` = 9/2) weren't tagged NEW after a scan. He has 4 concrete repro URLs already
captured in the memory file — do not ask him for SKUs again, they're there.

Postgres Phase D (the `/api/browse` read-path cutover) was the reason this got deferred — it's
now shipped, deployed, and confirmed live (v2.16.22, 2026-09-02). This bug's connection to
Postgres was already mechanically ruled out before the deferral (Phase A-D don't touch
`_cat_cache`, the anchor, `_run()`'s scan/merge logic, or `isNew` computation — the SQL Tier 1
path even reads the same precomputed `new_ids` set the JSON path does, it doesn't recompute
anchor logic). So there's no new Postgres-related angle to check here — proceed straight to the
leads below.

**Chuck has explicitly asked for a narrowly-scoped session, not a fresh open-ended dig** — he
does not want to redo "the whole nightmare" of re-deriving context. Use what's already in the
memory file.

## The task, in order (per the memory file's own next-steps list)

1. **Use the 4 repro URLs directly** — look them up in real production (`/api/pg-parity-check`-
   style admin access, the live `_cat_cache`, or the Postgres `items` table now that Phase D
   means Postgres is a trustworthy live mirror) and record each item's actual `date_listed`,
   `first_seen`, and what the affected user's `last_anchor` was at scan time:
   - https://www.guitarcenter.com/Used/Bose/Used-Bose-L1-M1S-with-B1-Bass-Module-Powered-Speaker.gc
   - https://www.guitarcenter.com/Used/American-DJ/Used-American-DJ-DMX-OPERATOR-384-Lighting-Controller-122795885.gc
   - https://www.guitarcenter.com/Used/Epiphone/Used-Epiphone-MB100-Natural-Banjo-122788905.gc
   - https://www.guitarcenter.com/Used/American-DJ/Used-American-DJ-INNO-POCKET-PRO-Intelligent-Lighting-122795883.gc

2. **Read `parse_products()`'s `startDate`/`creationDate` → `date_listed` mapping code** and
   reconcile it against the documented "6-12h" Algolia indexing-delay figure referenced in
   HANDOFF.md's Algolia notes — the memory file flags this as the most promising unexplored lead,
   never actually read end-to-end.

3. **Check the specific repro items' `date_listed` values against the string-comparison
   tie-break theory**: a date-only `date_listed` like `"2026-09-02"` lexicographically sorts
   BEFORE a full-timestamp anchor from earlier the same day (e.g. `"2026-09-02T10:15:00"`), even
   though the item may genuinely be newer within the day. This exact bug class (v2.10.2/v2.12.2
   history) was checked "extinct" against a cache snapshot before, but never against live,
   freshly-scanned production data — check it for real this time using the 4 repro items.

4. **Do NOT revert v2.16.11** — that reopens a different, already-confirmed bug (false
   sold-marking on transient per-store fetch failures). If the investigation points at v2.16.11
   after all, that needs a different, more surgical fix, not a revert.

5. **Investigate before proposing a fix, per standing project rule**: read the actual code, and
   test locally where possible (a scratch scan simulation, or reproducing the anchor-advance
   logic against the 4 real items' data) rather than guessing at root cause from description
   alone.

## Standing rules (same as always)

- Bump `APP_VERSION` in `gc_tracker_app.py` for every logical change.
- Verify with `python3 -m py_compile gc_tracker_app.py` AND `node --check static/gc.js` — and for
  any change touching Flask routes, actually import the module in a disposable venv and confirm
  the route table builds.
- Git pushes happen from Chuck's Mac terminal only, never from the sandbox. Give him the exact
  commands; do not attempt `git push` from the device bridge.
- All JS lives in `static/gc.js` (or a new file under `static/`) — CSP blocks inline scripts AND
  inline `onclick=`/event-handler attributes.
- Update `HANDOFF.md` and `HANDOFF_PROMPT.md` with a changelog entry for every version bump.

## What's explicitly out of scope this session

- Postgres Phase E (Tier 2/keyword SQL) and Phase F (retire JSON) — not scheduled, not related
  to this bug.
- Any other open item — this session is scoped to the NEW-item anchor bug only, per Chuck's
  explicit ask.

Current version: **v2.16.22** (deployed, live). Last updated: 2026-09-02.

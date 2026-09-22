# Next Session Prompt — v2.16.33 built (not yet pushed); Phase F stage 3 (tsquery diff-check endpoint) shipped, needs to run against live production data next

Copy and paste this to start the next Cowork session.

---

We're working on **GC Gear Tracker** (`gcgeartracker.com`), a Flask app on Railway tracking
Guitar Center used inventory. Read `HANDOFF.md` first — then project memory files
`postgres_migration_plan_2026-08-31.md` and `postgres_phase_f_design_2026-09-21.md` for full
Postgres-migration status. The actual Phase F design doc lives in the repo at
`POSTGRES_PHASE_F_DESIGN.md` — read it before writing any more Phase F code, especially the
"Stage 3 — SHIPPED" addendum under §7 (the diff-check endpoint's design, the sound
candidate-narrowing argument, and the structural self-test that verified it).

**All repo work happens via the device bridge in `~/Desktop/gc_tracker`, never the cloud sandbox
filesystem — check `device_bash` works before starting anything.**

## Where things stand (as of 2026-09-22)

Postgres Phases A–E are complete/deployed/healthy. Phase F stage 1 (schema) and stage 2 (tsquery
translator, unwired) are live and confirmed healthy (v2.16.31, v2.16.32). Stage 3 is built:

- **v2.16.33** (built this session, **NOT YET PUSHED** — confirm git status before assuming
  otherwise): a new admin-only `/api/tsquery-diff-check` endpoint pair (`POST` to start on a
  background thread, `GET` to poll status/result) plus its supporting logic — an inverted
  token-index builder, sound required-tokens candidate narrowing, and a ground-truth matcher that
  reuses the real unmodified Python matcher primitives, all in `gc_tracker_app.py` right after
  `/api/search-syntax-stats`. Diffs the stage-2 tsquery translator's output against the current
  Python regex matcher's output, entry by entry, for every real distinct want-list keyword and
  saved-search `filter_q` string across every account. **Not wired into any live request path** —
  purely a diagnostic. Verified structurally: py_compile, node --check (untouched), a disposable
  venv (built in `/tmp` — see the build-environment note below) confirming the app imports and
  registers all 63 routes including both new ones, and a 21-case self-test comparing the new
  candidate-narrowed ground truth against a naive brute-force reimplementation of the exact same
  production routing over a synthetic catalog — all pass. **Still NOT verified against live
  Postgres or real production data** — this sandbox has no DB access, so nothing yet confirms the
  translator's SQL actually matches the same SKUs the Python matcher finds for real want lists and
  saved searches. That's the very next action once this deploys.

## The task

**Run the diff check against real data, then act on the results.** Once v2.16.33 is live on
Railway:

1. As an admin, `curl -X POST https://gcgeartracker.com/api/tsquery-diff-check` (with an admin
   session cookie) to start a run, then poll `GET /api/tsquery-diff-check` until `status: "done"`.
2. Read the `keywords`/`saved_searches` result blocks: `total`/`matched`/`mismatched`/
   `unsupported`/`errors` counts, plus capped samples (20 each) of mismatching/unsupported/errored
   entry TEXT (never SKUs or item names — same privacy posture as `/api/search-syntax-stats`).
3. If `mismatched` is 0 (or very close, with `unsupported` accounting for the rest and matching
   the near-zero real-wildcard-usage finding from §3): stage 2's translator is proven correct
   against real data. Move to step 4.
4. If there ARE real mismatches: read the sample entries, reproduce the specific old-vs-new
   difference by hand (compile both sides for that one entry), and figure out whether it's a bug
   in the translator (Stage 2, `gc_tracker_app.py`) or in the diff-check's own ground-truth mirror
   (Stage 3 — `_tsquery_diff_old_want_list_matches`/`_tsquery_diff_old_filter_q_matches`) before
   touching any code. The Stage 3 module's own docstring lists the two known, deliberate,
   documented semantic narrowings (suffix-wildcard prefix matching, quoted-phrase word-boundary
   matching) — a mismatch that's explained by one of those is expected and not a bug; anything
   else needs investigation.
5. Once clean: **step 4** is unifying Tier 1/Tier 2 into one `_pg_browse()` that always uses the
   tsquery translator (with a documented fallback to the Python `_kw_match` path for the rare
   `_TsqueryUnsupported` entry). **Step 5** is shadow-mode (run both, log disagreements, serve the
   old path) then cutover — not started, not the current focus until step 4 is designed.

## Standing rules (same as always)

- Bump `APP_VERSION` in `gc_tracker_app.py` for every logical change.
- Verify with `python3 -m py_compile gc_tracker_app.py` AND `node --check static/gc.js` — and for
  any change touching Flask routes, actually import the module in a disposable venv and confirm
  the route table builds. **Build that venv in `/tmp`, not the mounted folder** —
  `python3 -m venv` fails with an `ensurepip` error inside `~/mnt/gc_tracker/...` (FUSE mount);
  `/tmp` is a real local filesystem on the device with its own free space, separate from both the
  mounted folder and the device VM's own `$HOME` (which stays persistently near-full — always
  write scratch files under the mounted folder, e.g. `.phase_working/`, never bare `$HOME`).
- Git pushes happen from Chuck's Mac terminal only, never from the sandbox or the device bridge.
  Give him the exact commands (heredoc-safe for any multi-line commit message — a plain multi-line
  double-quoted `-m "..."` can get stuck at zsh's `quote>` prompt); do not attempt `git push`
  yourself anywhere. If `git commit`/`git status` fails with a lock error, a stale lock file is
  the cause — tell Chuck to `cd ~/Desktop/gc_tracker` FIRST, then `rm -f .git/index.lock` (that
  order matters) and retry — the device bridge cannot remove these itself (permission denied by
  design; confirmed firsthand this session).
- All JS lives in `static/gc.js` (or a new file under `static/`) — CSP blocks inline scripts AND
  inline `onclick=`/event-handler attributes.
- Update `HANDOFF.md` and `HANDOFF_PROMPT.md` with a changelog entry for every version bump.
- Railway project is `serene-determination` / service `web` — see `reference_railway.md` in
  project memory for how to find metrics/logs, and its caution about the project canvas view
  registering accidental "Apply changes" state from mere navigation (never click Deploy/Apply
  there without meaning to).

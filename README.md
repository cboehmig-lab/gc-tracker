# GC Used Inventory Tracker

A web app that tracks Guitar Center used gear inventory across all US stores, flags new listings since your last scan, and lets you search Craigslist for used gear — all in one place.

**Live site:** [gcgeartracker.com](https://gcgeartracker.com)

---

## What It Does

- Scans Guitar Center's used inventory across 298+ stores and flags items listed since your last visit
- Filter by brand, condition, category, price drops, and more
- Watch list and want list sync across devices when signed in
- ZIP code sort — see gear closest to you first
- Save custom filter combinations as named searches
- Standalone `/cl` page for Craigslist used gear search across major US cities
- Guest mode (no account required) or full accounts with Google Sign-In

---

## Tech Stack

| Layer | Detail |
|---|---|
| Backend | Python / Flask (single entry point: `gc_tracker_app.py`) |
| Database | SQLite (`gc_users.db`) — user accounts, watchlists, want lists, favorites |
| Inventory data | Guitar Center Algolia search API |
| Hosting | Railway (auto-deploys from `main` branch) |
| Static assets | `static/gc.css`, `static/gc.js`, `static/cl.css`, `static/cl.js` |

---

## Project Structure

```
gc_tracker_app.py        ← Main Flask app (~8000+ lines)
requirements.txt         ← Python dependencies
Procfile                 ← Railway process definition
static/
  gc.css                 ← Main app styles
  gc.js                  ← Main app JavaScript
  cl.css                 ← Craigslist page styles
  cl.js                  ← Craigslist page JavaScript
```

All runtime data files (database, inventory cache, etc.) live on a Railway persistent volume set via `DATA_DIR` env var — they are not in this repo.

---

## Deployment (Railway)

The app is deployed on Railway with auto-deploy on push to `main`.

### Required Environment Variables

| Var | Purpose |
|---|---|
| `SECRET_KEY` | Flask session secret — must be set or app won't start |
| `APP_PASSWORD` | Admin pages password — must be set or admin access is denied |
| `DATA_DIR` | Path to Railway persistent volume (data files live here) |
| `ALGOLIA_APP_ID` | Guitar Center inventory API |
| `ALGOLIA_API_KEY` | Guitar Center inventory API |
| `GOOGLE_CLIENT_ID` | Google OAuth (optional — Google Sign-In disabled if unset) |
| `GOOGLE_CLIENT_SECRET` | Google OAuth (optional) |

### To deploy a change

```bash
# Always push from Mac terminal (not the sandbox)
git push https://cboehmig-lab@github.com/cboehmig-lab/gc-tracker.git main
```

Railway picks up the push and redeploys automatically (~2 min).

---

## Admin Pages

Navigate to `/admin/login` and enter `APP_PASSWORD`.

| URL | Purpose |
|---|---|
| `/admin/users` | User account list |
| `/admin/devices` | Device access log |
| `/admin/clear-lock` | Release a stuck scan lock |
| `/admin/listing-patterns` | Algolia timestamp analysis |
| `/admin/build-coords` | Re-geocode store locations |

---

## Development Notes

- **Python/JS template gotcha**: JS used to live inside Python triple-quoted strings. After the v2.10.18 static file refactor, all JS is in `static/gc.js` and `static/cl.js` — Python string escape issues no longer apply.
- **CSP**: `script-src` does not include `'unsafe-inline'`; `style-src` does (required for inline `style="..."` HTML attributes throughout the templates).
- **Git lock files**: if a commit fails with `cannot lock ref`, run `rm ~/Desktop/gc_tracker/.git/index.lock 2>/dev/null; true` and retry.
- **Wrong GitHub account on push**: default remote may push as the wrong account. Use the explicit URL above.

See `HANDOFF.md` for full architecture details, version history, and debugging guide.

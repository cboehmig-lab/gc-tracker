# GC Used Inventory Tracker

A Flask web app for tracking Guitar Center used inventory. The live app lets users browse cached used gear, run scans, save watch lists, save want list keywords, save searches, mark favorite stores, and sync account data across devices.

The app also includes a standalone Craigslist used gear search page at `/cl`.

## Current deployment

| Item | Detail |
|---|---|
| Platform | Railway |
| Repo | `cboehmig-lab/gc-tracker` |
| Branch | `main` |
| Entry point | `gc_tracker_app.py` |
| Start command | `python gc_tracker_app.py` |
| Runtime data | Stored under `DATA_DIR`, which should point to a Railway persistent volume |

Railway auto-deploys from `main`. Do not push directly to `main` for risky changes. Use a branch and pull request.

## Required environment variables

| Variable | Purpose |
|---|---|
| `DATA_DIR` | Directory for runtime database, cache, logs, and generated files. On Railway this must be a persistent volume path. |
| `SECRET_KEY` | Flask session signing secret. Must be a long random value. The app should not start without it. |
| `APP_PASSWORD` | Admin password used at `/admin/login`. Must be long and unique. |
| `ALGOLIA_APP_ID` | Guitar Center inventory Algolia application ID. |
| `ALGOLIA_API_KEY` | Guitar Center inventory Algolia API key. |
| `GOOGLE_CLIENT_ID` | Google OAuth client ID. Required only if Google Sign-In is enabled. |
| `GOOGLE_CLIENT_SECRET` | Google OAuth client secret. Required only if Google Sign-In is enabled. |
| `GA_MEASUREMENT_ID` | Optional Google Analytics measurement ID. |

Never commit `.env` files, production secrets, SQLite databases, runtime cache files, logs, or generated exports.

## Local development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export SECRET_KEY='replace-with-a-long-random-local-secret'
export APP_PASSWORD='replace-with-a-local-admin-password'
export DATA_DIR='./data'
python gc_tracker_app.py
```

Then open:

```text
http://localhost:5050
```

For local Google OAuth testing, also set `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` and make sure the Google callback URL matches your local route.

## Admin access

Admin pages use session-based admin login.

```text
/admin/login
```

Old `?pw=<password>` admin URLs should not be used and should not be reintroduced. Admin-only actions should check server-side admin session state, not client-side visibility or query-string passwords.

## Runtime data files

The live app stores runtime data outside the repo, under `DATA_DIR`.

| File | Purpose |
|---|---|
| `gc_users.db` | SQLite user database. Includes accounts, password hashes, Google IDs, and per-user saved data. |
| `gc_category_cache.json` | Shared cached inventory data. |
| `gc_last_scan.txt` | Global fallback scan timestamp for guest mode. |
| `gc_device_log.jsonl` | Device access log. |
| `gc_invalid_stores.json` | Auto-managed invalid-store blocklist. |
| `gc_new_inventory.xlsx` | Generated Excel export. |

These files should not be committed.

## Security notes

Before deploying security-sensitive changes, check:

- No secrets are committed in current files.
- Historical exposed secrets have been rotated if they were ever used in production.
- Admin endpoints require `/admin/login` session auth.
- Expensive endpoints have abuse controls or rate limits.
- User data reads and writes are scoped to the current session user.
- OAuth account linking requires verified identity.
- Error messages do not leak tokens, secrets, stack traces, or provider exception details.
- HTML generated from user, admin, device, store, or inventory data is escaped or rendered as text.

See `SECURITY_HARDENING.md` for the current security checklist.

## Rollback

Current known-good baseline before the security/docs branch:

```text
69308d33de8762fbbcd8cd6aa686568ab61dfd2e
```

For safer releases:

```bash
git tag pre-security-fixes 69308d33de8762fbbcd8cd6aa686568ab61dfd2e
git push origin pre-security-fixes
```

If a merged PR breaks the app, revert the merge commit and push to `main` so Railway redeploys the previous behavior.

```bash
git revert <merge_commit_sha>
git push origin main
```

Avoid destructive database migrations unless there is a backup and rollback plan.

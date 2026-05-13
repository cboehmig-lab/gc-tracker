# Security Hardening Checklist

This file tracks the defensive security work that should remain visible in the repo. It is not a substitute for a full penetration test.

## Current status

Recent hardening in v2.10.16 moved secrets to environment variables, removed the auto-updater, switched admin access away from query-string passwords, added security headers, added origin/referer CSRF checks, escaped admin-page output, and removed OAuth debug details from redirect URLs.

## Production secret rotation

These values appeared in public Git history or earlier code patterns and must be considered exposed if they were ever used in production:

- Old admin/reset fallback password: `Beatle909!`
- Old Flask session fallback key: `gc-tracker-default-key-change-me`
- Previously hardcoded Algolia credentials from earlier utility scripts and app versions

Required actions:

- Rotate `APP_PASSWORD` if it was ever the old fallback value.
- Rotate `SECRET_KEY` if the old fallback was ever used in a deployed environment.
- Rotate Algolia credentials if the exposed key has more access than intended.
- Keep `.env`, runtime databases, cache files, logs, and generated exports out of Git.

## Admin authentication

Admin access should go through:

```text
/admin/login
```

Do not reintroduce `?pw=<password>` admin URLs. Admin-only API routes should use server-side admin session checks.

Admin-sensitive routes include:

- `/admin/users`
- `/admin/devices`
- `/admin/clear-lock`
- `/admin/listing-patterns`
- `/admin/build-coords`
- `/admin/validate-stores`
- `/api/reset`
- `/api/clear-blocklist`
- `/api/validate-stores`
- `/api/build-store-coords`
- `/api/import-data`

## Rate limiting still recommended

The app should rate-limit both authentication and high-cost routes.

High priority:

- `/admin/login`
- `/api/login`
- `/api/register`
- `/api/setup-google-account`
- `/api/run`
- `/api/cl-search`

Medium priority:

- `/api/browse`
- `/api/saved-search-counts`
- `/download/excel`
- export/import endpoints

Do not rely on raw client-supplied `X-Forwarded-For` unless the hosting platform is known to sanitize it. Prefer trusted proxy handling and normalized client IPs.

## OAuth hardening

For Google OAuth:

- Validate state.
- Keep callback error details out of redirect URLs.
- Only allow relative `next` paths.
- Before auto-linking an existing account by email, require the Google email to be verified.
- Rate-limit account import/password-merge attempts.

## User data access

All reads and writes of user-owned data must derive the user ID from the server-side session, not from a client-supplied user ID.

User-owned fields include:

- watch list
- want list keywords
- favorites
- saved searches
- last scan timestamp
- new item IDs
- account linking state

## HTML and JavaScript safety

Treat all item data, store names, device log values, user names, saved search names, and request headers as untrusted.

Preferred patterns:

- Use server-side escaping for generated admin HTML.
- Use text rendering rather than raw HTML insertion where possible.
- Avoid inline event handlers in new code.
- Avoid building JavaScript from unsanitized strings.

The current CSP still allows inline scripts and styles because the app is a single-file Flask template. Long-term, move scripts/styles into static assets so the CSP can drop `'unsafe-inline'`.

## Documentation hygiene

Keep these docs current:

- `README.md`: public project overview and deployment expectations.
- `HANDOFF.md`: detailed implementation history and operational memory.
- `SECURITY_HARDENING.md`: security checklist and remaining hardening work.

When security behavior changes, update the docs in the same PR.

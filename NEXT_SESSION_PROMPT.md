# Next Session Prompt — Sovrn Affiliate Approval

Copy and paste this to start the next Cowork session.

---

We're working on **GC Gear Tracker** (`gcgeartracker.com`), a Flask web app deployed on Railway that lets users track Guitar Center used inventory for new listings. Read the full architecture doc first:

`/Users/charles.boehmig/Desktop/gc_tracker/HANDOFF.md`

**Goal for this session:** Improve the site's chances of being approved by **Sovrn** (formerly VigLink) for affiliate link monetization. Sovrn reviews sites manually and looks for signs of a real, quality, human-driven site before approving. We need to identify what's missing or weak and fix it.

## What to research first
Before touching any code, search for current Sovrn publisher requirements and approval criteria — their docs may have changed. Look at:
- https://www.sovrn.com/publishers/
- Any publisher FAQ or approval requirements page
- Third-party writeups about what Sovrn looks for in site reviews

## What we know about the site
- Single-page web app at `gcgeartracker.com` — users scan Guitar Center's used inventory API and see what's new since their last visit
- ~4,400 unique devices have visited; active daily users
- User accounts with Google Sign-In
- No "About" page, no contact info, no privacy policy, no terms of service — just the app
- No blog or editorial content — purely functional tool
- Has Google Analytics (GA4) with measurement ID
- Footer has PayPal/Venmo donate links and a link to the developer's music site (animalsintrees.com)
- The `/cl` page at `gcgeartracker.com/cl` is a Craigslist used gear search tool (separate feature, same domain)

## Likely gaps to address
We suspect Sovrn will want to see:
1. A real About/contact page explaining what the site does and who made it
2. A Privacy Policy (especially since we collect user accounts + Google OAuth data)
3. Terms of Service
4. Some signal that the site has a human author and editorial purpose
5. Possibly: more "content" (not just a tool)

## Technical context
- Main app is in `gc_tracker_app.py` (single file, ~8000+ lines of Flask)
- Static assets: `static/gc.js`, `static/gc.css`
- New pages can be added as Flask routes returning HTML
- The footer (`#dev-footer`) already has links — we can add more there
- There's an About modal (`#about-modal`) already in the app — might be expandable

---

## Known Bug to Fix (unrelated to Sovrn — fix this first)

**"Scan For New" anchor not advancing as you browse**

There's a bug where items that were already visible at the top of the table before scanning still get flagged as NEW after the scan.

**How it should work:** the app tracks a per-user `last_anchor` — the max `date_listed` of all items the user has been exposed to. Before scanning, the anchor is sent to the server as `device_last_anchor`. The server flags an item as NEW only if its `date_listed > anchor`. So "new" means "newer than the most recent thing you've already seen in the table."

**The bug:** `window._lastAnchorISO` (the anchor) is only ever updated in two places in `gc.js`:
1. After login/data merge (from server)
2. After a scan completes (server sends back `scan_anchor`)

It is **not** updated when browse results render. So if another user's scan ran and pushed new items to the top of the table (visible to you when you browse), those items already appear at the top — but when YOU scan, your anchor is still from your last scan, so those items incorrectly get flagged as NEW even though you could already see them.

**The fix (client-side, `static/gc.js`):** After `_fetchBrowsePage` receives results and sets `window._lastBrowseItems = d.items` (around line 1320), on page 1 only, advance `_lastAnchorISO` to `max(existing anchor, max(d.items.map(i => i.date_raw).filter(Boolean)))` and persist to localStorage. Also trigger a debounced `_syncToServer()` if logged in, so the anchor persists across devices.

The architecture for this already exists — `date_raw` is returned on every browse item, `_lastAnchorISO` is the right variable, `_lsSet('last_anchor', ...)` persists it, and `_syncToServer()` sends it up. It just needs to be called in `_fetchBrowsePage` after page 1 renders.

---

## What I want from this session
1. Assess what Sovrn specifically requires and what we're missing
2. Prioritize the gaps by impact on approval likelihood
3. Implement the highest-priority fixes (About page, Privacy Policy, etc.)
4. Make sure anything we add is honest and accurate, not boilerplate spam
5. Push all changes to Railway when done

Current version: **v2.11.4**. Push commands go to: `git push https://cboehmig-lab@github.com/cboehmig-lab/gc-tracker.git main`

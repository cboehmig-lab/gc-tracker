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

## What I want from this session
1. Assess what Sovrn specifically requires and what we're missing
2. Prioritize the gaps by impact on approval likelihood
3. Implement the highest-priority fixes (About page, Privacy Policy, etc.)
4. Make sure anything we add is honest and accurate, not boilerplate spam
5. Push all changes to Railway when done

Current version: **v2.11.5**. Last updated: 2026-05-18. Push commands go to: `git push https://cboehmig-lab@github.com/cboehmig-lab/gc-tracker.git main`

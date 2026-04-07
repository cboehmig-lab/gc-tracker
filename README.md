# Guitar Center Used Inventory Tracker

Automatically checks the Guitar Center used inventory for **Austin** and **South Austin** stores and logs new items to an Excel file.

---

## Quick Start

### 1. Install dependencies (one time)

```bash
pip install requests beautifulsoup4 openpyxl
```

### 2. Run the tracker

```bash
python gc_inventory_tracker.py
```

- **First run**: logs all current inventory as your baseline (nothing goes into the Excel file yet — this just sets the starting point).
- **Every run after that**: only items that are new since the last run get added to `gc_new_inventory.xlsx`.

---

## What you get

| File | Purpose |
|------|---------|
| `gc_new_inventory.xlsx` | Opens in Excel. New rows are added each run. |
| `gc_state.json` | Tracks which items you've seen. Don't delete this. |

### Excel columns

| Column | Description |
|--------|-------------|
| Date Found | Timestamp of the run that spotted this item |
| Item Name | Product description |
| Price | Listed price |
| Store | Austin or South Austin |
| Link | Clickable URL to the GC listing |

---

## Troubleshooting

### "Could not find product cards"

Guitar Center occasionally updates their page markup. If this happens:

1. Open `gc_debug_page.html` (saved automatically) in your browser.
2. Right-click a product listing → **Inspect Element**.
3. Note the CSS class on the outer `<div>` wrapping each product.
4. Add that class to the `cards = (...)` block in `parse_products()`.

### Website uses JavaScript rendering

If you see an empty or login page in `gc_debug_page.html`, Guitar Center may be rendering products via JavaScript. In that case:

```bash
pip install playwright
playwright install chromium
```

Then replace the `fetch_page()` function with:

```python
def fetch_page(start: int = 0) -> str:
    from playwright.sync_api import sync_playwright
    url = f"{BASE_URL}?{QUERY}&start={start}"
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, wait_until="networkidle")
        html = page.content()
        browser.close()
    return html
```

### "HTTP error 403" or "Access denied"

The site may be blocking automated requests. Try:
1. Increase `REQUEST_DELAY` to `3.0` or higher.
2. Run the script while your browser has a Guitar Center tab open.

---

## Running automatically (optional)

You can schedule this as a cron job on Mac to run daily:

```bash
# Open crontab editor
crontab -e

# Add this line to run at 8am daily (adjust path to your script):
0 8 * * * /usr/bin/python3 /path/to/gc_inventory_tracker.py >> /path/to/gc_tracker.log 2>&1
```

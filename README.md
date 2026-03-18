# 🎸 GC Used Inventory Tracker

Track new used gear listings at Guitar Center stores — see what's just arrived before anyone else.

---

## What it does

- Monitors any Guitar Center store's used inventory
- Shows you new listings since your last check
- Filters by condition (Excellent, Great, Good, Fair, Poor), category, and price
- Downloads results to Excel
- Remembers your favorite stores

---

## Setup (one time only)

### Step 1 — Install Python

**Mac:** Python is usually already installed. To check, open Terminal and type `python3 --version`. If you see a version number, skip to Step 2. Otherwise download it from [python.org/downloads](https://www.python.org/downloads/).

**Windows:** Download Python from [python.org/downloads](https://www.python.org/downloads/). **Important:** During installation, check the box that says "Add Python to PATH."

---

### Step 2 — Download the tracker

1. Go to [github.com/cboehmig-lab/gc-tracker](https://github.com/cboehmig-lab/gc-tracker)
2. Click the green **Code** button → **Download ZIP**
3. Unzip the downloaded file somewhere easy to find (like your Desktop or Documents)

---

### Step 3 — Run it

**Mac:**
1. Open the unzipped folder
2. Right-click `install_mac.command` → **Open**
3. If you see a security warning, click **Open** again
4. Your browser will open automatically with the tracker

**Windows:**
1. Open the unzipped folder
2. Double-click `install_windows.bat`
3. If Windows shows a "Windows protected your PC" warning, click **More info** → **Run anyway**
4. Your browser will open automatically with the tracker

> The first time you run it, it installs the required Python packages (takes about 30 seconds). After that it starts instantly.

---

## Using the tracker

**First run — Build your baseline**

Before you can track new items, you need to tell the tracker what already exists. Click **🌐 Build Baseline** — this scans all Guitar Center stores and saves the current inventory as your starting point. It takes 30–60 minutes but you only do it once.

**After that — Check for new items**

1. Select one or more stores from the left panel (click ★ to save favorites)
2. Click **Run**
3. New items appear highlighted with a **NEW** badge

**Validate your store list**

Click **✓ Validate Stores** occasionally to remove any stores that have closed and discover any new ones that have opened.

---

## Your data

All your data is saved in:
- **Mac:** `~/Documents/GCTracker/`
- **Windows:** `Documents\GCTracker\`

This includes your state, favorites, and Excel exports. It's never deleted when you update the app.

---

## Updates

When an update is available, a green banner appears at the top of the app. Click **Install Update** and it downloads and installs automatically. Then restart the app by closing the terminal window and double-clicking the launcher again.

---

## Troubleshooting

**"Python is not installed" on Mac**
Download Python from [python.org/downloads](https://www.python.org/downloads/) and try again.

**"Windows protected your PC" warning**
This is normal for apps not sold through the Microsoft Store. Click **More info** → **Run anyway**. The source code is fully visible on GitHub if you want to verify it.

**"Unverified developer" warning on Mac**
Right-click the `.command` file → **Open** → **Open** again. You only have to do this once.

**The browser doesn't open automatically**
Open your browser manually and go to: `http://localhost:5050`

**Port 5050 is already in use**
Another app is using port 5050. Close it, or set a different port by editing `install_mac.command` or `install_windows.bat` and changing `PORT=5050` to another number like `PORT=5051`. Use the same number in your browser URL.

---

## Questions or issues

Open an issue at [github.com/cboehmig-lab/gc-tracker/issues](https://github.com/cboehmig-lab/gc-tracker/issues)

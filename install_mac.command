#!/bin/bash
# GC Used Inventory Tracker — Mac Installer & Launcher
# Double-click this file to install and run the tracker.

set -e

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo ""
echo "🎸 GC Used Inventory Tracker"
echo "=============================="
echo ""

# Move to the folder this script lives in (so paths work wherever it's placed)
cd "$(dirname "$0")"

# ── Check Python ──────────────────────────────────────────────────────────────
if command -v python3 &>/dev/null; then
    PYTHON=python3
elif command -v python &>/dev/null && python --version 2>&1 | grep -q "Python 3"; then
    PYTHON=python
else
    echo -e "${RED}Python 3 is not installed.${NC}"
    echo ""
    echo "Please install it from: https://www.python.org/downloads/"
    echo "Then double-click this file again."
    echo ""
    read -p "Press Enter to close..."
    exit 1
fi

PYVER=$($PYTHON -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo -e "${GREEN}✓ Python $PYVER found${NC}"

# ── Install / upgrade dependencies ────────────────────────────────────────────
echo ""
echo "Installing dependencies (this only takes a moment)..."
$PYTHON -m pip install --upgrade --quiet flask requests openpyxl 2>&1 | tail -3
echo -e "${GREEN}✓ Dependencies ready${NC}"

# ── Data folder — save in ~/Documents/GCTracker so data persists between runs ─
DATA_DIR="$HOME/Documents/GCTracker"
mkdir -p "$DATA_DIR"
echo -e "${GREEN}✓ Data folder: $DATA_DIR${NC}"

# ── Launch ────────────────────────────────────────────────────────────────────
echo ""
echo -e "${YELLOW}Starting GC Tracker...${NC}"
echo "Your browser will open automatically."
echo "To stop the tracker, close this window or press Ctrl+C."
echo ""

export DATA_DIR="$DATA_DIR"
export PORT=5050
$PYTHON gc_tracker_app.py

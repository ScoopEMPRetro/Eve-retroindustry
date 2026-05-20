#!/usr/bin/env bash
set -e

echo "============================================================"
echo "  EVE Retroindustry — Linux/Mac build"
echo "============================================================"
echo

# Venv
if [ ! -d venv ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi
source venv/bin/activate

# Deps
echo "Installing dependencies..."
pip install -q -r requirements.txt
pip install -q pyinstaller

# PyInstaller
echo
echo "Running PyInstaller..."
pyinstaller eve_retroindustry.spec --noconfirm

echo
echo "============================================================"
echo "  Build complete: dist/EVE_Retroindustry/"
echo "  Distribute the entire EVE_Retroindustry folder as a ZIP."
echo "============================================================"

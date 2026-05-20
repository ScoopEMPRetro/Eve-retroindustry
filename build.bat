@echo off
setlocal EnableDelayedExpansion

echo ============================================================
echo  EVE Retroindustry — Windows build
echo ============================================================
echo.

REM Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Install Python 3.11+ and add to PATH.
    pause & exit /b 1
)

REM Create/activate venv
if not exist venv (
    echo Creating virtual environment...
    python -m venv venv
)
call venv\Scripts\activate.bat

REM Install deps including PyInstaller
echo Installing dependencies...
pip install -q -r requirements.txt
pip install -q pyinstaller

REM Run PyInstaller
echo.
echo Running PyInstaller...
pyinstaller eve_retroindustry.spec --noconfirm

if errorlevel 1 (
    echo.
    echo ERROR: PyInstaller build failed.
    pause & exit /b 1
)

echo.
echo ============================================================
echo  Build complete: dist\EVE_Retroindustry\
echo  Distribute the entire EVE_Retroindustry folder as a ZIP.
echo ============================================================
pause

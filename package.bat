@echo off
setlocal
title AI Interface Packager
cd /d "%~dp0"

REM ─── Pre-flight ──────────────────────────────────────────────
if not exist "app.py" (
    echo [ERROR] app.py not found. Run this from the project root.
    pause & exit /b 1
)
if not exist "core" (
    echo [ERROR] core\ folder missing. Your local copy isn't modular yet.
    pause & exit /b 1
)
if not exist "ui" (
    echo [ERROR] ui\ folder missing.
    pause & exit /b 1
)

echo ============================================
echo   AI Interface Packager
echo ============================================
echo.

REM ─── Ensure package markers ──────────────────────────────────
if not exist "core\__init__.py" (
    type nul > "core\__init__.py"
    echo [added] core\__init__.py
)
if not exist "ui\__init__.py" (
    type nul > "ui\__init__.py"
    echo [added] ui\__init__.py
)

REM ─── .gitignore (harmless even without git; useful later) ────
if not exist ".gitignore" (
    > .gitignore (
        echo .venv/
        echo venv/
        echo __pycache__/
        echo *.pyc
        echo *.pyo
        echo.
        echo data/*
        echo !data/.gitkeep
        echo.
        echo build/
        echo dist/
        echo *.spec
        echo.
        echo .DS_Store
        echo Thumbs.db
    )
    echo [added] .gitignore
)

REM ─── requirements.txt ────────────────────────────────────────
if not exist "requirements.txt" (
    > requirements.txt (
        echo # The app itself uses only the Python standard library.
        echo # ruff is optional - used by the Batch tab's lint check.
        echo ruff
    )
    echo [added] requirements.txt
)

REM ─── data\.gitkeep so folder ships in the ZIP ────────────────
if not exist "data" mkdir data
if not exist "data\.gitkeep" type nul > "data\.gitkeep"

REM ─── Check installer scripts exist ───────────────────────────
set "MISSING="
if not exist "install.bat" set "MISSING=install.bat"
if not exist "run.bat"     set "MISSING=%MISSING% run.bat"
if defined MISSING (
    echo.
    echo [WARN] Missing installer scripts: %MISSING%
    echo        Save these files into the project root before continuing:
    echo          install.bat   - one-time setup (venv, shortcut, Ollama check)
    echo          run.bat       - launcher
    echo        (Both were provided in the previous message - copy/paste them.)
    echo.
    choice /C YN /M "Continue and package anyway"
    if errorlevel 2 exit /b 1
)

REM ─── Stage a clean copy (exclude .venv, data, caches, builds) ─
echo.
echo [..] Staging files...
set "STAGE=%TEMP%\AI-Interface-pkg"
if exist "%STAGE%" rmdir /S /Q "%STAGE%"
mkdir "%STAGE%"

robocopy "." "%STAGE%\AI-Interface" /E ^
    /XD ".venv" "venv" "data" "__pycache__" "build" "dist" ".git" ".idea" ".vscode" ^
    /XF "*.pyc" "*.pyo" "AI-Interface.zip" ^
    /NFL /NDL /NJH /NJS /NP

if errorlevel 8 (
    echo [ERROR] robocopy failed.
    rmdir /S /Q "%STAGE%" & pause & exit /b 1
)

REM Restore empty data\ so it exists after extraction
if not exist "%STAGE%\AI-Interface\data" mkdir "%STAGE%\AI-Interface\data"
type nul > "%STAGE%\AI-Interface\data\.gitkeep"

REM ─── Compress ────────────────────────────────────────────────
echo [..] Compressing to AI-Interface.zip...
if exist "AI-Interface.zip" del "AI-Interface.zip"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Compress-Archive -Path '%STAGE%\AI-Interface' -DestinationPath 'AI-Interface.zip' -CompressionLevel Optimal"

if errorlevel 1 (
    echo [ERROR] Failed to create ZIP.
    rmdir /S /Q "%STAGE%" & pause & exit /b 1
)
rmdir /S /Q "%STAGE%"

REM ─── Report ──────────────────────────────────────────────────
echo.
echo ============================================
echo   Done!
echo ============================================
echo.
echo Built: %~dp0AI-Interface.zip
echo.
echo To distribute:
echo   - Send the ZIP by email / Drive / USB
echo   - Or upload it to github.com/JacobAFoster/AI-Interface/releases
echo     (click "Draft a new release", attach the ZIP, click "Publish")
echo.
echo End-user instructions (include these with the ZIP):
echo   1. Extract anywhere
echo   2. Double-click install.bat
echo   3. Double-click the "Local AI Hub" desktop shortcut
echo.
pause
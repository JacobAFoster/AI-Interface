@echo off
setlocal
cd /d "%~dp0"

echo Organizing project in: %CD%
echo.

if not exist core mkdir core
if not exist ui mkdir ui

rem ---- core/ ----
for %%F in (batch_editor.py context.py db.py parsing.py reminders.py) do (
    if exist "%%F" (
        move /Y "%%F" "core\%%F" >nul
        echo moved %%F -^> core\
    )
)

rem ---- ui/ ----
for %%F in (batch_tab.py calendar.py chat.py dashboard.py sidebar.py tasks.py theme.py) do (
    if exist "%%F" (
        move /Y "%%F" "ui\%%F" >nul
        echo moved %%F -^> ui\
    )
)

rem ---- package markers (only created if missing) ----
if not exist core\__init__.py type nul > core\__init__.py
if not exist ui\__init__.py type nul > ui\__init__.py

echo.
if exist app.py (
    echo app.py is in the project root - good.
) else (
    echo WARNING: app.py not found next to this script.
)

echo.
echo Final layout should be:
echo   app.py
echo   core\  batch_editor, context, db, parsing, reminders
echo   ui\    batch_tab, calendar, chat, dashboard, sidebar, tasks, theme
echo.
echo Run the app with:  python app.py
echo.
pause

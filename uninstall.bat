@echo off
REM Sprite Lore uninstaller: removes only what the installer placed. Options: --dry-run, --no-restore
cd /d "%~dp0"
where py >nul 2>&1
if %errorlevel%==0 (py -3 spritelore_uninstall.py %*) else (python spritelore_uninstall.py %*)
pause

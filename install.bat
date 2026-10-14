@echo off
REM Sprite Lore installer (Windows). Options: --dry-run, --uninstall [--purge]
cd /d "%~dp0"
where py >nul 2>&1
if %errorlevel%==0 (py -3 spritelore_install.py %*) else (python spritelore_install.py %*)
pause

@echo off
rem The plugin's `orc` for cmd/PowerShell: `python` if it works (not the Store alias), else the `py -3` launcher.
python -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>nul
if %errorlevel%==0 (
  python "%~dp0..\cli\orc" %*
) else (
  py -3 "%~dp0..\cli\orc" %*
)
exit /b %errorlevel%

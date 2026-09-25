@echo off
rem Windows launcher for the `lastcall` command. The python.org installer ships
rem the `py` launcher and `python`, rarely `python3`, so try them in that order.
where py >nul 2>nul
if %ERRORLEVEL% EQU 0 (
  py -3 "%~dp0lastcall" %*
) else (
  python "%~dp0lastcall" %*
)
exit /b %ERRORLEVEL%

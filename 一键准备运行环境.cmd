@echo off
where pwsh.exe >nul 2>nul
if errorlevel 1 goto legacy
pwsh.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0prepare-environment.ps1" %*
goto done
:legacy
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0prepare-environment.ps1" %*
:done
pause

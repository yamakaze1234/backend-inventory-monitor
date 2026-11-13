@echo off
setlocal
set "repairRoot=%~1"
if not defined repairRoot set /p "repairRoot=Original migration package folder: "
set "repairShell=%repairRoot%\robot\runtime\pwsh\pwsh.exe"
if not exist "%repairShell%" set "repairShell=pwsh.exe"
"%repairShell%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\repair_new_machine.ps1" -PackageRoot "%repairRoot%"
pause

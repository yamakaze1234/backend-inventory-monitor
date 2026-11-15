@echo off
chcp 65001 >nul
rem 同时兼容源码 tools 目录和发行包根目录。
set "setup_script=%~dp0prepare-environment.ps1"
if not exist "%setup_script%" set "setup_script=%~dp0tools\prepare-environment.ps1"
if not exist "%setup_script%" goto missing
where pwsh.exe >nul 2>nul
if errorlevel 1 goto bootstrap
pwsh.exe -NoProfile -ExecutionPolicy Bypass -File "%setup_script%" %*
goto done
:bootstrap
rem Windows PowerShell 仅用于尚未安装 PowerShell 7 的新电脑引导。
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%setup_script%" %*
:done
set "setup_result=%errorlevel%"
if not "%setup_result%"=="0" echo Environment setup did not complete. Exit code: %setup_result%
pause
exit /b %setup_result%
:missing
echo Missing tools\prepare-environment.ps1. Please extract the complete package first.
pause
exit /b 1

@echo off
rem One-time setup on a Windows PC so Print Pantry's "Create New Source" and
rem "Open in Bambu Studio" buttons can start Bambu Studio. (Snapmaker Orca needs
rem nothing: its own snapmaker-orca:// links already work.)
rem
rem   scripts\install-slicer-links.cmd                 trust 192.168.1.140:8484
rem   scripts\install-slicer-links.cmd host:port       trust another server
rem   scripts\install-slicer-links.cmd remove          undo
rem
rem Copies open-in-slicer.ps1 to %LOCALAPPDATA%\PrintPantry and registers the
rem printpantry:// link handler for this Windows user only (no admin needed).
setlocal
set "DEST=%LOCALAPPDATA%\PrintPantry"
set "KEY=HKCU\Software\Classes\printpantry"

if /i "%~1"=="remove" (
  reg delete "%KEY%" /f >nul 2>nul
  echo Removed the printpantry:// link handler. Files in %DEST% were left in place.
  exit /b 0
)

set "SERVER=%~1"
if "%SERVER%"=="" set "SERVER=192.168.1.140:8484"

if not exist "%DEST%" mkdir "%DEST%" || exit /b 1
copy /y "%~dp0open-in-slicer.ps1" "%DEST%\open-in-slicer.ps1" >nul || exit /b 1
findstr /x /i /c:"%SERVER%" "%DEST%\allowed-hosts.txt" >nul 2>nul || (echo %SERVER%>>"%DEST%\allowed-hosts.txt")

reg add "%KEY%" /ve /d "URL:Print Pantry" /f >nul || exit /b 1
reg add "%KEY%" /v "URL Protocol" /d "" /f >nul || exit /b 1
reg add "%KEY%\shell\open\command" /ve /f /d "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File \"%DEST%\open-in-slicer.ps1\" \"%%1\"" >nul || exit /b 1

echo Installed. Print Pantry at %SERVER% can now open files in Bambu Studio.
echo The first time, your browser asks whether to open the link: tick "Always allow".
exit /b 0

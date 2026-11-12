@echo off
rem Build and (re)start Print Pantry on TrueNAS over SSH. No Docker Desktop needed:
rem the NAS pulls the latest code from GitHub and builds the image itself.
rem   scripts\deploy-truenas.cmd          update to the latest main
rem   scripts\deploy-truenas.cmd setup    first install: datasets, checkout, app
setlocal
call "%~dp0_config.cmd" nodocker || exit /b 1
set "MODE=%~1"
if "%MODE%"=="" set "MODE=update"
where ssh >nul 2>nul || (echo ssh was not found. Enable the Windows OpenSSH client. & exit /b 1)
echo Copying deploy script to %PP_SSH_HOST%...
scp -q "%~dp0..\deploy\truenas.sh" %PP_SSH_HOST%:printpantry-truenas.sh || exit /b 1
ssh %PP_SSH_HOST% "sed -i 's/\r$//' ~/printpantry-truenas.sh && PP_NAS_ROOT=%PP_NAS_ROOT% bash ~/printpantry-truenas.sh %MODE%"
exit /b %ERRORLEVEL%

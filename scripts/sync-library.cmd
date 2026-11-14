@echo off
rem Copy the local library folder to the NAS (new and changed files; nothing is deleted).
rem   scripts\sync-library.cmd                  uses D:\PrintPantry\library
rem   scripts\sync-library.cmd "E:\My Models"   another folder
setlocal
call "%~dp0_config.cmd" nodocker || exit /b 1
set "SRC=%~1"
if "%SRC%"=="" set "SRC=%PP_LOCAL_LIBRARY%"
if not exist "%SRC%\" (echo Folder not found: %SRC% & exit /b 1)
echo Copying "%SRC%" to %PP_SSH_HOST%:%PP_NAS_ROOT%/library ...
scp -r -q "%SRC%\." %PP_SSH_HOST%:"%PP_NAS_ROOT%/library/" || exit /b 1
rem Windows files arrive with odd modes and this user's own group. Give the
rem library to the apps group, group-writable, so the app can write into synced
rem folders (renders, slices, moves). chmod can't do that for folders the app
rem created, so this goes through TrueNAS's middleware (deploy\truenas.sh perms).
scp -q "%~dp0..\deploy\truenas.sh" %PP_SSH_HOST%:printpantry-truenas.sh || exit /b 1
ssh %PP_SSH_HOST% "sed -i 's/\r$//' ~/printpantry-truenas.sh && PP_NAS_ROOT=%PP_NAS_ROOT% bash ~/printpantry-truenas.sh perms" || exit /b 1
echo Done. Print Pantry picks up new files within 5 minutes, or press Rescan.

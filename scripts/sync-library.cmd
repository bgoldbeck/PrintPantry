@echo off
rem Copy the local library folder to the NAS (new and newer files; nothing is deleted).
rem A file that is newer on the NAS (for example an STL swapped with Replace STL in
rem the app) is kept: the PC copy only wins when it was changed later.
rem   scripts\sync-library.cmd                  uses D:\PrintPantry\library
rem   scripts\sync-library.cmd "E:\My Models"   another folder
setlocal
call "%~dp0_config.cmd" nodocker || exit /b 1
set "SRC=%~1"
if "%SRC%"=="" set "SRC=%PP_LOCAL_LIBRARY%"
if not exist "%SRC%\" (echo Folder not found: %SRC% & exit /b 1)
echo Copying "%SRC%" to %PP_SSH_HOST%:%PP_NAS_ROOT%/library ...
rem Upload to a staging folder with file times kept (-p), then copy into the
rem library only what is new or newer there (cp -u).
set "STAGE=%PP_NAS_ROOT%/sync-incoming"
ssh %PP_SSH_HOST% "rm -rf '%STAGE%' && mkdir -p '%STAGE%'" || exit /b 1
scp -r -p -q "%SRC%\." %PP_SSH_HOST%:"%STAGE%/" || exit /b 1
ssh %PP_SSH_HOST% "cp -r -p -u '%STAGE%/.' '%PP_NAS_ROOT%/library/'; rc=$?; rm -rf '%STAGE%'; exit $rc" || exit /b 1
rem Windows files arrive with odd modes and this user's own group. Give the
rem library to the apps group, group-writable, so the app can write into synced
rem folders (renders, slices, moves). chmod can't do that for folders the app
rem created, so this goes through TrueNAS's middleware (deploy\truenas.sh perms).
scp -q "%~dp0..\deploy\truenas.sh" %PP_SSH_HOST%:printpantry-truenas.sh || exit /b 1
ssh %PP_SSH_HOST% "sed -i 's/\r$//' ~/printpantry-truenas.sh && PP_NAS_ROOT=%PP_NAS_ROOT% bash ~/printpantry-truenas.sh perms" || exit /b 1
echo Done. Print Pantry picks up new files within 5 minutes, or press Rescan.

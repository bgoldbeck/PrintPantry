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
rem Windows files arrive with odd modes; make them readable by the app (apps user), not world-writable.
ssh %PP_SSH_HOST% "chmod -R u+rwX,g+rwX,o+rX,o-w '%PP_NAS_ROOT%/library'" || exit /b 1
echo Done. Print Pantry picks up new files within 5 minutes, or press Rescan.

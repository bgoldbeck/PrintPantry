@echo off
rem Copy your exported slicer presets to the NAS, where the slicer picks them up.
rem Export them from Snapmaker Orca / OrcaSlicer / Bambu Studio (File > Export >
rem Export Preset Bundle) into D:\PrintPantry\config first.
rem   scripts\sync-config.cmd                  uses D:\PrintPantry\config
rem   scripts\sync-config.cmd "E:\Presets"     another folder
setlocal
call "%~dp0_config.cmd" nodocker || exit /b 1
set "SRC=%~1"
if "%SRC%"=="" set "SRC=%PP_LOCAL_CONFIG%"
if not exist "%SRC%\" (echo Folder not found: %SRC% & exit /b 1)
echo Copying "%SRC%" to %PP_SSH_HOST%:%PP_NAS_ROOT%/config ...
scp -r -q "%SRC%\." %PP_SSH_HOST%:"%PP_NAS_ROOT%/config/" || exit /b 1
ssh %PP_SSH_HOST% "chmod -R u+rwX,go+rX,go-w '%PP_NAS_ROOT%/config'" || exit /b 1
echo Done. The slicer reloads presets on the next Slice.

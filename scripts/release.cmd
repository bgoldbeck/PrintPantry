@echo off
rem Test, build and push in one go.   scripts\release.cmd          (all steps)
rem                                   scripts\release.cmd notest   (skip tests)
setlocal
call "%~dp0_config.cmd" || exit /b 1
echo === Print Pantry %PP_VERSION% ===
if /i not "%~1"=="notest" (
  call "%~dp0test.cmd" || exit /b 1
)
call "%~dp0build.cmd" || exit /b 1
call "%~dp0push.cmd" || exit /b 1
echo === Released %PP_IMAGE%:%PP_VERSION% ===

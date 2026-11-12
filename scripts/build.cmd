@echo off
rem Build the Print Pantry image and tag it with the version and "latest".
setlocal
call "%~dp0_config.cmd" || exit /b 1
pushd "%~dp0.."
echo Building %PP_IMAGE%:%PP_VERSION% for %PP_PLATFORM%...
docker build --platform %PP_PLATFORM% -t "%PP_IMAGE%:%PP_VERSION%" -t "%PP_IMAGE%:latest" .
set "RC=%ERRORLEVEL%"
popd
if not "%RC%"=="0" (
  echo Build FAILED.
  exit /b %RC%
)
echo Built %PP_IMAGE%:%PP_VERSION% and %PP_IMAGE%:latest
exit /b 0

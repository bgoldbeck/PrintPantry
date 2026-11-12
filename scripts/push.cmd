@echo off
rem Upload the image built by build.cmd to ghcr.io.
setlocal
call "%~dp0_config.cmd" || exit /b 1
docker image inspect "%PP_IMAGE%:%PP_VERSION%" >nul 2>nul
if errorlevel 1 (
  echo %PP_IMAGE%:%PP_VERSION% has not been built yet. Run scripts\build.cmd first.
  exit /b 1
)
for %%t in (%PP_VERSION% latest) do (
  echo Pushing %PP_IMAGE%:%%t...
  docker push "%PP_IMAGE%:%%t"
  if errorlevel 1 (
    echo Push FAILED. If it says "denied" or "unauthorized", run scripts\login.cmd first.
    exit /b 1
  )
)
echo.
echo Pushed %PP_IMAGE%:%PP_VERSION% and :latest
echo On TrueNAS: Apps ^> printpantry ^> Update ^(or Stop then Start^) to pull it.
exit /b 0

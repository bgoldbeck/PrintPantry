@echo off
rem Shared settings for the build scripts. Called by the other .cmd files.
rem Override any of these by setting them before running a script.

if not defined PP_IMAGE set "PP_IMAGE=ghcr.io/bgoldbeck/printpantry"
if not defined PP_GHCR_USER set "PP_GHCR_USER=bgoldbeck"
rem TrueNAS runs on x86-64.
if not defined PP_PLATFORM set "PP_PLATFORM=linux/amd64"

rem Version comes from app\__init__.py, e.g.  __version__ = "0.1.0"
set "PP_VERSION="
for /f "tokens=2 delims== " %%v in ('findstr /b "__version__" "%~dp0..\app\__init__.py"') do set "PP_VERSION=%%~v"
if not defined PP_VERSION (
  echo Could not read the version from app\__init__.py
  exit /b 1
)

where docker >nul 2>nul
if errorlevel 1 (
  echo Docker was not found. Install Docker Desktop and make sure it is running.
  exit /b 1
)
exit /b 0

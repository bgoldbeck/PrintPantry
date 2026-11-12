@echo off
rem Run the test suite inside a throwaway Python container (no local Python needed).
setlocal
call "%~dp0_config.cmd" || exit /b 1
pushd "%~dp0.."
echo Running tests...
docker run --rm -e PYTHONDONTWRITEBYTECODE=1 -v "%CD%:/src:ro" -w /src python:3.13-slim ^
  sh -c "pip install -q --root-user-action=ignore -r requirements.txt -r requirements-dev.txt && python -m pytest -q -p no:cacheprovider"
set "RC=%ERRORLEVEL%"
popd
if not "%RC%"=="0" echo Tests FAILED.
exit /b %RC%

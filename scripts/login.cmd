@echo off
rem One-time login to the GitHub container registry (ghcr.io).
rem Use a GitHub personal access token (classic) with the "write:packages" scope:
rem   https://github.com/settings/tokens/new?scopes=write:packages
rem Paste the token when asked for a password. Docker remembers it afterwards.
setlocal
call "%~dp0_config.cmd" || exit /b 1
docker login ghcr.io -u %PP_GHCR_USER%

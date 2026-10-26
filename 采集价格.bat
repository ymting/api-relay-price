@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"

set "PY="
where python >nul 2>nul && set "PY=python"
rem 回退：本机托管 Python。按版本目录名倒序取第一个可用的，不写死用户名与版本号，
rem 换机器或升级版本后仍然有效；找不到就继续走下面的安装提示。
if not defined PY (
  for /f "delims=" %%D in ('dir /b /ad /o-n "%USERPROFILE%\.workbuddy\binaries\python\versions" 2^>nul') do (
    if not defined PY (
      if exist "%USERPROFILE%\.workbuddy\binaries\python\versions\%%D\python.exe" (
        set "PY=%USERPROFILE%\.workbuddy\binaries\python\versions\%%D\python.exe"
      )
    )
  )
)
if not defined PY (
  echo Python 3.10 or newer is required. Please install Python and try again.
  pause
  exit /b 1
)

"%PY%" fetch_prices.py

echo.
pause

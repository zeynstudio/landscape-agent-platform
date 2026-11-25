@echo off
setlocal
cd /d "%~dp0"
title 景观生成平台启动器

rem ---- 端口（设环境变量 PLATFORM_PORT 可换端口）----
if not defined PLATFORM_PORT set PLATFORM_PORT=8400

rem ---- 探测可用 Python：PATH → py 启动器 → 常见安装路径（换机器不用改脚本）----
set PY=
python -c "pass" >nul 2>&1 && set PY=python
if not defined PY (py -c "pass" >nul 2>&1 && set PY=py)
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python310\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
if not defined PY (
  echo [错误] 找不到可用的 Python。请安装 Python 3.10+，或编辑本脚本手动填写 PY= 完整路径。
  pause
  exit /b 1
)

rem ---- 已在运行？不重复启动，直接开页面 ----
%PY% -c "import urllib.request; urllib.request.urlopen('http://localhost:%PLATFORM_PORT%/api/health', timeout=1)" >nul 2>&1
if not errorlevel 1 (
  echo 平台已在运行（http://localhost:%PLATFORM_PORT%），直接打开浏览器……
  start "" http://localhost:%PLATFORM_PORT%
  exit /b 0
)

rem ---- 启动服务器（保留可见窗口，报错看得见；关闭该窗口即停止平台）----
echo 使用 Python: %PY%
start "景观生成平台服务（关闭此窗口即停止）" %PY% app.py

rem ---- 轮询就绪（最多 20 秒），就绪再开浏览器，避免"拒绝连接"闪屏 ----
set N=0
:wait
%PY% -c "import urllib.request; urllib.request.urlopen('http://localhost:%PLATFORM_PORT%/api/health', timeout=1)" >nul 2>&1
if not errorlevel 1 goto open
set /a N+=1
if %N% geq 20 (
  echo [错误] 服务器 20 秒未就绪，请查看"景观生成平台服务"窗口里的报错信息。
  pause
  exit /b 1
)
timeout /t 1 /nobreak >nul
goto wait

:open
echo 平台已就绪： http://localhost:%PLATFORM_PORT%
start "" http://localhost:%PLATFORM_PORT%
exit /b 0

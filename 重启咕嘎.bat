@echo off
chcp 65001 >nul
rem ===== 重启咕嘎：先关掉正在运行的桌宠，再重新启动 =====
rem 插件改了代码后必须重启进程才会生效（Python 启动时就把钩子读进内存了）。
cd /d "%~dp0"

set "PYTHON="
for %%C in (python py) do (
    if not defined PYTHON (
        where %%C >nul 2>&1 && %%C --version >nul 2>&1 && set "PYTHON=%%C"
    )
)
if not defined PYTHON if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PYTHON=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PYTHON (
    echo [ERROR] 没找到 Python，请先双击 启动企鹅.bat
    pause
    exit /b 1
)

if "%PYTHON%"=="python" (
    where pythonw >nul 2>&1 && set "PYTHONW=pythonw" || set "PYTHONW=python"
) else if "%PYTHON%"=="py" (
    set "PYTHONW=pyw"
) else (
    set "PYTHONW=%PYTHON:\python.exe=\pythonw.exe%"
)

echo [1/2] 关闭正在运行的咕嘎...
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -like '*launcher.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
timeout /t 2 /nobreak >nul

echo [2/2] 重新启动...
start "" "%PYTHONW%" launcher.py
echo 已发出启动命令。几秒后企鹅会回到桌面或右下角托盘。
timeout /t 3 /nobreak >nul

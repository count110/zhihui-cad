@echo off
chcp 65001 >nul
setlocal
set "PYTHONUTF8=1"
set "PYTHONNOUSERSITE=1"
set "ZHIHUI_MCP=inproc"
cd /d "%~dp0app"
"%~dp0runtime\python\python.exe" -m pytest -q
if errorlevel 1 exit /b 1
"%~dp0runtime\python\python.exe" -m zhihui.bench.run --mock --agent-mode multi --category S
pause

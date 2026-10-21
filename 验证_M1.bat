@echo off
chcp 65001 >nul
setlocal
set "ROOT=%~dp0"
set "PY=%ROOT%runtime\python\python.exe"
set "PYTHONPATH=%ROOT%app"
set "PYTHONNOUSERSITE=1"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "ZHIHUI_LLM=mock"
set "ZHIHUI_MCP=inproc"
cd /d "%ROOT%app"
"%PY%" -m zhihui.contracts
if errorlevel 1 goto failed
"%PY%" -m pytest -q -p no:cacheprovider
if errorlevel 1 goto failed
"%PY%" -m zhihui.agent.replay run --mock --output "%ROOT%M1验收\S001-S005回放.json" --data-dir "%ROOT%M1验收\replay_data"
if errorlevel 1 goto failed
echo Milestone 1: all gates passed.
pause
exit /b 0
:failed
echo Milestone 1: gate failed. Please inspect the error above.
pause
exit /b 1

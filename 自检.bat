@echo off
chcp 65001 >nul
setlocal
rem ---- 可改：端口；HOST=0.0.0.0 时局域网其他电脑也能访问（注意防火墙和安全） ----
set "PORT=8000"
set "HOST=127.0.0.1"
rem ------------------------------------------------------------------------------
set "ROOT=%~dp0"
set "PY=%ROOT%runtime\python\python.exe"
set "PYTHONPATH=%ROOT%app"
set "PYTHONNOUSERSITE=1"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
if not exist "%PY%" (
  echo 找不到 runtime\python\python.exe，请确认压缩包完整解压。
  pause
  exit /b 1
)
cd /d "%ROOT%app"
echo [1/2] 单元测试（约 2 分钟）……
"%PY%" -m pytest -q -p no:cacheprovider
echo.
echo [2/2] 基准用例（脚本智能体，离线，约 1 分钟）……
"%PY%" -m zhihui.bench.run --llm scripted
echo.
echo 全部通过即部署成功。
pause

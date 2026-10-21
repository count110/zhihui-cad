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
if not exist "%ROOT%app\.env" (
  copy "%ROOT%app\04_后端与部署\.env.example" "%ROOT%app\.env" >nul
  echo 第一次使用：已生成 app\.env，请填写大模型 API Key（如 DEEPSEEK_API_KEY=...），保存后重新运行本文件。
  notepad "%ROOT%app\.env"
  pause
  exit /b 0
)
echo.
echo  智绘CAD · 大模型模式（需要能访问大模型接口的网络，Key 在 app\.env）
echo  浏览器地址：http://127.0.0.1:%PORT%   关闭本窗口即停止服务
echo.
start "" cmd /c "timeout /t 5 >nul & start http://127.0.0.1:%PORT%"
"%PY%" -m zhihui.api --host %HOST% --port %PORT%
echo 服务已退出（如果马上退出，多半是端口 %PORT% 被占用，或 app\.env 里没有填 Key）。
pause

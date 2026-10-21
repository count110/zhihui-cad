"""本机启动：python -m zhihui.api [--mock] [--port 8000]"""

import argparse
import os

import uvicorn

from zhihui.env import load_env

load_env()  # 读取项目根目录 .env（API Key、代理等）

ap = argparse.ArgumentParser()
ap.add_argument("--mock", action="store_true", help="离线演示：不调用真实大模型")
ap.add_argument("--host", default="127.0.0.1")
ap.add_argument("--port", type=int, default=8000)
args = ap.parse_args()
if args.mock:
    os.environ["ZHIHUI_LLM"] = "mock"
uvicorn.run("zhihui.api.app:app", host=args.host, port=args.port)

# 智绘CAD 镜像：API / Worker / MCP Server 共用一个镜像，启动命令不同
# 构建（在项目根目录执行）：docker build -f 04_后端与部署/Dockerfile -t zhihui-cad .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ZHIHUI_DATA_DIR=/data \
    ZHIHUI_CJK_FONT=wqy-microhei.ttc \
    MPLCONFIGDIR=/tmp/mpl

# 中文字体：PNG/PDF 预览里的中文全靠它，没有就是方框
RUN apt-get update \
 && apt-get install -y --no-install-recommends fonts-wqy-microhei \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
COPY zhihui ./zhihui
RUN pip install --no-cache-dir . && (ezdxf --fonts >/dev/null 2>&1 || true)
COPY 05_前端/web ./05_前端/web

RUN useradd -m app && mkdir -p /data && chown app /data
USER app
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "zhihui.api.app:app", "--host", "0.0.0.0", "--port", "8000"]

# syntax=docker/dockerfile:1
FROM python:3.12-slim

# 容器内不走宿主代理（pip 受影响）；国内构建用 PIP_INDEX_URL 换源
ENV HTTP_PROXY= HTTPS_PROXY= http_proxy= https_proxy= NO_PROXY=localhost,127.0.0.1 no_proxy=localhost,127.0.0.1

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# 国内构建可换成镜像源：中国科学技术大学 / 阿里云 / 清华
ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL}

WORKDIR /app

# 1) 先装依赖（利用构建缓存）
COPY pyproject.toml README.md ./
COPY src/msg_hub/__init__.py src/msg_hub/__init__.py
RUN pip install --no-cache-dir -U pip setuptools wheel \
 && pip install --no-cache-dir "mcp>=2.3,<3" "starlette>=0.40" "uvicorn>=0.30" \
      "jsonschema>=4.20" "httpx>=0.27" "PySocks>=1.7"

# 2) 再装源码
COPY src/ ./src/
RUN pip install --no-cache-dir --no-deps .

# 3) 渠道类型插件（每个类型一个目录：manifest.json + send_msg.py）
COPY types/ /app/types/

ENV MSG_HUB_DATA_DIR=/data \
    MSG_HUB_TYPES_DIR=/app/types \
    MSG_HUB_HOST=0.0.0.0 \
    MSG_HUB_PORT=8788

VOLUME ["/data"]
EXPOSE 8788

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8788/health', timeout=3).status == 200 else 1)"

CMD ["python", "-m", "msg_hub"]

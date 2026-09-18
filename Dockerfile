# StockLab · NAS 部署镜像
#
# 设计目标：构建过程只依赖 pip，不依赖 apt。
#   群晖在国内网络下访问 Debian 源经常超时，而 apt 失败会让整个构建中断。
#   时区数据改用 PyPI 的 tzdata 包提供（已在 requirements.txt 中），
#   因此这里不需要 apt-get install tzdata。
#
# 若 NAS 能直连 pypi，把 PIP_INDEX_URL 传成 https://pypi.org/simple 即可。
# 国内直连 pypi 会 SSL 中断（实测），所以默认用阿里云镜像。

FROM python:3.11-slim

ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
ARG PIP_TRUSTED_HOST=mirrors.aliyun.com

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai \
    SL_DATA_DIR=/data \
    SL_HOST=0.0.0.0 \
    SL_PORT=8787

WORKDIR /app

# 依赖层单独缓存：改代码不会导致重装依赖
COPY requirements.txt .
RUN pip install \
        --index-url "${PIP_INDEX_URL}" \
        --trusted-host "${PIP_TRUSTED_HOST}" \
        --retries 5 --timeout 60 \
        -r requirements.txt

COPY app ./app
COPY VERSION ./VERSION

# 数据目录（数据库 / 缓存 / 报告），由 compose 挂载到 NAS 磁盘
RUN mkdir -p /data/db /data/cache /data/reports /data/logs

VOLUME ["/data"]
EXPOSE 8787

# 健康检查用 Python 实现，避免依赖 curl（镜像里没装）
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request,os,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('SL_PORT','8787')+'/api/health',timeout=8).status==200 else 1)"

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8787", "--no-access-log"]

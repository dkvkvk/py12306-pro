# py12306-pro :: P0-1 环境现代化
# 上游是 python:3.6.6-slim（3.6 已 EOL 多年），这里升到 3.11。
FROM python:3.11-slim

LABEL org.opencontainers.image.title="py12306-pro" \
      org.opencontainers.image.description="py12306 生产化改造：风控熔断 / 自适应抖动 / 密钥环境变量化 / 可视化面板" \
      org.opencontainers.image.source="https://github.com/dkvkvk/py12306-pro"

# PYTHONUNBUFFERED  : 容器日志实时可见（不再依赖 -u）
# PYTHONDONTWRITEBYTECODE: 只读挂载下不写 .pyc
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    LANG=C.UTF-8 \
    TZ=Asia/Shanghai

WORKDIR /code

# ---- 系统依赖 ----
# libxml2-dev/libxslt1-dev/gcc: lxml 万一回落到源码编译也能装
# fonts-noto-cjk：pyppeteer 渲染中文验证码必需，缺了识别率暴跌（约 +250MB，
#   想省空间可换 fonts-wqy-zenhei，只覆盖简体）
# ca-certificates/curl：HTTPS 与健康检查
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        libxml2-dev \
        libxslt1-dev \
        gcc \
        ca-certificates \
        curl \
        tzdata \
        fonts-noto-cjk; \
    rm -rf /var/lib/apt/lists/*

# ---- Python 依赖（先只拷清单，利用 layer cache）----
COPY requirements.in requirements-lock.txt constraints-py311.txt /code/

# USE_LOCK=1（默认）：按锁文件可复现构建
# USE_LOCK=0        ：按 requirements.in 取最新兼容版本（升级验证用）
ARG USE_LOCK=1
ARG PIP_INDEX_URL=https://pypi.org/simple
RUN set -eux; \
    if [ "$USE_LOCK" = "1" ]; then \
        pip install --no-cache-dir -i "$PIP_INDEX_URL" -r requirements-lock.txt; \
    else \
        pip install --no-cache-dir -i "$PIP_INDEX_URL" -r requirements.in -c constraints-py311.txt; \
    fi

# ---- 非 root 运行 ----
# runtime/ 存登录态（能直接下单的 cookie，敏感），/data 存队列与指标，分开挂载
RUN set -eux; \
    groupadd --gid 10001 py12306; \
    useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin py12306; \
    mkdir -p /code/runtime /code/data /code/logs; \
    chown -R py12306:py12306 /code; \
    chmod 700 /code/runtime

COPY --chown=py12306:py12306 . /code

USER py12306

EXPOSE 8008 8010

# 健康检查：走面板的 /panel/api/health（会顺带查 Redis），
# 避免「进程活着但 Redis/配置是坏的」被判定为健康
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8010/panel/api/health',timeout=5).status==200 else 1)"

# 默认启动上游抢票流程（含面板与风控接入）：
#   docker compose up -d
# 其它入口：
#   docker compose run --rm py12306 -t                  自检
#   docker compose run --rm py12306 serve --port 8010    只启面板
ENTRYPOINT ["python", "main.py"]
CMD []

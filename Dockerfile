# P0-1 环境现代化：Python 3.6.6-slim -> 3.11-slim
FROM python:3.11-slim

# PYTHONUNBUFFERED: 容器日志实时可见（原来靠 -u 参数，容易漏）
# PYTHONDONTWRITEBYTECODE: 只读挂载下不写 .pyc
# PIP_NO_CACHE_DIR: 镜像里不留 pip 缓存
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    LANG=C.UTF-8 \
    TZ=Asia/Shanghai

WORKDIR /app

# ---- 系统依赖 ----
# libxml2-dev / libxslt1-dev / gcc: lxml 编译兜底（3.11 通常能拿到 wheel，
#   但锁文件万一在某架构上回落到 sdist，这里保证还能装得上）
# ca-certificates: 12306 走 HTTPS
# fonts-noto-cjk: pyppeteer 渲染中文验证码/页面时必需，缺字体识别率暴跌
#   （想省 ~250MB 可换成 fonts-wqy-zenhei，只覆盖简体）
# curl: 容器 HEALTHCHECK 用
# gosu: 以非 root 运行时的辅助（见下）
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

# ---- Python 依赖（分两段，利用 Docker layer cache）----
# 先只拷依赖清单：改业务代码不会让 pip 重装
COPY requirements.in constraints-py311.txt requirements-lock.txt /app/

# USE_LOCK=1（默认）：用锁文件可复现构建
# USE_LOCK=0：忽略锁文件，按 requirements.in 取最新兼容版本（升级验证时用）
ARG USE_LOCK=1
RUN set -eux; \
    if [ "${USE_LOCK}" = "1" ]; then \
        pip install --no-cache-dir -r requirements-lock.txt; \
    else \
        pip install --no-cache-dir -r requirements.in -c constraints-py311.txt; \
    fi

# ---- 非 root 运行 ----
# runtime/ 存登录态（能直接下单的 cookie），/data 存 Redis 之外的本地状态
RUN set -eux; \
    groupadd --gid 10001 py12306; \
    useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin py12306; \
    mkdir -p /app/runtime /data; \
    chown -R py12306:py12306 /app /data; \
    chmod 700 /app/runtime

COPY --chown=py12306:py12306 . /app

USER py12306

# ---- 健康检查：用 -t 自检，避免「进程活着但 Redis/登录态是坏的」被当成健康 ----
HEALTHCHECK --interval=30s --timeout=20s --start-period=90s --retries=3 \
    CMD python main.py -t --health-only || exit 1

ENTRYPOINT ["python", "main.py"]
CMD []

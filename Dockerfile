# 基础镜像：默认用 docker.1panel.live 代理（飞牛 NAS 实测可拉取，绕过 docker.io 的 401 与 dockerpull.com 的超时）。
# 若你的 NAS 能直连 docker.io，把下一行 docker.1panel.live/... 改回 python:3.11-slim 即可。
ARG PY_BASE=docker.1panel.live/library/python:3.11-slim
FROM ${PY_BASE}

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LEDGER_DB=/data/ledger.db

# 离线安装：依赖 wheel 已随项目打包在 wheels/（同时含 x86_64 与 aarch64 两种架构）。
# 安装前按容器实际架构筛一遍，避免 find-links 目录里混多架构 wheel 导致 pip 挑花眼报错。
COPY requirements.txt .
COPY wheels ./wheels

RUN set -e; \
    if [ -z "$(ls -A /app/wheels 2>/dev/null)" ]; then \
      echo "============================================================="; \
      echo "ERROR: /app/wheels 为空或不存在！"; \
      echo "请确认已把项目里的 wheels/ 目录（约 12MB，46 个 .whl）"; \
      echo "连同 Dockerfile / requirements.txt / app/ 一起上传到 NAS 项目。"; \
      echo "============================================================="; \
      exit 1; \
    fi; \
    ARCH=$(uname -m); \
    if [ "$ARCH" = "aarch64" ] || [ "$ARCH" = "arm64" ]; then KEEP=aarch64; else KEEP=x86_64; fi; \
    mkdir -p /app/wheels_$KEEP; \
    for f in /app/wheels/*; do \
      if echo "$f" | grep -q "$KEEP" || echo "$f" | grep -q "none-any"; then \
        cp "$f" /app/wheels_$KEEP/; \
      fi; \
    done; \
    echo ">>> 检测到容器架构: $ARCH -> 选用 $KEEP 架构 wheel，共 $(ls /app/wheels_$KEEP | wc -l) 个"; \
    pip install --no-cache-dir --no-index --find-links=/app/wheels_$KEEP -r requirements.txt; \
    rm -rf /app/wheels /app/wheels_$KEEP

COPY app ./app
RUN mkdir -p /data

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

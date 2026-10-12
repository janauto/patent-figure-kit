# syntax=docker/dockerfile:1.7
# patent-figure-kit 工作台镜像（WP4 · T2）。只支持 linux/amd64：cadquery-ocp 7.7.2 没有 Linux ARM 轮子。
#
#   docker build --platform linux/amd64 -t patent-figure-kit .
#   docker compose up -d            # 推荐：app + Caddy（自动 HTTPS + Basic Auth），见 docs/deploy/server.md
#
# 基底 Python 3.11：N10 金样在 3.11 上已于开发机（macOS arm64）通过；Linux/amd64 上由 CI（docker.yml、
# golden-matrix.yml）复核。镜像不含任何密钥：token 只经环境变量 WORKBENCH_TOKEN 在运行时注入。

ARG PYTHON_IMAGE=python:3.11-slim-bookworm

# ---------------------------------------------------------------- 构建：锁定依赖 + 本项目 wheel
# hadolint ignore=DL3029  # 故意钉 amd64：OCP 7.7.2 无 Linux ARM 轮子，ARM 主机上构建也要产出 amd64 镜像
FROM --platform=linux/amd64 ${PYTHON_IMAGE} AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /src
# 先只拷依赖清单，依赖层可缓存
COPY requirements-pinned.txt requirements-web.txt ./
# cadquery-ocp 7.7.2 是 manylinux_2_35 轮子，必须先升级 pip 才认得这个平台标签
RUN python -m venv /opt/venv \
 && /opt/venv/bin/python -m pip install -U pip \
 && /opt/venv/bin/python -m pip install -r requirements-pinned.txt -r requirements-web.txt
COPY . .
RUN /opt/venv/bin/python -m pip wheel --no-deps -w /tmp/wheels . \
 && /opt/venv/bin/python -m pip install --no-deps /tmp/wheels/patent_figure_kit-*.whl \
 && /opt/venv/bin/pfk paths --check

# ---------------------------------------------------------------- 运行：非 root、中文字体、健康检查
# hadolint ignore=DL3029  # 同上
FROM --platform=linux/amd64 ${PYTHON_IMAGE} AS runtime
# libgl1 / libx11-6：OCP 轮子在 Linux 上的外部依赖（其余已随轮子打包）；fonts-noto-cjk：预览与 PDF 的中文字形
# （scripts/patent_figure/sheet.py 的 PREVIEW_CJK_CANDIDATES 在 /usr/share/fonts/opentype/noto/ 下找）。
# 不钉 apt 版本（DL3008）：bookworm 安全更新会撤下旧版本号，钉死反而构建失败；基底镜像标签已固定发行版。
# hadolint ignore=DL3008
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgl1 libx11-6 libexpat1 fonts-noto-cjk \
 && rm -rf /var/lib/apt/lists/*
RUN groupadd --system --gid 10001 pfk \
 && useradd --system --uid 10001 --gid pfk --home-dir /home/pfk --create-home --shell /usr/sbin/nologin pfk \
 && mkdir -p /data \
 && chown pfk:pfk /data
COPY --from=build /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/home/pfk \
    MPLCONFIGDIR=/tmp/matplotlib \
    PATENT_WORKBENCH_CONFIG=/data/config
USER 10001:10001
WORKDIR /data
VOLUME ["/data"]
EXPOSE 8790
# /healthz 由部署闸门直接应答（不经业务代码、不需要 token、不含数据）；只在服务器形态存在。
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD ["python", "-c", "import sys,urllib.request; r=urllib.request.urlopen('http://127.0.0.1:8790/healthz', timeout=4); sys.exit(0 if r.status == 200 else 1)"]
# 远程监听在这里显式写出（--host 0.0.0.0 --allow-remote）；对外地址、可信代理由环境变量
# PFK_PUBLIC_URL / PFK_TRUSTED_PROXY 给出（缺 PFK_PUBLIC_URL 时拒绝启动，不会静默暴露）。
ENTRYPOINT ["patent-figure-kit"]
CMD ["--data", "/data/workbench", "--port", "8790", "--host", "0.0.0.0", "--allow-remote", "--no-browser", "--no-example"]

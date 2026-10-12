# 安装时长（实测）

> 由 `scripts/verified_envs.py render` 从各环境的记录生成，**不要手改**。时长 = 从建虚拟环境到依赖装完（不含下载 Python 本身）；强依赖网络，表里写明了测量时的网络与 pip 缓存状态。

| 环境 | 平台 | Python | 方式 | 时长 | 网络 / 缓存 | 日期 | 来源 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| macOS 本机（wheel 安装） | macOS 27.0.1 | 3.12.15 | pip install wheel[web,test]（仓库外全新虚拟环境） | 205 s（约 3.4 分钟） | 家用宽带直连 pypi.org，pip --no-cache-dir | 2026-10-12 | 本机实测 |
| macOS 本机（安装并启动.command） | macOS 27.0.1 | 3.12.15 | 安装并启动.command → install.sh | 178 s（约 3.0 分钟） | 家用宽带直连 pypi.org，pip 无缓存（PIP_NO_CACHE_DIR=1），下载期间有读超时重试 | 2026-10-12 | 本机实测 |

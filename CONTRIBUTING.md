# 参与贡献

欢迎提 Issue 和 PR。下面几条是这个项目能保持「可复现、可审计」的前提，提交前请逐条确认。

## 开发环境

```bash
python3 -m venv .venv && source .venv/bin/activate
python3 -m pip install -U pip
python3 -m pip install -r requirements-pinned.txt -r requirements-web.txt
python3 scripts/pfk_cli.py doctor        # 结论：可以启动工作台。
```

Python 3.9–3.12；新代码保持 Python 3.9 语法。

## 提交前

```bash
python3 acceptance/run_acceptance.py     # 19 个节点全部通过，退出码 0（需要几分钟；不要用管道接输出）
```

- **N10 不得变**：`tests/fixtures/golden_digest.txt` 是金样。摘要变了说明图纸变了，先读差异，不要直接重录。
- 新功能要有测试（`tests/`），改行为的同时改测试并在 PR 里说明为什么。
- 前端改动另跑 `python3 webui/dev/verify_ui.py`；动到真实后端接线时跑 `python3 webui/dev/e2e_live.py`。

## 红线

1. 模型（以及任何 AI 通道）只写 `figure-plan.json` 里的语义：分图、中文术语、标号方式。坐标、字高、版面常数、标号一律由程序计算。
2. 只有 `scripts/patent_figure/occ_backend.py` 可以 import OCP。
3. numpy 单线程，投影逐元素，保证同一环境下结果一致。
4. 没过质量闸门的图不得进入交付目录。
5. 工作台默认只绑 `127.0.0.1`；对外监听必须显式 `--allow-remote` 并给 `--public-url`。
6. 不提交密钥、token、本机绝对路径、客户数据或未公开产品的模型与附图。示例与截图只用 `scripts/make_demo_assemblies.py` 生成的三个公开演示模型。

## 文档同步

- `docs/impl-contract.md` 是模块边界、JSON 形状与确定性规则的唯一权威；改了实现要同步它。
- 改依赖版本表时，同步 `scripts/doctor.py` 的 `PINNED` 与 `requirements-pinned.txt`。
- 改 REST 路由时同步 `docs/openapi.yaml`（`tests/test_jobs_api.py` 会核对）。
- README 用图用 `python3 docs/assets/capture_readme.py` 重拍，不要手工替换；性能与安装时长只写实测值，并注明机型、版本与缓存状态。
- 措辞：不对合规性或受理结果作承诺；法律相关一律写「按 CNIPA 形式要求排版，最终以受理为准，不构成法律意见」。

## 提交信息

一句话说清「改了什么、为什么」，不写流水账。一个 PR 只做一件事。

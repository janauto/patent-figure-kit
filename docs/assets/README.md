# 重新生成这些素材

| 文件 | 来源 | 重新生成 |
| --- | --- | --- |
| `shot-studio.png`、`shot-home.png` | 真实 Workbench 界面截图 | `python3 docs/assets/capture.py` |
| `out-fig1-assembly.png`、`out-fig2-exploded.png` | 示例 plan 的真实渲染预览 | 见下 |

**界面截图**：`capture.py` 会用全新的临时数据目录与配置目录启动一个独立实例，只含合成示例工程，
不读写 `~/.patent-workbench` 与 `~/.config/patent-workbench`。需要 `pip install playwright` 与本机 Chrome
（没有 Chrome 时先执行 `playwright install chromium`）。

**出图预览**：

```bash
python3 scripts/analyze_assembly.py tests/fixtures/synthetic.stp -o /tmp/assembly.json --include 'SYN-*'
python3 scripts/render_patent_figure.py examples/synthetic/figure-plan.json --assembly /tmp/assembly.json -o /tmp/out --preview
cp /tmp/out/fig1.png docs/assets/out-fig1-assembly.png
cp /tmp/out/fig2.png docs/assets/out-fig2-exploded.png
```

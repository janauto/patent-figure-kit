# 重新生成这些素材

| 文件 | 来源 | 重新生成 |
| --- | --- | --- |
| `shot-studio.png`、`shot-home.png` | 真实 Workbench 界面截图 | `python3 docs/assets/capture.py` |
| `out-fig1-assembly.png`、`out-fig2-exploded.png` | `examples/planetary-gearbox` 的真实渲染预览（交底版） | 见下 |

**界面截图**：`capture.py` 会用全新的临时数据目录与配置目录启动一个独立实例，用行星齿轮减速器建一个示例工程，
不读写 `~/.patent-workbench` 与 `~/.config/patent-workbench`，并显式关闭 AI 通道，不调用任何大模型。需要 `pip install playwright` 与本机 Chrome
（没有 Chrome 时先执行 `playwright install chromium`）。

**出图预览**：

```bash
python3 scripts/make_demo_assemblies.py -o examples/planetary-gearbox
python3 scripts/analyze_assembly.py examples/planetary-gearbox/demo-planetary-gearbox.stp -o /tmp/assembly.json --include 'PGB-*'
python3 scripts/render_patent_figure.py examples/planetary-gearbox/figure-plan.json --assembly /tmp/assembly.json -o /tmp/out --preview
for n in 1 2; do python3 scripts/annotate_figure_sheet.py /tmp/out/fig$n.dxf --numerals /tmp/out/reference-numerals.json --figure-number $n -o /tmp/out/fig${n}_annotated.dxf --preview; done
cp /tmp/out/fig1_annotated.png docs/assets/out-fig1-assembly.png
cp /tmp/out/fig2_annotated.png docs/assets/out-fig2-exploded.png
```

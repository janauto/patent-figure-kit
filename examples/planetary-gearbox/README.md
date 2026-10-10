# 示例：行星齿轮减速器（27 个零件实例，15 种零件）

演示用装配体由 `scripts/make_demo_assemblies.py` 生成，与任何真实产品无关。齿轮齿形、轴承、油封、
内六角螺钉都参与隐藏线消隐，比 `examples/synthetic/` 的合成模型复杂得多。

```bash
python3 scripts/make_demo_assemblies.py -o examples/planetary-gearbox
python3 scripts/analyze_assembly.py examples/planetary-gearbox/demo-planetary-gearbox.stp \
        -o assembly.json --include 'PGB-*'
python3 scripts/validate_figure_plan.py examples/planetary-gearbox/figure-plan.json --assembly assembly.json
python3 scripts/render_patent_figure.py examples/planetary-gearbox/figure-plan.json \
        --assembly assembly.json -o out/ --preview
```

预期：`共 2 张图，全部通过`；`out/` 下有 `fig1.dxf`（整体结构图）、`fig2.dxf`（齿轮系分解图）、预览与 `reference-numerals.json`。

## 这份 plan 的拆图思路

27 个零件实例塞进一张图会被质量闸门拒绝（字高低于交付下限，或标号无处可放）。所以：

- **图1 整体结构图**：只放从外面看得见的零件。行星销、行星架等内部件在整体图里被遮住，标不出来
- **图2 齿轮系分解图**：把内部件沿主轴拉开，逐件标号
- **油封**在整体图里几乎被端盖遮住，闸门报「引线锚点离几何过远」，按提示把它从图1的 `members` 中移除，改在图2里标注
- 紧固螺钉设为 `label: "none"`：画出来，但不发标号

生成的 `.stp` 与 `out/` 已被 `.gitignore` 忽略。

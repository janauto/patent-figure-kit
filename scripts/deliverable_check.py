#!/usr/bin/env python3
"""逐页核验说明书附图 PDF / Word（F1）：页幅、页边、缩放后最小字高（mm 与 px）、图号存在、页数。

    python3 scripts/deliverable_check.py 说明书附图_递交版.pdf \\
            --sources 递交版_仅图号/fig1_filing.dxf 递交版_仅图号/fig2_filing.dxf [--preset standard]

``--sources`` 按页序（图1、图2…）给出该文件对应的规范版本 DXF（导出包里就有）。检查对象是**文件本身**：
图片的位置、像素与图号墨迹都从 PDF / Word 里读出来，不信任写出端的自报。实现见
``scripts/patent_figure/deliverable_check.py``；PDF 需要可选依赖 pypdf。

退出码：0 通过；1 不通过（原因逐页列出）；2 用法错误。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from patent_figure.deliverable_check import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())

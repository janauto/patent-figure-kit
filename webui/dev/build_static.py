#!/usr/bin/env python3
"""生成可独立部署的只读静态站 static-site/（WP4 会复用本脚本）。

正式工作台首页用绝对 /static/* 资源（workbench.py 的挂载）；但部署到普通静态目录或
GitHub Pages 子路径时必须是相对路径，且无 ?static 也要进静态模式。本脚本把首页/规划台
的资源重写为相对路径、注入 <meta name="inkline-mode" content="static">，并带上 manifest
与三个示例的预计算资产（GLB / SVG / QA）。

    python3 webui/dev/build_static.py
    # 在不做任何路由重写的静态服务器的子路径下验证：
    cd <该脚本打印的父目录>
    python3 -m http.server 8808
    # 打开 http://127.0.0.1:8808/pfk/home.html
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent          # webui/dev
WEBUI = HERE.parent                             # webui
OUT = HERE / "static-site" / "pfk"              # 故意放进子目录 pfk/，验证子路径部署

META = '<meta name="inkline-mode" content="static">'


def inject_meta(html: str) -> str:
    if 'inkline-mode' in html:
        return html
    return html.replace('<meta name="viewport"', META + '\n<meta name="viewport"', 1)


def build():
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    # 顶层资源
    for name in ("tokens.css", "home.css", "app.css"):
        shutil.copy2(WEBUI / name, OUT / name)
    shutil.copytree(WEBUI / "js", OUT / "js")
    shutil.copytree(WEBUI / "vendor", OUT / "vendor")   # 规划台 3D 需要 three.js

    # home.html：/static/* → 相对；注入静态 meta
    home = (WEBUI / "home.html").read_text(encoding="utf-8")
    home = home.replace('href="/static/', 'href="').replace('src="/static/', 'src="')
    home = inject_meta(home)
    (OUT / "home.html").write_text(home, encoding="utf-8")

    # index.html：已是相对路径；注入静态 meta
    idx = (WEBUI / "index.html").read_text(encoding="utf-8")
    idx = inject_meta(idx)
    (OUT / "index.html").write_text(idx, encoding="utf-8")

    # manifest + 三个示例的预计算资产（manifest 里用的是 dev/fixtures/... 相对路径）
    (OUT / "dev").mkdir()
    shutil.copy2(HERE / "manifest.json", OUT / "dev" / "manifest.json")
    shutil.copytree(HERE / "fixtures", OUT / "dev" / "fixtures")

    total = sum(f.stat().st_size for f in OUT.rglob("*") if f.is_file())
    print(f"静态站已生成：{OUT}")
    print(f"  文件数 {sum(1 for _ in OUT.rglob('*') if _.is_file())}，合计 {total/1048576:.1f} MB")
    print(f"  验证（无路由重写，子路径 /pfk/）：")
    print(f"    cd {OUT.parent}")
    print(f"    python3 -m http.server 8808")
    print(f"    open http://127.0.0.1:8808/pfk/home.html")


if __name__ == "__main__":
    build()

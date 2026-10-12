"""只读打开 zip（导出包样例审计、部署冒烟核对下载包）——WP4。

导出包只能由 ``patent_figure.export`` 写（``tests/test_bypass_guard.py`` 钉住）；部署工具链里需要**读**
下载回来的包（列成员、读文本成员做保密审计、核对必有文件），统一走这里，且只以只读模式打开。
纯标准库。
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Iterator, List, Tuple


def members(path) -> List[Tuple[str, int]]:
    """``[(成员名, 解压后字节数)]``；目录项略去。损坏的包抛 ``zipfile.BadZipFile``。"""
    with zipfile.ZipFile(str(Path(path))) as zf:
        bad = zf.testzip()
        if bad is not None:
            raise zipfile.BadZipFile("成员 CRC 校验失败：%s" % bad)
        return [(i.filename, i.file_size) for i in zf.infolist() if not i.is_dir()]


def iter_contents(path, max_bytes: int = 64 * 1024 * 1024) -> Iterator[Tuple[str, bytes]]:
    """逐个成员读出内容（单成员超过 ``max_bytes`` 时截断到该长度）。"""
    with zipfile.ZipFile(str(Path(path))) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            with zf.open(info) as fh:
                yield info.filename, fh.read(max_bytes)

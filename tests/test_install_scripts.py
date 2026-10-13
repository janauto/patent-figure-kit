"""安装脚本的静态约束（WP4b）：macOS 自带 /bin/bash 3.2 在 UTF-8 区域设置下的变量名吞字问题。

现象（WP5 从零部署发现）：``say "Python：$PY（$PY_VER）"`` 在 macOS 的 /bin/bash 3.2 + ``LANG=zh_CN.UTF-8``
（终端默认就是）下报 ``PY�: unbound variable`` 退出 1。bash 3.2 按单字节判断变量名字符，macOS libc 把 0x80–0xFF
当 Latin-1 字母，于是全角标点 / 汉字的首字节被并进变量名；脚本开了 ``set -u``，直接退出。

注意：这是**展开期**错误，``bash -n`` 语法检查查不出来（修复前的 install.sh 过 ``-n``）。这里两道检查：

1. ``/bin/bash --norc -n`` + ``LANG=zh_CN.UTF-8`` 做语法检查（兜住其他解析问题）；
2. 正则断言 ``$IDENT`` 后面不直接跟非 ASCII 字节——这一条才是这个 bug 的门禁，一律写成 ``${IDENT}``。

PowerShell（安装并启动.ps1）没有按字节吞字的问题：变量名只吸收 Unicode 字母 / 数字，全角标点会正常截断；
但它把弯引号 ‘ ’ ‚ ‛ “ ” „ 当成引号定界符，字符串里出现会把字符串提前截断。两条都在这里静态检查。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]

#: 安装脚本不进 wheel：只在源码检出里跑。
pytestmark = pytest.mark.skipif(not (_ROOT / "install.sh").is_file(), reason="只在源码检出里检查安装脚本")

#: 由 bash 执行的脚本（双击入口 .command 也是 bash）。
BASH_SCRIPTS = sorted(
    {p for p in [_ROOT / "install.sh", *_ROOT.glob("*.command"), *_ROOT.glob("*.sh"),
                 *(_ROOT / "scripts").rglob("*.sh")] if p.is_file()},
    key=lambda p: p.name)
#: 内嵌 shell 的文件：CI 工作流的 run: 块、Dockerfile 的 RUN。不做语法检查，只查同类写法。
EMBEDDED_SHELL = sorted([*(_ROOT / ".github" / "workflows").glob("*.yml"), _ROOT / "Dockerfile"],
                        key=lambda p: p.name)
PS1_SCRIPTS = sorted(_ROOT.glob("*.ps1"), key=lambda p: p.name)

#: ``$IDENT`` 紧跟一个非 ASCII 字节（任务书给定的模式；按字节匹配，与 bash 3.2 的行为一致）。
BARE_VAR_THEN_NON_ASCII = re.compile(rb"\$[A-Za-z_][A-Za-z0-9_]*[^\x00-\x7F]")

UTF8_ENV = {"LANG": "zh_CN.UTF-8", "LC_ALL": "zh_CN.UTF-8"}
SYSTEM_BASH = "/bin/bash"


def _rel(p: Path) -> str:
    return p.relative_to(_ROOT).as_posix()


def bare_var_hits(raw: bytes) -> list:
    """返回 ``[(行号, 行文本)]``：``$IDENT`` 后直接跟非 ASCII 字符的行。"""
    hits = []
    for no, line in enumerate(raw.split(b"\n"), 1):
        if BARE_VAR_THEN_NON_ASCII.search(line):
            hits.append((no, line.decode("utf-8", "replace").strip()))
    return hits


def _bash_env() -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("LC_", "BASH_ENV"))}
    env.pop("ENV", None)
    env.update(UTF8_ENV)
    return env


def test_script_inventory_is_not_empty():
    names = {p.name for p in BASH_SCRIPTS}
    assert {"install.sh", "安装并启动.command"} <= names, names
    assert PS1_SCRIPTS, "没找到 安装并启动.ps1"


def test_detector_flags_the_wp5_line_and_accepts_braces():
    """门禁自身有效：WP5 复现的那一行必须被判出来，花括号写法不得误报。"""
    assert bare_var_hits('say "Python：$PY（$PY_VER）"'.encode())
    assert bare_var_hits('fail "找到 Python $NEED_VENV_PKG，但缺少"'.encode())
    assert not bare_var_hits('say "Python：${PY}（${PY_VER}）"'.encode())
    assert not bare_var_hits('say "安装目录：$STATE_DIR"'.encode())        # 变量后是 ASCII 引号：安全
    assert not bare_var_hits('echo "PFK_HOME=$RUNNER_TEMP/pfk home 测试"'.encode())


@pytest.mark.skipif(not Path(SYSTEM_BASH).is_file(), reason="没有 /bin/bash")
@pytest.mark.parametrize("script", BASH_SCRIPTS, ids=_rel)
def test_bash_syntax_under_utf8_locale(script):
    """macOS 上 /bin/bash 就是双击 .command 时用的 3.2；Linux 上是系统 bash（缺 zh_CN 区域时只警告不影响 -n）。"""
    proc = subprocess.run([SYSTEM_BASH, "--norc", "-n", str(script)], capture_output=True,
                          env=_bash_env(), timeout=30)
    err = proc.stderr.decode("utf-8", "replace")
    assert proc.returncode == 0, "%s 语法检查未通过：%s" % (_rel(script), err)
    assert "syntax error" not in err, err


@pytest.mark.parametrize("path", BASH_SCRIPTS + EMBEDDED_SHELL, ids=_rel)
def test_no_bare_variable_before_non_ascii(path):
    if not path.is_file():
        pytest.skip("不存在：%s" % _rel(path))
    hits = bare_var_hits(path.read_bytes())
    assert not hits, ("%s：变量后紧跟中文 / 全角字符，macOS /bin/bash 3.2 + UTF-8 下会把字节并进变量名"
                      "（set -u 时直接退出）。改成 ${变量名}：\n%s"
                      % (_rel(path), "\n".join("  第 %d 行：%s" % h for h in hits)))


@pytest.mark.skipif(sys.platform != "darwin" or not Path(SYSTEM_BASH).is_file(),
                    reason="只在 macOS 自带 bash 上复现")
def test_braced_form_survives_macos_bash_utf8():
    """在本机 /bin/bash + UTF-8 + set -u 下实跑：花括号写法输出正确；裸写法若失败，失败原因就是吞字。"""
    braced = subprocess.run([SYSTEM_BASH, "--norc", "-u", "-c", 'X=a; printf "%s\\n" "x：${X}（y）"'],
                            capture_output=True, env=_bash_env(), timeout=30)
    assert braced.returncode == 0, braced.stderr.decode("utf-8", "replace")
    assert braced.stdout.decode("utf-8") == "x：a（y）\n"
    bare = subprocess.run([SYSTEM_BASH, "--norc", "-u", "-c", 'X=a; printf "%s\\n" "x：$X（y）"'],
                          capture_output=True, env=_bash_env(), timeout=30)
    if bare.returncode != 0:                      # bash 3.2：正是本测试要防的问题
        assert b"unbound variable" in bare.stderr, bare.stderr


def _ps1_code_lines(path: Path):
    raw = path.read_bytes()
    text = raw[3:].decode("utf-8") if raw[:3] == b"\xef\xbb\xbf" else raw.decode("utf-8")
    for no, line in enumerate(text.splitlines(), 1):
        if not line.lstrip().startswith("#"):
            yield no, line


@pytest.mark.parametrize("path", PS1_SCRIPTS, ids=_rel)
def test_powershell_variables_and_quotes(path):
    """PowerShell 变量名吸收 Unicode 字母 / 数字（汉字属于字母 Lo），弯引号是字符串定界符。"""
    var_hits, quote_hits = [], []
    for no, line in _ps1_code_lines(path):
        for m in re.finditer(r"\$[A-Za-z_][A-Za-z0-9_]*(.)", line):
            c = m.group(1)
            if ord(c) > 127 and unicodedata.category(c)[0] in ("L", "N"):
                var_hits.append("  第 %d 行：%s" % (no, line.strip()))
        if re.search("[‘-„]", line):
            quote_hits.append("  第 %d 行：%s" % (no, line.strip()))
    assert not var_hits, "%s：变量后紧跟汉字 / 字母，会被并进变量名，改成 ${变量名}：\n%s" % (
        _rel(path), "\n".join(var_hits))
    assert not quote_hits, "%s：弯引号在 PowerShell 里是引号定界符，改用「」：\n%s" % (
        _rel(path), "\n".join(quote_hits))

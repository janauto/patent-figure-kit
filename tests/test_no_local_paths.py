"""首次外发门禁（WP4b）：仓库里不得残留本机绝对路径与本机用户名。

扫描全部 git 跟踪的文本文件（含 NUL 字节或不是 UTF-8 的按二进制跳过），断言不含：

* ``/Users/<名字>``（macOS）、``/home/<名字>``（Linux）、``C:\\Users\\<名字>``（Windows，含 ``C:/Users/``
  与 JSON 里转义成双反斜杠的写法）；
* 运行本测试的机器的用户名（取自 ``getpass`` / ``$HOME``；``runner``、``root`` 这类通用账户名不查）。

允许清单只放「故意写出来的合成样例 / 检测规则」，不放真实残留：

* ``scripts/build_static_site.py``：清洗本机路径的正则本身；
* ``tests/test_deploy_tools.py``、``tests/test_e2e_contract_mock.py``：清洗逻辑的合成样例（``/Users/a``、
  ``/Users/someone``、``/home/runner`` 等）；
* 本文件自身。

另外允许 ``/home/pfk``：Dockerfile 里容器内服务账户的家目录（``useradd … --home-dir /home/pfk``），不是任何人的本机路径。
"""

from __future__ import annotations

import getpass
import os
import re
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SELF = Path(__file__).resolve().relative_to(_ROOT).as_posix()

ALLOWED_FILES = {
    "scripts/build_static_site.py",
    "tests/test_deploy_tools.py",
    "tests/test_e2e_contract_mock.py",
    _SELF,
}
#: 不是个人账户的家目录：容器服务账户（Dockerfile 建的 pfk）。
ALLOWED_HOME_NAMES = {"pfk"}
#: 通用账户名：CI / 容器里常见，出现在正文里不代表泄露了谁。
GENERIC_USERS = {"root", "runner", "runneradmin", "pfk", "user", "users", "admin", "administrator", "ubuntu",
                 "debian", "vscode", "codespace", "codespaces", "app", "build", "builder", "ci", "docker",
                 "nobody", "jenkins", "circleci", "travis", "github", "home", "test", "tester"}

_NAME = r"[A-Za-z0-9._-]+"
PATTERNS = [
    ("macOS 用户目录", re.compile(r"/Users/(%s)" % _NAME)),
    ("Linux 用户目录", re.compile(r"/home/(%s)" % _NAME)),
    ("Windows 用户目录", re.compile(r"\b[A-Za-z]:(?:\\{1,2}|/)Users(?:\\{1,2}|/)(%s)" % _NAME, re.IGNORECASE)),
]


def _tracked_files():
    try:
        proc = subprocess.run(["git", "ls-files", "-z"], cwd=str(_ROOT), capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return [p for p in proc.stdout.decode("utf-8", "surrogateescape").split("\0") if p]


_FILES = _tracked_files()
pytestmark = pytest.mark.skipif(not _FILES, reason="不在 git 检出里（如 wheel 安装）：跳过仓库内容扫描")


def _local_user_names() -> set:
    names = set()
    for get in (getpass.getuser, lambda: os.environ.get("USER", ""), lambda: os.environ.get("USERNAME", ""),
                lambda: Path.home().name):
        try:
            n = (get() or "").strip()
        except Exception:                        # getpass 在没有用户数据库的容器里会抛
            continue
        if len(n) >= 4 and n.lower() not in GENERIC_USERS:
            names.add(n)
    return names


def _read_text(rel: str):
    p = _ROOT / rel
    if not p.is_file() or p.is_symlink():
        return None
    raw = p.read_bytes()
    if b"\0" in raw:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def find_local_paths(text: str, user_names=()) -> list:
    """返回 ``[(行号, 类别, 片段)]``。"""
    hits = []
    user_res = [re.compile(r"(?<![A-Za-z0-9])%s(?![A-Za-z0-9])" % re.escape(n), re.IGNORECASE)
                for n in user_names]
    for no, line in enumerate(text.splitlines(), 1):
        for kind, rx in PATTERNS:
            for m in rx.finditer(line):
                if kind == "Linux 用户目录" and m.group(1) in ALLOWED_HOME_NAMES:
                    continue
                hits.append((no, kind, m.group(0)))
        for rx in user_res:
            for m in rx.finditer(line):
                hits.append((no, "本机用户名", m.group(0)))
    return hits


def test_detector_catches_each_form():
    assert find_local_paths('"step": "/Users/alice/dev/x.stp"')
    assert find_local_paths("cd /home/bob/work")
    assert find_local_paths(r"C:\Users\carol\AppData")
    assert find_local_paths(r'"C:\\Users\\carol\\AppData"')           # JSON 转义写法
    assert find_local_paths("C:/Users/carol/x")
    assert find_local_paths("作者 dave-local 的笔记", user_names={"dave-local"})
    assert not find_local_paths("HOME=/home/pfk")                     # 容器服务账户
    assert not find_local_paths('"step": "examples/planetary-gearbox/demo-ball-valve.stp"')
    assert not find_local_paths("dave-locally", user_names={"dave-local"})   # 边界：后接字母不算
    assert not find_local_paths("ci 里 runner 跑的", user_names=set())


def test_tracked_text_files_have_no_local_paths():
    users = _local_user_names()
    problems = []
    for rel in _FILES:
        hits = find_local_paths(rel, users)                   # 文件名本身也不得带
        if rel in ALLOWED_FILES:
            continue
        text = _read_text(rel)
        if text is not None:
            hits += find_local_paths(text, users)
        for no, kind, frag in hits:
            problems.append("  %s:%d  [%s] %s" % (rel, no, kind, frag))
    assert not problems, ("仓库里残留本机路径 / 用户名（改成仓库相对路径，或用合成名字）：\n"
                          + "\n".join(problems[:50]) + ("\n  …共 %d 处" % len(problems) if len(problems) > 50 else ""))

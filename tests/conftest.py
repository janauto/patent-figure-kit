"""整套测试：大模型通道一律隔离，并用哨兵断言「测试期间没有调用大模型客户端」（WP2b 修复轮 1）。

任何机器上跑 pytest 都不得外发大模型调用——不论本机配置了 DeepSeek 密钥、设了 ``DEEPSEEK_*`` 环境变量，
还是装了会被 ``provider: auto`` 自动探测到的本机 CodeBuddy CLI：

* 进程级（在任何测试模块导入 ``workbench_llm`` 之前设置，子进程同样继承）：``PFK_AI_PROVIDER=none``（通道
  一律关闭）、``PATENT_WORKBENCH_CONFIG`` 指向本次会话的临时目录（不读、不写本机配置）、清掉 ``DEEPSEEK_*``；
* 哨兵：``workbench_llm.OUTBOUND_HOOKS`` 装一个记录器——真实 ``chat_json``（所有 AI 功能的唯一客户端）一被
  调用就记下并抛错，早于任何网络 / 子进程 I/O；每个测试结束检查本测试期间没有新增记录，会话结束再查一遍
  （后台线程的迟到调用也算）。

需要「已接入通道」语义的测试自己替换 ``public_settings`` / ``chat_json`` / ``analyze_structure`` / ``draft_terms``
（替身不经过真实 ``chat_json``）；确需走真实 ``chat_json`` 的（如整次时限测试）显式 monkeypatch 掉哨兵与环境变量，
并替换掉网络函数。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ["PFK_AI_PROVIDER"] = "none"
os.environ["PATENT_WORKBENCH_CONFIG"] = tempfile.mkdtemp(prefix="pfk-test-llm-config-")
for _k in ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL"):
    os.environ.pop(_k, None)

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import pytest  # noqa: E402

import workbench_llm as _L  # noqa: E402

#: Every real ``chat_json`` call made during the session: ``(provider, purpose)``.
LLM_CALLS: list = []


def _sentinel(provider, purpose):
    LLM_CALLS.append((provider, purpose))
    raise _L.LLMError("测试期间禁止调用大模型客户端（哨兵）")


_L.OUTBOUND_HOOKS.append(_sentinel)


@pytest.fixture(autouse=True)
def _no_llm_client_calls():
    before = len(LLM_CALLS)
    yield
    assert len(LLM_CALLS) == before, "本测试期间调用了大模型客户端：%r" % (LLM_CALLS[before:],)


def pytest_sessionfinish(session, exitstatus):
    if LLM_CALLS:
        sys.stderr.write("\n哨兵：测试期间大模型客户端被调用 %d 次：%r\n" % (len(LLM_CALLS), LLM_CALLS))
        session.exitstatus = 1

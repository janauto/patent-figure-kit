"""WP4 部署工具链的纯逻辑测试：统一入口 pfk、静态站的路径清洗 / 资产审计 / 注释剥离、
已验证环境表的生成规则。不出图、不起服务器。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import build_static_site as B  # noqa: E402
import pfk_cli as P  # noqa: E402
import verified_envs as V  # noqa: E402


#: wheel 里不带 pyproject.toml（装进包目录后 pfk acceptance 在那里跑本文件）：读它的测试只在源码检出里跑。
_IN_CHECKOUT = (_ROOT / "pyproject.toml").is_file()
_checkout_only = pytest.mark.skipif(not _IN_CHECKOUT, reason="只在源码检出里检查打包配置")


# ----------------------------------------------------------------- pfk 入口


def test_pfk_root_resolves_from_scripts_and_resources_exist():
    assert P.ROOT == _ROOT and P.SCRIPTS == _SCRIPTS
    rep = P.resource_report()
    assert all(r["ok"] for r in rep["resources"]), [r for r in rep["resources"] if not r["ok"]]


def test_pfk_cli_version_paths_and_unknown_command():
    run = lambda *a: subprocess.run([sys.executable, str(_SCRIPTS / "pfk_cli.py")] + list(a),  # noqa: E731
                                    capture_output=True, text=True, encoding="utf-8")
    v = run("version")
    assert v.returncode == 0 and P.__version__ in v.stdout
    assert run("paths", "--check").returncode == 0
    bad = run("no-such-command")
    assert bad.returncode == 2 and "未知命令" in bad.stderr
    assert run().returncode == 2                         # 无参数：打印用法，退出码 2


@_checkout_only
def test_pfk_version_is_the_wheel_version():
    import re
    src = (_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'path = "scripts/pfk_cli.py"' in src            # hatch 从这里读版本：单一来源
    assert re.fullmatch(r"\d+\.\d+\.\d+", P.__version__)


@_checkout_only
def test_pyproject_pins_match_requirement_files():
    """pyproject 的依赖与两份 requirements 逐字一致（doctor 的 PINNED 表同源）。"""
    import re
    py = (_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    def pins(path):
        return sorted(l.strip() for l in (_ROOT / path).read_text(encoding="utf-8").splitlines()
                      if re.match(r"^[A-Za-z0-9_.-]+==", l.strip()))

    for path in ("requirements-pinned.txt", "requirements-web.txt"):
        for pin in pins(path):
            assert '"%s"' % pin in py, "%s 里的 %s 不在 pyproject.toml" % (path, pin)


def test_start_detects_busy_port_without_starting(monkeypatch, capsys):
    monkeypatch.setattr(P, "_port_free", lambda host, port: port != 8790)
    monkeypatch.setattr(P, "_is_our_workbench", lambda port, token: False)
    assert P.cmd_start(["--port", "8790"]) == 1               # 别的程序占着且没让自动换端口
    assert "被别的程序占用" in capsys.readouterr().err


# ----------------------------------------------------------------- 静态站：清洗与审计


@pytest.mark.parametrize("raw,want", [
    ("附图标记说明已写入 /private/var/folders/x/T/pfk-static-1/data/projects/p/studio/r.json",
     "附图标记说明已写入 r.json"),
    ("dev/fixtures/example-gearbox/fig1.svg", "dev/fixtures/example-gearbox/fig1.svg"),
    ("fig1.dxf，字高 7.0 mm", "fig1.dxf，字高 7.0 mm"),
    ("/home/runner/work/x/model.stp", "model.stp"),
])
def test_redact_paths(raw, want):
    assert B._redact_paths(raw) == want


def test_scrub_drops_path_fields_and_cleans_strings():
    out = B._scrub({"workdir": "/Users/a/x", "log": "写入 /Users/a/b/c.json", "n": 3,
                    "items": [{"path": "/tmp/x/y.svg", "name": "y.svg"}]})
    assert out == {"log": "写入 c.json", "n": 3, "items": [{"name": "y.svg"}]}


@pytest.mark.parametrize("text,hit", [
    ("iDock 底座", True), ("接入千问办公", True), ("/Users/someone/x", True),
    ("api_key: \"abcdefghijklmnop1234\"", True), ("sk-" + "a" * 24, True),
    ("公开演示模型 VISE-BASE", False), ("dev/fixtures/example-vise/model.glb", False),
])
def test_forbidden_scan(text, hit):
    assert bool(B._scan_text(text)) is hit


def test_strip_flagged_comments_only_touches_comments(tmp_path):
    site = tmp_path / "site"
    (site / "js").mkdir(parents=True)
    (site / "vendor").mkdir()
    (site / "home.html").write_text("<p>ok</p><!-- 不得出现「千问办公」 --><!-- 普通注释 -->",
                                    encoding="utf-8")
    (site / "js" / "brand.js").write_text("// 对外绝不出现「千问办公」\nexport const A = 1; // 尾注\n",
                                          encoding="utf-8")
    (site / "vendor" / "x.js").write_text("// 千问\n", encoding="utf-8")   # vendor 不动（审计照样会报）
    changed = B.strip_flagged_comments(site)
    assert sorted(changed) == ["home.html", "js/brand.js"]
    assert (site / "home.html").read_text(encoding="utf-8") == "<p>ok</p><!-- 普通注释 -->"
    assert (site / "js" / "brand.js").read_text(encoding="utf-8") == "export const A = 1; // 尾注\n"
    assert "千问" in (site / "vendor" / "x.js").read_text(encoding="utf-8")


def _mini_site(tmp_path, glb_names=("PGB-BASE",), extra_text=""):
    import struct
    site = tmp_path / "site"
    (site / "dev").mkdir(parents=True)
    doc = json.dumps({"asset": {"version": "2.0"}, "nodes": [{"name": n} for n in glb_names]}).encode()
    doc += b" " * (-len(doc) % 4)
    glb = b"glTF" + struct.pack("<II", 2, 20 + len(doc)) + struct.pack("<II", len(doc), 0x4E4F534A) + doc
    (site / "dev" / "m.glb").write_bytes(glb)
    import hashlib
    manifest = {"projects": [{"id": "example-" + e} for e in B.EXAMPLES],
                "assets": [{"path": "dev/m.glb", "sha256": hashlib.sha256(glb).hexdigest(),
                            "project": "example-gearbox"}], "samples": [], "note": extra_text}
    (site / "dev" / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False),
                                                encoding="utf-8")
    return site


def test_audit_passes_clean_site_and_flags_problems(tmp_path):
    assert B.audit_site(_mini_site(tmp_path))["ok"]
    rep = B.audit_site(_mini_site(tmp_path / "a", glb_names=("PGB-BASE", "CUSTOMER-HOUSING")))
    assert not rep["ok"] and any("非公开模型的节点名" in p for p in rep["problems"])
    rep = B.audit_site(_mini_site(tmp_path / "b", extra_text="见 /Users/someone/iDock/x"))
    assert not rep["ok"] and any("iDock" in p for p in rep["problems"])
    site = _mini_site(tmp_path / "c")
    (site / "stray.bin").write_bytes(b"x")
    rep = B.audit_site(site)
    assert any("stray.bin" in p and "来源不明" in p for p in rep["problems"])
    rep = B.audit_site(_mini_site(tmp_path / "d"), extra_forbidden=["PGB-BASE"])
    assert not rep["ok"]                                   # 构建环境敏感串（token / 用户名）一律拦


# ----------------------------------------------------------------- 已验证环境表


def _rec(label, failed=0, n10="通过", acceptance=True, secs=0):
    return {"label": label, "os": "Linux", "os_release": "Linux-x", "arch": "x86_64",
            "python": "3.11.9", "glibc": "2.35", "cadquery_ocp": "7.7.2", "commit": "abc",
            "date": "2026-10-13", "source": "CI", "install_method": "pip", "install_seconds": secs,
            "network": "GH", "acceptance": None if not acceptance else {
                "passed": 19 - failed, "failed": failed, "skipped": 0, "total": 19, "n10": n10,
                "failed_nodes": ["N10"] if failed else [], "skipped_nodes": [],
                "pytest": "600 passed, 1 warning"}}


def test_verified_table_only_lists_fully_passing_combos():
    docs = V.render([_rec("ok-env", secs=120), _rec("bad-env", failed=1, n10="未通过"),
                     _rec("install-only", acceptance=False, secs=200)])
    ver = docs["verified-environments.md"]
    table, _, rest = ver.partition("## 未通过")
    assert "| ok-env |" in table and "bad-env" not in table and "install-only" not in table
    assert "| bad-env |" in rest and "install-only" not in rest
    times = docs["install-times.md"]
    assert "ok-env" in times and "install-only" in times and "bad-env" not in times


def test_record_reads_install_timing(tmp_path):
    t = tmp_path / "install-timing.json"
    t.write_text('{"kind": "skip", "seconds": 0}\n{"kind": "install", "seconds": 178, '
                 '"script": "install.sh"}\n{"kind": "skip", "seconds": 0}\n', encoding="utf-8")
    out = tmp_path / "r.json"
    assert V.main(["record", "--install-timing", str(t), "--label", "mac", "--out", str(out)]) == 0
    rec = json.loads(out.read_text(encoding="utf-8"))
    assert rec["install_seconds"] == 178 and rec["install_method"] == "install.sh"
    assert rec["acceptance"] is None


# ----------------------------------------------------------------- 安装脚本静态约束


@_checkout_only                               # 安装脚本不进 wheel
def test_powershell_script_is_bom_crlf_and_python_snippets_avoid_double_quotes():
    """Windows PowerShell 5.1：无 BOM 时按系统代码页读中文；给原生程序传参会吃掉参数里的双引号。"""
    raw = (_ROOT / "安装并启动.ps1").read_bytes()
    assert raw[:3] == b"\xef\xbb\xbf"
    text = raw[3:].decode("utf-8")
    assert "\r\n" in text and "\n" not in text.replace("\r\n", "")
    snippets = [line for line in text.splitlines() if "'-c'" in line]
    assert snippets and all('"' not in line.split("'-c'", 1)[1] for line in snippets), snippets
    code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    assert "Invoke-Expression" not in code and "iex " not in code


@_checkout_only
def test_shell_scripts_quote_paths_and_never_eval():
    for name in ("install.sh", "安装并启动.command"):
        text = (_ROOT / name).read_text(encoding="utf-8")
        code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
        assert "eval " not in code and "curl " not in code, name
        assert "| sh" not in code and "| bash" not in code, name


@pytest.mark.parametrize("name", ["../important", "/abs/path", "a/b", "..", ".", "", ".hidden",
                                  "x" * 80, "a\\b"])
def test_site_name_must_be_a_single_directory(tmp_path, name):
    with pytest.raises(B.BuildError):
        B._site_dir(tmp_path, name)
    with pytest.raises(B.BuildError):
        B.build(tmp_path / "out", name, keep_data=False)
    assert not (tmp_path / "out").exists()              # 名字不合法时连输出根目录都不认领


def test_site_name_symlink_is_refused(tmp_path):
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "patent-figure-kit").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(B.BuildError):
        B._site_dir(tmp_path / "out", "patent-figure-kit")
    assert B._site_dir(tmp_path / "out", "demo-site").name == "demo-site"


def test_web_profile_requires_multipart(monkeypatch, capsys):
    """doctor 本身不查表单解析库；web profile 缺 python-multipart 必须判不通过（浏览器上传会 500）。"""
    report = {"ok": True, "checks": [{"id": c, "status": "ok", "required": False}
                                     for c in P.WEB_REQUIRED]}

    class _Done:
        returncode, stdout, stderr = 0, json.dumps(report), ""

    monkeypatch.setattr(P.subprocess, "run", lambda *a, **k: _Done())
    monkeypatch.setattr(P, "_has_multipart", lambda: False)
    assert P.cmd_doctor(["--json"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["profile_ok"] is False and "python-multipart" in out["profile_missing"]
    monkeypatch.setattr(P, "_has_multipart", lambda: True)
    assert P.cmd_doctor(["--json"]) == 0

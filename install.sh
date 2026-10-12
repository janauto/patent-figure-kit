#!/usr/bin/env bash
# patent-figure-kit 一键安装并启动（Linux；macOS 的「安装并启动.command」也调用本脚本）。
#
#   ./install.sh                 # 检测 Python → 建独立虚拟环境 → 装锁定依赖 → 自检 → 启动并打开浏览器
#   ./install.sh --no-start      # 只安装与自检（CI / 预装用）
#   ./install.sh --port 8791     # 指定端口（默认 8790；被别的程序占用时自动换）
#   ./install.sh --python /path/to/python3.11
#   ./install.sh --reinstall     # 依赖清单没变也强制重装
#   ./install.sh --data 目录     # 工作台数据目录（默认 ~/.patent-workbench）；--no-browser 不自动开浏览器
#
# 可重复运行：依赖清单与 Python 版本都没变时跳过安装，直接自检并启动；工作台已在运行时直接打开它。
# 虚拟环境放在用户目录（不放进仓库，避免 iCloud / 网盘同步上万个文件）：
#   macOS  ~/Library/Application Support/patent-figure-kit/
#   Linux  ${XDG_DATA_HOME:-~/.local/share}/patent-figure-kit/
# 可用环境变量 PFK_HOME 改位置。安装日志与计时也写在那里（install.log / install-timing.json）。
#
# 安全约定：所有路径一律加引号（支持空格与中文路径）；不 eval、不拼接命令字符串、不 curl | sh。

set -u
set -o pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
OS_NAME="$(uname -s)"
ARCH="$(uname -m)"
PORT="8790"
NO_START=0
REINSTALL=0
DATA_DIR=""
NO_BROWSER=0
PY_ARG="${PFK_PYTHON:-}"

say()  { printf '%s\n' "$*"; }
fail() {
  printf '\n【未完成】%s\n' "$1" >&2
  if [ "${2:-}" != "" ]; then printf '修复建议：%s\n' "$2" >&2; fi
  if [ "${LOG_FILE:-}" != "" ]; then printf '完整日志：%s\n' "$LOG_FILE" >&2; fi
  exit 1
}

while [ $# -gt 0 ]; do
  case "$1" in
    --no-start)  NO_START=1 ;;
    --reinstall) REINSTALL=1 ;;
    --port)      shift; PORT="${1:-}" ;;
    --python)    shift; PY_ARG="${1:-}" ;;
    --data)      shift; DATA_DIR="${1:-}" ;;
    --no-browser) NO_BROWSER=1 ;;
    -h|--help)   sed -n '2,17p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *)           fail "不认识的参数：$1" "./install.sh --help 查看用法" ;;
  esac
  shift
done
case "$PORT" in
  ''|*[!0-9]*) fail "端口必须是数字：$PORT" ;;
esac

if [ "$OS_NAME" = "Darwin" ]; then
  STATE_DIR="${PFK_HOME:-$HOME/Library/Application Support/patent-figure-kit}"
else
  STATE_DIR="${PFK_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/patent-figure-kit}"
fi
mkdir -p "$STATE_DIR" || fail "无法创建目录：$STATE_DIR" "检查磁盘空间与权限，或设置 PFK_HOME 指到可写目录"
LOG_FILE="$STATE_DIR/install.log"
exec > >(tee -a "$LOG_FILE") 2>&1
say ""
say "==== patent-figure-kit 安装并启动  $(date '+%Y-%m-%d %H:%M:%S') ===="
say "仓库：$REPO_DIR"
say "安装目录：$STATE_DIR"

[ -f "$REPO_DIR/requirements-pinned.txt" ] && [ -f "$REPO_DIR/scripts/pfk_cli.py" ] \
  || fail "没找到 requirements-pinned.txt / scripts/pfk_cli.py" "请在完整解压的仓库目录里运行本脚本"

# ---------------------------------------------------------------- 平台
case "$OS_NAME" in
  Darwin)
    case "$ARCH" in arm64|x86_64) ;; *) fail "不支持的 Mac 架构：$ARCH" ;; esac
    MACOS_VER="$(sw_vers -productVersion 2>/dev/null || echo 0)"
    MACOS_MAJOR="${MACOS_VER%%.*}"
    if [ "${MACOS_MAJOR:-0}" -lt 11 ] 2>/dev/null; then
      fail "macOS $MACOS_VER 太旧：几何内核 cadquery-ocp 需要 macOS 11 或更新" "升级系统，或在一台较新的电脑 / Linux 服务器上用 Docker"
    fi
    ;;
  Linux)
    [ "$ARCH" = "x86_64" ] || fail "不支持的 Linux 架构：$ARCH（几何内核 cadquery-ocp 7.7.2 只有 x86_64 轮子）" \
      "换一台 x86_64 机器；ARM 服务器目前不支持（见 docs/deploy/server.md）"
    ;;
  *)
    fail "不支持的系统：$OS_NAME" "Windows 请右键运行「安装并启动.ps1」"
    ;;
esac

# ---------------------------------------------------------------- Python 3.9–3.12
py_version() {  # 打印 "3.11"；不可用时返回非 0
  "$1" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null
}
py_ok() {       # 版本在 3.9–3.12 且带 venv/ensurepip
  local v
  v="$(py_version "$1")" || return 1
  case "$v" in 3.9|3.10|3.11|3.12) ;; *) return 1 ;; esac
  "$1" -c 'import venv, ensurepip' >/dev/null 2>&1 || { NEED_VENV_PKG="$v"; return 1; }
  return 0
}
is_xcode_stub() { # /usr/bin/python3 在没装命令行工具时会弹安装对话框：先探测，不去执行它
  [ "$OS_NAME" = "Darwin" ] && [ "$1" = "/usr/bin/python3" ] && ! xcode-select -p >/dev/null 2>&1
}

PY=""
NEED_VENV_PKG=""
if [ -n "$PY_ARG" ]; then
  py_ok "$PY_ARG" || fail "指定的 Python 不可用或版本不在 3.9–3.12：$PY_ARG"
  PY="$PY_ARG"
else
  for cand in python3.12 python3.11 python3.10 python3.9 python3 python; do
    path="$(command -v "$cand" 2>/dev/null || true)"
    [ -n "$path" ] || continue
    is_xcode_stub "$path" && continue
    if py_ok "$path"; then PY="$path"; break; fi
  done
fi
if [ -z "$PY" ]; then
  if [ -n "$NEED_VENV_PKG" ]; then
    fail "找到 Python $NEED_VENV_PKG，但缺少虚拟环境组件（venv / ensurepip）" \
      "Ubuntu / Debian：sudo apt install python${NEED_VENV_PKG}-venv，然后重新运行本脚本"
  fi
  fail "没找到 Python 3.9–3.12（3.13 及以上暂不支持：几何内核没有对应版本）" \
    "到 https://www.python.org/downloads/ 安装 Python 3.12，装好后重新运行本脚本"
fi
PY_VER="$(py_version "$PY")"
say "Python：$PY（$PY_VER）"

if [ "$OS_NAME" = "Linux" ]; then
  GLIBC="$("$PY" -c 'import platform; print(platform.libc_ver()[1] or "0")' 2>/dev/null || echo 0)"
  if ! "$PY" -c 'import sys; v=tuple(int(x) for x in sys.argv[1].split(".")[:2]); sys.exit(0 if v >= (2, 35) else 1)' "$GLIBC" 2>/dev/null; then
    fail "系统 glibc $GLIBC 低于 2.35：几何内核 cadquery-ocp 7.7.2 装不上" \
      "用 Ubuntu 22.04+ / Debian 12+，或改用 Docker 部署（docs/deploy/server.md）"
  fi
fi

# ---------------------------------------------------------------- 虚拟环境 + 锁定依赖
VENV="$STATE_DIR/venv-py$PY_VER"
VPY="$VENV/bin/python"
STAMP="$VENV/.pfk-install-stamp"
WANT_STAMP="$("$PY" - "$PY_VER" "$REPO_DIR/requirements-pinned.txt" "$REPO_DIR/requirements-web.txt" <<'PY'
import hashlib, sys
h = hashlib.sha256(sys.argv[1].encode())
for p in sys.argv[2:]:
    with open(p, "rb") as fh:
        h.update(fh.read())
print(h.hexdigest())
PY
)" || fail "无法计算依赖清单摘要"

if [ "$REINSTALL" = "0" ] && [ -x "$VPY" ] && [ -f "$STAMP" ] && [ "$(cat "$STAMP")" = "$WANT_STAMP" ] \
   && "$VPY" -c 'import OCP, ezdxf, fastapi, pypdfium2' >/dev/null 2>&1; then
  say "依赖已是最新（跳过安装）。"
  INSTALL_SECONDS=0
  INSTALL_KIND="skip"
else
  say "安装依赖（首次约需几分钟，取决于网络；几何内核约 150 MB）…"
  T0="$(date +%s)"
  if [ ! -x "$VPY" ] || ! "$VPY" -c 'import sys' >/dev/null 2>&1; then
    rm -rf "$VENV"
    "$PY" -m venv "$VENV" || fail "建虚拟环境失败：$VENV" "检查磁盘空间；Ubuntu / Debian 需 sudo apt install python${PY_VER}-venv"
  fi
  "$VPY" -m pip install --upgrade pip \
    || fail "升级 pip 失败（网络？）" "检查网络；国内网络可先 export PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple 再重新运行"
  "$VPY" -m pip install -r "$REPO_DIR/requirements-pinned.txt" -r "$REPO_DIR/requirements-web.txt" \
    || fail "安装依赖失败" "多为网络问题：重新运行本脚本即可续装；国内网络可先 export PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple"
  printf '%s' "$WANT_STAMP" > "$STAMP"
  INSTALL_SECONDS="$(( $(date +%s) - T0 ))"
  INSTALL_KIND="install"
  say "依赖安装完成，用时 ${INSTALL_SECONDS} 秒。"
fi

"$VPY" - "$STATE_DIR/install-timing.json" "$OS_NAME" "$ARCH" "$PY_VER" "$INSTALL_KIND" "$INSTALL_SECONDS" <<'PY'
import json, platform, sys, time
path, osn, arch, pyv, kind, secs = sys.argv[1:7]
rec = {"script": "install.sh", "os": osn, "os_release": platform.platform(), "arch": arch,
       "python": pyv, "kind": kind, "seconds": int(secs),
       "pip_index": __import__("os").environ.get("PIP_INDEX_URL", "pypi.org"),
       "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
with open(path, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
print("计时记录：%s" % path)
PY

# ---------------------------------------------------------------- 自检
say ""
say "环境自检 …"
"$VPY" "$REPO_DIR/scripts/pfk_cli.py" paths --check >/dev/null \
  || fail "仓库文件不完整" "重新下载 / 解压完整的仓库"
"$VPY" "$REPO_DIR/scripts/pfk_cli.py" doctor --profile web \
  || fail "环境自检未通过（见上面的「修复」行）" "按提示修复后重新运行本脚本；或加 --reinstall 强制重装依赖"

if [ "$NO_START" = "1" ]; then
  say ""
  say "安装完成（--no-start：不启动）。以后启动：\"$VPY\" \"$REPO_DIR/scripts/pfk_cli.py\" start"
  exit 0
fi

say ""
say "启动工作台（关闭本窗口或按 Ctrl+C 即停止）…"
START_ARGS=(start --auto-port --port "$PORT")
if [ -n "$DATA_DIR" ]; then START_ARGS+=(--data "$DATA_DIR"); fi
if [ "$NO_BROWSER" = "1" ]; then START_ARGS+=(--no-browser); fi
exec "$VPY" "$REPO_DIR/scripts/pfk_cli.py" "${START_ARGS[@]}"

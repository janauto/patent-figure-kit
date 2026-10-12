#!/usr/bin/env bash
# macOS：双击即可安装并启动 patent-figure-kit 工作台（首次几分钟，之后秒开）。
# 第一次双击若提示「无法打开，因为来自身份不明的开发者」：右键本文件 → 打开 → 再点「打开」。
# 实际工作由同目录的 install.sh 完成（与 Linux 共用一份逻辑）；参数原样透传，如 --no-start。
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
/bin/bash "$DIR/install.sh" "$@"
rc=$?
# 130 / 143 = 用户按 Ctrl+C 或关窗口停止了工作台，属正常退出
if [ $rc -ne 0 ] && [ $rc -ne 130 ] && [ $rc -ne 143 ]; then
  printf '\n安装或启动没有完成（退出码 %s）。把上面的中文提示原样发给维护者即可。\n' "$rc"
  # 双击打开的终端窗口会在脚本结束后保留；停一下，免得用户来不及看提示
  if [ -t 0 ]; then read -r -p "按回车关闭窗口…" _; fi
fi
exit $rc

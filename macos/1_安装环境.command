#!/bin/bash
# ===========================================================================
#  ragmini - macOS 步骤 1/6：安装环境
#  双击本文件即可（若提示“无法打开”，见 README_macOS.md 的处理办法）。
#  等价命令：bash "1_安装环境.command"
# ===========================================================================
cd "$(dirname "$0")/.." || exit 1

echo "============================================================"
echo " ragmini 环境安装（创建 .venv、装依赖、下模型）"
echo "============================================================"

if ! command -v python3 >/dev/null 2>&1; then
  echo "[错误] 未找到 python3。"
  echo "       请先安装 Python 3.11/3.12："
  echo "         * 官网下载： https://www.python.org/downloads/"
  echo "         * 或 Homebrew： brew install python@3.12"
  echo "       安装后重新双击本文件。"
  read -r -p "按回车退出…" _
  exit 1
fi

python3 install.py "$@"
echo
echo "[完成] 下一步：运行 2_入库PDF.command"
read -r -p "按回车退出…" _

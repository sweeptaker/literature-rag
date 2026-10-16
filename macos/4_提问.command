#!/bin/bash
# ===========================================================================
#  ragmini - macOS 步骤 4/6：提问（检索 + 生成）
#  提示：先设置 DEEPSEEK_API_KEY 才能得到生成答案；
#        未设置时只输出检索到的证据（链路依然可用）。
# ===========================================================================
cd "$(dirname "$0")/.." || exit 1

PY=".venv/bin/python"
[ -x "$PY" ] || PY="python3"

echo "============================================================"
echo " 步骤 4 - 提问"
echo "============================================================"
if [ -z "$DEEPSEEK_API_KEY" ]; then
  echo "[提示] 未设置 DEEPSEEK_API_KEY —— 将只输出检索证据，不生成答案。"
  echo "       设置方法： export DEEPSEEK_API_KEY=sk-xxxx"
  echo
fi

if [ "$#" -gt 0 ]; then
  Q="$*"
else
  read -r -p "请输入问题：" Q
fi

if [ -z "$Q" ]; then
  echo "[跳过] 问题为空"
  read -r -p "按回车退出…" _
  exit 0
fi

"$PY" run.py ask "$Q"
echo
read -r -p "按回车退出…" _

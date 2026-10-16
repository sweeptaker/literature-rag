#!/usr/bin/env python3
"""把 macos/*.command 与 windows/*.bat 设为可执行（跨平台安全的辅助脚本）。

macOS 下 .command 文件必须带可执行位才能双击运行；
若整套代码是通过 zip / U 盘 / 聊天工具传来的，可执行位常常丢失。
本脚本在 macOS/Linux 上运行即可批量修复（Windows 上无此概念，直接跳过）。

用法：  python 修复脚本权限.py
"""
from __future__ import annotations

import os
import stat
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))


def main() -> int:
    if sys.platform == "win32":
        print("Windows 下无需设置可执行位，直接双击 windows/1_install.bat 即可。")
        return 0
    changed = []
    for sub in ("macos",):
        d = os.path.join(ROOT, sub)
        if not os.path.isdir(d):
            continue
        for f in sorted(os.listdir(d)):
            if not f.endswith(".command"):
                continue
            p = os.path.join(d, f)
            mode = os.stat(p).st_mode
            os.chmod(p, mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            changed.append(os.path.relpath(p, ROOT))
    if not changed:
        print("未找到需要处理的 .command 文件。")
        return 0
    print("已设置可执行位：")
    for c in changed:
        print("  ✓", c)
    print("\n现在可以直接双击 macos/1_安装环境.command 了。")
    print("若仍被 Gatekeeper 拦截，请右键 → 打开，或执行：")
    print(f'  xattr -dr com.apple.quarantine "{os.path.join(ROOT, "macos")}"')
    return 0


if __name__ == "__main__":
    sys.exit(main())

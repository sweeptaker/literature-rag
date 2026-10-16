#!/usr/bin/env python3
"""ragmini 统一入口（跨平台）。

作用：
    * 若包内存在虚拟环境 .venv，自动切换过去运行，之后所有命令无需手工激活环境；
    * 把包根目录加入 sys.path，然后转交 ragcore.cli 处理子命令。

用法：
    python run.py doctor
    python run.py selftest
    python run.py ingest /path/to/pdfs
    python run.py embed
    python run.py ask "你的问题"
    python run.py serve
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))


def venv_python(venv_dir: str = "") -> str:
    """返回包内虚拟环境的解释器路径；不存在则返回空字符串。"""
    venv_dir = venv_dir or os.path.join(ROOT, ".venv")
    cand = (os.path.join(venv_dir, "Scripts", "python.exe") if sys.platform == "win32"
            else os.path.join(venv_dir, "bin", "python"))
    return cand if os.path.isfile(cand) else ""


def running_in(venv_dir: str) -> bool:
    """判断当前解释器是否就是该虚拟环境。

    注意：不能用 realpath(venv_python) == realpath(sys.executable) 来判断——
    venv 里的 python 是指向基础解释器的符号链接，两者 realpath 相同会误判。
    正确做法是比较 sys.prefix：在 venv 中运行时它等于 venv 目录本身。
    """
    try:
        return os.path.realpath(sys.prefix) == os.path.realpath(venv_dir)
    except OSError:
        return False


def relaunch_in_venv() -> bool:
    """如果包内有 .venv 且当前不是用它运行，则切换过去（返回 True 表示已切换/退出）。"""
    if os.environ.get("RAGMINI_NORELAUNCH") == "1":
        return False
    vp = venv_python()
    if not vp:
        return False
    if running_in(os.path.join(ROOT, ".venv")):
        return False

    env = dict(os.environ, RAGMINI_NORELAUNCH="1")
    argv = [vp, os.path.abspath(__file__)] + sys.argv[1:]
    print(f"[环境] 切换到包内虚拟环境：{vp}", flush=True)
    if sys.platform == "win32":
        import subprocess
        sys.exit(subprocess.call(argv, env=env))
    os.execve(vp, argv, env)          # 类 Unix：直接替换当前进程
    return True


def main() -> int:
    if relaunch_in_venv():
        return 0
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    try:
        from ragcore.cli import main as cli_main
    except ImportError as e:
        print(f"[错误] 无法导入 ragcore（{e}）。请确认在 ragmini 目录下运行本脚本。")
        print("       若尚未安装依赖，请先运行：python install.py")
        return 1
    return cli_main(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())

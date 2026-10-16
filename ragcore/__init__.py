"""ragmini —— 最小可迁移的文献 RAG。

链路：PDF → 章节切分/切片 → SQLite → bge-m3 向量 → 检索（意图/粗筛/细筛/重排）→ DeepSeek 生成。

本包的所有路径都相对于「包根目录」计算，不含任何硬编码的机器绝对路径，
因此整个目录可以直接复制或打包发给他人使用。

另外：入库后会自动对期刊求 JCR 分区 / 影响因子并折算成 1–6 档，参与融合打分。

版本号的唯一来源是包根目录的 `VERSION` 文件（由 代码/10_打包分发/打包ragmini.py 读取），
此处再从该文件加载，避免两处版本号不一致。
"""
import os

__version__ = "1.1.0"

# 用包根 VERSION 文件覆盖（文件缺失时保留上面的兜底值）
try:
    with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "VERSION"), encoding="utf-8") as _fh:
        _v = _fh.readline().strip()
    if _v:
        __version__ = _v
except OSError:
    pass

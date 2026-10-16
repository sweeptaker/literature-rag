"""向量化层：bge-m3 编码 → float32 BLOB 入库 → 导出检索用矩阵缓存。

要点：
* 模型加载**优先使用本地权重目录**（`模型/<短名>/`，由 install.py 下载）；
  本地不存在时回退在线名称，由 sentence-transformers 自行下载。
* 向量 L2 归一后存储，因此余弦相似度 = 点积，检索侧无需再归一。
* 增量编码：已存在的 (chunk_pk, model) 直接跳过；`--force` 全量重算。
* 每次编码完成即提交，中断后可续跑。
* 导出 `vectors.npy` + `vectors.uids.json`，检索时加载矩阵比逐行读 BLOB 快一个量级。
"""
from __future__ import annotations

import json
import os
import sqlite3
import time

_MODEL_CACHE: dict = {}      # {"embedder": (model, device)} / {"reranker": (model, device)}


# ============================================================================
# 设备与模型
# ============================================================================
def resolve_device(pref: str = "auto") -> str:
    """auto → 有 Apple MPS 用 mps，有 CUDA 用 cuda，否则 cpu。"""
    if pref and pref != "auto":
        return pref
    try:
        import torch
        if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            return "mps"
        if torch.cuda.is_available():
            return "cuda"
    except Exception:  # noqa: BLE001
        pass
    return "cpu"


def _local_or_name(cfg: dict, which: str) -> str:
    """返回本地权重目录（存在时）或在线模型名。"""
    name = cfg["models"][which]
    local = cfg["paths"].get(f"{which}_dir") or ""
    if local and os.path.isfile(os.path.join(local, "config.json")):
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
        return local
    return name


def load_embedder(cfg: dict):
    """惰性加载嵌入模型（单例）。返回 (model, device)。"""
    from .config import ensure_runtime_env
    ensure_runtime_env(cfg)                      # 必须在 import torch 之前
    if "embedder" not in _MODEL_CACHE:
        from sentence_transformers import SentenceTransformer
        target = _local_or_name(cfg, "embedding")
        dev = resolve_device(cfg["models"]["device"])
        try:
            model = SentenceTransformer(target, device=dev)
        except Exception as e:  # noqa: BLE001
            print(f"[警告] 以 device={dev} 加载嵌入模型失败（{e}），回退 cpu")
            dev, model = "cpu", SentenceTransformer(target, device="cpu")
        model.max_seq_length = int(cfg["models"]["embed_max_length"])
        if target != cfg["models"]["embedding"]:
            print(f"[模型] 嵌入模型本地加载：{target}")
        _MODEL_CACHE["embedder"] = (model, dev)
    return _MODEL_CACHE["embedder"]


def load_reranker(cfg: dict):
    """惰性加载交叉编码器重排模型。返回 (model, device)；模型不可用时返回 (None, "")。"""
    from .config import ensure_runtime_env
    ensure_runtime_env(cfg)
    if "reranker" not in _MODEL_CACHE:
        local = cfg["paths"].get("reranker_dir") or ""
        if not local:
            _MODEL_CACHE["reranker"] = (None, "")
            return _MODEL_CACHE["reranker"]
        from sentence_transformers import CrossEncoder
        dev = resolve_device(cfg["models"]["device"])
        try:
            ce = CrossEncoder(local, device=dev,
                              max_length=int(cfg["rerank"].get("max_length", 512)))
            print(f"[模型] 重排模型本地加载：{local}")
        except Exception as e:  # noqa: BLE001
            print(f"[警告] 重排模型加载失败（{e}），将跳过神经重排")
            _MODEL_CACHE["reranker"] = (None, "")
            return _MODEL_CACHE["reranker"]
        _MODEL_CACHE["reranker"] = (ce, dev)
    return _MODEL_CACHE["reranker"]


def encode(model, texts: list[str], batch: int = 16, query: bool = False):
    """编码为 float32 且 L2 归一化的矩阵（余弦 = 点积）。"""
    import numpy as np
    vecs = model.encode(texts, batch_size=batch, normalize_embeddings=True,
                        show_progress_bar=False, convert_to_numpy=True)
    return np.asarray(vecs, dtype="float32")


def encode_query(cfg: dict, text: str):
    """编码单条检索语句。"""
    model, _dev = load_embedder(cfg)
    return encode(model, [text], batch=1, query=True)[0]


# ============================================================================
# 入库
# ============================================================================
def pending_chunks(con: sqlite3.Connection, model_name: str) -> list[tuple[int, str]]:
    """返回尚未编码的 (chunk_pk, text)。"""
    return [(r[0], r[1]) for r in con.execute(
        """SELECT c.chunk_pk, c.text FROM chunks c
           WHERE c.kind='text' AND trim(c.text) <> ''
             AND NOT EXISTS (SELECT 1 FROM embeddings e
                             WHERE e.chunk_pk = c.chunk_pk AND e.model = ?)
           ORDER BY c.chunk_pk""", (model_name,))]


def build_embeddings(cfg: dict, force: bool = False, batch: int = 0,
                     device: str = "", dry_run: bool = False) -> dict:
    """把 chunks.text 编码写入 embeddings 表，并导出矩阵缓存。"""
    from . import db as DB
    from .config import ensure_runtime_env
    ensure_runtime_env(cfg)

    model_name = cfg["models"]["embedding"]
    batch = int(batch or cfg["models"]["batch_size"])
    if device:
        cfg["models"]["device"] = device

    con = DB.connect(cfg["paths"]["db"])
    DB.init_schema(con)
    orphans = DB.cleanup_orphans(con)

    if force:
        n = con.execute("DELETE FROM embeddings WHERE model = ?", (model_name,)).rowcount
        con.commit()
        print(f"[force] 已清空 model={model_name} 的旧向量 {n} 条")

    todo = pending_chunks(con, model_name)
    print(f"[向量] 待编码 {len(todo)} 条（批大小 {batch}，模型 {model_name}）")
    if orphans:
        print(f"[向量] 顺带清理孤儿向量 {orphans} 条")
    if dry_run or not todo:
        con.close()
        return {"pending": len(todo), "written": 0, "dim": 0}

    model, dev = load_embedder(cfg)
    print(f"[向量] 设备 {dev}；开始编码…")
    t0 = time.time()
    written, dim = 0, 0
    for i in range(0, len(todo), batch):
        part = todo[i:i + batch]
        vecs = encode(model, [t for _pk, t in part], batch=batch)
        dim = int(vecs.shape[1])
        for (pk, _t), v in zip(part, vecs):
            DB.put_embedding(con, pk, model_name, v)
        con.commit()                              # 逐批提交：中断可续跑
        written += len(part)
        if (i // batch) % 5 == 0 or written == len(todo):
            print(f"  {written}/{len(todo)}  {time.time()-t0:.1f}s", flush=True)

    n_cache = export_cache(con, cfg, model_name, dim)
    con.close()
    print(f"[向量] 完成：写入 {written} 条，维度 {dim}，耗时 {time.time()-t0:.1f}s")
    print(f"[向量] 矩阵缓存 {n_cache}×{dim} → {cache_path(cfg)}")
    return {"pending": len(todo), "written": written, "dim": dim,
            "elapsed_s": round(time.time() - t0, 1)}


# ============================================================================
# 矩阵缓存（检索加速）
# ============================================================================
def cache_path(cfg: dict) -> str:
    return os.path.join(cfg["paths"]["out_dir"], "vectors.npy")


def cache_uids_path(cfg: dict) -> str:
    return os.path.join(cfg["paths"]["out_dir"], "vectors.uids.json")


def export_cache(con: sqlite3.Connection, cfg: dict, model_name: str, dim: int = 0) -> int:
    """把 embeddings 表导出为 .npy 矩阵 + .uids.json（chunk_uid 顺序一一对应）。"""
    import numpy as np
    rows = con.execute(
        """SELECT e.vector, c.chunk_uid FROM embeddings e
           JOIN chunks c ON c.chunk_pk = e.chunk_pk
           WHERE e.model = ? ORDER BY e.chunk_pk""", (model_name,)).fetchall()
    if not rows:
        return 0
    dim = dim or len(rows[0][0]) // 4
    matrix = np.zeros((len(rows), dim), dtype="float32")
    uids = []
    for i, (blob, uid) in enumerate(rows):
        matrix[i] = np.frombuffer(blob, dtype="float32")
        uids.append(uid)
    np.save(cache_path(cfg), matrix)
    with open(cache_uids_path(cfg), "w", encoding="utf-8") as f:
        json.dump(uids, f)
    return len(uids)


def load_matrix_cache(cfg: dict):
    """读取矩阵缓存。返回 (matrix, uids) 或 (None, None)。"""
    import numpy as np
    p, pu = cache_path(cfg), cache_uids_path(cfg)
    if not (os.path.isfile(p) and os.path.isfile(pu)):
        return None, None
    try:
        matrix = np.load(p)
        with open(pu, encoding="utf-8") as f:
            uids = json.load(f)
        if len(uids) != matrix.shape[0]:
            return None, None
        return matrix, uids
    except Exception:  # noqa: BLE001
        return None, None

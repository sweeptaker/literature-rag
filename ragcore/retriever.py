"""检索层：查询规范化 → 意图识别 → 粗筛 → 五因子融合 → 文献去重 → 证据分级 →
神经重排 → 取 topk。

流水线顺序（与完整版项目一致，便于对照）：
    ⓪ normalize_query   错别字纠正/术语统一（仅用于检索侧）
    ① classify_intent   LLM 分类 concept/evidence/judgement → 动态召回宽度 N
    ② recall            bge-m3 余弦全库打分 → Top-N
    ③ fuse_scores       五因子加权融合（semantic/journal/recency/claim/section）
    ④ dedup_by_doc      按文献去重（一篇文献一个证据）
    ⑤ grade_evidence    正则证据分级 + 置信度
    ⑥ rerank            CrossEncoder 对 pool 条重打分并与语义分混合
    ⑦ pick_final        取 topk，同一文献至多 max_per_doc 条

每个候选都保留可溯源元数据：chunk_uid / doc_id / 期刊 / 年份 / DOI / 章节 / 页码。
"""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import time

# ============================================================================
# LLM 通用调用（意图分类与查询规范化共用）
# ============================================================================
_INTENT_SYS = (
    "你是学术检索系统的意图分类器。把用户提问归入三类之一：\n"
    "concept（概念/定义/机制解释类：问「是什么」「为什么」「如何影响机制」）；\n"
    "evidence（证据/数据类：要具体数据、案例、实验结果支持）；\n"
    "judgement（判断/结论类：问「是否」「会不会」「能否」「谁更重要」等需要下判断的问题）。\n"
    "只输出 JSON，不要输出其他任何文字。"
)

_NORM_SYS = (
    "你是学术检索的查询规范化器。请把用户提问改写为适合文献检索的规范表述，要求：\n"
    "1) 纠正明显的错别字与拼写错误（中英文都做）；\n"
    "2) 把口语化、非标准术语统一为学术规范术语，同义词择一（如「系统发生」→「系统发育」）；\n"
    "3) 保留原意与提问类型，不得改变问题的指向；\n"
    "4) 不做扩写解释、不加引号、不回答该问题；若原文无需修改，原样返回。\n"
    "只输出 JSON，不要输出其他任何文字。"
)


def _llm_json(cfg: dict, system: str, user: str, max_tokens: int = 300,
              temperature: float = 0.0) -> dict:
    """调用 OpenAI 兼容接口并解析 JSON 对象（失败抛异常，由调用方兜底）。"""
    import requests
    from .config import api_key
    key = api_key(cfg)
    if not key:
        raise RuntimeError("未配置 API Key")
    body = {
        "model": cfg["llm"].get("intent_model") or cfg["llm"]["model"],
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
        "stream": False,
    }
    r = requests.post(cfg["llm"]["base_url"].rstrip("/") + "/chat/completions",
                      headers={"Authorization": f"Bearer {key}",
                               "Content-Type": "application/json"},
                      json=body, timeout=cfg["llm"]["timeout"])
    r.raise_for_status()
    txt = r.json()["choices"][0]["message"]["content"]
    return json.loads(txt)


# ============================================================================
# ⓪ 查询规范化
# ============================================================================
def normalize_query(question: str, cfg: dict) -> dict:
    """错别字纠正 + 术语统一。只作用于检索侧；原始提问仍用于最终生成。"""
    out = {"original": question, "normalized": question, "changed": False,
           "corrections": [], "source": "off", "reason": "", "elapsed_ms": 0.0}
    Q = cfg.get("query_norm") or {}
    if not Q.get("enabled", True):
        return out
    from .config import api_key
    if not api_key(cfg):
        out["source"] = "fallback"
        out["reason"] = "未配置 API Key，跳过规范化"
        return out

    t0 = time.perf_counter()
    try:
        obj = _llm_json(cfg, _NORM_SYS,
                        f"提问：{question}\n\n"
                        '请返回 JSON：{"normalized":"规范化后的检索语句",'
                        '"changed":true/false,"corrections":[{"from":"错处","to":"改正"}],'
                        '"reason":"一句话说明"}', max_tokens=400)
        nz = str(obj.get("normalized", "") or "").strip()
        # 防改写护栏：空结果或长度超过上限即拒绝，回退原文
        cap = max(len(question) + 40, int(len(question) * float(Q.get("max_expansion", 3.0))))
        if not nz:
            out["source"] = "rejected"
            out["reason"] = "规范化结果为空"
        elif len(nz) > cap:
            out["source"] = "rejected"
            out["reason"] = f"规范化结果过长（{len(nz)} > {cap}），疑似被改写"
        else:
            out["normalized"] = nz
            out["changed"] = bool(obj.get("changed", nz != question))
            corr = obj.get("corrections") or []
            out["corrections"] = [{"from": str(c.get("from", "")), "to": str(c.get("to", ""))}
                                  for c in corr if isinstance(c, dict)][:10]
            out["reason"] = str(obj.get("reason", ""))[:200]
            out["source"] = "llm"
    except Exception as e:  # noqa: BLE001
        out["source"] = "error"
        out["reason"] = f"{type(e).__name__}: {e}"[:200]
    out["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return out


# ============================================================================
# ① 意图识别
# ============================================================================
# 关键词兜底规则（LLM 不可用或调用失败时使用）。
# 顺序即优先级：先看是否明确在要「证据/数据」，再看是否在要「判断」。
_RULES = [
    ("evidence", [r"证据", r"数据", r"实例", r"案例", r"支持", r"实验(结果|数据)",
                  r"多少", r"比例", r"百分比", r"显著性", r"evidence", r"data", r"experiment"]),
    ("judgement", [r"是否", r"会不会", r"能否", r"能不能", r"该不该", r"哪个更",
                   r"更(重要|关键|显著)", r"值得", r"结论", r"证实", r"证明",
                   r"取决于", r"归因", r"\bis\b.*\b(more|less)\b", r"\bwhether\b",
                   r"\bdoes\b.*\b(lead|drive|cause|matter)\b"]),
    ("concept", [r"是什么", r"什么是", r"定义", r"概念", r"机制", r"原理", r"如何影响",
                 r"为什么", r"哪些", r"什么(过程|机制|因素|条件|途径|方式)",
                 r"如何(影响|作用|调控)", r"支配", r"调控", r"what is", r"how does",
                 r"mechanism", r"define"]),
]


def classify_intent(question: str, cfg: dict) -> dict:
    """返回 {intent, confidence, reason, source, n_candidates}。"""
    I = cfg["intent"]
    n_map, n_def = I["n_by_intent"], I["n_default"]
    out = {"intent": "evidence", "confidence": 0.0, "reason": "", "source": "default",
           "n_candidates": int(n_def)}

    if I.get("use_llm", True):
        try:
            obj = _llm_json(cfg, _INTENT_SYS,
                            f"提问：{question}\n\n"
                            '请返回 JSON：{"intent":"concept|evidence|judgement",'
                            '"confidence":0.0~1.0,"reason":"一句话中文理由"}',
                            max_tokens=200)
            lab = str(obj.get("intent", "")).strip().lower()
            if lab in I["labels"]:
                out.update(intent=lab, confidence=float(obj.get("confidence", 0.0) or 0.0),
                           reason=str(obj.get("reason", ""))[:160], source="llm",
                           n_candidates=int(n_map.get(lab, n_def)))
                return out
        except Exception as e:  # noqa: BLE001
            out["reason"] = f"LLM 分类失败（{type(e).__name__}），改用规则"

    # 规则兜底
    for lab, pats in _RULES:
        for p in pats:
            if re.search(p, question, re.I):
                out.update(intent=lab, confidence=0.35, source="rule",
                           n_candidates=int(n_map.get(lab, n_def)),
                           reason=(out["reason"] or f"关键词命中「{p}」"))
                return out
    out["reason"] = out["reason"] or "无明确线索，按 evidence 处理"
    return out


# ============================================================================
# ② 资源加载与粗筛
# ============================================================================
def load_resources(cfg: dict) -> dict:
    """一次性载入向量矩阵与切片元数据，后续检索全部在内存中完成。

    矩阵优先读 .npy 缓存（与库内 chunk_uid 集合校验，不一致则回退读库）。
    """
    import numpy as np
    from . import db as DB
    from .embedder import load_matrix_cache, cache_path

    model_name = cfg["models"]["embedding"]
    con = DB.connect(cfg["paths"]["db"], create=False)
    try:
        dcols = {r[1] for r in con.execute("PRAGMA table_info(documents)")}
        canon_sel = "d.journal_canon" if "journal_canon" in dcols else "'' AS journal_canon"
        rows = con.execute(
            f"""SELECT c.chunk_pk, c.chunk_uid, c.doc_id, c.label, c.text,
                       c.n_tokens_est, c.page_start, c.page_end,
                       d.filename, d.journal, d.year, d.doi, d.title, {canon_sel},
                       e.vector
                FROM chunks c
                JOIN documents d ON d.doc_id = c.doc_id
                JOIN embeddings e ON e.chunk_pk = c.chunk_pk AND e.model = ?
                WHERE c.kind='text' AND trim(c.text) <> ''
                ORDER BY c.chunk_pk""", (model_name,)).fetchall()
        if not rows:
            raise RuntimeError(f"向量库为空，请先运行：python run.py embed\n"
                              f"（当前数据库：{cfg['paths']['db']}）")

        # ---- 期刊档位（1–6 档）：distinct 刊名 → journals 表记录 ----
        from . import journals as JN
        tmap = JN.tier_map(con)
        journal_map = {}
        for r in con.execute(
                f"""SELECT DISTINCT COALESCE(NULLIF(TRIM(journal),''),'') AS journal,
                           {canon_sel.replace('d.', '')} FROM documents"""):
            key = (r["journal_canon"] or r["journal"] or "")
            journal_map[key] = JN.resolve_tier(r["journal"], r["journal_canon"] or "", tmap, cfg)

        dim = len(rows[0]["vector"]) // 4
        matrix = np.zeros((len(rows), dim), dtype="float32")
        metas, pks, uids = {}, [], []
        for i, r in enumerate(rows):
            matrix[i] = np.frombuffer(r["vector"], dtype="float32")
            pks.append(r["chunk_pk"])
            uids.append(r["chunk_uid"])
            canon = r["journal_canon"] or ""
            d = journal_map.get(canon or r["journal"] or "")
            metas[r["chunk_pk"]] = {
                "chunk_pk": r["chunk_pk"], "chunk_uid": r["chunk_uid"],
                "doc_id": r["doc_id"], "filename": r["filename"], "label": r["label"],
                "text": r["text"], "n_tokens_est": r["n_tokens_est"],
                "page_start": r["page_start"], "page_end": r["page_end"],
                "journal": r["journal"], "journal_canon": canon,
                "journal_name": (d["name"] if d else (canon or r["journal"] or "")),
                "journal_tier": d["tier"] if d else None,
                "journal_tier_name": d["tier_name"] if d else "",
                "journal_jif": d["jif"] if d else None,
                "journal_quartile": d["quartile"] if d else "",
                "year": r["year"], "doi": r["doi"], "title": r["title"],
            }
    finally:
        con.close()

    # 缓存一致性检查（仅提示，不影响正确性：以库为准）
    cm, cu = load_matrix_cache(cfg)
    cache_ok = cm is not None and cm.shape == matrix.shape and set(cu) == set(uids)
    if not cache_ok and os.path.isfile(cache_path(cfg)):
        print("[提示] 向量矩阵缓存与数据库不一致（可能重建过切片），已改用数据库内容；"
              "如需加速请重跑：python run.py embed")

    return {"matrix": matrix, "pks": pks, "uids": uids,
            "pk2row": {pk: i for i, pk in enumerate(pks)},
            "metas": metas, "n_chunks": len(pks), "dim": dim, "model": model_name,
            "journal_map": journal_map, "journal_table_n": len(tmap)}


def recall(q_emb, n: int, res: dict) -> list[dict]:
    """全库余弦打分取 Top-N（向量已归一，余弦 = 点积）。"""
    import numpy as np
    sims = res["matrix"] @ q_emb.astype("float32")
    n = min(int(n), sims.shape[0])
    idx = np.argpartition(-sims, n - 1)[:n]
    idx = idx[np.argsort(-sims[idx])]
    out = []
    for r, i in enumerate(idx, 1):
        pk = res["pks"][int(i)]
        c = dict(res["metas"][pk])
        c["semantic"] = float(sims[int(i)])
        c["recall_rank"] = r
        out.append(c)
    return out


# ============================================================================
# ③ 五因子融合
# ============================================================================
_CLAIM_ASSERT = re.compile(
    r"\b(we (found|show|demonstrate|report|conclude|reveal)|"
    r"(results?|data|analys\w*) (show|indicate|suggest|reveal|demonstrate|confirm))\b", re.I)
_CLAIM_HEDGE = re.compile(r"\b(may|might|could|possibly|presumably|appears? to)\b", re.I)


def _journal_score(cfg: dict, cand: dict, res: dict | None = None) -> float:
    """期刊因子评分：1–6 档位分值（journals 表）> 手工覆盖 > 兜底分。

    档位由 ragcore/journals.py 依据 journal_metrics.yaml 计算：
        1 Nature/Science/Cell        2 三大刊子刊
        3 JCR Q1 且 JIF≥8            4 Q2 或 Q1 中段（JIF 4–8）
        5 Q3 或 JIF 1–4              6 Q4 或 JIF<1
    """
    from . import journals as JN
    name = cand.get("journal_name") or cand.get("journal")
    return JN.journal_score(name, cfg, (res or {}).get("journal_map"))


def _recency_score(cfg: dict, year) -> float:
    try:
        y = int(year)
    except (TypeError, ValueError):
        return 0.5
    base = int(cfg["fusion"].get("recency_base_year") or time.localtime().tm_year)
    hl = max(1.0, float(cfg["fusion"].get("recency_half_life_years", 12)))
    return float(math.exp(-max(0, base - y) * math.log(2) / hl))


def _claim_score(cfg: dict, text: str, label: str) -> float:
    """论点强度启发式：断言句 + 统计量加分，纯推测/背景减分（无外部标注数据源）。"""
    s = 0.45
    if _CLAIM_ASSERT.search(text):
        s += 0.30
    if any(re.search(p, text, re.I) for p in cfg["evidence"].get("stat_regex", [])):
        s += 0.15
    if _CLAIM_HEDGE.search(text) and not _CLAIM_ASSERT.search(text):
        s -= 0.15
    if label in ("Results", "Results and Discussion", "Discussion", "Conclusion"):
        s += 0.10
    return max(0.0, min(1.0, s))


def _section_score(cfg: dict, label: str | None) -> float:
    sw = cfg["fusion"]["section_weights"]
    return float(sw.get((label or "").strip().lower(), sw.get("default", 0.5)))


def fuse_scores(cands: list[dict], cfg: dict, res: dict | None = None) -> list[dict]:
    """final = Σ wᵢ·scoreᵢ（semantic / journal / recency / claim / section）。

    **被引次数因子（citation）已按要求删除**：原先它是 0.03 的占位（无数据源、恒为 0），
    去掉后权重按 0.97 归一化回 1.0，各因子相对比例不变。
    """
    W = cfg["fusion"]["weights"]
    for c in cands:
        c["section_s"] = _section_score(cfg, c.get("label"))
        c["journal_s"] = _journal_score(cfg, c, res)
        c["recency_s"] = _recency_score(cfg, c.get("year"))
        c["claim_s"] = _claim_score(cfg, c.get("text", ""), c.get("label", ""))
        c["fusion"] = (W["semantic"] * c["semantic"] + W["journal"] * c["journal_s"]
                       + W["recency"] * c["recency_s"] + W["claim"] * c["claim_s"]
                       + W["section"] * c["section_s"])
    cands.sort(key=lambda x: -x["fusion"])
    return cands


# ============================================================================
# ④ 去重 / ⑤ 证据分级
# ============================================================================
def dedup_by_doc(cands: list[dict], cfg: dict) -> list[dict]:
    """按论文去重：每篇只保留当前得分最高的一条（召回多条切片 → 一篇一个证据）。"""
    seen, out = set(), []
    for c in cands:
        key = c.get("doc_id") or c.get("filename")
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


def grade_evidence(cand: dict, cfg: dict) -> dict:
    """正则判定证据类别与置信度（就地写入 evidence_class / confidence）。"""
    pats = cfg["evidence"]["patterns"]
    text = cand.get("text", "")
    cls = "unclassified"
    for name in ("direct_support", "counterevidence", "indirect_support", "related_background"):
        if any(re.search(p, text, re.I) for p in pats.get(name, [])):
            cls = name
            break
    cand["evidence_class"] = cls
    has_stat = any(re.search(p, text, re.I) for p in cfg["evidence"].get("stat_regex", []))
    if cls == "direct_support":
        cand["confidence"] = "HIGH" if has_stat else "MEDIUM"
    elif cls == "indirect_support":
        cand["confidence"] = "MEDIUM"
    else:
        cand["confidence"] = "LOW"
    return cand


# ============================================================================
# ⑥ 神经重排
# ============================================================================
def rerank(cands: list[dict], query: str, cfg: dict) -> list[dict]:
    """CrossEncoder 对 pool 条候选重打分，并与语义分按 alpha 混合。"""
    from .embedder import load_reranker
    pool = max(1, int(cfg["rerank"]["pool"]))
    alpha = float(cfg["rerank"]["alpha"])
    sub = cands[:pool]
    if not sub:
        return cands
    model, _dev = load_reranker(cfg)
    if model is None:
        for c in cands:
            c["rerank_s"] = None
            c["final"] = c["fusion"]
        return cands
    pairs = [(query, c["text"]) for c in sub]
    scores = model.predict(pairs, batch_size=8, show_progress_bar=False)
    for c, s in zip(sub, scores):
        s = float(s)
        if not (0.0 <= s <= 1.0):                      # 未过 sigmoid 的模型输出压到 0~1
            s = 1.0 / (1.0 + math.exp(-s))
        c["rerank_s"] = round(s, 6)
        c["final"] = alpha * s + (1 - alpha) * c["fusion"]
    for c in cands[len(sub):]:
        c["rerank_s"] = None
        c["final"] = c["fusion"]
    cands.sort(key=lambda x: -x["final"])
    return cands


def pick_final(cands: list[dict], topk: int, max_per_doc: int = 1) -> list[dict]:
    """按 final 降序取 topk，同一文献至多 max_per_doc 条。"""
    per_doc: dict[str, int] = {}
    out = []
    for c in cands:
        key = c.get("doc_id") or c.get("filename")
        if per_doc.get(key, 0) >= max_per_doc:
            continue
        per_doc[key] = per_doc.get(key, 0) + 1
        out.append(c)
        if len(out) >= topk:
            break
    return out


# ============================================================================
# 编排
# ============================================================================
def _ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000, 1)


def retrieve(question: str, cfg: dict, res: dict | None = None,
             intent_override: str | None = None, n_override: int | None = None,
             topk_override: int | None = None, use_rerank: bool | None = None,
             use_dedup: bool | None = None, use_norm: bool | None = None,
             on_stage=None) -> dict:
    """执行完整检索流水线，返回可溯源结果。on_stage 为阶段回调（供流式推送）。"""
    from .embedder import encode_query
    t_all = time.perf_counter()
    stages: list[dict] = []

    def _emit(stage: dict):
        stages.append(stage)
        if on_stage:
            try:
                on_stage(stage)
            except Exception:  # noqa: BLE001
                pass

    if res is None:
        print("[检索] 首次调用，正在载入向量与元数据…")
        res = load_resources(cfg)

    # ⓪ 查询规范化（只影响检索侧）
    do_norm = (cfg.get("query_norm", {}).get("enabled", True)
               if use_norm is None else use_norm)
    t = time.perf_counter()
    nz = normalize_query(question, cfg) if do_norm else {
        "original": question, "normalized": question, "changed": False,
        "corrections": [], "source": "off", "reason": "", "elapsed_ms": 0.0}
    search_query = nz["normalized"] or question
    _emit({"name": "normalize", "count": len(nz.get("corrections") or []),
           "elapsed_ms": _ms(t), "original": nz["original"], "normalized": search_query,
           "changed": nz["changed"], "norm_source": nz["source"],
           "corrections": nz.get("corrections") or []})

    # ① 意图 → N
    t = time.perf_counter()
    if intent_override:
        n = int(n_override or cfg["intent"]["n_by_intent"].get(
            intent_override, cfg["intent"]["n_default"]))
        intent = {"intent": intent_override, "confidence": 1.0, "reason": "手动指定",
                  "source": "override", "n_candidates": n}
    else:
        intent = classify_intent(search_query, cfg)
        if n_override:
            intent["n_candidates"] = int(n_override)
    _emit({"name": "intent", "count": intent["n_candidates"], "elapsed_ms": _ms(t)})

    # ② 粗筛
    t = time.perf_counter()
    q_emb = encode_query(cfg, search_query)
    N = int(intent["n_candidates"])
    cands = recall(q_emb, N, res)
    recall_pks = [c["chunk_pk"] for c in cands]
    _emit({"name": "recall", "count": len(cands), "elapsed_ms": _ms(t)})

    # ③ 融合
    t = time.perf_counter()
    cands = fuse_scores(cands, cfg, res)
    _emit({"name": "fusion", "count": len(cands), "elapsed_ms": _ms(t)})

    # ④ 去重（可在重排前或后）
    do_dedup = cfg["dedup"].get("enabled", True) if use_dedup is None else use_dedup
    if do_dedup and cfg["dedup"].get("before_rerank", True):
        t = time.perf_counter()
        cands = dedup_by_doc(cands, cfg)
        _emit({"name": "dedup", "count": len(cands), "elapsed_ms": _ms(t)})

    # ⑤ 证据分级
    t = time.perf_counter()
    for c in cands:
        grade_evidence(c, cfg)
    _emit({"name": "grade", "count": len(cands), "elapsed_ms": _ms(t)})

    # ⑥ 神经重排
    do_rerank = cfg["rerank"]["enabled"] if use_rerank is None else use_rerank
    if do_rerank and not cfg["paths"].get("reranker_dir"):
        print("[提示] 未找到本地重排模型，跳过神经重排（可运行 install.py 下载）")
        do_rerank = False
    if do_rerank:
        t = time.perf_counter()
        cands = rerank(cands, search_query, cfg)
        _emit({"name": "rerank", "count": min(int(cfg["rerank"]["pool"]), len(cands)),
               "elapsed_ms": _ms(t)})
    else:
        for c in cands:
            c["rerank_s"] = None
            c["final"] = c["fusion"]
        _emit({"name": "rerank", "count": 0, "elapsed_ms": 0.0})

    # ⑦ 最终选取
    t = time.perf_counter()
    topk = int(topk_override or cfg["output"]["topk"])
    if do_dedup and not cfg["dedup"].get("before_rerank", True):
        cands = dedup_by_doc(cands, cfg)
    final = pick_final(cands, topk, int(cfg["dedup"].get("max_per_doc", 1)))
    for i, c in enumerate(final):
        c["rank"] = i + 1
    _emit({"name": "select", "count": len(final), "elapsed_ms": _ms(t)})

    return {
        "question": question, "search_query": search_query, "normalize": nz,
        "intent": intent["intent"], "intent_confidence": intent["confidence"],
        "intent_reason": intent["reason"], "intent_source": intent["source"],
        "n_recall": N, "corpus_chunks": res["n_chunks"],
        "stages": stages, "candidates": final, "recall_pks": recall_pks,
        "trace": cands, "elapsed_ms": _ms(t_all),
    }


# ============================================================================
# BM25 对照（基于 FTS5，仅用于自检/评测基线）
# ============================================================================
def fts_search(cfg: dict, query: str, topk: int = 10) -> list[dict]:
    """用 SQLite FTS5 的 bm25() 做关键词检索，作为向量检索的对照基线。"""
    from . import db as DB
    con = DB.connect(cfg["paths"]["db"], create=False)
    try:
        terms = [t for t in re.findall(r"[\w\u4e00-\u9fff]{2,}", query)][:8]
        if not terms:
            return []
        match = " OR ".join(f'"{t}"' for t in terms)
        rows = con.execute(
            """SELECT c.chunk_uid, d.filename, c.label, bm25(chunks_fts) AS score
               FROM chunks_fts
               JOIN chunks c ON c.chunk_pk = chunks_fts.rowid
               JOIN documents d ON d.doc_id = c.doc_id
               WHERE chunks_fts MATCH ? ORDER BY score LIMIT ?""", (match, topk)).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []
    finally:
        con.close()

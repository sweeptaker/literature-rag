"""期刊分级与指标富集（1–6 档），供入库后调用与检索时打分。

用户要求（本模块实现的三件事）
------------------------------
1. **每次生成数据库之后**，都对期刊求 JCR 分区与影响因子 —— 因此 `enrich` 是
   `ingest` 之后的标准工序（重建库后重跑一次即可）。
2. **优先使用搜索到的结果**：先查包内 `journal_metrics.yaml`（人工复核的检索结果表），
   表里没有的期刊才调用 DeepSeek 兜底，并在 `source` 字段标为 `ai`。
3. **按分级评价期刊**（1 档最高）：
       1 档 Nature / Science / Cell
       2 档 Nature 系列、Science 系列（正刊外）、Cell Press 系列、npj 系列
       3 档 JCR Q1 且 JIF ≥ 8（领域顶刊）
       4 档 JCR Q2，或 Q1 但 4 ≤ JIF < 8
       5 档 JCR Q3，或 1 ≤ JIF < 4
       6 档 JCR Q4，或 JIF < 1
   档位由**代码确定性计算**，AI 只负责提供 jif / quartile —— 避免模型自由裁量。

产物
----
* `数据库/rag.sqlite` 的 `journals` 表（档位、JIF、分区、来源、依据、文献数）
* `数据库/rag.sqlite` 的 `documents.journal_canon`（规范刊名）
* `journal_index.tsv`（刊名→别名/缩写/ISSN 索引，供 PDF 抽取阶段用；
  该阶段只需 pymupdf，故用 TSV 而非 yaml，避免额外依赖）
* 报告 CSV 落在 `中间文件/journal_metrics_report.csv` 与 `journal_todo.csv`
"""
from __future__ import annotations

import csv
import difflib
import json
import os
import re
import time

from .config import PKG_ROOT

# ============================================================================
# 工具
# ============================================================================
def norm_key(s):
    """归一化键：小写 + 只留字母数字（用于刊名比对与 journals 主键）。"""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def norm_spaced(s):
    """归一化（保留词间空格），用于档位正则匹配。"""
    t = re.sub(r"[^a-z0-9\s]", " ", (s or "").lower())
    return re.sub(r"\s+", " ", t).strip()


def parse_quartile(q):
    """'Q1' / 'q1' / 1 → 1；无法解析返回 None。"""
    if q is None:
        return None
    m = re.search(r"([1-4])", str(q))
    return int(m.group(1)) if m else None


def compute_tier(name, jif, quartile, tiering, explicit=None):
    """按用户口径计算档位（确定性）。返回 (tier, tier_name, tier_score, 依据说明)。"""
    scores = {int(k): v for k, v in (tiering.get("scores") or {}).items()
              if str(k).isdigit()}
    unk = float((tiering.get("scores") or {}).get("unknown", 0.45))
    names = {int(k): v for k, v in (tiering.get("names") or {}).items() if str(k).isdigit()}

    def pack(t, why):
        if t is None:
            return None, (tiering.get("names") or {}).get("unknown", "未知"), unk, why
        return t, names.get(t, f"{t} 档"), float(scores.get(t, unk)), why

    if explicit:
        t = int(explicit)
        return pack(t, f"表中显式指定为 {t} 档")

    flagship = {s.strip().lower() for s in (tiering.get("flagship") or [])}
    if (name or "").strip().lower() in flagship:
        return pack(1, "旗舰顶刊白名单")

    spaced = norm_spaced(name)
    for p in (tiering.get("subjournal_patterns") or []):
        if re.search(p, spaced):
            return pack(2, f"命中三大刊子刊规则 {p}")

    q = parse_quartile(quartile)
    try:
        jf = float(jif) if jif not in (None, "") else None
    except (TypeError, ValueError):
        jf = None
    bands = tiering.get("jif_bands") or {}
    t3 = float(bands.get("tier3_min", 8.0))
    t4 = float(bands.get("tier4_min", 4.0))
    t5 = float(bands.get("tier5_min", 1.0))

    if q:
        if q == 1:
            if jf is not None and jf >= t3:
                return pack(3, f"JCR Q1 且 JIF {jf} ≥ {t3}")
            return pack(4, (f"JCR Q1 但 JIF {jf} < {t3}（Q1 中段）" if jf is not None
                            else "JCR Q1（无 JIF，按 Q1 中段处理）"))
        return pack({2: 4, 3: 5, 4: 6}[q], f"JCR Q{q}")
    if jf is not None:
        if jf >= t3:
            return pack(3, f"无分区信息，按 JIF {jf} ≥ {t3}")
        if jf >= t4:
            return pack(4, f"无分区信息，按 JIF {jf} ∈ [{t4},{t3})")
        if jf >= t5:
            return pack(5, f"无分区信息，按 JIF {jf} ∈ [{t5},{t4})")
        return pack(6, f"无分区信息，按 JIF {jf} < {t5}")
    return pack(None, "既无 JIF 也无 JCR 分区（表中留空且 AI 未给出可核依据）")


def find_entry(name, entries):
    """在指标表里查找 name 对应的条目，返回 (entry, 命中方式) 或 (None, None)。

    匹配方式（实测教训：`Ecology` 是 `Microbial Ecology` / `Applied Soil Ecology` 的子串，
    故短名只允许完全相等）：
        exact : 归一化后完全相等（长度 ≥ 4）
        sub   : 子串包含（键长 ≥ 9）
        fuzzy : 相似度 ≥ 0.88（键长 ≥ 9），容忍截断/拼写噪声
    排序用**覆盖率**（较短串 / 较长串）而不是键长：文件名截断成 `Microbial Ecolo` 时，
    `Microbial Ecology`（覆盖 0.81）应胜过 `The ISME Journal` 的长别名
    `ISME Journal of Microbial Ecology`（覆盖 0.43）。
    """
    n = norm_key(name)
    if len(n) < 4:
        return None, None
    best = None
    for ent in entries:
        keys = [norm_key(ent["name"])] + [norm_key(a) for a in (ent.get("aliases") or [])]
        for k in keys:
            if len(k) < 4:
                continue
            cov = len(n) / max(len(n), len(k))
            if n == k:
                cur = (3, 1.0, "exact", ent)
            elif len(k) >= 9 and (k in n or n in k):
                cur = (2, cov, "sub", ent)
            elif len(k) >= 9 and difflib.SequenceMatcher(None, n, k).ratio() >= 0.88:
                cur = (1, cov, "fuzzy", ent)
            else:
                continue
            if best is None or cur[:2] > best[:2]:
                best = cur
    return (best[3], best[2]) if best else (None, None)


# ============================================================================
# 指标表读写
# ============================================================================
def load_metrics(path):
    """读取 journal_metrics.yaml，返回 (metrics, entries)。缺失时返回空表。"""
    import yaml
    if not path or not os.path.isfile(path):
        return {}, []
    with open(path, encoding="utf-8") as fh:
        m = yaml.safe_load(fh) or {}
    return m, list(m.get("journals") or [])


def build_table(metrics):
    """把表内条目算好档位，返回 {归一化键: 记录}。"""
    tiering = metrics.get("tiering") or {}
    table = {}
    for ent in (metrics.get("journals") or []):
        tier, tname, tscore, why = compute_tier(
            ent["name"], ent.get("jif"), ent.get("quartile"), tiering, ent.get("tier"))
        issns = [str(i).strip().upper() for i in (ent.get("issn") or [])]
        table[norm_key(ent["name"])] = {
            "name": ent["name"], "aliases": ent.get("aliases") or [],
            "issn": "|".join(issns), "issns": issns,
            "jif": ent.get("jif") or None, "quartile": ent.get("quartile") or None,
            "category": ent.get("category") or "",
            "tier": tier, "tier_name": tname, "tier_score": tscore,
            "source": ent.get("source") or "manual",
            "evidence": (ent.get("evidence") or "") + (f"（档位依据：{why}）" if why else ""),
            "n_docs": 0, "raw_names": [],
        }
    return table


def export_index(table, path):
    """导出刊名索引 TSV（供 ragcore/pdf_meta.py 把缩写刊名映射为规范全名）。"""
    lines = ["# 由 ragcore/journals.py 依据 journal_metrics.yaml 生成",
             "# 格式：规范刊名<TAB>别名(以 | 分隔)<TAB>ISSN(以 | 分隔)",
             "# 供 ragcore/pdf_meta.py 把 PDF 里的缩写刊名映射为规范全名"]
    for key in sorted(table):
        v = table[key]
        aliases = [a for a in v["aliases"] if norm_key(a) != norm_key(v["name"])]
        lines.append(f"{v['name']}\t{'|'.join(aliases)}\t{'|'.join(v['issns'])}")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return len(table)


def load_index(path):
    """读取刊名索引（委托给 pdf_meta，保持单一口径）。"""
    from . import pdf_meta
    return pdf_meta.load_journal_index(path)


# ============================================================================
# AI 兜底
# ============================================================================
_AI_SYS = (
    "你是学术期刊计量助手。给你一个期刊名，请给出它的 JCR 影响因子与 JCR 分区。要求：\n"
    "1) 基于你确信的公开 JCR（Journal Citation Reports）数据作答；**不确定就返回 null**，"
    "绝不允许编造数字；\n"
    "2) quartile 只能取 Q1/Q2/Q3/Q4 或 null；\n"
    "3) evidence 用一句中文说明数据来源或判断依据，没有依据时留空串；\n"
    "4) 只输出一个 JSON 对象，不要任何解释文字或 markdown 代码块。"
)


def ai_lookup(name, cfg, cfg_ai=None, timeout=60):
    """调用 DeepSeek 兜底查询期刊指标。失败/无依据时返回 None。"""
    cfg_ai = cfg_ai or {}
    from .config import api_key
    key = api_key(cfg)
    if not key:
        return None
    import requests
    base = cfg["llm"]["base_url"].rstrip("/")
    body = {
        "model": cfg_ai.get("model") or cfg["llm"].get("model", "deepseek-chat"),
        "messages": [
            {"role": "system", "content": _AI_SYS},
            {"role": "user", "content":
             f"期刊：{name}\n\n请返回 JSON："
             '{"jif":数值或null,"quartile":"Q1|Q2|Q3|Q4或null",'
             '"category":"学科（可空）","evidence":"依据","confidence":0.0~1.0}'},
        ],
        "temperature": float(cfg_ai.get("temperature", 0.0)),
        "max_tokens": int(cfg_ai.get("max_tokens", 400)),
        "response_format": {"type": "json_object"},
        "stream": False,
    }
    for attempt in range(3):
        try:
            r = requests.post(base + "/chat/completions",
                              headers={"Authorization": f"Bearer {key}",
                                       "Content-Type": "application/json"},
                              json=body, timeout=timeout)
            if r.status_code in (429, 500, 502, 503, 504):
                raise RuntimeError(f"HTTP {r.status_code}")
            r.raise_for_status()
            obj = json.loads(r.json()["choices"][0]["message"]["content"])
            jif = obj.get("jif")
            try:
                jif = float(jif) if jif not in (None, "", "null") else None
            except (TypeError, ValueError):
                jif = None
            q = obj.get("quartile")
            q = None if str(q).strip().lower() in ("null", "none", "") else str(q).strip().upper()
            if q and not re.match(r"^Q[1-4]$", q):
                q = None
            ev = str(obj.get("evidence") or "").strip()
            if cfg_ai.get("require_evidence", True) and not ev:
                return None                      # 无依据 → 视为不可信
            if jif is None and q is None:
                return None
            return {"jif": jif, "quartile": q,
                    "category": str(obj.get("category") or "").strip(), "evidence": ev}
        except Exception as e:                   # noqa: BLE001
            if attempt == 2:
                print(f"      [警告] AI 查询失败：{type(e).__name__}: {e}")
                return None
            time.sleep(1.5 * (2 ** attempt))
    return None


# ============================================================================
# 主流程
# ============================================================================
JOURNALS_DDL = """
CREATE TABLE IF NOT EXISTS journals (
    journal_key TEXT PRIMARY KEY,      -- 归一化键（小写字母数字）
    name        TEXT NOT NULL,         -- 规范刊名
    issn        TEXT,
    jif         REAL,                  -- 2025 年度影响因子
    quartile    TEXT,                  -- JCR 分区 Q1..Q4
    category    TEXT,
    tier        INTEGER,               -- 1..6；NULL 表示未知
    tier_name   TEXT,
    tier_score  REAL NOT NULL,         -- 融合分中使用的取值 0..1
    source      TEXT,                  -- search | ai | manual | unknown
    evidence    TEXT,
    n_docs      INTEGER DEFAULT 0,
    raw_names   TEXT,
    updated_at  TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_journal_tier ON journals(tier);
"""
DOC_COLUMNS = [("issn", "TEXT"), ("journal_canon", "TEXT"),
               ("journal_src", "TEXT"), ("year_src", "TEXT"), ("doi_src", "TEXT")]


def ensure_schema(con):
    """建 journals 表并给 documents 补上新增列（幂等）。"""
    con.executescript(JOURNALS_DDL)
    cols = {r[1] for r in con.execute("PRAGMA table_info(documents)")}
    for name, typ in DOC_COLUMNS:
        if name not in cols:
            con.execute(f"ALTER TABLE documents ADD COLUMN {name} {typ}")
    con.commit()


def _db_journals(con):
    """库中出现的期刊 → {原始名: {n, issns}}。"""
    out = {}
    for jn, n in con.execute(
            """SELECT COALESCE(NULLIF(TRIM(journal),''),'(未解析)') AS jn, COUNT(*) AS n
                 FROM documents GROUP BY jn ORDER BY n DESC"""):
        out.setdefault(jn, {"n": 0, "issns": set()})["n"] += n
    cols = {r[1] for r in con.execute("PRAGMA table_info(documents)")}
    if "issn" in cols:
        for jn, issn in con.execute(
                """SELECT COALESCE(NULLIF(TRIM(journal),''),'(未解析)') AS jn, issn
                     FROM documents WHERE issn IS NOT NULL AND issn <> ''"""):
            if jn in out:
                out[jn]["issns"].add(issn.strip().upper())
    return out


def rich_report(table):
    """按档位排序的报告行（供 CLI 打印与 CSV 导出）。

    最后一列为「是否在用」：只有 n_docs>0 的条目才真正参与融合打分；
    n_docs=0 的多为人工预留条目（如 Cell 正刊）或文件名与 PDF 内容不符、
    实际未落库的刊名——保留展示便于人工复核，但不影响检索结果。
    """
    rows = []
    for k in sorted(table, key=lambda x: (table[x]["tier"] is None,
                                          table[x]["tier"] or 9,
                                          -table[x]["n_docs"])):
        v = table[k]
        rows.append([v["name"], "|".join(v["raw_names"]), v["tier"] or "未知",
                     v["tier_name"], f"{float(v['tier_score']):.2f}",
                     v["jif"] or "", v["quartile"] or "", v["category"],
                     v["source"], v["n_docs"],
                     "是" if v["n_docs"] > 0 else "否",
                     v["issn"], v["evidence"]])
    return rows


def enrich(cfg, use_llm=True, dry_run=False, quiet=False):
    """富集期刊指标：写 journals 表 + 回填 documents.journal_canon + 导出索引与报告。

    返回统计字典 {"journals": 表内条数, "db_journals": 库中期刊数, "todo": 缺指标数}。
    """
    metrics_path = cfg["paths"]["journal_metrics"]
    index_path = cfg["paths"]["journal_index"]
    db_path = cfg["paths"]["db"]
    out_dir = cfg["paths"]["out_dir"]
    metrics = {}
    entries = []
    try:
        metrics, entries = load_metrics(metrics_path)
    except Exception as e:                        # noqa: BLE001
        print(f"[警告] 读取 {metrics_path} 失败（{type(e).__name__}: {e}），改用内置缺省规则")
    tiering = metrics.get("tiering") or {}
    ai_cfg = (metrics.get("sources") or {}).get("ai_fallback") or {}

    table = build_table(metrics)
    if dry_run:
        print(f"[dry-run] 指标表 {len(entries)} 条 → 分级 {len(table)} 条；不写库")
        return {"journals": len(table), "db_journals": 0, "todo": 0}

    n_idx = export_index(table, index_path)
    if not quiet:
        dist = {}
        for v in table.values():
            dist[v["tier"]] = dist.get(v["tier"], 0) + 1
        order = sorted(dist, key=lambda x: (x is None, x or 0))
        print(f"[期刊] 表内 {len(entries)} 条，已分级 {len(table)} 条；"
              f"档位分布 " + "、".join(f"{k or '未知'}档×{dist[k]}" for k in order))
        # 索引固定落在包根（与 DEFAULTS["paths"]["journal_index"] 的口径一致），
        # 因此显示路径时也用 PKG_ROOT 作基准，避免 RAGMINI_ROOT 被覆盖到别处时
        # 打出 ../../../ 这类看起来像「写到包外」的相对路径。
        try:
            shown = os.path.relpath(index_path, PKG_ROOT)
        except ValueError:
            shown = index_path
        print(f"[期刊] 已导出刊名索引 {shown}（{n_idx} 行）")

    from . import db as DB
    con = DB.connect(db_path)
    ensure_schema(con)
    found = _db_journals(con)

    resolved, todo = {}, []
    ai_filled = set()          # 已尝试过 AI 补全的期刊键（避免同一刊重复调用）
    for raw, info in sorted(found.items(), key=lambda kv: -kv[1]["n"]):
        key, how = None, ""
        hit_issn = None
        for iss in info["issns"]:
            for k, v in table.items():
                if iss in v["issns"]:
                    hit_issn = k
                    break
            if hit_issn:
                break
        if hit_issn:
            key, how = hit_issn, "issn"
        else:
            ent, match_how = find_entry(raw, entries) if entries else (None, None)
            if ent:
                key, how = norm_key(ent["name"]), f"name:{match_how}"
        # 命中了表但表内指标留空（source: todo）→ 交 AI 补全
        if key is not None:
            ent_out = table[key]
            if (not ent_out.get("jif") and not ent_out.get("quartile")
                    and use_llm and ai_cfg.get("enabled", True)
                    and key not in ai_filled):
                ai_filled.add(key)
                if not quiet:
                    print(f"      · 表内指标留空，调用 AI 补全：{ent_out['name']}")
                ai = ai_lookup(ent_out["name"], cfg, ai_cfg)
                if ai:
                    tier, tname, tscore, why = compute_tier(
                        ent_out["name"], ai["jif"], ai["quartile"], tiering)
                    ent_out.update(
                        jif=ai["jif"], quartile=ai["quartile"],
                        category=ai.get("category") or ent_out.get("category", ""),
                        tier=tier, tier_name=tname, tier_score=tscore,
                        source=ai_cfg.get("mark_source", "ai"),
                        evidence=(ai.get("evidence") or "") + f"（档位依据：{why}）")
        if key is None:
            rec = None
            if use_llm and ai_cfg.get("enabled", True):
                if not quiet:
                    print(f"      · 表中无此刊，调用 AI：{raw}")
                ai = ai_lookup(raw, cfg, ai_cfg)
                if ai:
                    tier, tname, tscore, why = compute_tier(
                        raw, ai["jif"], ai["quartile"], tiering)
                    rec = {"name": raw, "aliases": [], "issn": "", "issns": [],
                           "jif": ai["jif"], "quartile": ai["quartile"],
                           "category": ai.get("category") or "",
                           "tier": tier, "tier_name": tname, "tier_score": tscore,
                           "source": ai_cfg.get("mark_source", "ai"),
                           "evidence": (ai.get("evidence") or "") + f"（档位依据：{why}）",
                           "n_docs": 0, "raw_names": []}
            if rec is None:
                tier, tname, tscore, why = compute_tier(raw, None, None, tiering)
                rec = {"name": raw, "aliases": [], "issn": "", "issns": [],
                       "jif": None, "quartile": None, "category": "",
                       "tier": tier, "tier_name": tname, "tier_score": tscore,
                       "source": "unknown",
                       "evidence": f"未检索到指标，AI 亦未给出可核依据（{why}）",
                       "n_docs": 0, "raw_names": []}
            key = norm_key(raw)
            table[key] = rec
        resolved[raw] = key
        ent_out = table[key]
        ent_out["n_docs"] = ent_out.get("n_docs", 0) + info["n"]
        if raw != ent_out["name"] and raw not in ent_out["raw_names"]:
            ent_out["raw_names"].append(raw)
        if not ent_out.get("jif") and not ent_out.get("quartile"):
            todo.append((raw, ent_out["name"], ent_out["source"]))
        if not quiet:
            print(f"      {raw[:34]:<36} → {ent_out['name'][:40]:<42} "
                  f"{ent_out['tier'] or '-'} 档  JIF={ent_out.get('jif') or '—':<6} "
                  f"{ent_out.get('quartile') or '—':<3} [{ent_out['source']}/{how or 'ai'}]")

    con.execute("DELETE FROM journals")
    for k, v in table.items():
        con.execute(
            """INSERT OR REPLACE INTO journals
               (journal_key, name, issn, jif, quartile, category, tier, tier_name,
                tier_score, source, evidence, n_docs, raw_names, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, datetime('now'))""",
            (k, v["name"], v.get("issn") or "", v.get("jif"), v.get("quartile"),
             v.get("category") or "", v.get("tier"), v.get("tier_name"),
             float(v.get("tier_score", 0.45)), v.get("source"), v.get("evidence") or "",
             v.get("n_docs", 0), "|".join(v.get("raw_names") or [])))
    n_upd = 0
    for raw, key in resolved.items():
        cur = con.execute(
            """UPDATE documents SET journal_canon = ?
                WHERE COALESCE(NULLIF(TRIM(journal),''),'(未解析)') = ?""",
            (table[key]["name"], raw))
        n_upd += cur.rowcount or 0
    con.commit()

    os.makedirs(out_dir, exist_ok=True)
    report = os.path.join(out_dir, "journal_metrics_report.csv")
    todo_path = os.path.join(out_dir, "journal_todo.csv")
    with open(report, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["规范刊名", "库中原始名", "档位", "档位说明", "档位分值", "JIF(2025)",
                    "JCR分区", "学科", "来源", "文献数", "是否在用", "ISSN", "依据"])
        w.writerows(rich_report(table))
    with open(todo_path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["库中刊名", "规范刊名", "当前来源", "待补内容"])
        for raw, can, src in todo:
            w.writerow([raw, can, src, "JCR 分区 / 影响因子"])
    con.close()

    if not quiet:
        print(f"[期刊] journals 表写入 {len(table)} 条；documents.journal_canon 回填 {n_upd} 行")
        print(f"[期刊] 报告 {os.path.relpath(report, cfg['paths']['root'])}"
              f"；待补 {len(todo)} 条 → {os.path.relpath(todo_path, cfg['paths']['root'])}")
        unused = [v["name"] for v in table.values() if not v["n_docs"]]
        if unused:
            # 语料很小时（如自检/试跑）未引用条目会很多，逐一列举只会刷屏，
            # 因此只在小规模时列出刊名，否则仅报数量。
            tail = ("：" + "、".join(unused)) if len(unused) <= 8 else "（详见报告 CSV）"
            print(f"[期刊] 提示：{len(unused)} 条期刊当前无文献引用（n_docs=0），"
                  f"报告中标为「否」，不影响融合打分{tail}")
    return {"journals": len(table), "db_journals": len(found), "todo": len(todo),
            "canon": n_upd}


# ============================================================================
# 检索侧：刊名 → 档位
# ============================================================================
def tier_map(con):
    """读取 journals 表 → {journal_key: 记录}。表不存在时返回 {}。"""
    import sqlite3
    try:
        rows = con.execute("SELECT journal_key, name, tier, tier_name, tier_score, "
                           "jif, quartile FROM journals").fetchall()
    except sqlite3.Error:
        return {}
    return {r["journal_key"]: {"name": r["name"], "tier": r["tier"],
                               "tier_name": r["tier_name"], "score": float(r["tier_score"]),
                               "jif": r["jif"], "quartile": r["quartile"]} for r in rows}


def resolve_tier(journal, canon, tmap, cfg):
    """把库里的刊名解析到档位记录（返回记录字典或 None）。"""
    if tmap:
        for cand in (canon, journal):
            n = norm_key(cand)
            if len(n) < 4:
                continue
            if n in tmap:
                return tmap[n]
            best = None
            for k, v in tmap.items():
                if len(k) < 9:
                    continue
                cov = len(n) / max(len(n), len(k))
                if k in n or n in k:
                    cur = (2, cov, v)
                elif cov >= 0.75 and difflib.SequenceMatcher(None, n, k).ratio() >= 0.88:
                    cur = (1, cov, v)
                else:
                    continue
                if best is None or cur[:2] > best[:2]:
                    best = cur
            if best:
                return best[2]
    ov = (cfg.get("fusion") or {}).get("journal_overrides") or {}
    n = norm_key(canon or journal)
    for k, v in ov.items():
        if n and norm_key(k) == n:
            return {"name": canon or journal, "tier": None, "tier_name": "手工覆盖",
                    "score": float(v), "jif": None, "quartile": None}
    return None


def journal_score(name, cfg, journal_map=None):
    """期刊因子评分：journals 表档位分 > 手工覆盖 > 兜底分。"""
    d = (journal_map or {}).get(name or "")
    if d:
        return float(d["score"])
    ov = (cfg.get("fusion") or {}).get("journal_overrides") or {}
    n = norm_key(name)
    for k, v in ov.items():
        if n and norm_key(k) == n:
            return float(v)
    return float((cfg.get("fusion") or {}).get("journal_default_score", 0.45))

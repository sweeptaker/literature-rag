"""命令行入口：doctor / ingest / embed / ask / stats / serve / selftest / init。

用法（在包根目录执行）：
    python run.py doctor                      环境与数据自检
    python run.py init                        生成默认 config.yaml
    python run.py ingest [PDF目录或文件...]     PDF → 切片 → SQLite
    python run.py embed                       切片 → bge-m3 向量
    python run.py enrich                      期刊 JCR 分区 / 影响因子 → 1–6 档
    python run.py ask "你的问题"                检索 + 生成
    python run.py serve                       启动网页演示（127.0.0.1:8765）
    python run.py selftest                    造 3 篇合成论文跑通全链路（自证可用）

全局开关：--root DIR 指定工作目录；--config FILE 指定配置文件。
    enrich 相关：--no-llm 只用人工表（离线/省 API）；--no-enrich 让 ingest 跳过自动富集。
"""
from __future__ import annotations

import json
import os
import sys
import time

USAGE = __doc__


# ---------------------------------------------------------------------------
# 参数解析（沿用项目约定：不引入 argparse，手判 sys.argv）
# ---------------------------------------------------------------------------
def get_opt(name: str, default=None):
    if name in sys.argv:
        i = sys.argv.index(name)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


def has_flag(name: str) -> bool:
    return name in sys.argv


def positional() -> list[str]:
    """返回位置参数（跳过所有 --opt value 对）。"""
    out, skip = [], False
    for a in sys.argv[1:]:
        if skip:
            skip = False
            continue
        if a.startswith("--"):
            if "=" not in a:
                skip = True
            continue
        out.append(a)
    return out


def _load_cfg():
    from .config import load_config, ensure_runtime_env
    cfg = load_config(get_opt("--config", None), root=get_opt("--root", None))
    ensure_runtime_env(cfg)
    return cfg


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------
def cmd_doctor(cfg: dict) -> int:
    from .config import api_key, summary, package_root
    from . import db as DB

    print("=" * 72)
    print("ragmini 环境自检")
    print("=" * 72)
    print(f"Python      : {sys.version.split()[0]}  ({sys.executable})")
    print(f"平台        : {sys.platform}")
    print(f"包根目录    : {package_root()}")
    print()
    print("【路径与配置】")
    print("  " + summary(cfg).replace("\n", "\n  "))
    print()

    ok = True
    print("【依赖】")
    for mod, label in [("numpy", "numpy"), ("yaml", "pyyaml"), ("requests", "requests"),
                       ("tqdm", "tqdm"), ("torch", "torch"),
                       ("transformers", "transformers"),
                       ("sentence_transformers", "sentence-transformers"),
                       ("pymupdf", "pymupdf"), ("fitz", "pymupdf(旧名)")]:
        if mod == "fitz" and _has("pymupdf"):
            continue
        try:
            m = __import__(mod)
            print(f"  ✓ {label:22} {getattr(m, '__version__', '?')}")
        except Exception as e:  # noqa: BLE001
            if mod == "pymupdf" and _has("fitz"):
                import fitz
                print(f"  ✓ {'pymupdf(旧名)':22} {getattr(fitz, '__version__', '?')}")
                continue
            ok = False
            print(f"  ✗ {label:22} 缺失（{type(e).__name__}）→ 请运行 install.py")
    print()

    print("【模型权重】")
    for which, label in (("embedding", "嵌入模型"), ("reranker", "重排模型")):
        d = cfg["paths"].get(f"{which}_dir") or ""
        if d:
            size = sum(os.path.getsize(os.path.join(dp, f))
                       for dp, _dn, fn in os.walk(d) for f in fn) / 1e6
            print(f"  ✓ {label}：{d}  （{size:.0f} MB）")
        else:
            need = "必须，否则无法向量化" if which == "embedding" else "可选，缺失则跳过神经重排"
            print(f"  - {label}：本地未找到（{need}）")
            print(f"      期望路径：{os.path.join(cfg['paths']['model_dir'], cfg['models'][which].split('/')[-1])}")
            if which == "embedding":
                ok = False
    print()

    print("【数据库】")
    if os.path.isfile(cfg["paths"]["db"]):
        try:
            con = DB.connect(cfg["paths"]["db"], create=False)
            st = DB.stats(con)
            con.close()
            print(f"  ✓ {cfg['paths']['db']}")
            print(f"     文献 {st['documents']} 篇 / 切片 {st['chunks']} / "
                  f"已向量化 {st['chunks_with_vec']} / 期刊 {st['journal_count']} 种 / "
                  f"年份 {st['year_range']}")
            if st["chunks_with_vec"] < st["text_chunks"]:
                print(f"  ! 还有 {st['text_chunks'] - st['chunks_with_vec']} 条切片未向量化"
                      f" → 运行：python run.py embed")
            if st["chunks_with_vec"] == 0:
                ok = False
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"  ✗ 读取失败：{type(e).__name__}: {e}")
    else:
        print(f"  - 尚未建库：{cfg['paths']['db']}")
        print("      先运行：python run.py ingest <你的PDF目录>")
        ok = False
    print()

    print("【生成模型】")
    if api_key(cfg):
        print(f"  ✓ 已配置 {cfg['llm']['api_key_env']}，模型 {cfg['llm']['model']}")
    else:
        print(f"  - 未配置 {cfg['llm']['api_key_env']}：检索链路可用，生成阶段会降级为只输出证据")
        print(f"      Windows:  set {cfg['llm']['api_key_env']}=sk-xxx")
        print(f"      macOS  :  export {cfg['llm']['api_key_env']}=sk-xxx")
    print()

    print("=" * 72)
    print("结论：" + ("环境就绪，可以开始使用。" if ok else "尚有项目未就绪，请按上面提示处理后重跑。"))
    print("=" * 72)
    return 0 if ok else 1


def _has(mod: str) -> bool:
    try:
        __import__(mod)
        return True
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# init：写出默认 config.yaml（仅当不存在时）
# ---------------------------------------------------------------------------
def cmd_init(cfg: dict) -> int:
    from .config import DEFAULTS, package_root
    import yaml
    path = os.path.join(package_root(), "config.yaml")
    if os.path.isfile(path):
        print(f"[init] 配置文件已存在，保持不动：{path}")
        return 0
    header = ("# ragmini 配置文件（全部为相对路径，相对于本包根目录）\n"
              "# 删掉某个键即回退为内置默认值；也可用环境变量临时覆盖：\n"
              "#   RAGMINI_ROOT / RAGMINI_PDF_DIR / RAGMINI_DB / RAGMINI_OUT_DIR / RAGMINI_MODEL_DIR\n\n")
    with open(path, "w", encoding="utf-8") as f:
        f.write(header)
        yaml.safe_dump(DEFAULTS, f, allow_unicode=True, sort_keys=False)
    print(f"[init] 已生成配置文件：{path}")
    return 0


# ---------------------------------------------------------------------------
# ingest / embed / stats
# ---------------------------------------------------------------------------
def cmd_ingest(cfg: dict) -> int:
    from .pdf2chunks import ingest
    args = positional()[1:]                       # 去掉子命令名
    paths = args or [cfg["paths"]["pdf_dir"]]
    print(f"[入库] 输入：{', '.join(paths)}")
    rep = ingest(paths, cfg, force=has_flag("--force"), dry_run=has_flag("--dry-run"),
                 limit=int(get_opt("--limit", 0) or 0))
    print(f"[入库] 完成：入库 {rep['ingested']} 篇 / 跳过 {rep['skipped']} / 失败 {rep['failed']}；"
          f"共 {rep['sections']} 章节、{rep['chunks']} 切片")
    if rep["ingested"] and not has_flag("--dry-run"):
        # 用户要求：每次生成数据库以后都要对期刊求 JCR 分区与影响因子
        if not has_flag("--no-enrich"):
            print()
            cmd_enrich(cfg, auto=True)
        print("[入库] 下一步：python run.py embed")
    return 0


def cmd_enrich(cfg: dict, auto: bool = False) -> int:
    """期刊指标富集：写 journals 表（档位/JIF/分区/来源）+ 回填规范刊名 + 导出索引。"""
    from . import journals as JN
    r = JN.enrich(cfg, use_llm=(not has_flag("--no-llm")), dry_run=has_flag("--dry-run"))
    if not has_flag("--dry-run") and r.get("todo"):
        print(f"[期刊] 仍有 {r['todo']} 个期刊缺指标（见 中间文件/journal_todo.csv），"
              "可人工补检索后写入 journal_metrics.yaml 再重跑本命令")
    if not has_flag("--dry-run") and r.get("journals"):
        print("[期刊] 档位已写入 journals 表，检索时会自动按档位打分")
    return 0


def cmd_embed(cfg: dict) -> int:
    from .embedder import build_embeddings
    r = build_embeddings(cfg, force=has_flag("--force"),
                         batch=int(get_opt("--batch", 0) or 0),
                         device=get_opt("--device", "") or "",
                         dry_run=has_flag("--dry-run"))
    if r.get("written"):
        print("[向量] 下一步：python run.py ask \"你的问题\"")
    return 0


def cmd_stats(cfg: dict) -> int:
    from . import db as DB
    con = DB.connect(cfg["paths"]["db"], create=False)
    st = DB.stats(con)
    print(json.dumps(st, ensure_ascii=False, indent=2))
    print("\n按章节标签统计：")
    for r in con.execute("SELECT label, count(*) c FROM chunks GROUP BY label ORDER BY c DESC"):
        print(f"  {r[0]:<26} {r[1]}")
    print("\n按期刊统计（含档位/JIF）：")
    has_j = con.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='journals'"
                        ).fetchone()[0]
    if has_j:
        rows = con.execute(
            """SELECT COALESCE(d.journal_canon, NULLIF(TRIM(d.journal),''), '(未知)') j,
                      count(*) c, j2.tier, j2.jif, j2.quartile, j2.source
                 FROM documents d
                 LEFT JOIN journals j2 ON j2.name = COALESCE(d.journal_canon, d.journal)
                GROUP BY j ORDER BY (j2.tier IS NULL), j2.tier, c DESC LIMIT 20""").fetchall()
    else:
        rows = [(r[0], r[1], None, None, None, "") for r in con.execute(
            "SELECT COALESCE(journal,'(未知)') j, count(*) c FROM documents "
            "GROUP BY j ORDER BY c DESC LIMIT 20")]
    for j, c, tier, jif, q, src in rows:
        tag = (f"{tier}档 {q or ''} IF={jif or '—'} [{src or '未分级'}]" if tier
               else f"{q or ''} IF={jif or '—'} [{src or '未分级'}]")
        print(f"  {str(j)[:44]:<46} {c:>3} 篇   {tag}")
    con.close()
    return 0


# ---------------------------------------------------------------------------
# ask
# ---------------------------------------------------------------------------
def cmd_ask(cfg: dict) -> int:
    from . import retriever as R
    from .generator import generate

    args = positional()[1:]
    if not args:
        print('用法：python run.py ask "你的问题" [--topk N] [--no-rerank] [--json]')
        return 2
    question = " ".join(args)

    t0 = time.perf_counter()
    out = R.retrieve(question, cfg,
                     intent_override=get_opt("--intent", None),
                     n_override=int(get_opt("--n-recall", 0) or 0) or None,
                     topk_override=int(get_opt("--topk", 0) or 0) or None,
                     use_rerank=(False if has_flag("--no-rerank") else None),
                     use_dedup=(False if has_flag("--no-dedup") else None),
                     use_norm=(False if has_flag("--no-norm") else None))

    dump = get_opt("--dump", "")
    if dump:
        _dump_csv(out, dump)

    if has_flag("--json"):
        gen = generate(question, out["candidates"], cfg, search_query=out["search_query"])
        payload = {
            "question": question, "search_query": out["search_query"],
            "normalize": out["normalize"], "intent": out["intent"],
            "intent_confidence": out["intent_confidence"], "intent_reason": out["intent_reason"],
            "intent_source": out["intent_source"], "n_recall": out["n_recall"],
            "corpus_chunks": out["corpus_chunks"], "stages": out["stages"],
            "candidates": [{k: v for k, v in c.items() if k != "text"} for c in out["candidates"]],
            "answer": gen["answer"], "usage": gen["usage"],
            "gen_elapsed_ms": gen["elapsed_ms"],
            "total_elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 0

    # ---- 终端报告 ----
    nz = out["normalize"]
    print(f"\n问题：{question}")
    if nz["changed"]:
        print(f"[规范化] {nz['original']}  →  {out['search_query']}"
              f"（{nz['source']}，{nz['elapsed_ms']} ms）")
        for c in nz.get("corrections") or []:
            print(f"          {c['from']} ⇒ {c['to']}")
    print(f"[意图] {out['intent']}（置信度 {out['intent_confidence']:.2f}，来源 {out['intent_source']}）"
          f" → 召回宽度 N={out['n_recall']}")
    if out["intent_reason"]:
        print(f"       理由：{out['intent_reason']}")
    print(f"[语料] {out['corpus_chunks']} 切片")
    print("[阶段] " + " → ".join(f"{s['name']}({s['count']}条,{s['elapsed_ms']}ms)"
                                 for s in out["stages"]))
    print("\n=== Top 证据 ===")
    for c in out["candidates"]:
        pg = f"p{c['page_start']}-{c['page_end']}"
        rr = f" rerank={c['rerank_s']:.3f}" if c.get("rerank_s") is not None else ""
        jn = c.get("journal_name") or c.get("journal") or "?"
        tier = f"{c['journal_tier']}档" if c.get("journal_tier") else ""
        jq = c.get("journal_quartile") or ""
        jif = f"IF {c['journal_jif']}" if c.get("journal_jif") else ""
        jtag = " ".join(x for x in (tier, jq, jif) if x)
        print(f"[{c['rank']}] " + (f"{(c.get('title') or '').strip()} — "
              if (c.get('title') or '').strip() else "")
              + f"{jn}({c['year'] or '?'})"
              + (f" [{jtag}]" if jtag else "") + f" · {c['label']} · {pg}"
              f" · {c['evidence_class']}/{c['confidence']}"
              f" · final={c['final']:.4f}{rr} · sem={c['semantic']:.3f}")
        print(f"      {os.path.basename(c.get('filename') or '')} · chunk {c['chunk_uid'][:12]}")

    print("\n=== 答案 ===")
    gen = generate(question, out["candidates"], cfg, search_query=out["search_query"])
    print(gen["answer"])
    if gen.get("usage"):
        print(f"\n[用量] {gen['usage']}  生成耗时 {gen['elapsed_ms']} ms"
              f"  总耗时 {round((time.perf_counter()-t0)*1000, 1)} ms")
    return 0


def _dump_csv(out: dict, path: str) -> None:
    import csv
    rows = []
    for c in out["trace"]:
        rows.append({k: c.get(k) for k in (
            "recall_rank", "rank", "chunk_uid", "doc_id", "filename", "title", "journal", "year",
            "label", "page_start", "page_end", "semantic", "journal_s", "recency_s",
            "claim_s", "section_s", "fusion", "rerank_s", "final",
            "journal_canon", "journal_name", "journal_tier", "journal_tier_name",
            "journal_jif", "journal_quartile",
            "evidence_class", "confidence")})
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["empty"])
        w.writeheader()
        w.writerows(rows)
    print(f"[导出] 全阶段候选（{len(rows)} 条）→ {path}")


# ---------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------
def cmd_serve(cfg: dict) -> int:
    from .webapp import serve
    serve(cfg, host=get_opt("--host", ""), port=int(get_opt("--port", 0) or 0),
          preheat_model=not has_flag("--no-preheat"))
    return 0


# ---------------------------------------------------------------------------
# selftest：现场造合成论文跑通全链路（接收方无需自带 PDF 即可验证）
# ---------------------------------------------------------------------------
_FAKE = [
    {"journal": "Ecology", "year": 2019, "doi": "10.1000/demo.001",
     "title": "Soil microbial community assembly under drought stress",
     "topic": "drought soil microbial community assembly",
     "abstract": ("Soil microbial communities are key drivers of ecosystem function. "
                  "We examined how drought stress reshapes microbial community assembly "
                  "in temperate grassland soils. Using a three-year field experiment, "
                  "we found that drought increased the relative abundance of "
                  "Actinobacteria and reduced fungal diversity by 34%. "
                  "Results indicate that deterministic selection strengthens under "
                  "drought, whereas stochastic dispersal becomes less important. "
                  "These findings suggest that intensified drought will simplify soil "
                  "microbial networks and weaken carbon decomposition."),
     "intro": ("Understanding soil microbial community assembly is central to predicting "
               "ecosystem responses to climate change. Previous studies have shown that "
               "moisture is a major driver of microbial composition, yet the relative role "
               "of deterministic versus stochastic processes remains debated."),
     "methods": ("We established 24 plots in a temperate grassland and applied "
                 "rainfall exclusion shelters for three consecutive growing seasons. "
                 "Soil samples were collected at 0-10 cm depth each July. "
                 "Amplicon sequencing of the 16S rRNA gene and ITS region was performed. "
                 "Statistical analysis used the iCAMP framework and mixed linear models."),
     "results": ("Drought significantly reduced bacterial richness by 18% and fungal "
                 "richness by 34%. The relative abundance of Actinobacteria increased "
                 "significantly under drought conditions. Deterministic assembly "
                 "processes explained 62% of community variation in drought plots, "
                 "compared with 41% in control plots."),
     "discussion": ("Our results demonstrate that drought intensifies deterministic "
                    "selection in soil microbial communities. This contrasts with "
                    "earlier findings that stochastic processes dominate in drier soils. "
                    "We suggest that changes in community assembly may reduce carbon "
                    "decomposition and weaken the soil carbon sink."),
     "conclusion": ("Drought stress reorganises soil microbial community assembly toward "
                    "stronger deterministic selection, with measurable consequences for "
                    "carbon cycling. Future work should test whether these shifts persist "
                    "over longer drought periods.")},
    {"journal": "Nature Ecology & Evolution", "year": 2016, "doi": "10.1000/demo.002",
     "title": "Phylogenetic diversity stabilises grassland ecosystem productivity",
     "topic": "phylogenetic diversity ecosystem stability",
     "abstract": ("The relationship between biodiversity and ecosystem stability has been "
                  "debated for decades. We analysed a global dataset of 78 grassland "
                  "experiments to test whether phylogenetic diversity predicts "
                  "productivity stability better than species richness. "
                  "We found that phylogenetic diversity explained 46% of the variation "
                  "in temporal stability, whereas species richness explained only 21%. "
                  "The results indicate that evolutionary history provides a stronger "
                  "basis for predicting ecosystem stability."),
     "intro": ("Ecosystem stability is a central concept in ecology. Previous research has "
               "shown that species richness correlates with stability, but phylogenetic "
               "diversity has received less attention even though it captures functional "
               "differences that richness ignores."),
     "methods": ("We assembled a database of 78 grassland biodiversity experiments "
                 "spanning 12 countries. For each site we computed species richness, "
                 "phylogenetic diversity using a megaphylogeny, and temporal stability as "
                 "the inverse coefficient of variation of productivity."),
     "results": ("Phylogenetic diversity was positively and significantly associated with "
                 "temporal stability across all sites. Mixed models showed that "
                 "phylogenetic diversity explained 46% of stability variation, "
                 "against 21% for species richness."),
     "discussion": ("Phylogenetic diversity emerges as a better predictor of ecosystem "
                    "stability than species richness. This is consistent with the "
                    "insurance hypothesis, suggesting that evolutionary divergence "
                    "captures response diversity to environmental fluctuations."),
     "conclusion": ("Phylogenetic diversity should be incorporated into conservation "
                    "planning because it predicts stability better than richness alone.")},
    {"journal": "Microbiome", "year": 2021, "doi": "10.1000/demo.003",
     "title": "Antibiotic disturbance and microbiome resilience in human gut",
     "topic": "antibiotic gut microbiome resilience recovery",
     "abstract": ("Antibiotic treatment disturbs the human gut microbiome, but the "
                  "determinants of recovery remain unclear. We followed 144 patients "
                  "for 12 months after a course of broad-spectrum antibiotics. "
                  "We report that baseline diversity predicted recovery rate "
                  "(p < 0.01), and that 27% of patients had not recovered their "
                  "original composition after one year. Our findings indicate that "
                  "microbiome resilience is a measurable and heritable trait."),
     "intro": ("The gut microbiome is a complex ecosystem that is frequently disturbed by "
               "antibiotics. Previous work has characterised short-term responses, "
               "but long-term recovery patterns and their predictors are poorly known."),
     "methods": ("We recruited 144 patients prescribed broad-spectrum antibiotics and "
                 "collected stool samples at 0, 1, 3, 6 and 12 months. Shotgun "
                 "metagenomic sequencing was performed and strain-level tracking was "
                 "applied. Recovery was defined as returning within 10% of baseline "
                 "Bray-Curtis similarity."),
     "results": ("Baseline diversity predicted recovery rate significantly (p < 0.01). "
                 "27% of patients remained unrecovered at 12 months. "
                 "Recovery half-time ranged from 32 to 214 days across individuals."),
     "discussion": ("These findings demonstrate that microbiome resilience varies widely "
                    "between individuals and is predicted by baseline diversity. "
                    "Our results suggest that repeated antibiotic courses may drive "
                    "cumulative loss of taxa."),
     "conclusion": ("Microbiome recovery after antibiotics is slow and incomplete in a "
                    "substantial fraction of patients, motivating diversity-aware "
                    "antibiotic stewardship.")},
]


def _make_demo_pdfs(cfg: dict) -> list[str]:
    """用 pymupdf 现场生成 3 篇合成论文 PDF（无需外部素材）。

    注意：pymupdf 中 `doc.new_page()` 会重置已持有的 Page 句柄（`page.parent` 被置空），
    因此这里先按字符量把正文分好页、一次性建页，再逐页 `load_page()` 取句柄插入文本。
    """
    try:
        import pymupdf as fitz
    except ImportError:
        import fitz
    import textwrap

    out_dir = os.path.join(cfg["paths"]["out_dir"], "demo_pdfs")
    os.makedirs(out_dir, exist_ok=True)
    paths = []
    for p in _FAKE:
        lines = [p["title"], f"{p['journal']} ({p['year']})   doi:{p['doi']}", ""]
        for label, key in (("Abstract", "abstract"), ("Introduction", "intro"),
                           ("Methods", "methods"), ("Results", "results"),
                           ("Discussion", "discussion"), ("Conclusion", "conclusion")):
            lines.append(label)
            lines += textwrap.wrap(p[key], 92)
            lines.append("")
        lines.append("References")
        for i in range(6):
            lines.append(f"[{i+1}] Author A. et al. ({2010+i}) "
                         f"Journal of Demo Ecology 12: 1-20.")

        # 分页（每页约 2400 字符，确保 insert_textbox 不会溢出）
        pages, buf = [], ""
        for ln in lines:
            if len(buf) + len(ln) > 2400:
                pages.append(buf)
                buf = ""
            buf += ln + "\n"
        if buf.strip():
            pages.append(buf)

        doc = fitz.open()
        for _ in pages:
            doc.new_page()
        rect = fitz.Rect(56, 56, 540, 780)
        for i, txt in enumerate(pages):
            page = doc.load_page(i)          # 建页完成后再取句柄，避免句柄失效
            page.insert_textbox(rect, txt, fontsize=11, fontname="helv",
                                align=fitz.TEXT_ALIGN_LEFT)
        fp = os.path.join(out_dir, safe_filename(
            f"{p['journal']}-{p['year']}-{p['doi']}.pdf"))
        doc.save(fp)
        doc.close()
        paths.append(fp)
    return paths


def safe_filename(name: str) -> str:
    """把 DOI 等文本转成合法文件名（斜杠等 Illegal 字符替换为下划线）。

    Windows 的非法字符：\\ / : * ? " < > |
    """
    import re
    out = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", name)
    return out.strip(" .") or "untitled.pdf"


def cmd_selftest(cfg: dict) -> int:
    """端到端自检：生成合成 PDF → 入库 → 向量化 → 检索 → 断言命中。"""
    from . import db as DB
    from .embedder import build_embeddings
    from .pdf2chunks import ingest
    from . import retriever as R

    print("=" * 72)
    print("ragmini 端到端自检（使用合成论文，不依赖你的数据）")
    print("=" * 72)
    failures = []

    print("\n[1/5] 生成合成论文 …")
    pdfs = _make_demo_pdfs(cfg)
    for p in pdfs:
        print(f"      {os.path.basename(p)}")

    print("\n[2/5] 切片入库 …")
    rep = ingest(pdfs, cfg, force=True, quiet=False)
    if rep["ingested"] < 1:
        failures.append("入库失败：未解析出任何有效切片")
    else:
        print(f"      ✓ {rep['ingested']} 篇 / {rep['chunks']} 切片")

    print("\n[3/5] 向量化 …")
    r = build_embeddings(cfg, force=False)
    if not r.get("written") and not r.get("pending"):
        failures.append("向量化失败：无写入")
    else:
        print(f"      ✓ 写入 {r.get('written', 0)} 条，维度 {r.get('dim', 0)}")

    print("\n[4/5] 检索断言 …")
    import sqlite3
    con = sqlite3.connect("file:%s?mode=ro" % cfg["paths"]["db"], uri=True)
    rows = con.execute("SELECT journal, year, doi FROM documents").fetchall()
    con.close()
    if not rows:
        failures.append("库中无文献")
    res = R.load_resources(cfg)

    for p in _FAKE:
        out = R.retrieve(p["topic"], cfg, res, intent_override="concept", use_norm=False)
        got = [c["filename"] for c in out["candidates"]]
        hit_rank = next((i + 1 for i, c in enumerate(out["candidates"])
                         if str(p["doi"]).split("/")[-1] in (c.get("filename") or "")
                         or str(p["year"]) in (c.get("filename") or "")
                         and p["journal"].split()[0] in (c.get("filename") or "")), 0)
        mark = "✓" if hit_rank and hit_rank <= 3 else "✗"
        print(f"      {mark} 「{p['topic']}」→ 目标文献名次 {hit_rank or '未命中'}；"
              f"Top-1 = {got[0] if got else '—'}")
        if not hit_rank or hit_rank > 3:
            failures.append(f"检索未命中（或名次>3）：{p['topic']}")

    print("\n[5/5] 可溯源性与去重检查 …")
    out = R.retrieve(_FAKE[0]["topic"], cfg, res, intent_override="concept", use_norm=False)
    docs = [c["doc_id"] for c in out["candidates"]]
    if len(docs) != len(set(docs)):
        failures.append("最终结果存在同一文献的多条证据（去重失效）")
    else:
        print("      ✓ 最终结果一篇文献一个证据")
    miss = [c["rank"] for c in out["candidates"]
            if not all([c.get("journal"), c.get("label"), c.get("chunk_uid"),
                        c.get("page_start") is not None])]
    if miss:
        print(f"      ! 以下证据缺少量元数据（可能原 PDF 无此信息）：{miss}")
    else:
        print("      ✓ 每条证据均含期刊/章节/页码/chunk_uid")
    print(f"      ✓ 各阶段耗时：" +
          " → ".join(f"{s['name']} {s['elapsed_ms']}ms" for s in out["stages"]))

    print("\n" + "=" * 72)
    if failures:
        print("自检结果：未通过")
        for f in failures:
            print(f"  ✗ {f}")
        print("=" * 72)
        return 1
    print("自检结果：全部通过 —— 全链路（PDF→切片→SQLite→向量→检索）可用。")
    print("下一步：python run.py ingest <你的PDF目录> && python run.py embed && python run.py ask \"问题\"")
    print("=" * 72)
    return 0


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
COMMANDS = {
    "doctor": cmd_doctor, "init": cmd_init, "ingest": cmd_ingest, "embed": cmd_embed,
    "ask": cmd_ask, "stats": cmd_stats, "serve": cmd_serve, "selftest": cmd_selftest,
    "enrich": cmd_enrich,
}


def main(argv: list[str] | None = None) -> int:
    if argv is not None:
        sys.argv = [sys.argv[0]] + list(argv)
    cmd = (positional() or ["doctor"])[0]
    if cmd in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    if cmd not in COMMANDS:
        print(f"未知命令：{cmd}\n")
        print(USAGE)
        return 2
    cfg = _load_cfg()
    try:
        return COMMANDS[cmd](cfg)
    except KeyboardInterrupt:
        print("\n[中断] 用户取消")
        return 130
    except FileNotFoundError as e:
        print(f"[错误] {e}")
        return 1
    except RuntimeError as e:
        print(f"[错误] {e}")
        return 1

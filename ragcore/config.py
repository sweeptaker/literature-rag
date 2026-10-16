"""配置层：路径解析 + YAML 缺省补全 + 运行时环境注入。

设计要点（可迁移性的关键）：
1. **不含任何机器绝对路径**。所有路径都相对于「包根目录」（本文件的上级目录）计算，
   因此把整个 ragmini 目录拷到任何机器、任何位置都能直接运行。
2. 三层覆盖：内置缺省字典 < config.yaml < 环境变量。
   环境变量便于临时切换数据集，而不必改配置文件：
       RAGMINI_ROOT       包根（工作目录基准，默认 = 包根）
       RAGMINI_PDF_DIR    PDF 目录
       RAGMINI_DB         数据库文件
       RAGMINI_OUT_DIR    中间产物目录
       RAGMINI_MODEL_DIR  模型权重目录
       RAGMINI_CONFIG     配置文件路径
3. `ensure_runtime_env()` 负责跨平台运行时适配：
   - 把临时目录指向包内（避免某些受限环境下系统临时目录不可用）；
   - 默认把 HuggingFace 端点指向国内镜像，并关闭遥测。
"""
from __future__ import annotations

import copy
import os
import sys

# 包根目录：ragmini/（本文件位于 ragmini/ragcore/config.py）
PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULT_CONFIG_NAME = "config.yaml"

# ============================================================================
# 内置缺省配置（config.yaml 缺失键一律用这里的值补齐）
# ============================================================================
DEFAULTS = {
    "paths": {
        "pdf_dir": "pdf",                 # 存放 PDF 的目录（相对包根）
        "db": "数据库/rag.sqlite",          # SQLite 数据库
        "out_dir": "中间文件",              # 中间产物（缓存、导出）
        "model_dir": "模型",                # 模型权重父目录（其下按模型短名分目录）
        "web_index": "web/index.html",     # 演示页（相对代码目录）
        "journal_metrics": "journal_metrics.yaml",  # 期刊分级与指标表（人工复核的检索结果）
        "journal_index": "journal_index.tsv",       # 刊名索引（由 enrich 导出，PDF 抽取用）
    },
    # ---------------- 语料切分 ----------------
    "corpus": {
        "target_tokens": 350,      # 单个切片目标 token 数
        "overlap": 0.15,           # 相邻切片重叠比例
        "min_tokens": 120,         # 过短切片并入前一片
        "max_tokens": 700,         # 过长切片强制切分
        # 只保留这 8 类目标章节标签；命中规范词典但不在集合内的章节整段丢弃
        "keep_labels": ["Abstract", "Introduction", "Methods",
                        "Results", "Results and Discussion", "Discussion",
                        "Conclusion", "Front Matter"],
        "min_doc_chars": 2000,     # 正文过短的 PDF 跳过
    },
    # ---------------- 模型 ----------------
    "models": {
        "embedding": "BAAI/bge-m3",
        "reranker": "BAAI/bge-reranker-v2-m3",
        "device": "auto",              # auto | cpu | cuda | mps
        "batch_size": 16,
        "embed_max_length": 1024,
        "query_max_length": 256,
    },
    # ---------------- 意图识别 → 召回宽度 N ----------------
    "intent": {
        "use_llm": True,               # 有 API Key 时优先用 LLM 分类
        "labels": ["concept", "evidence", "judgement"],
        "n_by_intent": {"concept": 150, "judgement": 120, "evidence": 100},
        "n_default": 120,
    },
    # ---------------- 查询规范化（错别字纠正 + 术语统一） ----------------
    "query_norm": {
        "enabled": True,
        "max_expansion": 3.0,          # 规范化结果长度上限倍数（防改写护栏）
    },
    # ---------------- 五因子融合 ----------------
    "fusion": {
        # final = Σ wᵢ·scoreᵢ
        # **已删除被引次数（citation）因子**：原为 0.03 的占位（无数据源、恒为 0），
        # 去掉后把剩余 0.97 归一化回 1.0，各因子相对比例不变。
        "weights": {"semantic": 0.722, "journal": 0.103, "recency": 0.082,
                    "claim": 0.072, "section": 0.021},
        # 章节权重（键为小写标签；未命中走 default）
        "section_weights": {
            "results": 1.00, "results and discussion": 1.00,
            "discussion": 0.95, "conclusion": 0.92,
            "abstract": 0.85, "methods": 0.55, "introduction": 0.35,
            "front matter": 0.45, "default": 0.50,
        },
        # 期刊档位评分：优先取 journals 表的 tier_score（由 ragcore/journals.py 依据
        # journal_metrics.yaml 计算并写入）；此处只保留兜底分与手工覆盖。
        "journal_default_score": 0.45,
        "journal_overrides": {},          # 手工覆盖：{"theismejournal": 0.70}
        "recency_half_life_years": 12,   # 新近度指数衰减半衰期
        "recency_base_year": 0,          # 0 = 以当前年份为基准
    },
    # ---------------- 去重 ----------------
    "dedup": {
        "enabled": True,
        "before_rerank": True,     # 先按文献去重再重排（省算力，且保证一篇一证据）
        "max_per_doc": 1,
    },
    # ---------------- 证据分级（正则） ----------------
    "evidence": {
        # 顺序即优先级：direct > counter > indirect > background
        "patterns": {
            "direct_support": ["\\bwe (found|show|demonstrate|report|observed|reveal|conclude)\\b",
                               "\\b(results|data|analyses|analyses) (show|indicate|suggest|reveal|demonstrate|confirm)\\b",
                               "\\b(our|this) (study|results|analysis|work) (show|indicate|found|reveal|demonstrate)\\b",
                               "\\bnotably\\b", "\\bsignificantly\\b"],
            "counterevidence": ["\\bcontrary to\\b", "\\bcontrast(ed|ing)? with\\b",
                                "\\bno (significant )?(difference|effect|relationship|evidence)\\b",
                                "\\bnot (support|consistent) (with|by)\\b",
                                "\\brather than\\b"],
            "indirect_support": ["\\bsuggest(s|ing|ed)?\\b", "\\bimpl(y|ies|ied)\\b",
                                 "\\bmay\\b", "\\bmight\\b", "\\bcould\\b",
                                 "\\bpotential(ly)?\\b", "\\bconsistent with\\b",
                                 "\\bindicat(e|es|ing|ed)\\b"],
            "related_background": ["\\bprevious (stud(y|ies)|work|research)\\b",
                                   "\\bhas been (shown|reported|observed|demonstrated)\\b",
                                   "\\breview(s|ed|ing)?\\b", "\\bmeta-analysis\\b"],
        },
        # 统计量特征（用于把置信度抬到 HIGH）
        "stat_regex": ["p\\s*[<=>]\\s*0?\\.\\d+", "\\bR2?\\s*=\\s*0?\\.\\d+",
                       "\\d+(\\.\\d+)?\\s*%", "\\bsignifican(t|ce|tly)\\b",
                       "\\b95%\\s*(CI|confidence interval)\\b"],
    },
    # ---------------- 神经重排 ----------------
    "rerank": {
        "enabled": True,          # 模型缺失时自动跳过
        "pool": 50,               # 参与重排的候选数上限
        "alpha": 0.6,             # final = alpha·交叉编码器分 + (1-alpha)·语义分
        "max_length": 512,
    },
    # ---------------- 输出 ----------------
    "output": {
        "topk": 10,               # 最终证据条数
        "snippet_chars": 1200,    # 每条证据送入生成模型的字符上限
    },
    # ---------------- 生成模型 ----------------
    "llm": {
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "intent_model": "deepseek-chat",
        "temperature": 0.2,
        "intent_temperature": 0.0,
        "max_tokens": 1500,
        "timeout": 60,
        "retries": 3,
        "api_key_env": "DEEPSEEK_API_KEY",
        "api_key": "",            # 也可直接写在这里（不推荐，建议用环境变量）
    },
    # ---------------- 对外服务 ----------------
    "web": {"host": "127.0.0.1", "port": 8765},
}


# ============================================================================
# 工具
# ============================================================================
def package_root() -> str:
    """返回包根目录（ragmini/）。"""
    return PKG_ROOT


def _deep_merge(base: dict, extra: dict) -> dict:
    """把 extra 递归合并进 base（就地修改并返回 base）。"""
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def _env(*names, default=None):
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return default


def _resolve(base_dir: str, p: str) -> str:
    """相对路径按 base_dir 展开为绝对路径。"""
    return os.path.normpath(p if os.path.isabs(p) else os.path.join(base_dir, p))


def _model_subdir(model_dir: str, model_name: str) -> str:
    """模型权重目录：model_dir/<模型短名>；若不存在则返回空串（表示回退在线名称）。"""
    short = model_name.split("/")[-1]
    cand = os.path.join(model_dir, short)
    return cand if os.path.isfile(os.path.join(cand, "config.json")) else ""


def load_config(config_path: str | None = None, root: str | None = None) -> dict:
    """载入配置：内置缺省 → YAML → 环境变量，并把路径全部解析为绝对路径。"""
    root = os.path.abspath(root or _env("RAGMINI_ROOT", default=PKG_ROOT))
    cfg = copy.deepcopy(DEFAULTS)

    path = config_path or _env("RAGMINI_CONFIG", default=os.path.join(PKG_ROOT, DEFAULT_CONFIG_NAME))
    if path and os.path.isfile(path):
        try:
            import yaml
            with open(path, encoding="utf-8") as f:
                _deep_merge(cfg, yaml.safe_load(f) or {})
        except Exception as e:  # noqa: BLE001
            print(f"[警告] 读取配置 {path} 失败（{type(e).__name__}: {e}），改用内置缺省值")

    P = cfg["paths"]
    P["root"] = root
    P["pdf_dir"] = _resolve(root, _env("RAGMINI_PDF_DIR", default=P["pdf_dir"]))
    P["db"] = _resolve(root, _env("RAGMINI_DB", default=P["db"]))
    P["out_dir"] = _resolve(root, _env("RAGMINI_OUT_DIR", default=P["out_dir"]))
    P["model_dir"] = _resolve(root, _env("RAGMINI_MODEL_DIR", default=P["model_dir"]))
    P["web_index"] = _resolve(PKG_ROOT, P["web_index"])
    # 期刊分级表与刊名索引属于**包自带内容**（与 config.yaml 同级），因此按包根解析，
    # 而不是按 RAGMINI_ROOT：换工作目录时它们仍随包可用（也可用环境变量覆盖）。
    P["journal_metrics"] = _resolve(PKG_ROOT, _env("RAGMINI_JOURNAL_METRICS",
                                                  default=P["journal_metrics"]))
    P["journal_index"] = _resolve(PKG_ROOT, _env("RAGMINI_JOURNAL_INDEX",
                                                default=P["journal_index"]))
    # 本地权重目录（为空表示需要在线名称，由 sentence-transformers 自行下载）
    P["embedding_dir"] = _model_subdir(P["model_dir"], cfg["models"]["embedding"])
    P["reranker_dir"] = _model_subdir(P["model_dir"], cfg["models"]["reranker"])
    return cfg


def ensure_dirs(cfg: dict) -> dict:
    """创建运行所需目录（只创建、不删除）。"""
    for key in ("out_dir", "pdf_dir"):
        os.makedirs(cfg["paths"][key], exist_ok=True)
    os.makedirs(os.path.dirname(cfg["paths"]["db"]), exist_ok=True)
    os.makedirs(cfg["paths"]["model_dir"], exist_ok=True)
    return cfg


def ensure_runtime_env(cfg: dict) -> dict:
    """跨平台运行时适配（必须在 import torch / sentence_transformers 之前调用）。"""
    ensure_dirs(cfg)
    out = cfg["paths"]["out_dir"]

    # ① 临时目录统一放到包内：某些受限环境（如本机安全策略）下系统临时目录不可用，
    #    tempfile 的可用性探测会失败，导致 import torch 直接报错。
    tmp = os.path.join(out, "tmp")
    os.makedirs(tmp, exist_ok=True)
    os.environ["TMPDIR"] = tmp
    os.environ["TEMP"] = tmp
    os.environ["TMP"] = tmp
    try:
        import tempfile
        tempfile.tempdir = tmp
    except Exception:  # noqa: BLE001
        pass

    # ② 模型下载端点：默认国内镜像（可用 RAGMINI_HF_ENDPOINT 覆盖）
    os.environ.setdefault("HF_ENDPOINT",
                          os.environ.get("RAGMINI_HF_ENDPOINT", "https://hf-mirror.com"))
    os.environ.setdefault("HF_HOME", os.path.join(out, "hf_cache"))
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    # Apple Silicon 的 MPS 对个别算子无实现，回退 CPU 而不是直接报错
    os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
    # 限制线程数，避免小任务把整机 CPU 占满
    os.environ.setdefault("OMP_NUM_THREADS", "8")
    return cfg


def api_key(cfg: dict) -> str:
    """获取 LLM API Key（环境变量优先，其次配置文件）。"""
    return (os.environ.get(cfg["llm"]["api_key_env"], "") or cfg["llm"].get("api_key", "")).strip()


def summary(cfg: dict) -> str:
    """一行摘要，便于 doctor / 日志输出。"""
    P = cfg["paths"]
    return (f"root={P['root']}\n  pdf_dir={P['pdf_dir']}\n  db={P['db']}\n"
            f"  out_dir={P['out_dir']}\n  model_dir={P['model_dir']}\n"
            f"  embedding_dir={P['embedding_dir'] or '(未下载，需联网自动获取)'}\n"
            f"  reranker_dir={P['reranker_dir'] or '(未下载，将跳过神经重排)'}\n"
            f"  device={cfg['models']['device']}  topk={cfg['output']['topk']}  "
            f"api_key={'已配置' if api_key(cfg) else '未配置'}")

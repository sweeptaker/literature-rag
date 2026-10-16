#!/usr/bin/env python3
"""从 PDF 内部提取期刊名 / 年份 / DOI / ISSN（元数据抽取的**优先来源**）。

为什么需要它
------------
源文件名有 46 字符上限，期刊名与 DOI 都会被截断，例如
    "Applied Microbi-2017-10.1007_s00253-017-8330-5.pdf"  → Applied Microbiology and Biotechnology
    "Functional Ecolog-2022-10.1111_1.pdf"                → Functional Ecology（DOI 也被截断）
    "Proceedings of th-2009-10.1073_pnas.0811729106.pdf"  → PNAS
因此用户要求：**优先从 PDF 正文/元数据取值，文件名只作兜底**。

抽取依据（自实测 110 篇语料归纳，命中率最高的排在前）
--------------------------------------------------
journal
    ① ISSN 锚点：页脚常印 ISSN（如 Functional Ecology 的 "13652435"），
       与 journal_index.tsv 对上是**最强**证据，可直接得到规范全名；
    ② 页眉/页脚的「引文行」，各家格式都在这几类里：
         Ecology, 102(9), 2021, e03441                  （Wiley/ESA 风格）
         Appl Microbiol Biotechnol (2017) 101:4863–4870 （Springer 风格）
         Functional Ecology. 2022;36:1442–1456.         （Wiley 页脚）
         BioEssays 23:657–661                           （卷:页）
         Inland Waters (2011) 1, pp. 101-105
         Luo et al. AMB Expr (2019) 9:76                （作者 + 缩写刊名）
       其中刊名多为**缩写**（Appl Microbiol Biotechnol / Glob Change Biol / AMB Expr），
       故用 journal_index.tsv 的别名（含缩写）做规范映射；
    ③ 页边距里短小、像刊名的行（如 "Inland Waters"、"ECOGRAPHY"、"N A T U R E"）。
doi
    ① 首页正文里带前缀的完整 DOI（doi:10.xxxx/yyy、https://doi.org/…）；
    ② 首页正文里裸写的 10.xxxx/yyy；③ PDF 元数据 Subject / XMP。
year
    ① 引文行里的年份；② © YYYY / Copyright YYYY；③ Published/Accepted 行；
    ④ 页边距整行去空格后出现的 19xx/20xx（应对 "J U N E / 2 0 1 2" 这类字距撑开的排版）；
    ⑤ PDF 创建年份。
issn
    首页或页边距出现的 ISSN（形如 0032-079X / 13652435）。注意部分 PDF 会把
    ISSN 写成无连字符的 8 位数字，故两种形式都识别。

对外接口：
    load_journal_index(path)             读取刊名索引 TSV（纯文本，避免依赖 pyyaml）
    extract_pdf_meta(doc, prof, index)   提取并返回各字段 + 来源标记 + 候选留痕
    extract_title(doc, prof, journal)    论文标题（首页版式 + 元数据交叉验证）
                                         与 pick_title(lines, meta, journal) 纯函数核心

所有失败路径都返回空值，绝不臆造。
"""
import difflib
import os
import re

# ============ 正则 ============
ISSN_HYPHEN_RE = re.compile(r"\b(\d{4})-(\d{3}[\dXx])\b")
# 无连字符的 8 位 ISSN（仅当整行上下文含 issn / 页脚时启用，避免误伤页码）
ISSN_PLAIN_RE = re.compile(r"(?<!\d)(\d{4})(\d{3}[\dXx])(?!\d)")

DOI_PREFIXED_RE = re.compile(
    r"(?:doi\s*[:：]\s*|(?:dx\.)?doi\.org/)(10\.\d{4,9}/[^\s,;\"'<>()\[\]]+)", re.I)
DOI_BARE_RE = re.compile(r"\b(10\.\d{4,9}/[^\s,;\"'<>()\[\]]+)")

# 引文行：把「刊名」与「年份」一起抠出来（各出版商排版差异都在这 5 类里）
CITE_RES = [
    # Appl Microbiol Biotechnol (2017) 101:4863–4870 / Ecology Letters, (2019) 22: 1349–1356
    ("pdf:cite-paren", re.compile(
        r"^(?P<j>[A-Z][\w&'’.\- ]{2,70}?)\s*[,;:]?\s*[\(\uff08]\s*(?P<y>(?:19|20)\d{2})\s*[\)\uff09]")),
    # Journal of Animal Ecology 2013, 82, 1042–1051 / Ecology 2016, 104, 1234–1245
    ("pdf:cite-year-vol", re.compile(
        r"^(?P<j>[A-Z][\w&'’.\- ]{2,70}?)\s+(?P<y>(?:19|20)\d{2})\s*,\s*\d{1,4}\s*,")),
    # Ecology, 102(9), 2021, e03441 / Ecology, 93(8) Supplement, 2012, pp. S223–S233
    ("pdf:cite-comma", re.compile(
        r"^(?P<j>[A-Z][\w&'’.\- ]{2,70}?)\s*,\s*\d{1,4}\s*[\(\uff08]\s*\d+")),
    # Functional Ecology. 2022;36:1442–1456. / Glob Change Biol. 2019;25:2751–2762.
    ("pdf:cite-dot", re.compile(
        r"^(?P<j>[A-Z][\w&'’.\- ]{2,70}?)\s*\.\s*(?P<y>(?:19|20)\d{2})\s*;")),
    # BioEssays 23:657±661 / Soil Biol Biochem 44: 123–130
    ("pdf:cite-colon", re.compile(r"^(?P<j>[A-Z][\w&'’.\- ]{2,70}?)\s+\d{1,4}\s*:\s*\d")),
    # Ecology 102, 1234–1245, 2021
    ("pdf:cite-vol", re.compile(
        r"^(?P<j>[A-Z][\w&'’.\- ]{2,70}?)\s+\d{1,4}\s*,\s*\d{1,5}[–\-]\d{1,5}")),
]
YEAR_ANY_RE = re.compile(r"(?:19|20)\d{2}")

COPYRIGHT_RE = re.compile(r"(?:©|\uff08c\uff09|\(c\)|copyright|\u00df)\s*((?:19|20)\d{2})", re.I)
# 发表年份行。**故意不收 Received/Accepted**：实测这两者系统性早于出版年
# （"Received: 20 January 2019" 的论文常出现在 2020 年的卷期里），会把年份整体
# 拉早一年，比文件名还差；只有 Published / First published 才与出版年一致。
PUBYEAR_RE = re.compile(
    r"(?:published(?:\s+online)?(?:\s+on)?|first\s+published|date\s+of\s+publication)\b"
    r"[^|]{0,40}?((?:19|20)\d{2})(?!\d)", re.I)

# 期刊名特征词
JOURNAL_WORDS = (
    "journal", "ecology", "ecological", "ecography", "oikos", "microbiology",
    "microbiome", "microbial", "isme", "biology", "biological", "bioessays",
    "science", "sciences", "scientific", "nature", "npj", "cell", "trends",
    "letters", "review", "reviews", "proceedings", "philosophical",
    "transactions", "bulletin", "annals", "biogeochemistry", "biogeochemical",
    "soil", "water", "research", "environmental", "environment", "applied",
    "functional", "molecular", "global", "change", "climate", "phytologist",
    "plant", "inland", "aquatic", "limnology", "oceanography", "plos", "pnas",
    "frontiers", "current", "annual", "international", "european", "american",
)

# 明确不是刊名的特征
NEGATIVE_RE = re.compile(
    r"(issn|doi\s*[:：]|https?://|www\.|\.(com|org|net|edu)\b|/journal/|onlinelibrary|"
    r"©|\(c\)|copyright|all rights reserved|"
    r"received|accepted|revised|published|available\s+online|"
    r"\bvol\.?\s*\d|volume\s+\d|issue\s+\d|\bno\.\s*\d|pp?\.\s*\d|"
    r"supplementary|supporting\s+information|electronic\s+supplement|"
    r"university|universit|institute|institut|department|school\s+of|college|"
    r"correspondence|e-?mail|abstract|keywords|introduction|materials|"
    r"methods|results|discussion|conclusion|references|acknowledg|table\s+\d|"
    r"figure\s+\d|content\s+list|table\s+of\s+contents|downloaded\s+from|"
    r"terms\s+and\s+conditions|\bby\s+the\b|is\s+published\s+by|printed\s+in|"
    r"manuscript|\bin\s+preparation\b|"
    r"macmillan|wiley|elsevier|springer|oxford|taylor|pergamon|blackwell|"
    r"press\b|ltd\b|inc\.|authors?\b|et\s+al\.?\s*$)", re.I)

# 整行即为「文章类型标记」→ 不是刊名
SECTION_MARKER_RE = re.compile(
    r"^(review|reviews|mini[\s-]?review|primary\s+research\s+article|research\s+review|"
    r"research\s+(article|brief|paper|note)|original\s+(article|research|paper)|"
    r"perspective|editorial|commentary|comment|opinion|hypothesis|synthesis|forum|"
    r"article|note|brief\s+report|short\s+communication|rapid\s+communication|"
    r"technical\s+note|problems\s+and\s+paradigms|erratum|corrigendum|book\s+review|"
    r"letter\s+to\s+the\s+editor|front\s+matter|contents|abstract)$", re.I)

TAIL_TRIM_RES = [
    re.compile(r"\s*10\.\d{4,9}/\S*.*$"),                            # 尾部 DOI
    re.compile(r"\s*doi\s*[:：].*$", re.I),
    re.compile(r"\s*[\(\uff08]\s*(?:19|20)\d{2}\s*[\)\uff09].*$"),        # (2016) …
    re.compile(r"\s*\|.*$"),                                          # Nature 系列的竖线
    re.compile(r"\s*[,;:]\s*\d+.*$"),                                 # , 104: 1234
    re.compile(r"\s*\.\s*(?:19|20)\d{2}\s*;.*$"),                     # . 2022;36:…
    re.compile(r"\s+\d{1,4}\s*[\(\uff08]\s*\d{1,3}\s*[\)\uff09].*$"),   # 102(3)
    re.compile(r"\s+vol(?:ume)?\.?\s*\d+.*$", re.I),
    re.compile(r"\s*\bpp?\.\s*\d+.*$", re.I),
    re.compile(r"\s+\d{1,4}\s*\d{0,5}[–\-]\d{1,5}.*$"),                # 102, 1234–1245
    re.compile(r"\s+\d{1,4}\s*:.*$"),                                 # 23:657
    re.compile(r"\s+\d+(?:[.:]\d+)*\s*$"),                            # 尾部纯卷号 23.7
    re.compile(r"\s*\b(?:19|20)\d{2}\b.*$"),                          # 尾巴上的年份
    re.compile(r"\s*[.\-–—|•]+\s*$"),
]


# ============ 刊名索引 ============
def load_journal_index(path):
    """读取刊名索引 TSV：`规范名<TAB>别名(以 | 分隔)<TAB>ISSN(以 | 分隔)`。

    由 ragcore/journals.py 依据 journal_metrics.yaml 生成。
    作用：把 PDF 里的缩写刊名（Appl Microbiol Biotechnol）映射为规范全名，
    并把 ISSN 变成可用的锚点。文件缺失时返回空列表，抽取逻辑退化为纯启发式。
    """
    idx = []
    if not path or not os.path.exists(path):
        return idx
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.rstrip("\n")
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                parts = line.split("\t")
                name = parts[0].strip()
                if not name:
                    continue
                aliases = [a.strip() for a in (parts[1] if len(parts) > 1 else "").split("|") if a.strip()]
                issns = [i.strip().upper() for i in (parts[2] if len(parts) > 2 else "").split("|") if i.strip()]
                idx.append({"name": name, "aliases": aliases, "issns": issns,
                            "keys": _index_keys(name, aliases)})
    except OSError:
        pass
    return idx


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _cand(is_index, quality, rel, cov, name, src, year):
    """候选元组：(是否命中索引, 匹配质量, 行的可靠度, 覆盖率×1000, 刊名, 来源, 年份)。

    排序即按此顺序降序 —— 先看是否命中索引表，再看匹配质量（完全相等 > 子串 > 模糊 >
    单词级），再看这一行本身是否可靠（结构化引文行 > 首页/跨页重复的页眉页脚 > 其它），
    最后看覆盖率。这样可避免「2 栏排版页脚里的被引文献刊名」压过真正的刊名。
    """
    return (is_index, quality, rel, cov, name, src, year)


def _index_keys(name, aliases):
    """把规范名与别名都变成归一化匹配键，并标出匹配方式。

    匹配方式（防止短名误伤，实测教训：`Ecology` 是 `Microbial Ecology` /
    `Applied Soil Ecology` / `Functional Ecology` 的子串）：
        exact : 归一化后完全相等（长度 ≥ 4 即可）
        sub   : 子串包含（键长 ≥ 9 才允许）
        fuzzy : 相似度 ≥ 0.88（键长 ≥ 9 才允许，用于容忍 PDF 里的轻微拼写噪声）
    """
    keys = []
    for s in [name] + list(aliases):
        n = _norm(s)
        if len(n) < 4:
            continue
        if len(n) >= 9:
            keys.append((n, "exact", s))
            keys.append((n, "sub", s))
            keys.append((n, "fuzzy", s))
        else:
            keys.append((n, "exact", s))
    keys.sort(key=lambda t: -len(t[0]))
    return keys


def _despace(t):
    """去掉单字符之间的空格：应对 "N A T U R E" / "2 0 1 2" 这类撑开的排版。"""
    if re.search(r"(?:\b[A-Za-z]\s){3,}", t):
        return re.sub(r"(?<=\b[A-Za-z])\s(?=[A-Za-z]\b)", "", t)
    return t


# ============ 页边距扫描 ============
def _blocks(page, zone):
    """按块返回 zone 内的文本（zone=('top'|'bottom', 阈值)）。"""
    H = page.rect.height
    out = []
    for b in page.get_text("dict")["blocks"]:
        if b["type"] != 0:
            continue
        y0, y1 = b["bbox"][1], b["bbox"][3]
        kind, frac = zone
        if kind == "top" and y0 > H * frac:
            continue
        if kind == "bottom" and y1 < H * frac:
            continue
        txt = "\n".join("".join(s["text"] for s in ln["spans"]).strip()
                        for ln in b["lines"])
        if txt.strip():
            out.append(txt)
    return out


def _margin_candidates(doc, hf_top=0.12, hf_bottom=0.88, max_pages=4):
    """收集候选行：首页上下边距 + 前几页页边距中跨页重复的行。

    返回 [(文本, 来源, 是否跨页重复)]。重复出现的行可信度更高
    （刊名在页眉/页脚里是逐页复制的）。
    """
    out, seen = [], {}
    pages = min(doc.page_count, max_pages)
    for pno in range(pages):
        try:
            page = doc[pno]
        except Exception:                       # noqa: BLE001
            continue
        src = "pdf:firstpage" if pno == 0 else "pdf:header"
        for zone in (("top", hf_top), ("bottom", hf_bottom)):
            for txt in _blocks(page, zone):
                key = re.sub(r"\d+", "#", re.sub(r"\s+", " ", txt.strip().lower()))
                if len(key) < 3:
                    continue
                seen.setdefault(key, {"n": 0, "text": txt.strip(), "src": src})
                seen[key]["n"] += 1
    for v in seen.values():
        out.append((v["text"], v["src"], v["n"] >= 2))
    # 跨页重复的排前面
    out.sort(key=lambda t: (not t[2],))
    return out


# ============ 刊名判定 ============
def _clean_journal(raw):
    """清洗候选刊名；返回 (文本, 是否可用)。"""
    t = re.sub(r"\s+", " ", (raw or "").strip())
    t = t.strip(" \t\u00a0.,;:|-_()[]{}")
    if not t or len(t) < 3 or len(t) > 90:
        return "", False
    if SECTION_MARKER_RE.match(re.sub(r"\s+", " ", t)):
        return "", False
    if NEGATIVE_RE.search(t):
        return "", False
    for rx in TAIL_TRIM_RES:
        t2 = rx.sub("", t).strip(" \t.,;:|-_()")
        if len(t2) >= 3:
            t = t2
    if not (3 <= len(t) <= 70):
        return "", False
    if len(t.split()) > 10:
        return "", False
    if len(re.sub(r"[^A-Za-z]", "", t)) < 3:
        return "", False
    if not t[0].isalpha() or not t[0].isupper():
        return "", False
    if re.search(r"[.!?]\s+[A-Za-z]", t):         # 句末标点后又起句 → 正文
        return "", False
    if t.isupper() and len(t) > 34:
        return "", False
    return t, True


def _coverage(n, key):
    """匹配键对候选串的「解释度」= 较短者长度 / 较长者长度（0..1）。

    比「键长」更合理：实测 `Microbial Ecolo`（文件名截断）同时能子串匹配到
    `Microbial Ecology`（覆盖 0.81）与 `The ISME Journal` 的长别名
    `ISME Journal of Microbial Ecology`（覆盖 0.43）；按键长排会让后者胜出，
    按覆盖率排则前者胜出 —— 后者才正确。
    """
    return len(n) / max(len(n), len(key))


def match_index(text, index, min_key=4):
    """在刊名索引里查找 text（返回 (规范名, 命中方式, 覆盖率) 或 None）。

    **覆盖率护栏**：子串/模糊匹配要求覆盖率 ≥ 0.75。
    实测教训：机构行 "Environmental Microbiology and Ecology Research Center"（覆盖 0.48）、
    参考文献行 "Soil & Tillage Research 144 (2015) 107–115"（覆盖 0.61）都会被子串匹配
    命中真正的刊名，用 0.75 覆盖率即可拦下，同时不影响正常缩写行
    （"Glob Change Biol. 2019;25:2751–2762." 覆盖 1.0）。

    min_key：允许命中的最小键长。行尾短语、单词级匹配都要传 ≥ 8，
    以防 "…and Ecology" / "…in Nature" 这类正文片段被当成刊名。
    """
    n = _norm(text)
    if len(n) < max(4, min_key):
        return None
    best = None
    for ent in index or []:
        for key, how, _disp in ent["keys"]:
            if len(key) < min_key:
                continue
            if how == "exact":
                if n != key:
                    continue
                cur = (3, 1.0, ent["name"], "exact")
            elif how == "sub":
                if len(key) < 9 or not (key in n or n in key):
                    continue
                cov = _coverage(n, key)
                if cov < 0.75:
                    continue
                cur = (2, cov, ent["name"], "sub")
            else:                                   # fuzzy
                if len(key) < 9:
                    continue
                cov = _coverage(n, key)
                if cov < 0.75:
                    continue
                if difflib.SequenceMatcher(None, n, key).ratio() < 0.88:
                    continue
                cur = (1, cov, ent["name"], "fuzzy")
            if best is None or cur[:2] > best[:2]:
                best = cur
    if best is None:
        return None
    return best[2], best[3], best[1]


def _looks_like_journal(t):
    """未命中索引时：是否「像刊名」（含期刊特征词，且不像正文句）。"""
    if SECTION_MARKER_RE.match(t) or NEGATIVE_RE.search(t):
        return False
    low = " " + t.lower() + " "
    if not any(re.search(r"\b" + re.escape(w) + r"\b", low) for w in JOURNAL_WORDS):
        return False
    if len(t.split()) > 8:
        return False
    return True


# ============ 主函数 ============
def extract_pdf_meta(doc, prof=None, index=None, hf_top=0.12, hf_bottom=0.88):
    """提取期刊名 / DOI / 年份 / ISSN。返回带来源标记的字典。"""
    result = {"journal": "", "journal_src": "", "doi": "", "doi_src": "",
              "year": None, "year_src": "", "issn": "", "issn_src": "",
              "candidates": []}
    try:
        first = doc[0]
    except Exception:                           # noqa: BLE001
        return result

    cand_lines = _margin_candidates(doc, hf_top, hf_bottom)
    first_band = "\n".join(t for t, _s, _r in cand_lines if _s == "pdf:firstpage")
    # 首页正文区顶部若干行（引文行偶在正文区而非页边距）
    head_lines = []
    try:
        for b in first.get_text("dict")["blocks"]:
            if b["type"] != 0:
                continue
            for ln in b["lines"]:
                s = "".join(x["text"] for x in ln["spans"]).strip()
                if s:
                    head_lines.append(s)
    except Exception:                           # noqa: BLE001
        pass
    head_text = "\n".join(head_lines[:60])

    # ---------------- ISSN ----------------
    blob = first_band + "\n" + head_text[:1500]
    hyphen = [f"{m.group(1)}-{m.group(2)}".upper() for m in ISSN_HYPHEN_RE.finditer(blob)]
    # 无连字符的 8 位 ISSN（如 Functional Ecology 页脚的 "13652435"）。
    # 这类形式容易与页码/年份混淆，故**只有能对上刊名索引时才采信**。
    plain = [f"{m.group(1)}-{m.group(2)}".upper()
             for m in ISSN_PLAIN_RE.finditer(re.sub(r"[\s\-]", "", blob))]
    idx_issns = {i for ent in (index or []) for i in ent["issns"]}
    issn = next((c for c in hyphen + plain if c in idx_issns), None) or (hyphen[0] if hyphen else "")
    if issn:
        result["issn"] = issn
        result["issn_src"] = "pdf" if issn in idx_issns else "pdf:unverified"

    # ---------------- DOI ----------------
    # 优先级：页边距（页眉/页脚）内的带前缀 DOI > 首页正文带前缀 DOI >
    #         页边距内裸 DOI > 正文裸 DOI > PDF 元数据。
    # 教训：正文里的第一个 10.xxxx 可能来自参考文献或他刊引用，页边距更可靠。
    for src, text in (("pdf:band", first_band),
                      ("pdf:text", head_text[:4000])):
        m = DOI_PREFIXED_RE.search(text)
        if m:
            result["doi"] = m.group(1).rstrip(".,;:)]}")
            result["doi_src"] = src
            break
    if not result["doi"]:
        for src, text in (("pdf:band:bare", first_band),
                          ("pdf:bare", head_text[:4000])):
            m = DOI_BARE_RE.search(text)
            if m:
                result["doi"] = m.group(1).rstrip(".,;:)]}")
                result["doi_src"] = src
                break
    if not result["doi"]:
        try:
            blob2 = ((doc.metadata or {}).get("subject") or "") + " " + (doc.get_xml_metadata() or "")
        except Exception:                       # noqa: BLE001
            blob2 = ""
        m = DOI_BARE_RE.search(blob2)
        if m:
            result["doi"], result["doi_src"] = m.group(1).rstrip(".,;:)]}"), "pdf:meta"
    else:
        # 与元数据里的 DOI 比对：正文里那个若明显更短（被截断）则改用元数据版本
        try:
            blob2 = ((doc.metadata or {}).get("subject") or "") + " " + (doc.get_xml_metadata() or "")
        except Exception:                       # noqa: BLE001
            blob2 = ""
        m = DOI_BARE_RE.search(blob2)
        if m:
            c = m.group(1).rstrip(".,;:)]}")
            if len(c) > len(result["doi"]):
                result["doi"], result["doi_src"] = c, "pdf:meta"

    # ---------------- 候选刊名（含引文行解析）----------------
    cands = []          # (rank, keylen, canonical_or_raw, src, matched)

    # ISSN 锚点：能对上索引时是最强证据，直接给出规范全名
    if result["issn"] and result["issn_src"] == "pdf":
        for ent in index or []:
            if result["issn"] in ent["issns"]:
                cands.append(_cand(1, 4, 3, 30, ent["name"], "pdf:issn", None))
                break

    author_prefix_re = re.compile(
        r"^\s*[A-Z][A-Za-z'’\-]+(?:\s*(?:,|&|and)\s*[A-Z][A-Za-z'’\-]+)*\s+et\s+al\.?\s*[,;]?\s*")
    url_like_re = re.compile(r"[@/]|\.(?:org|com|net|edu|gov)\b", re.I)
    num_cut_re = re.compile(r"\s*\d[\d,;:.()/\-–—]*\s.*$")     # 从第一个数字串起截断

    def variants(raw):
        """候选刊名的多种写法 → [(文本, 允许命中的最小键长)]。

        实测各出版商的页眉/页脚格式差异极大，逐条对应：
            "Functional Ecology. 2022;36:1442–1456."        行首 = 刊名
            "Global Biogeochemical Cycles 10.1002/2017GB…"  刊名后紧跟数字
            "Phil. Trans. R. Soc. B 375: 20190240"          前面带 URL token
            "… 231 Applied and Environmental Microbiology"  刊名在行尾
            "Aguirre de Cárcer Microbiome"                  前面带作者名
        因此生成：原样 / 去字距空格 / 去作者前缀 / 截断首个数字串 / 去掉行首 URL /
        行尾 2–6 词短语（此路要求键长 ≥ 9，避免正文片段被误判）。
        """
        out = []

        def add(x, min_key):
            x = re.sub(r"\s+", " ", (x or "")).strip(" \t,;:|.-")
            if x and (x, min_key) not in out:
                out.append((x, min_key))

        bases = [raw, _despace(raw)]
        toks = raw.split()
        for i in range(1, min(4, len(toks))):                  # 去掉行首 URL / 邮箱 token
            if url_like_re.search(toks[i - 1]):
                bases.append(" ".join(toks[i:]))
        bases.append(author_prefix_re.sub("", raw))
        for b in bases:
            add(b, 4)
            add(num_cut_re.sub("", b), 4)
            add(author_prefix_re.sub("", b), 4)
            w = b.split()
            for n in range(2, 7):                              # 行尾短语
                if len(w) >= n:
                    add(" ".join(w[-n:]), 9)
        return out

    def line_year(flat):
        """从同一条行里取年份（与刊物名来自同一行，避免错取参考文献的年份）。"""
        for tag, rx in CITE_RES:
            mc = rx.match(flat)
            if mc:
                if mc.groupdict().get("y"):
                    return int(mc.group("y")), tag
                m2 = YEAR_ANY_RE.search(flat)
                if m2:
                    return int(m2.group(0)), tag + ":loose"
                return None, ""
        return None, ""

    def push_journal(raw, src, raw_ok=False, year=None, rel=1):
        """raw_ok=True 才允许「未命中索引表」时的裸候选（从严，宁可留空不误判）。

        rel = 该行的可靠度：3 结构化引文行 / 2 首页或跨页重复的页边距行 / 1 其它。
        实测教训：2 栏排版的页脚区会混入参考文献，例如 Microbial Ecology 的 PDF 底部
        出现 "Applied and Environmental Microbiology"（被引文献），若只按匹配键长排序，
        键更长的 AEM 会压过正确的 Microb Ecol —— 故把「行的可靠度」放在键长之前。
        """
        if not raw:
            return
        raw = re.sub(r"\s+", " ", raw).strip()
        if not raw:
            return
        for v, min_key in variants(raw):
            hit = match_index(v, index, min_key=min_key)
            if hit:
                q = {"exact": 3, "sub": 2, "fuzzy": 1}[hit[1]]
                cands.append(_cand(1, q, rel, int(hit[2] * 1000), hit[0],
                                   src + ":index", year))
                return
        # 单词级精确匹配：仅接受 ≥ 8 字符的键，避免 "Ecology"/"Nature"/"PNAS"
        # 这类通用短词在正文片段里被误判（那些情况交给 enrich_journals 按
        # 文件名兜底，反而更准）。
        for w in raw.split():
            if len(_norm(w)) < 8:
                continue
            hit = match_index(w, index, min_key=8)
            if hit and hit[1] == "exact":
                cands.append(_cand(1, 0, rel, int(hit[2] * 1000), hit[0],
                                   src + ":word", year))
                return
        if not raw_ok or rel < 3:
            # 未命中索引表时只接受「结构化引文行」给出的裸刊名。
            # 实测：跨页重复的页眉里可能整行都是正文（"Letter RESEARCH"、
            # "ECOLOGY Transient phenomena in ecology"），当刊名用比留空更糟 ——
            # 留空时 journals.py 会用文件名兜底，反而更准。
            return
        t, ok = _clean_journal(raw)
        if ok and len(re.sub(r"[^A-Za-z]", "", t)) >= 6 and _looks_like_journal(t):
            cands.append(_cand(0, 0, rel, len(t), t, src, year))

    # ① 只认三类证据：跨页重复的页眉/页脚、结构化引文行、ISSN 锚点。
    #    单次出现的页边距行一律不做逐段扫描 —— 实测那是误判的主要来源：
    #    2 栏排版的页脚区会混入参考文献与正文，导致把被引文献的刊名
    #    （"Trends in Ecology & Evolution"、"Soil & Tillage Research"）当成本刊名，
    #    比"留空"更糟（会在检索时套用错误的期刊档位）。这类漏掉的论文由
    #    journals.py 用文件名兜底，反而更准。
    scan = list(cand_lines) + [(l, "pdf:head", False) for l in head_lines[:6]]
    for txt, src, repeated in scan:
        flat = re.sub(r"\s+", " ", txt)
        ly = line_year(flat)
        rel = 2 if repeated else 1
        if repeated:                                # 跨页重复的行才逐段扫描
            for seg in re.split(r"\s*[|\u2022]\s*|\s+/\s+|\s{3,}", flat):
                if seg:
                    push_journal(seg, src, year=ly, rel=rel)
        for tag, rx in CITE_RES:                    # 结构化引文行（所有行都试一次）
            mc = rx.match(flat)
            if mc:
                push_journal(mc.group("j"), tag, raw_ok=True, year=ly, rel=3)
                break
        if repeated and src != "pdf:head" and len(flat.split()) <= 7:
            push_journal(flat, src, raw_ok=True, year=ly, rel=rel)

    # ---------------- 结论（先定刊名，再取候选里带年份的那一个）----------------
    year, ysrc = None, ""
    if cands:
        # 短刊名（< 8 字符：Ecology / Nature / Science / Cell / PNAS / Oikos）
        # 只有在「结构化引文行」或「ISSN 锚点」这类强证据下才采信 ——
        # 实测否则会因为一篇论文的页眉里出现 "…Ecology…" 之类的片段而误判。
        strong = [c for c in cands
                  if not (c[0] and c[1] == 3 and c[3] < 8000 and c[2] < 3)]
        pool = strong or cands
        pool.sort(key=lambda c: (-c[0], -c[1], -c[2], -c[3]))
        best = pool[0]
        result["journal"], result["journal_src"] = best[4], best[5]
        for c in pool:                              # 引文行自带的年份与刊名同源，最可信
            if c[6] and c[6][0]:
                year, ysrc = c[6]
                break
    if not year:
        mc = COPYRIGHT_RE.search(first_band + "\n" + head_text[:2000])
        if mc:
            year, ysrc = int(mc.group(1)), "pdf:copyright"
    if not year:
        mc = PUBYEAR_RE.search(first_band + "\n" + head_text[:2000])
        if mc:
            year, ysrc = int(mc.group(1)), "pdf:pubdate"
    if not year:                                # 字距撑开的年份（"2 0 1 2"）
        dsp = _despace(first_band)
        if dsp != first_band:                   # 只有确实存在字距撑开时才采信
            mc = YEAR_ANY_RE.search(dsp)
            if mc:
                year, ysrc = int(mc.group(0)), "pdf:band"
    if not year:                                # 最弱：PDF 创建年份（可能是下载/排版年）
        try:
            mc = YEAR_ANY_RE.search((doc.metadata or {}).get("creationDate") or "")
            if mc:
                year, ysrc = int(mc.group(0)), "pdf:create"
        except Exception:                       # noqa: BLE001
            pass
    if year and 1900 < year < 2100:
        result["year"], result["year_src"] = year, ysrc

    result["candidates"] = [
        {"text": c[4], "src": c[5], "index": bool(c[0]),
         "quality": c[1], "rel": c[2], "keylen": c[3]} for c in cands[:8]]
    return result


# ============================================================================
# 论文标题抽取（首页版式 + 文档元数据交叉验证）
# 供 pdf2chunks 使用；与主项目 代码/5_文本切片/pdf_meta.py 保持同一实现。
# ============================================================================
_TITLE_JUNK_RE = re.compile(
    r"^(doi[:\s]|https?://|www\.|©|copyright|issn|isbn|vol\.|volume|issue|"
    r"received|accepted|available online|research article|review article|"
    r"original article|short communication|editorial|letter to|corrigendum|"
    r"erratum|special issue|front matter|back matter|keywords?|jstor|"
    r"view project|supplementary|lawrence berkeley)\b|©", re.I)
_TITLE_META_JUNK_RE = re.compile(
    r"\.(docx?|pdf|indd|qxd|pages)\b|Microsoft Word|InDesign|Layout|"
    r"Untitled|unknown", re.I)
# 版权 / 许可 / 出版商样板（整簇出现即拒绝）
_TITLE_BOILER_RE = re.compile(
    r"creative commons|open access article|all rights reserved|jstor|"
    r"not-for-profit|view project|©|copyright|licensed under|terms of use|"
    r"downloaded from|by the ecological society|wiley & sons|springer nature|"
    r"pnas is a|proceedings of the national academy|university of chicago|"
    r"university press|see discussions, stats", re.I)
# 作者行：连续两个及以上「单字母缩写.」（如 "S. J. McNAUGHTON"）或含邮箱
_TITLE_AUTHOR_RE = re.compile(r"(?:\b[A-Z]\.\s*){2,}|[\w.+-]+@[\w-]+\.\w+|et al\.")
# 作者名单行（Elsevier 等）：一行里 ≥3 个「大写词(可带上标)+逗号」，
# 如 "Bin Zhanga, Aizhen Liangb,⁎, Zhanbo Weic, Xueli Dinga,⁎"（标题行极少这样）
_TITLE_AUTHORLIST_RE = re.compile(r"\b[A-Z][a-z]{1,14}[⁎*†‡\d]*,")
# 栏头 / 文章类型词（Ecography 等期刊在标题上方印一个同字号的 "Research"）。
# 只收「几乎不可能单独成为标题一行」的词，避免误伤折行标题里的短行。
_TITLE_SECTION_RE = re.compile(
    r"^(research|articles?|original (research )?articles?|reviews?|opinion|"
    r"perspectives?|comments?|commentary|corrections?|in ?brief|brief ?report|"
    r"letters?|news|policy|editorial|methods?|techniques?|statistics?|"
    r"statistical reports?|data papers?|the papers?|feature|focused review)\.?$", re.I)

# 连字（ligature）还原：PDF 文本层常把 fi/fl/ff 等编码成专用字符
_TT_LIG_MAP = {"ﬁ": "fi", "ﬂ": "fl", "ﬀ": "ff", "ﬃ": "ffi", "ﬄ": "ffl"}
_TT_LIG = re.compile("[" + "".join(_TT_LIG_MAP) + "]")


def _tt_norm(s):
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _tt_similar(a, b):
    """规范化后的串相似度（difflib）。覆盖率比值不适合等长串（会误判），故用这里。"""
    a, b = _tt_norm(a), _tt_norm(b)
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _tt_line_ok(t, journal_hint):
    """行级硬过滤：一定不是标题片段的行。长宽与词数校验放在合并之后（老期刊逐词分行）。"""
    t = (t or "").replace("_", " ").strip()          # Supplementary_information_R4
    if len(_tt_norm(t)) < 3 or len(t) > 300:
        return False
    if _TITLE_JUNK_RE.match(t):
        return False
    if re.fullmatch(r"[\d\s.,;:()\[\]–—-]+", t):        # 纯页码/日期行
        return False
    if _TITLE_AUTHOR_RE.search(t):                       # 作者行 / 邮箱
        return False
    if re.match(r"^\d{1,2}[A-Z]", t):                    # 单位行上标："1School of …"
        return False
    # 作者名单（姓名粘上标序号）："Cardinale1, J. Emmett Duffy2, Andrew Gonzalez3,"
    if len(re.findall(r"\b[A-Z][a-z]{2,}\d,", t)) >= 2:
        return False
    if _TITLE_SECTION_RE.match(t):                       # 栏头 / 文章类型词
        return False
    if journal_hint and _tt_similar(t, journal_hint) >= 0.75:  # 刊名横幅 / 页眉
        return False
    return True


def _tt_cluster_ok(t, journal_hint=""):
    """合并后的候选串是否像一个论文标题。"""
    t = (t or "").strip()
    if len(t) < 12 or len(t) > 300:
        return False
    if _TITLE_JUNK_RE.match(t) or _TITLE_AUTHOR_RE.search(t):
        return False
    if _TITLE_BOILER_RE.search(t):                       # 版权/许可/出版商样板
        return False
    if re.fullmatch(r"[\d\s.,;:()\[\]–—-]+", t):
        return False
    if t[0].islower():                                   # 不像标题的句首
        return False
    # 作者上标序号连串："CADOTTE,1,2,4" / "Knight 1,4,6,12*"（高精度，标题几乎不会有）
    if re.search(r"\d+(?:,\d+){2,}", t):
        return False
    # 姓名粘上标序号的作者名单："Anne Larigauderie12, Diane S. Srivastava13 & …"
    if len(re.findall(r"\b[A-Z][a-z]{2,}\d{1,3}\b", t)) >= 2:
        return False
    # 单位行："Biology Department, Indiana University, …"
    if re.search(r"\b(Department|Universit|Institute|Laborator)", t) and "," in t:
        return False
    if t.isupper() and len(t) <= 40:                     # 短全大写多为栏目名
        return False
    return True


def _tt_strip_banner(t, journal_hint):
    """剥掉并进来的刊名横幅前缀（如 "MICROBIOME Best practices…" / "ECOLOGY ent ways…"）。"""
    toks = t.split()
    jn = _tt_norm(journal_hint)
    while toks:
        w = toks[0].strip(".,;:")
        if len(w) >= 4 and w.isupper() and _tt_norm(w) and _tt_norm(w) in jn:
            toks.pop(0)
        else:
            break
    return " ".join(toks)


def pick_title(lines, meta_title="", journal_hint=""):
    """由首页文本行推断论文标题。

    lines: [(y0, size, text), ...]（首页各行，含字号与纵坐标）。
    返回 (title, src)；src ∈ font / meta / meta+font / font+meta / none。

    规则：
      1. 行级硬过滤：页码行、DOI/版权/URL、作者行（缩写连缀/邮箱）、刊名横幅
         （与 journal_hint 的 difflib 相似度 ≥0.75）；
      2. 按字号从大到小逐组尝试「锚点行 → 向下合并字号相近（≥0.85×，折行标题
         的各行字号几乎总与首行一致）且纵向相邻（≤40pt）的续行」，**合并后再做
         合法性校验**（老期刊标题逐词分行，单行常不足 12 字，不能在行级做长度过滤）；
      3. 真标题几乎都 ≥6 词，先取第一个满足的字号组；没有则退回第一个合法组
         —— 这同时挡掉大字号的栏目横幅（如 Ecology 的 "Statistical Reports"）；
      4. PDF 元数据标题交叉验证：为版式标题的完整超集时用元数据（补全截断），
         两者高度一致时用版式结果，明显冲突时仍信版式（元数据常见 Word 残留）。
    """
    rows = []
    for y, size, t in lines:
        t = re.sub(r"\s+", " ", (t or "")).strip()
        if t and _tt_line_ok(t, journal_hint):
            rows.append((float(y or 0.0), float(size or 0.0), t))
    rows.sort(key=lambda x: (x[0], -x[1]))              # y 升序 = 阅读顺序

    layout, layout_src = "", ""
    if rows:
        fallback = ""
        for asz in sorted({r[1] for r in rows}, reverse=True)[:6]:
            ai = next(i for i, r in enumerate(rows) if abs(r[1] - asz) < 0.3)
            ay = rows[ai][0]
            parts = [rows[ai][2]]
            for y, sz, t in rows[ai + 1:]:
                if len(" ".join(parts)) >= 300:
                    break
                if y < ay - 2 or (y - ay) > 40:
                    continue
                # 折行标题的各行字号几乎总是与首行完全一致；0.85 的门槛把
                # 紧随其后的作者行（略小一号，如 10.6/13.4=0.79）挡在外面
                if sz >= asz * 0.85:
                    # 一行里 ≥3 个「大写词+逗号」视为作者名单，就地截断
                    if len(_TITLE_AUTHORLIST_RE.findall(t)) >= 3:
                        break
                    parts.append(t)
                    ay = max(ay, y)                     # 锚点逐步下移，允许连续折行
            cand = _TT_LIG.sub(lambda m: _TT_LIG_MAP[m.group(0)],
                               " ".join(parts)).strip()
            cand = _tt_strip_banner(cand, journal_hint)
            if not _tt_cluster_ok(cand, journal_hint):
                continue
            if len(cand.split()) >= 6:                  # 真标题几乎都 ≥6 词
                layout, layout_src = cand, "font"
                break
            fallback = fallback or cand                 # 记住第一个合法但偏短的
        if not layout and fallback:
            layout, layout_src = fallback, "font"

    meta = re.sub(r"\s+", " ", (meta_title or "")).strip()
    # 下划线几乎只出现在文件名式的元数据标题里（如 "Supplementary_information_R4"）
    if _TITLE_META_JUNK_RE.search(meta) or len(meta) < 20 or "_" in meta:
        meta = ""

    if meta and not layout:
        return meta, "meta"
    if meta and layout:
        if _tt_norm(layout) and _tt_norm(layout) in _tt_norm(meta):
            return meta, "meta+font"                   # 元数据是完整版（版式标题被截断）
        if _tt_similar(layout, meta) >= 0.75:
            return layout, "font+meta"                 # 两源一致
        return layout, "font"                          # 冲突：信版式不信元数据
    return layout, (layout_src if layout else "none")


def extract_title(doc, prof=None, journal_hint=""):
    """打开中的 PDF → (标题, 来源)。只读首页，供入库与回填共用。

    顶部只裁掉 0.035H 以上的边缘（期刊社标/页眉页码），**不裁到 0.09H**——
    实测 Ecology Letters 等期刊的标题起始于 y≈0.08H，裁太狠会把真标题排除、
    锚到摘要首句。刊名横幅交给 journal_hint 与内容过滤器处理。
    """
    page = doc[0]
    H = float(page.rect.height)
    top, bot = H * 0.035, H * 0.92
    lines = []
    for b in page.get_text("dict")["blocks"]:
        if b.get("type") != 0:
            continue
        y0 = float(b["bbox"][1])
        if y0 < top or y0 > bot:                        # 页眉/页脚横幅区
            continue
        for ln in b.get("lines", []):
            spans = ln.get("spans") or []
            if not spans:
                continue
            t = "".join(s.get("text", "") for s in spans)
            size = max((float(s.get("size", 0)) for s in spans), default=0.0)
            lines.append((float(ln.get("bbox", [0, y0, 0, 0])[1]), size, t))
    try:
        meta_title = (doc.metadata or {}).get("title") or ""
    except Exception:                                   # noqa: BLE001
        meta_title = ""
    return pick_title(lines, meta_title, journal_hint)

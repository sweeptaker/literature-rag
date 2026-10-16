"""PDF → 章节 → 切片 → SQLite。

流程：
    ① 逐页抽取文本块（保留字号，用于判断标题层级）
    ② 噪声过滤：图表题注、页眉页脚、出版商样板、纯 DOI/URL 行
    ③ 章节切分：规范词典命中优先，其次字号启发式；未命中标题并入当前章节
    ④ 标签策略：只保留 8 类目标标签，命中词典但不在白名单的章节整段丢弃
    ⑤ 章节内切片：句子级累积，target 350 token / overlap 0.15 / min 120 / max 700，绝不跨章节
    ⑥ 写入 documents / sections / chunks 三表

所有阈值来自 config.yaml 的 corpus 节。
"""
from __future__ import annotations

import hashlib
import re
import unicodedata

# ============================================================================
# 文本工具
# ============================================================================
_SENT_PLACEHOLDER = "\x00"
_ABBR = ["e.g.", "i.e.", "et al.", "Fig.", "Tab.", "vs.", "approx.", "No.",
         "cf.", "sp.", "spp.", "Dr.", "Prof.", "ca.", "etc."]


def est_tokens(text: str) -> int:
    """粗估 token 数：中日韩字符按 1 计，拉丁词按 1.35 计（对 bge-m3 足够）。"""
    cjk = len(re.findall(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]", text))
    words = len(re.findall(r"[A-Za-z0-9][A-Za-z0-9'\-\u2019]*", text))
    return int(cjk + words * 1.35)


def split_sentences(text: str) -> list[str]:
    """句级切分（兼顾中英文），并保护 e.g. / Fig. 之类的缩写不被误切。"""
    t = text
    for i, a in enumerate(_ABBR):
        t = t.replace(a, f"\x01{i}\x01")
    parts = re.split(r"(?<=[.!?。！？])", t)
    out = []
    for s in parts:
        for i, a in enumerate(_ABBR):
            s = s.replace(f"\x01{i}\x01", a)
        s = s.strip()
        if s:
            out.append(s)
    return out


def normalize_ws(text: str) -> str:
    """统一空白与连字符断行（PDF 抽取常见的 -\\n 断词）。"""
    t = text.replace("\u00ad", "")            # 软连字符
    t = re.sub(r"(\w)-\n(\w)", r"\1\2", t)    # 行末断词
    t = re.sub(r"[ \t\u00a0]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


# ============================================================================
# 章节标签词典
# ============================================================================
# 值 = 目标标签；"DROP" 表示该章节整段丢弃；"STOP" 表示从此处起停止解析
CANON_LABELS = [
    (r"^(?:abstract|summary)$", "Abstract"),
    (r"^(?:graphical abstract|highlights|front ?matter)$", "Front Matter"),
    (r"^\d*\.?\s*(?:introduction|background|introduction and background)$", "Introduction"),
    (r"^(?:materials?\s+and\s+methods?|methods?|methodology|experimental\s+(?:procedures?|design)|"
     r"study\s+(?:site|area|system)|sampling(?:\s+design)?|data\s+(?:collection|analysis)|"
     r"statistical\s+analys\w*|field\s+methods?|model\s+description)$", "Methods"),
    (r"^(?:results?\s+and\s+discussion|results?\s+and\s+discussions?)$", "Results and Discussion"),
    (r"^(?:results?|findings?|results?\s+and\s+analys\w*)$", "Results"),
    (r"^(?:discussions?|general\s+discussion|concluding\s+remarks?)$", "Discussion"),
    (r"^(?:conclusions?|conclusions?\s+and\s+(?:outlook|perspectives?)|"
     r"summary\s+and\s+conclusions?|concluding\s+conclusions?)$", "Conclusion"),
    # ---- 以下为「规范词典命中但不在目标集」→ 整段丢弃 ----
    (r"^(?:acknowledg\w*|funding|financial\s+support|author\s+contributions?|"
     r"conflicts?\s+of\s+interest|competing\s+interests?|data\s+availability(?:\s+statement)?|"
     r"ethics\s+statement|supplementary\s+(?:information|material)s?|"
     r"supporting\s+information|appendix\s*[A-Za-z0-9]*|keywords?|key\s*words?|"
     r"abbreviations|declarations?|permissions?|orcid)$", "DROP"),
    (r"^(?:references?|literature\s+cited|bibliography|works\s+cited)$", "STOP"),
]
_CANON = [(re.compile(p, re.I), lab) for p, lab in CANON_LABELS]

# 出版商样板 / 页脚特征
BOILER_PATTERNS = [
    r"downloaded from", r"all rights reserved", r"this article is protected by copyright",
    r"terms and conditions", r"^https?://", r"www\.[a-z0-9\-]+\.(com|org|net|cn)",
    r"^\s*doi:\s*10\.", r"^\s*https?://doi\.org/", r"published by", r"oxford university press",
    r"wiley (online library|periodicals)", r"proceedings of the royal society",
    r"^\s*©\s*\d{4}", r"^\s*\d{1,3}\s*$", r"^\s*page \d+ of \d+",
    r"^\s*(received|accepted|published)\s*[:.]?\s*\d", r"^\s*journal homepage",
]
_BOILER = [re.compile(p, re.I) for p in BOILER_PATTERNS]

# 图表题注
CAPTION_RE = re.compile(r"^\s*(?:fig(?:ure)?|tab(?:le)?|plate|box|scheme)\s*\.?\s*[0-9]",
                        re.I)
# 纯 DOI / 章节号行
DOI_ONLY_RE = re.compile(r"^\s*(?:https?://)?(?:dx\.)?doi\.org/10\.\d{4,9}/\S+\s*$", re.I)
HEADING_NUM_RE = re.compile(r"^\s*(?:\d+(?:\.\d+)*|[IVXivx]+)[.)]?\s+\S")


def classify_heading(text: str) -> str | None:
    """把一行标题文本映射为标签；返回 None 表示「不是已知标题」。"""
    t = re.sub(r"^\d+(?:\.\d+)*[.)]?\s+", "", text.strip())   # 去掉 "2.1 " 之类编号
    t = t.strip().strip(":.").lower()
    if not t or len(t) > 60:
        return None
    for rx, lab in _CANON:
        if rx.match(t):
            return lab
    # 二次判定：长尾标题按关键词归入目标章节（如 "Sampling design and analyses" → Methods）
    # 加词数上限，避免把正文句（如 "results of modularity values indicated that…"）误判为标题
    if len(t.split()) <= 5:
        for rx, lab in _KW_HINTS:
            if rx.search(t):
                return lab
    return None


# 长尾标题的关键词归属（顺序即优先级）
_KW_HINTS = [(re.compile(p, re.I), lab) for p, lab in [
    (r"\bdiscussion\b", "Discussion"),
    (r"\bconclusions?\b", "Conclusion"),
    (r"\b(results?|findings?)\b", "Results"),
    (r"\b(methods?|methodology|sampling|analys[ei]s|analyses|analytical|experimental|"
     r"study (site|area|system)|data (collection|analys\w*)|model description|"
     r"statistical)\b", "Methods"),
    (r"\b(introduction|background|overview)\b", "Introduction"),
    (r"\babstract\b", "Abstract"),
]]

# 允许「标题与正文同处一个文本块」时做内联切分的白名单词
# （这些词几乎不会出现在正文句首，因此可安全切分）
INLINE_SAFE_WORDS = {"abstract", "summary", "keywords", "key words", "key word",
                     "graphical abstract", "highlights", "abbreviations"}
_INLINE_RE = re.compile(r"^([A-Za-z][A-Za-z \u2019'\-]{2,30}?)(\s*[:.\u2013\u2014\-]\s*|\s+)")


def split_inline_heading(text: str, is_large: bool = False):
    """识别「标题与正文同处一个文本块」，如 `Abstract Phytoplankton blooms are ...`。

    返回 (label, 余下正文)；无法判定时返回 (None, None)。
    三重护栏避免误切正文句首：仅当 ① 以冒号引导，或 ② 命中白名单词，
    或 ③ 该块字号明显大于正文（说明首行确为标题）时才切分，且余下正文须足够长。
    """
    first = _clean_line(text.split("\n")[0])
    m = _INLINE_RE.match(first)
    if not m:
        return None, None
    word, sep = m.group(1).strip(), m.group(2).strip()
    lab = classify_heading(word)
    if lab is None:
        return None, None
    colon = sep.startswith(":")
    if not (colon or word.lower() in INLINE_SAFE_WORDS or is_large):
        return None, None
    tail = first[m.end():].strip()
    if len(tail) < 40:                      # 太短：多半只是单纯的标题行
        return None, None
    rest = (tail + "\n" + "\n".join(text.split("\n")[1:])).strip()
    return lab, normalize_ws(rest)


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def _clean_line(s: str) -> str:
    return re.sub(r"[ \t\u00a0]+", " ", s).strip()


# ============================================================================
# PDF 抽取
# ============================================================================
def _open_pdf(path: str):
    """兼容 pymupdf 的新旧导入名。"""
    try:
        import pymupdf as fitz
    except ImportError:      # pymupdf < 1.24
        import fitz
    return fitz.open(path)


def extract_blocks(path: str, max_pages: int = 0,
                   journal_index=None) -> tuple[list[dict], dict]:
    """抽取每页的文本块。返回 (blocks, info)。

    blocks: [{page, text, size, n_chars}]，按阅读顺序（先页序、再 y 坐标）。
    info:   {n_pages, body_size, title_meta, meta_pdf}
            meta_pdf 为 PDF 优先的元数据抽取结果（期刊名/年份/DOI/ISSN + 来源标记），
            在同一个 doc 句柄上顺手完成，避免二次打开文件。
    """
    doc = _open_pdf(path)
    blocks: list[dict] = []
    sizes: list[tuple[float, int]] = []      # (字号, 字符数) 用于估计正文字号
    meta_title = ""
    meta_pdf = {}

    try:
        meta_title = (doc.metadata or {}).get("title") or ""
    except Exception:  # noqa: BLE001
        pass
    try:
        meta_pdf = PM.extract_pdf_meta(doc, None, journal_index)
    except Exception as e:  # noqa: BLE001
        info_err = f"{type(e).__name__}: {e}"
    else:
        info_err = ""

    n_pages = doc.page_count
    limit = max_pages if 0 < max_pages < n_pages else n_pages
    for pno in range(limit):
        page = doc.load_page(pno)
        data = page.get_text("dict")
        for blk in data.get("blocks", []):
            if blk.get("type") != 0:            # 非文本块（图片）跳过
                continue
            lines, max_size = [], 0.0
            for ln in blk.get("lines", []):
                spans = ln.get("spans", [])
                if not spans:
                    continue
                line_txt = "".join(sp.get("text", "") for sp in spans)
                if not line_txt.strip():
                    continue
                line_size = max(float(sp.get("size", 0)) for sp in spans)
                max_size = max(max_size, line_size)
                lines.append((line_txt, line_size))
            if not lines:
                continue
            text = normalize_ws("\n".join(t for t, _ in lines))
            if not text:
                continue
            # 逐行登记字号（只统计较长的行，避免标题行抬高正文字号估计）
            for t, sz in lines:
                if len(t.strip()) >= 40:
                    sizes.append((sz, len(t)))
            blocks.append({"page": pno + 1, "text": text, "size": max_size,
                           "n_chars": len(text)})
    doc.close()

    # 正文字号 = 按字符数加权的众数（四舍五入到 0.5）
    body_size = 10.0
    if sizes:
        bucket: dict[float, int] = {}
        for sz, n in sizes:
            k = round(sz * 2) / 2
            bucket[k] = bucket.get(k, 0) + n
        body_size = max(bucket.items(), key=lambda kv: kv[1])[0]
    return blocks, {"n_pages": limit, "body_size": body_size, "title_meta": meta_title,
                    "meta_pdf": meta_pdf, "meta_pdf_err": info_err}


def filter_noise(blocks: list[dict]) -> list[dict]:
    """去掉题注、样板行，以及重复出现在多页的短行（页眉页脚）。"""
    # ① 统计跨页重复短行
    from collections import Counter
    pages_of: dict[str, set] = {}
    for b in blocks:
        for line in b["text"].split("\n"):
            s = _clean_line(line)
            if 0 < len(s) <= 80:
                pages_of.setdefault(s.lower(), set()).add(b["page"])
    n_pages = max((b["page"] for b in blocks), default=1)
    repeated = {k for k, pgs in pages_of.items()
                if len(pgs) >= max(3, int(n_pages * 0.4))}

    out: list[dict] = []
    for b in blocks:
        if b["n_chars"] < 2:
            continue
        flat = _clean_line(b["text"])
        if not flat:
            continue
        if CAPTION_RE.match(flat) and b["n_chars"] < 400:
            continue
        if DOI_ONLY_RE.match(flat):
            continue
        if any(rx.search(flat) for rx in _BOILER) and b["n_chars"] < 200:
            continue
        if len(flat) <= 80 and flat.lower() in repeated:
            continue
        # 剔除「几乎不含字母数字」的块（公式符号残片）
        alnum = sum(c.isalnum() for c in flat)
        if alnum < max(6, len(flat) * 0.25):
            continue
        out.append(b)
    return out


# ============================================================================
# 章节切分
# ============================================================================
def build_sections(blocks: list[dict], body_size: float, keep_labels: list[str]) -> list[dict]:
    """把文本块归入章节。返回 [{label, raw_title, page_start, page_end, text, page_marks, keep}]。

    判定顺序：
        ① 整行命中规范词典 → 开新章节（DROP=整段丢弃，STOP=停止解析）
        ② 首行与正文同块（如 "Abstract Phytoplankton blooms …"）→ 内联切分，开新章节
        ③ 首行字号明显大于正文 → 视为长尾标题：丢掉标题行，余下正文并入当前章节
        ④ 其余 → 作为正文追加到当前章节（尚无章节时先建 Front Matter）
    """
    sections: list[dict] = []
    cur: dict | None = None
    dropping = False          # 命中 DROP 标签后的丢弃状态
    stopped = False

    def open_section(label: str, raw: str, page: int):
        return {"label": label, "raw_title": raw, "page_start": page, "page_end": page,
                "text": "", "page_marks": [], "keep": 1}

    def append_body(sec: dict, body: str, page: int):
        sec["text"] += ("\n" if sec["text"] else "") + body
        sec["page_marks"].append((max(0, len(sec["text"]) - len(body)), page))
        sec["page_end"] = max(sec["page_end"], page)

    for b in blocks:
        if stopped:
            break
        page, raw = b["page"], b["text"]
        lines = raw.split("\n")
        first = _clean_line(lines[0])
        is_large = b["size"] >= body_size * 1.06      # 该块字号明显大于正文

        label = classify_heading(first)
        body = None                                   # 本块中除去标题行后的正文
        if label is None:
            label, body = split_inline_heading(raw, is_large=is_large)
        if label is None:
            # ③ 未命中词典的长尾标题：只丢标题行，余下正文并入当前章节
            if is_large or (HEADING_NUM_RE.match(first) and b["size"] >= body_size * 0.98):
                rest = normalize_ws("\n".join(lines[1:]))
                if not rest:
                    continue                          # 整块就是标题行 → 丢弃
                body = rest
            else:
                body = normalize_ws(raw)
            if dropping:
                continue
            if cur is None:
                cur = open_section("Front Matter", "(document start)", page)
            append_body(cur, body, page)
            continue

        # ---- ①② 命中词典 / 内联切分得到的标题 ----
        if label == "STOP":
            stopped = True
            break
        if cur is not None:
            sections.append(cur)
            cur = None
        if label == "DROP":
            dropping = True
            continue
        dropping = False
        if keep_labels and label not in keep_labels:
            dropping = True
            continue
        cur = open_section(label, first, page)
        tail = body if body is not None else normalize_ws("\n".join(lines[1:]))
        if tail:
            append_body(cur, tail, page)

    if cur is not None and not stopped:
        sections.append(cur)

    # 过滤空章节与重复章节（同一标签只保留正文最长的一段）
    cleaned = []
    best: dict[str, int] = {}
    for s in sections:
        s["text"] = normalize_ws(s["text"])
        if len(s["text"]) < 200:
            continue
        cleaned.append(s)
    for s in cleaned:
        best[s["label"]] = max(best.get(s["label"], 0), len(s["text"]))
    result = []
    for s in cleaned:
        # 只保留该标签下最长的一段（避免目录/摘要重复段落干扰）
        if len(s["text"]) >= best[s["label"]]:
            best[s["label"]] = -1
            result.append(s)
    return result


# ============================================================================
# 章节内切片
# ============================================================================
def chunk_section(sec: dict, cfg: dict) -> list[dict]:
    """把章节正文切成若干片（句子级累积 + 重叠，绝不跨章节）。"""
    C = cfg["corpus"]
    target, overlap = int(C["target_tokens"]), float(C["overlap"])
    mn, mx = int(C["min_tokens"]), int(C["max_tokens"])
    text = sec["text"]
    if not text:
        return []

    sents = split_sentences(text)
    marks = sec.get("page_marks") or [(0, sec["page_start"])]

    def page_at(off: int) -> int:
        pg = marks[0][1]
        for m_off, m_pg in marks:
            if m_off <= off:
                pg = m_pg
            else:
                break
        return pg

    units: list[tuple[int, int, str]] = []   # (起始偏移, 结束偏移, 文本)
    pos = 0
    for s in sents:
        idx = text.find(s, pos)
        if idx < 0:
            idx = pos
        units.append((idx, idx + len(s), s))
        pos = idx + len(s)

    chunks: list[dict] = []
    i = 0
    while i < len(units):
        start_off = units[i][0]
        buf, toks, j = [], 0, i
        while j < len(units):
            t = est_tokens(units[j][2])
            if buf and toks + t > target:
                break
            buf.append(units[j][2])
            toks += t
            j += 1
            if toks >= target:
                break
        body = " ".join(buf).strip()
        end_off = units[j - 1][1] if j > i else units[i][1]

        # 过长则按硬上限二次切分
        if toks > mx:
            body = body[: int(len(body) * mx / max(toks, 1))].rstrip()
            toks = est_tokens(body)

        if toks < mn and chunks:
            # 过短：并入前一片（仍属同一章节）
            prev = chunks[-1]
            if est_tokens(prev["text"] + " " + body) <= mx:
                prev["text"] = (prev["text"] + " " + body).strip()
                prev["n_chars"] = len(prev["text"])
                prev["n_tokens_est"] = est_tokens(prev["text"])
                prev["char_end"] = end_off
                prev["page_end"] = page_at(end_off)
                i = j
                continue

        chunks.append({
            "text": body,
            "n_chars": len(body),
            "n_tokens_est": toks,
            "char_start": start_off,
            "char_end": end_off,
            "page_start": page_at(start_off),
            "page_end": page_at(end_off),
        })
        if j <= i:
            break
        # 重叠回退：按 token 比例回退若干句，保证上下文连续
        back, acc = 0, 0
        k = j - 1
        while k > i and acc < target * overlap:
            acc += est_tokens(units[k][2])
            k -= 1
            back += 1
        i = max(i + 1, j - back)
    return chunks


# ============================================================================
# 元数据（**PDF 优先**，文件名只作兜底）
# ============================================================================
from . import pdf_meta as PM  # noqa: E402

FNAME_RE = re.compile(r"^(?P<journal>.+?)[\-_](?P<year>(?:19|20)\d{2})[\-_](?P<doi>.+?)$")
DOI_RE = re.compile(r"\b(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)")


def _journal_from_filename(stem: str) -> str:
    m = re.match(r"^(?P<j>.+?)[\-_](?:19|20)\d{2}[\-_]", stem)
    return m.group("j").replace("_", " ").strip() if m else ""


def _year_from_filename(stem: str):
    m = re.search(r"[-_](19|20)\d{2}[-_]", stem)
    return int(m.group(0).strip("-_")) if m else None


def guess_metadata(path: str, blocks: list[dict], title_meta: str,
                   meta_pdf: dict | None = None) -> dict:
    """推断期刊/年份/DOI/ISSN/标题。

    **PDF 优先，文件名兜底**（用户明确要求）：源文件名有 46 字符上限，期刊名与 DOI
    都会被截断（如 "Applied Microbi-2017-…"、"Functional Ecolog-2022-10.1111_1.pdf"），
    因此优先采用 `pdf_meta.extract_pdf_meta` 从页眉/页脚/引文行/ISSN 提取的结果。

    优先级：
        journal : PDF（刊名索引/ISSN/引文行）→ 文件名
        doi     : PDF 正文/元数据 → 文件名
        year    : PDF 引文行/版权/出版行 → 文件名 → PDF 创建年份（最弱，可能是下载年）
        issn    : 仅来自 PDF（供 journals.py 做刊名规范化）
    返回的 `_src` 字段逐项记录来源，便于审计。
    """
    import os
    stem = re.sub(r"\.pdf$", "", os.path.basename(path), flags=re.I)
    pdf = meta_pdf or {}

    # ---- DOI ----
    doi_fn = ""
    m = FNAME_RE.match(stem)
    if m:
        raw = m.group("doi")
        doi_fn = raw.replace("_", "/") if raw.startswith("10.") else raw
    doi_pdf = pdf.get("doi") or ""
    if doi_pdf and (not doi_fn or len(doi_fn) < 16 or len(doi_pdf) >= len(doi_fn)):
        doi, doi_src = doi_pdf, (pdf.get("doi_src") or "pdf")
    elif doi_fn:
        doi, doi_src = doi_fn, "filename"
    else:
        doi, doi_src = None, ""

    # ---- 期刊 ----
    j_pdf, j_fn = pdf.get("journal") or "", _journal_from_filename(stem)
    if j_pdf:
        journal, journal_src = j_pdf, (pdf.get("journal_src") or "pdf")
    else:
        journal, journal_src = (j_fn or None), ("filename" if j_fn else "")

    # ---- 年份 ----
    y_pdf, y_pdf_src = pdf.get("year"), (pdf.get("year_src") or "")
    y_fn = _year_from_filename(stem)
    if y_pdf and y_pdf_src != "pdf:create":
        year, year_src = int(y_pdf), y_pdf_src
    elif y_fn:
        year, year_src = y_fn, "filename"
    else:
        year, year_src = (int(y_pdf) if y_pdf else None), y_pdf_src

    # ---- 标题 ----
    title = (title_meta or "").strip()
    if not title or len(title) < 12:
        cands = sorted(blocks[:8], key=lambda b: -b["size"])
        for b in cands:
            t = _clean_line(b["text"].split("\n")[0])
            if 20 <= len(t) <= 250 and not DOI_ONLY_RE.match(t) and b["size"] > 0:
                title = t
                break
    return {"journal": journal, "year": year, "doi": doi, "issn": pdf.get("issn", ""),
            "title": title or stem,
            "journal_src": journal_src, "year_src": year_src, "doi_src": doi_src,
            "meta_src": f"journal={journal_src or 'none'}; year={year_src or 'none'}; "
                        f"doi={doi_src or 'none'}; issn={pdf.get('issn_src') or 'none'}"}


def file_md5(path: str, chunk: int = 1 << 20) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


# ============================================================================
# 单篇处理
# ============================================================================
def _journal_index(cfg: dict):
    """载入刊名索引（配置缺失/文件不存在时返回空列表，抽取退化为纯启发式）。"""
    path = ((cfg.get("paths") or {}).get("journal_index")) or ""
    try:
        return PM.load_journal_index(path)
    except Exception:  # noqa: BLE001
        return []


def process_pdf(path: str, cfg: dict, max_pages: int = 0) -> dict | None:
    """解析一篇 PDF，返回 {doc, sections, chunks}；正文过短返回 None。"""
    import os
    idx = _journal_index(cfg)
    blocks_raw, info = extract_blocks(path, max_pages=max_pages, journal_index=idx)
    if not blocks_raw:
        return None
    blocks = filter_noise(blocks_raw)
    total = sum(b["n_chars"] for b in blocks)
    if total < int(cfg["corpus"]["min_doc_chars"]):
        return None

    sections = build_sections(blocks, info["body_size"], cfg["corpus"]["keep_labels"])
    if not sections:
        return None

    md5 = file_md5(path)
    meta = guess_metadata(path, blocks_raw, info["title_meta"], info.get("meta_pdf"))
    doc_id = md5                                    # 以内容 md5 作为主键，跨库稳定
    doc = {
        "doc_id": doc_id,
        "filename": os.path.basename(path),
        "journal": meta["journal"], "year": meta["year"], "doi": meta["doi"],
        "title": meta["title"], "issn": meta.get("issn") or "",
        "n_pages": info["n_pages"], "n_chars": total,
        "pdf_md5": md5, "pdf_size": os.path.getsize(path),
        "src_path": os.path.abspath(path),
        "meta_src": meta["meta_src"],
        "journal_src": meta["journal_src"], "year_src": meta["year_src"],
        "doi_src": meta["doi_src"],
    }

    out_sections, out_chunks = [], []
    for s_ord, sec in enumerate(sections, 1):
        pieces = chunk_section(sec, cfg)
        if not pieces:
            continue
        out_sections.append({**sec, "ord": s_ord})
        for c_ord, c in enumerate(pieces, 1):
            content_hash = hashlib.sha1(c["text"].encode("utf-8")).hexdigest()
            uid = hashlib.sha1((doc_id + content_hash).encode("utf-8")).hexdigest()
            out_chunks.append({
                **c, "chunk_uid": uid, "doc_id": doc_id, "label": sec["label"],
                "ord": c_ord, "sec_ord": s_ord, "content_hash": content_hash,
            })
    if not out_chunks:
        return None
    return {"doc": doc, "sections": out_sections, "chunks": out_chunks}


# ============================================================================
# 批量入库
# ============================================================================
def iter_pdfs(inputs: list[str], recursive: bool = True) -> list[str]:
    """把「文件或目录」列表展开为 PDF 文件路径列表。"""
    import os
    found: list[str] = []
    for p in inputs:
        if os.path.isfile(p) and p.lower().endswith(".pdf"):
            found.append(os.path.abspath(p))
        elif os.path.isdir(p):
            if recursive:
                for root, _dirs, files in os.walk(p):
                    for f in sorted(files):
                        if f.lower().endswith(".pdf"):
                            found.append(os.path.join(root, f))
            else:
                for f in sorted(os.listdir(p)):
                    fp = os.path.join(p, f)
                    if os.path.isfile(fp) and f.lower().endswith(".pdf"):
                        found.append(fp)
    return sorted(set(found))


def ingest(paths: list[str], cfg: dict, force: bool = False, dry_run: bool = False,
           limit: int = 0, quiet: bool = False) -> dict:
    """把 PDF 解析入库。默认增量：doc_id 已存在则跳过。"""
    from . import db as DB

    files = iter_pdfs(paths)
    if limit:
        files = files[:limit]
    report = {"files": len(files), "ingested": 0, "skipped": 0, "failed": 0,
              "sections": 0, "chunks": 0, "details": []}
    if not files:
        print(f"[入库] 在 {paths} 下未找到 PDF")
        return report

    con = DB.connect(cfg["paths"]["db"]) if not dry_run else None
    if con is not None:
        DB.init_schema(con)

    preview_rows = []
    for i, fp in enumerate(files, 1):
        import os
        name = os.path.basename(fp)
        try:
            parsed = process_pdf(fp, cfg)
        except Exception as e:  # noqa: BLE001
            report["failed"] += 1
            report["details"].append({"file": name, "status": f"失败：{type(e).__name__}: {e}"})
            if not quiet:
                print(f"  [{i}/{len(files)}] ✗ {name} —— {type(e).__name__}: {e}")
            continue
        if parsed is None:
            report["skipped"] += 1
            report["details"].append({"file": name, "status": "跳过（正文过短或无有效章节）"})
            if not quiet:
                print(f"  [{i}/{len(files)}] - {name} 跳过")
            continue

        doc = parsed["doc"]
        if dry_run:
            report["ingested"] += 1
            report["sections"] += len(parsed["sections"])
            report["chunks"] += len(parsed["chunks"])
            preview_rows.extend(
                {"filename": name, "journal": doc["journal"], "year": doc["year"],
                 "label": s["label"], "raw_title": s["raw_title"], "sec_chars": len(s["text"]),
                 "n_chunks": sum(1 for c in parsed["chunks"] if c["sec_ord"] == s["ord"])}
                for s in parsed["sections"])
            if not quiet:
                print(f"  [{i}/{len(files)}] ✓ {name}  {len(parsed['sections'])} 章节 / "
                      f"{len(parsed['chunks'])} 切片（dry-run，未写库）")
            continue

        exists = con.execute("SELECT doc_id FROM documents WHERE doc_id = ? OR filename = ?",
                             (doc["doc_id"], doc["filename"])).fetchone()
        if exists and not force:
            report["skipped"] += 1
            report["details"].append({"file": name, "status": "跳过（已入库）"})
            if not quiet:
                print(f"  [{i}/{len(files)}] = {name} 已入库，跳过")
            continue
        if exists and force:
            DB.delete_document(con, exists["doc_id"])

        DB.insert_document(con, doc)
        for sec in parsed["sections"]:
            sid = DB.insert_section(con, {
                "doc_id": doc["doc_id"], "ord": sec["ord"], "label": sec["label"],
                "raw_title": sec["raw_title"], "page_start": sec["page_start"],
                "page_end": sec["page_end"], "char_start": 0, "char_end": len(sec["text"]),
                "keep": 1,
            })
            for c in parsed["chunks"]:
                if c["sec_ord"] != sec["ord"]:
                    continue
                DB.insert_chunk(con, {**c, "section_id": sid})
        con.commit()
        report["ingested"] += 1
        report["sections"] += len(parsed["sections"])
        report["chunks"] += len(parsed["chunks"])
        report["details"].append({"file": name, "status": "已入库",
                                  "chunks": len(parsed["chunks"])})
        if not quiet:
            print(f"  [{i}/{len(files)}] ✓ {name}  {len(parsed['sections'])} 章节 / "
                  f"{len(parsed['chunks'])} 切片")

    if con is not None:
        con.close()

    if dry_run and preview_rows:
        import csv
        import os
        out = os.path.join(cfg["paths"]["out_dir"], "ingest_preview.csv")
        with open(out, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(preview_rows[0].keys()))
            w.writeheader()
            w.writerows(preview_rows)
        print(f"[入库] dry-run 复核表已导出：{out}")
    return report

"""SQLite 存储层：四张表 + 索引 + 可选 FTS5 全文索引。

表结构（与完整版项目保持一致的字段语义，便于互相迁移）：
    documents  一篇 PDF 一行（doc_id = PDF 内容的 md5，可跨库稳定引用）
    sections   章节（label 为规范化标签，keep=0 表示该章节不产出切片）
    chunks     切片（chunk_uid = sha1(doc_id + content_hash)，跨库重建稳定）
    embeddings 切片向量（chunk_pk + model 主键，vector 为 float32 原始字节）

FTS5 为外部内容虚表，若当前 Python 的 SQLite 未编译 FTS5 则自动跳过，不影响主链路。
"""
from __future__ import annotations

import array
import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    doc_id      TEXT PRIMARY KEY,
    filename    TEXT NOT NULL UNIQUE,
    journal     TEXT,
    year        INTEGER,
    doi         TEXT,
    title       TEXT,
    issn        TEXT,                  -- PDF 里印的 ISSN（期刊规范化的强锚点）
    journal_canon TEXT,                -- 规范刊名（由 ragcore/journals.py 回填）
    n_pages     INTEGER,
    n_chars     INTEGER,
    pdf_md5     TEXT,
    pdf_size    INTEGER,
    src_path    TEXT,
    meta_src    TEXT,
    journal_src TEXT,                  -- 逐字段来源（pdf / filename / …）
    year_src    TEXT,
    doi_src     TEXT,
    ingested_at TEXT DEFAULT (datetime('now'))
);

-- 期刊分级与指标（由 ragcore/journals.py 依据 journal_metrics.yaml 写入）
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

CREATE TABLE IF NOT EXISTS sections (
    section_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id      TEXT NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    ord         INTEGER NOT NULL,
    label       TEXT NOT NULL,
    raw_title   TEXT,
    page_start  INTEGER,
    page_end    INTEGER,
    char_start  INTEGER,
    char_end    INTEGER,
    keep        INTEGER NOT NULL DEFAULT 1,
    UNIQUE(doc_id, ord)
);

CREATE TABLE IF NOT EXISTS chunks (
    chunk_pk     INTEGER PRIMARY KEY,
    chunk_uid    TEXT NOT NULL UNIQUE,
    doc_id       TEXT NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    section_id   INTEGER REFERENCES sections(section_id) ON DELETE CASCADE,
    label        TEXT NOT NULL,
    ord          INTEGER NOT NULL,
    sec_ord      INTEGER NOT NULL,
    text         TEXT NOT NULL,
    n_chars      INTEGER NOT NULL,
    n_tokens_est INTEGER,
    kind         TEXT DEFAULT 'text',
    content_hash TEXT NOT NULL,
    page_start   INTEGER,
    page_end     INTEGER,
    UNIQUE(doc_id, content_hash)
);

CREATE TABLE IF NOT EXISTS embeddings (
    chunk_pk INTEGER NOT NULL REFERENCES chunks(chunk_pk) ON DELETE CASCADE,
    model    TEXT NOT NULL,
    dim      INTEGER NOT NULL,
    vector   BLOB NOT NULL,
    PRIMARY KEY (chunk_pk, model)
);

CREATE INDEX IF NOT EXISTS idx_chunk_doc   ON chunks(doc_id);
CREATE INDEX IF NOT EXISTS idx_chunk_label ON chunks(label);
CREATE INDEX IF NOT EXISTS idx_sec_doc     ON sections(doc_id);
CREATE INDEX IF NOT EXISTS idx_emb_model   ON embeddings(model);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text, label, content='chunks', content_rowid='chunk_pk',
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, text, label) VALUES (new.chunk_pk, new.text, new.label);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text, label)
    VALUES ('delete', old.chunk_pk, old.text, old.label);
END;
CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text, label)
    VALUES ('delete', old.chunk_pk, old.text, old.label);
    INSERT INTO chunks_fts(rowid, text, label) VALUES (new.chunk_pk, new.text, new.label);
END;
"""


def connect(db_path: str, create: bool = True) -> sqlite3.Connection:
    """打开数据库（WAL 模式，开启外键级联）。create=False 时要求库已存在。"""
    if not create and not os.path.isfile(db_path):
        raise FileNotFoundError(f"数据库不存在：{db_path}")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA foreign_keys = ON")   # 级联删除依赖此项，务必打开
    con.execute("PRAGMA synchronous = NORMAL")
    con.row_factory = sqlite3.Row
    return con


def init_schema(con: sqlite3.Connection) -> bool:
    """建表（幂等）。返回是否成功建立 FTS5 全文索引。"""
    con.executescript(SCHEMA)
    has_fts = True
    try:
        con.executescript(FTS_SCHEMA)
    except sqlite3.OperationalError as e:      # 未编译 FTS5 的 Python 会抛错
        has_fts = False
        print(f"[提示] 当前 SQLite 不支持 FTS5（{e}），已跳过全文索引（不影响检索）")
    con.commit()
    return has_fts


# ---------------------------------------------------------------------------
# 读写辅助
# ---------------------------------------------------------------------------
def delete_document(con: sqlite3.Connection, doc_id: str) -> None:
    """删除一篇文献及其章节/切片（外键级联）。"""
    con.execute("DELETE FROM documents WHERE doc_id = ?", (doc_id,))


def doc_exists(con: sqlite3.Connection, pdf_md5: str) -> bool:
    return con.execute("SELECT 1 FROM documents WHERE pdf_md5 = ? LIMIT 1",
                       (pdf_md5,)).fetchone() is not None


def insert_document(con: sqlite3.Connection, doc: dict) -> None:
    con.execute(
        """INSERT INTO documents(doc_id, filename, journal, year, doi, title, issn,
                                 journal_canon, n_pages, n_chars, pdf_md5, pdf_size,
                                 src_path, meta_src, journal_src, year_src, doi_src)
           VALUES (:doc_id, :filename, :journal, :year, :doi, :title,
                   :issn, :journal_canon, :n_pages, :n_chars, :pdf_md5, :pdf_size,
                   :src_path, :meta_src, :journal_src, :year_src, :doi_src)""",
        {"journal_canon": "", "issn": "", "journal_src": "", "year_src": "", "doi_src": "",
         **doc})


def insert_section(con: sqlite3.Connection, sec: dict) -> int:
    cur = con.execute(
        """INSERT INTO sections(doc_id, ord, label, raw_title, page_start, page_end,
                                char_start, char_end, keep)
           VALUES (:doc_id, :ord, :label, :raw_title, :page_start, :page_end,
                   :char_start, :char_end, :keep)""", sec)
    return int(cur.lastrowid)


def insert_chunk(con: sqlite3.Connection, ch: dict) -> None:
    con.execute(
        """INSERT OR IGNORE INTO chunks(chunk_uid, doc_id, section_id, label, ord, sec_ord,
                                        text, n_chars, n_tokens_est, kind, content_hash,
                                        page_start, page_end)
           VALUES (:chunk_uid, :doc_id, :section_id, :label, :ord, :sec_ord,
                   :text, :n_chars, :n_tokens_est, 'text', :content_hash,
                   :page_start, :page_end)""", ch)


def stats(con: sqlite3.Connection) -> dict:
    """库内规模统计。"""
    def one(sql, *a):
        r = con.execute(sql, a).fetchone()
        return r[0] if r and r[0] is not None else 0

    return {
        "documents": one("SELECT count(*) FROM documents"),
        "sections": one("SELECT count(*) FROM sections"),
        "chunks": one("SELECT count(*) FROM chunks"),
        "text_chunks": one("SELECT count(*) FROM chunks WHERE kind='text' AND trim(text)<>''"),
        "embeddings": one("SELECT count(*) FROM embeddings"),
        "chunks_with_vec": one(
            "SELECT count(*) FROM chunks c WHERE c.kind='text' AND trim(c.text)<>'' "
            "AND EXISTS (SELECT 1 FROM embeddings e WHERE e.chunk_pk=c.chunk_pk)"),
        "orphan_embeddings": one(
            "SELECT count(*) FROM embeddings e WHERE NOT EXISTS "
            "(SELECT 1 FROM chunks c WHERE c.chunk_pk=e.chunk_pk)"),
        "journal_count": one("SELECT count(DISTINCT journal) FROM documents"),
        "year_range": "{}~{}".format(one("SELECT min(year) FROM documents"),
                                     one("SELECT max(year) FROM documents")),
    }


# ---------------------------------------------------------------------------
# 向量读写（float32 原始字节 <-> numpy）
# ---------------------------------------------------------------------------
def pack_vector(vec) -> bytes:
    """numpy float32 一维数组 → 原始字节。"""
    return array.array("f", vec.astype("float32").tolist()).tobytes()


def unpack_vector(blob: bytes):
    """原始字节 → numpy float32 数组（延迟导入 numpy，便于无 numpy 环境做纯 SQL 检查）。"""
    import numpy as np
    return np.frombuffer(blob, dtype=np.float32)


def put_embedding(con: sqlite3.Connection, chunk_pk: int, model: str, vec) -> None:
    v = vec.astype("float32")
    con.execute(
        "INSERT OR REPLACE INTO embeddings(chunk_pk, model, dim, vector) VALUES (?,?,?,?)",
        (chunk_pk, model, int(v.shape[0]), pack_vector(v)))


def cleanup_orphans(con: sqlite3.Connection) -> int:
    """清理 chunk_pk 已不存在的向量（切片重建后可能残留）。返回清理条数。"""
    cur = con.execute(
        "DELETE FROM embeddings WHERE NOT EXISTS "
        "(SELECT 1 FROM chunks c WHERE c.chunk_pk = embeddings.chunk_pk)")
    con.commit()
    return cur.rowcount or 0

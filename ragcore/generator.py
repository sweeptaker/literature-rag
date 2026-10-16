"""生成层：把 top-k 证据拼成带编号的 prompt，调用 DeepSeek 生成带 [n] 引用的答案。

设计：
* system 提示严格约束「只依据证据作答、逐句标 [n]、证据不足必须声明为推测」；
* 若检索侧做了查询规范化，会把「原文 → 检索用语句」一并交给模型，
  避免模型对错别字做出与检索不一致的理解；
* 未配置 API Key 时降级为「只输出检索证据」，链路不中断；
* 429/5xx 指数退避重试；支持流式（SSE 风格增量）。
"""
from __future__ import annotations

import json
import time

_SYSTEM = (
    "你是一名严谨的科研助理。请**只依据**用户提供的证据回答问题，规则如下：\n"
    "1. 每一个具体论断后必须标注证据编号，形如 [1]、[2,3]，编号对应证据块序号；\n"
    "2. 不得引入证据之外的知识，不得编造数据、结论或文献；\n"
    "3. 若证据不足以回答，必须明确写出「现有证据不足以回答该问题」，"
    "并说明缺少哪方面的证据；\n"
    "4. 若给出的是推断而非证据直接支持的内容，必须显式标注为「推测」；\n"
    "5. 若证据之间存在冲突，需分别列出并说明分歧所在；\n"
    "6. 用中文作答，结构清晰（可分小标题或分点），不要复述证据原文。"
)


def build_prompt(question: str, candidates: list[dict], cfg: dict,
                 search_query: str | None = None) -> list[dict]:
    """拼装 messages。每条证据形如：
        【证据 1】期刊=Ecology | 年份=2012 | 章节=Discussion | 页=7-8
        <切片原文（截断到 snippet_chars）>
    """
    lim = int(cfg["output"].get("snippet_chars", 1200))
    blocks = []
    for c in candidates:
        pg = f"{c.get('page_start')}-{c.get('page_end')}" if c.get("page_start") else "?"
        # 期刊名 + 档位 + 影响因子一起给出，便于模型区分证据的分量
        jn = c.get("journal_name") or c.get("journal") or "未知"
        tier = f"{c['journal_tier']}档" if c.get("journal_tier") else ""
        jq = c.get("journal_quartile") or ""
        jif = f"IF {c['journal_jif']}" if c.get("journal_jif") else ""
        tag = "，".join(x for x in (tier, jq, jif) if x)
        head = (f"【证据 {c['rank']}】期刊={jn}" + (f"（{tag}）" if tag else "")
                + f" | 年份={c.get('year') or '未知'} | 章节={c.get('label')} | 页={pg}")
        txt = (c.get("text") or "").strip()
        if len(txt) > lim:
            txt = txt[:lim] + " …"
        blocks.append(f"{head}\n{txt}")

    user = f"提问：{question}\n"
    if search_query and search_query.strip() and search_query.strip() != question.strip():
        user += (f"（说明：检索系统对提问做了规范化处理，实际用于检索的语句是："
                 f"「{search_query}」。若原提问存在笔误，请按规范化后的语义理解。）\n")
    user += "\n以下是检索到的证据：\n\n" + "\n\n".join(blocks)
    user += "\n\n请依据上述证据作答，每个论断标注 [编号]。"
    return [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}]


def _iter_once(text: str):
    """把单段文本包装成生成器（用于流式接口的降级输出）。"""
    yield text


def call_deepseek(messages: list[dict], cfg: dict, stream: bool = False):
    """调用 DeepSeek（OpenAI 兼容接口）。

    stream=False → 返回 {"text", "usage", "ok"}
    stream=True  → 返回逐段产出文本的生成器

    注意：本函数体内**不能**出现顶层 yield，否则整个函数会变成生成器；
    流式分支把生成逻辑放在内部函数 `_gen()` 中。
    """
    import requests
    from .config import api_key
    key = api_key(cfg)
    if not key:
        msg = ("[未配置 API Key] 已跳过生成阶段，请设置环境变量 "
               f"{cfg['llm']['api_key_env']} 后重试。以上为检索到的证据。")
        return _iter_once(msg) if stream else {"text": msg, "usage": {}, "ok": False}

    body = {
        "model": cfg["llm"]["model"],
        "messages": messages,
        "temperature": cfg["llm"]["temperature"],
        "max_tokens": cfg["llm"]["max_tokens"],
        "stream": bool(stream),
    }
    url = cfg["llm"]["base_url"].rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    timeout = cfg["llm"]["timeout"]
    retries = int(cfg["llm"].get("retries", 3))

    last_err = None
    for attempt in range(retries):
        try:
            r = requests.post(url, headers=headers, json=body, timeout=timeout,
                              stream=bool(stream))
            if r.status_code in (429, 500, 502, 503, 504):
                raise RuntimeError(f"HTTP {r.status_code}")
            r.raise_for_status()

            if not stream:
                data = r.json()
                return {"text": data["choices"][0]["message"]["content"],
                        "usage": data.get("usage", {}), "ok": True}

            def _gen():
                for line in r.iter_lines(decode_unicode=True):
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        obj = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    delta = (obj.get("choices") or [{}])[0].get("delta") or {}
                    piece = delta.get("content")
                    if piece:
                        yield piece
            return _gen()
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt < retries - 1:
                time.sleep(1.5 * (2 ** attempt))
    raise RuntimeError(f"调用生成模型失败（已重试 {retries} 次）：{last_err}")


def generate(question: str, candidates: list[dict], cfg: dict,
             search_query: str | None = None) -> dict:
    """检索结果 → 答案。返回 {answer, usage, elapsed_ms, ok}。"""
    t0 = time.perf_counter()
    messages = build_prompt(question, candidates, cfg, search_query=search_query)
    try:
        out = call_deepseek(messages, cfg, stream=False)
        return {"answer": out["text"], "usage": out.get("usage", {}), "ok": out.get("ok", True),
                "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1)}
    except Exception as e:  # noqa: BLE001
        return {"answer": f"[生成失败] {type(e).__name__}: {e}",
                "usage": {}, "ok": False,
                "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1)}

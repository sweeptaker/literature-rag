"""Web 演示服务：仅用 Python 标准库，无需额外 Web 框架。

路由：
    GET  /                 演示页（web/index.html，与接口同源托管）
    GET  /api/status       语料规模、模型与配置摘要
    POST /api/ask          一次性返回检索与生成结果
    POST /api/ask_stream   以 ndjson 增量推送各阶段（意图→粗筛→细筛→重排→答案）

安全：默认只绑定 127.0.0.1；跨源请求显式放行（便于直接以 file:// 打开页面时使用）。
"""
from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import retriever as FR

RES = None
LOCK = threading.Lock()          # 模型推理串行化（MPS/CUDA 非线程安全）
STATE = {"device": None, "loaded_at": None}


def preheat(cfg: dict) -> None:
    """预热：载入向量矩阵与嵌入模型（重排模型按需惰性加载以省内存）。"""
    global RES
    t0 = time.perf_counter()
    RES = FR.load_resources(cfg)
    if cfg["paths"].get("embedding_dir") or True:
        try:
            from .embedder import load_embedder
            _m, dev = load_embedder(cfg)
            STATE["device"] = dev
        except Exception as e:  # noqa: BLE001
            STATE["device"] = f"未加载（{type(e).__name__}）"
    STATE["loaded_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[预热] {RES['n_chunks']} 切片 / {RES['dim']} 维（device={STATE['device']}，"
          f"{time.perf_counter()-t0:.1f}s）", flush=True)


def cand_json(c: dict, with_text: bool = True) -> dict:
    keep = ("rank", "recall_rank", "chunk_uid", "doc_id", "filename", "journal", "year",
            "doi", "label", "page_start", "page_end", "semantic", "journal_s", "recency_s",
            "claim_s", "section_s", "fusion", "rerank_s", "final",
            "journal_canon", "journal_name", "journal_tier", "journal_tier_name",
            "journal_jif", "journal_quartile",
            "evidence_class", "confidence")
    d = {k: c.get(k) for k in keep}
    if with_text:
        d["text"] = c.get("text", "")
    return d


def _json_safe(obj):
    return json.loads(json.dumps(obj, ensure_ascii=False, default=str))


def make_handler(cfg: dict):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass

        def _cors(self):
            # 直接以 file:// 打开页面时页面源为 null，必须显式放行
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")

        def _send(self, code, body: bytes, ctype="application/json; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self._cors()
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(_json_safe(obj), ensure_ascii=False).encode("utf-8"))

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}") if n else {}

        # ---------------- 路由 ----------------
        def do_OPTIONS(self):
            self.send_response(204)
            self._cors()
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            if self.path.startswith("/api/status"):
                return self._json(self.status())
            if self.path in ("/", "/index.html"):
                try:
                    with open(cfg["paths"]["web_index"], "rb") as f:
                        return self._send(200, f.read(), "text/html; charset=utf-8")
                except FileNotFoundError:
                    return self._send(404, b"index.html not found",
                                      "text/plain; charset=utf-8")
            return self._json({"error": "not found"}, 404)

        def do_POST(self):
            if self.path == "/api/ask":
                return self.handle_ask()
            if self.path == "/api/ask_stream":
                return self.handle_ask_stream()
            return self._json({"error": "not found"}, 404)

        # ---------------- 业务 ----------------
        def status(self):
            r = RES or {}
            return {
                "corpus_chunks": r.get("n_chunks"), "dim": r.get("dim"),
                "db": cfg["paths"]["db"],
                "embedding": cfg["models"]["embedding"],
                "reranker": cfg["models"]["reranker"],
                "reranker_ready": bool(cfg["paths"].get("reranker_dir")),
                "device": STATE["device"] or cfg["models"]["device"],
                "loaded_at": STATE["loaded_at"],
                "intent_n": cfg["intent"]["n_by_intent"],
                "topk": cfg["output"]["topk"],
                "rerank_pool": cfg["rerank"]["pool"], "rerank_alpha": cfg["rerank"]["alpha"],
                "fusion_weights": cfg["fusion"]["weights"],
                "query_norm": bool(cfg["query_norm"].get("enabled", True)),
                "api_key": bool(os.environ.get(cfg["llm"]["api_key_env"], "")
                                or cfg["llm"].get("api_key")),
            }

        def _opts(self, payload):
            o = payload.get("options") or {}
            return {
                "intent_override": o.get("intent") or None,
                "n_override": int(o["n_recall"]) if o.get("n_recall") else None,
                "topk_override": int(o["topk"]) if o.get("topk") else None,
                "use_rerank": (False if o.get("no_rerank") else None),
                "use_dedup": (False if o.get("no_dedup") else None),
                "use_norm": (False if o.get("no_norm") else None),
            }

        def handle_ask(self):
            global RES
            try:
                payload = self._body()
                q = (payload.get("question") or "").strip()
                if not q:
                    return self._json({"error": "question 为空"}, 400)
                with LOCK:
                    if RES is None:
                        preheat(cfg)
                    t0 = time.perf_counter()
                    out = FR.retrieve(q, cfg, RES, **self._opts(payload))
                    from .generator import generate
                    gen = generate(q, out["candidates"], cfg,
                                   search_query=out["search_query"])
                return self._json({
                    "question": q, "search_query": out["search_query"],
                    "normalize": out["normalize"], "intent": out["intent"],
                    "intent_confidence": out["intent_confidence"],
                    "intent_reason": out["intent_reason"], "intent_source": out["intent_source"],
                    "n_recall": out["n_recall"], "corpus_chunks": out["corpus_chunks"],
                    "stages": out["stages"],
                    "candidates": [cand_json(c) for c in out["candidates"]],
                    "recall_preview": [cand_json(RES["metas"][p], with_text=False)
                                       for p in out["recall_pks"][:30]],
                    "answer": gen["answer"], "usage": gen["usage"],
                    "gen_elapsed_ms": gen["elapsed_ms"],
                    "total_elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
                })
            except Exception as e:  # noqa: BLE001
                return self._json({"error": f"{type(e).__name__}: {e}"}, 500)

        def handle_ask_stream(self):
            global RES
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Transfer-Encoding", "chunked")
            self._cors()
            self.end_headers()

            def send(obj):
                data = (json.dumps(_json_safe(obj), ensure_ascii=False) + "\n").encode("utf-8")
                self.wfile.write(f"{len(data):X}\r\n".encode() + data + b"\r\n")
                self.wfile.flush()

            try:
                payload = self._body()
                q = (payload.get("question") or "").strip()
                if not q:
                    send({"type": "error", "message": "question 为空"})
                    return
                with LOCK:
                    if RES is None:
                        preheat(cfg)
                    send({"type": "start", "question": q, "corpus_chunks": RES["n_chunks"]})
                    out = FR.retrieve(q, cfg, RES,
                                      on_stage=lambda s: send({"type": "stage", **s}),
                                      **self._opts(payload))
                    send({"type": "intent", "intent": out["intent"],
                          "confidence": out["intent_confidence"],
                          "reason": out["intent_reason"], "source": out["intent_source"],
                          "n_recall": out["n_recall"]})
                    send({"type": "recall_preview",
                          "items": [cand_json(RES["metas"][p], with_text=False)
                                    for p in out["recall_pks"][:30]]})
                    send({"type": "candidates",
                          "items": [cand_json(c) for c in out["candidates"]],
                          "weights": cfg["fusion"]["weights"]})
                    from .generator import build_prompt, call_deepseek
                    for piece in call_deepseek(
                            build_prompt(q, out["candidates"], cfg,
                                         search_query=out["search_query"]), cfg, stream=True):
                        send({"type": "delta", "text": piece})
                    send({"type": "done", "elapsed_ms": out["elapsed_ms"]})
            except Exception as e:  # noqa: BLE001
                try:
                    send({"type": "error", "message": f"{type(e).__name__}: {e}"})
                except Exception:  # noqa: BLE001
                    pass
            finally:
                try:
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                except Exception:  # noqa: BLE001
                    pass

    return Handler


def serve(cfg: dict, host: str = "", port: int = 0, preheat_model: bool = True) -> None:
    host = host or cfg["web"]["host"]
    port = int(port or cfg["web"]["port"])
    if preheat_model:
        preheat(cfg)
    srv = ThreadingHTTPServer((host, port), make_handler(cfg))
    url = f"http://{host}:{port}/"
    print(f"[服务] {url}   （Ctrl-C 停止）", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[服务] 已停止")
    finally:
        srv.server_close()

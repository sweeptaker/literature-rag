#!/usr/bin/env python3
"""ragmini 一键安装器（纯 Python，Windows / macOS / Linux 通用）。

它做四件事：
    1. 检查 Python 版本（要求 ≥ 3.9）
    2. 在包内创建虚拟环境 .venv（不污染系统 Python）
    3. 安装 Python 依赖（优先国内镜像，失败自动回退官方源）
    4. 从 HuggingFace 镜像下载模型权重到 模型/ 目录（可直接写盘、不需重命名权限）

用法：
    python install.py                       # 完整安装
    python install.py --dry-run             # 只打印计划，不执行
    python install.py --check-only          # 只检查现状
    python install.py --no-venv             # 装到当前解释器（不建 venv）
    python install.py --skip-models         # 不下载模型（依赖仍安装）
    python install.py --model-source D:\\模型   # 复用已有的模型目录，跳过下载
    python install.py --pypi-mirror https://pypi.org/simple
    python install.py --hf-endpoint https://huggingface.co
    python install.py --proxy http://127.0.0.1:7890
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
VENV_DIR = os.path.join(ROOT, ".venv")
REQ_FILE = os.path.join(ROOT, "requirements.txt")
INFO_FILE = os.path.join(ROOT, "环境信息.json")

DEFAULT_PYPI_MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"
DEFAULT_HF_ENDPOINT = "https://hf-mirror.com"
EP_HF = "https://huggingface.co"
MODELS = ["BAAI/bge-m3", "BAAI/bge-reranker-v2-m3"]

MIN_PY = (3, 9)


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def opt(name, default=None):
    if name in sys.argv:
        i = sys.argv.index(name)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


def flag(name):
    return name in sys.argv


def say(msg=""):
    print(msg, flush=True)


def human(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"


def venv_python(venv_dir=None):
    venv_dir = venv_dir or VENV_DIR
    cand = (os.path.join(venv_dir, "Scripts", "python.exe") if sys.platform == "win32"
            else os.path.join(venv_dir, "bin", "python"))
    return cand if os.path.isfile(cand) else ""


# ---------------------------------------------------------------------------
# 步骤 1：Python 版本
# ---------------------------------------------------------------------------
def step_python() -> bool:
    v = sys.version_info
    say(f"[1/4] Python 版本：{v.major}.{v.minor}.{v.micro}  （{sys.executable}）")
    if (v.major, v.minor) < MIN_PY:
        say(f"      ✗ 版本过低，需要 Python {MIN_PY[0]}.{MIN_PY[1]} 或更高。")
        say("        Windows：到 python.org 下载 3.11/3.12 安装包，安装时勾选 "
            "“Add python.exe to PATH”")
        say("        macOS  ：brew install python@3.12  或到 python.org 下载安装包")
        return False
    say("      ✓ 版本满足要求")
    return True


# ---------------------------------------------------------------------------
# 步骤 2：虚拟环境
# ---------------------------------------------------------------------------
def step_venv(dry: bool) -> str:
    """创建/复用虚拟环境，返回应使用的解释器路径。"""
    if flag("--no-venv"):
        say("[2/4] 跳过虚拟环境（--no-venv），将安装到当前解释器")
        return sys.executable

    say(f"[2/4] 虚拟环境：{VENV_DIR}")
    existing = venv_python()
    if existing:
        say(f"      ✓ 已存在，直接复用（{existing}）")
        return existing
    if dry:
        say("      (dry-run) 将执行：python -m venv .venv")
        return venv_python() or sys.executable

    try:
        os.makedirs(os.path.dirname(VENV_DIR), exist_ok=True)
        rc = subprocess.call([sys.executable, "-m", "venv", VENV_DIR])
    except Exception as e:  # noqa: BLE001
        say(f"      ✗ 创建虚拟环境异常：{type(e).__name__}: {e}")
        rc = 1
    if rc != 0 or not venv_python():
        say("      ✗ 创建虚拟环境失败。")
        say("        常见原因：Debian/Ubuntu 缺少 venv 组件 → sudo apt install python3-venv")
        say("        也可改用系统环境安装：python install.py --no-venv")
        return ""
    say(f"      ✓ 创建完成：{venv_python()}")
    return venv_python()


def _pip_env():
    """pip/下载用的环境变量（UTF-8 输出、可选代理）。"""
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    proxy = opt("--proxy", "")
    if proxy:
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            env[k] = proxy
    return env


# ---------------------------------------------------------------------------
# 步骤 3：Python 依赖
# ---------------------------------------------------------------------------
def step_deps(py: str, dry: bool) -> bool:
    if not py:
        return False
    pkgs = opt("--packages", "")
    req = REQ_FILE if not pkgs else ""
    if pkgs:
        say(f"[3/4] 安装依赖（自定义包：{pkgs}）")
        targets = pkgs.split()
    else:
        say("[3/4] 安装 Python 依赖（torch / transformers / sentence-transformers / pymupdf 等）")
        if not os.path.isfile(req):
            say(f"      ✗ 未找到 {REQ_FILE}")
            return False
        targets = ["-r", req]

    if dry:
        say(f"      (dry-run) 将执行：{py} -m pip install {' '.join(targets)}")
        return True

    env = _pip_env()
    say(f"      使用解释器：{py}")
    # 先升级打包工具（失败不致命）
    subprocess.call([py, "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"],
                    env=env)

    mirrors = [opt("--pypi-mirror", "") or DEFAULT_PYPI_MIRROR, "https://pypi.org/simple"]
    for i, mir in enumerate(mirrors):
        say(f"      尝试镜像源：{mir}" + ("（国内镜像，通常更快）" if i == 0 else "（官方源）"))
        cmd = [py, "-m", "pip", "install", "--no-input", "-i", mir] + targets
        rc = subprocess.call(cmd, env=env)
        if rc == 0:
            say("      ✓ 依赖安装完成")
            return True
        say(f"      ! 该源安装失败（退出码 {rc}），换下一个源重试…")
    say("      ✗ 依赖安装失败。可尝试：")
    say("        * 指定其他镜像：python install.py --pypi-mirror https://mirrors.aliyun.com/pypi/simple")
    say("        * 走代理：      python install.py --proxy http://127.0.0.1:7890")
    say("        * 手工安装：    .venv/bin/python -m pip install -r requirements.txt")
    return False


# ---------------------------------------------------------------------------
# 步骤 4：模型权重
# ---------------------------------------------------------------------------
BAD_TOKENS = ("onnx", "openvino", "tflite", "tf_model", "flax", "msgpack", ".h5", ".ot",
              "rust_model", "coreml", ".mlmodel", "cuda", ".gguf", "tf2onnx", "imgs/",
              "resize/", ".ckpt", ".log", ".zip", ".tar", "optimizer", "training_args",
              ".ggml", ".pt", ".pth")
GOOD_EXT = (".json", ".model", ".txt", ".safetensors", ".bin", ".tiktoken")


def http_get(url: str, timeout: int = 60, no_proxy: bool = False):
    """带代理诊断的 GET。no_proxy=True 时绕过环境变量里的代理。"""
    req = urllib.request.Request(url, headers={"User-Agent": "ragmini-installer"})
    if no_proxy:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return opener.open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout)


def http_get_robust(url: str, timeout: int = 60):
    """先按环境（可能走代理）请求；失败则绕过代理再试一次。"""
    try:
        return http_get(url, timeout=timeout)
    except Exception as first:  # noqa: BLE001
        try:
            return http_get(url, timeout=timeout, no_proxy=True)
        except Exception as second:  # noqa: BLE001
            raise RuntimeError(f"{type(first).__name__}: {first}；"
                               f"绕过代理后同样失败（{type(second).__name__}: {second}）")


def show_network_env() -> None:
    """打印代理配置，便于排查下载失败。"""
    keys = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "NO_PROXY", "no_proxy")
    got = {k: os.environ[k] for k in keys if os.environ.get(k)}
    if got:
        say(f"      检测到代理设置：{got}")
        say("      （若代理不可用，可加 --proxy 指定正确代理，或临时 unset 这些变量重试）")
    else:
        say("      未检测到代理设置（直连）")


def list_repo_files(repo: str, endpoint: str) -> list[str]:
    """列出 HF 仓库文件（走镜像 API）。"""
    url = f"{endpoint.rstrip('/')}/api/models/{repo}"
    with http_get_robust(url, timeout=30) as r:
        data = json.loads(r.read().decode("utf-8"))
    return [s["rfilename"] for s in data.get("siblings", [])]


def pick_files(files: list[str]) -> list[str]:
    """挑选必需文件：过滤推理框架变体，优先 safetensors 而非 pytorch_model.bin。"""
    keep = []
    for f in files:
        low = f.lower()
        if any(b in low for b in BAD_TOKENS):
            continue
        if not low.endswith(GOOD_EXT):
            continue
        if f.startswith(".") or "/" == f[0]:
            continue
        keep.append(f)
    if any(f.endswith(".safetensors") for f in keep):
        keep = [f for f in keep if not f.endswith("pytorch_model.bin")]
    # 排序：小文件优先，权重最后（便于用户看到进度先跑起来）
    keep.sort(key=lambda f: (f.endswith((".bin", ".safetensors")), f))
    return keep


def download_file(url: str, dest: str, label: str = "") -> bool:
    """流式下载到目标路径（只创建文件，不做重命名，兼容受限文件系统）。"""
    tmp_dir = os.path.dirname(dest)
    os.makedirs(tmp_dir, exist_ok=True)
    try:
        with http_get_robust(url) as r:
            total = int(r.headers.get("Content-Length") or 0)
            already = os.path.getsize(dest) if os.path.isfile(dest) else 0
            if total and already == total:
                say(f"        = {label or os.path.basename(dest)} 已存在（{human(total)}），跳过")
                return True
            t0, done, last_pct = time.time(), 0, -5
            mode = "ab" if (already and total and already < total) else "wb"
            done = already if mode == "ab" else 0
            with open(dest, mode) as f:
                while True:
                    buf = r.read(1 << 20)
                    if not buf:
                        break
                    f.write(buf)
                    done += len(buf)
                    if total:
                        pct = int(done * 100 / total)
                        if pct >= last_pct + 5:
                            last_pct = pct
                            sp = done / max(time.time() - t0, 1e-6)
                            say(f"        {label or os.path.basename(dest)} "
                                f"{pct:3d}%  {human(done)}/{human(total)}  {human(sp)}/s")
            say(f"        ✓ {label or os.path.basename(dest)} 完成（{human(done)}，"
                f"{time.time()-t0:.0f}s）")
        return True
    except Exception as e:  # noqa: BLE001
        say(f"        ✗ 下载失败：{type(e).__name__}: {e}")
        say(f"          可重跑本脚本续传，或手动下载：{url}")
        return False


def step_models(dry: bool) -> bool:
    src = opt("--model-source", "")

    if flag("--skip-models"):
        say("[4/4] 跳过模型下载（--skip-models）")
        return True

    if src:
        say(f"[4/4] 复用已有模型目录：{src}")
        if not os.path.isdir(src):
            say("      ✗ 目录不存在。")
            return False
        found = [d for d in os.listdir(src)
                 if os.path.isfile(os.path.join(src, d, "config.json"))]
        if not found:
            say(f"      ✗ 该目录下没有找到含 config.json 的模型子目录。")
            say(f"        期望形如：{src}/bge-m3/config.json")
            return False
        say(f"      ✓ 找到模型：{', '.join(found)}")
        _write_config_model_dir(src, dry)
        return True

    # 端点列表：优先镜像，失败自动回退官方源
    primary = opt("--hf-endpoint", "") or DEFAULT_HF_ENDPOINT
    endpoints = [primary] + ([EP_HF] if primary != EP_HF else [])
    say(f"[4/4] 下载模型权重（端点 {primary}）")
    show_network_env()
    model_dir = os.path.join(ROOT, "模型")
    all_ok = True
    for repo in MODELS:
        short = repo.split("/")[-1]
        target = os.path.join(model_dir, short)
        if os.path.isfile(os.path.join(target, "config.json")) and not flag("--force-models"):
            say(f"      ✓ {repo} 已存在，跳过（如需重下加 --force-models）")
            continue
        say(f"      · {repo} → {target}")
        files, used_ep, err = None, "", ""
        for ep in endpoints:
            try:
                files = pick_files(list_repo_files(repo, ep))
                used_ep = ep
                break
            except Exception as e:  # noqa: BLE001
                err = f"{type(e).__name__}: {e}"
                say(f"        端点 {ep} 不可用（{err}），尝试下一个…")
        if not files:
            say(f"      ✗ 无法获取文件清单（{err}）")
            say("        排查建议：")
            say("          * 换端点：python install.py --hf-endpoint https://huggingface.co")
            say("          * 走代理：python install.py --proxy http://127.0.0.1:7890")
            say("          * 已有权重：python install.py --model-source /路径/到/模型目录 --skip-models")
            say("          * 手动下载后放到："
                f"{target}/（需含 config.json、modules.json 与权重文件）")
            all_ok = False
            continue
        if used_ep != primary:
            say(f"        （改用可用端点：{used_ep}）")
        if dry:
            say(f"        (dry-run) 将下载 {len(files)} 个文件：")
            for f in files:
                say(f"          - {f}")
            continue
        for f in files:
            url = f"{used_ep.rstrip('/')}/{repo}/resolve/main/{f}"
            dest = os.path.join(target, f.replace("/", os.sep))
            if not download_file(url, dest, label=f"{short}/{f}"):
                all_ok = False
        # 校验最小可用性
        miss = [n for n in ("config.json", "modules.json")
                if not os.path.isfile(os.path.join(target, n))]
        has_weight = any(n.endswith((".safetensors", ".bin"))
                         for _dp, _dn, fn in os.walk(target) for n in fn)
        if miss or not has_weight:
            say(f"      ✗ 该模型文件不完整：缺少 {miss or '权重文件'}")
            all_ok = False
        else:
            say(f"      ✓ {repo} 就绪")
    return all_ok


def _write_config_model_dir(src: str, dry: bool) -> None:
    """把 --model-source 写入 config.yaml 的 paths.model_dir。"""
    path = os.path.join(ROOT, "config.yaml")
    if dry:
        say(f"      (dry-run) 将把 paths.model_dir 写入 {path}")
        return
    import re
    if os.path.isfile(path):
        text = open(path, encoding="utf-8").read()
        if re.search(r"^\s*model_dir\s*:", text, re.M):
            text = re.sub(r"^(\s*model_dir\s*:).*$", rf"\1 {json.dumps(src)}", text, flags=re.M)
        else:
            text += f"\npaths:\n  model_dir: {json.dumps(src)}\n"
    else:
        text = f"paths:\n  model_dir: {json.dumps(src)}\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    say(f"      ✓ 已在 config.yaml 中设置 paths.model_dir = {src}")


# ---------------------------------------------------------------------------
# 汇总：环境信息.json
# ---------------------------------------------------------------------------
def write_info(py: str, models_ok: bool, deps_ok: bool) -> None:
    pkgs = []
    if py and os.path.isfile(py):
        try:
            out = subprocess.check_output([py, "-m", "pip", "freeze"],
                                          env=_pip_env()).decode("utf-8", "replace")
            pkgs = [l.strip() for l in out.splitlines() if l.strip()][:200]
        except Exception:  # noqa: BLE001
            pass
    info = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "platform": sys.platform,
        "system_python": sys.executable,
        "venv_python": py,
        "deps_ok": deps_ok,
        "models_ok": models_ok,
        "model_dir": os.path.join(ROOT, "模型"),
        "packages": pkgs,
        "next_steps": [
            "python run.py doctor                      # 环境自检",
            "python run.py selftest                    # 合成论文全链路自检",
            "python run.py ingest <你的PDF目录>          # 切片入库",
            "python run.py embed                       # 向量化",
            "python run.py ask \"你的问题\"                # 检索并生成答案",
            "python run.py serve                       # 网页演示 http://127.0.0.1:8765",
        ],
    }
    with open(INFO_FILE, "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=2)
    say(f"\n[信息] 已写出 {INFO_FILE}")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> int:
    dry = flag("--dry-run")
    check = flag("--check-only")
    say("=" * 74)
    say(" ragmini 安装器 —— 文献 RAG 最小可迁移版")
    say(f" 包目录：{ROOT}")
    say(f" 平台  ：{sys.platform}   Python：{sys.version.split()[0]}")
    if dry:
        say(" 模式  ：dry-run（只打印计划，不实际安装）")
    if check:
        say(" 模式  ：check-only（只检查现状）")
    say("=" * 74)

    if check:
        py = venv_python() or sys.executable
        say(f"\n虚拟环境解释器：{py}"
            + ("" if venv_python() else "   （包内 .venv 尚未创建）"))
        for repo in MODELS:
            d = os.path.join(ROOT, "模型", repo.split("/")[-1])
            mark = "✓" if os.path.isfile(os.path.join(d, "config.json")) else "-"
            say(f"  {mark} 模型 {repo}：{d}")
        say("\n如需安装，运行：python install.py")
        return 0

    if not step_python():
        return 1
    py = step_venv(dry)
    if not py:
        return 1
    deps_ok = step_deps(py, dry)
    models_ok = step_models(dry) if deps_ok or flag("--skip-models") else False
    if not deps_ok:
        say("\n[结果] 依赖未装好，已跳过模型下载。请解决依赖问题后重跑：python install.py")
        return 1

    if not dry:
        write_info(py, models_ok, deps_ok)

    say("\n" + "=" * 74)
    say(" 安装完成" if models_ok else " 安装部分完成（模型未全部就绪）")
    say("=" * 74)
    interpreter = VENV_DIR if os.path.isdir(VENV_DIR) and not flag("--no-venv") else ""
    say(f" 运行入口：python run.py <子命令>"
        + (f"   （会自动使用包内环境 .venv）" if interpreter else ""))
    say("")
    say(" 建议按顺序执行：")
    say("   python run.py doctor                  # ① 环境自检")
    say("   python run.py selftest                # ② 合成论文全链路自检（推荐先跑，验证装好了）")
    say("   python run.py ingest <你的PDF目录>      # ③ 把你的 PDF 切片入库")
    say("   python run.py embed                   # ④ 生成向量")
    say("   python run.py ask \"你的问题\"            # ⑤ 命令行问答")
    say("   python run.py serve                   # ⑥ 网页演示 http://127.0.0.1:8765")
    say("")
    say(" 生成答案需要 DeepSeek API Key（只检索不配置也能用）：")
    say("   Windows:  set DEEPSEEK_API_KEY=sk-xxxx      （当前窗口有效）")
    say("             setx DEEPSEEK_API_KEY sk-xxxx     （永久）")
    say("   macOS  :  export DEEPSEEK_API_KEY=sk-xxxx")
    say("=" * 74)
    return 0 if models_ok else 2


if __name__ == "__main__":
    sys.exit(main())

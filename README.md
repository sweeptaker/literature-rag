# ragmini —— 最小可迁移的文献 RAG

把整个 `ragmini` 目录**复制或打包发给任何人**，对方装好环境后即可完成：

> PDF → 章节切分/切片 → SQLite 存储 → bge-m3 向量 → 意图识别 → 粗筛召回 → 五因子融合 → 文献去重 →
> 证据分级 → 神经重排 → Top-K 证据 → DeepSeek 生成带 `[n]` 引用的答案

全部路径都相对于包根目录计算，**不含任何机器相关的绝对路径**，因此可整体搬迁、打包、分发。

---

## 一、前置条件

| 项目 | 要求 | 说明 |
|---|---|---|
| Python | **3.9 或更高**（推荐 3.11 / 3.12） | Windows 安装时务必勾选 “Add python.exe to PATH” |
| 磁盘 | 约 **6 GB** 空闲 | Python 依赖约 1 GB + 模型权重约 4.6 GB |
| 内存 | 建议 8 GB 以上 | 首次加载模型峰值约 2~3 GB |
| 网络 | 首次安装需要 | 走国内镜像（PyPI 清华 / hf-mirror），也可以离线准备模型 |
| DeepSeek API Key | **可选** | 不配置也能用：只输出检索到的证据，不生成答案 |

> 平台入口分成两套：`windows/` 与 `macos/`，详见 `README_Windows.md`、`README_macOS.md`。
> 核心代码是同一套纯 Python 实现（与操作系统无关），两套入口只负责环境激活、编码与浏览器打开等平台差异。

---

## 二、三步走

### 第 1 步：安装环境

```
Windows：双击  windows\1_install.bat
macOS  ：双击  macos/1_安装环境.command
```

等价的手工命令（任选其一，纯 Python，跨平台一致）：

```bash
python install.py
```

安装器会依次：检查 Python 版本 → 在包内创建虚拟环境 `.venv`（不污染系统）→
用清华镜像安装依赖（失败自动回退官方源）→ 从 hf-mirror 流式下载两个模型权重 →
写出 `环境信息.json` 与后续操作提示。常用开关：

| 开关 | 作用 |
|---|---|
| `--dry-run` | 只打印计划，不实际安装 |
| `--check-only` | 只检查现状 |
| `--no-venv` | 装到当前解释器（不建 venv） |
| `--skip-models` | 不下载模型（依赖仍安装） |
| `--model-source D:\模型` | 复用已有模型目录，跳过下载并写入配置 |
| `--pypi-mirror URL` | 指定 PyPI 镜像 |
| `--hf-endpoint URL` | 指定模型下载端点（默认 `https://hf-mirror.com`，失败自动回退官方源） |
| `--proxy URL` | 通过代理下载（如 `http://127.0.0.1:7890`） |

> **已有模型权重**（例如你之前下载过 bge-m3）时，加 `--model-source` 最省时间：
> 目录结构需为 `<该目录>/bge-m3/config.json`、`<该目录>/bge-reranker-v2-m3/config.json`。

### 第 2 步：自检（强烈建议先跑）

```bash
python run.py selftest
```

它会**现场生成 3 篇合成论文**（英文、含 Abstract/Introduction/Methods/Results/Discussion/Conclusion），
跑通「切片 → SQLite → 向量 → 检索」全链路，并断言「每篇的目标文献都能被检索到」。
**这一步不需要你自己的 PDF 和数据，用于验证环境装好了。** 输出末尾会给出 `全部通过` 或具体失败项。

### 第 3 步：用自己的文献

```bash
python run.py ingest <你的PDF目录>     # ① 切片入库（支持目录递归、也支持单个文件）
python run.py embed                    # ② 生成向量（增量，已编码的跳过）
python run.py ask "你的问题"            # ③ 命令行问答
python run.py serve                    # ④ 或启动网页演示（127.0.0.1:8765）
```

配置 DeepSeek Key（生成答案需要；不配则只返回证据）：

```
Windows： set DEEPSEEK_API_KEY=sk-xxxx          （当前窗口有效）
          setx DEEPSEEK_API_KEY sk-xxxx         （永久，需重开窗口）
macOS  ： export DEEPSEEK_API_KEY=sk-xxxx        （可写入 ~/.zshrc 永久生效）
```

平台脚本编号对应（两套入口功能一致）：

| 步骤 | Windows | macOS |
|---|---|---|
| 安装环境 | `windows\1_install.bat` | `macos/1_安装环境.command` |
| PDF 入库 | `windows\2_ingest.bat`（可把 PDF 文件夹拖到该文件上） | `macos/2_入库PDF.command`（可拖文件夹到终端窗口） |
| 生成向量 | `windows\3_embed.bat` | `macos/3_生成向量.command` |
| 提问 | `windows\4_ask.bat` | `macos/4_提问.command` |
| 网页演示 | `windows\5_web.bat` | `macos/5_网页演示.command` |

---

## 三、网页演示

```bash
python run.py serve            # 默认 http://127.0.0.1:8765
python run.py serve --port 8888
```

页面展示全链路每个阶段的中间态：查询规范化 → 意图识别 → 粗筛候选 → 各阶段耗时条 →
Top-K 证据（可展开原文，含期刊/年份/章节/页码/chunk_uid）→ 带 `[n]` 的答案（点击可高亮对应证据）。

- **推荐访问方式**：`http://127.0.0.1:8765/`（页面与接口同源托管，最稳妥）。
- 也可以直接双击 `web/index.html`（`file://` 方式）：页面会自动把接口指向
  `http://127.0.0.1:8765`（可用 `?port=8888` 或 `?api=` 覆盖），服务端已放行 CORS。
  **前提是服务已在运行**，否则页面会给出启动命令提示而不是晦涩的网络报错。
- 服务只监听 `127.0.0.1`，不对局域网暴露。

---

## 四、目录结构

```
ragmini/
├─ install.py              一键安装器（纯 Python，跨平台）
├─ run.py                  统一入口，自动切换到包内 .venv
├─ requirements.txt        Python 依赖清单
├─ config.yaml             配置（相对路径；缺键回退内置默认值）
├─ 修复脚本权限.py          macOS 下修复 .command 可执行位（Windows 可忽略）
├─ ragcore/                核心代码（与操作系统无关）
│   ├─ config.py           路径解析 + 缺省补全 + 运行时环境注入（临时目录/镜像/线程数）
│   ├─ db.py               SQLite 表结构与读写（documents/sections/chunks/embeddings + FTS5）
│   ├─ pdf2chunks.py       PDF 抽取 → 噪声过滤 → 章节切分 → 章节内切片
│   ├─ embedder.py         bge-m3 编码（本地权重优先）、向量入库、矩阵缓存导出
│   ├─ retriever.py        查询规范化 / 意图识别 / 粗筛 / 融合 / 去重 / 分级 / 重排 / 选取
│   ├─ generator.py        证据拼装 prompt + DeepSeek 调用（超时/退避/流式）
│   ├─ webapp.py           标准库 HTTP 服务（含流式 ndjson 与 CORS）
│   └─ cli.py              子命令实现（含 selftest 合成论文自检）
├─ web/index.html          单文件演示页
├─ windows/                第一套入口：Windows（.bat，内容为 ASCII 以避免 cmd 编码问题）
├─ macos/                  第二套入口：macOS（.command，自动打开浏览器）
├─ pdf/                    放置待入库 PDF（可自行改名或改用 ingest 指定路径）
├─ 数据库/rag.sqlite         SQLite 数据库（首次运行自动创建）
├─ 中间文件/                向量矩阵缓存、导出 CSV、临时目录
└─ 模型/                    bge-m3 与 bge-reranker-v2-m3 权重
```

首次运行会自动创建 `数据库/`、`中间文件/`、`模型/`、`pdf/`。

---

## 五、命令速查

| 命令 | 说明 |
|---|---|
| `python run.py doctor` | 环境自检（依赖、模型、数据库、API Key） |
| `python run.py init` | 生成默认 `config.yaml` |
| `python run.py ingest <路径...>` | PDF 切片入库（入库后自动富集期刊指标，可用 `--no-enrich` 跳过）。`--force` 重建；`--dry-run` 只导出复核表；`--limit N` 只处理前 N 篇 |
| `python run.py enrich` | 单独重跑期刊富集：JCR 分区 / 影响因子 → 1–6 档。`--no-llm` 只用人工表（离线/省 API） |
| `python run.py embed` | 生成向量。`--force` 全部重算；`--batch N`；`--device cpu/mps/cuda` |
| `python run.py ask "问题"` | 检索 + 生成。`--no-rerank`、`--no-dedup`、`--no-norm`、`--topk N`、`--intent L`、`--n-recall N`、`--json`、`--dump out.csv` |
| `python run.py stats` | 库内规模与标签/期刊分布（含档位与 JIF） |
| `python run.py serve` | 网页演示（`--port`、`--host`、`--no-preheat`） |
| `python run.py selftest` | 合成论文端到端自检 |

**推荐顺序**：`doctor` → `selftest` → `ingest` → `embed` → `ask` / `serve`。

---

## 六、检索链路与参数（`config.yaml`）

```
提问
 ├─ ⓪ 查询规范化   错别字纠正 + 术语统一（仅用于检索；原始提问仍用于作答）
 ├─ ① 意图识别     LLM 分类 concept / evidence / judgement → 召回宽度 N = 150 / 100 / 120
 ├─ ② 粗筛         bge-m3 编码（L2 归一）→ 全库余弦 → Top-N
 ├─ ③ 五因子融合   final = 0.722·semantic + 0.103·journal + 0.082·recency + 0.072·claim + 0.021·section
 ├─ ④ 文献去重     按文档只保留最高分切片（一篇文献一个证据）
 ├─ ⑤ 证据分级     正则判定 direct_support / counterevidence / indirect_support / related_background + HIGH/MEDIUM/LOW
 ├─ ⑥ 神经重排     bge-reranker-v2-m3 对 pool 条重打分，final = 0.6·重排分 + 0.4·融合分
 └─ ⑦ Top-K 选取   取 topk（默认 10）条证据
 └─ ⑧ 生成         编号证据块 → DeepSeek → 带 [n] 引用、证据不足须声明为推测
```

- **期刊因子按「档位」打分**（`journal` 权重 0.103）：档位取自 `journals` 表，
  1 档 1.00 / 2 档 0.85 / 3 档 0.70 / 4 档 0.50 / 5 档 0.30 / 6 档 0.10 / 未知 0.45。
- **元数据优先从 PDF 抽取**：入库时按「ISSN 锚点 > 结构化引文行 > 页眉页脚 > 文件名」的
  可靠度顺序提取期刊名 / 年份 / DOI，避免源文件名 46 字符上限造成的截断
  （110 篇实测规范刊名全部正确）。缩写刊名经 `journal_index.tsv` 映射为规范全名。
- 语料切分：target 350 token / overlap 0.15 / min 120 / max 700，**绝不跨章节拼接**；
  只保留 8 类目标标签（Abstract / Introduction / Methods / Results / Results and Discussion /
  Discussion / Conclusion / Front Matter），参考文献、致谢、关键词等整段丢弃。
- 所有阈值都在 `config.yaml` 中，未列出的键回退 `ragcore/config.py` 的 `DEFAULTS`。
- 临时目录、HF 镜像、线程数等运行时设置由 `config.py` 自动注入，可用环境变量覆盖：
  `RAGMINI_ROOT` / `RAGMINI_PDF_DIR` / `RAGMINI_DB` / `RAGMINI_OUT_DIR` / `RAGMINI_MODEL_DIR` /
  `RAGMINI_HF_ENDPOINT` / `RAGMINI_NORELAUNCH` / `RAGMINI_JOURNAL_METRICS` / `RAGMINI_JOURNAL_INDEX`。
  其中 `journal_metrics.yaml` 与 `journal_index.tsv` 按**包根**解析（不随 `RAGMINI_ROOT` 漂移），
  否则用临时根做测试时导出的索引会落到包外。

---

## 七、常见问题

**1. `python` 命令找不到（Windows）**
安装 Python 时未勾选 “Add python.exe to PATH”。重新安装并勾选，或使用 `py -3 install.py`。

**2. 依赖装不上 / 很慢**
`python install.py --pypi-mirror https://mirrors.aliyun.com/pypi/simple`，或走代理
`python install.py --proxy http://127.0.0.1:7890`；安装器会打印检测到的代理变量。

**3. 模型下载失败**
换端点 `--hf-endpoint https://huggingface.co`；已有权重则用 `--model-source <目录> --skip-models`；
也可手工下载 `BAAI/bge-m3`、`BAAI/bge-reranker-v2-m3` 的全部文件后按
`模型/bge-m3/`、`模型/bge-reranker-v2-m3/` 放置（需含 `config.json`、`modules.json` 与权重文件）。

**4. 首次提问很慢**
第一次需要从磁盘加载 bge-m3（约 2.3 GB，20~30 秒）与重排模型。用 `serve` 模式时启动即预热，
之后每次检索约 1 秒级。

**5. 没有 DeepSeek Key 能用吗？**
能。检索链路完整可用，`ask` 会打印 Top 证据并提示已跳过生成；网页端同样只展示证据。
若要换其他 OpenAI 兼容服务，改 `config.yaml` 的 `llm.base_url` 与 `llm.model` 即可。

**6. macOS 双击 `.command` 没反应**
先运行 `python 修复脚本权限.py` 补回可执行位；若仍被 Gatekeeper 拦截，右键 → 打开，
或执行 `xattr -dr com.apple.quarantine macos`。终极兜底：`bash "macos/1_安装环境.command"`。

**7. Windows 提示路径过长**
把整个包放到较短的 ASCII 路径下（如 `C:\ragmini`），避免中文与深层目录。

**8. 想扩展/重跑**
`ingest` 增量（同内容 PDF 跳过），`embed` 只编码新增切片；
改了切分规则后按顺序执行 `ingest --force` → `embed`（**不要并发执行这两个命令**）。

---

## 八、已验证的运行记录

本包在 macOS（Apple M2 Pro / MPS）上完成如下实测：

| 项目 | 结果 |
|---|---|
| 合成论文自检 | 3 篇 → 14 切片 → **3/3 目标文献均检索到第 1 名**，去重与可溯源断言通过 |
| 真实 PDF 入库 | 3 篇期刊论文 → 17 章节 / 74 切片；Abstract/Introduction/Methods/Results/Discussion 判定正确，Acknowledgements/Funding/Keywords 正确丢弃 |
| 向量化 | 74 条 × 1024 维，MPS 耗时 8.4 s，矩阵缓存导出成功 |
| 完整问答 | 规范化（识别并纠正「Phytoplankton blooms ⇒ 浮游植物藻华」）→ intent concept 0.90 / N=150 → recall 74 → fusion → dedup 3 → grade → rerank → select 3 → 生成带 `[n]` 引用答案 |
| 生成质量 | 答案主动区分「证据直接支持 / 证据不足 / 推测」，并指出与问题无关的证据不计入 |
| 网页接口 | `/api/status` 正常；`/api/ask_stream` 逐阶段推送；`file://` 场景跨源预检与请求均通过 |
| 安装器 | venv 创建 + 清华镜像装包 + `run.py` 自动切换解释器均验证通过 |
| 期刊指标富集 | `ingest` 后自动执行，Top 证据与 `stats` 均显示档位 / JIF；未配 API Key 时 AI 兜底自动跳过 |

单次提问耗时参考（3 篇小语料、模型已加载）：规范化 1.4 s、意图 1.1 s、粗筛 0.9 s、
融合 10 ms、去重 <1 ms、分级 1 ms、重排 3.5 s（含模型加载）、生成 5.5 s。

# macOS 使用说明（ragmini）

本文件只讲 macOS 特有的部分；功能与参数说明见上级目录 `README.md`。

---

## 一、准备 Python 3

先确认是否已有 python3（终端执行）：

```bash
python3 --version        # 需要 3.9 或更高
```

没有的话，两种安装方式：

- **官网安装包**：<https://www.python.org/downloads/macos/>（选 3.11 或 3.12 的 macOS 64-bit universal2）
- **Homebrew**：`brew install python@3.12`

> Apple 芯片（M1/M2/M3/M4）与 Intel 芯片都可以，安装器会自动选择 CPU / MPS 设备。
> 系统自带的 `/usr/bin/python3` 版本较旧且受系统保护，建议用官网包或 Homebrew 的 Python。

---

## 二、放置与安装

1. 把 `ragmini` 文件夹放到任意位置（例如 `~/ragmini` 或桌面）。
2. 首次从网络/聊天工具/压缩包得到该文件夹时，**可执行位可能丢失**，先修复：

   ```bash
   cd ~/ragmini
   python3 修复脚本权限.py
   ```

   输出 `已设置可执行位` 即可双击运行 `.command`。

3. 双击 **`macos/1_安装环境.command`**。它会在终端窗口中：

   - 检查 `python3` 是否存在
   - 在包内创建虚拟环境 `.venv`、用清华镜像安装依赖
   - 从 hf-mirror 下载两个模型权重（共约 4.6 GB）
   - 打印下一步操作指引

   等价的手工命令（推荐，效果完全相同且不受 Gatekeeper 限制）：

   ```bash
   cd ~/ragmini
   python3 install.py
   ```

---

## 三、日常使用（双击即可）

| 操作 | 双击文件 | 说明 |
|---|---|---|
| ① 安装环境 | `macos/1_安装环境.command` | 只需做一次 |
| ② PDF 入库 | `macos/2_入库PDF.command` | 也可把 PDF 文件夹拖到终端窗口后回车；或 `bash "macos/2_入库PDF.command" /路径/到/PDF目录`。入库成功后会**自动富集期刊指标**（档位 / JCR 分区 / 影响因子） |
| ③ 生成向量 | `macos/3_生成向量.command` | 入库后必须执行；增量，已编码的跳过 |
| ④ 提问 | `macos/4_提问.command` | 双击后在终端输入问题 |
| ⑤ 网页演示 | `macos/5_网页演示.command` | 服务就绪后自动打开浏览器 |
| ⑥ 期刊指标富集（可选） | `macos/6_期刊富集.command` | 入库时已自动跑过一次；仅在改了 `journal_metrics.yaml` 或想手动重跑时使用 |

命令行方式：

```bash
cd ~/ragmini
./.venv/bin/python run.py doctor          # 或直接 python3 run.py（会自动切到 .venv）
./.venv/bin/python run.py selftest
./.venv/bin/python run.py ingest ~/Documents/pdfs
./.venv/bin/python run.py embed
./.venv/bin/python run.py enrich         # 重跑期刊指标富集
./.venv/bin/python run.py enrich --no-llm   # 只用人工复核表，不调 AI
./.venv/bin/python run.py ask "火灾是否会驱动森林向非森林生态系统转变？"
./.venv/bin/python run.py serve
```

> `run.py` 检测到包内存在 `.venv` 时会**自动切换**过去，所以日常直接 `python3 run.py ...` 也可以。

### 期刊分级（1–6 档）

`ingest` 结束时会对语料中出现过的期刊逐条求 **JCR 分区**与**影响因子（JIF）**，并折算成 1–6 档：

| 档位 | 判定条件 | 融合权重得分 |
|---|---|---|
| 1 档 | Nature / Science / Cell 正刊 | 1.00 |
| 2 档 | Nature 子刊、Science Advances、Cell 子刊（含 Trends、npj 系列） | 0.85 |
| 3 档 | JCR Q1 且 JIF ≥ 8（领域内顶刊） | 0.70 |
| 4 档 | JCR Q2，或 Q1 中段（JIF 4–8） | 0.50 |
| 5 档 | JCR Q3，或 JIF 1–4 | 0.30 |
| 6 档 | JCR Q4，或 JIF < 1 | 0.10 |
| 未知 | 无可靠数据源（不猜） | 0.45 |

- **优先使用人工复核的检索结果**（`journal_metrics.yaml`，45 条），表内缺失的才走 AI 兜底；
  AI 必须给出可核验依据（`require_evidence`），拿不到依据就保持「未知档」，绝不硬填。
- 档位由代码**确定性计算**，AI 只提供 `JIF` / `quartile` 两个原始数字，避免幻觉。
- 被引次数（citation）因子**已彻底删除**，五因子权重已归一化到 Σ = 1.0000。
- 没有配置 `DEEPSEEK_API_KEY` 时自动跳过 AI 兜底，人工表部分照常生效；
  检查结果：`python3 run.py stats`。

---

## 四、配置 DeepSeek API Key

```bash
export DEEPSEEK_API_KEY=sk-你的密钥          # 当前终端有效
echo 'export DEEPSEEK_API_KEY=sk-你的密钥' >> ~/.zshrc   # 永久生效
```

不配置也可以用：`ask` 会打印检索到的 Top 证据并说明已跳过生成阶段。

---

## 五、常见问题（macOS）

**1. 双击 .command 提示“无法打开，因为来自身份不明的开发者”**
Gatekeeper 拦截。三种处理方式（任选其一）：

- 右键点击该文件 → **打开** → 在弹窗中再次点「打开」（只需一次）
- 终端执行：`xattr -dr com.apple.quarantine macos`
- 直接用 `bash` 运行，绕过可执行位与 Gatekeeper：`bash "macos/1_安装环境.command"`

**2. 双击没反应 / 打开的是文本编辑器**
可执行位丢失。运行 `python3 修复脚本权限.py`，或改用 `bash "macos/1_安装环境.command"`。

**3. `python3 --version` 报 command not found**
未安装 Python 3。按第一节安装；Homebrew 用户若提示找不到命令，需
`eval "$(/opt/homebrew/bin/brew shellenv)"` 或重开终端。

**4. pip 安装很慢 / 超时**
```bash
python3 install.py --pypi-mirror https://mirrors.aliyun.com/pypi/simple
```
本机有代理（Clash / Surge 等）时：
```bash
python3 install.py --proxy http://127.0.0.1:7890
```
安装器会先打印检测到的代理环境变量（`HTTP_PROXY`/`HTTPS_PROXY`）便于排查。

**5. 模型下载失败（hf-mirror 不可达）**
```bash
python3 install.py --hf-endpoint https://huggingface.co
python3 install.py --skip-models
```
已有权重可直接复用：
```bash
python3 install.py --model-source ~/模型 --skip-models
```
（该目录下需有 `bge-m3/config.json` 与 `bge-reranker-v2-m3/config.json`）

**6. 风扇狂转 / 电脑发热**
嵌入与重排默认走 **MPS**（Apple GPU）加速。可在 `config.yaml` 里把
`models.device` 设为 `cpu`，或把 `models.batch_size` 降到 8；
`config.py` 已默认设置 `OMP_NUM_THREADS=8`，避免吃满整机 CPU。

**7. 首次提问/首次启动很慢（20~60 秒）**
正常现象：需从磁盘加载 bge-m3（2.3 GB）。`serve` 模式在启动时预热，之后每次检索约 1 秒级。
CLI 每次运行都会重新加载，属于预期行为。

**8. 端口被占用（`Address already in use`）**
```bash
python3 run.py serve --port 8888
```
或先结束已有服务：`pkill -f "run.py serve"`。

**9. 沙箱/公司管控环境下系统临时目录不可写**
`config.py` 已自动把临时目录指向包内 `中间文件/tmp`，并把 HuggingFace 缓存指向
`中间文件/hf_cache`，一般无需手工干预。

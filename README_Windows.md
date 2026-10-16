# Windows 使用说明（ragmini）

本文件只讲 Windows 特有的部分；功能与参数说明见上级目录 `README.md`。

---

## 一、准备 Python

1. 打开 <https://www.python.org/downloads/>，下载 **Python 3.11 或 3.12** 的 Windows 安装包（64-bit）。
2. 安装时**务必勾选**：
   - ☑ **Add python.exe to PATH**（最关键，否则命令行找不到 `python`）
   - ☑ Install launcher for all users
3. 安装完成后，按 `Win + R` 输入 `cmd`，执行 `python --version`，应输出 `Python 3.1x.x`。

> 若公司电脑无法安装 Python，可下载 **Windows 免安装 embeddable 包** 后手工配置 PATH；
> 或者向管理员申请安装权限。`ragmini` 本身不写注册表、不需要管理员权限。

---

## 二、放置与安装

1. 把 `ragmini` 整个文件夹放到**较短的纯英文路径**下，例如：

   ```
   C:\ragmini
   ```

   > 不建议放在 `C:\Users\你的名字\OneDrive\桌面\...` 这类含中文、空格、超长路径的位置：
   > 深度学习依赖（torch）与模型权重文件路径过长时可能触发 Windows 的 260 字符限制。

2. 双击 **`windows\1_install.bat`**。它会：

   - 设置 UTF-8 输出编码（`chcp 65001` + `PYTHONUTF8=1`，中文提示不乱码）
   - 切到包根目录，调用 `python install.py`
   - 在包内创建虚拟环境 `.venv`、安装依赖、下载两个模型（共约 4.6 GB）

   > 脚本内的提示文字为纯 ASCII，这是刻意设计：`cmd.exe` 的代码页会让含中文的 `.bat`
   > 内容出现乱码或语法错误；详细中文信息由 Python 打印。

3. 安装结束若提示 **`安装部分完成（模型未全部就绪）`**，多半是网络问题，见第五节。

---

## 三、日常使用（双击即可）

| 操作 | 双击文件 | 说明 |
|---|---|---|
| ① 安装环境 | `windows\1_install.bat` | 只需做一次；换机器/重装后重做 |
| ② PDF 入库 | `windows\2_ingest.bat` | **可把装有 PDF 的文件夹直接拖到这个 .bat 上**；直接双击则读取 `pdf\` 目录。入库成功后会**自动富集期刊指标**（档位 / JCR 分区 / 影响因子） |
| ③ 生成向量 | `windows\3_embed.bat` | 入库后必须执行；之后新增 PDF 只需重跑，已编码的会跳过 |
| ④ 提问 | `windows\4_ask.bat` | 双击后在窗口里输入问题回车 |
| ⑤ 网页演示 | `windows\5_web.bat` | 自动新开一个服务窗口并打开浏览器 |
| ⑥ 期刊指标富集（可选） | `windows\6_enrich.bat` | 入库时已自动跑过一次；仅在改了 `journal_metrics.yaml` 或想手动重跑时使用 |

命令行方式（在 `C:\ragmini` 下打开 `cmd`）：

```bat
python run.py doctor
python run.py selftest
python run.py ingest "D:\我的文献"
python run.py embed
python run.py enrich                                  :: 重跑期刊指标富集
python run.py enrich --no-llm                         :: 只用人工复核表，不调 AI
python run.py ask "干旱如何影响土壤微生物群落组装？"
python run.py serve --port 8765
```

> 若包内已存在 `.venv`，`run.py` 会**自动切换**到该虚拟环境运行，无需手工 `activate`。

### 期刊指标富集（可选了解）

`ingest` 成功后会自动对语料期刊求 JCR 分区与影响因子，并折算成 1–6 档写入 `journals` 表，
检索时按档位打分；`python run.py stats` 可查看各刊档位。分档规则与数据来源见
`journal_metrics.yaml` 的注释；手工重跑：`python run.py enrich`（`--no-llm` 只用人工复核表）。

---

## 四、配置 DeepSeek API Key

```
:: 当前窗口有效
set DEEPSEEK_API_KEY=sk-你的密钥

:: 永久生效（需重开 cmd / 重新双击脚本）
setx DEEPSEEK_API_KEY sk-你的密钥
```

不配置也可以用：`ask` 会打印检索到的 Top 证据并说明已跳过生成阶段。

---

## 五、常见问题（Windows）

**1. 双击 .bat 闪一下就没了**
说明脚本提前退出且没走到 `pause`。用 `cmd` 打开包目录手工执行
`python install.py` 查看完整报错。最常见原因是 Python 未加入 PATH。

**2. `where python` 找不到，但确实装了 Python**
重装时勾选 “Add python.exe to PATH”，或把
`C:\Users\<用户名>\AppData\Local\Programs\Python\Python312\` 与同目录下的 `Scripts\`
加入系统 PATH。

**3. pip 安装很慢或超时**
```bat
python install.py --pypi-mirror https://mirrors.aliyun.com/pypi/simple
```
若本机有代理软件（Clash / v2ray 等）：
```bat
python install.py --proxy http://127.0.0.1:7890
```
安装器会先打印检测到的代理环境变量，便于排查。

**4. 模型下载失败（hf-mirror 不可达）**
依次尝试：
```bat
python install.py --hf-endpoint https://huggingface.co
python install.py --skip-models        :: 不下载，先装依赖
```
若你已有权重，直接复用：
```bat
python install.py --model-source D:\模型 --skip-models
```
（该目录下需有 `bge-m3\config.json` 与 `bge-reranker-v2-m3\config.json`）

**5. 中文问题在窗口里显示乱码**
`.bat` 已设置 `chcp 65001` 与 `PYTHONUTF8=1`；若仍乱码，请在
「设置 → 时间和语言 → 语言和区域 → 管理语言设置 → 更改系统区域设置」中
确认已勾选 “Beta：使用 Unicode UTF-8 提供全球语言支持”，或改用 Windows Terminal。

**6. 没有独立显卡也能跑吗？**
可以。torch 的 Windows 默认 wheel 是 **CPU 版本**，bge-m3 在 CPU 上编码 74 条切片约需十几秒，
语料上千篇时建议分批执行 `embed`。
若要启用 NVIDIA GPU，需按 <https://pytorch.org/> 的指引重装 CUDA 版 torch。

**7. 内存占用高 / 报内存不足**
把 `config.yaml` 的 `models.batch_size` 从 16 降到 8 或 4；或先不下载重排模型
（`--skip-models` 后手工只放 `bge-m3`），此时会跳过神经重排，检索仍可用。

**8. 防火墙提示是否允许网络访问**
`python run.py serve` 只绑定 `127.0.0.1`（本机），拒绝外部访问是正常且安全的；
可以在提示中直接选择「取消」。

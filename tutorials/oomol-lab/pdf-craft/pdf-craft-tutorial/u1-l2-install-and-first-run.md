# 环境安装与第一次转换

> 所属单元：u1 初识 pdf-craft（第 2 讲，beginner）
> 前置讲义：[u1-l1 pdf-craft 是什么：项目定位与能力地图](u1-l1-project-overview.md)

## 1. 本讲目标

学完本讲，你应该能够：

1. 正确安装 pdf-craft：知道什么时候用标准安装 `pip install pdf-craft`，什么时候需要 `pdf-craft[local]` 扩展。
2. 理解并配置系统级依赖 Poppler，明白它为什么不能用 pip 安装。
3. 独立编写并运行第一个 `convert.py` 脚本：调用 `PDFCraft.convert_pdf_to_markdown` 把一个 PDF 转成 Markdown，记录转换耗时，并保留、观察中间产物目录的结构。

承接上一讲：u1-l1 已经建立了「OCR 负责认字、LLM 负责翻译」「提取结果落成 DocumentPackage 中间包」的整体印象。本讲把印象落到可运行的环境上——先让库在你的机器上跑起来，后面的源码精读才有实验场地。

## 2. 前置知识

- **pip 与「extras」语法**：`pip install "pdf-craft[local]"` 中的 `[local]` 叫可选依赖组（extra）。它表示「在标准安装之外，额外安装一组可选依赖」。引号在部分 shell（如 zsh）中是必需的，否则方括号会被 shell 解释。
- **虚拟环境**：Python 的依赖隔离机制。官方安装指南推荐为每个项目建一个 `.venv`，避免 pdf-craft 的依赖污染全局环境。
- **Python 依赖 vs 系统依赖**：pip 能装的是「Python 包」；像 Poppler 这样编译好的系统程序（含可执行文件 `pdfinfo`、`pdftoppm` 等）必须用操作系统的包管理器（apt、brew）或手动下载安装。
- **vendor OCR 与 local OCR**（回顾 u1-l1）：vendor 模式调用远程 OCR 服务，只需要 URL、模型名和 API key；local 模式在本机 NVIDIA GPU（CUDA）上跑模型，需要 PyTorch 和显卡显存。本讲的首次转换使用 vendor 模式，避开 GPU 环境问题。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [docs/en/INSTALLATION.md](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/docs/en/INSTALLATION.md) | 官方安装指南：标准安装 / local 扩展的选择、Poppler 安装、验证方法 |
| [pyproject.toml](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pyproject.toml) | 包的元数据：Python 版本要求、依赖清单、`local` extra 的确切内容 |
| [.env.template](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/.env.template) | 仓库本地 CLI 工具 `pdf_craft_tool` 的环境变量模板（**不是**库本身的配置方式） |
| [pdf_craft/ocr_config.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/ocr_config.py) | 六种 OCR 配置数据类，本讲重点用 `DeepSeekOCRVendorConfig` |
| [pdf_craft/craft.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py) | `PDFCraft` 门面与 `PDFOptions`，本讲的 `convert_pdf_to_markdown` 入口 |
| [pdf_craft/document/package.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/document/package.py) | `DocumentPackage` 中间包的目录约定，用于解释实践观察到的目录结构 |
| [pdf_craft/functions.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/functions.py) | `predownload_models`，local 模式预下载模型的辅助函数 |

## 4. 核心概念与源码讲解

### 4.1 依赖体系：标准安装与 local 扩展

#### 4.1.1 概念说明

pdf-craft 的安装分两档，区别只在一件事：**OCR 模型在哪里运行**。

- **标准安装** `pip install pdf-craft`：适合 vendor OCR（远程服务）。不需要 CUDA、不需要显卡，只要网络和凭据。官方指南明确建议：除非你刻意要让 OCR 模型跑在自己的 NVIDIA GPU 上，否则一律从标准包开始。
- **local 扩展** `pip install "pdf-craft[local]"`：额外提供本地模型运行所需的 Python 运行时，但它**不替你选 PyTorch 的 CUDA 版本**——PyTorch 需要按你的 Python 版本、驱动和操作系统单独安装。如果你不确定，就用 vendor OCR。

一个容易混淆的点：`.env.template` 不是库的配置文件。它第一行注释就写明这是「仓库手工脚本（pdf_craft_tool）的私有配置」，而「库的 API 调用者需要显式配置 OCR 和 LLM」。README 也再次确认：库只接受通过 `PDFOptions(ocr=...)` 传入的配置对象，**不读取任何环境变量**。所以你的 `convert.py` 里凭据来自代码中的配置对象，不来自 `.env`。

#### 4.1.2 核心流程

安装决策可以画成一棵简单的决策树：

```text
你的机器有 CUDA-capable NVIDIA GPU 且想本地跑 OCR？
├── 否 → pip install pdf-craft          （vendor OCR，零 GPU 依赖）
└── 是 → pip install "pdf-craft[local]"
         再按 pytorch.org 选择器安装匹配的 CUDA 版 PyTorch
         用 nvidia-smi / torch.cuda.is_available() 验证
```

标准安装后，pip 会拉入这些关键依赖（对应 `pyproject.toml` 的 `dependencies`）：

- `pdf2image`：把 PDF 页面渲染成图像（它调用系统级 Poppler，见 4.2）。
- `pypdf`：读取 PDF 结构与元数据。
- `doc-page-extractor`：真正的 OCR 页面提取引擎（DeepSeek / Unlimited 后端都在这个包里）。
- `tiktoken`：token 计数（翻译分组时用，u2-l3 会讲）。
- `openai`：调用 OpenAI 兼容的 LLM / vendor OCR 服务。
- `epub-generator`、`reportlab` 等：渲染与 PDF 回写。

#### 4.1.3 源码精读

先看包的身份与 Python 版本约束——[pyproject.toml:L5-L7](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pyproject.toml#L5-L7) 声明包名 `pdf-craft`、版本 `2.0.1`；[pyproject.toml:L25](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pyproject.toml#L25) 要求 `>=3.11,<3.14`（Python 3.11/3.12/3.13，与 classifiers 一致）。装之前先确认 `python --version` 在这个区间内。

标准依赖清单在 [pyproject.toml:L26-L42](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pyproject.toml#L26-L42)：注意 `doc-page-extractor>=1.2.0,<2.0.0` 是基础依赖——也就是说标准安装**已经包含** OCR 引擎的调用代码，local 扩展补的只是「本地跑模型」的运行时。

local 扩展的确切定义在 [pyproject.toml:L48-L51](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pyproject.toml#L48-L51)：`local = ["doc-page-extractor[local]>=1.2.0,<2.0.0"]`，即给 `doc-page-extractor` 加上它自己的 `local` extra。旁边的注释解释了这个设计：把 transformer/CUDA 相关的依赖交由上游 `doc-page-extractor` 自己管理，pdf-craft 只做一次声明。

对应地，官方指南 [INSTALLATION.md:L5-L21](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/docs/en/INSTALLATION.md#L5-L21) 给出两条安装命令，并强调 vendor OCR「不需要本地 CUDA」、local extra「不替你选 PyTorch wheel」。

vendor 模式的配置对象是本讲实践的主角——[pdf_craft/ocr_config.py:L84-L92](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/ocr_config.py#L84-L92) 定义了 `DeepSeekOCRVendorConfig`：

```python
@dataclass(frozen=True)
class DeepSeekOCRVendorConfig:
    base_url: str
    api_key: str = field(repr=False)
    model: str
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int = 8000
    timeout_seconds: int = 180
```

三个必填字段 `base_url`、`api_key`、`model` 正是 INSTALLATION.md 里说的「provider URL, model name, and credentials」。一个值得学习的细节：`api_key: str = field(repr=False)` 让这个 frozen dataclass 被 `print` / 日志打印时**隐藏密钥**，避免凭据泄漏到日志。

作为对照，local 模式的配置长这样（[pdf_craft/ocr_config.py:L9-L31](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/ocr_config.py#L9-L31)）：`DeepSeekOCRLocalConfig` 的字段是 `models_cache_path`（模型缓存目录）、`local_only`（离线模式，不再联网下载）、`enable_devices_numbers`（指定 CUDA 设备号）。可以看到 vendor 和 local 配置的字段集完全不同——这就是「两种安装档位」在代码里的投影。

顺带一提，`ensure_ocr_config`（[pdf_craft/ocr_config.py:L132-L146](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/ocr_config.py#L132-L146)）是 `predownload_models` 的兜底逻辑：没传 `ocr` 时默认按 `DeepSeekOCRLocalConfig` 处理；如果同时传了 `ocr` 和 `models_cache_path` / `local_only` 就抛 `ValueError`，因为这两种指定方式互斥。U2-l1 会详细展开六种配置，这里只需记住：**传了 `PDFOptions(ocr=...)` 就不要再传 `models_cache_path`**。

最后澄清 `.env.template` 的定位——[.env.template:L1-L2](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/.env.template#L1-L2) 的注释直译过来是：「仓库手工脚本的私有配置。复制为 `.env`；不要提交结果。库的 API 调用者需显式配置 OCR 和 LLM。」其中 [PDF_CRAFT_DEEPSEEK_OCR_BASE_URL 等变量](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/.env.template#L24-L31) 是给第 11 单元的 `pdf_craft_tool` CLI 用的。README 的表述更直接：库通过 `PDFOptions(ocr=...)` 接受配置对象，[不读取环境变量](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/README.md#L198-L200)。

#### 4.1.4 代码实践

**实践目标**：完成一次正确的安装，并通过官方验证命令确认库可用。

**操作步骤**：

1. 创建并激活虚拟环境（[INSTALLATION.md:L30-L38](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/docs/en/INSTALLATION.md#L30-L38)）：

   ```bash
   python3.11 -m venv .venv
   source .venv/bin/activate   # Windows PowerShell: .venv\Scripts\Activate.ps1
   python -m pip install --upgrade pip
   python -m pip install pdf-craft
   ```

2. 运行官方验证命令（[INSTALLATION.md:L73-L77](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/docs/en/INSTALLATION.md#L73-L77)）：

   ```bash
   python -c "import pdf_craft; print(pdf_craft.__file__)"
   ```

3. 再确认关键符号能导入（示例代码）：

   ```bash
   python -c "from pdf_craft import DeepSeekOCRVendorConfig, PDFCraft, PDFOptions; print('ok')"
   ```

**需要观察的现象**：第 2 步打印出 `pdf_craft/__init__.py` 的实际路径（在你的 `.venv` 里）；第 3 步打印 `ok`。

**预期结果**：安装文档特别说明——验证命令只证明「包装好了」，它既不会下载模型，也不会调用 OCR 服务，所以瞬间完成、不产生网络请求。如果你想顺手测试配置对象的 repr 安全性（示例代码）：

```python
from pdf_craft import DeepSeekOCRVendorConfig

config = DeepSeekOCRVendorConfig(
    base_url="https://example.com/v1",
    api_key="secret-key",
    model="deepseek-ocr",
)
print(config)  # api_key 应显示为 field(repr=False) 的占位形式，不出现 secret-key
```

**待本地验证**：`field(repr=False)` 的实际打印格式（dataclass 会以 `field(...)` 形式占位）；关键是不出现明文密钥。

#### 4.1.5 小练习与答案

**练习 1**：同事的机器没有 NVIDIA 显卡，但他执行了 `pip install "pdf-craft[local]"`。这个安装有问题吗？他接下来能用 vendor OCR 吗？

**答案**：没有问题，也不浪费——local extra 只是在标准依赖之外多了本地运行时依赖，并不改变 API。他完全可以只用 `PDFOptions(ocr=DeepSeekOCRVendorConfig(...))` 走 vendor 模式，GPU 缺失只影响 local 模式。官方指南的建议反过来讲更清楚：不确定时先装标准包即可。

**练习 2**：为什么 `pyproject.toml` 里 `local` extra 直接写成 `doc-page-extractor[local]`，而不是把 torch 等 CUDA 依赖罗列在 pdf-craft 自己的清单里？

**答案**：见 [pyproject.toml:L48-L51](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pyproject.toml#L48-L51) 的注释：让 transformer/CUDA 相关需求继续由上游 `doc-page-extractor` 负责。pdf-craft 不重复维护一份容易过时的清单；且 PyTorch 的 CUDA wheel 本来就要按平台手动选择，声明在依赖里反而会装错版本。

**练习 3**：把 API key 写进 `.env`，然后运行 `convert.py`，pdf-craft 能读到它吗？

**答案**：不能。`.env.template` 的注释和 README 都明确：库不读环境变量，配置只来自代码里传给 `PDFOptions(ocr=...)` 的对象。`.env` 是仓库本地工具 `pdf_craft_tool`（第 11 单元）的配置方式。

### 4.2 系统依赖：Poppler 与 Python 版本

#### 4.2.1 概念说明

Poppler 是一套经典的 PDF 渲染工具库（`pdfinfo`、`pdftoppm`、`pdftocairo` 等命令行工具都来自它）。pdf-craft 处理 PDF 的第一步是把每一页渲染成图像交给 OCR，这个「PDF → 图片」的活由 Python 包 `pdf2image` 完成，而 `pdf2image` 只是一层包装——它真正调用的是系统里的 Poppler 可执行文件。

这就是为什么 Poppler 不能用 pip 装：它不是 Python 包，而是操作系统层面的程序。这也是新手最常见的第一个报错来源：`pip install pdf-craft` 成功了，一跑转换却失败——因为 Poppler 缺失。

官方要求清单（[INSTALLATION.md:L23-L28](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/docs/en/INSTALLATION.md#L23-L28)）共四条：Python `>=3.11,<3.14`、Poppler、vendor OCR 的网络与凭据、（仅 local 模式）CUDA GPU 与 PyTorch。本讲只需满足前三条。

#### 4.2.2 核心流程

一次 PDF 提取中，Poppler 参与的位置：

```text
input.pdf
  │  pypdf 读取页数/元数据（纯 Python，无需系统依赖）
  ▼
pdf2image 调用 Poppler（pdftoppm/pdftocairo）把页面按 DPI 渲染
  ▼
页面图像 → OCR 后端（vendor 远程服务 或 local GPU）
  ▼
OCR 结果 → 目录分析 → 章节 → DocumentPackage
```

Poppler 的查找规则：Linux/macOS 上从 `PATH` 中找；Windows 上要么把 Poppler 的 `bin` 目录加入 `PATH`，要么在代码里通过 `PDFOptions(pdf_handler=DefaultPDFHandler(poppler_path="C:/tools/poppler/bin"))` 显式指定路径（这是 INSTALLATION.md 给 Windows 用户的替代方案，`DefaultPDFHandler` 的细节在 u3-l2 展开）。

#### 4.2.3 源码精读

安装命令在 [INSTALLATION.md:L40-L48](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/docs/en/INSTALLATION.md#L40-L48)：macOS 用 `brew install poppler`，Debian/Ubuntu 用 `sudo apt-get update && sudo apt-get install poppler-utils`。Windows 的 `poppler_path` 方案在 [INSTALLATION.md:L50](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/docs/en/INSTALLATION.md#L50)，验证命令 `pdfinfo -v` 在 [INSTALLATION.md:L52](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/docs/en/INSTALLATION.md#L52)。

为什么缺了它会坏？看依赖声明的第一项——[pyproject.toml:L27](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pyproject.toml#L27) 要求 `pdf2image>=1.17.0,<2.0.0`。pdf2image 是 pdf-craft 标准安装的一部分，但 Poppler 本身不出现在 pip 依赖里（也不可能出现在里面），它只能作为系统依赖由用户准备。INSTALLATION.md 的 Requirements 一节把「Poppler for PDF conversion and the standard PDF translation/patch workflow」列为条目，就是说：凡是以 PDF 为输入的工作流（转 Markdown、转 EPUB、翻译回写）都需要它。

顺带留意 local 模式的验证方法（本讲不强求）：[INSTALLATION.md:L54-L62](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/docs/en/INSTALLATION.md#L54-L62) 用 `nvidia-smi` 确认驱动、用 `torch.cuda.is_available()` 确认 PyTorch 可用（local OCR 要求结果为 `True`）；模型默认首次使用时下载，也可以用 `predownload_models` 预先下载（[pdf_craft/functions.py:L7-L19](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/functions.py#L7-L19)，内部就是构造一个 `OCR` 对象并调用其 `predownload`）。

#### 4.2.4 代码实践

**实践目标**：确认 Poppler 已安装且能被命令行找到。

**操作步骤**：

1. 按你的操作系统执行 4.2.3 中的安装命令。
2. 验证（示例命令）：

   ```bash
   pdfinfo -v
   ```

3. 用仓库自带的测试 PDF 做一次渲染冒烟测试（示例代码，仅验证 Poppler 链路，不调用 OCR）：

   ```python
   # poppler_check.py —— 只验证 pdf2image + Poppler 链路
   from pdf2image import convert_from_path

   pages = convert_from_path("tests/assets/space.pdf", dpi=72)
   print(f"渲染出 {len(pages)} 页，第一页尺寸 {pages[0].size}")
   ```

**需要观察的现象**：`pdfinfo -v` 打印 Poppler 版本号；`poppler_check.py` 打印页数和图像尺寸。

**预期结果**：`tests/assets/` 下有多个小体积 PDF（如 `space.pdf`、`citation.pdf`），任选其一即可。如果 `pdfinfo` 提示 command not found，说明 Poppler 未安装或不在 `PATH`——先解决它再继续 4.3。若 Poppler 缺失时运行转换，pdf2image 会在首次渲染页面时抛出找不到 Poppler 的异常（具体异常形式**待本地验证**）。

#### 4.2.5 小练习与答案

**练习 1**：`pip install pdf-craft` 之后依赖列表里有 `pdf2image`，为什么还需要手动装 Poppler？

**答案**：`pdf2image` 是 Python 包装层，它通过 subprocess 调用系统里的 Poppler 可执行文件（如 `pdftoppm`）完成真正的渲染。Poppler 是系统程序，不是 Python 包，pip 无法安装，所以必须用 brew/apt 或手动下载（Windows 可用 `poppler_path` 指定）。

**练习 2**：`pdf-craft` 声明支持的 Python 版本范围是什么？如果机器上是 Python 3.10，pip install 会发生什么？

**答案**：`>=3.11,<3.14`（[pyproject.toml:L25](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pyproject.toml#L25)），即 3.11、3.12、3.13。Python 3.10 下 pip 会因为 `requires-python` 不满足而拒绝安装（提示找不到兼容版本）。

**练习 3**：只需要翻译一个现成的 EPUB（不碰任何 PDF），需要装 Poppler 吗？

**答案**：不需要。Poppler 只在「以 PDF 为输入」的工作流里被用到（INSTALLATION.md 的 Requirements 原文：用于 PDF conversion 和标准 PDF translation/patch workflow）。EPUB → 翻译 EPUB 的路径不渲染 PDF 页面。源码侧也有呼应：`PDFCraft.__init__` 刻意不初始化任何 PDF 基础设施，EPUB-only 用户甚至可以裸 `PDFCraft()`（见 [pdf_craft/craft.py:L69-L78](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L69-L78) 的类文档）。

### 4.3 首次转换：convert.py 与中间产物

#### 4.3.1 概念说明

环境就绪后，第一次转换只需要三样东西：一个 PDF 文件、一份 vendor OCR 凭据、四行 API 调用。核心是 u1-l1 介绍过的门面方法 `convert_pdf_to_markdown`——它一次完成「提取 → （可选转换步骤）→ 渲染 Markdown」。

本讲实践有两个观察点，为后续单元铺路：

1. **耗时与 token**：`convert_pdf_to_markdown` 的返回值是 `OCRTokensMetering`（OCR 输入/输出 token 计量），配合 `time.perf_counter` 可以量化一次转换的成本。
2. **中间产物目录**：默认情况下转换用完即删临时工作区；传入 `package_path` 后，DocumentPackage 中间包会保留在磁盘上，可以直观看到 `chapters/`、`assets/`、`toc.xml`、`document.json` 的布局——这正是 u1-l1 说的「中间包」，第 6 单元会精读它。

#### 4.3.2 核心流程

`convert_pdf_to_markdown` 内部四步（对照源码 4.3.3）：

```text
convert_pdf_to_markdown(source, output, package_path=None, ...)
  1. _package_workspace(package_path)
       有 package_path → 直接用该目录（转换后保留）
       没有            → TemporaryDirectory（结束/失败后自动删除）
  2. extract_pdf_with_metering(source, workspace, extraction)
       OCR 逐页识别 → 目录分析 → 章节生成 → 写 document.json
  3. _apply_steps(package, steps)      # 本讲不传 steps，原样通过
  4. render_markdown(package, output)  # 中间包 → Markdown 文件
  返回 OCRTokensMetering(input_tokens, output_tokens)
```

#### 4.3.3 源码精读

先看两个输入对象的定义。[pdf_craft/craft.py:L38-L45](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L38-L45) 定义 `PDFOptions`：长期存在的基础设施配置——`ocr`（OCR 配置对象）、`pdf_handler`（自定义 PDF 处理器，u3-l2）、`models_cache_path` / `local_only`（local 模式的简写，与 `ocr` 互斥）。它的文档字符串说得很准：「只在提取 PDF 时需要的长期基础设施」。

门面方法本体在 [pdf_craft/craft.py:L179-L190](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L179-L190)：

```python
def convert_pdf_to_markdown(
    self, source: PathLike | str, output: PathLike | str, *,
    package_path: PathLike | str | None = None, extraction: ExtractionOptions | None = None,
    assets_path: PathLike | str | None = None,
    steps: Sequence[TranslationStep | PackageTransformer] = (),
) -> OCRTokensMetering:
    with _package_workspace(package_path) as workspace:
        package, metering = self.extract_pdf_with_metering(source, workspace, extraction)
        package = self._apply_steps(package, steps)
        self.render_markdown(package, output, assets_path, ...)
        return metering
```

`with _package_workspace(...)` 这一行决定了中间产物的命运——[pdf_craft/craft.py:L270-L277](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L270-L277)：传了 `package_path` 就 `yield Path(package_path)`（保留）；否则进入 `TemporaryDirectory(prefix="pdf-craft-package-")`，`with` 块结束时自动清理。README 的 Quick Start 注释（[README.md:L77-L79](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/README.md#L77-L79)）与官方示例代码（[README.md:L64-L75](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/README.md#L64-L75)）描述的正是这个行为：默认临时目录，需要调试/复用时才传 `package_path`。

注意 `PDFCraft` 的构造是**惰性**的：[pdf_craft/craft.py:L76-L78](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L76-L78) 只是记住 `PDFOptions`；直到真正提取 PDF 时才在 [pdf_craft/craft.py:L253-L262](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L253-L262) 的 `_pdf_engine` 里延迟 import 提取引擎——如果构造时连 `PDFOptions` 都没给，这里会抛出 `ValueError("PDF extraction requires PDFCraft(pdf=PDFOptions(...))")`。所以「构造 `PDFCraft`」和「具备 PDF 提取能力」是两件事。

返回值类型定义在 [pdf_craft/metering.py:L15-L18](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/metering.py#L15-L18)：`OCRTokensMetering` 只有两个整数字段 `input_tokens` 和 `output_tokens`，分别对应 OCR 服务的输入与输出 token 消耗——用它可以直接估算 vendor OCR 的调用成本。

实践最后观察到的目录结构，契约写在 [pdf_craft/document/package.py:L17-L26](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/document/package.py#L17-L26) 的 `DocumentPackage.from_path`：

- `chapters/`——章节 XML 文件（`chapter_*.xml`）
- `assets/`——图片等资源
- `toc.xml`——目录结构（存在时才有意义）
- `cover.png`——封面（存在才有）
- `document.json`——元数据，由 [write_metadata](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/document/package.py#L42-L50) 写出，含 `schema`、`dpi`、`page_pixel_sizes` 等页面几何信息

提取过程中你可能还会看到 `ocr/` 等额外的工作文件（如逐页 OCR 缓存），它们属于提取器内部实现，第 3 单元精读。

#### 4.3.4 代码实践（本讲核心实践）

**实践目标**：编写 `convert.py`，用 `DeepSeekOCRVendorConfig` 完成 PDF → Markdown，记录耗时、token 消耗与中间目录结构。

**操作步骤**：

1. 准备一个体量小的 PDF。仓库自带的 `tests/assets/space.pdf` 等都可以（也可以用你自己的扫描件）。
2. 写入 `convert.py`（示例代码，框架来自 [README.md:L64-L75](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/README.md#L64-L75)，按本讲目标扩展了计时、计量与 `package_path`）：

   ```python
   import time
   from pathlib import Path

   from pdf_craft import DeepSeekOCRVendorConfig, PDFCraft, PDFOptions

   craft = PDFCraft(pdf=PDFOptions(ocr=DeepSeekOCRVendorConfig(
       base_url="https://你的-ocr-服务/v1",   # 替换为你的 OCR 服务地址
       api_key="your-api-key",               # 替换为你的凭据
       model="deepseek-ocr",                 # 替换为服务端模型名
   )))

   start = time.perf_counter()
   metering = craft.convert_pdf_to_markdown(
       "tests/assets/space.pdf",   # 输入 PDF
       "output.md",                # 输出 Markdown
       package_path="work/package",  # 保留中间产物，便于观察
   )
   elapsed = time.perf_counter() - start

   print(f"耗时: {elapsed:.1f} 秒")
   print(f"OCR tokens: 输入 {metering.input_tokens}, 输出 {metering.output_tokens}")

   def walk(directory: Path, indent: int = 0):
       for entry in sorted(directory.iterdir()):
           print("  " * indent + ("- " if entry.is_dir() else "") + entry.name +
                 ("/" if entry.is_dir() else ""))
           if entry.is_dir():
               walk(entry, indent + 1)

   print("中间目录结构:")
   walk(Path("work/package"))
   ```

3. 运行：

   ```bash
   python convert.py
   ```

**需要观察的现象**：

- 终端先打印耗时与 token 数（远程 OCR 通常以秒到分钟计，取决于页数）。
- `work/package/` 下出现 `chapters/`、`assets/`、`document.json`，可能还有 `toc.xml`（视 PDF 是否有可识别目录而定）；`chapters/` 里有若干 `chapter_*.xml`。
- 根目录生成 `output.md`，内容是提取出的正文。

**预期结果**：`output.md` 可读、章节顺序正确；`walk` 打印出的目录与 4.3.3 列出的契约一致。把 `package_path` 参数去掉再跑一次，转换仍成功，但 `work/package` 不再生成——中间产物进了临时目录并被自动清理。**待本地验证**：具体耗时、token 数值、`chapter_*.xml` 的数量与命名，都取决于所选 PDF 与 OCR 服务，请以你机器上的实际输出为准。

#### 4.3.5 小练习与答案

**练习 1**：`convert_pdf_to_markdown` 不传 `package_path` 时中间产物去哪了？什么场景下应该传？

**答案**：进了一个前缀为 `pdf-craft-package-` 的系统临时目录，`with` 块结束（无论成功或失败）即被删除（[pdf_craft/craft.py:L270-L277](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L270-L277)）。想调试中间结果、或希望二次渲染/翻译时复用提取结果（避免重复 OCR 花钱）时应该传。

**练习 2**：`convert_pdf_to_markdown` 的返回值是什么？能用来做什么？

**答案**：`OCRTokensMetering`，含 `input_tokens` 与 `output_tokens` 两个整数（[pdf_craft/metering.py:L15-L18](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/metering.py#L15-L18)）。用 vendor OCR 时可据此估算 API 成本；也可以在批量转换脚本里累计做预算监控。

**练习 3**：以下代码能跑吗？为什么？

```python
craft = PDFCraft()
craft.convert_pdf_to_markdown("input.pdf", "output.md")
```

**答案**：不能。`PDFCraft()` 没传 `pdf` 参数，构造虽然成功（惰性设计），但真正提取时 `_pdf_engine` 检查到 `self._pdf is None` 会抛 `ValueError("PDF extraction requires PDFCraft(pdf=PDFOptions(...))")`（[pdf_craft/craft.py:L253-L262](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/craft.py#L253-L262)），且至少要给 `PDFOptions(ocr=...)` 提供一份 OCR 配置，否则提取无从认字。

## 5. 综合实践

把本讲三块内容串成一个「环境体检 + 首次转换」小任务：

1. **体检脚本**（示例代码）：写 `check_env.py`，依次检查并打印结果——
   - `sys.version_info` 是否满足 `>=3.11,<3.14`；
   - `shutil.which("pdfinfo")` 是否找到 Poppler（Windows 未加 PATH 时应提示改用 `DefaultPDFHandler(poppler_path=...)`）；
   - `import pdf_craft` 成功且打印其 `__file__`；
   - 构造 `DeepSeekOCRVendorConfig` 并打印，确认 `api_key` 不以明文出现。
2. **首次转换**：体检全绿后，用 4.3.4 的 `convert.py` 转换 `tests/assets/` 里两个不同的 PDF（例如 `space.pdf` 和 `citation.pdf`），分别记录耗时与 token，写入一张对比表。
3. **产物观察**：打开其中一个 `work/package/chapters/chapter_*.xml` 粗览（现在看不懂没关系，第 5 单元会逐字段讲解），再打开 `document.json` 找到 `schema` 与 `page_pixel_sizes` 两个字段——它们是第 6 单元 `DocumentPackage` 与第 10 单元 PDF 回写的关键伏笔。
4. **清理实验**：删掉 `package_path` 参数再转换一次，确认临时工作区被自动清理、`output.md` 仍正常生成。

## 6. 本讲小结

- pdf-craft 的安装分两档：标准安装走 vendor OCR（远程服务、无需 CUDA），`[local]` 扩展补齐本地模型运行时且需自装匹配的 CUDA 版 PyTorch；Python 版本须在 `>=3.11,<3.14`。
- Poppler 是系统级依赖，pip 装不了：`pdf2image` 依赖它把 PDF 页面渲染成图像；用 `pdfinfo -v` 验证，Windows 可用 `poppler_path` 指定位置。
- 库**不读取环境变量**：`.env.template` 只服务于仓库本地 CLI `pdf_craft_tool`；库的凭据一律通过 `PDFOptions(ocr=DeepSeekOCRVendorConfig(...))` 这类配置对象显式传入。
- `convert_pdf_to_markdown` 一步完成提取与渲染，默认用完即删的临时工作区，传 `package_path` 可保留 `chapters/`、`assets/`、`toc.xml`、`document.json` 等中间产物。
- 返回值 `OCRTokensMetering` 记录 OCR 输入/输出 token，是量化 vendor OCR 成本的直接依据。

## 7. 下一步学习建议

下一讲 [u1-l3 仓库结构与模块地图](u1-l3-repo-structure.md) 将俯瞰 `pdf_craft/`、`pdf_craft_tool/`、`tests/` 的目录组织，帮你把本讲跑通的链路对应到具体源码模块。在进入下一讲前，建议先通读 [pdf_craft/__init__.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/__init__.py)——它是公开 API 的完整清单（本讲用到的 `DeepSeekOCRVendorConfig`、`PDFCraft`、`PDFOptions` 都从这里导出，见 [L12](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/__init__.py#L12) 与 [L25-L36](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/__init__.py#L25-L36)），以后不确定「某个功能是否公开支持」时先查这里。若你准备深入 OCR 配置，可提前浏览 [docs/en/OCR_BACKENDS.md](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/docs/en/OCR_BACKENDS.md)。

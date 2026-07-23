# 项目总览与定位

## 1. 本讲目标

本讲是 genai-bench 学习手册的第一篇，目标是从零建立对项目的整体认知。读完本讲，你应当能够：

- 理解 **LLM 服务基准测试（benchmarking）** 是什么，以及为什么需要 **token 级** 指标。
- 用一句话说清 genai-bench 是什么、解决什么问题。
- 说出 genai-bench 的三大核心能力：**CLI 工具**、**实时 UI 仪表盘**、**实验分析器**。
- 读懂 `pyproject.toml`，识别项目的 **关键依赖** 与 **版本信息**。
- 在本地安装 genai-bench 并通过 `genai-bench --version` 确认版本。

本讲不要求你已经熟悉任何具体源码，所有概念都会从直觉讲起。

## 2. 前置知识

在进入源码之前，先用通俗语言建立几个基础概念。

### 2.1 什么是 LLM 服务

当我们说"LLM 服务（LLM serving system）"，指的是把一个大语言模型（如 ChatGPT、Llama、Qwen 等）部署成一个**可以通过 HTTP 接口调用的服务**。用户发一段文字（prompt），服务返回一段文字（response）。

常见的 LLM 服务都遵循 OpenAI 的接口约定，比如：

- `POST /v1/chat/completions`：文本对话
- `POST /v1/embeddings`：文本向量
- `POST /v1/images/generations`：图像生成

### 2.2 什么是基准测试（benchmark）

基准测试就是：**给服务施加模拟压力，测量它在各种条件下的性能表现。**

这就像测试一辆车——你不能只看它在空旷公路上能跑多快，还要看它在满载、上坡、拥堵时的表现。对 LLM 服务也一样，需要测量：

- 同时来 100 个请求时，响应有多快？
- 请求的输入长度从 100 token 到 4000 token，性能如何变化？
- 输出长度对吞吐量的影响有多大？

genai-bench 就是用来做这类测试的工具。

### 2.3 为什么需要 token 级指标

LLM 服务的响应是**流式（streaming）**的——它不是一个请求过去、一坨结果一次性回来，而是像打字机一样一个 token 一个 token 地吐出来（token 可以粗略理解为"词片段"）。

因此，仅用"请求延迟"这一个指标是不够的，因为它无法区分两种情况：

1. 服务等了 5 秒才开始说话，然后 1 秒说完。
2. 服务立刻开始说话，但说得慢吞吞，总共 6 秒说完。

两者总延迟相同，但用户体验天差地别。所以需要 **token 级** 指标，例如：

- **TTFT（Time To First Token，首 token 延迟）**：用户发请求到看到第一个字的时间。
- **TPOT（Time Per Output Token，每输出 token 耗时）**：生成每个 token 的平均时间。
- **吞吐量（throughput）**：每秒生成的 token 数。

用公式表达其中两个：

\[ \text{TTFT} = t_{\text{first\_token}} - t_{\text{request\_start}} \]

\[ \text{throughput} = \frac{\text{output\_tokens}}{\text{output\_time}} \]

genai-bench 的核心定位，就是**精确测量这些 token 级指标**。

> 小贴士：如果你还不清楚 TTFT、TPOT 这些指标如何从源码里算出来，不用担心——这是本手册 U4 单元（指标计算与聚合）的内容。本讲只需建立"token 级很重要"这个直觉即可。

## 3. 本讲源码地图

本讲聚焦"认识项目"，涉及的关键文件都很轻量，集中在项目根与包入口：

| 文件 | 作用 | 本讲用途 |
| --- | --- | --- |
| `README.md` | 项目对外说明书，讲定位、特性、安装、用法 | 理解项目定位与三大特性 |
| `pyproject.toml` | Python 项目元数据与依赖清单 | 识别技术栈、关键依赖、版本、入口脚本 |
| `genai_bench/version.py` | 读取并暴露当前版本号 | 理解版本号的来源 |
| `genai_bench/__init__.py` | 包入口，做必要的全局初始化 | 理解项目对运行环境的特殊要求 |

补充说明：`genai_bench` 这个包内部还有许多子目录（如 `cli`、`user`、`metrics`、`analysis`、`auth`、`storage`、`distributed`、`ui` 等），它们各自承担一个子系统。**这些子系统的目录地图会在 u1-l3「目录结构与模块全景」里详细讲解**，本讲只需知道"有这么一个大地图"即可。

## 4. 核心概念与源码讲解

### 4.1 项目定位与核心特性

#### 4.1.1 概念说明

genai-bench 是一个用于 **LLM 服务 token 级性能评估** 的基准测试工具。它要解决的问题是：

> 当我把一个 LLM 部署成服务后，如何用可复现、可对比的方式，量化它在不同压力、不同输入长度、不同并发下的性能？

它不是一个通用的"测 HTTP 接口快不快"的工具，而是**专门针对 LLM 服务的特点**（流式输出、token 计量、多模态输入输出）设计的。

根据官方说明，genai-bench 提供 **三大核心能力**：

1. **🛠️ CLI 工具**：用命令行发起、配置基准测试，校验用户输入。
2. **📊 实时 UI 仪表盘（Live UI Dashboard）**：在压测进行时，实时展示进度、日志和指标。
3. **📈 实验分析器（Experiment Analyzer）**：压测结束后，把结果生成 Excel 报告与可配置的图表。

#### 4.1.2 核心流程

这三大能力并不是孤立的，它们构成一次完整实验的工作流：

```text
  发起压测            实时观察             事后分析
┌──────────┐      ┌────────────┐      ┌──────────────┐
│ CLI 工具  │ ───► │ 实时 UI     │ ───► │ 实验分析器    │
│ benchmark │      │ 仪表盘      │      │ excel / plot │
└──────────┘      └────────────┘      └──────────────┘
  配置参数         看进度与指标          出报告与图表
```

用文字描述就是：

1. 你通过 **CLI 工具** 配置好后端地址、模型、任务类型、流量场景等参数，启动 `benchmark`。
2. 压测运行期间，**实时 UI 仪表盘** 展示当前进度和实时指标。
3. 压测结束后，**实验分析器** 把采集到的原始指标整理成 Excel 报告和图表（如吞吐量、TTFT、TPOT、错误率、RPS 等）。

这三步对应 README 中 Quick Start 的三条命令，我们会在后续单元逐一深入。

#### 4.1.3 源码精读

先看 README 对项目的定位描述：

[README.md:30-34](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/README.md#L30-L34) — 用一句话定义项目："powerful benchmark tool designed for comprehensive token-level performance evaluation of large language model (LLM) serving systems"，关键词是 **token-level**（token 级）和 **LLM serving systems**（LLM 服务）。

紧接着是特性清单：

[README.md:36-41](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/README.md#L36-L41) — 四个特性分别是：CLI Tool、Live UI Dashboard、Rich Logs、Experiment Analyzer。注意这里比"三大能力"多了一条 **Rich Logs**（富日志），它会"在实验结束时自动 flush 到终端和文件"——这条属于工程细节，会在 U7 单元（日志系统）展开。从能力维度看，核心仍是 CLI / UI / Analyzer 三大件。

再看快速开始里的第一条命令，能直观感受"CLI 工具"的样子：

[README.md:52-61](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/README.md#L52-L61) — 一个 `text-to-text` 的基准测试命令，关键参数包括 `--api-backend`（后端类型）、`--api-base`（服务地址）、`--task`（任务类型）、`--max-time-per-run`（单轮最大时长）、`--max-requests-per-run`（单轮最大请求数）。这些参数的具体含义会在 u1-l2「安装与运行第一个基准」中讲解。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，目标是建立项目定位的直觉。

**实践目标**：确认你理解了 genai-bench 的定位与三大特性，并能对应到 README 的具体位置。

**操作步骤**：

1. 打开 `README.md`，定位到第 30–34 行的 Introduction 段落。
2. 打开第 36–41 行的 Features 列表。
3. 用一句话（不超过 30 个汉字）概括：genai-bench 是什么？

**需要观察的现象**：

- Introduction 中反复出现的两个关键词是 **token-level** 与 **LLM serving**，这与其他"通用压测工具"形成区别。

**预期结果**：

- 你的概括应包含"针对 LLM 服务""token 级性能""基准测试"这类要素。一个可接受的答案是：**genai-bench 是一个面向 LLM 服务、精确测量 token 级性能指标的基准测试工具。**
- 进一步自检：你能否说出三大能力分别对应 README 的哪一行？（CLI=38 行，UI=39 行，Analyzer=41 行）

#### 4.1.5 小练习与答案

**练习 1**：如果一个压测工具只报告"每个请求的平均延迟"，为什么不足以评估 LLM 服务？
**参考答案**：因为 LLM 响应是流式的，"总延迟"无法区分"很久才开口"和"开口了但说得很慢"。缺少 TTFT、TPOT、吞吐量等 token 级指标，就无法反映真实的流式生成体验。

**练习 2**：genai-bench 的三大核心能力分别叫什么？
**参考答案**：CLI 工具、实时 UI 仪表盘（Live UI Dashboard）、实验分析器（Experiment Analyzer）。

### 4.2 技术栈与关键依赖

#### 4.2.1 概念说明

genai-bench 是一个 **Python 项目**，使用现代 Python 项目管理约定（`pyproject.toml` + `hatchling` 构建后端）。它的技术栈可以按"支撑哪项能力"来划分：

| 技术栈分类 | 关键依赖（节选） | 支撑的能力 |
| --- | --- | --- |
| 压测引擎 | `locust`、`gevent` | 模拟大量并发虚拟用户，驱动请求 |
| CLI 框架 | `click` | 命令行界面、子命令、参数解析 |
| 数据模型 | `pydantic` | 统一的请求/响应/指标数据契约 |
| 实时 UI | `rich` | 终端里的实时仪表盘与富日志 |
| 分析报告 | `openpyxl`、`matplotlib`、`pandas` | 生成 Excel 报告与图表 |
| Token 计量 | `transformers` | 用模型 tokenizer 计数 token |
| 多云对接 | `oci`、`openai`、`oci-openai`、`httpx` | 对接各类模型后端与云存储 |

> 这些库我们会在后续单元逐一用到。本讲的目标只是让你建立一个"依赖与能力对应"的整体印象。

#### 4.2.2 核心流程

理解依赖的关键，是看懂 **依赖如何支撑三大能力**。我们可以画一张对应关系：

```text
依赖库                  支撑的能力
─────────────────      ─────────────────────────
click        ───────►  CLI 工具（命令解析与校验）
locust/gevent ──────►  压测引擎（虚拟用户并发）
rich         ───────►  实时 UI 仪表盘 + 富日志
openpyxl     ───────►  实验分析器（Excel 报告）
matplotlib   ───────►  实验分析器（图表）
pydantic     ───────►  全链路数据契约（贯穿三者）
transformers ───────►  token 计量（贯穿三者）
```

可以看到，`pydantic` 与 `transformers` 是"贯穿三者"的基础设施：前者统一了数据格式，后者统一了 token 计数——这正是 token 级指标的底层支撑。

#### 4.2.3 源码精读

项目元数据定义在 `pyproject.toml` 顶部：

[pyproject.toml:1-8](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/pyproject.toml#L1-L8) — 这里声明了项目名 `genai-bench`、版本 `0.0.5`、描述、作者、Python 版本约束（`>=3.10,<3.13`）与许可证（MIT）。注意 `requires-python`：项目只支持 Python 3.10、3.11、3.12。

核心依赖清单在这里：

[pyproject.toml:21-41](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/pyproject.toml#L21-L41) — 共列出 **19 个核心依赖**，每个都带版本下限（如 `locust>=2.37.14`）。这意味着 `pip install genai-bench` 会一次性装上所有这些库。

此外还有**可选依赖（optional-dependencies）**，按用途分组：

[pyproject.toml:46-75](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/pyproject.toml#L46-L75) — 如 `dev`（开发工具：ruff/mypy/pytest）、`aws`/`azure`/`gcp`/`multi-cloud`（各大云厂商 SDK）、`docs`（文档构建）。需要时用 `pip install "genai-bench[aws]"` 这样的语法按需安装。

#### 4.2.4 代码实践

这是本讲的**主实践之一**：从源码中梳理出 5 个关键依赖及其作用。

**实践目标**：能复述 genai-bench 依赖的 5 个关键库各自的作用，并对应到它们支撑的能力。

**操作步骤**：

1. 打开 `pyproject.toml` 的依赖清单（第 21–41 行）。
2. 从中挑选 5 个"最能体现项目本质"的库。
3. 为每个库写一句话说明它的作用。

**需要观察的现象**：

- 有些库是"基础设施型"（如 `pydantic`、`gevent`），有些是"直接体现能力型"（如 `locust`、`rich`）。

**预期结果**：下面是一份参考答案（你可以挑选不同的 5 个，只要理由成立即可）。

| 依赖 | 作用 |
| --- | --- |
| `locust` | 压测引擎，genai-bench 基于它的虚拟用户（User）机制发起并发请求 |
| `click` | 命令行框架，用来定义 `cli` 命令组与 `benchmark`/`excel`/`plot` 等子命令 |
| `pydantic` | 数据模型库，统一请求、响应、实验元数据等数据契约（见 `protocol.py`） |
| `rich` | 终端富文本库，支撑实时 UI 仪表盘与彩色日志输出 |
| `transformers` | 提供模型 tokenizer，用于把文本计成 token 数（token 级指标的基础） |

**说明**：如果你对 `locust` 完全陌生，可以这样理解——它是一个知名的 Python 压测框架，genai-bench 复用了它的"模拟用户"与"指标采集"机制，再在之上做了 LLM 专属定制。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `pydantic` 和 `transformers` 被称为"贯穿三大能力"的基础设施？
**参考答案**：因为无论是 CLI、实时 UI 还是分析器，都需要统一的数据格式（pydantic 定义请求/响应/指标模型）和统一的 token 计数（transformers 提供 tokenizer），它们服务于全链路而非某单一能力。

**练习 2**：如果你想用 AWS Bedrock 作为后端，是否需要安装额外依赖？为什么？
**参考答案**：需要。AWS 相关 SDK（`boto3`、`botocore`）在可选依赖 `aws` 分组里，需要通过 `pip install "genai-bench[aws]"` 单独安装，它们不在 19 个核心依赖中。

### 4.3 版本与发布信息

#### 4.3.1 概念说明

一个项目的"版本"看似简单，背后却涉及一个值得注意的设计。genai-bench 的版本号：

- **不是写死在代码里的字符串**，而是通过 Python 标准库 `importlib.metadata` 在运行时从**已安装的包元数据**中读取。
- 当前版本为 **0.0.5**（定义在 `pyproject.toml` 的 `version` 字段）。
- 通过 **PyPI**（Python 官方包仓库）发布，所以可以用 `pip install genai-bench` 安装。

这种方式的好处是"单一数据源"：版本只在 `pyproject.toml` 里维护一次，代码里自动读取，不会出现"代码里的版本号忘了更新"的不一致问题。

#### 4.3.2 核心流程

版本号的"生命周期"是这样的：

```text
pyproject.toml            安装时写入             运行时读取            CLI 暴露
version = "0.0.5"   ──►   包元数据 metadata  ──►  version.py  ──►  genai-bench --version
                       (随 pip install 写入)     (__version__)      (cli.py version_option)
```

文字步骤：

1. 开发者在 `pyproject.toml` 写下 `version = "0.0.5"`。
2. `pip install` 时，`hatchling` 构建后端把这个版本号写进包的元数据。
3. 运行时，`version.py` 用 `importlib.metadata.version("genai-bench")` 读出版本，赋给 `__version__`。
4. CLI 入口 `cli.py` 引入 `__version__`，通过 `click.version_option` 把它接到 `--version` 选项上。

#### 4.3.3 源码精读

先看版本号的来源：

[genai_bench/version.py:1-3](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/version.py#L1-L3) — 整个文件只有三行：导入 `importlib.metadata`，然后 `__version__ = importlib.metadata.version("genai-bench")`。这就是"运行时读取已安装版本"的实现。

再看包入口 `__init__.py`，它揭示了项目的一个**重要运行前提**：

[genai_bench/__init__.py:1-7](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/__init__.py#L1-L7) — 这里在**所有其他 import 之前**调用了 `gevent.monkey.patch_all()`。注释解释：这是为了配合 Locust 的协程式并发（cooperative multitasking）；如果不做这步 monkey patch，阻塞 I/O（如 HTTP 请求）会卡住整个 worker 进程，导致心跳超时。**这是一个初学者容易踩坑的点**：如果你在其他地方先 import 了会被 gevent patch 的库，可能出现意外行为。

最后看 CLI 如何把版本接到命令行：

[genai_bench/cli/cli.py:47-53](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L47-L53) — `@click.version_option` 装饰器把 `version=GENAI_BENCH_VERSION`（来自 `version.py`）、`prog_name="genai-bench"`、输出模板 `"%(prog)s version %(version)s"` 绑定到命令组上。

> 小贴士：`GENAI_BENCH_VERSION` 的导入见 [cli.py:44](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L44)，即 `from genai_bench.version import __version__ as GENAI_BENCH_VERSION`。

#### 4.3.4 代码实践

这是本讲的**主实践之二**：真正安装并确认版本。

**实践目标**：在本地安装 genai-bench，运行 `genai-bench --version` 确认版本号正确。

**操作步骤**：

1. 确认你的 Python 版本在 3.10–3.12 之间（`python --version`）。
2. 执行安装：
   ```bash
   pip install genai-bench
   ```
3. 确认安装的命令脚本（`pyproject.toml` 第 43–44 行把 `genai-bench` 这个命令指向 `genai_bench.cli.cli:cli`）：
   ```bash
   genai-bench --version
   ```

**需要观察的现象**：

- 终端会根据 `cli.py` 中 `version_option` 的模板 `"%(prog)s version %(version)s"` 输出一行。

**预期结果**：

- 输出应为：
  ```text
  genai-bench version 0.0.5
  ```
  其中 `0.0.5` 来自 `pyproject.toml` 的 `version` 字段。
- **待本地验证**：上述输出基于源码逻辑推断。如果你的 pip 安装了不同版本（如较新的 release），版本号会相应变化，请以你本地 `pip show genai-bench` 的 `Version` 为准。

> 说明：本讲不要求你真的发起一次压测，那需要可访问的模型后端。**发起第一个基准的任务在 u1-l2 完成。**

#### 4.3.5 小练习与答案

**练习 1**：如果有人把 `pyproject.toml` 里的版本号改成 `0.0.6` 并重新安装，`genai-bench --version` 会显示什么？需要改 `version.py` 吗？
**参考答案**：会显示 `genai-bench version 0.0.6`。**不需要**改 `version.py`，因为它通过 `importlib.metadata.version` 在运行时动态读取已安装包的版本，单一数据源就是 `pyproject.toml`。

**练习 2**：为什么 `__init__.py` 要在所有 import 之前调用 `gevent.monkey.patch_all()`？
**参考答案**：因为 genai-bench 基于 Locust 做协程式并发，需要 gevent 把标准库的阻塞 I/O 替换成协程友好的版本。如果 patch 之前已经 import 了相关库，阻塞 I/O 会卡住整个 worker 进程导致心跳超时，所以必须最先执行。

## 5. 综合实践

现在把本讲的三个模块串起来，完成一个贯穿性的小任务：**建立一张属于自己的"项目名片"。**

**实践目标**：把"定位 → 技术栈 → 版本"三件事整合成一份可复用的项目速览。

**操作步骤**：

1. **安装并验证版本**：执行 `pip install genai-bench`，再运行 `genai-bench --version`，记录输出。
2. **探查命令入口**：运行 `genai-bench --help`，观察它暴露了哪些子命令（预期能看到 `benchmark`、`excel`、`plot`，这与本讲讲的三大能力呼应——这些子命令会在 u1-l4 详解）。
3. **梳理依赖**：回到 `pyproject.toml`，挑选 5 个关键依赖并各写一句作用（可参考 4.2.4 的参考答案）。
4. **产出项目名片**：用如下格式写一段笔记（示例代码）：

   ```text
   项目名：genai-bench
   版本：0.0.5
   定位：面向 LLM 服务的 token 级基准测试工具
   三大能力：CLI 工具 / 实时 UI 仪表盘 / 实验分析器
   关键依赖：locust（压测）、click（CLI）、pydantic（数据模型）、rich（UI）、transformers（token 计数）
   Python 版本：3.10 – 3.12
   ```

**需要观察的现象**：

- `--version` 与 `--help` 是否都能正常返回，说明安装成功。
- 你能否不查资料，凭这张名片向别人介绍 genai-bench。

**预期结果**：

- 拿到一张可复用的"项目名片"，并验证了 `--version`（应为 `genai-bench version 0.0.5`，待本地确认）与 `--help` 的输出。
- 如果 `--help` 里出现的子命令与你预期一致，说明你已经初步掌握了项目的命令结构。

## 6. 本讲小结

- genai-bench 是一个面向 **LLM 服务** 的 **token 级** 基准测试工具，核心动机是流式生成场景下"请求延迟"不足以反映真实体验。
- 三大核心能力是：**CLI 工具**、**实时 UI 仪表盘**、**实验分析器**（外加富日志这一工程细节）。
- token 级关键指标包括 TTFT、TPOT、吞吐量等，公式如 \(\text{throughput} = \text{output\_tokens} / \text{output\_time}\)。
- 项目是 Python 工程，用 `pyproject.toml` + `hatchling` 管理，核心依赖 19 个（如 `locust`、`click`、`pydantic`、`rich`、`transformers`），另有 `aws`/`azure`/`gcp` 等可选依赖分组。
- 版本号采用"单一数据源"设计：`pyproject.toml` 定义版本，`version.py` 用 `importlib.metadata` 运行时读取，CLI 经 `--version` 暴露。
- `__init__.py` 必须最先执行 `gevent.monkey.patch_all()`，这是配合 Locust 协程并发的关键前提。

## 7. 下一步学习建议

你已经"认识"了 genai-bench，下一步是"让它跑起来"。建议：

1. **u1-l2「安装与运行第一个基准」**：在本讲安装的基础上，实际发起一次最小的 `text-to-text` 基准测试，观察 `experiments` 目录的产出物。
2. 在阅读后续单元前，可以先扫一眼 `genai_bench` 包下的子目录名（`cli`、`user`、`metrics` 等），带着"它们各自干什么"的疑问进入 **u1-l3「目录结构与模块全景」**。
3. 如果你对 `--version`、`--help` 这类 CLI 机制好奇，可以先跳到 **u1-l4「CLI 入口与三大命令」**了解 `click` 的 group 与子命令注册。

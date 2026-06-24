# 安装与运行：从 CLI 翻译第一个 PDF

## 1. 本讲目标

上一讲我们建立了心智模型：BabelDOC 是一个「PDF → 中间表示 IL → PDF」的双语对照翻译库。本讲把它**真正跑起来**。读完本讲，你应当能够：

- 用 `uv` 把 BabelDOC 装到本机，并理解为什么项目官方推荐 `uv tool install`。
- 在命令行里用一条 `babeldoc` 命令、配合任意 OpenAI 兼容服务，把一个 PDF 翻译成 mono（单语译文）与 dual（双语对照）两种 PDF。
- 读懂 CLI 的入口结构：`babeldoc` 这个命令到底指向哪个函数、参数是怎么分组定义的。
- 看懂「输入参数 → 翻译器 → 配置对象 → 主流程」这条最浅层的调用关系，为后续单元精读主流程打基础。

本讲重点是「会跑」+「能看懂入口」，参数的完整含义留到下一讲（u1-l3、u1-l4）和后续进阶单元展开。

## 2. 前置知识

本讲假设你已经读过 u1-l1，知道 Parsing / Rendering / IL / mono·dual PDF 这几个词。此外补充几个 CLI 与 Python 打包相关的名词：

- **uv**：一个用 Rust 写的、极快的 Python 包管理器（Astral 出品）。它能管理 Python 解释器本身、虚拟环境、依赖安装。BabelDOC 官方推荐用它的 `tool` 子命令来安装命令行工具。
- **命令行工具（CLI tool）**：装好后可以直接在终端敲的命令，比如 `babeldoc --help`。在 Python 里，这类工具是「把某个函数注册成一个命令」实现的。
- **入口点（entry point / script）**：`pyproject.toml` 里的一行配置，告诉安装器「`babeldoc` 这个命令 = 运行 `babeldoc.main` 模块里的 `cli` 函数」。
- **OpenAI 兼容服务**：只要某个翻译/大模型服务的 HTTP 接口长得像 OpenAI 的 `/v1/chat/completions`，BabelDOC 就能直接用。常见的有官方 OpenAI、DeepSeek、智谱 GLM，以及本地的 Ollama。
- **QPS（Queries Per Second）**：每秒请求数。翻译时会按这个值对翻译服务做限流，避免把对方 API 打爆或被限速。

如果你本机还没装 Python 3.10+，uv 会顺带帮你装好，不必提前准备。

## 3. 本讲源码地图

本讲涉及的文件都很轻量，主要是「入口与配置」：

| 文件 | 作用 | 本讲用来讲什么 |
| --- | --- | --- |
| `babeldoc/main.py` | CLI 的全部实现：参数定义 `create_parser`、异步主流程 `main`、入口函数 `cli` | 命令如何被解析、参数如何分组、入口如何启动 |
| `babeldoc/__init__.py` | 包初始化文件，只有一行版本号 | 版本号在源码里的实际落点 |
| `pyproject.toml` | 项目元数据：依赖、Python 版本要求、`babeldoc` 命令入口点 | 为什么敲 `babeldoc` 就能跑、依赖与 Python 版本要求 |
| `README.md` | 官方安装与用法说明 | `uv tool install` 的官方推荐用法、示例命令 |

> 所有文件路径与行号都基于当前 HEAD `980fd28`。

## 4. 核心概念与源码讲解

### 4.1 uv 安装方式

#### 4.1.1 概念说明

BabelDOC 是一个标准的 Python 包（发布在 PyPI 上，名为 `BabelDOC`）。理论上你可以用 `pip install BabelDOC` 安装，但官方**强烈推荐用 uv 的 tool 功能**。原因有三：

1. **隔离干净**：`uv tool install` 会为 `babeldoc` 创建一个独立的虚拟环境，不会污染你系统的 Python，也不会和你别的项目打架。装出来的 `babeldoc` 命令却可以在任何目录直接调用。
2. **自动管解释器**：BabelDOC 要求 Python `>=3.10,<3.14`（见 [pyproject.toml:7](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/pyproject.toml#L7)）。`uv tool install --python 3.12` 能在没有 3.12 的情况下自动帮你下载并使用指定版本的解释器。
3. **快**：uv 用 Rust 实现，安装速度比 pip 快一个数量级，BabelDOC 依赖又多（PyMuPDF、onnxruntime、scikit-image 等），这点很关键。

#### 4.1.2 核心流程

从零到能跑，分三步：

```text
1. 安装 uv 本体（一次性，参考 uv 官方安装说明，并按提示设置 PATH）
2. uv tool install --python 3.12 BabelDOC   # 把 babeldoc 命令装到独立环境
3. babeldoc --help                            # 验证安装成功
```

如果你是开发者、想直接改源码跑，README 还提供了「从源码运行」的方式：克隆仓库后在项目目录里用 `uv run babeldoc ...`，uv 会自动按 `pyproject.toml` 建好虚拟环境并运行，连 `--python` 都不用手动指定。

#### 4.1.3 源码精读

`pyproject.toml` 里声明了 Python 版本要求与依赖规模，这决定了「为什么装的时候要等一会儿」：

[pyproject.toml:7-7](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/pyproject.toml#L7) —— 限定 Python 解释器版本为 `>=3.10,<3.14`，这就是 README 建议用 `--python 3.12` 的依据（3.12 落在合法区间且功能稳定）。

[pyproject.toml:19-55](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/pyproject.toml#L19-L55) —— `dependencies` 列表，能看到 PyMuPDF、onnxruntime、pdfminer 的 vendor 版、scikit-image、tiktoken、hyperscan 等一堆重型依赖，这也是为什么 uv 的速度优势在这里特别明显。

安装成功后，`babeldoc` 这个命令指向哪个函数，由下面这一行入口点决定（**这是本讲最关键的一行**）：

[pyproject.toml:66-68](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/pyproject.toml#L66-L68) —— `[project.scripts]` 段把命令名 `babeldoc` 映射到 `babeldoc.main:cli`，含义是「敲 `babeldoc` = 调用 `babeldoc/main.py` 文件里的 `cli()` 函数」。

而 README 给出的官方推荐命令就一行：

[README.md:70-74](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/README.md#L70-L74) —— `uv tool install --python 3.12 BabelDOC` 然后 `babeldoc --help`。

#### 4.1.4 代码实践

1. **实践目标**：在干净环境里把 BabelDOC 装上，验证 `babeldoc` 命令可用。
2. **操作步骤**：
   - 按 [uv 官方安装说明](https://github.com/astral-sh/uv#installation) 安装 uv（通常一行脚本），并按提示把 uv 的可执行目录加进 `PATH`。
   - 执行 `uv tool install --python 3.12 BabelDOC`。
   - 执行 `babeldoc --help`。
3. **需要观察的现象**：安装阶段会拉取数十个依赖（首次较慢）；`babeldoc --help` 会打印一长串参数（带 `Translation`、`Translation - OpenAI Options` 等分组标题）。
4. **预期结果**：终端出现 `usage: babeldoc ...` 与 `version: %(prog)s 0.6.3` 相关输出，说明入口点 `babeldoc.main:cli` 已正确注册。版本号 `0.6.3` 与源码 [babeldoc/__init__.py:1](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/__init__.py#L1) 中 `__version__ = "0.6.3"` 一致。

> 待本地验证：若你的网络受限，依赖下载可能超时；可考虑配合镜像源或先在联网机器上 `--generate-offline-assets`（见 u8-l1）。

#### 4.1.5 小练习与答案

**练习 1**：为什么官方推荐 `uv tool install` 而不是直接 `pip install`？

> **参考答案**：`uv tool install` 会为命令行工具创建隔离的虚拟环境，避免重型依赖污染系统 Python 或与其他项目冲突，同时 uv 基于 Rust、安装大量依赖更快；它还能自动管理 Python 解释器版本。

**练习 2**：在不看源码的情况下，如何确认本机装的是哪个版本的 BabelDOC？

> **参考答案**：运行 `babeldoc --version`（由 [babeldoc/main.py:42-46](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L42-L46) 的 `--version` 参数实现），输出形如 `babeldoc 0.6.3`。

### 4.2 babeldoc 命令与必需参数

#### 4.2.1 概念说明

装好之后，最小可用的翻译命令需要这几样东西：**要翻译的 PDF、一个翻译服务及其密钥、源/目标语言**。BabelDOC 目前在 CLI 层只内置了 **OpenAI 兼容**这一种翻译服务（README 明确说明更多翻译服务请用下游 PDFMathTranslate-next）。所以最小命令必然包含：

- `--openai`：声明「我用 OpenAI 兼容服务」（这是必需的开关）。
- `--openai-api-key`：API 密钥（必需）。
- `--openai-model` / `--openai-base-url`：模型名与服务地址（用本地 Ollama 时 base-url 指向本地，api-key 随便填）。
- `--files`：一个或多个 PDF 路径。
- `--lang-in` / `--lang-out`：源/目标语言码，默认 `en` → `zh`。

这五项凑齐就能翻译。其余几十个参数都是「调优 / 调试」用的，有默认值，不填也能跑。

#### 4.2.2 核心流程

一次完整的 CLI 翻译，在入口层大致经历：

```text
babeldoc --openai --openai-api-key K --files a.pdf
  └─ cli()                      # 初始化日志、关闭噪音 logger、init()
      └─ asyncio.run(main())
          └─ main()             # 解析参数 → 校验 → 建翻译器 → 建配置 → 跑主流程
              └─ async_translate(config)  # 真正的翻译主链路（后续单元精讲）
                  └─ 产出 mono.pdf / dual.pdf
```

其中 `main()` 会先做**两道强制校验**：必须选了某个翻译服务、选了 OpenAI 就必须给 api-key，否则直接报错退出。

#### 4.2.3 源码精读

`--files` 用 `action="append"`，所以多文件要写多次 `--files`：

[babeldoc/main.py:47-51](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L47-L51) —— `--files` 可以重复出现（`action="append"`），每次追加一个 PDF 路径。

默认语言是英译中：

[babeldoc/main.py:131-142](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L131-L142) —— `--lang-in` 默认 `en`，`--lang-out` 默认 `zh`。

两道强制校验在 `main()` 开头，不满足就 `parser.error` 直接退出：

[babeldoc/main.py:487-493](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L487-L493) —— 校验「必须选翻译服务（当前即 `--openai`）」与「用 OpenAI 必须提供 api-key」。

拿到合法参数后，构造 `OpenAITranslator` 实例：

[babeldoc/main.py:499-514](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L499-L514) —— 用 `lang_in/lang_out/model/base_url/api_key/ignore_cache` 等参数实例化 `OpenAITranslator`（翻译器本身在 u6-l1 精讲，这里只需知道它被建出来）。

每个待翻译文件还会被做一次存在性与扩展名校验，非 `.pdf` 或不存在直接 `exit(1)`：

[babeldoc/main.py:611-622](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L611-L622) —— 校验每个输入文件存在且以 `.pdf` 结尾，否则报错退出。

最后真正进入翻译主流程的是这一段（具体主链路留到 u2-l2 精讲）：

[babeldoc/main.py:744-755](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L744-L755) —— 在进度上下文里 `async for event in babeldoc.format.pdf.high_level.async_translate(config)`，逐事件消费，遇到 `finish` 取结果、遇到 `error` 打印并中止。

README 给出的官方最小示例正是这些必需参数的组合：

[README.md:78-83](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/README.md#L78-L83) —— `babeldoc --openai --openai-model "gpt-4o-mini" --openai-base-url "https://api.openai.com/v1" --openai-api-key "..." --files example.pdf`，并演示了多文件写两次 `--files`。

#### 4.2.4 代码实践

1. **实践目标**：用任意 OpenAI 兼容服务，翻译仓库自带的 `examples/ci/test.pdf`，得到 mono 与 dual 两份 PDF。
2. **操作步骤**：
   - 准备一个可用的 OpenAI 兼容服务（官方 OpenAI、DeepSeek、GLM 或本地 Ollama 均可）。本地 Ollama 时 `--openai-api-key` 可填任意值。
   - 在仓库根目录执行（**示例命令**，请把尖括号内容换成你自己的值）：
     ```bash
     babeldoc --openai \
       --openai-model <模型名，如 gpt-4o-mini> \
       --openai-base-url <服务地址，如 https://api.openai.com/v1> \
       --openai-api-key <你的 api-key> \
       --files examples/ci/test.pdf \
       --output ./out
     ```
   - 翻译过程中观察终端的进度条（由 `create_progress_handler` 渲染）。
3. **需要观察的现象**：终端依次出现各阶段进度（layout、translate 等阶段名，见后续单元）；`./out` 目录下生成以原文件名派生的 `*.mono.pdf`（单语译文）和 `*.dual.pdf`（双语对照）。
4. **预期结果**：得到两份 PDF，打开 dual PDF 能看到原文与译文并排（默认）排列，公式与版面基本保留。

> 待本地验证：实际产物文件名后缀取决于 BabelDOC 版本；若无可用 API key，可改用 `--only-parse-generate-pdf`（见 4.4 或 u7-l3）体验「不调用翻译服务、只解析重建 PDF」的流程，它不需要任何翻译服务。

#### 4.2.5 小练习与答案

**练习 1**：忘带 `--openai-api-key` 会发生什么？

> **参考答案**：`main()` 在 [babeldoc/main.py:492-493](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L492-L493) 处用 `parser.error(...)` 报「使用 OpenAI 服务时必须提供 API key」并以非零状态码退出，不会进入翻译。

**练习 2**：怎么让 BabelDOC 用本地 Ollama 跑的模型？

> **参考答案**：把 `--openai-base-url` 指向 Ollama 的 OpenAI 兼容端点（如 `http://localhost:11434/v1`），`--openai-model` 填 Ollama 里拉取的模型名，`--openai-api-key` 填任意非空字符串即可——这利用了 README 提到的「支持任意 OpenAI 兼容端点」特性。

### 4.3 create_parser 参数分组

#### 4.3.1 概念说明

`babeldoc --help` 会打印出一长串参数，但它们不是胡乱堆在一起的。`create_parser()` 用 `configargparse`（一个兼容 argparse、又能读 TOML 配置文件的库）把参数分成**若干有标题的组**，方便你按需查找：

- **顶层通用项**（不带组）：`--config`、`--version`、`--files`、`--debug`、`--warmup`、离线资源 `--generate/restore-offline-assets`、`--working-dir` 等。
- **Translation 组**：翻译与 PDF 处理相关，如 `--pages`、`--lang-in/out`、`--output`、`--qps`、`--no-dual/--no-mono`、水印模式、分片 `--max-pages-per-part` 等几十个选项。
- **Translation - OpenAI Options 组**：OpenAI 专属，如 `--openai-model`、`--openai-base-url`、`--openai-api-key`、术语抽取专用模型、JSON mode 开关等。

分组的意义：当你在 `--help` 里找「怎么改目标语言」，直接跳到 `Translation` 组；要换模型，跳到 `OpenAI Options` 组。这也对应 README「Advanced Options」里的小节划分。

#### 4.3.2 核心流程

`create_parser()` 的组织逻辑是「先建 parser，再 add 顶层参数，再 add 各 argument_group，再在各 group 上 add 具体参数」：

```text
configargparse.ArgParser(可读 TOML, 前缀 [babeldoc])
  ├─ add 顶层参数（-c/--config, --files, --debug, --warmup, 离线资源, ...）
  ├─ g1 = add_argument_group("Translation", ...)        # 翻译/PDF 选项组
  │     └─ g1.add_argument(--pages, --lang-in, --output, --qps, --no-dual, ...)
  ├─ 互斥组（g1 下）：service_group { --openai }          # 服务选择
  └─ g2 = add_argument_group("Translation - OpenAI Options", ...)  # OpenAI 专属
        └─ g2.add_argument(--openai-model, --openai-base-url, --openai-api-key, ...)
return parser
```

注意一个细节：`--openai` 是挂在 Translation 组下的**互斥组**里——目前虽然只有一个选项，但这个结构为将来支持更多翻译服务（互斥二选一）留好了位置。

#### 4.3.3 源码精读

parser 本体支持读 TOML 配置（前缀 `[babeldoc]`），这就是 `--config` 能用 TOML 文件的原因：

[babeldoc/main.py:32-41](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L32-L41) —— 用 `TomlConfigParser(["babeldoc"])` 创建 parser，并注册 `-c`/`--config` 作为配置文件入口。

Translation 组的创建与它的几个代表参数：

[babeldoc/main.py:116-124](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L116-L124) —— 创建名为 `Translation` 的参数组；组内第一个参数 `--pages`/`-p` 支持 `1,2,1-,-3,3-5` 这样的页码表达式。

QPS 限流默认值与水印模式选择也都在这个组里：

[babeldoc/main.py:148-154](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L148-L154) —— `--qps`/`-q` 默认 `4`，翻译时会据此限流。

[babeldoc/main.py:214-220](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L214-L220) —— `--watermark-output-mode` 三选一：`watermarked`（默认）/`no_watermark`/`both`。

OpenAI 服务开关与专属选项组：

[babeldoc/main.py:387-402](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L387-L402) —— `--openai` 在 Translation 组内的互斥组中；紧接着新建 `Translation - OpenAI Options` 组，第一个参数 `--openai-model` 默认 `gpt-4o-mini`。

[babeldoc/main.py:403-411](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L403-L411) —— `--openai-base-url` 与 `--openai-api-key`/`-k`。

函数末尾返回构造好的 parser：

[babeldoc/main.py:457-458](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L457-L458) —— `create_parser()` 返回完整的 parser 对象。

#### 4.3.4 代码实践

1. **实践目标**：通过 `--help` 直观感受参数分组，并对照源码确认每个分组标题的出处。
2. **操作步骤**：
   - 执行 `babeldoc --help`（或 `uv run babeldoc --help`）。
   - 在输出里定位三个层级：顶层无标题的通用项、标题为 `Translation` 的组、标题为 `Translation - OpenAI Options` 的组。
   - 打开源码 [babeldoc/main.py:116-119](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L116-L119) 与 [babeldoc/main.py:394-397](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L394-L397)，核对组标题字符串与帮助输出一致。
3. **需要观察的现象**：`--help` 输出中，`Translation` 组里的参数（`--pages`、`--qps` 等）与 `Translation - OpenAI Options` 组里的参数（`--openai-model` 等）被分组标题清晰隔开。
4. **预期结果**：能在帮助文本里找到与源码 `add_argument_group(...)` 字符串完全一致的组标题，建立「帮助文本 ↔ 源码定义」的对应关系。

#### 4.3.5 小练习与答案

**练习 1**：`--qps` 属于哪个参数组？默认值是多少？

> **参考答案**：属于 `Translation` 组（见 [babeldoc/main.py:148-154](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L148-L154)，挂在 `translation_group` 上），默认值 `4`。

**练习 2**：为什么 `--openai` 被放在「互斥组（mutually exclusive group）」里？

> **参考答案**：见 [babeldoc/main.py:388-388](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L388-L388) 的 `add_mutually_exclusive_group()`。翻译服务应当「多选一」，互斥组从结构上保证将来新增更多服务（如 `--google`、`--bing`）时，用户不会同时指定两个互相冲突的服务。

### 4.4 main 与 cli 入口

#### 4.4.1 概念说明

`pyproject.toml` 已经告诉我们 `babeldoc` = `babeldoc.main:cli`。但 `main.py` 里其实有**两个**关键函数，职责分明：

- `cli()`：**同步入口**，负责「环境准备」——配置日志格式（用 rich 美化输出）、把 httpx/openai/pdfminer/peewee 等噪音 logger 关掉、调用 `high_level.init()` 做全局初始化，最后用 `asyncio.run(main())` 把控制权交给异步主流程。
- `main()`：**异步主流程**，负责「业务」——解析参数、校验、建翻译器与版面模型、为每个文件建 `TranslationConfig`、调用 `async_translate(config)` 消费事件流。

这种「薄同步壳 + 异步核心」的写法，是因为翻译主链路是 `async` 的（要边翻译边产出进度事件），而命令行入口本身不需要是异步的——用 `asyncio.run` 桥接即可。

#### 4.4.2 核心流程

从敲下 `babeldoc ...` 到翻译结束，入口层的完整时序：

```text
用户: babeldoc --openai ... --files a.pdf
  │
  ▼
cli()                                   # 同步入口
  ├─ logging.basicConfig(rich 美化)     # 配置日志
  ├─ 关闭 httpx/openai/pdfminer 等噪音 logger
  ├─ speed_up_logs()                    # 日志走队列，避免阻塞
  ├─ babeldoc.format.pdf.high_level.init()  # 全局初始化
  └─ asyncio.run(main())                # ★ 进入异步
        │
        ▼
      main()                            # 异步主流程
        ├─ create_parser().parse_args() # 解析参数
        ├─ 校验 openai / api-key
        ├─ OpenAITranslator(...)        # 建翻译器
        ├─ set_translate_rate_limiter(qps)  # 限流
        ├─ 选版面模型 (RPC 或本地 DocLayoutModel.load_onnx())
        ├─ for file in pending_files:
        │     ├─ TranslationConfig(...) # 建配置对象
        │     ├─ create_progress_handler(config)
        │     └─ async for event in async_translate(config):  # ★ 真正翻译
        │           progress_handler(event)
        └─ 打印 token 用量统计
```

#### 4.4.3 源码精读

`cli()` 的入口定义与它做的环境准备：

[babeldoc/main.py:907-937](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L907-L937) —— `cli()` 用 `RichHandler` 配置日志，把 httpx/openai/httpcore 等设为 `CRITICAL` 并关闭一批噪音 logger，然后 `high_level.init()`，最后 `asyncio.run(main())`。这正是 `babeldoc` 命令真正执行的函数体。

`main()` 的开头：解析参数并按需切换 debug 日志：

[babeldoc/main.py:461-466](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L461-L466) —— `async def main()` 先 `create_parser()` 再 `parse_args()`，`--debug` 时把根 logger 调到 `DEBUG`。

`main()` 里对「暖机 / 离线资源」这类「只做一件事就退出」的快捷模式做了提前返回，这些模式根本不进入翻译：

[babeldoc/main.py:482-485](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L482-L485) —— `--warmup` 只下载并校验资源后 `return` 退出（与 u8-l1 的资源管理对应）。

建立翻译器后，立即设置限流，并按是否提供 RPC 地址选择版面模型（RPC 远程 vs 本地 ONNX）：

[babeldoc/main.py:541-575](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L541-L575) —— `set_translate_rate_limiter(args.qps)` 设限流；若给了任一 `--rpc-doclayout*` 用 RPC 模型，否则用本地 `DocLayoutModel.load_onnx()`。

为每个输入文件构造 `TranslationConfig`（这是贯穿全项目的中心配置对象，u1-l4 专讲）：

[babeldoc/main.py:678-732](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L678-L732) —— 把解析出来的 `args.*` 逐项灌进 `TranslationConfig(...)`，形成后续主流程唯一的输入。

最后还有一个特别的「无翻译服务」入口：`--only-parse-generate-pdf`，它只解析 PDF 并重建输出 PDF、跳过一切翻译相关步骤，因此**不需要任何 API key**——很适合在本讲用来验证「安装能跑」：

[babeldoc/main.py:357-362](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L357-L362) —— `--only-parse-generate-pdf` 的参数定义：跳过版面分析、段落识别、样式处理与翻译本身，仅做解析重建。

> 注意：即便用 `--only-parse-generate-pdf`，由于 [babeldoc/main.py:488-493](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L488-L493) 仍要求 `--openai` 与 api-key，最省事的「零服务」验证仍是 `babeldoc --warmup`（[babeldoc/main.py:482-485](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L482-L485)），它只会下载校验资源然后退出。`--only-parse-generate-pdf` 的真正用法留到 u7-l3 精讲。

#### 4.4.4 代码实践

1. **实践目标**：用零依赖翻译服务的方式验证安装可跑，并跟踪从命令到主流程的入口调用链。
2. **操作步骤**：
   - 执行 `babeldoc --warmup`，观察它下载并校验字体/模型等资源（无需任何 API key）。
   - 资源落点在缓存目录 [babeldoc/const.py:11](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/const.py#L11)（`~/.cache/babeldoc`），`ls ~/.cache/babeldoc` 查看产物。
   - 在源码中按顺序打开 [babeldoc/main.py:907](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L907)（`cli`）→ [babeldoc/main.py:461](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L461)（`main`）→ [babeldoc/main.py:745](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L745)（`async_translate`），手绘这条入口调用链。
3. **需要观察的现象**：`--warmup` 结束打印 `Warmup completed, exiting...`（对应 [babeldoc/main.py:484](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L484)）；缓存目录出现字体、ONNX 模型、tiktoken 等子目录/文件。
4. **预期结果**：确认「`babeldoc` 命令 → `cli()` → `asyncio.run(main())` → `async_translate(config)`」这条入口链路，并能解释每一步的职责。

> 待本地验证：`--warmup` 是否真正下载取决于网络；若已缓存会快速跳过。

#### 4.4.5 小练习与答案

**练习 1**：`cli()` 为什么要关掉 httpx、openai、pdfminer 这些 logger？

> **参考答案**：这些第三方库默认会产生大量 DEBUG/INFO 日志（HTTP 请求明细、解析细节），会把 BabelDOC 自己的进度与提示淹没。`cli()` 在 [babeldoc/main.py:913-933](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L913-L933) 把它们设为 `CRITICAL` 并禁用传播，保证终端只显示对用户有用的信息。

**练习 2**：`main()` 是 `async def`，但 `babeldoc` 命令本身不是协程，二者如何衔接？

> **参考答案**：`cli()` 用 [babeldoc/main.py:937](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L937) 的 `asyncio.run(main())` 创建事件循环并运行异步主流程，循环结束即退出。这是「同步入口壳 + 异步业务核心」的标准衔接方式。

## 5. 综合实践

把本讲四个模块串起来，完成一次「从安装到产出」的完整闭环：

1. 用 `uv tool install --python 3.12 BabelDOC` 安装，运行 `babeldoc --version` 确认版本 `0.6.3`（对应 [babeldoc/__init__.py:1](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/__init__.py#L1)）。
2. 运行 `babeldoc --help`，在输出里圈出 `Translation` 组与 `Translation - OpenAI Options` 组的标题，并在源码 [babeldoc/main.py:116](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L116) 与 [babeldoc/main.py:394](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L394) 找到它们的定义。
3. 用一个 OpenAI 兼容服务翻译 `examples/ci/test.pdf`（命令见 4.2.4），在 `--output ./out` 得到 mono/dual PDF。
4. 翻译过程中打开另一个终端，对照 [babeldoc/main.py:744-755](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L744-L755) 思考：你看到的进度条，正是这段 `async for event in async_translate(config)` 消费事件、再交给 `progress_handler` 渲染出来的。
5. 最后写一句话总结：从敲下 `babeldoc` 到拿到 PDF，命令依次经过了 `cli()` → `main()` → `async_translate()` 三个层次。

> 如果没有可用的翻译服务，第 3 步可替换为 `babeldoc --warmup` 完成资源准备（验证安装与入口），翻译步骤留到你有 API key 时再补。

## 6. 本讲小结

- BabelDOC 官方推荐用 `uv tool install --python 3.12 BabelDOC` 安装，命令 `babeldoc` 由 `pyproject.toml` 的入口点 `babeldoc.main:cli` 注册。
- 最小可用翻译命令需要 `--openai`（服务开关）、`--openai-api-key`、`--files`，以及默认 `en→zh` 的语言参数；CLI 层目前只内置 OpenAI 兼容服务。
- `create_parser()` 用 `configargparse` 把参数分成顶层项、`Translation`、`Translation - OpenAI Options` 三层，且支持 `[babeldoc]` TOML 配置文件。
- 入口分两层：同步壳 `cli()`（日志/初始化/桥接）与异步核心 `main()`（解析/校验/建翻译器与配置/跑主流程），靠 `asyncio.run` 衔接。
- `main()` 里有 `--warmup`、`--only-parse-generate-pdf` 等「快捷模式」会提前返回，其中 `--warmup` 无需任何 API key，最适合验证安装。
- 所有参数最终汇聚成 `TranslationConfig` 对象喂给 `async_translate(config)`，这条链路是后续所有单元的起点。

## 7. 下一步学习建议

你已经能让 BabelDOC 跑起来、并看懂了 CLI 入口结构。接下来：

- **u1-l3 目录结构与入口文件**：从 `main.cli()` 一路追到 `babeldoc.format.pdf.high_level`，建立全局目录地图，弄清 `document_il`、`new_parser`、`docvision`、`translator` 这些子包各管什么。
- **u1-l4 配置体系**：本讲多次出现的 `TranslationConfig` 是全项目中心，下一讲会专门讲 CLI 参数、TOML 配置与它三者的关系。
- 若想先尝鲜「翻译主链路长什么样」，可以跳读 [babeldoc/main.py:744-755](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L744-L755) 调用的 `async_translate`，但完整精读建议放到 u2-l2。

# 项目定位、技术栈与设计理念

> 本讲是 OpenLLM 学习手册的第一篇。如果你还不知道 OpenLLM 是什么、它和 BentoML/vLLM/OpenAI SDK/uv 各自负责什么、为什么运行模型还需要一个 `HF_TOKEN`,那么这一篇就是为你准备的。本讲不要求你读过任何源码,我们将从 README 和 `pyproject.toml` 两个文件入手。

## 1. 本讲目标

读完本讲后,你应该能够:

- 用一句话说清 OpenLLM 解决的核心问题,以及它提供的能力(serve / run / deploy、Chat UI、模型仓库)。
- 画出「OpenLLM ↔ BentoML ↔ vLLM ↔ OpenAI SDK ↔ uv」五者之间的分工关系,知道哪一层是谁在干活。
- 看懂 `pyproject.toml` 里声明了哪些依赖,并能把这些依赖对应到 OpenLLM 的具体功能。
- 说清楚什么是「gated 模型」,为什么运行这类模型必须设置 `HF_TOKEN`。
- 认识 `openllm serve llama3.2:1b` 这条命令背后涉及的开源项目。

## 2. 前置知识

本讲面向零基础读者,但有几个名词最好先有个模糊印象,不需要深入:

| 名词 | 通俗解释 |
| --- | --- |
| LLM(大语言模型) | 像 Llama、Qwen、Mistral 这类可以对话/续写文本的 AI 模型。 |
| 推理(inference) | 用训练好的模型去「生成回答」这个过程,区别于「训练」。 |
| OpenAI 兼容 API | 一套和 OpenAI 官方接口格式一致的 HTTP 接口(`/v1/chat/completions` 等)。只要服务端兼容这套接口,任何 OpenAI 客户端都能直接连。 |
| CLI(命令行工具) | 在终端里通过命令运行的程序,例如 `openllm serve ...`。 |
| Python 包(pyproject.toml) | 一个 Python 项目的「身份证 + 装配图」,声明名字、依赖、入口命令等。 |

如果你对「模型权重」「Hugging Face」这些词完全陌生也不用担心,本讲会在用到时解释。

## 3. 本讲源码地图

本讲只涉及两个文件,它们是认识 OpenLLM 的最佳入口:

| 文件 | 作用 |
| --- | --- |
| `README.md` | 面向用户的项目说明书:OpenLLM 是什么、支持哪些模型、怎么 serve/run/deploy、需要什么鉴权、用到了哪些开源项目。 |
| `pyproject.toml` | 项目的技术档案:包名、Python 版本要求、全部运行依赖、控制台命令 `openllm` 是如何被定义出来的。 |

在讲解时,我们还会顺带看一眼 `src/openllm/__main__.py` 的开头几行,用来确认「`openllm` 这个命令到底指向哪段代码」。它只是旁证,本讲不深入。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块:项目简介与能力边界、依赖与技术栈、支持的模型与鉴权要求。

### 4.1 项目简介与能力边界

#### 4.1.1 概念说明

OpenLLM 的定位可以用 README 开头一句话概括:

> OpenLLM allows developers to run **any open-source LLMs** ... or **custom models** as **OpenAI-compatible APIs** with a single command.

也就是说,它解决的核心问题是:**把一个开源大模型,「一条命令」地变成一个对外提供 OpenAI 兼容接口的服务**。你不需要自己写服务端代码、不需要自己拼推理引擎、不需要自己处理依赖安装——OpenLLM 把这些封装成了几个 CLI 命令。

它对外暴露的能力主要有四类:

1. **serve**:在本地启动一个 OpenAI 兼容的对话服务(默认监听 `http://localhost:3000`),并带一个浏览器 Chat UI。
2. **run**:在终端里直接和一个模型多轮对话,适合快速试用。
3. **deploy**:把模型部署到云端(BentoCloud),得到一个生产可用的服务。
4. **hello**:一个交互式引导,帮你一步步挑模型、挑版本、挑动作——这是新手最友好的入口。

除此之外还有一组「管理类」子命令:`repo`(管理模型仓库)、`model`(查看模型)、`clean`(清理磁盘缓存)。

需要强调的是 OpenLLM 的**能力边界**:它本身不训练模型,也**不存储模型权重**(README 里明确写了 "OpenLLM does not store model weights")。它更像一个「编排者/胶水层」,把模型仓库、推理后端、服务框架这些组件粘合在一起。理解这一点,后面看到它依赖一大堆外部项目就不会奇怪。

#### 4.1.2 核心流程

从用户视角,OpenLLM 的使用主链路是线性的:

```text
安装 openllm
   │
   ├── openllm hello          # 交互式:探测硬件 → 列模型 → 选版本 → 选动作
   │
   ├── openllm serve <model>  # 本地起一个 OpenAI 兼容服务 + Chat UI
   │        │
   │        └── 任意 OpenAI 客户端 ──HTTP──> localhost:3000/v1/chat/completions
   │
   ├── openllm run <model>    # 终端里直接和模型对话
   │
   └── openllm deploy <model> # 部署到 BentoCloud
```

关键在于:无论选哪条路,用户面对的只是一个 `openllm xxx` 命令;真正「下载权重、加载模型、起服务」这些重活,都由 OpenLLM 在背后调用其他开源项目来完成(详见 4.2)。

#### 4.1.3 源码精读

README 第一段就给出了项目的核心定位(高亮「any open-source LLMs / OpenAI-compatible APIs / single command」三个关键词):

- [README.md:L13](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/README.md#L13) —— 一句话定义 OpenLLM 的核心能力:任意开源 LLM 一条命令变成 OpenAI 兼容 API,并提到内置 Chat UI、推理后端、云端部署工作流。

最简上手方式只有两行(`pip install` + `openllm hello`):

- [README.md:L21-L24](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/README.md#L21-L24) —— 安装与交互式探索入口。

要确认「`openllm` 这个命令到底来自哪里」,可以看 `pyproject.toml` 的脚本入口定义:

- [pyproject.toml:L73-L74](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/pyproject.toml#L73-L74) —— `[project.scripts]` 把控制台命令 `openllm` 指向 `openllm.__main__:app`,即 `src/openllm/__main__.py` 里的 `app` 对象。

而这个 `app` 是一个 Typer 应用,并在开头注册了三个子命令组:

- [src/openllm/__main__.py:L19-L27](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/__main__.py#L19-L27) —— 创建 `OpenLLMTyper`(Typer 应用),并用 `add_typer` 注册 `repo`/`model`/`clean` 三个子命令组;`hello`/`serve`/`run`/`deploy` 则直接作为顶层命令挂在这个 app 上(本讲先不展开,后续讲义会逐条精读)。

#### 4.1.4 代码实践

1. **实践目标**:确认 OpenLLM 安装后会得到哪些命令,并验证命令入口与源码的对应关系。
2. **操作步骤**:
   - 阅读 [README.md:L21-L24](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/README.md#L21-L24) 了解推荐安装方式。
   - (可选,待本地验证)执行 `pip install openllm`,然后运行 `openllm --help`。
3. **需要观察的现象**:`--help` 输出里应能看到 `hello`、`serve`、`run`、`deploy` 四个顶层命令,以及 `repo`、`model`、`clean` 三个子命令组。
4. **预期结果**:命令列表与 [src/openllm/__main__.py:L19-L27](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/__main__.py#L19-L27) 中注册的结构一致。
5. 如果没有装好环境,**不必运行**,改成「源码阅读型实践」:对照 `__main__.py` 第 19–27 行,在纸上把命令树画出来即可。

#### 4.1.5 小练习与答案

**练习 1**:OpenLLM 自己会把 Llama 的模型权重打包进安装包吗?

> **答案**:不会。OpenLLM 是「编排者」,不在安装包里存放模型权重,运行时才去模型仓库/权重源拉取。这也是 4.3 要讲的鉴权问题的由来。

**练习 2**:用户想「在浏览器里和一个本地模型聊天」,应该用哪条命令?想在终端里聊呢?

> **答案**:浏览器聊天用 `openllm serve`(它会起一个带 `/chat` 页面的服务);终端聊天用 `openllm run`。

### 4.2 依赖与技术栈

#### 4.2.1 概念说明

OpenLLM 之所以能「一条命令搞定一切」,是因为它站在几个成熟开源项目的肩膀上。理解技术栈,关键不是背依赖清单,而是搞清楚**每个项目在哪一层出力**:

| 开源项目 | 在 OpenLLM 中的角色 | 通俗类比 |
| --- | --- | --- |
| **BentoML** | 服务框架:把模型打包成可部署单元(Bento),并提供 `bentoml serve`/`bentoml deploy` 的生产能力。 | 「厨房」:负责把菜做好并端上桌。 |
| **vLLM** | 推理后端:真正高效跑模型推理的引擎(通常作为 Bento 里的服务实现)。 | 「灶台/厨师」:真正把食材做成菜。 |
| **OpenAI SDK(`openai`)** | 客户端协议:`run` 命令用它连到本地服务做对话;同时也定义了服务端要兼容的接口格式。 | 「菜单/点餐话术」:大家都按这套格式点单。 |
| **uv** | 极快的依赖安装器:OpenLLM 用它为每个模型快速创建虚拟环境并装依赖。 | 「采购员」:飞速备齐食材。 |
| **Typer / questionary / tabulate** | CLI 框架与交互式 UI:构建命令、交互选择、表格展示。 | 「点餐机界面」。 |
| **nvidia-ml-py / psutil** | 硬件探测:读取本机 GPU 型号与显存,判断模型「能不能跑」。 | 「后厨盘点库存」。 |
| **dulwich** | 纯 Python 的 git 库:用于克隆/更新模型仓库(以 git 仓库形式存在)。 | 「仓库搬运工」。 |

一句话总结分工:**OpenLLM 是指挥官,BentoML 负责服务化,vLLM 负责推理,uv 负责备环境,OpenAI SDK 负责对话协议**。

#### 4.2.2 核心流程

以 `openllm serve llama3.2:1b` 为例,可以把各层串起来看(本讲只看「谁负责什么」,具体调用链在后续讲义):

```text
openllm serve llama3.2:1b          # OpenLLM CLI(Typer)
        │
        ├─ questionary/tabulate    # (hello 模式)交互式选模型
        ├─ nvidia-ml-py            # 探测本机 GPU,判断能否运行
        ├─ dulwich                 # 克隆/更新模型仓库,拿到 bento 元信息
        ├─ uv                      # 为该模型创建独立 venv 并安装依赖
        ├─ bentoml                 # 真正执行 `bentoml serve` 起服务
        │     └─ vllm              # 作为推理后端跑模型
        └─ openai (客户端)         # run 模式下连到本地服务对话
```

注意:服务一旦起来,它对外暴露的是 **OpenAI 兼容**接口,所以**任何**支持 OpenAI 协议的客户端(OpenAI Python SDK、LlamaIndex 等)都能直接连——这正是 OpenLLM「生态友好」的根源。

#### 4.2.3 源码精读

所有运行依赖都声明在 `pyproject.toml` 的 `dependencies` 列表里:

- [pyproject.toml:L32-L48](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/pyproject.toml#L32-L48) —— 完整依赖清单。可以看到 `bentoml==1.4.23`、`typer`、`questionary`、`tabulate`、`uv`、`openai==1.90.0`、`nvidia-ml-py`、`dulwich`、`huggingface-hub` 等,与本节角色表一一对应。

其中两个依赖是**被锁定版本**的(用 `==`),说明 OpenLLM 对它们的行为非常敏感:

- `bentoml==1.4.23`:服务框架,版本必须精确匹配,否则 `serve`/`deploy` 的命令格式可能变化。
- `openai==1.90.0`:客户端 SDK,用于 `run` 命令的对话。

README 末尾的「Acknowledgements」也官方点明了它用了哪些项目(与依赖清单互为印证):

- [README.md:L285-L291](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/README.md#L285-L291) —— 致谢列表:bentoml(模型服务)、vllm(LLM 后端)、chatgpt-lite(Web Chat UI)、uv(快速依赖安装)。

另外,`pyproject.toml` 还声明了最低 Python 版本:

- [pyproject.toml:L71](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/pyproject.toml#L71) —— `requires-python = ">=3.9"`,安装前需满足。

#### 4.2.4 代码实践

1. **实践目标**:把 `pyproject.toml` 的依赖逐条对应到 OpenLLM 的功能点,建立「依赖 ↔ 功能」的心智模型。
2. **操作步骤**:
   - 打开 [pyproject.toml:L32-L48](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/pyproject.toml#L32-L48)。
   - 对每个依赖,在笔记里写一句话:它支撑了 OpenLLM 的哪个功能(例如 `nvidia-ml-py` → 探测 GPU 判断可运行性;`dulwich` → 克隆模型仓库)。
   - 对照 [README.md:L285-L291](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/README.md#L285-L291) 的致谢,看看哪些项目被官方「点名」。
3. **需要观察的现象**:你会发现致谢里重点提到的 4 个项目(bentoml/vllm/chatgpt-lite/uv)是技术栈的「主干」,其余依赖是支撑性工具。
4. **预期结果**:能画出 4.2.2 那张分层调用图,并标出每层用到的依赖。

#### 4.2.5 小练习与答案

**练习 1**:`bentoml` 和 `openai` 为什么用 `==` 锁死版本,而 `typer` 没有?

> **答案**:OpenLLM 直接调用 bentoml 的 `serve`/`deploy` 子命令、直接用 openai 客户端 API,这两者的命令格式或接口在版本间可能变化,锁版本可保证行为稳定;typer 是 CLI 框架,接口相对稳定,允许浮动版本。

**练习 2**:如果没有 `uv`,OpenLLM 还能跑吗?

> **答案**:理论上能找到替代的依赖安装方式,但 OpenLLM 在源码里(后续 venv 讲义会看到)直接用 `uv venv` / `uv pip install` 来为每个模型准备环境,所以实际运行强依赖 uv;它带来的好处是「极快地安装模型所需的大量依赖」。

### 4.3 支持的模型与鉴权要求

#### 4.3.1 概念说明

OpenLLM 不是「凭空」知道有哪些模型可用的。它依赖一个**模型仓库(model repository)**的概念:一个以 git 仓库形式存在的「模型目录」,里面按约定存放每个模型的可部署描述(Bento)。OpenLLM 默认连到官方仓库 `bentoml/openllm-models`,你也可以 `openllm repo add` 添加自己的仓库。

关于「支持哪些模型」,README 给了一张大表(deepseek、gemma、llama3.x、mistral、qwen、phi4 等)。注意表里每行都标了 **Required GPU**(如 `24G`、`80Gx8`),这其实是模型对应 Bento 的资源要求,OpenLLM 会拿它和你本机探测到的 GPU 做匹配(可运行性判定)。

关于**鉴权**,有一个新手常踩的坑:

- 模型权重实际托管在 **Hugging Face** 上。
- 有些模型是 **gated 模型**(受限模型,例如 Meta 的 Llama 系列):你需要先在 Hugging Face 上**申请访问权限**,并提供一个 **HF_TOKEN**(访问令牌)才能下载权重。
- OpenLLM「不存权重」,所以下载权重的鉴权就落到用户头上:必须设置 `HF_TOKEN` 环境变量。

这就是为什么 README 反复强调 `HF_TOKEN`。

#### 4.3.2 核心流程

运行一个 gated 模型的完整鉴权链路:

```text
1. 去 huggingface.co/settings/tokens 创建一个 access token
2. 去 gated 模型页面(如 meta-llama/Llama-3.2-1B-Instruct)申请访问权限
3. 在终端设置:export HF_TOKEN=<your token>
4. 运行:openllm serve llama3.2:1b
        │
        └─ OpenLLM 在背后用 HF_TOKEN 去下载该模型的权重 ──> 本地起服务
```

如果是**部署到云端**(`openllm deploy`),则需要在部署命令里把 token 传上去,让云端环境也能下载权重:

```bash
openllm deploy llama3.2:1b --env HF_TOKEN
```

`--env HF_TOKEN` 的作用是把当前环境里的 `HF_TOKEN` 透传给云端部署环境(具体机制在后续 cloud.py 讲义展开)。

#### 4.3.3 源码精读

README 的支持模型表列出了主流开源 LLM 及其 GPU 要求:

- [README.md:L28-L129](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/README.md#L28-L129) —— 支持模型表(节选):例如 `llama3.2 1b 需要 24G`、`deepseek r1-671b 需要 80Gx16`。每行给出模型名、参数量、所需 GPU、对应 `openllm serve` 命令。

关于权重的鉴权说明(关键的 NOTE 段落):

- [README.md:L137-L149](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/README.md#L137-L149) —— 明确「OpenLLM does not store model weights」,并给出 gated 模型获取 `HF_TOKEN` 的三步:建 token → 申请访问 → `export HF_TOKEN=...`,随后才是 `openllm serve llama3.2:1b`。

部署到 BentoCloud 时同样需要 token:

- [README.md:L259-L264](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/README.md#L259-L264) —— `openllm deploy llama3.2:1b --env HF_TOKEN`,并提示部署 gated 模型务必设置 `HF_TOKEN`。

模型仓库(可扩展自定义模型)的入口:

- [README.md:L215-L251](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/README.md#L215-L251) —— 介绍默认模型仓库、`openllm model list`、`openllm repo update`,以及如何用 `openllm repo add <repo-name> <repo-url>` 添加自定义仓库(仅支持公开仓库)。

#### 4.3.4 代码实践

1. **实践目标**:把「鉴权链路」和「模型表」串起来,理解为什么 `openllm serve llama3.2:1b` 在没有 `HF_TOKEN` 时可能失败。
2. **操作步骤**:
   - 阅读 [README.md:L28-L129](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/README.md#L28-L129),找到 `llama3.2` 这一行,记录它的参数量与所需 GPU。
   - 阅读 [README.md:L137-L149](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/README.md#L137-L149),把获取 `HF_TOKEN` 的三步写下来。
   - (可选,待本地验证)如果你手头有 Hugging Face 账号和 token,尝试 `export HF_TOKEN=<token>` 后查看环境变量是否生效:`echo $HF_TOKEN`。
3. **需要观察的现象**:理解「`llama3.2` 属于 gated 模型,权重在 Hugging Face,需要 token 才能下载」这一事实链条。
4. **预期结果**:能解释「为什么即便 OpenLLM 装好了,第一次 `serve llama3.2:1b` 仍可能因为缺 token 而下载失败」。
5. 本练习**不要求真的下载权重**(体积大、需 GPU),重点是理清鉴权逻辑。

#### 4.3.5 小练习与答案

**练习 1**:同样是 `openllm serve`,为什么有的模型不用设 `HF_TOKEN`,有的必须设?

> **答案**:取决于该模型在 Hugging Face 上是否是 gated(受限)模型。gated 模型需要先申请访问权限并凭 `HF_TOKEN` 鉴权下载;公开模型不需要。

**练习 2**:`openllm deploy llama3.2:1b --env HF_TOKEN` 里的 `--env HF_TOKEN` 起什么作用?为什么本地 `serve` 时只 `export` 就行,部署时却要显式 `--env`?

> **答案**:`--env HF_TOKEN` 把本地环境里的 `HF_TOKEN` 透传给**云端**部署环境。本地 `serve` 时进程就在你当前 shell 里,能直接读到 `export` 的变量;而云端是另一台机器,必须显式把变量传过去,否则云端下载 gated 权重会失败。

## 5. 综合实践

把本讲三个模块串起来,完成下面这个综合任务(这也是本讲的核心实践):

> **任务**:阅读 [README.md](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/README.md) 与 [pyproject.toml](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/pyproject.toml),写一段话回答两个问题:
>
> 1. `openllm serve llama3.2:1b` 这条命令,分别依赖了哪几个开源项目?各自负责什么?
> 2. 为什么运行 gated 模型(如 Llama 3.2)需要 `HF_TOKEN`?请从「OpenLLM 不存权重」这一事实出发解释。

**参考作答思路**(自己先写,再对照):

- 问题 1:命令由 **Typer** 构建的 CLI 接收;OpenLLM 用 **nvidia-ml-py** 探测本机 GPU 判断能否运行,用 **dulwich** 从模型仓库(git 仓库)拉取该模型的 Bento 描述;用 **uv** 为模型创建虚拟环境并装依赖;最终调用 **BentoML** 的 `serve` 起一个服务,服务内部用 **vLLM** 作为推理后端跑 Llama 3.2;对外暴露 **OpenAI 兼容**接口(`run` 模式下还会用 **openai** 客户端去连)。依据见 [pyproject.toml:L32-L48](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/pyproject.toml#L32-L48) 与 [README.md:L285-L291](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/README.md#L285-L291)。
- 问题 2:OpenLLM 只负责编排,不打包权重([README.md:L137-L149](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/README.md#L137-L149));Llama 3.2 权重在 Hugging Face 上且为 gated 模型,必须凭 `HF_TOKEN` 鉴权才能下载。

## 6. 本讲小结

- OpenLLM 的核心定位:把任意开源 LLM「一条命令」变成 OpenAI 兼容 API,提供 serve/run/deploy/hello 与 repo/model/clean 等命令。
- 它是一个「编排者/胶水层」,**不存储模型权重**,运行时才去拉取权重与元信息。
- 技术栈分工:BentoML 做服务化、vLLM 做推理、uv 备环境、OpenAI SDK 定对话协议,Typer/questionary/tabulate 做 CLI 与交互,nvidia-ml-py 做硬件探测,dulwich 管 git 模型仓库。
- 依赖清单见 `pyproject.toml`,其中 `bentoml` 和 `openai` 被锁死版本,体现对这两个项目接口的强依赖。
- 模型以「模型仓库(git 仓库)」形式组织,默认连官方 `openllm-models`,可 `repo add` 自定义。
- gated 模型权重在 Hugging Face,运行/部署前必须设置 `HF_TOKEN` 才能下载。

## 7. 下一步学习建议

本讲只看了「门面」(README + pyproject.toml)。下一讲 **u1-l2「安装、目录结构与运行方式」** 会带你走进仓库内部:

- 看 `src/openllm` 的模块布局(`__main__.py`、`common.py`、`repo.py` 等 9 个模块分别干嘛)。
- 理解 `openllm` 控制台命令是如何由 `[project.scripts]` 与 hatch 构建出来的。
- 认识运行时在用户主目录下创建的 `OPENLLM_HOME`(repos / venv / temp / config.json)。

建议在进入下一讲前,先完成本讲第 5 节的综合实践,确保你能把 `openllm serve` 这条命令和它背后的开源项目对应清楚。

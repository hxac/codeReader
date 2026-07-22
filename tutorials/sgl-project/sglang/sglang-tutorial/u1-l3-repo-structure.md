# 仓库目录结构总览

## 1. 本讲目标

读完本讲，你应该能够：

- 看清 SGLang 仓库顶层每个目录（`python/`、`sgl-kernel/`、`rust/`、`sgl-model-gateway/`、`benchmark/`、`test/`、`docs_new/`、`examples/` 等）各自负责什么。
- 在 `python/sglang` 包里，区分**前端语言 `lang`** 与**后端运行时 `srt`** 两大子模块，并理解它们的分工。
- 读懂 `python/sglang/__init__.py` 这个「公开 API 门面」导出了哪些符号，以及 `Engine`/`ServerArgs`/`Runtime` 等名字从哪里来。
- 在 `srt` 下快速定位本手册后续单元要讲的子系统（`managers`、`mem_cache`、`model_executor`、`layers`、`sampling`、`disaggregation`、`speculative` 等）。
- 亲手画出一张到二级的目录树，并为每个关键目录写一句职责说明。

本讲是「**地图课**」：不深挖任何子系统，只帮你在脑子里建立一张「要找的东西放在哪」的索引。后续每一讲都会反复用到这张地图。

## 2. 前置知识

本讲默认你已经读过 **u1-l1（SGLang 是什么）**，知道：

- SGLang 是一个**高性能推理服务框架**，不是训练框架。
- 它的核心特性（RadixAttention、零开销调度器、PD 分离、投机解码、连续批处理等）来自 README 的 `About` 章节。
- 一条请求大致会经过 TokenizerManager → Scheduler → ModelRunner → Sampler 的链路。

如果你还没建立这个全局印象，建议先回到 u1-l1。另外，本讲会用到几个工程常识：

- **「公开 API（public API）」**：一个库对外承诺稳定、用户可以直接 `from sglang import xxx` 使用的接口集合。它通常集中在一个包的 `__init__.py` 里。
- **「门面（facade）」**：一个看起来内容不多、但实际把内部实现「转手」暴露出去的薄层。`__init__.py` 就是 SGLang 的门面。
- **「运行时（runtime）」**：真正执行计算的那部分代码（建模、调度、显存管理）。SGLang 把它放在 `srt` 子包里，`srt` 是 **S**GLang **R**un**T**ime 的缩写。
- **「DSL（Domain-Specific Language，领域特定语言）」**：为某个领域量身定制的「小语言」。SGLang 的前端 DSL 让你用 Python 原语（`gen`/`select`/`function`）写出结构化的生成程序。

## 3. 本讲源码地图

本讲涉及的「源码」主要是**目录与门面文件**，而不是某段算法逻辑：

| 路径 | 作用 |
| --- | --- |
| `README.md` | 项目的「门面说明」，`About` 章节列出了核心特性与定位。 |
| `python/pyproject.toml` | Python 包配置，`[project.scripts]` 定义了 `sglang` 命令行入口。 |
| `python/sglang/__init__.py` | 包的**公开 API 门面**，定义 `sglang.Engine`、`sglang.gen` 等顶层符号。 |
| `python/sglang/srt/` | 后端运行时目录（本讲重点画它的子系统布局）。 |
| `python/sglang/lang/` | 前端 DSL 目录。 |
| `sgl-kernel/`、`rust/`、`sgl-model-gateway/`、`benchmark/`、`test/`、`docs_new/`、`examples/` | 顶层其它职能目录。 |

> 说明：本讲引用的是「目录与配置文件」级别的源码，因此永久链接多指向 `__init__.py`、`pyproject.toml`、`README.md` 这类门面文件的具体行号；目录本身的「代码」是它们内部成百上千个文件，我们只画结构，不逐个引用。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

1. **4.1 仓库顶层目录布局**——回答「`git clone` 下来后，根目录里这一堆文件夹分别是什么？」
2. **4.2 `python/sglang` 包：`lang` 与 `srt` 的分工**——回答「真正写代码时，我要改哪个子包？」
3. **4.3 顶层 `__init__.py` 的公开 API**——回答「`import sglang` 之后我能拿到什么？」
4. **4.4 `srt` 运行时子系统目录布局**——回答「调度、缓存、前向、采样、分布式……各在哪个子目录？」

### 4.1 仓库顶层目录布局

#### 4.1.1 概念说明

SGLang 是一个**多语言、多子工程**的大型项目：核心运行时是 Python，但高性能算子用 CUDA/C++（`sgl-kernel`），部分通信/多媒体组件用 Rust（`rust`），网关用 Go（`sgl-model-gateway`）。因此它的仓库不是「单一 Python 包」，而是「**一个 monorepo（单仓库多工程）**」。

理解顶层布局的原则是：**先按「语言/职能」分大类，再在每个大类里找细节**。具体来说：

- **核心代码**：`python/`（几乎所有 Python 逻辑都在这里，包括 `sglang` 包）。
- **算子工程**：`sgl-kernel/`（提前编译好的 CUDA/C++ 算子）。
- **其它语言组件**：`rust/`、`sgl-model-gateway/`。
- **使用与验证**：`examples/`（示例）、`benchmark/`（基准测试脚本）、`test/`（测试套件）。
- **文档与运维**：`docs_new/`（文档站，含 cookbook）、`scripts/`（开发脚本）、`docker/`、`proto/`（protobuf 定义）、`experimental/`（实验性代码）、`3rdparty/`（第三方）。

#### 4.1.2 核心流程

当你想在仓库里找一个东西时，推荐的「定位流程」是：

```text
想找的东西               先去哪个目录
─────────────────────────────────────────────
一段 Python 业务逻辑   →  python/sglang/  （继续进 srt 或 lang）
一个 CUDA 算子实现     →  sgl-kernel/csrc/
一个示例怎么用         →  examples/
一个跑分脚本           →  benchmark/
一个已有测试           →  test/
文档 / 部署指南        →  docs_new/
命令行入口 sglang      →  python/pyproject.toml 的 [project.scripts]
```

#### 4.1.3 源码精读

**入口命令 `sglang` 的来源**。我们关心的是：在终端敲 `sglang serve` 时，到底运行了哪个 Python 函数？答案写在 `python/pyproject.toml` 的 `[project.scripts]` 里：

[python/pyproject.toml:188-191](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/pyproject.toml#L188-L191) ——这段配置注册了两个控制台命令：`sglang` 映射到 `sglang.cli.main:main`，`killall_sglang` 映射到 `sglang.cli.killall:main`。也就是说，`sglang` 这个 shell 命令的真实入口是 `python/sglang/cli/main.py` 里的 `main()` 函数（u1-l2 已经讲过它的分发过程）。

**项目自我介绍**。README 的 `About` 章节是理解顶层布局的「题眼」，因为它把 SGLang 的定位和核心特性集中列出：

[README.md:61-71](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/README.md#L61-L71) ——这里说明了 SGLang 是「高性能推理服务框架」，并枚举了 `Fast Runtime`、`Broad Model Support`、`Extensive Hardware Support`、`Active Community`、`RL & Post-Training Backbone` 五个能力维度。这些维度几乎都能在顶层目录里找到对应：`Fast Runtime` 的实现散落在 `python/sglang/srt/` 的各子系统，`Broad Model Support` 体现在 `python/sglang/srt/models/`（本仓库有 **212 个模型文件**），`Extensive Hardware Support` 则体现在 `python/` 下并列的多个 `pyproject_*.toml`（CPU/NPU/XPU/ROCm 等硬件变体）。

#### 4.1.4 代码实践

**实践目标**：用 `git` 命令亲手核验顶层目录，而不是只读本讲义。

1. **操作步骤**：在仓库根目录执行下面命令，列出顶层非隐藏目录，并统计每个目录的一级子项数量。
   ```bash
   # 列出顶层目录
   ls -d */
   # 看每个顶层目录有多少直接子项
   for d in */; do echo "$d $(find "$d" -maxdepth 1 -mindepth 1 | wc -l)"; done
   ```
2. **需要观察的现象**：`python/`、`sgl-kernel/`、`benchmark/`、`test/`、`docs_new/` 等目录会出现在输出里；`benchmark/` 的子项数量会非常大（几十个）。
3. **预期结果**：你得到一张「顶层目录 → 子项数量」的表，与本讲 4.1.1 的分类对应。
4. **无法运行时的替代**：如果当前环境不能执行 shell，你可以直接用编辑器的文件树面板人工核对，效果相同。

> 说明：本实践是「只读探查」，不修改任何源码，符合本讲义的约束。

#### 4.1.5 小练习与答案

**练习 1**：`sglang` 这个终端命令对应的 Python 入口函数是哪个？
> **答案**：`sglang.cli.main:main`，定义在 `python/sglang/cli/main.py`，由 `python/pyproject.toml` 的 `[project.scripts]` 注册。

**练习 2**：仓库里同时存在 `python/pyproject.toml` 和 `python/pyproject_cpu.toml`、`python/pyproject_npu.toml` 等，为什么？
> **答案**：因为 SGLang 支持多种硬件（NVIDIA/AMD/Intel CPU/Ascend NPU/Intel XPU 等），不同硬件的后端依赖不同，所以为每种硬件准备了一个 `pyproject_*.toml` 变体，便于按目标硬件安装。

**练习 3**：我想找一个「怎么调用 SGLang 跑离线批量推理」的范例，应该去哪个顶层目录？
> **答案**：去 `examples/`（具体在 `examples/runtime/engine/`，后续 u12-l3 会精读其中的 `offline_batch_inference.py`）。

### 4.2 `python/sglang` 包：`lang` 与 `srt` 的分工

#### 4.2.1 概念说明

进入 `python/sglang/` 之后，最重要的心智模型是「**前端语言（lang） vs 后端运行时（srt）**」：

- **`lang/`（前端 DSL）**：面向**写生成程序的人**。它提供 `gen`、`select`、`function`、`assistant` 等原语，让你用 Python 描述「先生成思路、再在若干选项里 select、最后拼出答案」这类结构化流程。它有一个解释器（`interpreter.py`）负责执行这些程序，还有 tracer（`tracer.py`）负责抽取共享前缀以提高缓存命中。`lang/` 本身**不做真正的模型计算**，而是把生成请求转发给后端。
- **`srt/`（后端运行时）**：面向**让模型真正跑起来的人**。调度器、KV 缓存、模型前向、采样、分布式、分离部署、投机解码……全部在这里。它是 SGLang 性能的来源。

除了这两大块，`python/sglang/` 下还有几个职能目录/文件：

- **`cli/`**：命令行入口（`main.py`、`serve.py`、`killall.py`、`generate.py`）。
- **`launch_server.py`**：拉起 HTTP 服务的入口脚本。
- **`jit_kernel/`**、**`kernels/`**：运行时即时编译（JIT）算子与轻量算子。
- **`profiler.py`**：性能分析工具。
- **`bench_*.py`**、**`benchmark/`**、**`eval/`**、**`test/`**：仓库内置的基准、评测与测试（注意这些和顶层 `benchmark/`、`test/` 是不同层级的目录）。
- **`multimodal_gen/`**：多模态生成相关。
- **`global_config.py`**、**`version.py`**、**`utils.py`**：全局配置、版本号、通用工具。

#### 4.2.2 核心流程

一条「用前端 DSL 写的」请求，从 `lang` 到 `srt` 的简化数据流：

```text
用户写 @sgl.function 程序 (lang/api.py)
        │  被 interpreter.py 解释执行
        ▼
生成请求 (lang/ir.py 的 IR 节点)
        │  通过 Runtime / RuntimeEndpoint / Engine 后端提交
        ▼
进入后端 srt (TokenizerManager → Scheduler → ModelRunner → Sampler)
        │  前向计算、采样、解码
        ▼
返回文本流 (回流到 lang 的 ProgramState)
```

关键直觉：**`lang` 负责「编排出什么程序」，`srt` 负责「高效地算出来」**。两者通过明确的请求接口解耦——这也是为什么 SGLang 的前端可以独立地连到一个已运行的 HTTP 服务（`RuntimeEndpoint`），也可以驱动一个进程内 `Engine`。

#### 4.2.3 源码精读

**`lang/` 目录的内部组成**，直接列出来看：

`python/sglang/lang/` 下有 `api.py`、`interpreter.py`、`ir.py`、`tracer.py`、`choices.py`、`chat_template.py` 和 `backend/` 子目录。其中：

- `api.py`：DSL 的公开原语（`gen`/`select`/`function` 等）。
- `interpreter.py`：解释执行 DSL 程序。
- `ir.py`：中间表示（IR）节点定义。
- `tracer.py`：抽取前缀用于缓存复用（后续 u6-l4 精读）。
- `backend/`：各种后端适配（openai/anthropic/vertexai/litellm 等）。

**`srt/` 目录体量**：它是整个项目最大的子包。仅 `srt/models/` 一个子目录就有 212 个模型文件，`srt/managers/` 下有调度器、分词管理器、去分词管理器等核心组件，`srt/layers/` 下有注意力、MoE、量化、采样器等基础层。`srt` 的详细布局留到 4.4 模块统一讲。

**`cli/` 的入口**：`python/sglang/cli/` 下的 `main.py` 是 `sglang` 命令的总分发器，`serve.py` 实现 `sglang serve` 子命令（u1-l2 已讲）。这两个文件是「命令行 → 运行时」的桥梁。

#### 4.2.4 代码实践

**实践目标**：亲手确认 `lang` 与 `srt` 的边界，并验证「`lang` 不直接算模型」。

1. **操作步骤**：
   - 在 `python/sglang/lang/api.py` 里搜索 `def gen`、`def select`、`def function`，确认它们是 DSL 原语。
   - 在 `python/sglang/srt/model_executor/model_runner.py` 里搜索 `def forward`，确认真正的前向计算在这里。
2. **需要观察的现象**：`lang/api.py` 里的 `gen/select` 更像「描述要生成什么」，而 `srt/model_executor/model_runner.py` 里的 `forward` 才会出现 `torch` 张量、attention backend 等计算细节。
3. **预期结果**：你会直观感受到「`lang` 偏编排、`srt` 偏计算」的分工。
4. **待本地验证**：如果你的编辑器支持「转到定义」，可以在某个示例里从 `sgl.gen(...)` 一路追到运行时调用，验证两层的衔接点。

#### 4.2.5 小练习与答案

**练习 1**：`lang/` 里的 tracer（`tracer.py`）主要解决什么问题？
> **答案**：它通过跟踪 DSL 程序的执行，抽取**共享前缀**，从而让后端的 RadixCache（基数树缓存）能命中这些前缀，减少重复计算。这是「前端帮助后端提效」的典型例子（u6-l4 详讲）。

**练习 2**：为什么 `python/sglang/cli/` 既不属于 `lang` 也不属于 `srt`，而是平级目录？
> **答案**：因为 CLI 是「调度入口」：它要解析命令行参数、然后决定是去启动服务（调用 `srt`）还是别的子命令。它是 `lang`/`srt` 之上的「壳」，不属于任何一方内部。

**练习 3**：`srt` 是哪几个英文单词的缩写？它和 `lang` 谁是性能的来源？
> **答案**：`srt` = **S**GLang **R**un**T**ime（运行时）。性能的来源是 `srt`，`lang` 主要是易用性与结构化表达。

### 4.3 顶层 `__init__.py` 的公开 API

#### 4.3.1 概念说明

`python/sglang/__init__.py` 是整个包的**门面文件**。当你写 `import sglang` 或 `from sglang import Engine, gen` 时，Python 解释器执行的就是这个文件。它决定了「对外承诺稳定的名字有哪些」。

这个文件通常很薄——它本身不写业务逻辑，而是**从内部模块「转手」导出**符号。SGLang 的 `__init__.py` 还做了一件额外的事：在导出之前，先做一些**平台兼容性修补（patch）**，确保在某些依赖缺失的平台（如 macOS MPS 没有 triton）上也能优雅降级。

理解门面文件的好处：它是「**进入项目内部前最后一张路标**」——看到 `__init__.py` 里某个符号，你就能反查它来自哪个子模块。

#### 4.3.2 核心流程

`__init__.py` 的执行顺序可以分成三段：

```text
① 平台修补阶段
   - macOS MPS：安装 triton stub、mps stub（让缺少的 API 不报错）
   - 应用 HF transformers 补丁（apply_all）

② 符号导入阶段
   - 前端 DSL API：global_config, gen, select, function, assistant/user/system, image/video ...
   - 后端运行时入口：ServerArgs, Engine（通过 LazyImport 延迟导入）
   - 后端适配器：OpenAI/Anthropic/VertexAI/LiteLLM/Crusoe（LazyImport）

③ 声明 __all__ 阶段
   - 列出所有对外公开的名字，作为公开 API 的「白名单」
```

#### 4.3.3 源码精读

**第①段：平台修补**。文件开头先处理 macOS 与 HF 兼容性：

[python/sglang/__init__.py:1-32](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/__init__.py#L1-L32) ——这段在导入任何业务模块之前，先安装 triton/mps 的桩（stub），再调用 `apply_all` 应用 HuggingFace transformers 补丁。这保证了后续导入不会因为平台缺失某些 API 而崩溃。

**第②段：前端 DSL 导入**。从 `sglang.lang.api` 批量导入生成原语：

[python/sglang/__init__.py:36-59](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/__init__.py#L36-L59) ——这里导入 `Engine`、`Runtime`、`gen`、`select`、`function`、`assistant`/`user`/`system`（及其 `_begin`/`_end` 变体）、`image`/`video`、`set_default_backend`、`flush_cache` 等前端原语，以及来自 `lang.backend.runtime_endpoint` 的 `RuntimeEndpoint`。注意这里第一次出现了 `Engine`（来自 `lang.api`）。

**第②段续：后端运行时入口（LazyImport，覆盖同名符号）**：

[python/sglang/__init__.py:68-79](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/__init__.py#L68-L79) ——这段先用 `LazyImport` 包装了 `OpenAI/Anthropic/VertexAI/LiteLLM/Crusoe` 等后端适配器，然后关键的两行是：

```python
ServerArgs = LazyImport("sglang.srt.server_args", "ServerArgs")
Engine = LazyImport("sglang.srt.entrypoints.engine", "Engine")
```

这里**第二次**给 `Engine` 赋值——由于 Python 是「后赋值覆盖先赋值」，最终 `sglang.Engine` 指向的是**运行时的 `sglang.srt.entrypoints.engine.Engine`**（u1-l4 会精讲），而不是前面从 `lang.api` 导入的那个同名符号。这是阅读门面文件时容易忽略的细节：**同名的最后一次赋值才生效**。`ServerArgs` 也在这里被「延迟」导出，目的是避免一 `import sglang` 就把沉重的运行时全部加载进来。

**第③段：公开 API 白名单**：

[python/sglang/__init__.py:81-116](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/__init__.py#L81-L116) ——`__all__` 列出了所有对外承诺的名字。这就是「`import sglang` 之后能用什么」的权威清单。可以看到，前端原语（`gen`/`select`/`function`/`assistant`…）、运行时入口（`Engine`/`ServerArgs`）、后端适配器（`OpenAI`/`Anthropic`…）、工具（`global_config`/`__version__`）都被集中在这里。

#### 4.3.4 代码实践

**实践目标**：在不启动服务的前提下，验证 `import sglang` 之后能拿到哪些公开符号，并反查它们的真实来源。

1. **操作步骤**：写一个最小脚本（**示例代码，非项目原有代码**）：
   ```python
   # example_inspect_api.py  （示例代码）
   import sglang
   # 1) 列出公开 API
   print("public symbols:", sorted(sglang.__all__))
   # 2) 反查 Engine 真实模块
   print("Engine from:", sglang.Engine.__module__ if hasattr(sglang.Engine, "__module__") else sglang.Engine)
   ```
2. **需要观察的现象**：`__all__` 里包含 `Engine`、`gen`、`select`、`ServerArgs` 等名字；`sglang.Engine` 指向运行时模块（`sglang.srt.entrypoints.engine`）。
3. **预期结果**：直观看到「公开 API = `__all__`」，并验证 `Engine` 的最终绑定确实是运行时版本。
4. **待本地验证**：`LazyImport` 对象在被真正「调用/实例化」之前不会触发真实导入，因此 `__module__` 的打印结果可能表现为 LazyImport 封装；以你本地的实际行为为准，关键是理解「延迟导入」这一设计意图。

#### 4.3.5 小练习与答案

**练习 1**：`__init__.py` 为什么要在最开头做「平台修补」，而不是放到某个业务模块里？
> **答案**：因为 `__init__.py` 是 `import sglang` 时**第一个**被执行的代码，必须在任何依赖 torch/triton 的业务模块导入之前把缺失的 API 补齐（如 macOS MPS 缺 triton）。放晚了就来不及。

**练习 2**：`ServerArgs` 和 `Engine` 为什么用 `LazyImport` 而不是直接 `from ... import`？
> **答案**：运行时（`srt`）依赖很重（torch、CUDA 算子等），直接导入会让 `import sglang` 变得很慢且强依赖 GPU 栈。`LazyImport` 把真实导入推迟到「真正用到」的那一刻，让纯前端用法（如连到远端 OpenAI）也能轻量使用 sglang。

**练习 3**：`Engine` 这个名字在 `__init__.py` 里出现了两次（第 37 行附近和第 79 行），最终 `sglang.Engine` 是哪一个？
> **答案**：是第 79 行的 `LazyImport("sglang.srt.entrypoints.engine", "Engine")`，即运行时 `Engine`。Python 中模块级的同名赋值，**后一次覆盖前一次**。这正是 SGLang「运行时才是主入口」的体现。

### 4.4 `srt` 运行时子系统目录布局

#### 4.4.1 概念说明

`srt/` 是 SGLang 的「**重工业车间**」，几乎所有性能关键代码都在这里。它内部又按职能拆成几十个子目录。本模块不逐个讲实现，而是给你一张「**子系统 → 目录 → 后续讲义**」的对照表，让你建立长期可用的索引。

学习建议：**不要现在就记住所有子目录**，只要记住「调度 / 缓存 / 前向 / 采样 / 分布式 / 分离 / 投机」这几条主线对应的目录即可，其余在后续单元里自然会反复出现。

#### 4.4.2 核心流程

把 `srt/` 按一条请求的生命周期归类：

```text
请求进入        entrypoints/        （HTTP / Engine / OpenAI 兼容 API）
   │
分词 & 路由     managers/           （TokenizerManager / DataParallelController）
   │
调度 & 组批     managers/           （Scheduler / schedule_batch / schedule_policy）
   │
KV 缓存命中     mem_cache/          （RadixCache / 内存池 / HiCache）
   │
前向计算        model_executor/     （ModelRunner / ForwardBatch / CUDA Graph）
   │            + models/           （212 个模型实现）
   │            + layers/           （attention / moe / quantization / sampler / ...）
   │
采样 & 解码     sampling/           （SamplingParams / Sampler）
   │            + constrained/      （结构化输出 / 文法后端）
   │
返回流式结果    managers/           （DetokenizerManager / io_struct）
```

横向贯穿的能力（不专属某一步）：`distributed/`（并行）、`disaggregation/`（PD 分离）、`speculative/`（投机解码）、`lora/`（多 LoRA）、`observability/`（可观测性）、`connector/`（跨进程 KV 传输）。

#### 4.4.3 源码精读

下面这张表是本模块的核心产出。**目录**列是 `python/sglang/srt/` 下的真实子目录；**职责**列是一句话说明；**后续讲义**列指出本手册哪一讲会精读它。

| 子目录 | 职责（一句话） | 后续讲义 |
| --- | --- | --- |
| `managers/` | 多进程编排：分词、调度、组批、去分词、DP 控制、TP worker | U2、U3、U8 |
| `mem_cache/` | KV 缓存：RadixCache（基数树）、内存池、HiCache（分层卸载）、淘汰策略 | U4 |
| `model_executor/` | 单次前向执行：ModelRunner、ForwardBatch、CUDA Graph、buffer registry | U5、U7 |
| `model_loader/` | 权重加载：默认加载器、量化加载、auto_loader 权重映射 | U5 |
| `models/` | 模型实现（Llama/Qwen/DeepSeek… 共 212 个文件）+ `registry.py` | U5、U12 |
| `layers/` | 基础层：`attention/`、`moe/`、`quantization/`、`sampler.py`、`logits_processor.py`、`radix_attention.py`、`rotary_embedding/`、`model_parallel.py` | U5、U6、U8、U11 |
| `sampling/` | 采样：`sampling_params.py`、`sampling_batch_info.py`、`custom_logit_processor.py` | U6 |
| `constrained/` | 结构化输出：文法后端（xgrammar/outlines/llguidance）+ `grammar_manager.py` | U6 |
| `distributed/` | 并行状态：进程组管理（`parallel_state.py`） | U8 |
| `disaggregation/` | PD 分离部署：`prefill.py`/`decode.py`/`encode_server.py` + 多种 connector 后端 | U9 |
| `speculative/` | 投机解码：EAGLE / N-gram / DFlash 等 + `spec_registry.py` | U10 |
| `lora/` | 多 LoRA 批量服务：`lora_manager.py`、lora 层 | U12 |
| `entrypoints/` | 对外入口：`http_server.py`、`engine.py`、`EngineBase.py`、`openai/` | U2、U12 |
| `observability/` | 可观测性：指标采集、Prometheus 导出、请求时延统计 | U7 |
| `connector/` | 跨进程 KV 传输连接器抽象（NIXL/Mooncake/MoRI 等） | U9 |
| `server_args.py` | `ServerArgs` 配置数据类（上百字段） | U2 |

> 关于永久链接：本表引用的是**目录**而非单行代码，因此不附带 `#L` 行号链接——目录的「源码」是它内部成百上千个文件。后续每一讲都会带行号精读其中具体文件。

作为本模块唯一的「单点源码」，再确认一次运行时入口：`sglang.srt.entrypoints.engine.Engine` 就是被 `__init__.py` 通过 LazyImport 暴露成 `sglang.Engine` 的那个类（见 4.3.3），它和 `entrypoints/http_server.py` 一起构成「使用 SGLang 的两大入口」（u1-l4 详讲）。

#### 4.4.4 代码实践

**实践目标**：用目录列表命令，核验上表的子目录确实存在，并挑一个子系统看它的文件量。

1. **操作步骤**：
   ```bash
   # 列出 srt 的子目录（只看目录）
   ls -d python/sglang/srt/*/
   # 数一数 models/ 有多少模型文件
   ls python/sglang/srt/models/*.py | wc -l
   # 看 managers/ 的核心文件
   ls python/sglang/srt/managers/*.py | head
   ```
2. **需要观察的现象**：你会看到 `managers/`、`mem_cache/`、`model_executor/`、`layers/`、`speculative/`、`disaggregation/` 等子目录；`models/` 的 `.py` 文件数约为 200+。
3. **预期结果**：与 4.4.3 表格一致，从而确认「地图」准确。
4. **待本地验证**：不同发行版/分支下 `models/` 的确切数量可能略有差异，以本地 `wc -l` 结果为准。

#### 4.4.5 小练习与答案

**练习 1**：调度器（Scheduler）相关代码在哪个子目录？组批策略（schedule_policy）和批数据模型（schedule_batch）又在哪？
> **答案**：都在 `python/sglang/srt/managers/` 下——分别是 `scheduler.py`、`schedule_policy.py`、`schedule_batch.py`（U3 全单元精讲）。

**练习 2**：我想加一个新的投机解码算法，应该改 `srt/` 下哪个子目录？
> **答案**：`srt/speculative/`，并在 `spec_registry.py` 注册算法（U10-l1 讲注册机制）。

**练习 3**：`mem_cache/`、`model_executor/`、`layers/` 三者在一层前向里大致是什么关系？
> **答案**：`mem_cache/` 提供 KV 缓存的命中与分配（决定哪些 token 不用重算）；`model_executor/`（ModelRunner）负责组织一次前向的输入输出与执行流程；`layers/` 提供前向真正用到的算子层（attention、moe、linear 等）。三者配合完成「调度好的一批请求 → 一次 GPU 前向 → 采样结果」。

## 5. 综合实践

把本讲四个模块串起来，完成一份**仓库地图文档**（这是本讲规格里要求的实践任务）：

1. **画一张到二级的目录树**。从仓库根目录出发，至少包含以下二级节点：
   ```text
   sglang/
   ├── python/sglang/
   │   ├── lang/            （前端 DSL）
   │   ├── srt/             （后端运行时，本讲重点）
   │   ├── cli/             （命令行入口）
   │   ├── jit_kernel/      （JIT 算子）
   │   └── __init__.py      （公开 API 门面）
   ├── sgl-kernel/          （AOT CUDA/C++ 算子）
   ├── rust/                （Rust 组件：grpc、mm）
   ├── sgl-model-gateway/   （Go 网关）
   ├── benchmark/           （基准测试脚本）
   ├── test/                （测试套件）
   ├── docs_new/            （文档站 + cookbook）
   ├── examples/            （使用示例）
   ├── scripts/             （开发脚本）
   └── docker/ proto/ experimental/ 3rdparty/
   ```
   你可以用 `tree -L 2 -d`（若已安装）或本讲给出的 `ls`/`find` 命令核验。

2. **为每个关键目录写一句话职责说明**。直接复用本讲 4.1、4.2、4.4 表格里的「职责」列即可，但**用自己的话改写一遍**，确保你真的理解了。

3. **标记本手册后续每个单元对应的主要目录**。完成下面这张「单元 → 目录」映射（答案见本讲 4.4.3 表格与下方校验）：

   | 单元 | 主题 | 主要目录 |
   | --- | --- | --- |
   | U2 | 服务架构与请求生命周期 | `srt/managers/`、`srt/entrypoints/` |
   | U3 | 调度器与连续批处理 | `srt/managers/`（scheduler / schedule_batch / schedule_policy / scheduler_components/） |
   | U4 | KV 缓存与 RadixAttention | `srt/mem_cache/`、`srt/layers/radix_attention.py` |
   | U5 | 模型执行层 | `srt/model_executor/`、`srt/model_loader/`、`srt/models/`、`srt/layers/attention/` |
   | U6 | 采样与结构化输出 | `srt/sampling/`、`srt/constrained/`、`lang/` |
   | U7 | CUDA Graph 与性能优化 | `srt/model_executor/`（cuda_graph_*）、`srt/observability/` |
   | U8 | 分布式与并行 | `srt/distributed/`、`srt/layers/moe/`、`srt/managers/data_parallel_controller.py` |
   | U9 | PD 分离部署 | `srt/disaggregation/`、`srt/connector/` |
   | U10 | 投机解码 | `srt/speculative/` |
   | U11 | 量化与算子 | `srt/layers/quantization/`、`sgl-kernel/`、`python/sglang/jit_kernel/` |
   | U12 | LoRA / 扩展 / RL | `srt/lora/`、`srt/models/`、`srt/entrypoints/` |

4. **验收**：把这份地图保存为个人笔记。当你后续读到某一讲、需要找源码时，先在这张地图上定位目录，再用编辑器打开具体文件——这就是本讲想帮你建立的「**长期索引**」。

## 6. 本讲小结

- SGLang 是 **monorepo**：`python/`（核心 Python 代码）、`sgl-kernel/`（AOT CUDA 算子）、`rust/`、`sgl-model-gateway/` 等多语言工程并存。
- `python/sglang/` 内部分两大子包：**`lang/`（前端 DSL，偏编排）** 与 **`srt/`（后端运行时，偏计算与性能）**。
- `python/sglang/__init__.py` 是**公开 API 门面**：先做平台修补，再导出前端原语与运行时入口，最后用 `__all__` 声明白名单；其中 `Engine`/`ServerArgs` 用 `LazyImport` 延迟加载，且 `Engine` 的最终绑定是运行时版本。
- `srt/` 的子系统可按请求生命周期归类：`entrypoints/`（入口）→ `managers/`（分词/调度/组批/去分词）→ `mem_cache/`（KV 缓存）→ `model_executor/`+`models/`+`layers/`（前向）→ `sampling/`+`constrained/`（采样与结构化）。
- 横向贯穿的能力目录：`distributed/`、`disaggregation/`、`speculative/`、`lora/`、`observability/`、`connector/`，分别对应后续 U8–U12、U7、U9。
- 本讲的产出是一张「**到二级的目录树 + 职责说明 + 单元映射**」地图，后续每讲都会在这张地图上定位源码。

## 7. 下一步学习建议

- **直接下一步**：进入 **u1-l4（两种使用入口：HTTP 服务 vs 进程内 Engine）**，它会精读 `srt/entrypoints/http_server.py` 与 `srt/entrypoints/engine.py`——正好是本讲 4.3、4.4 指向的「入口」子系统。
- **如果想巩固目录认知**：先随手翻一翻 `examples/` 下的示例（如 `examples/runtime/engine/launch_engine.py`），把示例文件和本讲的「单元 → 目录」映射对上号。
- **后续按单元下钻**：当你学到 U2 之后，每读一讲都回到本讲的 4.4.3 表格，确认「当前讲义在 `srt/` 下的哪个子目录」，逐步把整张地图的每个格子填满。

# 目录结构与架构全景

## 1. 本讲目标

本讲承接 [u1-l1 项目总览](u1-l1-project-overview.md)。上一篇你已经知道 lmdeploy 是「压缩—推理—服务」一体化工具包，并且有 **PyTorch 引擎**与 **TurboMind 引擎**两条路线。但要做到「看懂源码」，还差一张地图。

学完本讲，你应该能够：

1. 看懂仓库根目录与 `lmdeploy/` 包内子目录各自负责什么。
2. 用一句话解释 `pytorch/`、`turbomind/`、`lite/`、`serve/`、`vl/` 这五个核心子包的职责。
3. 理解「**两条后端、一个 Pipeline**」这条架构主线：用户只调用 `pipeline()`，内部自动落到两个引擎之一。
4. 在源码里精确定位三个入口文件：`lmdeploy/__init__.py`、`lmdeploy/api.py`、`lmdeploy/pipeline.py`。

本讲只做「建立全局地图」这件事，不深入任何一个子包的内部实现。后续每个单元会针对一个子包展开。

## 2. 前置知识

- **Python 包的目录约定**：一个目录里有 `__init__.py`，它就是一个可被 `import` 的包（package）；包里还可以嵌套子包。
- **入口文件**：`__init__.py` 决定 `import lmdeploy` 时对外暴露哪些名字；`__main__.py` 决定在命令行敲 `lmdeploy ...` 时执行什么。
- **工厂函数**：`api.py` 里的 `pipeline()` 就是一个工厂函数——它本身不做推理，只负责「生产」一个 `Pipeline` 对象交还给用户。
- **后端（backend）**：在本项目里指真正执行 Transformer 前向计算的引擎，有两套实现：PyTorch（纯 Python）与 TurboMind（C++）。
- **前置认知**：本讲默认你已经读过 [u1-l1](u1-l1-project-overview.md)，知道 continuous batching、blocked KV cache、张量并行这些术语，以及两套引擎「并存互补」的关系。

## 3. 本讲源码地图

本讲涉及的关键文件如下，全部围绕「入口与目录」：

| 文件 | 作用 |
| --- | --- |
| [`CLAUDE.md`](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/CLAUDE.md) | 项目给开发者的架构速览，本身就是一份高质量目录导览。 |
| [`lmdeploy/__init__.py`](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/__init__.py) | 包入口，决定 `import lmdeploy` 暴露哪些公开符号。 |
| [`lmdeploy/api.py`](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/api.py) | 工厂函数 `pipeline()` 所在地，用户最常用的入口。 |
| [`lmdeploy/pipeline.py`](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pipeline.py) | `Pipeline` 类定义，包含「选哪个后端」的关键代码。 |
| [`lmdeploy/archs.py`](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/archs.py) | 架构注册表，负责自动判定 turbomind / pytorch 后端与任务类型。 |

> 说明：本讲为了讲清「后端选择」这条主线，额外引用了 `pipeline.py` 与 `archs.py`。它们在 [u2-l5](u2-l5-arch-registry-and-backend-selection.md) 会更系统地讲；本讲只取其中和「目录与入口」直接相关的几行。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **顶层目录**：仓库根目录里有哪些东西、各自属于「源码 / 构建 / 文档 / 测试」哪一类。
2. **lmdeploy 子包划分**：`lmdeploy/` 包内子目录的职责地图。
3. **Pipeline 统一入口**：`pipeline()` 如何把请求路由到两个引擎之一。

---

### 4.1 顶层目录

#### 4.1.1 概念说明

一个大型项目仓库的根目录通常混着四类东西：

- **源码**：真正会被打包发布、被用户 `import` 的代码。
- **构建脚本**：把源码编译、打包成可安装产物的配置（CMake、setup.py、requirements）。
- **文档与示例**：README、docs、examples，给人看的。
- **测试与工程辅助**：tests、benchmark、docker、CI 脚本。

把它们分清楚，是「看懂项目」的第一步：当你想找「推理逻辑」，去源码；想找「怎么装」，去构建脚本；想找「怎么用」，去示例。

#### 4.1.2 核心流程

仓库根目录可以按职责分成四组：

```text
InternLM-lmdeploy/                      ← 仓库根
├── lmdeploy/        ← ★ 源码主包（一切 import 的起点）
├── src/             ← ★ TurboMind 的 C++ 源码（编译成 pybind 扩展）
│
├── setup.py         ← 构建：Python 打包入口
├── CMakeLists.txt   ← 构建：CMake 总入口（编译 src/ 下的 C++）
├── cmake/           ← 构建：CMake 辅助脚本
├── builder/         ← 构建：打包辅助
├── requirements/    ← 构建：按设备拆分的运行时依赖
├── requirements_cuda.txt / _ascend.txt / _maca.txt / _rocm.txt / _camb.txt
├── pyproject.toml / MANIFEST.in
│
├── README.md / README_zh-CN.md / README_ja.md   ← 文档
├── CLAUDE.md                                    ← 文档：架构速览
├── docs/  examples/  resources/                 ← 文档与示例
│
├── tests/          ← 测试：pytest 单元测试
├── benchmark/      ← 工程：性能基准
├── docker/  k8s/   ← 工程：容器与部署
├── autotest/  eval/  scripts/  debug.sh  generate.sh  ← 工程：辅助脚本
└── lmdeploy-tutorial/   ← 本手册输出目录（非项目源码）
```

要点：

- 真正的 Python 源码只在 `lmdeploy/` 一个目录下；`src/` 是 TurboMind 的 C++ 实现，编译后以 `lmdeploy.lib._turbomind` 的形式被 Python 调用。
- 安装依赖按**目标设备**拆成多份（`requirements_cuda.txt` 等），这点在 [u1-l3 安装与构建](u1-l3-installation-and-build.md) 会详讲。
- `tests/test_lmdeploy` 是单元测试目录，`CLAUDE.md` 给出了运行方式。

#### 4.1.3 源码精读

`CLAUDE.md` 的 Architecture 小节明确点出了「源码在哪里」这件事：

[CLAUDE.md:30-33](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/CLAUDE.md#L30-L33) 说明 TurboMind 的 C++ 扩展由 `setup.py` + CMake 构建，并把依赖按设备拆分在 `requirements/runtime_cuda.txt`、`runtime_ascend.txt` 等文件里。

[CLAUDE.md:63](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/CLAUDE.md#L63) 说明 TurboMind 的 Python 包装 `lmdeploy/turbomind/turbomind.py` 通过 pybind11 桥接到 `lmdeploy/lib/_turbomind`（即 `src/turbomind/` 编译产物）。这正好把「`lmdeploy/`（Python）」和「`src/`（C++）」两块拼在了一起。

#### 4.1.4 代码实践

**实践目标**：用一条命令把根目录的真实结构打印出来，验证上面的分组。

**操作步骤**：

```bash
# 在仓库根目录执行
ls -1F
# -F 会给目录加 / 后缀，给可执行文件加 * 后缀，便于区分类型
```

进一步，只看「构建相关」文件：

```bash
ls -1 setup.py CMakeLists.txt requirements*
```

**需要观察的现象**：`lmdeploy/` 和 `src/` 都带 `/`（是目录）；`setup.py`、`debug.sh`、`generate.sh` 等带或不带标记；`requirements_*.txt` 有 5 份（cuda / ascend / maca / rocm / camb）。

**预期结果**：你能把根目录每一项归入「源码 / 构建 / 文档 / 测试 / 工程」五类之一，且确认 Python 源码只在 `lmdeploy/` 下。

> 上面命令只读地列目录，不会改动任何文件，可放心运行。

#### 4.1.5 小练习与答案

**练习 1**：仓库里同时存在 `lmdeploy/` 和 `src/` 两个源码目录，它们写的是什么语言、分别对应哪套引擎？

> **答案**：`lmdeploy/` 是 Python 源码，是整套工具包的主体，PyTorch 引擎完全在这里实现；`src/` 是 C++ 源码，主要是 TurboMind 引擎的实现，编译成 `lmdeploy.lib._turbomind` 后被 Python 调用。

**练习 2**：为什么 `requirements` 要拆成 `requirements_cuda.txt`、`requirements_ascend.txt` 等多份？

> **答案**：lmdeploy 支持多种硬件（NVIDIA CUDA、华为昇腾 Ascend、AMD ROCm、摩尔线程 maca、寒武纪 camb 等），不同设备需要的运行时依赖（如 `torch` 的编译版、设备 SDK 桥接库）不同，按设备拆分可以让用户只装自己需要的那一份。

---

### 4.2 lmdeploy 子包划分

#### 4.2.1 概念说明

进入 `lmdeploy/` 目录后，会看到两类东西：

- **散落的模块文件**（`.py`）：如 `messages.py`、`model.py`、`pipeline.py`、`archs.py`，是跨子系统共享的基础设施。
- **子包**（带 `__init__.py` 的目录）：如 `pytorch/`、`turbomind/`、`lite/`、`serve/`、`vl/`，每个对应一个相对独立的子系统。

理解这些子包的职责，等于在脑子里建好了「去哪找代码」的索引：以后想看推理调度就进 `pytorch/engine`，想看量化就进 `lite/`，想看 HTTP 服务就进 `serve/`。

#### 4.2.2 核心流程

下面是 `lmdeploy/` 包的结构（节选到关键子包，省略部分深层文件）：

```text
lmdeploy/
├── __init__.py        ← 包入口：对外暴露 pipeline / serve / 配置类等
├── __main__.py        ← CLI 入口：敲 `lmdeploy ...` 时执行
├── api.py             ← 工厂函数 pipeline() 所在地
├── pipeline.py        ← Pipeline 类（用户 API 层）
├── archs.py           ← 模型架构注册与后端自动选择
├── messages.py        ← 核心数据类型：GenerationConfig / EngineConfig / Response ...
├── model.py           ← chat 模板（对话如何格式化成 token）
├── tokenizer.py       ← HF / SentencePiece 分词器封装
├── logger.py / utils.py / version.py / profiler.py
│
├── cli/               ← 命令行子命令（serve / lite / chat）
├── pytorch/           ← ★ PyTorch 引擎（纯 Python 后端）
├── turbomind/         ← ★ TurboMind 引擎（Python 包装 + 对接 C++）
├── lite/              ← ★ 量化压缩链路（AWQ / GPTQ / SmoothQuant）
├── serve/             ← ★ 服务部署（OpenAI 兼容 API、异步引擎、代理）
├── vl/                ← ★ 视觉语言模型（图像/视频预处理）
├── metrics/  monitoring/   ← 可观测性（指标、Prometheus/Grafana）
```

五个核心子包的一句话职责：

| 子包 | 一句话职责 |
| --- | --- |
| `pytorch/` | PyTorch 引擎：加载 HF 模型后用 patch 机制替换为优化实现，并负责调度、KV 缓存、算子。 |
| `turbomind/` | TurboMind 引擎：Python 包装层，桥接到 `src/` 编译出的 C++ 高性能后端，并负责模型转换。 |
| `lite/` | 模型压缩：AWQ / GPTQ / SmoothQuant 等量化算法，把权重量化成 4bit/8bit。 |
| `serve/` | 服务化：把引擎包装成 OpenAI 兼容的 HTTP API，支持多卡/多机/代理。 |
| `vl/` | 多模态：图像/视频的加载与预处理，把视觉输入送进推理引擎。 |

`pytorch/` 内部进一步细分，体现了「模型—算子—调度」三层结构：

```text
lmdeploy/pytorch/
├── config.py          ← 引擎内部配置数据类（ModelConfig/CacheConfig/...）
├── models/            ← 40+ 个模型的 patch 重写实现（llama.py / qwen.py ...）
├── nn/                ← 可复用优化算子（attention / norm / rope / linear / moe）
├── kernels/           ← Triton / CUDA kernel
├── backends/          ← 按设备/量化类型分发算子
├── engine/            ← 引擎主类、异步循环、请求管理
├── paging/            ← 调度器、KV 块管理、前缀缓存（BlockTrie）
├── adapter/  disagg/  multimodal/  spec_decode/  distributed.py  ...
```

这张子图对应 `CLAUDE.md` 里描述的「PyTorch Backend」结构，会在 U3–U5 三個单元逐一展开。

#### 4.2.3 源码精读

包入口 `__init__.py` 决定了 `import lmdeploy` 之后你能直接用到哪些名字：

[lmdeploy/\_\_init\_\_.py:3-8](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/__init__.py#L3-L8) 把 `pipeline`、`serve`、`client` 从 `api` 导入，把 `GenerationConfig`、`PytorchEngineConfig`、`TurbomindEngineConfig`、`VisionConfig` 从 `messages` 导入，把 `ChatTemplateConfig` 从 `model` 导入，把 `Pipeline` 从 `pipeline` 导入。这几行就是整个包的「公开 API 表」。

[lmdeploy/\_\_init\_\_.py:10-13](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/__init__.py#L10-L13) 用 `__all__` 显式列出了对外公开的符号集合，等于在说「这些就是用户该用的全部入口」。

> 注意：`serve` 和 `client` 虽然被导出，但它们在 `api.py` 中已被标记为 `@deprecated`（见下文 4.3.3），实际调用会抛 `NotImplementedError`。公开 API 表里有它们，更多是历史兼容考虑，新代码请用 CLI `lmdeploy serve api_server` 或 `from lmdeploy.serve import APIClient`。

`CLAUDE.md` 同样把「关键文件」逐一点名，可作为本节的权威索引：

[CLAUDE.md:89-95](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/CLAUDE.md#L89-L95) 列出了 `messages.py`（核心类型）、`model.py`（chat 模板）、`archs.py`（架构注册）、`tokenizer.py`（分词器封装）、`serve/openai/`（OpenAI 兼容服务）这几把「理解全项目的钥匙」。

#### 4.2.4 代码实践

**实践目标**：亲手打印出 `lmdeploy/` 的子包结构，并验证五个核心子包各有哪些关键文件。

**操作步骤**：

```bash
# 1. 看 lmdeploy 顶层（-F 标记目录）
ls -1F lmdeploy/

# 2. 看 pytorch 引擎的子目录
ls -1F lmdeploy/pytorch/

# 3. 看 serve 服务包的子目录
ls -1F lmdeploy/serve/

# 4. 验证公开 API：import 后打印 __all__
python -c "import lmdeploy; print(lmdeploy.__all__)"
```

**需要观察的现象**：

- 步骤 1 中，`pytorch/`、`turbomind/`、`lite/`、`serve/`、`vl/`、`cli/`、`metrics/`、`monitoring/` 都带 `/`（是子包）；其余是 `.py` 文件。
- 步骤 2 中，`pytorch/` 下能看到 `engine/`、`paging/`、`nn/`、`kernels/`、`backends/`、`models/` 等子目录，以及 `config.py`。
- 步骤 4 打印出的列表应与源码 `__all__` 完全一致：`['pipeline', 'serve', 'client', 'Tokenizer', 'GenerationConfig', '__version__', 'version_info', 'ChatTemplateConfig', 'PytorchEngineConfig', 'TurbomindEngineConfig', 'VisionConfig', 'Pipeline']`。

**预期结果**：你能在不查文档的情况下，凭目录结构说出「想看量化去 `lite/`，想看调度去 `pytorch/paging/`」。

> 如果环境里尚未安装好 lmdeploy 的 C++ 扩展，步骤 4 的 `import lmdeploy` 仍可成功（顶层 `__init__.py` 不直接依赖 TurboMind 扩展）。若遇到 `ImportError`，请先按 [u1-l3](u1-l3-installation-and-build.md) 完成安装。

#### 4.2.5 小练习与答案

**练习 1**：用户想调用 `lmdeploy.pipeline(...)`，需要 `import` 哪些符号？为什么 `pipeline` 能直接从 `lmdeploy` 顶层拿到？

> **答案**：只需 `import lmdeploy`。因为 `lmdeploy/__init__.py` 第 3 行 `from .api import ..., pipeline, ...` 已经把 `api.py` 里的 `pipeline` 函数提升到了包顶层，所以 `lmdeploy.pipeline` 可直接访问。

**练习 2**：`lmdeploy/messages.py` 和 `lmdeploy/pytorch/config.py` 都是「配置」，它们的服务对象有何不同？

> **答案**：`messages.py` 里的配置（如 `PytorchEngineConfig`、`TurbomindEngineConfig`、`GenerationConfig`）面向**用户**，是调用 `pipeline()` 时传入的参数；`pytorch/config.py` 里的数据类（如 `ModelConfig`、`CacheConfig`、`SchedulerConfig`）面向**引擎内部**，由用户配置派生而来，只在 PyTorch 引擎内部流转。

---

### 4.3 Pipeline 统一入口（两条后端一个 Pipeline）

#### 4.3.1 概念说明

本模块是整篇讲义的核心，也是 lmdeploy 最重要的一条架构主线：**两条后端，一个 Pipeline**。

意思是：用户永远只面对一个统一入口 `pipeline()`，不必关心底层是 PyTorch 还是 TurboMind；`Pipeline` 在初始化时会**自动判定**该用哪套引擎，并把后续请求转发给它。这样做的好处是：

- 用户 API 极简：`pipe = lmdeploy.pipeline(model_path)` 一行搞定。
- 两套引擎可平滑切换：换引擎只需换 `backend_config`，上层代码不动。
- 多模态也统一：视觉语言模型（VLM）走的是同一套 `Pipeline`，只是内部换成 `VLAsyncEngine`。

#### 4.3.2 核心流程

从用户调用到引擎启动，主链路是这样的：

```text
用户: lmdeploy.pipeline(model_path)
        │
        ▼
api.pipeline()                    ← 工厂函数，只负责 new 一个 Pipeline
        │  return Pipeline(...)
        ▼
Pipeline.__init__()               ← 在这里做两件大事：
        │
        ├─① autoget_backend_config()   ← 决定 backend = 'pytorch' 还是 'turbomind'
        │       （位于 archs.py，依据模型架构与是否安装 TurboMind）
        │
        ├─② get_task()                 ← 决定 pipeline_class = AsyncEngine 还是 VLAsyncEngine
        │       （纯文本 LLM → AsyncEngine；VLM → VLAsyncEngine）
        │
        └─③ self.async_engine = pipeline_class(...)   ← 真正实例化引擎
                self.async_engine.start_loop(...)      ← 启动推理循环
```

后端选择的关键判定（在 `archs.autoget_backend` 里）：

```text
若已安装 TurboMind 且 该模型被 supported_models 支持  → backend = 'turbomind'
否则                                                  → backend = 'pytorch'（并打印 fallback 警告）
```

也就是说，**两个候选引擎分别是 PyTorch 引擎与 TurboMind 引擎**，`pipeline()` 最终会进入其中之一。

#### 4.3.3 源码精读

工厂函数非常薄，核心只有一句 `return Pipeline(...)`：

[lmdeploy/api.py:15-22](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/api.py#L15-L22) 定义 `pipeline(model_path, backend_config=None, chat_template_config=None, ...)`，参数含义在 docstring 里写得很清楚：`model_path` 可以是本地 TurboMind 模型目录、lmdeploy 量化模型 repo、或任意 HF 模型 repo；`backend_config` 决定后端。

[lmdeploy/api.py:72-79](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/api.py#L72-L79) `pipeline()` 把全部参数原样转交给 `Pipeline(...)` 并返回。这就是「工厂」的全部职责——不做推理，只造对象。

真正的「选后端」发生在 `Pipeline.__init__` 里：

[lmdeploy/pipeline.py:72-77](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pipeline.py#L72-L77) 先调用 `autoget_backend_config(...)` 得到 `backend`（字符串 `'turbomind'` 或 `'pytorch'`）与最终的 `backend_config`；再调用 `get_task(...)` 得到 `pipeline_class`（即引擎类）。这两行是「两条后端一个 Pipeline」的命门。

[lmdeploy/pipeline.py:78-85](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pipeline.py#L78-L85) 用上一步得到的 `pipeline_class` 实例化 `self.async_engine`，并把 `backend`、`backend_config`、`chat_template_config` 等全部传进去。注意 `Pipeline` 自己并不持有模型权重，它只是包了一个 `async_engine`。

那 `autoget_backend_config` 具体怎么判？看 `archs.py`：

[lmdeploy/archs.py:12-53](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/archs.py#L12-L53) `autoget_backend` 尝试 `from lmdeploy.turbomind.supported_models import is_supported`，若 TurboMind 未装好（`ImportError`）或模型不在支持列表里，就回退到 `'pytorch'` 并打 warning；最后一行 `backend = 'turbomind' if turbomind_has else 'pytorch'` 给出结论。

[lmdeploy/archs.py:56-92](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/archs.py#L56-L92) `autoget_backend_config` 在 `autoget_backend` 之上多做了两件事：①若用户已显式传 `PytorchEngineConfig`，直接返回 `'pytorch'`；②当自动判出的后端与用户传入的配置类型不一致时，把字段尽量迁移过去（并处理 `block_size` ↔ `cache_block_seq_len` 的命名差异）。

而 `get_task` 决定的是「纯文本还是多模态」：

[lmdeploy/archs.py:125-140](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/archs.py#L125-L140) `get_task` 读取模型架构，调用 `check_vl_llm(...)` 判断是不是视觉语言模型：是 → 返回 `VLAsyncEngine`（来自 `serve.core`）；否则默认返回 `AsyncEngine`。注意这两个引擎类都来自 `lmdeploy/serve/core/`，而不是直接来自 `pytorch/` 或 `turbomind/`——`AsyncEngine` 是对底层具体后端的再一次异步封装。

> 关于 `api.py` 里另外两个函数：[lmdeploy/api.py:82-102](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/api.py#L82-L102) 的 `serve` 和 [lmdeploy/api.py:105-123](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/api.py#L105-L123) 的 `client` 都带 `@deprecated` 装饰器，函数体直接 `raise NotImplementedError`。它们只是为了向后兼容而保留名字，真正启服务请用 `lmdeploy serve api_server` 命令（见 [u8-l2](u8-l2-server-launch.md)）。

#### 4.3.4 代码实践

**实践目标**：在源码里亲自走一遍「`pipeline()` → 选后端 → 实例化引擎」的调用链，并标出最终进入的两个引擎。

**操作步骤（源码阅读型）**：

1. 打开 [`lmdeploy/api.py`](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/api.py)，定位第 72 行 `return Pipeline(...)`，确认工厂函数没有做后端选择。
2. 跟进到 [`lmdeploy/pipeline.py:72-77`](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pipeline.py#L72-L77)，记下两个关键调用：`autoget_backend_config` 与 `get_task`。
3. 打开 [`lmdeploy/archs.py`](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/archs.py)，分别看 `autoget_backend`（第 52 行的返回）与 `get_task`（第 137、140 行的返回）。
4. 用下面的「思维实验」填充下表：

| 场景 | backend | pipeline_class |
| --- | --- | --- |
| 本机装了 TurboMind，模型在其支持列表 | `turbomind` | `AsyncEngine`（纯文本） |
| 模型不在 TurboMind 支持列表 | `pytorch` | `AsyncEngine` |
| 一个 Qwen-VL 多模态模型 | （由支持情况定） | `VLAsyncEngine` |
| 用户显式传 `PytorchEngineConfig` | `pytorch` | `AsyncEngine` 或 `VLAsyncEngine` |

**可选的运行型实践**（需要已装好 lmdeploy 与一个可用小模型）：

```bash
# 用 debug 日志观察后端选择过程
LMDEPLOY_LOG_LEVEL=DEBUG python -c "
import lmdeploy
pipe = lmdeploy.pipeline('Qwen/Qwen2.5-0.5B-Instruct')   # 换成你本地有的小模型
print('backend_config =', pipe.backend_config)
"
```

**需要观察的现象**：日志里会出现类似 `Fallback to pytorch engine because ... not supported by turbomind engine.` 或直接选中 turbomind 的信息；`pipe.backend_config` 的类型是 `TurbomindEngineConfig` 或 `PytorchEngineConfig`，正好对应最终进入的那一套引擎。

**预期结果**：你能指着源码说——`pipeline()` 最终会进入 **PyTorch 引擎** 或 **TurboMind 引擎** 中的一个，判定发生在 `archs.py`，而封装层是 `serve/core` 的 `AsyncEngine` / `VLAsyncEngine`。

> 若手头没有可运行的环境或模型，本实践以「源码阅读 + 填表」为准，标注为**待本地验证**的部分是第 4 步运行型实践的实际日志输出。

#### 4.3.5 小练习与答案

**练习 1**：为什么说 `api.pipeline()` 是「工厂函数」而不是「引擎」？

> **答案**：因为它只负责 `return Pipeline(...)`，本身不解析模型、不做前向计算；真正的后端选择与引擎实例化都发生在 `Pipeline.__init__` 内部。它的角色是「生产 Pipeline 对象的工厂」。

**练习 2**：如果一个新模型既不被 TurboMind 支持、又是多模态的，`pipeline()` 会进入哪个引擎、用哪个 pipeline_class？

> **答案**：`autoget_backend` 会回退到 `'pytorch'`（并打 warning），`get_task` 因为是 VLM 会返回 `VLAsyncEngine`。最终进入 **PyTorch 引擎**，并由 `serve/core` 的 `VLAsyncEngine` 做异步封装。

**练习 3**：`AsyncEngine` 来自哪个目录？为什么它不在 `pytorch/` 或 `turbomind/` 下？

> **答案**：来自 `lmdeploy/serve/core/`（见 `archs.py` 第 130 行 `from lmdeploy.serve.core import AsyncEngine`）。它放在 `serve/core` 是因为它是对「任意后端」的统一异步封装层，不应绑定到某一个具体后端目录；具体的 PyTorch/TurboMind 后端是在它内部再被选择的。

## 5. 综合实践

把本讲的三个模块串起来，完成一张属于你自己的「LMDeploy 架构速查图」。

**任务**：

1. 在仓库根目录执行 `ls -1F lmdeploy/`，把输出贴进一份笔记。
2. 为 `pytorch/`、`turbomind/`、`lite/`、`serve/`、`vl/` 各写一句**你自己的话**的职责说明（不要照抄本讲表格）。
3. 画一张从 `pipeline()` 到两个引擎的调用链流程图，至少包含这些节点：`api.pipeline` → `Pipeline.__init__` → `autoget_backend_config` / `get_task` → `AsyncEngine` / `VLAsyncEngine` →（PyTorch 引擎 或 TurboMind 引擎）。
4. 在流程图上用两种颜色（或标记）标出「最终的两个引擎」，并写明判定它们的源码文件与行号（提示：`archs.py` 的 `autoget_backend` 与 `get_task`）。
5. 回答收尾问题：如果要让 `pipeline()` 强制走 PyTorch 引擎，最简单的办法是什么？（提示：看 `autoget_backend_config` 开头对 `PytorchEngineConfig` 的特殊处理。）

**验收标准**：只要你的图能让一个没读过 lmdeploy 的人明白「用户只调一个 `pipeline()`，内部自动二选一」，就算通过。

## 6. 本讲小结

- 仓库根目录分四类：Python 源码只在 `lmdeploy/`，C++ 源码在 `src/`（TurboMind），其余是构建脚本（`setup.py` + CMake + 按设备拆分的 `requirements`）、文档示例与测试工程。
- `lmdeploy/` 包由若干基础设施文件（`messages.py` / `model.py` / `archs.py` / `pipeline.py` 等）和五个核心子包（`pytorch/` / `turbomind/` / `lite/` / `serve/` / `vl/`）组成，外加 `cli/`、`metrics/`、`monitoring/`。
- `__init__.py` 的 `__all__` 是整个包的公开 API 表：`pipeline`、`Pipeline`、各类 `Config`、`Tokenizer` 等。
- 架构主线是「两条后端，一个 Pipeline」：用户只调 `pipeline()`，`Pipeline.__init__` 通过 `archs.autoget_backend_config` 选 `'pytorch'` 或 `'turbomind'`，通过 `archs.get_task` 选 `AsyncEngine` 或 `VLAsyncEngine`。
- 两个最终引擎分别是 **PyTorch 引擎**（`lmdeploy/pytorch/`）与 **TurboMind 引擎**（`lmdeploy/turbomind/` + `src/` C++）。
- `api.py` 中的 `serve` / `client` 已 `@deprecated` 并会抛 `NotImplementedError`，新代码请用 CLI 或 `from lmdeploy.serve import APIClient`。

## 7. 下一步学习建议

本讲建立了全局地图，下一步建议沿着「入口 → 配置」继续：

- **下一讲 [u1-l3 安装与构建方式](u1-l3-installation-and-build.md)**：搞清楚怎么把这份源码（尤其是 TurboMind 的 C++ 扩展）装到你的机器上，是后续所有实践的前提。
- **之后 [u1-l4 pipeline 推理快速上手](u1-l4-pipeline-quickstart.md)**：亲手用 `pipeline()` 跑通第一次推理。
- **想深入「后端选择」**：直接跳到 [u2-l5 架构注册与后端自动选择 archs.py](u2-l5-arch-registry-and-backend-selection.md)，本讲 4.3 节是它的预热版。
- **想看某个子包内部**：U3–U5 讲 PyTorch 引擎，U6 讲 TurboMind，U7 讲 Lite 量化，U8 讲 serve 服务——按需取用即可。

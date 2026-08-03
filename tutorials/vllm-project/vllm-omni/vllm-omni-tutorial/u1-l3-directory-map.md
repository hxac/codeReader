# 源码地图：目录结构与包布局

## 1. 本讲目标

学完本讲，你应当能够：

1. 画出 vLLM-Omni 仓库的**顶层目录全景图**，说出 `examples/`、`recipes/`、`tests/`、`benchmarks/`、`docs/`、`vllm_omni/` 各自承担什么职责。
2. 解释 `vllm_omni` 包内部的「🟡 修改（Modified）+ 🔴 新增（Added）」组织方式，并复述包初始化时的 **import 顺序**。
3. 看懂 `vllm_omni/` 下九大核心子系统目录（`entrypoints` / `engine` / `diffusion` / `distributed` / `config` / `platforms` / `quantization` / `worker` / `core`）分别对应架构中的哪个组件。
4. 区分 `examples/offline_inference` 与 `examples/online_serving` 的用途，并知道去 `docs/design` 和 `docs/contributing` 找哪类资料。

本讲是一张「地图」：不会深入任何模块的实现细节，而是帮你在后续阅读时知道「这段功能大概住在哪个目录」。

## 2. 前置知识

阅读本讲前，建议你已经：

- 读过 **u1-l1**，知道 vLLM-Omni 是在 vLLM 之上做「增量扩展」的全模态推理框架，了解五大组件（OmniRouter / EntryPoints / AR / Diffusion / OmniConnector）。
- 知道「AR（自回归）」与「DiT（扩散 Transformer）」是两类不同的生成结构，vLLM-Omni 把它们组合成多个「stage（阶段）」。
- 大致了解 Python 包的 `__init__.py` 是包的入口文件。

下面用到的两个术语先做个澄清：

- **monkey-patch（猴子补丁）**：在运行时动态替换另一个库里已有的函数或类。vLLM-Omni 用这种方式「改写」vLLM，而不是去 fork 一份 vLLM 源码。
- **stage（阶段）**：一条请求被拆成多个顺序执行的子任务，例如 Qwen3-Omni 的「Thinker → Talker → Code2wav」就是三个 stage。每个 stage 可以是 AR 也可以是 Diffusion。

## 3. 本讲源码地图

本讲只读两个「源头文件」，其余靠目录结构本身来讲解：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/README.md) | 项目的「门面」，说明它是什么、能跑哪些模型、怎么快速上手。 |
| [vllm_omni/\_\_init\_\_.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/__init__.py) | 整个 Python 包的入口，定义了导入顺序与对外暴露的 `Omni` / `AsyncOmni` / `OmniModelConfig`。 |

辅助参考的设计文档：

| 文件 | 作用 |
| --- | --- |
| [docs/design/architecture_overview.md](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/design/architecture_overview.md) | 官方架构总览，含「Key Components」表与代表模型分类。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

- 4.1 顶层仓库目录全景
- 4.2 `vllm_omni` 包的顶层布局与初始化顺序
- 4.3 九大核心子系统目录速览
- 4.4 学习资源地图：examples / recipes / tests / docs

### 4.1 顶层仓库目录全景

#### 4.1.1 概念说明

当你 `git clone` 下 vLLM-Omni 后，仓库根目录下会看到这样一些顶层目录。先建立「每个目录是干嘛的」的直觉，后面才不会在大量文件里迷路。

#### 4.1.2 核心流程

仓库根目录的顶层目录可以分成四类：

1. **核心代码**：`vllm_omni/` —— 唯一的 Python 包源码，所有功能都在这里。
2. **使用与示例**：`examples/`（怎么用）、`recipes/`（厂商/模型的部署配方）、`docs/`（文档源码）。
3. **工程与质量**：`tests/`（测试）、`benchmarks/`（性能压测）、`scripts/`、`tools/`、`docker/`、`requirements/`、`apps/`。
4. **打包元数据**：`pyproject.toml`、`setup.py`（在 u1-l2 已讲）。

#### 4.1.3 源码精读

README 的「About」段落一句话点明了项目定位，是理解整个目录为什么这样划分的起点：

- [README.md:31-37](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/README.md#L31-L37) —— 「About」小节，说明 vLLM-Omni 相对 vLLM 的三项扩展（全模态、非自回归结构、异构输出）。

README 还列出了它「快」与「易用」的卖点，这些卖点直接对应某些顶层目录的存在意义：

- [README.md:45-57](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/README.md#L45-L57) —— 列出 KV cache、流水线阶段重叠、OmniConnector 全解耦（性能）与异构流水线、HuggingFace 集成、分布式并行、流式输出、OpenAI 兼容 API（易用）。

下面这张表把顶层目录和它们的职责对应起来（**本表为示例整理，基于实际仓库结构**）：

| 顶层目录 | 职责（≤15 字） |
| --- | --- |
| `vllm_omni/` | 框架全部 Python 源码 |
| `examples/` | 离线推理与在线服务示例 |
| `recipes/` | 各厂商模型部署配方 |
| `docs/` | mkdocs 文档源码 |
| `tests/` | 单元/集成/端到端测试 |
| `benchmarks/` | 性能压测脚本 |
| `docker/` | 容器镜像构建 |
| `requirements/` | 各硬件平台依赖清单 |
| `scripts/` | 开发与运维辅助脚本 |
| `tools/` | 构建/打包辅助工具 |
| `apps/` | 配套应用（如展示界面） |

#### 4.1.4 代码实践

**实践目标**：亲手确认仓库根目录的真实面貌，而不是死记上表。

**操作步骤**：

1. 在仓库根目录执行 `git ls-files | cut -d/ -f1 | sort -u`，列出所有被 git 跟踪的「顶层路径」。
2. 对照上表，看看是否有你本机多出来的目录（例如 `vllm-omni-tutorial/` 是本学习手册目录，并不属于上游框架）。

**需要观察的现象**：输出里既有目录名（如 `vllm_omni`）也有顶层文件（如 `pyproject.toml`、`setup.py`、`README.md`、`CONTRIBUTING.md`）。

**预期结果**：你能区分「目录」与「打包元数据文件」。

#### 4.1.5 小练习与答案

**练习 1**：如果你想知道某个模型该怎么部署上线，应该先去哪个目录找现成的配方？

> **答案**：`recipes/`。该目录按厂商/团队分子目录（如 `recipes/Qwen`、`recipes/NVIDIA`），存放可复用的部署配方。

**练习 2**：项目根目录的 `CONTRIBUTING.md` 属于上表里的哪一类？

> **答案**：属于「打包元数据/工程文档」，它是贡献流程的入口说明，不放进任何子目录。

---

### 4.2 `vllm_omni` 包的顶层布局与初始化顺序

#### 4.2.1 概念说明

`vllm_omni/` 是框架唯一的源码包。它的内部组织遵循一个核心二分法：

- 🟡 **Modified（修改）**：把 vLLM 已有的组件「打补丁」改写，让它支持多模态。
- 🔴 **Added（新增）**：vLLM 里完全没有的新组件，比如扩散（Diffusion）引擎、OmniConnector。

这个二分法不是随手写的注释，而是阅读整个包的「阅读指南」——你在任何文件里看到的行为，要么是改了 vLLM 的，要么是完全新加的。

#### 4.2.2 核心流程

`vllm_omni/__init__.py` 在被 `import` 时，按严格顺序执行五步：

```text
1. 导入 version      → 最早执行，用于校验 vLLM / vllm_omni 主次版本是否对齐（不一致发告警）
2. 应用 patch        → 把 vLLM 的若干函数/类替换成 omni 版本（monkey-patch）
3. 注册 transformers_utils → 注册自定义 AutoConfig / AutoTokenizer / parser
4. 暴露 OmniModelConfig     → 把核心配置类挂到包顶层
5. 懒加载 Omni / AsyncOmni  → 真正用到时才 import，避免 import 时拖入重依赖
```

顺序非常关键：**version 必须早于 patch**。原因是「如果 vLLM 版本不匹配，patch 阶段去 import vllm 时可能直接抛错」，所以先用 version 比对发出告警，给人提前预警。

#### 4.2.3 源码精读

包入口文件开头的 docstring 就把二分法讲清楚了：

- [vllm_omni/\_\_init\_\_.py:9-13](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/__init__.py#L9-L13) —— docstring 说明本项目用「🟡 Modified」+「🔴 Added」两类来扩展 vLLM，支持多模态与非自回归结构。

初始化顺序在源码里按 import 的先后排布，每一步都有注释说明「为什么要在这一步」：

- [vllm_omni/\_\_init\_\_.py:15-19](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/__init__.py#L15-L19) —— 注释解释「version 要早于 patch」，因为版本不一致时 patch 阶段 import vllm 会抛错；随后 `from .version import __version__`。
- [vllm_omni/\_\_init\_\_.py:21-27](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/__init__.py#L21-L27) —— `from . import patch` 应用猴子补丁；并用 `try/except` 允许在没有 vllm 的环境下（如文档构建）跳过。
- [vllm_omni/\_\_init\_\_.py:29-37](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/__init__.py#L29-L37) —— 尽早注册自定义 configs 与 parsers（AutoConfig/AutoTokenizer）。
- [vllm_omni/\_\_init\_\_.py:39](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/__init__.py#L39) —— 暴露 `OmniModelConfig` 到包顶层。
- [vllm_omni/\_\_init\_\_.py:42-56](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/__init__.py#L42-L56) —— 用 `__getattr__` 实现 `Omni` / `AsyncOmni` 的懒加载，注释引用了 issue #1793：避免在 `import` 时拉入 `vllm.model_loader → fused_moe → pynvml` 等重依赖，导致没有 CUDA 上下文的轻量子进程崩溃。

#### 4.2.4 代码实践

**实践目标**：用一句话验证「懒加载」确实生效。

**操作步骤**：

1. 在装好 vllm_omni 的环境里执行：
   ```bash
   python -c "import vllm_omni; print('omni loaded'); print(hasattr(vllm_omni, 'Omni'))"
   ```
2. 注意：此时 `Omni` 尚未被真正 import（只是 `__getattr__` 会响应）。

**需要观察的现象**：`import vllm_omni` 本身很快返回，并不会立刻去加载模型相关重依赖；只有当你真正访问 `vllm_omni.Omni` 时，`__getattr__` 才触发真正的 `from .entrypoints.omni import Omni`。

**预期结果**：第一条 print 立即输出；第二条返回 `True`（因为 `__getattr__` 能解析它），但这不代表模型代码已被加载。

> 待本地验证：如果你在纯净环境（无 GPU）下运行，应能成功 import 而不报 CUDA 相关错误——这正是懒加载的设计目的。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `from .version import __version__` 必须放在 `from . import patch` 之前？

> **答案**：version 检查会在 vLLM 与 vllm_omni 主次版本不一致时提前发出 `RuntimeWarning`；若 patch 先执行，版本不一致可能导致 patch 阶段 `import vllm` 直接抛错，用户拿不到清晰的版本告警。

**练习 2**：如果有人把 `Omni` 直接写成 `from .entrypoints.omni import Omni` 放在 `__init__.py` 顶部，会引入什么风险？

> **答案**：会触发 eager import，把 `vllm.model_loader → fused_moe → pynvml` 等重依赖在 `import vllm_omni` 时就全部加载，可能让缺少 CUDA 上下文的轻量子进程（如模型结构检查）崩溃（见 issue #1793）。

---

### 4.3 九大核心子系统目录速览

#### 4.3.1 概念说明

`vllm_omni/` 内部有几十个子目录，但与架构「五大组件」直接对应的是下面九个**核心子系统目录**。先记住这九个，就足够在阅读后续讲义时「定位到目录」。其余子目录（如 `attention`、`cache`、`inputs`、`outputs`、`transformers_utils` 等）大多是被这九个目录引用的「工具目录」，本讲先不展开。

#### 4.3.2 核心流程

把九个目录按「请求的生命周期」串起来：

```text
用户请求
  └─► entrypoints  (Omni / AsyncOmni / cli / openai：定义入口与 API)
        └─► engine (AsyncOmniEngine + Orchestrator：多阶段编排)
              ├─► core    (AR/Generation 调度器)
              ├─► worker  (AR/Generation 的 GPU worker + model runner)
              └─► diffusion (DiT 引擎/调度/worker/模型/pipeline)
                    ├─► distributed  (OmniConnector 全解耦 + OmniCoordinator 协调)
                    ├─► platforms    (CUDA/ROCm/NPU/XPU/MUSA 平台抽象)
                    └─► quantization (统一量化框架)

config 贯穿所有阶段：声明「一个请求分成哪几个 stage、每 stage 是 AR 还是 Diffusion」
```

#### 4.3.3 源码精读

架构总览文档里的「Key Components」表，是这九个目录与架构组件对应关系的权威来源：

- [docs/design/architecture_overview.md:65-75](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/design/architecture_overview.md#L65-L75) —— 「Key Components」表，列出 OmniRouter / EntryPoints / AR / Diffusion / OmniConnector 五大组件的职责，并说明 Qwen3-Omni 的 Thinker/Talker/Code2wav 如何被声明为独立 stage。

下表把九个核心子目录与它们的关键文件、对应的架构组件对应起来（**本表为示例整理，基于实际仓库结构**）：

| 子目录 | 对应架构组件 | 关键文件举例 | 一句话职责 |
| --- | --- | --- | --- |
| `entrypoints/` | EntryPoints | `omni.py`、`async_omni.py`、`cli/`、`openai/` | 离线类与在线 API 入口 |
| `engine/` | EntryPoints（引擎层） | `async_omni_engine.py`、`orchestrator.py`、`stage_engine_core_proc.py` | 多阶段引擎与跨阶段编排 |
| `diffusion/` | Diffusion | `diffusion_engine.py`、`sched/`、`worker/`、`models/` | DiT 扩散推理引擎全栈 |
| `distributed/` | OmniConnector | `omni_connectors/`、`omni_coordinator/` | 阶段间全解耦通信与协调 |
| `config/` | （贯穿所有） | `model.py`、`omni_config.py`、`stage_config.py` | 模型/阶段/管道配置体系 |
| `platforms/` | （跨组件） | `interface.py`、`cuda/`、`npu/`、`rocm/`、`xpu/`、`musa/` | 多硬件平台抽象 |
| `quantization/` | （跨组件） | `factory.py`、`mxfp8_config.py`、`component_config.py` | 统一量化框架 |
| `worker/` | AR | `gpu_ar_worker.py`、`gpu_ar_model_runner.py`、`base.py` | AR/Generation 的 GPU 执行单元 |
| `core/` | AR | `core/sched/omni_ar_scheduler.py`、`omni_generation_scheduler.py` | AR/Generation 调度器 |

> 小贴士：`diffusion/` 是 vLLM 里**完全没有**的 🔴 新增模块，体量也最大——它内部还有自己的 `attention/`、`cache/`、`distributed/`、`executor/`、`worker/`、`models/` 等子目录，几乎是一个「迷你 vLLM」。后续 U5、U7 会专门拆解它。

#### 4.3.4 代码实践

**实践目标**：用 `git ls-files` 自行核对每个子目录的关键文件，避免凭记忆。

**操作步骤**：

1. 执行 `git ls-files vllm_omni/engine/ | cut -d/ -f3 | sort -u`，列出 `engine/` 下的直接成员。
2. 对 `entrypoints`、`diffusion`、`distributed`、`config`、`core` 各做一次，对比是否与上表「关键文件举例」一致。

**需要观察的现象**：你会看到每个目录都既有 `.py` 文件，也有更深的子目录（例如 `engine/` 下有 `orchestrator.py`，也有 `stage_engine_core_proc.py` 等多个「stage_*」文件）。

**预期结果**：你能独立从目录内容判断「这个目录大概负责什么」，而不依赖本讲表格。

#### 4.3.5 小练习与答案

**练习 1**：`diffusion/` 和 `worker/` 都和「执行模型」有关，它们分工有什么不同？

> **答案**：`worker/` 负责 **AR/Generation**（自回归）阶段的 GPU 执行单元（worker + model runner）；`diffusion/` 是 **DiT 扩散**阶段的一整套独立引擎，内部自带自己的 worker 与 model runner。两者分别对应架构里的 AR 组件与 Diffusion 组件。

**练习 2**：如果我想新增一种硬件后端，最可能要改哪个子目录？

> **答案**：`platforms/`。它已经按硬件分子目录（`cuda/`、`rocm/`、`npu/`、`xpu/`、`musa/`），新增硬件通常就是加一个同名子目录并实现 `interface.py` 里定义的抽象（详见 u8-l2）。

---

### 4.4 学习资源地图：examples / recipes / tests / docs

#### 4.4.1 概念说明

除了源码，仓库里还有四个目录是「读源码时的好帮手」：`examples`（怎么用）、`recipes`（怎么部署）、`tests`（怎么验证行为）、`docs`（怎么理解设计）。学会用它们，能让你在「看不懂某段源码」时迅速找到对应的示例或测试来佐证。

#### 4.4.2 核心流程

四类资源的用法各不相同：

```text
想跑一个功能      → examples/offline_inference（离线脚本）或 examples/online_serving（在线服务）
想部署上线        → recipes/<厂商或模型>/ 里的部署配方
想确认某行为对不对 → tests/<对应子系统>/ 里的断言
想理解某设计动机   → docs/design（设计文档）或 docs/contributing（贡献/CI 指南）
```

#### 4.4.3 源码精读

**examples** 分两大类，对应架构总览里「Offline Inference」与「Online Serving」两种用法：

- `examples/offline_inference/`：Python 脚本，用 `Omni` / `AsyncOmni` 类做离线批处理推理，按任务类型分子目录（如 `text_to_image/`、`text_to_video/`、`qwen3_omni/`、`speech_to_video/`）。
- `examples/online_serving/`：先 `vllm serve --omni` 起服务，再用客户端（curl / Python client / gradio demo）调用，同样按模型分子目录。

> 简记：**offline = 直接 `import` 类来跑；online = 起服务后用 HTTP 调。** 两者的对应关系在 [docs/design/architecture_overview.md:126-151](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/design/architecture_overview.md#L126-L151) 里有代码示例（Omni 类离线推理 / `vllm serve --omni` 在线服务）。

**docs** 也分两大块，性质完全不同：

| 目录 | 性质 | 典型内容 |
| --- | --- | --- |
| `docs/design/` | **设计文档**（讲「为什么这样设计」） | `architecture_overview.md`、`module/`（ar_module、dit_module、async_omni_architecture）、`feature/`（teacache、tensor_parallel、cfg_parallel、disaggregated_inference 等） |
| `docs/contributing/` | **贡献与工程指南**（讲「怎么参与、怎么跑 CI」） | `ci/`（test_system_overview、test_writing_guide、ci_settings）、`model/`（adding_diffusion_model、adding_omni_model、adding_tts_model）、`profiling.md` |

**tests** 的目录几乎**镜像**了 `vllm_omni/` 的结构：有 `tests/diffusion/`、`tests/engine/`、`tests/entrypoints/`、`tests/core/`、`tests/quantization/`、`tests/platforms/` 等，外加 `tests/e2e/`、`tests/dfx/`、`tests/examples/` 这类更高层级的测试。这意味着「想看某模块怎么用，就去同名 tests 目录找用例」。

**recipes** 按厂商/团队分子目录（如 `recipes/Qwen`、`recipes/NVIDIA`、`recipes/ByteDance`），存放各模型的可复用部署配方。

#### 4.4.4 代码实践

**实践目标**：用真实目录结构印证「tests 镜像 vllm_omni」这一规律。

**操作步骤**：

1. 执行 `git ls-files tests/ | cut -d/ -f1-2 | sort -u`，列出 `tests/` 的一级子目录。
2. 执行 `git ls-files vllm_omni/ | cut -d/ -f2 | sort -u`，列出 `vllm_omni/` 的一级成员。
3. 把两边的结果并排对比。

**需要观察的现象**：`tests/` 下的 `diffusion`、`engine`、`entrypoints`、`core`、`quantization`、`platforms` 等名字，几乎都能在 `vllm_omni/` 下找到同名的源码目录。

**预期结果**：你得到一份「源码目录 ↔ 测试目录」的对照表，今后遇到看不懂的源码，能第一时间去同名 tests 目录找断言佐证。

#### 4.4.5 小练习与答案

**练习 1**：我想搞清楚 TeaCache（扩散加速）的设计动机，该去 `docs/` 的哪里找？

> **答案**：去 `docs/design/feature/teacache.md`（设计文档，讲为什么）。注意区分：`docs/design` 讲设计，`docs/contributing` 讲工程流程。

**练习 2**：`examples/offline_inference/qwen3_omni/` 和 `examples/online_serving/qwen3_omni/`（若存在）有什么本质区别？

> **答案**：前者是直接 `from vllm_omni.entrypoints.omni import Omni` 然后调 `generate()` 的离线脚本；后者是先 `vllm serve --omni` 启动 OpenAI 兼容服务、再用 HTTP 客户端调用的在线示例。两者跑的是同一个模型，但入口方式不同。

---

## 5. 综合实践

把本讲知识串成一张**速查表**，这是后续阅读时你会反复回看的产物。

**任务**：为 `vllm_omni/` 下的每个一级目录生成一张「树形图 + ≤15 字职责」的速查表。

**操作步骤**：

1. 用下面命令拿到 `vllm_omni/` 的全部一级成员：
   ```bash
   git ls-files vllm_omni/ | cut -d/ -f2 | sort -u
   ```
2. 区分哪些是 `.py` 文件（如 `__init__.py`、`patch.py`、`version.py`、`request.py`、`errors.py`、`logger.py`、`data_entry_keys.py`），哪些是子目录。
3. 对每个子目录，结合本讲 4.3 的表格与你在 4.3.4、4.4.4 实践里观察到的内容，写一句**不超过 15 个汉字**的职责说明。
4. 用 Markdown 画成树形图，例如：

   ```text
   vllm_omni/
   ├── __init__.py        # 包入口，定义导入顺序
   ├── patch.py           # 对 vLLM 打猴子补丁
   ├── version.py         # 版本号与对齐告警
   ├── entrypoints/       # 离线/在线服务入口
   ├── engine/            # 多阶段引擎与编排
   ├── diffusion/         # DiT 扩散推理引擎全栈
   ├── distributed/       # 全解耦通信与协调
   ├── config/            # 模型与阶段配置体系
   ├── core/              # AR/Generation 调度器
   ├── worker/            # AR/Generation GPU 执行单元
   ├── platforms/         # 多硬件平台抽象
   ├── quantization/      # 统一量化框架
   ├── inputs/            # 多模态 prompt 数据结构
   ├── outputs/           # 统一输出数据结构
   ├── transformers_utils/# 自定义 config/tokenizer 注册
   ├── model_executor/    # 模型执行辅助
   ├── attention/         # （扩散）注意力层（部分）
   ├── experimental/      # 实验特性（世界模型等）
   └── ...                # 其余工具目录
   ```

**需要观察的现象**：你会发现 `vllm_omni/` 的一级成员远不止九个核心子系统，还有 `attention`、`cache`（在 diffusion 内）、`lora`、`metrics`、`profiler`、`reasoning`、`sample`、`tokenizers`、`utils`、`plugins`、`deploy`、`model_extras`、`benchmarks` 等「工具型」目录。

**预期结果**：得到一份你自己核验过、可直接贴在桌面的「vllm_omni 目录速查表」。今后读到任何讲义提到某个文件，你都能在这张表上快速定位它属于哪一类。

> 对于不确定职责的目录，可以在表里标注「待确认」，留到对应专题讲义（如 `attention` 留到 u7-l1、`quantization` 留到 u8-l1）再回来补全。

## 6. 本讲小结

- 仓库顶层分四类：**核心代码** `vllm_omni/`、**使用示例** `examples/recipes/docs`、**工程质量** `tests/benchmarks/scripts/tools/docker`、**打包元数据** `pyproject.toml/setup.py`。
- `vllm_omni/__init__.py` 用「🟡 修改 + 🔴 新增」二分法组织扩展，并严格按 **version → patch → 注册 configs → 暴露 OmniModelConfig → 懒加载 Omni/AsyncOmni** 的顺序初始化。
- 九大核心子系统目录与架构组件一一对应：`entrypoints`/`engine`（EntryPoints）、`core`/`worker`（AR）、`diffusion`（Diffusion）、`distributed`（OmniConnector）、`config`/`platforms`/`quantization`（跨组件基础）。
- `diffusion/` 是最大的 🔴 新增模块，内部几乎是一个「迷你 vLLM」，自带 attention/cache/worker/models 等子目录。
- `examples` 分 `offline_inference`（直接 import 类）与 `online_serving`（起服务用 HTTP 调）两类；`docs` 分 `design`（为什么这样设计）与 `contributing`（怎么参与/跑 CI）两类。
- `tests/` 的目录结构**镜像** `vllm_omni/`，看不懂源码时可去同名 tests 目录找断言佐证。

## 7. 下一步学习建议

- 想立刻「跑起来」？下一讲 **u1-l4（离线推理初体验）** 会用 `examples/offline_inference/text_to_image/` 做第一次文生图，建议接着学。
- 想理解「包入口怎么把请求接到引擎」？学完 u1-l4、u1-l5 后，进入 **u2-l1（patch 机制）**，深入 `patch.py` 的猴子补丁细节。
- 想提前看官方架构全貌？直接读 [docs/design/architecture_overview.md](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/design/architecture_overview.md) 的「Key Components」与「Main features」两节。
- 后续遇到某个目录看不懂时，回到本讲 **第 5 节的速查表** 定位它属于哪一类，再去对应的专题讲义（如 diffusion 看 U5、distributed 看 U3、quantization 看 U8）深入学习。

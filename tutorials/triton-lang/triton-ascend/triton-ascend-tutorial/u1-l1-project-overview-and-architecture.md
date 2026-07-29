# Triton-Ascend 是什么：项目定位与逻辑架构

## 1. 本讲目标

读完这一篇，你应该能够：

- 说清楚 **Triton-Ascend** 和社区标准 **Triton** 的关系，理解它「为什么存在」。
- 认识 Triton-Ascend 的三大核心组件：**Ascend 语言扩展（language extension）**、**编译器（compiler）**、**驱动（driver）**，并说清楚每个组件负责什么。
- 在脑子里建立起一条端到端的编译链路：

  ```text
  Triton IR (TTIR) → Linalg IR → AscendNPU IR → triton_xxx_kernel.o
  ```

- 知道这条链路对应的真实源码文件分别在哪里，后续讲义会逐个深入。

本篇是整本学习手册的**第一讲**，定位是「先建立全局印象」。我们不追求把任何一个组件讲透，而是让你在深入源码之前先有一张地图。

## 2. 前置知识

在开始之前，用通俗的语言先建立几个概念。即使你完全不熟悉这些名词也没关系，下面都会解释。

- **Triton**：一个用于编写高性能 GPU/NPU kernel 的 Python DSL（领域特定语言）。你用 `@triton.jit` 装饰一个普通 Python 函数，里面用 `tl.load`、`tl.store`、`tl.dot` 这样的接口描述「每个并行任务（program）要做什么」，Triton 编译器再把它编译成可以在硬件上跑的二进制。社区标准 Triton 主要面向 NVIDIA GPU。
- **Ascend（昇腾）NPU**：华为的自研 AI 处理器，例如 Atlas A2/A3/950 系列。它和 GPU 在硬件架构上有较大差别（比如有专门的 Cube 矩阵单元和 Vector 向量单元），软件栈叫 **CANN**。
- **BiSheng Compiler（毕昇编译器）**：昇腾生态里的底层编译器，能把中间表示（IR）最终编译成 NPU 上可执行的二进制（`.o`）。在 Triton-Ascend 里，它扮演「最后一棒」的角色。
- **IR（Intermediate Representation，中间表示）**：编译器内部用来表示程序的「中间语言」。Triton-Ascend 这条链路上会出现好几种 IR，你可以把它们理解成同一份 kernel 在编译不同阶段的「翻译版本」。
- **TTIR**：Triton 自己定义的 IR，是 Triton 编译流程的「起点产物」。

> 一句话定位：**Triton-Ascend 就是把社区标准 Triton「接」到华为昇腾 NPU 上的那套后端实现。** 你写 kernel 的方式几乎和写 GPU Triton 一样，但跑在 NPU 上。

## 3. 本讲源码地图

本讲主要「远观」以下文件，先认识它们各自承担的角色，不需要逐行读懂：

| 文件 | 所属组件 | 作用 |
| --- | --- | --- |
| [README.md](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/README.md) | 项目总览 | 项目定位、支持的硬件、安装方式、文档入口 |
| [docs/en/architecture_design_and_core_features.md](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/docs/en/architecture_design_and_core_features.md) | 架构文档 | 官方对三大组件与编译链路的权威描述 |
| [third_party/ascend/backend/compiler.py](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py) | compiler | 编译后端入口：注册编译阶段、组织 TTIR 的 lowering |
| [third_party/ascend/backend/driver.py](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py) | driver | 运行时驱动：连接 CANN、生成 launcher、启动 kernel |
| [third_party/ascend/language/cann/\_\_init\_\_.py](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/language/cann/__init__.py) | Ascend 语言扩展 | 暴露 `triton.language.extra.cann`，提供 libdevice 等扩展接口 |

> 注意目录命名：凡是 `third_party/ascend/` 开头的，都是「昇腾亲和（target affinitive）」代码；而仓库根目录下的 `python/`、`include/`、`lib/` 则是「与目标无关（target independent）」的社区 Triton core。这个分层原则会在 [u1-l2](u1-l2-directory-structure-and-layering.md) 专门讲，本讲先记住这个大致区分。

## 4. 核心概念与源码讲解

本讲按「先讲定位，再讲三个组件，最后串成一条链」的顺序展开。四个最小模块分别对应：项目定位、compiler、driver、Ascend 语言扩展。

### 4.1 Triton-Ascend 的定位：社区 Triton 的「昇腾后端」

#### 4.1.1 概念说明

标准 Triton 的设计是「前端语言统一，后端可插拔」。也就是说，你写的 Python kernel 先被翻译成与硬件无关的 TTIR；至于「TTIR 之后怎么变成能在某块硬件上跑的二进制」，由具体的**后端（backend）**决定。社区自带的是 NVIDIA GPU 后端。

**Triton-Ascend 就是新增的那个「昇腾 NPU 后端」**。它的职责是：接收上游 Triton 编译器产出的 TTIR，再针对昇腾硬件做一系列变换，最终借助 BiSheng 编译器生成 NPU 可执行二进制 `triton_xxx_kernel.o`，并在运行时通过 CANN 软件栈把它跑起来。

#### 4.1.2 核心流程

从「宏观」看，一个 kernel 从你写下 Python 代码到在 NPU 上执行，会经过这样几个阶段：

```text
[你写的 Python kernel] @triton.jit
        │  上游社区 Triton 负责
        ▼
   Triton IR (TTIR)        ← 上游产出的「起点」，与硬件无关
        │  交给 Triton-Ascend 后端（compiler 组件）
        ▼
   Linalg IR               ← Ascend 后端把 TTIR lowering 成 Linalg
        │
        ▼
   AscendNPU IR            ← 进一步靠近昇腾硬件
        │  交给 BiSheng 编译器
        ▼
   triton_xxx_kernel.o     ← NPU 上可执行的二进制
        │  运行时由 driver 组件通过 CANN 加载并启动
        ▼
   [在 NPU 上运行]
```

这条链路是 Triton-Ascend 的「主旋律」，后续所有讲义（编译流水线、pass 链、运行时、调优……）都是在为这条链路的某个环节「放大讲解」。所以**请把这张草图牢牢记住**。

#### 4.1.3 源码精读

官方架构文档给出了三大组件的权威定义和这条编译链路的文字描述：

> 核心组件：`Ascend language extension`、`compiler`、`driver`。
> compiler 把 Triton IR 一步步变换为适配昇腾硬件的形式，最终经 BiSheng 编译器生成可执行二进制。

- 三大组件的官方定义见 [docs/en/architecture_design_and_core_features.md:8-12](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/docs/en/architecture_design_and_core_features.md#L8-L12)。
- 编译链路 `Triton IR → Linalg IR → AscendNPU IR → triton_xxx_kernel.o` 的原文见 [docs/en/architecture_design_and_core_features.md:22-29](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/docs/en/architecture_design_and_core_features.md#L22-L29)（这一段同时点明了 compiler 负责 lowering、driver 负责加载二进制）。
- 项目支持的硬件（Atlas A2/A3/950 系列）见 [README.md:55-59](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/README.md#L55-L59)，软件依赖（Python / CANN / TorchNPU）见 [README.md:65-69](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/README.md#L65-L69)。

这几个引用帮你确认：本项目「面向的对象」（昇腾 NPU + CANN）和「要做的事」（把标准 Triton 接到这套硬件上）。

#### 4.1.4 代码实践

**实践目标**：通过对比「项目说的」和「源码里写的」，确认 Triton-Ascend 确实是「后端」而非「全新的语言」。

**操作步骤**：

1. 打开架构文档的 [「3.2.1 Compiler Options」表](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/docs/en/architecture_design_and_core_features.md#L69-L91)，注意它讨论的是「NPU Option」，而不是「Triton 语法」——说明这一层关注的是「如何为某块硬件编译」，正是后端的职责。
2. 在 `compiler.py` 中搜索类名 `AscendBackend`，确认它继承自一个叫 `BaseBackend` 的基类（见下方 4.2.3）。这正是「可插拔后端」架构的体现：昇腾只是众多「子类后端」之一。

**需要观察的现象**：你会发现整篇文档/源码都在讨论「TTIR 之后怎么走」，几乎不重新定义 `@triton.jit` 本身——这正是「后端」而非「新语言」的标志。

**预期结果**：你能用自己的话说出「上游 Triton 负责把 Python 变成 TTIR，Triton-Ascend 负责把 TTIR 变成昇腾二进制」。

#### 4.1.5 小练习与答案

1. **练习**：如果社区标准 Triton 已经能编译到 GPU，那 Triton-Ascend 为什么还要单独存在？不能直接在 GPU 后端里加一段吗？
   **参考答案**：因为昇腾 NPU 的硬件模型（Cube/Vector 双单元、UB 片上存储、CANN 软件栈）和 GPU 差别很大，所需的 IR 变换、算子 lowering、运行时启动方式都完全不同。把这些强绑硬件的代码塞进通用 GPU 后端会污染标准 Triton。所以采用「可插拔后端」的方式，让昇腾相关逻辑独立成 `third_party/ascend/`。

2. **练习**：在这条链路 `TTIR → Linalg IR → AscendNPU IR → .o` 中，哪一段最「贴近硬件」？
   **参考答案**：`.o`（`triton_xxx_kernel.o`）最贴近硬件，它是直接可执行的二进制；越靠左越抽象、越与硬件无关（TTIR 是与硬件无关的起点）。

### 4.2 compiler：TTIR 到可执行二进制的编译后端

#### 4.2.1 概念说明

compiler 组件是三个组件里「最重」的一个，本讲只看它的「外壳」：它如何向 Triton 框架**注册**自己、如何**声明**自己包含哪些编译阶段。至于每个阶段内部的 MLIR pass 细节，会留给 [u4（MLIR pass 流水线）](u4-l1-ttir-to-linalg-pipeline-overview.md) 详解。

关键类有三个：

- **`AscendBackend`**：昇腾后端的「门面类」，继承自 Triton 框架的 `BaseBackend`。Triton 框架通过它识别「这是不是昇腾后端」「有哪些编译选项」「有哪些编译阶段」。
- **`NPUOptions`**：编译选项的数据类，比如 `num_warps`、`compile_mode`、`arch` 等，决定一个 kernel「按什么参数编译」。
- **`make_ttir` / `ttir_to_linalg` / `ttir_to_npubin`**：每一个就是一个「编译阶段」的实际实现函数，对应链路上的一个箭头。

#### 4.2.2 核心流程

Triton 框架要求每个后端实现一个 `add_stages(...)` 方法，把「阶段名 → 该阶段的处理函数」登记进一个字典 `stages`。框架会按依赖顺序依次调用这些函数，前一个的输出就是后一个的输入。Triton-Ascend 登记的阶段大致是：

```text
ttir   ──make_ttir──▶  (force_simt_only 特殊路径直接到 npubin)
  │ ttir_to_linalg
  ▼
ttadapter  ──(可选 bytecode 往返)──▶  mlirbc ──▶ bcmlir
  │
  ▼  linalg_to_bin_*  （由硬件架构选择不同实现）
npubin   = 最终可执行二进制
```

伪代码（简化自 `add_stages`）：

```python
# 示例代码：简化自 AscendBackend.add_stages，仅展示阶段注册结构
stages["ttir"]       = lambda src, metadata: make_ttir(src, metadata, options)
if options.force_simt_only:                      # 纯 SIMT：跳过 linalg 直达二进制
    stages["npubin"] = lambda src, metadata: ttir_to_npubin(src, metadata, options)
    return
stages["ttadapter"]  = lambda src, metadata: ttir_to_linalg(src, metadata, options, ...)
stages["npubin"]     = lambda src, metadata: linalg_to_bin_*(src, metadata, options)  # 按架构二选一
```

注意那个 `force_simt_only` 分支：它**跳过 Linalg 阶段**，把 TTIR 直接送到 AscendNPU IR。这就是为什么不同 `compile_mode` 会走出不同的链路形态（详见 [u6（SIMD/SIMT 双路径）](u6-l1-compile-mode-overview.md)）。

#### 4.2.3 源码精读

- **`AscendBackend` 门面类**与「是否支持某 target」的判断：[third_party/ascend/backend/compiler.py:1200-1204](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L1200-L1204)。这里 `supports_target` 只认 `backend == "npu"`，这正是「我是昇腾后端」的身份声明。
- **`NPUOptions` 编译选项数据类**：[third_party/ascend/backend/compiler.py:988-1000](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L988-L1000)。可以看到默认 `num_warps=32`、`arch` 默认为空（运行时再补全）等关键字段。
- **`parse_options` 如何把用户传入的字典变成 `NPUOptions`**：[third_party/ascend/backend/compiler.py:1213-1232](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L1213-L1232)。
- **`add_stages` 阶段注册**（本组件的核心）：[third_party/ascend/backend/compiler.py:1269-1290](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L1269-L1290)。这一段就是 4.2.2 那张流程图的源码出处，注意末尾 `compile_on_910_95` 分支会为 910/950 与 A2/A3 选择不同的 `linalg_to_bin_*` 实现。
- **`make_ttir`**（链路第一个阶段，做与硬件无关的标准优化）：[third_party/ascend/backend/compiler.py:132-152](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L132-L152)。它调用的是 `passes.common.add_inliner`、`passes.ttir.add_combine` 这类**通用** Triton pass，说明这一步确实「与目标无关」。
- **`ttir_to_linalg`**（链路第二个阶段，开始进入昇腾专属 pass 链）：[third_party/ascend/backend/compiler.py:155](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L155)。从这行往下的 `ascend.passes.ttir.add_*` 调用，就是后续 u4 会逐个剖析的昇腾 pass（structured / unstructured / linalg 等）。

#### 4.2.4 代码实践

**实践目标**：在源码里「数出」这条链路到底注册了哪些阶段名，把抽象的链路图落到具体符号上。

**操作步骤**：

1. 打开 [third_party/ascend/backend/compiler.py:1269-1290](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L1269-L1290)。
2. 找到所有形如 `stages["xxx"] = ...` 的赋值，记录 `xxx` 分别是什么（应能看到 `ttir`、`npubin`、`ttadapter`，以及在 `use_bytecode` 为真时额外的 `mlirbc`、`bcmlir`）。
3. 对照 4.1.2 的草图，把每个阶段名标到草图对应的箭头上。

**需要观察的现象**：阶段名和草图里的 IR 名不完全一样（例如草图里叫「Linalg IR」，源码里对应阶段名叫 `ttadapter`）。这是因为 `ttadapter` 是 Ascend 后端对「Linalg 阶段产物」的内部命名。

**预期结果**：你能写出一份「阶段名 → 处理函数 → 产物 IR」的对照表。例如：`ttir → make_ttir → TTIR`；`ttadapter → ttir_to_linalg → Linalg IR`。

#### 4.2.5 小练习与答案

1. **练习**：`make_ttir` 里调用的是 `passes.common.*` 和 `passes.ttir.*`，而 `ttir_to_linalg` 里大量调用 `ascend.passes.ttir.*`。这种区别说明了什么？
   **参考答案**：说明 `make_ttir` 做的是**与硬件无关**的通用 Triton 优化（所以可以放在社区 core 也可在后端复用），而 `ttir_to_linalg` 开始进入**昇腾专属**的 pass 链。这正是「链路越往右越贴硬件」的体现。

2. **练习**：`add_stages` 里 `force_simt_only` 为真时，为什么直接 `return` 了？
   **参考答案**：因为纯 SIMT 模式（`simt_only`）要把 TTIR **直接**送到 AscendNPU IR，跳过 Linalg 那一整套 lowering，所以登记完 `ttir` 和 `npubin` 两个阶段就不再需要 `ttadapter` 等中间阶段了，直接 `return`。

### 4.3 driver：运行时与 CANN 的连接器

#### 4.3.1 概念说明

compiler 把 kernel 编译成了 `.o` 二进制，但「谁来把这个二进制加载到设备上、按什么参数启动它」？这就是 **driver** 组件的活。它的核心任务是：

- **发现设备、探测硬件**：当前是哪块卡、什么架构（arch）、有多少 AI 核 / Vector 核。
- **生成 launcher（启动器）**：针对每个 kernel 的签名，动态生成一小段 C++ 代码（launcher），负责把 Python 传进来的张量指针、标量参数打包好，再调用 CANN 运行时 API 启动 kernel。
- **连接 CANN / TorchNPU 运行时**：拿到当前的 device、stream，把 kernel 提交给正确的硬件流。

driver 里的三个关键类：

- **`NPUDriver`**：驱动门面，继承自框架的 `DriverBase`。框架靠它判断「当前环境能不能用昇腾」、拿到当前 target/device。
- **`NPUUtils`**：工具类，负责探测硬件信息（如架构、核数）。
- **`NPULauncher`** + **`make_launcher` / `make_npu_launcher_stub`**：负责按签名生成、编译、缓存 C++ launcher，并最终在设备上启动 kernel。

#### 4.3.2 核心流程

driver 在「编译完成 → 真正运行」之间做的事：

```text
[compiler 产出的 npubin 二进制]
        │
   NPUDriver.is_active? ───yes──▶ 确认环境可用（探测 bisheng 是否支持 hiipu64）
        │
        ▼
   NPUUtils 探测 arch / 核数
        │
        ▼
   make_launcher：按 kernel 签名生成 C++ launcher 源码 ──编译成 .so ──缓存
        │
        ▼
   NPULauncher：收集张量指针/标量参数，绑定到当前 device/stream
        │
        ▼
   通过 CANN 运行时 API 启动 kernel ──▶ [在 NPU 上运行]
```

#### 4.3.3 源码精读

- **`NPUDriver` 门面类**：[third_party/ascend/backend/driver.py:199-203](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L199-L203)。它在 `__init__` 里就把 `self.utils = NPUUtils()`、`self.launcher_cls = NPULauncher` 装配好，说明驱动 = 探测 + 启动两部分。
- **`is_active`：如何判断「当前能不能用昇腾」**：[third_party/ascend/backend/driver.py:206-222](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L206-L222)。它的判定方式很具体：调用 bisheng 编译器的 `-print-targets`，看结果里有没有 `hiipu64`（昇腾设备的目标三元组标识）。
- **`get_current_target`：如何确定当前架构**：见同文件 [driver.py:227-235](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L227-L235)，会优先读环境变量，再回退到 `NPUUtils.get_arch()`。
- **`NPUUtils` 与 `NPULauncher` 类定义位置**：[driver.py:42](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L42)、[driver.py:150](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L150)（本讲只定位，深入留给 [u5](u5-l1-npu-driver-and-utils.md)）。
- **`make_npu_launcher_stub`：launcher stub 的生成与缓存入口**：[third_party/ascend/backend/driver.py:276-285](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L276-L285)。可以看到它根据 `header_src + wrapper_src` 算 hash 作为缓存键，并处理 CXX11 ABI，这正是「按签名生成 + 缓存」的实现。
- **`make_launcher`：按签名生成 launcher 源码的核心函数**：[third_party/ascend/backend/driver.py:428](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L428)。

#### 4.3.4 代码实践

**实践目标**：理解「launcher 是按需生成并缓存的」这件事，而不只是抽象地「启动 kernel」。

**操作步骤**：

1. 阅读 [make_npu_launcher_stub 的缓存逻辑](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L276-L285)，回答：launcher 的 `.so` 缓存键由什么决定？
2. 思考：为什么同一个 kernel 第一次运行会比较慢、之后会变快？

**需要观察的现象 / 预期结果**：缓存键由 `header_src + wrapper_src`（即 kernel 的签名/包装代码）的 hash 决定，且区分 CXX11 ABI。因此签名相同的 kernel 第二次运行会命中缓存、跳过重新编译。如果你本地有昇腾环境，开启 `TRITON_DEBUG=1` 运行一个 tutorial，能在 dump 目录里看到生成的 launcher `.cxx` 源码；没有环境则标注「待本地验证」。

#### 4.3.5 小练习与答案

1. **练习**：`NPUDriver.is_active` 为什么不去直接查「有没有 NPU 设备」，而是去问 bisheng 编译器支不支持 `hiipu64`？
   **参考答案**：因为 Triton 的后端选择发生在编译期。即使有物理设备，如果**编译工具链**（bisheng）不可用或不含昇腾 target，整个编译链路也跑不通。所以用「编译器是否支持该 target」作为「后端是否可用」的判定更稳妥。

2. **练习**：driver 组件和 compiler 组件的「输入/输出」分别是什么？
   **参考答案**：compiler 的输入是 TTIR（+ 编译选项），输出是 `triton_xxx_kernel.o`（npubin 二进制）；driver 的输入是这个二进制（+ 运行时的张量指针、grid 等参数），输出是「kernel 在 NPU 上被启动执行」。

### 4.4 Ascend 语言扩展：让 kernel 能用昇腾特有的能力

#### 4.4.1 概念说明

前两个组件（compiler、driver）对用户是「透明」的——你写 kernel 时不会直接调用它们。而 **Ascend 语言扩展**是「你能直接在 kernel 里用到」的那一层。

标准 Triton 语言（`tl.load`、`tl.dot` 等）面向通用场景，但昇腾有一些特有的硬件能力（比如片上 Unified Buffer 的高效 gather/scatter、跨核同步原语、底层数学算子）需要专门的接口来暴露。Ascend 语言扩展就是干这个的，它通过 `triton.language.extra.cann` 这个命名空间挂载进来，提供：

- **`libdevice`**：底层数学函数封装（如 `reciprocal`、`log1p` 等）。
- **`extension`**：昇腾亲和算子，包括 `tl.custom_op` 自定义算子、`tl.compile_hint` 编译提示、`tl.sync_block_*` 跨核同步原语等。

这些扩展在后面的 [u7（Ascend 语言扩展）](u7-l1-cann-extension-and-libdevice.md) 会逐个深入，本讲只建立「它存在、它挂在哪里、它提供什么」的印象。

#### 4.4.2 核心流程

语言扩展的「挂载 + 使用」链路：

```text
安装时：third_party/ascend/language/  ──链接到──▶  triton.language.extra.cann
                                                        │
写 kernel 时：  import triton.language.extra.cann.extension as extension
                import triton.language.extra.cann.libdevice as libdevice
                                                        │
                                                        ▼
        kernel 内可调用 extension.* / libdevice.* ──▶ 进入 compiler 的 pass 链被 lowering
```

#### 4.4.3 源码精读

- **语言扩展的导出入口**：[third_party/ascend/language/cann/\_\_init\_\_.py:21-53](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/language/cann/__init__.py#L21-L53)。可以看到它 `from . import libdevice` 和 `from . import extension`，并在 `__all__` 里导出这两者——这就是 `triton.language.extra.cann` 对外暴露的两个子模块。
- **官方对语言扩展职责的描述**：[docs/en/architecture_design_and_core_features.md:16-17](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/docs/en/architecture_design_and_core_features.md#L16-L17)（「基于标准 Triton 语言，为昇腾 NPU 架构引入语法和语义扩展」）。
- **真实 kernel 中如何使用该扩展（证据）**：tutorial 里就有现成例子，例如 [third_party/ascend/tutorials/03-matrix-multiplication.py:29](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/tutorials/03-matrix-multiplication.py#L29) 的 `import triton.language.extra.cann.extension as extension`，以及 [third_party/ascend/tutorials/07-extern-functions.py:31](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/tutorials/07-extern-functions.py#L31) 的 `import triton.language.extra.cann.libdevice as libdevice`。这说明「语言扩展 = 在 kernel 里 `import` 即可用的那层」是真实落地的，不是文档空话。

#### 4.4.4 代码实践

**实践目标**：亲手确认「昇腾语言扩展确实以 `triton.language.extra.cann` 的形式存在，且被真实 kernel 使用」。

**操作步骤**：

1. 打开上面的 [03-matrix-multiplication.py](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/tutorials/03-matrix-multiplication.py#L29)，看它在 import 之后，函数体里是怎么用 `extension` 的（搜一下 `extension.` 的调用）。
2. 对比标准 Triton 的 import（`import triton.language as tl`）和这里的 `import triton.language.extra.cann.extension as extension`，体会「标准语言」与「昇腾扩展」是**并列叠加**的关系，而不是替换。

**需要观察的现象**：kernel 里既用 `tl.dot`（标准），又用 `extension.xxx`（昇腾扩展）——两套 API 在同一个 kernel 里和平共处。

**预期结果**：你能说清楚「`tl` 是标准 Triton 语言，`extension`/`libdevice` 是昇腾在这个语言上额外挂载的能力」。

#### 4.4.5 小练习与答案

1. **练习**：语言扩展（`extra.cann`）和 compiler、driver 在「用户可见性」上有什么不同？
   **参考答案**：语言扩展是**用户写 kernel 时直接 import、直接调用**的；而 compiler 和 driver 对用户基本透明，用户通常只通过 `@triton.jit`、编译选项和运行参数间接影响它们。

2. **练习**：为什么 `triton.language.extra.cann` 这种「`extra` + 后端名」的命名方式是合理的？
   **参考答案**：因为它把「标准语言」和「后端专属扩展」放在同一个 `triton.language` 命名空间下、用 `extra.<backend>` 区分，既保持了 API 的一致性（都从 `triton.language` 出发），又清晰隔离了后端亲和代码，方便不同后端各自挂载自己的扩展。

## 5. 综合实践

> 这正是本讲规格里要求的核心实践：阅读架构文档，用自己的话画一张「Python kernel → `.o` 可执行文件」的数据流草图，标注三大组件分别介入的位置。

**任务**：把本讲的四个模块串成一张完整的「全局图」。请你（可以用纸笔或任何画图工具）画出下面这张图，并满足三个要求。

1. **画出完整链路**：从「你写下 `@triton.jit` 的 Python 函数」开始，到「kernel 在 NPU 上运行」结束，中间至少包含 `TTIR → Linalg IR → AscendNPU IR → triton_xxx_kernel.o` 四个产物节点。
2. **标注三大组件的介入区间**：用三种颜色（或三种标记）分别标出
   - **Ascend 语言扩展**介入在哪里（提示：在「写 kernel」阶段，提供 `extra.cann`）；
   - **compiler** 介入在哪里（提示：`TTIR → ... → npubin` 这一段）；
   - **driver** 介入在哪里（提示：`npubin → 在设备上启动` 这一段）。
3. **标注关键源码落点**：在图上对应位置写上源码文件，至少包含
   - `third_party/ascend/backend/compiler.py` 的 `add_stages`（compiler 阶段注册）；
   - `third_party/ascend/backend/driver.py` 的 `NPUDriver`（driver 门面）；
   - `third_party/ascend/language/cann/__init__.py`（语言扩展入口）。

**完成后自检**：你的图能否回答以下问题？

- 「TTIR 是谁产生的？」「`.o` 是谁产生的？」「`.o` 是谁加载运行的？」
- 三个组件里，哪个是「写代码时用到」、哪个是「编译时用到」、哪个是「运行时用到」？

如果三个问题都能对着图答上来，本讲的目标就达成了。把这张图保存好——它将是你阅读后续所有讲义时的「总导航图」。

## 6. 本讲小结

- **Triton-Ascend 是社区标准 Triton 的「昇腾 NPU 后端」**，不重新发明语言，而是负责把上游产出的 TTIR 变成能在昇腾上跑的二进制。
- 三个核心组件各有分工：**Ascend 语言扩展**（kernel 里可调用的昇腾能力）、**compiler**（TTIR → 二进制的编译后端）、**driver**（运行时连接 CANN、生成 launcher、启动 kernel）。
- 编译主链路是 `Triton IR (TTIR) → Linalg IR → AscendNPU IR → triton_xxx_kernel.o`，越往右越贴近硬件。
- compiler 的「外壳」由 `AscendBackend` + `NPUOptions` + `add_stages` 构成，`add_stages` 把每个编译阶段登记成 `阶段名 → 处理函数`。
- driver 由 `NPUDriver`（门面）、`NPUUtils`（探测硬件）、`NPULauncher` + `make_launcher`（按签名生成/缓存启动器）构成。
- 语言扩展以 `triton.language.extra.cann` 挂载，提供 `libdevice`（数学函数）和 `extension`（昇腾亲和算子/同步原语）两个子模块，已被真实 tutorial 使用。

## 7. 下一步学习建议

本讲只建立了「全局地图」。接下来建议：

1. 先读 [u1-l2 代码结构与「核心 vs Ascend」分层原则](u1-l2-directory-structure-and-layering.md)：搞清楚「哪些代码放 core、哪些放 `third_party/ascend`」，为后续阅读源码建立目录直觉。
2. 再读 [u1-l3 环境准备、安装与构建](u1-l3-installation-and-build.md) 和 [u1-l4 跑通第一个 kernel](u1-l4-first-kernel-vector-add.md)：动手跑通 vector-add，把本讲的「链路」在真实环境里「看到」一次。
3. 之后再进入 [u3（编译流水线总览）](u3-l1-jit-and-compile-entry.md)：本讲里一笔带过的 `make_ttir`、`ttir_to_linalg`、`add_stages`，会在那里被逐个放大精读。

> 阅读源码时，建议把本讲第 5 节画的那张「全局图」放在手边，每读一个文件就问自己一句：「它属于图上的哪一段？」——这能帮你始终不迷失在细节里。

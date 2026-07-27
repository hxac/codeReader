# 仓库目录结构与模块地图

> 本讲对应学习路线 `u1-l3`。建议先完成 `u1-l1（项目定位与整体架构）`。

## 1. 本讲目标

学完本讲，你应当能够：

1. 说出 `tilelang-ascend` 顶层目录（`src/`、`tilelang/`、`3rdparty/`、`examples/`、`docs/`、`testing/`）各自的职责。
2. 在 `src/` 下区分 `op`、`transform`、`target`、`tl_templates`、`layout`、`runtime` 六个子目录的作用，并指出哪些是 Ascend 专用代码。
3. 在 `tilelang/` 下区分 `language`、`jit`、`engine`、`transform` 四个 Python 子包的分工。
4. 用一句话串联端到端链路：**前端 DSL（`language`）→ 编译 Pass（`transform`/`engine`）→ Codegen（`target`）→ 运行时（`jit`）**，并能指出每一段落在哪个目录。

本讲只画「地图」，不深入每个文件的实现细节——那是后续讲义的任务。

## 2. 前置知识

本讲假定你已经读过 `u1-l1`，对以下概念有基本印象（不记得也没关系，本讲会用通俗方式再点一次）：

- **DSL（领域专用语言）**：tile-lang 用 Pythonic 语法（`@T.prim_func`、`T.copy`、`T.gemm_v0` 等）来写算子，这套语法本身就是一个 DSL。
- **TIR / TensorIR**：TVM 的中间表示。tile-lang 的 DSL 代码会被翻译成 TIR，再被一系列「编译 Pass」改写，最后生成 C++ 源码。
- **编译 Pass（pass）**：对中间表示做一次改写的函数，例如「把高层 tile 操作降级成底层指令」「自动插入同步」「规划片上内存地址」。多个 pass 按顺序排成一条流水线。
- **Codegen（代码生成）**：把改写好的 TIR 翻译成目标语言（这里是 Ascend C 或 PTO 指令）的 C++ 源码字符串。
- **bisheng（毕昇编译器）**：华为 CANN 提供的设备编译器，把 Codegen 生成的 C++ 源码编译成可在 NPU 上运行的 `.so`。
- **JIT（即时编译）**：第一次调用算子时才触发「 lowering → codegen → bisheng 编译 → 加载 `.so`」的全过程。

一个通俗类比：tile-lang 像一个「翻译公司」——`language` 是客户提交订单的窗口，`transform` 是改稿车间，`target` 是最终定稿排版，`jit` 是把成品交付上机的物流部门。

## 3. 本讲源码地图

| 文件 / 目录 | 角色 | 本讲用来做什么 |
| :--- | :--- | :--- |
| `CMakeLists.txt` | C++ 后端的构建脚本 | 看 `USE_ASCEND` 如何决定哪些源文件被编进 `libtilelang.so` |
| `tilelang/__init__.py` | Python 包入口 | 看它加载哪个 `.so`、导出哪些顶层 API |
| `tilelang/language/__init__.py` | 前端 DSL 的 `T.` 命名空间 | 看 `T.copy`/`T.gemm_v0`/`T.alloc_*` 都从哪来 |
| `src/op/` | 算子（Tile Library）定义层 | 看高层算子如何注册与降级 |
| `src/transform/` | 编译 Pass 仓库（约 48 个 `.cc`） | 看 Ascend 专用 pass 与通用 pass 的分布 |
| `src/target/` | Codegen 与 runtime module | 看 `target.build.tilelang_ascend` 如何注册 |
| `src/tl_templates/` | C++ 模板库 | 看生成代码 `#include` 的头文件从哪来 |
| `tilelang/engine/phase.py` | 两阶段 Pass 流水线定义 | 看 `LowerAndLegalize` / `OptimizeForTarget` 的 pass 顺序 |
| `tilelang/engine/lower.py` | 编译总驱动 | 看 lowering → codegen 的分阶段调用 |
| `tilelang/jit/__init__.py` + `kernel.py` | JIT 装饰器与 `JITKernel` | 看装饰器到可调用对象的过程 |
| `tilelang/jit/adapter/libgen.py` | bisheng 调用 | 看 Codegen 产物如何变成 `.so` |
| `docs/get_started/overview.md` | 官方编译流程图说明 | 佐证三层编程接口与六步编译流程 |

> 永久链接统一以当前 HEAD `ee60e122` 为基准。下文形如 `路径:起-止` 的链接均可在浏览器直接打开对应行。

## 4. 核心概念与源码讲解

### 4.1 仓库整体布局：三大组成 + 配套目录

#### 4.1.1 概念说明

整个仓库可以分成三大块加几个配套目录：

1. **`src/` —— C++ 编译器后端**：所有「重活」都在这里。它编译成 `libtilelang.so`，负责算子定义、编译 Pass、Codegen 和 runtime module。Ascend 专用的 C++ 代码集中在这里。
2. **`tilelang/` —— Python 前端与驱动**：用户接触到的 Python API（`import tilelang`、`@tilelang.jit`、`T.copy` 等）都在这里。它通过 TVM 的 FFI（foreign function interface）调用 `src/` 编出来的 `.so`。
3. **`3rdparty/` —— 第三方子模块**：`tvm`（编译基础设施）、`catlass`（Ascend C 的矩阵模板库，类似 GPU 的 CUTLASS）、`pto-isa`（PTO 后端的指令定义）、`shmem`（核间共享内存 put/get API）。JIT 编译时需要这些头文件。

配套目录：

- **`examples/`**：约 44 个算子示例目录（`gemm/`、`flash_attention/`、`elementwise/` 等），每个目录里是可直接 `python xxx.py` 运行的脚本，成功会打印 `Kernel Output Match!`。`examples/bench_test.sh` 会自动发现并批量运行这些脚本，是 CI 的入口。
- **`docs/`**：文档。`get_started/`（安装与概览）、`tutorials/`（各原语教程）、`deeplearning_operators/`（算子深入）、`language_ref/`、以及一份权威的 `TileLang-Ascend Programming Guide.md`。
- **`testing/`**：`testing/python/` 下是 pytest 用例（按算子/语言特性组织），`testing/cpp/` 留作 C++ 测试位。
- 其余：`benchmark/`（基准）、`scripts/`、`maint/`（维护脚本）、顶层构建脚本 `CMakeLists.txt`、`setup.py`、`build_wheel_ascend.sh`、`install_ascend.sh`、`set_env.sh`。

> 关键认知：**Ascend 相关的 C++ 代码几乎都集中在 `src/` 下**；而 `tilelang/` 这层 Python 代码是「跨后端」共享的（同一套也能驱动 CUDA/HIP/CPU），只在调用 `.so` 里的注册函数时按 target 分流。

#### 4.1.2 核心流程

仓库的物理目录如何对应到运行流程：

```text
用户写 Python 算子             tilelang/language/        （T.copy / T.gemm_v0 …）
        │  (@tilelang.jit)
        ▼
JIT 装饰器捕获 + 缓存          tilelang/jit/             （jit / JITKernel）
        │
        ▼
编译总驱动 lower()             tilelang/engine/          （lower.py / phase.py）
        │  ┌─ Pass 阶段1 LowerAndLegalize ─┐
        │  │   Pass 阶段2 OptimizeForTarget │   ← src/transform/ 里的 .cc 实现
        │  └────────────────────────────────┘
        ▼
按 target.model 分发 Codegen   src/target/               （codegen_ascend*.cc / rt_mod_ascend*.cc）
        │  生成 C++ 源码（#include src/tl_templates/）
        ▼
bisheng 编译为 .so              tilelang/jit/adapter/libgen.py
        │
        ▼
ctypes/cython 加载并调用        tilelang/jit/adapter/      （ctypes/ cython/ dlpack.py）
```

记住这张图，本讲后面每个模块都是在填它的某一段。

#### 4.1.3 源码精读

构建脚本最能体现「哪些源文件属于 Ascend」。`CMakeLists.txt` 先用 glob 收集**所有后端共享**的源文件，再用开关把各后端的源文件追加进来：

- 共享源文件分组（op / transform / layout / 通用 codegen）：
  [CMakeLists.txt:108-118](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/CMakeLists.txt#L108-L118) —— 注意这里 `src/op/*.cc`、`src/transform/*.cc`、`src/layout/*.cc` 是**无条件**收集的，说明这些 pass/op 在所有后端都被链接进库。
- Ascend 后端的四个源文件，由 `USE_ASCEND` 开关控制：
  [CMakeLists.txt:130-138](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/CMakeLists.txt#L130-L138) —— 只有 `codegen_ascend.cc`、`codegen_ascend_pto.cc`、`rt_mod_ascend.cc`、`rt_mod_ascend_pto.cc` 这 4 个是 Ascend 独占的，对应 u1-l2 讲过的「双 Codegen 路线」。

这正好印证了一条重要事实：**`src/op` 和 `src/transform` 是跨后端共享的**（其中包含大量 Ascend 专用 pass），而 `src/target` 才是「按后端分流」的真正分叉点。

Python 包入口 `tilelang/__init__.py` 展示了「先加载 `.so`，再导出 API」的模式：
- 加载编译产物 `libtilelang.so`（或 `libtilelang_module.so`）：
  [tilelang/__init__.py:72-86](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/__init__.py#L72-L86)
- 导出顶层 API（`jit`、`compile`、`language`、`transform`、`engine`、`lower`、`PassConfigKey`）：
  [tilelang/__init__.py:88-110](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/__init__.py#L88-L110)

这意味着用户 `import tilelang` 时，C++ 后端就已经就绪，后续 `@tilelang.jit` 的每一次编译都会通过 FFI 调进这个 `.so`。

#### 4.1.4 代码实践

1. **实践目标**：建立「目录 → 运行流程阶段」的直觉。
2. **操作步骤**：
   - 在仓库根目录执行 `ls -1 src/` 和 `ls -1 tilelang/`，对照本节列出的子目录。
   - 打开 `CMakeLists.txt` 第 108–138 行，确认 `src/op/*.cc`、`src/transform/*.cc` 是无条件收集，而 4 个 ascend 文件在 `if(USE_ASCEND)` 块里。
   - 执行 `ls -1 examples/ | head -20` 与 `ls -1 docs/tutorials/`，感受示例与教程的覆盖面。
3. **需要观察的现象**：`src/` 下与 `tilelang/` 下子目录的数量、命名差异；`examples/` 几乎一个算子一个目录。
4. **预期结果**：你能用一句话说出 `src/`、`tilelang/`、`3rdparty/` 分别放什么。
5. **运行结果**：待本地验证（取决于本机是否已 clone 仓库）。

#### 4.1.5 小练习与答案

- **练习 1**：为什么 `src/op/*.cc` 和 `src/transform/*.cc` 不放在 `if(USE_ASCEND)` 里，而 4 个 `*_ascend*.cc` 要放进去？
  - **答案**：`op` 和 `transform` 里有大量跨后端共享的逻辑（GPU/CPU 也用），只有 `target/` 下的 codegen/rt_mod 是「按硬件分流」的，Ascend 的 4 个文件 CUDA 构建时不需要，所以用开关隔离。
- **练习 2**：`import tilelang` 之后，C++ 后端代码在什么时机被加载？
  - **答案**：在 `tilelang/__init__.py` 模块导入时，由 `_load_tile_lang_lib()` 用 `ctypes.CDLL` 加载 `libtilelang.so`，之后所有 FFI 调用都指向它。

---

### 4.2 `tilelang/language`：前端 DSL（`T.` 命名空间）

#### 4.2.1 概念说明

用户写算子时几乎只用一个对象：`T`（来自 `from tvm.script.parser.tir import *` + tile-lang 自己的扩展）。`T.copy`、`T.gemm_v0`、`T.alloc_L1`、`T.Parallel`、`T.Pipelined`、`T.Scope("C")`……这些全是 `tilelang.language` 这个子包拼装出来的。

`tilelang/language/` 的职责就是：**把这些 Pythonic 的原语，定义成能在 `@T.prim_func` 里使用、并能翻译成 TIR 调用的形式**。它不直接生成设备代码，只负责「表达」。

> 它和上游 tile-lang（GPU 版）共享同一套 `T`，本仓库额外加了一批 Ascend 专用原语（`alloc_L1/ub/L0A/L0B/L0C`、`T.Scope`、`T.set_flag` 等）。

#### 4.2.2 核心流程

`tilelang/language/__init__.py` 是 `T.` 命名空间的「组装车间」，它从各个子模块把原语 import 进来，统一挂到 `T` 上：

```text
tilelang/language/__init__.py
   ├── kernel.py        → T.Kernel（核绑定 cid/vid）
   ├── allocate.py      → T.alloc_shared / alloc_fragment / alloc_L1 / alloc_ub / alloc_L0A/L0B/L0C
   ├── copy_op.py       → T.copy
   ├── gemm.py          → T.gemm / T.gemm_v0
   ├── customize.py     → T.mma（即 npu_gemm）、T.atomic_add
   ├── parallel.py      → T.Parallel
   ├── pipeline.py      → T.Pipelined
   ├── persistent.py    → T.Persistent
   ├── reduce*.py       → T.reduce_sum / reduce_max / reduce_min
   ├── ascend.py        → T.Scope、T.set_flag/wait_flag、T.barrier_all（Ascend 专用）
   └── ascend_tile.py   → T.tile.add / mul / exp …（显式 vector 范式）
```

这些原语最终都会变成 TIR 里的 `call_intrin` / `Call` 节点，其 `op` 名字（如 `tl.ascend_use_swizzle`）会跟 `src/op/`、`src/target/` 里的注册项一一对应——这是「前端」与「后端」对接的暗号。

#### 4.2.3 源码精读

- `T.Kernel` 与各 `alloc_*` 原语的导入位置：
  [tilelang/language/__init__.py:34-52](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/__init__.py#L34-L52) —— 注意这里同时导入了「跨后端的 `alloc_shared/alloc_fragment`」和「Ascend 专用的 `alloc_L1/alloc_ub/alloc_L0A/L0B/L0C`」，体现 Developer/Expert 两套抽象共存。
- `T.copy`、`T.gemm`、reduce、`T.mma` 等计算原语：
  [tilelang/language/__init__.py:53-77](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/__init__.py#L53-L77) —— `npu_gemm as mma` 这一行说明用户写的 `T.mma` 实际调用的是 `customize.npu_gemm`。
- Ascend 专用原语与 `T.tile.*`：
  [tilelang/language/__init__.py:83-86](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/__init__.py#L83-L86) —— `from .ascend import *`（同步原语、Scope）和 `from . import ascend_tile as tile`（显式 vector 操作）是 Ascend 独有的扩展。

如果你在 README 的 GEMM 例子里看到 `T.copy(...)`、`T.gemm_v0(...)`、`T.barrier_all()`，它们的「定义源头」就是这三个片段。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：搞清楚一个常用原语的「来路」。
2. **操作步骤**：
   - 在 `tilelang/language/__init__.py` 里搜索 `alloc_L1`、`gemm`、`mma`、`tile`，确认它们分别来自 `allocate.py`、`gemm.py`、`customize.py`、`ascend_tile.py`。
   - 打开 `tilelang/language/gemm.py`，找到 `gemm_v0` 的定义，看它最终发出的 TIR `op` 名字是什么（形如 `tl.xxx`）。
3. **需要观察的现象**：前端原语只是「发请求」，真正干活的是 `op` 名字背后在 `src/` 里注册的实现。
4. **预期结果**：你能说出「`T.gemm_v0` 这个名字 → `gemm.py` → 某个 `tl.*` intrinsic → `src/op/gemm.cc` 的实现」这条链。
5. **运行结果**：纯源码阅读，无需运行。

#### 4.2.5 小练习与答案

- **练习 1**：`T.alloc_shared` 和 `T.alloc_L1` 分别面向哪种编程抽象？它们会在同一个 kernel 里出现吗？
  - **答案**：`alloc_shared` 是 Developer 抽象（由 pass 自动推断落到 L1 还是 UB），`alloc_L1` 是 Expert 抽象（显式指定 L1）。可以混用——本仓库支持 Developer/Expert 混合编程。
- **练习 2**：用户写的 `T.mma`，实际对应哪个函数？
  - **答案**：`tilelang/language/customize.py` 里的 `npu_gemm`（`__init__.py` 中 `npu_gemm as mma`）。

---

### 4.3 `src/op`：算子（Tile Library）定义层

#### 4.3.1 概念说明

前端 `T.copy`、`T.gemm_v0` 这些原语发出的 TIR 调用，需要一个「实现」。`src/op/` 就是这些高层算子的 C++ 实现层：它定义每个算子的语义、如何**降级（Lower）**成更底层的操作、以及如何**推断布局（InferLayout）**。

可以把它理解成「Tile Library」：一组预制好的算子构件，编译器在 pass 阶段会调用它们的 `Lower` 方法，把高层算子展开成更接近硬件的指令。

#### 4.3.2 核心流程

`src/op/` 下的文件按算子类别拆分：

| 文件 | 覆盖的算子类别 |
| :--- | :--- |
| `op.cc` / `op.h` | 算子注册框架（`TIR_REGISTER_TL_OP`、`ParseOperator`） |
| `gemm.cc` | `T.gemm` / `T.gemm_v0` / `T.mma` |
| `bulk_copy.cc` | `T.copy`（各级存储搬运） |
| `reduce.cc` | `T.reduce_sum/max/min` |
| `parallel.cc` | `T.Parallel` 相关 |
| `elem.cc` | 元素级算子 |
| `math.cc` | 数学函数 |
| `ascend.cc` | Ascend 专用算子（同步、Scope 等） |
| `builtin.cc` / `logical.cc` | 内建与逻辑算子 |

当 pass 遇到一个算子调用时，会通过 `ParseOperator` 查表，找到对应的 `Operator` 对象，再调用它的 `Lower` / `InferLayout`。

#### 4.3.3 源码精读

- 算子注册框架与示例注册：
  [src/op/op.cc:16-35](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/op/op.cc#L16-L35) —— `TIR_REGISTER_TL_OP(RegionOp, region)` 是注册一个算子的典型写法；`ParseOperator` 通过 `TLOpBuilder` 属性表把 TIR `Call` 映射到 C++ 的 `Operator*`。
- 算子基类提供的可重写接口：
  [src/op/op.cc:81-93](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/op/op.cc#L81-L93) —— 每个 `Operator` 子类实现 `Lower`（降级）、`Canonialize`（规范化）、`InferLayout`（布局推断）。后续 pass 正是调这些方法来改写 IR。

这解释了为什么 `src/op/*.cc` 被无条件链接：无论目标是 Ascend 还是 GPU，都需要这一层算子定义才能展开高层 tile 操作。

#### 4.3.4 代码实践（源码阅读型）

1. **实践目标**：体会「一个前端原语 → 一个 op 文件」的对应。
2. **操作步骤**：
   - 打开 `src/op/bulk_copy.cc` 的头部，看它注册了哪些 `tl.copy_*` 类 intrinsic。
   - 打开 `src/op/gemm.cc`，找到 `gemm_v0` / `mma` 对应的 `Operator` 子类与 `Lower` 方法。
3. **需要观察的现象**：每个算子类都有 `Lower`，它返回的 `Stmt` 就是降级后的 TIR。
4. **预期结果**：你能指出「`T.copy` 的搬运语义」最终由 `bulk_copy.cc` 的 `Lower` 落实。
5. **运行结果**：纯源码阅读。

#### 4.3.5 小练习与答案

- **练习 1**：`src/op/op.cc` 里 `ParseOperator` 是怎么把一个 TIR `Call` 变成 `Operator*` 的？
  - **答案**：通过 `Op::GetAttrMap<OpBuilderFunc>("TLOpBuilder")` 查表——每个用 `TIR_REGISTER_TL_OP` 注册的算子都带一个 builder，调用它即可构造出对应的 `Operator` 对象。
- **练习 2**：为什么 `InferLayout` 这个接口对 GEMM 特别重要？
  - **答案**：GEMM 的性能强依赖片上 buffer 的数据布局（如 zn 布局）。`InferLayout` 让算子自己声明「我期望/产出什么布局」，供 `LayoutInference` pass 使用。

---

### 4.4 `src/transform`：编译 Pass 仓库

#### 4.4.1 概念说明

`src/transform/` 是仓库里**最大**的 C++ 目录（约 48 个 `.cc`），存放全部编译 Pass。Pass 的工作就是「改写 IR」：把高层、抽象、跨后端的 TIR，一步步变成底层、具体、贴近硬件的 TIR。

其中 Ascend 专用 pass 名字基本都以 `ascend_` 开头，很容易识别。

#### 4.4.2 核心流程

按职责大致分三类：

1. **Ascend 专用 pass（14 个，`ascend_*.cc`）**：
   - `ascend_infer_buffer_scope.cc` —— 自动推断 buffer scope（落到 L1/UB/L0A…）
   - `ascend_vid_reduction.cc` —— vid 消除（`threads=2` 编程模型）
   - `ascend_lower_parallel_to_vector.cc` —— `T.Parallel` → vector 指令
   - `ascend_workspace_reduction.cc` —— workspace 消除（跨核拷贝两阶段 GM）
   - `ascend_tail_mask_propagation.cc` —— UB tail 有效区域改写
   - `ascend_combinecv.cc` —— Cube/Vector scope 自动分离
   - `ascend_sync_insert.cc` / `ascend_sync_insert_vs.cc` —— 自动同步插入
   - `ascend_memory_planning.cc` / `ascend_storage_rewrite.cc` —— 缓冲复用与地址分配
   - ……
2. **tile-lang 通用 pass**：`lower_tile_op.cc`（高层 tile op 降级）、`layout_inference.cc`（布局推断）、`inject_pipeline.cc` + `pipeline_planning.cc`（软件流水）、`cross_core_pipeline.cc`（跨核流水）、`legalize_vectorized_loop.cc`、`legalize_safe_memory_access.cc` 等。
3. **TVM/通用辅助 pass**：`flatten_buffer.cc`、`simplify.cc`、`config_index_bitwidth.cc` 等。

这些 pass **不会**在用户代码里被直接调用——它们由 `tilelang/engine/phase.py` 排好顺序，串成两条流水线（见 4.6）。

#### 4.4.3 源码精读

Pass 在 Python 侧通过 FFI 暴露。`tilelang/transform/__init__.py` 是这些 C++ pass 的 Python 包装层，每个函数体内基本都是一行 `_ffi_api.Xxx()`：

- 典型包装（含 host 处理与 pass context 获取）：
  [tilelang/transform/__init__.py:14-27](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/transform/__init__.py#L14-L27)
- Ascend 专用 pass 的 Python 包装示例（`AscendSyncInsert`、`CombineCV`、`AscendVidReduction`）：
  [tilelang/transform/__init__.py:371-450](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/transform/__init__.py#L371-L450)

也就是说：**`src/transform/*.cc`（C++ 实现）↔ `tilelang/transform/__init__.py`（Python 包装）↔ `tilelang/engine/phase.py`（编排调用）** 是一条清晰的链。

另外，pass 的开关配置统一收敛在 `PassConfigKey` 枚举里：
[tilelang/transform/pass_config.py:10-60](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/transform/pass_config.py#L10-L60) —— 你在 README 里见过的 `TL_ASCEND_AUTO_SYNC`、`TL_ASCEND_MEMORY_PLANNING`、`TL_ASCEND_AUTO_CV_COMBINE` 等开关，全部定义在这里。

#### 4.4.4 代码实践（源码阅读型）

1. **实践目标**：确认「Ascend pass 名字 ↔ 开关 ↔ 编排」三者对应。
2. **操作步骤**：
   - 在 `tilelang/transform/pass_config.py` 里找到 `TL_ASCEND_AUTO_SYNC`，记下它的字符串值。
   - 在 `tilelang/transform/__init__.py` 里找到 `AscendSyncInsert` 包装，确认它调用 `_ffi_api.AscendSyncInsert`。
   - 在 `src/transform/` 里确认存在 `ascend_sync_insert.cc`。
3. **需要观察的现象**：同一个机制在三个文件里各出现一次（配置、包装、实现）。
4. **预期结果**：你能口述「用户开 `TL_ASCEND_AUTO_SYNC=True` → pass context → `phase.py` 调用 `AscendSyncInsert` → FFI → `ascend_sync_insert.cc`」。
5. **运行结果**：纯源码阅读。

#### 4.4.5 小练习与答案

- **练习 1**：`src/transform/` 里的 pass，会被 GPU 后端用到吗？
  - **答案**：通用 pass（如 `lower_tile_op`、`layout_inference`、`flatten_buffer`）会；名字带 `ascend_` 的不会——它们对非 Ascend target 通常是无操作或不会进入对应分支。
- **练习 2**：用户怎么知道有哪些 pass 开关可用？
  - **答案**：查 `tilelang/transform/pass_config.py` 里的 `PassConfigKey` 枚举，每个键都带文档字符串说明默认值。

---

### 4.5 `src/target`：Codegen 与 runtime module

#### 4.5.1 概念说明

改写好的 TIR 最终要变成「能在 NPU 上跑的 C++ 源码」，这一步由 `src/target/` 完成。它分成两类文件：

- **`codegen_*.cc/.h`**：代码生成器。遍历 TIR，把每个 intrinsic 翻译成 AscendC（或 PTO）的 C++ 调用，输出一个完整的 `.cpp` 字符串。
- **`rt_mod_*.cc`**：runtime module 构建器。把 codegen 输出的源码包成一个 TVM runtime module，并注册一个全局函数名（如 `target.build.tilelang_ascend`），供 Python 侧按 target 名调用。

Ascend 有两条 codegen 路线，各有一对 codegen + rt_mod 文件：

| 路线 | codegen | rt_mod | 注册的全局函数 |
| :--- | :--- | :--- | :--- |
| Ascend C（主线） | `codegen_ascend.cc/.h` | `rt_mod_ascend.cc` | `target.build.tilelang_ascend` |
| PTO（支持 A5 仿真） | `codegen_ascend_pto.cc/.h` | `rt_mod_ascend_pto.cc` | `target.build.tilelang_ascend_pto` |

#### 4.5.2 核心流程

1. Python 侧 `device_codegen` 根据 `target.model` 选择调用哪个注册函数（见 4.6.3）。
2. 该注册函数（如 `BuildTileLangAscend`）创建对应的 `CodeGenTileLangAscend` 对象，对每个 `PrimFunc` 调 `AddFunction`，遍历 TIR 生成 C++。
3. 生成的 C++ 里会 `#include` 来自 `src/tl_templates/` 的模板库头文件（`ascend/common.h`、`pto/common.h` 等），把 `T.copy` 之类映射成 `DataCopy`、把 `T.mma` 映射成模板 MMA 调用。
4. 用 `CSourceModuleCreate` 把源码包成 runtime module 返回。

> `src/tl_templates/` 就是「生成代码要 include 的头文件库」。`ascend/` 给 AscendC 路线用，`pto/` 给 PTO 路线用，`cuda/`、`hip/`、`cpp/`、`cpu/` 服务其他后端。这正是 u1-l2 讲过的「wheel 要打包 catlass/shmem/pto-isa 头文件」的原因——生成代码在 JIT 阶段需要它们。

#### 4.5.3 源码精读

- Codegen 类声明（AscendC 路线），可见它override了大量 `Visit*` 方法来翻译各类 intrinsic：
  [src/target/codegen_ascend.h:27-66](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend.h#L27-L66) —— 例如 `CopyCodegen`、`GemmOpCodegen`、`MmaCodegen`、`FlagOpCodegen` 等私有方法，分别对应 `T.copy`、`T.gemm`、`T.mma`、`T.set_flag` 的翻译。
- runtime module 构建与全局函数注册：
  [src/target/rt_mod_ascend.cc:9-32](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/rt_mod_ascend.cc#L9-L32) —— `BuildTileLangAscend` 创建 `CodeGenTileLangAscend`，逐个 `AddFunction`，最后 `CSourceModuleCreate(code, "c", ...)`；末尾 `TVM_REGISTER_GLOBAL("target.build.tilelang_ascend")` 把它挂到全局注册表，这便是 Python 侧 `tvm._ffi.get_global_func("target.build.tilelang_ascend")` 能找到它的原因。
- PTO 路线的注册与之对称：
  [src/target/rt_mod_ascend_pto.cc:31](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/rt_mod_ascend_pto.cc#L31) —— 注册名是 `target.build.tilelang_ascend_pto`。

#### 4.5.4 代码实践（源码阅读型）

1. **实践目标**：理解「target.model → 注册函数 → Codegen 类」的对接。
2. **操作步骤**：
   - 在 `src/target/codegen_ascend.h` 里搜索 `CopyCodegen`、`MmaCodegen`，感受 codegen 如何为每种 intrinsic 准备一个翻译方法。
   - 在 `src/target/rt_mod_ascend.cc` 里确认注册名 `target.build.tilelang_ascend`。
   - 对照 `src/tl_templates/ascend/common.h`（可在仓库打开），理解生成代码 include 它的原因。
3. **需要观察的现象**：Codegen 类的方法名与前端原语名（copy/mma/gemm/flag）高度对应。
4. **预期结果**：你能解释「`T.copy` 为什么最终会变成一段 `DataCopy(...)` 的 C++」。
5. **运行结果**：纯源码阅读。

#### 4.5.5 小练习与答案

- **练习 1**：为什么 Ascend 有两套 codegen，而 CUDA 只有一套？
  - **答案**：Ascend 有 Ascend C 与 PTO 两条技术路线（目标不同，PTO 还支持 A5 仿真），所以各有一对 codegen+rt_mod；CUDA 只有一条路线，故只有一套。
- **练习 2**：`target.build.tilelang_ascend` 这个名字在哪里被「使用」？
  - **答案**：在 `tilelang/engine/lower.py` 的 `device_codegen` 里，通过 `tvm._ffi.get_global_func("target.build.tilelang_ascend")` 取出并调用（详见 4.6.3）。

---

### 4.6 `tilelang/engine`：编译驱动与两阶段 Pass 流水线

#### 4.6.1 概念说明

`tilelang/engine/` 是把前面所有零件「串起来」的总驱动。它有两个关键文件：

- `lower.py`：暴露 `tilelang.lower()`，把一个 `PrimFunc` 编译成 `CompiledArtifact`。
- `phase.py`：定义两条 pass 流水线 `LowerAndLegalize`（阶段一：合法化）和 `OptimizeForTarget`（阶段二：针对目标优化），并把 `src/transform/` 里的 pass 排好顺序。

`engine` 自己不实现 pass，它只是「指挥」`transform` 干活，再把结果交给 `target` 做 codegen。

#### 4.6.2 核心流程

`lower()` 的三步：

```text
PrimFunc
   │  Phase 1: LowerAndLegalize(mod, target)      ← phase.py
   │     AscendInferBufferScope → VidReduction → LowerParallelToVector
   │     → LayoutInference → LowerTileOp → TailMask → WorkspaceReduction
   │     → LegalizeVectorizedLoop → LegalizeSafeMemoryAccess …
   ▼
   │  Phase 2: OptimizeForTarget(mod, target, platform)   ← phase.py
   │     CrossCorePipeline → CombineCV → PipelinePlanning → InjectSoftwarePipeline
   │     → FlattenBuffer → VectorizeLoop → AscendStorageRewrite
   │     → AscendMemoryPlanning → AscendSyncInsert(VS) …
   ▼
   │  device_codegen(mod, target, platform)        ← lower.py
   │     按 target.model 分发到 target.build.tilelang_ascend / _pto
   ▼
CompiledArtifact（含生成的 C++ 源码）
```

阶段一偏「语义合法化」（让 IR 正确、补齐 scope/tail），阶段二偏「性能与地址」（流水、缓冲复用、同步、地址分配）。

#### 4.6.3 源码精读

- 阶段一的全部 pass 顺序：
  [tilelang/engine/phase.py:49-90](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/engine/phase.py#L49-L90) —— 注意每个 `tilelang.transform.Xxx()(mod)` 都对应 `src/transform/` 里的一个 `.cc`。
- 阶段二的全部 pass 顺序：
  [tilelang/engine/phase.py:93-121](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/engine/phase.py#L93-L121)
- `lower()` 如何依次调用两个阶段 + codegen：
  [tilelang/engine/lower.py:226-237](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/engine/lower.py#L226-L237)
- `device_codegen` 按 `target.model` 分发到 `target.build.tilelang_ascend` 或 `_ascend_pto`：
  [tilelang/engine/lower.py:159-170](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/engine/lower.py#L159-L170) —— `ascendc`/`auto` 走 Ascend C 路线，`pto` 走 PTO 路线，其余报错。

#### 4.6.4 代码实践（源码阅读型）

1. **实践目标**：验证「两个阶段 + codegen」在源码里的顺序。
2. **操作步骤**：
   - 打开 `phase.py`，数一下 `LowerAndLegalize` 和 `OptimizeForTarget` 各调了多少个 pass。
   - 在 `lower.py` 第 226–237 行确认调用次序是「Phase1 → Phase2 → device_codegen」。
3. **需要观察的现象**：`AscendSyncInsert` 在阶段二末尾（`OptimizeForTarget`），而 `LowerTileOp` 在阶段一——这能解释「为什么改 tile op 不会影响同步插入顺序」。
4. **预期结果**：你能说出任意一个 Ascend pass 属于哪个阶段。
5. **运行结果**：纯源码阅读。

#### 4.6.5 小练习与答案

- **练习 1**：`AscendVidReduction` 和 `AscendMemoryPlanning` 分别在哪个阶段？为什么？
  - **答案**：`AscendVidReduction` 在阶段一（`LowerAndLegalize`，因为它要改 UB 形状与循环范围，必须尽早）；`AscendMemoryPlanning` 在阶段二末尾（`OptimizeForTarget`，因为缓冲复用要在所有形状/地址变化稳定后再做）。
- **练习 2**：如果 `target.model` 既不是 `ascendc`/`auto` 也不是 `pto`，会怎样？
  - **答案**：`device_codegen` 会 `raise ValueError`，提示 target 不支持（见 `lower.py:166-168`）。

---

### 4.7 `tilelang/jit`：JIT 装饰器、`JITKernel` 与 bisheng 运行时

#### 4.7.1 概念说明

`tilelang/jit/` 把编译产物变成「可直接调用的 Python 函数」。它面向用户的核心 API 是 `@tilelang.jit` 装饰器，背后由三个部分支撑：

- `__init__.py`：定义 `jit` 装饰器与 `compile()` 函数，负责缓存。
- `kernel.py`：`JITKernel` 类，封装「编译产物 + 调用适配器」，对外的可调用对象。
- `adapter/`：与设备 `.so` 交互的适配层。其中 `libgen.py` 负责调用 **bisheng** 把 codegen 产物编成 `.so`，`ctypes/`、`cython/`、`dlpack.py` 负责不同方式的张量传递与调用。

#### 4.7.2 核心流程

```text
@tilelang.jit 装饰 my_op
   │  首次 my_op(args) 触发
   ▼
wrapper 调 compile() → cached()        （tilelang/jit/__init__.py）
   │
   ▼
JITKernel._compile_and_create_adapter
   │  在 tvm PassContext 里调 tilelang.lower()   （engine）
   ▼
得到 CompiledArtifact（含生成的 C++ 源码）
   │
   ▼
LibraryGenerator.compile_lib()          （tilelang/jit/adapter/libgen.py）
   │  调 bisheng，-I 指向 catlass/shmem/pto-isa + CANN 头文件
   ▼
产出 kernel.so，ctypes 加载
   │
   ▼
JITKernel.__call__ → adapter → host call() 启动 kernel
```

`@tilelang.jit` 还内置按参数元组的缓存（`_kernel_cache`），同一组参数只编译一次。

#### 4.7.3 源码精读

- `compile()` 函数：处理 `pass_configs` 与 `compile_flags` 后交给 `cached()`：
  [tilelang/jit/__init__.py:32-103](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/__init__.py#L32-L103)
- `JITKernel` 类定义与关键属性：
  [tilelang/jit/kernel.py:22-45](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/kernel.py#L22-L45) —— 它持有 `artifact`（编译产物）、`adapter`（调用适配器）、`torch_function`（最终可调用对象）。
- 编译并创建 adapter（在 `PassContext` 内调用 `tilelang.lower`）：
  [tilelang/jit/kernel.py:203-228](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/kernel.py#L203-L228)
- 调用入口与获取生成源码：
  - `__call__`：[tilelang/jit/kernel.py:184-201](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/kernel.py#L184-L201)
  - `get_kernel_source`：[tilelang/jit/kernel.py:378-389](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/kernel.py#L378-L389) —— 这是后续讲义「打印生成代码」实践的关键 API。
- bisheng 编译命令的组装（AscendC 路线，`-xasc`，include 指向 catlass/shmem/CANN）：
  [tilelang/jit/adapter/libgen.py:142-183](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/libgen.py#L142-L183) —— 这里 `-I{TL_ROOT}/3rdparty/catlass/include`、`-I{TL_ROOT}/3rdparty/shmem/include` 正是 u1-l2 强调的「JIT 需要这些头文件」的落地证据；PTO 路线则用 `-xcce` 与 `pto-isa`（见同文件第 184–228 行）。

#### 4.7.4 代码实践（可运行，依赖 NPU 环境）

1. **实践目标**：亲眼看到「生成的 C++ 源码」，建立 codegen 的直觉。
2. **操作步骤**：
   - 进入 `examples/gemm/`，运行 `python example_gemm.py`，确认打印 `Kernel Output Match!`。
   - 在脚本里拿到编译后的对象，调用 `func.get_kernel_source()` 打印生成的 Ascend C 代码（若脚本已用 `@tilelang.jit`，则 `func` 即为返回的 `JITKernel`）。
   - 在打印出的 C++ 里搜索 `DataCopy`、`MMA` 之类模板调用，并确认文件顶部 `#include` 了 `catlass`/`shmem` 的头文件。
3. **需要观察的现象**：生成代码里能看到 4.5 讲的 codegen 翻译结果，以及 `PrintHostFunc` 产出的 host 侧 `call` 符号。
4. **预期结果**：生成代码是一段可读的 C++，且 include 路径与本节 `libgen.py` 的 `-I` 参数一致。
5. **运行结果**：待本地验证（需要已配置好 CANN 与 NPU/sim 环境；无设备时可改为阅读 `libgen.py` 的命令拼接）。

> 无 NPU 时，可把本实践降级为「源码阅读型」：打开 `libgen.py` 第 142–228 行，对照 AscendC（`-xasc`）与 PTO（`-xcce`）两条命令的差异。

#### 4.7.5 小练习与答案

- **练习 1**：`@tilelang.jit` 装饰的函数，第二次用相同参数调用时还会重新编译吗？
  - **答案**：不会。`_JitImplementation` 用 `(args, sorted(kwargs))` 作 key 缓存了 `JITKernel`（见 `__init__.py` 的 `wrapper`），命中即直接返回。
- **练习 2**：为什么 `libgen.py` 一定要传 `-I{TL_ROOT}/3rdparty/...`？
  - **答案**：因为 codegen 生成的 C++ 会 `#include` catlass/shmem（AscendC）或 pto-isa（PTO）的头文件，bisheng 编译时必须能找到它们。

---

## 5. 综合实践：绘制「前端 → Pass → Codegen → 运行时」模块依赖图

这是本讲的主实践任务，用来把全部模块串成一张图。

**实践目标**：用一张图表达「一段用户代码从写入到上机，依次经过哪些目录的哪些文件」。

**操作步骤**：

1. 选一个最小例子——README 的 Quick Start GEMM（`@tilelang.jit` + `T.Kernel` + `T.copy` + `T.gemm_v0`）。
2. 按下表，在每个阶段填入「涉及的目录 / 代表文件 / 一句职责」，全部要求引用真实路径：

   | 阶段 | 目录 | 代表文件（本讲已验证） | 职责（一句话） |
   | :--- | :--- | :--- | :--- |
   | ① 表达 | `tilelang/language` | `__init__.py`、`gemm.py`、`copy_op.py` | 提供 `T.copy/T.gemm_v0` 原语 |
   | ② 捕获/缓存 | `tilelang/jit` | `__init__.py`、`kernel.py` | `@tilelang.jit` → `JITKernel` |
   | ③ 编译驱动 | `tilelang/engine` | `lower.py`、`phase.py` | `lower()` 串两阶段 pass |
   | ④ Pass 改写 | `src/transform`（+ Python 包装 `tilelang/transform`） | `lower_tile_op.cc`、`ascend_*.cc` | 高层 op 降级、同步、地址 |
   | ⑤ 算子实现 | `src/op` | `gemm.cc`、`bulk_copy.cc` | 提供 `Lower`/`InferLayout` |
   | ⑥ Codegen | `src/target` | `codegen_ascend.cc`、`rt_mod_ascend.cc` | TIR → C++ 源码 |
   | ⑦ 模板库 | `src/tl_templates` | `ascend/common.h` | 生成代码 include 的头文件 |
   | ⑧ 设备编译/加载 | `tilelang/jit/adapter` | `libgen.py` | bisheng 编 `.so`、ctypes 加载 |

3. 在图上用高亮标注 **Ascend 专用** 落点：`src/transform/ascend_*.cc`、`src/target/{codegen,rt_mod}_ascend*.cc`、`src/tl_templates/{ascend,pto}/`、`src/op/ascend.cc`。

**需要观察的现象**：同一份用户代码，会依次「穿过」`tilelang/`（Python）和 `src/`（C++）两个世界，中间靠 FFI 与 `target.build.*` 注册函数衔接。

**预期结果**：一张能解释「为什么改一个 pass 要去 `src/transform/`、为什么生成代码里能 include 模板、为什么第二次调用更快」的端到端依赖图。

**运行结果**：本实践为绘图 + 源码对照，无需运行设备代码；若想验证某个环节，可结合 4.7.4 的 `get_kernel_source()` 实践。

## 6. 本讲小结

- 仓库分三大块：`src/`（C++ 后端）、`tilelang/`（Python 前端与驱动）、`3rdparty/`（TVM/catlass/pto-isa/shmem 子模块），外加 `examples/`、`docs/`、`testing/` 配套目录。
- Ascend 专用 C++ 代码集中在 `src/`：其中 `src/op` 是算子定义层、`src/transform` 是 Pass 仓库（14 个 `ascend_*` pass）、`src/target` 是 Codegen 与 runtime module（双路线各一对文件）、`src/tl_templates` 是生成代码 include 的模板库。
- Python 侧 `tilelang/language` 提供 `T.` 命名空间，`tilelang/transform` 给 C++ pass 做 FFI 包装，`tilelang/engine` 编排两阶段 pass 流水线，`tilelang/jit` 负责装饰器、`JITKernel` 与 bisheng 编译加载。
- 端到端链路是：**前端 `language` → `jit` 捕获 → `engine.lower` → `transform` 改写 → `target` codegen → `jit/adapter` bisheng 编译加载**；`op` 在 pass 改写阶段被调用，`tl_templates` 在 codegen 阶段被 include。
- `CMakeLists.txt` 的源文件分组印证了一个关键事实：`src/op`、`src/transform` 跨后端共享，只有 `src/target` 下 4 个 ascend 文件由 `USE_ASCEND` 开关隔离。
- 后续每一篇讲义，本质都是在放大本讲地图里的某一块。

## 7. 下一步学习建议

- 想看「第一次运行」的完整体验：进入 `u1-l4（第一个算子：运行并读懂 GEMM）`，亲手跑 `examples/gemm/example_gemm.py`。
- 想搞懂 JIT 全链路细节：进入 `u1-l5（JIT 即时编译与运行总流程）`，它会放大本讲的 4.6 + 4.7 两节。
- 想深入 Pass 体系：等学到 `u6-l1（编译 Pass 全景与配置）` 时，再回头看本讲的 `phase.py` 两阶段顺序，会有更深的理解。
- 建议随手保留本讲的「端到端依赖图」，后续每学一个新机制（CV 分离、流水、workspace 消除……），都把它标注到图上对应阶段，作为长期学习地图。

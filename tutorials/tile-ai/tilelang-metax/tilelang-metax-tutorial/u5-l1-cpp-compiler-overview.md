# C++ 编译器核心总览

## 1. 本讲目标

在前几讲里，我们一直在 Python 侧（`tilelang/`）看 TileLang：写 `@T.prim_func`、配 `T.Kernel`、调 `tilelang.compile`。但真正把那份「计算规格」编译成可运行设备代码的，是仓库里的 C++ 编译核心 `src/`，它最终被编译成动态库 `libtilelang.so`，经 `ctypes` 被 Python 前端驱动。

本讲带你从「鸟瞰」视角理解 `src/`：

- 把握 `src/` 的整体目录架构，看清「公共层 + 后端自有层」的双层布局。
- 理解 `TileOperatorNode` 这个所有 tile 算子的基类，掌握它的两个核心虚方法 `Lower()` 与 `InferLayout()`。
- 了解 `op/builtin.cc` 如何用注册宏定义内置算子与编译开关。
- 读懂 `src/ir.cc` 为 TVM 前端扩展的「线程绑定帧」机制，理解 `T.Kernel` 在 C++ 侧长什么样。

学完后，你应当能在 `src/` 里迅速定位「新增一个算子」「新增一个后端」「写一个 pass」分别该改哪里，并为后续 `u5-l2`（transform 体系）、`u5-l3`（CUDA/HIP codegen）、`u7`（MACA 后端）打好地基。

## 2. 前置知识

本讲默认你已学完 **u4-l1（lowering 流程）**，知道 `tilelang.engine.lower` 会按 `target.kind.name` 取一条 pass 流水线，把 IR 降级后拆成 host/device 两段。本讲要回答的，就是这条流水线「在 C++ 里到底由哪些部件组成」。

在进入源码前，先明确几个贯穿全讲的术语：

- **TIR**：基于 TVM 的中间表示（Tensor IR），TileLang 用一套自有的 `tirx` 命名空间覆盖了 TVM 原版，额外支持 eager JIT 与专属属性。C++ 编译器的一切工作，本质都是在「读、改、写 TIR」。
- **tile 算子（TileOperator）**：TileLang DSL 里的高级原语，如 `T.copy`、`T.gemm`、`T.fill`。它们在前端是一条 `tl.tileop.xxx` 的 TIR 调用，在 C++ 里被解析成一个 `TileOperatorNode` 子类对象。
- **codegen（代码生成）**：把降级后的 TIR 印刷成某种后端源码（CUDA/HIP/MACA/C/Metal）的过程。
- **pass（变换）**：对 TIR 做一次机械改写的函数，多个 pass 串成流水线。
- **target（目标）**：回答「kernel 编给谁」，由 kind（cuda/hip/maca…）与属性（arch、warp_size…）组成。详见 u3-l1。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `src/ir.cc` | 为 TVM script 前端扩展的「帧」构造器：`T.Kernel`/`T.Parallel`/`T.Pipelined`/`T.Persistent` 在 C++ 侧的实现，产出 target 中立的线程绑定循环。 |
| `src/op/operator.h` | tile 算子基类 `TileOperatorNode`、`LowerArgs`/`LayoutInferArgs` 参数结构、注册宏 `TIR_REGISTER_TL_TILE_OP`。 |
| `src/op/operator.cc` | `ParseOperator`——把 TIR 调用解析成 `TileOperator` 对象的分派入口。 |
| `src/op/builtin.cc` | 用 `TIR_DEFINE_TL_BUILTIN` 注册 TileLang 专属内置算子（`access_ptr`、`__exp` 等），并注册一批 pass 配置开关。 |
| `src/op/gemm.cc` | GEMM 算子（`TileOperatorNode` 的一个具体子类）的 `Lower`/`InferLayout` 实现，可作为「基类如何被继承」的范例。 |
| `src/transform/lower_tile_op.cc` | `LowerTileOp` pass：调用 `ParseOperator` + `tile_op->Lower(...)`，是「算子降级」的总枢纽。 |
| `src/transform/materialize_kernel_launch.cc` | `MaterializeKernelLaunch` pass：把 `ir.cc` 产出的线程绑定帧物化成后端具体形式（GPU 的 `thread_extent` 或 CPU 的串行循环）。 |
| `src/maca/runtime/maca_target_kind.cc` | MACA target 的 C++ 注册，含 `thread_warp_size=64` 等属性，用于对比后端差异。 |

## 4. 核心概念与源码讲解

### 4.1 src/ 整体架构：公共层 + 后端自有层

#### 4.1.1 概念说明

`src/` 是 TileLang 编译器的「引擎舱」。理解它的关键不是记住每个文件，而是看清它的**双层组织原则**：

1. **公共层**：与具体 GPU 后端无关的代码。包括 IR 定义、公共算子基类、布局（layout）理论、与后端无关的 transform pass。
2. **后端自有层**：每个硬件后端（cuda/rocm/maca/metal/cpu/webgpu）各自一套实现，彼此结构对称。

这种「一份公共逻辑 + N 份后端实现」的切分，正是 TileLang 能用同一份 kernel 源码编译到多后端的根因——u4-l1 讲的「按 `target.kind.name` 取 pass 流水线」就是在这一层做分派。

#### 4.1.2 核心流程

顶层 `src/` 由若干目录组成，各自承担编译流水线的一段：

```
src/
├── ir.cc            # 前端扩展：T.Kernel 等「帧」的 C++ 构造器
├── config.h         # pass 配置开关的读取工具
├── op/              # 公共 tile 算子层（基类 + 各算子的 target 无关逻辑）
├── transform/       # 公共 pass（lowering、布局推断、流水线、存储优化…）
├── layout/          # 布局理论（Layout/Fragment/swizzle），详见 u4-l3
├── tl_templates/    # 各后端的「模板下译」头文件（mma/wgmma/reduce 等）
├── backend/common/  # 跨后端的公共 codegen 与 target 工具
├── cuda/ rocm/ maca/ metal/ cpu/ webgpu/   # 各后端自有层
├── runtime/         # 运行期辅助（错误处理、日志）
└── support/         # 小工具（ICHECK 等检查宏）
```

每个**后端自有层**目录又是一个对称的「四件套」（细节因后端略有差异）：

- `codegen/`：把 TIR 印成该后端源码（如 `codegen_cuda.cc`、`codegen_maca.cc`），并含 `intrin_rule_*.cc`（内置函数降低规则）与 `rt_mod_*.cc`（运行时模块）。
- `op/`：该后端对公共算子的具体实现（如 `cuda/op/gemm.cc` 与 `maca/op/gemm.cc` 各自实现 GEMM 指令选择）。
- `target_utils.cc`：该后端的 target 属性探测与能力检测（如是否支持异步拷贝）。
- `transform/`（仅 GPU 后端）：该后端专属的 pass（如 cuda 的 Hopper/Blackwell intrin lowering，maca 的 `lower_maca_intrin`）。

> 注意：包名（目录名）与 target kind **未必一致**。例如 ROCm 后端的 target kind 是 `hip`（详见 u3-l1），但目录叫 `rocm`。

统一的「能力分发」收口在 `src/backend/common/target_utils.cc`，它用一个函数按 target kind 分派到各后端的具体实现，避免调用方写满 `if/else`：

```cpp
bool TargetHasAsyncCopy(Target target) {
  if (TargetIsCuda(target)) { return TargetCudaHasAsyncCopy(target); }
  if (TargetIsRocm(target)) { return TargetRocmHasAsyncCopy(target); }
  if (TargetIsMaca(target)) { return TargetMacaHasAsyncCopy(target); }
  return false;
}
```

新增后端时，这里往往要加一个分支——这正是 u9-l1「新增后端」要落笔的地方。

#### 4.1.3 源码精读

跨后端能力分派函数 `TargetHasAsyncCopy`：[src/backend/common/target_utils.cc:L15-L26](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/backend/common/target_utils.cc#L15-L26) —— 它按 `TargetIsCuda/Rocm/Maca` 逐个判断，把「是否支持异步拷贝」这个后端相关的问题转发给各自实现。metax 分支在此加入了 MACA 分支。

MACA target 的 C++ 注册（对比后端差异的典型样本）：[src/maca/runtime/maca_target_kind.cc:L59-L70](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/runtime/maca_target_kind.cc#L59-L70) —— `TVM_REGISTER_TARGET_KIND("maca", ...)` 注册 target kind，`thread_warp_size` 默认值为 **64**（CUDA 为 32），这是 metax 分支最显眼的差异之一。

MACA target 的 triple 与 mcpu 默认值：[src/maca/runtime/maca_target_kind.cc:L40-L53](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/runtime/maca_target_kind.cc#L40-L53) —— canonicalizer 把 `mtriple` 补成 `mxc-metax-macahca`，把 `mcpu` 默认成 `xcore1000`。

#### 4.1.4 代码实践

**实践目标**：亲手为 `src/` 各顶层目录写一句话职责，建立「空间地图」。

**操作步骤**：

1. 在仓库根目录列出后端目录，观察对称结构：
   ```bash
   ls src/cuda src/rocm src/maca src/cpu src/metal src/webgpu
   ```
2. 对比每个后端是否都含 `codegen/`、`op/`、`target_utils.cc` 三件套，找出哪些后端**没有** `transform/`（提示：看 cpu/metal/webgpu）。
3. 列出 `src/tl_templates/` 下各后端目录，观察 MACA 是否有与 CUDA 对应的 `mma.h`、`gemm.h`。

**需要观察的现象**：cuda/rocm/maca 三家后端目录结构高度对称（都含 transform），cpu/metal/webgpu 较精简（无 transform）；`tl_templates/maca/` 与 `tl_templates/cuda/` 几乎一一对应。

**预期结果**：你会得到一张「公共层贯通、后端层对称」的目录表。本讲 4.1.2 的目录树可直接作为答案模板。

#### 4.1.5 小练习与答案

**练习 1**：TileLang 为什么要把 `op/`（公共）与 `cuda/op/`、`maca/op/`（后端）分开？

> **参考答案**：公共 `op/` 定义算子的基类、解析逻辑与 target 无关的共性（如 warp 划分策略的抽象）；后端 `op/` 实现该算子在本硬件上的指令选择（cuda→wgmma、maca→mfma）。分开后，新增算子只需在公共层加基类与解析、在后端层加实现，互不污染。

**练习 2**：包名 `rocm` 与 target kind `hip` 为什么不同？

> **参考答案**：`hip` 是 TVM/TileLang 体系内的 target kind 名称（HIP 是 ROCm 的 C++ 运行时接口），而 `rocm` 是这套 AMD GPU 工具链的生态名。目录沿用生态名 `rocm`，target 沿用接口名 `hip`，二者指向同一后端。

---

### 4.2 TileOperatorNode：所有 tile 算子的基类

#### 4.2.1 概念说明

你在 DSL 里写的 `T.copy`、`T.gemm`、`T.fill`、`T.alloc_fragment`，在前端被重写成 TIR 里一条 `tl.tileop.xxx` 的调用（`Evaluate(Call)`）。到了 C++ 侧，编译器需要一个**面向对象**的方式来统一处理它们：解析参数、推断布局、降级成底层 TIR。这个统一抽象就是基类 `TileOperatorNode`。

`TileOperatorNode` 定义了每个 tile 算子必须实现的契约。其中最关键的是两个纯虚方法：

- `Lower(...)`：把这条高级算子降级（lower）成具体硬件指令或更底层的 TIR 语句。
- `InferLayout(...)`：推断这条算子涉及的 buffer（尤其是 fragment）应该采用怎样的寄存器布局。

这两个方法的名字你已经在 u4-l2、u4-l3 听过——`LowerTileOp` pass 会调 `Lower`，`LayoutInference` pass 会调 `InferLayout`。本讲就是把这两个方法在 C++ 里的「根」交代清楚。

#### 4.2.2 核心流程

一个 tile 算子从「DSL 调用」到「被降级」的生命周期：

```
DSL: T.gemm(A, B, C)
   │  （前端重写）
   ▼
TIR: Evaluate(Call(op = tl.tileop.gemm, args=[...], annotations={...}))
   │  （LowerTileOp pass 内：ParseOperator）
   ▼
TileOperator 对象（TileOperatorNode 子类，如 GemmNode）
   │  （tile_op->Lower(lower_args, analyzer)）
   ▼
底层 TIR Stmt（含 mma/wgmma/mfma 指令调用）
   │  （后续 codegen）
   ▼
设备源码（.cu/.hip/.cpp…）
```

`ParseOperator` 是分派入口：它查一张名为 `TLOpBuilder` 的属性表，找到 `tl.tileop.gemm` 对应的「构造器」，用调用的 `args` 与 `annotations` 实例化出 `GemmNode`。这张表由注册宏 `TIR_REGISTER_TL_TILE_OP` 在程序启动时填充。

`Lower` 接收一个 `LowerArgs` 结构，里面装着降级所需的全部上下文：target、线程范围、布局表、buffer 重映射、以及一组**回调**（如 `add_workspace` 申领动态 shared 内存、`alloc_mbarrier` 申领同步屏障）。这些回调让算子能向「宿主 pass」请求资源，而不必自己持有全局状态。

#### 4.2.3 源码精读

基类定义与两个核心虚方法：[src/op/operator.h:L148-L156](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/operator.h#L148-L156) —— `TileOperatorNode` 声明了 `Lower`、`InferLayout`（均为纯虚 `= 0`）、`Clone`，外加一个带默认实现的 `GetAccessRegions`。本讲的实践任务正是要求你指出这两个核心虚方法。

`LowerArgs` 上下文结构：[src/op/operator.h:L95-L129](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/operator.h#L95-L129) —— 它携带 target、thread_bounds、layout_map、buffer_remap，以及四个回调（`add_workspace`、`alloc_mbarrier`、`update_barrier_arrive`、`require_smem_alignment`），是算子与宿主 pass 之间的「资源协商通道」。

`ParseOperator` 分派入口：[src/op/operator.cc:L34-L43](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/operator.cc#L34-L43) —— 查 `TLOpBuilder` 属性表，找到构造器并实例化；找不到则返回空的 `TileOperator()`。

`LowerTileOp` 调用算子的 `Lower`：[src/transform/lower_tile_op.cc:L1143-L1224](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/lower_tile_op.cc#L1143-L1224) —— 这是「算子降级」的总枢纽：先用 `ParseOperator` 解析，再装配 `LowerArgs`（含四个回调的 lambda），最后第 1221 行 `tile_op->Lower(lower_args, analyzer_)` 真正降级。降级结果是底层 TIR，交回 `IRMutatorWithAnalyzer` 继续递归处理。

GEMM 作为子类范例，其 `Lower` 把工作外包给 Python：[src/op/gemm.cc:L178-L221](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/gemm.cc#L178-L221) —— `GemmNode::Lower` 查找全局函数 `tl.gemm.lower`（Python 侧注册），把 `Gemm` 对象、布局表、target 等传过去，拿回一个 `PrimFunc` 再包装成 `SBlockRealize`。这正是 u4-l2 提到的「C++ 是薄壳、实现外包给 Python」模式——指令选择在 Python 侧完成。

GEMM 的 `InferLayout` 同样外包：[src/op/gemm.cc:L223-L261](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/gemm.cc#L223-L261) —— 查找 `tl.gemm.infer_layout`，对返回的 fragment 布局做 `BindThreadRange`（把逻辑坐标绑定到线程范围），并按 `reuse_existing_shared_layout` 决定是否复用既有 shared 布局（普通 MMA 可复用，WGMMA/TCGEN5 不可，详见 u4-l2）。

#### 4.2.4 代码实践

**实践目标**：跟踪一条 `T.gemm` 从 TIR 调用到 `Lower()` 的完整路径，确认 `TileOperatorNode` 的两个核心虚方法。

**操作步骤**：

1. 用 `grep` 找到所有继承 `TileOperatorNode` 的算子（在各自 `.h` 里）：
   ```bash
   grep -rn "public TileOperatorNode\|: public tl::TileOperatorNode\|TIR_REGISTER_TL_TILE_OP" src/op src/cuda/op src/maca/op | head
   ```
2. 找到 `Lower` 被调用的唯一地点（应当就是 `lower_tile_op.cc`）：
   ```bash
   grep -rn "->Lower(lower_args" src/transform
   ```
3. 找到 `InferLayout` 被调用的地点：
   ```bash
   grep -rn "->InferLayout(" src/transform src/backend
   ```

**需要观察的现象**：`->Lower(lower_args` 只出现在 `lower_tile_op.cc` 一处；`InferLayout` 主要出现在 `layout_inference.cc` 与若干公共算子头文件里（后者是算子之间的级联推断）。

**预期结果**：你会确认「`Lower` 由 `LowerTileOp` 驱动、`InferLayout` 由 `LayoutInference` 驱动」，这两个 pass 是基类虚方法的两条消费主链。

> 本实践为「源码阅读型实践」，无需编译；若想验证运行期行为，可在 GEMM kernel 的 `tl.gemm.lower` Python 实现里加日志（见 u4-l2）。

#### 4.2.5 小练习与答案

**练习 1**：`LowerArgs` 里的四个回调分别解决什么问题？为什么不用全局变量？

> **参考答案**：`add_workspace` 申领动态 shared 内存（如临时 tile）、`alloc_mbarrier` 申领同步屏障槽位、`update_barrier_arrive` 登记屏障到达计数、`require_smem_alignment` 申报 shared 内存对齐要求（TMA/MMA 的 swizzle 约束）。用回调而非全局变量，是因为资源归「宿主 pass」所有、生命周期受 pass 管理，算子只「申请」不「持有」，避免多算子并发降级时的状态污染。

**练习 2**：`GemmNode::Lower` 为什么要把实现在 Python 侧的 `tl.gemm.lower` 完成？

> **参考答案**：指令选择（WGMMA/MFMA/MMA/标量）依赖大量 target×dtype×形状的组合判断，用 Python 表达更灵活、更易扩展（metax 分支正是借这套机制新增了 MACA 的 mfma 分派）。C++ 只做解析与返回值包装，保持「薄壳」，降低改后端时的 C++ 编译成本。

---

### 4.3 op/builtin：内置算子与编译开关

#### 4.3.1 概念说明

除了「tile 算子」（`tl.tileop.*`，有 `TileOperatorNode` 子类），TileLang 还有一批更底层的**内置函数**（`tl.*`），它们是 TIR 层的原子操作或编译期注解，例如：

- `tl.access_ptr`：指针访问元数据（前端专用，后续 lowering）。
- `tl.__exp`/`tl.__log`/`tl.__sin` 等：fast-math 快速数学函数。
- `tl.max_nan`/`tl.min_nan`/`tl.ieee_add` 等：带特殊语义（NaN 传播、IEEE 精确）的运算。

这些内置函数在 `op/builtin.cc` 里用统一宏 `TIR_DEFINE_TL_BUILTIN` 注册，并标注属性（输入数、调用副作用类别）。`builtin.cc` 还承担另一项职责：注册一批 **pass 配置开关**（如 `tl.disable_fast_math`、`tl.enable_async_copy`、`tl.disable_wgmma`），它们就是你在 PassContext 里能读到的那些 `tl.*` 选项。

> 区分两套命名：`tl.tileop.*` 是高级 tile 算子（走 `TileOperatorNode`），`tl.*`（去掉 tileop）是底层内置函数（走 `builtin.cc`）。前者数量少而重，后者数量多而轻。

#### 4.3.2 核心流程

注册一个内置函数的固定三步（由宏展开完成）：

```
TIR_DEFINE_TL_BUILTIN(__exp)
   ├─ 定义函数 __exp()，返回 Op::Get("tl.__exp")（惰性单例）
   ├─ TVM_REGISTER_OP("tl.__exp")：在全局 Op 注册表登记
   └─ set_attr<TScriptPrinterName>("TScriptPrinterName", "__exp")：让 TVM Script 打印机认识它
```

后续调用方（如前端、codegen）通过 `Op::Get("tl.__exp")` 拿到这个 Op，再读它的属性来决定如何 lower 或印出。

注册一个 pass 配置开关则用 `TVM_REGISTER_PASS_CONFIG_OPTION`：它把一个字符串键（如 `tl.disable_vectorize_256`）与默认类型登记进 PassContext，使 `config.h` 里的 `ctxt->GetConfig(...)` 能取到用户在 PassContext 里设的值。

#### 4.3.3 源码精读

pass 配置开关的集中注册：[src/op/builtin.cc:L22-L52](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/builtin.cc#L22-L52) —— 这里登记了 `tl.debug_merge_shared_memory_allocations`、`tl.disable_wgmma`、`tl.enable_async_copy`、`tl.ptxas_register_usage_level` 等一大批开关。这些键就是 PassContext 配置字典里能用的字段。

内置函数注册宏 `TIR_DEFINE_TL_BUILTIN`：[src/op/builtin.cc:L56-L62](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/builtin.cc#L56-L62) —— 一行宏同时完成「定义取 Op 的函数」「注册 Op」「设打印机名」三件事。

一批 fast-math 内置算子的注册：[src/op/builtin.cc:L65-L102](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/builtin.cc#L65-L102) —— `access_ptr`、`__exp`、`__log`、`__sin`、`__cos`、`fast_rcp`、`max_nan`、`min_nan` 等在此登记，各自带 `TCallEffectKind`（`kPure`/`kOpaque`）标注副作用，供后续优化与 codegen 判断能否消除或重排。

配置开关在 C++ 侧的读取方式（`config.h` 工具）：[src/config.h:L19-L33](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/config.h#L19-L33) —— `VectorizePlannerVerboseEnabled()`/`Vectorize256Disabled()` 通过 `transform::PassContext::Current()->GetConfig("tl....")` 读取对应开关，`value_or(Bool(false))` 给出默认值。任何 pass 想读配置，都走这条路径。

#### 4.3.4 代码实践

**实践目标**：统计 `builtin.cc` 注册了多少个 pass 开关与内置算子，并理解配置如何被读取。

**操作步骤**：

1. 统计 pass 配置开关数量：
   ```bash
   grep -c "TVM_REGISTER_PASS_CONFIG_OPTION" src/op/builtin.cc
   ```
2. 统计内置算子数量（`tl.` 前缀的 `TIR_DEFINE_TL_BUILTIN` 用例）：
   ```bash
   grep -c "TIR_DEFINE_TL_BUILTIN" src/op/builtin.cc
   ```
3. 找出哪些 pass 实际读取了某个开关，例如 `tl.disable_vectorize_256`：
   ```bash
   grep -rn "tl.disable_vectorize_256\|Vectorize256Disabled\|disable_vectorize_256" src/ | head
   ```
4. 阅读一处读取点，确认它用 `PassContext::Current()->GetConfig(...)`。

**需要观察的现象**：开关在 `builtin.cc` 集中注册，在散落各 pass 的 `config.h` 工具函数或 `PassContext::Current()->GetConfig(...)` 处读取；读取方与注册方分离。

**预期结果**：你将看到「注册集中、读取分散」的模式，能据此判断「新增一个编译开关」需要改哪两处（注册处 + 读取处）。

#### 4.3.5 小练习与答案

**练习 1**：`TCallEffectKind` 标成 `kPure` 还是 `kOpaque`，对编译器意味着什么？

> **参考答案**：`kPure`（纯）表示无副作用、可重排、可消除（如 `max_nan`、`ieee_add`），优化 pass 可大胆处理；`kOpaque`（不透明）表示有副作用或状态（如 `__exp` 这类 fast-math，可能改变精度/语义），优化须保守，不能随意重排或删除。

**练习 2**：若想新增一个 `tl.enable_my_feature` 开关，需要在哪两个地方落笔？

> **参考答案**：在 `src/op/builtin.cc` 用 `TVM_REGISTER_PASS_CONFIG_OPTION(kEnableMyFeature, Bool)` 注册键（`kEnableMyFeature` 字符串常量通常定义在 `transform/common/attr.h`）；在需要读取的 pass 里用 `PassContext::Current()->GetConfig("tl.enable_my_feature", ffi::Optional<Bool>()).value_or(Bool(false))` 取值，或仿照 `config.h` 封装一个工具函数。

---

### 4.4 src/ir.cc：前端扩展与线程绑定帧

#### 4.4.1 概念说明

还记得 u2-l2 里讲的 `with T.Kernel(*grid, threads=...)` 吗？它定义「怎么启动」kernel：grid 定 `gridDim`、threads 定 `blockDim`、`as (bx, by)` 解包出 block 绑定。本节揭开它的 C++ 实现。

`src/ir.cc` 的注释写得明白：它是「TVM script 前端的扩展」。TVM 的 `@T.prim_func` 在编译期把 Python 函数执行成一棵 TIR，执行过程中遇到 `T.Parallel`、`T.Pipelined`、`T.Kernel` 等结构时，前端会调用对应的 C++ 函数来构造「帧（Frame）」。`src/ir.cc` 就是为 TileLang 量身定制的这一组帧构造器。

最核心的设计是：`ir.cc` 产出的线程绑定循环是 **target 中立**的——它一律生成 `ForKind::kThreadBinding` 的循环，tag 写成 `blockIdx.x`/`threadIdx.x`，但**不直接绑定到任何后端语法**。真正变成 CUDA 的 `<<<grid,block>>>` 或 CPU 的串行 `for`，是后面 `MaterializeKernelLaunch` pass 的事（详见 u2-l2）。

#### 4.4.2 核心流程

`T.Kernel(grid, threads)` 在 C++ 侧的展开（`KernelLaunch` 函数）：

```
with T.Kernel(Gx, Gy, threads=128) as (bx, by):
   │  KernelLaunch([Gx,Gy], [128], attrs)
   ▼
KernelLaunchFrame = [
   MakeThreadBindingFrame("bx", "blockIdx.x", Gx),   # grid 帧（1~3 个）
   MakeThreadBindingFrame("by", "blockIdx.y", Gy),
   MakeThreadBindingFrame("tx", "threadIdx.x", 128), # thread 帧（恒 3 个，threads 归一化为 3D）
   MakeThreadBindingFrame("ty", "threadIdx.y", 1),
   MakeThreadBindingFrame("tz", "threadIdx.z", 1),
   empty_block (DeviceMainBlockName)                 # 主体 block（1 个）
]
```

每个 `MakeThreadBindingFrame` 产出一个 `ForFrame`，其 `f_make_for_loop` 闭包负责最终生成一条 `For(..., ForKind::kThreadBinding, ..., thread_binding=IterVar(tag=...))` 语句。这就是 u2-l2 里「按固定顺序生成帧、用 `frames[0:-4]` 取 grid 绑定」的物理来源——「3 个 thread 帧 + 1 个主体 block」恒占末尾 4 个，其余是 grid 帧。

之后，`MaterializeKernelLaunch` pass 遍历这组嵌套循环：

- SIMT 后端（CUDA/ROCm/MACA/Metal）：每条循环改成 `AttrStmt(thread_extent)`，复用循环变量。
- CPU 后端：`blockIdx.*` 变成普通串行 `for`，`threadIdx.*` 退化成单步循环（变量钉死为 0），丢弃 `threads=128` 这类请求。

#### 4.4.3 源码精读

单维线程绑定帧构造器 `MakeThreadBindingFrame`：[src/ir.cc:L31-L55](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/ir.cc#L31-L55) —— 它的闭包 `f_make_for_loop` 生成一条 `ForKind::kThreadBinding` 循环，`thread_binding` 设为带 `thread_tag`（如 `"blockIdx.x"`）的 `IterVar`。注释强调：这套帧是 target 中立的，具体形态由 `tl.MaterializeKernelLaunch` 在 target 已知后才物化。

`KernelLaunch` 装配启动嵌套：[src/ir.cc:L264-L297](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/ir.cc#L264-L297) —— 顶部定义了 `bx/by/bz`、`blockIdx.*`、`tx/ty/tz`、`threadIdx.*` 的固定名字数组；先按 `grid_size` 推 grid 帧，再按 `block_size` 推 thread 帧，最后追加一个空主体 block（`DeviceMainBlockName`）。`ICHECK(grid_size.size() <= 3)` 等断言保证维度合法。

帧的对外注册：[src/ir.cc:L299-L306](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/ir.cc#L299-L306) —— `TVM_FFI_STATIC_INIT_BLOCK` 把 `tl.Parallel`、`tl.Pipelined`、`tl.Persistent`、`tl.KernelLaunch` 注册成可被前端（Python）调用的全局函数。也就是说，你在 DSL 里用的 `T.Parallel` 等，最终调用的就是这里的 C++ 函数。

`ParallelFor` 的外层注解策略：[src/ir.cc:L57-L97](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/ir.cc#L57-L97) —— 它把 `parallel_loop_layout` 等注解**只挂到最外层**并行循环（注释解释：内层循环无法管辖或标注外层，只有最外层能管理整个嵌套区域）。这印证了 u2-l3 里「`loop_layout` 必须挂最外层」的规则。

物化 pass `MaterializeKernelLaunch`：[src/transform/materialize_kernel_launch.cc:L73-L96](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/materialize_kernel_launch.cc#L73-L96) —— `ConvertNest` 剥出连续的启动嵌套：`lower_thread_binding_=true` 时改成 `AttrStmt(thread_extent)`；为 false（CPU）时 `blockIdx` 变串行 `for`、`threadIdx` 退化为单步循环。文件头注释（第 1–22 行）对这套机制有完整说明，强烈建议通读。

#### 4.4.4 代码实践

**实践目标**：验证「`ir.cc` 产 target 中立帧、`MaterializeKernelLaunch` 物化」这条链，并理解维度固定顺序。

**操作步骤**：

1. 阅读文件头注释，它把整套机制讲得很清楚：
   ```bash
   sed -n '1,22p' src/transform/materialize_kernel_launch.cc
   ```
2. 确认 `KernelLaunch` 里 grid/thread 帧的生成顺序（grid 先、thread 后、body 最后）：
   ```bash
   sed -n '264,297p' src/ir.cc
   ```
3. 回顾 u2-l2 提到的「`frames[0:-4]` 取 grid 绑定」，对照本节 4.4.2 的展开图，解释为何是「减 4」。

**需要观察的现象**：`ir.cc` 不含任何 cuda/hip/maca 字样，纯粹生成 `kThreadBinding` 循环；后端差异完全延迟到 `materialize_kernel_launch.cc`（以及更后的 codegen）。

**预期结果**：你能口述「`T.Kernel` → `ir.cc` 中立帧 → `MaterializeKernelLaunch` 后端化」三步，并解释为何 `threads` 在 CPU 上会被丢弃。

> 本实践为「源码阅读型实践」。可选的运行期验证：写一个 GEMM kernel，分别在 `target="cuda"` 与 `target="llvm"`（CPU）下用 `get_kernel_source()` 导出源码，对比前者出现 `<<<...>>>` 启动语法、后者是普通函数调用。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `ir.cc` 要把线程绑定循环设计成 target 中立的，而不是直接生成 CUDA 的 `<<<grid, block>>>`？

> **参考答案**：TileLang 的目标是一份 kernel 编译到多后端（CUDA/HIP/MACA/Metal/CPU）。若 `ir.cc` 直接绑死 CUDA 语法，就无法编译到 CPU（CPU 没有 blockIdx）。中立帧让「启动语义」与「后端语法」解耦，由 `MaterializeKernelLaunch` 按 `lower_thread_binding` 配置分别物化，从而复用同一份 IR。

**练习 2**：在 `KernelLaunch` 中，为何 thread 帧恒为 3 个（tx/ty/tz），即使 `threads=128` 是一维？

> **参考答案**：前端把一维 `threads` 经 `_normalize_threads` 归一化为三维（128→(128,1,1)，详见 u2-l2）。`KernelLaunch` 按归一化后的三维 `block_size` 生成 3 个 thread 帧，保证下游统一处理 3D，避免维度数不固定的特判。这也正是「末尾恒占 3 thread 帧 + 1 body = 4」、可用 `frames[0:-4]` 取 grid 帧的原因。

---

## 5. 综合实践

把本讲四节串起来，完成一张「`src/` 导航图」：

1. **画目录职责表**：为 `src/` 下 `op`、`transform`、`layout`、`tl_templates`、`backend/common`，以及 `cuda`/`rocm`/`maca`/`metal`/`cpu`/`webgpu` 各后端目录，各写一句话职责。要求体现「公共层 vs 后端自有层」的划分。

2. **定位核心抽象**：在 `src/op/operator.h` 指出 `TileOperatorNode` 的两个核心虚方法 `Lower` 与 `InferLayout`（行号 150–154），并写出：
   - 谁调用 `Lower`？（答：`LowerTileOp` pass，见 `lower_tile_op.cc:1221`）
   - 谁调用 `InferLayout`？（答：`LayoutInference` pass）

3. **追踪一条调用链**：选 GEMM，画出从 `T.gemm`（DSL）→ `tl.tileop.gemm`（TIR 调用）→ `ParseOperator`（`operator.cc:34`）→ `GemmNode`（`gemm.h`）→ `GemmNode::Lower`（`gemm.cc:178`，外包 `tl.gemm.lower`）→ 底层 TIR 的链路图。

4. **对比一个后端差异**：阅读 `src/maca/runtime/maca_target_kind.cc:59-70`，记录 MACA 的 `thread_warp_size=64`，并说明这一差异会如何影响 `gemm.cc` 里 `ComputeWarpPartition` 的 warp 划分结果（提示：回顾 u4-l2 关于 `k_n_per_warp` 的讨论）。

完成后，你应当拥有这张导航图（建议存成笔记），后续 u5-l2（transform）、u5-l3（codegen）、u7（MACA 后端）都将在这张图上「放大」某一块继续深入。

## 6. 本讲小结

- `src/` 采用「公共层 + 后端自有层」双层布局：`op/`、`transform/`、`layout/`、`tl_templates/`、`backend/common/` 是公共层；`cuda/rocm/maca/metal/cpu/webgpu` 是结构对称的后端自有层，各含 `codegen/`+`op/`+`target_utils`+（GPU 才有的）`transform/` 四件套。
- `TileOperatorNode`（`operator.h:148`）是所有 tile 算子的基类，两个核心纯虚方法是 `Lower()`（降级成底层 TIR）与 `InferLayout()`（推断 fragment 寄存器布局），另有 `Clone()` 与 `GetAccessRegions()`。
- 算子经 `ParseOperator`（`operator.cc:34`，查 `TLOpBuilder` 属性表）实例化，`Lower` 由 `LowerTileOp` pass 调用、`InferLayout` 由 `LayoutInference` pass 调用；GEMM 等算子的 `Lower`/`InferLayout` 常外包给 Python 全局函数（如 `tl.gemm.lower`）。
- `op/builtin.cc` 用 `TIR_DEFINE_TL_BUILTIN` 注册 `tl.*` 底层内置函数（access_ptr、fast-math、ieee_*），并用 `TVM_REGISTER_PASS_CONFIG_OPTION` 集中注册 pass 开关；读取则散落在各 pass（经 `config.h` 或 `PassContext::Current()->GetConfig`）。
- `src/ir.cc` 是 TVM script 前端的扩展，`KernelLaunch` 产出 **target 中立**的 `kThreadBinding` 线程绑定帧（grid 帧 + 恒 3 个 thread 帧 + 1 个主体 block），再由 `MaterializeKernelLaunch` pass 按后端物化（GPU→`thread_extent`，CPU→串行 `for`）。
- metax 分支的关键印记：MACA 作为平级后端出现在 `src/maca/`（与 cuda/rocm 对称），并在 `backend/common/target_utils.cc` 的统一分发里占一席；其 `thread_warp_size=64` 是与 CUDA（32）最显眼的差异。

## 7. 下一步学习建议

- **u5-l2 编译 pass / transform 体系**：本讲只点了 `LowerTileOp`、`LayoutInference`、`MaterializeKernelLaunch` 三个 pass 的名，下一讲将按执行顺序串起 `src/transform/` 的主流水线（`lower_tile_op`→`split_host_device`→`materialize_kernel_launch`→存储优化），是理解编译全链路的必经之路。
- **u5-l3 CUDA/HIP codegen 后端**：本讲把后端 `codegen/` 作为四件套之一带过，下一讲深入 `CodeGenTileLangCUDA`/`CodeGenTileLangHIP` 的继承结构与 `intrin_rule` 降低规则。
- **顺带阅读**：若对布局理论感兴趣，可提前翻 `src/layout/layout.h`，为 u4-l3 的 Layout/Fragment 补足 C++ 侧的类定义；若对算子扩展感兴趣，可对照 `src/op/copy.cc` 与 `src/op/copy.h` 看 `TileOperatorNode` 的一个「自包含」子类（不外包给 Python），为 u9-l2「新增 tile 算子」做准备。

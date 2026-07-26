# 设备代码生成、模板与 tile op lowering

## 1. 本讲目标

上一讲（u6-l2）我们看到了 Pass 流水线如何把 tile op 的**占位节点**展开成底层 intrinsic，并推导出 fragment/shared 的物理布局。本讲接着回答最后一个问题：**这些展开后的 TIR，是怎么变成一段真正能被 nvcc 编译的 CUDA C++ 源码的？源码里那些 `tl::AllReduce`、`cp.async`、`wgmma` 调用又是从哪儿冒出来的？**

学完本讲你应该能够：

- 说清 `CodeGenTileLangCUDA` 如何用访问者模式把 device IRModule 翻译成 CUDA C++ 字符串，并按需拼出头文件 `#include`。
- 解释 tile op 的「注册表 + builder」机制：`tl.tileop.gemm` 这个占位 intrinsic 是怎么被 `ParseOperator` 识别、又被 `GemmNode::Lower` 展开的。
- 区分两条 lowering 路径：GEMM 的「C++ 选指令 + Python 发射」混合模式，与 Copy 的「纯 C++ 函数指针」模式。
- 知道 `src/tl_templates/cuda/` 下的模板头（reduce.h、copy.h、instruction/mma.h 等）如何通过 `-I` 注入到生成的源码里。
- 拿到一份 `get_kernel_source()` 后，能逐段标注每段代码的「来源 Pass 与来源文件」。

## 2. 前置知识

本讲默认你已经读过 u6-l2（Pass 体系与关键 lowering Pass）和 u3-l1（T.gemm 与 tile op 体系）。回顾三个关键结论：

1. **占位 → 指令**：DSL 层的 `T.gemm`/`T.copy` 只生成一个 `tl.tileop.gemm`/`tl.tileop.copy` 的 `call_intrin` 占位节点；真正的 MMA/cp.async 指令在 `LowerTileOp` Pass 里展开。
2. **LayoutInference 在 LowerTileOp 之前**：先推导 fragment/shared 的物理布局，LowerTileOp 再按布局把占位展开成线程级指令。
3. **device codegen 是最后一棒**：等到 codegen 运行时，tile op 已经是底层 intrinsic（`mma_sync`/`cp.async`/`wgmma`…），codegen 只负责把它们打印成 C++。

还需要两个朴素概念：

- **访问者模式（Visitor）**：TVM 的 codegen 继承 `CodeGenC`，对每种 TIR 节点（`For`、`BufferStore`、`Call`…）重写一个 `VisitStmt_`/`VisitExpr_` 方法，遍历到该节点就往一个字符串流里写对应的 C++ 代码。
- **按需 include（lazy include）**：codegen 在翻译过程中如果遇到需要某个头文件支持的指令（比如 `mma_sync` 需要 `<mma.h>`），就把一个 `need_mma_h_` 标志置真；最后在 `Finish()` 里根据这些标志决定把哪些 `#include` 拼到源码顶部，避免无谓依赖。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [src/cuda/codegen/codegen_cuda.cc](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/cuda/codegen/codegen_cuda.cc) | CUDA 代码生成本体：把 device TIR 打印成 CUDA C++，并在 `Finish()` 拼装 `tl_templates` 头文件 |
| [src/cuda/codegen/codegen_cuda.h](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/cuda/codegen/codegen_cuda.h) | `CodeGenTileLangCUDA` 类声明与一堆 `need_*` 头文件标志 |
| [src/cuda/codegen/rt_mod_cuda.cc](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/cuda/codegen/rt_mod_cuda.cc) | `BuildTileLangCUDA`：调用 codegen 生成源码 → 调 nvcc → 产出 runtime module |
| [tilelang/backend/device_codegen.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/backend/device_codegen.py) | Python 侧 device codegen 注册表（`resolve_device_codegen`） |
| [tilelang/cuda/codegen.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cuda/codegen.py) | 把 cuda 后端的 `target.build.tilelang_cuda` 注册进注册表 |
| [tilelang/engine/lower.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/lower.py) | `tilelang_callback_cuda_compile`：运行时调 nvcc，并传入 `-I` 模板路径 |
| [src/op/operator.cc](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/op/operator.cc) | `ParseOperator`：用 `TLOpBuilder` 属性表把 intrinsic 识别成 TileOperator |
| [src/op/gemm.cc](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/op/gemm.cc) | `GemmNode`：构造、`Lower`（回调 Python）、`InferLayout`、C++ 注册表 |
| [src/op/copy.cc](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/op/copy.cc) | `CopyNode::Lower`：**纯 C++** 的 copy lowering 入口 |
| [src/cuda/op/gemm.cc](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/cuda/op/gemm.cc) | CUDA 后端 GEMM 指令选择（`SelectInst`）与 `RegisterCudaGemm` |
| [src/cuda/op/copy.cc](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/cuda/op/copy.cc) | CUDA 后端 copy 的 TMA/cp.async/普通循环 lowering |
| [src/transform/lower_tile_op.cc](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/lower_tile_op.cc) | `LowerTileOpPass`：遍历 Evaluate(Call)，调 `ParseOperator` + `tile_op->Lower()` |
| [src/tl_templates/cuda/](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/tl_templates/cuda) | 预写好的 C++ 模板头：reduce.h、copy.h、instruction/mma.h 等 |

---

## 4. 核心概念与源码讲解

### 4.1 设备代码生成：把 TIR 打印成 CUDA C++（src.cuda.codegen）

#### 4.1.1 概念说明

device codegen 是编译流水线的**最后一棒**。它接收的输入是一个「只含 device PrimFunc」的 IRModule——此时所有 tile op 占位已经被 `LowerTileOp` 展开成 `mma_sync`、`cp.async`、`wgmma` 之类的底层 intrinsic。codegen 的工作只有两件：

1. 遍历这个 IRModule，用访问者模式把每条 TIR 语句打印成对应的 CUDA C++ 代码。
2. 在打印过程中收集「需要哪些头文件」（`need_*` 标志），最后在源码顶部把这些 `#include` 拼上。

CUDA 后端的 codegen 类叫 `CodeGenTileLangCUDA`，继承自 TVM 的 `CodeGenC`。它**重写**了一大批 `Visit*` 方法来处理 CUDA 特有的东西：`__shared__` 作用域、warp 级向量操作、PTX 内联汇编、fragment 的 `wmma`/`wgmma` 调用等。

#### 4.1.2 核心流程

从 Python 一路追到 cubin，链路如下（关键节点都用粗体标出）：

1. `lower()` 用 `resolve_device_codegen(target)` 查注册表，按 target kind 拿到一个 `DeviceCodegen` 条目。
2. cuda 后端在 [tilelang/cuda/codegen.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cuda/codegen.py) 里把 `build`/`build_without_compile` 两个函数指针都绑到 C++ 全局函数 `target.build.tilelang_cuda`（及 `_without_compile`）。
3. C++ 侧 [rt_mod_cuda.cc](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/cuda/codegen/rt_mod_cuda.cc) 的 `BuildTileLangCUDA` 创建 `CodeGenTileLangCUDA`，对每个 PrimFunc 调 `AddFunction`，然后 `Finish()` 得到一整段 CUDA C++ 字符串。
4. `BuildTileLangCUDA` 调用 Python 全局函数 `tilelang_callback_cuda_compile(code, target, pass_config)`，后者跑 nvcc 把源码编成 cubin/fatbin（带二进制缓存）。
5. 用 `CUDAModuleCreateWithFallback` 把二进制 + 源码包成 runtime module 返回。

注意有**两条路径**：`build`（真编译，产出可执行 cubin）和 `build_without_compile`（只生成源码，塞一个 dummy ptx 占位，专门给 `get_kernel_source()` 用）。这就是为什么即便你没有 GPU，也能用 `build_without_compile` 看到生成的 CUDA 源码。

#### 4.1.3 源码精读

先看 codegen 类的骨架与那堆头文件标志：

[src/cuda/codegen/codegen_cuda.h:23-37](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/cuda/codegen/codegen_cuda.h#L23-L37) 声明了 `CodeGenTileLangCUDA`，它 `final : public CodeGenC`，并重写了一长串 `VisitStmt_`/`VisitExpr_`/`Print*` 方法——这就是「访问者打印」的接口面。

[src/cuda/codegen/codegen_cuda.h:118-156](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/cuda/codegen/codegen_cuda.h#L118-L156) 列出了一堆 `need_*` 布尔标志（`need_mma_h_`、`need_copy_sm90_h_`、`need_atomic_h_`、`enable_fp8_`…）。这些就是「按需 include」的状态位：翻译过程中遇到对应指令就置真。

`Finish()` 是头文件注入的核心。它先写死 `<cuda.h>`，然后按 `need_*` 标志条件 `#include` 一批模板头，最后还有几个**无条件** include 的（reduce.h、scan.h、ldsm.h、debug.h 等）：

[src/cuda/codegen/codegen_cuda.cc:660-738](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/cuda/codegen/codegen_cuda.cc#L660-L738) —— 例如 `need_mma_instruction_h_` 为真就 `#include <tl_templates/cuda/instruction/mma.h>`；而 [第 731-735 行](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/cuda/codegen/codegen_cuda.cc#L731-L735) 的 reduce.h / scan.h / ldsm.h 则**每个 kernel 都会带上**。这正是本讲综合实践要在源码里首先定位的目标。

那 `need_*` 标志是怎么被置真的？答案在 `VisitExpr_(const CallNode*)` 里——翻译到某个 intrinsic 调用时顺手置标志。例如遇到 `tvm_fill_fragment`/`tvm_load_matrix_sync`/`tvm_mma_sync` 这一组 wmma intrinsic：

[src/cuda/codegen/codegen_cuda.cc:2909-2959](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/cuda/codegen/codegen_cuda.cc#L2909-L2959) 把 `tvm_mma_sync` 打印成 `nvcuda::wmma::mma_sync(...)` 并同时 `need_mma_h_ = true`。也就是说：**TIR 里的 intrinsic 名 → 一段 C++ 代码 + 一个头文件标志**，这就是 codegen 翻译 intrinsic 的统一套路。

再往上，是谁驱动 codegen 跑起来的？看 `BuildTileLangCUDA`：

[src/cuda/codegen/rt_mod_cuda.cc:97-138](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/cuda/codegen/rt_mod_cuda.cc#L97-L138) —— 创建 `CodeGenTileLangCUDA`、校验每个 PrimFunc 的 `calling_conv == kDeviceKernelLaunch`、逐个 `AddFunction`、`cg.Finish()` 得到 `code`，再 [第 123 行](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/cuda/codegen/rt_mod_cuda.cc#L123) 调 `tilelang_callback_cuda_compile` 把源码编成二进制。`BuildTileLangCUDAWithoutCompile`（[第 140-173 行](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/cuda/codegen/rt_mod_cuda.cc#L140-L173)）走同样的打印流程，但跳过 nvcc、塞 dummy ptx，把源码保存在 `source_map` 里供 `get_source` 读取。二者都在文件末尾注册成全局函数：

[src/cuda/codegen/rt_mod_cuda.cc:175-181](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/cuda/codegen/rt_mod_cuda.cc#L175-L181) 把 `target.build.tilelang_cuda` / `_without_compile` 挂到 TVM 的全局函数表，Python 侧 `global_func_device_codegen("target.build.tilelang_cuda")` 就是通过这个名字找到它的。

Python 注册侧则是一个标准的「按 target kind 注册 + 谓词匹配」注册表：

[tilelang/backend/device_codegen.py:102-110](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/backend/device_codegen.py#L102-L110) 的 `resolve_device_codegen` 按 `target.kind.name` 取候选，再用 `supports_target` 谓词分流（普通 cuda vs 带 `cutedsl` 标签的 cuda）；[tilelang/cuda/codegen.py:16-36](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cuda/codegen.py#L16-L36) 就是 cuda 后端的两条注册（`cuda` 与 `cutedsl`），它们的 `build` 都指向同一个 C++ 全局函数。

#### 4.1.4 代码实践

**目标**：用「只生成源码」路径拿一份 CUDA 源码，验证 `Finish()` 的 include 策略。

**操作步骤**（需要可 import tilelang 的环境，无 GPU 也能做「只生成源码」部分）：

1. 编写 `examples/gemm/example_gemm.py` 的精简版（见 4.4 的综合实践完整版），用 `matmul.compile(...)` 得到 `kernel`。
2. 调用 `print(kernel.get_kernel_source())` 打印生成的 CUDA 源码。`get_kernel_source()` 内部走的就是 `build_without_compile` 路径。
3. 在源码**顶部**逐行核对 `#include`：找到无条件出现的 `#include <tl_templates/cuda/reduce.h>`、`<tl_templates/cuda/scan.h>`、`<tl_templates/cuda/ldsm.h>`。

**需要观察的现象**：reduce.h 等若干头**无论 kernel 是否用到归约都会出现**；而 `<mma.h>`、`<tl_templates/cuda/copy_sm90.h>` 等只在用到对应指令时才出现。

**预期结果**：能数出「无条件 include」与「条件 include」两组，并与 [codegen_cuda.cc:660-738](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/cuda/codegen/codegen_cuda.cc#L660-L738) 一一对应。若无法在本地运行，标注「待本地验证」并仅做源码侧静态核对。

#### 4.1.5 小练习与答案

**练习 1**：为什么 reduce.h 是无条件 include，而 copy_sm90.h 是条件 include？
**答案**：reduce/scan/ldsm 是几乎所有 kernel 都可能用到的基础设施（归约、扫描、矩阵加载），强制带上代价小、收益稳；而 copy_sm90 只在 SM90 TMA 拷贝时才需要，条件 include 可避免在非 Hopper kernel 里引入不必要的 TMA 头依赖。

**练习 2**：`BuildTileLangCUDAWithoutCompile` 不调 nvcc，那它返回的 module 还能「运行」吗？
**答案**：不能执行真正的 kernel。它塞了一个 dummy ptx 占位，主要价值是保留 `source_map["cuda"]` 供 `get_kernel_source()`/`get_source()` 读取，用于离线查看与调试。

---

### 4.2 tile op 的注册与分发（src.op）

#### 4.2.1 概念说明

上一棒（codegen）打印的 intrinsic 并不是凭空出现的，它们是 `LowerTileOp` Pass 把 tile op 占位展开后的结果。这一节回答：**占位节点是怎么被识别成一个 C++ 对象、又怎么被展开的？**

tilelang 用 `TileOperator` 抽象描述一个 tile op。每个具体的 op（Gemm、Copy、Reduce、Fill…）是一个 `XxxNode` 类，实现两个核心钩子：

- `InferLayout(...)`：在 LayoutInference Pass 里被调用，推导它涉及的 buffer 的物理布局。
- `Lower(...)`：在 LowerTileOp Pass 里被调用，把自己展开成底层 TIR 语句。

而「intrinsic 名 `tl.tileop.gemm` → 构造 `GemmNode`」的映射，靠的是 TVM 的 **Op 属性表**：每个 Op 上挂一个 `TLOpBuilder` 属性，值是一个「(args, annotations) → TileOperator」的 builder 函数。

#### 4.2.2 核心流程

LowerTileOp 内部对每个 `Evaluate(Call)` 节点的处理：

1. `ParseOperator(call, block_annotations)` 查 `TLOpBuilder` 属性表，找到对应 builder 并构造 `TileOperator`（如 `Gemm`）。
2. 组装 `LowerArgs`（target、线程范围、线程索引、layout_map、各种回调…）。
3. 调 `tile_op->Lower(lower_args, analyzer)`，拿到展开后的 TIR 语句，递归 visit。

GEMM 的 `Lower` 有个**特别之处**：它不在 C++ 里硬编码指令发射，而是**回调 Python 全局函数 `tl.gemm.lower`**，把工作交给 Python 侧的实现类。这是 tilelang「C++ 框架 + Python 策略」分层的关键设计——下一节会看到 Copy 走的是完全不同的纯 C++ 路径。

#### 4.2.3 源码精读

先看识别入口 `ParseOperator`：

[src/op/operator.cc:37-53](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/op/operator.cc#L37-L53) 查 `Op::GetAttrMap<OpBuilderFunc>("TLOpBuilder")`，命中就 `op_map[op](call->args, call->annotations)` 构造 TileOperator。`ParseOperator(Stmt, ...)`（[第 67-74 行](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/op/operator.cc#L67-L74)）只是把 `Evaluate(Call)` 解包后转调上面这个重载。

再看 LowerTileOp Pass 是在哪里调它的：

[src/transform/lower_tile_op.cc:1117-1198](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/lower_tile_op.cc#L1117-L1198) 的 `VisitStmt_(EvaluateNode)`：[第 1123 行](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/lower_tile_op.cc#L1123) `ParseOperator`，[第 1178-1194 行](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/lower_tile_op.cc#L1178-L1194) 组装 `LowerArgs`（含 `add_workspace`/`alloc_mbarrier`/`require_smem_alignment` 等回调），[第 1195 行](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/lower_tile_op.cc#L1195) `tile_op->Lower(lower_args, analyzer_)`。

GEMM 节点的注册与 Lower：

[src/op/gemm.cc:261-264](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/op/gemm.cc#L261-L264) 用 `TIR_REGISTER_TL_TILE_OP(Gemm, gemm)` 把 op 名 `gemm` 绑到 `Gemm` 的 builder（即 `Gemm(args, annotations)` 构造函数）。这就是 DSL 层 `tl.tileop.gemm` 能被识别的根因。`wgmma_gemm`/`tcgen05_gemm` 是另外注册的两个 Op，它们的 builder 只是多打一个 `is_wgmma`/`is_tcgen05` 注解后复用同一个 `Gemm` 构造（[第 266-292 行](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/op/gemm.cc#L266-L292)）。

[src/op/gemm.cc:176-219](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/op/gemm.cc#L176-L219) 是 `GemmNode::Lower`：[第 178 行](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/op/gemm.cc#L178) `Function::GetGlobal("tl.gemm.lower")` 拿到 Python 函数，[第 185-187 行](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/op/gemm.cc#L185-L187) 把 `Gemm` 对象、layout_map、target、thread_bounds、thread_index、mbar_phase 全传过去，拿回一个 `PrimFunc`，再包成 `SBlockRealize` 返回。注意第 183 行的注释明确写了：「Decide the instruction key and compute warp partition on Python side」——C++ 把决策权交给了 Python。

Python 侧接收的就是这个全局函数：

[tilelang/tileop/gemm/__init__.py:18-29](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tileop/gemm/__init__.py#L18-L29) 的 `gemm_lower` 转调 `gemm.lower(...)`，而 [第 127-139 行](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tileop/gemm/__init__.py#L127-L139) 的 `lower` 先 `_select_gemm_instruction`（选指令键），再 `_get_implementation_class`（选实现类），最后调实现类的 `lower()` 真正发射 TIR。这条链路把 C++ 的 `GemmNode` 与 Python 的 `GemmMMA`/`GemmWGMMA` 连了起来。

#### 4.2.4 代码实践（源码阅读型）

**目标**：跟踪 `T.gemm` 从占位到展开的完整调用链。

**操作步骤**：

1. 在 [src/op/operator.cc:37](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/op/operator.cc#L37) 处看到 `ParseOperator` 通过 `TLOpBuilder` 属性表构造 TileOperator。
2. 在 [src/op/gemm.cc:261](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/op/gemm.cc#L261) 确认 `gemm` op 绑到了 `Gemm` builder。
3. 在 [src/transform/lower_tile_op.cc:1195](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/lower_tile_op.cc#L1195) 看到 `tile_op->Lower(...)` 触发展开。
4. 在 [src/op/gemm.cc:178](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/op/gemm.cc#L178) 看到 C++ 跨语言回调 Python 的 `tl.gemm.lower`。

**需要观察的现象**：C++ 与 Python 之间通过 `(Gemm 对象, layout_map, target, thread_bounds, thread_index, mbar_phase)` 这组参数传递，`Gemm` 对象本身经 reflection 序列化跨 FFI。

**预期结果**：能画出 `T.gemm` → `tl.tileop.gemm` intrinsic → `ParseOperator` → `GemmNode` → `Lower` → Python `tl.gemm.lower` → 实现类 `lower()` → 展开后的 TIR 的调用链。

#### 4.2.5 小练习与答案

**练习 1**：`TIR_REGISTER_TL_TILE_OP(Gemm, gemm)` 这行代码运行时产生的效果是什么？
**答案**：它把名为 `gemm` 的 tile op 注册进 tilelang 的 op 表，并把 `TLOpBuilder` 属性设为 `Gemm(args, annotations)` 构造函数。这样 `ParseOperator` 遇到 `tl.tileop.gemm` 调用时就能查表构造出 `GemmNode`。

**练习 2**：为什么 `wgmma_gemm` 和 `tcgen05_gemm` 不各自实现一个 Node 类，而是复用 `Gemm`？
**答案**：它们在 IR 层是同一种「矩阵乘」语义，只在指令选择阶段需要区分。复用 `Gemm` + 一个 `is_wgmma`/`is_tcgen05` 注解，既避免了代码重复，又让 LayoutInference 等共用逻辑只写一份，差异推迟到 `SelectInst`（见 4.3）。

---

### 4.3 后端指令选择与 lowering（src.cuda.op）

#### 4.3.1 概念说明

同一个 `Gemm` 节点在不同 GPU 架构上要映射到不同张量核指令：

| 架构 | 指令键 | 典型实现类 |
| --- | --- | --- |
| Volta（SM70） | `cuda.mma` | `GemmMMASm70` |
| Turing（SM75） | `cuda.mma` | `GemmMMASm75` |
| Ampere/Ada（SM80+） | `cuda.mma` | `GemmMMA` |
| Hopper（SM90） | `cuda.wgmma` | `GemmWGMMA` |
| Blackwell（SM100） | `cuda.tcgen05` | `GemmTCGEN5` |

「选哪条指令」这件事分两层注册表，是本节的重点，也呼应了 u3-l1 已经讲过的 Python 侧 `resolve_gemm_impl`：

- **C++ 注册表**（本节主线）：`GemmImpl` 结构体，含 `select_inst`/`compute_warp_partition`/`reuse_existing_shared_layout` 三个函数指针。负责**选指令键 + 算 warp 切分 + 决定是否复用 shared 布局**，被 C++ 的 `InferLayout`/`GetGemmInstructionKey` 使用。
- **Python 注册表**（u3-l1 已讲）：按指令键 → 实现类。负责**真正发射 TIR 指令**。

两层共同构成「两段式分发」：C++ 先选定 `cuda.wgmma`，Python 再据此挑 `GemmWGMMA` 去发射 `wgmma` 指令。

Copy 走的是**完全不同的纯 C++ 路径**：它的 `Lower` 不回调 Python，而是用 `CopyImpl` 的 `lower`/`infer_layout` 函数指针，按 scope 组合直接在 C++ 里选 TMA / cp.async / 普通循环。这是 tilelang 内部「GEMM 偏 Python 策略、Copy 偏 C++ 实现」的有趣分工。

#### 4.3.2 核心流程

GEMM 指令选择的优先级（`SelectInst`）：

1. 若节点显式标注 `isWgmma_`/`isTcgen05_`（即用户写了 `T.wgmma_gemm`/`T.tcgen05_gemm`），校验能力后直接返回 `cuda.wgmma`/`cuda.tcgen05`，不满足就 `LOG(FATAL)`。
2. 否则**按能力自动选**：先试 `AllowTcgen5Mma`（Blackwell），再试 `AllowWgmma`（Hopper），都失败则回退 `cuda.mma`。

warp 切分满足约束 \(m_{\text{warp}} \cdot n_{\text{warp}} = \text{num\_warps}\)，并由 `GemmWarpPolicy`（Square/FullRow/FullCol）决定偏向。WGMMA 额外要求 `num_warps % 4 == 0`（warpgroup 为 4 个 warp）。

#### 4.3.3 源码精读

C++ 侧的指令选择入口：

[src/cuda/op/gemm.cc:266-287](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/cuda/op/gemm.cc#L266-L287) 是 `Gemm::SelectInst`，逻辑正是上面描述的优先级。能力判定函数 `AllowWgmma`（[第 87-95 行](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/cuda/op/gemm.cc#L87-L95)）检查 `TargetIsHopper && m>=64 && num_warps%4==0 && CheckWgmma`，并尊重 PassConfig 的 `kDisableWGMMA` 开关；`CheckWgmma`（[第 37-75 行](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/cuda/op/gemm.cc#L37-L75)）按 dtype 与 K 的整除性判断硬件是否支持。

把这套选择挂进 C++ 注册表：

[src/cuda/op/gemm.cc:318-329](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/cuda/op/gemm.cc#L318-L329) 的 `RegisterCudaGemm` 调 `RegisterGemmImpl{...}`，把 `MatchCudaGemmTarget`、`SelectInst`、`ComputeWarpPartition`、`ReuseExistingSharedLayout` 四个函数指针打包成一个 `GemmImpl`，name 为 `"cuda.Gemm"`。文件末尾 `const bool cuda_gemm_registered = RegisterCudaGemm();` 利用静态变量初始化完成自注册。

C++ 注册表的查找逻辑：

[src/op/gemm.cc:35-52](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/op/gemm.cc#L35-L52) 的 `ResolveGemmImpl(target)` 遍历注册表，用 `match_target` 谓词挑出唯一实现，要求**恰好命中一个**（多了报冲突、少了报未注册）。`GetGemmInstructionKey`（[第 166-168 行](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/op/gemm.cc#L166-L168)）就是调它的 `select_inst`。这个 key 经 FFI `tl.GemmGetGemmInstructionKey`（[第 307-311 行](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/op/gemm.cc#L307-L311)）暴露给 Python，于是 Python 的 `_select_gemm_instruction` 拿到的指令键其实是 C++ 算出来的——两段式分发的衔接点就在这里。

对比 Copy 的纯 C++ 路径：

[src/op/copy.cc:569-603](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/op/copy.cc#L569-L603) 的 `CopyNode::Lower` 没有 `GetGlobal(...)` 回调 Python，而是 [第 147 行](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/op/copy.cc#L147) 的 `LowerCopyForTarget` 按 target 在 C++ 函数指针表里挑实现，由 [src/cuda/op/copy.cc](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/cuda/op/copy.cc) 提供 global→shared 的 TMA / cp.async / 普通循环 lowering。**GEMM 跨语言回调 Python，Copy 全程在 C++**——这是记住二者差异的最简口诀。

Python 侧的注册（衔接 u3-l1）：

[tilelang/cuda/op/gemm/__init__.py:34-38](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cuda/op/gemm/__init__.py#L34-L38) 把五个实现类按 `(name, inst_name, predicate, impl_class)` 注册；[tilelang/tileop/gemm/registry.py:38-46](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tileop/gemm/registry.py#L38-L46) 的 `resolve_gemm_impl(gemm_inst, target)` 用 `inst_name == gemm_inst and predicate(target)` 挑出实现类。以 `GemmMMA` 为例，[tilelang/cuda/op/gemm/gemm_mma.py:71-90](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cuda/op/gemm/gemm_mma.py#L71-L90) 的 `lower` 用 `TensorCoreIntrinEmitter` 把矩阵乘展开成一串 `mma_sync`/`load_matrix_sync` intrinsic 调用——这些 intrinsic 最终被 4.1 的 codegen 打印成 `nvcuda::wmma::*`。

#### 4.3.4 代码实践

**目标**：观察同一份 GEMM 源码在不同 target 下的指令差异。

**操作步骤**（待本地验证，需要可指定 target 的环境）：

1. 用 4.4 综合实践的 GEMM kernel，分别以 `target="cuda --arch=sm_80"` 和 `target="cuda --arch=sm_90a"` 编译（可在 `compile(...)` 传 `target=...`）。
2. 分别 `get_kernel_source()`，在源码里搜索 `mma_sync`（SM80）与 `wgmma`（SM90）。

**需要观察的现象**：SM80 源码里出现 `nvcuda::wmma::mma_sync` 或 CuTe 的 `mma` 包装，且 `#include <tl_templates/cuda/instruction/mma.h>`；SM90 源码里出现 `wgmma.mma_async` 内联汇编，且 `#include <tl_templates/cuda/instruction/wgmma.h>`。

**预期结果**：能确认「target → `SelectInst` 选 key → Python 实现类 → 对应模板头」这条链路，并理解 C++ 注册表与 Python 注册表是**接力**而非重复。无 GPU 时标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 C++ 和 Python 各需要一个 GEMM 注册表？能不能合并成一个？
**答案**：C++ 注册表服务于 LayoutInference（C++ Pass，需要 warp 切分与 shared 布局复用决策），Python 注册表服务于指令发射（用 TVM TIR builder 写 intrinsic 更灵活）。合并意味着要么把布局推理搬到 Python（拖慢编译），要么把指令发射硬编码进 C++（失去 Python 迭代便利）。两段式分发是有意的取舍。

**练习 2**：`AllowWgmma` 里 `num_warps % 4 == 0` 的物理含义是什么？
**答案**：WGMMA（warp-group MMA）以 4 个 warp（128 线程）为一个 warpgroup 发射指令，因此线程数必须是 4×warp_size 的整数倍。

---

### 4.4 模板头文件注入（src.tl_templates.cuda）

#### 4.4.1 概念说明

codegen 并不重新发明归约、cp.async、MMA 包装这些轮子。它生成的源码里大量是 `tl::AllReduce<...>::run(...)`、`tl::cp_async_gs<16>(...)`、`tl::call_fma_impl(...)` 这样的**调用**，真正实现在 `src/tl_templates/cuda/` 下一组预写好的 C++ 模板头里。把这些头「喂」给 nvcc 的过程，就是模板注入。

注入有**两条路径**，分别对应「运行时 JIT」和「C++ 编译期」：

- **运行时 JIT**：`tilelang_callback_cuda_compile` 在 nvcc 选项里加 `-I <TILELANG_TEMPLATE_PATH>`，其中 `TILELANG_TEMPLATE_PATH` 指向 `src/` 目录，于是 `#include <tl_templates/cuda/reduce.h>` 能被找到。
- **C++ 编译期**：CMake 的 `TILE_LANG_INCLUDES` 把 `src/` 加入 include 路径，给 `tilelang_objs` 等 C++ 目标用。

#### 4.4.2 核心流程

模板注入与 4.1 的 codegen 是配合关系：

1. codegen 翻译 intrinsic 时，对「需要模板头」的指令置 `need_xxx_h_` 标志，并在源码里写出 `tl::xxx(...)` 调用。
2. `Finish()` 按标志拼 `#include <tl_templates/cuda/xxx.h>`。
3. nvcc 在编译时通过 `-I` 找到这些头，模板实例化、生成最终机器码。

`TILELANG_TEMPLATE_PATH` 的默认值是仓库的 `src/` 目录：因为模板头放在 `src/tl_templates/cuda/` 下，只要 `-I` 指向 `src/`，`#include <tl_templates/cuda/reduce.h>` 就能命中。

#### 4.4.3 源码精读

模板头注入的「运行时」入口：

[tilelang/engine/lower.py:101-126](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/lower.py#L101-L126) 的 `tilelang_callback_cuda_compile` 在 `options` 里显式加上 `"-I" + TILELANG_TEMPLATE_PATH` 与 `"-I" + CUTLASS_INCLUDE_DIR`（[第 124-125 行](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/lower.py#L124-L125)），再带着 `--use_fast_math`、`--ptxas-options` 等跑 nvcc，并经 `CUDABinaryCache` 缓存 cubin/fatbin。注释也点明 reduce.h 用了 C++20 的显式 lambda 模板参数，故强制 `-std=c++20`。

`TILELANG_TEMPLATE_PATH` 的初始化：

[tilelang/env.py:626-630](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/env.py#L626-L630) 在环境变量未设时，把它默认指向 `THIRD_PARTY_ROOT/../src`，即仓库 `src/`。于是 `<tl_templates/cuda/reduce.h>` 这种 include 路径自洽。

「C++ 编译期」入口：

[CMakeLists.txt:359-362](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/CMakeLists.txt#L359-L362) 把 `src/` 插到 `TILE_LANG_INCLUDES` 最前，[第 537 行](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/CMakeLists.txt#L537) `target_include_directories(tilelang_objs PRIVATE ${TILE_LANG_INCLUDES})` 让 C++ 目标也能 include 这些模板（部分 runtime/stub 代码会用到）。

接着看几个典型模板头，它们就是 `get_kernel_source()` 里那些 `tl::` 调用的真实定义：

[src/tl_templates/cuda/reduce.h:14](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/tl_templates/cuda/reduce.h#L14) 声明 `warp_reduce`；[第 238-265 行](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/tl_templates/cuda/reduce.h#L238-L265) 的 `AllReduce` 模板结构体是跨线程归约的核心（`T.reduce_*` 经 LowerTileOp 展开后调用的就是它，对应 u3-l2 讲过的 AllReduce）。

[src/tl_templates/cuda/copy.h:7-35](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/tl_templates/cuda/copy.h#L7-L35) 定义 `cp_async_commit`/`cp_async_wait`/`cp_async_gs<N>`，用内联汇编发射 `cp.async.cg.shared.global`——这是 global→shared 异步搬运的底层，被 Copy 的 C++ lowering 与 `ptx_async_copy_injector` Pass 共同使用。

[src/tl_templates/cuda/instruction/mma.h:1-10](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/tl_templates/cuda/instruction/mma.h#L1-L10) include 了 CuTe 的 `mma_sm75.hpp`/`mma_sm80.hpp`，是 `GemmMMA` 展开出的 `mma_sync` 调用的模板包装；同目录还有 `wgmma.h`、`tcgen05mma.h`、`mma_sm70.h`、`mma_sp.h` 分别对应不同指令键。

最后别忘了 include 的「决策点」还是 [src/cuda/codegen/codegen_cuda.cc:660-738](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/cuda/codegen/codegen_cuda.cc#L660-L738) 的 `Finish()`——它把上面这些头按 `need_*` 标志拼进源码。

#### 4.4.4 代码实践

**目标**：在生成的 kernel 源码里找到模板调用片段，回溯到模板头与触发它的 Pass。

**操作步骤**：

1. 用下面的综合实践编译一个 GEMM，`print(kernel.get_kernel_source())`。
2. 在源码里搜索 `tl::` 命名的调用（如 `tl::cp_async`、`tl::AllReduce`、`tl::warp_reduce_sum`）。
3. 对每个找到的片段，回溯：调用 → 模板头（reduce.h / copy.h / instruction/mma.h）→ 触发它的 Pass。

**需要观察的现象**：
- `tl::cp_async_gs<16>(...)` 来自 [copy.h](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/tl_templates/cuda/copy.h)，触发链路是 `T.copy` → LowerTileOp（`LowerParallelLoop`）→ `ptx_async_copy_injector` → `cp_async` intrinsic → codegen 打印。
- 若 kernel 含归约，`tl::AllReduce<...>::run(...)` 来自 [reduce.h](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/tl_templates/cuda/reduce.h)，触发链路是 `T.reduce_*` → LowerTileOp → `AllReduce` intrinsic → codegen 打印。
- `mma_sync`/wgmma 来自 [instruction/mma.h](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/tl_templates/cuda/instruction/mma.h) 等，触发链路是 `T.gemm` → 4.2/4.3 的 Lower → Python 实现类发射 → codegen 打印。

**预期结果**：能为源码里至少两类 `tl::` 调用标注「来源模板头 + 来源 Pass + 来源 .cc/.py 文件」。无 GPU 时 `get_kernel_source()` 仍可用（走 `build_without_compile`），标注「待本地验证」仅指性能部分。

#### 4.4.5 小练习与答案

**练习 1**：如果你把 `TILELANG_TEMPLATE_PATH` 指向一个空目录，编译会在哪一步失败？
**答案**：在 `tilelang_callback_cuda_compile` 跑 nvcc 时失败——nvcc 找不到 `<tl_templates/cuda/reduce.h>` 等头，报 `fatal error: ... No such file or directory`。lower/codegen 本身不会失败，因为它们只写 `#include` 字符串。

**练习 2**：reduce.h 里 `AllReduce` 是模板结构体而非普通函数，这样设计有什么好处？
**答案**：用模板参数（`Reducer`、`threads`、`scale`、`Barrier`）在编译期生成针对特定归约算子、线程数、同步方式的具体代码，便于编译器内联与展开，避免运行时分支；这也是它需要 C++20 显式 lambda 模板参数的原因。

---

## 5. 综合实践

把四节串起来：编译一个 GEMM，拿到它的设备源码，逐段标注「来源」。以下是示例代码（基于 [examples/gemm/example_gemm.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/examples/gemm/example_gemm.py)）：

```python
# 示例代码（基于 examples/gemm/example_gemm.py）
import tilelang
import tilelang.language as T

@tilelang.jit
def matmul(A, B, block_M, block_N, block_K, dtype=T.float16, accum_dtype=T.float32):
    M, N, K = T.const("M, N, K")
    A: T.Tensor((M, K), dtype)
    B: T.Tensor((K, N), dtype)
    C = T.empty((M, N), dtype)
    with T.Kernel(T.ceildiv(N, block_N), T.ceildiv(M, block_M), threads=128) as (bx, by):
        A_shared = T.alloc_shared((block_M, block_K), dtype)
        B_shared = T.alloc_shared((block_K, block_N), dtype)
        C_local = T.alloc_fragment((block_M, block_N), accum_dtype)
        T.clear(C_local)
        for k in T.Pipelined(T.ceildiv(K, block_K), num_stages=3):
            T.copy(A[by * block_M, k * block_K], A_shared)
            T.copy(B[k * block_K, bx * block_N], B_shared)
            T.gemm(A_shared, B_shared, C_local)
        T.copy(C_local, C[by * block_M, bx * block_N])
    return C

kernel = matmul.compile(M=1024, N=1024, K=1024, block_M=128, block_N=128, block_K=32)
src = kernel.get_kernel_source()
print(src)
```

拿到 `src` 后，按下面的表格逐项核对（这是本讲四节内容的「索引」）：

| 源码片段 | 来源模板头 | 触发它的 Pass / 代码 | 本讲章节 |
| --- | --- | --- | --- |
| 顶部 `#include <tl_templates/cuda/reduce.h>` 等 | reduce.h / scan.h / ldsm.h | `Finish()` 无条件 include | 4.1 / 4.4 |
| `#include <tl_templates/cuda/instruction/mma.h>` | instruction/mma.h | `need_mma_instruction_h_`（`Finish()` 条件 include） | 4.4 |
| `mma_sync(...)` 或 wgmma 内联汇编 | instruction/mma.h / wgmma.h | `T.gemm` → `LowerTileOp` → `tl.gemm.lower`（Python `GemmMMA`/`GemmWGMMA`） | 4.2 / 4.3 |
| `cp.async.cg.shared.global` 或 `tl::cp_async_*` | copy.h / copy_sm90.h | `T.copy` → `LowerTileOp`（`LowerParallelLoop`）→ `ptx_async_copy_injector` | 4.4 |
| 整段源码由谁产出 | —— | `BuildTileLangCUDA` → `CodeGenTileLangCUDA` → nvcc（`tilelang_callback_cuda_compile`） | 4.1 |

**进阶**：把 `threads=128` 的 GEMM 在 `target="cuda --arch=sm_80"` 与 `"cuda --arch=sm_90a"` 下分别编译（待本地验证），对比 `SelectInst` 选出的指令键（`cuda.mma` vs `cuda.wgmma`）如何改变源码里的矩阵乘片段与 include 的指令头。

## 6. 本讲小结

- **device codegen 是最后一棒**：`CodeGenTileLangCUDA` 继承 `CodeGenC`，用访问者模式把已展开的 device TIR 打印成 CUDA C++，并在 `Finish()` 按 `need_*` 标志拼装 `tl_templates` 头文件。
- **两条产出路径**：`BuildTileLangCUDA`（真编译，nvcc 出 cubin）与 `BuildTileLangCUDAWithoutCompile`（只出源码，供 `get_kernel_source()`），均注册为 `target.build.tilelang_cuda(_without_compile)` 全局函数。
- **tile op 靠属性表识别**：`ParseOperator` 查 `TLOpBuilder` 把 `tl.tileop.*` 占位构造成 `XxxNode`；`LowerTileOp` Pass 在 `EvaluateNode` 处调 `tile_op->Lower()` 展开它。
- **GEMM 是混合 lowering、Copy 是纯 C++ lowering**：`GemmNode::Lower` 回调 Python `tl.gemm.lower`；`CopyNode::Lower` 全程在 C++ 用函数指针表。
- **两段式 GEMM 分发**：C++ 注册表（`SelectInst`）选指令键 + warp 切分，Python 注册表（`resolve_gemm_impl`）按键选实现类发射指令，衔接点是 FFI `tl.GemmGetGemmInstructionKey`。
- **模板注入走 `-I`**：运行时 `tilelang_callback_cuda_compile` 加 `-I $TILELANG_TEMPLATE_PATH`（默认 `src/`），C++ 编译期用 CMake 的 `TILE_LANG_INCLUDES`，二者都指向 `src/tl_templates/`。

## 7. 下一步学习建议

- 想了解生成的二进制如何被包装成可调用对象、与 PyTorch tensor 对接，请读 **u7-l1（执行后端与 kernel adapter）** 与 **u7-l2（host/device 拆分与编译回调）**——后者会深入 `tilelang_callback_cuda_compile` 的 nvcc 调用细节。
- 想自己加一个新 tile op、新 Pass 或移植到新后端，请读 **u10-l2（扩展 TileLang）**，它会用到本讲的 `TIR_REGISTER_TL_TILE_OP`、`RegisterGemmImpl` 与 device codegen 注册表。
- 想可视化观察 codegen 前后的 IR 差异，可配合 **u9-l1（调试工具：lower trace）** 的 pass_visualizer，把 `LowerTileOp` 与 device codegen 之间的每一步 IR 都 diff 出来。

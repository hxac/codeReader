# 编译 pass / transform 体系

> 承接 [u5-l1 C++ 编译器核心总览](./u5-l1-cpp-compiler-overview.md)：上一讲我们从「鸟瞰」视角认识了 `src/` 的双层布局与 `TileOperatorNode` 的两个核心方法 `Lower()`/`InferLayout()`。本讲往下钻一层，看这些抽象在**哪条流水线上、以什么顺序**被调用——也就是 `src/transform/` 里的各个 pass。

## 1. 本讲目标

学完本讲，你应当能够：

- 说出 TileLang 设备端 lowering 主流水线的**执行顺序**，并能把 `LowerTileOp`、`SplitHostDevice`、`MaterializeKernelLaunch`、`StorageRewrite` 放到正确位置。
- 解释 **`LowerTileOp`** 如何把 `T.copy`/`T.gemm` 这类高层 tile 算子替换成底层 TIR（`cp.async`/`mma` 等），并把 layout 推断结果「刷」进所有 buffer 访问。
- 解释 **`SplitHostDevice`** 的输入输出：它吃进怎样的 IRModule、吐出怎样的 host/device 两份 IR。
- 解释 **`MaterializeKernelLaunch`** 如何把 `T.Kernel` 留下的「与 target 无关的线程绑定帧」物化成各后端的形式（GPU 的 `thread_extent`、CPU 的串行 `for`）。
- 区分**循环优化 pass**（向量化/unswitch/unroll）与**存储优化 pass**（`StorageRewrite`、`MergeSharedMemoryAllocations`），并理解寄存器/shared memory 复用各自的归属。

## 2. 前置知识

本讲假设你已掌握下面这些概念（前三讲已建立）：

- **pass 流水线（pipeline）**：编译器把一个 IRModule 经过一连串「变换函数」逐次改写，每个变换叫一个 pass。TVM/TireLang 的 pass 是可组合的，且按 target 不同选择不同流水线（见 [u4-l1](./u4-l1-lowering-pipeline.md)）。
- **TIR（Tensor IR）**：基于 TVM 的中间表示，由 `PrimFunc`、`Buffer`、`For`、`AttrStmt`、`BufferLoad`/`BufferStore` 等节点构成的 AST。
- **tile 算子（TileOperatorNode）**：`T.copy`/`T.gemm` 等在 IR 里以 `tl.tileop.*` intrinsic 调用出现，`LowerTileOp` 负责把它们降级（见 [u5-l1](./u5-l1-cpp-compiler-overview.md)）。
- **fragment 与 layout 推断**：`alloc_fragment` 分配的 tile，其逻辑坐标到物理寄存器的映射由 `LayoutInference` 推断，结果挂在 SBlock 的 `layout_map` 注解上（见 [u4-l3](./u4-l3-layout-inference.md)）。
- **host/device 拆分**：一份 kernel 最终要变成「host 上发起 kernel 调用」+「device 上真正的核函数」两部分（见 [u4-l1](./u4-l1-lowering-pipeline.md)）。
- **warp_size**：MACA 为 64、CUDA 为 32，是各后端差异的显眼标志（见 [u1-l3](./u1-l3-repo-layout.md)）。

本讲新增术语：**生存期（liveness）/gen-kill**、**inplace**、**thread_extent 属性语句**、**lower_thread_binding 开关**。

## 3. 本讲源码地图

本讲聚焦 `src/transform/` 下四个 pass，并以上层 Python 流水线为「指挥棒」串起它们的顺序。

| 文件 | 角色 | 本讲定位 |
|------|------|---------|
| [src/transform/lower_tile_op.cc](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/lower_tile_op.cc) | 把 tile 算子降级为底层 TIR，并把 layout 刷进 buffer | 核心降级 pass |
| [src/transform/split_host_device.cc](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/split_host_device.cc) | 把设备区抽成独立 device PrimFunc | host/device 拆分 |
| [src/transform/materialize_kernel_launch.cc](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/materialize_kernel_launch.cc) | 把 `T.Kernel` 的线程绑定帧物化为后端形式 | kernel launch 物化 |
| [src/transform/storage_rewrite.cc](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/storage_rewrite.cc) | 生存期分析 + 内存复用/合并 | 存储优化 |
| [tilelang/cuda/pipeline.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/pipeline.py) | CUDA 设备流水线（pass 顺序的权威来源） | 顺序参考 |
| [tilelang/maca/pipeline.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/pipeline.py) | MACA 设备流水线（CUDA 骨架 + `LowerMACAIntrin`） | 顺序参考 |

> 关键认知：**C++ 侧只实现「单个 pass」，pass 的执行顺序由 Python 侧的流水线函数编排**。引擎 `tilelang.engine.lower` 通过 `resolve_pipeline` 取到对应 target 的流水线（如 `CUDAPassPipelineBody`/`MACAPassPipelineBody`），逐条 `mod = pass(mod)` 串联执行。所以「顺序」要看 Python 流水线，不是 C++。

### 流水线全景（按执行顺序的关键 pass）

下面这张表摘自 `CUDAPassPipelineBody`（[tilelang/cuda/pipeline.py:68-254](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/pipeline.py#L68-L254)），只保留本讲关心的骨干，其余 `Simplify` 等清理 pass 省略：

| 阶段 | 代表 pass | 作用 |
|------|-----------|------|
| 绑定 target | `BindTarget` | 给 PrimFunc 打上 target 属性 |
| **kernel launch 物化** | `MaterializeKernelLaunch` | `T.Kernel` 帧转成 `thread_extent` |
| 软件流水 | `PipelinePlanning`→`InjectSoftwarePipeline` | 多版本化 + 三段拆分 |
| 布局推断 | `LayoutInference` | 推断 fragment 寄存器布局 |
| **tile 算子降级** | `LowerTileOp` | `tl.tileop.*` → 底层 TIR |
| 安全/访问修正 | `LegalizeSafeMemoryAccess`/`LowerAccessPtr` | 边界守卫、指针归一化 |
| 展平缓冲 | `FlattenBuffer`/`ConfigIndexBitwidth` | 多维 buffer → 一维 + 索引位宽 |
| **循环优化** | `VectorizeLoop`→`LoopUnswitching`→`UnrollLoop` | 向量化/循环外提/展开 |
| **存储优化** | `StorageRewrite` | 寄存器生存期复用 |
| 标记设备区 | `AnnotateDeviceRegions` | 用 `AttrStmt(kTarget)` 包住设备区 |
| **host/device 拆分** | `SplitHostDevice` | 抽出独立 device PrimFunc |
| shared 合并 | `MergeSharedMemoryAllocations` | 多个 shared alloc → 一块动态显存 |
| 线程同步 | `ThreadSync("shared")` | 插入 `__syncthreads` |
| 收尾 | `MakePackedAPI`→`LowerDeviceKernelLaunch` | 打包 API + 标记 kernel 调用约定 |

MACA 流水线（[tilelang/maca/pipeline.py:77-148](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/pipeline.py#L77-L148)）与 CUDA 同构，仅在三处不同：去掉了 warp-specialization/Blackwell/Hopper 专属 pass，并在 `LowerHopperIntrin` 之后多插一行 `tilelang.maca.transform.LowerMACAIntrin()`（[L124](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/pipeline.py#L124)）——这正是 metax 分支的核心印记之一。

## 4. 核心概念与源码讲解

### 4.1 LowerTileOp：把 tile 算子降级成底层 TIR

#### 4.1.1 概念说明

经过 `LayoutInference` 后，IR 里还保留着 `T.copy`/`T.gemm` 这些**高层 tile 算子**——它们在 TIR 中长成 `Evaluate(Call(tl.tileop.copy / tl.tileop.gemm, ...))`，只是「占位」的算子调用，并不能直接被 codegen 翻译成 CUDA/HIP/MACA 代码。

`LowerTileOp`（C++ 注册名 `tl.LowerTileOp`）的职责就是把这些占位调用**替换成真正的底层 TIR 语句**：

- `T.copy` → `cp.async`/TMA/普通 load-store 循环；
- `T.gemm` → `mma`/`wgmma`/`mfma` 等张量核指令序列；
- `T.clear`/`T.fill` → 清零/赋值循环。

同时它要把 `LayoutInference` 推出的 fragment 布局「刷」进**所有**对 buffer 的访问：fragment 的逻辑下标原本不对应物理寄存器，降级后必须改成 `(物理寄存器 buffer, 经 layout 变换后的下标)`。

#### 4.1.2 核心流程

`LowerTileOpPass` 继承 `arith::IRMutatorWithAnalyzer`，对 PrimFunc body 做一次遍历改写，大致流程：

```text
Substitute(f):
  1. 从 buffer_map 建立  data_var -> Buffer  的查找表
  2. 取 target 属性（必须有）
  3. 遍历 body（StmtMutator）：
       进入 SBlock:
         - 读 layout_map 注解（LayoutInference 产物）
         - 对每个带 layout 的 buffer：makeBufferWithLayout
              fragment -> local 寄存器 buffer（改 storage scope）
              用 layout->OutputShape() 算出新形状
         - 建立 buffer_remap / layout_map
       遇到 Evaluate(Call(tl.tileop.*)):
         - ParseOperator 识别出是哪个 tile 算子
         - 组装 LowerArgs（target、线程范围、thread_var、layout_map、
                         workspace/mbarrier/smem-alignment 回调）
         - tile_op->Lower(...)  => 返回底层 TIR Stmt
       BufferLoad/BufferStore:
         - 若 buffer 在 buffer_remap 中：用 layout->Forward() 重算下标
  4. RemapBufferRewriter + LayoutRemapRewriter 收尾改写
  5. 写回 kHasTMA、kSmemAlignmentMap 属性；必要时注入 mbarrier buffer
```

#### 4.1.3 源码精读

**pass 入口与注册**（[src/transform/lower_tile_op.cc:1586-1596](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/lower_tile_op.cc#L1586-L1596)）：它是一个 `PrimFunc` 级 pass，每个函数独立改写，名字是 `tl.LowerTileOp`。

```cpp
tvm::transform::Pass LowerTileOp() {
  auto pass_func = [=](PrimFunc f, const IRModule &m, const PassContext &ctx) {
    return LowerTileOpPass::Substitute(std::move(f));
  };
  return CreatePrimFuncPass(pass_func, 0, "tl.LowerTileOp", {});
}
```

**SBlock 处理：把 layout 推断结果落地**（[src/transform/lower_tile_op.cc:337-358](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/lower_tile_op.cc#L337-L358)）：读 SBlock 的 `layout_map` 注解，对每个 buffer 调 `makeBufferWithLayout` 生成新 buffer（fragment 会被改成 `local` scope 的寄存器 buffer），并登记到 `buffer_remap_`：

```cpp
if (op->annotations.count(attr::kLayoutMap)) {
  auto layout_map = op->annotations.at(attr::kLayoutMap)
                        .as<Map<Buffer, Layout>>().value();
  for (auto [buffer, layout] : layout_map) {
    buffer_remap_.Set(buffer, makeBufferWithLayout(buffer, layout, var_remap_));
    layout_map_.Set(buffer, layout);
  }
}
```

**核心降级点：识别 tile 算子并调用 `Lower()`**（[src/transform/lower_tile_op.cc:1143-1224](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/lower_tile_op.cc#L1143-L1224)）：每个 `Evaluate` 节点都用 `ParseOperator` 试着解析成一个 tile 算子；解析成功就组装 `LowerArgs` 并调用 `tile_op->Lower()`，把返回的底层 TIR 再交给 mutator 继续遍历。这一处就是 [u5-l1](./u5-l1-cpp-compiler-overview.md) 所说的「`Lower()` 由 `LowerTileOp` 驱动」的接合点：

```cpp
auto tile_op = ParseOperator(GetRef<Stmt>(op));
if (!tile_op.defined())
  return IRMutatorWithAnalyzer::VisitStmt_(op);
// ... 组装 LowerArgs（含 target、thread_bounds、layout_map、回调） ...
auto lowered = tile_op->Lower(lower_args, analyzer_);
return IRMutatorWithAnalyzer::VisitStmt(lowered);
```

注意 `LowerArgs` 里塞了好几个**回调**（[L1161-L1219](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/lower_tile_op.cc#L1161-L1219)）：`add_workspace`（向当前 block 申请一块 `shared.dyn` workspace）、`alloc_mbarrier`（为 TMA 异步拷贝分配屏障）、`require_smem_alignment`（记录某块 shared memory 需要的对齐字节）。这些回调让各算子的 `Lower()` 不必关心「buffer 挂在哪」「对齐谁来收集」，只需声明需求。

**收尾：刷属性与注入屏障**（[src/transform/lower_tile_op.cc:248-259](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/lower_tile_op.cc#L248-L259)）：遍历结束后把 `has_tma_` 写成 `kHasTMA` 属性（供后续 `OptimizeForTarget` 选流水线分支），把收集到的 shared 对齐写成 `kSmemAlignmentMap`（供后续 `MergeSharedMemoryAllocations` 排布动态显存时遵守）。

```cpp
f = WithAttr(std::move(f), kHasTMA, Bool(substituter.has_tma_));
if (!substituter.smem_alignment_map_.empty()) {
  f = WithAttr(std::move(f), kSmemAlignmentMap, substituter.smem_alignment_map_);
}
```

#### 4.1.4 代码实践

**实践目标**：亲眼看到 `LowerTileOp` 前后 IR 的差异——`T.gemm`/`T.copy` 占位调用被替换成真正的底层语句。

**操作步骤**（源码阅读型 + 可选运行）：

1. 打开 `examples/gemm/example_gemm.py`，找到其中的 `T.gemm(...)` 与 `T.copy(...)`。
2. 在 `tilelang/engine/lower.py` 里定位 `LowerTileOp` 的调用（它在设备流水线里，由 `resolve_pipeline` 取到）。在它前后各打印一次 `mod.script()`。

   > 示例代码（仅示意插入位置，非项目原有代码）：
   > ```python
   > # 在 LowerTileOp 之前
   > print("=== BEFORE LowerTileOp ===\n", mod.script())
   > mod = tilelang.transform.LowerTileOp()(mod)
   > print("=== AFTER  LowerTileOp ===\n", mod.script())
   > ```

3. **需要观察的现象**：BEFORE 里能看到 `T.tl_tileop_gemm(...)`（或等价的 `tl.tileop.gemm` intrinsic 调用）与 copy 占位；AFTER 里这些占位消失，出现 `mma`/`wgmma`（CUDA）或 `tvm_mfma`（MACA）的 intrinsic 调用、以及 `cp.async`/load-store 循环。
4. **预期结果**：占位算子被替换；fragment buffer 的访问下标被 `layout->Forward()` 重算；shared buffer 可能多出 `replicate` 维。

5. 如果本地有 GPU：直接运行 `python examples/gemm/example_gemm.py`，并在 `tilelang/cuda/pipeline.py` 的 `LowerTileOp` 一行前后临时加 `print(mod.script())`（**注意：调试完务必还原，本讲禁止修改源码作为提交**）。无设备时此步为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `LowerTileOp` 必须在 `LayoutInference` **之后**运行？
> **答**：`LowerTileOp` 要把 layout 推断的结果（`layout_map` 注解）刷进 buffer 访问，并把 fragment 改写成寄存器 buffer。若 layout 还没推断，fragment 的逻辑下标无法映射到物理寄存器，算子的 `Lower()`（尤其 GEMM 的 warp 划分）也无从依据布局做指令选择。

**练习 2**：`LowerArgs` 里的 `require_smem_alignment` 回调收集的信息，最终被谁消费？
> **答**：被写成 PrimFunc 的 `kSmemAlignmentMap` 属性，随后在 `SplitHostDevice` 透传到 device kernel，最终由 `MergeSharedMemoryAllocations` 在排布合并后的动态 shared memory 时遵守这些对齐约束（见 [lower_tile_op.cc:252-259](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/lower_tile_op.cc#L252-L259) 与本讲 4.2/4.4）。

---

### 4.2 SplitHostDevice：host/device 拆分

#### 4.2.1 概念说明

一份 TileLang kernel 最终要跑成两部分：

- **device 函数**：真正的核函数，由 GPU/CPU 执行；
- **host 函数**：在 CPU 上负责「准备参数 + 发起 kernel 调用」。

`SplitHostDevice`（`tl.SplitHostDevice`）就是把两者**物理拆开**的 pass：它从 host PrimFunc 的 body 里把「设备区」整块抠出来，做成一个**独立的 device PrimFunc** 放进单独的 `device_mod` IRModule，再把原 body 里那块区域替换成一句「调用 device kernel」的 `Call`。

它前面紧挨着一个配套 pass `AnnotateDeviceRegions`（`tl.AnnotateDeviceRegions`）：负责先用 `AttrStmt(node=Target, key=kTarget)` 把设备区「框」出来，给 `SplitHostDevice` 一个明确的切割边界。

#### 4.2.2 核心流程

```text
前置：AnnotateDeviceRegions
  遍历 body，把 thread_extent / pipeline_exec_scope / device_scope 这些
  「只可能出现在 device 侧」的 AttrStmt，外层包一层 AttrStmt(Target, kTarget, body)

SplitHostDevice（模块级 pass，对 mod 里每个 PrimFunc）：
  HostDeviceSplitter 遍历 body：
    遇到第一个 AttrStmt(attr_key == kTarget):
      => 找到设备区，调用 SplitDeviceFunc
  SplitDeviceFunc(body, device_target):
    1. 收集 host 侧 assume 语句
    2. 用 VarUseDefAnalyzer 从设备 body 的 use-def 推断 device 参数
       （source kernel 走另一条：从 host 函数签名重建）
    3. 为每个参数新建 Var（避免与 host 共享 Var 对象，防 ConvertSSA 误改名）
    4. Substitute 替换 + 重映射 buffer
    5. 必要时为 CPU/ext_dev 加返回 int32 错误码 + AssertStmt
    6. 组装 device PrimFunc，挂 Target/kNoAlias/kIsGlobalFunc/cluster_dims/
       smem_alignment_map 等属性
    7. 取一个 GlobalVar（命名 <name>_kernel），把 device func 加进 device_mod
    8. 把原区域替换成 Evaluate(Call(global_var, args))
  最后对整个 mod 跑一次 ConvertSSA
```

#### 4.2.3 源码精读

**文件头与算法说明**（[src/transform/split_host_device.cc:50-56](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/split_host_device.cc#L50-L56)）原文清晰地总结了三步：

```cpp
// 1. Traverse AST and collect all assume statements into host_assumes_.
// 2. Until the first AttrStmtNode with tvm::attr::kTarget.
// 3. Call SplitDeviceFunc, which will create a new device function and replace
//    the original body with a call to that function.
```

**找到设备区**（[src/transform/split_host_device.cc:81-98](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/split_host_device.cc#L81-L98)）：遍历到 `AttrStmt(attr_key == kTarget)` 即认定进入设备区，调 `SplitDeviceFunc`；同时把沿途的 `tilelang_assume` 收集起来（它们必须仍在 host 侧）。

**抽函数的核心**（[src/transform/split_host_device.cc:269-311](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/split_host_device.cc#L269-L311)）：用 `VarUseDefAnalyzer` 从设备 body 推断参数（普通 kernel），或从 host 函数签名重建（source kernel，无 DSL body）；并为每个参数**新建同名 Var**，避免与 host 函数共享 Var 对象——注释点明这是为了让后续 `ConvertSSA` 不把两个函数的变量错误地视作同一个：

```cpp
// Create new parameter variables for the device function to avoid sharing
// Var objects with the host function. This prevents ConvertSSA from
// incorrectly renaming variables when it processes multiple functions.
```

**替换为调用**（[src/transform/split_host_device.cc:390-415](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/split_host_device.cc#L390-L415)）：device func 加进 `device_mod` 后，原区域被替换成一句 `Evaluate(Call(kernel_symbol_global, args))`；若是 CPU/ext_dev 这类能把错误码返回 host 的 target，则替换成 `Bind(error_code, call) + AssertStmt` 以传播运行时错误。

**模块级 pass 编排**（[src/transform/split_host_device.cc:473-505](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/split_host_device.cc#L473-L505)）：它建一个空 `device_mod`，遍历 `mod->functions` 逐个拆分，最后 `mod->Update(device_mod)` 合并、并跑 `ConvertSSA` 收尾。注意 kernel 命名约定 `<global_symbol>_kernel`。

#### 4.2.4 代码实践

**实践目标**：讲清 `SplitHostDevice` 的**输入输出**（这也是本讲综合实践的一部分）。

**操作步骤**（源码阅读型）：

1. 先读 [src/transform/annotate_device_regions.cc:46-61](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/annotate_device_regions.cc#L46-L61)，确认设备区是「被 `AttrStmt(kTarget)` 框起来」的。
2. 读 [split_host_device.cc:426-469](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/split_host_device.cc#L426-L469)（`SplitHostDevice` 自由函数），看它如何把 `cluster_dims`、`kSmemAlignmentMap`、`kNonRestrictParams` 等属性从 host 函数**搬到** device kernel。

**需要观察的现象 / 预期结果**（请你自己用一句话填出下表的输入输出，答案见 4.2.5）：

| | 输入 | 输出 |
|---|------|------|
| `mod`（host 侧） | 每个 PrimFunc body 内含 `AttrStmt(kTarget, device_target, <kernel body>)` 设备区 | device 区被替换为 `Call(<name>_kernel, args)` |
| `device_mod`（新增） | 空 | 装着抽出来的 device PrimFunc，自带 Target/kNoAlias/kIsGlobalFunc 等属性 |

3. **预期结果**：拆分后 `mod` 里 host 函数只剩「调用 kernel」；真正的核函数体跑到 `device_mod` 里，且携带了 smem 对齐、cluster 维度等 codegen 需要的属性。

> 待本地验证：在 `tilelang/cuda/pipeline.py` 的 `SplitHostDevice` 一行前后打印 `mod.functions` 的键，可看到拆分后多出一个 `<...>_kernel` 的 device 函数。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `SplitHostDevice` 要为 device 参数**新建同名 Var**，而不是直接复用 host 的 Var？
> **答**：复用会让 host 与 device 函数共享同一个 `VarNode` 对象，后续 `ConvertSSA` 处理多个函数时会把它当作同一个变量做重命名，破坏正确性。新建同名 Var + Substitute 既保持语义一致，又让两函数的变量对象相互独立（见 [split_host_device.cc:296-310](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/split_host_device.cc#L296-L310) 的注释）。

**练习 2**：`SplitHostDevice` 必须在 `MergeSharedMemoryAllocations` **之前**，为什么？
> **答**：`MergeSharedMemoryAllocations` 把多个 shared alloc 合并成「device 函数开头的一块动态显存」。它要求合并点在**每个 device 函数的开头**——而 device 函数正是 `SplitHostDevice` 抽出来的。CUDA 流水线的注释也明确写了 "MergeSharedMemoryAllocations must be applied after SplitHostDevice"（[cuda/pipeline.py:220-221](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/pipeline.py#L220-L221)）。

---

### 4.3 MaterializeKernelLaunch：kernel launch 物化

#### 4.3.1 概念说明

还记得 [u2-l2](./u2-l2-kernel-launch-context.md) 讲过：`with T.Kernel(*blocks, threads=...)` 在 trace 时会变成一串 `For` 循环，其 `ForKind` 是 `kThreadBinding`，`thread_binding` 标签是 `blockIdx.*`/`threadIdx.*`。这套帧是**与 target 无关**的「中间表示」——它只表达了「这里有一个 grid/thread 维度」，但还没决定「到底变成 CUDA 的 `thread_extent` 属性，还是 CPU 的普通 `for`」。

`MaterializeKernelLaunch`（`tl.MaterializeKernelLaunch`）就是这个「决定」动作，它在流水线里紧跟 `BindTarget` 之后执行（[cuda/pipeline.py:69-70](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/pipeline.py#L69-L70)）。

#### 4.3.2 核心流程

pass 接收一个布尔开关 `lower_thread_binding`（Python 默认 `True`，见 [tilelang/transform/__init__.py:201](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/transform/__init__.py#L201)；CPU 流水线显式传 `False`，见 [tilelang/cpu/pipeline.py:18](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cpu/pipeline.py#L18)）：

```text
KernelLaunchMaterializer 遍历 body，遇到最外层连续的 launch 绑定嵌套（ConvertNest）：
  若 lower_thread_binding == True（SIMT 后端：CUDA/ROCm/MACA/Metal）：
     每个 For(blockIdx.x/threadIdx.x, extent) 
       => AttrStmt(IterVar(tag), thread_extent, extent, body)
     复用原 loop_var，保证 body 里的引用仍有效。
  若 lower_thread_binding == False（无 SIMT，如 CPU）：
     blockIdx.*  => 普通串行 For，extent 不变；
     threadIdx.* => 单次串行 For（extent 强制为 1，循环变量钉在 0），
                    原 threads=128 的请求被丢弃。
  只转换最外层连续嵌套；更深处的 thread_binding 留给 LowerOpaqueBlock 处理。
```

#### 4.3.3 源码精读

**文件头是最好的教材**（[src/transform/materialize_kernel_launch.cc:1-22](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/materialize_kernel_launch.cc#L1-L22)）原文说明了两种后端的行为与「只转最外层」的约定，建议直接通读。

**识别绑定类型**（[src/transform/materialize_kernel_launch.cc:40-56](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/materialize_kernel_launch.cc#L40-L56)）：靠 `thread_binding` 的 tag 前缀区分 `blockIdx.` 与 `threadIdx.`：

```cpp
bool IsBlockBinding(const ForNode *op) {
  ...
  return tag.rfind("blockIdx.", 0) == 0;
}
bool IsThreadBinding(const ForNode *op) {
  ...
  return tag.rfind("threadIdx.", 0) == 0;
}
```

**两种物化分支**（[src/transform/materialize_kernel_launch.cc:73-96](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/materialize_kernel_launch.cc#L73-L96)）：SIMT 走 `AttrStmt(thread_extent)`；CPU 把 thread 维退化成 `extent=1` 的串行循环：

```cpp
if (lower_thread_binding_) {
  ...
  return AttrStmt(iter_var, tirx::attr::thread_extent, op->extent, body);
}
// No SIMT: thread dims are ignored (a unit loop keeps the loop var pinned to 0).
PrimExpr extent = IsThreadBinding(op) ? PrimExpr(IntImm(..., 1)) : op->extent;
return For(op->loop_var, op->min, std::move(extent), ForKind::kSerial, body, ...);
```

#### 4.3.4 代码实践

**实践目标**：验证同一个 `T.Kernel` 在 GPU 与 CPU 后端下被物化成不同形态。

**操作步骤**（源码阅读 + 可选运行）：

1. 读 [materialize_kernel_launch.cc:103-113](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/materialize_kernel_launch.cc#L103-L113) 确认 pass 名 `tl.MaterializeKernelLaunch` 与签名。
2. 对比两处调用：GPU 流水线 [cuda/pipeline.py:70](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/pipeline.py#L70)（默认 `True`）与 CPU 流水线 [cpu/pipeline.py:18](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cpu/pipeline.py#L18)（显式 `False`）。

**需要观察的现象 / 预期结果**：

- GPU：body 里出现 `attr [IterVar(threadIdx.x, ...)] thread_extent = 128` 这类属性语句——这就是 codegen 后续生成 `__global__` + `<<<grid,block>>>` 的依据。
- CPU：`threadIdx.*` 变成 `extent=1` 的串行 `for`（变量恒为 0），`blockIdx.*` 变成遍历 grid 的串行 `for`——于是同一份 kernel 在 CPU 上「串行跑完所有 tile」。

3. 待本地验证：用 `target="c"` 编译一个 GEMM，打印 `MaterializeKernelLaunch` 后的 `mod.script()`，确认没有 `thread_extent`，只有串行 `for`。

#### 4.3.5 小练习与答案

**练习 1**：为什么 CPU 后端不直接删掉 `threadIdx.*` 循环，而是保留一个 `extent=1` 的单次循环？
> **答**：循环变量 `threadIdx.x` 在 kernel body 里可能被引用（如 `thread_idx % warp_size`）。直接删除会让该变量未定义；保留 `extent=1` 的串行循环、把变量钉在 0，既消除了多线程语义，又让变量仍有定义（见 [materialize_kernel_launch.cc:88-95](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/materialize_kernel_launch.cc#L88-L95) 注释）。

**练习 2**：为什么「只转换最外层连续的 launch 嵌套」？
> **答**：更深处（被 `tilelang_root` block 隔开的）的 `thread_binding` 循环属于 kernel body 内部的线程级逻辑，由 `LowerOpaqueBlock` 在它的常规阶段统一处理。本 pass 只管 `T.Kernel` 顶层的启动嵌套（见文件头注释 [L19-21](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/materialize_kernel_launch.cc#L19-L21)）。

---

### 4.4 存储优化：StorageRewrite 与生存期复用

#### 4.4.1 概念说明

降级到这一步，IR 里已经有大量 `AllocBuffer`（寄存器/local、shared）。如果每个临时 buffer 都各占一块显存/寄存器，开销会很大。`StorageRewrite`（`tir.StorageRewrite`，但 tilelang 用自己的实现，注册名 `tl.transform.StorageRewrite`）通过**生存期分析（liveness analysis）**找出「活期不重叠」的 buffer，让它们**共用同一块后端存储**——这是经典的寄存器分配/栈分配思想。

它的核心思想可以用生存期来刻画。设某 buffer \(b\) 的分配点为 \(\text{gen}(b)\)、最后一次使用为 \(\text{kill}(b)\)，则两个 buffer \(A, B\) 能共享同一块后端数组，当且仅当它们的生存期不重叠：

\[
\text{kill}(A) \le \text{gen}(B) \quad \text{或} \quad \text{kill}(B) \le \text{gen}(A)
\]

> 重要边界：在 tilelang 里 `StorageRewrite` 的 `enable_reuse` **被硬编码为 `false`**（见下方源码）。**寄存器**层面的复用最终交给 nvcc/后端编译器，**shared memory** 的复用/合并则由专门的 `MergeSharedMemoryAllocations` 负责。所以 `StorageRewrite` 在本流水线里主要做：指针类型改写、inplace 检测（可选）、以及把分配点 attach 到正确作用域。

#### 4.4.2 核心流程

`StoragePlanRewriter::Rewrite` 分四步：

```text
1. LinearAccessPatternFinder：把语句树线性化成一串「作用域点」
     （每个嵌套 scope 用 before/after 两个点表示），记录每条语句 touch 了哪些 buffer 变量。
2. LivenessAnalysis：在线性序列上做 gen/kill，求出每个 buffer 变量的生存期。
3. PlanMemory：把生存期不重叠的分配归并进同一个 StorageEntry（共享后端数组）；
     支持按 bits_offset 把多个分配折叠进同一块（按位宽/对齐切片）；
     可选 inplace 检测（detect_inplace，默认关）。
4. PrepareNewAlloc + 改写：把 AllocBuffer/BufferLoad/BufferStore 改写到
     合并后的 backing array 与新下标。
```

#### 4.4.3 源码精读

**文件头定位职责**（[src/transform/storage_rewrite.cc:20-24](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/storage_rewrite.cc#L20-L24)）：「Memory access pattern analysis and optimization. Re-write data access to enable memory sharing when possible.」

**四步主流程**（[src/transform/storage_rewrite.cc:438-457](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/storage_rewrite.cc#L438-L457)）：`finder` → `LivenessAnalysis` → `PlanMemory` → `PrepareNewAlloc` → 改写：

```cpp
LinearAccessPatternFinder finder;
finder(stmt);
this->LivenessAnalysis(finder.linear_seq_);
this->PlanMemory(finder.linear_seq_, finder.alloc_info_, enable_reuse,
                 reuse_require_exact_matched_dtype);
...
this->PrepareNewAlloc();
stmt = operator()(std::move(stmt));   // 真正改写
```

**生存期分析的线性化**（[src/transform/storage_rewrite.cc:93-107](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/storage_rewrite.cc#L93-L107)）：`LinearAccessPatternFinder` 把复合作用域（循环/线程启动/If）用 before/after 两点表示，访问记录在 after_scope 点，从而把树压成可做 gen/kill 的线性序列。

**共享条目的关键字段**（[src/transform/storage_rewrite.cc:620-655](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/storage_rewrite.cc#L620-L655)）：`StorageEntry` 里的 `attach_scope_`（分配挂在哪个作用域：shared/local 挂在 thread_extent 开头，global 挂在最外层）、`bits_offset`（折叠进同一块时的位偏移，按 bits 而非 bytes 以支持硬件特殊索引与跨类型共享）。

**pass 注册与 reuse 关闭**（[src/transform/storage_rewrite.cc:1930-1954](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/storage_rewrite.cc#L1930-L1954)）：注意 `enable_reuse = false` 及其注释——shared 复用归 `MergeSharedMemoryAllocations`，寄存器复用归 nvcc：

```cpp
bool enable_reuse = false;
// Always disable reuse currently, for shared memory reuse we depend on
// MergeSharedMemoryAllocations pass, for register reuse we depend on nvcc
// or other compiler itself.
```

#### 4.4.4 代码实践

**实践目标**：在 IR 层面看到 `StorageRewrite` 前后分配形态的变化（attach 点与指针类型改写）。

**操作步骤**（源码阅读 + 可选运行）：

1. 在 [tilelang/cuda/pipeline.py:186](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/pipeline.py#L186) 的 `StorageRewrite` 一行前后各打印 `mod.script()`。
2. 读 [storage_rewrite.cc:148-175](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/storage_rewrite.cc#L148-L175)，理解 `BufferStore` 访问如何被记入线性序列（attach 到分配所在 level）。

**需要观察的现象 / 预期结果**：

- BEFORE：`FlattenBuffer` 之后是大量一维 `allocate`；AFTER：分配点被 attach 到正确作用域（shared/local 挂到 thread_extent 之下），指针类型经 `PointerValueTypeRewrite` 改写。
- 因为 `enable_reuse=false`，你**不会**看到两个临时 buffer 被合并成一块——那是 shared memory 的事，要去看 `MergeSharedMemoryAllocations`（[cuda/pipeline.py:224](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/pipeline.py#L224)）的效果。

3. 待本地验证：有 GPU 时跑 GEMM 并对比前后 `mod.script()`；无设备则纯源码阅读。

#### 4.4.5 小练习与答案

**练习 1**：既然 `StorageRewrite` 关掉了 reuse，那 tilelang 的 shared memory 复用靠谁？
> **答**：靠 `MergeSharedMemoryAllocations`。它运行在 `SplitHostDevice` 之后（合并点必须在 device 函数开头），把多个 shared alloc 合并成一块动态 shared memory，并遵守 `LowerTileOp` 收集、`SplitHostDevice` 透传过来的 `kSmemAlignmentMap` 对齐约束。

**练习 2**：`bits_offset` 为什么用「位（bit）」而不是「字节（byte）」做偏移单位？
> **答**：为了支持硬件的特殊索引方式，并在合并不同 dtype 的 buffer 时仍能按各自的 `max_simd_bits` 对齐切片，从而让不同类型的分配也能安全共享同一后端数组（见 [storage_rewrite.cc:643-654](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/storage_rewrite.cc#L643-L654) 注释）。

---

## 5. 综合实践

**任务**（对应本讲 practice_task）：阅读本讲的四个 pass，**按执行顺序**列出 lowering 主流水线的关键 pass，并说明 `SplitHostDevice` 的输入输出。

### 第 1 步：排出关键 pass 的执行顺序

以 [tilelang/cuda/pipeline.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/pipeline.py) 为权威，把下列 pass 填入正确序号（参考答案见下）：

```
( ) LowerTileOp
( ) SplitHostDevice
( ) MaterializeKernelLaunch
( ) StorageRewrite
( ) AnnotateDeviceRegions
( ) LayoutInference
( ) MergeSharedMemoryAllocations
( ) VectorizeLoop
```

**参考顺序**（行号均在 [cuda/pipeline.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/pipeline.py)）：

1. `MaterializeKernelLaunch`（[L70](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/pipeline.py#L70)，紧跟 `BindTarget`）
2. `LayoutInference`（[L113](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/pipeline.py#L113)）
3. `LowerTileOp`（[L117](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/pipeline.py#L117)）
4. `VectorizeLoop`（[L185](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/pipeline.py#L185)）
5. `StorageRewrite`（[L186](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/pipeline.py#L186)）
6. `AnnotateDeviceRegions`（[L212](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/pipeline.py#L212)）
7. `SplitHostDevice`（[L213](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/pipeline.py#L213)）
8. `MergeSharedMemoryAllocations`（[L224](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/pipeline.py#L224)）

> 从这个顺序能读出三条关键依赖：① `LowerTileOp` 必须在 `LayoutInference` 之后（要刷布局）；② `StorageRewrite` 必须在 `FlattenBuffer`/`VectorizeLoop` 之后（buffer 已一维化）；③ `SplitHostDevice` 必须在 `AnnotateDeviceRegions` 之后、`MergeSharedMemoryAllocations` 之前。

### 第 2 步：说明 `SplitHostDevice` 的输入输出

请合上讲义，用自己的话写出（然后对照 4.2）：

- **输入**（IRModule 形态）：每个 PrimFunc 的 body 里，设备区已被前置的 `AnnotateDeviceRegions` 用 `AttrStmt(node=Target, key=kTarget, <kernel body>)` 框住；host 函数还携带 `cluster_dims`、`kSmemAlignmentMap`、`kNonRestrictParams` 等需要搬到 device 侧的属性。
- **输出**（两份产物）：
  1. **host 侧 `mod`**：原设备区被替换成一句 `Evaluate(Call(<name>_kernel, args))`（CPU/ext_dev 则是 `Bind(error_code, call)+AssertStmt`）。
  2. **新增 `device_mod`**：装着抽出来的 device PrimFunc，自带 `Target`/`kNoAlias`/`kIsGlobalFunc`/`cluster_dims`/`smem_alignment_map` 等属性，命名为 `<global_symbol>_kernel`；整个模块最后跑一次 `ConvertSSA`。

### 第 3 步（进阶）：对比 MACA 差异

打开 [tilelang/maca/pipeline.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/pipeline.py)，找出它与 CUDA 流水线的三处结构差异（提示：warp-specialization、Blackwell/Hopper 专属 pass、`LowerMACAIntrin` 的位置）。这会为 [u7-l4 MACA 编译流水线](./u7-l4-maca-pipeline.md) 铺路。

## 6. 本讲小结

- **顺序看 Python，实现看 C++**：单个 pass 在 `src/transform/*.cc`，执行顺序由 `tilelang/<backend>/pipeline.py` 编排，引擎 `lower` 经 `resolve_pipeline` 取用。
- **`MaterializeKernelLaunch`** 紧跟 `BindTarget`，把 `T.Kernel` 的 target 无关线程绑定帧物化：SIMT 后端 → `thread_extent`，CPU → 串行 `for`（thread 维钉在 0）。
- **`LowerTileOp`** 在 `LayoutInference` 之后，把 `tl.tileop.*` 占位调用换成底层 TIR（`cp.async`/`mma`/`mfma`…），并把 fragment 布局刷进所有 buffer 访问；它还收集 TMA、smem 对齐、mbarrier 信息写成属性。
- **`SplitHostDevice`** 在 `AnnotateDeviceRegions` 之后，把设备区抽成独立 device PrimFunc 放进 `device_mod`，原区域换成 `Call(<name>_kernel)`；必须早于 `MergeSharedMemoryAllocations`。
- **存储优化**分两头：`StorageRewrite` 做生存期分析、attach 作用域、指针类型改写（`enable_reuse=false`）；shared memory 的复用/合并不归它，归 `MergeSharedMemoryAllocations`；寄存器复用归 nvcc/后端编译器。
- **循环优化**集中在 `VectorizeLoop`→`LoopUnswitching`→`UnrollLoop`，位于 `FlattenBuffer` 之后、`StorageRewrite` 前后。

## 7. 下一步学习建议

- 想看 `LowerTileOp` 调用的 `tile_op->Lower()` 到底生成了什么？继续读 [u5-l3 CUDA/HIP codegen 后端](./u5-l3-cuda-hip-codegen.md) 与 [u5-l4 tl_templates 模板下译](./u5-l4-tl-templates.md)，看 GEMM 的 `mma`/`wgmma` 模板如何被调用。
- 想理解 layout 是怎么推出来再被 `LowerTileOp` 消费的？回顾 [u4-l3 内存布局推断](./u4-l3-layout-inference.md)。
- 想看 MACA 流水线的专属 pass（`LowerMACAIntrin`、`lower_maca_intrin`）？进入专家层 [u7-l4 MACA 编译流水线与 transform](./u7-l4-maca-pipeline.md)。
- 想搞清 host 侧最终怎么发起 kernel 调用？关注流水线末尾的 `MakePackedAPI` 与 `LowerDeviceKernelLaunch`（后者给 device 函数打 `DEVICE_KERNEL_LAUNCH` 调用约定，见 [u4-l1](./u4-l1-lowering-pipeline.md)）。

# 关键 lowering Pass 解读

## 1. 本讲目标

上一讲（u6-l1）我们已经建立了三个基础概念：Pass 是「IRModule → IRModule」的纯变换、PassContext 是线程局部的配置容器、PassConfigKey 是键名表。本讲不再重复这些，而是**走进 Pass 内部**，逐个拆解 tilelang 编译流水线上最关键的几个 C++ lowering Pass。

读完本讲，你应当能够：

- 说清 **LayoutInference** 如何为 `fragment`/`shared` 缓冲推导出物理布局（Layout/Fragment），以及它为什么要在三个严格级别（Strict/Common/Free）下做一次类似「约束传播 + 寄存器最小化」的搜索；
- 说清 **LowerTileOp** 如何把 `T.gemm`/`T.copy`/`T.reduce` 这类占位式 tile op 展开成真实的 MMA/cp.async/AllReduce 指令；
- 说清 **InjectSoftwarePipeline** 如何把带 `num_stages` 注解的循环重写成 prologue / body / epilogue 三段并做多版本缓冲；
- 理解 **LegalizeSafeMemoryAccess / VectorizeLoop / StorageRewrite** 这三个「代码质量收尾 Pass」各自负责什么、对最终 CUDA 源码质量有什么影响。

最重要的是：你能对着 dump 出来的 IR，指出**哪一段变化是由哪个 Pass 造成的**——这是阅读与调试编译器最核心的能力。

## 2. 前置知识

本讲假设你已经掌握：

- tilelang 的整体编译链路：DSL → TIR PrimFunc → Pass 流水线 → 设备代码生成（u4-l1）。
- tile op 的「占位模型」：DSL 里写 `T.gemm(A,B,C)` 时，前端只生成一个 `tl.tileop.gemm` 的 `call_intrin` 占位节点，真正的指令留到后端 Pass 展开（u3-l1、u3-l2）。
- Pass 的双面镜像：算法在 C++（`src/transform/*.cc`，经 `TVM_REGISTER_GLOBAL` / `GlobalDef().def("tl.transform.Xxx")` 注册），Python 门面（`tilelang/transform/__init__.py`）仅经 `_ffi_api` 转发（u6-l1）。
- 内存层级 scope：`global` / `shared`（含 `shared.dyn`）/ `local.fragment` / `local`（u2-l2）。

两个本讲会反复用到、但前面没细讲的概念，先建立直觉：

- **Layout（布局）**：一个「逻辑索引 → 物理位置」的纯函数。tilelang 里的缓冲（Buffer）默认按行优先线性排布，但张量核指令要求的数据排布往往不是行优先。Layout 描述的就是「为了让数据喂给指令，逻辑下标 `(i,j)` 实际应该落在存储里的哪个位置」（u3-l4 有详细讨论）。
- **Fragment（片段）**：在 Layout 之上再叠加「Thread-Value 映射」——不仅说清物理位置，还说清每个元素归哪个线程、哪个寄存器。它是生成 MMA/WGMMA/MFMA 指令的精确依据。`local.fragment` scope 的缓冲 lowering 后会变成线程私有的 `local` 寄存器。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/transform/layout_inference.cc](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/layout_inference.cc) | 推导 fragment/shared 缓冲的 Layout/Fragment，把结果写回 Block 注解 |
| [src/transform/lower_tile_op.cc](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/lower_tile_op.cc) | 把 `tl.tileop.*` 占位调用展开为底层指令，并把 fragment 缓冲改写为 local |
| [src/op/gemm.cc](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/op/gemm.cc) | `GemmNode::Lower` / `InferLayout`：`T.gemm` 的两个钩子实现 |
| [src/transform/inject_pipeline.cc](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/inject_pipeline.cc) | 软件流水线重写：注解循环 → prologue/body/epilogue + 多版本缓冲 |
| [src/transform/legalize_safe_memory_access.cc](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/legalize_safe_memory_access.cc) | 对无法证明在界内的 global 访问插入运行时守卫 |
| [src/transform/storage_rewrite.cc](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/storage_rewrite.cc) | 分析访存模式、重写访问以启用缓冲复用与生命周期重叠 |
| [src/op/operator.h](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/op/operator.h) | `TileOperatorNode` 抽象：`Lower` / `InferLayout` 钩子与 `InferLevel` 枚举 |
| [tilelang/cuda/pipeline.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cuda/pipeline.py) | CUDA 后端 Pass 的实际执行顺序（本讲各 Pass 的「时序」来源） |

## 4. 核心概念与源码讲解

### 4.1 LayoutInference：为片上缓冲推导物理布局

#### 4.1.1 概念说明

用户写 `T.gemm(A_s, B_s, C_f)`（A、B 在 shared，C 在 fragment）时，并不需要告诉编译器 shared 里数据该怎么摆、fragment 里每个元素归哪个线程——这些是**硬件指令的硬性要求**（例如 m16n8k16 的 MMA 要求 A 的 shared 布局是某种 swizzle、C 的 fragment 要按特定线程-值映射分布）。

**LayoutInference** 做的就是反推这件事：扫一遍 kernel 里所有的 tile op（gemm/copy/reduce/parallel 循环等），依据**每个 op 自身的 `InferLayout` 钩子**，逐步给涉及的缓冲赋予 Layout/Fragment，使整套数据排布满足所有 op 的约束，并且尽量省寄存器。

为什么这是 Pass 而不是用户的责任？因为布局存在**传递性**：一个 `T.copy(A_global, A_shared)` 决定了 A_shared 的布局，而这个布局又被下游 `T.gemm(A_shared, ...)` 消费；反过来 gemm 对 A_shared 的布局要求又会反向约束 copy。这种「互相约束、需要传播」的问题，正是 Pass 该做的事。

#### 4.1.2 核心流程

整个 Pass 的骨架是一个**约束传播 + 多解择优**的过程，可以概括为五步：

1. **收集（Collect）**：遍历 PrimFunc，把所有 tile op 调用、`T.Parallel` 循环、`alloc_buffers` 收集进 `infer_list_`，并建立「缓冲 → 使用它的 op 列表（use_list_）」与「data Var → 多个 Buffer 别名」的映射。
2. **初始化**：对「漂浮」的 fragment 缓冲（在 tile op 之外被访问，例如在 `if` 条件里读取）先赋予 **FullyReplicated**（全复制）布局；用户用 `T.annotate_layout` 显式标注的布局作为初始解。
3. **Strict 推断**：每个 op 在最严格级别 `kStrict` 下推断一次，得到一批「不可妥协」的布局。
4. **Common BFS 传播**：把所有 op 入队，按「谁涉及的缓冲已有布局就优先出队」的优先级做广度优先传播，遇到冲突（同一缓冲被要求两种不同布局）就报错或尝试合并 swizzle。
5. **Free 模式择优**：对仍不确定的自由度，按连通分量枚举「以哪个 op 为推断根」，每种方案算一遍**总寄存器占用**，取最小者。

三种严格级别（[src/op/operator.h:L85-L88](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/op/operator.h#L85-L88)）是理解本 Pass 的钥匙：

```cpp
enum class InferLevel : uint8_t {
  kFree = 0,     // 自由：可重排，用于最后择优
  kCommon = 1,   // 常规：默认传播
  kStrict = 2,   // 严格：不可妥协的硬约束
};
```

直觉上：`kStrict` 像「不可变」的输入约束，`kCommon` 像「能推就推」的传播，`kFree` 像「还有余地，那就挑最省的」。

最终结果被打包成 `LayoutInferenceResult`（[src/transform/layout_inference.cc:L84-L89](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/layout_inference.cc#L84-L89)）：

```cpp
struct LayoutInferenceResult {
  Map<Buffer, Layout> layout_map;        // 每个缓冲的布局
  Map<For, Fragment> for_map;            // Parallel 循环的循环布局
  Map<For, PrimExpr> predicate_map;      // 并行循环的越界谓词
  Map<For, Bool> padding_guard_map;      // 是否需要对 padding 点加守卫
};
```

#### 4.1.3 源码精读

**(1) Pass 入口与注册。** LayoutInference 是一个 PrimFunc 级 Pass，主体委托给 `LayoutInferencer::Substitute`，最后再做一次并行循环布局校验（[src/transform/layout_inference.cc:L1246-L1259](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/layout_inference.cc#L1246-L1259)）：

```cpp
tvm::transform::Pass LayoutInference() {
  auto pass_func = [=](PrimFunc f, const IRModule &m, const PassContext &ctx) {
    f = LayoutInferencer::Substitute(std::move(f));
    ParallelLoopLayoutValidator::Validate(f->body);
    return f;
  };
  return CreatePrimFuncPass(pass_func, 0, "tl.LayoutInference", {});
}
TVM_FFI_STATIC_INIT_BLOCK() {
  refl::GlobalDef().def("tl.transform.LayoutInference", LayoutInference);
}
```

这段代码做的事：`LayoutInferencer::Substitute` 先做 `Fuse`（合并并行循环），再让 `BufferUseDefCollector` 收集并 `Run()` 出布局，最后用一个 mutator 把布局写回 IR。注意它注册名为 `tl.transform.LayoutInference`——这正是 Python 门面 `tilelang.transform.LayoutInference()` 转发的目标。

**(2) 推断的四步主流程。** `BufferUseDefCollector::Run()` 是整本 Pass 的大脑，四步在注释里标得清清楚楚（[src/transform/layout_inference.cc:L340-L366](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/layout_inference.cc#L340-L366)）：

```cpp
// step 0: set fully replicated layout for floating fragment buffers
for (const auto &[buffer, thread_bounds] : floating_fragment_buffers_) {
  ...
  auto frag = Fragment::FullyReplicated(buffer->shape, thread_bounds->extent);
  layout_map.Set(buffer, frag);
}
// step 1: infer strict layout
for (int i = 0; i < num_infer; i++)
  RunInferStep(i, InferLevel::kStrict, false, layout_map, ...);
// step 2: infer common layout with BFS
FinishInferQueue(InferLevel::kCommon, layout_map, ...);
// step 3: relax constraints to free and re-run
InferInFreeMode(layout_map, strict_layout_map);
// step 4: finalize alias layouts by Var
```

`RunInferStep` 是单步推断：取出一个 tile op，调用它的 `InferLayout(LayoutInferArgs{...}, level)`（这是 [src/op/operator.h:L166-L167](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/op/operator.h#L166-L167) 定义的纯虚钩子），拿到「缓冲 → 布局」更新；若缓冲已有布局则检查是否一致，否则写入并把它**下游的 op** 重新入队（约束传播）。`EnqueueWithPriority` 会把「涉及缓冲已有布局」的 op 放到队头，加速收敛。

**(3) Free 模式的寄存器最小化。** `InferInFreeMode` 用并查集（`UnionFind`）把共享缓冲的 op 划分成若干连通分量；对每个分量，**枚举每个 op 作为推断根**，跑一遍推断，统计该方案的总寄存器占用，取最小者（[src/transform/layout_inference.cc:L1108-L1135](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/layout_inference.cc#L1108-L1135)）：

```cpp
int64_t reg_num = 0;
for (const auto &[buffer, layout] : tmp_layout_map) {
  if (auto frag = layout.as<Fragment>()) {
    int64_t frag_reg_num = 1;
    for (auto i : frag.value()->OutputShape())
      frag_reg_num *= *as_const_int(i);   // 每个 fragment 的物理体积
    reg_num += frag_reg_num;
  }
}
if (reg_num < min_reg_num || ...) {
  best_layout_map = tmp_layout_map;
  min_reg_num = reg_num;
}
```

寄存器占用 \(R\) 是各 fragment 物理输出形状之积的总和：

\[
R = \sum_{b \in \text{fragments}} \prod_{d \in \text{OutputShape}(b)} d
\]

最小化 \(R\) 直接关系到 kernel 能否塞进更多的线程、能否少溢出到 local memory，是性能的关键。

**(4) 把布局写回 IR。** 推断完成后，`LayoutInferencer` 把 `layout_map` 挂到根 Block 的 `attr::kLayoutMap` 注解上（[src/transform/layout_inference.cc:L1180-L1186](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/layout_inference.cc#L1180-L1186)）；对 `T.Parallel` 循环，把循环布局、越界谓词、padding 守卫分别挂到 For 节点的注解上（[src/transform/layout_inference.cc:L1204-L1233](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/layout_inference.cc#L1204-L1233)）。注意：**LayoutInference 不展开任何循环，只挂注解**——真正的展开留给 LowerTileOp。

> 小提示：`has_tma` 这个标志其实是 **LowerTileOp** 设的，而不是 LayoutInference。pipeline.py 里 `module_has_tma` 读的就是 LowerTileOp 写入的 `tl.has_tma` 属性——这也是为什么 LowerTileOp 必须在依赖该属性的后续 Pass（如 `FuseMBarrierArriveExpectTx`）之前运行。

#### 4.1.4 代码实践

**目标**：观察 LayoutInference 给 fragment 缓冲附加的布局注解。

**步骤**（源码阅读型实践）：

1. 在 [tilelang/cuda/pipeline.py:L113](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cuda/pipeline.py#L113) 上下文确认 LayoutInference 的执行位置：它紧跟在 `PipelinePlanning` + `InjectSoftwarePipeline`（L108-L109）之后、`LowerTileOp`（L117）之前。这个顺序是**故意的**——布局推断必须看到流水线化之后的最终循环结构。
2. 写一个最小 GEMM kernel，用 `TL_ENABLE_DUMP_IR` 把 IR 落盘：

   ```python
   import tilelang, tilelang.language as T

   @tilelang.jit
   def gemm(M, N, K):
       @T.prim_func
       def main(A: T.Tensor((M, K), "float16"),
                B: T.Tensor((N, K), "float16"),
                C: T.Tensor((M, N), "float32")):
           with T.Kernel(T.ceildiv(M, 128), T.ceildiv(N, 128), threads=128) as (bx, by):
               A_s = T.alloc_shared((128, 128), "float16")
               B_s = T.alloc_shared((128, 128), "float16")
               C_f = T.alloc_fragment((128, 128), "float32")
               T.clear(C_f)
               for k in T.Pipelined(T.ceildiv(K, 128), num_stages=3):
                   T.copy(A[bx*128:(bx+1)*128, k*128:(k+1)*128], A_s)
                   T.copy(B[by*128:(by+1)*128, k*128:(k+1)*128], B_s)
                   T.gemm(A_s, B_s, C_f)
               T.copy(C_f, C[bx*128:(bx+1)*128, by*128:(by+1)*128])
       return main

   gemm(512, 512, 512).compile(
       pass_configs={"tl.enable_dump_ir": True, "tl.dump_ir_path": "./dump_ir"})
   ```

3. 在 `./dump_ir/` 目录里找到文件名含 `LayoutInference` 的 IR 文件，对比它**前后**两个文件。

**需要观察的现象**：LayoutInference **之后**的 IR 里，`C_f`（fragment）所在的 Block 注解中会多出 `layout_map`，其中 `C_f` 的值变成了一个 `Fragment`（带 thread-value 映射），而 `A_s`/`B_s`（shared）通常是一个带 swizzle 的 Layout。LayoutInference **之前**的文件里这些缓冲是没有布局信息的。

**预期结果**：你能指出「`C_f` 的 Fragment 注解」是 LayoutInference 这一步引入的。若你看到 IR 里有 `attr::kParallelLoopLayout` 注解，那也是本 Pass 挂的。

> 待本地验证：本实践依赖可运行 tilelang 的 CUDA 环境；若无 GPU，可改为纯源码阅读——在 `BufferUseDefCollector::Run` 的 step 0–4 注释处设断点（或加日志）跟踪一个 gemm kernel 的推断顺序。

#### 4.1.5 小练习与答案

**练习 1**：为什么「漂浮的 fragment 缓冲」（在 tile op 之外被访问）要被赋予 `FullyReplicated` 全复制布局？

**参考答案**：因为它的访问模式无法被任何 tile op 的 `InferLayout` 推断出来（比如出现在 `if` 条件里被标量读取），编译器无从知道哪个线程该持有哪个元素；全复制让每个线程都持有一份完整副本，保证语义正确，代价是占用更多寄存器。

**练习 2**：LayoutInference 会修改 `T.Parallel` 循环的循环体吗？它对 For 节点做了什么？

**参考答案**：不会展开循环体。它只把推断出的循环布局（Fragment）、越界谓词、padding 守卫作为**注解**（`kParallelLoopLayout` / `kParallelLoopPredicate` / `kParallelLoopRequiresPaddingGuard`）挂到 For 节点上，真正的展开由 LowerTileOp 完成。

---

### 4.2 LowerTileOp：把占位 tile op 展开为底层指令

#### 4.2.1 概念说明

回顾 u3-l1 的铁律：DSL 里写 `T.gemm` 时，前端只生成一个 `tl.tileop.gemm` 的 `call_intrin` **占位节点**，并不确定要发射哪条 MMA 指令。这是因为选哪条指令（m16n8k16？wmma？wgmma？tcgen05？mfma？）取决于**目标硬件、block 大小、A/B 的 scope 与转置、布局**——这些信息在 LayoutInference 之后才齐全。

**LowerTileOp** 就是那个「占位 → 指令」的展开器。它遍历 IR，每遇到一个 tile op 占位调用，就：

- 调用该 op 的 `Lower` 钩子，得到一段具体的 TIR（含 `tl.mma`/`tl.ptx_cp_async`/`tl.cublas_math` 之类的底层 intrinsic 调用）；
- 把 `local.fragment` 缓冲改写为普通 `local`（寄存器），并按布局重排其形状；
- 收集 side-effect 信息（如是否用了 TMA、是否需要 mbarrier），写回 PrimFunc 属性供后续 Pass 使用。

#### 4.2.2 核心流程

LowerTileOp 的核心是一个 IRMutator，关键访问点是 `VisitStmt_(const EvaluateNode*)`——因为 tile op 占位是以 `Evaluate(Call(...))` 形式出现在 IR 里的：

```
对每个 Evaluate 节点:
    tile_op = ParseOperator(stmt)          # 识别出它是 gemm/copy/reduce/...
    if not tile_op.defined():
        交给基类（普通表达式语句）
    else:
        构造 LowerArgs（target、thread_bounds、layout_map、回调…）
        lowered = tile_op->Lower(lower_args, analyzer)   # 展开成具体 TIR
        return Visit(lowered)
```

`ParseOperator`（[src/op/operator.h:L197-L202](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/op/operator.h#L197-L202)）是「Call/Stmt → TileOperator 对象」的统一解析入口，返回一个实现了 `Lower` / `InferLayout` 的对象。`Lower` 是纯虚钩子（[src/op/operator.h:L163-L164](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/op/operator.h#L163-L164)），每种 tile op 各自实现。

以 `T.gemm` 为例，展开走的是「C++ 调度 + Python 实现」的混合模式：C++ 的 `GemmNode::Lower` 通过 `Function::GetGlobal("tl.gemm.lower")` 调用 Python 侧注册的 lowering 函数，由后者根据 `resolve_gemm_impl` 选出具体的 MMA 实现类并产出 PrimFunc（参见 u3-l1）。这样设计的好处是：指令选择逻辑用 Python 写，便于按 target 扩展，而 IR 改写的骨架留在 C++。

#### 4.2.3 源码精读

**(1) 占位展开的调度点。** `LowerTileOpPass::VisitStmt_(const EvaluateNode*)` 是整个 Pass 的心脏（[src/transform/lower_tile_op.cc:L1117-L1125](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/lower_tile_op.cc#L1117-L1125)）：

```cpp
Stmt VisitStmt_(const EvaluateNode *op) final {
  const CallNode *call = op->value.as<CallNode>();
  if (call && call->op.as<GlobalVarNode>())
    return Downcast<Evaluate>(IRMutatorWithAnalyzer::VisitStmt_(op));

  auto tile_op = ParseOperator(GetRef<Stmt>(op), block_annotations_);
  if (!tile_op.defined())
    return IRMutatorWithAnalyzer::VisitStmt_(op);
  ...
```

读到这段就知道：非 tile op 的 `Evaluate`（比如纯标量运算）走基类原样保留；只有 `ParseOperator` 识别出的 tile op 才进入展开分支。紧随其后，代码构造了一组回调（`add_workspace` 给 reduce 申请临时 shared、`alloc_mbarrier` 给 TMA 申请 barrier、`require_smem_alignment` 记录对齐要求），再在 [src/transform/lower_tile_op.cc:L1195](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/lower_tile_op.cc#L1195) 调用 `tile_op->Lower(lower_args, analyzer)` 得到展开后的 Stmt。

**(2) `T.gemm` 的两个钩子。** `GemmNode::Lower` 把活儿转交给 Python 侧的 `tl.gemm.lower`，并把结果包成一个带 `global_symbol` 的 `SBlockRealize`（[src/op/gemm.cc:L176-L219](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/op/gemm.cc#L176-L219)）：

```cpp
Stmt GemmNode::Lower(const LowerArgs &lower_args, arith::Analyzer *analyzer) const {
  if (const auto f = Function::GetGlobal("tl.gemm.lower")) {
    ...
    // NOTE(wt): Decide the instruction key and compute warp partition on Python side.
    auto prim_func = Downcast<PrimFunc>(
        (*f)(GetRef<Gemm>(this), lower_args.layout_map, lower_args.target,
             lower_args.thread_bounds, lower_args.thread_index, mbar_phase));
    ...
  }
}
```

注释 `Decide the instruction key and compute warp partition on Python side` 一语道破：**指令选择（m16n8k16 / wgmma / tcgen05 / mfma）发生在 Python 端的 `resolve_gemm_impl`**，C++ 只负责把选好的实现实例化成 IR。对应的 `InferLayout` 钩子（[src/op/gemm.cc:L221-L259](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/op/gemm.cc#L221-L259)）同样转交 `tl.gemm.infer_layout`，并用 `GetGemmInstructionKey` + `ResolveGemmImpl` 决定是否复用已有 shared 布局——这正是 4.1 里 LayoutInference 调用的同一个钩子。

**(3) fragment → local 的改写。** tile op 占位里，fragment 缓冲的 scope 是 `local.fragment`；lowering 后它们要变成真实的线程私有寄存器（scope `local`），并按布局的 `OutputShape` 重排形状。这件事在 `makeBufferWithLayout` 里完成（[src/transform/lower_tile_op.cc:L41-L88](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/lower_tile_op.cc#L41-L88)）：

```cpp
static Buffer makeBufferWithLayout(const Buffer &buffer, const Layout &layout,
                                   Map<Var, Var> &var_remap) {
  ...
  // convert fragments to normal local buffer
  if (IsFragmentBuffer(buffer)) {
    new_type = PointerType(ptr_type->element_type, "local");
  } else {
    new_type = buffer->data->type_annotation;
  }
  Array<PrimExpr> layout_shape = layout->OutputShape();
  ...
}
```

读到 `PointerType(..., "local")` 就明白了：fragment 在 IR 层是一种「带布局语义的占位 scope」，lowering 后落归到 `local`。对 shared 缓冲，如果它的物理体积大于布局体积（即被多份复制，如多 warp 各持一份），还会在形状最前面插入 `replicate_extent` 维。

**(4) Pass 入口与注册。** 与 LayoutInference 同样的模式（[src/transform/lower_tile_op.cc:L1578-L1588](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/lower_tile_op.cc#L1578-L1588)），注册名 `tl.transform.LowerTileOp`。`Substitute` 末尾会把 `has_tma_` 写成 `tl.has_tma` 属性（见 [src/transform/lower_tile_op.cc:L219](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/lower_tile_op.cc#L219)），这正是 pipeline.py 里 `module_has_tma` 判断的来源。

#### 4.2.4 代码实践

**目标**：在 dump 的 IR 里定位「`T.gemm` 占位被展开成 MMA 调用」的位置，并说出是哪个 Pass 干的。

**步骤**：

1. 复用 4.1.4 的 dump 实践，把 `./dump_ir/` 里**LowerTileOp 之前**和**之后**的两个文件都打开。
2. 在「之前」的文件里搜索 `tl.tileop.gemm`——你应该能看到一个形如 `Evaluate(tl.tileop.gemm(A_s, B_s, C_f, ...))` 的占位调用。
3. 在「之后」的文件里搜索 `tl.tileop.gemm`——它应该**消失**了，取而代之的是 `tl.mma`（或 `tl.wgmma.mma_async`、`tl.mfma` 等）的底层调用，并且 `C_f` 的 scope 从 `local.fragment` 变成了 `local`。

**需要观察的现象**：占位的 `tl.tileop.gemm` → 具体的 MMA intrinsic；fragment 缓冲 scope 的改写。这两件事都发生在 LowerTileOp 这一个 Pass 内。

**预期结果**：你能在练习报告里写出——「`tl.tileop.gemm` 占位的展开由 `tl.LowerTileOp` Pass 完成，具体 MMA 指令由 Python 侧 `tl.gemm.lower`（即 `resolve_gemm_impl`）选定」。

> 待本地验证：具体出现 `tl.mma` 还是 `tl.wgmma.*` 取决于你机器的架构（SM75/80 用 m16n8k16，SM90 用 wgmma）。无 GPU 时只能读 IR 文件名与占位变化，不能确认具体指令。

#### 4.2.5 小练习与答案

**练习 1**：为什么 LayoutInference 必须在 LowerTileOp **之前**运行？（提示：看 `GemmNode::InferLayout` 用到了什么。）

**参考答案**：因为 `GemmNode::Lower` 需要知道 shared/fragment 缓冲的布局才能选出正确的 MMA 实现并生成正确的索引；而这些布局是 LayoutInference 推导并写入 `layout_map` 注解的。若先 Lower，选指令时布局未知，无法正确展开。

**练习 2**：fragment 缓冲在 LowerTileOp 之后 scope 变成了什么？为什么？

**参考答案**：变成 `local`（线程私有寄存器）。因为 `local.fragment` 只是 tilelang 用来承载「带布局语义、可对接张量核」的 IR 层占位 scope，物理上它就是寄存器；lowering 后布局已经落实到具体索引，scope 落归 `local` 即可被后续 codegen 当作普通寄存器处理。

---

### 4.3 InjectSoftwarePipeline：注解循环 → 三段流水线

#### 4.3.1 概念说明

u3-l3 讲过软件流水线的**规划**：`PipelinePlanning` 推断出每个 producer/consumer 的 `stage` / `order` 注解。本节讲它的**执行**：`InjectSoftwarePipeline`（注意，不是 `InjectPipeline` 这个函数名，注册的 Pass 名是 `tl.InjectSoftwarePipeline`）把这些注解**物化**成真正的多级缓冲与三段循环结构，让搬运（`T.copy`）与计算（`T.gemm`）在时间上重叠。

核心思想是经典的多版本缓冲（double/triple buffering）：把原本「先搬一块、算一块」的串行循环，改写成「prologue 预热前若干 stage、body 稳态一搬一算、epilogue 收尾」的三段。稳态 body 里，第 `i` 轮的计算消费的是若干 stage 前启动的搬运，从而隐藏访存延迟。

#### 4.3.2 核心流程

整个重写由 `PipelineInjector::Inject` 驱动，关键三步：

1. **解析注解**：读出循环上的 `software_pipeline_stage` / `software_pipeline_order`（以及 `num_stages`）。每个 block 拿到一个 `(stage, order)` 二元组（`PipelineAnnotation`，[src/transform/inject_pipeline.cc:L66-L71](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/inject_pipeline.cc#L66-L71)）。若只有 stage 没有 order（或反之）会直接报错。
2. **多版本化缓冲**：对每个被流水线消费的 shared 缓冲，按其「跨 stage 的最大读写跨度」算出需要的版本数 `num_versions`（通常等于 `use-def 深度+1`，上限为 `num_stages`），把分配改成 `shape × num_versions`，并用 `floormod(iter, num_versions)` 选版本。
3. **发射三段**：把循环切成 prologue / body / epilogue 三段，每段按 `order` 重排内部语句，并插入相应的 barrier 同步。

`num_stages` 的语义：它表示流水线深度，即同一时刻 shared 缓冲最多有多少个「在途」的版本。若 `num_stages = 3`，则 shared 最多开 3 份（实际版本数还受 `use-def` 约束，可能小于 3）。

#### 4.3.3 源码精读

**(1) 三段的发射。** 这是本 Pass 最直观的一段（[src/transform/inject_pipeline.cc:L1220-L1240](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/inject_pipeline.cc#L1220-L1240)）：

```cpp
// Step 2: Emit the pipeline prologue, body and epilogue.
Optional<Integer> pipeline_num_stages = GetPipelineNumStages(pipeline_loop_.get());
Stmt prologue = EmitImpl(pipeline_loop_->min,
                         pipeline_loop_->min + max_stage_, true, true);
Stmt body = EmitImpl(pipeline_loop_->min + max_stage_,
                     pipeline_loop_->min + pipeline_loop_->extent, false, false);
Stmt epilogue = EmitImpl(
    pipeline_loop_->min + pipeline_loop_->extent,
    pipeline_loop_->min + pipeline_loop_->extent + max_stage_, true, true);
```

设原循环为 `for i in [lo, hi)`，最大 stage 偏移为 `S = max_stage_`，则三段的迭代区间为：

\[
\begin{aligned}
\text{prologue:} &\quad i \in [lo,\; lo + S) \\
\text{body:}     &\quad i \in [lo + S,\; hi) \\
\text{epilogue:} &\quad i \in [hi,\; hi + S)
\end{aligned}
\]

body 是「稳态」：每一轮 `i` 同时启动 stage 0 的搬运、执行 stage `S` 的计算（对应逻辑上更早的迭代）。prologue 与 epilogue 是「不完整」的边界，用 `EmitImpl(..., true, true)` 的布尔参数表示它们是边界段（会按 stage 决定哪些语句实际发射）。

**(2) 多版本缓冲的形状放大。** 版本数由 `ComputeBufferVersion` 算出，超过 1 时用 `RewriteAllocBuffer` 把分配形状放大 `num_versions` 倍（参见 [src/transform/inject_pipeline.cc:L1197-L1207](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/inject_pipeline.cc#L1197-L1207) 及前文 L841 的 `new_node->shape = {num_stages * buf->shape[0]}`）。barrier 索引也会加上 stage 偏移（[src/transform/inject_pipeline.cc:L762-L841](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/inject_pipeline.cc#L762-L841)）。

**(3) Pass 入口与注册。** `InjectSoftwarePipeline` 调用 `software_pipeline::InjectPipeline(f)` 做 IR 改写，再做一次 `ConvertSSA` 收尾（[src/transform/inject_pipeline.cc:L4030-L4045](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/inject_pipeline.cc#L4030-L4045)）：

```cpp
tirx::transform::Pass InjectSoftwarePipeline() {
  auto pass_func = [=](PrimFunc f, const IRModule &m, const PassContext &ctx) {
    auto *fptr = f.CopyOnWrite();
    fptr->body = software_pipeline::InjectPipeline(f);
    fptr->body = ConvertSSA(std::move(fptr->body));
    return f;
  };
  return CreatePrimFuncPass(pass_func, 0, "tl.InjectSoftwarePipeline", {});
}
```

`Inject` 的入口在 [src/transform/inject_pipeline.cc:L4020-L4022](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/inject_pipeline.cc#L4020-L4022)。值得注意的是，**InjectSoftwarePipeline 运行在 LayoutInference 之前**（pipeline.py L109 → L113），这样布局推断看到的就是流水线化后的最终循环结构，shared 多版本缓冲的布局能一次推对。

#### 4.3.4 代码实践

**目标**：观察 `num_stages` 如何改变 shared 缓冲的分配形状。

**步骤**：

1. 用 4.1.4 的 GEMM，对比 `num_stages=1`（即不流水线）与 `num_stages=3` 两份 dump IR。
2. 在 InjectSoftwarePipeline **之后**的 IR 里找到 `A_s` / `B_s` 的 `alloc_buffer`。

**需要观察的现象**：`num_stages=1` 时 `A_s` 形状是 `(128, 128)`；`num_stages=3` 时形状应变为类似 `(3, 128, 128)` 或在第一维放大（具体倍数取 `ComputeBufferVersion` 的结果，受 use-def 约束可能小于 3）。同时主循环会被切成三段，且能看到 `floormod(..., num_versions)` 形式的版本选择索引与 mbarrier 同步。

**预期结果**：你能写出——「shared 缓冲的多版本化与三段循环重写由 `tl.InjectSoftwarePipeline` 完成，注解 `software_pipeline_stage` / `software_pipeline_order` 由 `tl.PipelinePlanning` 生成」。

> 待本地验证：版本数的具体取值依赖 kernel 的读写结构，需在本地 dump IR 后确认。

#### 4.3.5 小练习与答案

**练习 1**：为什么 InjectSoftwarePipeline 要在 LayoutInference **之前**运行？

**参考答案**：因为流水线化会把 shared 缓冲开成多份、并把循环切成三段；布局推断需要看到这个最终结构才能为多版本缓冲推对布局。若先推断再流水线，多版本缓冲的布局可能要返工。

**练习 2**：若一个 kernel 的 `software_pipeline_stage` 注解存在但 `software_pipeline_order` 缺失，会发生什么？

**参考答案**：编译会直接 `LOG(FATAL)` 报错（见 [src/transform/inject_pipeline.cc:L3997-L4004](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/inject_pipeline.cc#L3997-L4004)）："Stage of the software pipeline is not defined." 二者必须成对出现。

---

### 4.4 代码质量收尾：LegalizeSafeMemoryAccess / VectorizeLoop / StorageRewrite

#### 4.4.1 概念说明

前面三个 Pass 负责「正确性与性能骨架」，而 tilelang 还有一组 Pass 负责「最终代码质量」，其中三个最值得关注：

- **LegalizeSafeMemoryAccess**：tile 分块常会导致尾块越界（比如 tile 大小 128，但维度 N=200，最后一个 tile 只覆盖 72 个有效元素）。对 `global` 缓冲，越界访问会引发段错误，本 Pass 自动插入运行时 `if` 守卫；对 shared/local 只告警。这正是 u2-l3 提到的「自动边界保护」。
- **VectorizeLoop**：把连续的标量访存循环向量化成 `<N x dtype>` 的向量读写（如 `float4`），提高内存带宽利用率。
- **StorageRewrite**：分析所有缓冲的生命周期，让不重叠生命的缓冲**复用同一块存储**，显著降低 shared/local 占用。

#### 4.4.2 核心流程

三者在 pipeline.py 里的时序（[tilelang/cuda/pipeline.py:L125-L186](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cuda/pipeline.py#L125-L186)）是：

```
LowerTileOp                  # 4.2：占位 → 指令
  → ... → LegalizeVectorizedLoop   # 先把向量化循环合法化
  → LegalizeSafeMemoryAccess        # 给 global 越界插守卫
  → ... → FlattenBuffer             # 把多维缓冲拍平
  → VectorizeLoop                   # 真正向量化
  → StorageRewrite                  # 缓冲复用
```

注意 `LegalizeSafeMemoryAccess` 在 vectorize **之前**运行——这样守卫是标量 `if`，结构简单；之后再向量化就能把循环体里的连续访问合并。

LegalizeSafeMemoryAccess 的判断逻辑很直观：对每个 BufferLoad/Store 的每个下标 `index`，尝试证明 `0 <= index < shape_dim`。能证明显然成立则什么都不做；证不出来且缓冲是 `global`，就把条件 `index < shape_dim` 与 `index >= 0` 作为运行时守卫加进去。

#### 4.4.3 源码精读

**(1) 越界判断。** `SafeMemChecker::CheckBufferIndices` 是核心（[src/transform/legalize_safe_memory_access.cc:L253-L316](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/legalize_safe_memory_access.cc#L253-L316)）：

```cpp
PrimExpr upper_bound_cond = index < shape_dim;
bool can_prove_upper = analyzer_->CanProve(upper_bound_cond, arith::ProofStrength::kSymbolicBound);
...
if (!can_prove_upper) {            // const_int_bound 兜底
  arith::ConstIntBound index_bound = analyzer_->const_int_bound(index);
  arith::ConstIntBound shape_bound = analyzer_->const_int_bound(shape_dim);
  if (index_bound->max_value < shape_bound->min_value) can_prove_upper = true;
}
if (!can_prove_upper) {
  if (throw_warning) LOG(WARNING) << "Index access may exceed buffer bounds ...";
  if (IsGlobalBuffer(buffer)) PushCondition(upper_bound_cond);   // 仅 global 加守卫
}
```

读这段要抓住两点：① 它有两级证明——先用符号界（`kSymbolicBound`，跟踪 Var 约束）证，证不出来再用 `const_int_bound`（跟踪 PrimExpr 约束，能利用 `if` 条件里的界）兜底；② 守卫**只对 global 缓冲**生效（`IsGlobalBuffer`），shared/local 只 `LOG(WARNING)`。

**(2) 守卫的两种插入方式。** 对**写**，用 `IfThenElse(cond, store)` 包裹整条语句——越界时直接跳过写入（[src/transform/legalize_safe_memory_access.cc:L486-L490](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/legalize_safe_memory_access.cc#L486-L490)）；对**读**，用 `if_then_else(cond, value, safe_value)`——越界时返回一个安全占位值（如 0），避免读到脏数据（[src/transform/legalize_safe_memory_access.cc:L450-L455](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/legalize_safe_memory_access.cc#L450-L455)）：

```cpp
PrimExpr value = load;
for (auto cond : conditions)
  value = if_then_else(cond, value, GetSafeValue(load->buffer));  // 越界读安全值
```

**(3) 开关与注册。** 整个 Pass 受 PassConfigKey `tl.disable_safe_memory_legalize`（即 `TL_DISABLE_SAFE_MEMORY_ACCESS`）控制，开启时直接返回原函数（[src/transform/legalize_safe_memory_access.cc:L763-L783](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/legalize_safe_memory_access.cc#L763-L783)）。StorageRewrite 的注册见 [src/transform/storage_rewrite.cc:L1924-L1952](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/storage_rewrite.cc#L1924-L1952)，文件头注释（L34-L37）点明它的职责是「分析访存模式、重写访问以在可能时启用存储复用」。

#### 4.4.4 代码实践

**目标**：观察 LegalizeSafeMemoryAccess 为尾块插入的运行时守卫，并用环境变量关闭它对比效果。

**步骤**：

1. 改写 4.1.4 的 GEMM，把 `M, N, K` 设成**不能被 128 整除**的值（如 `M=N=K=200`），这样最后一个 tile 是尾块。
2. 在 dump IR 里找到 LegalizeSafeMemoryAccess **之后**的文件，搜索 `if_then_else` 或 `IfThenElse`，定位针对 global 读写 `A`/`B`/`C` 的边界守卫。
3. 重新编译，传入 `pass_configs={"tl.disable_safe_memory_legalize": True}`，对比 dump IR——守卫应当消失。

**需要观察的现象**：开启时，尾块 tile 对 `A`/`B`（读）会用 `if_then_else(cond, value, 0.0)`、对 `C`（写）会用 `IfThenElse(cond, store)`；关闭时这些守卫不存在（尾块越界访问「裸奔」，结果可能出错或段错误）。

**预期结果**：你能在报告里写出——「global 尾块越界的运行时守卫由 `tl.LegalizeSafeMemoryAccess` 插入，可用 `tl.disable_safe_memory_legalize` 关闭；shared/local 越界只告警不加守卫」。

> 待本地验证：关闭守卫后若 kernel 仍能跑，是因为尾块恰好落在已分配的 shared 范围内；但 global 越界通常会触发段错误。

#### 4.4.5 小练习与答案

**练习 1**：为什么 LegalizeSafeMemoryAccess 对 shared/local 越界只告警、不加守卫？

**参考答案**：因为 tile 分块时，shared/local 缓冲是按 tile 大小完整分配的（如 `(128,128)`），即使尾块只用到一部分有效元素，访问仍在分配范围内，不会越界段错误；而 global 缓冲的真实大小由用户张量决定，越界就是真的越界。因此 shared/local 只需告警提示可能的逻辑错误，global 才必须加守卫。

**练习 2**：StorageRewrite 能带来什么实际收益？举一个例子。

**参考答案**：当一个 kernel 里有两个 shared 缓冲 `A_s` 和 `B_s`，且它们的生命周期不重叠（比如先全用 `A_s` 再全用 `B_s`）时，StorageRewrite 可以让二者复用同一块 shared 内存，从而把 shared 占用从 `size(A_s) + size(B_s)` 降到 `max(size(A_s), size(B_s))`，腾出 shared 给更大的 tile。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次「IR 取证」任务。

**任务**：给下面这个带尾块与流水线的 GEMM kernel 做完整的 Pass 追踪，绘制一张「Pass → IR 变化」对照表。

```python
import tilelang, tilelang.language as T

@tilelang.jit
def gemm(M, N, K):
    @T.prim_func
    def main(A: T.Tensor((M, K), "float16"),
             B: T.Tensor((N, K), "float16"),
             C: T.Tensor((M, N), "float32")):
        with T.Kernel(T.ceildiv(M, 128), T.ceildiv(N, 128), threads=128) as (bx, by):
            A_s = T.alloc_shared((128, 128), "float16")
            B_s = T.alloc_shared((128, 128), "float16")
            C_f = T.alloc_fragment((128, 128), "float32")
            T.clear(C_f)
            for k in T.Pipelined(T.ceildiv(K, 128), num_stages=3):
                T.copy(A[bx*128:(bx+1)*128, k*128:(k+1)*128], A_s)
                T.copy(B[by*128:(by+1)*128, k*128:(k+1)*128], B_s)
                T.gemm(A_s, B_s, C_f)
            T.copy(C_f, C[bx*128:(bx+1)*128, by*128:(by+1)*128])
    return main

gemm(200, 200, 200).compile(
    pass_configs={"tl.enable_dump_ir": True, "tl.dump_ir_path": "./dump_ir"})
```

**要求**：打开 `./dump_ir/` 目录，按下表逐行填空。每个 Pass 的「之前」与「之后」各看一个文件，记录该 Pass 引入或消除的标志性 IR 特征。

| Pass | 之前 IR 标志 | 之后 IR 标志 | 变化用一句话描述 |
| --- | --- | --- | --- |
| `tl.InjectSoftwarePipeline` | 单段主循环、`A_s` 形状 `(128,128)` | ？ | ？ |
| `tl.LayoutInference` | 缓冲无 `layout_map` 注解 | ？ | ？ |
| `tl.LowerTileOp` | `Evaluate(tl.tileop.gemm(...))` 占位、`C_f` 为 `local.fragment` | ？ | ？ |
| `tl.LegalizeSafeMemoryAccess` | 尾块对 `A`/`B`/`C` 的访问无守卫 | ？ | ？ |

**参考答案要点**（供自检）：

- InjectSoftwarePipeline 之后：主循环切成 prologue/body/epilogue 三段，`A_s`/`B_s` 形状第一维放大（多版本），出现 `floormod` 版本选择与 mbarrier 同步。
- LayoutInference 之后：`C_f` 获得 Fragment（带 thread-value 映射）注解，`A_s`/`B_s` 获得 swizzle Layout 注解，挂在 `layout_map` 上。
- LowerTileOp 之后：`tl.tileop.gemm` 消失，出现 `tl.mma`/`tl.wgmma.*` 等底层 intrinsic；`C_f` scope 由 `local.fragment` 变 `local`；`tl.has_tma` 属性被写入。
- LegalizeSafeMemoryAccess 之后：尾块（M=N=K=200，最后 tile 只有 72 个有效元素）对 global 的读写被 `if_then_else`/`IfThenElse` 守卫包裹。

> 待本地验证：表格中「之后」列的具体形态随本地架构（SM 版本）与版本数策略而变，需在本地 dump 后据实填写。

## 6. 本讲小结

- **LayoutInference** 在三个严格级别（Strict → Common → Free）下做约束传播，最终在 Free 模式里枚举推断根、按总寄存器占用 \(R=\sum_b\prod_d d\) 择优；它**只挂 `layout_map` 注解、不展开循环**。
- **LowerTileOp** 是「占位 → 指令」的展开器：在 `VisitStmt_(EvaluateNode)` 里用 `ParseOperator` 识别 tile op，调 `tile_op->Lower()` 展开；`T.gemm` 的指令选择实际由 Python 侧 `resolve_gemm_impl` 完成；同时把 `local.fragment` 落归为 `local`。
- **InjectSoftwarePipeline** 把带 `stage`/`order` 注解的循环切成 prologue / body / epilogue 三段（区间 \([lo,lo+S)\)/\([lo+S,hi)\)/\([hi,hi+S)\)），并对 shared 缓冲做多版本化；它运行在 LayoutInference **之前**。
- **代码质量三件套**：LegalizeSafeMemoryAccess 对无法证明在界的 global 访问插运行时守卫（读用 `if_then_else`、写用 `IfThenElse`），shared/local 只告警；VectorizeLoop 做向量化；StorageRewrite 做缓冲复用。
- 这四个 Pass 在 [tilelang/cuda/pipeline.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cuda/pipeline.py) 里的**时序是性能与正确性的关键**：PipelinePlanning → InjectSoftwarePipeline → LayoutInference → LowerTileOp → LegalizeSafeMemoryAccess → (FlattenBuffer) → VectorizeLoop → StorageRewrite。
- tile op 的两个钩子 `Lower` / `InferLayout`（[src/op/operator.h:L163-L167](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/op/operator.h#L163-L167)）是贯穿 LayoutInference 与 LowerTileOp 的统一接口——LayoutInference 调 `InferLayout`，LowerTileOp 调 `Lower`。

## 7. 下一步学习建议

- **u6-l3（设备代码生成、模板与 tile op lowering）**：本讲到 `tile_op->Lower()` 为止；下一讲跟进 `tl.gemm.lower` 之后的事——codegen_cuda 如何把 TIR 翻译成 CUDA C++，以及 `src/tl_templates` 下的 CuTe/HIP 模板如何被注入。
- **u9-l1（调试工具：lower trace、pass 可视化）**：本讲的 dump IR 实践是手工的；下一阶段会介绍 `tools/lower_trace`（IR diff/HTML）、`tools/pass_visualizer` 与 `pass_diff_hook`，让本讲的「Pass 取证」自动化、可视化。
- **u3-l3 / u3-l4（软件流水线、布局与 swizzle）**：若你对 InjectSoftwarePipeline 的 `stage`/`order` 推断、LayoutInference 里的 Fragment/swizzle 还想深入，可回看这两篇从 DSL 与数学定义角度的讲解。
- **阅读建议**：以 `src/transform/lower_tile_op.cc` 的 `VisitStmt_(EvaluateNode)` 为锚点，顺着 `ParseOperator` → 各 op 的 `Lower`（gemm/copy/reduce）读一遍，能建立「tile op 从占位到指令」的完整心智模型。

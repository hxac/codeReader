# Tile Op lowering 与 Tail Mask

## 1. 本讲目标

本讲聚焦 `LowerAndLegalize` 阶段里紧挨着的四个 pass，它们共同把「前端写的高层 tile 语义」彻底落地为「硬件能安全执行的底层 IR」：

1. **LowerTileOp**：把高层 tile op（如 `T.copy`、`T.gemm_v0`、`T.tile.add` 背后的 intrinsic）展开为底层 TIR 语句，并解析 layout 标注。
2. **AscendTailMaskPropagation**：当某个 UB tile 只装了「部分有效数据」（尾块 tail）时，把后续的向量算子改写成「只算有效区域」的 tail 变体。
3. **LegalizeVectorizedLoop**：把带 `kVectorized` 提示的循环真正向量化（展开为连续语句）。
4. **LegalizeSafeMemoryAccess**：给可能越界的 GM 访问加上运行时边界保护。

学完本讲，读者应能：

- 说清一个 `T.copy` / `T.tile.add` 从前端 intrinsic 到底层 `call_extern("tl::ascend::copy_gm_to_ub<...>", ...)` 的 lowering 过程；
- 理解「尾块（tail block）」问题——当张量维度不是 block 的整数倍时，编译器如何在不改前端写法的前提下保证正确性；
- 掌握 `TL_ASCEND_TAIL_MASK` 开关背后的 valid-region 改写机制；
- 区分「向量化合法化」与「安全访存合法化」这两个保证 IR 合法性的 pass。

## 2. 前置知识

本讲假设你已经读过：

- **u3-l5（Element-wise 与 T.Parallel）**：知道 `T.Parallel` 经 `AscendLowerParallelToVector` 被降级为一串 `tl.ascend_*` intrinsic（如 `tl.ascend_add`、`tl.ascend_exp`），这些 intrinsic 就是本讲所说的「高层 tile op」。
- **u3-l2（T.copy）**：知道 `T.copy` 在 Ascend 上由 `AscendCopy::Lower` 按 scope 派发出 `copy_gm_to_ub` / `copy_l0c_to_gm` 等模板调用。
- **u6-l1（编译 Pass 全景）**：知道 `LowerAndLegalize` 遵循「先让它对、再让它快」，本讲四个 pass 全部落在 `LowerAndLegalize` 阶段。

两个关键术语先解释清楚：

- **尾块（tail block）**：前端按固定 `block_M × block_N` 切 tile，但真实张量维度 `M` 往往不是 `block_M` 的整数倍（例如 `M=130, block_M=32`，最后一块只有 `130 - 4×32 = 2` 行）。这「最后一块」就叫尾块。前端用 `T.ceildiv(M, block_M)` 数 block 数、用 `bx * block_M` 算偏移，从不为边界特判——边界由编译器兜底。
- **valid region（有效区域）**：一块物理大小为 `physRow × physCol` 的 UB tile，实际只有前 `validRow × validCol` 是真实数据，其余是 gap（垃圾/填充）。跨 lane 的算子（尤其是 reduce）绝不能把 gap 混进有效 lane。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/transform/lower_tile_op.cc](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/lower_tile_op.cc) | `LowerTileOp` pass：解析 layout 标注、把高层 tile op 调用展开为底层 IR |
| [src/op/ascend.cc](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/op/ascend.cc) | Ascend 各 op 的 `Lower()` 实现，其中 `compute_valid_extent` 产出 copy 的 valid 区域 |
| [src/transform/ascend_tail_mask_propagation.cc](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_tail_mask_propagation.cc) | `AscendTailMaskPropagation` pass：追踪 UB valid 区域并改写向量算子 |
| [src/transform/common/ascend_tail_mask.h](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/common/ascend_tail_mask.h) | tail mask 的数据模型（`TailMaskInfo` 等），供 pass 与未来消费者共享 |
| [src/transform/legalize_vectorized_loop.cc](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/legalize_vectorized_loop.cc) | `LegalizeVectorizedLoop` pass：把 `kVectorized` 循环真正展开 |
| [src/transform/legalize_safe_memory_access.cc](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/legalize_safe_memory_access.cc) | `LegalizeSafeMemoryAccess` pass：给越界 GM 访问加边界保护 |
| [tilelang/engine/phase.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/engine/phase.py) | `LowerAndLegalize`：把四个 pass 串起来的编排函数 |
| [examples/tail_mask/example_tail_add.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/tail_mask/example_tail_add.py) | 触发 tail mask 的最小 elementwise-add 示例 |
| [src/target/codegen_ascend.cc](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend.cc) | AscendC codegen：把 tail intrinsic 译成 `tl::ascend::tail_*` 模板调用 |
| [src/tl_templates/ascend/common.h](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h) | 模板库：`tail_unary/binary/scalar/reduce` 的真实实现 |

## 4. 核心概念与源码讲解

### 4.1 LowerTileOp：把高层 tile op 降为底层操作

#### 4.1.1 概念说明

前端写 `T.copy(...)` 或 `T.tile.add(...)` 时，TIR 里记录的只是一个 `Evaluate(Call)` 语句——调用某个 intrinsic（如 `npu_copy_v2`、`tl.ascend_add`）。这个 Call 本身「不可执行」，它只是一个**占位的高层语义**。`LowerTileOp` 的工作就是把这些占位 Call **展开（lower）**成真正的底层语句：copy 变成带 valid 区域参数的 `call_extern("tl::ascend::copy_gm_to_ub<...>", ...)`，element-wise op 变成带 `count` 参数的 `tl.ascend_add(...)` 调用。

同时，`LowerTileOp` 还承担一个与 layout 相关的职责：解析 block 上的 `kLayoutMap` 标注（由 u4-l4 的 `LayoutInference` 产出），把 buffer 按新 layout 重命名、并经 `layout->Forward()` 改写所有访问下标。这部分主要服务 GPU 的 fractal/swizzle，Ascend 路线同样经过这里。

> 注意：`LowerTileOp` 是 GPU 与 Ascend **共享**的通用 pass（注册名 `tl.LowerTileOp`）；Ascend 特有的 lowering 逻辑（如 valid 区域计算）封装在各 op 自己的 `Lower()` 实现里（copy 走 `AscendCopy::Lower`，位于 `src/op/ascend.cc`），`LowerTileOp` 只是个调度外壳。

#### 4.1.2 核心流程

```
对 PrimFunc 体做 IRMutator 遍历：
├─ 进入 Block：读 kLayoutMap 标注 → makeBufferWithLayout 重命名 buffer → 记 buffer_remap_
├─ 遇到 Evaluate(Call)：
│    ├─ ParseOperator(stmt) 尝试把 Call 解析成 TileOp
│    ├─ 解析失败 → 原样保留（如调用全局函数）
│    └─ 解析成功 → tile_op->Lower(LowerArgs{...}) 展开为底层 IR
│                   （copy: AscendCopy::Lower 产出 copy_gm_to_ub + valid 区域；
│                    element-wise: 产出 tl.ascend_add(dst,src0,src1,count)）
├─ 遇到 BufferLoad/BufferStore：经 layout->Forward() 改写下标
└─ 收尾：RemapBufferRewriter 用 buffer_remap_ 刷新 padding 注解
```

对 Ascend 而言，最关键的一步是 `tile_op->Lower(...)` 中 copy 的展开——它产出了后续 tail mask pass 要消费的 valid 区域参数。

#### 4.1.3 源码精读

pass 注册与外壳，注册名 `tl.LowerTileOp`：

[lower_tile_op.cc:485-493](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/lower_tile_op.cc#L485-L493) —— 把 `LowerTileOpPass::Substitute` 包成 `CreatePrimFuncPass`，priority 为 0。

lowering 的真正入口是 `VisitStmt_(EvaluateNode)`：每遇到一条 `Evaluate(Call)`，先用 `ParseOperator` 判断它是不是 tile op：

[lower_tile_op.cc:404-445](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/lower_tile_op.cc#L404-L445) —— `ParseOperator` 返回 `nullptr` 则原样返回；否则构造一个 `AddWorkspaceCallback`（让 op 在需要时申请临时 UB），读 `kDisableTMALower` 等配置，组装 `LowerArgs`，调用 `tile_op->Lower(LowerArgs{...}, analyzer_)`，再用返回的底层 IR 替换原语句。这段代码里的 `LowerArgs` 是传给每个 op 的「lowering 上下文」，包含 target、线程范围、layout 映射、buffer 重命名表等。

对于 Ascend 的 `T.copy`，`LowerArgs` 最终传到 `AscendCopy::Lower`（`src/op/ascend.cc`），它把切片 copy 展开为 `copy_gm_to_ub<...>` 并用如下公式计算有效区域：

[ascend.cc:410-418](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/op/ascend.cc#L410-L418) —— `compute_valid_extent` 的 clamp 公式。设 GM 维度为 `shape`、tile 偏移为 `min_val`、tile 大小为 `extent`，则有效长度为：

\[
\text{valid} =
\begin{cases}
\text{extent} & \text{shape} - \text{min\_val} \geq \text{extent} \quad (\text{整块，full})\\
\text{shape} - \text{min\_val} & \text{shape} - \text{min\_val} > 0 \quad (\text{尾块，tail})\\
0 & \text{otherwise} \quad (\text{完全越界，OOB})
\end{cases}
\]

这正是 u3-l2 讲过的「切片只给起点、搬运量由目标 buffer 决定」在边界处的精确化：当起点 + block 超出 GM 实际维度，搬运量被夹到剩余量。该公式同时用于 `gm2l1`/`l0c2gm`（Cube 路径）与 `gm2ub`/`ub2gm`（Vector 路径），覆盖 M/N/K 三个方向的尾块（参见 [test_tilelang_ascend_language_tail_block.py:20-31](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/testing/python/language/test_tilelang_ascend_language_tail_block.py#L20-L31) 的注释说明）。

layout 标注的处理在 Block 访问器里：

[lower_tile_op.cc:196-219](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/lower_tile_op.cc#L196-L219) —— 读 `kLayoutMap`，用 `makeBufferWithLayout` 为每个 buffer 生成按 layout 重排的新 buffer（`buffer_remap_`），遍历完子语句后替换 `alloc_buffers`、追加 op 申请的 workspace、并 `erase` 掉 `kLayoutMap` 注解（lowering 完即消费完毕）。

#### 4.1.4 代码实践

**实践目标**：亲眼看到 `T.copy` 被展开成带 `validRow/validCol` 的 `copy_gm_to_ub` 调用。

**操作步骤**（基于 [example_tail_add.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/tail_add/example_tail_add.py)，需 Ascend NPU 环境）：

1. 把示例里的 `M, N` 改成不被 block 整除的值，例如 `M=34, N=130, block_M=32, block_N=32`（与示例一致即可）。
2. 在脚本末尾、`func = tail_add(...)` 之后加一行打印生成代码：

   ```python
   func = tail_add(34, 130, 32, 32, dtype="float")
   print(func.get_kernel_source())
   ```

3. 在打印出的 C++ 源码里搜索 `copy_gm_to_ub`。

**需要观察的现象**：

- 对 `bx` 指向最后一块（`bx == 1`，对应第 32~33 行，只有 2 行有效）的核，`copy_gm_to_ub` 的 `validRow` 实参应是一个运行时表达式（类似 `Select(...)` 或 `Min(...)`），而不是常量 32；`validCol` 同理会因 `by` 指向 `N=130` 的尾块（130 - 4×32 = 2）而收缩。
- 对完整块的核，`validRow/validCol` 退化为常量 32。

**预期结果**：源码中出现形如 `tl::ascend::copy_gm_to_ub<...>(src, dst, strideN, validRow, validCol, ...)` 的调用，且 `validRow/validCol` 是依赖 block 坐标的表达式。

> 待本地验证：上述具体实参形式取决于 bisheng 展开，若无法在 NPU 上运行，至少应在 `get_kernel_source()` 输出里确认 `copy_gm_to_ub` 与含 `Min/Select` 的 valid 实参存在。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `LowerTileOp` 必须排在 `LayoutInference`（phase.py:67）之后、`AscendTailMaskPropagation`（phase.py:78）之前？

**参考答案**：`LowerTileOp` 要消费 `LayoutInference` 产出的 `kLayoutMap` 标注来改写 buffer 与下标；同时它把 `T.copy` 展开成带 `validRow/validCol` 的 `copy_gm_to_ub`，这正是 `AscendTailMaskPropagation` 的输入。顺序反了，tail mask pass 拿不到 valid 区域参数。

**练习 2**：`compute_valid_extent` 在 `remaining.dtype().lanes() > 1` 时直接返回 `extent`（不 clamp），为什么？

**参考答案**：`lanes() > 1` 表示该表达式是向量化的（ramp/lane 表达），说明此处已在向量展开内部、维度信息已折叠，无法再做标量 clamp，于是保守地按整块处理，把边界保护交给后续的 `LegalizeSafeMemoryAccess`。

---

### 4.2 AscendTailMaskPropagation：处理 UB tail 有效区域

#### 4.2.1 概念说明

`LowerTileOp` 之后，一条 `gm2ub` copy 已经带上了 `validRow/validCol`，告诉硬件「这块 UB 只前 `validRow × validCol` 是真数据」。但**后续的向量算子并不知道这件事**——一条普通的 `tl.ascend_add(dst, src0, src1, count)` 里，`count` 仍是整块大小 `physRow × physCol`，它会把 gap 里的垃圾也加进去。

对 element-wise 算子，这通常**不会出错**，因为 `ub2gm` 回写时同样会 clamp（只写回有效区域，gap 算了也白算）。但对**跨 lane 的 reduce**（如沿某轴求和/求最大），把 gap 混进有效 lane 会让结果被垃圾污染——这就是为什么 reduce 必须用 u3-l4 讲的 `real_shape` 显式告知有效范围。

`AscendTailMaskPropagation` 提供了第三条路：**追踪每个 UB buffer 的有效区域，把下游的向量算子改写成 tail 变体**（`tl.ascend_tail_add` 等），让算子只计算有效区域。它由 `TL_ASCEND_TAIL_MASK` 开关显式开启（默认关），与「pad_value 填充」「`real_shape`」三套机制并存、互为补充。

#### 4.2.2 核心流程

```
对每个 UB data Var 维护一个 TailMaskInfo {valid_row, valid_col, physical_row, physical_col}
├─ 遇到 copy_gm_to_ub：读 args[4,5,7,8] 作 validRow/validCol/physRow/physCol
│                     若静态满 → kFull（后续不改写）；否则 → kTail，记入 state_
├─ 遇到 copy_ub_to_ub：dst 继承 src 的 mask
├─ 遇到向量算子（add/exp/reduce/...）：
│    ├─ 取操作数 mask，binary 取两边交集（IntersectMasks）
│    ├─ 一组守卫判定能否改写：dtype 是 float/bfloat、count==physRow*physCol（CleanTail）、
│    │   valid 表达式里没有出作用域的循环变量、不是 broadcast-scalar mask
│    └─ 通过 → 改写为 tl.ascend_tail_<unary|binary|scalar|reduce>(tag, ..., valid_row, valid_col, phys_col)
│       不通过 → 原 op 保留（mask 可能仍向下游传播）
└─ codegen 把 tail intrinsic 译成 tl::ascend::tail_* 模板，模板内部只算有效区域
```

#### 4.2.3 源码精读

pass 自门控，开关默认关——未开启时直接返回原函数，非尾块 kernel 完全不受影响：

[ascend_tail_mask_propagation.cc:519-531](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_tail_mask_propagation.cc#L519-L531) —— 读 `tl.ascend_tail_mask`（即 `TL_ASCEND_TAIL_MASK`），为假则 `return f`。注意 `phase.py` 调用时传的是 `rewrite_reduce=False`，即 **reduce 当前不被改写**（注释说明 `tail_reduce` 的 `ReduceSum<AR>` 路径有输出布局 bug，留待 batch 2），reduce 仍走 `real_shape` + pad_value 全块路径。

数据模型 `TailMaskInfo` 与构造逻辑在共享头文件里，刻意做得很轻以便复用：

[ascend_tail_mask.h:53-62](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/common/ascend_tail_mask.h#L53-L62) —— `valid_row/valid_col` 是逻辑有效矩形（**可以是运行时表达式**，如对 block 坐标的 `Select`），`physical_row/physical_col` 是 UB tile 的物理尺寸（决定 repeat stride）。`kind` 区分 `kFull`（静态满，不改写）/`kTail`（2D 尾块）/`kPackedCmp`（留给 batch 2 的 compare/select）。

[ascend_tail_mask.h:95-110](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/common/ascend_tail_mask.h#L95-L110) —— `MakeCopyMask`：若 `valid == physical`（静态满）返回 `kFull`，否则返回 `kTail`。这个「静态满判别」是保证非尾块 kernel 生成代码完全不变的关键。

mask 的「源头」是 gm2ub copy：

[ascend_tail_mask_propagation.cc:264-287](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_tail_mask_propagation.cc#L264-L287) —— `HandleGmToUbCopy` 按固定参数布局取值：`args[4]=validRow, args[5]=validCol, args[7]=physRow, args[8]=physCol`（1D tile 时 physRow 省略为 1），写入 `state_[dst_var]`。这正是 4.1 节 `LowerTileOp` 产出的 copy 的下游消费者。

改写的派发与守卫：

[ascend_tail_mask_propagation.cc:341-361](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_tail_mask_propagation.cc#L341-L361) —— `RewriteBinary`：取两操作数 mask 的交集 `IntersectMasks`，再用一组守卫判定 `ok`：

- `CleanTail(count, out)` —— 算子的 `count` 必须等于 `physRow × physCol`，否则 2D 模型不成立；
- `SupportedTailDtype` —— 仅 float / bfloat16 有验证过的 tail helper，int/uint 走全块路径；
- `!HasOutOfScopeLoopVar` —— valid 表达式若引用了已出作用域的循环变量（如 copy 在 `for by` 内播种 `valid_col=f(by)`、reduce 在循环外），改写会产生未声明标识符，必须 bail；
- `!IsBroadcastScalarMask` —— `valid_col==1` 但 `phys_col>1` 的标量广播场景，改写会只算 1 个元素而非整行广播，必须 bail。

通过后发射 `tl.ascend_tail_binary(tag, dst, src0, src1, valid_row, valid_col, phys_col)`，把运行时有效区域随身带走。

下游 codegen 把它译成模板调用：

[codegen_ascend.cc:2381-2398](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend.cc#L2381-L2398) —— `TailUnaryOpCodegen` 生成 `tl::ascend::tail_unary<dtype>(TailVecUnOp::tag, dst, src, vrow, vcol, pcol);`。注意它先把 `vrow/vcol/pcol` 表达式作为 let-binding 输出到语句位置（因为它们可能含嵌套 `Select/Min`，base codegen 会把它们降级成 `condval_N` 中间变量）。

模板内部的三段式策略是这套机制能既正确又高效的核心：

[common.h:729-760](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h#L729-L760) —— `tail_binary` 按有效列宽分三种走法：

1. `validCol == physCol`（整列有效）：直接调 `TailApplyBinCount` 按总元素数一次向量化，最快；
2. `validCol` 较小且对齐：用带 mask 的 `BinaryRepeatParams`（`TailApplyBinMask`），靠 `repStride` 跳过 gap；
3. 兜底：退化为逐行 `for (r = 0; r < validRow; ++r)` 的标量循环（`TailApplyBinCount` 每行一次），性能差但永远正确。

> 这个「快路径 + mask + 逐行兜底」的分级，正好呼应头文件注释里「物理 pitch 不一致时是性能问题而非正确性问题」——`IntersectMasks` 取两边 min 保证正确，模板内部再尽力选快路径。

#### 4.2.4 代码实践

**实践目标**：开启 `TL_ASCEND_TAIL_MASK`，对比同一 kernel 生成代码里 `T.tile.add` 的两种形态。

**操作步骤**：

1. 复制 [example_tail_add.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/tail_add/example_tail_add.py)，准备两份 pass_configs：一份带 `TL_ASCEND_TAIL_MASK: True`（如示例 [example_tail_add.py:15-22](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/tail_add/example_tail_add.py#L15-L22)），一份去掉这一行。
2. 分别 `func = tail_add(34, 130, 32, 32, ...)` 并 `print(func.get_kernel_source())`。
3. 在两份输出里搜索 add 相关调用。

**需要观察的现象**：

- **关闭**时：看到普通的 `AscendC::Add(dst, src0, src1, count)`（或 `tl::ascend::add`），`count` 是整块 `32*32=1024`，对尾块核也算满整块（gap 算了但回写时被 clamp 掉）。
- **开启**时：尾块核的 add 被替换为 `tl::ascend::tail_binary<float>(TailVecBinOp::Add, dst, src0, src1, validRow, validCol, physCol)`，且 `validRow/validCol` 是依赖 block 坐标的运行时表达式（如 `by==4` 时 `validCol` 收缩到 2）。

**预期结果**：两份代码数值结果一致（`torch.testing.assert_close(c, a+b)` 都通过），但开启版的尾块核不再对 gap 做无用计算。这正是测试 [test_tilelang_ascend_language_tail_block.py:265-270](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/testing/python/language/test_tilelang_ascend_language_tail_block.py#L265-L270) 用 `@parametrize(tail_mask, [False, True])` 双跑同一 kernel 要验证的——「结果必须两种方式都匹配」。

> 待本地验证：精确的实参表达式形态需在 NPU 上 `get_kernel_source()` 后确认；若无 NPU，可对照 [codegen_ascend.cc:2400-2416](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend.cc#L2400-L2416) 的 `TailBinaryOpCodegen` 推导应输出格式。

#### 4.2.5 小练习与答案

**练习 1**：为什么 reduce 在当前实现里（`rewrite_reduce=False`）不被改写成 `tail_reduce`，却仍能正确处理尾块？

**参考答案**：reduce 走的是另一套机制——前端 `T.reduce_*` 的 `real_shape=[rows, cols]` 参数（u3-l4）直接告诉 reduce 它的逻辑有效范围，reduce 模板从头就不碰 gap。`tail_reduce` 路径因 `ReduceSum<AR>` 输出布局 bug 暂被关闭，但 `real_shape` 已保证正确性，故 `TL_ASCEND_TAIL_MASK` 开启与否对 reduce 结果无影响（测试 [test_tilelang_ascend_language_tail_block.py:339-344](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/testing/python/language/test_tilelang_ascend_language_tail_block.py#L339-L344) 专门断言这点）。

**练习 2**：`HasOutOfScopeLoopVar` 守卫要解决什么编译错误？举一个会触发它的场景。

**参考答案**：若 `copy_gm_to_ub` 在 `for by` 循环内播种 `valid_col = f(by)`，而某个 reduce 在该循环**外**执行，直接把 `valid_col` 表达式塞进 `tail_reduce` 会让 `by` 成为未声明标识符，bisheng 编译报错。守卫检测到 `valid_col` 引用了不在作用域内的循环变量就 bail，退回全块路径。这体现「运行时有效区域」带来的作用域管理负担。

**练习 3**：`IsBroadcastScalarMask` 为什么要对 `valid_col==1 && phys_col>1` 特判？

**参考答案**：这是把一个 1D 标量（如每通道 scale）拷进多元素 UB tile 的场景。对 copy 而言 `valid_col=1` 是对的（只有一个标量元素），但下游算子会把它广播到整行；若此时把下游改写成 `valid_col=1` 的 tail helper，就只会算 1 个元素而非整行广播，结果错误。故必须 bail，让全块路径（读 pad 填充的 gap）处理广播。

---

### 4.3 LegalizeVectorizedLoop：向量化循环合法化

#### 4.3.1 概念说明

TIR 的 `For` 节点有个 `ForKind` 提示，其中 `kVectorized` 表示「这个循环请向量化」。但在 lowering 中后期，带这个提示的循环必须被**真正展开**成连续的标量语句（按向量化因子展开），否则后续 pass 与 codegen 拿到的是一个「声明了要向量化但还没展开」的循环，属于非法 IR。`LegalizeVectorizedLoop` 就是干这件事的合法化 pass。

它和 u3-l5 的 `AscendLowerParallelToVector` 是两回事：后者把 `T.Parallel`（`kParallel`）**翻译**成 `tl.ascend_*` 向量 intrinsic；本 pass 处理的是剩余的、仍以 `kVectorized` 循环形式存在的结构（多见于上游 TVM 通用 lowering 或动态 shape 路径），把它们落定为 `kSerial` 并调用 `VectorizeLoop` 展开。

#### 4.3.2 核心流程

```
遍历所有 For 节点：
└─ 若 ForKind == kVectorized：
     ├─ 改 kind 为 kSerial
     └─ VectorizeLoop(for)  // TVM 工具：按 extent 把循环体复制展开成连续语句
```

#### 4.3.3 源码精读

[legalize_vectorized_loop.cc:65-77](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/legalize_vectorized_loop.cc#L65-L77) —— `VisitStmt_(ForNode)`：先递归访问子节点，若 kind 不是 `kVectorized` 直接返回；否则改成 `kSerial` 并 `return VectorizeLoop(std::move(for_node))`。`VectorizeLoop` 来自 TVM 的 `loop_vectorize.h`，它把 `for i in [0, n)` 的循环体按向量化因子展开。

pass 注册：[legalize_vectorized_loop.cc:80-88](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/legalize_vectorized_loop.cc#L80-L88)，注册名 `tl.LegalizeVectorizedLoop`，无开关、总是执行（无条件合法化）。

#### 4.3.4 代码实践

**实践目标**：理解本 pass 是「收尾合法化」，而非 Ascend 向量化的主路径。

**操作步骤**（源码阅读型实践，无需 NPU）：

1. 在 [phase.py:65](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/engine/phase.py#L65) 确认 `AscendLowerParallelToVector` 已把 `T.Parallel` 降为 `tl.ascend_*` intrinsic。
2. 跟踪这些 intrinsic：它们是 `Evaluate(Call)`，不是 `kVectorized` 循环，故不会被本 pass 触碰。
3. 在 `OptimizeForTarget` 阶段还有一处 [phase.py:109](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/engine/phase.py#L109) 的 `VectorizeLoop`（受 `tir.disable_vectorize` 控制），那是另一套上游向量化。

**需要观察的现象**：Ascend 上绝大多数向量化由 `AscendLowerParallelToVector` 完成；本 pass 主要兜底处理上游通用 lowering 残留的 `kVectorized` 循环（如 CPU 后端路径、动态 shape 的 `LoopVectorizeDynamic` 残留）。

**预期结果**：经过本 pass 后，IR 中不再有 `kVectorized` 的 For 节点——它们要么已是 `tl.ascend_*` intrinsic，要么被展开为 `kSerial`。

#### 4.3.5 小练习与答案

**练习**：本 pass 与 `OptimizeForTarget` 里的 `VectorizeLoop`（phase.py:109）都叫「向量化」，为什么不合并？

**参考答案**：二者作用阶段与对象不同。本 pass 在 `LowerAndLegalize` 末尾，做的是**合法化**——把已声明 `kVectorized` 但未展开的循环展开掉，保证 IR 合法；`phase.py:109` 的 `VectorizeLoop` 在 `OptimizeForTarget`，做的是**优化**——主动把可向量化的 `kSerial` 循环识别并转成向量指令（受 `allow_vectorize` 控制）。一个收尾、一个主动优化，职责不同，且分属两个阶段。

---

### 4.4 LegalizeSafeMemoryAccess：安全访存合法性

#### 4.4.1 概念说明

`LowerTileOp` 的 `compute_valid_extent` 只在 copy 路径上 clamp 了搬运量。但 IR 里仍可能存在**直接对 GM 的标量访问**（如 `A[bx*block_M + i]`），当 `bx*block_M + i` 可能超出 GM 实际维度时，硬件会越界访存。`LegalizeSafeMemoryAccess` 给这类「无法静态证明在界内」的 GM 访问加上运行时边界保护：用 `IfThenElse` 把整条语句包起来，越界则不执行（GM 写）或填 padding 值（shared/local 写）。

它和尾块机制是互补的：尾块处理的是「整块 copy 的有效区域」，本 pass 处理的是「单点/零散访问的边界保护」，是最后一道合法性防线。

#### 4.4.2 核心流程

```
只在「叶子循环」（无内层循环的 For）上触发：
├─ GlobalMemChecker 扫描循环体里所有 BufferLoad/BufferStore
│    └─ 对 global buffer 的每个含变量的下标 index：
│         ├─ 若 analyzer 无法证明 index < shape_dim → 收集条件 index < shape_dim
│         └─ 若 analyzer 无法证明 index >= 0      → 收集条件 index >= 0
├─ SafeMemorysRewriter 对每个 store：
│    ├─ 目标是 global：用 IfThenElse(cond, store) 包裹（越界不写）
│    ├─ 目标是 shared/local：把 value 改成 if_then_else(cond, value, padding)
│    └─ store.value 已是 IfThenElse（用户手写边界）→ 跳过并告警
└─ 同样处理 call_extern 形式的访问（如 atomicAdd）
```

#### 4.4.3 源码精读

收集越界条件的访问器：

[legalize_safe_memory_access.cc:91-128](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/legalize_safe_memory_access.cc#L91-L128) —— `CheckBufferIndices`：只对**含变量**的下标检查（纯常量下标编译期已确定，无需运行时保护）。用 `analyzer_->CanProve(index < shape_dim)` 与 `CanProve(index >= 0)` 判定，证不出来就收集为条件。这体现了「能证明就信、证不出来就加保护」的保守策略。

改写逻辑按 buffer scope 分三种：

[legalize_safe_memory_access.cc:146-203](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/legalize_safe_memory_access.cc#L146-L203) —— `VisitStmt_(BufferStoreNode)`：

- **global**：`IfThenElse(cond, store_with_conditions)` 逐层包裹，越界时整条 store 不执行；
- **shared / local**：把 `store.value` 改成 `if_then_else(cond, value, GetPadding(buffer))`，越界位置写 padding（默认 0，可用 `T.annotate` 的 `kPaddingMap` 覆盖）；
- 若 `store.value` 本身已是 `IfThenElse`（用户手写边界），打 `LOG(WARNING)` 跳过，避免双重包裹。

只在叶子循环触发：

[legalize_safe_memory_access.cc:282-309](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/legalize_safe_memory_access.cc#L282-L309) —— `VisitStmt_(ForNode)` 用 `HasInnerLoop` 判断，只有无内层循环的 For 才调 `SafeMemorysRewriter`。原因是边界条件绑定的是具体下标，在最内层循环处加保护粒度最准、冗余最少。

pass 注册与开关：

[legalize_safe_memory_access.cc:341-354](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/legalize_safe_memory_access.cc#L341-L354) —— 注册名 `tl.LegalizeSafeMemoryAccess`，开关 `tl.disable_safe_memory_legalize`（默认关 = 默认启用本 pass）。

#### 4.4.4 代码实践

**实践目标**：观察一个可能越界的 GM 写被 `IfThenElse` 包裹。

**操作步骤**（源码阅读型，可在无 NPU 时推理）：

1. 设想一个 kernel：循环上限是静态的 `block_M`，但写入 `C[bx*block_M + i]`，当 `bx` 指向尾块时 `bx*block_M + i` 可能 ≥ `M`。
2. 跟踪：`compute_valid_extent` clamp 的是 copy，不直接管这种标量 store；本 pass 的 `GlobalMemChecker` 会发现 `bx*block_M + i < M` 无法静态证明。
3. 推导改写结果：`if ((bx*block_M + i) < M) { C[bx*block_M + i] = ...; }`。

**需要观察的现象**：`get_kernel_source()` 里尾块核对应的 GM store 外层多出一层 `if` 条件保护；完整块核因下标可静态证明在界内，不 extra 保护。

**预期结果**：越界访问被运行时条件挡住，硬件不会真正越界写。

> 待本地验证：实际是否触发取决于 `analyzer` 能否在 `LowerTileOp`/`Simplify` 后证明下标范围；若 copy clamp 已足够，本 pass 可能无额外产出（这正是「能证明就信」的体现）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 shared/local buffer 的越界用「填 padding」而非「整条不执行」？

**参考答案**：shared/local 是固定大小的片上 buffer，下标越界时位置本身仍存在（不会段错误），但写垃圾会污染后续计算。填 padding（通常是 0）既保证该位置有确定值，又不影响后续向量指令的连续访存模式；而 GM 越界会真正段错误，必须整条 store 跳过。

**练习 2**：本 pass 默认启用，开关 `tl.disable_safe_memory_legalize` 何时会用到？

**参考答案**：当用户能保证所有访问都在界内（如 shape 恒被 block 整除、或已用 `T.ceildiv` + copy clamp 完全覆盖），可关掉本 pass 去掉冗余的 `if` 保护以提升性能。它是「安全优先、可按需关闭」的设计。

---

## 5. 综合实践

把本讲四个 pass 串成一个端到端的观察任务，用 [example_tail_add.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/tail_add/example_tail_add.py) 作为载体：

1. **设置非对齐 shape**：`M=34, N=130, block_M=32, block_N=32`（M、N 都不被 block 整除，行列方向都产生尾块）。
2. **对照两组配置**：`TL_ASCEND_TAIL_MASK` 分别取 `False` 与 `True`，其余配置不变。
3. **打印并对比生成代码** `func.get_kernel_source()`，定位以下四个 pass 的产物：
   - **LowerTileOp**：找到 `copy_gm_to_ub`，确认 `validRow/validCol` 是依赖 `bx/by` 的运行时表达式（尾块核收缩、完整块核为常量 32）。
   - **AscendTailMaskPropagation**：开启版里，尾块核的 add 从 `AscendC::Add(..., 1024)` 变为 `tl::ascend::tail_binary<float>(TailVecBinOp::Add, ..., validRow, validCol, physCol)`。
   - **LegalizeSafeMemoryAccess**：检查 `ub2gm` 的回写 store 是否被 `if (offset < M)` 类条件保护（若 copy clamp 已覆盖则可能无额外 `if`）。
   - **正确性**：两种配置都应通过 `torch.testing.assert_close(c, a + b, rtol=1e-2, atol=1e-2)`。
4. **画一张数据流图**：`GM(A) --copy_gm_to_ub(validRow,validCol)--> a_ub(tail) --tail_binary(valid)--> c_ub(tail) --copy_ub_to_gm(clamp)--> GM(C)`，标注每一步的有效区域如何传递与收缩。

> 待本地验证：步骤 3 的精确代码形态需在 Ascend NPU 上运行 `get_kernel_source()` 确认；步骤 4 的数值正确性需 `.npu()` 张量验证。无 NPU 时，可对照本讲引用的 codegen 与模板源码推导预期输出。

## 6. 本讲小结

- **LowerTileOp** 是高层 tile op 的总展开器：解析 `kLayoutMap` 重排 buffer，并把 `T.copy`/element-wise 等 intrinsic 经各 op 的 `Lower()` 展开为底层 IR；Ascend 的 valid 区域由 `AscendCopy::Lower` 里的 `compute_valid_extent` clamp 公式产出。
- **尾块问题**的根源是张量维度不被 block 整除，前端从不特判，全靠编译器兜底；存在 **pad_value 填充**、**reduce 的 `real_shape`**、**tail mask 改写**三套并存机制。
- **AscendTailMaskPropagation**（开关 `TL_ASCEND_TAIL_MASK`，默认关）追踪每个 UB 的 valid 区域，把下游向量算子改写成只算有效区域的 `tl.ascend_tail_*` 变体；改写受 dtype、count 匹配、作用域、broadcast 等一组守卫约束，不满足则 bail 回全块路径。
- 改写后的 tail intrinsic 经 codegen 译成 `tl::ascend::tail_unary/binary/scalar/reduce` 模板，模板内部按「整列向量化 → mask 向量 → 逐行兜底」三级选路，保证正确且尽量高效。
- **LegalizeVectorizedLoop** 是无条件合法化，把残留的 `kVectorized` 循环展开为 `kSerial`，区别于 `AscendLowerParallelToVector` 的主动翻译。
- **LegalizeSafeMemoryAccess**（默认开，`tl.disable_safe_memory_legalize` 可关）给无法静态证明在界内的 GM 访问加运行时 `IfThenElse` 保护，GM 越界跳过、shared/local 越界填 padding，是合法性最后一道防线。

## 7. 下一步学习建议

- **继续往下读 codegen**：本讲的 tail intrinsic 如何最终变成 `tl::ascend::tail_binary` 模板，属于 u6-l2（Ascend C / PTO 双 Codegen）与 u6-l3（tl_templates 模板库）的范畴，建议结合这两讲把 `tail_*` 模板的 `TailApplyBinCount/Mask` 实现读完。
- **回看 reduce 的另一条路**：对比 u3-l4 的 `real_shape` 与本讲的 `tail_reduce`（当前关闭），理解为何 Ascend 对 reduce 的尾块处理选择 `real_shape` 而非 pad_value（PTO 后端对切片 load 发 `PadValue::Null`，pad 不可靠）。
- **进入实战**：尾块机制在 FlashAttention 等 CV 融合算子里普遍存在，建议在 u7-l1（FlashAttention 实现案例）里留意 `T.ceildiv` 与 copy clamp 如何配合，把本讲机制放到真实算子中验证。
- **动手扩展（进阶）**：阅读 [ascend_tail_mask_propagation.cc](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_tail_mask_propagation.cc) 文件头注释提到的「Batch 2: compare/select/broadcast」，思考 `kPackedCmp` 这个 mask kind 未来如何支持比较类算子的尾块。

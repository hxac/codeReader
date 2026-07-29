# TritonToUnstructure：离散访存标量化

> 承接：本讲是「Ascend 编译后端 MLIR pass 流水线」单元的第四篇。在 [u4-l2](u4-l2-triton-to-structured.md) 我们看到 `TritonToStructured` 把「连续块访存」结构化；在 [u4-l3](u4-l3-discrete-mask-access-conversion.md) 我们看到 `discrete-mask-access-conversion` 把离散掩码降级为 `select`。但这两者都没有解决一个更根本的问题：当访存的**地址本身是离散的（indirect / gather）**——例如 `ptr = base + indices_tensor`，其中 `indices_tensor` 的元素在编译期无法归纳成「基址 + 等差步长」的矩形块——硬件的向量化 load/store 单元就无法直接处理。本讲讲解 `triton-to-unstructure` 如何把这类访存**展开成标量循环**，以及紧随其后的 `bubble-up-operation` 如何把被展开拖慢的 `tensor.extract` 重新「上提」以恢复向量化。

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚「结构化（structured）访存」与「非结构化（unstructured）访存」的区别，以及为什么后者必须标量化。
- 读懂 `UnstructuredMemAccessConverter` 的核心逻辑：它如何按维度遍历，对结构化维保留向量化、对非结构化维套 `scf.for` 循环，最终把一条 `tt.load/store/atomic` 拆成「多循环 + extract/insert + 标量访存」。
- 理解 `bubble-up-operation` 的「上提」思想：把 `tensor.extract` 推过它的定义算子，让 extract 发生在更小的数据上，从而让下游重新识别出可向量化的形态。
- 掌握该 pass 在「纯 SIMD 模式」与「unstructured_in_simt 混合模式」下的**回退语义**：何时走标量循环、何时走 SIMT 间接访存快路径（`indirect_load/store`）。

## 2. 前置知识

本讲默认你已掌握：

- **TTIR 与 `tt.load/store`**：Triton 用「指针张量」`tensor<Nx!tt.ptr<T>>` 描述一批地址，`tt.load` 一次性读出 `tensor<NxT>`。详见 [u1-l4](u1-l4-first-kernel-vector-add.md)。
- **MLIR 的 `scf.for` 与 `tensor.extract/insert`**：`scf.for` 是结构化循环，`iter_args`/`yield` 用来在循环间传递不断更新的张量（SSA 风格）；`tensor.extract` 从张量取一个标量元素，`tensor.insert` 写回一个标量元素。
- **u4-l1 讲过的 pass 流水线**：`triton-to-unstructure` 处于 `ttir_to_linalg` 的中段，位于 `discrete-mask-access-conversion` 之后、`triton-to-linalg` 之前，是 TTIR→Linalg 的「预处理」之一。
- **compile_mode 三模式**（[u6-l1](u6-l1-compile-mode-overview.md) 会详讲）：`simd`（纯向量化）、`unstructured_in_simt`（默认，结构化走 SIMD、离散走 SIMT）、`simt_only`（纯 SIMT，跳过整条 linalg 主线，因此**不经过本 pass**）。

一个直觉：NPU 的向量化访存单元一次搬运一「条」连续内存。若你给它的地址表是 `[0, 3, 7, 1, ...]` 这种「东一榔头西一棒子」的散点地址，硬件没法批量搬，只能一个地址一个地址地读。`triton-to-unstructure` 做的，就是把这种「硬件干不了的批量操作」翻译成「硬件能干的标量循环」。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [Passes.td](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/include/TritonToUnstructure/Passes.td) | 用 TableGen 注册 `triton-to-unstructure` 与 `bubble-up-operation` 两个 pass 及其命令行选项。 |
| [OffsetAnalysis.h](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/include/TritonToUnstructure/OffsetAnalysis.h) | `PtrOffsetInfo` 数据结构：把每个指针的「偏移性质」分类为 structured / unstructured / scalarlike。 |
| [UnstructureConversionPass.h](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/include/TritonToUnstructure/UnstructureConversionPass.h) | `UnstructuredMemAccessConverter` 模板类声明，以及一段极其重要的「转换前后」对照注释。 |
| [UnstructureConversionPass.cpp](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToUnstructure/UnstructureConversionPass.cpp) | 转换主逻辑：标量循环生成 + 950 SIMT 间接访存快路径 + 回退。 |
| [BubbleUpOperation.h](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/include/TritonToUnstructure/BubbleUpOperation.h) / [.cpp](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToUnstructure/BubbleUpOperation.cpp) | `BubbleUpExtract` 模式：把 extract 推过父算子。 |
| [compiler.py](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py) | 把两个 pass 接入 `ttir_to_linalg` 流水线。 |
| [unstructure_mix.mlir](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/unittest/Conversion/General/TritonToUnstructure/unstructure_mix.mlir) / [bubbleupoperation.mlir](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/unittest/Conversion/General/TritonToUnstructure/bubbleupoperation.mlir) | FileCheck 回归测试，是观察 IR 变化的最佳样本。 |

## 4. 核心概念与源码讲解

### 4.1 triton-to-unstructure：离散访存标量化的总思路

#### 4.1.1 概念说明

在 TTIR 里，一次 `tt.load %ptr : tensor<MxNx!tt.ptr<T>>` 读出 `tensor<MxNxT>`。这隐含一个假设：这 M×N 个地址「排列整齐」，硬件能用向量化指令一次性搬。

但当 `%ptr` 是由一张**数据相关的索引张量**计算出来时（典型场景：gather、`index_select`、attention 里的 `q @ k` 间接寻址），地址之间没有等差规律。u4-l2 的 `TritonToStructured` 用 `PtrState` 试图把地址归纳成「基址 + 每维 (stride, shape)」的矩形；归纳失败时，这些访存就留给本 pass。

`triton-to-unstructure` 的策略是**化整为零**：既然硬件不能批量搬这堆散点地址，那就把它**展开成一串标量访存**——为每个「离散维度」生成一层 `scf.for` 循环，循环体内用 `tensor.extract` 取出第 i 个地址、做一次标量 `tt.load`、再用 `tensor.insert` 把结果塞回结果张量。这样，无论地址多散，最终都变成硬件一定能执行的「标量访存序列」。

关键术语：

- **DiscreteMemAccess**：本 pass 给展开出来的访存/extract/insert 打的属性标记（字符串 `"DiscreteMemAccess"`），见 [Utils.h](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/include/Utils/Utils.h#L48-L49)，防止下游 pass 把它误当成普通访存再处理。
- **ExtractedLoadOrStore**：打在生成的 `scf.for` 上的标记，表示「这个循环体是一次离散访存的展开」。

#### 4.1.2 核心流程

整个 pass 由「分析」与「重写」两阶段组成，在 [UnstructureConversionPass.cpp 的 runOnOperation](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToUnstructure/UnstructureConversionPass.cpp#L844-L904) 里编排：

```text
1. 遍历所有 FuncOp，replacePtrArguments 处理指针参数
2. processIfYieldAddHoistOperations：把 scf.if 里 yield 的 add 计算上提（预处理）
3. 【分析阶段 A】runPreparse：对每个循环的 iter_args/yield 做指针偏移分析
4. 【分析阶段 B】runParse：对每个 load/store/atomic 的 ptr 调 parse()，
   得到 offsetMap[ptr] = PtrOffsetInfo（该指针每个维度的 structured/unstructured 标记）
5. 【重写阶段】用 applyPatternsGreedily 跑 4 个 UnstructuredMemAccessConverter
   模板实例（Load/Store/AtomicRMW/AtomicCAS）
6. 末尾跑 CSE + Canonicalizer 收尾
```

重写阶段对每个访存 op 的决策树（见 [matchAndRewrite](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToUnstructure/UnstructureConversionPass.cpp#L448-L762)）：

```text
对 tt.load/store/atomic 的 ptr：
  ├─ 全是结构化维 且 形状非全 1 → 直接返回 failure()（不需要本 pass，留给 linalg）
  ├─ 是 scalarLike（所有元素相同，如 splat/broadcast 到标量）→ 走 splatAndLoadScenario
  │                                                      （取一个标量再 splat 回去）
  ├─ 【950 快路径闸门开启 且 满足条件】→ tryRewriteIndirectFastPath
  │      tt.load  → tt.indirect_load        （rank ≤ 5）
  │      tt.store → tt.indirect_store       （rank ≤ 5）
  │      atomic   → hivm.hir.custom "__builtin_indirect_atomic"（静态形状）
  │      └─ 快路径失败 → 继续往下（回退）
  └─ 【标量循环回退】按维度从外到内：
        structured 维 → 保留，extract_slice 整段
        unstructured 维 → 套一层 scf.for，extract 单个元素
```

#### 4.1.3 源码精读

**pass 注册与选项**。两个 pass 在 [Passes.td](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/include/TritonToUnstructure/Passes.td#L6-L28) 定义。`triton-to-unstructure` 有三个选项：

- `force-scalarize-mode`：即使有结构化维混合，也强制全部标量化（默认 `false`）。
- `compile-on-910-95`：是否在 910_95/950 代硬件上编译（控制快路径与若干内部行为，默认 `false`）。
- `force-simt-template`：是否启用 SIMT 间接访存模板（默认 `false`）。

**接入流水线**。[compiler.py:207-211](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L207-L211) 把它俩串进 `ttir_to_linalg`：

```python
ascend.passes.ttir.add_triton_to_unstructure(pm, compile_on_910_95, force_simt_template)
ascend.passes.ttir.add_triton_to_hivm(pm)
ascend.passes.ttir.add_triton_to_hfusion(pm, compile_on_910_95)
ascend.passes.ttir.add_triton_to_llvm(pm)
ascend.passes.ttir.add_bubble_up_operation(pm)        # 紧随其后
```

注意 `bubble-up-operation` 在 `triton-to-unstructure` **之后**才跑——因为标量化会产生大量 `tensor.extract`，bubble-up 正是来「收拾残局」的。

**转换前后的经典对照**。源码头文件里写了一段绝佳的文档化示例，[UnstructureConversionPass.h:56-83](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/include/TritonToUnstructure/UnstructureConversionPass.h#L56-L83)。转换前是一条 `tt.addptr` + `tt.load` 的批量间接访存：

```mlir
%0 = tt.load %structured : tensor<128x128x!tt.ptr<i32>>   // 读出一张「地址偏移表」
%1 = tt.addptr %ptr_2, %0                                  // base + 偏移表 = 散点地址表
%2 = tt.load %1 : tensor<128x128x!tt.ptr<f32>>            // 用散点地址批量 load
```

转换后变成双层 `scf.for`，逐元素读、逐元素写回（节选）：

```mlir
%1 = tensor.empty() : tensor<128x128xf32>
%2 = scf.for %arg2 = %c0 to %c128 step %c1 iter_args(%arg3 = %1) -> (tensor<128x128xf32>) {
  %4 = scf.for %arg4 = %c0 to %c128 step %c1 iter_args(%arg5 = %arg3) -> (tensor<128x128xf32>) {
    %extracted = tensor.extract %0[%arg2, %arg4] {DiscreteMemAccess} : tensor<128x128xi32>
    %5 = arith.extsi %extracted : i32 to i64
    %6 = tt.addptr %base, %5
    %7 = tt.load %6 {DiscreteMemAccess} : !tt.ptr<f32>
    %inserted = tensor.insert_slice %7 into %arg5[%arg2, %arg4] ...
    scf.yield %inserted
  }
  scf.yield %4
}
```

这就是「离散访存标量化」的全貌：一条批量 load，被改写成两层标量循环。

#### 4.1.4 代码实践

**实践目标**：用 `triton-opt` 直接驱动本 pass，肉眼对比一条间接访存在标量化前后的 IR。

**操作步骤**：

1. 在构建产物中找到 `triton-opt`（由 CMake 产出，与 `triton-mlir-opt` 同目录；若未单独安装，源码安装后通常在 `build` 目录或 python 包的 bin 下）。
2. 准备一个最小输入 `demo.mlir`（取自仓库测试，可简化）：

```mlir
tt.func @demo(%base: !tt.ptr<f32>, %idx: tensor<128xi32>, %off: !tt.ptr<i32>) {
  %o = tt.splat %off : !tt.ptr<i32> -> tensor<128x!tt.ptr<i32>>
  %tab = tt.load %o : tensor<128x!tt.ptr<i32>>     // 偏移表
  %ptrs = tt.addptr %o, %tab : tensor<128x!tt.ptr<i32>>, tensor<128xi32>
  %v = tt.load %ptrs : tensor<128x!tt.ptr<f32>>    // 离散访存
  tt.return
}
```

3. 运行：

```bash
triton-opt --triton-to-unstructure demo.mlir
```

**需要观察的现象**：输出里原来的单条 `%v = tt.load %ptrs` 消失，取而代之的是一个 `scf.for`，循环体里有带 `{DiscreteMemAccess}` 的 `tensor.extract`、标量 `tt.load`、`tensor.insert_slice`，且整个循环带 `{ExtractedLoadOrStore}` 属性。

**预期结果**：与仓库自带测试 [unstructure_mix.mlir](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/unittest/Conversion/General/TritonToUnstructure/unstructure_mix.mlir#L50-L62) 的 `CHECK` 输出形态一致——其中一维被展开成循环、另一维仍以 `extract_slice [1,8]` 保留向量化（混合形态）。若环境未编译 `triton-opt`，可改用 `MLIR_ENABLE_DUMP=1` 运行真实 kernel，在 dump 出的 `ttadapter` 阶段 IR 中寻找同样的 `scf.for {ExtractedLoadOrStore}`。**待本地验证**（取决于是否已构建该工具）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `triton-to-unstructure` 要给生成的 op 打 `DiscreteMemAccess` 属性，而不让它们看起来像普通访存？

> **答案**：这些标量访存是「为了绕过硬件限制而人为展开」的，并非用户语义。打属性后，下游 pass（如 `triton-to-linalg`、CSE）能识别并区别对待，避免把它们误优化回向量化形态（硬件又处理不了），也便于调试时定位。

**练习 2**：纯 `simt_only` 模式下，本 pass 还会执行吗？

> **答案**：不会。`simt_only` 走 `ttir → npubin` 的独立通道，直接跳过整条 linalg 主线（见 [u3-l2](u3-l2-ascend-backend-stages-and-options.md) 的 `force_simt_only` 分支），因此 `triton-to-unstructure` 不参与编译。

---

### 4.2 UnstructuredMemAccessConverter：标量循环重写

#### 4.2.1 概念说明

`UnstructuredMemAccessConverter` 是一个 C++ 模板，模板参数是四种访存 op 之一：`tt.LoadOp`、`tt.StoreOp`、`tt.AtomicRMWOp`、`tt.AtomicCASOp`（[UnstructureConversionPass.h:84-89](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/include/TritonToUnstructure/UnstructureConversionPass.h#L84-L89) 用 `static_assert` 限定）。它继承 `OpRewritePattern<MemAccOpTy>`，靠 `matchAndRewrite` 决定如何改写。

它依赖一个前置分析产物：`offsetMap`，把每个指针 `Value` 映射到一个 `PtrOffsetInfo`。`PtrOffsetInfo` 描述该指针偏移的「性质」，分类定义在 [OffsetAnalysis.h:42-74](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/include/TritonToUnstructure/OffsetAnalysis.h#L42-L74)：

- **ScalarLike**：所有元素相同（如 `splat`、`load tensor<1xptr>`）。它是 Structured 的特例。
- **Structured**：能归纳成「基址 + 等差步长」的矩形块，硬件可向量化。
- **Unstructured**：上述之外的一切（散点步长、来自浮点转换、运行时未知值等）。

关系记作：

\[ \text{ScalarLike} \subseteq \text{Structured}, \qquad \text{Unstructured} = \overline{\text{Structured}} \]

每个维度还有逐维标记（`AxisInfo::structured/unstructured/scalarlike/scalar`，[OffsetAnalysis.h:77](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/include/TritonToUnstructure/OffsetAnalysis.h#L77)），允许「这一维连续、那一维散乱」的**混合**形态——这正是 pass 名里「mix」的来源，也是性能关键：只展开必须展开的维，其余维保持向量化。

#### 4.2.2 核心流程

`matchAndRewrite` 的主干（[UnstructureConversionPass.cpp:448-762](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToUnstructure/UnstructureConversionPass.cpp#L448-L762)）分四步：

```text
Step 0  早退：若 ptr 全结构化且非全 1 形状 → failure()（无需处理）
Step 1  快路径尝试（仅 950 + force_simt_template）：
        indirectFastPathEnabled && tryRewriteIndirectFastPath 成功 → success()
Step 2  对齐兜底：连续结构化维乘积的 sizeInByte 若不是 32 的倍数 → 全部标量化
Step 3  按维度生成循环（核心）：
        for i in 0 .. rank:
          if 维 i 结构化  → offsets[i]=0, sizes[i]=dim   (整段切片)
          if 维 i 非结构化 → 创建 scf.for, offsets[i]=iv, sizes[i]=1 (单元素)
        循环体最内层：extract 偏移 → addptr → 标量访存 → insert 回结果张量
```

**对齐兜底**值得专门说明：[UnstructureConversionPass.cpp:512-521](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToUnstructure/UnstructureConversionPass.cpp#L512-L521) 从最内维往前累乘，算出连续结构化部分的总字节数 `sizeInByte`：

\[ \text{sizeInByte} = \text{elementSize} \times \prod_{\text{连续结构化维}} \text{dim}_i \]

若 `sizeInByte % 32 != 0`（不满足昇腾 32 字节访存对齐，呼应 [u2-l3](u2-l3-memory-alignment-and-ub-constraints.md)），即使维度「看起来结构化」，也强制降为全 unstructured，走标量循环——宁可慢，不能错。

#### 4.2.3 源码精读

**循环生成的核心循环**。[UnstructureConversionPass.cpp:577-636](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToUnstructure/UnstructureConversionPass.cpp#L577-L636) 按维度遍历，结构化维只填 offsets/sizes，非结构化维创建 `scf.for` 并把插入点移入循环体：

```cpp
for (size_t i = 0; i < resultShape.size(); i++) {
  auto structured = ptrOffsetInfo.getStructuredRef()[i] ==
                    PtrOffsetInfo::AxisInfo::structured;
  if (structured) {
    offsets.push_back(rewriter.getIndexAttr(0));   // 整段
    sizes.push_back(rewriter.getIndexAttr(size));
  } else {
    // 离散维：套一层 scf.for
    forOp = rewriter.create<scf::ForOp>(loc, loopLower, loopUpper, oneIdx, ...);
    sizes.push_back(rewriter.getIndexAttr(1));      // 每次 1 个
    offsets.push_back(forOp.getInductionVar());     // 用归纳变量做偏移
    forOp->setAttr("ExtractedLoadOrStore", ...);
    rewriter.setInsertionPointToStart(forOp.getBody());  // 后续 op 生成在循环内
  }
}
```

**循环体最内层**。[UnstructureConversionPass.cpp:641-732](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToUnstructure/UnstructureConversionPass.cpp#L641-L732) 取出偏移、构造指针、做访存、写回。对 load 类（有结果）走 `tensor.empty` + `insert_slice`/`insert` 累积回结果张量；对 store/atomic 类（无结果）直接在循环体里执行。

**四种 op 的具体构造**由 `createMemAccOp` 模板特化提供：load 最简单（[L323-330](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToUnstructure/UnstructureConversionPass.cpp#L323-L330)），store/atomic 还要 `extract` 出对应的 value/mask（[L404-415](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToUnstructure/UnstructureConversionPass.cpp#L404-L415)）。

**950 SIMT 快路径与回退**。代码顶部 [L114-148](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToUnstructure/UnstructureConversionPass.cpp#L114-L148) 有一段详尽注释，定义了快路径闸门：仅当 `compileOn91095Flag && forceSimtTemplateFlag` 且访存为 unstructured（或带 `route_discrete_mask_to_simt` 标记）时启用。`load/store` 额外要求 rank ≤ 5；`atomic` 额外要求静态形状（由 [IndirectAtomicUtils](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/include/TritonToUnstructure/IndirectAtomicUtils.h#L33-L38) 的 `canUseIndirectAtomicFastPath` 判定）。闸门判断在 [L533-542](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToUnstructure/UnstructureConversionPass.cpp#L533-L542)：

```cpp
bool indirectFastPathEnabled =
    compileOn91095Flag && forceSimtTemplateFlag &&
    ((!ptrOffsetInfo.isStructured() && sizeInByte < 64) || routeDiscreteMaskToSimt);
```

**回退语义**：注释 [L145-148](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToUnstructure/UnstructureConversionPass.cpp#L145-L148) 明确——「若 SIMT 间接 lowering 无法对某 op 生成，则优雅地回退到 legacy 标量循环路径」。代码里这表现为：`tryRewriteIndirectFastPath` 返回 `failure()` 后，**不 return**，而是继续往下执行标量循环生成逻辑。

由此可推出**模式语义**（呼应学习目标「SIMD 模式下的回退语义」）：

| compile_mode | force_simt_template | 快路径状态 | 离散访存的归宿 |
|---|---|---|---|
| `simd` | `false` | 永久关闭 | **一律走标量循环**（这正是「SIMD 模式下的回退」） |
| `unstructured_in_simt`（默认） | `true` | 在 950 上尝试 | 满足条件→`indirect_load/store`；否则→标量循环 |
| `simt_only` | — | — | 本 pass 不执行（走 `ttir_to_npubin`） |

> `force_simt_template` 的取值来自 [compiler.py 的 __post_init__](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L1113-L1120)：`unstructured_in_simt` 把它置 `True`，`simd` 不动（保持 `False`）。

#### 4.2.4 代码实践

**实践目标**：对比「混合模式（默认）」与「纯 SIMD 模式」下，同一段离散访存 dump 出的 IR，说明为何需要标量化展开。

**操作步骤**：

1. 写一个含间接访存的 kernel（例如用 `tl.load` 配合一张运行时计算出的索引张量做 gather）。
2. 用默认模式（`compile_mode="unstructured_in_simt"`）跑一次，导出 dump：

```bash
MLIR_ENABLE_DUMP=1 python your_kernel.py 2>dump.log
```

3. 再用纯 SIMD 模式跑一次（在 kernel 前设置）：

```python
# 仅作示意：实际通过 NPUOptions / 环境变量传入 compile_mode，具体传参方式「待本地验证」
```

4. 在 dump 中定位 `ttir_to_unstructure` 之后的 `ttadapter` 阶段 IR。

**需要观察的现象**：

- 默认模式（950 硬件上）：若满足 rank ≤ 5 等条件，离散 load 可能变成 `tt.indirect_load`（一条指令，由 SIMT 模板执行）；不满足则仍是 `scf.for`。
- 纯 SIMD 模式：`force_simt_template=False`，快路径恒关，**一定**是 `scf.for` + `{DiscreteMemAccess}` 标量访存。

**预期结果**：两种模式的 IR 在「离散访存」处形态不同——SIMD 模式永远展开成标量循环。**为何需要展开**：因为向量化访存单元只能搬运连续矩形地址，散点地址无法批量处理；标量循环把一次不可批量的访存拆成一串可执行的标量访存，保证语义正确（牺牲性能换正确性）。若无法实地运行，可改为阅读 [unstructure_mix.mlir](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/unittest/Conversion/General/TritonToUnstructure/unstructure_mix.mlir) 的输入与 `CHECK` 输出做源码阅读型对比。

#### 4.2.5 小练习与答案

**练习 1**：一个 `tensor<16x8x!tt.ptr<f32>>` 的访存，若外维（16）离散、内维（8）连续，展开后会有几层 `scf.for`？循环体里 load 的形状是什么？

> **答案**：1 层 `scf.for`（只对离散的外维展开）。循环体里保留内维向量化，load 形状是 `tensor<1x8x!tt.ptr<f32>>`，对应 `extract_slice [iv, 0] [1, 8]`。这正是 `unstructure_mix.mlir` 测试的形态。

**练习 2**：`sizeInByte % 32 != 0` 时为什么要把所有维都强制设为 unstructured？

> **答案**：昇腾向量化访存要求 32 字节对齐（见 u2-l3）。若连续结构化部分的总字节数不是 32 的倍数，硬件无法对齐搬运，强行向量化会出错或低效；降为全标量循环可绕过对齐要求，保证正确。

---

### 4.3 bubble-up-operation：extract op 上提优化

#### 4.3.1 概念说明

标量化展开有一个副作用：循环体里会冒出大量 `tensor.extract`（从张量取一个标量）。如果这个被 extract 的张量本身是由某个逐元素算子算出来的，比如：

```mlir
%0 = arith.addi %a, %b : tensor<128xi32>     // 整张量相加
%1 = tensor.extract %0[%i] : tensor<128xi32> // 再取第 i 个
```

那么「先算整张量、再取一个」是浪费——本该只算第 i 个。`bubble-up-operation`（气泡上提）做的事是：把 `extract` **推过**它的定义算子，变成「先 extract 两个操作数、再做标量运算」：

```mlir
%ax = tensor.extract %a[%i] : tensor<128xi32>
%bx = tensor.extract %b[%i] : tensor<128xi32>
%1 = arith.addi %ax, %bx : i32               // 标量加法
```

这样整张量的 `addi` 若无其他用户就会被删除，计算量从「张量级」降到「标量级」。这正是 `triton-to-unstructure` 之后紧跟 `bubble-up-operation` 的原因——后者回收前者制造的 extract，恢复效率。

#### 4.3.2 核心流程

`BubbleUpExtract` 模板对 `tensor::ExtractOp` 和 `tensor::ExtractSliceOp` 两种取值操作各实例化一份（[BubbleUpOperation.cpp:513-515](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToUnstructure/BubbleUpOperation.cpp#L513-L515)）。`matchAndRewrite` 的逻辑（[L38-146](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToUnstructure/BubbleUpOperation.cpp#L38-L146)）：

```text
1. 找到 extract 的源张量 %t 的定义算子 parentOp
2. 闸门：若未开 enableAggressiveMode 且 parentOp 不止一个用户 → failure()
   （避免为一个 extract 破坏共享的整张量计算）
3. 按 parentOp 的类型分派到对应 bubbleUp* 重载：
     整型二元(addi/subi/muli/divsi/...):  对两个操作数各做 extract，再标量二元
     浮点二元(addf/subf/mulf/...):        同上
     一元类(extsi/truncf/fptosi/sitofp/floor/ceil/...): extract 输入，再标量一元
     Triton 类(broadcast/expand_dims/splat/make_range/addptr): 特殊处理索引/形状
4. 若原 parentOp 已无用户 → 删除（消除整张量计算）
```

它支持约 25 种父算子，覆盖了绝大多数逐元素算术与 Triton 形状算子（完整列表见 [BubbleUpOperation.h:71-102](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/include/TritonToUnstructure/BubbleUpOperation.h#L71-L102)）。

#### 4.3.3 源码精读

**闸门**。[BubbleUpOperation.cpp:59-61](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToUnstructure/BubbleUpOperation.cpp#L59-L61)：

```cpp
if (!parentOp || (!enableAggressiveMode && !parentOp->hasOneUse()))
  return failure();
```

`enableAggressiveMode` 默认 `true`（[Passes.td:25](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/include/TritonToUnstructure/Passes.td#L21-L28)），即默认激进上提；关闭时只对「独占」的 parentOp 上提，避免破坏被多处共享的整张量计算。

**整型二元上提**。[L180-192](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToUnstructure/BubbleUpOperation.cpp#L180-L192)：

```cpp
template <typename BinOpTy>
void bubbleUpIntBinaryOp(ExtractOpTy op, BinOpTy binOp, ...) const {
  auto lhs = createExtractOp(op, binOp.getLhs(), loc, rewriter);  // extract 左操作数
  auto rhs = createExtractOp(op, binOp.getRhs(), loc, rewriter);  // extract 右操作数
  rewriter.replaceOpWithNewOp<BinOpTy>(op, lhs, rhs);             // 标量二元
}
```

`createExtractOp` 用原 extract 的索引，对父算子的输入再做一个 extract（[L155-164](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToUnstructure/BubbleUpOperation.cpp#L155-L164)）。最终若 `parentOp->use_empty()` 则删除（[L137-138](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToUnstructure/BubbleUpOperation.cpp#L137-L138)）。

**Triton 形状算子的特殊处理**。`broadcast`/`expand_dims`/`splat`/`make_range` 不能简单地把索引搬过去，因为维度语义变了。例如 `make_range` 上提（[L358-374](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToUnstructure/BubbleUpOperation.cpp#L358-L374)）：`extract(make_range(start,end)[i])` 直接化简成 `i + start` 这个标量，连循环都不需要。`splat` 上提（[L341-347](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToUnstructure/BubbleUpOperation.cpp#L341-L347)）：`extract(splat(x)[i])` 直接替换成标量 `x`。这些是「上提即消除」的强优化。

**pass 收尾**。[runOnOperation](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToUnstructure/BubbleUpOperation.cpp#L508-L528) 跑完 greedy rewrite 后再跑一遍 CSE + Canonicalizer，把上提后冗余的 extract/常量折叠干净。

#### 4.3.4 代码实践

**实践目标**：单独驱动 `bubble-up-operation`，观察 extract 如何被推过父算子。

**操作步骤**：

1. 仓库自带测试就是现成样本，直接用 [bubbleupoperation.mlir](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/unittest/Conversion/General/TritonToUnstructure/bubbleupoperation.mlir)。挑其中一条：

```mlir
tt.func @test_subi_extract_bubbleup(%a: tensor<128xi32>, %b: tensor<128xi32>, %i: index, %c: i32) -> i32 {
  %0 = arith.subi %a, %b : tensor<128xi32>      // 整张量减
  %1 = tensor.extract %0[%i] : tensor<128xi32>  // 取一个
  %2 = arith.muli %1, %c : i32
  tt.return %2 : i32
}
```

2. 运行（注意此测试用 `--bubble-up-operation` 标志，见其 [RUN 行](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/unittest/Conversion/General/TritonToUnstructure/bubbleupoperation.mlir#L1)）：

```bash
triton-opt --bubble-up-operation bubbleupoperation.mlir
```

**需要观察的现象**：`arith.subi` 从「张量减张量」变成「标量减标量」，即先 `tensor.extract %a[%i]`、`tensor.extract %b[%i]`，再 `arith.subi` 两个标量；原来的整张量 `subi` 消失。

**预期结果**：输出里 `%0 = arith.subi %a, %b : tensor<128xi32>` 不复存在，取而代之的是两个 extract 加一个标量 `subi`。**待本地验证**（需已构建 `triton-opt`）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `enableAggressiveMode=false` 时要求 `parentOp->hasOneUse()`？

> **答案**：若 parentOp 被多个 extract（或其他用户）共享，把它「上提」成标量计算会让每个用户各自重做一份标量 extract + 运算，可能反而增加指令数。要求独占（hasOneUse）保证上提后原 parentOp 一定能删除，收益确定。激进模式则信任下游 CSE/规范化能合并重复，故放宽限制。

**练习 2**：`extract(splat(x)[i])` 上提后变成什么？为什么？

> **答案**：直接变成标量 `x`。因为 `splat` 把同一个标量广播成张量，每个元素都是 `x`，extract 任意位置都等于 `x`，所以整张量与 extract 都可消除——这是最彻底的上提。

---

## 5. 综合实践

把两个 pass 串起来观察一次完整的「离散访存标量化 + 上提回收」。

**任务**：

1. 构造一个 kernel，其指针偏移由 `arith.muli`/`arith.addi` 这类逐元素运算产生（模拟 gather 的地址计算），再 `tt.load`。
2. 分别运行：

```bash
triton-opt --triton-to-unstructure demo.mlir > after_unstructure.mlir
triton-opt --triton-to-unstructure --bubble-up-operation demo.mlir > after_bubble.mlir
```

3. 对比 `after_unstructure.mlir` 与 `after_bubble.mlir`：
   - `after_unstructure.mlir` 里循环体中的 `tensor.extract %offset_table[%i]` 是否还连着整张量的 `muli`/`addi`？
   - `after_bubble.mlir` 里这些整张量运算是否被「拆」成了对操作数的标量 extract + 标量运算？整张量父算子是否被删除？

**预期结论**：

- `triton-to-unstructure` 把离散 load 展开成 `scf.for`，但循环体里仍带着整张量的地址计算（低效）。
- `bubble-up-operation` 把这些整张量计算「气泡上提」成标量计算并删除原父算子，让循环体只剩「标量 extract → 标量地址运算 → 标量 load → insert」，恢复成接近手写标量循环的形态。

这一对比恰好解释了为什么两个 pass 必须配套出现：前者解决「硬件能不能执行」，后者解决「执行得够不够省」。

## 6. 本讲小结

- `triton-to-unstructure` 处理 `TritonToStructured` 归纳不了的**离散/间接访存**，把它们展开成 `scf.for` 标量循环，保证语义可执行。
- 核心是 `UnstructuredMemAccessConverter` 模板（覆盖 load/store/atomic_rmw/atomic_cas），按维度决策：结构化维保留向量化 `extract_slice`，非结构化维套循环 `extract` 单元素。
- `PtrOffsetInfo` 给每个指针打逐维 structured/unstructured/scalarlike 标签，允许「混合」形态——只展开必要的维。
- 对齐兜底：连续结构化部分 `sizeInByte % 32 != 0` 时强制全标量化，呼应昇腾 32 字节对齐约束。
- **回退语义**：纯 `simd` 模式（`force_simt_template=false`）下 SIMT 快路径恒关，离散访存一律走标量循环；默认 `unstructured_in_simt` 模式在 950 上优先尝试 `indirect_load/store` 快路径，失败才回退。
- `bubble-up-operation` 紧随其后，把标量化制造的 `tensor.extract` 推过父算子，消除冗余的整张量计算，恢复效率。

## 7. 下一步学习建议

- 下一篇 [u4-l5 TritonToLinalg：TTIR 到 Linalg 算子转换](u4-l5-triton-to-linalg.md) 将讲解本 pass 产出的（已预处理好的）TTIR 如何被系统性 lower 成 Linalg 算子——本 pass 是它的「清道夫」。
- 若想深入「快路径」分支，跳到 [u6-l2 离散访存 SIMT 模板与纯 SIMT 路径](u6-l2-simt-templates-and-pure-simt.md)，那里详讲 `indirect_load/store` 与 `ttir_to_npubin` 的纯 SIMT 通道。
- 想动手扩展 pass 的读者，可参考 [u10-l5 扩展 C++ pass：二次开发实战](u10-l5-extending-cpp-pass.md)，用本讲的 `Passes.td` 注册 + Converter 模式作为模板。
- 推荐继续阅读 [OffsetAnalysis.cpp](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToUnstructure/OffsetAnalysis.cpp)，理解 `PtrOffsetInfo` 是如何通过遍历算子 DAG（`parseArithOp`/`parseTritonOp`）推断出逐维标签的——这是本 pass 一切决策的数据基础。

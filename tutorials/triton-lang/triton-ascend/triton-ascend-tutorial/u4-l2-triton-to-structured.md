# TritonToStructured：指针与掩码张量化

## 1. 本讲目标

本讲精读 Ascend 后端 pass 链中的**第一个结构化 pass** —— `triton-to-structured`（在 `ttir_to_linalg` 中由 `add_triton_to_structure` 注册，见 [compiler.py:203](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L203)）。

读完本讲，你应当能够：

- 说清楚 `PtrState` / `MaskState` 这两套「结构化中间表示」各用什么字段描述一块访存，以及 `stride=0`（广播维）为何是触发本 pass 的关键。
- 跟着 `visitOperand*` 这棵递归 AST 分析器，理解一段形如 `ptr + x // 1024 * 4096` 的指针表达式是如何被「理解」成 N 维块访存的，并能指出 `DivSI`/`RemSI` 在其中扮演的角色。
- 说明 `LoadConverter` / `StoreConverter` 在什么条件下才真正改写一条 `tt.load`/`tt.store`（`shouldLinearize` 闸门），以及改写后插入的 `broadcast`/`reshape`/`permute`/`select` 各自在补什么洞。
- 认出本 pass 的「整除/块大小」硬限制：除数、步长、形状之间必须满足倍数关系，否则分析失败、访存原样保留交给后续 pass。

本讲是 u4-l1「ttir_to_linalg pass 编排总览」的下一篇，聚焦其中 `add_triton_to_structure` 这一个 pass 的内部 IR 变换。

## 2. 前置知识

### Triton 指针是一维整数张量的算术

在 TTIR 里，`tt.make_range`、`tt.splat`、`tt.broadcast`、`tt.expand_dims`、`arith.addi/muli/divsi/remsi` 这些算子组合出的是一个**一维或多维的「偏移量张量」**，再用 `tt.addptr` 把它加到基址指针上，得到一个「指针张量」，最后喂给 `tt.load`/`tt.store`。

例如经典 matmul 教程里的二维块指针（见 [03-matrix-multiplication.py:86-90](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/tutorials/03-matrix-multiplication.py#L86-L90)）：

```python
offs_am = pid_m * BLOCK_SIZE_M + tl.arange(0, BLOCK_SIZE_M)   # 1D, shape [M]
offs_bn = pid_n * BLOCK_SIZE_N + tl.arange(0, BLOCK_SIZE_N)   # 1D, shape [N]
a_ptrs_base = a_ptr + (offs_am[:, None] * stride_am + offs_k[None, :] * stride_ak)
```

这里的 `offs_am[:, None]`（`expand_dims` + `broadcast`）把一维偏移撑成二维块。它本质上等价于一个**二维块访存**：第 0 维步长是 `stride_am`、第 1 维步长是 `stride_ak`。

### 为什么要把指针「结构化」

昇腾 NPU 的硬件访存单元（以及后续的 Linalg/BiSheng 流水线）更愿意看到**结构化的块访存**——明确的每维 `(步长, 形状)`，而不是一团揉在一起的一维地址计算式。`triton-to-structured` 的职责，就是**逆向工程**这团一维算术表达式，重新解读成「基址 + 标量偏移 + 每维 (步长,形状)」的结构化形式，并在必要处补上 `broadcast`/`reshape`/`select` 等算子把「隐式」的结构显式化。

一句话直觉：**TritonToStructured 把「指针算术」翻译成「块形状描述」。**

### 一个块访存的地址公式

一个 N 维块里，索引向量 \(\vec{i}=(i_0,\dots,i_{n-1})\) 对应的地址可写成：

\[
\text{addr}(\vec{i}) \;=\; \text{base} \;+\; \text{offset} \;+\; \sum_{d=0}^{n-1} \text{stride}_d \cdot i_d
\]

当某个 \(\text{stride}_d = 0\)，意味着该维是**广播维**——沿该维移动地址不变（值是「复制」出来的）。本 pass 的核心判定就落在「是否存在 stride=0 的维」上。

### 整除分解

对一个一维连续索引 \(x\in[0, L)\)，用除数 \(D\) 做整除/取模可把它拆成二维：

\[
q = x \,//\, D,\qquad r = x \,\%\, D,\qquad x = q\cdot D + r,\qquad 0 \le r < D
\]

这正是 `x // 1024` 能把一维 run「撑」出二维结构的数学根源，也是本讲实践任务的核心。

## 3. 本讲源码地图

本讲涉及的关键文件集中在 `third_party/ascend/` 下的 `lib/TritonToStructured/` 与对应头文件，外加 `compiler.py` 里的接线：

| 文件 | 作用 |
| --- | --- |
| [lib/TritonToStructured/TritonToStructuredPass.cpp](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToStructured/TritonToStructuredPass.cpp) | pass 入口 `runOnOperation`：先跑规范化 pattern，再跑 `LoadConverter`/`StoreConverter`，最后 CSE+canonicalizer。 |
| [include/TritonToStructured/TritonToStructuredPass.h](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/include/TritonToStructured/TritonToStructuredPass.h) | pass 类声明与两个开关字段 `optimizeDynamicOffset`、`enableMaskFallbackConversion`。 |
| [include/TritonToStructured/PtrAnalysis.h](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/include/TritonToStructured/PtrAnalysis.h) | `StateInfo`、`PtrState` 结构体与 `PtrAnalysis` 分析器声明。 |
| [lib/TritonToStructured/PtrAnalysis.cpp](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToStructured/PtrAnalysis.cpp) | 指针分析的全部实现：`visitOperand*` 递归派发、`addState/mulState`、`visitOperandDiv/Rem`、`createAddPtrOp`。 |
| [include/TritonToStructured/MaskAnalysis.h](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/include/TritonToStructured/MaskAnalysis.h) | `dimInfo`、`MaskState` 结构体声明。 |
| [lib/TritonToStructured/MaskAnalysis.cpp](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToStructured/MaskAnalysis.cpp) | 掩码分析实现：`parse*` 派发、`parseCmp/And/Div/Rem`、`createNewMask`。 |
| [lib/TritonToStructured/MemOpConverter.cpp](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToStructured/MemOpConverter.cpp) | 真正改写 `tt.load`/`tt.store` 的 `LoadConverter`/`StoreConverter`，以及 `MemOpTransformer` 助手。 |
| [backend/compiler.py](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py) | 在 `ttir_to_linalg` 里两次调用 `add_triton_to_structure`（[:203](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L203) 与 [:212](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L212)），并把两个开关从 metadata 灌入。 |

> 提示：本讲用「`RewriteAddPtrOp` / `RewriteLoadOp` / `RewriteStoreOp`」这些概念名指代三类改写逻辑；落到代码里，它们分别是 `PtrAnalysis::rewriteAddptrOp`（指针表达式建模）、`MemOpConverter::LoadConverter` 与 `StoreConverter`（访存重建）。

## 4. 核心概念与源码讲解

### 4.1 PtrState / MaskState：指针与掩码的结构化中间表示

#### 4.1.1 概念说明

要在 IR 里「重建」一块结构化访存，先得有一套数据结构把「这块访存长什么样」记下来。TritonToStructured 用两套并行的中间表示：

- **`PtrState`**（描述指针）：把一个指针张量分解成「基址 + 标量偏移 + 每维 (步长, 形状)」。
- **`MaskState`**（描述掩码）：把一个布尔掩码分解成「每维一个比较 `(range+offset) <op> rhs`」。

二者都是「按维分解」的，这样指针和掩码才能逐维对齐匹配。

#### 4.1.2 核心流程

`PtrState` 的字段（见头文件 [PtrAnalysis.h:52-70](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/include/TritonToStructured/PtrAnalysis.h#L52-L70)）：

| 字段 | 含义 |
| --- | --- |
| `source` | 基址指针（`Value`），即 `tt.addptr` 的 ptr 侧。 |
| `offset` | 标量偏移（`OpFoldResult`），可静态可动态。 |
| `sizes` | 原始张量每维形状（保留原始 rank 信息）。 |
| `stateInfo` | `SmallVector<StateInfo>`，每个元素是 `(stride, shape, dimIndex)`。 |
| `shouldLinearize` | **关键闸门**：只要存在 `stride==0` 的维就置 true，触发访存改写。 |
| `isPermuted` / `permuteIds` / `order` | 处理维度置换与 block ptr 的辅助信息。 |

其中 `StateInfo`（[PtrAnalysis.h:41-50](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/include/TritonToStructured/PtrAnalysis.h#L41-L50)）就是三元组 `(stride, shape, dimIndex)`：

```cpp
struct StateInfo {
  OpFoldResult stride;
  OpFoldResult shape; // rem value
  size_t dimIndex;
  ...
};
```

`stride==0` 的物理含义就是地址公式里 \(\text{stride}_d\cdot i_d\) 恒为 0——该维是广播维（如 `offs_am[:, None]` 撑出来的那维）。规范化函数 `normalizeState` 会把连续的零步长维合并（注释里的例子：`stride [0,0,1] shape [4,32,16] → stride [0,1] shape [128,16]`，见 [PtrAnalysis.cpp:198-226](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToStructured/PtrAnalysis.cpp#L198-L226)）。

`MaskState` 与 `dimInfo`（[MaskAnalysis.h:41-65](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/include/TritonToStructured/MaskAnalysis.h#L41-L65)）则是：

| 字段 | 含义 |
| --- | --- |
| `offset` / `shape` | 该维比较的左值是 `range(0,shape) + offset`。 |
| `rhs` | 比较的右值（标量）。 |
| `currentType` | 比较类型 `slt/sge/ult/uge`（小于或大于等于）。 |
| `hasBroadCast` | 该维是否由 broadcast 撑出。 |

于是掩码 `offs < N` 会被理解成「第 d 维：左值 `range+offset`，右值 `N`，比较 `slt`」。多维掩码用 `&`（`arith.andi`）连接 → 多个 `dimInfo`。

#### 4.1.3 源码精读

`StateInfo` / `PtrState` 的字段定义（指针侧的结构化表示）：

[PtrAnalysis.h:41-69](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/include/TritonToStructured/PtrAnalysis.h#L41-L69) 定义了 `StateInfo`（每维 stride/shape/dimIndex）与 `PtrState`（含 `source`、`offset`、`sizes`、`stateInfo`、`shouldLinearize`、置换信息）。这段就是「一块访存的形状描述」。

`dimInfo` / `MaskState` 的字段定义（掩码侧的结构化表示）：

[MaskAnalysis.h:41-77](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/include/TritonToStructured/MaskAnalysis.h#L41-L77) 定义了 `dimInfo`（offset/shape/rhs/比较类型/广播标记）与 `MaskState`。注意 `dimInfo::setType` 只接受 `slt/ult/sge/uge` 四种比较（[MaskAnalysis.cpp:79-95](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToStructured/MaskAnalysis.cpp#L79-L95)），其它比较类型会让掩码分析失败。

两个判断「是否需要处理」的谓词，是贯穿全 pass 的开关：

- `PtrState::isEmpty()`（[PtrAnalysis.cpp:157-159](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToStructured/PtrAnalysis.cpp#L157-L159)）：`stateInfo` 为空且无 source/offset 时为空，用于断言「进入某个 visit 函数前 state 必须是空的」。
- `shouldLinearize` 在 `createNewPtr` 里被赋值（见 4.3.3），它决定整条 load/store 是否被改写。

#### 4.1.4 代码实践（源码阅读型）

**目标**：直观感受 `PtrState` 如何把一维算术「读」成多维结构。

**步骤**：

1. 打开 [PtrAnalysis.cpp:198-226](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToStructured/PtrAnalysis.cpp#L198-L226) 的 `normalizeState`，读懂注释里的合并示例：`stride [0,0,1] shape [4,32,16]` 是怎么变成 `stride [0,1] shape [128,16]` 的——连续两个零步长维被乘成一个。
2. 对照地址公式 \(\text{addr}=\text{base}+\text{offset}+\sum_d \text{stride}_d\cdot i_d\)，自己手算：若 `stateInfo = [(stride=0, shape=128), (stride=1, shape=16)]`，块形状 `[128,16]`，写出每个元素 \((i_0,i_1)\) 的地址。验证第 0 维步长为 0 意味着「沿第 0 维走地址不变」。

**预期结果**：你能口述「stride=0 的维就是 broadcast 维，地址随它不变化」，这正是后续 `shouldLinearize` 触发的根因。本步骤无需运行，纯阅读。

#### 4.1.5 小练习与答案

**练习 1**：`PtrState` 里 `stride` 用 `OpFoldResult` 而不是固定的 `int64_t`，为什么？

**参考答案**：因为步长可能是编译期常量（多数情况），也可能是运行时 `Value`（如 matmul 里由参数传入的 `stride_am`）。`OpFoldResult` 能同时承载「整数属性」与「SSA 值」，让分析器对静态/动态步长统一处理；只是部分路径（如 `createAddPtrOp` 重建、`visitOperandDiv` 整除判定）要求步长能取到静态整数，取不到就分析失败。

**练习 2**：掩码 `offs >= M`（大于等于）能被 `MaskState` 处理吗？`offs != M`（不等）呢？

**参考答案**：`>=` 对应 `sge`，在 `dimInfo::setType` 支持的四种谓词里（[MaskAnalysis.cpp:79-95](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToStructured/MaskAnalysis.cpp#L79-L95)），可以处理。`!=`（`ne`）不在支持列表里，`setType` 返回 false，掩码分析失败——此时若 `enableMaskFallbackConversion` 关闭，整条 load 不会被本 pass 改写。

---

### 4.2 RewriteAddPtrOp：把指针表达式建模为 PtrState

> 对应代码：`PtrAnalysis::rewriteAddptrOp` 及其调用的 `visitOperand*` 递归分析器。

#### 4.2.1 概念说明

一段指针表达式本质上是一棵 AST：根是 `tt.addptr`，子树是各种 `addi/muli/divsi/remsi/make_range/splat/broadcast/expand_dims`。`PtrAnalysis` 做的就是**后序遍历**这棵 AST，把每个算子翻译成对 `PtrState` 的增量更新，最终在根节点汇总出整块访存的 `(基址, 偏移, 每维步长/形状)`。

这套分析借鉴自社区 Triton 的 `triton-analysis`（PtrAnalysis.cpp 头部保留了 Microsoft 的版权声明），Ascend 在此基础上做了扩展（如 `optimizeDynamicOffset` 注解、置换分析）。

#### 4.2.2 核心流程

派发中枢是 `visitOperand`（[PtrAnalysis.cpp:1316-1399](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToStructured/PtrAnalysis.cpp#L1316-L1399)），它按「操作数的定义算子」分流：

```
visitOperand(v):
  若 v 已在 knownPtrs 缓存 → 直接复用
  若 v 是标量 / 指针类型 → initStateByScalar / initStateByPointer
  否则按 defining op 派发：
    addi  → visitOperandAdd  → state.addState(...)
    muli  → visitOperandMul  → state.mulState(...)
    subi  → visitOperandSub  → state.subState(...)
    divsi → visitOperandDiv        ← 本讲重点（x // 1024）
    remsi → visitOperandRem        ← 本讲重点（x % 1024）
    make_range  → visitOperandMakeRange   (stride=1 的连续 run)
    splat       → visitOperandSplat
    broadcast   → visitOperandBroadcast
    expand_dims → visitOperandExpandDims
    addptr      → visitOperandAddptr       (递归到 ptr 与 offset 两支)
    constant    → visitOperandConstSplat
    extsi       → visitOperandExtSI
    其它        → failure（不支持，原样保留）
```

整除 `//`（`DivSI`）与取模 `%`（`RemSI`）是「把一维索引撑成多维」的两个关键算子，它们直接在 `stateInfo` 里**分裂出新的维**。以 `visitOperandDiv`（[PtrAnalysis.cpp:1176-1300](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToStructured/PtrAnalysis.cpp#L1176-L1300)）为例，对每一维 `(stride, shape)`：

- 若 `divisor` 是 `stride` 的倍数（`isMultiple(divisor, stride)`）：步长整除，新步长 `stride/divisor`，形状不变。
- 若 `stride` 是 `divisor` 的倍数（`isMultiple(stride, divisor)`，即连续段）：把这一维**拆成两段**——一段连续（保留步长、形状变小），一段非连续（步长变 0、形状为 `divisor/stride`）。**步长变 0 的这一段就是把 `shouldLinearize` 推向 true 的元凶。**
- 都不满足 → 分析失败。

`addState`（[PtrAnalysis.cpp:499-600](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToStructured/PtrAnalysis.cpp#L499-L600)）把两个 `PtrState` 按维合并（对应两个偏移表达式相加），它要求两侧形状互为倍数（`isMultiple` 检查 [PtrAnalysis.cpp:531-544](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToStructured/PtrAnalysis.cpp#L531-L544)）——这就是学习目标里说的「整除/块大小限制条件」。

最后 `createAddPtrOp`（[PtrAnalysis.cpp:602-700](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToStructured/PtrAnalysis.cpp#L602-L700)）把分析出的 `PtrState` **重新物化成 IR**：对每个非零步长维 `make_range × stride`，再 `expand_dims`+`broadcast` 拼成多维，加上标量偏移，最后 `tt.addptr`。这正是「结构化后」的指针表达式。

#### 4.2.3 源码精读

派发中枢 `visitOperand` 的核心 if-else 链（节选）：

[PtrAnalysis.cpp:1316-1399](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToStructured/PtrAnalysis.cpp#L1316-L1399) 按 `defining op` 递归派发到各 `visitOperand*`；开头先查 `knownPtrs` 缓存避免重复分析（[:1319](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToStructured/PtrAnalysis.cpp#L1319)），结尾对未知算子返回 `failure`（[:1387-1397](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToStructured/PtrAnalysis.cpp#L1387-L1397)），保证「分析不了的指针原样保留」。

`tt.addptr` 的入口 `visitOperandAddptr`：

[PtrAnalysis.cpp:228-275](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToStructured/PtrAnalysis.cpp#L228-L275) 分别对 `ptr` 侧与 `offset` 侧递归 `visitOperand`，得到两个 `PtrState`，再用 `state.addState(ptrState, offsetState, ...)` 合并。它要求 ptr 侧必须提供 `source`（基址），否则失败（[:268-273](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToStructured/PtrAnalysis.cpp#L268-L273)）。

整除算子 `visitOperandDiv` 的「分裂维」逻辑：

[PtrAnalysis.cpp:1238-1289](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToStructured/PtrAnalysis.cpp#L1238-L1289) 对每维判断 `divisor` 与 `stride` 的倍数关系；落到 `isMultiple(divisorAttr, info.stride)` 分支（[:1260](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToStructured/PtrAnalysis.cpp#L1260)）时，把一维拆成「连续段 `(stride, contiguousSize)`」+「广播段 `(zero, nonContiguousSize)`」。注意 [:1293](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToStructured/PtrAnalysis.cpp#L1293) 调用 `updatePtrState(..., true)`，把 `shouldLinearize` 置为 true。另在 [:1222-1229](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToStructured/PtrAnalysis.cpp#L1222-L1229) 有一道前置闸门：若偏移量既非静态、又不能被 `divisor` 整除、且没有 `optimizeDynamicOffset` 注解，直接失败。

把 `PtrState` 重建为 IR 的 `createAddPtrOp`：

[PtrAnalysis.cpp:602-700](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToStructured/PtrAnalysis.cpp#L602-L700) 先把零步长维跳过（[:611-612](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToStructured/PtrAnalysis.cpp#L611-L612)），对剩余维 `make_range` × `stride` → `expand_dims` → `broadcast` → 逐维 `addi` 相加，再加标量 `offset`，最后 `tt.addptr`。重建出的指针「只有非零步长维」，广播维留给 4.3 节的 `materializeImplicitBroadcast` 去补。

#### 4.2.4 代码实践（源码阅读型 + 手算）

**目标**：用本节的整除分解，手算 `ptr + x // 1024 * 4096`（其中 `x = arange(0, 8192)`）会得到怎样的 `PtrState`。

**步骤**：

1. `arange(0,8192)` 经 `visitOperandMakeRange`（[PtrAnalysis.cpp:785-820](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToStructured/PtrAnalysis.cpp#L785-L820)）得到 `stateInfo=[(stride=1, shape=8192)]`，`offset=0`。
2. 套 `visitOperandDiv`（divisor=1024）：对 `(stride=1, shape=8192)`，命中 `isMultiple(divisor=1024, stride=1)` 分支 → `nonContiguousSize=min(1024,8192)=1024`，`contiguousSize=8192/1024=8`。拆成两段：`(stride=1, shape=8)` 与 `(stride=0, shape=1024)`。**出现 stride=0** → `shouldLinearize=true`。
3. 再 `* 4096`（`visitOperandMul`，[PtrAnalysis.cpp:748-767](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToStructured/PtrAnalysis.cpp#L748-L767)）：4096 是标量，`mulState` 把每维 stride × 4096（[PtrAnalysis.cpp:443-448](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToStructured/PtrAnalysis.cpp#L443-L448)），得 `(stride=4096, shape=8)` 与 `(stride=0, shape=1024)`。
4. 最后 `ptr + 上述`：`visitOperandAddptr` 把 `source=ptr` 与该 offset 态相加。

**预期结果**：一块 `[8, 1024]` 的二维访存，第 0 维步长 4096（每跨 1024 个元素地址跳 4096），第 1 维步长 0（1024 个元素共享同一地址——广播）。这正是 `x // 1024 * 4096` 的语义：把 8192 个连续索引按每 1024 一组分成 8 组，组内地址相同、组间间隔 4096。

> 待本地验证：第 3 步 `mulState` 要求「乘法两侧至少一侧是标量」（[PtrAnalysis.cpp:426-434](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToStructured/PtrAnalysis.cpp#L426-L434)），4096 是常量标量，满足。若把 4096 换成张量，分析会失败。

#### 4.2.5 小练习与答案

**练习 1**：把上例的 `8192` 换成 `5000`（不能被 1024 整除），`x // 1024` 还能被分析成功吗？

**参考答案**：不能。`contiguousSize = 5000 / 1024 = 4`（整除），但 `nonContiguousSize × contiguousSize = 1024 × 4 = 4096 ≠ 5000`，尾部 904 个元素无法整齐归入「连续段」。虽然 `getIntAttr` 检查能过，但最终块形状与原始 `sizes`（8192 维原始张量）对不上，后续 `addState`/掩码匹配会因形状不互为倍数而失败。这正是 pass 要求「形状整除」的体现。

**练习 2**：`visitOperand` 开头的 `knownPtrs` 缓存（[PtrAnalysis.cpp:1319-1322](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToStructured/PtrAnalysis.cpp#L1319-L1322)）有什么实际意义？

**参考答案**：同一个指针值可能被多条 load/store 共用，或在多层嵌套循环里作为 iter-arg 传递（见 [:1373-1386](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToStructured/PtrAnalysis.cpp#L1373-L1386) 的注释）。缓存避免对同一子表达式重复做 AST 遍历，也让父循环已分析好的 `PtrState` 能在子循环里直接复用。

---

### 4.3 RewriteLoadOp / RewriteStoreOp：掩码分解与张量化重建

> 对应代码：`MemOpConverter::LoadConverter`、`StoreConverter` 及 `MemOpTransformer` 助手。

#### 4.3.1 概念说明

`PtrAnalysis` 只是「看懂了」指针；真正动刀改写 IR 的是 `LoadConverter`/`StoreConverter` 两个 `OpRewritePattern`。它们的目标是：把一条用「一维算术指针 + 复杂掩码」表达的 `tt.load`/`tt.store`，重写成「结构化指针（已去掉广播维）+ 结构化掩码 + 显式 broadcast/reshape/select」的组合。

关键设计：**并非所有 load/store 都会被改写**。只有指针里出现广播维（`shouldLinearize==true`）的才会被重写；普通的一维连续访存（如 vector-add 的指针）原样保留。这避免了对简单 kernel 的无谓扰动。

#### 4.3.2 核心流程

`LoadConverter::matchAndRewrite`（[MemOpConverter.cpp:78-128](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToStructured/MemOpConverter.cpp#L78-L128)）的步骤：

```
1. createNewPtr(oldPtr)
   - 跑 PtrAnalysis，得到 ptrState，并据「是否存在 stride=0」设 shouldLinearize
   - 若分析失败 → shouldLinearize=false，返回旧指针
2. 若 !shouldLinearize → return failure()        ← 闸门：不改写
3. createNewMask(oldMask)
   - 跑 MaskAnalysis，并把 mask 各维与 ptr 各维对齐匹配
   - 若有旧掩码却分析不出新掩码，且 !enableMaskFallbackConversion → failure
4. 用 newPtr/newMask/newOther 重建一条 tt.load
5. 依次 materialize 隐式变换：
   - materializeImplicitBroadcast  （补回被去掉的广播维）
   - materializeImplicitPermute    （若发生了维度置换）
   - materializeImplicitReshape    （恢复原始 sizes 形状）
   - materializeImplicitSelect     （掩码分析失败时，用 arith.select 兜底）
6. replaceOp
```

`StoreConverter`（[MemOpConverter.cpp:130-186](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToStructured/MemOpConverter.cpp#L130-L186)）流程类似，但有两个区别：对 value 先做 `select`/`reshape`/`permute` 再 store；并且当「有掩码却分析不出新掩码」时，会在 store 前后插入 `hivm.sync_block_lock`/`unlock`（[:164-167](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToStructured/MemOpConverter.cpp#L164-L167) 与 [:181-183](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToStructured/MemOpConverter.cpp#L181-L183)），用锁来保证这种「退化掩码写」在核内的正确性——这是因为带掩码的离散 store 若无法结构化，就只能逐元素 select 后整体写回，需要同步保护。

掩码分析 `MaskState::analysisMask` → `parse`（[MaskAnalysis.cpp:124-158](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToStructured/MaskAnalysis.cpp#L124-L158)）同样是一棵递归派发树，结构与指针分析对称。最常见入口是 `parseCmp`（[MaskAnalysis.cpp:484-573](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToStructured/MaskAnalysis.cpp#L484-L573)），它把 `cmpi(range+offset, rhs)` 拆成每维一个 `dimInfo`；多维掩码由 `parseAnd`（[MaskAnalysis.cpp:745-885](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToStructured/MaskAnalysis.cpp#L745-L885)）合并。最后 `createNewMask`（[MaskAnalysis.cpp:913-1011](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToStructured/MaskAnalysis.cpp#L913-L1011)）把 `dimInfo` 重新生成为 `make_range+offset → cmpi → expand_dims → broadcast → andi` 的 IR。

#### 4.3.3 源码精读

`shouldLinearize` 闸门——决定 load 是否被改写：

[MemOpConverter.cpp:342-384](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToStructured/MemOpConverter.cpp#L342-L384) 是 `createNewPtr`：先跑 `ptrAnalysis.visitOperand`（[:351](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToStructured/MemOpConverter.cpp#L351)），失败则 `shouldLinearize=false` 并返回旧指针；随后 [:374-379](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToStructured/MemOpConverter.cpp#L374-L379) 扫一遍 stateInfo，**只要存在零步长维就把 `shouldLinearize` 置 true**。该值随后在 [LoadConverter:91-94](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToStructured/MemOpConverter.cpp#L91-L94) 与 [StoreConverter:142-145](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToStructured/MemOpConverter.cpp#L142-L145) 作为闸门：为 false 直接 `return failure()`，不动这条访存。

掩码与指针的逐维对齐匹配：

[MemOpConverter.cpp:386-503](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToStructured/MemOpConverter.cpp#L386-L503) 的 `createNewMask` 跑完 `maskState.analysisMask` 后，用 `itPtr`/`itMask` 两个游标把指针维与掩码维配对（[:418-457](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToStructured/MemOpConverter.cpp#L418-L457)），要求两者形状互为倍数（`isMultiple` 检查 [:421](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToStructured/MemOpConverter.cpp#L421)），否则返回 `nullptr`（掩码分析失败）。注意 [:449-451](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToStructured/MemOpConverter.cpp#L449-L451) 只保留指针侧步长非零的掩码维——广播维不参与掩码生成。

重建后的「隐式变换补全」链：

[MemOpConverter.cpp:188-228](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToStructured/MemOpConverter.cpp#L188-L228) `materializeImplicitBroadcast` 把零步长维用 `linalg.broadcast`（或标量 `splat`）补回，使结果形状与原始 `sizes` 一致；[MemOpConverter.cpp:277-306](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToStructured/MemOpConverter.cpp#L277-L306) `materializeImplicitSelect` 在掩码分析失败时用 `arith.select(mask, value, other)` 兜底——即「load 全量再用掩码挑」，这正是「离散掩码」的退化解法（与下一讲 u4-l3 的 `discrete-mask-access-conversion` 相呼应）。

#### 4.3.4 代码实践（IR dump 型，本讲的核心实践）

**目标**：构造一个含 `ptr + x // 1024 * 4096` 的 kernel，dump 出 `triton-to-structured` pass 执行前后的 IR，亲眼看到整除表达式被重写。

**操作步骤**：

1. 准备一个最小 kernel（**示例代码**，非项目原有文件）：

   ```python
   # 示例代码：demo_div_ptr.py
   import torch, torch_npu, triton, triton.language as tl

   @triton.jit
   def div_ptr_kernel(ptr, N, BLOCK: tl.constexpr):
       pid = tl.program_id(0)
       x = pid * BLOCK + tl.arange(0, BLOCK)   # BLOCK 取 8192
       off = x // 1024 * 4096                  # 关键表达式：先除再乘
       p = ptr + off
       v = tl.load(p, mask=x < N, other=0.0)
       tl.store(ptr + x, v, mask=x < N)

   N = 1 << 20
   a = torch.randn(N, device='npu', dtype=torch.float32)
   div_ptr_kernel[(N // 8192)](a, N, BLOCK=8192)
   ```

2. 开启 pass 级 dump（参考 [debugging.md:456-473](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/docs/en/debug_guide/debugging.md#L456-L473)）：

   ```bash
   export MLIR_ENABLE_DUMP=1     # 每个 pass 前后把 IR 输出到 stderr
   export TRITON_DEBUG=1         # 同时把 ttir/ttadapter 落盘到 ~/.triton/dump/
   python demo_div_ptr.py 2> dump.log
   ```

3. 在 `dump.log` 里定位 pass 边界（pass 名来自 [TritonToStructuredPass.cpp:69](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToStructured/TritonToStructuredPass.cpp#L69) 的 `DEBUG_TYPE "triton-to-structured"`）。

**需要观察的现象**：

- **pass 之前**：`tt.load` 的指针由一串 `arith.divsi(x, 1024)` → `arith.muli(..., 4096)` → `tt.addptr` 组成，是一维算术。
- **pass 之后**：那段 `divsi/muli` 链被重写，指针变成基于 `make_range` × 步长的多维形式；`tt.load` 后面多出 `linalg.broadcast` / `tensor.reshape`（补回 `[8,1024]` 的广播维）；掩码 `x < N` 被重写成新的 `cmpi`+`broadcast`+`andi` 组合。
- **vector-add 对照**：把同一套 dump 用在 [01-vector-add.py](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/tutorials/01-vector-add.py) 上，会看到它的 `tt.load` **前后完全不变**——因为一维连续指针没有广播维，`shouldLinearize=false`，pass 直接跳过。

**预期结果**：div-ptr kernel 的 load/store 被显著重写，vector-add 的 load/store 原样保留。两种结局共同印证了 `shouldLinearize` 闸门的作用。

> 待本地验证：实际 dump 文件名与 pass 边界文本以本机 MLIR 版本输出为准；若 `optimize_dynamic_offset`/`enable_mask_fallback_conversion` 未开启（默认关闭，见 [compiler.py:1007-1008](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L1007-L1008)），动态偏移或退化掩码会让某些 load 不被重写而走 select 兜底。

#### 4.3.5 小练习与答案

**练习 1**：为什么 vector-add 的 load 不被 TritonToStructured 改写，而 matmul 的 `a_ptrs_base`（含 `[:, None]`）会？

**参考答案**：vector-add 的指针是 `base + pid*BLOCK + arange(0,BLOCK)`，经分析得到单维 `(stride=1, shape=BLOCK)`，无零步长维，`shouldLinearize=false`，故 `LoadConverter` 在 [MemOpConverter.cpp:91-94](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToStructured/MemOpConverter.cpp#L91-L94) 直接返回 failure。matmul 的 `offs_am[:, None] * stride_am` 经过 `expand_dims`+`broadcast` 会产生零步长维（第 1 维），`shouldLinearize=true`，触发改写。

**练习 2**：`StoreConverter` 在「掩码分析失败」时为什么要插入 `sync_block_lock`/`unlock`（[MemOpConverter.cpp:164-167](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToStructured/MemOpConverter.cpp#L164-L167)），而 `LoadConverter` 不需要？

**参考答案**：load 的退化用 `select` 在寄存器里挑值，是纯计算、无副作用、无需同步。store 的退化是「带掩码的写」，若无法结构化成块写，就只能逐元素判断后写回，多个核/线程对同一区域的离散写需要顺序保护，否则会有数据竞争。`sync_block_lock` 提供核内互斥，保证这块退化 store 的正确性。

**练习 3**：把 `enable_mask_fallback_conversion` 从默认的 `False` 改成 `True`，对 4.3 节的流程有什么影响？

**参考答案**：默认 False 时，若 `oldMask` 存在但 `createNewMask` 返回 `nullptr`，`LoadConverter` 在 [MemOpConverter.cpp:104-110](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToStructured/MemOpConverter.cpp#L104-L110) 直接 `return failure()`，整条 load 不被改写。改成 True 后这个早退被跳过，pass 会继续重建 load 并用 `materializeImplicitSelect`（`arith.select`）兜底掩码——即「先全量 load，再用原掩码 select」。代价是多了一次全量 load 与 select，收益是让更多带复杂掩码的 load 能进入结构化流水线。

## 5. 综合实践

把本讲的三条主线（PtrState 建模、整除分解、load/store 重建）串起来，完成下面这个**阅读 + 实验**任务：

1. **阅读**：以 matmul 教程 [03-matrix-multiplication.py:69-92](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/tutorials/03-matrix-multiplication.py#L69-L92) 为对象。它同时含有整除/取模（`pid // num_pid_in_group`、`pid % num_pid_in_group`，[:73-77](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/tutorials/03-matrix-multiplication.py#L73-L77)）和二维块指针（`offs_am[:, None] * stride_am`，[:89](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/tutorials/03-matrix-multiplication.py#L89)）与掩码（`msk_m = offs_am < M`，[:91](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/tutorials/03-matrix-multiplication.py#L91)）。

2. **画图**：手画 `a_ptrs_base` 这条指针表达式的 AST，标注每个节点会被哪个 `visitOperand*` 处理（`addptr → addi → muli(expand_dims(arange), stride_am)` 等）。

3. **实验**：用 4.3.4 的 dump 方法（`MLIR_ENABLE_DUMP=1`）跑 matmul，在 `triton-to-structured` 的前后 IR 中：
   - 找到 `pid // num_pid_in_group` 对应的 `divsi` 是否被改写（提示：它的结果喂给了 `group_size_m` 等控制流变量，未必直接进指针；重点看**直接进入 `tt.load` 指针**的那段 `divsi`/`remsi`）。
   - 找到 `offs_am[:, None]` 对应的 `expand_dims`/`broadcast` 是否在 load 之后被 `materializeImplicitBroadcast` 补回。
   - 找到 `msk_m < M` 对应的 `cmpi` 是否被重写为新的掩码 IR。

4. **结论**：写一段话说明——matmul 里哪些 load 触发了 `shouldLinearize=true` 被改写，哪些（如 K 维循环内的指针推进）可能没有，为什么。

> 这是一个开放式任务，没有唯一答案；重点是把「Python 表达式 → AST → visitOperand* → PtrState/MaskState → 重写后的 IR」这条链路在真实 kernel 上走通一遍。dump 出的具体 IR 形态待本地验证。

## 6. 本讲小结

- `triton-to-structured` 是 `ttir_to_linalg` 里的**第一个结构化 pass**，在 [compiler.py:203](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L203) 与 [:212](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L212) 被调用两次，职责是把「一维指针算术 + 复杂掩码」逆向工程成「结构化块访存」。
- 它用两套对称的中间表示：`PtrState`（基址+标量偏移+每维 `(stride,shape)`，[PtrAnalysis.h:52](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/include/TritonToStructured/PtrAnalysis.h#L52)）与 `MaskState`/`dimInfo`（每维一个比较，[MaskAnalysis.h:41](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/include/TritonToStructured/MaskAnalysis.h#L41)）。
- 指针分析是一棵递归 AST 遍历（`visitOperand` 派发，[PtrAnalysis.cpp:1316](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToStructured/PtrAnalysis.cpp#L1316)）；其中 `DivSI`/`RemSI`（`x//D`、`x%D`）能把一维索引分裂成二维，是产生广播维的主因。
- **`shouldLinearize` 是核心闸门**：只有指针含零步长（广播）维时才为 true，`LoadConverter`/`StoreConverter` 才会改写（[MemOpConverter.cpp:91](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToStructured/MemOpConverter.cpp#L91)）；vector-add 这类一维连续访存原样保留。
- 改写后用 `materializeImplicitBroadcast/Reshape/Permute/Select` 补回被「抽走」的广播/形状/置换/掩码（[MemOpConverter.cpp:188-306](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToStructured/MemOpConverter.cpp#L188-L306)）；store 在掩码退化时额外加 `sync_block_lock`。
- 本 pass 有硬性「整除/块大小」限制：除数、步长、形状之间必须互为倍数，掩码只支持 `slt/sge/ult/uge` 四种比较；不满足时分析失败，访存留给后续 pass（如 u4-l3 的离散掩码转换、u4-l4 的标量化）处理。

## 7. 下一步学习建议

- **u4-l3 DiscreteMaskAccessConversion**：本讲里「掩码分析失败 → `arith.select` 兜底」只是退化解法；下一讲讲离散（非连续）掩码如何被专门转换为 `load+select+store` 序列，是本讲 select 路径的正式版。
- **u4-l4 TritonToUnstructure**：当本 pass 完全搞不定（`shouldLinearize=false` 且掩码也不结构化）的离散访存，会在 SIMD 模式下被进一步展开为标量循环，与本讲形成「结构化优先、否则标量化」的互补关系。
- **u4-l5 TritonToLinalg**：本 pass 产出的结构化 `tt.load`/`tt.store` 最终在 `triton-to-linalg` 落到 `memref::copy`/`linalg` 算子；阅读那一讲时可以回看本讲，理解「为什么 Linalg 阶段拿到的 load/store 已经是结构化的」。
- **源码延伸**：若对置换分析感兴趣，可读 `PtrState::analyzePermute`（[PtrAnalysis.cpp:1417-1506](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToStructured/PtrAnalysis.cpp#L1417-L1506)）与 `countContiguousAxes`（[:1557-1577](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToStructured/PtrAnalysis.cpp#L1557-L1577)），看它如何判断「置换能否增加连续维」以决定是否重排维度。

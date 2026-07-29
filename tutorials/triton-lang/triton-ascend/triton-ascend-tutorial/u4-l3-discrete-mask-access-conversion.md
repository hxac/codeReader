# DiscreteMaskAccessConversion：离散掩码访存转换

## 1. 本讲目标

本讲是「Ascend 编译后端 MLIR pass 流水线」单元的第三篇，承接 u4-l2（`TritonToStructured` 指针与掩码张量化）。学完后你应当能够：

- 说清「连续掩码」与「离散掩码」的区别，以及为什么离散掩码无法直接交给硬件做单次连续访存。
- 读懂 `discrete-mask-access-conversion` 这个 pass 如何把带离散掩码的 `tt.load` / `tt.store` / `tt.atomic_rmw` 改写成 `load + arith.select (+store)` 序列。
- 理解「读改写（read-modify-write）越界」风险、`contMask & discMask` 掩码分解如何规避越界，以及 `sync_block_lock` 跨核锁的作用。
- 看懂该 pass 与 950 / SIMT 模板的联动：在 950 + `force_simt_template` 下，它只打标记、把活儿交给后续的 `triton-to-unstructure`。
- 会用 `triton-opt` 跑一条 FileCheck 回归用例，亲眼看到 `load → load + select` 的改写。

## 2. 前置知识

阅读本讲前，请先建立以下认知（来自 u1/u3/u4-l2）：

- **TTIR 与 `tl.load`/`tl.store`/`mask`**：Triton kernel 里的 `tl.load(ptr, mask=m, other=v)` 会被翻译成 `tt.load` 算子，带一个 `mask`（`tensor<...xi1>`）和一个可选的 `other`（mask 为假时的填充值）。`mask` 决定「这一块里哪些元素真正要读」。
- **连续掩码（continuous / rectangle mask）**：u4-l2 介绍过，当掩码能被 `MaskState::parse()` 解析成一个「矩形」（如 `offsets < N` 这种沿每维的区间比较），访存就是一个规整的块，硬件可以一次连续 load/store。
- **离散掩码（discrete mask）**：掩码不是单个矩形，而是若干比较的「或/与」组合，例如 `(x < 200) | (x > 400)`——选中的元素在地址上不连续。这种掩码无法用一个矩形表达。
- **Ascend 的两类执行路径**：SIMD（向量，整块连续访存）与 SIMT（按线程散列访存）。本 pass 处于 `ttir_to_linalg` 流水线中（见 u4-l1），是「结构化」pass 之后的第一个针对离散访存的兜底 pass。
- **`arith.select`**：MLIR 里的逐元素三元选择 `select(cond, a, b)`——`cond` 为真取 `a`，否则取 `b`。它是本 pass 把「掩码」从「访存门控」变成「数值选择」的核心算子。

> 一句话直觉：**连续掩码可以「挡住」一次访存；离散掩码不行，只能先全读、再用 `select` 挑出要的元素。** 本 pass 就是做这件事，并尽力让「先全读」不越界。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [third_party/ascend/lib/DiscreteMaskAccessConversion/DiscreteMaskAccessConversionPass.cpp](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/DiscreteMaskAccessConversion/DiscreteMaskAccessConversionPass.cpp) | 本讲主角：pass 的全部实现，含三个 rewrite pattern 与驱动逻辑。 |
| [third_party/ascend/include/DiscreteMaskAccessConversion/Passes.td](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/include/DiscreteMaskAccessConversion/Passes.td) | pass 的 TableGen 注册声明，定义三个命令行选项。 |
| [third_party/ascend/backend/compiler.py](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py) | 在 `ttir_to_linalg` 中把该 pass 接线进流水线，并在下游解析 `sync_block_lock` 元数据。 |
| [third_party/ascend/lib/TritonToUnstructure/UnstructureConversionPass.cpp](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToUnstructure/UnstructureConversionPass.cpp) | 消费本 pass 打下的 `route_discrete_mask_to_simt` 标记，走 SIMT 间接访存快路径（u4-l4 详讲）。 |
| [third_party/ascend/unittest/Conversion/General/DiscreteMaskAccess/loadstore.mlir](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/unittest/Conversion/General/DiscreteMaskAccess/loadstore.mlir) | FileCheck 回归用例：直观展示 `load → load + select`。 |
| [third_party/ascend/unittest/pytest_ut/test_discrete_mask_tail_block_mte_oob.py](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/unittest/pytest_ut/test_discrete_mask_tail_block_mte_oob.py) | 板端回归测试：验证「受界 load」能避免 MTE 越界。 |

## 4. 核心概念与源码讲解

本讲拆成五个最小模块：先讲 pass 的定位与接线（4.1），再讲「怎么判定一个掩码是离散的、怎么把 AND 树拆开」（4.2），然后逐个精读三个 rewrite pattern——Load（4.3）、Store（4.4）、Atomic 与 950/SIMT 联动（4.5）。

### 4.1 discrete-mask-access-conversion：pass 定位、注册与接线

#### 4.1.1 概念说明

在 u4-l2 的 `TritonToStructured` 里，只有「能解析成矩形」的连续掩码才会被改写成结构化块访存。现实里很多 kernel 的掩码并非单个矩形——典型如 attention 里的因果掩码、边界 `program_id` 相关的混合掩码、或者像 `(idx < 200) | (idx > 400)` 这种「挖洞」掩码。`MaskState::parse()` 在这些场景下会失败，于是访存原样保留下来，带着一个离散 mask 继续往后走。

问题是：**Ascend 的 SIMD 向量访存单元期望一次连续的块拷贝，它没有「按任意 i1 掩码逐元素取舍」的直接指令语义。** 所以必须有一个 pass 把「离散掩码门控的访存」显式地降级成「先读、再用 `arith.select` 逐元素挑选」。这就是 `discrete-mask-access-conversion` 的职责。它是一个**预处理/兜底 pass**——不改写连续访存，只把离散访存「拉平」成 `load + select`，方便后续 pass（如 u4-l4 的标量化、u4-l5 的 `triton-to-linalg`）继续处理。

#### 4.1.2 核心流程

pass 的整体执行流程（由 `runOnOperation()` 驱动）：

1. 从 pass 选项读出三个文件级开关：`compileOn91095Flag`、`forceSimtTemplateFlag`，以及由「块非重叠分析」算出的 `enableSyncBlockLockFlag`。
2. 注册三个 rewrite pattern：`DiscreteMaskLoadConversion`、`DiscreteMaskStoreConversion`、`DiscreteMaskAtomicConversion`。
3. 用 `applyPatternsGreedily` 贪心匹配，对每个 `tt.load`/`tt.store`/`tt.atomic_rmw` 尝试改写。
4. 收尾：跑一次 `CSE` + `canonicalizer`，清理 `MaskState::parse()` 留下的死分析算子。

接线侧（`compiler.py`）在 `ttir_to_linalg` 流水线里、紧跟 `add_triton_to_structure` 之后调用本 pass，并把三个开关从编译元数据里读出来传进去。

#### 4.1.3 源码精读

**注册声明**（TableGen）——定义 pass 名、三个选项及默认值：

[third_party/ascend/include/DiscreteMaskAccessConversion/Passes.td:11-25](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/include/DiscreteMaskAccessConversion/Passes.td#L11-L25) 定义了 `compile-on-910-95`、`force-simt-template`、`enable-sync-block-lock` 三个命令行选项。其中 `enable-sync-block-lock` 默认 `true`，但要注意（见 4.4）运行时真正决定是否插锁的是「块非重叠分析」，而非这个选项值。

**接线进流水线**——在 `ttir_to_linalg` 里，紧接结构化 pass 之后：

[third_party/ascend/backend/compiler.py:204-205](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L204-L205) 调用 `ascend.passes.ttir.add_discrete_mask_access_conversion(pm, compile_on_910_95, force_simt_template, enable_sync_block_lock)`。这三个实参来自元数据：

[third_party/ascend/backend/compiler.py:179-182](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L179-L182) 从 `metadata` 取出 `compile_on_910_95`、`force_simt_template`、`enable_sync_block_lock`。它们源自 `NPUOptions.__dict__`（core 的 `compile` 会把 `options.__dict__` 灌进 metadata），而 `force_simt_template` 由 `compile_mode="unstructured_in_simt"` 在 `__post_init__` 里派生为 `True`（见 u3-l2、u6-l1）。

**驱动函数** `runOnOperation()`：

[third_party/ascend/lib/DiscreteMaskAccessConversion/DiscreteMaskAccessConversionPass.cpp:461-492](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/DiscreteMaskAccessConversion/DiscreteMaskAccessConversionPass.cpp#L461-L492) 是 pass 主体：先把选项写入文件级 `static` 变量（`compileOn91095Flag` 等，第 462-465 行），这样各 `OpRewritePattern` 子类不必逐个传参就能读到；接着注册三个 pattern 并贪心应用（第 468-474 行）；最后用一个小 `PassManager` 跑 `CSE` + `canonicalizer` 清理 `parse()` 产生的死算子（第 479-485 行）。

> 设计要点：pattern 类是 `const` 方法，无法持有可变开关；作者用文件级 `static bool` 在 `runOnOperation()` 里「一次性广播」开关值，是一种常见但需谨慎（不可重入）的写法。

#### 4.1.4 代码实践（源码阅读型）

**目标**：确认本 pass 在整条流水线里的位置，并能复现它的命令行形式。

**步骤**：

1. 打开 `compiler.py` 的 `ttir_to_linalg`（[L155-L264](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L155-L264)），数一下 `add_discrete_mask_access_conversion` 前后各有哪些 pass，确认它在 `add_triton_to_structure`（第一次）之后、`add_triton_to_annotation` 之前。
2. 设想开启 `TRITON_DEBUG` 编译一个 kernel，找到日志里 `[DEBUG] cmd list:` 打出的 `--pass-pipeline=...` 字符串，在其中定位 `discrete-mask-access-conversion` 片段，观察它携带的 `compile-on-910-95=...`、`force-simt-template=...` 实参。

**预期结果**：你能说清「结构化 pass 处理连续掩码 → 本 pass 兜底离散掩码 → annotation/unstructure 接力」这条邻接关系。

**待本地验证**：`--pass-pipeline` 字符串的具体内容取决于 `NPUOptions`，需在本机 dump 后确认。

#### 4.1.5 小练习与答案

- **练习**：本 pass 处理失败（`failure()`）的访存会怎样？会被后续哪个 pass 接手？
- **答案**：`matchAndRewrite` 返回 `failure()` 表示「不改写」，该 `tt.load`/`tt.store`/`tt.atomic_rmw` 原样保留，留给后续 `triton-to-unstructure`（u4-l4，标量化）或 `triton-to-linalg`（u4-l5）继续 lowering。

---

### 4.2 离散掩码的识别与 AND 树分解

#### 4.2.1 概念说明

本 pass 有两个关键的前置步骤被所有 pattern 共享：

1. **是不是离散掩码？**——`isDiscreteMask()` 复用 u4-l2 的 `MaskState::parse()`：能解析成矩形 → 连续 → 跳过；解析失败 → 离散 → 改写。
2. **掩码能否拆成「连续部分 + 离散部分」？**——`decomposeAndMask()` 把一棵 `andi`（按位与）树拆成 `contMask & discMask`：能解析成矩形的叶子归入 `contMask`（可用来限定访存范围，防越界），其余归入 `discMask`（驱动逐元素 `select`）。

为什么要拆？因为很多掩码是「一个连续矩形 AND 一个离散条件」，例如边界块里 `row_offs < M`（连续，矩形）`&` `(row_offs*2 < BLOCK_M)`（离散）。前者可以安全地「限定 load 的范围」，后者只能靠 `select` 挑选。把两者分开，就能做到「在安全范围内 load，再用离散条件 select」，避免无界 load 越界。

#### 4.2.2 核心流程

`isDiscreteMask` 的判定：

```
若 mask 为空 或 op 已带 route_discrete_mask_to_simt 属性 → failure（不改写）
否则 MaskState.parse(mask)
  解析成功（连续矩形）→ eraseInsertedOps 清理分析副作用 → failure（不改写，留给结构化 pass）
  解析失败（离散）    → success（交由本 pattern 改写）
```

`decomposeAndMask` 的分解（伪代码）：

```
leaves = collectAndLeaves(mask)   # 递归展开 andi 树，并把 broadcast(andi(a,b)) 拆成 andi(broadcast(a), broadcast(b))
for leaf in leaves:
    if MaskState.parse(leaf) 成功 且 isMask():  contLeaves.add(leaf)
    else:                                        discLeaves.add(leaf)
contMask = AND(contLeaves)   # 可空
discMask = AND(discLeaves)   # 可空
return {contMask, discMask}
```

数学上，设掩码 \(M\) 是若干因子的按位与：\(M = C_1 \wedge C_2 \wedge \dots \wedge D_1 \wedge D_2 \wedge \dots\)，其中 \(C_i\) 可解析为矩形、\(D_j\) 不可。则

\[
M = \underbrace{\bigwedge_i C_i}_{\text{contMask}} \wedge \underbrace{\bigwedge_j D_j}_{\text{discMask}}
\]

后续用 `contMask` 限定访存范围、用 `discMask` 做 `select`。

#### 4.2.3 源码精读

**离散判定闸门** `isDiscreteMask`：

[third_party/ascend/lib/DiscreteMaskAccessConversion/DiscreteMaskAccessConversionPass.cpp:174-187](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/DiscreteMaskAccessConversion/DiscreteMaskAccessConversionPass.cpp#L174-L187) 先排除「无 mask」和「已标记路由到 SIMT」两种情况（第 176 行）；然后调用 `mstate.parse(mask, ...)`（第 181 行），解析成功就 `eraseInsertedOps` 清理并返回 `failure`（第 182-185 行），否则返回 `success`。`MaskState` 复用自 [third_party/ascend/include/TritonToLinalg/MaskAnalysis.h](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/include/TritonToLinalg/MaskAnalysis.h)（即 u4-l2 介绍过的同一套掩码分析结构）。

**AND 树叶子收集** `collectAndLeaves`：

[third_party/ascend/lib/DiscreteMaskAccessConversion/DiscreteMaskAccessConversionPass.cpp:193-216](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/DiscreteMaskAccessConversion/DiscreteMaskAccessConversionPass.cpp#L193-L216) 递归展开 `arith.andi`；遇到 `broadcast(andi(a,b))` 时，利用恒等式 `broadcast(andi(a,b)) == andi(broadcast(a), broadcast(b))` 把广播「分配」进与运算（第 200-209 行），从而能单独审视被包在广播里的每个因子。

**连续/离散归类** `decomposeAndMask`：

[third_party/ascend/lib/DiscreteMaskAccessConversion/DiscreteMaskAccessConversionPass.cpp:230-264](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/DiscreteMaskAccessConversion/DiscreteMaskAccessConversionPass.cpp#L230-L264) 对每个叶子再调一次 `MaskState::parse()`：成功且 `isMask()` 的进 `contLeaves`（第 241-243 行），其余进 `discLeaves`（第 245-248 行）；最后分别用 `arith.andi` 串成 `contMask`/`discMask`（第 251-261 行），任一为空时对应字段保持 `nullptr`。

#### 4.2.4 代码实践（源码阅读型）

**目标**：理解 `isDiscreteMask` 对同一 IR 的不同判定结果。

**步骤**：阅读 [loadstore.mlir:11-14](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/unittest/Conversion/General/DiscreteMaskAccess/loadstore.mlir#L11-L14)。掩码 `%3 = ori(%1, %2)`，其中 `%1 = cmpi slt(range, 200)`、`%2 = cmpi sgt(range, 400)`。问自己：`MaskState::parse(%3)` 会成功还是失败？为什么？

**需要观察的现象**：`ori`（或）不是 `andi`，且「<200 或 >400」在地址上选中两段不连续区间，无法表达成单个矩形。

**预期结果**：`parse()` 失败 → `isDiscreteMask` 返回 `success` → 触发改写。这正是 FileCheck 里 `arith.select` 出现的原因。

#### 4.2.5 小练习与答案

- **练习 1**：若掩码是单纯的 `cmpi slt(range, 200)`（连续矩形），本 pass 会改写吗？
- **答案**：不会。`MaskState::parse()` 成功 → `isDiscreteMask` 返回 `failure`，交给 u4-l2 的结构化 pass 处理。
- **练习 2**：`collectAndLeaves` 为什么要处理 `broadcast(andi(...))`？
- **答案**：Triton 经常把标量比较 `splat`/`broadcast` 成张量掩码再 `andi`。若不把广播分配进 `andi`，包在 `broadcast` 里的 `andi` 因子对递归不可见，会漏掉「连续矩形」因子的识别，导致本可受界的 load 退化成无界 load。

---

### 4.3 DiscreteMaskLoadConversion：离散 load → load + select

> 这是本讲三个 pattern 中最基础的一个，也是规格要求的核心模块之一。

#### 4.3.1 概念说明

`tt.load ptr, mask, other` 的语义是「在 mask 为真的位置读 `ptr`，为假的位置取 `other`」。当 mask 是离散的，硬件没法只拷贝「选中」的不连续元素，于是改写成等价序列：

1. 先做一次 **不带离散 mask 的 load**（拿到整块数据）；
2. 再用 `arith.select(mask, loaded, other)` 逐元素挑选。

这里有个隐患：去掉 mask 后那次 load 可能读到了**有效数据范围之外**的地址（Out-Of-Bounds, OOB）。如果掩码能拆出 `contMask`（连续矩形因子），就用 `contMask` 给 load 兜底——只读「至少可能被选中」的矩形范围，把离散的 `discMask` 留给 `select`。这就是本 pattern 的三条分支：

- **950 + SIMT 模板分支**：只打 `route_discrete_mask_to_simt` 标记后返回 `failure`，把改写权让给 `triton-to-unstructure`（见 4.5）。
- **受界分支（contMask && discMask 都存在）**：`safeLoad = load(ptr, contMask)`；`result = select(contMask & discMask, safeLoad, other)`。
- **纯离散分支（无 contMask）**：`newLoad = load(ptr)`（无界）；`result = select(mask, newLoad, other)`。有 OOB 风险，但纯 SIMD 下别无选择。

#### 4.3.2 核心流程

```
DiscreteMaskLoadConversion.matchAndRewrite(load):
  if not isDiscreteMask(load): return failure
  if compileOn91095 and forceSimtTemplate and rank(ptr) <= 5:
      load.setAttr("route_discrete_mask_to_simt")   # 让权给 SIMT 路径
      return failure
  (contMask, discMask) = decomposeAndMask(load.mask)
  if contMask and discMask:                          # 受界分支
      other = other or zero_constant(ptr.type)
      safeLoad = load(ptr, mask=contMask)
      combined = andi(contMask, discMask)
      return select(combined, safeLoad, other)       # 用合并掩码 select，避免读到未初始化值
  # 纯离散分支
  other = other or zero_constant(ptr.type)
  newLoad = load(ptr)                                 # 无界
  return select(load.mask, newLoad, other)
```

注意受界分支里 `select` 用的是 `contMask & discMask`（合并掩码），而非只用 `discMask`——这样在 `contMask` 为假的位置也填 `other`，避免依赖 `safeLoad` 中未初始化的内存内容。

#### 4.3.3 源码精读

[third_party/ascend/lib/DiscreteMaskAccessConversion/DiscreteMaskAccessConversionPass.cpp:337-399](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/DiscreteMaskAccessConversion/DiscreteMaskAccessConversionPass.cpp#L337-L399) 是 `DiscreteMaskLoadConversion` 的全部实现。要点对应：

- 第 347-348 行：`isDiscreteMask` 闸门。
- 第 350-357 行：950 + SIMT 模板让权分支（打 `route_discrete_mask_to_simt` 属性后 `return failure`）。`rankWithinIndirectFastPathLimit` 判定张量秩 ≤ 5。
- 第 362-382 行：**受界分支**。第 364-371 行处理 `other` 缺省情况——调用 `specializeTypelessValueToConstant(TypelessValue::Zero, ptr.getType(), ...)` 生成一个「类型相关的零」（声明见 [Utils/Utils.h](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/include/Utils/Utils.h)，`TypelessValue` 枚举有 `Zero/Min/Max/Undefined`）；第 372-373 行用 `contMask` 做受界 load；第 376-380 行用 `andi(contMask, discMask)` 做 `select`。
- 第 384-397 行：**纯离散分支**。无界 `load(ptr)`（第 392-393 行）后 `select(mask, newLoad, other)`。

**对照真实测试**：[loadstore.mlir:3-6](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/unittest/Conversion/General/DiscreteMaskAccess/loadstore.mlir#L3-L6) 的 FileCheck 正是纯离散分支的产物——`tt.load %5, %3, %cst` 被改写成 `tt.load %5` + `arith.select %3, loaded, %cst`。而无 `other` 的版本 [loadstore.mlir:24-28](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/unittest/Conversion/General/DiscreteMaskAccess/loadstore.mlir#L24-L28) 则展示了「先合成 `arith.constant dense<0>` 再 select」。

#### 4.3.4 代码实践（规格指定的核心实践）

**目标**：亲眼看到一个带非连续 mask 的 `tl.load` 被改写成 `load + select`。

**方式 A——纯 IR（推荐，无需 NPU）**：用构建产物 `triton-opt` 直接跑 FileCheck 用例。

1. 在已编译的仓库里找到 `triton-opt`（构建产物，路径见 `compiler.py` 的 `_get_triton_opt_path()`）。
2. 执行（注意 `--split-input-file` 让一个文件里多个 `tt.func` 各自独立检查）：
   ```bash
   triton-opt --discrete-mask-access-conversion --split-input-file \
     third_party/ascend/unittest/Conversion/General/DiscreteMaskAccess/loadstore.mlir
   ```
3. 在输出里找 `@discrete_load`，确认原来的 `tt.load %5, %3, %cst` 消失了，取而代之的是不带 mask 的 `tt.load` + `arith.select`。

**方式 B——从 Python 触发**：写一个 kernel，其 load 的 mask 是「两段区间的或」：

```python
# 示例代码：仅用于说明 mask 形态，非项目原有文件
import triton, triton.language as tl, torch, torch_npu

@triton.jit
def discrete_load_kernel(ptr, out, N: tl.constexpr, BLOCK: tl.constexpr):
    offs = tl.arange(0, BLOCK)
    # 离散掩码：选中 offs<200 或 offs>400 的位置（两段不连续区间）
    mask = (offs < 200) | (offs > 400)
    x = tl.load(ptr + offs, mask=mask, other=0.0)
    tl.store(out + offs, x, mask=mask)
```

3. 设 `TRITON_DEBUG=1` 运行，在 dump 出的 `kernel.ttir.mlir` / `kernel.ttadapter.mlir`（位于 `make_ttir`/`ttir_to_linalg` 打印的 dump 目录）里，定位到 `arith.ori` 掩码驱动的 `tt.load`，确认它在本 pass 之后变成 `tt.load` + `arith.select`。

**需要观察的现象**：改写后不再有带 `mask=%3` 的 `tt.load`；新增了一条 `arith.select %3, %loaded, %other`。

**预期结果**：与 `loadstore.mlir` 的 FileCheck 一致。Python 路径的具体 dump 文件名/路径以本机 `TRITON_DEBUG` 输出为准（**待本地验证**）。

#### 4.3.5 小练习与答案

- **练习**：受界分支里，为什么 `select` 的条件是 `andi(contMask, discMask)` 而不是只用 `discMask`？
- **答案**：`safeLoad` 只在 `contMask` 为真的范围内有效；在 `contMask` 为假的位置其值未定义。用 `contMask & discMask` 作条件，可在这些位置直接取 `other`，避免把未初始化内存当成结果。

---

### 4.4 DiscreteMaskStoreConversion：离散 store → 读改写 + 跨核锁

> 这是规格要求的第二个核心模块。

#### 4.4.1 概念说明

`tt.store ptr, value, mask` 在离散 mask 下更棘手：硬件只能整块写，而你想「只改 mask 为真的位置、其余保持原值」。唯一等价做法是**读改写（read-modify-write, RMW）**：

1. 先 `load` 目标地址（拿到旧值）；
2. `select(mask, value, old)` 合成「该改的改、不该改的保留」；
3. 再 `store` 回去。

这带来两个新问题：

- **越界更严重**：纯离散分支下，为合成旧值必须 `load(dst)`（无界），可能读到有效范围之外。同样靠 `contMask` 受界缓解。
- **跨核数据竞争**：多个 program（核）可能对同一目标块做 RMW。若两个核交错「读旧值→写新值」，后写者会覆盖前者的「保留位」。因此需要 `hivm.sync_block_lock` / `sync_block_unlock` 把这段 RMW 临界区串行化。这也是本 pass 与运行时（u5）和 AutoBlockify（u2-l2/u2-l3）联动的关键点。

#### 4.4.2 核心流程

```
DiscreteMaskStoreConversion.matchAndRewrite(store):
  if not isDiscreteMask(store): return failure
  if 950 + forceSimtTemplate + rank <= 5:
      store.setAttr("route_discrete_mask_to_simt"); return failure
  (contMask, discMask) = decomposeAndMask(store.mask)
  if contMask and discMask:                              # 受界 RMW
      lockVar = createSyncBlockLockVar()
      if enableSyncBlockLockFlag: SyncBlockLockOp(lockVar)
      safeLoad = load(dst, mask=contMask)
      sel = select(discMask, src, safeLoad)
      newStore = store(dst, sel, mask=contMask) {DiscreteMask}
      if enableSyncBlockLockFlag: SyncBlockUnlockOp(lockVar)
      return newStore
  # 纯离散 RMW（有 DDR OOB 风险）
  lockVar = createSyncBlockLockVar()
  if enableSyncBlockLockFlag: SyncBlockLockOp(lockVar)
  old = load(dst)
  sel = select(mask, src, old)
  newStore = store(dst, sel) {DiscreteMask}
  if enableSyncBlockLockFlag: SyncBlockUnlockOp(lockVar)
  return newStore
```

注意受界 store 分支里 `select` 的条件是 **`discMask`**（不是合并掩码）——因为 `safeLoad` 已被 `contMask` 限定为有效，可以直接作为「保留位」的旧值；而最终 store 又用 `contMask` 限定写回范围。新生成的 store 还被打上 `DiscreteMask` 属性（`ConverterUtils::discreteMaskAttrName`），供下游识别。

#### 4.4.3 源码精读

[third_party/ascend/lib/DiscreteMaskAccessConversion/DiscreteMaskAccessConversionPass.cpp:266-335](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/DiscreteMaskAccessConversion/DiscreteMaskAccessConversionPass.cpp#L266-L335) 是 `DiscreteMaskStoreConversion`：

- 第 276-277 行：`isDiscreteMask` 闸门。
- 第 279-287 行：950/SIMT 让权分支。
- 第 292-312 行：**受界 RMW**。第 295 行 `createSyncBlockLockVar`（来自 [MemOpConverter.h](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/include/TritonToStructured/MemOpConverter.h) 的 `MemOpConverter::createSyncBlockLockVar`）创建锁变量；第 296-298 行按 `enableSyncBlockLockFlag` 决定是否插 `SyncBlockLockOp`；第 299-300 行用 `contMask` 受界 load；第 301-302 行 `select(discMask, src, safeLoad)`；第 303-306 行受界 store 并打 `discreteMaskAttrName` 属性；第 307-309 行配对的 `SyncBlockUnlockOp`。
- 第 317-333 行：**纯离散 RMW**。无界 `load(dst)`（第 321-322 行），`select(mask, src, old)`，无 mask 的 store。

**锁开关的真实来源**——回到驱动函数：

[third_party/ascend/lib/DiscreteMaskAccessConversion/DiscreteMaskAccessConversionPass.cpp:462-465](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/DiscreteMaskAccessConversion/DiscreteMaskAccessConversionPass.cpp#L462-L465) 把 `compileOn91095Flag`、`forceSimtTemplateFlag` 从选项写入；而 `enableSyncBlockLockFlag` 并非直接取自 `this->enableSyncBlockLock` 选项，而是取自 **块非重叠分析** 的反：`enableSyncBlockLockFlag = !tileNonOverlap`。`tileNonOverlap` 由 `checkAllProgramIdNonOverlap(module)` 算出（见 4.4 节末与下文）。

> **易错点**：`Passes.td` 声明了 `enable-sync-block-lock` 选项（默认 `true`），且 `compiler.py:205` 确实把 `enable_sync_block_lock` 传了进来，但当前 HEAD 的 `runOnOperation()` 并未把该选项值用于决定 `enableSyncBlockLockFlag`——决定权在 `checkAllProgramIdNonOverlap()` 的分析结果。即「块不重叠 → 不需要锁；块可能重叠 → 插锁」。

**非重叠分析** `checkAllProgramIdNonOverlap` + `traceUserToTargetOp`：

[third_party/ascend/lib/DiscreteMaskAccessConversion/DiscreteMaskAccessConversionPass.cpp:164-172](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/DiscreteMaskAccessConversion/DiscreteMaskAccessConversionPass.cpp#L164-L172) 遍历所有 `tt.get_program_id`，对每个调用 `traceUserToTargetOp`。后者（[L61-L162](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/DiscreteMaskAccessConversion/DiscreteMaskAccessConversionPass.cpp#L61-L162)）沿 use 链查找经典的「`pid * BLOCK_SIZE + arange(0, BLOCK_SIZE)`」块偏移模式：找到一个 `muli(_, 常量 BLOCK)`，再看其结果是否加到一个 `make_range{end == BLOCK}` 上（第 98-129 行）。若每个 program_id 都能匹配到这种「按块大小切分」的模式，说明各 program 处理的是不重叠的块 → `tileNonOverlap = true` → 不插锁。

**下游联动**：插了 `sync_block_lock` 之后，[compiler.py:391-396](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L391-L396) 在 `_parse_linalg_metadata` 里用正则 `\bsync_block_lock\b` 探测到它，就把 `metadata["has_auto_blockify_blacklist_op"]` 置 `True`——这会关闭 AutoBlockify（见 u2-l2/u2-l3），因为把逻辑块打包进顺序循环与「跨块 RMW 锁」互斥。

#### 4.4.4 代码实践（源码阅读型）

**目标**：理解 store 的「读改写」改写与锁的插入条件。

**步骤**：

1. 读 [loadstore.mlir:46-50](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/unittest/Conversion/General/DiscreteMaskAccess/loadstore.mlir#L46-L50)（`@discrete_store`）。注意它的 IR 里没有 `program_id`，所以 `checkAllProgramIdNonOverlap` 不会发现块偏移模式。
2. 读板端回归测试 [test_discrete_mask_tail_block_mte_oob.py:58-81](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/unittest/pytest_ut/test_discrete_mask_tail_block_mte_oob.py#L58-L81)：这是一个「连续掩码 `row_boundary` AND 离散掩码 `row_disc`」的真实 kernel，正是受界分支的用例。

**需要观察的现象**：测试注释说明——修复前，copy 大小是 `BLOCK_M × BLOCK_N × 2 = 32768` 字节（无界，越界）；修复后受界为 `M × BLOCK_N × 2 = 8192` 字节（安全）。

**预期结果**：该测试在板端验证「受界 load」确实避免了 MTE（Memory Tag Extension）越界错误。**待本地验证**（需要真实 NPU 与特定内存布局）。

#### 4.4.5 小练习与答案

- **练习 1**：为什么 store 比 load 更需要 `sync_block_lock`？
- **答案**：离散 store 是「读旧值→合并→写回」的 RMW，多个核若交错执行会互相覆盖对方想保留的位；load 只读不写，无此竞争。
- **练习 2**：若一个 kernel 的所有 program_id 都满足 `pid * BLOCK + arange` 模式（块不重叠），会发生什么？
- **答案**：`tileNonOverlap=true` → `enableSyncBlockLockFlag=false`，store 改写不插锁（因为各核写的地址不重叠，无需互斥）。

---

### 4.5 DiscreteMaskAtomicConversion 与 950 / SIMT 模板联动

#### 4.5.1 概念说明

第三个 pattern 处理 `tt.atomic_rmw`（原子读改写）。它的思路与 load/store 不同：原子操作本身是逐元素的，所以可以把离散 mask **推进数值里**——对 mask 为假的位置，用一个「对原子操作无害的中性元（neutral element）」替换 `src`，然后做**无 mask 的原子操作**。例如 `atomic_rmw add`：把 `src` 改成 `select(mask, src, 0)`，加 0 不改变目标值，语义等价于「只在 mask 为真处累加」。

不同 RMW 操作的中性元不同：`add/or/xor` 用 0，`min` 用最大值，`max` 用最小值，`and` 用全 1……代码里用 `TypelessValue` 枚举（`Zero/Min/Max/Undefined`）抽象。

**950 / SIMT 模板联动**：前面三个 pattern 都有同一条「让权分支」——当 `compileOn91095 && forceSimtTemplate && rank<=5` 时，不给 SIMD 改写，而是给原 op 打上 `route_discrete_mask_to_simt` 属性并返回 `failure`。这个属性随后被 `triton-to-unstructure`（u4-l4）消费，把该访存路由到 **SIMT 间接访存快路径**（`indirect_load`/`indirect_store`/间接 atomic）。也就是说：在 950 的混合模式下，离散访存的真正处理不在本 pass，而在下游的 SIMT 模板里；本 pass 只负责「挂号」。

#### 4.5.2 核心流程

**Atomic 改写**：

```
DiscreteMaskAtomicConversion.matchAndRewrite(atomic_rmw):
  if not isDiscreteMask(atomic): return failure
  neutral = initMap[rmwOp]              # add→Zero, min→Max, max→Min, and→Max, ...
  if neutral == Undefined:              # XCHG 没有中性元
      atomic.setAttr("DiscreteMask"); return failure   # 留给 AscendNPU-IR 分解
  fill = specializeTypelessValueToConstant(neutral, src.type)
  maskedValue = select(mask, src, fill)
  newAtomic = atomic_rmw(rmwOp, ptr, maskedValue, mask=Value())  # 无 mask 原子
  return newAtomic
```

中性元表（来自源码 `initMap`）：

| RMW 操作 | 中性元 `TypelessValue` | 含义 |
| --- | --- | --- |
| `add` / `fadd` / `or` / `xor` | `Zero` | 加/或/异或 0 不变 |
| `and` | `Max`（全 1） | 与全 1 不变 |
| `min` / `umin` | `Max` | 与最大值取 min 不变 |
| `max` / `umax` | `Min` | 与最小值取 max 不变 |
| `xchg` | `Undefined` | 无中性元，特殊处理 |

**SIMT 路由（load/store）**：`route_discrete_mask_to_simt` 属性的流转——本 pass 打标记 → `triton-to-unstructure` 读标记 → 走 `indirect_load/store` 快路径。

#### 4.5.3 源码精读

**Atomic pattern**：

[third_party/ascend/lib/DiscreteMaskAccessConversion/DiscreteMaskAccessConversionPass.cpp:401-455](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/DiscreteMaskAccessConversion/DiscreteMaskAccessConversionPass.cpp#L401-L455) 是 `DiscreteMaskAtomicConversion`。第 416-427 行就是上表的中性元映射 `initMap`；第 430-435 行处理 `XCHG`（`Undefined`）——只打 `discreteMaskAttrName` 属性后返回 `failure`，注释说明「会在 AscendNPU-IR 里分解」；第 437-446 行把中性元特化成具体类型的常量（失败则 `emitError`）；第 448-452 行 `select(mask, src, fill)` 后生成**无 mask** 的 `atomic_rmw`（注意第 450 行 mask 参数传 `mlir::Value()` 即空）。

> 注意：本 pattern 只处理 `tt.atomic_rmw`，**不**处理 `tt.atomic_cas`（后者由 [IndirectAtomicUtils.h](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/include/TritonToUnstructure/IndirectAtomicUtils.h) 等机制负责）。

**SIMT 路由标记的诞生**：

[third_party/ascend/lib/DiscreteMaskAccessConversion/DiscreteMaskAccessConversionPass.cpp:58-59](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/DiscreteMaskAccessConversion/DiscreteMaskAccessConversionPass.cpp#L58-L59) 定义属性名常量 `route_discrete_mask_to_simt`；load/store pattern 在 950+SIMT 条件下用 `op->setAttr(routeDiscreteMaskToSimtAttrName, rewriter.getUnitAttr())` 打标记（[L283-L287](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/DiscreteMaskAccessConversion/DiscreteMaskAccessConversionPass.cpp#L283-L287) 与 [L353-L357](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/DiscreteMaskAccessConversion/DiscreteMaskAccessConversionPass.cpp#L353-L357)）。

**下游消费**（`triton-to-unstructure`）：

[third_party/ascend/lib/TritonToUnstructure/UnstructureConversionPass.cpp:455](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToUnstructure/UnstructureConversionPass.cpp#L455) 读出该属性 `routeDiscreteMaskToSimt`；[L467-L470](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToUnstructure/UnstructureConversionPass.cpp#L467-L470) 用它放行「本应结构化但因标记而改走 SIMT」的访存；[L533-L536](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToUnstructure/UnstructureConversionPass.cpp#L533-L536) 把它并入 SIMT 间接快路径的启用条件 `indirectFastPathEnabled`。

**对照 950 测试**：[indirect_loadstore.mlir:1-4](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/unittest/Conversion/950PR/DiscreteMaskAccess/indirect_loadstore.mlir#L1-L4) 用 `--discrete-mask-access-conversion=compile-on-910-95=True force-simt-template=True` 跑，FileCheck 期望 `tt.load`/`tt.store` 上**保留** `{route_discrete_mask_to_simt}` 属性（而非被改写成 `select`），证明在 950+SIMT 下本 pass 「只挂号、不改写」。

#### 4.5.4 代码实践（源码阅读型）

**目标**：对比同一 IR 在「纯 SIMD」与「950+SIMT」两种开关下的不同产出。

**步骤**：

1. 用纯 SIMD 跑：`triton-opt --discrete-mask-access-conversion loadstore.mlir`，看到 `arith.select`。
2. 用 950+SIMT 跑：`triton-opt '--discrete-mask-access-conversion=compile-on-910-95=True force-simt-template=True' third_party/ascend/unittest/Conversion/950PR/DiscreteMaskAccess/indirect_loadstore.mlir`，看到 `{route_discrete_mask_to_simt}` 属性、且**没有** `arith.select`。

**需要观察的现象**：同一类「离散 mask 访存」，前者被就地改写，后者仅被打标记交给下游。

**预期结果**：与两个 FileCheck 用例的 `CHECK` 行一致。命令能否运行取决于本机是否已编译 `triton-opt`（**待本地验证**）。

#### 4.5.5 小练习与答案

- **练习 1**：为什么 `atomic_rmw max` 的中性元是 `Min`（最小值）？
- **答案**：`max(x, 最小值) == x`，对目标值无影响；若用 0，当目标本为负数时 `max(x,0)` 会错误地抬升目标。
- **练习 2**：在 950+`force_simt_template` 下，一个秩为 6 的离散 load 会走 SIMT 路由吗？
- **答案**：不会。SIMT 路由还要求 `rankWithinIndirectFastPathLimit`（秩 ≤ 5）；秩 6 不满足，会回落到 SIMD 的 `load+select` 改写（见 [L350-L357](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/DiscreteMaskAccessConversion/DiscreteMaskAccessConversionPass.cpp#L350-L357)）。

---

## 5. 综合实践

**任务**：把本讲的「识别 → 分解 → 改写 → 联动」串起来，画一张本 pass 的行为决策图，并用 IR 验证其中两条路径。

**步骤**：

1. **画决策图**。以一个带 mask 的 `tt.load`/`tt.store`/`tt.atomic_rmw` 为入口，按以下顺序自顶向下画出分支（用你自己的话）：
   - `isDiscreteMask` 通过吗？（连续 → 不改写）
   - 950 + `force_simt_template` + `rank≤5`？（是 → 打 `route_discrete_mask_to_simt` 标记，让权）
   - 能拆出 `contMask & discMask` 吗？（是 → 受界 load/select/store；否 → 无界 load/select）
   - 是 store 且块可能重叠？（是 → 包裹 `sync_block_lock/unlock`）
2. **验证纯离散路径**：跑 `loadstore.mlir`，确认 `@discrete_load` 与 `@discrete_store` 的改写结果与你的图一致（参见 4.3.4）。
3. **验证 SIMT 路由路径**：跑 `indirect_loadstore.mlir`（带 950 选项），确认 load/store 仅被打标记、未被改写（参见 4.5.4）。
4. **追踪下游**：在 `UnstructureConversionPass.cpp` 里 grep `route_discrete_mask_to_simt`，确认该标记在 `triton-to-unstructure` 中被读出并启用 SIMT 间接快路径（[L455](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToUnstructure/UnstructureConversionPass.cpp#L455)、[L536](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToUnstructure/UnstructureConversionPass.cpp#L536)）。

**预期产出**：一张决策图 + 两段 IR 证据 + 一句对「标记如何跨 pass 流转」的说明。IR 命令需在本机构建环境中运行（**待本地验证**）。

## 6. 本讲小结

- `discrete-mask-access-conversion` 是结构化 pass（u4-l2）之后的**离散访存兜底 pass**：只处理 `MaskState::parse()` 解析失败的离散掩码访存，连续的不管。
- 它把 `load/store/atomic_rmw` 的「掩码门控」改写成 `load + arith.select (+store)`，让离散选择变成数值层面的 `select`。
- 利用 `decomposeAndMask` 把 AND 树拆成 `contMask & discMask`：用 `contMask` 限定访存范围（防 OOB/越界 MTE），用 `discMask` 驱动 `select`。
- 离散 store 是「读改写」，需要 `sync_block_lock/unlock` 串行化跨核临界区；是否插锁由 `checkAllProgramIdNonOverlap` 的块非重叠分析决定（块不重叠则免锁），而非直接由 `enable-sync-block-lock` 选项决定。
- 离散 atomic 把 mask 推进数值（`select(mask, src, neutral)`）后做无 mask 原子操作；不同 RMW 用不同中性元，`xchg` 无中性元需特殊处理。
- 在 950 + `force_simt_template` 且秩 ≤ 5 时，本 pass 只打 `route_discrete_mask_to_simt` 标记，把改写权让给下游 `triton-to-unstructure` 的 SIMT 间接快路径；插锁会触发下游关闭 AutoBlockify。

## 7. 下一步学习建议

- **u4-l4（TritonToUnstructure：离散访存标量化）**：本 pass 打下的 `route_discrete_mask_to_simt` 标记在那里被消费，离散访存会被展开成标量循环或走 SIMT 间接 `indirect_load/store`。建议重点读 `UnstructureConversionPass.cpp` 中读该属性的几处。
- **u6-l2（离散访存 SIMT 模板与纯 SIMT 路径）**：从更高层理解 `indirect_load/store`、`--pure-simt` 等 SIMT 路径如何承接本 pass 的「挂号」。
- **u5-l3（内核启动：sync_block_lock）**：本 pass 生成的 `sync_block_lock` 在运行时如何映射到 `lock_num`/`lock_init_val`（见 `compiler.py` 的 `_infer_sync_block_lock_num_function` 回调）。
- **源码延伸**：若想理解 `MaskState::parse()` 为何对某些掩码失败，可回看 [TritonToStructured/MaskAnalysis.h](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/include/TritonToStructured/MaskAnalysis.h) 与 u4-l2，对照「连续 vs 离散」的边界条件。

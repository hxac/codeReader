# 离散访存 SIMT 模板与纯 SIMT 路径

## 1. 本讲目标

本讲是「SIMD 与 SIMT 双编译路径」单元的第二篇，承接 [u6-l1](u6-l1-compile-mode-overview.md) 讲清的 `compile_mode` 三种模式与 `force_simt_only` / `force_simt_template` 两个派生开关。u6-l1 只回答了「模式怎么分流」，本讲要钻进**分流之后到底发生了什么**。

学完后你应该能够：

- 说清混合模式（`unstructured_in_simt`）下，一个离散访存点是怎么从 `tt.load`/`tt.store` 变成 `indirect_load`/`indirect_store`、从 `atomic` 变成 `indirect_atomic` 的，以及「打标记 → 改写」两步分别在哪个 pass 完成。
- 复述 SIMT 模板快路径的**三个启用条件**（950 + `force_simt_template` + 秩 ≤ 5 等）与**回退语义**（条件不满足时退回标量循环）。
- 读懂纯 SIMT 路径 `ttir_to_npubin`，说清 `--pure-simt` / `--num-warps` / `--threads-per-warp` 等编译选项的作用，以及它为何能跳过整个 Linalg 主线。

## 2. 前置知识

本讲默认你已经掌握以下概念（否则请先读对应讲义）：

- **TTIR / Linalg IR / AscendNPU IR**：Triton 的三层中间表示，越往右越贴近硬件（见 [u1-l1](u1-l1-project-overview-and-architecture.md)）。
- **结构化访存 vs 离散/非结构化访存**：地址能用等差数列（基址 + 每维 stride/shape）描述的是结构化访存，向量化单元（Vector Core）可用 DMA 批量搬运；地址由数据相关的索引张量算出、无等差规律的是离散/非结构化访存，向量化单元搬不动（见 [u4-l2](u4-l2-triton-to-structured.md)、[u4-l4](u4-l4-triton-to-unstructure.md)）。
- **`compile_mode` 三模式与派生字段**：`simd` / `unstructured_in_simt`（默认）/ `simt_only`，经 `NPUOptions.__post_init__` 派生出 `force_simt_only`、`force_simt_template`、`parallel_mode`（见 [u6-l1](u6-l1-compile-mode-overview.md)）。
- **SIMD vs SIMT**：SIMD（单指令多数据）是昇腾 Vector 核的批量执行模型；SIMT（单指令多线程）是 950（A5）新增的、按线程处理不规则访存的执行模型，擅长间接寻址。

两个本讲要用到的关键结论（来自 u6-l1，不重复推导）：

- `force_simt_only = True` 时，`add_stages` 只注册 `ttir → npubin` 并提前返回，**跳过 Linalg 主线**。
- `force_simt_template` 是 `ttir_to_linalg` 内部的**软开关**，控制离散掩码 / 非结构化访存 pass 是否生成 SIMT 模板；它只在 **950 + 秩 ≤ 5** 时真正生效。
- SIMT **仅在 950（A5）生效**；非 950 平台上混合模式退化为与 `simd` 完全等价。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| `third_party/ascend/backend/compiler.py` | 编译后端门面与阶段装配 | `ttir_to_npubin` 纯 SIMT 编译、`add_stages` 的 `force_simt_only` 短路径、`_parse_ttir_metadata` |
| `third_party/ascend/lib/DiscreteMaskAccessConversion/DiscreteMaskAccessConversionPass.cpp` | 离散掩码 pass | 满足条件时给访存打 `route_discrete_mask_to_simt` 标记、让权下游 |
| `third_party/ascend/lib/TritonToUnstructure/UnstructureConversionPass.cpp` | 非结构化访存 pass | `tryRewriteIndirectFastPath`：把 load/store 改写为 `indirect_load/store`、把 atomic 改写为间接 atomic；快路径闸门与回退 |
| `third_party/ascend/lib/TritonToUnstructure/IndirectAtomicUtils.cpp` | 间接原子工具 | `__builtin_indirect_atomic`、元素类型与静态形状约束 |
| `third_party/ascend/unittest/Conversion/950PR/...` | MLIR conversion 回归测试 | 用 FileCheck 离线复现「标记→indirect_load/store→library call」 |
| `third_party/ascend/unittest/autotune_ut/test_reduce_simt.py` | 端到端用例 | `compile_mode='simt_only'` 的真实写法 |
| `docs/en/architecture_design_and_core_features.md` | 架构文档 | SIMT Compiler（950）章节、三模式编译流程对比 |

## 4. 核心概念与源码讲解

### 4.1 离散访存的 SIMT 快路径：indirect_load / indirect_store

#### 4.1.1 概念说明

回忆 [u4-l4](u4-l4-triton-to-unstructure.md)：当 `tl.load`/`tl.store` 的地址是数据相关索引算出的（间接寻址、离散掩码），向量化单元无法批量搬运，SIMD 路径只能把它展开成一串 `scf.for` 标量循环——一个元素一个元素地读写，性能很差。

950（A5）多了 SIMT 执行单元，**天生擅长间接寻址**：硬件能按一组不规则地址一次性取数。于是 Triton-Ascend 在混合模式下为这类访存准备了一条「SIMT 模板快路径」：不再展开成标量循环，而是把 `tt.load`/`tt.store` 改写成两个专门的算子 `ascend.indirect_load` / `ascend.indirect_store`，让下游知道「这一处请用 SIMT 模板来发射」。

> 注意：`indirect_load`/`indirect_store` **不是用户在 Python 里直接调用的 API**。在 `language/cann/extension/mem_ops.py` 里你找不到它们——它们是编译器在 pass 内部生成的 `triton::ascend::IndirectLoadOp` / `IndirectStoreOp`。架构文档把它们与 `index_select`/`index_put` 并列列为「Ascend 亲和算子」，是从「概念上存在」的角度说的；用户侧真正能调的是 `index_select`/`index_put`/`gather_out_to_ub`/`scatter_ub_to_out` 等。这点容易误解，务必分清。

这条快路径是**逐访存点判断**的，不是把整个 kernel 都搬到 SIMT——这是 u6-l1 强调过的混合模式语义。

#### 4.1.2 核心流程

一次离散访存走 SIMT 快路径，要经过两个 pass、分两步：

```
discrete-mask-access-conversion        triton-to-unstructure          triton-to-linalg
        (打标记)                            (改写算子)                    (lowering)
tt.load %p, %mask   ──①满足条件──►   tt.load %p, %mask   ──②命中闸门──►   call @triton_indirect_load(...)
                                      {route_discrete_mask_to_simt}        ascend.indirect_load
```

**第一步：打标记（discrete-mask-access-conversion）。** 这个 pass 本职是把离散掩码访存降级为 `load + select`（见 [u4-l3](u4-l3-discrete-mask-access-conversion.md)）。但在 950 + 混合模式下，它多了一条捷径：如果判定这是离散掩码、且满足 SIMT 快路径条件，它**不改写 IR**，只给该访存 op 贴一个 `{route_discrete_mask_to_simt}` 属性，然后 `return failure()`（表示「我不处理，交给下游」）。

**第二步：改写算子（triton-to-unstructure）。** 这个 pass 本职是把非结构化访存展开成标量循环（见 [u4-l4](u4-l4-triton-to-unstructure.md)）。但它先检查 SIMT 快路径闸门：若启用且秩 ≤ 5，就调用 `tryRewriteIndirectFastPath`，把 `tt.load` 直接替换成 `ascend.indirect_load`、`tt.store` 替换成 `ascend.indirect_store`，提前 `return success()`，**跳过标量循环展开**。

**第三步：lowering（triton-to-linalg）。** `indirect_load`/`indirect_store` 最终被 lower 成对运行库函数 `@triton_indirect_load` / `@triton_indirect_store` 的 `call`，由 BiSheng 工具链映射到 950 的 SIMT 间接寻址指令。

启用快路径需要同时满足三个条件（闸门）：

1. `compile_on_910_95` 为真（即 950 / A5 平台）；
2. `force_simt_template` 为真（即混合模式 `unstructured_in_simt`，由 `compile_mode` 派生）；
3. 张量秩（rank） ≤ 5——SIMT 模板目前最多支持 5 维张量。

任一条件不满足，就**回退**到标量循环路径（与纯 `simd` 模式一致）。

#### 4.1.3 源码精读

**(a) 打标记的逻辑。** 在 `DiscreteMaskAccessConversionPass.cpp` 中，离散 store 的 converter 判定离散掩码后，检查三条件并贴属性：

[third_party/ascend/lib/DiscreteMaskAccessConversion/DiscreteMaskAccessConversionPass.cpp:347-357](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/DiscreteMaskAccessConversion/DiscreteMaskAccessConversionPass.cpp#L347-L357) —— 先 `isDiscreteMask` 判定是离散掩码，再在 `compileOn91095Flag && forceSimtTemplateFlag && rankWithinIndirectFastPathLimit`（秩 ≤ 5）三者同时成立时，`setAttr(routeDiscreteMaskToSimtAttrName, ...)` 贴标记并 `return failure()` 让权下游。

离散 load 的 converter 逻辑完全对称：

[third_party/ascend/lib/DiscreteMaskAccessConversion/DiscreteMaskAccessConversionPass.cpp:279-287](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/DiscreteMaskAccessConversion/DiscreteMaskAccessConversionPass.cpp#L279-L287) —— load 侧同样在「950 + force_simt_template + 秩 ≤ 5」时贴 `{route_discrete_mask_to_simt}` 并返回 failure。

属性名字符串定义在 pass 顶部：

[third_party/ascend/lib/DiscreteMaskAccessConversion/DiscreteMaskAccessConversionPass.cpp:58-59](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/DiscreteMaskAccessConversion/DiscreteMaskAccessConversionPass.cpp#L58-L59) —— `routeDiscreteMaskToSimtAttrName = "route_discrete_mask_to_simt"`。

**(b) 快路径闸门与改写。** 在 `UnstructureConversionPass.cpp` 中，源文件顶部一段注释把整套机制讲得最清楚：

[third_party/ascend/lib/TritonToUnstructure/UnstructureConversionPass.cpp:114-148](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToUnstructure/UnstructureConversionPass.cpp#L114-L148) —— 注释说明：闸门需 `compileOn91095Flag && forceSimtTemplateFlag` 且访存「非结构化，或带 `route_discrete_mask_to_simt` 标记」；`tt.load/store` 还要求秩 ≤ 5；改写目标是 `tt.indirect_load / tt.indirect_store`；无法改写则优雅回退到标量循环。

`tryRewriteIndirectFastPath` 是改写主体，秩 ≤ 5 的硬限制就在函数入口：

[third_party/ascend/lib/TritonToUnstructure/UnstructureConversionPass.cpp:154-184](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToUnstructure/UnstructureConversionPass.cpp#L154-L184) —— `rankWithinIndirectLoadStoreFastPathLimit = resultShape.size() <= 5`；LoadOp 分支在秩超限时 `return failure()`，否则 `rewriter.create<triton::ascend::IndirectLoadOp>(...)` 并 `replaceOp`，日志打印 `Rewriting tt.load to tt.indirect_load`。

StoreOp 分支对称，把 `tt.store` 改写为 `ascend.indirect_store`：

[third_party/ascend/lib/TritonToUnstructure/UnstructureConversionPass.cpp:185-215](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToUnstructure/UnstructureConversionPass.cpp#L185-L215) —— 同样先查秩 ≤ 5，再 `create<triton::ascend::IndirectStoreOp>`；对 bool store 还会先解开 `ptr<i1> → ptr<i8>` 的 bitcast。

调用 `tryRewriteIndirectFastPath` 的闸门在 `UnstructuredMemAccessConverter::matchAndRewrite` 里，由两个「或」条件触发：

[third_party/ascend/lib/TritonToUnstructure/UnstructureConversionPass.cpp:532-542](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToUnstructure/UnstructureConversionPass.cpp#L532-L542) —— `indirectFastPathEnabled = compileOn91095Flag && forceSimtTemplateFlag && ((!isStructured && sizeInByte < 64) || routeDiscreteMaskToSimt)`。也就是说，即便没有打标记，**非结构化且连续结构部分小于 64 字节**的访存也会走快路径；命中则 `tryRewriteIndirectFastPath` 成功即返回，不再展开标量循环。

**(c) 秩 > 5 的回退。** 当秩超过 5，快路径不生效，落回标量循环，并有明确日志：

[third_party/ascend/lib/TritonToUnstructure/UnstructureConversionPass.cpp:550-557](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToUnstructure/UnstructureConversionPass.cpp#L550-L557) —— `Skip tt.indirect_load/store fast path because rank is N (>5), falling back to scalar loop path`。

**(d) 闸门开关从哪来。** 两个 flag 在 pass 的 `runOnOperation` 开头从 pass option 读入：

[third_party/ascend/lib/TritonToUnstructure/UnstructureConversionPass.cpp:844-853](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToUnstructure/UnstructureConversionPass.cpp#L844-L853) —— `compileOn91095Flag = this->compileOn91095; forceSimtTemplateFlag = this->forceSimtTemplate;`。这两个 pass option 正是 `compiler.py` 的 `ttir_to_linalg` 在 `add_triton_to_unstructure(pm, compile_on_910_95, force_simt_template)` 处传入的（见 [u4-l1](u4-l1-ttir-to-linalg-pipeline-overview.md)），源头是 `NPUOptions` 的派生字段。

**测试佐证。** 这套「标记 → indirect_load/store」有现成的 FileCheck 测试可复现。打标记阶段：

[third_party/ascend/unittest/Conversion/950PR/DiscreteMaskAccess/indirect_loadstore.mlir:1-4](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/unittest/Conversion/950PR/DiscreteMaskAccess/indirect_loadstore.mlir#L1-L4) —— RUN 行用 `triton-opt '--discrete-mask-access-conversion=compile-on-910-95=True force-simt-template=True'`，CHECK 行确认输出 `tt.load ... {route_discrete_mask_to_simt}`。

改写阶段：

[third_party/ascend/unittest/Conversion/950PR/TritonToUnstructure/indirect_mem_access.mlir:1-4](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/unittest/Conversion/950PR/TritonToUnstructure/indirect_mem_access.mlir#L1-L4) —— RUN 行用 `triton-opt '--triton-to-unstructure=compile-on-910-95=True force-simt-template=True'`，CHECK 行确认 `ascend.indirect_load ... -> tensor<1024xi32>`。

lowering 阶段（unstructure + linalg 串跑）：

[third_party/ascend/unittest/Conversion/950PR/TritonToLinalg/indirect_load_rewrite.mlir:1-3](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/unittest/Conversion/950PR/TritonToLinalg/indirect_load_rewrite.mlir#L1-L3) —— RUN 行依次跑 `--triton-to-unstructure` 与 `--triton-to-linalg`，CHECK 行确认最终 lower 成 `call @triton_indirect_load(...)`。

#### 4.1.4 代码实践

**实践目标：** 用 `triton-opt` 离线复现「离散掩码 → 打标记 → indirect_load」三步，亲眼看到 IR 变化。这是不需要 NPU 设备的纯编译期实践。

**操作步骤：**

1. 确认已构建出 `triton-opt` 工具（源码编译产物，见 [u1-l3](u1-l3-installation-and-build.md)）。
2. 单步看「打标记」。执行测试文件 RUN 行的命令（把 `%s` 换成该 `.mlir` 文件路径），观察输出中 `tt.load` 是否带上了 `{route_discrete_mask_to_simt}`：

   ```bash
   triton-opt '--discrete-mask-access-conversion=compile-on-910-95=True force-simt-template=True' \
       --split-input-file \
       third_party/ascend/unittest/Conversion/950PR/DiscreteMaskAccess/indirect_loadstore.mlir
   ```

3. 单步看「改写」。对同一类输入跑 unstructure pass，观察 `tt.load` 是否变成 `ascend.indirect_load`：

   ```bash
   triton-opt '--triton-to-unstructure=compile-on-910-95=True force-simt-template=True' \
       third_party/ascend/unittest/Conversion/950PR/TritonToUnstructure/indirect_mem_access.mlir
   ```

4. 对照实验：把命令里的 `force-simt-template=True` 改成 `False`（等价于纯 `simd` 模式），再跑一次步骤 3。

**需要观察的现象：**

- 步骤 2：输出里 `tt.load` / `tt.store` 后面多出 `{route_discrete_mask_to_simt}` 属性，IR 结构本身没变。
- 步骤 3：输出里出现 `ascend.indirect_load` / `ascend.indirect_store`，且**没有** `scf.for` 标量循环。
- 步骤 4（对照）：不再出现 `indirect_load`，而是出现一串 `scf.for {ExtractedLoadOrStore}` 标量循环——这正是回退路径。

**预期结果：** `force-simt-template=True` 时命中 SIMT 模板（`indirect_load/store`）；`=False` 时回退到标量循环。两组对照清楚展示「闸门开关」的作用。

**若没有构建环境：** 待本地验证。可改为纯阅读型实践——打开 `indirect_mem_access.mlir`，对照本节 (b) 的源码，逐行解释 `@triton_indirect_load` 这个 1D 用例为什么命中快路径，而文件末尾 `discrete_highrank_and_structured_lowrank_loadstore_4d/5d` 用例的 CHECK 指向 `scf.for {DiscreteMemAccess}`（回退到标量循环）。

#### 4.1.5 小练习与答案

**练习 1：** 一个 kernel 同时含一处连续 `tl.load`（地址等差）和一处离散 `tl.load`（带离散掩码），在混合模式下会怎样？

**答案：** 连续访存仍走 SIMD（DMA 批量搬运，由 [u4-l2](u4-l2-triton-to-structured.md) 的结构化 pass 处理），离散访存走 SIMT 模板（`indirect_load`）。两者并存于同一 kernel，这正是「混合模式逐访存点判断、不整 kernel 迁移」的体现。

**练习 2：** 把步骤 3 命令里的 `compile-on-910-95=True` 改成 `False`，会发生什么？为什么？

**答案：** `indirect_load` 消失，回退为标量循环。因为 SIMT 快路径闸门要求 `compileOn91095Flag` 为真——SIMT 是 950（A5）才有的执行单元，非 950 平台即便 `force_simt_template=True`（混合模式）也拿不到 SIMT 硬件支撑，只能退化为纯 SIMD 的标量循环回退。

**练习 3：** 为什么 SIMT 模板要限制秩 ≤ 5？

**答案：** 源码注释明确写 `simt template only supports up to 5D tensors for now`（见 4.1.3 (b)）。这是当前 SIMT 模板实现的能力上限——更高维张量的间接寻址模板尚未实现，故超限即回退标量循环，留待后续扩展。

---

### 4.2 间接原子操作：indirect_atomic 快路径

#### 4.2.1 概念说明

`indirect_load`/`indirect_store` 处理的是普通读写；那 `tl.atomic_add` / `atomic_cas` 这类**原子读改写（RMW）**遇到离散地址怎么办？SIMD 路径同样是展开成标量循环、逐元素做原子操作。

950 的 SIMT 单元同样为间接原子提供了一条快路径：把 `tt.atomic_rmw` / `tt.atomic_cas` 改写成一个特殊的 `hivm.hir.custom` 自定义 op，符号名为 `__builtin_indirect_atomic`，把偏移、数值、掩码「拍平」成一维后交给硬件的间接原子指令。

注意它和 load/store 的两点不同：

- **没有专门的 `indirect_atomic` 方言算子**，而是复用 `hivm.hir.custom` 这个通用逃逸口，靠符号串 `__builtin_indirect_atomic` 区分语义。
- **约束更严**：要求偏移 / 数值 / 掩码张量是**静态形状**（因为要拍平成一维）；且部分元素类型不支持。

#### 4.2.2 核心流程

```
tt.atomic_rmw fadd, %ptr, %value, %mask
        │ 命中 SIMT 闸门（950 + force_simt_template + 静态形状）
        ▼
① canUseIndirectAtomicFastPath 校验（元素类型、偏移静态形状）
        │ 通过
        ▼
② tryConvertAtomicRmwToIndirectCustom：
   把 %offset/%value/%mask 拍平成 1D，掩码 i1→i8
        ▼
③ 生成 hivm.hir.custom { ... "operate=fadd" } "__builtin_indirect_atomic" ins(%ptr,%offset,%value,%mask) outs(%out)
        ▼
④ 把 1D 结果 reshape 回原张量形状
```

不满足约束（如元素类型是 i8/i16、或偏移是动态形状）时，`canUseIndirectAtomicFastPath` 返回 false，`tryRewriteIndirectFastPath` 对 atomic 分支 `return failure()`，于是同样**回退到标量循环**。

#### 4.2.3 源码精读

`tryRewriteIndirectFastPath` 的 atomic 分支：

[third_party/ascend/lib/TritonToUnstructure/UnstructureConversionPass.cpp:216-235](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToUnstructure/UnstructureConversionPass.cpp#L216-L235) —— `AtomicRMWOp` 分支先调 `IndirectAtomicUtils::canUseIndirectAtomicFastPath` 校验，失败则 `return failure()`（回退）；通过则 `tryConvertAtomicRmwToIndirectCustom` 生成 custom op 并 `replaceOp`。`AtomicCASOp` 分支紧随其后，逻辑对称。

约束的细节在工具文件里。符号名常量：

[third_party/ascend/lib/TritonToUnstructure/IndirectAtomicUtils.cpp:41-42](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToUnstructure/IndirectAtomicUtils.cpp#L41-L42) —— `kIndirectAtomicBuiltin = "__builtin_indirect_atomic"`。

元素类型约束（不能是 8 位 / 16 位整数）：

[third_party/ascend/lib/TritonToUnstructure/IndirectAtomicUtils.cpp:60-65](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToUnstructure/IndirectAtomicUtils.cpp#L60-L65) —— `canUseIndirectAtomicFastPathForElementType`：整数类型宽度不能是 8 或 16，其余（如 i32/i64/各种 float）通过。

偏移形状约束（需静态形状）：

[third_party/ascend/lib/TritonToUnstructure/IndirectAtomicUtils.cpp:67-72](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToUnstructure/IndirectAtomicUtils.cpp#L67-L72) —— `canUseIndirectAtomicFastPathForOffset`：偏移张量必须 `hasStaticShape`，或是标量 index。注释解释：拍平成一维是强制的，因为底层 SIMT 执行模型只认 1D 张量。

掩码 i1→i8 的转换理由也很值得读（避免对齐填充导致掩码错位）：

[third_party/ascend/lib/TritonToUnstructure/IndirectAtomicUtils.cpp:108-122](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToUnstructure/IndirectAtomicUtils.cpp#L108-L122) —— `castMaskToI8`，注释给出一个 2×3→6 的具体例子，说明直接 reshape i1 会因 i8 对齐填充错位、必须先 ExtUI 到 i8。

#### 4.2.4 代码实践

**实践目标：** 通过阅读测试与源码，理解「为什么某些 atomic 走不了间接快路径」。

**操作步骤：**

1. 打开 `third_party/ascend/unittest/Conversion/General/DiscreteMaskAccess/indirect_atomic.mlir`，找到它的 RUN 行与 CHECK 行，确认输出形如 `hivm.hir.custom ... "__builtin_indirect_atomic"`。
2. 回到 [IndirectAtomicUtils.cpp:60-72](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToUnstructure/IndirectAtomicUtils.cpp#L60-L72)。
3. 设想两个反例：一个 `atomic_add` 操作 `tensor<...xi8>`、一个偏移来自运行时动态形状张量。

**需要观察的现象 / 预期结果：**

- 正例（i32/f32、静态偏移）→ 生成 `__builtin_indirect_atomic` custom op。
- i8/i16 元素类型 → `canUseIndirectAtomicFastPathForElementType` 返回 false → 回退标量循环。
- 动态形状偏移 → `canUseIndirectAtomicFastPathForOffset` 返回 false → 回退标量循环。

**若需运行：** 用 `triton-opt '--triton-to-unstructure=compile-on-910-95=True force-simt-template=True'` 跑该 `.mlir` 即可对照 CHECK。待本地验证。

#### 4.2.5 小练习与答案

**练习 1：** 为什么间接 atomic 要求偏移是静态形状，而 `indirect_load` 没有这个要求？

**答案：** 间接 atomic 要把 offset/value/mask **拍平成一维**（见核心流程②），拍平需要知道各维大小的乘积，故要求静态形状；底层 SIMT 原子指令只认 1D。`indirect_load/store` 不做拍平，保留原张量形状（最多 5D），所以无此约束。

**练习 2：** 一个对 `bf16` 张量做 `atomic_cas` 的离散访存，能走间接 atomic 快路径吗？

**答案：** 不能。源码中 `isUnsupportedCasOrXchgElementType` 把 f16/bf16 列为 CAS/XCHG 不支持的类型（见 [IndirectAtomicUtils.cpp:56-58](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToUnstructure/IndirectAtomicUtils.cpp#L56-L58)）。这类情况会回退标量循环。

---

### 4.3 纯 SIMT 路径：ttir_to_npubin 与 --pure-simt 选项族

#### 4.3.1 概念说明

前两节讲的是**混合模式**下「个别离散访存点」借 SIMT 模板加速。还有一种更激进的选择：`compile_mode="simt_only"`——**整个 kernel 都用 SIMT 编译**。

u6-l1 已给出结论：`simt_only` → `force_simt_only=True` → `add_stages` 只注册 `{ttir, npubin}` 并提前返回，**跳过 Linalg 主线**。本节要回答：这个 `npubin` 阶段（即 `ttir_to_npubin` 函数）内部到底做了什么、传了哪些 SIMT 专属选项。

直觉上：混合模式是 `TTIR → Linalg IR → AscendNPU IR → .o`，把 TTIR 一路 lower 到 Linalg 再交给 BiSheng；纯 SIMT 则是 `TTIR → AscendNPU IR → .o`，**直接把 TTIR 文本喂给 BiSheng 编译器**，让它用自己的 `--pure-simt` 模式从 TTIR 自行 lower。这条路径更短，但只产出向量核（aiv）kernel，且目前主要面向 reduce / elementwise 这类无 `tl.dot` 的算子。

#### 4.3.2 核心流程

```
compile_mode="simt_only"
        │ NPUOptions.__post_init__ 派生 force_simt_only=True, parallel_mode="simt"
        ▼
add_stages：因 force_simt_only 为真 → 只注册
        stages["ttir"]   = make_ttir          （通用优化 pass，见 u3-l3）
        stages["npubin"] = ttir_to_npubin     （纯 SIMT 编译）
        return                            ← 提前返回，跳过 ttadapter/mlirbc/bcmlir
        ▼
ttir_to_npubin：
  ① _parse_ttir_metadata   （从 TTIR 抠 kernel_name；mix_mode 固定 "aiv"）
  ② 组装 BiSheng 命令行：
       --enable-hivm-compile=false      （关掉 hivm 这条 Linalg 后端路径）
       --enable-triton-ir-compile       （启用「直接吃 TTIR」路径）
       --pure-simt                      （纯 SIMT 编译模式）
       --num-warps / --threads-per-warp （SIMT 并行度）
       (+ 一组可选 SIMT 调优开关)
  ③ 调 bishengir-compile，产出 kernel.o（bytes）
```

对比混合模式默认路径的 `{ttir, ttadapter, mlirbc, bcmlir, npubin}` 五阶段（见 [u3-l2](u3-l2-ascend-backend-stages-and-options.md)），纯 SIMT 只有 `ttir → npubin` 两阶段，省去了所有 Linalg / 字节码中转环节。

#### 4.3.3 源码精读

**(a) 阶段注册的短路径。** `add_stages` 在 `force_simt_only` 为真时提前返回：

[third_party/ascend/backend/compiler.py:1271-1275](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L1271-L1275) —— 注册 `ttir`（`make_ttir`）后，`if options.force_simt_only:` 注册 `npubin`（`ttir_to_npubin`）并 `return`，后续的 `ttadapter`/`mlirbc`/`bcmlir` 与 910_95/A2_A3 两个 `npubin` 分支都不再注册。

**(b) 纯 SIMT 编译选项。** `ttir_to_npubin` 在 `force_simt_only` 分支里组装的关键选项：

[third_party/ascend/backend/compiler.py:1147-1165](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L1147-L1165) —— 核心四件套：`--enable-hivm-compile=false`（关闭走 hivm/Linalg 的常规路径）、`--enable-triton-ir-compile`（启用直接吃 Triton IR）、`--pure-simt`（纯 SIMT 模式）、`--num-warps` / `--threads-per-warp`（SIMT 并行度参数）；其后是 `enable_bishengir_simt_optimization`、`simt_stack_limit`、`shared_mem_dynamic_size`、`enable_simt_reorder_instruction`、`disable_fma` 等可选 SIMT 调优开关。

**(c) 元数据：mix_mode 固定 aiv。** 纯 SIMT 路径不走 Linalg，因此用 `_parse_ttir_metadata` 而非 `_parse_linalg_metadata`：

[third_party/ascend/backend/compiler.py:414-444](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L414-L444) —— 从 TTIR 抠 `kernel_name` 与 `tensor_kinds`；注释明确 `for TTIR inputs, we only support vector kernels`，故 `mix_mode` 硬编码为 `"aiv"`（第 432 行）。这也意味着纯 SIMT kernel 在运行时按 Vector 核数分配物理核（见 [u5-l1](u5-l1-npu-driver-and-utils.md)）。

**(d) 派生字段如何决定 shared_mem_dynamic_size。** `force_simt_only` 会改变默认的动态共享内存大小，这是 SIMT 执行模型需要的 per-block 局部存储（与 [u5-l3](u5-l3-kernel-launch-and-resources.md) 讲的 `rtKernelLaunchWithFlagV2` 携带的 `localMemorySize` 对应）：

[third_party/ascend/backend/compiler.py:1122-1126](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L1122-L1126) —— `force_simt_only` 时若未指定，`shared_mem_dynamic_size` 默认 122880（120 KB），否则 221184（216 KB）。

**(e) 文档侧的官方说明。** 架构文档把三种模式的编译路径列在同一张表里：

[docs/en/architecture_design_and_core_features.md:214-233](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/docs/en/architecture_design_and_core_features.md#L214-L233) —— `simt_only` 的编译路径标注为 `Triton IR → AscendNPU IR`（直达，不经 Linalg），并给出用法示例 `kernel[grid](..., compile_mode="simt_only", num_warps=32)`。

**端到端用例佐证。** `test_reduce_simt.py` 是一个真实的 reduce kernel 用 `simt_only` 启动的例子：

[third_party/ascend/unittest/autotune_ut/test_reduce_simt.py:68-74](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/unittest/autotune_ut/test_reduce_simt.py#L68-L74) —— `triton_unk_reduce[(grid_size, 1, 1)](..., compile_mode='simt_only')`。注意该 kernel 是一个含两层循环的 reduce（无 `tl.dot`），正是纯 SIMT 路径的典型场景；且测试用 `@pytest.mark.skipif(not is_compile_on_910_95(), reason="simt is support on A5")` 标注，再次印证 SIMT 仅 950 可用。

#### 4.3.4 代码实践

**实践目标：** 验证 `simt_only` 确实跳过 Linalg 阶段、直接从 TTIR 生成 npubin。这是本讲的主线实践。

**操作步骤（有 950 设备）：**

1. 设置环境并准备一个 reduce kernel。可直接复用 `test_reduce_simt.py`，或自己写一个 `tl.sum` 的 kernel。
2. 开启调试 dump：

   ```bash
   export TRITON_DEBUG=1
   python -c "
   import triton, triton.language as tl, torch, torch_npu
   @triton.jit
   def reduce_kernel(x_ptr, y_ptr, N: tl.constexpr, BLOCK: tl.constexpr):
       pid = tl.program_id(0)
       offs = tl.arange(0, BLOCK)
       acc = tl.zeros((BLOCK,), tl.float32)
       # 简化：单 block reduce
       x = tl.load(x_ptr + pid*N + offs)
       acc = tl.sum(x, axis=0)
       tl.store(y_ptr + pid, acc)
   x = torch.randn(1, 1024, device='npu', dtype=torch.float32)
   y = torch.empty(1, device='npu', dtype=torch.float32)
   reduce_kernel[(1,)](x, y, 1024, 1024, compile_mode='simt_only')
   "
   ```

3. 观察终端打印的 `Dumping intermediate results to <cache_dir>` 路径，进入该目录列出文件。

**需要观察的现象 / 预期结果：**

- 目录里应能看到 `kernel.ttir.mlir`（make_ttir 产出）与 `kernel.npubin`（或 `kernel.o`），但**没有** `kernel.ttadapter.mlir`、`kernel.mlirbc`、`kernel.mlir` 这些 Linalg / 字节码中转文件——因为 `add_stages` 提前返回，那些阶段根本没注册。
- 对比：把 `compile_mode='simt_only'` 去掉（改用默认混合模式）再跑一次，这次目录里会多出 `ttadapter.mlir`、`mlirbc` 等文件。

**操作步骤（无设备，纯源码追踪）：**

1. 在 [compiler.py:1271-1275](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L1271-L1275) 确认 `force_simt_only` 时只注册 `{ttir, npubin}` 两个阶段。
2. 推导：core 按阶段字典顺序执行，既然只有这两个阶段，就只会有 `make_ttir` 与 `ttir_to_npubin` 两次产物落盘——Linalg 阶段（`ttir_to_linalg`）根本不会被调用。

**预期结果（源码追踪）：** `compile_mode="simt_only"` ⟹ `force_simt_only=True` ⟹ `add_stages` 注册 `{ttir, npubin}` 并提前 return ⟹ 缓存目录只有 `ttir.mlir` + `npubin`，无任何 Linalg 中间产物。这正是「跳过 linalg 直接生成 npubin」的字面证据。

#### 4.3.5 小练习与答案

**练习 1：** 纯 SIMT 路径为什么 `mix_mode` 固定为 `"aiv"`？

**答案：** 纯 SIMT 不经 Linalg，`_parse_linalg_metadata`（会从 IR 读 `mix_mode`）跑不到；改用 `_parse_ttir_metadata`，它注释说明 TTIR 输入只支持向量 kernel，故硬编码 `mix_mode="aiv"`。运行时据此按 Vector 核数分配物理核。

**练习 2：** `--pure-simt` 和 `--enable-triton-ir-compile` 各起什么作用？能否只用其中一个？

**答案：** `--enable-triton-ir-compile` 让 BiSheng 编译器**直接吃 Triton IR（TTIR）**而非 Linalg IR，这是「跳过 Linalg 主线」在工具链侧的落点；`--pure-simt` 则指示编译器用**纯 SIMT 模式**来 lower 这份 TTIR。两者配合才构成纯 SIMT 路径：前者解决「喂什么 IR」，后者解决「用什么模式编」。单用 `--pure-simt` 而不启用 triton-ir-compile，BiSheng 仍会按常规 Linalg 流程处理，无法实现「TTIR 直达」。

**练习 3：** 一个含 `tl.dot`（矩阵乘）的 kernel 适合用 `simt_only` 吗？

**答案：** 不适合。纯 SIMT 路径 `mix_mode` 固定 `aiv`（纯向量核），不支持 Cube 核矩阵乘；且它面向 reduce / elementwise 这类算子。含 `tl.dot` 的 kernel 应走默认混合模式或 `simd`，让 `tl.dot` lower 到 `linalg.matmul` 命中 Cube 核（见 [u4-l5](u4-l5-triton-to-linalg.md)、[u8-l1](u8-l1-cube-vector-model-and-cv-fusion.md)）。

## 5. 综合实践

把本讲三条线索串起来：**用同一个 reduce kernel，对比三种模式下的「编译阶段产物」与「离散访存 lowering」，体会 SIMT 何时介入、以何种形式介入。**

**任务：**

1. 选定一个含离散访存的 reduce kernel（直接用 `test_reduce_simt.py` 的 `triton_unk_reduce` 最省事，它的 `tl.load(in_ptr0 + x1_numel*y0 + x1, x1_mask, other=0.0)` 在 grid 跨步下属于间接 / 离散访存）。
2. **模式 A（默认混合模式）：** 不传 `compile_mode` 跑一次，`TRITON_DEBUG=1`。
   - 记录缓存目录里的文件清单（应有 `ttir.mlir`、`ttadapter.mlir`、`mlirbc`、`npubin`）。
   - 在 `ttadapter.mlir`（即 `ttir_to_linalg` 产出）里搜索 `indirect_load` 或 `scf.for {DiscreteMemAccess}`，判断离散访存走了 SIMT 模板还是标量循环回退。
3. **模式 B（纯 SIMD）：** 加 `compile_mode='simd'` 跑一次。
   - 对比 `ttadapter.mlir`：离散访存应**只**出现标量循环（`scf.for {DiscreteMemAccess}`），没有 `indirect_load`。
4. **模式 C（纯 SIMT）：** 加 `compile_mode='simt_only'` 跑一次。
   - 对比缓存目录文件清单：应**只有** `ttir.mlir` 与 `npubin`，无 `ttadapter.mlir` / `mlirbc`。
5. 用一张表总结三种模式的「阶段产物」与「离散访存 lowering 形式」。

**预期总结表（待本地验证）：**

| 模式 | 缓存产物 | 离散访存 lowering |
| --- | --- | --- |
| `unstructured_in_simt`（默认） | ttir / ttadapter / mlirbc / npubin | 950+秩≤5：`indirect_load`；否则标量循环 |
| `simd` | ttir / ttadapter / mlirbc / npubin | 标量循环（`scf.for {DiscreteMemAccess}`） |
| `simt_only` | ttir / npubin（无 ttadapter） | 整 kernel 由 BiSheng `--pure-simt` 处理 |

这个综合实践把「阶段注册（add_stages）」「pass 内改写（indirect_load）」「工具链选项（--pure-simt）」三个层面统一起来看，是检验是否真正理解本讲的好方法。

## 6. 本讲小结

- 混合模式下离散访存的 SIMT 快路径分两步：`discrete-mask-access-conversion` 满足条件时只**打 `{route_discrete_mask_to_simt}` 标记**不改 IR；`triton-to-unstructure` 命中闸门后把 `tt.load/store` **改写为 `ascend.indirect_load/store`**，跳过标量循环展开。
- 快路径闸门 = **950 + `force_simt_template` + 秩 ≤ 5**（load/store）；atomic 额外要求**偏移/数值/掩码静态形状**且元素类型非 i8/i16，改写目标是 `hivm.hir.custom "__builtin_indirect_atomic"`。
- 任一条件不满足即**回退标量循环**（与纯 `simd` 一致）；秩 > 5 有专门日志 `falling back to scalar loop path`。
- `indirect_load/store` 与 `indirect_atomic` 都是**编译器内部生成的算子/符号**，不是用户 Python API；用户侧的等价能力是 `index_select`/`index_put`/`gather_out_to_ub`/`scatter_ub_to_out`。
- 纯 SIMT 路径 `simt_only` ⟹ `force_simt_only=True` ⟹ `add_stages` 只注册 `{ttir, npubin}` 提前返回，**跳过 Linalg 主线**；`ttir_to_npubin` 用 `--enable-triton-ir-compile` + `--pure-simt` 把 TTIR 直接喂给 BiSheng，`mix_mode` 固定 `aiv`。
- 纯 SIMT 仅 950 可用，且面向 reduce/elementwise 等无 `tl.dot` 的向量 kernel；含矩阵乘的算子不应走此路径。

## 7. 下一步学习建议

- **运行时侧闭环：** 本讲讲清了编译期，但 `parallel_mode="simt"` 如何传导到 launcher 选 `rtKernelLaunchWithFlagV2`（携带 `localMemorySize`），请读 [u5-l3](u5-l3-kernel-launch-and-resources.md)，把「编译旋钮 → 启动 API」整条链补全（u6-l1 末尾也点了这条线）。
- **离散访存的全景：** 若想从 SIMD 侧理解「为什么离散访存需要特殊处理」，回头读 [u4-l3](u4-l3-discrete-mask-access-conversion.md) 与 [u4-l4](u4-l4-triton-to-unstructure.md)，把本讲的 SIMT 快路径放回「结构化 → 离散掩码 → 非结构化 → SIMT 模板」整条 lowering 链中。
- **调试与复现：** 学 [u10-l1](u10-l1-debugging-methods.md) 的 `TRITON_DEBUG` / `MLIR_ENABLE_DUMP`，掌握如何 dump 出本讲提到的 `ttadapter.mlir`、如何用 `triton-opt --pass-pipeline=` 在命令行复现 pass 行为。
- **二次开发：** 若想新增一类「SIMT 模板算子」，参考 [u10-l5](u10-l5-extending-cpp-pass.md)，研究如何在 `UnstructureConversionPass` 里加一个 converter 并接入 `ttir_to_linalg` 流水线。

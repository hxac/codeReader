# 内存规划与存储重写

## 1. 本讲目标

本讲聚焦 tile-lang 在昇腾（Ascend）上的**两件事**：每个片上 buffer 最终落在物理存储的哪个地址，以及如何让生命期不重叠的 buffer 复用同一块地址以省下宝贵的片上内存。

读完本讲你应当能够：

- 说清 `AscendStorageRewrite` 与 `AscendMemoryPlanning` 两个 pass 的分工，以及它们在两阶段流水线中的先后位置与依赖关系。
- 理解「线性（linear）模式」与「自动（auto）模式」的差别：前者给每个 buffer 顺序分配独立地址、不复用；后者用生命期分析 + 线性扫描（linear scan）让不重叠的 buffer 共享地址。
- 掌握 `TL_ASCEND_MEMORY_PLANNING` 开关的语义，以及 `T.annotate_address` 手写地址与自动规划的替代关系。
- 会用 `get_kernel_source()` 读出每个 buffer 的地址偏移，定量对比开关自动规划后的 UB/L1 占用差异。

本讲依赖 [u6-l1 编译 Pass 全景与配置](u6-l1-pass-overview.md)（两阶段 pass 流水线、`PassConfigKey`）与 [u3-l1 内存层级与分配原语](u3-l1-memory-alloc.md)（shared/fragment 抽象与 TIR scope）。

## 2. 前置知识

### 2.1 为什么需要「地址」

昇腾 AI Core 的片上存储是**一块连续的物理内存**，而不是 GPU 那种「声明一个 shared memory 数组」就自动有独立空间。在 Ascend C（ascendc）后端里，每个 buffer 是用 `TBuf.GetWithOffset<type>(size, offset)` 从某块 `TPosition` 内存里**按字节偏移切出来**的（见 [src/target/codegen_ascend.cc:833-835](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend.cc#L833-L835)）。也就是说，编译器必须为每个 buffer 算出一个**字节偏移地址**，否则生成的代码无法编译。

```cpp
// codegen 生成出来的形态（示意，非逐字）
auto a_ub = ascend_ub.GetWithOffset<half>(8192, 0);     // offset=0
auto b_ub = ascend_ub.GetWithOffset<half>(8192, 8192);  // offset=8192
```

如果两个 buffer 在时间上**不会同时存活**（一个用完了、之后另一个才被写），它们完全可以共用同一段地址——这就是「缓冲复用（buffer reuse）」，本讲的核心主题。

### 2.2 物理存储与容量上限

`AscendMemoryPlanning` 把需要规划地址的 scope 与容量上限写在一张表里（[src/transform/ascend_memory_planning.cc:34-38](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_memory_planning.cc#L34-L38)）：

| TIR scope | 物理存储 | 规划用上限（字节） |
|---|---|---|
| `shared.l1` | L1（Cube 核） | 524032（~512KB） |
| `shared.ub` | Unified Buffer（Vector 核） | 196352（~192KB） |
| `wmma.matrix_a` | L0A | 65536（64KB） |
| `wmma.matrix_b` | L0B | 65536（64KB） |
| `wmma.accumulator` | L0C | 131072（128KB） |

> 这些是规划阶段用的保守常量；codegen 阶段 `InitBuffer` 会按平台代际（A2/A3 vs A5）给实际尺寸（[src/target/codegen_ascend.cc:1016-1035](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend.cc#L1016-L1035)）。两者不必完全相同——规划上限只要不小于实际可用即可。

### 2.3 生命期（liveness）直觉

一个 buffer 的「生命期」从它第一次被写（GEN）开始，到最后一次被读（KILL）结束。两个 buffer 的生命期区间若不重叠，就能共用地址；若重叠（同时存活），地址必须不冲突。这和寄存器分配里的「活跃区间（live interval）」是同一类问题，本讲的自动模式正是用经典的**线性扫描（linear scan）**算法求解。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [src/transform/ascend_storage_rewrite.cc](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_storage_rewrite.cc) | `AscendStorageRewrite`：buffer 的**结构化放置**——决定每个 Allocate 挂到哪个作用域、合并等价分配、inplace 检测。迁移自 TVM 的 `StorageRewrite`。 |
| [src/transform/ascend_memory_planning.cc](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_memory_planning.cc) | `AscendMemoryPlanning`：为每个 buffer 分配**字节地址**，写 `address_map` / `size_map` 属性供 codegen 消费；含 linear 与 auto 两种模式。 |
| [tilelang/engine/phase.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/engine/phase.py) | 两阶段流水线编排；两个 pass 都在 `OptimizeForTarget` 阶段。 |
| [tilelang/transform/pass_config.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/transform/pass_config.py) | `TL_ASCEND_MEMORY_PLANNING` 等配置键定义。 |
| [tilelang/language/\_\_init\_\_.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/__init__.py) | `T.annotate_address`：把 buffer 钉到指定地址的前端原语。 |
| [examples/sparse_flash_attention/example_sparse_flash_attn.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/sparse_flash_attention/example_sparse_flash_attn.py) | 综合案例：开自动规划 + 注释掉的手写地址表。 |
| [src/target/codegen_ascend.cc](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend.cc) | 消费 `address_map`，生成 `GetWithOffset(size, offset)`。 |
| [testing/python/language/test_ascend_memory_planning.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/testing/python/language/test_ascend_memory_planning.py) | 官方测试，断言地址不重叠、复用正确、手写地址被尊重。 |

## 4. 核心概念与源码讲解

### 4.1 两个 pass 的分工与流水线位置

#### 4.1.1 概念说明

读者最容易混淆的是：**为什么要有两个 pass？它们不是都在「分配内存」吗？**

答案是它们解决的是两个**正交**的子问题：

- **`AscendStorageRewrite`（结构化放置）**：决定每个 `Allocate` 语句**挂在 IR 树的哪个位置**（例如挂到 kernel launch / `thread_extent` 作用域）、把逻辑上等价的小分配**合并**成一个、检测 `dst = f(src)` 这类可以**原地（inplace）**完成的操作。它**不**算字节地址，只整理「谁和谁能共用一个 Allocate 节点」。这个 pass 实际是从 TVM 的 `StorageRewrite` 迁移来的（见文件头 `\todo` 注释）。
- **`AscendMemoryPlanning`（地址规划）**：在放置好之后，为每个 buffer **在所属物理存储里分配一个字节偏移**，并写进函数属性 `address_map` 与 `size_map`，供 codegen 的 `GetWithOffset` 使用。它才真正决定「UB 里谁在 0、谁在 8192」。

#### 4.1.2 核心流程

两个 pass 都落在 `OptimizeForTarget` 阶段，且**先后固定**（[tilelang/engine/phase.py:110](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/engine/phase.py#L110) 与 [tilelang/engine/phase.py:117](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/engine/phase.py#L117)）：

```text
OptimizeForTarget 阶段（节选，自上而下）：
  VectorizeLoop
  AscendStorageRewrite(is_npu=True)   ← 4.2：结构化放置（合并/inplace，NPU 下关闭跨 buffer 复用）
  UnrollLoop / Simplify / RemoveNoOp / HoistIfThenElse
  AscendMemoryPlanning()              ← 4.3：算字节地址，写 address_map/size_map
  AscendSyncInsert / AscendSyncInsertVS
```

为什么 `AscendMemoryPlanning` 排得这么靠后？因为它需要看到**最终展开、提升后的 IR**：循环已经 unroll、`if/else` 已经 hoist、`Allocate` 已经被前一个 pass 整理过——此时统计出来的生命期才准确。它紧接着同步插入 pass，因为同步 pass 也要读它的 `address_map`/`size_map` 来判断两段搬运是否落在同一物理地址（即同一片上区域），从而决定要不要插 flag。

最后由 codegen 消费属性（[src/target/codegen_ascend.cc:818-835](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend.cc#L818-L835)），按 buffer 名查 `address_map` 得到 offset，生成 `GetWithOffset(size, offset)`；若查不到则直接 `ICHECK` 失败——**每个 buffer 都必须有地址**。

#### 4.1.3 代码实践

1. **目标**：从生成的 C++ 源码里亲眼看到「地址」长什么样。
2. **步骤**：
   - 用 `tilelang.compile(...)` 编译任一 ascendc kernel，调用 `kernel.get_kernel_source()`。
   - 在源码里搜索 `GetWithOffset`，记录每个 buffer 的 `(size, offset)`。
3. **观察**：你会看到形如 `auto acc_o = ascend_ub.GetWithOffset<float>(..., 0);` 与 `auto sumexp = ascend_ub.GetWithOffset<float>(..., 65536);` 的成对出现。
4. **预期结果**：同一 scope（如 `ascend_ub`）下，地址按规划结果分布；不同 scope（`ascend_l1` / `ascend_l0c` / `ascend_ub`）各自从 0 开始独立编址。
5. 若手头没有 NPU，`get_kernel_source()` 仍可在仅做 codegen 的路径上工作，地址信息不依赖真实硬件（待本地验证运行路径）。

#### 4.1.4 小练习与答案

**练习 1**：把 `AscendMemoryPlanning` 从流水线里去掉（例如临时注释 [phase.py:117](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/engine/phase.py#L117)），编译任意带 UB buffer 的 kernel，会发生什么？

> **答案**：codegen 在 [src/target/codegen_ascend.cc:827-831](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend.cc#L827-L831) 处 `ICHECK(found_by_name)` 失败，报错 "Cannot find pre-allocated address for buffer: ... All buffers must be pre-allocated via address_map_." 说明没有地址规划，codegen 无法产出可编译代码。

### 4.2 AscendStorageRewrite：buffer 的结构化放置

#### 4.2.1 概念说明

`AscendStorageRewrite` 解决的是「**Allocate 节点该怎么摆**」。它源自 TVM 的 `StorageRewrite`（文件头 `\todo` 注明「migrated from TVM commit c2921fd」），核心能力有三个：

1. **生命期分析 + 复用判定**：用 `LinearAccessPatternFinder` 把 IR 线性化，找出每个 buffer 的 GEN/KILL 点（[src/transform/ascend_storage_rewrite.cc:876-902](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend.cc#L876-L902)），判定哪些 buffer 能共用一个 `Allocate`。
2. **inplace 检测**：对 `dst[index] = f(src[index])` 这种逐元素写，若 `src` 此后不再被读、且 dtype/shape 匹配，就让 `dst` 直接复用 `src` 的存储（[src/transform/ascend_storage_rewrite.cc:309-421](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_storage_rewrite.cc#L309-L421) 的 `InplaceOpVerifier`）。
3. **挂载点（attach_scope）与合并**：把分配挂到正确的作用域（`thread_extent` / kernel 入口），并把多个小分配合并成一个大的（`PrepareNewAlloc`）。

#### 4.2.2 核心流程

关键在 pass 入口对 `is_npu` 的处理（[src/transform/ascend_storage_rewrite.cc:1935-1938](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_storage_rewrite.cc#L1935-L1938)）：

```cpp
if (is_npu) {
  // For NPU target, we disable the smem reuse to avoid potential issues.
  enable_reuse = false;
}
```

也就是说，**在 NPU 上 `AscendStorageRewrite` 关闭了跨 buffer 复用**，把复用职责完全交给后面的 `AscendMemoryPlanning`。它仍然做 inplace 检测、挂载点规划与类型重写（`PointerValueTypeRewrite`），但不再自己合并 buffer 地址。

`FindAlloc` 是复用判定的核心（[src/transform/ascend_storage_rewrite.cc:1035-1121](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_storage_rewrite.cc#L1035-L1121)）：当 `enable_reuse=false` 时直接走 `NewAlloc`（[L1063-1065](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_storage_rewrite.cc#L1063-L1065)），每个 buffer 独立建条目。inplace 检测在 `PlanMemory` 里先于 `FindAlloc` 触发（[L945-978](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_storage_rewrite.cc#L945-L978)）。

#### 4.2.3 源码精读

线性化访问序列是后续所有分析的基础。`LinearAccessPatternFinder` 把每个复合作用域（For/IfThenElse/kernel launch）表示成「before_scope … after_scope」两个点，buffer 的访问记在 after_scope 上（[src/transform/ascend_storage_rewrite.cc:109-281](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_storage_rewrite.cc#L109-L281)）。这段注释（[L283-307](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_storage_rewrite.cc#L283-L307)）解释了它要找的「同一个作用域内的最后访问点」。

inplace 的安全性由 `InplaceOpVerifier` 保证：它拒绝从 `dst` 读（无 reduction）、拒绝嵌套间接寻址 `A[B[i]]`、拒绝 extern scope（[src/transform/ascend_storage_rewrite.cc:377-407](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_storage_rewrite.cc#L377-L407)）。这把「原地操作」限制在安全的逐元素形态。

> 注意：NPU 下「关闭 smem 复用」是一条**保守**策略——把地址复用完全交给 `AscendMemoryPlanning`，避免两个 pass 各算一套地址互相冲突。这也是为什么本讲的「复用」几乎都指 `AscendMemoryPlanning`。

#### 4.2.4 代码实践

1. **目标**：观察 inplace 检测的效果。
2. **步骤**：写一个 `c = a + b` 的逐元素 kernel（参考 [examples/elementwise/elementwise_add.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/elementwise/elementwise_add.py) 的 `T.tile.add(c_ub, a_ub, b_ub)`），编译后看生成的 `Allocate` 数量。
3. **观察**：对比「a/b/c 三个独立 UB buffer」与「inplace 后 c 复用其中之一」时，源码里 `GetWithOffset` 出现的次数与地址。
4. **预期结果**：NPU 下因 `enable_reuse=false`，inplace 仍可能触发但跨 buffer 复用被抑制；地址最终由 `AscendMemoryPlanning` 决定（待本地验证具体合并数目）。

#### 4.2.5 小练习与答案

**练习 2**：为什么 NPU 上要显式 `enable_reuse = false`，而不是让 `AscendStorageRewrite` 自己完成复用？

> **答案**：`AscendStorageRewrite` 来自 TVM，它的复用结果是「让多个 buffer 共用一个 `Allocate` 节点」，并不会给出字节级 offset；而昇腾 codegen 需要的是每个 buffer 在物理存储里的**精确字节地址**（`GetWithOffset`）。把地址计算交给专门的 `AscendMemoryPlanning`（它能做线性扫描、loop-aware 生命期扩展），职责更清晰、也避免了两个 pass 对同一片存储各算一套冲突的地址。

### 4.3 AscendMemoryPlanning：生命期分析与线性扫描复用

这是本讲的重头戏。

#### 4.3.1 概念说明

`AscendMemoryPlanning` 给每个 buffer 算一个字节地址，有**两种模式**，由开关 `TL_ASCEND_MEMORY_PLANNING` 切换：

- **linear 模式（默认，开关 `False`）**：每个 buffer 顺序递增地分一个独立地址，**不做复用**。实现简单、确定，但在 UB buffer 很多时可能浪费空间。
- **auto 模式（开关 `True`）**：先做**生命期分析**求出每个 buffer 的活跃区间 `[start, end]`，再用**线性扫描分配器（LinearScanAllocator）**让区间不重叠的 buffer 共享地址。等价于寄存器分配里的 linear scan register allocation。

无论哪种模式，产出的地址都写进函数属性 `address_map`（buffer→offset）与 `size_map`（buffer→字节数），见 [src/transform/ascend_memory_planning.cc:81-99](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_memory_planning.cc#L81-L99)。

#### 4.3.2 核心流程

入口 `Substitute` 先读两个外部输入（[src/transform/ascend_memory_planning.cc:57-100](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_memory_planning.cc#L57-L100)）：

- `address_map` 属性：来自前端 `T.annotate_address`，即**用户手写钉死的地址**（pre-alloc），两种模式都必须尊重。
- `logic_buffer_shapes` 属性（`kLogicBufferShapes`，见 [src/transform/common/attr.h:29](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/common/attr.h#L29)）：由更早的 `BufferShapeCollector`/`CollectBufferShapes` pass 写入，给 PTO 的 4D tile `[physical_M, physical_N, valid_M, valid_N]` 提供物理 footprint 来算尺寸（[L920-941](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_memory_planning.cc#L920-L941) 的 `CalculateBufferSize`）。

随后按 scope 分组，分别规划（[src/transform/ascend_memory_planning.cc:302-324](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_memory_planning.cc#L302-L324)）：

```text
PlanMemory():
  LivenessAnalysis()      # 求 GEN/KILL
  CollectLoopInfo()       # 收集每个循环的 [begin,end] 与每个 buffer 的访问点
  按 scope 分组 buffers
  for 每个 scope:
    if auto:  PlanMemoryForScope()       # 线性扫描复用
    else:     PlanMemoryForScopeLinear() # 顺序分配不复用
```

**auto 模式的分配算法**（`PlanMemoryForScope`，[L543-593](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_memory_planning.cc#L543-L593)）：

1. 为每个 buffer 求活跃区间 `[start, end]`，其中 `end` 经 `ExtendKillIndex` 做循环感知扩展（见 4.3.3）。
2. 区间按 `start` 排序，逐个喂给 `LinearScanAllocator`。

**LinearScanAllocator**（[L666-918](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_memory_planning.cc#L666-L918)）的循环逻辑（经典 linear scan）：

```text
active_queue  ← 按 end 升序的优先队列（当前在用的区间）
free_blocks   ← 已释放可复用的空闲块
next_new_offset ← 高水位线（从未复用区段分到的最大 offset）

for 每个区间 iv (按 start 升序):
    把 active_queue 里 end < iv.start 的区间全部出队 → 它们的块加入 free_blocks
    合并相邻 free_blocks
    if iv 是 pre-alloc (T.annotate_address):     # 钉死地址
        offset = AlignUp(pre_addr, 32); 冲突检测
    else:                                         # 普通分配
        优先在高水位线 next_new_offset 上新分配（若不超上限、不与 pre-alloc 冲突）
        否则在 free_blocks 里找一个能放下的复用块（findReusableBlock）
        都不行 → LOG(FATAL) 内存不足
    把 (iv, offset) 入 active_queue
```

所有地址按 32 字节对齐（`AlignUp(..., 32)`，[L51-53](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_memory_planning.cc#L51-L53)）。`is_reused` 标记区分「新分」与「复用」，最后日志会打印复用率（[L787-795](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_memory_planning.cc#L787-L795)）。

> **关键正确性点**：地址复用的**唯一**判据是生命期区间不重叠。所以生命期分析必须保守且正确——尤其是循环与分支，见 4.3.3。

#### 4.3.3 源码精读：循环感知的生命期扩展

这是本 pass 最微妙、也最常踩坑的地方。

朴素的生命期分析在**线性化 IR** 上做反向扫描求 KILL（[src/transform/ascend_memory_planning.cc:326-367](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_memory_planning.cc#L326-L367)）。但循环体在 IR 里只展开成「一趟」，于是「循环体内最后一次访问」会被误当成整个 KILL 点。考虑：

```python
index_ub = T.alloc_ub(...)        # 循环外定义
T.copy(A[0,:], index_ub)
for k in T.serial(3):
    merged_ub = T.alloc_ub(...)   # 循环内定义
    T.copy(A[k,:], merged_ub)
    T.tile.add(merged_ub, merged_ub, index_ub)   # 每轮都读 index_ub
```

`index_ub` 在循环外定义、循环内每轮都读——它**在整个循环期间都活着**。若不修正，分析会以为 `index_ub` 在循环体内的某次访问后就死了，把它的地址分给 `merged_ub`，下一轮 `index_ub` 就被覆盖，导致**静默数据错误**。

`ExtendKillIndex`（[src/transform/ascend_memory_planning.cc:516-541](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_memory_planning.cc#L516-L541)）修复这点：对每个满足 `GEN < loop_begin ≤ KILL < loop_end` 的 buffer，把它的 KILL 扩展到 `loop_end`，并对所有外层循环取最大值。它的注释（[L493-515](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_memory_planning.cc#L493-L515)）举的就是上面 `index_ub`/`merged_ub` 的例子。循环区间本身由 `CollectLoopInfo`（[L474-491](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_memory_planning.cc#L474-L491)）预先算好。

此外还有一处**分支正确性**：`if/else` 两个分支各自分配的 buffer，运行时只走其一，但静态分析必须保证它们的地址互不重叠、也不与外层存活 buffer 重叠——否则选中的分支会踩到另一分支的残影。官方测试 [testing/python/language/test_ascend_memory_planning.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/testing/python/language/test_ascend_memory_planning.py) 的 `test_if_else_branch_buffers_no_overlap` 等用例专门守护这点。

**linear 模式**（`PlanMemoryForScopeLinear`，[L595-647](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_memory_planning.cc#L595-L647)）则简单得多：对 L1/L0A/L0B/L0C 顺序累加 `current_offset`；对 `shared.ub`，把所有 UB buffer（经 `SetTmpBuffers` 收集，[L292-300](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_memory_planning.cc#L292-L300)）按声明顺序排在已钉死地址之后，每个都独立、不复用。

#### 4.3.4 代码实践

1. **目标**：直观看到「循环内 buffer 复用同一地址」与「跨轮存活 buffer 不被复用」。
2. **步骤**：参考官方测试 `test_loop_buffer_before_loop_not_reused_inside`（[testing/python/language/test_ascend_memory_planning.py:165-191](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/testing/python/language/test_ascend_memory_planning.py#L165-L191)），用如下最小 kernel 编译并取地址：

   ```python
   # 示例代码（仿官方测试，非项目原有文件）
   pass_configs = {tilelang.PassConfigKey.TL_ASCEND_MEMORY_PLANNING: True}
   @T.prim_func
   def main(A: T.Tensor((4,64),"float32"), B: T.Tensor((4,64),"float32")):
       with T.Kernel(1, is_npu=True) as (cid, vid):
           index_ub = T.alloc_ub((64,), "float32")
           T.copy(A[0, :], index_ub)
           for k in T.serial(3):
               merged_ub = T.alloc_ub((64,), "float32")
               T.copy(A[k, :], merged_ub)
               T.tile.add(merged_ub, merged_ub, index_ub)
               T.copy(merged_ub, B[k, :])
   ```
   
   用正则 `auto\s+(\w+)\s*=.*GetWithOffset<[^>]+>\(\s*(\d+)\s*,\s*(\d+)\s*\)` 从 `get_kernel_source()` 里抓 `(name, size, offset)`。
3. **观察**：`merged_ub` 的 offset 应与 `index_ub` **不重叠**（因为 `index_ub` 跨轮存活）；而若把 `index_ub` 的使用移到循环外，`merged_ub` 就能在不同轮次复用同一地址。
4. **预期结果**：满足 `index_ub.offset + 256 <= merged_ub.offset` 或反之；关掉 `TL_ASCEND_MEMORY_PLANNING`（linear 模式）时同样不重叠，但地址是顺序累加、不复用。运行结果待本地验证（需 ascendc codegen 可用）。

#### 4.3.5 小练习与答案

**练习 3**：auto 模式下，下面两段代码哪段更省 UB？为什么？

```python
# (a) 三个 buffer 串联使用，互不重叠
x = alloc_ub(...); fill(x); y = alloc_ub(...); y = f(x); z = alloc_ub(...); z = g(y)
# (b) 三个 buffer 同时存活
x = alloc_ub(...); y = alloc_ub(...); z = alloc_ub(...); z = f(x, y)
```

> **答案**：(a) 更省。(a) 中 x 用完才定义 y、y 用完才定义 z，生命期两两不重叠，线性扫描会让三者复用同一地址，UB 占用 ≈ 1 份。(b) 中三者同时存活，生命期完全重叠，必须各占独立地址，UB 占用 ≈ 3 份。这正是 softmax/attention 里「先 reduce_max 再 exp 再 reduce_sum」如果不拆临时缓冲会很吃 UB 的原因（参见 [u3-l5](u3-l5-parallel.md) 提到的「复杂表达式拆临时缓冲」）。

**练习 4**：`LinearScanAllocator` 优先在 `next_new_offset`（高水位线）上分配，只有放不下才去找复用块。这种「优先新分」策略有什么代价？

> **答案**：它倾向于把地址往上顶，可能让总占用偏高；好处是分配快、且复用块只在「新高水位放不下」时才启用，避免频繁碎片化。配合 `mergeFreeBlocks`（[L822-846](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_memory_planning.cc#L822-L846)）合并相邻空闲块、`findReusableBlock`（[L848-883](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_memory_planning.cc#L848-L883)）尽量塞进洞，整体是「先保证正确与速度、再压占用」的工程取舍。

### 4.4 TL_ASCEND_MEMORY_PLANNING 开关与 T.annotate_address

#### 4.4.1 概念说明

开关定义在 [tilelang/transform/pass_config.py:44-45](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/transform/pass_config.py#L44-L45)，默认 `False`（即 linear 模式）。它在 C++ 侧注册为 `TVM_REGISTER_PASS_CONFIG_OPTION("tl.ascend_memory_planning", Bool)`（[src/transform/ascend_memory_planning.cc:46-49](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_memory_planning.cc#L46-L49)），由 `PassContext` 读取（[L58-59](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_memory_planning.cc#L58-L59)）。

`T.annotate_address({buf: addr, ...})` 是前端原语（[tilelang/language/\_\_init\_\_.py:226-228](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/__init__.py#L226-L228)），它把 `{buffer.data: addr}` 作为块属性 `address_map` 贴到 kernel block 上，最终被 `AscendMemoryPlanning` 当作 pre-alloc 读入（[L64-68](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_memory_planning.cc#L64-L68)）。

两者的**替代关系**是本节核心：

- **不开自动规划（linear）+ 手写 `T.annotate_address`**：用户手动为每个 UB buffer 指定地址，靠人脑安排复用。这就是早期 sparse FlashAttention 的写法（[example_sparse_flash_attn.py:119-147](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/sparse_flash_attention/example_sparse_flash_attn.py#L119-L147) 里被注释掉的那张大表）。
- **开自动规划（auto）+ 删掉 `T.annotate_address`**：编译器自动算复用，手写表「不再需要」（README 原话，[README.md:386-398](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/README.md#L386-L398)）。

`T.annotate_address` 在 auto 模式下依然有效：它把指定 buffer **钉死**在指定地址，分配器会绕开它（冲突检测在 [L733-743](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_memory_planning.cc#L733-L743) 与 `CheckConflict` [L801-820](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_memory_planning.cc#L801-L820)）。所以两者可混用：关键 buffer 钉死，其余交给自动规划。

#### 4.4.2 核心流程

以 [examples/sparse_flash_attention/example_sparse_flash_attn.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/sparse_flash_attention/example_sparse_flash_attn.py) 为例，它把四个相关开关一起开（[L10-15](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/sparse_flash_attention/example_sparse_flash_attn.py#L10-L15)）：

```python
pass_configs = {
    tilelang.PassConfigKey.TL_ASCEND_AUTO_CV_COMBINE: True,
    tilelang.PassConfigKey.TL_ASCEND_AUTO_CV_SYNC: True,
    tilelang.PassConfigKey.TL_ASCEND_AUTO_SYNC: True,
    tilelang.PassConfigKey.TL_ASCEND_MEMORY_PLANNING: True,
}
```

随后 kernel 体里**不再出现** `T.annotate_address`（[L119-147](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/sparse_flash_attention/example_sparse_flash_attn.py#L119-L147) 整段被注释）。这十几个 UB buffer（`acc_o`/`sumexp`/`m_i`/`acc_s_ub`/`acc_s_half`/`acc_o_ub`/`acc_o_half` 等）的地址全部由 `AscendMemoryPlanning` 自动复用安排。

#### 4.4.3 源码精读

pre-alloc 的读入与冲突检测：

- 读入：`SetPreAllocBuffer` 把 `address_map` 里每条 `{name: offset}` 存进 `pre_alloc_buffer_`（[src/transform/ascend_memory_planning.cc:280-290](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_memory_planning.cc#L280-L290)），重名直接 `LOG(FATAL)`。
- auto 模式尊重：`PlanMemoryForScope` 先把 pre-alloc buffer 收进 `pre_alloc_scope_buffer` 传给分配器（[L548-552](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_memory_planning.cc#L548-L552)），分配器在 [L728-753](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_memory_planning.cc#L728-L753) 命中 pre-alloc 时直接用其地址，并更新高水位线。
- 冲突保护：`CheckConflict`（[L801-820](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_memory_planning.cc#L801-L820)）确保任何普通 buffer 不会落到某个 pre-alloc buffer 的地址区间上。

> 还有一个相关但独立的键 `tir.merge_static_smem`（[pass_config.py:84-85](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/transform/pass_config.py#L84-L85)），它触发 `AscendStorageRewrite` 走另一条 `MergeSharedMemoryAllocations` 合并路径（[ascend_storage_rewrite.cc:1917-1926](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_storage_rewrite.cc#L1917-L1926)）。注意它管的是「Allocate 节点的合并」，与 `TL_ASCEND_MEMORY_PLANNING` 管的「字节地址复用」是两件事，不要混用。

#### 4.4.4 代码实践

1. **目标**：验证 `T.annotate_address` 在 auto 模式下被尊重，且其余 buffer 自动避开它。
2. **步骤**：参考官方测试 `test_annotate_address_no_conflict_in_auto_mode`（[testing/python/language/test_ascend_memory_planning.py:276-298](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/testing/python/language/test_ascend_memory_planning.py#L276-L298)），写两个 64×128 float32 的 UB buffer，用 `T.annotate_address({a_ub: 0})` 把 `a_ub` 钉到 0，开 `TL_ASCEND_MEMORY_PLANNING: True` 编译。
3. **观察**：从 `get_kernel_source()` 抓 offset。
4. **预期结果**：`a_ub` 的 offset == 0；`b_ub` 的 offset ≥ `64*128*4 = 32768`（即自动避开了 `a_ub` 的区间）。这正是「手写与自动可混用」的证据。

#### 4.4.5 小练习与答案

**练习 5**：什么时候你仍然会手动写 `T.annotate_address`，而不是无脑开自动规划？

> **答案**：三种典型场景——(1) 你要和某个外部约定对齐地址（如与另一个 kernel 共享 workspace 的固定布局）；(2) 你对某块 buffer 有性能/对齐上的特殊要求，想强制钉到某段；(3) 自动规划在极少数复杂控制流下给出了非最优布局，你想手工覆盖关键 buffer。其余情况开 `TL_ASCEND_MEMORY_PLANNING: True` 即可，维护成本远低于手写一整张地址表。

## 5. 综合实践

**任务**：以 [examples/sparse_flash_attention/example_sparse_flash_attn.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/sparse_flash_attention/example_sparse_flash_attn.py) 为对象，定量对比「开/关 `TL_ASCEND_MEMORY_PLANNING`」两种配置下的 UB 地址分配，量化自动规划节省了多少空间。

**步骤**：

1. 复制该示例为两份，分别用下面两套配置（其余开关保持原样）：
   - 配置 A（auto）：`TL_ASCEND_MEMORY_PLANNING: True`，**不写** `T.annotate_address`。
   - 配置 B（linear）：`TL_ASCEND_MEMORY_PLANNING: False`，并**取消注释** [L119-147](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/sparse_flash_attention/example_sparse_flash_attn.py#L119-L147) 的手写地址表（注意：linear 模式下未在表里的 UB buffer 仍会被 `SetTmpBuffers` 顺序分配，但布局会与手写表不一致）。
2. 对两份分别 `tilelang.compile(..., target="ascendc")`，取 `get_kernel_source()`。
3. 写一个解析函数（直接借用官方测试里的正则，[test_ascend_memory_planning.py:55-74](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/testing/python/language/test_ascend_memory_planning.py#L55-L74)），从源码抓出所有 UB buffer 的 `(name, size, offset)`：
   ```python
   # 示例代码
   import re
   def get_ub_offsets(src):
       out = {}
       for line in src.split("\n"):
           m = re.search(r"auto\s+(\w+)\s*=.*ascend_ub\.GetWithOffset<[^>]+>\(\s*(\d+)\s*,\s*(\d+)\s*\)", line)
           if m: out[m.group(1)] = (int(m.group(2)), int(m.group(3)))  # (size, offset)
       return out
   ```
4. 计算两份各自的 UB 总占用 = `max(offset + size for ...)`，以及「Σsize」（不复用时的累加）。

**需要观察与记录**：

- auto 模式下，多个生命期不重叠的 buffer（如 `acc_s_ub` 与 `acc_o_ub` 这类不同阶段才用的）应落在**相同或相近**的 offset（被复用）。
- linear 模式下，offset 单调递增、基本不复用。
- 两个数字的差值 = 自动规划省下的 UB 字节数；对照 `shared.ub` 上限 196352 字节，算出节省百分比。

**预期结果**：auto 模式的 UB 占用显著低于 linear 模式；这正是 README 说「手写地址表不再需要」的量化依据。若手头无 NPU，仅做 codegen 取源码即可完成地址对比（运行 kernel 验证正确性需真实硬件，待本地验证）。

**进阶**：把同样的对比用到 L1（抓 `ascend_l1.GetWithOffset`）与 L0C（`ascend_l0c.GetWithOffset`），观察 Cube 侧存储的复用情况。

## 6. 本讲小结

- 片上存储地址由**两个 pass** 协作产出：`AscendStorageRewrite` 做 buffer 的结构化放置（合并、inplace、挂载点），`AscendMemoryPlanning` 做字节地址分配与复用；NPU 下前者关闭自己的跨 buffer 复用，把地址职责完全让给后者。
- 两者都排在 `OptimizeForTarget` 阶段，`AscendStorageRewrite` 在前（[phase.py:110](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/engine/phase.py#L110)）、`AscendMemoryPlanning` 在后（[phase.py:117](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/engine/phase.py#L117)），后者紧贴同步插入 pass。
- `AscendMemoryPlanning` 有两模式：**linear**（默认）顺序分配不复用；**auto**（`TL_ASCEND_MEMORY_PLANNING: True`）用生命期分析 + 线性扫描让不重叠 buffer 共享地址。
- 正确性命门是**生命期**：循环外定义、循环内每轮读的 buffer，其 KILL 必须经 `ExtendKillIndex` 扩展到循环末尾，否则会被循环内 buffer 覆盖，造成静默数据错误；`if/else` 分支 buffer 也必须互不重叠。
- 产出的 `address_map`/`size_map` 被 codegen 消费成 `GetWithOffset(size, offset)`，每个 buffer 都必须有地址，否则 codegen `ICHECK` 失败。
- `T.annotate_address` 把 buffer 钉到固定地址（pre-alloc），两种模式都尊重并自动避让；开自动规划后，sparse FlashAttention 里那张手写地址表可整体删除。

## 7. 下一步学习建议

- **[u6-l6 Tile Op lowering 与 Tail Mask](u6-l6-lower-tile-tailmask.md)**：同属 `OptimizeForTarget` 后段的合法性 pass，与本讲共同构成 codegen 前的最后一道准备。
- **[u4-l3 自动同步插入](u4-l3-auto-sync.md)**：`AscendSyncInsert` 紧跟在本讲之后执行，且会读 `address_map`/`size_map` 来判断两段搬运是否落在同一物理地址，进而决定是否插 flag——两者是直接的数据依赖。
- **动手方向**：挑一个自己的算子，先用 linear 模式跑通，再开 `TL_ASCEND_MEMORY_PLANNING` 对比 UB 占用；如果遇到「奇怪的计算结果」，优先怀疑生命期分析（尤其是循环外 buffer 被循环内 buffer 覆盖），可用本讲的 offset 抓取脚本定位。

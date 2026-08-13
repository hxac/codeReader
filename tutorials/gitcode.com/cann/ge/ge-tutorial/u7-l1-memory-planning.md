# 内存规划与复用

## 1. 本讲目标

本讲聚焦 GE 编译器在「静态 shape 图」中以**整图视角**规划与复用张量内存的机制。学完后你应该能够：

- 说清 GE 为什么在**编译期**就把整张图的设备内存一次性规划好，这与运行时动态分配有何不同；
- 画出 `MemoryBlock`（内存块）、`BlockMemAssigner`（块分配器）、`HybridMemAssigner`（混合分配器）三层抽象的职责关系，并指出 `OpMemoryType`、`MemoryNoReuseScope` 等枚举现在的引用方式；
- 用「生命期区间不相交」这一条核心判据，解释 GE 如何判定两个张量能共享同一块内存；
- 描述 `HybridMemAssigner` 用「多策略并行 + 取最小」压低峰值内存的优化思路。

本讲建立在 u4-l5（构建阶段 GraphBuilder）与 u6-l2（v1 执行器 DavinciModel）之上：构建阶段产出内存排布，v1 执行器在设备侧按这套排布加载张量。内存规划正是连接这两者的关键一步。

## 2. 前置知识

在进入源码前，先用三个直觉建立认知。

**直觉一：静态图 vs 动态图，决定内存由谁管。** 在 u2-l1 我们见过 `ComputeGraph` 有一个判别开关 `GetGraphUnknownFlag()`：静态 shape 图（unknown flag = false）在编译期所有张量大小都已知，于是 GE 选择在**编译期**就把每个张量在设备内存里的偏移量算好、写进 OM；运行时只需一次 `rtMalloc` 拿到一大块，张量按偏移各就各位。动态 shape 图则反之，张量大小运行时才确定，只能用运行时分配器（见本讲 4.4 末尾的对比）按需申请。本讲讲的是**前者**——编译期静态内存规划。

**直觉二：内存复用 = 让「生命期不重叠」的张量共用一块地址。** 一个张量从被算子写出到最后一个消费者读完，这段时间叫它的**生命期（life time）**。如果张量 A 在第 1~2 步活跃、张量 B 在第 3~4 步活跃，两者生命期不重叠，就没必要各占一块内存，可以让 B 复用 A 释放出来的那块。这是整张图内存复用的唯一核心思想。

**直觉三：「块（block）」是复用的单位，不是单个张量。** GE 不是逐个张量地判断复用，而是先把若干生命期相邻、同流的张量打包进一个 `MemoryBlock`，再以 block 为单位在可复用池里寻找匹配。这样能减少碎片、提高复用率。

> 名词速查：**生命期（life time）** 用节点拓扑序号度量，`life_time_begin_` / `life_time_end_` 是张量活跃区间的起止拓扑步；**stream（流）** 是设备侧的执行队列（u7-l3 详述），同流的张量按序执行，复用判定最简单。

## 3. 本讲源码地图

| 文件 / 目录 | 作用 |
|------|------|
| [compiler/graph/build/memory/](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/compiler/graph/build/memory) | 编译期静态内存规划的**全部实现**所在目录 |
| `memory_block.h` / `.cc` | 定义 `MemoryBlock`（内存块）、`NodeTypeIndex`（块内张量条目）、以及复用判定函数 `CanReuseBlock` / `ReuseBlock` / `CanBlockLifeReuse` |
| `block_mem_assigner.h` / `.cc` | `BlockMemAssigner`：块分配器核心，主循环 `AssignMemoryWithReuse`、申请 `ApplyMemory`、释放 `ReleaseMemory` |
| `hybrid_mem_assigner.cc` | `HybridMemAssigner`：混合分配器，多策略并行执行后取内存最小者（峰值优化） |
| `mem_assigner.h` | 对齐常量 `MEM_ALIGN_SIZE = 512`、分配器抽象基类 |
| `block_type_list.h` | `BlockTypeList`：用「内存属性」做块间冲突快速判定 |
| `continuous_mem.h` / `.cc` | 连续内存（HCOM、连续输入）的特殊规划 |
| `dynamic_batch_mem_assigner.h` | 动态多 batch 场景的复用约束（`kMaxSplitSizeForDynamicBatch = 400MB`） |
| `docs/zh/design/constraints/memory-constraints.md` | 内存约束的官方说明，列出了所有「不能复用」的特殊场景 |
| `runtime/v1/graph/manager/active_memory_allocator.h` | 运行时动态分配器（动态 shape 用），与本讲静态规划对照 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：**4.1 内存规划**（编译期整图规划的总流程）、**4.2 内存块与分配器抽象**（数据结构与类层次）、**4.3 复用判定**（两个张量何时能共享一块内存——直接回答本讲实践任务）、**4.4 峰值内存优化**（多策略取最小 + 对齐 + 特殊场景）。

### 4.1 内存规划：编译期的整图静态规划

#### 4.1.1 概念说明

「内存规划（memory planning / assign）」指的是：给定一张静态 shape 的 `ComputeGraph`，为图里**每一个张量**（算子输出、workspace）算出一个设备内存偏移量，使得整图占用的设备内存尽量小。这是图编译四阶段中「构建阶段（Build）」的一个子任务（u4-l5）。

为什么要在编译期做？因为静态图下所有 shape 编译期已知，GE 可以做一件运行时做不到的事——**站在整张图的视角**，看清所有张量的生命期，把它们像拼积木一样塞进尽可能少、尽可能紧凑的内存块里。规划结果（每个张量的偏移）序列化进 OM，运行时 v1 执行器（u6-l2）加载 OM 后直接用这套偏移，不必再为每个张量单独申请内存。

与之对照，动态 shape 图运行时才知道大小，只能用运行时分配器 `ActiveMemoryAllocator` 按需 `rtMalloc`/`rtFree`（见 4.4.3）。

#### 4.1.2 核心流程

整图静态内存规划由 `HybridMemAssigner::Assign()` 驱动，它内部再委托给一个或多个 `BlockMemAssigner`。流程如下：

1. **求 ref 映射**：先算出图里哪些输出锚点指向「同一个符号」（引用关系），保证被引用的张量必须落在同一块内存。
2. **Inplace 处理**：标记可就地（inplace）复用的张量。
3. **预处理** `BlockMemAssigner::PreparationForAssign`：为每个算子补全 stream id、生命期等规划所需的元信息。
4. **主分配循环** `AssignMemoryWithReuse`：按拓扑序遍历所有节点，逐个为输出与 workspace 申请内存（能复用就复用，不能就新建块），并在节点消费完输入后把输入块放回可复用池。
5. **二次复用** `ReuseBlocksByLifeTime`：循环结束后再做一轮基于生命期的跨块复用，进一步压低块数。
6. **连续块与 resize**：处理连续内存，对每个块按其实际最大占用 resize，算出最终的 head/tail 偏移。

#### 4.1.3 源码精读

主分配循环在 [block_mem_assigner.cc:L2598](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/compiler/graph/build/memory/block_mem_assigner.cc#L2598)，先读全局复用开关，再遍历所有节点：

[compiler/graph/build/memory/block_mem_assigner.cc:L2598-L2636](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/compiler/graph/build/memory/block_mem_assigner.cc#L2598-L2636) —— 读 `OPTION_EXEC_DISABLE_REUSED_MEMORY` 决定全局是否复用（`is_ge_reuse_mem_`），随后对每个节点依次「分配输出内存 → 分配 workspace 内存 → 释放本节点已消费完的输入块」。

关键三点：

- 全局复用开关由环境/GE 选项 `ge.exec.disableReuseMemory` 控制，设为 `"1"` 时 `is_ge_reuse_mem_ = false`，关掉所有复用（[L2604](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/compiler/graph/build/memory/block_mem_assigner.cc#L2604)）。
- 主循环**只读不改图**：依据 [memory-constraints.md](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/docs/zh/design/constraints/memory-constraints.md) 的约束 1，`AssignMemoryWithReuse` 及其触发的所有函数禁止对 `ComputeGraph` 增删改属性，只允许读 `OpDesc`——因为这一步会与其它分配策略**多线程并发**跑（见 4.4），改图会引发数据竞争。
- 遍历结束后调用 `ReuseBlocksByLifeTime()`（[L2645](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/compiler/graph/build/memory/block_mem_assigner.cc#L2645)）做一轮生命期复用，再 `ResizeMemoryBlocks` 算最终偏移。

#### 4.1.4 代码实践

**实践目标**：跟踪「主循环每跑一个节点，可复用池如何涨落」。

1. 打开 [block_mem_assigner.cc:L2627-L2636](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/compiler/graph/build/memory/block_mem_assigner.cc#L2627-L2636)，确认对每个节点调用了 `AssignOutputMemoryWithReuse`、`AssignWorkSpaceMemoryWithReuse`、`ReleaseInputNodeOutMemory` 三步。
2. 在 `ReleaseInputNodeOutMemory`（[L2222](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/compiler/graph/build/memory/block_mem_assigner.cc#L2222)）内找到 `ReleaseMemory(block, reusable_memory[...], ...)` 这一行——它把「当前节点已读完的上游输出块」放回可复用池 `reusable_blocks_`。
3. **待本地验证**：若你想观察真实涨落，可在 `AssignMemoryWithReuse` 主循环里临时给每个节点加一行 `GELOGI`，打印此时 `reusable_blocks_` 里该 stream 下的块数（仅阅读、不改业务逻辑），再用一个含约 10 个算子的小图编译，对照日志画出「块数随拓扑步变化」的曲线。

#### 4.1.5 小练习与答案

**练习 1**：为什么主循环必须「只读不改图」？
**答**：因为 `HybridMemAssigner` 会同时跑多个分配策略（4.4），它们共享同一张 `ComputeGraph`。若某个策略改了节点属性，并发执行的其它策略会读到不一致的图，造成数据竞争与错排。读 `OpDesc` 是安全的。

**练习 2**：把全局复用关掉（`ge.exec.disableReuseMemory=1`），整图内存会变成什么样？
**答**：每个张量都会新建独立的 `MemoryBlock`，互不复用，总内存约等于所有张量大小之和（再按 512 字节对齐），峰值最高、零复用。

---

### 4.2 内存块与分配器抽象

#### 4.2.1 概念说明

GE 的静态内存规划用三层抽象：

- **`NodeTypeIndex`**：块里的一个「张量条目」，记录这个张量属于哪个节点（`node_`）、是哪类内存（`mem_type_`）、第几个输出（`index_`）以及生命期起止。一个块里有一串 `NodeTypeIndex`。
- **`MemoryBlock`**：内存块，是复用的基本单位。它持有一组 `NodeTypeIndex`、块大小、head/tail 偏移，以及一连串复用相关的属性（是否连续、是否零拷贝、batch label、stream id 等）。
- **`BlockMemAssigner`**：块分配器，负责遍历图、为每个张量建块或复用块、维护可复用池 `reusable_blocks_`。
- **`HybridMemAssigner`**：混合分配器，创建并调度多个 `BlockMemAssigner`（不同复用策略），挑结果最优者。

> **本讲重点变更（enum class 重构）**：本版本里，[memory_block.h:L53](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/compiler/graph/build/memory/memory_block.h#L53) 的 `MemoryNoReuseScope` 与 [L70](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/compiler/graph/build/memory/memory_block.h#L70) 的 `OpMemoryType` 都已从普通 `enum` 改成 **`enum class`**（C++11 强类型枚举）。这意味着引用其值时必须加作用域限定符 `OpMemoryType::kOutput`，不能再裸写 `kOutput`。后续所有判别、`switch` 的 case、成员默认值都改成了带前缀的形式。读源码或写与内存模块交互的代码时请沿用 `OpMemoryType::xxx` / `MemoryNoReuseScope::xxx` 的写法。

#### 4.2.2 核心流程：一个张量如何拿到内存块

当一个算子的某个输出要分配内存时，`ApplyMemory` 决定「复用已有块」还是「新建块」：

```
对节点 n 的输出 index:
  计算 block_size、real_size、batch_label、stream_id
  判断是否能复用: do_reuse = (允许复用) && (前置可复用) && (非连续) && (非零拷贝) && ...
  若 do_reuse:
      按 reuse_strategy 选 GetFirstReleaseBlock 或 GetLastReleaseBlock
      从可复用池里找一个 ReuseBlock() 通过的块
  若找到可复用块 -> 把本张量 AddNodeTypeIndex 进该块
  若没找到       -> new 一个新 MemoryBlock，加入 memory_blocks_
```

#### 4.2.3 源码精读

`OpMemoryType` 现在是强类型枚举，四个值分别表示一个张量在算子里扮演的内存角色：

[compiler/graph/build/memory/memory_block.h:L70](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/compiler/graph/build/memory/memory_block.h#L70)
```cpp
enum class OpMemoryType { kOutput, kWorkspace, kOutputDesc, kInput };
```

`NodeTypeIndex` 构造时用它判定「是否子图输出」，注意 case 里已带 `OpMemoryType::` 前缀：

[compiler/graph/build/memory/memory_block.h:L127](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/compiler/graph/build/memory/memory_block.h#L127) —— `is_subgraph_out_ = (... == ge::PARTITIONEDCALL) && (mem_type_ == OpMemoryType::kOutput);`

`GetMemType` 的 switch 同样用带前缀的 case（[L138-L151](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/compiler/graph/build/memory/memory_block.h#L138-L151)），成员默认值也改为 `OpMemoryType::kOutput`（[L232](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/compiler/graph/build/memory/memory_block.h#L232)）。`MemoryNoReuseScope` 的三个值 `kReuse / kSessionNoReuse / kGraphNoReuse` 同理要写成 `MemoryNoReuseScope::kReuse` 等。

`ApplyMemory` 的「复用 or 新建」决策：

[compiler/graph/build/memory/block_mem_assigner.cc:L1406-L1426](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/compiler/graph/build/memory/block_mem_assigner.cc#L1406-L1426) —— 若 `do_reuse` 为真，按策略调 `GetFirstReleaseBlock`/`GetLastReleaseBlock` 找可复用块；若返回 `nullptr`，则 `new MemoryBlock(...)` 新建一块并加入 `memory_blocks_` 与 `blocks_store_`（后者保证块最终被析构）。

注意 [L1399-L1400](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/compiler/graph/build/memory/block_mem_assigner.cc#L1399-L1400) 的复用前置条件串联：`is_reuse_memory && is_ge_reuse_mem_ && (mem_type != OpMemoryType::kOutputDesc) && !HasAttr(kL2FusionDynamicConvergeOp) && !no_reuse && param.is_op_reuse_mem`。输出描述（`kOutputDesc`）、带 L2 融合属性、显式标了 `kOpNoReuseMem` 的算子都被排除在复用之外。

候选块的选择由复用策略决定：

[compiler/graph/build/memory/block_mem_assigner.cc:L1474-L1502](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/compiler/graph/build/memory/block_mem_assigner.cc#L1474-L1502) —— `GetFirstReleaseBlock` 正序扫描可复用池取第一个通过的块（先释放先复用），`GetLastReleaseBlock` 逆序扫描（先释放后复用）。选哪个由 `reuse_strategy_.reuse_first_release_` 决定。

#### 4.2.4 代码实践

**实践目标**：熟悉「块 vs 张量条目」的包含关系，并验证 enum class 引用方式。

1. 打开 [memory_block.h:L114-L252](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/compiler/graph/build/memory/memory_block.h#L114-L252)，画出 `NodeTypeIndex` 的字段：`node_`、`mem_type_`、`index_`、`life_time_begin_`、`life_time_end_`、`stream_id_`。
2. 打开 [memory_block.h:L255-L308](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/compiler/graph/build/memory/memory_block.h#L255-L308)，确认 `MemoryBlock` 持有 `std::vector<NodeTypeIndex> node_type_index_list_`（块内所有张量条目）。
3. 在仓库里用 `Grep` 搜索 `OpMemoryType::kWorkspace`，观察它被使用的地方（如 `ApplyMemory` 的 no_reuse 判定、`ReleaseMemorys` 的输出判定）。**预期结果**：所有引用都带 `OpMemoryType::` 前缀，找不到裸 `kWorkspace`——这正是 enum class 重构的结果。
4. **待本地验证**：搜索 `MemoryNoReuseScope::` 看它当前在哪些位置被使用，确认其作用域限定用法。

#### 4.2.5 小练习与答案

**练习 1**：`enum class` 相比普通 `enum`，在这里带来什么直接好处？
**答**：强类型、不隐式转 int、枚举值名不会污染外层作用域。`OpMemoryType::kOutput` 不会和别处同名的 `kOutput` 冲突，编译期就能发现「漏写前缀」的错误。

**练习 2**：为什么 `ApplyMemory` 要把新建的块同时放进 `memory_blocks_` 和 `blocks_store_` 两个 vector？
**答**：`memory_blocks_` 在二次复用 `ReuseBlocksByLifeTime` 时可能因 `Swap` 而减少成员，导致某些块指针丢失；`blocks_store_` 只增不减，专门用来保证所有 `new` 出来的块最终都能被 `delete`，避免内存泄漏。

---

### 4.3 复用判定：两个张量何时能共享一块内存

> 本模块直接回答本讲实践任务的核心问题：**GE 是如何判断两个张量可以共享同一块内存的？**

#### 4.3.1 概念说明

判定的理论基础是**生命期区间不相交**。把张量 A 的活跃区间记为 \([b_A, e_A]\)、张量 B 记为 \([b_B, e_B]\)（起止都是节点拓扑序号）。两者能共享同一块内存的充要条件是区间不重叠：

\[
e_A < b_B \quad \text{或} \quad e_B < b_A
\]

GE 在代码里采用的是后一种单向表达：**待分配张量 B 的起始要晚于可复用块 A 的结束**：

\[
\text{CanReuse} \;\Longleftrightarrow\; \text{life\_begin}(B) > \text{life\_end}(A)
\]

这条判据只解决「时间上不冲突」。实际复用还要叠加一系列**否决条件**：块大小相等、同 batch label、块类型不冲突（如「需要集中清零」的块不能和普通块混）、非不同流优先块、块本身允许复用（`reuse_mem_`）等。这些否决条件就是 [memory-constraints.md](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/docs/zh/design/constraints/memory-constraints.md) 约束 3 列举的那些特殊场景。

#### 4.3.2 核心流程

复用判定分两层：

1. **`ReuseBlock`**（粗筛）：对一个候选块，依次否决——连续块 / 零拷贝块 / batch label 不一致 / 块类型冲突 / 不同流优先块，最后调 `CanReuseBlock` 做生命期判定。
2. **`CanReuseBlock`**（精判）：要求块大小相等，且 `life_begin > block.GetLifeEnd(stream_id)`。
3. **`CanBlockLifeReuse`**（跨块二次复用）：在 `ReuseBlocksByLifeTime` 阶段，判断两个**块**能否合并，要处理同流（直接比生命期）与跨流（用 `GetDependLifeBegin` 找依赖点）两种情况。

`CrossLifeTime` 是底层原语：判定两个 `NodeTypeIndex` 的生命期是否相交。

#### 4.3.3 源码精读

核心精判 `CanReuseBlock`：

[compiler/graph/build/memory/memory_block.cc:L71-L84](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/compiler/graph/build/memory/memory_block.cc#L71-L84)
```cpp
bool CanReuseBlock(size_t life_begin, const ge::MemoryBlock &reusable_block, size_t block_size) {
  bool can_reuse = false;
  if (reusable_block.Size() == block_size) {            // 块大小必须相等
    if (life_begin > 0) {
      if (life_begin > reusable_block.GetLifeEnd(reusable_block.stream_id_)) {
        can_reuse = true;                                // 生命期不相交：B 起始 > A 结束
      }
    } else {
      can_reuse = true;
    }
  }
  return (can_reuse && (!CanNotLifeReuse(reusable_block)));
}
```

粗筛 `ReuseBlock` 叠加否决条件：

[compiler/graph/build/memory/memory_block.cc:L86-L105](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/compiler/graph/build/memory/memory_block.cc#L86-L105) —— 先排除连续块 / 零拷贝块 / 异 batch / 块类型冲突（`IsBlockTypeConflictWithNode`）/ 不同流优先块，最后才调 `CanReuseBlock`。注释写明「一个节点可以复用同流及前序流的块」。

底层原语 `CrossLifeTime`（[L107-L127](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/compiler/graph/build/memory/memory_block.cc#L107-L127)）实现的就是区间相交判断：若一方开始得更早，就看它的结束是否「≥」另一方的开始。

块类型冲突由 `BlockTypeList` 用一个属性集合快速判定——只有「数据输入节点 `kData`」与「集中清零 `kConcentrateAtomic`」这两类属性会触发冲突，定义在 [block_type_list.h:L20-L66](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/compiler/graph/build/memory/block_type_list.h#L20-L66) 中（`NodeMemAttr` 枚举 + `BlockTypeList::IsConflictWithBlock/IsConflictWithOneNode`）。这正是约束 3「需要 atomic 清零的内存块不能被其他节点复用」的代码落点。

#### 4.3.4 代码实践

**实践目标**：画出两个张量复用同一块内存的示例，并用源码判据验证。

考虑一段直线图（拓扑序即编号）：

```
Node1(Add) ──> Node2(Relu) ──> Node3(无)      Node4(Add) ──> Node5(Relu)
   (id=1)        (id=2)                       (id=4)        (id=5)
```

- Node1 的输出张量 A：生命期 \([1, 2]\)（在 Node2 处被最后一次消费）。
- Node4 的输出张量 B：生命期 \([4, 5]\)。

**判定**：当主循环走到 Node4 时，张量 A 所在的块已在 Node2 处理后放回可复用池。此时 `life_begin(B) = 4 > life_end(A) = 2`，且块大小相等、同流、无特殊属性 → `CanReuseBlock` 返回 true，B 复用 A 的块。内存示意：

```
拓扑步:    1     2     3     4     5
块内存:  [  A  ][        ][  B  ]     ← A 与 B 物理上共用同一块地址
```

操作步骤：

1. 打开 [memory_block.cc:L71-L84](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/compiler/graph/build/memory/memory_block.cc#L71-L84)，把 `CanReuseBlock` 的条件逐一对照上面的例子（块大小相等 ✓、`4 > 2` ✓）。
2. 打开 [memory_block.cc:L86-L105](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/compiler/graph/build/memory/memory_block.cc#L86-L105)，确认本例没有触发任何否决条件（非连续、非零拷贝、同 batch、块类型不冲突）。
3. **预期结果**：上面两步都通过，故 B 复用 A。把 `Node4` 改成「需要集中清零」的算子（带 `kConcentrateAtomic` 属性），则 `IsBlockTypeConflictWithNode` 命中，复用被否决，B 会另开一块——这就是约束 3 的效果。
4. **待本地验证**：构造一个真实的小图用 atc 编译，dump 内存排布日志（`GELOGD` 的 block String 打印在 [block_mem_assigner.cc:L2642-L2643](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/compiler/graph/build/memory/block_mem_assigner.cc#L2642-L2643)），观察 block 的 `node_type_index_list_` 里是否确实混入了来自不同拓扑步的张量。

#### 4.3.5 小练习与答案

**练习 1**：如果两个张量生命期完全相同（都在 \([1,3]\) 活跃），能复用吗？
**答**：不能。`CrossLifeTime` 对「起始相等」直接返回 true（相交），`CanReuseBlock` 里 `life_begin > life_end` 不成立。生命期相同意味着同一时刻都要读写这块内存，物理上不能共享。

**练习 2**：为什么需要 atomic 清零的块要单独排除？
**答**：atomic 清零算子会在算子执行前向该块写 0，若这块同时被别的普通张量复用，清零会破坏那个张量的数据。`BlockTypeList` 用 `kConcentrateAtomic` 属性把这类块与普通块标记为冲突，从复用池里隔离。

---

### 4.4 峰值内存优化

#### 4.4.1 概念说明

「峰值内存优化」要解决的问题是：同一张图，用不同的复用策略（先释放先复用 vs 先释放后复用、按大小排序等）会得到不同的总内存。GE 不赌某一种策略最优，而是**同时跑多种策略，选总内存最小的那一个**。这是 `HybridMemAssigner` 的核心设计。

另外两类优化：

- **对齐优化**：所有块按 512 字节对齐，零拷贝块按 32 字节对齐，在保证硬件地址对齐要求的前提下尽量少浪费。
- **特殊场景的复用控制**：连续内存（HCOM、连续输入）、atomic 清零、零拷贝、固定地址输出（constant/variable）、P2P 内存、动态多 batch 等，每种都有专门的复用约束（见约束 2~5）。

#### 4.4.2 核心流程

`HybridMemAssigner::Assign` 的优化策略：

1. 创建若干个不同 `ReuseStrategy` 的 `BlockMemAssigner`（默认 `binary-block` + `max-block`；开启内存优先级模式时再加 4 个 range 变体）。
2. 用线程池**并发**执行每个分配器的 `AssignMemoryWithReuse`，各自独立算出一种排布与总内存。
3. 按总内存**升序排序**，取第 0 个（最小）作为最终结果，把它的偏移写回算子（`SetOpMemOffset`）。

#### 4.4.3 源码精读

多策略并发 + 取最小：

[compiler/graph/build/memory/hybrid_mem_assigner.cc:L80-L92](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/compiler/graph/build/memory/hybrid_mem_assigner.cc#L80-L92) —— 默认创建 `binary-block`（`ReuseStrategy(false, true, false, ...)`，不用 range、升序）和 `max-block`（`ReuseStrategy(true)`，用 range）两个分配器；[L94-L118](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/compiler/graph/build/memory/hybrid_mem_assigner.cc#L94-L118) 在内存优先级模式下再追加 4 个 range-binary 变体（frfr/frlr，升序/降序组合）。

[compiler/graph/build/memory/hybrid_mem_assigner.cc:L120-L144](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/compiler/graph/build/memory/hybrid_mem_assigner.cc#L120-L144) —— 用 `ThreadPool` 并发提交所有分配器（`HybridMemAssigner::AssignMemory` 静态方法逐个调 `AssignMemoryWithReuse`），等待全部完成后 `std::sort` 按内存大小升序，取 `memory_assigners[0]` 作为 priority assigner，调 `SetOpMemOffset(false)` 把它的偏移写回算子。注释明确：「ascending sort by memory size, so assigner 0 is priority assigner」。

`ReuseStrategy` 结构体（[memory_block.h:L72-L83](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/compiler/graph/build/memory/memory_block.h#L72-L83)）四个开关：`use_range_`（是否按档位 range）、`ascending_sort_`（生命期升/降序）、`reuse_first_release_`（先释放先复用 / 后复用）、`memory_priority_mode_`（内存优先级模式）。

对齐常量：

[compiler/graph/build/memory/mem_assigner.h:L18](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/compiler/graph/build/memory/mem_assigner.h#L18)
```cpp
static const int64_t MEM_ALIGN_SIZE = 512;
```
普通块一律 512 字节对齐；零拷贝块因对应用户输入、需 32 字节对齐（见 [memory-constraints.md](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/docs/zh/design/constraints/memory-constraints.md) 约束 5）。对齐算法在 [graph_mem_assigner.cc:L940-L944](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/compiler/graph/build/memory/graph_mem_assigner.cc#L940-L944) 的 `AlignMemOffset`：`(size + 511) / 512 * 512`。

特殊场景举例（均来自约束文档与源码）：

- **连续内存**：[continuous_mem.h:L19-L75](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/compiler/graph/build/memory/continuous_mem.h#L19-L75) 的 `ContinuousMem` / `ContinuousMemMng` 管理需要逻辑连续的输出（如 HCOM），可合并成一大块，但「连续输出—连续输入且集中清零」场景不复用。
- **动态多 batch**：[dynamic_batch_mem_assigner.h:L22](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/compiler/graph/build/memory/dynamic_batch_mem_assigner.h#L22) 限定 `kMaxSplitSizeForDynamicBatch = 400MB`，batch 间有对齐合并策略、batch 内外不复用（约束 2）。
- **固定地址输出**：constant/variable/fileconstant 等算子输出地址编译期固定（`is_fixed_addr_prior_`），可被复用但地址不可变（约束 3）。

**与运行时动态分配的对照**（动态 shape 图用）：[active_memory_allocator.h:L194](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/v1/graph/manager/active_memory_allocator.h#L194) 的 `PhysicalMemoryAllocator` 与 [L483](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/v1/graph/manager/active_memory_allocator.h#L483) 的 `ActiveMemoryAllocator` 用 `std::recursive_mutex`（见各类内的 `mutex_` 成员）保护，最终调 `rtMalloc`/`rtFree` 按需申请——这与本讲的「编译期一次规划、写入偏移」是两条完全不同的路径。

#### 4.4.4 代码实践

**实践目标**：理解「多策略取最小」如何压低峰值，并量化对齐开销。

1. 打开 [hybrid_mem_assigner.cc:L69-L149](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/compiler/graph/build/memory/hybrid_mem_assigner.cc#L69-L149)，确认流程：建 N 个分配器 → 线程池并发 → sort 取最小 → 写回偏移。
2. 在 [L137-L139](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/compiler/graph/build/memory/hybrid_mem_assigner.cc#L137-L139) 有一行 `GELOGI("%s memory assigner memory size:%zu", ...)`，编译时打开 INFO 日志即可看到每种策略各自的总内存，直观对比谁更省。
3. **对齐开销估算**：若一张图有 100 个互不复用的小张量，每个 300 字节，对齐后每个变 512 字节，总内存 \(100 \times 512 = 51200\) 字节，比未对齐的 \(100 \times 300 = 30000\) 字节多约 70%。这解释了为什么「能复用就复用、减少块数」对降低峰值如此重要。
4. **待本地验证**：在内存优先级模式下（`memory_priority_mode_` 为 true）会有 6 个分配器并发，观察日志里 6 个 size，确认最终选中的确实是其中最小者。

#### 4.4.5 小练习与答案

**练习 1**：`HybridMemAssigner` 并发跑多个分配器，会不会因为它们共享同一张图而出问题？
**答**：不会，前提是「只读不改图」。约束 1 保证了 `AssignMemoryWithReuse` 及其调用链只读 `OpDesc`、不改图属性；每个分配器维护自己独立的 `memory_blocks_`、`reusable_blocks_`，互不干扰。最终只有一个（内存最小的）分配器的结果通过 `SetOpMemOffset` 写回图，这一步由单线程在并发结束后执行。

**练习 2**：为什么零拷贝块用 32 字节对齐而非 512？
**答**：零拷贝块直接对接用户的输入/输出地址（u7-l4 详述），用户的地址往往只保证 32 字节对齐，强行 512 对齐会要求用户多占内存且可能地址不连续。普通块在设备内部使用，按硬件友好的 512 对齐以获得更好性能。

---

## 5. 综合实践

把本讲四个模块串起来，完成一个**源码阅读 + 复用推演**的综合任务。

**任务**：给定下面这张含分支与汇合的小静态图，推演 GE 会如何规划内存，并对照源码验证你的推演。

```
         ┌──> Node2(Square, id=2) ──┐
Node1(Add,id=1)                     ├──> Node4(Concat, id=4) ──> Node5(Data, id=5)
         └──> Node3(Square, id=3) ──┘
```

操作步骤：

1. **划分生命期**：写出每个算子输出的生命期区间。提示：Node1 的输出同时被 Node2、Node3 消费，其生命期结束于 `max(id of consumers) = 3`，即 \([1, 3]\)。
2. **判断复用**：Node2 的输出（\([2, 4]\)）与 Node3 的输出（\([3, 4]\)）能否复用同一块？用 `CrossLifeTime` 判据——两者区间 \([2,4]\) 与 \([3,4]\) 相交（3 < 4），**不能复用**。
3. **定位源码**：在 [memory_block.cc:L107-L127](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/compiler/graph/build/memory/memory_block.cc#L107-L127) 确认 `CrossLifeTime` 对相交区间的返回值；在 [memory_block.cc:L71-L84](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/compiler/graph/build/memory/memory_block.cc#L71-L84) 确认 `CanReuseBlock` 会因此返回 false。
4. **峰值优化**：假设此图开启了内存优先级模式，[hybrid_mem_assigner.cc:L94-L118](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/compiler/graph/build/memory/hybrid_mem_assigner.cc#L94-L118) 会并发跑 6 个分配器，最终选内存最小的。说明这种「多策略取最小」对这张图的意义——不同 `reuse_first_release_` 设置会让 Concat 前的可复用池状态不同，从而可能压低一两块。
5. **enum class 留意**：在推演过程中，凡涉及张量类型一律用 `OpMemoryType::kOutput` 等带前缀写法记录，养成与当前代码一致的习惯。
6. **待本地验证**：用 atc 编译一个等价的小 ONNX 图，打开 block 打印日志（[block_mem_assigner.cc:L2642](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/compiler/graph/build/memory/block_mem_assigner.cc#L2642)），核对每个 block 的 `node_type_index_list_` 是否与你的推演一致。

**预期成果**：一张标注了每个张量生命期、归属 block、head/tail 偏移的内存排布图，以及一段用源码判据解释「为什么这样排」的说明。

## 6. 本讲小结

- GE 在**静态 shape 图**上做**编译期整图内存规划**：一次 `rtMalloc` 拿大块，每个张量按编译期算好的偏移就位，运行时无需逐张量分配。
- 三层抽象：`NodeTypeIndex`（块内张量条目）⊂ `MemoryBlock`（复用基本单位，持有多个 `NodeTypeIndex`）⊂ `BlockMemAssigner`（遍历图建/复用块），再上层 `HybridMemAssigner` 调度多策略。
- 复用核心判据是**生命期区间不相交**：`life_begin(B) > life_end(A)`，叠加块大小相等、同 batch、块类型不冲突等否决条件；`CanReuseBlock` / `ReuseBlock` / `CrossLifeTime` 是判定链。
- 峰值优化靠**多策略并行 + 取内存最小**：`HybridMemAssigner` 用线程池并发跑多个 `ReuseStrategy` 的分配器，sort 后取最小者写回偏移。
- 特殊场景（连续内存、atomic 清零、零拷贝、固定地址、P2P、动态多 batch）各有专门约束，集中记录在 [memory-constraints.md](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/docs/zh/design/constraints/memory-constraints.md)。
- **本版本变更**：`MemoryNoReuseScope`、`OpMemoryType` 已重构为 `enum class`，引用须写 `OpMemoryType::kOutput` 等带前缀形式；对齐常量 `MEM_ALIGN_SIZE = 512`（零拷贝 32）。

## 7. 下一步学习建议

- 阅读 [docs/zh/design/constraints/memory-constraints.md](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/docs/zh/design/constraints/memory-constraints.md) 全文，把约束 2~5 的每个场景在源码里找到对应落点（如 `IsNodeAndPeerNodeTaskSupportZeroCopy`、`ContinuousMemMng`）。
- 进入 **u7-l2 内存冲突检测与排布优化**：本讲只讲了「正确的复用」，下一讲讲编译期如何**检测并消除**读写冲突与 Inplace 冲突（`mem_layout_conflict_optimize`），它与本讲的「不支持地址刷新算子列表」必须保持一致。
- 若对运行时侧感兴趣，对照阅读 **runtime/v1/graph/manager/active_memory_allocator.h** 的 `PhysicalMemoryAllocator` / `ActiveMemoryAllocator`，理解动态 shape 图「运行时按需分配」与本讲「编译期一次规划」的差异（约束文档「动态内存复用」一节）。
- 结合 **u7-l3 流分配与多流并行**，理解 `stream_id` 如何影响复用判定（同流直接比生命期，跨流要用 `GetDependLifeBegin` 找依赖点），这是 `CanBlockLifeReuse` 跨流分支的背景。

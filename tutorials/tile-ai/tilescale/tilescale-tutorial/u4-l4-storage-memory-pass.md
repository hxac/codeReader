# 存储与内存管理 pass

## 1. 本讲目标

本讲深入 TileLang 编译流水线第三阶段 `OptimizeForTarget` 中「与存储/内存相关」的一组 pass。学完本讲，你应当能够：

- 说清楚为什么 GPU 代码生成之前必须把多维 `Buffer` 拍平成一维物理内存，以及 `FlattenBuffer` 做了哪些附带处理。
- 理解 `StorageRewrite` 如何用「活跃变量分析（liveness analysis）」让生命期不重叠的临时缓冲区复用同一块显存，以及它在什么时候会「让位」给下游的 shared memory 合并 pass。
- 掌握 `MergeSharedMemoryAllocations` 如何把一个 kernel 里数十个 `shared`/`shared.dyn` 分配合并成**一个** arena 分配，并能解释 `enable_aggressive_merge` 开关改变了什么。
- 理解 `ThreadSync` 如何根据共享内存的读写冲突自动插入 `__syncthreads` 屏障，包括循环外提、async-wait 后补屏障、以及部分线程参与时的命名屏障（named barrier）。
- 能够用 pass config 开关（如 `tl.enable_aggressive_shared_memory_merge`、`tl.debug_merge_shared_memory_allocations`）观察并对比生成的 CUDA 源码中的 shared memory 占用。

本讲承接 [u3-l4 OptimizeForTarget](u3-l4-optimize-target.md)：那一讲给出了整条目标相关优化流水线的「地图」与三段划分，本讲则把镜头推进到其中的「缓冲区与索引整形段」和「codegen 收尾段」里和存储/同步直接相关的四道 pass。

## 2. 前置知识

阅读本讲前，你需要具备以下基础（不熟悉的概念下面会简单解释）：

- **GPU 显存层级**：global memory（HBM，大但慢）、shared memory（片上 SRAM，小但快、块内可见）、local/register（寄存器，线程私有）。TileLang 用 scope 字符串 `"global"`/`"shared"`/`"shared.dyn"`/`"local"` 等区分，详见 [u2-l2 Tile 声明与显存层级](u2-l2-tile-alloc.md)。
- **静态 vs 动态 shared memory**：CUDA 里 `__shared__` 声明的是**静态** shared memory（编译期大小固定，scope tag 为空串）；而 `extern __shared__` 风格、由启动参数指定大小的是**动态** shared memory（TileLang 中 scope tag 为 `".dyn"`）。本讲的合并 pass 二者都会处理。
- **活跃变量分析（liveness analysis）**：编译原理里的经典概念。对一段程序，分析每个变量（缓冲区）从哪条语句开始「活跃」（gen，第一次被定义/写入）、到哪条语句结束「活跃」（kill，最后一次被使用）。两个缓冲区的活跃区间不重叠，就可以让它们复用同一块物理内存——这是本讲四个 pass 共用的核心思想。
- **线性扫描寄存器分配（linear-scan）**：一种把「生命期区间」打包进有限物理资源的经典算法，常用 `free list`（空闲链表）回收生命期结束的槽位。本讲的 shared memory arena 打包就是它的变种。
- **TIR 的 Buffer / Allocate / DeclBuffer**：TVM TIR 里，一块逻辑上的多维数组是 `Buffer`；它底层由一个 `Allocate` 语句申请一块 `Var`（指针）指向的内存；`DeclBuffer` 把 `Buffer` 对象绑定到那个指针。本讲的 pass 就是在重写这些节点的 shape、下标、指针和作用域。

一句话直觉：**这一讲的所有 pass 都在做一件事——把程序员写出来的「很多块、很多维」的临时内存，重新编排成「少块、一维、生命期不冲突」的物理内存，并补上必要的同步，让最终生成的 CUDA 代码又小又对。**

## 3. 本讲源码地图

本讲涉及的关键源码文件及其职责：

| 文件 | 职责 | 在流水线中的位置 |
|------|------|------------------|
| [src/transform/flatten_buffer.cc](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/flatten_buffer.cc) | 把多维 `Buffer` 拍平成一维，重写下标，提升索引位宽 | 整形段开头 |
| [src/transform/storage_rewrite.cc](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/storage_rewrite.cc) | 临时内存（local/global/static-shared）的活跃分析与复用 | 整形段中部 |
| [src/transform/merge_shared_memory_allocations.cc](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/merge_shared_memory_allocations.cc) | 把多个 shared 分配合并成单个 arena 分配 | codegen 收尾段（`SplitHostDevice` 之后） |
| [src/transform/thread_storage_sync.cc](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/thread_storage_sync.cc) | 根据 shared 读写冲突自动插入 `__syncthreads` | 紧跟合并 pass 之后 |
| [tilelang/engine/phase.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/phase.py) | `OptimizeForTarget` 流水线编排，决定这些 pass 的执行顺序 | —— |
| [src/transform/common/thread_sync_types.h](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/common/thread_sync_types.h) | 命名屏障编号约定（Hopper 16 个硬件命名屏障） | 被 ThreadSync 使用 |

它们的执行顺序（摘自 `phase.py` 的 `OptimizeForTarget`）是：

```
... FlattenBuffer → ConfigIndexBitwidth → ... → StorageRewrite ...
... SplitHostDevice → MergeSharedMemoryAllocations
                  → ThreadSync("shared") → ThreadSync("shared.dyn") → InjectPTXAsyncCopy ...
```

注意：**`MergeSharedMemoryAllocations` 必须在 `SplitHostDevice` 之后**，因为合并后的单一分配要放在每个 device 函数的开头；而 **`InjectPTXAsyncCopy` 必须在 `ThreadSync` 之后**，因为 cp.async 调用不会被识别为普通的 buffer load，提前插入会让同步分析漏判。本讲的四个模块基本按这条流水线顺序展开。

## 4. 核心概念与源码讲解

### 4.1 FlattenBuffer：把多维 Buffer 拍平成一维物理内存

#### 4.1.1 概念说明

在 `LowerTileOp`/`LayoutInference` 阶段，TileLang 的 IR 里还保留着程序员视角的**多维** `Buffer`（比如一个 `(128, 32)` 的 `float16` shared tile）。但 GPU 的物理显存是一维的字节序列，硬件指令（TMA、mma、cp.async）和 codegen 都更希望面对「一个一维指针 + 线性偏移」。

`FlattenBuffer` 就是这个「降维」工序：它把每个 `Buffer` 重写成 `GetFlattened_buffer()` 得到的一维等价 Buffer，并把所有 `BufferLoad`/`BufferStore` 的多维下标换算成单一线性偏移。它是后面 `StorageRewrite`、`MergeSharedMemoryAllocations` 能正常工作的前提——后两者都要求「flat memory buffers」（一维）。

#### 4.1.2 核心流程

`FlattenBuffer` 的核心是一个 `BufferFlattener`（继承自 `IRMutatorWithAnalyzer`），它遍历整个函数体并对几类节点做改写：

1. **拍平 Buffer 声明**：`Block` 的 `alloc_buffers`、`reads`、`writes` 里出现的 `Buffer`，逐个经 `GetFlattenedBuffer` 换成一维版本。
2. **重写下标**：对 `BufferLoad`/`BufferStore`，用 `buffer->ElemOffset(indices)` 把多维下标算成线性偏移，再把访问重定向到一维 Buffer。
3. **处理 `Allocate` 的大小**：把 N 维 `extents` 折算成一维 extent（或匹配内部 `DeclBuffer` 的拍平结果）。
4. **剥除 `DeclBuffer`**：当前实现把 `DeclBuffer` 节点剥掉（`VisitStmt_(DeclBufferNode)` 直接返回 body），因为不是所有下游 pass 都支持 `DeclBuffer`。
5. **附带处理**：索引在可能溢出时提升为 `int64`（`Int64Promoter`）；`bool` 类型用 `int8` 作为后端存储，访问处插入 cast。
6. **保留 buffer_map 不变**：函数参数（`func->buffer_map`）刻意不拍平，仍用于校验用户传入的参数；body 里拍平后的 Buffer 与参数 Buffer 互为别名。

#### 4.1.3 源码精读

pass 入口与类定义见 [flatten_buffer.cc:47-64](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/flatten_buffer.cc#L47-L64)，`Flatten` 是静态入口，它会读出 `kLocalVarInit` 注解（给 `local.var` 标量赋初值用），标记 buffer_map 形状，再 mutate body。

`Allocate` 节点的拍平逻辑在 [flatten_buffer.cc:146-217](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/flatten_buffer.cc#L146-L217)：若已经是 1 维则不动；否则若内部有匹配的 `DeclBuffer`，用该 Buffer 的拍平 shape；否则把所有 extent 连乘作为 fallback：

```cpp
// Fallback, this is an allocation without a matching DeclBuffer
PrimExpr flat_extent = 1;
for (const auto &dim : op->extents) {
  flat_extent *= dim;
}
return {flat_extent};
```

`BufferLoad`/`BufferStore` 的下标重算核心在 `VisitBufferAccess`（[flatten_buffer.cc:323-333](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/flatten_buffer.cc#L323-L333)）：调用 `GetSimplifiedElemOffset` 把多维 indices 转成线性偏移，再把 buffer 换成拍平版本。

索引位宽提升由内嵌的 `Int64Promoter`（[flatten_buffer.cc:72-107](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/flatten_buffer.cc#L72-L107)）负责，触发条件在 `GetSimplifiedElemOffset`（[flatten_buffer.cc:295-321](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/flatten_buffer.cc#L295-L321)）：当某下标的常量界逼近其位宽上限（如 int32 可能溢出）时，整组下标提升为 int64，避免大缓冲区寻址溢出。

pass 注册见 [flatten_buffer.cc:381-391](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/flatten_buffer.cc#L381-L391)，导出为 FFI 全局函数 `tl.transform.FlattenBuffer`。

#### 4.1.4 代码实践

**实践目标**：直观看到「拍平」前后 IR 的差别。

1. 用一个极简 matmul kernel（可基于 [examples/quickstart.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/quickstart.py)）。
2. 在 `phase.py` 的 `OptimizeForTarget` 里，临时在 `FlattenBuffer()` 调用前后各加一次 `mod.show()`（或 `print(mod.script())`）。
3. 观察：拍平前 `A_shared: T.Buffer((128, 32), ...)` 的访问形如 `A_shared[i, j]`；拍平后变成 `A_shared_flattened[i * 32 + j]`（一维下标）。

**需要观察的现象**：多维 `Buffer` 变为一维；`DeclBuffer` 节点消失；大 kernel 的索引类型从 int32 变为 int64。

**预期结果**：拍平后 IR 里所有 `alloc_buffers` 的 shape 都是单元素数组，所有 `BufferLoad/Store` 的 `indices` 长度为 1。具体输出「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `FlattenBuffer` 不拍平 `func->buffer_map` 里的参数 Buffer？
**答案**：参数 Buffer 用于校验用户运行时传入的张量形状/类型，必须保留原始多维语义；body 里拍平后的同名 Buffer 与参数 Buffer 通过指针别名共享存储，互不影响（见 [flatten_buffer.cc:59-63](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/flatten_buffer.cc#L59-L63) 的注释）。

**练习 2**：`Int64Promoter` 在什么情况下会被触发？
**答案**：当某下标的常量界（`const_int_bound` 的 `max_value`）逼近甚至超过其位宽的有符号上界（如 int32 的 `2^31-1`）时，说明该下标可能溢出，于是把整组下标提升为 int64（[flatten_buffer.cc:302-315](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/flatten_buffer.cc#L302-L315)）。

---

### 4.2 StorageRewrite：临时内存的存活分析与复用

#### 4.2.1 概念说明

一个 TileLang kernel 在 IR 层面会产生大量临时 `Allocate`（累加器、中间 fragment、搬运用的 staging buffer……）。如果每个临时都独占一块物理显存，shared memory 和寄存器会迅速爆掉。`StorageRewrite` 用经典的活跃变量分析，让**生命期不重叠**的临时缓冲区复用同一块存储。

需要特别说明的是它和 shared memory 合并 pass 的分工：当 kernel 使用了动态 shared memory（`shared.dyn` 分配数 > 1）或显式开启 `tir.merge_static_smem` 时，`StorageRewrite` 会**主动关闭复用**（`enable_reuse = false`），把 shared memory 的合并工作让给下游更专门的 `MergeSharedMemoryAllocations`。因此 `StorageRewrite` 在实践中主要处理 global 临时、以及未被合并 pass 接管的那些「足够大」的缓冲区复用；真正小的局部数组（warp/local 作用域、handle、或编译期 ≤32 bit）会被它显式跳过，交给底层编译器（LLVM/nvcc）做寄存器分配。

#### 4.2.2 核心流程

`StorageRewrite` 由 `StoragePlanRewriter` 实现，分四步：

1. **线性化访问模式**：`LinearAccessPatternFinder` 把嵌套的控制流树（For/If/AttrStmt）压平成一个线性的 `StmtEntry` 序列，记录每条语句「触及（touched）」了哪些 buffer var，并用 `scope_pair_offset` 标注嵌套作用域的起止配对。
2. **活跃分析**：`LivenessAnalysis` 对线性序列做一次反向扫描找 `kill`（最后一次访问）、一次正向扫描找 `gen`（第一次访问），得到每个 buffer 的活跃区间。
3. **内存规划**：`PlanMemory` 顺序扫描 gen/kill 事件，在 `gen` 时调 `FindAlloc` 决定该 buffer 复用哪块已 `Free` 的 `StorageEntry`（或新建），在 `kill` 时把对应 `StorageEntry` 放回空闲池。
4. **重写**：`PrepareNewAlloc` 决定每个 `StorageEntry` 挂载（attach）到哪个作用域，然后 `operator()` 真正改写 body——把多个 `Allocate` 合并/删除，把 `BufferLoad/Store` 的指针与下标偏移改写到复用后的 `alloc_var`。

复用匹配的核心是 `FindAlloc`：对编译期已知大小的 buffer，在 `const_free_map_`（按 bit 数索引的多 map）里找大小落在 \([const\_nbits/16,\ const\_nbits \times 16]\) 区间、且 scope/attach_scope/元素类型都匹配的空闲块；大小未知时退化为 `sym_free_list_` 的轮转。

#### 4.2.3 源码精读

pass 入口 [storage_rewrite.cc:1959-2007](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/storage_rewrite.cc#L1959-L2007) 里有关键的「让位」逻辑：

```cpp
bool has_dynamic = collector.dyn_shmem_allocs_.size() > 1;
if (has_dynamic || merge_static_smem) {
  // ... dynamic shared memory benefits from MergeSharedMemoryAllocations
  enable_reuse = false;
}
```

也就是说，一旦检测到多于一个动态 shared 分配，本 pass 就不再做复用，避免和下游合并 pass 冲突（[storage_rewrite.cc:1969-1979](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/storage_rewrite.cc#L1969-L1979)）。此外，对 Vulkan/WebGPU 目标会强制要求复用时 dtype 完全一致（`reuse_require_exact_matched_dtype = true`）。

活跃分析 `LivenessAnalysis`（[storage_rewrite.cc:906-932](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/storage_rewrite.cc#L906-L932)）一次反向求 kill、一次正向求 gen，和下游合并 pass 的思路如出一辙。

`FindAlloc` 的复用判定在 [storage_rewrite.cc:1065-1145](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/storage_rewrite.cc#L1065-L1145)。三条短路规则决定了哪些 buffer 不参与复用：

```cpp
bool is_small_array =
    (scope.tag.empty()) &&
    (scope.rank >= StorageRank::kWarp || op->dtype.is_handle() ||
     (is_known_size && const_nbits <= 32));
if (!enable_reuse || is_small_array || !is_flat_memory_space) {
  return NewAlloc(op, attach_scope, scope, const_nbits);
}
```

含义：小数组（warp/local 作用域、handle、或 ≤32 bit）交给后续 LLVM/ptxas 做寄存器分配更划算；非一维存储（如 2D texture）不参与；当然 `enable_reuse` 关闭时一律新建。匹配窗口用 `match_range = 16` 控制，复用块大小需落在目标大小的 \(1/16 \sim 16\) 倍内：

\[
\frac{const\_nbits}{16} \;\le\; candidate \;\le\; const\_nbits \times 16
\]

可选的原地（inplace）检测由 `InplaceOpVerifier` 配合 `PlanMemory` 中的 gen/kill 完成（[storage_rewrite.cc:984-1008](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/storage_rewrite.cc#L984-L1008)），由 pass config `tl.storage_rewrite_detect_inplace` 开启，允许把 `dst[i] = f(src[i])` 的写复用到 `src` 上（引入别名、省一块临时）。

pass 末尾还会调用 `PointerValueTypeRewrite`（[storage_rewrite.cc:2003](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/storage_rewrite.cc#L2003)）做指针/向量类型收尾。

#### 4.2.4 代码实践

**实践目标**：观察 `StorageRewrite` 前后临时分配数量的变化。

1. 写一个 elementwise kernel，故意声明 3 个不重叠生命期的 `alloc_local`/`alloc_fragment` 临时。
2. 在 `phase.py` 的 `StorageRewrite()` 调用前后各 `print(mod.script())`。
3. 数一下 `Allocate` 节点的个数：复用前应有 3 个独立 `Allocate`，复用后应合并为更少（理想情况 1 个）。

**需要观察的现象**：生命期不重叠的临时被合并到同一个底层 `Var`，访问处出现 `bits_offset` 偏移。

**预期结果**：`Allocate` 数量减少；若临时都很小（≤32 bit 或在 local 作用域），则可能不合并（交给寄存器分配）。具体「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `scope.rank >= StorageRank::kWarp` 的小数组不参与复用？
**答案**：这类小数组最终会被 lowering 成寄存器，底层编译器（LLVM/nvcc）的寄存器分配做得比 IR 层的显式复用更好，强行在 IR 层合并反而会干扰（[storage_rewrite.cc:1086-1094](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/storage_rewrite.cc#L1086-L1094) 注释）。

**练习 2**：动态 shared memory 存在时，`StorageRewrite` 为什么关闭复用？
**答案**：因为动态 shared memory 不需要保持可读性，且由更专门的 `MergeSharedMemoryAllocations` 做更优的合并；两边同时复用会冲突，所以让位（[storage_rewrite.cc:1970-1979](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/storage_rewrite.cc#L1970-L1979)）。

---

### 4.3 MergeSharedMemoryAllocations：shared memory 的单一 arena 合并

#### 4.3.1 概念说明

GPU 对一个 kernel 的 shared memory 分配有约束：**动态 shared memory 通常只允许一个 `extern __shared__` 分配**。但 TileLang 经过软件流水（多缓冲 `num_stages` 份）、layout 推理、warp 特化等 pass 后，一个 kernel 里可能冒出几十个 `shared`/`shared.dyn` 的 `Allocate`。

`MergeSharedMemoryAllocations` 就是把这些分配**合并成单个一维 `UInt(8)`（字节）arena 分配**，然后让每个原 buffer 的访问加上各自的字节偏移去索引这个大 arena。文件头注释把目标说得很清楚：

> Each GPU kernel is allowed to have only one dynamic or static shared memory allocation. This pass merges multiple TIR-level dynamic or static shared memory allocations into one allocation.

它和 `StorageRewrite` 的关键区别在于：它是**专门为 shared memory 设计、用线性扫描 arena 打包算法**，并且合并出的单个分配挂在 `thread_extent`（线程块）作用域开头——这正是它必须在 `SplitHostDevice` 之后运行的原因。

#### 4.3.2 核心流程

pass 入口 `MergeSharedMemoryAllocations(Stmt, ...)`（[merge_shared_memory_allocations.cc:1334-1352](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/merge_shared_memory_allocations.cc#L1334-L1352)）先用 `AllocateCollector` 收集所有动态/静态 shared 分配，**只有当某类分配数 > 1 时才真正合并**（动态分配恒合并，静态分配仅当 `tir.merge_static_smem` 为真时合并）。然后对动态、静态分别构造一个 `SharedMemoryRewriter` 做合并。

`SharedMemoryRewriter` 的核心算法（`PlanReuse`）：

1. **`SharedMemLinearAccessPatternFinder`**：把控制流压平成线性序列并做活跃分析。其中 `enable_aggressive_merge` 控制一次访问被记到「哪一层作用域」——开启时记到最内层（更激进、复用更多），关闭时记到分配所在层（更保守）。
2. **`SharedMemoryAlignmentPlanner`**：规划每个 shared var 的对齐字节数。遇到 `tl_gemm`/`tl_gemm_sp`/`tma_load`/`tma_store`/`initialize_wgmma_descriptor`/`initialize_tcgen05_descriptor` 等需要严格对齐的 intrin 时，把子树标记为「对齐作用域」；Hopper 目标要求 **1024 字节**对齐，其它目标 **16 字节**。
3. **`LivenessAnalysis`**：反向求 kill、正向求 gen；并做 kill 点重排——若某 buffer 的 kill 语句比 gen 语句嵌套更深，则把 kill 上提到 gen 所在层的末尾，保证内存在正确的作用域边界释放。
4. **`PlanMemory`**：对每个 buffer 构造生命期区间 `[start, end)`，编译期已知大小的进入 **`LinearScanPack`** arena 打包；动态大小的顺序追加在 arena 末尾。最终得到 `buffer_byte_offsets_`（每个 buffer 在 arena 中的字节偏移）和总大小 `merged_alloc_size_`。

`LinearScanPack`（[merge_shared_memory_allocations.cc:743-793](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/merge_shared_memory_allocations.cc#L743-L793)）是经典的线性扫描分配器：区间按 `start` 排序，用最小堆维护活跃区间；扫描到某区间时先 `retire` 掉已结束的（归还 `FreeList`），再尝试用 `FreeList`（best-fit）回收空闲块，找不到才 bump arena top。对齐由 `AlignUpSize` 保证：

\[
\text{AlignUp}(v, a) = \begin{cases} v & a = 0 \\ v + (a - (v \bmod a)) \bmod a & \text{otherwise}\end{cases}
\]

最后 `SharedMemoryRewriter` 在 `thread_extent` 作用域开头**插一个** `Allocate(merged_buf_var, UInt(8), {merged_alloc_size_}, ...)`，并改写所有访问：buffer 的 `data` 指向 `merged_buf_var`，下标加上 `GetBufferOffset`（字节偏移除以元素字节数）。`tvm_access_ptr` 与 `ptx_cp_async` 这类直接拿指针的调用也有专门改写（[merge_shared_memory_allocations.cc:553-595](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/merge_shared_memory_allocations.cc#L553-L595)），保证 cp.async 目的地址正确折算成字节偏移。

容错：若 verbose 模式检测到常量区间在生命期重叠的同时内存也重叠（说明分析有误），会 fallback 到「顺序排列、不复用」的安全路径（[merge_shared_memory_allocations.cc:1286-1304](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/merge_shared_memory_allocations.cc#L1286-L1304)）。

#### 4.3.3 源码精读

文件头注释点明设计目标 [merge_shared_memory_allocations.cc:20-25](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/merge_shared_memory_allocations.cc#L20-L25)。区分动态/静态 shared 的判定函数在 [merge_shared_memory_allocations.cc:58-70](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/merge_shared_memory_allocations.cc#L58-L70)：动态是 `rank==kShared && tag==".dyn"`，静态是 `rank==kShared && tag.empty()`。

`enable_aggressive_merge` 的影响在 `SharedMemLinearAccessPatternFinder` 的三处访问记录点（[merge_shared_memory_allocations.cc:166-172](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/merge_shared_memory_allocations.cc#L166-L172) 的写、[merge_shared_memory_allocations.cc:212-224](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/merge_shared_memory_allocations.cc#L212-L224) 的读、[merge_shared_memory_allocations.cc:236-245](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/merge_shared_memory_allocations.cc#L236-L245) 的直接引用）：

```cpp
auto enable_aggressive_merge = enable_aggressive_merge_;
if (enable_aggressive_merge) {
  scope_[scope_.size() - 1].touched.push_back(buf);  // 记到最内层
} else {
  scope_[it->second.level].touched.push_back(buf);   // 记到分配层
}
```

开启 aggressive 时，访问被归到最内层作用域，使得生命期窗口更短、复用更激进。注意 [phase.py:48-57](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/phase.py#L48-L57) 里有一个重要 workaround：**启用 warp 特化时强制关闭 aggressive merge**，因为不同 warp 可能访问不同 buffer，流水线下的活跃分析很难保证正确。

对齐规划在 `SharedMemoryAlignmentPlanner`（[merge_shared_memory_allocations.cc:347-407](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/merge_shared_memory_allocations.cc#L347-L407)），关键一行：

```cpp
const int alignment = TargetIsHopper(target) ? 1024 : 16;
```

这是因为 Hopper 的 TMA/wgmma/tcgen05 描述符对 shared memory 起始地址有 1024 字节对齐的硬性要求。

单一 arena 分配的插入点在 `VisitStmt_(AttrStmtNode)`（[merge_shared_memory_allocations.cc:444-486](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/merge_shared_memory_allocations.cc#L444-L486)）：在第一个 `thread_extent` 处（`!allocated_`）插入合并后的 `Allocate`，并把 `allocated_` 置真避免重复。

pass 注册与 config 读取在 [merge_shared_memory_allocations.cc:1358-1381](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/merge_shared_memory_allocations.cc#L1358-L1381)：`enable_aggressive_merge` 由调用方（`phase.py`）传入，`merge_static_smem` 与 `tl.debug_merge_shared_memory_allocations`（verbose 日志）从 PassContext 读取。

#### 4.3.4 代码实践

**实践目标**：对比开关 `tl.enable_aggressive_shared_memory_merge` 时生成的 CUDA 源码中 shared memory 分配的差异，估算占用变化。这是本讲的**主实践**。

1. 基于 [examples/quickstart.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/quickstart.py) 写两版编译，仅 pass config 不同：

```python
# 示例代码：对比 aggressive merge 开关
import tilelang, tilelang.language as T

def build(aggressive):
    configs = {
        tilelang.PassConfigKey.TL_ENABLE_AGGRESSIVE_SHARED_MEMORY_MERGE: aggressive,
        # 可选：打印 arena 规划日志（DLOG，需 DEBUG 级别）
        tilelang.PassConfigKey.TL_DEBUG_MERGE_SHARED_MEMORY_ALLOCATIONS: True,
    }
    @tilelang.jit(pass_configs=configs)
    def matmul(M, N, K, block_M=128, block_N=128, block_K=32,
               dtype=T.float16, accum_dtype=T.float32):
        @T.prim_func
        def kernel(A: T.Tensor((M, K), dtype), B: T.Tensor((K, N), dtype),
                   C: T.Tensor((M, N), dtype)):
            with T.Kernel(T.ceildiv(N, block_N), T.ceildiv(M, block_M),
                          threads=128) as (bx, by):
                A_shared = T.alloc_shared((block_M, block_K), dtype)
                B_shared = T.alloc_shared((block_K, block_N), dtype)
                C_local  = T.alloc_fragment((block_M, block_N), accum_dtype)
                T.clear(C_local)
                for ko in T.Pipelined(T.ceildiv(K, block_K), num_stages=3):
                    T.copy(A[by * block_M, ko * block_K], A_shared)
                    T.copy(B[ko * block_K, bx * block_N], B_shared)
                    T.gemm(A_shared, B_shared, C_local)
                T.copy(C_local, C[by * block_M, bx * block_N])
        return kernel
    return matmul(1024, 1024, 1024)

k_off = build(aggressive=False)
k_on  = build(aggressive=True)
print("=== aggressive=False ===")
print(k_off.get_kernel_source())
print("=== aggressive=True ===")
print(k_on.get_kernel_source())
```

2. 在两份 CUDA 源码里找到唯一的 `extern __shared__`（或 `__shared__`）声明，以及各 buffer 在其中的字节偏移（通常表现为 `buf_dyn_shmem + offset` 形式的指针运算）。
3. 估算总 shared memory 占用：`A_shared` 与 `B_shared` 生命期是否重叠决定了 aggressive 开关能否让它们复用同一段。对于本例的 `num_stages=3` 软件流水，`A_shared`/`B_shared` 在每个 K 迭代里同时活跃，aggressive 影响有限；若你把 kernel 改成两个生命期明显不重叠的 shared tile，差异会更显著。

**需要观察的现象**：
- 两版都只有**一个** shared memory 分配（合并成功）。
- 开启 aggressive 时，某些 buffer 的起始偏移可能变小（复用更紧凑），`merged_alloc_size_` 相应更小。
- Hopper 目标下，TMA 相关 buffer 起始偏移应是 1024 的整数倍。

**预期结果**：两版生成的 CUDA 源码中 shared memory 总字节数不同；具体数值「待本地验证」（依赖目标架构、是否走 TMA/warp 特化路径——注意 warp 特化下 aggressive 会被强制关闭）。

#### 4.3.5 小练习与答案

**练习 1**：为什么合并后的单一分配必须挂在 `thread_extent`（线程块）作用域开头，而不是某个内层循环里？
**答案**：shared memory 的生命期是整个线程块，且每个 device kernel 只允许一个动态 shared 分配；挂在块开头能覆盖所有访问。也正因合并点要在每个 device 函数入口，本 pass 必须排在 `SplitHostDevice` 之后——这条约束记录在 [phase.py:264-267](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/phase.py#L264-L267)。

**练习 2**：`LinearScanPack` 为什么要先尝试 `FreeList` 回收、再 bump arena top？
**答案**：先回收已结束生命期的空闲块（best-fit）能让 arena 更紧凑、总占用更小；只有找不到合适空闲块时才向后扩张 arena top，这是线性扫描寄存器分配的标准做法（[merge_shared_memory_allocations.cc:775-790](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/merge_shared_memory_allocations.cc#L775-L790)）。

**练习 3**：warp 特化开启时，aggressive merge 会被强制关闭，原因是什么？
**答案**：warp 特化下不同 warp group 可能并发访问不同 buffer，而软件流水让活跃分析很难精确判断哪些 buffer 可安全复用；强行激进合并可能让两个并发 warp 踩同一块 shared memory。为安全起见 [phase.py:52-56](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/phase.py#L52-L56) 直接关闭。

---

### 4.4 ThreadSync：shared 内存访问的屏障插入

#### 4.4.1 概念说明

shared memory 是线程块内所有线程共享的，但 CUDA 不保证线程间的执行顺序。如果一个线程写 shared、另一个线程读同一地址，中间没有同步就会数据竞争。TileLang 前端并不要求程序员手动写 `__syncthreads`，`ThreadSync` pass 会**根据 IR 中 shared/shared.dyn 的读写冲突自动插入 `tvm_storage_sync` 屏障**。

它对 `shared` 和 `shared.dyn` 分别跑一次（`phase.py` 里先 `"shared"` 再 `"shared.dyn"`），可选地对 `global` 跑一次（需开启 `tir.detect_global_barrier`，默认关闭）。注意它必须排在 `MergeSharedMemoryAllocations` **之后**：合并 pass 把所有 `shared.dyn` 访问重定向到同一个 buffer var，`ThreadSync` 的 planner 也专门把所有 `shared.dyn` 访问归并到同一个 var 上一起规划。

#### 4.4.2 核心流程

`ThreadSync` 由三个协作类完成（入口 `TileLangThreadSync`，[thread_storage_sync.cc:814-831](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/thread_storage_sync.cc#L814-L831)）：

1. **`ThreadSyncAfterWaitQueueInserter`**（预处理）：在所有 `async_wait_queue` 之后补一个 `tvm_storage_sync`。因为软件流水的 `async_wait` 之后共享内存可见性需要屏障，而主 planner 不懂异步语义，无法自动判断（详见 [thread_storage_sync.cc:453-473](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/thread_storage_sync.cc#L453-L473) 的长注释）。仅对 `shared`（无 tag）作用域执行。
2. **`TileLangThreadSyncPlanner`**（规划）：用仿真式冲突检测决定「在哪些语句之前需要插屏障」。核心 `Summarize` 维护未同步的 `reads`/`writes` 两个列表，逐条语句检查：当前读是否与历史写冲突（RAW）、当前写是否与历史读（WAR）或历史写（WAW）冲突；一旦冲突就标记在该语句前插屏障并清空历史。若处于循环内，还会判断是否有跨迭代依赖，决定把屏障插在循环外（若循环内无同 scope 读）还是循环体内第一条冲突语句前。
3. **`ThreadSyncInserter`**（插入）：遍历语句，凡 planner 标记处就插入一个 `tvm_storage_sync(scope)` 调用（即 `__syncthreads`）；对 global scope 则生成 `MakeGlobalBarrier`（协作组全局屏障）。
4. **`ThreadPartialSyncRewriter`**（部分线程优化）：若某屏障只有部分线程参与（如 warp 特化后只有部分 `threadIdx` 范围到达），把全块 `__syncthreads` 改写为**命名屏障**（`bar.arrive`/`bar.sync`，带 barrier_id 和参与线程数），减少不必要的等待。Hopper 提供 16 个硬件命名屏障，编号从 `kFirstUsedBarrier = 3` 起分配，ID 0 保留给 `__syncthreads`（见 [thread_sync_types.h:22-32](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/common/thread_sync_types.h#L22-L32)）。

冲突判定 `FindConflict`（[thread_storage_sync.cc:253-399](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/thread_storage_sync.cc#L253-L399)）有几个关键优化：

- **async-copy 写写不冲突**：两个 TMA/cp.async 写同一 buffer 不需要屏障（[thread_storage_sync.cc:259-262](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/thread_storage_sync.cc#L259-L262)）。
- **可证不交叠则不冲突**：用 `const_int_bound` 和符号证明（`CanProve(prev < curr)`）判断两次访问的下标区间是否在数学上不相交，若不相交则无需屏障。
- **double buffer 优化**：若前次是 double buffer 的写、本次是读且非循环携带，则不冲突（[thread_storage_sync.cc:389-391](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/thread_storage_sync.cc#L389-L391)）。

#### 4.4.3 源码精读

pass 注册在 [thread_storage_sync.cc:837-856](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/thread_storage_sync.cc#L837-L856)，受 `tl.disable_thread_storage_sync` 控制（默认不禁用）。

`TileLangThreadSyncPlanner::Summarize` 的主循环（[thread_storage_sync.cc:61-239](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/thread_storage_sync.cc#L61-L239)）开头会把所有 `shared.dyn` 访问重定向到同一个 buffer var（[thread_storage_sync.cc:64-77](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/thread_storage_sync.cc#L64-L77)），与 `MergeSharedMemoryAllocations` 的合并结果配套——合并后所有 `shared.dyn` 指针本就是同一个，统一规划才能正确判断冲突。

循环外提逻辑（[thread_storage_sync.cc:133-194](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/thread_storage_sync.cc#L133-L194)）：先判断循环体内是否有同 scope 的读；若无读（例如只有 `stmatrix` 写 `shared.dyn`），则把屏障外提到循环前，避免每迭代都同步。

`ThreadSyncInserter` 的 global barrier（[thread_storage_sync.cc:632-654](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/thread_storage_sync.cc#L632-L654)）会统计线程块数 `num_blocks_` 和「leader 线程」`is_lead_`，生成协作组级别的全局屏障；并在首个 thread scope 处插入 `tvm_prepare_global_barrier` / `tvm_global_barrier_kinit`（[thread_storage_sync.cc:612-631](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/thread_storage_sync.cc#L612-L631)）。

`ThreadPartialSyncRewriter::ProcessSharedSync`（[thread_storage_sync.cc:703-740](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/thread_storage_sync.cc#L703-L740)）：用 `const_int_bound` 推断 `tx/ty/tz` 的实际范围，若并非全线程参与，则按线程范围去重分配 `barrier_id`，生成带 `barrier_id` 与 `thread_count` 的 sync 调用；若参与线程数不是 32 的倍数则放弃（返回空语句，回退为不优化，[thread_storage_sync.cc:728-733](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/thread_storage_sync.cc#L728-L733)）。

#### 4.4.4 代码实践

**实践目标**：用 pass config 禁用 `ThreadSync`，观察生成的 CUDA 源码里 `__syncthreads` 的增减。

1. 同样基于 quickstart，编译两版：一版默认，一版设置 `tilelang.PassConfigKey.TL_DISABLE_THREAD_STORAGE_SYNC: True`。
2. 对比 `get_kernel_source()` 中 `__syncthreads()`（或 `tl::sync`）出现的次数。
3. 思考：禁用后程序还能正确吗？（提示：默认禁用 global sync，shared sync 仍是开的；若强制全关，多线程读写同一段 shared 会出现数据竞争。）

**需要观察的现象**：默认版在 `T.copy` 写 shared 与 `T.gemm` 读 shared 之间应有屏障；禁用版应缺失这些屏障。

**预期结果**：禁用版源码里 `__syncthreads` 数量显著减少甚至为 0，运行结果可能出错。具体「待本地验证」，且**不建议在实际 kernel 上禁用**，仅用于理解。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `ThreadSync("shared")` 和 `ThreadSync("shared.dyn")` 要分两次跑？
**答案**：两者是不同的 storage scope，各自独立做冲突分析；且 `MergeSharedMemoryAllocations` 把所有 `shared.dyn` 合并到一个 var 后，`shared.dyn` 的 planner 会把这些访问归并到同一 var 统一规划（[thread_storage_sync.cc:64-77](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/thread_storage_sync.cc#L64-L77)）。

**练习 2**：`ThreadSyncAfterWaitQueueInserter` 解决了什么主 planner 解决不了的问题？
**答案**：软件流水会在 `async_wait_queue` 之后访问 shared，但主 planner 不懂异步语义，无法判断这时需要屏障；因此专门在所有 `async_wait` 后无条件补一个 sync（[thread_storage_sync.cc:453-497](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/thread_storage_sync.cc#L453-L497)）。

**练习 3**：什么情况下全块 `__syncthreads` 会被改写成命名屏障？
**答案**：当 `ThreadPartialSyncRewriter` 通过 `const_int_bound` 推断出当前只有部分 `threadIdx` 范围（如 warp 特化的某个 warp group）会到达该屏障，且参与线程数是 32 的倍数时，改写为带 `barrier_id` 的命名屏障以减少不必要的跨 warp 等待（[thread_storage_sync.cc:703-740](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/thread_storage_sync.cc#L703-L740)）。

---

## 5. 综合实践

把本讲四个 pass 串起来，做一个**端到端的 shared memory 占用审计**小任务：

1. 选一个中等复杂的 kernel（建议用带 `num_stages=3` 软件流水的 matmul，或 [examples/flash_attention/](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/flash_attention/) 下的 attention）。
2. 开启 `tl.debug_merge_shared_memory_allocations`，把日志级别调到 DEBUG，编译并运行。日志会打印 arena 规划：每个 buffer 的 `start`/`end`/`alignment`/`offset`/`size`，以及 `Total Merged Size`（见 [merge_shared_memory_allocations.cc:1246-1255](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/merge_shared_memory_allocations.cc#L1246-L1255)）。
3. 在 `phase.py` 中临时把 `MergeSharedMemoryAllocations(...)` 注释掉，重新编译，观察 `get_kernel_source()` 里出现了多少个独立的 `__shared__`/`extern __shared__` 分配，以及 `ThreadSync` 是否还能正常工作（提示：合并 pass 也是 `shared.dyn` 访问归一化的来源）。
4. 恢复合并 pass，分别在 `aggressive=False/True` 下记录 `Total Merged Size`，结合日志中各 buffer 的生命期 `[start, end)` 解释：为什么某些 buffer 能复用同一段、某些不能。
5. 最后回答：在你的硬件上，该 kernel 的 shared memory 占用是否会成为 occupancy（占用率）的瓶颈？软件流水的 `num_stages` 增大时，合并后的 arena 大致如何增长？

**预期产出**：一张表，列出「合并前独立分配数 / 合并后 arena 字节数（aggressive off）/ 合并后 arena 字节数（aggressive on）」，并用生命期重叠关系解释差异。具体数值「待本地验证」。

## 6. 本讲小结

- **FlattenBuffer** 把多维 `Buffer` 拍平成一维物理内存、重写下标、按需提升索引为 int64、`bool`→`int8`，是所有下游存储 pass 的前提；它刻意不拍平函数参数 buffer_map。
- **StorageRewrite** 用线性化的活跃变量分析让生命期不重叠的临时缓冲区（主要是 local/global、部分静态 shared）复用存储；但一旦检测到动态 shared 或开启 `merge_static_smem`，就关闭复用、把 shared 合并让给下游专门 pass。
- **MergeSharedMemoryAllocations** 把一个 kernel 的多个 shared 分配合并成**单个** `UInt(8)` arena，用线性扫描 + best-fit free list 打包，Hopper 因 TMA/wgmma 要求 1024 字节对齐；`enable_aggressive_merge` 让访问归到最内层作用域以更激进复用，但 warp 特化下被强制关闭。
- **ThreadSync** 基于 shared/shared.dyn 的 RAW/WAR/WAW 冲突仿真自动插入 `__syncthreads`，支持循环外提、async-wait 后补屏障、global 协作屏障，以及部分线程参与时改写为硬件命名屏障。
- 这四个 pass 在 `OptimizeForTarget` 中有严格顺序：`FlattenBuffer → ... → StorageRewrite → ... → SplitHostDevice → MergeSharedMemoryAllocations → ThreadSync("shared")/("shared.dyn") → InjectPTXAsyncCopy`，顺序错乱会导致正确性或性能问题。
- 它们共享同一种思想：**线性化访问 + 活跃区间分析 + 区间打包/冲突检测**，这是理解整个存储/同步子系统的钥匙。

## 7. 下一步学习建议

- **回到上游上下文**：若想看这四个 pass 在整条 `OptimizeForTarget` 中的精确位置与相邻 pass（如 `ConfigIndexBitwidth`、`VectorizeLoop`、`InjectPTXAsyncCopy`），重读 [u3-l4 OptimizeForTarget](u3-l4-optimize-target.md)。
- **软件流水与本讲的耦合**：`MergeSharedMemoryAllocations` 与 `ThreadSync` 都要正确处理 `T.Pipelined` 产生的多缓冲与 `async_wait`，建议接着读 [u4-l2 软件流水线与异步拷贝](u4-l2-software-pipeline.md)，理解 `num_stages` 是如何放大 shared 分配数量、从而让本讲的合并 pass 变得至关重要的。
- **Warp 特化的交互**：本讲多次提到 warp 特化会关闭 aggressive merge、并触发命名屏障改写，配合 [u4-l3 Warp 特化与 Hopper wgmma](u4-l3-warp-specialization.md) 阅读会更立体。
- **直接读源码**：若要二次开发，重点文件是 `merge_shared_memory_allocations.cc`（arena 打包算法）和 `thread_storage_sync.cc`（冲突检测），它们都附带较详尽的注释；配置项集中在 [tilelang/transform/pass_config.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/transform/pass_config.py) 与 [src/op/builtin.h](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/builtin.h)。

# EmitContexts：内存分配与同步状态

## 1. 本讲目标

上一讲（u6-l2）我们看清了「一条 Tilus 指令如何被发射器（emitter）翻译成 Hidet IR」。但发射器本身「近乎无状态」——它在翻译指令时所需的**共享内存地址、barrier 编号、全局 workspace 指针、warp 内 leader lane、CTA 不变量表达式**等跨指令信息，都来自一个集中的状态容器：`EmitContexts`。

读完本讲，你应当能够：

1. 说出 `EmitContexts` 包含哪九个上下文，以及它们的「构造 → initialize → 遍历指令 → finalize」生命周期。
2. 理解共享内存分配器（`smem_alloc_ctx`）的 first-fit 分配、swizzle 对齐与「水位线」回写机制。
3. 掌握同步上下文（`sync_ctx`）如何按线程组规模分派到 `syncthreads / syncwarp / bar.sync / mbarrier`，以及 `barrier_alloc_ctx` 如何在共享内存里分配 mbarrier。
4. 了解全局内存 arena（`gmem_alloc_ctx`）、张量视图（`global_view_ctx`）与不变量追踪（`invariant_ctx`）如何协作。
5. 识别 `const_reg_ctx`、`leader_lane_ctx`、`tcgen05_ctx` 三类「优化与硬件专属」上下文的用途。

## 2. 前置知识

- **两层 IR 与发射器**（u6-l1、u6-l2）：Tilus IR 经 `generate_ir_module` 降级为 Hidet IR；每条指令由 `FunctionCodegen.visit_Instruction` 派单到注册表里的发射器。
- **线程组（thread group）**（u2-l3）：`ThreadGroupStmt` 把一段代码的执行权收窄到一段连续线程，`sync()` 只同步当前线程组。
- **共享内存 swizzle**（u4-l3）：`SharedLayout` 用 swizzle（base/bits/shift）把逻辑相邻元素打散到不同 bank，消除 bank conflict，TMA 搬运时要求基地址对齐到 swizzle 重复周期。
- **CUDA 同步原语**：`__syncthreads()`（全 CTA）、`__syncwarp()`（单 warp）、命名 `bar.sync`（barrier 0–15，其中 0 保留给 syncthreads）、`mbarrier`（异步内存屏障，存于共享内存）。
- **主机/设备函数分离**（u6-l1）：一个 Tilus 函数裂变为设备 `cuda_kernel` 与主机 `public` 启动函数，由 `LaunchKernelStmt` 衔接；某些上下文要在主机侧声明变量再作为 extra param 传入设备。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `python/tilus/backends/contexts/contexts.py` | `EmitContexts` 容器，创建九个上下文并编排 initialize/finalize |
| `python/tilus/backends/context.py` | `BaseEmitContext` 基类，提供跨上下文访问与 host/kernel 语句注入辅助方法 |
| `python/tilus/backends/contexts/smem_alloc_ctx.py` | 共享内存分配器与分配上下文（first-fit、swizzle 对齐、水位线回写） |
| `python/tilus/backends/contexts/sync_ctx.py` | 同步上下文：按线程组规模选择同步原语 |
| `python/tilus/backends/contexts/mbarrier_alloc_ctx.py` | mbarrier 分配上下文：在共享内存里放置 64-bit barrier |
| `python/tilus/backends/contexts/gmem_alloc_ctx.py` | 全局内存 arena 分配（clean/dirty 两套） |
| `python/tilus/backends/contexts/global_view_ctx.py` | 记录 `GlobalTensor` → `(ptr, dtype, layout)` 视图映射 |
| `python/tilus/backends/contexts/invariant_ctx.py` | 追踪 grid/block 不变量，供表达式重写 |
| `python/tilus/backends/contexts/const_reg_ctx.py` | 把 CTA 不变寄存器张量记录为「索引→表达式」闭式函数 |
| `python/tilus/backends/contexts/leader_lane_ctx.py` | 惰性创建每 warp 的 leader lane 谓词变量 |
| `python/tilus/backends/contexts/tcgen05_ctx.py` | Blackwell tcgen05 的 cta_group 一致性与 TMEM 生命周期追踪 |
| `python/tilus/backends/codegen.py` | `FunctionCodegen` 创建 `EmitContexts` 并在遍历指令前后调用 initialize/finalize |
| `python/tilus/backends/emitter.py` | `BaseInstEmitter.sync()` 转发到 `sync_ctx` |

## 4. 核心概念与源码讲解

### 4.1 EmitContexts 总览：九上下文与生命周期

#### 4.1.1 概念说明

代码生成是一个**有状态的遍历过程**：`FunctionCodegen` 自上而下访问 IR 语句树，每遇到一条 `InstStmt` 就派单给发射器。然而发射器需要的信息远不止「这条指令本身」：

- 这块共享内存放在动态共享内存的哪个偏移？
- 这段线程组的同步该用哪种 CUDA 原语？
- 这个全局 workspace 的基地址是多少？
- 这个寄存器张量其实是个 CTA 不变量，能不能不算数组而用算式代替？

这些问题都需要**跨指令累积的状态**。Tilus 把这些状态集中到一个容器 `EmitContexts` 里，每个职责对应一个 `BaseEmitContext` 子类。这样发射器就能保持「无状态」——它只读取/写入 `codegen.contexts`，自身不记跨指令信息。

#### 4.1.2 核心流程

```
FunctionCodegen.visit_Function
  ├── EmitContexts(codegen)          # 构造时创建 9 个上下文
  ├── contexts.initialize()          # 遍历指令前：seed 不变量、探测 gridDim/blockIdx
  ├── visit(func.body)               # 逐条指令派单给发射器，发射器读写 contexts
  └── contexts.finalize()            # 遍历指令后（逆序）：回写 smem 大小、声明 workspace、校验 TMEM
```

关键点是 **finalize 走逆序**。这是因为上下文之间存在依赖：`barrier_alloc_ctx.finalize` 要往共享内存里分配 mbarrier（调用 `smem_alloc_ctx`），所以必须在 `smem_alloc_ctx.finalize`（它负责把所有分配汇总成 `dynamic_smem_bytes`）**之前**执行。逆序正是为了保证「消费者先 finalize，汇总者后 finalize」。

#### 4.1.3 源码精读

[EmitContexts.__init__ 创建九个上下文：contexts.py:27-42](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/contexts/contexts.py#L27-L42)：构造函数只做一件事——按固定顺序实例化九个上下文，每个都传入 `codegen` 引用。顺序很重要，它决定了 `contexts()` 的返回顺序，也决定了 finalize 的逆序顺序。

```python
self.global_view_ctx   = GlobalTensorViewContext(codegen)
self.gmem_alloc_ctx    = GlobalMemoryAllocationContext(codegen)
self.invariant_ctx     = InvariantTrackingContext(codegen)
self.smem_alloc_ctx    = SharedMemoryAllocationContext(codegen)
self.tcgen05_ctx       = Tcgen05EmitContext(codegen)
self.barrier_alloc_ctx = BarrierAllocContext(codegen)
self.sync_ctx          = SyncContext(codegen)
self.const_reg_ctx     = ConstRegTensorEmitContext(codegen)
self.leader_lane_ctx   = LeaderLaneContext(codegen)
```

[contexts() 过滤出所有上下文：contexts.py:44-52](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/contexts/contexts.py#L44-L52)：用 `isinstance(ctx, BaseEmitContext)` 过滤掉 `codegen` 字段本身，只保留真正的上下文。`__dict__` 按 Python 字典插入序返回，所以列表顺序与构造顺序一致。

[initialize 正序 / finalize 逆序：contexts.py:54-68](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/contexts/contexts.py#L54-L68)：注意 `finalize` 用了 `reversed(...)`，这是全讲最关键的一行——它确立了跨上下文的 finalize 依赖方向。

[BaseEmitContext 基类与跨上下文访问：context.py:25-40](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/context.py#L25-L40)：每个上下文持有 `codegen`，并通过 `contexts` 属性（返回 `codegen.contexts`）拿到其它兄弟上下文——这是跨上下文调用的入口。基类还提供 `host_prepend/append`、`kernel_prepend/append`、`append_extra_param` 等注入语句的辅助方法，[initialize/finalize 默认空实现：context.py:91-103](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/context.py#L91-L103)。

[FunctionCodegen 中 contexts 的生命周期：codegen.py:94](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/codegen.py#L94) 创建容器，[codegen.py:236](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/codegen.py#L236) 与 [codegen.py:242](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/codegen.py#L242) 分别在 `visit(func.body)` 前后调用 initialize/finalize。

下表汇总九个上下文的职责与 finalize 是否回写：

| 上下文 | 职责 | initialize | finalize 产出 |
| --- | --- | --- | --- |
| `global_view_ctx` | 记录 GlobalTensor 视图 | — | — |
| `gmem_alloc_ctx` | 全局内存 arena 分配 | — | host 侧 request_workspace + extra param |
| `invariant_ctx` | grid/block 不变量追踪 | seed params/gridDim/blockIdx | — |
| `smem_alloc_ctx` | 共享内存分配 | — | 声明 shared_workspace、回写 dynamic_smem_bytes |
| `tcgen05_ctx` | tcgen05 cta_group 与 TMEM 生命周期 | — | 校验 TMEM 全部释放 |
| `barrier_alloc_ctx` | mbarrier 分配 | — | 在 smem 放置 mbarrier 并初始化 |
| `sync_ctx` | 同步原语分派 | — | — |
| `const_reg_ctx` | CTA 不变寄存器张量记录 | — | — |
| `leader_lane_ctx` | 每 warp leader lane 谓词 | — | 声明 is_leader_lane 变量 |

#### 4.1.4 代码实践

**实践目标**：在源码层面确认「finalize 走逆序」这一关键设计。

**操作步骤**：

1. 打开 `python/tilus/backends/contexts/contexts.py`，对照 `__init__`（L34–L42）的构造顺序，在纸上列出 9 个上下文的顺序。
2. 读 `finalize`（L62–L68），写出逆序后的调用次序。
3. 找到 `barrier_alloc_ctx.finalize`（`mbarrier_alloc_ctx.py` L41–L65）里调用 `self.contexts.smem_alloc_ctx.allocate_shared_tensor` 的那行，再找到 `smem_alloc_ctx.finalize`（L151–L171）里 `builder.update_attrs(dynamic_smem_bytes=...)` 的那行。

**需要观察的现象**：在逆序 finalize 中，`barrier_alloc_ctx` 排在 `smem_alloc_ctx` **之前**。也就是说，mbarrier 先在共享内存里占了一块地，然后 `smem_alloc_ctx` 才汇总「水位线」——barrier 占用的共享内存被正确计入最终的 `dynamic_smem_bytes`。

**预期结果**：你会确认「如果 finalize 不是逆序，barrier 的共享内存就不会被计入总大小，动态共享内存申请就会偏小，运行时越界」——这正是逆序设计存在的原因。

#### 4.1.5 小练习与答案

**练习 1**：如果某个上下文既不需要 initialize 也不需要 finalize，它要做什么？
**答案**：什么都不用做。`BaseEmitContext` 的 `initialize`/`finalize` 默认是空实现（`context.py` L91–L103），子类只需覆盖自己关心的方法。

**练习 2**：为什么 `contexts()` 要用 `isinstance(ctx, BaseEmitContext)` 过滤？
**答案**：`EmitContexts.__dict__` 里除了九个上下文，还包含 `codegen` 字段本身（一个 `FunctionCodegen`，不是 `BaseEmitContext`）。过滤是为了在遍历 initialize/finalize 时跳过它。

### 4.2 共享内存分配：smem_alloc_ctx

#### 4.2.1 概念说明

GPU 的共享内存（shared memory / SRAM）是每个线程块私有的高速片上存储。Tilus 用**动态共享内存**（dynamic shared memory）：内核启动时按需申请一大块，所有 `SharedTensor` 都在这块连续空间里就地分配偏移。`SharedMemoryAllocationContext`（`smem_alloc_ctx`）就是这个分配器，它解决三个问题：

1. **空间复用**：`free_shared_tensor` 释放的块要能被后续分配重新利用（否则共享内存很快耗尽）。
2. **swizzle 对齐**：带 swizzle 的 SharedTensor 基地址必须对齐到 swizzle 重复周期，否则软件 swizzle 与硬件（TMA）swizzle 错位导致数据损坏。
3. **总量回写**：所有分配的「高水位」要汇总成 `dynamic_smem_bytes`，回写给内核启动配置。

#### 4.2.2 核心流程

```
AllocateSharedInst ──► emitter ──► smem_alloc_ctx.allocate_shared_tensor(tensor, nbytes)
                                        │  1) 算 swizzle 对齐 alignment
                                        │  2) allocator.allocate(nbytes, alignment)  # first-fit
                                        │  3) 记录 tensor→addr
                                        ▼
                            dynamic_shared_memory(byte_offset=addr)  # 生成 Hidet 取址语句
FreeSharedInst   ──► emitter ──► smem_alloc_ctx.free_shared_tensor(tensor)
                                        │  allocator.free(addr)  # 合并相邻空闲块
                                        ▼
...... 遍历结束 ......
smem_alloc_ctx.finalize()
   ├── maximum_allocated = max(所有分配的高水位, barrier 占用)
   ├── 若有 shared_workspace：对齐后加上 workspace_bytes
   ├── 声明 shared_workspace 变量（kernel_prepend）
   └── builder.update_attrs(dynamic_smem_bytes=maximum_allocated)   # 回写给启动
```

分配采用 **first-fit（首次适配）**：维护一个空闲区间列表 `free_slots`，从前往后找第一个能放下（且满足对齐）的区间，切分出所需部分，剩余归还。

#### 4.2.3 源码精读

[SharedMemoryAllocator：first-fit 分配：smem_alloc_ctx.py:28-57](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/contexts/smem_alloc_ctx.py#L28-L57)：核心是 `allocate`。先把 `nbytes` 向上对齐到 `alignment`，再遍历 `free_slots` 找第一个 `aligned_start + nbytes <= end` 的区间，切分前后残余并重新排序。`maximum_allocated` 记录历史最高占用地址，`allocated` 记录当前在用量。

```python
def allocate(self, nbytes, alignment=128):
    nbytes = (nbytes + alignment - 1) // alignment * alignment
    for i, (start, end) in enumerate(self.free_slots):
        aligned_start = (start + alignment - 1) // alignment * alignment
        if aligned_start + nbytes <= end:
            break
    ...
    self.maximum_allocated = max(self.maximum_allocated, addr + nbytes)
```

[free：合并相邻空闲块：smem_alloc_ctx.py:59-84](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/contexts/smem_alloc_ctx.py#L59-L84)：释放时分四种情况——与前块相邻、与后块相邻、前后都相邻（三合一）、孤立成新块。合并是空间复用的关键，避免碎片化导致后续分配失败。

[allocate_shared_tensor：按 swizzle 决定对齐：smem_alloc_ctx.py:99-104](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/contexts/smem_alloc_ctx.py#L99-L104)：先取对齐，再分配，再登记 `tensor→addr`。

[_get_swizzle_alignment：对齐到 swizzle 重复周期：smem_alloc_ctx.py:106-136](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/contexts/smem_alloc_ctx.py#L106-L136)：当 SharedLayout 带 swizzle 时，TMA 硬件以**绝对共享内存地址**为基准施加 swizzle；若基地址未对齐到 swizzle 重复边界，软件 swizzle（作用于局部元素偏移）与硬件 swizzle 之间会出现错位。重复周期（字节）为：

\[
\text{repeat\_bytes} = 2^{\,(\text{base} + \text{bits} + \text{shift})} \times \text{dtype.nbytes}
\]

文档注释给出了典型值（32B/64B/128B swizzle 分别对应 256/512/1024 字节周期），最小不低于 128 字节。

[finalize：回写 dynamic_smem_bytes：smem_alloc_ctx.py:151-171](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/contexts/smem_alloc_ctx.py#L151-L171)：把高水位 `maximum_allocated` 作为动态共享内存总量。这里有个**容易踩坑的修正**：若存在 `shared_workspace`，要从「对齐到 128 字节后的 workspace_offset」起算 arena 大小，而不是直接用可能未对齐的高水位——否则对齐填充那段没被预留，workspace 尾部会越过申请的动态共享内存边界。

```python
maximum_allocated = workspace_offset + self.shared_workspace_bytes
...
self.codegen.builder.update_attrs(dynamic_smem_bytes=maximum_allocated)
```

这个 `dynamic_smem_bytes` 最终在 [launch_kernel 里被读取并校验是否超限：codegen.py:160-161](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/codegen.py#L160-L161)，超过 `shared_memory_per_block` 会直接抛错。

#### 4.2.4 代码实践

**实践目标**：用 `dump_ir` 观察一个使用了共享内存的内核，确认 `dynamic_smem_bytes` 与共享内存分配的对应关系。

**操作步骤**：

1. 选一个会用共享内存的例子，例如 `examples/matmul/matmul_v2.py`（Ampere，含共享内存分块）。
2. 在脚本开头设置：
   ```python
   import tilus
   tilus.option.debug.dump_ir()
   tilus.option.cache_dir("u6l3-cache")
   ```
3. 运行后进入缓存目录 `u6l3-cache/programs/<hash>/`，找到生成的 `source.cu`。
4. 在 `source.cu` 末尾找到内核启动语句，看 `shared_mem` 参数（即 `dynamic_smem_bytes`）的值。
5. 在内核体里找到 `extern __shared__` 或 `dynamic_shared_memory` 生成的指针声明，确认其偏移布局。

**需要观察的现象**：`shared_mem` 的字节数等于（或略大于）所有 `SharedTensor` 的高水位（含对齐填充）。如果内核同时分配并释放了多个 SharedTensor（分块循环里复用），`shared_mem` 反映的是**同时存活**的最大占用，而非累计分配量。

**预期结果**：你能把 `source.cu` 里 `extern __shared__` 的总大小与 `smem_alloc_ctx.maximum_allocated` 对应起来。实际运行依赖本地 GPU（Ampere 或更高），若无合适 GPU 可改为纯源码阅读：对照 `smem.py` 的 `AllocateSharedInstEmitter` 与 `smem_alloc_ctx.allocate_shared_tensor`，手动累加各 SharedTensor 的 `storage_nbytes`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 first-fit 分配后要对 `free_slots` 排序？
**答案**：`free` 时可能把残余块 `append` 到列表末尾，导致列表无序；排序保证下一轮 `allocate` 仍按地址升序扫描，使分配结果紧凑、碎片少。

**练习 2**：一个 SharedTensor 的 swizzle 是 128B 模式（repeat_bytes=1024），它的基地址会被对齐到多少？
**答案**：对齐到 1024 字节（`_get_swizzle_alignment` 返回 `repeat_bytes`，因为 1024 ≥ 128）。

### 4.3 同步上下文：sync_ctx 与 barrier_alloc_ctx

#### 4.3.1 概念说明

`sync()` 看似简单，实则要根据**当前线程组的规模**选择不同的 CUDA 同步原语：

- 全 CTA → `__syncthreads()`
- 单线程 → 无需同步
- 恰好一个 warp → `__syncwarp()`
- warp 内子集 → `bar.warp.sync(membermask)`
- 多 warp 且 32 的倍数 → 命名 `bar.sync`（barrier 1–4）
- 其它不规则情况 → `mbarrier`（异步屏障，存于共享内存）

`SyncContext` 负责这个分派；而 `mbarrier` 本身需要共享内存空间，由 `BarrierAllocContext`（`barrier_alloc_ctx`）统一分配。两者是「同步策略」与「同步资源」的分工。

#### 4.3.2 核心流程

```
SyncThreadsInst ──► SyncThreadsEmitter.sync()
                        │  contexts.sync_ctx.sync()
                        ▼
                   读 thread_group_stack 当前 [begin, end]
                   ┌─ 全 CTA? ─────────────► syncthreads()
                   ├─ 1 线程? ─────────────► None（不同步）
                   ├─ 恰好 1 warp? ────────► syncwarp()
                   ├─ warp 内 <32 子集? ───► bar_warp_sync(membermask)
                   ├─ 不 warp 对齐 / 非 32 倍数? ─► mbarrier（fallback）
                   └─ 多 warp 且 32 倍数? ─► 命名 barrier（名额 1..4）或 mbarrier

mbarrier 需要 smem ──► barrier_alloc_ctx.allocate_barriers(counts=[thread_count])
                        │  记录 counts / barriers（延迟到 finalize 真正放 smem）
                        ▼
                   finalize(): smem_alloc_ctx.allocate_shared_tensor(...)
                              单线程 init 每个 mbarrier(arrive_count)
                              fence_mbarrier_init_cluster() + syncthreads()
```

#### 4.3.3 源码精读

[MAX_NAMED_BARRIERS：sync_ctx.py:28](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/contexts/sync_ctx.py#L28)：命名 barrier 只有 1..4 可用（barrier 0 保留给 `__syncthreads`），共 4 个名额。

[sync() 分派：sync_ctx.py:54-86](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/contexts/sync_ctx.py#L54-L86)：读取 `thread_group_stack` 当前线程区间，按规模逐级判断。值得注意两个细节：(1) warp 未对齐起点或非 32 倍数的线程组只能用 mbarrier（命名 `bar.sync` 要求 warp 对齐）；(2) 命名 barrier 名额用尽（`next_barrier_id > 4`）时回退到 mbarrier。

```python
if thread_begin == 0 and thread_end == total_threads:
    return syncthreads()                 # 全 CTA
elif current_threads == 1:
    return None                          # 单线程，不同步
...
barrier_id = self._try_allocate_named_barrier(thread_begin, thread_end)
if barrier_id is not None:
    return bar_sync_aligned(barrier_id=barrier_id, thread_count=current_threads)
else:
    return self._mbarrier_sync(...)       # 名额用尽，回退 mbarrier
```

[_mbarrier_sync：跨上下文申请 mbarrier：sync_ctx.py:88-95](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/contexts/sync_ctx.py#L88-L95)：mbarrier 的地址来自 `barrier_alloc_ctx`，并用 `thread_group_barrier` 字典缓存，避免同一线程组重复分配。

[BarrierAllocContext.allocate_barriers：mbarrier_alloc_ctx.py:67-85](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/contexts/mbarrier_alloc_ctx.py#L67-L85)：每个 mbarrier 是 64-bit 结构，按 `base_addr + i*8` 连续排列；`counts` 记录每个 barrier 的到达线程数，`barriers` 记录地址变量——真正放到共享内存的动作延迟到 `finalize`。

[BarrierAllocContext.finalize：在 smem 放置并初始化 mbarrier：mbarrier_alloc_ctx.py:41-65](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/contexts/mbarrier_alloc_ctx.py#L41-L65)：这里出现了第一个**跨上下文依赖**——`self.contexts.smem_alloc_ctx.allocate_shared_tensor(tensor, ...)`。注意它把 mbarrier 作为一个 `uint64` SharedTensor 分配，因此 mbarrier 的共享内存消耗被计入 `smem_alloc_ctx.maximum_allocated`（依赖 4.1 讲到的逆序 finalize）。初始化只让 `threadIdx.x == 0` 的线程执行（`mbarrier_init_shared` + `arrive_count`），随后 `fence_mbarrier_init_cluster()` 让集群可见，再 `syncthreads()` 确保所有线程看到已初始化的 barrier。

[BaseInstEmitter.sync() 转发：emitter.py:62-65](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitter.py#L62-L65)：发射器侧的 `sync()` 只是把 `sync_ctx.sync()` 的返回值（可能为 `None`）追加到当前语句流。

#### 4.3.4 代码实践

**实践目标**：理解 `sync()` 在不同线程组下生成不同 CUDA 语句。

**操作步骤**：

1. 打开 `python/tilus/backends/contexts/sync_ctx.py`，通读 `sync()`（L54–L86）。
2. 假设一个内核有 4 个 warp（128 线程），构造以下三种线程组场景，预测 `sync()` 的返回：
   - 线程组 `[0, 128)`（全 CTA）
   - 线程组 `[0, 32)`（一个 warp）
   - 线程组 `[0, 64)`（两个 warp，warp 对齐）
3. 检查命名 barrier 分配：第 3 种场景会消耗 1 个命名 barrier 名额；若再出现 `[64, 128)`、`[0, 96)`、`[0, 128)` 四种不同的线程组，第 5 个会怎样？

**需要观察的现象**：前两种场景分别生成 `__syncthreads()` 与 `__syncwarp()`；第三种生成 `bar.sync %n, 64`。当不同线程组的种类超过 4 种时，`_try_allocate_named_barrier` 返回 `None`，回退到 mbarrier。

**预期结果**：你应能口头复述「全 CTA / 单 warp / 命名 barrier / mbarrier」四级 fallback 的判定顺序。运行验证依赖本地 GPU；若无 GPU，可在 `tests/` 中检索使用 `thread_group` 的测试，对照其生成的 `source.cu` 阅读同步语句（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 warp 未对齐起点（如 `[4, 36)`）不能用命名 `bar.sync`？
**答案**：`bar.sync` 要求参与的线程按 warp 对齐。`sync()` 在 `thread_begin % 32 != 0` 时直接走 mbarrier 分支（`sync_ctx.py` L69–L71）。

**练习 2**：`BarrierAllocContext.finalize` 里为什么 init 只在 `threadIdx.x == 0` 执行？
**答案**：mbarrier 的初始化（设置 `arrive_count`）只需做一次，否则多线程并发写会破坏 barrier 状态。用单个线程初始化后，再 `fence + syncthreads` 让全 CTA 看到一致状态。

### 4.4 全局内存与视图：gmem_alloc_ctx、global_view_ctx、invariant_ctx

#### 4.4.1 概念说明

这三者处理**全局内存（DRAM）**相关的状态：

- **`gmem_alloc_ctx`**：内核运行时可能需要临时全局内存 workspace（如中间结果太大放不下寄存器/共享内存）。它在主机侧申请一块 workspace，用 bump allocator 切分给各个 `AllocateGlobalInst`，再把基地址作为 extra param 传给设备。它区分 clean（要求清零）与 dirty（不要求）两套 arena。
- **`global_view_ctx`**：记录 `global_view` 创建的 `GlobalTensor` 视图——把 `(指针, dtype, layout)` 三元组缓存起来，供 TMA 等需要 layout 信息的发射器使用。
- **`invariant_ctx`**：追踪哪些变量在整个 grid 或 block 内不变（如函数参数、`gridDim`、`blockIdx`），用于把表达式重写为「grid-invariant」形式——这对 TMA 描述符的构建至关重要（TMA 要求 offset 只依赖 grid-invariant 变量，见 u4-l3）。

#### 4.4.2 核心流程

```
主机侧（finalize）：
  gmem_alloc_ctx.finalize()
    └── request_cuda_workspace(nbytes, require_clean)  # 申请 workspace
        + host_prepend(declare base_ptr)               # 放在主机函数最前
        + append_extra_param(base_ptr)                 # 传给设备内核

设备侧（遍历指令时）：
  AllocateGlobalInst ──► gmem_alloc_ctx.allocate_global_memory(nbytes, clean)
                            └── bump: ret = base + allocated; allocated += nbytes
  GlobalViewInst     ──► global_view_ctx.add_tensor_view(tensor, ptr, layout)
  LetStmt            ──► invariant_ctx.bind(var, value)   # codegen.visit_LetStmt 触发
```

#### 4.4.3 源码精读

[GlobalMemoryAllocationContext：两套 arena：gmem_alloc_ctx.py:28-47](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/contexts/gmem_alloc_ctx.py#L28-L47)：这是经典的 **bump allocator（碰撞分配）**——维护一个递增的 `allocated` 偏移，每次分配返回 `base + allocated` 再把 `allocated` 加上对齐后的字节数。clean 与 dirty 分开两个 arena，避免为不需要清零的分配付出 `cudaMemset` 代价。

```python
def allocate_global_memory(self, nbytes, clean):
    nbytes = (nbytes + 127) // 128 * 128   # 对齐到 128 字节
    if clean:
        ret = self.gmem_clean_base_ptr + self.gmem_clean_allocated
        self.gmem_clean_allocated = self.gmem_clean_allocated + nbytes
    ...
```

[finalize：主机侧申请并传参：gmem_alloc_ctx.py:49-67](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/contexts/gmem_alloc_ctx.py#L49-L67)：遍历指令结束后才知道总需求量。它调用 `request_cuda_workspace`（运行时 workspace），用 `host_prepend` 把 `base_ptr` 声明放到主机函数最前面，再用 `append_extra_param` 把它加入内核参数列表——这样设备内核就能直接用这个指针。

[GlobalTensorViewContext：视图缓存：global_view_ctx.py:33-41](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/contexts/global_view_ctx.py#L33-L41)：`tensor2view` 把 `GlobalTensor` 映射到 `(ptr, dtype, layout)`。TMA 发射器构建张量描述符时需要 `layout`（特别是 swizzle 与 shape），这就是缓存的用途。

[InvariantTrackingContext.bind：分类变量不变性：invariant_ctx.py:82-96](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/contexts/invariant_ctx.py#L82-L96)：当 `codegen.visit_LetStmt` 绑定一个变量时（[codegen.py:394-399](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/codegen.py#L394-L399)），调用 `bind`：用 `collect(value, Var)` 找出表达式用到的所有变量，若它们都已是 grid/block 不变量，则新变量也升级为不变量，并记录 `var2expr`（展开后的表达式）。这是抽象解释式的传播。

```python
for v in used_vars:
    if v not in self.block_invariants: is_block_invariant = False
    if v not in self.grid_invariants:  is_grid_invariant  = False
if is_block_invariant or is_grid_invariant:
    self.var2expr[var] = rewrite(value, self.var2expr)
```

[rewrite_to_grid_invariant：invariant_ctx.py:98-103](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/contexts/invariant_ctx.py#L98-L103)：若表达式所有变量都是 grid 不变量，则用 `var2expr` 重写后返回；否则报错。TMA 描述符的 offset 必须是 grid-invariant，此方法即做这个校验与重写。

#### 4.4.4 代码实践

**实践目标**：追踪 `AllocateGlobalInst` 如何在主机侧申请 workspace 并传给设备。

**操作步骤**：

1. 读 `gmem_alloc_ctx.py` 的 `allocate_global_memory`（L35–L47）与 `finalize`（L49–L67）。
2. 读 `gmem.py` 的 `AllocateGlobalInstEmitter`（约 L33–L42）：它调用 `ctx.allocate_global_memory(...)` 得到 `ptr`，并 `assign` 给张量变量。
3. 思考：为什么分配发生在遍历指令时（设备侧），而 workspace 申请发生在 finalize（主机侧）？

**需要观察的现象**：遍历时只记录偏移（bump），不真正申请；finalize 汇总两套 arena的总量后一次性 `request_cuda_workspace`。因为只有遍历完所有指令才知道总共需要多大 workspace。

**预期结果**：你应能解释「延迟申请」的合理性——提前申请会浪费，事后申请又来不及传参，所以用 bump 记账 + finalize 统一申请。这是源码阅读型实践，无需运行。

#### 4.4.5 小练习与答案

**练习 1**：clean 与 dirty 两套 arena 为什么不合并？
**答案**：合并后，只要有一个分配要求 clean，整块 workspace 都得清零，浪费带宽。分开后只有 clean arena 付出清零代价。

**练习 2**：`invariant_ctx` 在 `initialize` 阶段 seed 了哪些变量？
**答案**：grid 不变量包括所有函数参数 + `gridDim.{x,y,z}` + `this_cluster.dim_blocks/dim_threads`；block 不变量额外包括 `blockIdx.{x,y,z}`（`invariant_ctx.py` L43–L67）。

### 4.5 优化与硬件专属上下文：const_reg_ctx、leader_lane_ctx、tcgen05_ctx

#### 4.5.1 概念说明

剩下的三个上下文偏「优化」与「硬件专属」：

- **`const_reg_ctx`**：有些寄存器张量（如 barrier 地址）的元素其实是 CTA 不变量，可写成「索引→表达式」的闭式函数。与其物化成数组（可能被 nvcc 溢出到 local memory），不如直接用算式求值。本上下文登记这类张量，供特定发射器（如 `SliceRegisterInst`）绕过数组索引。
- **`leader_lane_ctx`**：许多 SASS 指令（TMA copy、tcgen05 MMA）是 warp 协作的，但 PTX 要求由单个线程发起。用 `if (elect)` 分支会让 ptxas 生成 `BSSY/BSYNC` 分支开销；本上下文提供预计算的 `is_leader_lane` 谓词，直接以 `@p` 谓词形式喂给内联汇编，避免分支。
- **`tcgen05_ctx`**：Blackwell 的第五代张量核。PTX 要求一个内核里**所有** tcgen05 指令使用同一个 `cta_group` 值，所以只让 `tcgen05.alloc` 指定一次、本上下文全局追踪；同时校验 TMEM 张量在内核结束前都被释放。

#### 4.5.2 核心流程

```
AllocBarrierInst ──► AllocBarrierInstEmitter
                       ├── barrier_alloc_ctx.allocate_barriers(...)   # 拿到地址
                       └── const_reg_ctx.register(out, axes=[i], expr=base + i*8)
                              # 登记：第 i 个元素 = base + i*8

SliceRegisterInst(切片 const_reg 张量) ──► const_reg_ctx.get_value(tensor, indices)
                                            └── rewrite(expr, {axes: indices})  # 直接算

warp 协作指令 ──► leader_lane_ctx.leader_lane  (惰性创建 is_leader_lane)
                    └── finalize: DeclareStmt(is_leader_lane, elect_one_sync())  # 放最外层

tcgen05.alloc(cta_group=2) ──► tcgen05_ctx.set_cta_group(2)  # 全局锁定
其它 tcgen05 指令          ──► tcgen05_ctx.get_cta_group()   # 读回 2
tcgen05.dealloc            ──► tcgen05_ctx.mark_tmemory_tensor_deallocate(...)
finalize: 若仍有未释放 TMEM ──► 抛 ValueError
```

#### 4.5.3 源码精读

[ConstRegTensorEmitContext.register/get_value：const_reg_ctx.py:61-104](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/contexts/const_reg_ctx.py#L61-L104)：`register` 记录 `(tensor, axes, expr)`；`get_value` 用 `rewrite(info.expr, {axes: logical_indices})` 把索引代入闭式表达式，返回标量值。文档注释明确：正常数组物化仍作为 fallback 发射，本上下文只是让特定发射器能**绕过**它。

[实际使用：AllocBarrierInstEmitter 登记 barrier 地址：fence.py:38-41](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/cuda/fence.py#L38-L41)：barrier 数组发射器在常规 `buffer_store` 之后，额外把 `value(i) = base_addr + i * uint64.nbytes` 登记进 `const_reg_ctx`。这样后续切片第 i 个 barrier 地址时，可直接算出而不必读数组。

[LeaderLaneContext.leader_lane：惰性创建：leader_lane_ctx.py:42-56](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/contexts/leader_lane_ctx.py#L42-L56)：`leader_lane` 是个 property，首次访问才创建 `Var("is_leader_lane", uint32)`。

[LeaderLaneContext.finalize：放最外层：leader_lane_ctx.py:58-64](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/contexts/leader_lane_ctx.py#L58-L64)：只有被访问过（`_leader_lane_var is not None`）才发射声明，用 `elect_one_sync()` 计算谓词，并通过 `kernel_prepend` 放到内核最外层作用域——保证所有线程在最开始就拿到一致的 leader 标记。

[Tcgen05EmitContext：cta_group 一致性：tcgen05_ctx.py:50-58](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/contexts/tcgen05_ctx.py#L50-L58)：`set_cta_group` 只接受首次设置；若后续 tcgen05 指令给出不同的 `cta_group`，直接报错——因为 PTX 不允许同一内核内 cta_group 不一致。

[Tcgen05EmitContext.finalize：TMEM 生命周期校验：tcgen05_ctx.py:68-76](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/contexts/tcgen05_ctx.py#L68-L76)：遍历结束后若 `allocated_tmemory_tensors` 非空（还有 TMEM 没释放），抛出带张量列表的 `ValueError`。这是一种编译期资源泄漏检查。

#### 4.5.4 代码实践

**实践目标**：理解 `const_reg_ctx` 如何把 barrier 地址数组转化为闭式算式。

**操作步骤**：

1. 读 `fence.py` 的 `AllocBarrierInstEmitter`（L26–L41），注意它既做了常规的 `buffer_store`（物化数组），又做了 `const_reg_ctx.register`（登记算式）。
2. 读 `const_reg_ctx.py` 的 `get_value`（L83–L104），理解 `rewrite(expr, {axis: index})` 的代换。
3. 假设一个 barrier 数组有 3 个元素，基地址为 `B`：写出第 0、1、2 个 barrier 地址的表达式。

**需要观察的现象**：第 i 个 barrier 地址 = `B + i * 8`（`uint64.nbytes == 8`）。使用 `const_reg_ctx` 后，读取第 i 个 barrier 不再是数组索引 `arr[i]`，而是直接 `B + i*8` 算术。

**预期结果**：你能解释为何这能避免 nvcc 把数组溢出到 local memory——因为根本没有数组，只有标量算式。源码阅读型实践，无需运行。

#### 4.5.5 小练习与答案

**练习 1**：`leader_lane_ctx.finalize` 为什么用 `kernel_prepend` 而不是 `kernel_append`？
**答案**：`is_leader_lane` 必须在使用它的指令之前声明并赋值。`kernel_prepend` 把声明放到内核最外层作用域最前面，保证后续 warp 协作指令能引用它。

**练习 2**：如果内核里先 `tcgen05.alloc(cta_group=1)`，后又 `tcgen05.alloc(cta_group=2)`，会发生什么？
**答案**：第二次 `set_cta_group(2)` 与已记录的 `1` 冲突，`tcgen05_ctx.set_cta_group` 抛 `ValueError`（`tcgen05_ctx.py` L54–L58）。

## 5. 综合实践

**任务**：追踪一条「store to global」在 Blackwell TMA epilogue 路径下如何流经 `smem_alloc_ctx` 与 `sync_ctx`，解释共享内存布局的确定与同步的插入时机。

**背景**：在 Blackwell 上，把累加器结果写回全局内存常走 TMA epilogue——寄存器结果先经 `store_shared` 写入共享内存（generic proxy），再由 `tma.shared_to_global` 经 async proxy 批量搬走。这条路径恰好同时用到本讲的两个核心上下文。

**步骤**：

1. **定位真实代码**。打开 [examples/blackwell_matmul/matmul_v1.py 的 epilogue：matmul_v1.py:101-113](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/blackwell_matmul/matmul_v1.py#L101-L113)：

   ```python
   self.store_shared(s_c, r_acc.to(float16))   # 寄存器 → 共享内存（generic proxy）
   self.fence.proxy_async(space="shared")       # fence.proxy.async.shared::cta
   self.sync()                                   # 确保写可见
   with self.single_warp():
       self.tma.shared_to_global(s_c, g_c, ...)  # 共享内存 → 全局（async proxy / TMA）
       self.tma.commit_group()
       self.tma.wait_group(n=0, read=True)
   self.sync()
   ```

2. **追踪 `s_c` 的共享内存布局来源**。`s_c` 是一个 `SharedTensor`，它的内存偏移在 `AllocateSharedInst` 被发射时由 `smem_alloc_ctx.allocate_shared_tensor` 决定（`smem.py` L30–L46 → `smem_alloc_ctx.py` L99–L104）。若 `s_c` 带 swizzle，其基地址会被 `_get_swizzle_alignment` 对齐到 swizzle 重复周期——这正是 TMA 正确施加硬件 swizzle 的前提。

3. **追踪两次 `sync()` 的语义**。对照 `sync_ctx.sync()`（L54–L86）：
   - 第一次 `self.sync()` 在 `single_warp()` 之外，作用于更宽的线程组（取决于上层 `thread_group_stack`），确保所有写过 `s_c` 的线程都已落盘到共享内存，TMA 才不会读到残影。
   - 第二次 `self.sync()` 在 `single_warp()` 块之后，等待 TMA 搬运完成、shared 缓冲可被复用。
   - 关键：`store_shared` 走 generic proxy，`tma.shared_to_global` 走 async proxy，**两者之间必须有 `fence.proxy.async.shared::cta`**（CLAUDE.md 明确强调），否则 TMA 引擎可能读到 stale 数据。

4. **解释「布局确定时机」与「同步插入时机」**：
   - **布局**：`s_c` 的共享内存偏移在「发射 `AllocateSharedInst` 时」确定（遍历指令过程中），总量在 `smem_alloc_ctx.finalize`（遍历结束后）汇总回 `dynamic_smem_bytes`。
   - **同步**：每次遇到 `SyncThreadsInst`（来自用户的 `self.sync()`），`SyncThreadsEmitter` 即时调用 `sync_ctx.sync()` 选择原语；同步语句被插入到**当前语句流位置**，保证 producer/consumer 顺序正确。

5. **（可选，待本地验证）** 若有 Blackwell（sm_100）GPU：设置 `tilus.option.debug.dump_ir()` 与 `cache_dir`，运行 `matmul_v1`，在缓存的 `source.cu` 中找到 `extern __shared__` 声明、`st.shared` 写入、`fence.proxy.async` 与 `cp.async.bulk`（TMA）语句，对照上述调用链。若无 Blackwell，则纯做源码阅读：把第 2、3 步的结论写在一份笔记里。

**预期产出**：一份说明，讲清「s_c 的共享内存地址何时由谁分配、为何要 swizzle 对齐」「两次 sync 各自同步哪个线程组、为何需要 proxy fence」。这把本讲的分配与同步两个模块串成了一个真实的端到端数据流。

## 6. 本讲小结

- `EmitContexts` 是代码生成的集中状态容器，包含 9 个 `BaseEmitContext` 子类；发射器自身无状态，所有跨指令信息从这里读写。
- 生命周期是「initialize（正序）→ 遍历指令 → finalize（逆序）」；**逆序 finalize** 保证消费者（如 `barrier_alloc_ctx` 先在 smem 占地）先于汇总者（`smem_alloc_ctx` 计算总量）执行。
- `smem_alloc_ctx` 用 first-fit 分配器管理动态共享内存，按 swizzle 重复周期对齐，finalize 把高水位回写为 `dynamic_smem_bytes`。
- `sync_ctx` 按线程组规模分派 `syncthreads / syncwarp / bar.warp.sync / 命名 bar.sync / mbarrier`；`barrier_alloc_ctx` 负责把 mbarrier 放进共享内存并单线程初始化。
- `gmem_alloc_ctx` 用双 arena（clean/dirty）的 bump 分配管理全局 workspace，延迟到 finalize 在主机侧统一申请并传参。
- `global_view_ctx`/`invariant_ctx` 缓存张量视图与不变量传播；`const_reg_ctx`/`leader_lane_ctx`/`tcgen05_ctx` 提供闭式算式、warp leader 谓词与 Blackwell cta_group 一致性等优化与校验。

## 7. 下一步学习建议

- **下一讲 u6-l4（通用发射器）**：本讲讲清了「状态住在哪里」，u6-l4 将讲清「发射器如何消费这些状态」——精读 `elementwise / reduce / ldst / shared_ldst` 如何把张量布局翻译成每线程的标量地址与操作。
- **延伸阅读**：对照 `examples/blackwell_matmul/matmul_v2.py` 与 `examples/hopper_matmul/matmul_v3.py`（u7-l4 软件流水线），看 `Pipeline` 如何反复触发 `smem_alloc_ctx` 的分配/释放与 `sync_ctx` 的同步，体会多级缓冲下分配与同步的配合。
- **动手验证**：尝试给某个发射器加一条调试日志，打印它从 `contexts` 取到的共享内存偏移或 barrier 编号，结合 `dump_ir` 的产物印证本讲的调用链。

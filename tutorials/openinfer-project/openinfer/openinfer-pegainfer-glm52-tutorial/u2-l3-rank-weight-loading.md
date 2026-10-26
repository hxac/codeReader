# u2-l3 rank 权重加载：mmap、staging 与 GPU 驻留

## 1. 本讲目标

上一讲（u2-l2）我们弄清了「加载什么」：`Glm52WeightManifest` 把 `model.safetensors.index.json` 解析成清单，`all_rank_load_bundles` 为每个 rank 切出一份 `Glm52RankLoadBundle`（计划），`expert_placement` 约定了每个专家张量在打包区域里的最终落位。本讲回答「怎么加载」：

1. 说清加载发生在**哪个线程**上、引擎如何异步驱动它（worker 线程与指令-响应协议）。
2. 读懂 pageable 路径的完整循环：mmap 分片 → 逐张量校验 → 直落位 H2D → 事件护栏管理 mmap 生命周期，并解释 `MMAP_EVENT_GROUP = 1`、`LIVE_MMAP_GROUPS = 16` 这两个分组常量针对的「大尾部回归」。
3. 读懂 staged 路径的 pinned 双缓冲：32 MiB × 2 槽、4 个常驻 fill 工作线程、`dma_done` 事件门，以及 pageable 与 staged 两种上传器的取舍（为何 staged 是 opt-in 而非默认）。
4. 弄清加载结束时的 `free_vram_bytes` 探测如何一路喂给启动期的显存预算（fleet 最小值 → 扣减 → `derive_max_model_len`）。

学完本讲，你应该能独立追踪「一个 safetensors 分片文件里的字节」直到「rank 上一块驻留 GPU 的 `CudaSlice<u8>`」的完整路径，并解释这条路径上每一个安全护栏存在的理由。

## 2. 前置知识

本讲涉及的底层数据通路概念，用三段通俗解释先建立直觉。

**mmap 与页缓存。** `mmap` 把文件「映射」进进程地址空间：读 `mmap` 出来的内存等于读文件内容，但第一次读之前并不真正占有内存——由操作系统按需把文件的**页**（page）从磁盘/网络文件系统调入**页缓存**（page cache）。好处是不用一次性把上百 GiB 的 checkpoint 读进内存；代价是首次触碰每页会产生缺页（page fault）。GLM5.2 的 checkpoint 一个 rank 分包约 100～190 GiB，只能走 mmap。

**pageable 与 pinned 内存。** CPU 侧普通分配（包括 mmap 出来的内存）是**可分页的**（pageable）：操作系统可以随时把它换出。GPU 的 DMA 引擎不能直接搬运随时可能消失的内存，所以对 pageable 源的 H2D 拷贝，驱动要先在内部暂存区（一块隐藏的 pinned 内存）里过渡一遍——这段「先暂存再 DMA」是 pageable 拷贝慢、且难以与 host 侧工作重叠的原因。**pinned**（页锁定）内存是用 `cudaHostAlloc` 之类接口申请的、操作系统承诺不换出的内存：DMA 引擎可以直接从它取数，`memcpy_htod_async` 入队后立刻返回，让「host 填充下一块」与「GPU DMA 上一块」真正并行。代价是 pinned 内存是稀缺资源，要省着用。

**CUDA stream 与 event。** stream 是 GPU 侧的指令队列：host 把 kernel/拷贝**异步入队**后立即返回，GPU 按序执行。event 是插在 stream 里的一个「里程碑」：`record` 之后 host 可以 `synchronize` 等它完成，从而得知「到这个里程碑为止的所有工作（包括某个 H2D 拷贝）都已经完成」。异步 H2D 有一个经典陷阱：**入队返回 ≠ 源数据已被消费**——如果 host 在 DMA 真正执行前把源内存（比如 mmap）释放/unmap，就是 use-after-free。本讲的 mmap 护栏与 `dma_done` 事件门都是在解决这一问题。

**双缓冲（double buffering）。** 一块缓冲在 DMA 时，另一块同时被 host 填充，两块轮换（ping-pong），让「填充」和「搬运」两个阶段流水线化。理想情况下总时间由两者中较慢的一个决定，而不是两者之和。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [src/runner.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/runner.rs) | worker 线程与 12 变体指令协议；`Glm52RankThreadState::load_weights` 是 rank 侧加载编排入口 |
| [src/weights/load.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/load.rs) | 加载主循环：mmap、逐张量校验、expert-major 直落位、mmap 事件护栏、pageable/staged 上传器分发 |
| [src/weights/staging.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/staging.rs) | `Glm52WeightStager`：pinned 双缓冲上传器与常驻 fill 线程池 |
| [src/weights/context.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/context.rs) | `Glm52RankGpuContext`：rank 的 CUDA context/stream 绑定、`free_vram_bytes` 探测 |
| [src/weights.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights.rs) | （上一讲已讲）清单与 bundle；本讲只用到它的 `mmap_file`、`expert_placement`、`expected_tensor_contract` 与 `retype_owned` |
| [src/lib.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs) | 引擎侧编排：spawn 全部 worker、发 `LoadWeights`、收集报告、取 fleet 最小空闲 VRAM 喂给 `derive_max_model_len` |
| [docs/models/glm52/ep4-gb300.md](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/docs/models/glm52/ep4-gb300.md) | GB300 带来的实测记录：staging 参数扫描、pageable/pinned A/B、冷启动回归（staging.rs 常量的出处） |

## 4. 核心概念与源码讲解

### 4.1 worker 线程与异步加载

#### 4.1.1 概念说明

CUDA context 不是自由共享的：一个 context 绑定到一个线程后，该线程的所有 CUDA 调用才落到对应设备。GLM5.2 的解法是**每 GPU 一个专职 worker 线程**，独占持有该 rank 的 `Glm52RankGpuContext`（context + stream 对）；引擎线程（可能同时驱动 8 个 rank）**从不直接碰 CUDA**，只通过 crossbeam 通道发指令、等响应。这就是 u1-l3 介绍过的 12 变体指令协议 `Glm52RankCommand`，本讲只关心其中的 `LoadWeights`。

为什么要异步？启动期引擎要同时驱动 fleet 里本进程 hosting 的所有 rank 并行加载——如果同步逐个加载，rank 0 加载完才开始 rank 1，启动时间会线性放大。

#### 4.1.2 核心流程

`LoadWeights` 阶段的完整时序：

```
引擎线程 (lib.rs)                         worker 线程 ×N (runner.rs)
────────────────                         ─────────────────────────
Glm52RankWorker::spawn(placement, bundle)
   │  创建 unbounded 命令通道 (tx→rx)
   │  创建 bounded(1) 启动通道
   │  ──── spawn ────────────────────►  Glm52RankGpuContext::new(ordinal)
   │                                     （创建并绑定 CUDA context、
   │                                       保留异步分配池、开 stream）
   │  ◄── startup_tx.send(Ok(())) ────  （失败则 send(Err) 并退出）
   │  recv() 确认启动成功
   │
   │  ── LoadWeights{path, topo,       rank_worker_loop 收到指令
   │       staging, resp} ──────────►   state.load_weights(...)
   │                                     ├─ load_rank_weights_to_gpu(...)
   │                                     ├─ (TP 拓扑) 第二趟切片加载
   │                                     ├─ ctx.free_vram_bytes() 探测
   │  ◄── resp.send(report) ──────────  └─ 组装 Glm52RankWeightLoadReport
   │  recv() 拿到 report，校验 rank 一致
```

加载完成后，权重以 `Glm52RankGpuWeights` 的形态驻留在 worker 线程的 `Glm52RankThreadState.loaded` 里，等 `BuildFixed` 指令来消费。

#### 4.1.3 源码精读

先看 worker 的诞生。每个 rank spawn 时校验「加载计划里的 rank 与 placement 的 rank 一致」，然后开两条通道：

[src/runner.rs:319-353](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/runner.rs#L319-L353) —— `Glm52RankWorker::spawn`：创建 unbounded 的命令通道与 `bounded(1)` 的启动通道，spawn 名为 `glm52-rank-{rank}` 的线程；线程体内先 `Glm52RankGpuContext::new(device_ordinal)`（失败即向启动通道报错退出），成功后 send `Ok(())` 并进入 `rank_worker_loop`。引擎侧在 `startup_rx.recv()` 上**阻塞等待启动握手**——这一步是同步的，确保 `spawn` 返回时每个 worker 的 CUDA context 已经就绪（或已经失败）。

[src/weights/context.rs:22-39](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/context.rs#L22-L39) —— `Glm52RankGpuContext::new`：创建 context、`bind_to_thread()` 把它绑到 worker 线程、`retain_async_alloc_pool`、关闭 event 追踪、开一条主 stream。其中 [src/weights/context.rs:120-144](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/context.rs#L120-L144) 的 `retain_async_alloc_pool` 把设备默认内存池的 `RELEASE_THRESHOLD` 设为 `u64::MAX`——让 stream allocator 归还的块**留在池内复用**而不还给驱动，避免反复 cudaMalloc/cudaFree 的抖动（加载期几万次 `alloc` 直接受益）。[src/weights/context.rs:16-19](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/context.rs#L16-L19) 用 `unsafe impl Send/Sync` 声明这一独占所有权模型。

引擎侧的异步接口与 worker 侧的分发：

[src/runner.rs:355-371](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/runner.rs#L355-L371) —— `load_weights_async`：创建 `bounded(1)` 响应通道，把 [src/runner.rs:205-211](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/runner.rs#L205-L211) 定义的 `Glm52RankCommand::LoadWeights { model_path, moe_topo, weight_staging, resp }` 发进命令通道，立刻返回响应 `Receiver`。指令枚举自带 `resp: Sender`，即「每条指令携带自己的回信信封」的模式。

[src/runner.rs:1043-1053](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/runner.rs#L1043-L1053) —— `rank_worker_loop` 对 `LoadWeights` 的分发：一行 `resp.send(state.load_weights(...))`，`Result` 整体回投，`let _ =` 表示接收端丢弃响应也不阻塞 worker。

worker 侧的加载编排在 `Glm52RankThreadState::load_weights`：

[src/runner.rs:635-712](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/runner.rs#L635-L712) —— ① 调 `load_rank_weights_to_gpu`（4.2 的主循环）；② 若是 TP 张量复制拓扑（`uses_tensor_replicated_moe`，即 tp4），bundle 本趟不含路由专家，走第二趟 `load_tp_slice_layer` 逐层加载本 rank 的 1/TP 切片（这正是 u2-l2 讲过的「TP4 首趟不带专家包」的兑现处，[src/runner.rs:644-686](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/runner.rs#L644-L686)）；③ 校验加载字节数与驻留字节数一致（L687-693）；④ **`ctx.free_vram_bytes()` 探测**（L694）并组装报告（L695-702）。

驻留数据的形态在 load.rs 里定义：

[src/weights/load.rs:119-123](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/load.rs#L119-L123) —— `Glm52RankGpuWeights`：非专家张量是「张量名 → `CudaSlice<u8>`」的 `BTreeMap`（**raw checkpoint 字节**，不做任何重排），专家是「层号 → `Glm52ExpertLayerRegions`（四个打包区域）」。消费端用 `take_tensor`/`take_expert_layer` **移出**（move），最后 [src/weights/load.rs:143-159](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/load.rs#L143-L159) 的 `ensure_consumed` 强制「驻留清单里的每个条目都必须被模型取走」——注释写明动机：留着不被消费的张量会白白花费 H2D 带宽和瞬态 HBM。类型化视图由消费方在构造时用 `retype_owned`（[src/weights.rs:668-684](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights.rs#L668-L684)）把字节缓冲零拷贝升级成 `CudaSlice<f32>` 之类。

引擎侧把以上汇成「全员并行加载」：

[src/lib.rs:1698-1719](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L1698-L1719) —— 引擎侧 `load_rank_weights_to_gpu`：对 `startup.ranks` 里的每个 hosted rank spawn 一个 worker（device ordinal 就是本进程的 GPU 序号）；[src/lib.rs:1732-1751](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L1732-L1751) —— 先对**所有** worker `load_weights_async`（全部指令入队，N 个 rank 同时开跑），再逐个 `recv()` 收报告并校验 `report.rank == rank && loaded_to_gpu`。注意 L1740-1741 的注释：多进程部署时 `report.rank` 是全局 fleet rank，而 `enumerate` 下标只是本进程的槽位，要用 `startup.ranks.start + local_index` 重算后再校验。

#### 4.1.4 代码实践

**实践目标**：亲手画出 `LoadWeights` 阶段的线程/消息时序图，验证你对「指令自带回信信封」协议的理解。

**操作步骤**（源码阅读型，无需 GPU）：

1. 打开 [src/runner.rs:319-353](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/runner.rs#L319-L353)，列出 spawn 过程中创建的**两条通道**及其容量（提示：`unbounded()` 与 `bounded::<Result<()>>(1)`）。
2. 打开 [src/lib.rs:1732-1751](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L1732-L1751)，注意「先全部 send、再逐个 recv」的两段式。
3. 画一张时序图：纵轴是时间，泳道是「引擎线程」+「worker-0」+「worker-1」，标出 startup 握手、`LoadWeights` 指令、`resp` 响应三类消息的发送方/接收方。

**需要观察的现象**（对照你画的图回答）：如果引擎是「send 给 worker-0 → recv worker-0 → send 给 worker-1 → recv worker-1」的串行写法，两个 rank 的加载会重叠吗？

**预期结果**：时序图中有 2 条 startup 消息（worker→引擎）+ 2 条 `LoadWeights`（引擎→worker）+ 2 条 report（worker→引擎），且两个 `LoadWeights` 在时间上先于任何一次 recv——这就是「全员入队、并行加载」的形状。串行写法则两个 rank 的加载完全不重叠，这正是两段式存在的原因。

**待本地验证**：如需运行时证据，可在有 GPU 的机器上启动并观察日志 `start spawn GLM5.2 rank workers` → `start load GLM5.2 rank weights` → `GLM5.2 rank weight load cost` 三条的时间戳关系。

#### 4.1.5 小练习与答案

**练习 1**：为什么命令通道是 unbounded，而 startup 通道是 `bounded(1)`？

**答案**：startup 通道只承载一次性的「context 创建成功/失败」握手，容量 1 恰好够用且能让引擎的 `recv()` 语义明确（一条消息、阻塞到 worker 就绪或死亡）。命令通道 unbounded 是因为引擎会在收响应之前连续发出多条指令（本讲的并行加载、以及后面每步的 `Step` 等指令序列），发送方永远不该因为通道满而阻塞——worker 是单线程串行消费，指令在通道里排队即可。

**练习 2**：`Glm52RankGpuWeights` 里的非专家张量为什么存 raw 字节（`CudaSlice<u8>`）而不是直接存类型化张量？

**答案**：加载器只对字节做搬运与落位，不解释内容；checkpoint 里 fp8 权重、bf16 权重、f32 scale 混杂，统一按 `u8` 搬运让「一个上传器服务所有 dtype」（staging.rs 的注释也点明了这一点）。类型化发生在消费端构造时——`take_tensor` 移出字节，`retype_owned` 按元素大小零拷贝升级，`ensure_consumed` 保证没有字节被搬上去却没人要。

**练习 3**：`Drop for Glm52RankWorker` 会做什么（[src/runner.rs:557-561](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/runner.rs#L557-L561)）？为什么 worker 循环退出时一定要打日志（[src/runner.rs:1122-1132](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/runner.rs#L1122-L1132)）？

**答案**：Drop 发送 `Shutdown` 并 join 线程，保证引擎对象析构时 worker 一定退出。打日志是因为 worker 的消亡是「承重」的：EP fleet 里一个 rank 静默消失会让所有对端卡死在下一次 collective 里（free-running 纪律的镜像），所以退出原因必须永远可见。

---

### 4.2 mmap 分组与 pageable 上传：加载主循环

#### 4.2.1 概念说明

主循环 `load_rank_weights_to_gpu` 处理的是这样的规模：EP8 每个 rank 约 16,260 个张量、103.6 GiB 逻辑载荷；EP4 每个 rank 30,660 个张量、188.0 GiB（数据来自 [ep4-gb300.md](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/docs/models/glm52/ep4-gb300.md) 的实测记录）。循环要同时满足四个约束：

1. **内存约束**：同一时刻只 mmap 少数分片文件，不能 190 GiB 全部常驻 host。
2. **生命周期约束**：H2D 拷贝是异步入队的，mmap 源必须活到拷贝真正完成为止。
3. **落位约束**（u2-l2 的结论）：专家张量必须在 H2D 时直接落进 expert-major 打包布局，因为约 85 GiB 的专家板放不下「先装进来再重排」的第二个副本。
4. **正确性约束**：每个打包区域每个字节都要被恰好写一次——一个「洞」意味着设备上的陈旧字节会静默变成模型权重。

「mmap 分组」指的是用 CUDA event 把「已提交拷贝的 mmap」分小批管理：这正是常量 `MMAP_EVENT_GROUP = 1`、`LIVE_MMAP_GROUPS = 16` 的职责，也是本讲学习目标里「大尾部回归」的出处。

#### 4.2.2 核心流程

```
for shard in bundle.shards:                    # 按分片文件逐个处理
    mmap = mmap_file(分片路径)                 # 只读映射该分片
    safetensors = SafeTensors::deserialize(mmap)
    for spec in shard.tensors:                 # 该分片里属于本 rank 的张量
        view = safetensors.tensor(spec.name)
        三关校验: dtype / shape / byte_len == expected_tensor_contract(name)
        if expert_placement(name, expert_range) 是 Some(p):
            按需 alloc 该层的四个打包区域（首次遇到该层时）
            上传 view → 区域内偏移 p.offset      # 直落位，无重排
            region_written[(p.layer, p.region)] += bytes   # 覆盖对账
        else:
            dst = alloc(bytes)                  # 独立 CudaSlice<u8>
            上传 view → dst
            tensors.insert(name, dst)
    drop(safetensors)
    mmap_guard.push_completed_shard(mmap)       # 移交护栏，事件封存生命周期
循环后:
    校验 张量数 == 计划数; 字节数 == 计划字节数
    校验 每个专家区域 written == region_bytes    # 无洞证明
    drain 护栏; drop 上传器; ctx.sync()           # 全部落定
    打出 weight load profile 日志
```

mmap 护栏的工作方式：

```
push_completed_shard(mmap):
    pending_mmaps.push(mmap)
    if pending 数 == MMAP_EVENT_GROUP(1):  record_pending_group()
        # 在 stream 上 record 一个 event，把这一组 mmap 与 event 绑定
    if 存活组数 == LIVE_MMAP_GROUPS(16):  wait_oldest_group()
        # synchronize 最老组的 event → 它的拷贝已全部完成 → 组可释放
```

即：每个 mmap 绑一个 event；最多同时保活 16 个 mmap；第 17 个到来时等最老的那个 event 完成、释放其映射。这样映射的「退休速度」与拷贝的完成速度同节奏。

#### 4.2.3 源码精读

先看两个分组常量及其注释——这是本讲最重要的一行注释：

[src/weights/load.rs:26-29](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/load.rs#L26-L29) —— `MMAP_EVENT_GROUP = 1`（每条 event 封存一个分片映射）、`LIVE_MMAP_GROUPS = 16`（最多 16 组同时存活）。注释说明动机：**一条 event 对应一个 mmap，让源生命周期显式化，同时避免「大量分片映射长期共存」时观察到的大尾部回归**。直觉解释：如果组太大（一条 event 封很多 mmap）或保活上限太高，会同时挂着几十个上百 GiB 分片的 VMA（虚拟内存区域）与页缓存引用——内核的映射管理压力、页缓存挤出与缺页行为都会退化，表现为加载的「尾部」（最后一批分片）显著变慢。`1 × 16` 的选择让每个映射在最老的 event 完成后立刻退役，把这个回归消掉。

上传器分发的骨架：

[src/weights/load.rs:30-49](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/load.rs#L30-L49) —— `enum WeightUploader { Pageable, Staged(Glm52WeightStager) }`，由 `--glm52-weight-staging` 决定（4.3 详解）。[src/weights/load.rs:51-77](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/load.rs#L51-L77) 的 `upload` 里，Pageable 分支（L59-72）做越界检查后在流上直接 `memcpy_htod(src, &mut view)`——src 是 mmap 出来的 pageable 内存，返回「1 次 H2D」计数。

主循环本体：

[src/weights/load.rs:204-209](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/load.rs#L204-L209) —— 逐分片：`mmap_file`（[src/weights.rs:686-690](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights.rs#L686-L690) 打开并只读映射）→ `CurrentShardMmap::new` 包装 → `SafeTensors::deserialize`。

[src/weights/load.rs:210-236](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/load.rs#L210-L236) —— 逐张量三关校验：dtype、shape、字节数都必须与 `expected_tensor_contract`（u2-l2 讲过的名字→契约推导）完全一致，任何一关不符立即报错。装载前的契约核对让「加载计划损坏」在拷贝之前就 fail-fast。

[src/weights/load.rs:238-263](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/load.rs#L238-L263) —— 专家分支：`expert_placement` 解析出 `(layer, region, offset)`；该层首次出现时才 `Glm52ExpertLayerRegions::alloc`（[src/weights/load.rs:89-104](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/load.rs#L89-L104)，四个区域各 `local_experts × stride` 字节，SAFETY 注释声明「每个字节恰好写一次，覆盖计数器兜底」）；`mmap.mark_stream_work()` 标记「这块映射已有流上工作引用它」；上传到区域内偏移并累加 `region_written`。

[src/weights/load.rs:264-281](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/load.rs#L264-L281) —— 非专家分支：独立 `alloc` 一个恰好大小的 `CudaSlice<u8>`，整块单次上传，插入 `tensors` 映射（重复名直接报错——计划里不该有重复）。

[src/weights/load.rs:286-292](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/load.rs#L286-L292) —— 分片处理完：drop safetensors 视图，把 mmap 移交护栏（`push_completed_shard`），并跟踪最慢分片。

收尾的四道闸门：

[src/weights/load.rs:295-320](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/load.rs#L295-L320) —— ① 张量数必须等于计划数；② 字节数必须等于计划字节数；③ **无洞证明**：遍历每个驻留专家层的四种区域，`region_written == region_bytes` 必须成立，注释写得很直白——「这里有个洞，意味着设备上的陈旧字节会静默变成模型权重」（新分配的 `CudaSlice` 不保证清零，洞里的旧字节如果没被写就拿来用，是最阴险的一类 bug）。

[src/weights/load.rs:321-350](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/load.rs#L321-L350) —— ④ drain 护栏、drop 上传器、最终 `ctx.sync()`，然后把加载画像打进日志：总时长、纯拷贝时长、同步时长、上传器类型、张量数、H2D 次数、字节数、专家层数、最慢分片。注意 `copy_secs` 的算法：总耗时减去所有同步耗时的正值——把「等 event/等流」与「真在搬运/反序列化」分开计量（ep4-gb300.md 里冷启动回归的定位就是靠「回归出现在 `mmap_deser_copy` 而非 `sync`」这一分辨）。

mmap 生命周期护栏的三件套：

[src/weights/load.rs:364-406](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/load.rs#L364-L406) —— `CurrentShardMmap`：持有当前分片的 mmap 与一个 `Cell<bool>` 的 `stream_work` 标志；每发起一次上传前 `mark_stream_work()`；若该包装在正常移交护栏**之前**被 drop（例如中途报错走 unwind），`Drop` 实现会先 `set_current + sync` 再放行——宁可同步一次，也不允许在流上拷贝未完成时 unmap。

[src/weights/load.rs:408-462](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/load.rs#L408-L462) —— `MmapH2dLifetimeGuard`：`push_completed_shard` 把 mmap 归组（组大小 `MMAP_EVENT_GROUP`），组满即 `record_pending_group`——在流上 record 一个 `CudaEvent`，把「这组 mmap」与「event 之前入队的所有拷贝」绑定；存活组数到 `LIVE_MMAP_GROUPS` 就 `wait_oldest_group`（[src/weights/load.rs:464-477](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/load.rs#L464-L477)，synchronize 最老 event 后弹出该组，mmap 随之析构）。[src/weights/load.rs:437-446](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/load.rs#L437-L446) 的 `drain` 在循环结束后清空所有残留组；[src/weights/load.rs:480-492](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/load.rs#L480-L492) 的 `Drop` 兜底：护栏析构时若还有未清组，同步后再放行。

#### 4.2.4 代码实践

**实践目标**：用 CPU 单测验证「打包落位与覆盖对账」这套纯逻辑，并手工复算一次对账。

**操作步骤**：

1. 运行 weights 模块的纯 CPU 单测（含 u2-l2 介绍过的 `expert_placement_matches_from_host_packing`、`resident_load_plans_exclude_validation_only_and_duplicate_tensors`）：

   ```bash
   cargo test --release -p pegainfer-glm52 --features glm52 --lib weights
   ```

   注意：虽然这些测试本身不碰 GPU，但 `--features glm52` 会连带构建 pegainfer-kernels 的 CUDA 内核，需要 nvcc 工具链（SM ≥ 10.0 的 Blackwell 目标）。无 GPU 机器上**待本地验证**，可退化为纯阅读步骤 2-3。

2. 手工复算对账：取 `model.layers.7.mlp.experts.33.gate_proj.weight`（EP8 rank 0，专家区间 0..32，本地专家号 33-32=1）。对照 [src/weights.rs:142-156](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights.rs#L142-L156) 写出它的 `(region, offset)`；再算出该层 `W13Weight` 区域的总字节数（`2 * 2048 * 6144 * 32` 个 fp8 字节）。

3. 回答：如果这个 rank 的加载计划里漏掉了 `experts.33.up_proj.weight`，主循环会在哪一行、以什么错误信息失败？

**需要观察的现象**：步骤 1 的测试全部绿色；步骤 2 的偏移落在 `[stride, stride + 2048*6144)` 区间（gate 在前、up 在后）。

**预期结果**：① `expert_placement("...experts.33.gate_proj.weight", 0..32)` = `(layer 7, W13Weight, offset = 1 * EXPERT_W13_WEIGHT_STRIDE)`，即 up 份额之前；`W13Weight` 区域 = 2 × 2048 × 6144 × 32 = 805,306,368 字节/层。② 漏掉 up_proj 时，`region_written[(7, W13Weight)]` 会比 `region_bytes` 少恰好 2048×6144 字节，[src/weights/load.rs:310-320](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/load.rs#L310-L320) 以 `"...expert region W13Weight incomplete: wrote X of Y bytes"** 拒绝启动。**待本地验证**（单测运行部分）。

#### 4.2.5 小练习与答案

**练习 1**：把 `LIVE_MMAP_GROUPS` 调大到 1024 会发生什么？调小到 1 呢？

**答案**：调到 1024 意味着最多 1024 个分片映射同时保活——GLM5.2 的分片数量级就在这个范围，等于几乎所有映射活到 drain 才释放，重新引入注释里说的「大量分片映射长期共存」的大尾部回归（VMA 与页缓存压力，尾部分片变慢），同时 host 侧 RSS 可能显著上涨。调到 1 则每次提交第二个分片就要同步等上一个 event——H2D 流水线被打断，拷贝与下一个分片的反序列化无法重叠，加载明显变慢。16 是「保住重叠、封顶映射数」的折中。

**练习 2**：pageable 分支的 `memcpy_htod` 已经把数据交给了驱动，为什么 `CurrentShardMmap::drop` 还要 `mark_stream_work` + sync 这一套？

**答案**：流上的 H2D 是异步语义：入队返回不代表设备已消费完源字节。Rust 的 `Mmap` 析构会 unmap 文件映射——若在拷贝真正完成前 unmap，拷贝读到的就是无效地址（use-after-unmap）。`mark_stream_work` 让「当前分片还没移交护栏就被 drop（错误路径 unwind）」的情形也有兜底：先 `sync` 等流排空再放行。护栏（正常路径）与 `Drop`（异常路径）共同把「源生命周期 ≥ 拷贝完成时刻」变成不可违反的构造性事实。

**练习 3**：为什么「无洞证明」（`region_written == region_bytes`）不能省略——alloc 之后立刻把区域清零不就好了？

**答案**：清零只能把洞从「陈旧字节」变成「零字节」，洞本身（该专家的权重缺失）依然存在，模型会用错误的权重静默出结果。对账的目的不是初始化，而是**证明计划里的每个张量都真的写进了它该在的位置**——它在加载失败时给出可定位的错误（哪层哪个区域缺多少字节），而不是在推理期产出错误 logits。这也是仓库「fail-fast 优先于静默容错」哲学在加载器里的体现。

---

### 4.3 staging：pinned 双缓冲上传器

#### 4.3.1 概念说明

pageable 路径有个结构性瓶颈：每次 `memcpy_htod` 都从 pageable 的 mmap 内存出发，驱动必须先把字节过渡进自己的暂存区，host 侧「读 mmap（缺页/页缓存命中）」与「DMA 上设备」两段无法流水线化。staged 路径把暂存区**搬到明面上**：rank 自己申请两块 32 MiB 的 pinned 缓冲，第 N 块在 DMA 上设备的同时，fill 线程池把第 N+1 块从 mmap 填满——这就是「双缓冲重叠」：一次 host 填充与一次 H2D DMA 并行。

它的三个常量都来自 [ep4-gb300.md](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/docs/models/glm52/ep4-gb300.md) 的同主机参数扫描：

| 常量 | 值 | 依据（源码注释 + 文档） |
| --- | --- | --- |
| `STAGE_BYTES` | 32 MiB | 两槽共 64 MiB/rank；32 与 64 MiB 分布重叠、中心差 <1%，取 32 省一半 pinned 占用 |
| `FILL_THREADS` | 4 | EP4 每 rank 30,660 次上传，常驻 4 工人避免反复建线程；4 明显优于 2 和 8 |
| `PARALLEL_FILL_MIN_BYTES` | 1 MiB | 小于 1 MiB（如 3 KiB 的专家 scale）直接单线程拷贝，避免任务分发开销大于拷贝本身 |

关键取舍：**staged 是 opt-in（`--glm52-weight-staging`），不是默认**。热页缓存下它是 3～6 倍的加速；但冷启动（网络文件系统、页缓存为空）时它反而更慢——4 个 rank × 4 个 fill 工人并发缺页会破坏顺序预读。所以默认走 pageable，让运维按部署形态选择。

#### 4.3.2 核心流程

一次张量上传（可能切成多个 32 MiB 块）：

```
upload(src, dst, dst_offset):
    校验 dst 与 stager 同 stream；校验 [dst_offset, dst_offset+len) 在 dst 内
    for chunk in src.chunks(STAGE_BYTES):
        stage_chunk(chunk, dst_at):
            idx = next; next 轮转 (0↔1)          # 双缓冲选槽
            buf.dma_done.synchronize()             # 等这个槽上一轮 DMA 完成
            fill.fill(chunk → buf.pinned[..len])   # 线程池填充（<1MiB 直接拷）
            memcpy_htod_async(dst_at ← buf.pinned) # 异步 DMA，立刻返回
            buf.dma_done.record(stream)            # 封存「DMA 完成时刻」
```

时间线示意（A/B 两槽，t 为时间）：

```
槽A: [fill t1][DMA t2................][fill t5][DMA t6...]
槽B:         [fill t3][DMA t4................][fill t7]...
                 ↑ t3 的填充与 t2 的 DMA 重叠 ← 双缓冲的意义
```

安全性底线：pinned 内存绝不能在有 in-flight DMA 时被释放——`Drop` 里 drain 事件失败就 `std::process::abort`，宁可进程死也不冒「DMA 写已释放内存」的险。

#### 4.3.3 源码精读

[src/weights/staging.rs:19-31](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/staging.rs#L19-L31) —— 三个常量与注释：32 MiB 槽（两槽 64 MiB/rank，EP4 全机 256 MiB）；4 个常驻工人（EP4 30,660 次上传/rank，四 rank 共 16 个活跃 fill 工人）；1 MiB 并行下限（专家 scale 只有 3 KiB，扇出比拷贝贵）。注释指明参数出处是 GB300 bring-up 文档。

[src/weights/staging.rs:32-74](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/staging.rs#L32-L74) —— `FillPool`：一个 `rayon::ThreadPool`（线程名 `glm52-weight-fill-{rank}-{worker}`），`fill` 方法对 <1 MiB 的源直接 `copy_from_slice`，否则按 worker 数均分块、`pool.scope` 里并行拷贝。**scope 借用语义是安全关键**：`scope` 返回前所有借用的任务必然结束——API 层面保证 `fill` 返回时没有工人还借着 mmap 或 pinned 槽（文档里「ownership-safe pool」即此）。

[src/weights/staging.rs:86-112](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/staging.rs#L86-L112) —— `Glm52WeightStager`：持有同一条 stream 的 Arc、`[StagingBuf; 2]`（每槽 = pinned 缓冲 + `dma_done` 事件）、fill 池与轮转下标。构造时每槽 `alloc_pinned::<u8>(STAGE_BYTES)` 并创建 `CU_EVENT_BLOCKING_SYNC` 事件；SAFETY 注释声明「DMA 消费的每个字节都先被 fill 池初始化，复用由 `dma_done` 门控」。

[src/weights/staging.rs:115-145](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/staging.rs#L115-L145) —— `upload`：两条前置校验（目标必须在 stager 的 stream 上分配——否则事件时序失去意义；区间不得越界），然后按 `STAGE_BYTES` 切块逐块 `stage_chunk`，返回 H2D 次数（大张量一次上传可能产生多个块拷贝）。

[src/weights/staging.rs:150-183](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/staging.rs#L150-L183) —— `stage_chunk`：轮转选槽 → `dma_done.synchronize()`（**这个槽上一次 DMA 已完成**，此刻起 pinned 内存可安全重写）→ fill 池同步填充 → `memcpy_htod_async` 入队 → `dma_done.record(stream)`。两次失败路径（拷贝失败、record 失败）都先 `drain_or_abort` 排空流再返回错误。

[src/weights/staging.rs:186-206](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/staging.rs#L186-L206) —— `Drop` 与 `drain_or_abort`：逐槽同步 `dma_done`；同步失败（CUDA 已进入坏状态）则**记录错误并 abort 进程**——注释直说理由：与其在 in-flight DMA 下释放 pinned 内存，不如崩溃。这是「pinned + DMA」组合的标准铁律。

CLI 开关与 A/B 证据：

[src/model_line.rs:56-60](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model_line.rs#L56-L60) —— `--glm52-weight-staging`：帮助文本写明「可显著加速热页缓存加载；冷的网络文件系统启动请保持关闭」。

[docs/models/glm52/ep4-gb300.md:155-168](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/docs/models/glm52/ep4-gb300.md#L155-L168) —— 同主机 uploader A/B（100% 热页缓存，`mincore` 验证）：pageable 中位 24.765 s（7.592 GiB/s）对 pinned 11.913 s（15.782 GiB/s），2.079×。

[docs/models/glm52/ep4-gb300.md:210-219](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/docs/models/glm52/ep4-gb300.md#L210-L219) —— 强制冷缓存（`POSIX_FADV_DONTNEED`，`mincore` 证明页缓存近乎为空）：pageable 135.76 s 对 pinned **213.58 s**——pinned 反而慢，回归出现在 `mmap_deser_copy` 计量段，与「4 rank × 4 工人并发缺页破坏顺序读」一致。**结论：默认关闭，作为生产 A/B 开关保留**。文档还给出 EP4 四 rank 端到端（3.202×，[L197-208](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/docs/models/glm52/ep4-gb300.md#L197-L208)）与 EP8 rank-0（5.376×，[L221-227](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/docs/models/glm52/ep4-gb300.md#L221-L227)）的数据。

#### 4.3.4 代码实践

**实践目标**：验证 fill 池的纯 CPU 正确性，并手推一次大张量的分块与双缓冲时间线。

**操作步骤**：

1. 运行 staging 模块唯一的单测（纯 CPU，不碰 GPU——但编译仍需 glm52 特性的 CUDA 构建，**待本地验证**）：

   ```bash
   cargo test --release -p pegainfer-glm52 --features glm52 --lib staging
   ```

   对照 [src/weights/staging.rs:212-223](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/staging.rs#L212-L223)：它对 `len ∈ {0, 1, 3, 4095, 4096, 1 MiB, 3 MiB+17}` 逐一验证 `FillPool::fill` 的逐字节精确——注意哪些长度走单线程路径、哪些走 4 工人并行路径。

2. 手推分块：一个 70 MiB 的张量经 `upload` 会被切成几块？块序列落在哪个槽？画出各槽的 fill/DMA 时间线（标注 `dma_done` 在哪里 synchronize、在哪里 record）。

3. 回答：若把 `STAGE_BYTES` 改成 4 MiB，这个 70 MiB 张量的 H2D 次数与 `h2d_copies` 计数各变成多少？ep4 文档的参数表为什么最终保留 32 MiB？

**需要观察的现象**：步骤 1 测试绿；步骤 2 你应得到 3 块（32+32+6 MiB），槽序 0→1→0，其中槽 0 的第二次 `synchronize` 等的正是它自己第一次 DMA 的 `dma_done`。

**预期结果**：① 3 块，槽轮转 0,1,0；② 4 MiB 槽则 70/4 = 18 块（`h2d_copies` +17），事件 record/synchronize 次数同步暴涨——文档保留 32 MiB 正因为块数对槽大小不敏感（30,803 对 30,946 chunks）而 32 省一半 pinned 占用。**待本地验证**（单测运行部分）。

#### 4.3.5 小练习与答案

**练习 1**：`dma_done.synchronize()` 等的是「这个槽」的什么事件？如果去掉这行会发生什么？

**答案**：等的是**这个槽上一次** `record` 的事件——即该槽前一次 DMA 的完成时刻。去掉后，host 可能在设备还从这块 pinned 内存取数时就开始重写它（双缓冲轮转回来的下一轮 fill），DMA 读到新数据——权重被静默污染，且几乎不可复现。事件门是双缓冲正确性的全部支点。

**练习 2**：为什么 stager 的 `Drop` 在 drain 失败时选择 `std::process::abort()` 而不是返回错误？

**答案**：`Drop` 的签名拿不到 `Result` 传播链；更本质的是，此刻进程的 CUDA 状态已不可信（连事件同步都失败了），继续运行并析构 pinned 内存等于允许 in-flight DMA 访问已释放内存——可能损坏任意进程内存。abort 是把损害范围钉死在本进程的Fail-stop选择，与 free-running「fail loud, never hang peers」的纪律同源。

**练习 3**：为什么 `upload` 开头要断言 `dst` 与 stager 在同一条 stream 上（`Arc::ptr_eq(dst.stream(), &self.stream)`）？

**答案**：`dma_done` 事件是在 stager 自己的 stream 上 record 的，它只证明「**这条 stream 上**该点之前的拷贝完成了」。若目标缓冲在别的 stream 上分配/使用，事件时序与 DMA 实际执行流脱钩，门就失效。把「同 stream」变成入口断言，使这个前提不可违反。

---

### 4.4 加载报告与 VRAM 探测

#### 4.4.1 概念说明

加载不只是搬字节——它的副产品是**一次精确的显存测量**。权重驻留完成后，设备上「还剩多少空闲 VRAM」直接决定这台机器能开多大的上下文（`max_model_len`）和 KV 池。GLM5.2 的预算哲学（#818 两阶段构建）是「实测优先」：与其用估计账本算 KV 池，不如在**真实驻留之后**问驱动「还剩多少」。

`free_vram_bytes` 在启动期被探测三次，喂给三个不同决策：

1. **LoadWeights 报告里的探测**（本讲）：权重落位后立刻测，报告随 `Glm52RankWeightLoadReport` 回引擎 → 取 fleet 最小值 → 推导 `max_model_len`（上下文上限）。
2. **BuildFixed 的回执**（[src/runner.rs:717-741](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/runner.rs#L717-L741)）：固定半构建完再测 → 用真实余量买 KV 页（u2-l4 详讲）。
3. **FreeVram 指令**（[src/runner.rs:276-279](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/runner.rs#L276-L279)、[L1104-1106](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/runner.rs#L1104-L1106)）：构建后的 headroom 复查。

本讲聚焦第 1 条链路的「测量 → 上报 → 取最小 → 扣减 → 预算」全程；池填充的细节留给 u2-l4。

#### 4.4.2 核心流程

```
worker 侧 (load_weights 尾部):
    free_vram_bytes = ctx.free_vram_bytes()     # 先重绑 context 再 cuMemGetInfo
    report = { rank, tensor_count, total_bytes, resident_raw_bytes,
               loaded_to_gpu: true, free_vram_bytes }

引擎侧 (start_engine):
    min_free = reports.iter().map(free_vram_bytes).min()     # fleet 最短板
    post_weight_charge = qa|kv_a 孪生缓冲 + DeepGEMM 增量    # 精确已知
    budget = derive_max_model_len(requested,
                                  min_free - post_weight_charge, ...)
    # 之后 BuildFixed 阶段再用第 2 次实测决定 pool_blocks（u2-l4）
```

为什么取**最小值**？因为 fleet 是对称配置的：所有 rank 必须用同一个 `max_model_len`（collective 协议两侧几何一致），所以瓶颈由最穷的 rank 决定——一台机器余量再大也救不了另一台的紧张。

#### 4.4.3 源码精读

探测本身：

[src/weights/context.rs:50-59](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/context.rs#L50-L59) —— `free_vram_bytes`：**先 `set_current()` 重绑本 rank 的 context**，再 `cuMemGetInfo`。注释解释了这个看似多余的步骤：这个数字要用来给每个 rank 的 cache arena 定尺寸，「读错设备」必须不可表示——若恰好当前线程绑的是别的 context，裸调 `mem_get_info` 会安静地返回别的设备的余量，整个预算从源头错掉。

报告结构与组装：

[src/runner.rs:51-61](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/runner.rs#L51-L61) —— `Glm52RankWeightLoadReport`：字段含 rank、张量数、总字节、驻留原始字节、`loaded_to_gpu` 与 `free_vram_bytes`；`free_vram_bytes` 的文档注释写明「权重刚落位后本设备的空闲 VRAM——启动期 context-cap 探测取 fleet 最小值」。

[src/runner.rs:687-703](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/runner.rs#L687-L703) —— 组装处：先断言 `loaded_total_bytes == weights.total_bytes`（装载字节数 = 驻留字节数，防止「搬了没驻留/驻留没搬」的记账裂缝），再探测 VRAM、装报告、把驻留权重存进 `self.loaded`。

引擎侧的汇聚与预算：

[src/lib.rs:881-887](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L881-L887) —— `GpuWeightLoadReport` 把各 rank 的张量数、字节、`free_vram_bytes` 收成三个按 rank 索引的 `Vec`。

[src/lib.rs:916-939](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L916-L939) —— 预算链路：`min()` 取 fleet 最小空闲（L916-922）；再减去 `post_weight_charge_bytes`——这是「测量之后、推导之前**必定**还要发生的两笔精确开销」：MLA 前半的 q_a/kv_a 孪生缓冲 + DeepGEMM 工作区增量（L923-931，注释写明：在这里精确入账，auto 模式才不会把这笔钱花到 KV 上、然后在构建后的 headroom 门上 OOM）；最后喂给 [src/lib.rs:743-855](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L743-L855) 的 `derive_max_model_len`（u2-l4 的主角，这里只需知道：显式 `--max-model-len` 走精确入账校验，auto 模式在 64 对齐候选上二分搜索最大可负担 cap）。

之后 L950-979 打出那条信息量极大的 `GLM5.2 max_model_len=...` 日志：min rank free VRAM、孪生缓冲与 DeepGEMM 扣减、floor-pool arena、reserve、budget——每个字段都能回溯到上面源码里的一个量。

#### 4.4.4 代码实践

**实践目标**：从一条真实启动日志反推出报告字段与预算链路的每一项（日志取证型实践）。

**操作步骤**：

1. 在有 8×GPU 与 GLM5.2 权重的机器上启动：

   ```bash
   cargo run --release --features glm52 -- --model-path models/GLM5.2 2>&1 | tee glm52-start.log
   ```

2. 提取两条关键日志并对照源码：

   ```bash
   grep -E "weight load profile|max_model_len=" glm52-start.log
   ```

3. 把 `max_model_len=` 行里的 "min rank free VRAM" 与每个 rank 的 `weight load profile` 行末字节对照：哪条 profile 对应最穷的 rank？再找出「qa|kv_a twins」与「DeepGEMM post-weight delta」两项扣减各自对应 [src/lib.rs:926-931](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L926-L931) 的哪个变量。

**需要观察的现象**：8 条 `weight load profile`（每 rank 一条，含 total/copy/sync 三段时长与字节数）；一条 `max_model_len=` 日志，其 min free 值等于 8 条 profile 所在 rank 报告的最小余量。

**预期结果**：能填出这样一张表——

| 日志片段 | 源码出处 |
| --- | --- |
| `weight load profile: total=…s, mmap_deser_copy=…s, sync=…s, uploader=…` | [load.rs:337-350](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/load.rs#L337-L350) |
| `max_model_len=…min rank free VRAM …after weights (qa|kv_a twins … + DeepGEMM post-weight delta … charged)` | [lib.rs:950-979](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L950-L979) |

**待本地验证**：本实践需要 GPU 与 checkpoint；无 GPU 时替代方案是纯源码版——从 [lib.rs:907](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L907) 出发，把「加载完成 → min → 扣减 → derive → 日志」的每个量抄进同一张表并注明出处行号。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `free_vram_bytes` 要在**权重加载完**这个时刻测，而不是进程启动时测？

**答案**：预算的对象是「权重之后还能装多少 cache」——只有权重真实驻留后，余量才把「fp8 专家板的实际占用、非专家张量的碎片、stream 分配池的驻留」全部包含进去。启动时测会高估余量（还没扣掉 100+ GiB 权重），账本式估算又会漏掉分配器行为；实测点选在最大不确定项落定之后，是 #818「实测优先」哲学的第一步。

**练习 2**：假设 8 个 rank 中 rank 5 因为同机跑了别的进程，余量比其他 rank 少 30 GiB。`max_model_len` 与 KV 池会怎么变？这公平吗？

**答案**：`min()` 让 rank 5 成为全局短板，所有 rank 按少 30 GiB 的口径推导同一个 `max_model_len`、分配同样大小的池——fleet 必须几何对称（collective 与镜像布局的前提），没有「穷 rank 少装点」的选项。这正是文档要求 GLM5.2 独占整机的原因之一：任何 rank 的邻居噪声都会直接折损全 fleet 的容量。

**练习 3**：`post_weight_charge_bytes` 里为什么恰好是「q_a/kv_a 孪生缓冲 + DeepGEMM 增量」这两笔，而不是更多或更少？

**答案**：这两笔是「**探测点之后、构建期之前**必然发生的已知精确开销」——它们还没落地（所以实测余量里没包含），但字节数可以从常量精确算出（所以不需要估计）。更早发生的开销已在实测值里；更晚、无法精确入账的（CUDA graph 实例化、cuBLAS 工作区、碎片）则交给 `glm52_graph_reserve_bytes` 那道粗粒度保留（3 GiB 默认，可被 `GLM52_GRAPH_RESERVE_GIB` 覆盖）。精确的精确记、模糊的统一保留——不多不少正是这两笔。

---

## 5. 综合实践

**任务：给一次 GLM5.2 启动做「加载画像 + 预算追踪」全链路分析。**

在有 GPU 的机器上（无 GPU 则做源码版，见下）：

1. **两次启动、一次对照**：先默认（pageable），再加 `--glm52-weight-staging` 各启动一次，各存一份日志。

   ```bash
   cargo run --release --features glm52 -- --model-path models/GLM5.2 2>&1 | tee load-pageable.log
   cargo run --release --features glm52 -- --model-path models/GLM5.2 \
     --glm52-weight-staging 2>&1 | tee load-staged.log
   ```

2. **画像**：从两份日志提取每个 rank 的 `weight load profile`，算出 pageable 与 staged 的中位吞吐（字节 ÷ `mmap_deser_copy` 时长），对照 [ep4-gb300.md](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/docs/models/glm52/ep4-gb300.md) 的 A/B 表，判断你的主机更接近文档里的哪种形态（热缓存/冷缓存）。
3. **预算追踪**：沿 `start_engine` 的顺序，把 `max_model_len=` 日志里的每个数（min free、twins、DeepGEMM delta、reserve、budget、floor-pool arenas）填进一张表，并在表里给每个数标注源码出处（文件:行号），验证恒等式：

   \[ \text{budget} = \min_{\text{rank}}(\text{free\_vram}) - \text{twins} - \text{deepgemm} - \text{reserve} \]

4. **协议复盘**：在日志时间轴上标出「spawn 完成 → 各 rank 加载开始/结束 → 报告收集完 → 预算推导」，验证多 rank 加载是否真的重叠（各 rank profile 的 total 区间应互相交叠）。

**源码版替代（无 GPU）**：完成第 3 步的表 + 第 4 步改为画出从 [lib.rs:894](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L894) `start_engine` 到 worker `load_weights` 的完整数据流图（引擎线程 / worker 线程 / GPU stream 三条泳道，标注每条消息与每次 H2D），并回答：**这条链路上共有几类「不可违反的构造性保证」（rank 一致性、契约三关、无洞对账、mmap 护栏、dma_done 门、ensure_consumed、abort 纪律）？各防的是什么错？** 把答案整理成「保证 → 源码位置 → 防住的故障」三列表。

运行部分**待本地验证**；源码版可完全离线完成。

## 6. 本讲小结

- **加载发生在 worker 线程**：每 GPU 一线程独占 CUDA context，引擎经「指令自带回信信封」的 crossbeam 通道异步驱动；`lib.rs` 先向全部 rank 入队 `LoadWeights` 再收报告，实现并行加载。
- **pageable 主循环**按「mmap 分片 → 三关契约校验 → 专家直落位/非专家独立缓冲 → 覆盖对账」搬运每 rank 100～190 GiB；`MMAP_EVENT_GROUP=1 × LIVE_MMAP_GROUPS=16` 的事件护栏让 mmap 生命周期与拷贝完成节奏绑定，消除大量映射共存的大尾部回归。
- **驻留形态是 raw 字节**：非专家 `BTreeMap<String, CudaSlice<u8>>` + 每层四个打包专家区域；消费端 take-and-retype，`ensure_consumed` 保证没有字节白白搬运。
- **staged 上传器**用 2×32 MiB pinned 双缓冲 + 4 常驻 fill 工人把「host 填充」与「H2D DMA」流水线化（热缓存 3～6×），但冷网络文件系统启动反而更慢，故 `--glm52-weight-staging` 是 opt-in 的生产 A/B 开关。
- **pinned + DMA 的铁律**：`dma_done` 事件门控槽复用，`Drop` drain 失败即 abort——宁可进程死，不在 in-flight DMA 下释放 pinned 内存。
- **加载报告喂预算**：`free_vram_bytes` 在权重落位后实测（先重绑 context 防读错设备），引擎取 fleet 最小值、扣除精确已知的两笔构建开销后交给 `derive_max_model_len` 定上下文上限。

## 7. 下一步学习建议

本讲止步于「预算推导的入口」。下一讲 **u2-l4 显存预算：上下文上限推导与实测 KV 池填充**将深入 `derive_max_model_len` 的精确账本（`glm52_cap_bytes`）与 64 对齐二分搜索，以及 BuildFixed/FinishKv 两阶段构建如何用第二次实测把 KV 池买到最后一个页——本讲的 `free_vram_bytes` 正是那条链路的输入。之后再进入 u3 算子层时，你会带着「这些权重以什么布局躺在 GPU 上」的具象认知去读 fp8.rs 与 mla_front.rs。建议顺带阅读 [docs/models/glm52/ep4-gb300.md](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/docs/models/glm52/ep4-gb300.md) 全文——它是「常量从何而来」的完整范例。

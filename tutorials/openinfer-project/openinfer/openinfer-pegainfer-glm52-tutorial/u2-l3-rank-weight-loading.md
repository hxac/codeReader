# u2-l3 · rank 权重加载：mmap、staging 与 GPU 驻留

> 上一讲（u2-l2）搞清楚了 checkpoint 的静态布局：`Glm52WeightManifest` 把 `model.safetensors.index.json` 解释成「张量名 → 分片文件」，再切出每个 rank 的加载包 `Glm52RankLoadBundle`。本讲把镜头推进到运行期：这些加载包如何真正变成每块 GPU 上的字节，加载完的空闲显存数字又流向哪里。

## 1. 本讲目标

学完本讲，你应该能够：

1. 完整复述一条权重字节从磁盘到显存的路径：**mmap 分片 → 契约三关校验 → 上传器 → 驻留缓冲**，并说清每一步在哪个文件里。
2. 解释 `MMAP_EVENT_GROUP` 与 `LIVE_MMAP_GROUPS` 两个常量针对的「大尾部回归」问题，理解事件滑动窗口如何同时保住**正确性**（mmap 不能先于 DMA 消失）与**吞吐**（不让大量映射同时存活）。
3. 对比 pageable 与 staged（pinned 双缓冲）两种上传器：实现、成本、适用场景，以及为什么 staging 默认关闭。
4. 弄清 `free_vram_bytes` 在哪里被探测、为什么探测前必须重绑 context、它如何被聚合成 fleet 最小值并喂给下一讲的显存预算函数 `derive_max_model_len`。

## 2. 前置知识

先补齐本讲要用的底层词汇，每个一句话：

| 术语 | 一句话解释 |
|---|---|
| H2D | Host-to-Device，把主机内存字节拷进显存，走 PCIe/NVLink 总线。 |
| mmap | 把文件映射进进程虚拟地址空间；读文件变成读内存，内核按页缺页装入。 |
| safetensors 分片（shard） | GLM5.2 checkpoint 的一卷文件：一个小 JSON header + 连续存放的几十个张量字节。 |
| pageable 内存 | 可被操作系统换页的普通内存（`Vec`、mmap 页都是）；设备对它做异步 DMA 前驱动常要内部中转。 |
| pinned（页锁定）内存 | 物理页锁定、设备 DMA 可直达的主机内存；配 `cudaMemcpyAsync` 才是真异步。 |
| CUDA stream | GPU 上的操作队列，H2D 拷贝按入队顺序执行。 |
| CUDA event | 队列里的里程碑；`event.synchronize()` 阻塞到它之前的全部操作完成。 |
| CUDA context | 一块 GPU 的软件状态句柄；本 crate 让每个 GPU 一个专职 worker 线程独占其 context，避免跨线程踩踏。 |
| rayon | Rust 数据并行线程池库；本讲中用作「pinned 缓冲填充线程池」。 |
| crossbeam 通道 | 引擎线程与 worker 线程之间传指令/响应的管道，`unbounded()` 无界、`bounded(n)` 有界。 |

再把上一讲的结论接到本讲入口：

- `Glm52RankLoadBundle`（[src/weights.rs:L185](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights.rs#L185)）= 本 rank 的加载计划：`plan`（rank 号、张量总数、专家区间）+ `shards`（每个分片文件里要取哪些张量）。
- `expected_tensor_contract`（u2-l2 讲过）按张量名给出期望的 dtype/shape/字节数——本讲的「契约三关」拿它现场对账。
- `expert_placement` 把路由专家张量名换算成 `(layer, region, offset)` 落位三元组，region 是四种打包区之一（[src/weights.rs:L69-L98](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights.rs#L69-L98)）。

## 3. 本讲源码地图

| 文件 | 行数 | 职责 |
|---|---|---|
| [src/weights/load.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/load.rs) | 492 | 单 rank 加载主循环：mmap 分片、契约三关、专家打包落位、三重对账、mmap 生命周期窗口 |
| [src/weights/staging.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/staging.rs) | 224 | pinned 双缓冲上传器 `Glm52WeightStager` 与填充线程池 `FillPool` |
| [src/weights/context.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/context.rs) | 152 | `Glm52RankGpuContext`：context/stream 绑定、free VRAM 探测、kernels 视图 |
| [src/runner.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/runner.rs) | 1143 | `Glm52RankWorker` 线程与指令协议、`Glm52RankThreadState::load_weights`（含 TP4 第二趟）、加载报告 |
| [src/lib.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs) | — | fleet 编排层 `load_rank_weights_to_gpu`（与 load.rs 同名的另一个函数）、报告聚合、预算交接 |
| [src/weights.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights.rs) | 916 | （u2-l2 主角）`mmap_file`、专家区域种类与 `region_bytes` |
| [src/model_line.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model_line.rs) | — | `--glm52-weight-staging` CLI flag 定义 |

一个容易踩的坑：**仓库里有两个同名的 `load_rank_weights_to_gpu`**——`src/lib.rs:1698` 是 fleet 编排版（spawn 一堆 worker、扇出指令、聚合报告），`src/weights/load.rs:168` 是单 rank 实现版。下文引用时写全路径区分。

## 4. 核心概念与源码讲解

### 4.1 worker 线程与异步加载

#### 4.1.1 概念说明

「异步加载」在本讲有两层含义，不要混：

1. **rank 间并行**：默认拓扑 EP8 下 8 个 rank 各自在自己的 GPU 上加载，互不等待——这是 lib.rs 编排层的事。
2. **单 rank 内部的流水**：mmap 读取、主机侧填充、DMA 传输尽量重叠——这是 4.2 / 4.3 的事。

worker 线程存在的根本原因是 **CUDA context 的线程亲和性**：一块 GPU 的 context 由一个线程创建并绑定后，其他线程直接摸它的设备状态是危险的。所以本 crate 的设计是：

- 引擎（调度）线程**完全不碰 CUDA**；
- 每个 GPU 一个名为 `glm52-rank-{rank}` 的专职线程，独占创建并绑定 context；
- 两侧只通过 crossbeam 通道交换「指令（command）→ 响应（response）」。

这与 u1-l3 讲过的 12 变体指令协议 `Glm52RankCommand` 是同一套机制，本讲只用到其中的 `LoadWeights`。

#### 4.1.2 核心流程

启动期权重加载的全链路（示例伪代码，仅示意调用关系）：

```
start_engine (lib.rs:894)
└─ load_rank_weights_to_gpu(model_path, startup, moe_topo, staging)   ← lib.rs:1698，fleet 编排版
   ├─ for (device_ordinal, rank) in startup.ranks:                    ← lib.rs:1710
   │    Glm52RankWorker::spawn(placement, bundle)                     ← runner.rs:320
   │      ├─ unbounded 指令通道 + bounded(1) 启动屏障通道
   │      └─ 新线程 glm52-rank-{rank}:
   │           Glm52RankGpuContext::new(device_ordinal)               ← context.rs:22（创建并绑定 context）
   │           startup_tx.send(Ok(()))                                ← 屏障放行
   │           rank_worker_loop(rx, Glm52RankThreadState)             ← runner.rs:1043
   ├─ spawn 侧 startup_rx.recv()：context 创建失败在此 fail-fast        ← runner.rs:346
   ├─ for worker: load_weights_async(...) → 发 LoadWeights 指令        ← runner.rs:355
   ├─ for rx: rx.recv() → 校验 report.rank / loaded_to_gpu            ← lib.rs:1742
   └─ 聚合 GpuWeightLoadReport { tensor_counts, bytes, free_vram_bytes } ← lib.rs:1772

worker 线程内：
rank_worker_loop 收到 LoadWeights                                      ← runner.rs:1046
└─ state.load_weights(...)                                            ← runner.rs:635
   ├─ load_rank_weights_to_gpu(&ctx, path, &bundle, staging)          ← weights/load.rs:168，单 rank 实现版
   ├─ [仅 TP4 拓扑] 第二趟逐层 load_tp_slice_layer 装专家切片           ← runner.rs:644
   ├─ ctx.free_vram_bytes()                                           ← context.rs:54
   └─ resp.send(Glm52RankWeightLoadReport { ..., free_vram_bytes })
```

#### 4.1.3 源码精读

**① spawn：线程与启动屏障**。[src/runner.rs:L320-L345](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/runner.rs#L320-L345) 先校验加载计划的 rank 与放置一致（防止拿错包），然后建两条通道、spawn 线程；线程内第一件事就是创建并绑定 CUDA context，成败通过启动屏障回报：

```rust
        let (tx, rx) = unbounded();
        let (startup_tx, startup_rx) = bounded::<Result<()>>(1);
        let handle = thread::Builder::new()
            .name(format!("glm52-rank-{}", placement.rank))
            .spawn(move || {
                let ctx = match Glm52RankGpuContext::new(placement.device_ordinal) {
                    Ok(ctx) => ctx,
                    Err(err) => {
                        let _ = startup_tx.send(Err(err));
                        return;
                    }
                };
                let _ = startup_tx.send(Ok(()));
                rank_worker_loop(&rx, Glm52RankThreadState::new(placement, ctx, bundle));
            })
```

**② 主线程等屏障**。[src/runner.rs:L346-L353](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/runner.rs#L346-L353)：worker 若在 context 创建阶段就死掉（比如 GPU 被别的进程占满），启动当场失败而不是留一个哑线程：

```rust
        startup_rx
            .recv()
            .map_err(|_| anyhow::anyhow!("GLM5.2 rank worker exited during startup"))??;
```

**③ 指令与响应**。`LoadWeights` 指令携带模型路径、拓扑与 staging 开关，响应类型是 `Result<Glm52RankWeightLoadReport>`：[src/runner.rs:L205-L211](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/runner.rs#L205-L211)。发起侧 `load_weights_async` 建一个 `bounded(1)` 响应通道、把指令扔进队列就返回：[src/runner.rs:L355-L371](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/runner.rs#L355-L371)。

```rust
        let (resp_tx, resp_rx) = bounded(1);
        self.tx
            .send(Glm52RankCommand::LoadWeights {
                model_path: model_path.to_path_buf(),
                moe_topo,
                weight_staging,
                resp: resp_tx,
            })
            .map_err(|_| anyhow::anyhow!("GLM5.2 rank worker channel closed"))?;
        Ok(resp_rx)
```

**④ worker 循环**。[src/runner.rs:L1043-L1053](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/runner.rs#L1043-L1053)：`rank_worker_loop` 就是 `while let Ok(cmd) = rx.recv()` 加一个 match；结果（含 `Err`）原样回传。引擎侧 `recv()` 出错即报「worker dropped load response」——**响应被丢弃视作致命**（[src/lib.rs:L1742-L1744](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L1742-L1744)）：

```rust
    while let Ok(cmd) = rx.recv() {
        match cmd {
            Glm52RankCommand::LoadWeights {
                model_path,
                moe_topo,
                weight_staging,
                resp,
            } => {
                let _ = resp.send(state.load_weights(&model_path, moe_topo, weight_staging));
            }
```

**⑤ 编排层扇出与聚合**。[src/lib.rs:L1726-L1735](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L1726-L1735) 用一次 `map` 把**所有** rank 的 `LoadWeights` 指令先入队（此时 8 个 worker 真并行），然后才开始逐个收响应；[src/lib.rs:L1736-L1751](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L1736-L1751) 校验报告 rank 与全局 rank 对得上——跨节点时 `ranks.start != 0`，enumerate 下标只是本进程槽位（见注释）；最后 [src/lib.rs:L1752-L1779](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L1752-L1779) 把三组数字拉平成 `GpuWeightLoadReport`：

```rust
    let load_results = workers
        .iter()
        .map(|worker| worker.load_weights_async(model_path, moe_topo, weight_staging))
        .collect::<Result<Vec<_>>>()?;
    let mut reports = Vec::with_capacity(load_results.len());
    for (local_index, rx) in load_results.into_iter().enumerate() {
        // `report.rank` is the global fleet rank; the enumerate index is only
        // this process's worker slot, so recompute the rank from the hosted
        // range before validating (matters when ranks.start != 0).
        let rank = startup.ranks.start + local_index;
```

顺带说明：lib.rs 里 worker 类型以 `Glm52Worker` 之名出现（[src/lib.rs:L70](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L70) 的 `use runner::Glm52Worker;`），它在 runner.rs 中的结构体名是 `Glm52RankWorker`（[src/runner.rs:L314](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/runner.rs#L314)）。

#### 4.1.4 代码实践

**实践目标**：把 4.1.2 的伪代码落到真实行号，验证你对加载时序的理解。

**操作步骤**（纯源码阅读，无需 GPU）：

1. 打开 [src/runner.rs:L320-L353](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/runner.rs#L320-L353)，抄下三个事实：线程名格式、指令通道容量（无界）、启动屏障容量（1）。
2. 打开 [src/lib.rs:L1732-L1751](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L1732-L1751)，回答：指令入队和响应接收是**同一个**循环还是**两个**循环？这决定了 8 个 rank 是否真的并行加载。
3. 读 [src/lib.rs:L1737-L1744](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L1737-L1744) 的注释，解释为什么 rank 要用 `startup.ranks.start + local_index` 重算，而不能直接用 enumerate 下标。

**需要观察的现象**：第 2 步的答案应为「先全部入队、再统一收响应」——`map` 阶段就把 8 条指令发完，`for` 循环才开始 `recv()`，所以总时长由最慢的 rank 决定，而不是各 rank 串行相加。

**预期结果**：不看讲义复述「spawn → 屏障 → 扇出 → 收报告 → 聚合」五步及各自文件行号。

#### 4.1.5 小练习与答案

**练习 1**：启动屏障为什么用 `bounded(1)` 而不是 `unbounded`？

答案：屏障只需要传**一个**结果（context 创建成败），容量 1 语义精确；若 worker 提前退出没发送，`recv()` 返回 `Err` 同样被 [src/runner.rs:L346-L348](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/runner.rs#L346-L348) 的 `??` 转成启动失败。容量再大没有意义，还会掩盖「只应发一次」的约定。

**练习 2**：跨节点 EP16（两个进程分别 `--glm52-ranks 0..8`、`8..16`）时，第二个进程的 `local_index` 与 `report.rank` 是什么关系？

答案：本进程 enumerate 下标是 0..8 的槽位，真实 rank 是 `8 + local_index`；[src/lib.rs:L1741](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L1741) 用 `startup.ranks.start + local_index` 重算后与 `report.rank` 比对，防止把别的 rank 的报告错记到本进程账上。

**练习 3**：为什么引擎线程不能自己直接调用 weights/load.rs 的 `load_rank_weights_to_gpu`，非要绕 worker？

答案：单进程 EP8 下有 8 个 context 分别对应 8 块 GPU，而一个线程同一时刻只能可靠持有一个当前 context。绕 worker 让「每 context 一个线程」成为结构保证（[src/weights/context.rs:L16-L19](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/context.rs#L16-L19) 的 SAFETY 注释也点明这一点），引擎线程因此完全不碰 CUDA。

### 4.2 mmap 生命周期与「大尾部回归」

#### 4.2.1 概念说明

先看矛盾：

- 读分片用 mmap（[src/weights.rs:L686-L690](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights.rs#L686-L690)）：零拷贝、按需缺页。
- 但 H2D 拷贝是**入队即返回**的异步操作（尤其 4.3 的 staged 路径），DMA 真正读那些字节可能晚得多。
- mmap 的 `Mmap` 值一 drop，映射就解除——**如果 DMA 还没读完，源字节就悬空了**。

最朴素的两个解法都不好：

- 每个张量拷完立刻 `ctx.sync()`：正确但把流水线打成逐张量串行，极慢。
- 所有分片的 mmap 存活到加载结束：快但回归——源码注释直说这就是**大尾部回归（large-tail regression）**：「many shard mappings stay live together」时观测到的加载尾部劣化（[src/weights/load.rs:L26-L27](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/load.rs#L26-L27)）。机制上的通行解释是大量存活映射带来的虚拟内存区（VMA）与页缓存管理压力让后面的分片越来越慢（注释只陈述现象，机制属工程解读）。

本 crate 的解法是**事件滑动窗口**：

- `MMAP_EVENT_GROUP = 1`：每 1 个分片 mmap 配一个 CUDA event，源的生命周期显式；
- `LIVE_MMAP_GROUPS = 16`：最多 16 组「已完成分片」的映射同时存活，窗口满时先等最老组的 event。

正确性由两层 Drop 兜底（当前分片层 + 整个 guard 层），任何路径下 drop 前都先 sync。

#### 4.2.2 核心流程

分片主循环的骨架（示例伪代码，仅示意）：

```
for shard in bundle.shards:
    mmap = mmap_file(shard 路径)                 # weights.rs:686
    safetensors = SafeTensors::deserialize(mmap)
    for spec in shard.tensors:
        view = safetensors.tensor(spec.name)      # 契约三关（见 4.4）
        if 是本 rank 路由专家: 上传进打包区域（见 4.4）
        else:                    单独 alloc + 上传
    drop(safetensors)
    guard.push_completed_shard(mmap)              # 进入窗口
guard.drain()                                      # 收尾等全部组
```

`push_completed_shard` 的窗口逻辑（示例伪代码）：

```
pending.push(mmap)
if pending.len() == MMAP_EVENT_GROUP:             # = 1，立即成组
    在 stream 上 record event；组 = (mmaps, event) 入队 live_mmap_groups
if live_mmap_groups.len() >= LIVE_MMAP_GROUPS:    # = 16，窗口满
    等最老组 event 完成，pop 它（此时它的 DMA 一定已结束，unmap 安全）
```

窗口内存上界近似为：

\[ W_{\max} \approx \text{LIVE\_MMAP\_GROUPS} \times \text{MMAP\_EVENT\_GROUP} \times S_{\text{shard}} = 16 \cdot S_{\text{shard}} \]

其中 \( S_{\text{shard}} \) 是单个分片文件大小——窗口把「全部存活」的 \( N_{\text{shard}} \cdot S_{\text{shard}} \) 压到常数因子。

#### 4.2.3 源码精读

**① 常量与注释**。[src/weights/load.rs:L26-L29](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/load.rs#L26-L29) 一句话交代设计动机（one mmap per event + 显式生命周期 + 避免大尾部回归），随后给出两个常量：

```rust
// One mmap per event keeps source lifetimes explicit without the large-tail
// regression observed when many shard mappings stay live together.
const MMAP_EVENT_GROUP: usize = 1;
const LIVE_MMAP_GROUPS: usize = 16;
```

**② mmap_file**。[src/weights.rs:L686-L690](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights.rs#L686-L690)：`File::open` + `Mmap::map`，错误信息带路径。

**③ 分片循环头**。[src/weights/load.rs:L204-L209](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/load.rs#L204-L209)：每个分片 mmap 一次、反序列化 header，然后逐张量取 view：

```rust
    for shard in &bundle.shards {
        let path = model_path.join(&shard.shard);
        let shard_started = Instant::now();
        let mmap = CurrentShardMmap::new(mmap_file(&path)?, ctx, bundle.plan.rank);
        let safetensors = safetensors::SafeTensors::deserialize(mmap.as_ref())
            .with_context(|| format!("failed to deserialize {}", path.display()))?;
```

**④ 分片收尾**。[src/weights/load.rs:L286-L292](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/load.rs#L286-L292)：先 `drop(safetensors)`（借用结束），再 `mmap.into_mmap()` 交还裸 mmap 塞进窗口；顺手统计最慢分片：

```rust
        drop(safetensors);
        sync_secs += mmap_guard.push_completed_shard(mmap.into_mmap())?;
        let shard_secs = shard_started.elapsed().as_secs_f64();
```

**⑤ 当前分片的自保**。[src/weights/load.rs:L385-L406](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/load.rs#L385-L406)：`CurrentShardMmap` 在每次发起 H2D 时 `mark_stream_work()`；Drop 时若曾有流上工作，先 `set_current + sync` 再解除映射——保证「正在拷的这个分片」绝不会被提前 unmap：

```rust
impl Drop for CurrentShardMmap<'_> {
    fn drop(&mut self) {
        if self.stream_work.get() {
            if let Err(error) = self.ctx.set_current().and_then(|()| self.ctx.sync()) {
                error!(
                    "failed to synchronize GLM5.2 rank {} before dropping current H2D mmap: {error:#}",
                    self.rank
                );
            }
        }
    }
}
```

**⑥ 窗口本体**。guard 结构与构造（[src/weights/load.rs:L408-L423](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/load.rs#L408-L423)）；`push_completed_shard`（[L425-L435](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/load.rs#L425-L435)，成组 + 窗口满则等最老）；`record_pending_group`（[L448-L462](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/load.rs#L448-L462)，record event 并成组入队）；`wait_oldest_group`（[L464-L477](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/load.rs#L464-L477)，等最老 event、等待耗时计入 sync_secs）；`drain`（[L437-L446](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/load.rs#L437-L446)，收尾把 pending 与全部 live 组排干）：

```rust
    fn push_completed_shard(&mut self, mmap: Mmap) -> Result<f64> {
        self.pending_mmaps.push(mmap);
        let mut sync_secs = 0.0;
        if self.pending_mmaps.len() == MMAP_EVENT_GROUP {
            self.record_pending_group()?;
        }
        if self.live_mmap_groups.len() >= LIVE_MMAP_GROUPS {
            sync_secs += self.wait_oldest_group()?;
        }
        Ok(sync_secs)
    }
```

**⑦ guard 的 Drop 兜底**。[src/weights/load.rs:L480-L492](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/load.rs#L480-L492)：加载中途任何一步出错走 Drop，同样先 sync 再释放——**错误路径也不许悬空 DMA**。

#### 4.2.4 代码实践

**实践目标**：手工模拟滑动窗口，验证你理解 `MMAP_EVENT_GROUP=1 / LIVE_MMAP_GROUPS=16` 的组合行为。

**操作步骤**（纸笔推演，无需 GPU）：

1. 设分片序列 s1..s20，各分片 DMA 的完成时间任意（比如 s_i 的 DMA 在第 i+5 步才完成）。
2. 按 4.2.2 的伪代码逐步执行：写出每步结束后 `pending` 与 `live_mmap_groups` 的内容。
3. 标出第一次发生 `wait_oldest_group` 的步骤，以及那次等待等的是哪个分片的 event。

**需要观察的现象**：第 16 步（push s16 成组后 live 长度达到 16）之后、push s17 之前发生第一次等待。

**预期结果**：任意时刻存活映射数 ≤ 16 + 1（当前分片自身不在窗口里），且每个被 unmap 的分片其 event 必已同步——正确性与有界性同时成立。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `MMAP_EVENT_GROUP` 取 1 而不是 8（8 个分片共享一个 event）？

答案：组越大，一个 event 背后的映射越多，被最慢成员拖住的时间越长，窗口的实际内存上界与排空时延都变大；取 1 让「谁的 DMA 完成了、谁可以走」粒度最细，生命周期最显式（注释原话 "keeps source lifetimes explicit"）。代价只是 event 多一点，event 本身廉价。

**练习 2**：把 `LIVE_MMAP_GROUPS` 调成 1 会怎样？调成 1024 呢？

答案：调成 1 约等于「每分片一个同步点」，流水退化、加载变慢（走向 4.2.1 被否掉的朴素解法一）；调成 1024 在分片数不足 1024 时等价于「全部存活」，重新暴露大尾部回归（朴素解法二）。16 是实测折中。改动属实验性质，真实收益需按仓库规范做 same-context A/B（待本地验证）。

**练习 3**：`CurrentShardMmap::drop` 里的 sync 失败为什么只 `error!` 记日志而不 panic？

答案：Drop 里的 panic 会掩盖正在展开的原始错误；真正致命的问题会在随后必然发生的 `ctx.sync()`（[src/weights/load.rs:L328-L334](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/load.rs#L328-L334)）或指令响应里再次暴露，日志保留了诊断线索。

### 4.3 两种上传器：pageable 与 pinned staging 双缓冲

#### 4.3.1 概念说明

`WeightUploader` 是个二选一的枚举（[src/weights/load.rs:L30-L33](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/load.rs#L30-L33)）：

- `Pageable`：直接 `stream().memcpy_htod(src, view)`，src 是 mmap 里的 pageable 字节。简单、零额外主机内存；缺点是驱动对 pageable 源的异步 DMA 常要内部中转，且「读盘」与「DMA」难以重叠。
- `Staged(Glm52WeightStager)`：先把字节填进 pinned 双缓冲，再 `memcpy_htod_async` 发起真 DMA。快（尤其热页缓存），代价是每 rank 64 MiB pinned 内存 + 4 个填充线程。

选哪个由 CLI flag `--glm52-weight-staging` 决定，默认关。flag 的文档注释把取舍说得直白：**热页缓存加载明显加速；冷的网络文件系统启动请关掉**（[src/model_line.rs:L56-L60](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model_line.rs#L56-L60)）——冷启动瓶颈在缺页读盘，填充线程只会空转抢 CPU。

三个实测常量（[src/weights/staging.rs:L19-L30](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/staging.rs#L19-L30)，注释注明来自 GLM5.2 GB300 bring-up 文档的参数扫描）：

| 常量 | 值 | 作用 |
|---|---|---|
| `STAGE_BYTES` | 32 MiB | 单个 pinned 槽大小；双缓冲 = 64 MiB/rank（EP4 单机 4 rank 共 256 MiB） |
| `FILL_THREADS` | 4 | 每 rank 常驻填充线程数；EP4 每分片 30,660 次上传，常驻池避免反复建线程，4 rank × 4 = 16 个活跃填充线程 |
| `PARALLEL_FILL_MIN_BYTES` | 1 MiB | 低于此大小的张量不值得 fan-out；专家 scale 张量只有 3 KiB，直接单线程 `copy_from_slice` |

#### 4.3.2 核心流程

staged 上传一个张量的时间线（示例伪代码）：

```
upload(src, dst, off):
    校验 dst 与 stager 同 stream；校验 [off, off+len) 在 dst 内
    for chunk in src.chunks(STAGE_BYTES):        # 大张量切 32 MiB 块
        stage_chunk(chunk, dst + off + ...)

stage_chunk:
    buf = bufs[next]; next 翻转                  # 双缓冲轮换
    buf.dma_done.synchronize()                   # 等这块缓冲上一轮 DMA 结束（BLOCKING_SYNC event）
    fill.fill(src, buf.pinned[..len])            # rayon 填充（≥ 1 MiB 才并行切分）
    memcpy_htod_async(dst_at, stage, stream)     # 真 DMA，入队即返回
    buf.dma_done.record(stream)                  # 立刻立里程碑
```

双缓冲的意义：第 k 块在 DMA 时，第 k+1 块同时在主机侧填充，理想情况下两段完全重叠，有效耗时 \( T \approx \max(T_{\text{fill}}, T_{\text{DMA}}) \) 而非两者之和。主机 pinned 内存开销：

\[ M_{\text{pinned}} = 2 \times \text{STAGE\_BYTES} \times N_{\text{ranks on host}} \]

EP4 单机（4 rank）：\( 2 \times 32\,\text{MiB} \times 4 = 256\,\text{MiB} \)，与常量注释一致。

#### 4.3.3 源码精读

**① 上传器构造**。[src/weights/load.rs:L36-L49](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/load.rs#L36-L49)：staging 开则建 `Glm52WeightStager`，否则 `Pageable`；`label()` 返回 `"pageable"` / `"pinned"`，最后会打进 profile 日志。

**② pageable 分支**。[src/weights/load.rs:L59-L72](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/load.rs#L59-L72)：checked_add 防溢出、ensure 防越界，然后一次 `memcpy_htod`；返回 1（一次拷贝计数）：

```rust
            Self::Pageable => {
                let end = dst_offset.checked_add(src.len()).ok_or_else(|| {
                    anyhow::anyhow!("GLM5.2 pageable upload destination range overflow")
                })?;
                ...
                let mut view = dst.slice_mut(dst_offset..end);
                ctx.stream()
                    .memcpy_htod(src, &mut view)
                    .context("GLM5.2 pageable H2D copy")?;
                Ok(1)
            }
```

**③ staged 分支**：直接转给 `stager.upload`（[src/weights/load.rs:L74](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/load.rs#L74)），返回**分块数**（每块一次 DMA）作为 h2d_copies 计数——所以日志里 staged 的 h2d_copies 通常比 pageable 大。

**④ 填充线程池**。[src/weights/staging.rs:L43-L74](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/staging.rs#L43-L74)：`FillPool::new` 建 4 线程 rayon 池（线程名带 rank 号）；`fill` 里小于 `PARALLEL_FILL_MIN_BYTES` 直接 `copy_from_slice`，否则按 `div_ceil(workers)` 切块并行：

```rust
    fn fill(&self, src: &[u8], dst: &mut [u8]) {
        debug_assert_eq!(src.len(), dst.len());
        if src.is_empty() {
            return;
        }
        if src.len() < PARALLEL_FILL_MIN_BYTES {
            dst.copy_from_slice(src);
            return;
        }
        let per = src.len().div_ceil(self.workers);
        self.pool.scope(|scope| {
            for (src_part, dst_part) in src.chunks(per).zip(dst.chunks_mut(per)) {
                scope.spawn(move |_| {
                    dst_part.copy_from_slice(src_part);
                });
            }
        });
    }
```

**⑤ stager 构造**。[src/weights/staging.rs:L94-L112](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/staging.rs#L94-L112)：两块 pinned（`alloc_pinned(STAGE_BYTES)`）各配一个 `CU_EVENT_BLOCKING_SYNC` 事件（阻塞式同步：等 event 时线程睡眠让出 CPU，而非自旋）：

```rust
        let make = || -> Result<StagingBuf> {
            // SAFETY: every byte consumed by DMA is initialized by the fill
            // pool first; buffer reuse is gated by `dma_done`.
            let pinned = unsafe { ctx.cuda_context().alloc_pinned::<u8>(STAGE_BYTES) }
                .context("alloc GLM5.2 pinned weight staging buffer")?;
            let dma_done = ctx
                .cuda_context()
                .new_event(Some(CUevent_flags::CU_EVENT_BLOCKING_SYNC))
                .context("create GLM5.2 pinned weight staging event")?;
            Ok(StagingBuf { pinned, dma_done })
        };
```

**⑥ upload 入口**。[src/weights/staging.rs:L115-L145](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/staging.rs#L115-L145)：两条 ensure 分别钉死「目标必须在 stager 的 stream 上」与「目标区间必须可写」，然后按 `STAGE_BYTES` 分块：

```rust
        for (chunk_idx, chunk) in src.chunks(STAGE_BYTES).enumerate() {
            let dst_at = dst_ptr + (dst_offset + chunk_idx * STAGE_BYTES) as u64;
            // SAFETY: chunks partition the source, and the entry bound proves
            // the corresponding destination range is writable.
            unsafe { self.stage_chunk(chunk, dst_at) }?;
            copies += 1;
        }
```

**⑦ stage_chunk 核心**。[src/weights/staging.rs:L150-L165](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/staging.rs#L150-L165)：翻转缓冲 → 等上一轮 DMA → 填充 → 发起异步拷贝；拷贝失败或 record 失败走 [L166-L183](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/staging.rs#L166-L183) 的错误路径：

```rust
        let idx = self.next;
        self.next = (self.next + 1) % self.bufs.len();
        let buf = &mut self.bufs[idx];
        buf.dma_done
            .synchronize()
            .context("drain GLM5.2 pinned staging buffer")?;
        let stage = buf
            .pinned
            .as_mut_slice()
            .context("get GLM5.2 pinned staging slice")?;
        let stage = &mut stage[..src.len()];
        self.fill.fill(src, stage);
        let copied = unsafe { memcpy_htod_async(dst_at, stage, self.stream.cu_stream()) };
```

**⑧ 错误纪律：宁可 abort 也不悬空 DMA**。[src/weights/staging.rs:L186-L206](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/staging.rs#L186-L206)：Drop 时逐块 `dma_done.synchronize()`，失败就 `std::process::abort()`；`drain_or_abort` 同理。释放一块仍在 DMA 的 pinned 内存等于把设备侧目标地址变成未定义行为——这里用「进程死」换「静默内存损坏」。

#### 4.3.4 代码实践

**实践目标**：跑通 staging 模块里唯一的主机侧单测，理解 `fill` 的分界行为。

**操作步骤**：

```bash
cargo test --release -p pegainfer-glm52 --features glm52 --lib persistent_fill_pool
```

（需要 CUDA 工具链参与编译；该测试本身只用 rayon 线程池，不触碰 GPU 设备。`--release` 下构建耗时较长是正常的——kernels 的 CUDA 编译都在 build.rs 里完成。）

**需要观察的现象**：测试名 `persistent_fill_pool_copies_exact_bytes` 通过；它对 `[0, 1, 3, 4095, 4096, 1<<20, (3<<20)+17]` 七种长度断言 `fill` 后 dst 与 src 逐字节相等（[src/weights/staging.rs:L212-L223](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/staging.rs#L212-L223)）。

**预期结果**：全部通过。长度 0 提前返回；`1<<20` 恰好等于分界值但判断是严格小于（[src/weights/staging.rs:L61-L64](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/staging.rs#L61-L64)），所以 1 MiB 整仍走单线程拷贝；`(3<<20)+17` 走 4 线程切分，`div_ceil` 保证尾字节被覆盖。本环境未实际执行，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `upload` 要求目标 `CudaSlice` 必须分配在 stager 持有的同一个 stream 上（[src/weights/staging.rs:L121-L124](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/staging.rs#L121-L124)）？

答案：`memcpy_htod_async` 入队到 stager 的 stream；若目标缓冲属于另一条 stream，其所有权/生命周期记录就与真实使用的 stream 不一致，DMA 可能在目标仍被别的 stream 视为空闲时进行。跨 stream 共享必须显式 event 同步，这里直接禁止更稳。

**练习 2**：一个 100 MiB 的张量，staged 路径要几次 DMA、各多大？pageable 呢？

答案：staged：`chunks(32 MiB)` → 4 次（32 + 32 + 32 + 4 MiB），h2d_copies 加 4（[src/weights/staging.rs:L137-L143](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/staging.rs#L137-L143)）；pageable：1 次 100 MiB 完整拷贝，h2d_copies 加 1（[src/weights/load.rs:L68-L72](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/load.rs#L68-L72)）。

**练习 3**：`dma_done` 事件为什么选 `CU_EVENT_BLOCKING_SYNC` 而不是默认（忙等/自旋）语义？

答案：加载期 CPU 侧还有活干（mmap 下一分片、rayon 填另一块缓冲、其他 rank 的编排），阻塞式同步让等待线程睡眠让出核心；自旋只适合极短等待，在这里只会和填充线程抢 CPU。（构造点见 [src/weights/staging.rs:L100-L103](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/staging.rs#L100-L103)。）
